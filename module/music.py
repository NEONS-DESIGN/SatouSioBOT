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
from module.options import YTDLP_OPTIONS, FFMPEG_OPTIONS, FAST_META_OPTIONS
from module.other import play_time, shorten_url
from module.sqlite import sql_execution

ytdl = YoutubeDL(YTDLP_OPTIONS)
fast_ytdl = YoutubeDL(FAST_META_OPTIONS)

logger = get_bot_logger()

# サーバーごとの状態管理用辞書
server_music_data: Dict[int, Dict[str, Any]] = {}

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

async def guild_prefetch_worker(guild_id: int, bot: commands.Bot):
	"""
	各ギルドに1つだけ存在するバックグラウンドワーカー。
	キューから曲を1つずつ取り出し、順番に重い解析（ストリームURLの取得）を行う。
	"""
	data = server_music_data[guild_id]

	sys.stdout.write(f"{Color.CYAN}[⚙️ WORKER] ギルド {guild_id} のプレフェッチワーカーが起動しました。{Color.RESET}\n")
	sys.stdout.flush()

	while True:
		try:
			track = await data["prefetch_queue"].get()

			if not track.get("stream_url") and not track.get("error"):
				try:
					fetch_task = bot.loop.run_in_executor(None, lambda: ytdl.extract_info(track["url"], download=False))
					info = await loading_spinner(fetch_task, f"音源のロード中: {track['title']}")

					if 'entries' in info and len(info['entries']) > 0:
						info = info['entries']

					track["stream_url"] = info.get('url')
					track["http_headers"] = info.get('http_headers', {})
					track["duration"] = info.get("duration", track.get("duration"))

				except Exception as e:
					track["error"] = e

			track["ready_event"].set()
			data["prefetch_queue"].task_done()

		except asyncio.CancelledError:
			# 終了ログも装飾して出力
			sys.stdout.write(f"{Color.CYAN}[⚙️ WORKER] ギルド {guild_id} のワーカーが終了しました。{Color.RESET}\n")
			sys.stdout.flush()
			break
		except Exception as e:
			# 予期せぬ致命的エラーは赤色で出力
			sys.stdout.write(f"{Color.RED}[⚙️ WORKER FATAL] 予期せぬエラー: {e}{Color.RESET}\n")
			sys.stdout.flush()
			await asyncio.sleep(1)

async def ensure_guild_data(guild_id: int, bot: commands.Bot = None):
	"""指定ギルドのデータ領域とワーカーを初期化する。"""
	if guild_id not in server_music_data:
		server_music_data[guild_id] = {
			"queue": [],
			"prefetch_queue": asyncio.Queue(),
			"loop": False,
			"current": None,
			"worker_task": None
		}

	data = server_music_data[guild_id]
	if bot and (data["worker_task"] is None or data["worker_task"].done()):
		data["worker_task"] = bot.loop.create_task(guild_prefetch_worker(guild_id, bot))

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
	data = server_music_data[guild_id]
	voice_client = ctx.guild.voice_client

	if data["loop"] and data["current"]:
		data["current"]["ready_event"].clear()
		data["queue"].append(data["current"])
		data["prefetch_queue"].put_nowait(data["current"])

	if not data["queue"]:
		data["current"] = None
		if voice_client and voice_client.is_connected():
			await voice_client.disconnect()
		await play_completed_embed(ctx)

		if data["worker_task"] and not data["worker_task"].done():
			data["worker_task"].cancel()
		return

	next_track = data["queue"].pop(0)
	data["current"] = next_track

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
		vol = guild_vol[0][0] if guild_vol else 0.25

		player = await YTDLSource.from_track(next_track, volume=vol)

		def after_playing(error):
			if error:
				logger.error(f"再生時エラー: {error}")
			asyncio.run_coroutine_threadsafe(play_next_song(ctx, bot), bot.loop)

		voice_client.play(player, after=after_playing)
		await music_info_embed(ctx, player, len(data["queue"]))

	except Exception as e:
		logger.error(f"再生ソース生成エラー: {e}")
		await playback_error_embed(ctx, next_track.get('title', '不明な曲'))
		await play_next_song(ctx, bot)

async def music_info_embed(ctx: commands.Context, player: YTDLSource, queue_count: int):
	"""再生中の曲情報をEmbedで送信する"""
	try:
		embed = discord.Embed(title="🎵 再生中", color=0x1DB954)
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
	await ensure_guild_data(ctx.guild.id, bot)
	data = server_music_data[ctx.guild.id]
	is_input_url = url.startswith(('http://', 'https://'))

	try:
		search_query = url if is_input_url else f"ytsearch1:{url}"

		fetch_task = bot.loop.run_in_executor(None, lambda: fast_ytdl.extract_info(search_query, download=False))

		info = await loading_spinner(fetch_task, "メタデータ検索")

		if info is None:
			raise ValueError("情報の取得に失敗")

		limits = await sql_execution(f"SELECT queue_limit, playlist_limit FROM serverData WHERE guild_id={ctx.guild.id};")
		queue_limit = limits[0][0] if limits else 50
		playlist_limit = limits[0][1] if limits else 10

		queued_count = 0
		entries = info.get('entries', [info]) if 'entries' in info else [info]
		is_playlist = 'entries' in info and not search_query.startswith('ytsearch')

		if is_playlist:
			entries = entries[:playlist_limit]

		available_slots = queue_limit - len(data["queue"])
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

			data["queue"].append(track_data)
			data["prefetch_queue"].put_nowait(track_data)

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
		await queue_added_embed(ctx, single_info, len(data["queue"]))

	if not data["current"] and not ctx.guild.voice_client.is_playing():
		bot.loop.create_task(play_next_song(ctx, bot))