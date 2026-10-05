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

# 抽出モード: meta=メタデータのみ / stream=ストリームURLまで / stream_fallback=stream 失敗時の予備
_EAGER_MODES = ("meta", "stream")

# 本抽出で省略する YouTube の初期データ (/next API) を表す player_skip の値
_SKIP_INITIAL_DATA = "initial_data"

# 親プロセスの終了を検知してワーカーを終了させるときの終了コード
_ORPHAN_EXIT_CODE = 1

# yt-dlp のキャッシュのうち、署名・n チャレンジの解決 (Deno) に関するものを置く区画と、前処理済みプレイヤーのファイル名の接頭辞
_SOLVER_CACHE_SECTION = "challenge-solver"
_PLAYER_CACHE_PREFIX = "player,3A"
# 前処理済みプレイヤーのキャッシュを残す版数 (1 版あたり約 4MB。YouTube がプレイヤーを更新するたびに増える)
PLAYER_CACHE_KEEP = 2
# ワーカー起動時に接続を確立しておくための軽い URL (最初の HTTP リクエストに約 0.9s の固定費があるため)
WARMUP_URL = "https://www.youtube.com/generate_204"

# ミックス (YouTube Music のラジオ等) の再生リスト ID の接頭辞。動画 ID 無しでは YouTube が開けない (This playlist type is unviewable)
_MIX_PLAYLIST_PREFIX = "RD"
# ミックスの URL として扱う YouTube のホスト
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
	yt-dlp の前処理済みプレイヤーのキャッシュを有効にする。差し替え先が見つからない場合は False を返す。
	既定では無効で、抽出のたびに Deno がプレイヤー JS (約 3MB) を前処理し直す (本番機で約 1.5s)。
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
	try:
		cache_dir = pathlib.Path(ydl.cache._get_root_dir()) / _SOLVER_CACHE_SECTION
		files = sorted(cache_dir.glob(f"{_PLAYER_CACHE_PREFIX}*"), key=lambda path: path.stat().st_mtime, reverse=True)
	except (AttributeError, OSError):
		return
	for path in files[PLAYER_CACHE_KEEP:]:
		# 他のワーカーが同時に消した場合も無視する
		path.unlink(missing_ok=True)

def _warm_up_connection(ydl: Any) -> None:
	"""YouTube への接続を確立しておく (接続は YoutubeDL ごとに使い回される)。失敗しても抽出時に接続し直すだけなので無視する"""
	try:
		with ydl.urlopen(WARMUP_URL) as response:
			response.read()
	except Exception:
		pass

def _stream_options() -> dict[str, Any]:
	"""
	本抽出のオプションを返す。youtube_premium が明示されていれば、初期データ (/next API) の取得を省いたものにする。
	初期データは再生に不要だが、yt-dlp は Premium 判定にのみ使うため、先に判定を設定値で固定しておく。
	固定できなければ高音質フォーマットを選ばなくなるため、省略しない。
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

def _build_options() -> dict[str, dict[str, Any]]:
	"""抽出モードごとの YoutubeDL オプションを返す"""
	from yt_dlp.networking.impersonate import ImpersonateTarget
	return {
		"meta": FAST_META_OPTIONS,
		"stream": _stream_options(),
		"stream_fallback": {**STREAM_FALLBACK_OPTIONS, "impersonate": ImpersonateTarget.from_str(STREAM_FALLBACK_IMPERSONATE)},
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
	子プロセス起動時の初期化。親の監視を開始し、よく使うモードの YoutubeDL の生成・Cookie の読み込み・接続の確立を済ませて初回抽出を速くする。
	- 前処理済みプレイヤーのキャッシュを有効にし、古い版を掃除する
	- JS チャレンジの解読を常駐 Deno で行うプロバイダを登録し、Deno の起動と最新のプレイヤーの読み込みを済ませておく
	- 予備設定 (stream_fallback) はめったに使わないため、初めて必要になったときに生成する
	- 起動方法に左右されないよう、通常の優先度 (Priority.CURRENT) から始める
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
		_warm_up_connection(ydl)
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
	try:
		parsed = urllib.parse.urlparse(query)
	except ValueError:
		return None
	if parsed.hostname not in _YOUTUBE_HOSTS:
		return None
	params = urllib.parse.parse_qs(parsed.query)
	playlist_id = next(iter(params.get("list", ())), "")
	if not playlist_id.startswith(_MIX_PLAYLIST_PREFIX) or params.get("v"):
		return None
	return playlist_id

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

def _extract(query: str, is_fast: bool) -> dict:
	"""extract の本体。yt-dlp の例外をそのまま送出する"""
	from yt_dlp.utils import DownloadError
	query = _resolve_mix_url(query)
	used_fallback = False
	if is_fast:
		info = _get_ydl("meta").extract_info(query, download=False)
	else:
		try:
			info = _get_ydl("stream").extract_info(query, download=False)
		except DownloadError:
			info = _get_ydl("stream_fallback").extract_info(query, download=False)
			used_fallback = True
	if not info:
		raise ValueError(f"情報を取得できませんでした: {query}")
	result = _slim(info)
	if used_fallback:
		result[FALLBACK_FLAG] = True
	return result
