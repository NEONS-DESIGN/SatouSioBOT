import sqlite3
from module.color import Color

async def sql_execution(sql: str):
    """
    SQLiteを操作するための関数
    辞書型で返します。

    Parameters
    ----------
    :param <sql>:
        <sql>は実行したいSQL文 str型

    Example
    -------
    .. code-block:: python3
        sql = f"SELECT * FROM Example"
        await sql_execution(sql)
    """

    try:
        # カーソルの生成、自動切断
        class AutoCloseCursur(sqlite3.Cursor):
            def __init__(self, connection):
                super().__init__(connection)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.close()

        # SQLiteへアクセス。ファイルが存在しない場合は、自動作成。
        # 自動コミットモードに設定、自動切断
        with sqlite3.connect("data/database.db", isolation_level=None) as con:
            with AutoCloseCursur(con) as cur:
                # SQLの実行
                res = cur.execute(sql)
                # 結果の取得
                result = res.fetchall()

                # print(f"{Color.BG_GREEN}[SQL RESULT]{Color.RESET}:\n{result}")
                return result
    except sqlite3.Error as e:
        raise sqlite3.Error(f"{Color.BG_RED}[SQL ERROR]{Color.RESET}:\n{e}")