import asyncio
import discord
from discord.ext import commands
from yt_dlp import YoutubeDL
from typing import Dict, Any
import sys
import itertools

from module.color import *
from module.embed import *
from module.options import YTDLP_OPTIONS, FFMPEG_OPTIONS
from module.other import *
from module.sqlite import sql_execution

ytdl = YoutubeDL(YTDLP_OPTIONS)
server_music_data: Dict[int, Dict[str, Any]] = {}

class YTDLSource(discord.PCMVolumeTransformer):
	def __init__(self, source: discord.AudioSource, *, data: dict, volume: float = 0.5):
		super().__init__(source, volume)
		self.data = data
		self.title = data.get('title', 'Unknown Title')
		self.url = data.get('url', '')

	@classmethod
	async def from_url(cls, url: str, *, loop: asyncio.AbstractEventLoop = None, stream: bool = True, volume: float = 0.5):
		loop = loop or asyncio.get_event_loop()
		data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=not stream))

		if 'entries' in data and len(data['entries']) > 0:
			data = data['entries']

		filename = data['url'] if stream else ytdl.prepare_filename(data)
		return cls(discord.FFmpegPCMAudio(filename, **FFMPEG_OPTIONS), data=data, volume=volume)

async def ensure_guild_data(guild_id: int):
	if guild_id not in server_music_data:
		server_music_data[guild_id] = {
			"queue": [],
			"loop": False,
			"current": None
		}

async def loading_spinner(task_future: asyncio.Future, message: str = "yt-dlp 解析中"):
	"""
	非同期タスクの完了を待機しつつ、コンソールにローディングアニメーションを描画する。
	終了時には行を完全にクリアして残像を防ぐ。
	"""
	spinner = itertools.cycle(['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'])
	colors = itertools.cycle([Color.RED, Color.YELLOW, Color.GREEN, Color.CYAN, Color.BLUE, Color.MAGENTA])

	# ターミナルの横幅に合わせてクリアするための空白
	clear_line = ' ' * 80

	try:
		while not task_future.done():
			# \r で先頭に戻り、現在の色と文字で上書き
			sys.stdout.write(f"\r{next(colors)}[{next(spinner)}] {message}...{Color.RESET}")
			sys.stdout.flush()
			await asyncio.sleep(0.1)
	finally:
		# 完了時：行全体を一度空白で埋めてから「完了」を表示し、確実に改行する
		sys.stdout.write(f"\r{clear_line}")
		sys.stdout.write(f"\r{Color.GREEN}[✓] {message} 完了!{Color.RESET}\n")
		sys.stdout.flush()

async def play_next_song(ctx: commands.Context, bot: commands.Bot):
	guild_id = ctx.guild.id
	data = server_music_data[guild_id]
	voice_client = ctx.guild.voice_client

	if data["loop"] and data["current"]:
		data["queue"].append(data["current"])

	if not data["queue"]:
		data["current"] = None
		if voice_client and voice_client.is_connected():
			await voice_client.disconnect()
		await play_completed_embed(ctx)
		return

	next_track = data["queue"].pop(0)
	data["current"] = next_track

	try:
		guild_db = await sql_execution(f"SELECT * FROM serverData WHERE guild_id={guild_id};")
		vol = 0.25

		try:
			if guild_db and isinstance(guild_db, list) and len(guild_db) > 0:
				row = guild_db
				if isinstance(row, (list, tuple)) and len(row) > 1 and row is not None:
					vol = float(row)
		except Exception as db_err:
			pass

		# 再生ソースの生成処理（ロード中アニメーションを付与）
		fetch_task = bot.loop.create_task(
			YTDLSource.from_url(next_track["url"], loop=bot.loop, stream=True, volume=vol)
		)
		bot.loop.create_task(loading_spinner(fetch_task, f"音源のロード中: {next_track.get('title', 'Unknown')}"))
		player = await fetch_task

		def after_playing(error):
			if error:
				print(f"[ERROR] 再生時エラー: {error}")
			asyncio.run_coroutine_threadsafe(play_next_song(ctx, bot), bot.loop)

		voice_client.play(player, after=after_playing)
		await music_info_embed(ctx, player, len(data["queue"]))

	except Exception as e:
		print(f"[ERROR] 再生ソース生成エラー: {e}")
		await playback_error_embed(ctx, next_track.get('title', '不明な曲'))
		await play_next_song(ctx, bot)

async def music_info_embed(ctx: commands.Context, player: YTDLSource, queue_count: int):
	try:
		embed = discord.Embed(title="🎵 再生中", color=0x1DB954)
		title_str = str(player.title) if player.title else "Unknown Title"
		url_str = str(player.url) if player.url else ""

		if len(title_str) > 100:
			title_str = title_str[:97] + "..."

		field_value = f"[{title_str}]({url_str})"
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
			fallback_image_path = "./default_thumb.png"
			try:
				file = discord.File(fallback_image_path, filename="default.png")
				embed.set_image(url="attachment://default.png")
				await ctx.send(embed=embed, file=file)
			except FileNotFoundError:
				fallback_url = "https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4?q=80&w=1024&auto=format&fit=crop"
				embed.set_image(url=fallback_url)
				await ctx.send(embed=embed)

	except Exception as e:
		print(f"[ERROR] Embed表示エラー: {e}")
		fallback_title = player.title if player and player.title else "Unknown Title"
		try:
			await music_info_fallback_embed(ctx, fallback_title)
		except Exception:
			pass

async def play_music(ctx: commands.Context, url: str, bot: commands.Bot):
	await ensure_guild_data(ctx.guild.id)
	data = server_music_data[ctx.guild.id]

	try:
		search_query = url if url.startswith(('http://', 'https://')) else f"ytsearch1:{url}"

		fetch_task = bot.loop.run_in_executor(
			None,
			lambda: ytdl.extract_info(search_query, download=False)
		)
		bot.loop.create_task(loading_spinner(fetch_task, "メタデータの解析中"))
		info = await fetch_task

		if info is None:
			raise ValueError("情報の取得に失敗")

		queued_count = 0

		# プレイリストの場合
		if 'entries' in info:
			entries = list(info['entries'])
			if not entries:
				raise ValueError("動画データが存在しない")

			for entry in entries:
				if entry is None: continue
				track_url = entry.get("url") or entry.get("webpage_url")
				if track_url:
					data["queue"].append({
						"url": track_url,
						"title": entry.get("title", "Unknown Title"),
						"author_id": ctx.author.id
					})
					queued_count += 1
		# 単一動画の場合
		else:
			data["queue"].append({
				"url": info.get("webpage_url", info.get("url", url)),
				"title": info.get("title", "Unknown Title"),
				"author_id": ctx.author.id
			})
			queued_count = 1

	except Exception as e:
		print(f"[ERROR] play_music解析エラー: {e}")
		await load_error_embed(ctx, e)
		return

	# 再生中でなければ再生開始
	if not ctx.guild.voice_client.is_playing():
		await play_next_song(ctx, bot)

		# プレイリスト追加時の通知
		if queued_count > 1:
			await playlist_added_embed(ctx, info, queued_count)
	else:
		# 再生中にキューへ追加された時の詳細通知
		if queued_count > 1:
			await playlist_added_embed(ctx, info, queued_count)
		else:
			# 単一曲追加時は、infoをそのまま渡して詳細を表示
			await queue_added_embed(ctx, info, len(data["queue"]))