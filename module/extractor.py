"""
yt-dlp による情報抽出。ProcessPoolExecutor の子プロセス上で実行される。
子プロセスでの import を軽くするため、discord など Bot 本体側のモジュールには依存しない。
"""
import multiprocessing
import multiprocessing.connection
import os
import threading
from typing import Any

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from module.options import FAST_META_OPTIONS, STREAM_FALLBACK_OPTIONS, STREAM_OPTIONS

# 予備設定で抽出した場合に結果へ付与するフラグのキー
FALLBACK_FLAG = "_used_fallback"

# 親プロセスへ返すキー (formats 等の巨大なデータをプロセス間で受け渡さないため)
_KEEP_KEYS = ("id", "title", "url", "webpage_url", "original_url", "duration", "thumbnail", "http_headers")

_OPTIONS: dict[str, dict[str, Any]] = {
	"meta": FAST_META_OPTIONS,
	"stream": STREAM_OPTIONS,
	"stream_fallback": STREAM_FALLBACK_OPTIONS,
}

# 親プロセスの終了を検知してワーカーを終了させるときの終了コード
_ORPHAN_EXIT_CODE = 1

# 子プロセスごとに使い回す YoutubeDL インスタンス
_instances: dict[str, YoutubeDL] = {}

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

def _get_ydl(mode: str) -> YoutubeDL:
	"""mode に対応する YoutubeDL を返す (未生成なら生成する)"""
	ydl = _instances.get(mode)
	if ydl is None:
		ydl = _instances[mode] = YoutubeDL(_OPTIONS[mode])
	return ydl

def init_worker() -> None:
	"""子プロセス起動時の初期化。親の監視を開始し、YoutubeDL の生成と Cookie の読み込みを済ませて初回抽出を速くする"""
	_start_parent_watchdog()
	for mode in _OPTIONS:
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

def extract(query: str, is_fast: bool) -> dict:
	"""
	query の情報を取得して縮小した辞書を返す。
	- is_fast=True : メタデータのみ (FAST_META_OPTIONS)
	- is_fast=False: ストリームURL込み。失敗時は予備設定で再試行し、FALLBACK_FLAG を付与する
	"""
	if is_fast:
		info = _get_ydl("meta").extract_info(query, download=False)
		used_fallback = False
	else:
		try:
			info = _get_ydl("stream").extract_info(query, download=False)
			used_fallback = False
		except DownloadError:
			info = _get_ydl("stream_fallback").extract_info(query, download=False)
			used_fallback = True
	if not info:
		raise ValueError(f"情報を取得できませんでした: {query}")
	result = _slim(info)
	if used_fallback:
		result[FALLBACK_FLAG] = True
	return result
