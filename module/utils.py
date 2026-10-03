import asyncio
import itertools
import shutil
import sys
import unicodedata
from collections.abc import Awaitable
from typing import TypeVar

from module.color import Color
import module.logger as _logger_module

T = TypeVar("T")

# スピナーの描画間隔(秒)
_SPINNER_INTERVAL = 0.2
_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_SPINNER_COLORS = (Color.RED, Color.YELLOW, Color.GREEN, Color.CYAN, Color.BLUE, Color.MAGENTA)

# スピナー行の装飾 (メッセージの前の "[⠋] " と後ろの "...") の表示幅
_SPINNER_DECORATION_WIDTH = 7
# 行を切り詰めたときに末尾に付ける記号
_ELLIPSIS = "…"
# 端末の幅を取得できない場合 (出力先がファイル等) に使う幅
_FALLBACK_COLUMNS = 120

# 同時に動作しているスピナーの数 (並行抽出時に共有フラグを正しく管理するため)
_active_spinners = 0

def _display_width(text: str) -> int:
	"""コンソールでの表示幅を返す (全角などの幅広文字は 2 として数える)"""
	return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text)

def _fit_width(text: str, width: int) -> str:
	"""表示幅が width を超える場合、末尾を _ELLIPSIS に置き換えて width 以内に切り詰める"""
	if _display_width(text) <= width:
		return text
	limit = max(0, width - _display_width(_ELLIPSIS))
	used = 0
	for index, char in enumerate(text):
		used += _display_width(char)
		if used > limit:
			return text[:index] + _ELLIPSIS
	return text

def _spinner_message(message: str) -> str:
	"""
	スピナー行が端末の 1 行に収まるようメッセージを切り詰める。
	折り返すと、行頭へ戻る再描画が 2 行目の先頭から書くことになり、描画のたびに行が増えるため
	"""
	columns = shutil.get_terminal_size((_FALLBACK_COLUMNS, 0)).columns
	# 最終列まで書くと端末によっては次の行へ送られるため、1 列空ける
	return _fit_width(message, columns - _SPINNER_DECORATION_WIDTH - 1)

def _spinner_finished(line: str) -> None:
	"""スピナー1つの終了処理。全スピナーが止まったら共有フラグを下げ、結果行を出力する"""
	global _active_spinners
	_active_spinners -= 1
	if _active_spinners == 0:
		_logger_module.spinner_active = False
		_logger_module.spinner_line = ""
	sys.stdout.write(f"{_logger_module.ERASE_LINE}{line}{Color.RESET}\n")
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
			line = f"{_logger_module.ERASE_LINE}{next(colors)}[{next(frames)}] {_spinner_message(message)}...{Color.RESET}"
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
