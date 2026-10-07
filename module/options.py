import configparser
import datetime
from pathlib import Path
from typing import Any

# プロジェクトのルートディレクトリ (起動時のカレントディレクトリに依存しないよう、ここを基準にする)
BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.ini"
CONFIG_SECTION = "MusicBot"

# キュー上限・プレイリスト取得上限として設定できる最大値
MAX_LIMIT = 50
# 音量 (倍率) の許容範囲と、/vol で指定できる音量 (%) の範囲
VOLUME_RANGE = (0.0, 2.0)
VOLUME_PERCENT_MIN, VOLUME_PERCENT_MAX = 1, 200
# 再生速度の範囲 (FFmpeg の atempo が 1 段で扱える下限が 0.5)
SPEED_MIN, SPEED_MAX = 0.5, 3.0
# 聴者不在時の自動退出までの秒数として設定できる最大値 (0 は自動退出しない)
ALONE_TIMEOUT_MAX = 600

def _load_config() -> tuple[configparser.ConfigParser, str | None]:
	"""config.ini を読み込む (BOM 付きも可。重複したキーは後の値)。読めなければ空の設定 (すべて既定値) と理由を返す"""
	parser = configparser.ConfigParser(strict=False)
	try:
		parser.read(CONFIG_PATH, encoding="utf-8-sig")
	except (configparser.Error, UnicodeDecodeError) as e:
		return configparser.ConfigParser(), f"{type(e).__name__}: {e}"
	return parser, None

# 読み込みに失敗した理由はロガーの準備後に bot.main() が出す
_config, CONFIG_LOAD_ERROR = _load_config()

def _normalize_user_agent(value: str) -> str:
	"""前後のクォートと "User-Agent:" 接頭辞を取り除いた UA 文字列を返す"""
	value = value.strip().strip("\"'").strip()
	prefix = "user-agent:"
	if value.lower().startswith(prefix):
		value = value[len(prefix):].strip()
	return value

# youtube_premium で「自動判定」を表す値
PREMIUM_AUTO = "auto"

def _parse_premium(value: str) -> bool | None:
	"""youtube_premium の値を True/False に変換する。auto・解釈できない値は None (yt-dlp の自動判定)"""
	value = value.strip().lower()
	if value == PREMIUM_AUTO:
		return None
	return configparser.ConfigParser.BOOLEAN_STATES.get(value)

# database_path が未指定・空欄のときのファイル名
_DEFAULT_DATABASE = "data.db"

# daily_restart の既定値と書式 (24 時間表記の HH:MM)
DAILY_RESTART_DEFAULT = "00:00"
_DAILY_RESTART_FORMAT = "%H:%M"

def _parse_daily_restart(value: str) -> datetime.time | None:
	"""daily_restart の値を時刻に変換する。空欄は None (再起動しない)、解釈できない値は既定の時刻"""
	value = value.strip()
	if not value:
		return None
	try:
		return datetime.time.strptime(value, _DAILY_RESTART_FORMAT)
	except ValueError:
		return datetime.time.strptime(DAILY_RESTART_DEFAULT, _DAILY_RESTART_FORMAT)

# Config._get の value_type ごとの読み出し関数 (それ以外は文字列のまま)
_GETTERS = {
	bool: configparser.ConfigParser.getboolean,
	int: configparser.ConfigParser.getint,
	float: configparser.ConfigParser.getfloat,
}

class Config:
	"""config.ini の [MusicBot] セクションを読み込む。キー欠落・型不正・ファイル欠落時はデフォルト値を使う"""
	def __init__(self) -> None:
		# 空文字なら yt-dlp 既定の User-Agent を使う
		self.USER_AGENT: str = _normalize_user_agent(self._get("user_agent", ""))
		self.DATABASE_PATH: Path = BASE_DIR / (self._get("database_path", "").strip() or _DEFAULT_DATABASE)
		self.DEFAULT_VOLUME: float = min(max(self._get("default_volume", 0.25, float), VOLUME_RANGE[0]), VOLUME_RANGE[1])
		self.DEFAULT_QUEUE_LIMIT: int = min(max(self._get("default_queue_limit", 50, int), 1), MAX_LIMIT)
		self.DEFAULT_PLAYLIST_LIMIT: int = min(max(self._get("default_playlist_limit", 10, int), 1), MAX_LIMIT)
		self.MAX_RETRIES: int = max(self._get("max_retries", 3, int), 1)
		self.MAX_WORKER_THREADS: int = max(self._get("max_worker_threads", 2, int), 1)
		# 再生リストの保存した結果を、取り直さずにそのまま使う秒数
		self.CACHE_TTL: int = max(self._get("cache_ttl", 14400, int), 0)
		# 曲名検索の結果 (どの曲か) を保存しておく日数。0 は保存しない
		self.SEARCH_CACHE_DAYS: int = max(self._get("search_cache_days", 30, int), 0)
		# 曲のメタ情報 (タイトル・再生時間・サムネイル) を保存しておく日数。0 は検索・再生リストの結果も含めて保存しない
		self.TRACK_CACHE_DAYS: int = max(self._get("track_cache_days", 183, int), 0)
		self.DEFAULT_ALONE_TIMEOUT: int = min(max(self._get("default_alone_timeout", 10, int), 0), ALONE_TIMEOUT_MAX)
		# True のときのみ所要時間の計測や内部処理の詳細をログに出す
		self.DEBUG: bool = self._get("debug", False, bool)
		# Cookie のアカウントが YouTube Premium か。None なら yt-dlp が初期データから判定する
		self.YOUTUBE_PREMIUM: bool | None = _parse_premium(self._get("youtube_premium", PREMIUM_AUTO))
		# 毎日この時刻を過ぎてから、使われていないときに Bot を再起動する。None なら再起動しない
		self.DAILY_RESTART: datetime.time | None = _parse_daily_restart(self._get("daily_restart", DAILY_RESTART_DEFAULT))

	@staticmethod
	def _get(key: str, default: Any, value_type: type = str) -> Any:
		"""[MusicBot] から key を value_type で取得する。取得できなければ default を返す"""
		getter = _GETTERS.get(value_type, configparser.ConfigParser.get)
		try:
			return getter(_config, CONFIG_SECTION, key)
		except (configparser.Error, ValueError):
			return default

app_config = Config()

def _build_http_headers() -> dict[str, str]:
	"""yt-dlp に渡す追加 HTTP ヘッダーを生成する (yt-dlp 既定ヘッダーに上書きマージされる)"""
	headers = {"Accept-Language": "ja,en-US;q=0.9,en;q=0.8"}
	if app_config.USER_AGENT:
		headers["User-Agent"] = app_config.USER_AGENT
	return headers

# 全抽出モード共通のオプション
_BASE_OPTIONS: dict[str, Any] = {
	"quiet": True,
	"no_warnings": True,
	# エラーメッセージを Discord に表示するため ANSI カラーを付けない
	"color": "no_color",
	"default_search": "ytsearch",
	# プレイリストは設定可能な上限までしか取得しない (巨大プレイリストの全件取得を防ぐ)
	"playlist_items": f"1:{MAX_LIMIT}",
	"http_headers": _build_http_headers(),
	# ホストの Firefox プロファイルの Cookie を使う (年齢制限・ログイン必須動画の再生用)
	"cookiesfrombrowser": ("firefox",),
}

# メタデータのみ取得 (検索・プレイリスト展開用)。"in_playlist" はプレイリストへの転送 (watch?v=ID&list=... 等) を追い、中身だけ平坦に取る
FAST_META_OPTIONS: dict[str, Any] = {
	**_BASE_OPTIONS,
	"extract_flat": "in_playlist",
}

# ストリームURLまで解決する本抽出。web_music + player_skip はログイン Cookie 前提で webpage/config の取得を省く
STREAM_OPTIONS: dict[str, Any] = {
	**_BASE_OPTIONS,
	"format": "bestaudio/best",
	"extract_flat": "in_playlist",
	# IPv4 で接続する
	"source_address": "0.0.0.0",
	# YouTube の署名/チャレンジ解決用 JS ランタイム (yt-dlp の既定と同じだが、Deno が必須であることを明示する)
	"js_runtimes": {"deno": {}},
	"extractor_args": {
		"youtube": {"player_client": ["web_music"], "player_skip": ["webpage", "configs"]},
	},
}

# 本抽出が失敗した際の予備設定 (YouTube は既定クライアント + ブラウザ TLS 偽装で取り直す)。
# impersonate は ImpersonateTarget に変換する必要があるため、本体に yt-dlp を読み込ませないよう抽出側で追加する
STREAM_FALLBACK_OPTIONS: dict[str, Any] = {key: value for key, value in STREAM_OPTIONS.items() if key != "extractor_args"}
STREAM_FALLBACK_IMPERSONATE = "chrome"

# FFmpeg の通信が応答しないまま止まったとみなすまでの秒数 (既定は無制限)
FFMPEG_IO_TIMEOUT_SECONDS = 15

FFMPEG_OPTIONS = {
	"before_options": (
		"-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
		f"-rw_timeout {FFMPEG_IO_TIMEOUT_SECONDS * 1_000_000} -analyzeduration 0 -probesize 32"
	),
	"options": "-vn -sn",
}
