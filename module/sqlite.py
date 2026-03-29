import aiosqlite
import os

from module.options import DATABASE_PATH

DB_PATH = DATABASE_PATH

async def init_db():
    """
    データベースファイルとテーブルの存在を確認し、存在しない場合は自動生成する。
    """
    # テーブル作成用クエリ
    # スキーマ: guild_id (INTEGER/PRIMARY KEY), volume (REAL)
    create_table_query = """
    CREATE TABLE IF NOT EXISTS "serverData" (
        "guild_id" INTEGER PRIMARY KEY UNIQUE NOT NULL,
        "volume" REAL
    );
    """

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(create_table_query)
            await db.commit()
    except Exception as e:
        # 初期化失敗時のエラー処理
        print(f"[SQL ERROR] データベースの初期化に失敗しました: {e}")

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
        print(f"[SQL ERROR] クエリ実行エラー: {e}")
        return None