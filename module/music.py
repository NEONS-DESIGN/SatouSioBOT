import asyncio
import collections
import concurrent.futures
from aiocache import Cache, cached
import discord
from discord.ext import commands
from typing import Dict, Optional
import sys

from module.color import Color
from module.embed import *
from module.logger import get_bot_logger
from module.options import FFMPEG_OPTIONS, app_config
from module.utils import loading_spinner, shorten_url
from module.sqlite import sql_execution

logger = get_bot_logger()

_process_pool: Optional[concurrent.futures.ProcessPoolExecutor] = None
_ytdl_cache: dict = {}

def get_process_pool() -> concurrent.futures.ProcessPoolExecutor:
	global _process_pool
	if _process_pool is None:
		_process_pool = concurrent.futures.ProcessPoolExecutor(max_workers=app_config.MAX_WORKER_THREADS)
	return _process_pool

@cached(ttl=app_config.CACHE_TTL, cache=Cache.MEMORY)
async def fetch_track_info_with_cache(query: str, is_fast: bool) -> dict:
	logger.info(f"キャッシュの新規取得を実行します: {query}")
	loop = asyncio.get_running_loop()
	pool = get_process_pool()
	return await loop.run_in_executor(pool, extract_info_process, query, is_fast)

def extract_info_process(query: str, is_fast: bool) -> dict:
	from yt_dlp import YoutubeDL
	from module.options import YTDLP_OPTIONS, FAST_META_OPTIONS
	cache_key = 'fast' if is_fast else 'normal'
	if cache_key not in _ytdl_cache:
		opts = FAST_META_OPTIONS if is_fast else YTDLP_OPTIONS
		_ytdl_cache[cache_key] = YoutubeDL(opts)
	return _ytdl_cache[cache_key].extract_info(query, download=False)

class GuildMusicPlayer:
	"""
	ギルド(サーバー)単位の音楽再生状態を管理するクラス。
	キュー・プリフェッチワーカー・ループ状態・ボイスクライアントを保持する。
	"""
	def __init__(self, guild_id: int, bot: commands.Bot):
		self.guild_id = guild_id
		self.bot = bot
		self.queue = collections.deque()
		self.queue_updated_event = asyncio.Event() # ワーカーを同期するためのイベント
		self.loop = False
		self.current = None
		self.worker_task = None
		self.voice_client = None
	def start_worker(self) -> None:
		"""ワーカータスクが未起動または終了済みの場合のみ新規起動する"""
		if self.worker_task is None or self.worker_task.done():
			self.worker_task = self.bot.loop.create_task(guild_prefetch_worker(self), name=f"prefetch_worker_{self.guild_id}")
	def cleanup(self) -> None:
		"""ワーカーのキャンセルとキューの全破棄を行う"""
		if self.worker_task and not self.worker_task.done():
			self.worker_task.cancel()
		self.queue.clear()
		self.queue_updated_event.clear()
		self.current = None

server_music_data: Dict[int, GuildMusicPlayer] = {}

class YTDLSource(discord.PCMVolumeTransformer):
	"""
	yt-dlpで取得したストリームURLをFFmpegで再生するAudioSourceラッパー。
	PCMVolumeTransformerを継承してリアルタイムの音量調整に対応する。
	"""
	def __init__(self, source: discord.AudioSource, *, data: dict, display_url: str, volume: float = 0.25):
		super().__init__(source, volume)
		self.data = data
		self.title = data.get('title', 'Unknown Title')
		self.display_url = display_url
	@classmethod
	async def from_track(cls, track: dict, volume: float = 0.25):
		stream_url = track.get('stream_url')
		if not stream_url:
			raise ValueError(f"ストリームURLが存在しません: {track.get('title', 'Unknown')}")
		http_headers = track.get('http_headers', {})
		header_str = "".join([f"{k}: {v}\r\n" for k, v in http_headers.items()])
		dynamic_before_options = FFMPEG_OPTIONS['before_options']
		if header_str:
			dynamic_before_options += f" -headers \"{header_str}\""
		return cls(
			discord.FFmpegPCMAudio(
				stream_url,
				before_options=dynamic_before_options,
				options=FFMPEG_OPTIONS['options'],
				stderr=sys.stderr
			),
			data=track,
			display_url=track.get("display_url", ""),
			volume=volume
		)

async def guild_prefetch_worker(player: GuildMusicPlayer):
	sys.stdout.write(f"{Color.CYAN}[⚙️ WORKER] ギルド {player.guild_id} のプレフェッチワーカーが起動しました。{Color.RESET}\n")
	sys.stdout.flush()
	while True:
		try:
			# キューに動きがあるまで待機
			await player.queue_updated_event.wait()
			track_to_fetch = None
			# dequeの中身を走査し、未取得(かつ現在取得中でない)の最初の曲を探す
			for track in player.queue:
				if not track.get("stream_url") and not track.get("error") and not track.get("is_fetching"):
					track_to_fetch = track
					break
			# 対象がなければイベントを下げてやり直し (clearコマンド等で消された場合もここを通る)
			if not track_to_fetch:
				player.queue_updated_event.clear()
				await asyncio.sleep(0.5)
				continue
			# 取得中フラグを立てて二重取得を防ぐ
			track_to_fetch["is_fetching"] = True
			max_retries = app_config.MAX_RETRIES
			for attempt in range(max_retries):
				try:
					# タスクの生成を明示化
					fetch_coro = fetch_track_info_with_cache(track_to_fetch["url"], False)
					fetch_task = asyncio.create_task(fetch_coro)
					info = await loading_spinner(fetch_task, f"音源のロード中: {track_to_fetch['title']} (試行 {attempt+1}/{max_retries})")
					if 'entries' in info and len(info['entries']) > 0:
						info = info['entries']
					track_to_fetch["stream_url"] = info.get('url')
					track_to_fetch["http_headers"] = info.get('http_headers', {})
					track_to_fetch["duration"] = info.get("duration", track_to_fetch.get("duration"))
					break
				except Exception as e:
					if attempt == max_retries - 1:
						track_to_fetch["error"] = e
					else:
						sys.stdout.write(f"\r{Color.YELLOW}[!] {track_to_fetch['title']} のロードに失敗。リトライします...{Color.RESET}\n")
						sys.stdout.flush()
						await asyncio.sleep(2)
			track_to_fetch["ready_event"].set()
		except asyncio.CancelledError:
			sys.stdout.write(f"{Color.CYAN}[⚙️ WORKER] ギルド {player.guild_id} のワーカーが終了しました。{Color.RESET}\n")
			sys.stdout.flush()
			break
		except Exception as e:
			sys.stdout.write(f"{Color.RED}[⚙️ WORKER FATAL] 予期せぬエラー: {e}{Color.RESET}\n")
			sys.stdout.flush()
			await asyncio.sleep(1)

async def ensure_guild_data(guild_id: int, bot: commands.Bot = None) -> GuildMusicPlayer:
	if guild_id not in server_music_data:
		server_music_data[guild_id] = GuildMusicPlayer(guild_id, bot)
	player = server_music_data[guild_id]
	if bot:
		player.start_worker()
	return player

async def play_next_song(ctx: commands.Context, bot: commands.Bot):
	guild_id = ctx.guild.id
	player_data = server_music_data.get(guild_id)
	if not player_data: return
	voice_client = ctx.guild.voice_client
	if player_data.loop and player_data.current:
		# ループ時は独立したコピーとして扱い、TTLキャッシュによるURL再取得判定を挟ませる
		loop_track = player_data.current.copy()
		loop_track["ready_event"] = asyncio.Event()
		loop_track["is_fetching"] = False
		loop_track["stream_url"] = None  # URLを消すことでワーカーが aiocache を通して安全に再評価する
		loop_track["error"] = None
		player_data.queue.append(loop_track)
		player_data.queue_updated_event.set()
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
		wait_msg = next_track.get("wait_msg")
		if not next_track["ready_event"].is_set():
			if not wait_msg:
				wait_msg = await preparing_audio_embed(ctx)
			await next_track["ready_event"].wait()
		if next_track.get("error"):
			if wait_msg:
				try:
					await wait_msg.delete()
				except Exception: pass
			await skip_error_embed(ctx, next_track['title'])
			return await play_next_song(ctx, bot)
		guild_vol = await sql_execution("SELECT volume FROM serverData WHERE guild_id=?;", (guild_id,))
		vol = guild_vol[0][0] if guild_vol else app_config.DEFAULT_VOLUME
		player = await YTDLSource.from_track(next_track, volume=vol)
		def after_playing(error):
			if error:
				logger.error(f"再生時エラー: {error}")
			asyncio.run_coroutine_threadsafe(play_next_song(ctx, bot), bot.loop)
		voice_client.play(player, after=after_playing)
		await music_info_embed(ctx, player, len(player_data.queue), wait_msg)
	except Exception as e:
		logger.error(f"再生ソース生成エラー: {e}")
		await playback_error_embed(ctx, next_track.get('title', '不明な曲'))
		await play_next_song(ctx, bot)

async def music_info_embed(ctx: commands.Context, player: YTDLSource, queue_count: int, wait_msg: discord.Message = None):
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
		else:
			fallback_url = "https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4?q=80&w=1024&auto=format&fit=crop"
			embed.set_image(url=fallback_url)
		if wait_msg:
			try:
				await wait_msg.edit(embed=embed)
				return
			except Exception:
				pass
		await ctx.send(embed=embed)
	except Exception as e:
		logger.error(f"Embed表示エラー: {e}")
		fallback_title = player.title if player and player.title else "Unknown Title"
		try:
			await music_info_fallback_embed(ctx, fallback_title)
		except Exception:
			pass

async def play_music(ctx: commands.Context, url: str, bot: commands.Bot):
	player_data = await ensure_guild_data(ctx.guild.id, bot)
	is_input_url = url.startswith(('http://', 'https://'))
	is_playlist_url = is_input_url and ('list=' in url or 'playlist' in url)
	is_playing_immediately = not player_data.current and not ctx.guild.voice_client.is_playing()
	wait_msg = None
	if is_playing_immediately:
		wait_msg = await preparing_audio_embed(ctx)
	try:
		if is_input_url and not is_playlist_url:
			fetch_coro = fetch_track_info_with_cache(url, False)
			fetch_task = asyncio.create_task(fetch_coro)
			info = await loading_spinner(fetch_task, "音源の取得")
			if info is None: raise ValueError("情報の取得に失敗")
			entries = [info]
			is_playlist = False
		else:
			search_query = url if is_input_url else f"ytsearch1:{url}"
			fetch_coro = fetch_track_info_with_cache(search_query, True)
			fetch_task = asyncio.create_task(fetch_coro)
			info = await loading_spinner(fetch_task, "メタデータ検索")
			if info is None: raise ValueError("情報の取得に失敗")
			entries = info.get('entries', [info]) if 'entries' in info else [info]
			is_playlist = 'entries' in info and not search_query.startswith('ytsearch')
		limits = await sql_execution("SELECT queue_limit, playlist_limit FROM serverData WHERE guild_id=?;", (ctx.guild.id,))
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
			track_url = entry.get("webpage_url") or entry.get("original_url")
			if not track_url and video_id:
				track_url = f"https://www.youtube.com/watch?v={video_id}"
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
				"ready_event": asyncio.Event(),
				"wait_msg": wait_msg if not is_playlist else None,
				"is_fetching": False # 重複取得防止フラグ
			}
			if is_input_url and not is_playlist_url:
				track_data["stream_url"] = entry.get('url')
				track_data["http_headers"] = entry.get('http_headers', {})
				track_data["ready_event"].set()
				track_data["is_fetching"] = True
			player_data.queue.append(track_data)
			player_data.queue_updated_event.set() # ワーカーを起動/再開させる
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
		if not is_playing_immediately:
			await queue_added_embed(ctx, single_info, len(player_data.queue))
	if is_playing_immediately:
		bot.loop.create_task(play_next_song(ctx, bot))