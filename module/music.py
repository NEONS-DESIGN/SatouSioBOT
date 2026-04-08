import asyncio
import discord
from discord.ext import commands
from yt_dlp import YoutubeDL
from typing import Dict, Any
import sys
import itertools

from module.color import Color
from module.embed import *
from module.logger import get_bot_logger
from module.options import YTDLP_OPTIONS, FFMPEG_OPTIONS, FAST_META_OPTIONS, app_config
from module.other import play_time, shorten_url
from module.sqlite import sql_execution

ytdl = YoutubeDL(YTDLP_OPTIONS)
fast_ytdl = YoutubeDL(FAST_META_OPTIONS)

logger = get_bot_logger()

class GuildMusicPlayer:
	def __init__(self, guild_id: int, bot: commands.Bot):
		self.guild_id = guild_id
		self.bot = bot
		self.queue = []
		self.prefetch_queue = asyncio.Queue()
		self.loop = False
		self.current = None
		self.worker_task = None
		self.voice_client = None

	def start_worker(self):
		"""プレフェッチワーカーを起動する"""
		if self.worker_task is None or self.worker_task.done():
			self.worker_task = self.bot.loop.create_task(guild_prefetch_worker(self))

	def cleanup(self):
		"""タスクのキャンセルとキューの破棄を行う"""
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
	async def from_track(cls, track: dict, volume: float = 0.25):
		"""
		ワーカーによって事前解析されたTrackデータからAudioSourceを生成する。
		"""
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
	キューから曲を1つずつ取り出し、順番に重い解析（ストリームURLの取得）を行う。
	"""
	sys.stdout.write(f"{Color.CYAN}[⚙️ WORKER] ギルド {player.guild_id} のプレフェッチワーカーが起動しました。{Color.RESET}\n")
	sys.stdout.flush()
	while True:
		try:
			track = await player.prefetch_queue.get()
			if not track.get("stream_url") and not track.get("error"):
				max_retries = 3
				for attempt in range(max_retries):
					try:
						fetch_task = player.bot.loop.run_in_executor(None, lambda: ytdl.extract_info(track["url"], download=False))
						info = await loading_spinner(fetch_task, f"音源のロード中: {track['title']} (試行 {attempt+1}/{max_retries})")
						if 'entries' in info and len(info['entries']) > 0:
							info = info['entries'][0]
						track["stream_url"] = info.get('url')
						track["http_headers"] = info.get('http_headers', {})
						track["duration"] = info.get("duration", track.get("duration"))
						break # 成功したらループを抜ける
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
			await asyncio.sleep(0.1)
		# 行をクリア
		sys.stdout.write(f"\r{clear_line}")
		# task_future.result() はエラーが起きていた場合、ここで例外を再送出する
		result = task_future.result()
		# 成功時のログ出力
		sys.stdout.write(f"\r{Color.GREEN}[✓] {message} 完了!{Color.RESET}\n")
		sys.stdout.flush()
		return result
	except asyncio.CancelledError:
		sys.stdout.write(f"\r{clear_line}")
		sys.stdout.write(f"\r{Color.YELLOW}[!] {message} キャンセルされました{Color.RESET}\n")
		sys.stdout.flush()
		raise
	except Exception as e:
		# エラー時のログ出力 (✗に変更して出力)
		sys.stdout.write(f"\r{clear_line}")
		sys.stdout.write(f"\r{Color.RED}[✗] {message} 失敗: {e}{Color.RESET}\n")
		sys.stdout.flush()
		raise e

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
	next_track = player_data.queue.pop(0)
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
		guild_vol = await sql_execution(f"SELECT volume FROM serverData WHERE guild_id={guild_id};")
		vol = guild_vol[0][0] if guild_vol else app_config.DEFAULT_VOLUME
		player = await YTDLSource.from_track(next_track, volume=vol)
		def after_playing(error):
			if error:
				logger.error(f"再生時エラー: {error}")
			asyncio.run_coroutine_threadsafe(play_next_song(ctx, bot), bot.loop)
		voice_client.play(player, after=after_playing)
		await music_info_embed(ctx, player, len(player_data.queue))
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
	try:
		search_query = url if is_input_url else f"ytsearch1:{url}"

		# 単一の直接URLの場合は、最初からフル解析(ytdl)を行い二度手間を防ぐ
		if is_input_url and not is_playlist_url:
			fetch_task = bot.loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=False))
			info = await loading_spinner(fetch_task, "音源データの直接取得")
			if info is None: raise ValueError("情報の取得に失敗")
			entries = [info]
			is_playlist = False
		else:
			fetch_task = bot.loop.run_in_executor(None, lambda: fast_ytdl.extract_info(search_query, download=False))
			info = await loading_spinner(fetch_task, "メタデータ検索")
			if info is None: raise ValueError("情報の取得に失敗")
			entries = info.get('entries', [info]) if 'entries' in info else [info]
			is_playlist = 'entries' in info and not search_query.startswith('ytsearch')

		# DBからキューとプレイリストの上限値を取得。存在しない場合はデフォルト値を使用
		limits = await sql_execution(f"SELECT queue_limit, playlist_limit FROM serverData WHERE guild_id={ctx.guild.id};")
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