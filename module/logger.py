import logging
import re
import sys
import time
from logging.handlers import TimedRotatingFileHandler

from module.options import BASE_DIR, app_config

# ==========================================
# スピナー状態の共有変数 (utils.py と連携)
# spinner_active  : スピナーが現在表示中かどうか
# spinner_line    : スピナーが最後に描画した行の文字列 (再描画用)
# ==========================================
spinner_active: bool = False
spinner_line: str = ""

# コンソールをクリアするためのパディング幅
_CLEAR_WIDTH = 150
# ログ出力先ディレクトリとファイル名
LOG_DIR = BASE_DIR / "log"
_LOG_FILE_NAME = "bot.log"
# ログファイルの保持日数
_LOG_BACKUP_DAYS = 30
# Bot 本体のロガー名
BOT_LOGGER_NAME = "MusicBot"

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
	- Bot 本体のロガーは config の debug が True のときだけ DEBUG (計測・内部処理の詳細) まで出す
	- ライブラリ (discord 等) は常に INFO 以上
	"""
	LOG_DIR.mkdir(parents=True, exist_ok=True)
	root = logging.getLogger()
	root.setLevel(logging.INFO)
	logging.getLogger(BOT_LOGGER_NAME).setLevel(logging.DEBUG if app_config.DEBUG else logging.INFO)
	# 重複登録を防ぐため既存ハンドラをクリアする
	if root.hasHandlers():
		root.handlers.clear()
	formatter = logging.Formatter(
		fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
		datefmt="%Y-%m-%d %H:%M:%S",
	)
	# ファイルハンドラ: 全ログを日付ごとのファイルに保存する
	file_handler = TimedRotatingFileHandler(
		filename=LOG_DIR / _LOG_FILE_NAME,
		when="midnight",
		interval=1,
		backupCount=_LOG_BACKUP_DAYS,
		encoding="utf-8",
	)
	file_handler.suffix = "%Y_%m_%d.log"
	file_handler.extMatch = re.compile(r"^\d{4}_\d{2}_\d{2}\.log$")
	file_handler.setFormatter(formatter)
	# コンソールハンドラ: スピナー対応版、discordのINFO以下は除外する
	console_handler = SpinnerAwareHandler(sys.stdout)
	console_handler.setFormatter(formatter)
	console_handler.addFilter(ConsoleFilter())
	root.addHandler(file_handler)
	root.addHandler(console_handler)

def get_bot_logger(name: str = BOT_LOGGER_NAME) -> logging.Logger:
	"""Bot専用のロガーインスタンスを取得する"""
	return logging.getLogger(name)

# ==========================================
# パフォーマンス計測 (config の debug が True のときのみ出力)
# ==========================================
def perf(label: str, start: float) -> None:
	"""start (time.perf_counter() の値) からの経過時間を "[PERF] ラベル: NNms" 形式で DEBUG 出力する"""
	logger = logging.getLogger(BOT_LOGGER_NAME)
	if logger.isEnabledFor(logging.DEBUG):
		logger.debug(f"[PERF] {label}: {(time.perf_counter() - start) * 1000:.1f}ms")
