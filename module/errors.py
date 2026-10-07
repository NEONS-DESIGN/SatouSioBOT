"""
エラーを原因 (利用者の入力 / Bot 側の環境) ごとに分類し、利用者と管理者に向けた説明に変換する。
分類できないエラーは UNKNOWN として、元のエラー文をそのまま見せる。
"""
import concurrent.futures.process
import enum
from dataclasses import dataclass

import discord

from module.logger import get_bot_logger

logger = get_bot_logger()

class ErrorCause(enum.Enum):
	"""エラーの原因の区分"""
	USER = "user"        # 入力された URL・動画の問題 (利用者が直せる)
	SERVER = "server"    # Bot の実行環境・認証・ライブラリ・通信の問題 (管理者が直す)
	UNKNOWN = "unknown"  # 想定外

@dataclass(frozen=True, slots=True)
class ErrorInfo:
	"""
	分類したエラーの説明。
	- message: 利用者に見せる説明 (UNKNOWN では元のエラー文)
	- admin_hint: 管理者向けの対処 (SERVER のみ)
	"""
	cause: ErrorCause
	message: str
	admin_hint: str | None = None

class UserFacingError(Exception):
	"""利用者の操作が原因で、メッセージをそのまま利用者に見せてよいエラー (キューの上限・検索結果なし等)"""

class AudioOpenError(RuntimeError):
	"""FFmpeg を起動しても音声を受信できなかったエラー"""

@dataclass(frozen=True, slots=True)
class _Rule:
	"""エラー文 (小文字) に keywords のいずれかが含まれれば該当する分類規則。transient は利用者側の原因でも一時的に起こりうるもの"""
	keywords: tuple[str, ...]
	cause: ErrorCause
	message: str
	admin_hint: str | None = None
	transient: bool = False

# 利用者側の文言でも、一時的な制限を表すもの (再試行で直りうる)
_TRANSIENT_MARKERS = ("try again later",)

# yt-dlp・管理者向けの対処で共通に使う文言
_UPDATE_YTDLP = "`venv\\Scripts\\python.exe -m pip install -U \"yt-dlp[default,curl-cffi]\"` で yt-dlp を更新し、Bot を再起動してください。"
_RELOGIN = "ホストの Firefox で YouTube にログインし直し、Bot を再起動してください。(Cookie は起動時に読み込むため)"
_VOICE_LIBRARY_INFO = ErrorInfo(
	ErrorCause.SERVER,
	"音声の送信に必要なライブラリを読み込めないため、再生できません。",
	"`venv\\Scripts\\python.exe -m pip install -r requirements.txt` で依存ライブラリを入れ直してください。",
)

# 例外の型で判定する規則 (エラー文より優先する)
_TYPE_RULES: tuple[tuple[type[BaseException], ErrorInfo], ...] = (
	(concurrent.futures.process.BrokenProcessPool, ErrorInfo(
		ErrorCause.SERVER,
		"曲の情報を取得する処理が異常終了しました。もう一度お試しください。",
		"抽出ワーカーのプロセスが異常終了しました。続く場合はログを確認し、Bot を再起動してください。",
	)),
	(TimeoutError, ErrorInfo(
		ErrorCause.SERVER,
		"Discord との通信 (ボイスチャンネルへの接続など) がタイムアウトしました。",
		"Bot のネットワーク接続と、Bot のロールにそのボイスチャンネルの「接続」「発言」権限があるか確認してください。",
	)),
	(discord.Forbidden, ErrorInfo(
		ErrorCause.SERVER,
		"Bot に必要な権限が無いため、操作できませんでした。",
		"Bot のロールに、チャンネルの閲覧・メッセージの送信・埋め込みリンク・接続・発言の権限があるか確認してください。",
	)),
	(discord.opus.OpusNotLoaded, _VOICE_LIBRARY_INFO),
	(AudioOpenError, ErrorInfo(
		ErrorCause.SERVER,
		"音声データを受信できませんでした。",
		f"FFmpeg が音声を取得できませんでした。動画サイトの拒否 (HTTP 403) が続く場合は {_UPDATE_YTDLP}",
	)),
)

# エラー文で判定する規則 (上から順に照合する。具体的な規則を先に置く)
_TEXT_RULES: tuple[_Rule, ...] = (
	# ---- Bot 側 (先に判定しないと利用者側の規則に吸われるもの) ----
	_Rule(("not a bot",), ErrorCause.SERVER,
		"YouTube に Bot と判定され、取得を拒否されました。",
		f"{_RELOGIN} 続く場合は {_UPDATE_YTDLP}"),
	# ---- 利用者側 ----
	_Rule(("incomplete youtube id", "looks truncated"), ErrorCause.USER,
		"動画の URL が途中で切れているようです。URL を最後までコピーできているか確認してください。"),
	_Rule(("playlists that require authentication", "playlist does not exist", "this playlist is private"), ErrorCause.USER,
		"プレイリストを読み込めませんでした。URL が途中で切れていないか、プレイリストが非公開になっていないか確認してください。"),
	_Rule(("private video",), ErrorCause.USER,
		"非公開の動画のため再生できません。"),
	_Rule(("confirm your age", "age-restricted", "inappropriate for some users"), ErrorCause.USER,
		"年齢制限のある動画のため再生できません。"),
	_Rule(("members-only", "channel's members", "join this channel"), ErrorCause.USER,
		"メンバー限定の動画のため再生できません。"),
	_Rule(("available in your country", "geo restrict", "geo-restrict"), ErrorCause.USER,
		"地域制限により、この動画は再生できません。"),
	_Rule(("live event will begin", "premieres in", "premiere will begin"), ErrorCause.USER,
		"まだ公開 (配信) が始まっていない動画です。開始後にもう一度お試しください。"),
	_Rule(("drm protected",), ErrorCause.USER,
		"著作権保護 (DRM) のかかった動画のため再生できません。"),
	_Rule(("video is unavailable", "video unavailable", "has been removed", "may not exist or was deleted", "does not exist", "http error 404"), ErrorCause.USER,
		"動画が見つかりませんでした。削除・非公開になっていないか、URL が正しいか確認してください。"),
	_Rule(("unsupported url", "is not a valid url"), ErrorCause.USER,
		"対応していないサイト、または動画・曲のページではない URL です。YouTube・ニコニコ動画・SoundCloud などの動画や曲のページの URL を指定してください。"),
	_Rule(("could not resolve host", "getaddrinfo failed", "name or service not known", "nodename nor servname"), ErrorCause.USER,
		"サイトに接続できませんでした。URL (ドメイン名) が正しいか確認してください。", transient=True),
	# ---- Bot 側 ----
	_Rule(("http error 403", "403: forbidden"), ErrorCause.SERVER,
		"動画サイトにアクセスを拒否されました。",
		_UPDATE_YTDLP),
	_Rule(("http error 429", "too many requests"), ErrorCause.SERVER,
		"動画サイトへのアクセスが一時的に制限されています。時間をおいてお試しください。",
		"短時間にリクエストが集中しています。続く場合は Firefox の YouTube のログイン状態を確認してください。"),
	_Rule(("cookies database", "failed to decrypt", "could not copy"), ErrorCause.SERVER,
		"Bot のログイン情報 (Cookie) を読み込めませんでした。",
		f"ホストに Firefox がインストールされ、YouTube にログイン済みか確認してください。{_RELOGIN}"),
	_Rule(("only available for registered users", "login required", "sign in", "--cookies"), ErrorCause.SERVER,
		"ログインが必要な動画のため取得できませんでした。",
		f"Bot のログイン (Firefox の Cookie) が切れている可能性があります。{_RELOGIN}"),
	_Rule(("javascript runtime", "n challenge", "nsig", "signature extraction failed", "requested format is not available", "no video formats found", "unable to extract"), ErrorCause.SERVER,
		"動画サイトの仕様変更などにより、音声を取得できませんでした。",
		f"{_UPDATE_YTDLP} Deno が PATH にあるかも確認してください。"),
	_Rule(("ffmpeg was not found",), ErrorCause.SERVER,
		"音声の変換に必要なソフトウェアが見つからないため、再生できません。",
		"FFmpeg がインストールされ、PATH が通っているか確認してください。"),
	_Rule(("pynacl library needed",), ErrorCause.SERVER,
		_VOICE_LIBRARY_INFO.message,
		_VOICE_LIBRARY_INFO.admin_hint),
	_Rule(("timed out", "connection reset", "connection aborted", "remote end closed", "temporary failure", "http error 5"), ErrorCause.SERVER,
		"通信エラーのため取得できませんでした。時間をおいてお試しください。",
		"Bot のネットワーク接続、または動画サイト側の一時的な障害の可能性があります。続く場合はネットワークを確認してください。"),
)

def describe_error(error: BaseException) -> ErrorInfo:
	"""error を原因ごとに分類した説明を返す。どの規則にも当てはまらなければ UNKNOWN (元のエラー文)"""
	if isinstance(error, UserFacingError):
		return ErrorInfo(ErrorCause.USER, str(error))
	for error_type, info in _TYPE_RULES:
		if isinstance(error, error_type):
			return info
	if (rule := _match_rule(str(error).lower())) is not None:
		return ErrorInfo(rule.cause, rule.message, rule.admin_hint)
	return ErrorInfo(ErrorCause.UNKNOWN, str(error) or type(error).__name__)

def _match_rule(text: str) -> _Rule | None:
	"""小文字にしたエラー文に当てはまる最初の規則を返す"""
	return next((rule for rule in _TEXT_RULES if any(keyword in text for keyword in rule.keywords)), None)

def is_permanent(error: BaseException) -> bool:
	"""再試行しても直らない利用者側の原因 (削除・非公開・年齢制限など) か。DNS の失敗・回数制限など一時的に起こりうるものは False"""
	if describe_error(error).cause is not ErrorCause.USER:
		return False
	text = str(error).lower()
	if any(marker in text for marker in _TRANSIENT_MARKERS):
		return False
	rule = _match_rule(text)
	return rule is None or not rule.transient

def report_error(context: str, error: BaseException) -> None:
	"""
	error を分類してログに出す。
	- USER: INFO (利用者の入力の問題で、Bot の異常ではないため)
	- SERVER: ERROR (管理者向けの対処も出す)
	- UNKNOWN: ERROR (トレースバック付き)
	"""
	info = describe_error(error)
	if info.cause is ErrorCause.USER:
		logger.info(f"{context}: {info.message} ({error})")
	elif info.cause is ErrorCause.SERVER:
		logger.error(f"{context}: {info.message} 対処: {info.admin_hint} ({error!r})")
	else:
		logger.error(f"{context}: 想定外のエラー {error!r}", exc_info=error)
