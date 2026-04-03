import logging
from logging.handlers import TimedRotatingFileHandler
import os
import re

def setup_daily_logger():
	"""
	logフォルダーを作成し、毎日深夜0時にログファイルをローテーションする。
	過去30日分のログを保持する。
	"""
	log_dir = "log"
	if not os.path.exists(log_dir):
		os.makedirs(log_dir)
	root_logger = logging.getLogger()
	root_logger.setLevel(logging.INFO)
	# 現在書き込んでいる最新のファイルは 'bot.log' となる
	file_handler = TimedRotatingFileHandler(
		filename=os.path.join(log_dir, "bot.log"),
		when="midnight",
		interval=1,
		backupCount=30,
		encoding="utf-8"
	)
	# ログが分割・保存される際、末尾を 2026_04_02.log の形式にする
	file_handler.suffix = "%Y_%m_%d.log"
	# 古いファイルの自動削除(backupCount)が正しく動くように正規表現を上書きする
	file_handler.extMatch = re.compile(r"^\d{4}_\d{2}_\d{2}\.log$")
	formatter = logging.Formatter(
		fmt='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
		datefmt='%Y-%m-%d %H:%M:%S'
	)
	file_handler.setFormatter(formatter)
	root_logger.addHandler(file_handler)

def get_bot_logger(name: str = "MusicBot"):
	"""
	Bot専用のロガーインスタンスを取得する。
	"""
	return logging.getLogger(name)