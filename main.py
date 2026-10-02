"""
起動スクリプト。
yt-dlp 抽出用の子プロセス (spawn) はこのファイルを import し直すため、
子プロセスで Bot 本体を読み込まないよう import は __main__ ブロック内で行う。
"""

if __name__ == "__main__":
	from module.bot import main
	main()
