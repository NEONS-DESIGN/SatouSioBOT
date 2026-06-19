import asyncio
import itertools
import sys
from typing import Any

import aiohttp

from module.color import Color
import module.logger as _logger_module

# aiohttp セッションのシングルトン (TCPコネクションを使い回す)
_session: aiohttp.ClientSession | None = None
# TinyURL API エンドポイント
_TINYURL_API = "https://tinyurl.com/api-create.php?url={}"
# URL短縮をスキップする最大長 (これ以下なら短縮不要)
_SHORTEN_THRESHOLD = 100
# コンソールクリア用パディング幅
_CLEAR_WIDTH = 150

async def _get_session() -> aiohttp.ClientSession:
	"""モジュール全体で単一のaiohttpセッションを使い回す"""
	global _session
	if _session is None or _session.closed:
		_session = aiohttp.ClientSession()
	return _session

async def shorten_url(url: str) -> str:
	"""
	URLが_SHORTEN_THRESHOLD文字を超える場合にTinyURLで短縮する。
	- 短縮失敗時は元のURLをそのまま返す
	"""
	if not url or len(url) < _SHORTEN_THRESHOLD:
		return url
	try:
		session = await _get_session()
		async with session.get(_TINYURL_API.format(url), timeout=aiohttp.ClientTimeout(total=5)) as resp:
			if resp.status == 200:
				return await resp.text()
	except Exception as e:
		sys.stdout.write(f"{Color.RED}[URL短縮] 失敗: {e}{Color.RESET}\n")
		sys.stdout.flush()
	return url

async def close_http_session() -> None:
	"""共有aiohttpセッションを閉じる（終了時に呼ぶ）"""
	global _session
	if _session is not None and not _session.closed:
		await _session.close()
	_session = None

async def loading_spinner(task: asyncio.Task, message: str = "処理中") -> Any:
	"""
	コルーチンの完了を待つ間、コンソールにローディングアニメーションを表示する。
	- コルーチンが渡された場合は自動的にTaskに変換する
	- スピナー動作中は _logger_module.spinner_active = True にしてSpinnerAwareHandler にログ割り込み時の再描画を委譲する
	- 完了・キャンセル・例外の各ケースで適切な出力を行い、フラグをリセットする
	"""
	if asyncio.iscoroutine(task):
		task = asyncio.create_task(task)
	spinner = itertools.cycle(["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"])
	colors  = itertools.cycle([Color.RED, Color.YELLOW, Color.GREEN, Color.CYAN, Color.BLUE, Color.MAGENTA])
	clear_pad = " " * _CLEAR_WIDTH
	# スピナー動作中フラグを立てる
	_logger_module.spinner_active = True
	try:
		while not task.done():
			line = f"\r{next(colors)}[{next(spinner)}] {message}...{Color.RESET}"
			# SpinnerAwareHandler が再描画に使えるよう現在行を共有する
			_logger_module.spinner_line = line
			sys.stdout.write(line)
			sys.stdout.flush()
			await asyncio.sleep(0.2)
		# 完了: フラグを先に下げてからクリア→完了メッセージを出力する
		_logger_module.spinner_active = False
		_logger_module.spinner_line = ""
		sys.stdout.write(f"\r{clear_pad}\r{Color.GREEN}[✓] {message} 完了!{Color.RESET}\n")
		sys.stdout.flush()
		return task.result()
	except asyncio.CancelledError:
		_logger_module.spinner_active = False
		_logger_module.spinner_line = ""
		sys.stdout.write(f"\r{clear_pad}\r{Color.YELLOW}[!] {message} キャンセル{Color.RESET}\n")
		sys.stdout.flush()
		raise
	except Exception as e:
		_logger_module.spinner_active = False
		_logger_module.spinner_line = ""
		sys.stdout.write(f"\r{clear_pad}\r{Color.RED}[✗] {message} 失敗: {e}{Color.RESET}\n")
		sys.stdout.flush()
		raise

async def play_time(duration: int) -> str:
	"""
	秒数を "MM:SS" または "HH:MM:SS" 形式の文字列に変換する。
	- durationが0またはNoneの場合は "00:00" を返す
	"""
	if not duration:
		return "00:00"
	h, rem = divmod(int(duration), 3600)
	m, s = divmod(rem, 60)
	return f"{h:02}:{m:02}:{s:02}" if h > 0 else f"{m:02}:{s:02}"