import datetime
import itertools
import os
import re
import asyncio
import sys
import discord
import pyshorteners
import aiohttp
from urllib.parse import urlparse
from module.color import Color

# =======================================================
# aiohttp シングルトンセッション (TCPコネクションの使い回し)
# =======================================================
_aiohttp_session = None

async def get_session() -> aiohttp.ClientSession:
	"""モジュール全体で単一のaiohttpセッションを使い回す"""
	global _aiohttp_session
	if _aiohttp_session is None or _aiohttp_session.closed:
		_aiohttp_session = aiohttp.ClientSession()
	return _aiohttp_session

async def loading_spinner(task_future, message: str = "処理中"):
	"""
	コンソール用ローディングアニメーション。
	コルーチンが渡された場合は自動的にTaskにラップして実行する。
	"""
	# aiocacheの戻り値(コルーチン)を安全にTask化する
	if asyncio.iscoroutine(task_future):
		task_future = asyncio.create_task(task_future)

	spinner = itertools.cycle(['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'])
	colors = itertools.cycle([Color.RED, Color.YELLOW, Color.GREEN, Color.CYAN, Color.BLUE, Color.MAGENTA])
	clear_line = ' ' * 150
	try:
		while not task_future.done():
			sys.stdout.write(f"\r{next(colors)}[{next(spinner)}] {message}...{Color.RESET}")
			sys.stdout.flush()
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
		asyncio.create_task(self.add_short_link_button())
	async def add_short_link_button(self):
		api_key = os.getenv("bitly_api")
		if not api_key:
			return
		loop = asyncio.get_event_loop()
		for i in range(3):
			try:
				s = pyshorteners.Shortener(api_key=api_key)
				short_url = await loop.run_in_executor(None, lambda: s.bitly.short(self.url))
				print(f"{Color.BG_GREEN}[Shortener]{Color.RESET}: Successful shortener of {Color.BOLD}{short_url}")
				self.add_item(discord.ui.Button(label="ダウンロード", url=short_url))
				break
			except Exception as e:
				print(f"{Color.RED}[ERROR]{Color.RESET} 短縮失敗 (残り {2-i}回): {e}")
				await asyncio.sleep(1)

async def shorten_url(url: str):
	if not url or len(url) < 100:
		return url
	api_url = f"https://tinyurl.com/api-create.php?url={url}"
	try:
		session = await get_session() # シングルトンセッションを使用
		async with session.get(api_url) as response:
			if response.status == 200:
				return await response.text()
		return url
	except Exception as e:
		print(f"{Color.RED}[ERROR]{Color.RESET} URL短縮に失敗しました: {e}")
		return url

async def download_file(url: str, dst_path: str):
	try:
		session = await get_session() # シングルトンセッションを使用
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
	if not url.startswith(("http://", "https://")):
		return "", "title"
	yt_regex = r'(?:v=|\/)([0-9A-Za-z_-]{11})'
	parsed_url = urlparse(url)
	domain = parsed_url.netloc
	path = parsed_url.path
	if "youtube.com" in domain or "youtu.be" in domain:
		if match := re.search(yt_regex, url):
			return match.group(1), "youtube"
	elif "nicovideo.jp" in domain:
		vid = path.split("/")[-1]
		return vid, "niconico"
	return "", "url"

async def play_time(duration: int):
	if not duration:
		return "00:00"
	h, remainder = divmod(duration, 3600)
	m, s = divmod(remainder, 60)
	if h > 0:
		return f"{h:02}:{m:02}:{s:02}"
	return f"{m:02}:{s:02}"