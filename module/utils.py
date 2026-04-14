import datetime
import itertools
import os
import re
import asyncio
import sys
import discord
import pyshorteners
import aiohttp
from urllib.parse import urlparse, parse_qs
from module.color import Color

async def loading_spinner(task_future: asyncio.Future, message: str = "処理中"):
	"""
	コンソール用ローディングアニメーション。
	タスクの完了を待機し、結果を返す。エラー時は失敗を表示して例外を投げる。
	"""
	spinner = itertools.cycle(['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'])
	colors = itertools.cycle([Color.RED, Color.YELLOW, Color.GREEN, Color.CYAN, Color.BLUE, Color.MAGENTA])
	clear_line = ' ' * 150
	try:
		while not task_future.done():
			sys.stdout.write(f"\r{next(colors)}[{next(spinner)}] {message}...{Color.RESET}")
			sys.stdout.flush()
			# I/O負荷次第で調整 (0.2秒ごとに更新)
			await asyncio.sleep(0.2)
		sys.stdout.write(f"\r{clear_line}")
		result = task_future.result()
		sys.stdout.write(f"\r{Color.GREEN}[✓] {message} 完了!{Color.RESET}\n")
		sys.stdout.flush()
		return result
	except asyncio.CancelledError:
		sys.stdout.write(f"\r{clear_line}")
		sys.stdout.write(f"\r{Color.YELLOW}[!] {message} キャンセルされました{Color.RESET}\n")
		sys.stdout.flush()
		raise
	except Exception as e:
		sys.stdout.write(f"\r{clear_line}")
		sys.stdout.write(f"\r{Color.RED}[✗] {message} 失敗: {e}{Color.RESET}\n")
		sys.stdout.flush()
		raise e

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
	aiohttpを使用して完全非同期でTinyURL APIを叩き、URLを短縮する。
	"""
	if not url or len(url) < 100:
		return url
	api_url = f"https://tinyurl.com/api-create.php?url={url}"
	try:
		async with aiohttp.ClientSession() as session:
			async with session.get(api_url) as response:
				if response.status == 200:
					return await response.text()
		return url
	except Exception as e:
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
		if match := re.search(yt_regex, url):
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
	h, remainder = divmod(duration, 3600)
	m, s = divmod(remainder, 60)
	if h > 0:
		return f"{h:02}:{m:02}:{s:02}"
	return f"{m:02}:{s:02}"