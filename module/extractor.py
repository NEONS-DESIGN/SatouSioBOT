"""
yt-dlp による情報抽出。ProcessPoolExecutor の子プロセス上で実行される。
Bot 本体のプロセスも関数の参照のためにこのモジュールを import するため、yt-dlp は子プロセスで呼ばれる関数の中で import する。
子プロセスでの import を軽くするため、discord など Bot 本体側のモジュールには依存しない。
"""
import multiprocessing
import multiprocessing.connection
import os
import pathlib
import threading
import urllib.parse
from typing import Any

from module.options import (
	FAST_META_OPTIONS, STREAM_FALLBACK_IMPERSONATE, STREAM_FALLBACK_OPTIONS, STREAM_OPTIONS, app_config,
)
from module.priority import Priority, set_priority

# 予備設定で抽出した場合に結果へ付与するフラグのキー
FALLBACK_FLAG = "_used_fallback"

# 親プロセスへ返すキー (formats 等の巨大なデータをプロセス間で受け渡さないため)
_KEEP_KEYS = ("id", "title", "url", "webpage_url", "original_url", "duration", "thumbnail", "http_headers")

# 抽出モード: meta=メタデータのみ / stream=ストリームURLまで / stream_fallback=stream 失敗時の予備。起動時に生成するモード
_EAGER_MODES = ("meta", "stream")

# 本抽出で省略する YouTube の初期データ (/next API) を表す player_skip の値
_SKIP_INITIAL_DATA = "initial_data"

# 親プロセスの終了を検知してワーカーを終了させるときの終了コード
_ORPHAN_EXIT_CODE = 1

# yt-dlp のキャッシュで前処理済みプレイヤーを保存するキーの接頭辞 (キーは "player:<プレイヤーの URL>")
PLAYER_CACHE_KEY_PREFIX = "player:"
# 前処理済みプレイヤーのキャッシュを残す版数 (1 版あたり約 4MB。YouTube がプレイヤーを更新するたびに増える)
PLAYER_CACHE_KEEP = 2
# ワーカー起動時に接続を確立しておく軽い URL (モードごとに抽出で使うホスト。web_music の API は music.youtube.com)
_WARMUP_URLS = {
	"meta": ("https://www.youtube.com/generate_204",),
	"stream": ("https://www.youtube.com/generate_204", "https://music.youtube.com/generate_204"),
}

# ミックス (YouTube Music のラジオ等) の再生リスト ID の接頭辞。動画 ID 無しでは YouTube が開けない (This playlist type is unviewable)
MIX_PLAYLIST_PREFIX = "RD"
# YouTube のホスト (再生リストの URL の判定に使う)
_YOUTUBE_HOSTS = frozenset(("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"))
# YouTube の /next API (web クライアント) の応答で、ミックスの曲の動画 ID がある場所
_MIX_VIDEO_ID_PATH = ("contents", "twoColumnWatchNextResults", "playlist", "playlist", "contents", ..., "playlistPanelVideoRenderer", "videoId", {str})
# 先頭の曲を付けたミックスの URL
_MIX_WATCH_URL = "https://www.youtube.com/watch?{query}"

# 子プロセスごとのモード別オプションと、使い回す YoutubeDL インスタンス (init_worker で設定する)
_options: dict[str, dict[str, Any]] = {}
_instances: dict[str, Any] = {}
# 登録できた常駐 Deno のプロバイダのモジュール (登録前・失敗時は None)
_resident_jsc: Any = None

def youtube_params(url: str) -> dict[str, list[str]] | None:
	"""url が YouTube の URL ならクエリのパラメータを返す (それ以外・解釈できない URL は None)"""
	try:
		parsed = urllib.parse.urlsplit(url)
	except ValueError:
		return None
	if parsed.hostname not in _YOUTUBE_HOSTS:
		return None
	return urllib.parse.parse_qs(parsed.query)

def player_cache_files(ydl: Any) -> list[pathlib.Path]:
	"""yt-dlp のキャッシュにある前処理済みプレイヤーのファイルを、更新日時の新しい順に返す (読めなければ空)"""
	# yt-dlp はキーを URL エンコードし、% を , に置き換えたファイル名で保存する
	prefix = urllib.parse.quote(PLAYER_CACHE_KEY_PREFIX, safe="").replace("%", ",")
	try:
		from yt_dlp.extractor.youtube.jsc._builtin.ejs import EJSBaseJCP
		cache_dir = pathlib.Path(ydl.cache._get_root_dir()) / EJSBaseJCP._CACHE_SECTION
		return sorted(cache_dir.glob(f"{prefix}*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
	except (ImportError, AttributeError, OSError):
		return []

def _exit_when_parent_dies(sentinel: int) -> None:
	"""親プロセスの終了を待ち、終了したらこのワーカーを即時終了する (監視スレッドで実行)"""
	multiprocessing.connection.wait([sentinel])
	# 親が close() を経ずに落ちた場合、ワーカーは自分では終了しないため強制的に終了する
	os._exit(_ORPHAN_EXIT_CODE)

def _start_parent_watchdog() -> None:
	"""親プロセスの監視スレッドを起動する (親が無い場合は何もしない)"""
	parent = multiprocessing.parent_process()
	if parent is None:
		return
	threading.Thread(target=_exit_when_parent_dies, args=(parent.sentinel,), name="parent-watchdog", daemon=True).start()

def _pin_premium_status(is_premium: bool) -> bool:
	"""yt-dlp の YouTube Premium 判定を設定値で固定する。差し替え先が見つからない場合は False を返す"""
	try:
		from yt_dlp.extractor.youtube import YoutubeIE
	except ImportError:
		return False
	if not callable(getattr(YoutubeIE, "_is_premium_subscriber", None)):
		return False
	def _is_premium_subscriber(self, initial_data) -> bool:
		return is_premium and self.is_authenticated
	YoutubeIE._is_premium_subscriber = _is_premium_subscriber
	return True

def _enable_player_cache() -> bool:
	"""
	yt-dlp の前処理済みプレイヤーのキャッシュを有効にする (既定では抽出のたびに Deno が前処理し直す)。差し替え先が無ければ False。
	yt-dlp は古い版を消さないため、_prune_player_cache で掃除する
	"""
	try:
		from yt_dlp.extractor.youtube.jsc._builtin.ejs import EJSBaseJCP
	except ImportError:
		return False
	if not hasattr(EJSBaseJCP, "_ENABLE_PREPROCESSED_PLAYER_CACHE"):
		return False
	EJSBaseJCP._ENABLE_PREPROCESSED_PLAYER_CACHE = True
	return True

def _register_resident_jsc() -> bool:
	"""
	常駐 Deno の JS チャレンジプロバイダを yt-dlp に登録する。登録できなければ False (標準の Deno プロバイダで解く)。
	YoutubeDL が最初の抽出でプロバイダの一覧を作るため、抽出より前に呼ぶ
	"""
	global _resident_jsc
	try:
		# import でプロバイダが登録される
		from module import resident_jsc
	except Exception:
		# yt-dlp の内部構成が変わった・登録済み など。ワーカーの起動は止めない
		return False
	_resident_jsc = resident_jsc
	return True

def _apply_resident_jsc_priority(priority: Priority) -> None:
	"""常駐 Deno (登録済みで起動していれば) の CPU 優先度をワーカーに合わせる"""
	if _resident_jsc is not None:
		_resident_jsc.apply_priority(priority)

def _prune_player_cache(ydl: Any) -> None:
	"""前処理済みプレイヤーのキャッシュを、更新日時の新しい PLAYER_CACHE_KEEP 版だけ残して削除する。失敗しても無視する"""
	for path in player_cache_files(ydl)[PLAYER_CACHE_KEEP:]:
		try:
			# 他のワーカーが同時に消した場合も無視する
			path.unlink(missing_ok=True)
		except OSError:
			pass

def _warm_up_connection(ydl: Any, url: str) -> None:
	"""url のホストへの接続を確立しておく (接続は YoutubeDL ごとに使い回される)。失敗しても抽出時に接続し直すだけなので無視する"""
	try:
		with ydl.urlopen(url) as response:
			response.read()
	except Exception:
		pass

def _stream_options() -> dict[str, Any]:
	"""
	本抽出のオプションを返す。youtube_premium が明示されていれば、初期データ (/next API) の取得を省いたものにする。
	初期データは Premium 判定にしか使われないため先に判定を固定する。固定できなければ高音質を選ばなくなるため省略しない
	"""
	is_premium = app_config.YOUTUBE_PREMIUM
	if is_premium is None or not _pin_premium_status(is_premium):
		return STREAM_OPTIONS
	extractor_args = STREAM_OPTIONS["extractor_args"]
	youtube_args = extractor_args["youtube"]
	return {
		**STREAM_OPTIONS,
		"extractor_args": {
			**extractor_args,
			"youtube": {**youtube_args, "player_skip": [*youtube_args["player_skip"], _SKIP_INITIAL_DATA]},
		},
	}

class _SilentLogger:
	"""yt-dlp の出力を捨てるロガー。失敗は例外として親プロセスへ送り、Bot のログに出すため、コンソールへの重複した出力を止める"""
	def debug(self, message: str) -> None: ...
	def info(self, message: str) -> None: ...
	def warning(self, message: str) -> None: ...
	def error(self, message: str) -> None: ...

def _build_options() -> dict[str, dict[str, Any]]:
	"""抽出モードごとの YoutubeDL オプションを返す"""
	from yt_dlp.networking.impersonate import ImpersonateTarget
	logger = _SilentLogger()
	return {
		"meta": {**FAST_META_OPTIONS, "logger": logger},
		"stream": {**_stream_options(), "logger": logger},
		"stream_fallback": {
			**STREAM_FALLBACK_OPTIONS, "logger": logger, "impersonate": ImpersonateTarget.from_str(STREAM_FALLBACK_IMPERSONATE),
		},
	}

def _get_ydl(mode: str) -> Any:
	"""mode に対応する YoutubeDL を返す (未生成なら生成する)"""
	ydl = _instances.get(mode)
	if ydl is None:
		from yt_dlp import YoutubeDL
		ydl = _instances[mode] = YoutubeDL(_options[mode])
	return ydl

def init_worker() -> None:
	"""
	子プロセスの初期化。親の監視・プロバイダ登録のほか、よく使うモードの YoutubeDL の生成・Cookie の読み込み・接続の確立と、
	常駐 Deno への最新のプレイヤーの読み込みを済ませて初回の抽出を速くする。優先度は起動方法に左右されないよう CURRENT から始める
	"""
	set_priority(Priority.CURRENT)
	_start_parent_watchdog()
	_enable_player_cache()
	_register_resident_jsc()
	_options.update(_build_options())
	for mode in _EAGER_MODES:
		ydl = _get_ydl(mode)
		try:
			# cookiejar は初回アクセス時に読み込まれるため、ここで読み込ませておく
			_ = ydl.cookiejar
		except Exception:
			# 読み込みに失敗しても抽出時に再試行されるため、起動は継続する
			pass
		for url in _WARMUP_URLS[mode]:
			_warm_up_connection(ydl, url)
	_prune_player_cache(_get_ydl("stream"))
	if _resident_jsc is not None:
		_resident_jsc.warm_up(_get_ydl("stream"))

def ping() -> bool:
	"""ワーカープロセスを起動させるための空処理"""
	return True

def _slim(info: dict) -> dict:
	"""抽出結果から必要なキーだけを残した辞書を返す (entries も再帰的に処理する)"""
	result = {key: info[key] for key in _KEEP_KEYS if info.get(key) is not None}
	if "thumbnail" not in result and info.get("thumbnails"):
		result["thumbnail"] = info["thumbnails"][-1].get("url")
	if info.get("entries") is not None:
		result["entries"] = [_slim(entry) for entry in info["entries"] if entry]
	return result

class ExtractionError(Exception):
	"""抽出失敗。yt-dlp の例外は HTTP 応答やトレースバックを抱えて親プロセスへ送れないため、メッセージだけを持たせて送る"""

def extract(query: str, is_fast: bool, priority: Priority = Priority.CURRENT) -> dict:
	"""
	query の情報を取得して縮小した辞書を返す。
	- is_fast=True : メタデータのみ (FAST_META_OPTIONS)
	- is_fast=False: ストリームURL込み。失敗時は予備設定で再試行し、FALLBACK_FLAG を付与する
	- priority: このワーカー (と常駐 Deno・yt-dlp が起動する Deno) の CPU 優先度。抽出ごとに設定し直す
	- 失敗時は ExtractionError を送出する
	"""
	set_priority(priority)
	_apply_resident_jsc_priority(priority)
	try:
		return _extract(query, is_fast)
	except Exception as e:
		raise ExtractionError(str(e)) from None

def _mix_playlist_id(query: str) -> str | None:
	"""query が動画 ID の無い YouTube のミックスの URL なら、その再生リスト ID を返す (それ以外は None)"""
	params = youtube_params(query)
	if params is None or params.get("v"):
		return None
	playlist_id = next(iter(params.get("list", ())), "")
	return playlist_id if playlist_id.startswith(MIX_PLAYLIST_PREFIX) else None

def _resolve_mix_url(query: str) -> str:
	"""
	動画 ID の無いミックスの URL (music.youtube.com/playlist?list=RD... 等) を、先頭の曲の動画 ID を付けた watch URL にして返す。
	yt-dlp はミックスを watch ページからしか取得できないため、/next API で先頭の曲を求める。
	該当しない・先頭の曲を取得できない場合は query をそのまま返す (yt-dlp の本来のエラーになる)
	"""
	playlist_id = _mix_playlist_id(query)
	if playlist_id is None:
		return query
	from yt_dlp.utils import traverse_obj
	try:
		response = _get_ydl("meta").get_info_extractor("YoutubeTab")._extract_response(
			item_id=playlist_id, query={"playlistId": playlist_id}, ep="next", check_get_keys="contents",
		)
	except Exception:
		# 通信失敗・yt-dlp の内部構成の変更など。元の URL で抽出させる
		return query
	video_id = traverse_obj(response, _MIX_VIDEO_ID_PATH, get_all=False)
	if not video_id:
		return query
	return _MIX_WATCH_URL.format(query=urllib.parse.urlencode({"v": video_id, "list": playlist_id}))

def _extract_stream(query: str) -> dict | None:
	"""本抽出を行う。高速設定で失敗したら予備設定で取り直し、結果に FALLBACK_FLAG を付ける (両方失敗したら両方の理由を載せて送出する)"""
	from yt_dlp.utils import DownloadError
	try:
		return _get_ydl("stream").extract_info(query, download=False)
	except DownloadError as first_error:
		try:
			info = _get_ydl("stream_fallback").extract_info(query, download=False)
		except Exception as fallback_error:
			# 原因の分類 (errors.py) に高速設定の理由も使えるよう、両方の文言を残す
			raise ExtractionError(f"{first_error} / 予備設定: {fallback_error}") from None
	if info:
		info[FALLBACK_FLAG] = True
	return info

def _extract(query: str, is_fast: bool) -> dict:
	"""extract の本体。yt-dlp の例外をそのまま送出する"""
	query = _resolve_mix_url(query)
	info = _get_ydl("meta").extract_info(query, download=False) if is_fast else _extract_stream(query)
	if not info:
		raise ValueError(f"情報を取得できませんでした: {query}")
	result = _slim(info)
	if info.get(FALLBACK_FLAG):
		result[FALLBACK_FLAG] = True
	return result
