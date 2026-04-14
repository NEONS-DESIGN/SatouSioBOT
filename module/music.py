import asyncio
import collections
import concurrent.futures
from aiocache import Cache, cached
import discord
from discord.ext import commands
from typing import Dict
import sys

from module.color import Color
from module.embed import *
from module.logger import get_bot_logger
from module.options import FFMPEG_OPTIONS, app_config
from module.utils import loading_spinner, shorten_url
from module.sqlite import sql_execution

# ロガーの取得
logger = get_bot_logger()

# プロセスプールとワーカー用のキャッシュ設定（ノイズ対策）
_process_pool = None
# 別プロセス内でyt-dlpを使い回すためのキャッシュ（起動の高速化）
_ytdl_cache = {}

def get_process_pool():
	"""
	プロセスプールを安全に取得する。
	別プロセスに処理を逃がすことで、PythonのGIL（排他ロック）を回避する。
	"""
	global _process_pool
	if _process_pool is None:
		# 最大3つの別プロセスを立ち上げて並列処理
		_process_pool = concurrent.futures.ProcessPoolExecutor(max_workers=3)
	return _process_pool

# TTLキャッシュ（有効期限付きメモリ保存）
# ttl=10800 は3時間（3時間でYouTubeのURLが無効になる前に破棄する）
@cached(ttl=10800, cache=Cache.MEMORY)
async def fetch_track_info_with_cache(query: str, is_fast: bool):
	"""
	キャッシュを確認し、無ければ別プロセスでyt-dlpを動かして解析する関数。
	※引数(query, is_fast)の組み合わせが「キャッシュの鍵」になります。
	"""
	logger = get_bot_logger()
	logger.info(f"🔍 [CACHE MISS] 新規取得を実行します: {query}")

	loop = asyncio.get_running_loop()
	pool = get_process_pool()
	# 別プロセスに処理を投げて結果を待つ
	return await loop.run_in_executor(pool, extract_info_process, query, is_fast)

def extract_info_process(query: str, is_fast: bool):
	"""完全に独立したプロセスで実行される解析関数"""
	from yt_dlp import YoutubeDL
	from module.options import YTDLP_OPTIONS, FAST_META_OPTIONS

	cache_key = 'fast' if is_fast else 'normal'
	# そのプロセス内で初めて呼ばれた時だけインスタンスを生成する
	if cache_key not in _ytdl_cache:
		opts = FAST_META_OPTIONS if is_fast else YTDLP_OPTIONS
		_ytdl_cache[cache_key] = YoutubeDL(opts)
	return _ytdl_cache[cache_key].extract_info(query, download=False)

class GuildMusicPlayer:
	def __init__(self, guild_id: int, bot: commands.Bot):
		self.guild_id = guild_id
		self.bot = bot
		self.queue = collections.deque()
		self.prefetch_queue = asyncio.Queue()
		self.loop = False
		self.current = None
		self.worker_task = None
		self.voice_client = None

	def start_worker(self):
		if self.worker_task is None or self.worker_task.done():
			self.worker_task = self.bot.loop.create_task(guild_prefetch_worker(self))

	def cleanup(self):
		if self.worker_task and not self.worker_task.done():
			self.worker_task.cancel()
		self.queue.clear()
		while not self.prefetch_queue.empty():
			try:
				self.prefetch_queue.get_nowait()
				self.prefetch_queue.task_done()
			except asyncio.QueueEmpty:
				break
		self.current = None

# サーバーごとの状態管理用辞書
server_music_data: Dict[int, GuildMusicPlayer] = {}

class YTDLSource(discord.PCMVolumeTransformer):
	def __init__(self, source: discord.AudioSource, *, data: dict, display_url: str, volume: float = 0.25):
		super().__init__(source, volume)
		self.data = data
		self.title = data.get('title', 'Unknown Title')
		self.display_url = display_url
	@classmethod
	async def from_track(cls, track: dict, volume: float = 0.25): #ワーカーによって事前解析されたTrackデータからAudioSourceを生成する。
		filename = track.get('stream_url')
		if not filename:
			raise ValueError("ストリームURLが存在しません。")
		http_headers = track.get('http_headers', {})
		header_str = "".join([f"{k}: {v}\r\n" for k, v in http_headers.items()])
		dynamic_before_options = FFMPEG_OPTIONS['before_options']
		if header_str:
			dynamic_before_options += f" -headers \"{header_str}\""
		return cls(
			discord.FFmpegPCMAudio(
				filename,
				before_options=dynamic_before_options,
				options=FFMPEG_OPTIONS['options'],
				stderr=sys.stderr
			),
			data=track,
			display_url=track.get("display_url", ""),
			volume=volume
		)

async def guild_prefetch_worker(player: GuildMusicPlayer):
	"""
	各ギルドに1つだけ存在するバックグラウンドワーカー。
	キューから曲を1つずつ取り出し、順番にストリームURLの取得を行う。
	"""
	sys.stdout.write(f"{Color.CYAN}[⚙️ WORKER] ギルド {player.guild_id} のプレフェッチワーカーが起動しました。{Color.RESET}\n")
	sys.stdout.flush()
	pool = get_process_pool() # プロセスプールを取得
	while True:
		try:
			track = await player.prefetch_queue.get()
			if not track.get("stream_url") and not track.get("error"):
				# 再取得試行回数 (デフォルトは3回)
				max_retries = app_config.MAX_RETRIES
				# 指定の試行回数まで取得できるまで繰り返す。
				for attempt in range(max_retries):
					try:
						fetch_task = player.bot.loop.run_in_executor(pool, extract_info_process, track["url"], False)
						info = await loading_spinner(fetch_task, f"音源のロード中: {track['title']} (試行 {attempt+1}/{max_retries})")
						if 'entries' in info and len(info['entries']) > 0:
							info = info['entries'][0]
						track["stream_url"] = info.get('url')
						track["http_headers"] = info.get('http_headers', {})
						track["duration"] = info.get("duration", track.get("duration"))
						break
					except Exception as e:
						if attempt == max_retries - 1:
							track["error"] = e
						else:
							sys.stdout.write(f"\r{Color.YELLOW}[!] {track['title']} のロードに失敗。リトライします...{Color.RESET}\n")
							sys.stdout.flush()
							await asyncio.sleep(2)
			track["ready_event"].set()
			player.prefetch_queue.task_done()
		except asyncio.CancelledError:
			# 終了ログも装飾して出力
			sys.stdout.write(f"{Color.CYAN}[⚙️ WORKER] ギルド {player.guild_id} のワーカーが終了しました。{Color.RESET}\n")
			sys.stdout.flush()
			break
		except Exception as e:
			# 予期せぬ致命的エラーは赤色で出力
			sys.stdout.write(f"{Color.RED}[⚙️ WORKER FATAL] 予期せぬエラー: {e}{Color.RESET}\n")
			sys.stdout.flush()
			await asyncio.sleep(1)

async def ensure_guild_data(guild_id: int, bot: commands.Bot = None) -> GuildMusicPlayer:
	"""指定ギルドのデータ領域とワーカーを初期化する。"""
	if guild_id not in server_music_data:
		server_music_data[guild_id] = GuildMusicPlayer(guild_id, bot)
	player = server_music_data[guild_id]
	if bot:
		player.start_worker()
	return player

async def play_next_song(ctx: commands.Context, bot: commands.Bot):
	"""キューから次の曲を取得し、再生処理を行う"""
	guild_id = ctx.guild.id
	player_data = server_music_data.get(guild_id)
	if not player_data: return

	voice_client = ctx.guild.voice_client
	if player_data.loop and player_data.current:
		player_data.current["ready_event"].clear()
		player_data.queue.append(player_data.current)
		player_data.prefetch_queue.put_nowait(player_data.current)
	if not player_data.queue:
		player_data.current = None
		if voice_client and voice_client.is_connected():
			await voice_client.disconnect()
		await play_completed_embed(ctx)
		player_data.cleanup()
		return
	next_track = player_data.queue.popleft()
	player_data.current = next_track
	try:
		if not next_track["ready_event"].is_set():
			wait_msg = await preparing_audio_embed(ctx)
			await next_track["ready_event"].wait()
			try:
				await wait_msg.delete()
			except discord.NotFound:
				pass
		if next_track.get("error"):
			await skip_error_embed(ctx, next_track['title'])
			return await play_next_song(ctx, bot)
		guild_vol = await sql_execution(f"SELECT volume FROM serverData WHERE guild_id=?;", (guild_id,))
		vol = guild_vol[0][0] if guild_vol else app_config.DEFAULT_VOLUME
		player = await YTDLSource.from_track(next_track, volume=vol)
		def after_playing(error):
			if error:
				logger.error(f"再生時エラー: {error}")
			asyncio.run_coroutine_threadsafe(play_next_song(ctx, bot), bot.loop)
		voice_client.play(player, after=after_playing)
		await music_info_embed(
			ctx=ctx,
			title=player.title,
			display_url=player.display_url,
			duration_raw=player.data.get('duration', 0),
			thumbnail_url=player.data.get("thumbnail"),
			queue_count=len(player_data.queue),
			wait_msg=wait_msg
		)
	except Exception as e:
		logger.error(f"再生ソース生成エラー: {e}")
		await playback_error_embed(ctx, next_track.get('title', '不明な曲'))
		await play_next_song(ctx, bot)

async def music_info_embed(ctx: commands.Context, player: YTDLSource, queue_count: int):
	"""再生中の曲情報をEmbedで送信する"""
	try:
		embed = discord.Embed(title="🎵 再生中", color=Embed.GREEN)
		title_str = str(player.title)
		display_url = await shorten_url(player.display_url)
		if len(title_str) > 100:
			title_str = title_str[:97] + "..."
		field_value = f"[{title_str}]({display_url})"
		if len(field_value) > 1024:
			field_value = title_str[:1024]
		embed.add_field(name="タイトル", value=field_value, inline=False)
		raw_duration = player.data.get('duration') or 0
		duration = await play_time(raw_duration)
		embed.add_field(name="再生時間", value=duration, inline=True)
		embed.add_field(name="待機数", value=f"{queue_count} 曲", inline=True)
		icon_url = ctx.author.display_avatar.url if ctx.author.display_avatar else None
		embed.set_footer(text=f"Requested by: {ctx.author.display_name}", icon_url=icon_url)
		thumbnail_url = player.data.get("thumbnail")
		if thumbnail_url:
			embed.set_image(url=thumbnail_url)
			await ctx.send(embed=embed)
		else:
			fallback_url = "https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4?q=80&w=1024&auto=format&fit=crop"
			embed.set_image(url=fallback_url)
			await ctx.send(embed=embed)
	except Exception as e:
		logger.error(f"Embed表示エラー: {e}")
		fallback_title = player.title if player and player.title else "Unknown Title"
		try:
			await music_info_fallback_embed(ctx, fallback_title)
		except Exception:
			pass

async def play_music(ctx: commands.Context, url: str, bot: commands.Bot):
	"""入力値からメタデータを抽出し、キューとワーカーに渡す"""
	player_data = await ensure_guild_data(ctx.guild.id, bot)
	is_input_url = url.startswith(('http://', 'https://'))
	# URLがプレイリストかどうかを判定
	is_playlist_url = is_input_url and ('list=' in url or 'playlist' in url)
	is_playing_immediately = not player_data.current and not ctx.guild.voice_client.is_playing()
	wait_msg = None
	if is_playing_immediately:
		wait_msg = await preparing_audio_embed(ctx)
	pool = get_process_pool() # プロセスプールを取得
	try:
		# 単一の直接URLの場合は、最初からフル解析(ytdl)を行い二度手間を防ぐ
		if is_input_url and not is_playlist_url:
			fetch_task = fetch_track_info_with_cache(url, False)
			info = await loading_spinner(fetch_task, "音源の取得")
			if info is None: raise ValueError("情報の取得に失敗")
			entries = [info]
			is_playlist = False
		else:
			search_query = url if is_input_url else f"ytsearch1:{url}"
			fetch_task = fetch_track_info_with_cache(search_query, True)
			info = await loading_spinner(fetch_task, "メタデータ検索")
			if info is None: raise ValueError("情報の取得に失敗")
			entries = info.get('entries', [info]) if 'entries' in info else [info]
			is_playlist = 'entries' in info and not search_query.startswith('ytsearch')

		# DBからキューとプレイリストの上限値を取得。存在しない場合はデフォルト値を使用
		limits = await sql_execution(f"SELECT queue_limit, playlist_limit FROM serverData WHERE guild_id=?;", (ctx.guild.id,))
		queue_limit = limits[0][0] if limits else app_config.DEFAULT_QUEUE_LIMIT
		playlist_limit = limits[0][1] if limits else app_config.DEFAULT_PLAYLIST_LIMIT

		queued_count = 0
		if is_playlist:
			entries = entries[:playlist_limit]
		available_slots = queue_limit - len(player_data.queue)
		if available_slots <= 0:
			raise ValueError(f"キューの最大曲数({queue_limit}曲)に達しているため、これ以上追加できません。")
		entries = entries[:available_slots]
		for entry in entries:
			if entry is None: continue
			video_id = entry.get("id")
			# 生のURL(url)より先に、ページURL(webpage_url)を優先して取得する
			track_url = entry.get("webpage_url") or entry.get("original_url")
			if not track_url and video_id:
				track_url = f"https://www.youtube.com/watch?v={video_id}"
			# 最終フォールバック
			if not track_url:
				track_url = entry.get("url")
			if not track_url:
				continue
			thumb_url = entry.get("thumbnail")
			if not thumb_url and entry.get("thumbnails"):
				thumb_url = entry.get("thumbnails")[-1].get("url")
			track_data = {
				"url": track_url,
				"display_url": url if is_input_url and not is_playlist else track_url,
				"title": entry.get("title", "Unknown Title"),
				"author_id": ctx.author.id,
				"thumbnail": thumb_url,
				"duration": entry.get("duration", 0),
				"stream_url": None,
				"http_headers": {},
				"error": None,
				"ready_event": asyncio.Event()
			}

			# 単一直接URLの場合は既にストリームURLが取得できているため、ワーカーの処理をスキップさせる
			if is_input_url and not is_playlist_url:
				track_data["stream_url"] = entry.get('url')
				track_data["http_headers"] = entry.get('http_headers', {})
				track_data["ready_event"].set()

			player_data.queue.append(track_data)
			player_data.prefetch_queue.put_nowait(track_data)
			queued_count += 1
			if not is_playlist:
				single_info = track_data
		if queued_count == 0:
			raise ValueError("再生可能な動画が見つからない")
	except Exception as e:
		logger.error(f"play_music解析エラー: {e}")
		await load_error_embed(ctx, e)
		return
	if is_playlist:
		await playlist_added_embed(ctx, info, queued_count)
	else:
		await queue_added_embed(ctx, single_info, len(player_data.queue))
	if not player_data.current and not ctx.guild.voice_client.is_playing():
		bot.loop.create_task(play_next_song(ctx, bot))