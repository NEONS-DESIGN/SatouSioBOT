import os
import configparser
import sys
import re
from module.color import Color

PO_TOKEN = os.getenv("po_token")
VISITOR_DATA = os.getenv("visitor_data")

# config.iniの読み込み
# 失敗した場合や値が不正な場合はデフォルト値の10を適用する
config = configparser.ConfigParser()
config.read('config.ini', encoding='utf-8')

try:
    PLAYLIST_LIMIT = int(config['MusicBot']['playlist_limit'])
except (KeyError, ValueError, configparser.NoSectionError):
    PLAYLIST_LIMIT = 10

COOKIE_FILE_PATH = "cookies.txt"

class YTDLLogger:
    """
    yt-dlpログ出力制御用カスタムロガー。
    対象ログのみ抽出し、コンソールへプログレスバーとして描画する。
    注意: 対象外のログはpassにより破棄される。
    """
    def debug(self, msg):
        try:
            # プレイリスト等の解析進捗を正規表現で抽出
            match = re.search(r'Downloading (?:video|item) (\d+) of (\d+)', msg)
            if match:
                current = int(match.group(1))
                total = int(match.group(2))
                percent = current / total if total > 0 else 0
                bar_width = 40
                filled = int(bar_width * percent)

                # 進行状態に応じた色と構成文字の変更
                if percent < 0.33:
                    color = Color.RED
                    char = '░'
                elif percent < 0.66:
                    color = Color.YELLOW
                    char = '▒'
                elif percent < 1.0:
                    color = Color.GREEN
                    char = '▓'
                else:
                    color = Color.CYAN
                    char = '█'

                bar = char * filled + '-' * (bar_width - filled)
                sys.stdout.write(f"\r{color}[解析] {percent*100:5.1f}% |{bar}| {current}/{total}{Color.RESET}")
                sys.stdout.flush()

                # 完了時に改行を挿入してコンソールの表示崩れを防ぐ
                if current == total:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
        except Exception:
            # 描画処理でのクラッシュを防止
            pass

    def warning(self, msg):
        pass

    def error(self, msg):
        # 致命的なエラーのみ標準出力へ通す
        print(f"\n[YTDL ERROR] {msg}")

def yt_dlp_progress_hook(d):
    """
    yt-dlpのダウンロード進捗を監視しプログレスバーを描画する。
    注意: 外部ライブラリのコールバックとして呼び出される。
    """
    try:
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate')
            if total:
                percent = d.get('downloaded_bytes', 0) / total
                bar_width = 40
                filled = int(bar_width * percent)

                # 進行状態に応じた色と構成文字の変更
                if percent < 0.33:
                    color = Color.RED
                    char = '░'
                elif percent < 0.66:
                    color = Color.YELLOW
                    char = '▒'
                elif percent < 1.0:
                    color = Color.GREEN
                    char = '▓'
                else:
                    color = Color.CYAN
                    char = '█'

                bar = char * filled + '-' * (bar_width - filled)
                speed = d.get('speed', 0)
                speed_str = f"{speed / 1024 / 1024:.2f}MB/s" if speed else "---"

                sys.stdout.write(f"\r{color}[DL] {percent*100:5.1f}% |{bar}| {speed_str}{Color.RESET}")
                sys.stdout.flush()
        elif d['status'] == 'finished':
            sys.stdout.write(f"\r{Color.CYAN}[DL] 100.0% |{'█'*40}| Complete{Color.RESET}\n")
            sys.stdout.flush()
    except Exception:
        # 描画処理でのクラッシュを防止
        pass

youtube_args = {
    'player_client': ['web_music', 'android']
}
if PO_TOKEN:
    youtube_args['po_token'] = [f'web_music.gvs+{PO_TOKEN}']
if VISITOR_DATA:
    youtube_args['visitor_data'] = [VISITOR_DATA]

YTDLP_OPTIONS = {
    'format': 'bestaudio/best',
    'writethumbnail': False,
    'extractaudio': False,
    'outtmpl': 'temp/%(extractor)s-%(id)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': False,
    'extract_flat': 'in_playlist',
    'playlistend': PLAYLIST_LIMIT,
    'nocheckcertificate': True,
    'ignoreerrors': True,

    # ロガーを機能させるために標準出力を有効化
    'logtostderr': False,
    'quiet': False,
    'no_warnings': True,

    # カスタムロガーおよび進捗フックの適用
    'logger': YTDLLogger(),
    'progress_hooks': [yt_dlp_progress_hook],

    'default_search': 'ytsearch',
    'source_address': '0.0.0.0',
    'js_runtimes': {'deno': {}},
    'allow_remote_strings': True,
    'remote_components': ['ejs:github'],
    'extractor_args': {
        'youtube': youtube_args
    },
    'headers': {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
        'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
    },
}

if os.path.exists(COOKIE_FILE_PATH):
    YTDLP_OPTIONS['cookiefile'] = COOKIE_FILE_PATH
else:
    print(f"[WARNING] {COOKIE_FILE_PATH} が見つかりません。Cookieなしで実行します。")

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-threads 4 -vn"
}