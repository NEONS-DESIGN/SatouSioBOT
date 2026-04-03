import datetime
import os
import re
import asyncio
import discord
import pyshorteners
import aiohttp
from urllib.parse import urlparse, parse_qs
from module.color import Color

class Link(discord.ui.View):
	def __init__(self, url: str):
		super().__init__()
		self.url = url
		# 非同期でボタンを追加するためのタスクを作成
		asyncio.create_task(self.add_short_link_button())
	async def add_short_link_button(self):
		"""別スレッドでURL短縮を実行し、ボタンを追加する"""
		api_key = os.getenv("bitly_api")
		if not api_key:
			return
		loop = asyncio.get_event_loop()
		short_url = None
		for i in range(3):  # 最大3回試行
			try:
				# pyshortenersは同期ライブラリなのでexecutorで実行してブロックを防ぐ
				s = pyshorteners.Shortener(api_key=api_key)
				short_url = await loop.run_in_executor(None, lambda: s.bitly.short(self.url))

				print(f"{Color.BG_GREEN}[Shortener]{Color.RESET}: Successful shortener of {Color.BOLD}{short_url}")
				self.add_item(discord.ui.Button(label="ダウンロード", url=short_url))
				break
			except Exception as e:
				print(f"{Color.RED}[ERROR]{Color.RESET} 短縮失敗 (残り {2-i}回): {e}")
				await asyncio.sleep(1)

async def shorten_url(url: str):
	"""
	URLの文字列長が一定（100文字）を超える場合、短縮URLを生成して返却する。
	APIキー不要のTinyURLを使用し、同期処理を非同期スレッドで実行する。

	Parameters
	----------
	url : str
		短縮対象のURL

	Returns
	-------
	str
		短縮されたURL、またはエラー・短尺時のオリジナルURL
	"""
	# 短いURLはそのまま返却して処理を効率化する
	if not url or len(url) < 100:
		return url
	try:
		s = pyshorteners.Shortener()
		# 同期ライブラリの実行によるイベントループの停止を回避
		# TinyURLはAPIキーなしで利用可能
		short_url = await asyncio.to_thread(s.tinyurl.short, url)
		return short_url
	except Exception as e:
		# 短縮失敗時はオリジナルのURLを返却し、処理を継続させる
		print(f"{Color.RED}[ERROR]{Color.RESET} URL短縮に失敗しました: {e}")
		return url

async def download_file(url: str, dst_path: str):
	"""aiohttpを使用して非同期にファイルをダウンロードする"""
	try:
		async with aiohttp.ClientSession() as session:
			async with session.get(url) as response:
				if response.status == 200:
					with open(dst_path, 'wb') as f:
						f.write(await response.read())
					print(f"{Color.BG_GREEN}[Downloader]{Color.RESET}: Successful download of {Color.BOLD}{url}")
				else:
					print(f"{Color.RED}[ERROR]{Color.RESET} Download failed with status: {response.status}")
	except Exception as e:
		print(f"{Color.RED}[ERROR]{Color.RESET} Download Exception: {e}")

async def get_id(url: str):
	"""
	URLから動画IDとサイト種別を抽出する
	YouTube (Shorts, Music, 通常), NicoNico に対応
	"""
	if not url.startswith(("http://", "https://")):
		return "", "title"
	# YouTube用正規表現 (通常の動画IDは11桁)
	yt_regex = r'(?:v=|\/)([0-9A-Za-z_-]{11})'
	parsed_url = urlparse(url)
	domain = parsed_url.netloc
	path = parsed_url.path
	# YouTube (youtube.com / youtu.be / music.youtube.com)
	if "youtube.com" in domain or "youtu.be" in domain:
		match = re.search(yt_regex, url)
		if match:
			return match.group(1), "youtube"
	# NicoNico
	elif "nicovideo.jp" in domain:
		# /watch/sm12345 の 'sm12345' 部分を抽出
		vid = path.split("/")[-1]
		return vid, "niconico"
	# どれにも一致しない場合、とりあえずURLとして扱うかエラー
	return "", "url"

async def play_time(duration: int):
	"""秒数を HH:MM:SS または MM:SS 形式に変換"""
	if not duration:
		return "00:00"
	td = datetime.timedelta(seconds=duration)
	total_seconds = int(td.total_seconds())
	h, remainder = divmod(total_seconds, 3600)
	m, s = divmod(remainder, 60)
	if h > 0:
		return f"{h:02}:{m:02}:{s:02}"
	return f"{m:02}:{s:02}"