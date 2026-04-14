import asyncio
import collections
import concurrent.futures
from aiocache import Cache, cached
import discord
from discord.ext import commands
from typing import Dict, Optional
import sys

from module.color import Color, Embed
from module.embed import (
	music_info_fallback_embed, playlist_added_embed, queue_added_embed,
	preparing_audio_embed, play_completed_embed, playback_error_embed,
	skip_error_embed, load_error_embed
)
from module.logger import get_bot_logger
from module.options import FFMPEG_OPTIONS, app_config
from module.utils import loading_spinner, shorten_url
from module.sqlite import sql_execution

logger = get_bot_logger()

# プロセス内でyt-dlpインスタンスを使い回す
_process_pool: Optional[concurrent.futures.ProcessPoolExecutor] = None
# yt-dlpを使い回すためのキャッシュ
_ytdl_cache: dict = {}

def get_process_pool() -> concurrent.futures.ProcessPoolExecutor:
	"""
	プロセスプールを遅延初期化して返す。
	GILを回避するためProcessPoolExecutorを使用する。
	"""
	global _process_pool
	if _process_pool is None:
		_process_pool = concurrent.futures.ProcessPoolExecutor(max_workers=app_config.MAX_WORKERS)
	return _process_pool

# TTLキャッシュ: YouTubeのストリームURLの有効期限(約6時間)を考慮してttl=18000に設定
@cached(ttl=18000, cache=Cache.MEMORY)
async def fetch_track_info_with_cache(query: str, is_fast: bool) -> dict:
	"""
	キャッシュを確認し、なければ別プロセスでyt-dlpを実行して情報を取得する。
	引数(query, is_fast)の組み合わせがキャッシュキーとなる。

	Parameters
	----------
	query : str
		検索クエリまたはURL
	is_fast : bool
		Trueの場合はメタデータのみ取得するFAST_META_OPTIONSを使用する

	Returns
	-------
	dict
		yt-dlpが返す動画情報辞書
	"""
	logger.info(f"[CACHE MISS] 新規取得を実行: {query}")
	loop = asyncio.get_running_loop()
	pool = get_process_pool()
	# run_in_executorでサブプロセスに処理を投げてawaitする
	return await loop.run_in_executor(pool, extract_info_process, query, is_fast)

def extract_info_process(query: str, is_fast: bool) -> dict:
	"""
	サブプロセス内で実行されるyt-dlp解析関数。
	プロセス内でインスタンスをキャッシュすることで、二回目以降の起動コストを排除する。

	Parameters
	----------
	query : str
		検索クエリまたはURL
	is_fast : bool
		Trueの場合はFAST_META_OPTIONSを使用する
	"""
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
		# UIおよび順序管理用の主キュー
		self.queue: collections.deque = collections.deque()
		# ワーカーへのフィード用キュー (同一オブジェクトを参照する)
		self.prefetch_queue: asyncio.Queue = asyncio.Queue()
		self.loop: bool = False
		self.current: Optional[dict] = None
		self.worker_task: Optional[asyncio.Task] = None
		self.voice_client: Optional[discord.VoiceClient] = None
	def start_worker(self) -> None:
		"""ワーカータスクが未起動または終了済みの場合のみ新規起動する"""
		if self.worker_task is None or self.worker_task.done():
			self.worker_task = self.bot.loop.create_task(guild_prefetch_worker(self), name=f"prefetch_worker_{self.guild_id}")
	def cleanup(self) -> None:
		"""ワーカーのキャンセルとキューの全破棄を行う"""
		if self.worker_task and not self.worker_task.done():
			self.worker_task.cancel()
		self.queue.clear()
		# asyncio.Queueの残留タスクを全て消化してロックを解放する
		while not self.prefetch_queue.empty():
			try:
				self.prefetch_queue.get_nowait()
				self.prefetch_queue.task_done()
			except asyncio.QueueEmpty:
				break
		self.current = None

# ギルドIDをキーとする状態管理辞書
server_music_data: Dict[int, GuildMusicPlayer] = {}

class YTDLSource(discord.PCMVolumeTransformer):
	"""
	yt-dlpで取得したストリームURLをFFmpegで再生するAudioSourceラッパー。
	PCMVolumeTransformerを継承してリアルタイムの音量調整に対応する。
	"""
	def __init__(self, source: discord.AudioSource, *, data: dict, display_url: str, volume: float = 0.25):
		super().__init__(source, volume)
		self.data = data
		self.title: str = data.get('title', 'Unknown Title')
		self.display_url: str = display_url
	@classmethod
	async def from_track(cls, track: dict, volume: float = 0.25) -> "YTDLSource":
		"""
		プリフェッチ済みのトラック辞書からFFmpegAudioSourceを生成する。
		stream_urlが存在しない場合はValueErrorを送出する。

		Parameters
		----------
		track : dict
			プリフェッチワーカーが解析済みのトラックデータ
		volume : float
			初期音量 (0.0～2.0)
		"""
		stream_url = track.get('stream_url')
		if not stream_url:
			raise ValueError(f"ストリームURLが存在しません: {track.get('title', 'Unknown')}")
		# ニコニコ等のヘッダー認証が必要なサービス向けに動的にヘッダーを組み立てる
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

async def guild_prefetch_worker(player: GuildMusicPlayer) -> None:
	"""
	ギルドに1つだけ存在するバックグラウンドプリフェッチワーカー。
	prefetch_queueからトラックを取り出し、ストリームURLを事前取得してready_eventをセットする。
	再生側はready_event.wait()でブロッキングせずに待機できる。
	"""
	logger.info(f"[WORKER] ギルド {player.guild_id} のプリフェッチワーカーが起動")
	pool = get_process_pool()
	while True:
		try:
			track = await player.prefetch_queue.get()
			# 既にストリームURLが取得済み、またはエラー済みの場合はスキップ
			if track.get("stream_url") or track.get("error"):
				track["ready_event"].set()
				player.prefetch_queue.task_done()
				continue
			max_retries: int = app_config.MAX_RETRIES
			for attempt in range(max_retries):
				try:
					loop = asyncio.get_running_loop()
					# サブプロセスでフル解析を実行する
					fetch_task = loop.run_in_executor(
						pool, extract_info_process, track["url"], False
					)
					info = await asyncio.wait_for(fetch_task, timeout=30.0)
					# プレイリスト形式で返ってきた場合は先頭エントリを使用する
					if 'entries' in info and info['entries']:
						info = info['entries'][0]
					stream_url = info.get('url')
					if not stream_url:
						raise ValueError("yt-dlpからストリームURLを取得できませんでした")
					track["stream_url"] = stream_url
					track["http_headers"] = info.get('http_headers', {})
					track["duration"] = info.get("duration", track.get("duration", 0))
					logger.info(f"[WORKER] プリフェッチ完了: {track['title']}")
					break
				except asyncio.TimeoutError:
					logger.warning(f"[WORKER] タイムアウト ({attempt+1}/{max_retries}): {track['title']}")
					if attempt == max_retries - 1:
						track["error"] = TimeoutError("ストリームURL取得がタイムアウトしました")
					else:
						await asyncio.sleep(2)
				except Exception as e:
					logger.warning(f"[WORKER] 取得失敗 ({attempt+1}/{max_retries}): {e}")
					if attempt == max_retries - 1:
						track["error"] = e
					else:
						await asyncio.sleep(2)
			track["ready_event"].set()
			player.prefetch_queue.task_done()
		except asyncio.CancelledError:
			logger.info(f"[WORKER] ギルド {player.guild_id} のワーカーが正常終了")
			break
		except Exception as e:
			logger.error(f"[WORKER FATAL] 予期せぬエラー: {e}")
			await asyncio.sleep(1)

async def ensure_guild_data(guild_id: int, bot: commands.Bot = None) -> GuildMusicPlayer:
	"""
	指定ギルドのGuildMusicPlayerを取得または新規生成する。
	botが渡された場合はプリフェッチワーカーを起動する。

	Parameters
	----------
	guild_id : int
		DiscordのギルドID
	bot : commands.Bot, optional
		ワーカー起動に使用するBotインスタンス
	"""
	if guild_id not in server_music_data:
		server_music_data[guild_id] = GuildMusicPlayer(guild_id, bot)
	player = server_music_data[guild_id]
	if bot:
		player.start_worker()
	return player

async def play_next_song(ctx: commands.Context, bot: commands.Bot) -> None:
	"""
	キューから次のトラックを取り出して再生する。
	ループ有効時は現在のトラックをキューの末尾に再追加する。
	キューが空になった場合はVCを切断してリソースを解放する。

	Parameters
	----------
	ctx : commands.Context
		コマンドのコンテキスト
	bot : commands.Bot
		after_playingコールバックから非同期処理を呼び出すために必要
	"""
	guild_id = ctx.guild.id
	player_data = server_music_data.get(guild_id)
	if not player_data:
		return
	voice_client = ctx.guild.voice_client
	# ループ有効時: 再生完了したトラックのready_eventをリセットしてキューと
	# prefetch_queueに再投入する
	if player_data.loop and player_data.current:
		player_data.current["ready_event"].clear()
		player_data.current["stream_url"] = None  # 再取得させるためにリセット
		player_data.queue.append(player_data.current)
		player_data.prefetch_queue.put_nowait(player_data.current)
	# キューが空の場合はVCを切断してクリーンアップする
	if not player_data.queue:
		player_data.current = None
		if voice_client and voice_client.is_connected():
			await voice_client.disconnect()
		await play_completed_embed(ctx)
		player_data.cleanup()
		return
	next_track = player_data.queue.popleft()
	player_data.current = next_track

	# wait_msgはトラックの準備待ちが発生した場合のみ生成する
	wait_msg: Optional[discord.Message] = None
	try:
		# ready_eventがセットされるまで待機する (プリフェッチが完了していない場合)
		if not next_track["ready_event"].is_set():
			wait_msg = await preparing_audio_embed(ctx)
			await next_track["ready_event"].wait()
			# メッセージ削除はベストエフォートで行う
			if wait_msg:
				try:
					await wait_msg.delete()
				except discord.NotFound:
					pass
				wait_msg = None
		# プリフェッチ時にエラーが記録されていた場合は次の曲へスキップする
		if next_track.get("error"):
			await skip_error_embed(ctx, next_track['title'])
			return await play_next_song(ctx, bot)
		# DBからギルドの音量設定を取得する。レコードがない場合はデフォルト値を使用する
		guild_vol = await sql_execution(
			"SELECT volume FROM serverData WHERE guild_id=?;", (guild_id,)
		)
		vol: float = guild_vol[0][0] if guild_vol else app_config.DEFAULT_VOLUME
		player = await YTDLSource.from_track(next_track, volume=vol)
		def after_playing(error: Optional[Exception]) -> None:
			# after_playingはdiscord.pyの内部スレッドから呼び出されるため
			# run_coroutine_threadsafeで安全にイベントループへ投げる
			if error:
				logger.error(f"再生中エラー: {error}")
			asyncio.run_coroutine_threadsafe(play_next_song(ctx, bot), bot.loop)
		voice_client.play(player, after=after_playing)
		# 再生開始後に楽曲情報Embedを送信する
		await music_info_embed(ctx, player, len(player_data.queue))
	except Exception as e:
		logger.error(f"再生ソース生成エラー: {e}")
		# wait_msgが残っている場合はここで削除する
		if wait_msg:
			try:
				await wait_msg.delete()
			except discord.NotFound:
				pass
		await playback_error_embed(ctx, next_track.get('title', '不明な曲'))
		await play_next_song(ctx, bot)

async def music_info_embed(ctx: commands.Context, player: YTDLSource, queue_count: int) -> None:
	"""
	再生中の楽曲情報をEmbedで送信する内部関数。
	URLの短縮はベストエフォートで行い、失敗しても元のURLにフォールバックする。

	Parameters
	----------
	ctx : commands.Context
		送信先のコンテキスト
	player : YTDLSource
		再生中のAudioSourceインスタンス
	queue_count : int
		現在のキューに残っているトラック数
	"""
	# フォールバック用サムネイル (楽曲画像が取得できない場合に使用する)
	FALLBACK_THUMBNAIL = (
		"https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4"
		"?q=80&w=1024&auto=format&fit=crop"
	)
	try:
		from module.utils import play_time
		embed = discord.Embed(title="🎵 再生中", color=Embed.GREEN)
		title_str = str(player.title)
		if len(title_str) > 100:
			title_str = title_str[:97] + "..."
		# URL短縮はベストエフォート。失敗しても元のURLを使用する
		display_url = await shorten_url(player.display_url)
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
		thumbnail_url = player.data.get("thumbnail") or FALLBACK_THUMBNAIL
		embed.set_image(url=thumbnail_url)
		await ctx.send(embed=embed)
	except Exception as e:
		logger.error(f"楽曲情報Embed送信エラー: {e}")
		fallback_title = player.title if player and player.title else "Unknown Title"
		try:
			await music_info_fallback_embed(ctx, fallback_title)
		except Exception:
			pass

async def play_music(ctx: commands.Context, url: str, bot: commands.Bot) -> None:
	"""
	入力値(URLまたは検索クエリ)からメタデータを取得してキューとワーカーに渡す。
	プレイリストURLと単一URLと検索クエリで処理フローを分岐する。

	Parameters
	----------
	ctx : commands.Context
		コマンドのコンテキスト
	url : str
		ユーザーが入力したURLまたは検索キーワード
	bot : commands.Bot
		再生開始タスクの生成に使用する
	"""
	player_data = await ensure_guild_data(ctx.guild.id, bot)
	is_input_url: bool = url.startswith(('http://', 'https://'))
	is_playlist_url: bool = is_input_url and ('list=' in url or 'playlist' in url)
	is_playing_immediately: bool = (
		not player_data.current and not ctx.guild.voice_client.is_playing()
	)
	wait_msg: Optional[discord.Message] = None
	if is_playing_immediately:
		wait_msg = await preparing_audio_embed(ctx)
	pool = get_process_pool()
	try:
		# 単一直接URLの場合: フル解析を行いストリームURLも同時に取得する
		# プレイリストURLと検索クエリの場合: まずメタデータのみ取得し、
		# ストリームURLはワーカーが非同期で取得する
		if is_input_url and not is_playlist_url:
			loop = asyncio.get_running_loop()
			fetch_task = asyncio.ensure_future(
				fetch_track_info_with_cache(url, False)
			)
			info = await loading_spinner(fetch_task, "音源の取得")
			if info is None:
				raise ValueError("情報の取得に失敗しました")
			entries = [info]
			is_playlist = False
		else:
			search_query = url if is_input_url else f"ytsearch1:{url}"
			fetch_task = asyncio.ensure_future(
				fetch_track_info_with_cache(search_query, True)
			)
			info = await loading_spinner(fetch_task, "メタデータ検索")
			if info is None:
				raise ValueError("情報の取得に失敗しました")
			entries = info.get('entries', [info]) if 'entries' in info else [info]
			# ytsearchプレフィックスがない場合のみプレイリストと判定する
			is_playlist = 'entries' in info and not search_query.startswith('ytsearch')
		# DBからキューとプレイリストの上限値を取得する
		limits = await sql_execution(
			"SELECT queue_limit, playlist_limit FROM serverData WHERE guild_id=?;",
			(ctx.guild.id,)
		)
		queue_limit: int = limits[0][0] if limits else app_config.DEFAULT_QUEUE_LIMIT
		playlist_limit: int = limits[0][1] if limits else app_config.DEFAULT_PLAYLIST_LIMIT
		# 追加可能な残りスロット数を計算する
		available_slots = queue_limit - len(player_data.queue)
		if available_slots <= 0:
			raise ValueError(
				f"キューの最大曲数({queue_limit}曲)に達しているため追加できません。"
			)
		# プレイリストとスロット上限の小さい方を適用する
		max_entries = min(playlist_limit, available_slots) if is_playlist else available_slots
		entries = entries[:max_entries]
		queued_count = 0
		# 単一曲の場合にEmbed表示用データを保持する変数
		single_info: Optional[dict] = None
		for entry in entries:
			if entry is None:
				continue
			# ページURLを優先し、なければ動画URLにフォールバックする
			video_id = entry.get("id")
			track_url = (
				entry.get("webpage_url")
				or entry.get("original_url")
				or (f"https://www.youtube.com/watch?v={video_id}" if video_id else None)
				or entry.get("url")
			)
			if not track_url:
				logger.warning(f"[play_music] URLを特定できないエントリをスキップ: {entry.get('title')}")
				continue
			# サムネイルはthumbnailフィールドを優先し、thumbnailsリストにフォールバックする
			thumb_url = entry.get("thumbnail")
			if not thumb_url and entry.get("thumbnails"):
				thumb_url = entry["thumbnails"][-1].get("url")
			track_data = {
				"url": track_url,
				"display_url": url if (is_input_url and not is_playlist_url) else track_url,
				"title": entry.get("title", "Unknown Title"),
				"author_id": ctx.author.id,
				"thumbnail": thumb_url,
				"duration": entry.get("duration", 0),
				"stream_url": None,
				"http_headers": {},
				"error": None,
				"ready_event": asyncio.Event()
			}
			# 単一直接URLの場合: フル解析済みのためストリームURLを直接セットし
			# ワーカーの処理をスキップさせる
			if is_input_url and not is_playlist_url:
				track_data["stream_url"] = entry.get('url')
				track_data["http_headers"] = entry.get('http_headers', {})
				track_data["ready_event"].set()
			player_data.queue.append(track_data)
			player_data.prefetch_queue.put_nowait(track_data)
			queued_count += 1
			# 単一曲の場合はEmbed表示用に最初のエントリを保持する
			if not is_playlist and single_info is None:
				single_info = track_data
		if queued_count == 0:
			raise ValueError("再生可能な動画が見つかりませんでした")
	except Exception as e:
		logger.error(f"[play_music] 解析エラー: {e}")
		# wait_msgが残っていれば削除してからエラーEmbedを送信する
		if wait_msg:
			try:
				await wait_msg.delete()
			except discord.NotFound:
				pass
		await load_error_embed(ctx, e)
		return
	# wait_msgが残っていれば削除する (正常系)
	if wait_msg:
		try:
			await wait_msg.delete()
		except discord.NotFound:
			pass
	# 追加完了通知Embedを送信する
	if is_playlist:
		await playlist_added_embed(ctx, info, queued_count)
	elif single_info:
		await queue_added_embed(ctx, single_info, len(player_data.queue))
	# 現在再生中でない場合はplay_next_songをタスクとして起動する
	if not player_data.current and not ctx.guild.voice_client.is_playing():
		bot.loop.create_task(
			play_next_song(ctx, bot),
			name=f"play_next_{ctx.guild.id}"
		)