import asyncio
import aiosqlite

from module.logger import get_bot_logger
from module.options import app_config

logger = get_bot_logger()

# config.ini から読み込んだ設定値
_DB_PATH: str = app_config.DATABASE_PATH
_DEFAULT_VOLUME: float = app_config.DEFAULT_VOLUME
_DEFAULT_QUEUE_LIMIT: int = app_config.DEFAULT_QUEUE_LIMIT
_DEFAULT_PLAYLIST_LIMIT: int = app_config.DEFAULT_PLAYLIST_LIMIT

# 永続コネクションとロック (モジュールレベルのシングルトン)
_connection: aiosqlite.Connection | None = None
_lock = asyncio.Lock()

async def _get_connection() -> aiosqlite.Connection:
	"""
	永続的なSQLiteコネクションを返す。
	- 初回接続時にWALモードを有効化して並行読み書き性能を向上させる
	- 接続が切断されていた場合は再接続する
	"""
	global _connection
	if _connection is None:
		_connection = await aiosqlite.connect(_DB_PATH)
		await _connection.execute("PRAGMA journal_mode=WAL;")
		await _connection.execute("PRAGMA synchronous=NORMAL;")  # WAL時の推奨設定
		await _connection.commit()
		logger.info(f"SQLiteに接続しました: {_DB_PATH}")
	return _connection

async def init_db() -> None:
	"""
	テーブルの作成と既存テーブルへのカラム追加(マイグレーション)を行う。
	- ALTER TABLE が重複カラムエラーを出した場合は安全に無視する
	"""
	create_server_data = f"""
	CREATE TABLE IF NOT EXISTS "server_data" (
		"guild_id"       INTEGER PRIMARY KEY UNIQUE NOT NULL,
		"volume"         REAL    DEFAULT {_DEFAULT_VOLUME},
		"queue_limit"    INTEGER DEFAULT {_DEFAULT_QUEUE_LIMIT},
		"playlist_limit" INTEGER DEFAULT {_DEFAULT_PLAYLIST_LIMIT}
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
		("queue_limit",    f'ALTER TABLE "server_data" ADD COLUMN "queue_limit"    INTEGER DEFAULT {_DEFAULT_QUEUE_LIMIT};'),
		("playlist_limit", f'ALTER TABLE "server_data" ADD COLUMN "playlist_limit" INTEGER DEFAULT {_DEFAULT_PLAYLIST_LIMIT};'),
	]
	try:
		db = await _get_connection()
		async with _lock:
			await db.execute(create_server_data)
			await db.execute(create_bot_admins)
			for col_name, alter_sql in migrate_columns:
				try:
					await db.execute(alter_sql)
				except Exception as e:
					# duplicate column name は正常なケースなので無視する
					if "duplicate column name" not in str(e).lower():
						logger.warning(f"[SQLite] {col_name} カラム追加時の予期せぬエラー: {e}")
			await db.commit()
		logger.info("[SQLite] データベースの初期化が完了しました。")
	except Exception as e:
		logger.error(f"[SQLite] データベース初期化エラー: {e}")
		raise

async def sql_execution(query: str, params: tuple = ()) -> list | None:
	"""
	SQLクエリを実行し、結果行のリストを返す。
	- Lockで排他制御して並行書き込みの競合を防ぐ
	- 失敗時はNoneを返してロガーにエラーを記録する
	"""
	try:
		db = await _get_connection()
		async with _lock:
			async with db.execute(query, params) as cursor:
				result = await cursor.fetchall()
			await db.commit()
			return result
	except Exception as e:
		logger.error(f"[SQLite] クエリ実行エラー: {e} | SQL: {query} | params: {params}")
		return None