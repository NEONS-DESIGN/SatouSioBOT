import aiosqlite
from module.color import Color

async def sql_execution(sql: str):
    """
    SQLiteデータベースに対して非同期でSQL文を実行し、結果を取得する。

    Parameters
    ----------
    sql : str
        実行するSQL文。

    Returns
    -------
    list
        fetchall()による取得結果のリスト（タプルのリスト）。
        条件に一致するデータが存在しない場合は空のリストを返す。

    Notes
    -----
    - aiosqliteライブラリを使用し、イベントループのブロッキングを防止している。
    - isolation_level=None を指定し、自動コミットモードで動作する。
    - 実行には aiosqlite のインストールが必須。
    """
    try:
        # aiosqliteを使用して非同期でデータベースに接続および切断
        async with aiosqlite.connect("serverData.db", isolation_level=None) as db:
            async with db.execute(sql) as cursor:
                # 実行結果の取得
                result = await cursor.fetchall()
                return result

    except aiosqlite.Error as e:
        # SQLite関連のエラー処理
        raise Exception(f"{Color.BG_RED}[SQL ERROR]{Color.RESET}:\n{e}")
    except Exception as e:
        # その他の予期せぬエラー処理
        raise Exception(f"{Color.BG_RED}[ERROR]{Color.RESET}:\n{e}")