import configparser
from yt_dlp.networking.impersonate import ImpersonateTarget

# config.iniの読み込み
config_file = configparser.ConfigParser()
config_file.read('config.ini', encoding='utf-8')

# オプションの読み込みとデフォルト値の設定
class Config:
	def __init__(self):
		self.USER_AGENT = self.get_config('USER_AGENT', "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36")
		self.DATABASE_PATH = self.get_config('database_path', "data.db")
		self.DEFAULT_VOLUME = self.get_config('default_volume', 0.25, value_type=float)
		self.DEFAULT_QUEUE_LIMIT = self.get_config('default_queue_limit', 50, value_type=int)
		self.DEFAULT_PLAYLIST_LIMIT = self.get_config('default_playlist_limit', 10, value_type=int)
		self.MAX_RETRIES = self.get_config('max_retries', 3, value_type=int)
	def get_config(self, key, default, value_type=str):
		try:
			if value_type == bool:
				return config_file.getboolean('MusicBot', key)
			elif value_type == int:
				return config_file.getint('MusicBot', key)
			elif value_type == float:
				return config_file.getfloat('MusicBot', key)
			else:
				return config_file.get('MusicBot', key)
		except (KeyError, ValueError, configparser.NoSectionError):
			return default

app_config = Config()

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
		'User-Agent': Config().USER_AGENT,
		'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
	},
	# yt-dlpのブラウザクッキー読み込み機能を利用して、Firefoxのクッキーを使用する
	'cookiesfrombrowser': ('firefox',)
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
		'User-Agent': Config().USER_AGENT,
	},
	# yt-dlpのブラウザクッキー読み込み機能を利用して、Firefoxのクッキーを使用する
	'cookiesfrombrowser': ('firefox',)
}

FFMPEG_OPTIONS = {
	"before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -analyzeduration 0 -probesize 32",
	"options": "-threads 4 -vn"
}