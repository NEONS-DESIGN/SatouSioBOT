import logging
import re
import sys
import time
from logging.handlers import TimedRotatingFileHandler

from module.options import BASE_DIR, app_config

# ログ出力先ディレクトリとファイル名
LOG_DIR = BASE_DIR / "log"
_LOG_FILE_NAME = "bot.log"
# ログファイルの保持日数
_LOG_BACKUP_DAYS = 30
# Bot 本体のロガー名
BOT_LOGGER_NAME = "MusicBot"
# コンソールの文字コードで表せない文字の扱い (例外にせず "?" に置き換える)
_CONSOLE_ENCODE_ERRORS = "replace"

def _tolerate_console_encoding() -> None:
	"""
	標準出力・標準エラーを、文字コードで表せない文字があっても例外にしない設定にする。
	出力をパイプやファイルにリダイレクトすると cp932 になり、曲名の絵文字などで
	UnicodeEncodeError が起きてログが失われるため
	"""
	for stream in (sys.stdout, sys.stderr):
		reconfigure = getattr(stream, "reconfigure", None)
		if reconfigure is None:
			continue
		try:
			reconfigure(errors=_CONSOLE_ENCODE_ERRORS)
		except (ValueError, OSError):
			# 既に閉じられている・書き込み途中などで変更できない場合は元の設定のまま使う
			pass

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
	logフォルダにデイリーローテーションするファイルハンドラと、コンソールハンドラを設定する。
	- コンソール出力は表せない文字を置き換え、文字コードの違いで例外にしない
	- Bot 本体のロガーは config の debug が True のときだけ DEBUG (計測・内部処理の詳細) まで出す
	- ライブラリ (discord 等) は常に INFO 以上
	"""
	_tolerate_console_encoding()
	LOG_DIR.mkdir(parents=True, exist_ok=True)
	root = logging.getLogger()
	root.setLevel(logging.INFO)
	logging.getLogger(BOT_LOGGER_NAME).setLevel(logging.DEBUG if app_config.DEBUG else logging.INFO)
	# 重複登録を防ぐため既存ハンドラをクリアする
	if root.hasHandlers():
		root.handlers.clear()
	formatter = logging.Formatter(
		fmt="%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
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
	# コンソールハンドラ: discordのINFO以下は除外する
	console_handler = logging.StreamHandler(sys.stdout)
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
