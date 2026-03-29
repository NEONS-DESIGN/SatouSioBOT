import os
import configparser
import sys
import re
from module.color import Color
# ImpersonateTargetを直接インポート
from yt_dlp.networking.impersonate import ImpersonateTarget

# config.iniの読み込み
config = configparser.ConfigParser()
config.read('config.ini', encoding='utf-8')

try:
	PLAYLIST_LIMIT = int(config['MusicBot']['playlist_limit'])
except (KeyError, ValueError, configparser.NoSectionError):
	PLAYLIST_LIMIT = 10

try:
	FFMPEG_HEADERS = config['MusicBot']['ffmpeg_headers']
except (KeyError, configparser.NoSectionError):
	FFMPEG_HEADERS = "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"

try:
	COOKIE_FILE_PATH = config['MusicBot']['cookie_file_path']
except (KeyError, ValueError, configparser.NoSectionError):
	COOKIE_FILE_PATH = "cookies.txt"

class YTDLLogger:
	"""
	yt-dlpログ出力制御用カスタムロガー。
	"""
	def debug(self, msg):
		try:
			match = re.search(r'Downloading (?:video|item) (\d+) of (\d+)', msg)
			if match:
				current = int(match.group(1))
				total = int(match.group(2))
				percent = current / total if total > 0 else 0
				bar_width = 40
				filled = int(bar_width * percent)

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

				if current == total:
					sys.stdout.write("\n")
					sys.stdout.flush()
		except Exception:
			pass

	def warning(self, msg):
		pass

	def error(self, msg):
		print(f"\n[YTDL ERROR] {msg}")

def yt_dlp_progress_hook(d):
	"""
	ダウンロード進捗を監視しコンソールに描画する。
	"""
	try:
		if d['status'] == 'downloading':
			total = d.get('total_bytes') or d.get('total_bytes_estimate')
			if total:
				percent = d.get('downloaded_bytes', 0) / total
				bar_width = 40
				filled = int(bar_width * percent)

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
		pass

youtube_args = {
	'player_client': ['web_music', 'android'],
}

nicovideo_args = {
	'action_wait_time': 1.0,
}

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
	'ignoreerrors': False,
	'logtostderr': False,
	'quiet': False,
	'no_warnings': True,
	'logger': YTDLLogger(),
	'progress_hooks': [yt_dlp_progress_hook],

	# Python 3.13以降用のImpersonateTargetオプション
	# 文字列ではなくImpersonateTargetオブジェクトを生成して渡す
	# これにより内部のassert isinstance(target, ImpersonateTarget)をパスする
	'impersonate': ImpersonateTarget.from_str('chrome'),

	'js_runtimes': {
		'deno': {},
		'node': {}
	},
	'allow_remote_strings': True,
	'remote_components': ['ejs:github'],
	'default_search': 'ytsearch',
	'source_address': '0.0.0.0',
	'extractor_args': {
		'youtube': youtube_args,
		'nicovideo': nicovideo_args
	},
	'headers': {
		'User-Agent': FFMPEG_HEADERS,
		'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
	},
}

if os.path.exists(COOKIE_FILE_PATH):
	YTDLP_OPTIONS['cookiefile'] = COOKIE_FILE_PATH

FFMPEG_OPTIONS = {
	"before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
	"options": "-threads 4 -vn"
}