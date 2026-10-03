import asyncio
from typing import Any, NamedTuple

import aiosqlite

from module.logger import get_bot_logger
from module.options import app_config

logger = get_bot_logger()

# ギルド設定として更新を許可するカラム (SQL へ埋め込むためホワイトリストで制限する)
_SETTING_COLUMNS = frozenset({"volume", "queue_limit", "playlist_limit", "alone_timeout"})

# 永続コネクションとロック (モジュールレベルのシングルトン)
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
	"""永続的なSQLiteコネクションを返す。初回のみ接続して WAL モードを有効化する"""
	global _connection
	if _closed:
		raise RuntimeError("データベース接続は既に閉じられています。")
	if _connection is None:
		_connection = await aiosqlite.connect(app_config.DATABASE_PATH)
		await _connection.execute("PRAGMA journal_mode=WAL;")
		await _connection.execute("PRAGMA synchronous=NORMAL;")  # WAL時の推奨設定
		await _connection.commit()
		logger.debug(f"SQLiteに接続しました: {app_config.DATABASE_PATH}")
	return _connection

async def init_db() -> None:
	"""
	テーブルの作成と既存テーブルへのカラム追加(マイグレーション)を行う。
	- ALTER TABLE が重複カラムエラーを出した場合は安全に無視する
	"""
	defaults = _default_settings()
	create_server_data = f"""
	CREATE TABLE IF NOT EXISTS "server_data" (
		"guild_id"       INTEGER PRIMARY KEY UNIQUE NOT NULL,
		"volume"         REAL    DEFAULT {defaults.volume},
		"queue_limit"    INTEGER DEFAULT {defaults.queue_limit},
		"playlist_limit" INTEGER DEFAULT {defaults.playlist_limit},
		"alone_timeout"  INTEGER DEFAULT {defaults.alone_timeout}
	);
	"""
	create_bot_admins = """
	CREATE TABLE IF NOT EXISTS "bot_admins" (
		"guild_id" INTEGER NOT NULL,
		"user_id"  INTEGER NOT NULL,
		PRIMARY KEY ("guild_id", "user_id")
	);
	"""
	# 旧スキーマに存在しない可能性があるカラムをマイグレーションで追加する
	migrate_columns: list[tuple[str, str]] = [
		("queue_limit",    f'ALTER TABLE "server_data" ADD COLUMN "queue_limit"    INTEGER DEFAULT {defaults.queue_limit};'),
		("playlist_limit", f'ALTER TABLE "server_data" ADD COLUMN "playlist_limit" INTEGER DEFAULT {defaults.playlist_limit};'),
		("alone_timeout",  f'ALTER TABLE "server_data" ADD COLUMN "alone_timeout"  INTEGER DEFAULT {defaults.alone_timeout};'),
	]
	try:
		db = await _get_connection()
		async with _lock:
			await db.execute(create_server_data)
			await db.execute(create_bot_admins)
			for col_name, alter_sql in migrate_columns:
				try:
					await db.execute(alter_sql)
				except aiosqlite.OperationalError as e:
					# duplicate column name は正常なケースなので無視する
					if "duplicate column name" not in str(e).lower():
						logger.warning(f"[SQLite] {col_name} カラム追加時の予期せぬエラー: {e}")
			await db.commit()
		logger.debug("[SQLite] データベースの初期化が完了しました。")
	except Exception as e:
		logger.error(f"[SQLite] データベース初期化エラー: {e}")
		raise

async def close_db() -> None:
	"""永続SQLite接続を閉じ、以後の再接続を禁止する（終了時に呼ぶ）"""
	global _connection, _closed
	_closed = True
	if _connection is not None:
		try:
			await _connection.close()
		finally:
			_connection = None
			logger.debug("[SQLite] 接続を閉じました。")

async def sql_execution(query: str, params: tuple = ()) -> list:
	"""
	SQLクエリを実行し、結果行のリストを返す。
	- Lockで排他制御して並行書き込みの競合を防ぐ
	- SELECT 以外のときのみ commit する
	- 失敗時はロガーにエラーを記録してから例外を送出する
	"""
	try:
		db = await _get_connection()
		async with _lock:
			async with db.execute(query, params) as cursor:
				result = await cursor.fetchall()
			if not query.lstrip().upper().startswith("SELECT"):
				await db.commit()
			return result
	except Exception as e:
		logger.error(f"[SQLite] クエリ実行エラー: {e} | SQL: {query} | params: {params}")
		raise

# ギルド設定のメモリキャッシュ (曲の切り替えのたびに DB を読まないため)。書き込みはこのプロセスの save_guild_setting だけが行う
_settings_cache: dict[int, GuildSettings] = {}
# ギルドごとの設定の更新回数。読み込み中に更新された古い値をキャッシュしないために使う
_settings_versions: dict[int, int] = {}

async def get_guild_settings(guild_id: int) -> GuildSettings:
	"""ギルド設定を返す。未登録はデフォルト値、NULL のカラムは個別に補う。取得失敗時はデフォルト値を返す (キャッシュしない)"""
	if (cached := _settings_cache.get(guild_id)) is not None:
		return cached
	defaults = _default_settings()
	version = _settings_versions.get(guild_id, 0)
	try:
		rows = await sql_execution(
			"SELECT volume, queue_limit, playlist_limit, alone_timeout FROM server_data WHERE guild_id=?;",
			(guild_id,),
		)
	except Exception:
		return defaults
	settings = GuildSettings(*(value if value is not None else default for value, default in zip(rows[0], defaults))) if rows else defaults
	if _settings_versions.get(guild_id, 0) == version:
		_settings_cache[guild_id] = settings
	return settings

async def save_guild_setting(guild_id: int, column: str, value: Any) -> None:
	"""ギルド設定の1項目を UPSERT し、キャッシュにも反映する。失敗時は例外を送出する"""
	if column not in _SETTING_COLUMNS:
		raise ValueError(f"更新できない設定項目です: {column}")
	await sql_execution(
		f"INSERT INTO server_data (guild_id, {column}) VALUES (?, ?) "
		f"ON CONFLICT(guild_id) DO UPDATE SET {column}=excluded.{column};",
		(guild_id, value),
	)
	_settings_versions[guild_id] = _settings_versions.get(guild_id, 0) + 1
	if (cached := _settings_cache.get(guild_id)) is not None:
		_settings_cache[guild_id] = cached._replace(**{column: value})
