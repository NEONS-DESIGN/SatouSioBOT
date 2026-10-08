import contextlib
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
		# 置き換えられた・閉じられたストリームなど、変更できない場合は元の設定のまま使う
		with contextlib.suppress(AttributeError, ValueError, OSError):
			stream.reconfigure(errors=_CONSOLE_ENCODE_ERRORS)

class AlignedFormatter(logging.Formatter):
	"""
	"[INFO]" などのレベル表記を最長のレベル名の幅にスペースで揃え、ロガー名以降の開始位置を一定にする。
	書式中の %(levelbracket)s が、幅を揃えた "[レベル名]" に置き換わる
	"""
	# 標準レベルのうち最長の名前 ("CRITICAL") に角括弧の 2 文字を足した幅
	LEVEL_WIDTH = max(
		len(logging.getLevelName(level))
		for level in (logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL)
	) + 2

	def format(self, record: logging.LogRecord) -> str:
		record.levelbracket = f"[{record.levelname}]".ljust(self.LEVEL_WIDTH)
		return super().format(record)

class ConsoleFilter(logging.Filter):
	"""discord ライブラリの WARNING 未満のログをコンソールから除外する"""
	def filter(self, record: logging.LogRecord) -> bool:
		return not (record.name.startswith("discord") and record.levelno < logging.WARNING)

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
	formatter = AlignedFormatter(
		fmt="%(asctime)s.%(msecs)03d %(levelbracket)s %(name)s: %(message)s",
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

def get_bot_logger() -> logging.Logger:
	"""Bot専用のロガーインスタンスを取得する"""
	return logging.getLogger(BOT_LOGGER_NAME)

# ==========================================
# パフォーマンス計測 (config の debug が True のときのみ出力)
# ==========================================
def perf(label: str, start: float) -> None:
	"""start (time.perf_counter() の値) からの経過時間を "[PERF] ラベル: NNms" 形式で DEBUG 出力する"""
	logger = get_bot_logger()
	if logger.isEnabledFor(logging.DEBUG):
		logger.debug(f"[PERF] {label}: {(time.perf_counter() - start) * 1000:.1f}ms")
