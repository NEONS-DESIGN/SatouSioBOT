import asyncio
import itertools
import sys
from collections.abc import Awaitable
from typing import TypeVar

from module.color import Color
import module.logger as _logger_module

T = TypeVar("T")

# コンソールクリア用パディング幅
_CLEAR_WIDTH = 150
# スピナーの描画間隔(秒)
_SPINNER_INTERVAL = 0.2
_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_SPINNER_COLORS = (Color.RED, Color.YELLOW, Color.GREEN, Color.CYAN, Color.BLUE, Color.MAGENTA)

# 同時に動作しているスピナーの数 (並行抽出時に共有フラグを正しく管理するため)
_active_spinners = 0

def _spinner_finished(line: str) -> None:
	"""スピナー1つの終了処理。全スピナーが止まったら共有フラグを下げ、結果行を出力する"""
	global _active_spinners
	_active_spinners -= 1
	if _active_spinners == 0:
		_logger_module.spinner_active = False
		_logger_module.spinner_line = ""
	sys.stdout.write(f"\r{' ' * _CLEAR_WIDTH}\r{line}{Color.RESET}\n")
	sys.stdout.flush()

async def loading_spinner(awaitable: Awaitable[T], message: str = "処理中") -> T:
	"""
	awaitable の完了を待つ間、コンソールにローディングアニメーションを表示して結果を返す。
	- 動作中は logger.spinner_active / spinner_line を更新し、SpinnerAwareHandler にログ割り込み時の再描画を委譲する
	- 完了・キャンセル・例外の各ケースで結果行を出力する (例外は再送出する)
	"""
	global _active_spinners
	task = asyncio.ensure_future(awaitable)
	frames = itertools.cycle(_SPINNER_FRAMES)
	colors = itertools.cycle(_SPINNER_COLORS)
	_active_spinners += 1
	_logger_module.spinner_active = True
	try:
		while not task.done():
			line = f"\r{next(colors)}[{next(frames)}] {message}...{Color.RESET}"
			# SpinnerAwareHandler が再描画に使えるよう現在行を共有する
			_logger_module.spinner_line = line
			sys.stdout.write(line)
			sys.stdout.flush()
			# タスク完了か描画間隔のどちらか早い方で次へ進む (完了を最大 0.2 秒待たせない)
			await asyncio.wait((task,), timeout=_SPINNER_INTERVAL)
		result = task.result()
	except asyncio.CancelledError:
		task.cancel()
		_spinner_finished(f"{Color.YELLOW}[!] {message} キャンセル")
		raise
	except Exception as e:
		_spinner_finished(f"{Color.RED}[✗] {message} 失敗: {e}")
		raise
	_spinner_finished(f"{Color.GREEN}[✓] {message} 完了!")
	return result

def format_duration(duration: float | None) -> str:
	"""秒数を "MM:SS" または "HH:MM:SS" 形式に変換する。0 / None は "00:00" """
	if not duration:
		return "00:00"
	h, rem = divmod(int(duration), 3600)
	m, s = divmod(rem, 60)
	return f"{h:02}:{m:02}:{s:02}" if h > 0 else f"{m:02}:{s:02}"
