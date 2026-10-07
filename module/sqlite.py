import asyncio
from collections.abc import Iterable
from typing import Any, NamedTuple

import aiosqlite

from module.logger import get_bot_logger
from module.options import app_config

logger = get_bot_logger()

# ギルド設定として更新を許可するカラム (SQL へ埋め込むためホワイトリストで制限する)。並びは GuildSettings と同じ
_SETTING_COLUMNS = ("volume", "queue_limit", "playlist_limit", "alone_timeout")

# 永続コネクションと、その利用を直列化するロック (モジュールレベルのシングルトン)
_connection: aiosqlite.Connection | None = None
_lock = asyncio.Lock()
# close_db() 後の再接続を禁止するフラグ (終了処理後に接続が開き直されて残るのを防ぐ)
_closed = False

class GuildSettings(NamedTuple):
	"""ギルド単位の設定値"""
	volume: float
	queue_limit: int
	playlist_limit: int
	# 聴者がいなくなってから自動退出するまでの秒数 (0 は自動退出しない)
	alone_timeout: int

def _default_settings() -> GuildSettings:
	"""config.ini 由来のデフォルト設定を返す"""
	return GuildSettings(
		app_config.DEFAULT_VOLUME, app_config.DEFAULT_QUEUE_LIMIT,
		app_config.DEFAULT_PLAYLIST_LIMIT, app_config.DEFAULT_ALONE_TIMEOUT,
	)

async def _get_connection() -> aiosqlite.Connection:
	"""永続的なSQLiteコネクションを返す。初回のみ接続して WAL モードを有効化する。_lock を取得してから呼ぶこと"""
	global _connection
	if _closed:
		raise RuntimeError("データベース接続は既に閉じられています。")
	if _connection is None:
		_connection = await aiosqlite.connect(app_config.DATABASE_PATH)
		await _connection.execute("PRAGMA journal_mode=WAL;")
		await _connection.execute("PRAGMA synchronous=NORMAL;")  # WAL時の推奨設定
		logger.debug(f"SQLiteに接続しました: {app_config.DATABASE_PATH}")
	return _connection

# server_data の設定カラムは DEFAULT を持たせない (NULL は読み込み時に config.ini の既定値で補うため、既定値を変えると全ギルドに反映される)
_CREATE_TABLES = (
	"""
	CREATE TABLE IF NOT EXISTS "server_data" (
		"guild_id"       INTEGER PRIMARY KEY NOT NULL,
		"volume"         REAL,
		"queue_limit"    INTEGER,
		"playlist_limit" INTEGER,
		"alone_timeout"  INTEGER
	);
	""",
	"""
	CREATE TABLE IF NOT EXISTS "bot_admins" (
		"guild_id" INTEGER NOT NULL,
		"user_id"  INTEGER NOT NULL,
		PRIMARY KEY ("guild_id", "user_id")
	);
	""",
	# 曲のメタ情報。url は抽出結果の曲の URL (曲名検索・再生リストの保存から参照する)
	"""
	CREATE TABLE IF NOT EXISTS "track_meta" (
		"url"        TEXT    PRIMARY KEY NOT NULL,
		"title"      TEXT,
		"duration"   REAL,
		"thumbnail"  TEXT,
		"fetched_at" INTEGER NOT NULL
	);
	""",
	# 曲名検索の結果。query は表記ゆれを揃えた検索語
	"""
	CREATE TABLE IF NOT EXISTS "search_cache" (
		"query"      TEXT    PRIMARY KEY NOT NULL,
		"track_url"  TEXT    NOT NULL,
		"fetched_at" INTEGER NOT NULL
	);
	""",
	# 再生リストの結果。track_urls は曲の URL の JSON 配列 (並び順どおり)
	"""
	CREATE TABLE IF NOT EXISTS "playlist_cache" (
		"url"         TEXT    PRIMARY KEY NOT NULL,
		"title"       TEXT,
		"webpage_url" TEXT,
		"thumbnail"   TEXT,
		"track_urls"  TEXT    NOT NULL,
		"fetched_at"  INTEGER NOT NULL
	);
	""",
)
# 旧スキーマの server_data に無い可能性があるカラム (マイグレーションで追加する)
_MIGRATE_COLUMNS = (("queue_limit", "INTEGER"), ("playlist_limit", "INTEGER"), ("alone_timeout", "INTEGER"))

async def init_db() -> None:
	"""テーブルの作成と、既存テーブルへのカラム追加 (既にあれば無視) を行う"""
	try:
		async with _lock:
			db = await _get_connection()
			for create_sql in _CREATE_TABLES:
				await db.execute(create_sql)
			for column, column_type in _MIGRATE_COLUMNS:
				try:
					await db.execute(f'ALTER TABLE "server_data" ADD COLUMN "{column}" {column_type};')
				except aiosqlite.OperationalError as e:
					if "duplicate column name" not in str(e).lower():
						logger.warning(f"[SQLite] {column} カラム追加時の予期せぬエラー: {e}")
			await db.commit()
		logger.debug("[SQLite] データベースの初期化が完了しました。")
	except Exception as e:
		logger.error(f"[SQLite] データベース初期化エラー: {e}")
		raise

async def close_db() -> None:
	"""永続SQLite接続を閉じ、以後の再接続を禁止する (実行中のクエリの完了を待つ。終了時に呼ぶ)"""
	global _connection, _closed
	async with _lock:
		_closed = True
		connection, _connection = _connection, None
		if connection is not None:
			await connection.close()
			logger.debug("[SQLite] 接続を閉じました。")

async def sql_execution(query: str, params: tuple = ()) -> list:
	"""
	SQLクエリを実行し、結果行のリストを返す。更新があれば commit する。
	- 失敗時はロガーにエラーを記録してから例外を送出する
	"""
	try:
		async with _lock:
			db = await _get_connection()
			async with db.execute(query, params) as cursor:
				result = await cursor.fetchall()
			if db.in_transaction:
				await db.commit()
			return result
	except Exception as e:
		logger.error(f"[SQLite] クエリ実行エラー: {e} | SQL: {query} | params: {params}")
		raise

async def sql_execute_batch(statements: Iterable[tuple[str, tuple]]) -> None:
	"""
	更新系の SQL をまとめて実行し、最後に 1 回だけ commit する。(途中で失敗・取り消しされたら全体を取り消す)
	- 失敗時はロガーにエラーを記録してから例外を送出する
	"""
	try:
		async with _lock:
			db = await _get_connection()
			try:
				for query, params in statements:
					await db.execute(query, params)
				await db.commit()
			except BaseException:
				await db.rollback()
				raise
	except Exception as e:
		logger.error(f"[SQLite] 一括実行エラー: {e}")
		raise

# ギルド設定のメモリキャッシュ (書き込みはこのプロセスの save_guild_setting のみ)
_settings_cache: dict[int, GuildSettings] = {}
# ギルドごとの設定の更新回数。読み込み中に更新された古い値をキャッシュしないために使う
_settings_versions: dict[int, int] = {}

async def get_guild_settings(guild_id: int) -> GuildSettings:
	"""ギルド設定を返す。未登録・NULL のカラムは config.ini の既定値で補う。取得失敗時はデフォルト値を返す (キャッシュしない)"""
	if (cached := _settings_cache.get(guild_id)) is not None:
		return cached
	defaults = _default_settings()
	version = _settings_versions.get(guild_id, 0)
	try:
		rows = await sql_execution(
			f"SELECT {', '.join(_SETTING_COLUMNS)} FROM server_data WHERE guild_id=?;",
			(guild_id,),
		)
	except Exception:
		return defaults
	settings = GuildSettings(*(value if value is not None else default for value, default in zip(rows[0], defaults, strict=True))) if rows else defaults
	if _settings_versions.get(guild_id, 0) == version:
		_settings_cache[guild_id] = settings
	return settings

async def save_guild_setting(guild_id: int, column: str, value: Any) -> None:
	"""
	ギルド設定の1項目を UPSERT し、キャッシュにも反映する。失敗時は例外を送出する。
	- 新しい行の他の項目は NULL (既定値に従う) にする。旧スキーマの DEFAULT が入らないよう明示する
	"""
	if column not in _SETTING_COLUMNS:
		raise ValueError(f"更新できない設定項目です: {column}")
	values = tuple(value if name == column else None for name in _SETTING_COLUMNS)
	await sql_execution(
		f"INSERT INTO server_data (guild_id, {', '.join(_SETTING_COLUMNS)}) VALUES (?{', ?' * len(_SETTING_COLUMNS)}) "
		f"ON CONFLICT(guild_id) DO UPDATE SET {column}=excluded.{column};",
		(guild_id, *values),
	)
	_settings_versions[guild_id] = _settings_versions.get(guild_id, 0) + 1
	if (cached := _settings_cache.get(guild_id)) is not None:
		_settings_cache[guild_id] = cached._replace(**{column: value})
