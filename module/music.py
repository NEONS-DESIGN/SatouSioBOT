import asyncio
import discord
from discord.ext import commands
from yt_dlp import YoutubeDL
from typing import Dict, Any
import sys
import itertools

# 自作モジュール群のインポート
from module.color import Color
from module.embed import *
from module.options import YTDLP_OPTIONS, FFMPEG_OPTIONS
from module.other import play_time, shorten_url
from module.sqlite import sql_execution

# yt-dlpインスタンスの生成
ytdl = YoutubeDL(YTDLP_OPTIONS)

# サーバーごとの状態管理用辞書
# 構造: { guild_id: { "queue": [], "loop": bool, "current": dict } }
server_music_data: Dict[int, Dict[str, Any]] = {}

class YTDLSource(discord.PCMVolumeTransformer):
	def __init__(self, source: discord.AudioSource, *, data: dict, display_url: str, volume: float = 0.25):
		super().__init__(source, volume)
		self.data = data
		self.title = data.get('title', 'Unknown Title')
		self.display_url = display_url

	@classmethod
	async def from_url(cls, url: str, display_url: str, *, loop: asyncio.AbstractEventLoop = None, stream: bool = True, volume: float = 0.5):
		"""
		URLから再生可能なAudioSourceを生成する。
		ニコニコ動画等のアクセス制限を回避するため、FFmpegにカスタムヘッダーを注入する。
		注意: 再生開始の直前に呼び出すこと。
		"""
		loop = loop or asyncio.get_event_loop()
		# yt-dlpの抽出処理を非同期で実行
		data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=not stream))

		# プレイリストや検索結果が含まれる場合は最初の要素を取得
		if 'entries' in data and len(data['entries']) > 0:
			data = data['entries']

		filename = data['url'] if stream else ytdl.prepare_filename(data)

		# --- ヘッダー情報の動的注入 ---
		# yt-dlpが解析時に使用したヘッダー（RefererやUser-Agent）を取得
		http_headers = data.get('http_headers', {})

		# FFmpegの引数形式に変換
		header_str = ""
		for key, value in http_headers.items():
			header_str += f"{key}: {value}\r\n"

		# 基本のbefore_optionsに、動画固有のヘッダーを結合
		dynamic_before_options = f"{FFMPEG_OPTIONS['before_options']} -headers \"{header_str}\""
		# ----------------------------

		return cls(
			discord.FFmpegPCMAudio(
				filename,
				before_options=dynamic_before_options,
				options=FFMPEG_OPTIONS['options']
			),
			data=data,
			display_url=display_url,
			volume=volume
		)

async def ensure_guild_data(guild_id: int):
	"""指定ギルドのデータ領域を初期化する。"""
	if guild_id not in server_music_data:
		server_music_data[guild_id] = {
			"queue": [],
			"loop": False,
			"current": None
		}

async def loading_spinner(task_future: asyncio.Future, message: str = "yt-dlp 解析中"):
	"""
	非同期タスクの完了を待機しつつ、コンソールにローディングアニメーションを描画する。
	終了時には行を完全にクリアして残像を防止する。
	"""
	spinner = itertools.cycle(['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'])
	colors = itertools.cycle([Color.RED, Color.YELLOW, Color.GREEN, Color.CYAN, Color.BLUE, Color.MAGENTA])

	# 行をクリアするための余白
	clear_line = ' ' * 80

	try:
		while not task_future.done():
			sys.stdout.write(f"\r{next(colors)}[{next(spinner)}] {message}...{Color.RESET}")
			sys.stdout.flush()
			await asyncio.sleep(0.1)
	finally:
		# 完了時またはエラー時に行をクリアして改行する
		sys.stdout.write(f"\r{clear_line}")
		sys.stdout.write(f"\r{Color.GREEN}[✓] {message} 完了!{Color.RESET}\n")
		sys.stdout.flush()

async def play_next_song(ctx: commands.Context, bot: commands.Bot):
	"""キューから次の曲を取得し、再生処理を行うメインロジック。"""
	guild_id = ctx.guild.id
	data = server_music_data[guild_id]
	voice_client = ctx.guild.voice_client

	# ループ有効時、直前に再生していた曲をキューの末尾へ再追加
	if data["loop"] and data["current"]:
		data["queue"].append(data["current"])

	# キューが空の場合は処理を終了し、ボイスチャンネルから退出
	if not data["queue"]:
		data["current"] = None
		if voice_client and voice_client.is_connected():
			await voice_client.disconnect()
		await play_completed_embed(ctx)
		return

	next_track = data["queue"].pop(0)
	data["current"] = next_track

	try:
		vol = 0.25  # デフォルト値
		# DBから音量設定を取得
		guild_vol = await sql_execution(f"SELECT volume FROM serverData WHERE guild_id={guild_id};")

		if guild_vol is not None:
			vol = guild_vol[0][0]

		# 再生ソースの生成処理（ロード中アニメーションを付与）
		fetch_task = bot.loop.create_task(
			YTDLSource.from_url(next_track["url"], next_track["display_url"], loop=bot.loop, stream=True, volume=vol)
		)
		bot.loop.create_task(loading_spinner(fetch_task, f"音源のロード中: {next_track.get('title', 'Unknown')}"))
		player = await fetch_task

		def after_playing(error):
			if error:
				print(f"[ERROR] 再生時エラー: {error}")
			# 次の曲の再生処理を予約
			asyncio.run_coroutine_threadsafe(play_next_song(ctx, bot), bot.loop)

		voice_client.play(player, after=after_playing)
		await music_info_embed(ctx, player, len(data["queue"]))

	except Exception as e:
		print(f"[ERROR] 再生ソース生成エラー: {e}")
		await playback_error_embed(ctx, next_track.get('title', '不明な曲'))
		await play_next_song(ctx, bot)

async def music_info_embed(ctx: commands.Context, player: YTDLSource, queue_count: int):
	"""
	再生中の曲情報をEmbedで送信する。
	タイトルをMarkdownリンク形式にし、URL短縮を適用する。
	"""
	try:
		embed = discord.Embed(title="🎵 再生中", color=0x1DB954)
		title_str = str(player.title)

		# 表示用URLを短縮（長い場合のみ）
		display_url = await shorten_url(player.display_url)
		if len(title_str) > 100:
			title_str = title_str[:97] + "..."
		# フィールド値としてMarkdown形式のリンクを生成
		field_value = f"[{title_str}]({display_url})"
		if len(field_value) > 1024:
			field_value = title_str[:1024]
		embed.add_field(name="タイトル", value=field_value, inline=False)

		# 再生時間の変換
		raw_duration = player.data.get('duration') or 0
		duration = await play_time(raw_duration)
		embed.add_field(name="再生時間", value=duration, inline=True)
		embed.add_field(name="待機数", value=f"{queue_count} 曲", inline=True)

		# リクエスト者の情報をフッターに追加
		icon_url = ctx.author.display_avatar.url if ctx.author.display_avatar else None
		embed.set_footer(text=f"Requested by: {ctx.author.display_name}", icon_url=icon_url)

		thumbnail_url = player.data.get("thumbnail")
		# サムネイル画像の埋め込み処理
		if thumbnail_url:
			embed.set_image(url=thumbnail_url)
			await ctx.send(embed=embed)
		else:
			# 取得失敗時はローカルの代替画像またはデフォルトURLを使用
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
	"""
	入力値からメタデータを抽出し、キューに追加する。
	入力URLがある場合はそれを表示用に保持し、検索語の場合は抽出URLを使用する。
	"""
	await ensure_guild_data(ctx.guild.id)
	data = server_music_data[ctx.guild.id]

	# 入力自体がURLかどうかを判定
	is_input_url = url.startswith(('http://', 'https://'))

	try:
		search_query = url if is_input_url else f"ytsearch1:{url}"

		# メタデータ取得処理（ロード中アニメーションを付与）
		fetch_task = bot.loop.run_in_executor(None, lambda: ytdl.extract_info(search_query, download=False))
		bot.loop.create_task(loading_spinner(fetch_task, "メタデータの解析中"))
		info = await fetch_task

		if info is None:
			raise ValueError("情報の取得に失敗")

		queued_count = 0
		if 'entries' in info:
			# プレイリストの場合
			for entry in info['entries']:
				if entry is None: continue
				track_url = entry.get("webpage_url") or entry.get("url")
				data["queue"].append({
					"url": track_url,
					"display_url": track_url, # プレイリスト内の個別URLを表示用にする
					"title": entry.get("title", "Unknown Title"),
					"author_id": ctx.author.id
				})
				queued_count += 1
		else:
			# 単一動画データの場合
			video_url = info.get("webpage_url") or info.get("url")
			data["queue"].append({
				"url": video_url,
				"display_url": url if is_input_url else video_url, # 入力URLを優先保持
				"title": info.get("title", "Unknown Title"),
				"author_id": ctx.author.id
			})
			queued_count = 1

		if queued_count == 0:
			raise ValueError("再生可能な動画が見つからない")

	except Exception as e:
		print(f"[ERROR] play_music解析エラー: {e}")
		await load_error_embed(ctx, e)
		return

	# キューに追加された曲がある場合のみ、以降の処理を行う
	if queued_count == 0:
		return await none_result_embed(ctx)

	# ボイスクライアントが未稼働の場合は再生を開始
	if not ctx.guild.voice_client.is_playing():
		await play_next_song(ctx, bot)
		if queued_count > 1:
			await playlist_added_embed(ctx, info, queued_count)
	else:
		if queued_count > 1:
			await playlist_added_embed(ctx, info, queued_count)
		else:
			await queue_added_embed(ctx, info, len(data["queue"]))