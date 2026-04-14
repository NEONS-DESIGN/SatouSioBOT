import aiosqlite
import os
import asyncio

from module.logger import get_bot_logger
from module.options import app_config

logger = get_bot_logger()
DB_PATH = app_config.DATABASE_PATH
VOLUME = app_config.DEFAULT_VOLUME
QUEUE_LIMIT = app_config.DEFAULT_QUEUE_LIMIT
PLAYLIST_LIMIT = app_config.DEFAULT_PLAYLIST_LIMIT

# =======================================================
# データベース接続の永続化とスレッドセーフなロック管理
# =======================================================
_db_connection = None
_db_lock = asyncio.Lock()

async def get_db() -> aiosqlite.Connection:
	"""単一の永続的なデータベースコネクションを取得する"""
	global _db_connection
	if _db_connection is None:
		_db_connection = await aiosqlite.connect(DB_PATH)
		# 読み書きの並行処理性能を劇的に上げるWALモードを有効化
		await _db_connection.execute("PRAGMA journal_mode=WAL;")
		await _db_connection.commit()
	return _db_connection

async def init_db():
	"""
	データベースファイルとテーブルの存在を確認し、
	構造が古い場合は新しいカラムやテーブルを自動で追加する。
	"""
	create_serverData_query = f"""
	CREATE TABLE IF NOT EXISTS "serverData" (
		"guild_id" INTEGER PRIMARY KEY UNIQUE NOT NULL,
		"volume" REAL DEFAULT {VOLUME},
		"queue_limit" INTEGER DEFAULT {QUEUE_LIMIT},
		"playlist_limit" INTEGER DEFAULT {PLAYLIST_LIMIT}
	);
	"""
	create_bot_admins_query = """
	CREATE TABLE IF NOT EXISTS "bot_admins" (
		"guild_id" INTEGER NOT NULL,
		"user_id" INTEGER NOT NULL,
		PRIMARY KEY ("guild_id", "user_id")
	);
	"""
	try:
		db = await get_db()
		async with _db_lock:
			await db.execute(create_serverData_query)
			await db.execute(create_bot_admins_query)
			cursor = await db.execute("PRAGMA table_info('serverData');")
			columns = [row for row in await cursor.fetchall()]
			if "queue_limit" not in columns:
				await db.execute(f'ALTER TABLE "serverData" ADD COLUMN "queue_limit" INTEGER DEFAULT {QUEUE_LIMIT};')
			if "playlist_limit" not in columns:
				await db.execute(f'ALTER TABLE "serverData" ADD COLUMN "playlist_limit" INTEGER DEFAULT {PLAYLIST_LIMIT};')
			await db.commit()
	except Exception as e:
		print(f"[SQL ERROR] データベース初期化エラー: {e}")

async def sql_execution(query: str, params: tuple = ()):
	"""
	SQLクエリを実行し、結果をリスト形式で返却する。
	排他制御(Lock)を使用し、高速な永続コネクションを使い回す。
	"""
	try:
		db = await get_db()
		async with _db_lock:
			async with db.execute(query, params) as cursor:
				result = await cursor.fetchall()
				await db.commit()
				return result
	except Exception as e:
		logger.error(f"[SQL ERROR] クエリ実行エラー: {e}")
		return None