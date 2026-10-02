import configparser
from pathlib import Path
from typing import Any

from yt_dlp.networking.impersonate import ImpersonateTarget

# プロジェクトのルートディレクトリ (起動時のカレントディレクトリに依存しないよう、ここを基準にする)
BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.ini"
CONFIG_SECTION = "MusicBot"

# キュー上限・プレイリスト取得上限として設定できる最大値
MAX_LIMIT = 50
# 音量 (倍率) の許容範囲
VOLUME_RANGE = (0.0, 2.0)

config_file = configparser.ConfigParser()
config_file.read(CONFIG_PATH, encoding="utf-8")

def _normalize_user_agent(value: str) -> str:
	"""前後のクォートと "User-Agent:" 接頭辞を取り除いた UA 文字列を返す"""
	value = value.strip().strip("\"'").strip()
	prefix = "user-agent:"
	if value.lower().startswith(prefix):
		value = value[len(prefix):].strip()
	return value

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
		self.MAX_WORKER_THREADS: int = max(self._get("max_worker_threads", 4, int), 1)
		self.CACHE_TTL: int = max(self._get("cache_ttl", 14400, int), 0)

	@staticmethod
	def _get(key: str, default: Any, value_type: type = str) -> Any:
		"""[MusicBot] から key を value_type で取得する。取得できなければ default を返す"""
		try:
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
FAST_META_OPTIONS: dict[str, Any] = {
	**_BASE_OPTIONS,
	"extract_flat": True,
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
STREAM_FALLBACK_OPTIONS: dict[str, Any] = {
	**STREAM_OPTIONS,
	# 文字列ではなく ImpersonateTarget オブジェクトで渡す必要がある
	"impersonate": ImpersonateTarget.from_str("chrome"),
	"extractor_args": {"nicovideo": _NICOVIDEO_ARGS},
}

FFMPEG_OPTIONS = {
	"before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -analyzeduration 0 -probesize 32",
	"options": "-threads 2 -vn -sn",
}
