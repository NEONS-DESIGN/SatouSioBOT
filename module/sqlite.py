import aiosqlite
import os

from module.logger import get_bot_logger
from module.options import app_config

logger = get_bot_logger()
DB_PATH = app_config.DATABASE_PATH
VOLUME = app_config.DEFAULT_VOLUME
QUEUE_LIMIT = app_config.DEFAULT_QUEUE_LIMIT
PLAYLIST_LIMIT = app_config.DEFAULT_PLAYLIST_LIMIT

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
		async with aiosqlite.connect(DB_PATH) as db:
			await db.execute(create_serverData_query)
			await db.execute(create_bot_admins_query)
			# 既存のテーブルに新しいカラムが存在するか確認し、なければ追加する
			cursor = await db.execute("PRAGMA table_info('serverData');")
			columns = [row[1] for row in await cursor.fetchall()]
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
	実行前にDBの初期化状態を確認する。

	Parameters
	----------
	query : str
		実行するSQLクエリ
	params : tuple
		クエリに渡すパラメータ

	Returns
	-------
	list
		クエリの実行結果（フェッチデータ）
	"""
	# 実行のたびにファイル存在チェックを行い、なければ初期化
	if not os.path.exists(DB_PATH):
		await init_db()
	try:
		async with aiosqlite.connect(DB_PATH) as db:
			async with db.execute(query, params) as cursor:
				result = await cursor.fetchall()
				await db.commit()
				return result
	except Exception as e:
		# クエリ実行失敗時のエラー処理
		logger.error(f"[SQL ERROR] クエリ実行エラー: {e}")
		return None