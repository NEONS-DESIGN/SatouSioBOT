import configparser
from pathlib import Path
from typing import Any

# プロジェクトのルートディレクトリ (起動時のカレントディレクトリに依存しないよう、ここを基準にする)
BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.ini"
CONFIG_SECTION = "MusicBot"

# キュー上限・プレイリスト取得上限として設定できる最大値
MAX_LIMIT = 50
# 音量 (倍率) の許容範囲
VOLUME_RANGE = (0.0, 2.0)
# 聴者不在時の自動退出までの秒数として設定できる最大値 (0 は自動退出しない)
ALONE_TIMEOUT_MAX = 600

config_file = configparser.ConfigParser()
config_file.read(CONFIG_PATH, encoding="utf-8")

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

class Config:
	"""config.ini の [MusicBot] セクションを読み込む。キー欠落・型不正・ファイル欠落時はデフォルト値を使う"""
	def __init__(self) -> None:
		# 空文字なら yt-dlp 既定の User-Agent を使う
		self.USER_AGENT: str = _normalize_user_agent(self._get("user_agent", ""))
		self.DATABASE_PATH: Path = BASE_DIR / self._get("database_path", "data.db")
		self.DEFAULT_VOLUME: float = min(max(self._get("default_volume", 0.25, float), VOLUME_RANGE[0]), VOLUME_RANGE[1])
		self.DEFAULT_QUEUE_LIMIT: int = min(max(self._get("default_queue_limit", 50, int), 1), MAX_LIMIT)
		self.DEFAULT_PLAYLIST_LIMIT: int = min(max(self._get("default_playlist_limit", 10, int), 1), MAX_LIMIT)
		self.MAX_RETRIES: int = max(self._get("max_retries", 3, int), 1)
		self.MAX_WORKER_THREADS: int = max(self._get("max_worker_threads", 2, int), 1)
		self.CACHE_TTL: int = max(self._get("cache_ttl", 14400, int), 0)
		self.DEFAULT_ALONE_TIMEOUT: int = min(max(self._get("default_alone_timeout", 10, int), 0), ALONE_TIMEOUT_MAX)
		# True のときのみ所要時間の計測や内部処理の詳細をログに出す
		self.DEBUG: bool = self._get("debug", False, bool)
		# Cookie のアカウントが YouTube Premium か。None なら yt-dlp が初期データから判定する
		self.YOUTUBE_PREMIUM: bool | None = _parse_premium(self._get("youtube_premium", PREMIUM_AUTO))

	@staticmethod
	def _get(key: str, default: Any, value_type: type = str) -> Any:
		"""[MusicBot] から key を value_type で取得する。取得できなければ default を返す"""
		try:
			if value_type is bool:
				return config_file.getboolean(CONFIG_SECTION, key)
			if value_type is int:
				return config_file.getint(CONFIG_SECTION, key)
			if value_type is float:
				return config_file.getfloat(CONFIG_SECTION, key)
			return config_file.get(CONFIG_SECTION, key)
		except (configparser.Error, ValueError):
			return default

app_config = Config()

def _build_http_headers() -> dict[str, str]:
	"""yt-dlp に渡す追加 HTTP ヘッダーを生成する (yt-dlp 既定ヘッダーに上書きマージされる)"""
	headers = {"Accept-Language": "ja,en-US;q=0.9,en;q=0.8"}
	if app_config.USER_AGENT:
		headers["User-Agent"] = app_config.USER_AGENT
	return headers

_NICOVIDEO_ARGS = {"action_wait_time": ["1.0"]}

# 全抽出モード共通のオプション
_BASE_OPTIONS: dict[str, Any] = {
	"quiet": True,
	"no_warnings": True,
	# エラーメッセージを Discord に表示するため ANSI カラーを付けない
	"color": "no_color",
	"default_search": "ytsearch",
	# プレイリストは設定可能な上限までしか取得しない (巨大プレイリストの全件取得を防ぐ)
	"playlistend": MAX_LIMIT,
	"http_headers": _build_http_headers(),
	# ホストの Firefox プロファイルの Cookie を使う (年齢制限・ログイン必須動画の再生用)
	"cookiesfrombrowser": ("firefox",),
}

# メタデータのみ取得 (検索・プレイリスト展開用。ストリームURLは解決しない)
# extract_flat=True は最上位の転送も解決しないため、プレイリストへ転送される URL
# (youtu.be/ID?list=... や watch?v=ID&list=...) が entries の無い結果になる。"in_playlist" は転送を追い、中身だけ平坦に取る
FAST_META_OPTIONS: dict[str, Any] = {
	**_BASE_OPTIONS,
	"extract_flat": "in_playlist",
	"extractor_args": {"nicovideo": _NICOVIDEO_ARGS},
}

# ストリームURLまで解決する本抽出 (速度優先設定)
# web_music + player_skip はログイン Cookie 前提で webpage/config の取得を省略し、解決時間を短縮する
STREAM_OPTIONS: dict[str, Any] = {
	**_BASE_OPTIONS,
	"format": "bestaudio/best",
	"noplaylist": False,
	"extract_flat": "in_playlist",
	# IPv4 で接続する
	"source_address": "0.0.0.0",
	# YouTube の署名/チャレンジ解決用 JS ランタイム (yt-dlp-ejs パッケージと併用)
	"js_runtimes": {"deno": {}},
	"extractor_args": {
		"youtube": {"player_client": ["web_music"], "player_skip": ["webpage", "configs"]},
		"nicovideo": _NICOVIDEO_ARGS,
	},
}

# 本抽出が失敗した際の予備設定 (YouTube は既定クライアント + ブラウザ TLS 偽装で取り直す)
# 偽装先 (impersonate) は yt-dlp の ImpersonateTarget に変換して渡す必要があるため、抽出側の子プロセスで追加する
# (Bot 本体のプロセスに yt-dlp を読み込ませないため)
STREAM_FALLBACK_OPTIONS: dict[str, Any] = {
	**STREAM_OPTIONS,
	"extractor_args": {"nicovideo": _NICOVIDEO_ARGS},
}
STREAM_FALLBACK_IMPERSONATE = "chrome"

# FFmpeg の通信が応答しないまま止まったとみなすまでの秒数 (既定は無制限で、応答が止まると読み込みを待ち続けるため)
FFMPEG_IO_TIMEOUT_SECONDS = 15

FFMPEG_OPTIONS = {
	"before_options": (
		"-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
		f"-rw_timeout {FFMPEG_IO_TIMEOUT_SECONDS * 1_000_000} -analyzeduration 0 -probesize 32"
	),
	"options": "-vn -sn",
}
