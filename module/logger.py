import logging
import sys
import os
import re
from logging.handlers import TimedRotatingFileHandler

# ==========================================
# スピナー状態の共有変数 (utils.py と連携)
# spinner_active  : スピナーが現在表示中かどうか
# spinner_line    : スピナーが最後に描画した行の文字列 (再描画用)
# ==========================================
spinner_active: bool = False
spinner_line: str = ""

# コンソールをクリアするためのパディング幅
_CLEAR_WIDTH = 150

class SpinnerAwareHandler(logging.StreamHandler):
	"""
	コンソール出力用のハンドラ。
	スピナー動作中にログが割り込む場合、以下の順で出力する:
		1. \r + スペースでスピナー行を消す
		2. ログ行を出力する
		3. スピナー行を再描画する (改行なし)
	これによりスピナーとログが混在せずに表示される。
	"""
	def emit(self, record: logging.LogRecord) -> None:
		try:
			msg = self.format(record)
			stream = self.stream
			if spinner_active and spinner_line:
				# スピナー行を消してからログを出力し、スピナーを再描画する
				clear = " " * _CLEAR_WIDTH
				stream.write(f"\r{clear}\r{msg}\n")
				stream.write(spinner_line)
			else:
				stream.write(f"{msg}\n")
			stream.flush()
		except Exception:
			self.handleError(record)

class ConsoleFilter(logging.Filter):
	"""
	discordライブラリが発信するINFO以下のログをコンソールから除外する。
	ERRORやWARNINGは通過させる。
	"""
	def filter(self, record: logging.LogRecord) -> bool:
		if record.name.startswith("discord") and record.levelno < logging.WARNING:
			return False
		return True

def setup_daily_logger() -> None:
	"""
	logフォルダにデイリーローテーションするファイルハンドラと、
	SpinnerAwareHandlerによるコンソールハンドラを設定する。
	"""
	log_dir = "log"
	if not os.path.exists(log_dir):
		os.makedirs(log_dir)
	root = logging.getLogger()
	root.setLevel(logging.INFO)
	# 重複登録を防ぐため既存ハンドラをクリアする
	if root.hasHandlers():
		root.handlers.clear()
	formatter = logging.Formatter(
		fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
		datefmt="%Y-%m-%d %H:%M:%S",
	)
	# ファイルハンドラ: 全ログを日付ごとのファイルに保存する
	file_handler = TimedRotatingFileHandler(
		filename=os.path.join(log_dir, "bot.log"),
		when="midnight",
		interval=1,
		backupCount=30,
		encoding="utf-8",
	)
	file_handler.suffix = "%Y_%m_%d.log"
	file_handler.extMatch = re.compile(r"^\d{4}_\d{2}_\d{2}\.log$")
	file_handler.setFormatter(formatter)
	file_handler.setLevel(logging.INFO)
	# コンソールハンドラ: スピナー対応版、discordのINFO以下は除外する
	console_handler = SpinnerAwareHandler(sys.stdout)
	console_handler.setFormatter(formatter)
	console_handler.setLevel(logging.INFO)
	console_handler.addFilter(ConsoleFilter())
	root.addHandler(file_handler)
	root.addHandler(console_handler)

def get_bot_logger(name: str = "MusicBot") -> logging.Logger:
	"""Bot専用のロガーインスタンスを取得する"""
	return logging.getLogger(name)