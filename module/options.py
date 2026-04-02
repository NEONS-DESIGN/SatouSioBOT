import os
import configparser
from yt_dlp.networking.impersonate import ImpersonateTarget

# config.iniの読み込み
config = configparser.ConfigParser()
config.read('config.ini', encoding='utf-8')

try:
	USER_AGENT = config['MusicBot']['USER_AGENT']
except (KeyError, configparser.NoSectionError):
	USER_AGENT = "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"

try:
	DATABASE_PATH = config['MusicBot']['database_path']
except (KeyError, ValueError, configparser.NoSectionError):
	DATABASE_PATH = "data.db"

try:
	COOKIE_FILE_PATH = config['MusicBot']['cookie_file_path']
except (KeyError, ValueError, configparser.NoSectionError):
	COOKIE_FILE_PATH = "cookies.txt"

YTDLP_OPTIONS = {
	'format': 'bestaudio',
	'audioformat': 'opus',
	'audioquality': 0,
	'writethumbnail': False,
	# 解析のみでダウンロードは行わない
	'extractaudio': False,
	'outtmpl': 'temp/%(extractor)s-%(id)s.%(ext)s',
	'restrictfilenames': True,
	'noplaylist': False,
	'extract_flat': 'in_playlist',
	'playlistend': 50,
	'nocheckcertificate': True,
	# エラーを無視して処理を続行する
	'ignoreerrors': False,
	'logtostderr': False,
	# コンソール出力を抑制
	'quiet': True,
	'no_warnings': True,
	# Python 3.13以降用のImpersonateTarget指定
	# 内部のアサーション(isinstance)を通過させるためオブジェクトを生成して渡す
	'impersonate': ImpersonateTarget.from_str('chrome'),
	# 解析用のjsランタイムを指定
	'js_runtimes': {
		'deno': {},
		# 'node': {}, # nodeは環境によってはエラーになるため一旦外す
	},
	'allow_remote_strings': True,
	'remote_components': ['ejs:github'],
	'default_search': 'ytsearch',
	# IPv4優先
	'source_address': '0.0.0.0',
	'force_ipv4': True,
	'extractor_args': {
		'youtube': {'player_client': ['web_music', 'android']},
		'nicovideo': {'action_wait_time': 1.0},
	},
	# HTTPリクエストヘッダーのカスタマイズ
	'headers': {
		'User-Agent': USER_AGENT,
		'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
	},
}

# 動画本体のURLを解析せずメタデータのみ取得
FAST_META_OPTIONS = {
	'extract_flat': True,
	'quiet': True,
	'no_warnings': True,
	'default_search': 'ytsearch',
	'extractor_args': {
		'youtube': {'player_client': ['web_music', 'android']},
		'nicovideo': {'action_wait_time': 1.0},
	},
	'headers': {
		'User-Agent': USER_AGENT,
	}
}

if os.path.exists(COOKIE_FILE_PATH):
	YTDLP_OPTIONS['cookiefile'] = COOKIE_FILE_PATH
	FAST_META_OPTIONS['cookiefile'] = COOKIE_FILE_PATH

FFMPEG_OPTIONS = {
	"before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
	"options": "-threads 4 -vn"
}