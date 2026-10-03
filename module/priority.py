"""
プロセスの CPU 優先度の設定。
再生中の音声 > 再生待ちの曲の抽出 > 次曲の先読み > それ以降の先読み の順に CPU を割り当てる。
Windows の優先度クラスで実装しており、その他の OS では何もしない。(一般権限では優先度を上げ直せないため)
"""
import enum
import sys

class Priority(enum.IntEnum):
	"""処理の優先度。値が小さいほど優先する"""
	# 再生中の音声 (Bot 本体の送信スレッド・FFmpeg)
	PLAYBACK = 0
	# 再生待ちの曲の抽出 (利用者が完了を待っている処理)
	CURRENT = 1
	# 次に再生する曲の先読み
	NEXT = 2
	# それ以降の曲の先読み
	LATER = 3

# Priority に対応する Windows の優先度クラス (SetPriorityClass の引数)
# LATER は IDLE にしない。始まった抽出の優先度は後から上げられず、スキップで現在の曲になったときに
# CPU の空き待ちで止まりうるため。NEXT との順序は先読みを1曲ずつ順に行うことで守る
_WINDOWS_PRIORITY_CLASSES = {
	Priority.PLAYBACK: 0x00008000,  # ABOVE_NORMAL_PRIORITY_CLASS
	Priority.CURRENT: 0x00000020,   # NORMAL_PRIORITY_CLASS
	Priority.NEXT: 0x00004000,      # BELOW_NORMAL_PRIORITY_CLASS
	Priority.LATER: 0x00004000,     # BELOW_NORMAL_PRIORITY_CLASS
}
# OpenProcess で優先度の変更に必要なアクセス権
_PROCESS_SET_INFORMATION = 0x0200

if sys.platform == "win32":
	import ctypes
	from ctypes import wintypes

	_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
	_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
	_kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
	_kernel32.OpenProcess.restype = wintypes.HANDLE
	_kernel32.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
	_kernel32.SetPriorityClass.restype = wintypes.BOOL
	_kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
	_kernel32.CloseHandle.restype = wintypes.BOOL

def set_priority(priority: Priority, pid: int | None = None) -> bool:
	"""
	プロセスの CPU 優先度を設定する。成功時 True、Windows 以外・失敗時は False (例外は出さない)。
	- pid を省略すると自プロセスを対象にする
	- Windows では BELOW_NORMAL / IDLE のプロセスから起動した子プロセスは同じ優先度を引き継ぐ
	"""
	if sys.platform != "win32":
		return False
	priority_class = _WINDOWS_PRIORITY_CLASSES[priority]
	if pid is None:
		return bool(_kernel32.SetPriorityClass(_kernel32.GetCurrentProcess(), priority_class))
	handle = _kernel32.OpenProcess(_PROCESS_SET_INFORMATION, False, pid)
	if not handle:
		return False
	try:
		return bool(_kernel32.SetPriorityClass(handle, priority_class))
	finally:
		_kernel32.CloseHandle(handle)
