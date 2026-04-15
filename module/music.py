import asyncio
import collections
import concurrent.futures
import sys
from typing import Any

import discord
from aiocache import Cache, cached
from discord.ext import commands

from module.color import Color, Embed as EmbedColor
from module.embed import (
	music_info_embed, music_info_fallback_embed, preparing_audio_embed,
	playlist_added_embed, queue_added_embed, play_completed_embed,
	load_error_embed, skip_error_embed, playback_error_embed,
)
from module.logger import get_bot_logger
from module.options import FFMPEG_OPTIONS, app_config
from module.sqlite import sql_execution
from module.utils import loading_spinner, shorten_url

logger = get_bot_logger()

# プロセスプール (シングルトン)
_process_pool: concurrent.futures.ProcessPoolExecutor | None = None
def _get_process_pool() -> concurrent.futures.ProcessPoolExecutor:
	"""ProcessPoolExecutorのシングルトンを返す"""
	global _process_pool
	if _process_pool is None:
		_process_pool = concurrent.futures.ProcessPoolExecutor(max_workers=app_config.MAX_WORKER_THREADS)
	return _process_pool

# ==========================================
# yt-dlp 情報取得 (プロセス分離 + キャッシュ)
# ==========================================
# extract_info_process内でのYoutubeDLインスタンスを使い回すキャッシュ
_ytdl_instances: dict[str, Any] = {}
def extract_info_process(query: str, is_fast: bool) -> dict:
	"""
	子プロセス上でyt-dlpを実行する関数。
	- is_fast=True  : メタデータのみ高速取得 (FAST_META_OPTIONS)
	- is_fast=False : ストリームURL込みで完全取得 (YTDLP_OPTIONS)
	"""
	from yt_dlp import YoutubeDL
	from module.options import YTDLP_OPTIONS, FAST_META_OPTIONS
	key = "fast" if is_fast else "normal"
	if key not in _ytdl_instances:
		_ytdl_instances[key] = YoutubeDL(FAST_META_OPTIONS if is_fast else YTDLP_OPTIONS)
	return _ytdl_instances[key].extract_info(query, download=False)

@cached(ttl=app_config.CACHE_TTL, cache=Cache.MEMORY)
async def fetch_track_info(query: str, is_fast: bool) -> dict:
	"""
	extract_info_processをプロセスプール経由で非同期実行し、結果をキャッシュする。
	- ttl: config.iniのcache_ttl秒
	"""
	logger.info(f"キャッシュの新規取得: {query}")
	loop = asyncio.get_running_loop()
	return await loop.run_in_executor(_get_process_pool(), extract_info_process, query, is_fast)

# GuildMusicPlayer (ギルド単位の管理クラス)
class GuildMusicPlayer:
	"""
	ギルド単位の再生状態を管理するクラス。
	- queue: collections.deque でトラックを保持
	- queue_updated_event: プリフェッチワーカーを起こすイベント
	- loop / current / voice_client: 再生状態
	"""
	__slots__ = ("guild_id", "bot", "queue", "queue_updated_event", "loop", "current", "worker_task", "voice_client")
	def __init__(self, guild_id: int, bot: commands.Bot) -> None:
		self.guild_id = guild_id
		self.bot = bot
		self.queue: collections.deque[dict] = collections.deque()
		self.queue_updated_event = asyncio.Event()
		self.loop = False
		self.current: dict | None = None
		self.worker_task: asyncio.Task | None = None
		self.voice_client: discord.VoiceClient | None = None
	def start_worker(self) -> None:
		"""ワーカーが未起動または終了済みの場合のみ起動する"""
		if self.worker_task is None or self.worker_task.done():
			self.worker_task = self.bot.loop.create_task(
				_prefetch_worker(self),
				name=f"prefetch_worker_{self.guild_id}",
			)
	def cleanup(self) -> None:
		"""ワーカーをキャンセルし、全状態を初期化する"""
		if self.worker_task and not self.worker_task.done():
			self.worker_task.cancel()
		self.queue.clear()
		self.queue_updated_event.clear()
		self.current = None

# ギルドIDをキーにしたプレイヤー管理辞書
server_music_data: dict[int, GuildMusicPlayer] = {}

# YTDLSource (FFmpeg AudioSource ラッパー)
class YTDLSource(discord.PCMVolumeTransformer):
	"""
	yt-dlpで取得したストリームURLをFFmpegで再生するAudioSource。
	- PCMVolumeTransformerを継承してリアルタイム音量調整に対応
	"""
	def __init__(self, source: discord.AudioSource, *, data: dict, display_url: str, volume: float) -> None:
		super().__init__(source, volume)
		self.data = data
		self.title: str = data.get("title", "Unknown Title")
		self.display_url: str = display_url
	@classmethod
	async def from_track(cls, track: dict, volume: float = 0.25) -> "YTDLSource":
		"""
		trackデータからFFmpegPCMAudioを生成して返す。
		- stream_urlが存在しない場合はValueErrorを送出する
		"""
		stream_url = track.get("stream_url")
		if not stream_url:
			raise ValueError(f"ストリームURLが存在しません: {track.get('title', 'Unknown')}")
		# HTTPヘッダーが存在する場合のみ before_options に追加する
		http_headers: dict = track.get("http_headers", {})
		before_options = FFMPEG_OPTIONS["before_options"]
		if http_headers:
			header_str = "".join(f"{k}: {v}\r\n" for k, v in http_headers.items())
			before_options += f' -headers "{header_str}"'
		return cls(
			discord.FFmpegPCMAudio(
				stream_url,
				before_options=before_options,
				options=FFMPEG_OPTIONS["options"],
				stderr=sys.stderr,
			),
			data=track,
			display_url=track.get("display_url", ""),
			volume=volume,
		)

# プリフェッチワーカー
async def _prefetch_worker(player: GuildMusicPlayer) -> None:
	"""
	バックグラウンドでキュー内の未取得トラックのストリームURLを事前取得するワーカー。
	- queue_updated_eventでスリープ/再開を制御する
	- is_fetchingフラグで二重取得を防ぐ
	"""
	_log_prefix = f"[WORKER] ギルド {player.guild_id}"
	logger.info(f"{_log_prefix} プリフェッチワーカー起動")
	while True:
		try:
			await player.queue_updated_event.wait()
			# 未取得かつ取得中でないトラックを先頭から検索する
			track = next(
				(t for t in player.queue if not t.get("stream_url") and not t.get("error") and not t.get("is_fetching")),
				None,
			)
			if not track:
				player.queue_updated_event.clear()
				await asyncio.sleep(0.5)
				continue
			track["is_fetching"] = True
			for attempt in range(app_config.MAX_RETRIES):
				try:
					task = asyncio.create_task(fetch_track_info(track["url"], False))
					info = await loading_spinner(task, f"音源ロード: {track['title']} ({attempt+1}/{app_config.MAX_RETRIES})")
					# プレイリスト形式で返ってきた場合は先頭エントリを使用する
					if "entries" in info and info["entries"]:
						info = info["entries"][0]
					track["stream_url"] = info.get("url")
					track["http_headers"] = info.get("http_headers", {})
					track["duration"] = info.get("duration", track.get("duration"))
					break
				except Exception as e:
					if attempt == app_config.MAX_RETRIES - 1:
						track["error"] = e
						logger.warning(f"{_log_prefix} {track['title']} の取得に最終的に失敗: {e}")
					else:
						logger.warning(f"{_log_prefix} {track['title']} のロード失敗、リトライ ({attempt+1})...")
						await asyncio.sleep(2)
			track["ready_event"].set()
		except asyncio.CancelledError:
			logger.info(f"{_log_prefix} ワーカー終了")
			break
		except Exception as e:
			logger.error(f"{_log_prefix} 予期せぬエラー: {e}")
			await asyncio.sleep(1)

# ギルドプレイヤーの取得/生成
async def ensure_guild_data(guild_id: int, bot: commands.Bot | None = None) -> GuildMusicPlayer:
	"""
	guild_idに対応するGuildMusicPlayerを返す。存在しない場合は新規生成する。
	- botが渡された場合はワーカーを自動起動する
	"""
	if guild_id not in server_music_data:
		server_music_data[guild_id] = GuildMusicPlayer(guild_id, bot)
	player = server_music_data[guild_id]
	if bot:
		player.start_worker()
	return player

# 次の曲を再生する
async def play_next_song(ctx: commands.Context, bot: commands.Bot) -> None:
	"""
	キューから次のトラックを取り出して再生する。
	- ループ有効時は現在のトラックをキュー末尾に再追加する
	- キューが空になった場合はVCを切断してリソースをクリーンアップする
	- エラートラックはスキップして次の曲へ進む（末尾再帰を反復ループに変換）
	"""
	guild_id = ctx.guild.id
	player = server_music_data.get(guild_id)
	if not player:
		return
	# 再帰の代わりにループで次のトラックを探す
	while True:
		vc = ctx.guild.voice_client
		if player.loop and player.current:
			# ループ: 現在曲を再キューし、ワーカーにURL再取得させる
			loop_track = {
				**player.current,
				"ready_event": asyncio.Event(),
				"is_fetching": False,
				"stream_url": None,
				"error": None,
			}
			player.queue.append(loop_track)
			player.queue_updated_event.set()
		if not player.queue:
			player.current = None
			if vc and vc.is_connected():
				await vc.disconnect()
			await play_completed_embed(ctx)
			player.cleanup()
			return
		next_track = player.queue.popleft()
		player.current = next_track
		wait_msg: discord.Message | None = next_track.get("wait_msg")
		try:
			# ストリームURLが用意されるまで待機する
			if not next_track["ready_event"].is_set():
				if not wait_msg:
					wait_msg = await preparing_audio_embed(ctx)
				await next_track["ready_event"].wait()
			if next_track.get("error"):
				if wait_msg:
					try:
						await wait_msg.delete()
					except discord.NotFound:
						pass
				await skip_error_embed(ctx, next_track["title"])
				continue  # 次のトラックへ
			# 音量をDBから取得する (なければデフォルト値)
			rows = await sql_execution("SELECT volume FROM server_data WHERE guild_id=?;", (guild_id,))
			vol: float = rows[0][0] if rows else app_config.DEFAULT_VOLUME
			source = await YTDLSource.from_track(next_track, volume=vol)
			def _after_playing(error: Exception | None) -> None:
				if error:
					logger.error(f"再生時エラー (ギルド {guild_id}): {error}")
				asyncio.run_coroutine_threadsafe(play_next_song(ctx, bot), bot.loop)
			vc.play(source, after=_after_playing)
			await music_info_embed(ctx, source, len(player.queue), wait_msg)
			return
		except Exception as e:
			logger.error(f"再生ソース生成エラー (ギルド {guild_id}): {e}")
			await playback_error_embed(ctx, next_track.get("title", "不明な曲"))
			continue  # 次のトラックへ

# 音楽再生エントリポイント
async def play_music(ctx: commands.Context, url: str, bot: commands.Bot) -> None:
	"""
	URLまたは検索クエリを受け取りキューに追加する。
	- プレイリストURL: playlist_limit件までキューに追加
	- 単曲URL: ストリームURLを即時セットしてready_eventを立てる（ワーカー不要）
	- 検索クエリ: ytsearch1:でメタデータのみ先行取得し、実URLはワーカーに任せる
	"""
	player = await ensure_guild_data(ctx.guild.id, bot)
	is_url = url.startswith(("http://", "https://"))
	is_playlist = is_url and ("list=" in url or "playlist" in url)
	is_idle = not player.current and not ctx.guild.voice_client.is_playing()
	wait_msg: discord.Message | None = None
	if is_idle:
		wait_msg = await preparing_audio_embed(ctx)
	try:
		# 情報取得
		if is_url and not is_playlist:
			# 単曲URL: 完全取得 (ストリームURL込み)
			task = asyncio.create_task(fetch_track_info(url, False))
			info = await loading_spinner(task, "音源の取得")
			if info is None:
				raise ValueError("情報の取得に失敗しました")
			entries = [info]
			is_playlist_result = False
		else:
			# プレイリストまたは検索: 高速メタデータ取得
			search_query = url if is_url else f"ytsearch1:{url}"
			task = asyncio.create_task(fetch_track_info(search_query, True))
			info = await loading_spinner(task, "メタデータ検索")
			if info is None:
				raise ValueError("情報の取得に失敗しました")
			entries = info.get("entries", [info]) if "entries" in info else [info]
			is_playlist_result = "entries" in info and not search_query.startswith("ytsearch")
		# キュー上限の確認
		rows = await sql_execution(
			"SELECT queue_limit, playlist_limit FROM server_data WHERE guild_id=?;",
			(ctx.guild.id,),
		)
		queue_limit: int = rows[0][0] if rows else app_config.DEFAULT_QUEUE_LIMIT
		playlist_limit: int = rows[0][1] if rows else app_config.DEFAULT_PLAYLIST_LIMIT
		available = queue_limit - len(player.queue)
		if available <= 0:
			raise ValueError(f"キューの上限({queue_limit}曲)に達しているため追加できません。")
		if is_playlist_result:
			entries = entries[:min(playlist_limit, available)]
		else:
			entries = entries[:available]
		queued_count = 0
		last_single: dict | None = None
		for entry in entries:
			if not entry:
				continue
			# 再生可能URLを解決する
			video_id = entry.get("id")
			track_url = (
				entry.get("webpage_url")
				or entry.get("original_url")
				or (f"https://www.youtube.com/watch?v={video_id}" if video_id else None)
				or entry.get("url")
			)
			if not track_url:
				continue
			# サムネイルURLを解決する
			thumb = entry.get("thumbnail") or (
				entry["thumbnails"][-1]["url"] if entry.get("thumbnails") else None
			)
			ready_event = asyncio.Event()
			track: dict = {
				"url": track_url,
				"display_url": url if (is_url and not is_playlist) else track_url,
				"title": entry.get("title", "Unknown Title"),
				"author_id": ctx.author.id,
				"thumbnail": thumb,
				"duration": entry.get("duration", 0),
				"stream_url": None,
				"http_headers": {},
				"error": None,
				"ready_event": ready_event,
				"wait_msg": wait_msg if not is_playlist_result else None,
				"is_fetching": False,
			}
			# 単曲URLの場合はストリームURLが既に取得済みなのでそのままセットする
			if is_url and not is_playlist:
				track["stream_url"] = entry.get("url")
				track["http_headers"] = entry.get("http_headers", {})
				track["is_fetching"] = True
				ready_event.set()
			player.queue.append(track)
			player.queue_updated_event.set()
			queued_count += 1
			if not is_playlist_result:
				last_single = track
		if queued_count == 0:
			raise ValueError("再生可能な動画が見つかりませんでした。")
	except Exception as e:
		logger.error(f"play_music 解析エラー: {e}")
		await load_error_embed(ctx, e, edit_msg=wait_msg)
		return
	# 追加完了通知
	if is_playlist_result:
		await playlist_added_embed(ctx, info, queued_count, edit_msg=wait_msg)
	elif not is_idle and last_single:
		await queue_added_embed(ctx, last_single, len(player.queue))
	# アイドル状態なら即時再生を開始する
	if is_idle:
		bot.loop.create_task(play_next_song(ctx, bot))