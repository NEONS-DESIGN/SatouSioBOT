"""
yt-dlp による情報抽出。ProcessPoolExecutor の子プロセス上で実行される。
Bot 本体のプロセスも関数の参照のためにこのモジュールを import するため、yt-dlp は子プロセスで呼ばれる関数の中で import する。
子プロセスでの import を軽くするため、discord など Bot 本体側のモジュールには依存しない。
"""
import multiprocessing
import multiprocessing.connection
import os
import threading
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

# 子プロセスごとのモード別オプションと、使い回す YoutubeDL インスタンス (init_worker で設定する)
_options: dict[str, dict[str, Any]] = {}
_instances: dict[str, Any] = {}

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
	子プロセス起動時の初期化。親の監視を開始し、よく使うモードの YoutubeDL の生成と Cookie の読み込みを済ませて初回抽出を速くする。
	- 予備設定 (stream_fallback) はめったに使わないため、初めて必要になったときに生成する
	- 起動方法に左右されないよう、通常の優先度 (Priority.CURRENT) から始める
	"""
	set_priority(Priority.CURRENT)
	_start_parent_watchdog()
	_options.update(_build_options())
	for mode in _EAGER_MODES:
		ydl = _get_ydl(mode)
		try:
			# cookiejar は初回アクセス時に読み込まれるため、ここで読み込ませておく
			_ = ydl.cookiejar
		except Exception:
			# 読み込みに失敗しても抽出時に再試行されるため、起動は継続する
			pass

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
	- priority: このワーカー (と yt-dlp が起動する Deno) の CPU 優先度。抽出ごとに設定し直す
	- 失敗時は ExtractionError を送出する
	"""
	set_priority(priority)
	try:
		return _extract(query, is_fast)
	except Exception as e:
		raise ExtractionError(str(e)) from None

def _extract(query: str, is_fast: bool) -> dict:
	"""extract の本体。yt-dlp の例外をそのまま送出する"""
	from yt_dlp.utils import DownloadError
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
