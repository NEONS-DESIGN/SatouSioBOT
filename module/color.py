class Color:
    """コンソール出力用のANSIエスケープシーケンス定数群"""
    BLACK         = '\033[30m'  # 文字色: 黒
    RED           = '\033[31m'  # 文字色: 赤
    GREEN         = '\033[32m'  # 文字色: 緑
    YELLOW        = '\033[33m'  # 文字色: 黄
    BLUE          = '\033[34m'  # 文字色: 青
    MAGENTA       = '\033[35m'  # 文字色: マゼンタ
    CYAN          = '\033[36m'  # 文字色: シアン
    WHITE         = '\033[37m'  # 文字色: 白
    COLOR_DEFAULT = '\033[39m'  # 文字色をデフォルトにリセット
    BOLD          = '\033[1m'   # 太字装飾
    UNDERLINE     = '\033[4m'   # 下線装飾
    INVISIBLE     = '\033[08m'  # 不可視化
    REVERSE       = '\033[07m'  # 文字色と背景色を反転 (REVERCEから修正)
    BG_BLACK      = '\033[40m'  # 背景色: 黒
    BG_RED        = '\033[41m'  # 背景色: 赤
    BG_GREEN      = '\033[42m'  # 背景色: 緑
    BG_YELLOW     = '\033[43m'  # 背景色: 黄
    BG_BLUE       = '\033[44m'  # 背景色: 青
    BG_MAGENTA    = '\033[45m'  # 背景色: マゼンタ
    BG_CYAN       = '\033[46m'  # 背景色: シアン
    BG_WHITE      = '\033[47m'  # 背景色: 白
    BG_DEFAULT    = '\033[49m'  # 背景色をデフォルトにリセット
    RESET         = '\033[0m'   # 全ての装飾設定をリセット

class Embed:
    """DiscordのEmbed用カラーコード定数群"""
    BLACK       = 0x000000
    WHITE       = 0xffffff
    RED         = 0xff6b6b
    LIGHT_GREEN = 0x1dd1a1