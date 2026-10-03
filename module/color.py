class Color:
	"""コンソール出力用のANSIエスケープシーケンス定数群"""
	RED     = '\033[31m'  # 文字色: 赤
	GREEN   = '\033[32m'  # 文字色: 緑
	YELLOW  = '\033[33m'  # 文字色: 黄
	BLUE    = '\033[34m'  # 文字色: 青
	MAGENTA = '\033[35m'  # 文字色: マゼンタ
	CYAN    = '\033[36m'  # 文字色: シアン
	RESET   = '\033[0m'   # 全ての装飾設定をリセット

class Embed:
	"""DiscordのEmbed用カラーコード定数群"""
	RED    = 0xFF4686
	GREEN  = 0x00976B
	BLUE   = 0x1F64E1
	YELLOW = 0xF1C40F
