"""
プロセスの CPU 優先度の設定。
再生中の音声 > 再生待ちの曲の抽出 > 次曲の先読み > それ以降の先読み の順に CPU を割り当てる。
再生中の音声のうち、送信スレッドはさらにスレッド優先度を最上位にする。
Windows の優先度クラス・スレッド優先度で実装しており、その他の OS では何もしない。(一般権限では優先度を上げ直せないため)
"""
import enum
import sys
import threading

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

# Priority に対応する Windows の優先度クラス (SetPriorityClass の引数)。
# LATER は後から上げられないため IDLE にしない (NEXT との順序は 1 曲ずつの先読みで守る)。PLAYBACK は OS の処理を妨げないよう REALTIME にしない
_WINDOWS_PRIORITY_CLASSES = {
	Priority.PLAYBACK: 0x00000080,  # HIGH_PRIORITY_CLASS
	Priority.CURRENT: 0x00000020,   # NORMAL_PRIORITY_CLASS
	Priority.NEXT: 0x00004000,      # BELOW_NORMAL_PRIORITY_CLASS
	Priority.LATER: 0x00004000,     # BELOW_NORMAL_PRIORITY_CLASS
}
# OpenProcess で優先度の変更に必要なアクセス権
_PROCESS_SET_INFORMATION = 0x0200
# 音声の送信スレッドに設定するスレッド優先度 (優先度クラスの中で最上位。HIGH クラスでは 15)
_THREAD_PRIORITY_TIME_CRITICAL = 15

# スレッドごとの優先度の設定結果 (スレッドが終われば消える)
_boosted_threads = threading.local()

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
	_kernel32.GetCurrentThread.restype = wintypes.HANDLE
	_kernel32.SetThreadPriority.argtypes = (wintypes.HANDLE, ctypes.c_int)
	_kernel32.SetThreadPriority.restype = wintypes.BOOL

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

def boost_playback_thread() -> bool:
	"""
	呼び出したスレッド (音声の送信スレッド) をプロセス内で最優先にする。成功時 True、Windows 以外・失敗時は False (例外は出さない)。
	毎フレーム呼ばれても、設定はスレッドごとに初回だけ行い、以降は初回の結果を返す
	"""
	result = getattr(_boosted_threads, "result", None)
	if result is None:
		result = sys.platform == "win32" and bool(
			_kernel32.SetThreadPriority(_kernel32.GetCurrentThread(), _THREAD_PRIORITY_TIME_CRITICAL)
		)
		_boosted_threads.result = result
	return result
