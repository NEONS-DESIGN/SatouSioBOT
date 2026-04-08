import logging
from logging.handlers import TimedRotatingFileHandler
import os
import re
import sys

class ConsoleFilter(logging.Filter):
	"""
	コンソール出力用のフィルター。
	discordライブラリが発信するINFO以下のログを除外する。
	（ERRORやWARNINGは通過させる）
	"""
	def filter(self, record):
		# ロガー名が "discord" から始まり、かつ深刻度が WARNING(30) 未満なら除外
		if record.name.startswith("discord") and record.levelno < logging.WARNING:
			return False
		return True

def setup_daily_logger():
	"""
	logフォルダーを作成し、毎日深夜0時にログファイルをローテーションする。
	"""
	log_dir = "log"
	if not os.path.exists(log_dir):
		os.makedirs(log_dir)
	root_logger = logging.getLogger()
	root_logger.setLevel(logging.INFO)
	# 重複出力を防ぐため、既存のハンドラーがあれば一度クリアする
	if root_logger.hasHandlers():
		root_logger.handlers.clear()
	formatter = logging.Formatter(
		fmt='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
		datefmt='%Y-%m-%d %H:%M:%S'
	)
	# ==========================================
	# 1. ファイル出力用ハンドラー (すべてのログを保存)
	# ==========================================
	file_handler = TimedRotatingFileHandler(
		filename=os.path.join(log_dir, "bot.log"),
		when="midnight",
		interval=1,
		backupCount=30,
		encoding="utf-8"
	)
	file_handler.suffix = "%Y_%m_%d.log"
	file_handler.extMatch = re.compile(r"^\d{4}_\d{2}_\d{2}\.log$")
	file_handler.setFormatter(formatter)
	file_handler.setLevel(logging.INFO)
	# ==========================================
	# 2. コンソール出力用ハンドラー (エラーとBotのINFOのみ表示)
	# ==========================================
	console_handler = logging.StreamHandler(sys.stdout)
	console_handler.setFormatter(formatter)
	console_handler.setLevel(logging.INFO)
	console_handler.addFilter(ConsoleFilter()) # ここで除外フィルターを適用
	# ルートロガーに両方のハンドラーをセット
	root_logger.addHandler(file_handler)
	root_logger.addHandler(console_handler)

def get_bot_logger(name: str = "MusicBot"):
	"""
	Bot専用のロガーインスタンスを取得する。
	"""
	return logging.getLogger(name)