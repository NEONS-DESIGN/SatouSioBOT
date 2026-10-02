import asyncio
import collections
import concurrent.futures
import itertools
import sys
import time
from collections.abc import Coroutine
from typing import Any, TypeVar

import discord
from aiocache import SimpleMemoryCache
from discord.ext import commands

from module import extractor
from module.embed import (
	music_info_embed, preparing_audio_embed, playlist_added_embed, queue_added_embed,
	play_completed_embed, load_error_embed, skip_error_embed, playback_error_embed,
)
from module.logger import get_bot_logger, perf
from module.options import FFMPEG_OPTIONS, app_config
from module.sqlite import get_guild_settings
from module.utils import loading_spinner

logger = get_bot_logger()

T = TypeVar("T")

# キューの先頭から何曲先までストリームURLを先読みするか
PREFETCH_AHEAD = 2
# ストリームURL解決のリトライ間隔(秒)
STREAM_RETRY_DELAY = 2.0
# 検索クエリに付与する yt-dlp の検索プレフィックス (先頭1件のみ取得)
SEARCH_PREFIX = "ytsearch1:"
# URL にこれらが含まれる場合はプレイリストとして扱う
PLAYLIST_URL_MARKERS = ("list=", "playlist")

# ==========================================
# バックグラウンドタスク管理
# ==========================================
# 実行中タスクの参照を保持する (参照が無いと GC で途中消滅しうるため)
_background_tasks: set[asyncio.Task] = set()

def spawn(coro: Coroutine[Any, Any, Any], *, name: str | None = None, log_errors: bool = True) -> asyncio.Task:
	"""
	参照を保持したままバックグラウンドタスクを起動する。
	- log_errors=True なら未処理例外をログに出す。False なら例外を回収するだけ (呼び出し側で扱う場合)
	"""
	task = asyncio.create_task(coro, name=name)
	_background_tasks.add(task)
	def _on_done(t: asyncio.Task) -> None:
		_background_tasks.discard(t)
		if t.cancelled():
			return
		exc = t.exception()
		if exc is not None and log_errors:
			logger.error(f"バックグラウンドタスク {t.get_name()} で例外: {exc!r}")
	task.add_done_callback(_on_done)
	return task

# ==========================================
# yt-dlp 情報取得 (プロセス分離 + キャッシュ)
# ==========================================
_process_pool: concurrent.futures.ProcessPoolExecutor | None = None

def _get_process_pool() -> concurrent.futures.ProcessPoolExecutor:
	"""ProcessPoolExecutorのシングルトンを返す (各子プロセスは起動時に YoutubeDL を事前生成する)"""
	global _process_pool
	if _process_pool is None:
		_process_pool = concurrent.futures.ProcessPoolExecutor(
			max_workers=app_config.MAX_WORKER_THREADS,
			initializer=extractor.init_worker,
		)
	return _process_pool

async def warmup_process_pool() -> None:
	"""全ワーカープロセスを起動して初期化を済ませ、初回リクエストの待ち時間をなくす"""
	loop = asyncio.get_running_loop()
	pool = _get_process_pool()
	t = time.perf_counter()
	try:
		await asyncio.gather(*(loop.run_in_executor(pool, extractor.ping) for _ in range(app_config.MAX_WORKER_THREADS)))
		logger.info(f"抽出ワーカー {app_config.MAX_WORKER_THREADS} 件の準備が完了しました。")
		perf("ワーカー準備", (time.perf_counter() - t) * 1000)
	except Exception as e:
		logger.error(f"抽出ワーカーの準備に失敗しました: {e!r}")

def shutdown_process_pool() -> None:
	"""ProcessPoolExecutorを停止し、実行中の子プロセスも終了させる（終了時に呼ぶ）"""
	global _process_pool
	pool, _process_pool = _process_pool, None
	if pool is None:
		return
	# Python 3.14+ は公開 API で子プロセスを即時終了できる
	terminate_workers = getattr(pool, "terminate_workers", None)
	if terminate_workers is not None:
		terminate_workers()
		return
	# shutdown() は内部のプロセス一覧を破棄するため、先に控えておく
	processes = list((getattr(pool, "_processes", None) or {}).values())
	pool.shutdown(wait=False, cancel_futures=True)
	# 抽出中の子プロセスが残ると Python の終了がブロックされるため直接終了させる
	for process in processes:
		if process.is_alive():
			process.terminate()

def _reset_broken_pool(pool: concurrent.futures.ProcessPoolExecutor) -> None:
	"""壊れたプロセスプールを破棄し、次回利用時に作り直させる"""
	global _process_pool
	if _process_pool is pool:
		_process_pool = None
		pool.shutdown(wait=False, cancel_futures=True)

async def _run_extract(query: str, is_fast: bool) -> dict:
	"""extractor.extract をプロセスプールで実行する。プールが壊れていた場合は作り直して1回だけ再試行する"""
	loop = asyncio.get_running_loop()
	pool = _get_process_pool()
	try:
		return await loop.run_in_executor(pool, extractor.extract, query, is_fast)
	except concurrent.futures.process.BrokenProcessPool:
		logger.warning("抽出ワーカーが異常終了したため、プロセスプールを再生成します。")
		_reset_broken_pool(pool)
		return await loop.run_in_executor(_get_process_pool(), extractor.extract, query, is_fast)

# メタデータ専用のインメモリTTLキャッシュ（失効しないデータのみ保持する）
_meta_cache = SimpleMemoryCache()

async def fetch_track_info(query: str, is_fast: bool) -> dict:
	"""
	extractor.extract をプロセスプール経由で非同期実行する。返り値は共有されうるため変更しないこと。
	- is_fast=True  (メタデータ): CACHE_TTL秒キャッシュする。メタデータは失効しないため安全。
	- is_fast=False (ストリームURL): キャッシュしない。URL失効による403を避けるため毎回解決する。
	"""
	if is_fast:
		hit = await _meta_cache.get(query)
		if hit is not None:
			perf("メタ取得(cacheヒット)", 0.0)
			return hit
	logger.info(f"{'メタデータの新規取得' if is_fast else 'ストリームURLの解決'}: {query}")
	t = time.perf_counter()
	info = await _run_extract(query, is_fast)
	perf("メタ抽出(yt-dlp)" if is_fast else "本抽出(yt-dlp/stream_url)", (time.perf_counter() - t) * 1000)
	if info.pop(extractor.FALLBACK_FLAG, False):
		logger.warning(f"高速設定での抽出に失敗したため予備設定で取得しました: {query}")
	if is_fast:
		await _meta_cache.set(query, info, ttl=app_config.CACHE_TTL)
	return info

# ==========================================
# トラック (plain dict)
# ==========================================
def _new_track(entry: dict, requester_id: int) -> dict | None:
	"""抽出結果のエントリから track dict を生成する。再生用URLが無ければ None"""
	url = entry.get("webpage_url") or entry.get("original_url") or entry.get("url")
	if not url:
		return None
	return {
		"url": url,
		"title": entry.get("title") or "Unknown Title",
		"author_id": requester_id,
		"thumbnail": entry.get("thumbnail"),
		"duration": entry.get("duration") or 0,
		"stream_url": None,
		"http_headers": {},
		"fetch_task": None,
		"wait_msg": None,
		"t_request": None,
	}

def requeue_track(track: dict) -> dict:
	"""ループ・リプレイ用に、ストリーム情報と表示状態をリセットした track のコピーを返す"""
	return {**track, "stream_url": None, "http_headers": {}, "fetch_task": None, "wait_msg": None, "t_request": None}

async def _resolve_stream(track: dict) -> None:
	"""track の stream_url を解決する (MAX_RETRIES 回まで再試行)。最終的に失敗したら例外を送出する"""
	max_retries = app_config.MAX_RETRIES
	for attempt in range(1, max_retries + 1):
		try:
			info = await loading_spinner(
				fetch_track_info(track["url"], False),
				f"音源ロード: {track['title']} ({attempt}/{max_retries})",
			)
			# プレイリスト形式で返ってきた場合は先頭エントリを使用する
			if info.get("entries"):
				info = info["entries"][0]
			if not info.get("url"):
				raise ValueError("ストリームURLが取得できませんでした")
			track["stream_url"] = info["url"]
			track["http_headers"] = info.get("http_headers") or {}
			track["duration"] = info.get("duration") or track["duration"]
			return
		except Exception as e:
			if attempt == max_retries:
				logger.warning(f"{track['title']} の取得に最終的に失敗: {e}")
				raise
			logger.warning(f"{track['title']} のロード失敗、リトライ ({attempt})...")
			await asyncio.sleep(STREAM_RETRY_DELAY)

def ensure_stream(track: dict) -> asyncio.Task:
	"""track のストリームURL解決タスクを返す。未開始なら開始する (同じ track で多重起動しない)"""
	task = track["fetch_task"]
	if task is None:
		task = spawn(_resolve_stream(track), name=f"resolve:{track['title']}", log_errors=False)
		track["fetch_task"] = task
	return task

# ==========================================
# GuildMusicPlayer (ギルド単位の管理クラス)
# ==========================================
class GuildMusicPlayer:
	"""
	ギルド単位の再生状態を管理するクラス。
	- queue: 再生待ちの track dict
	- current: 再生中 (または再生準備中) の track
	- volume: 音量キャッシュ (Noneなら次回再生時にDBから読込、/vol で更新)
	- advance_lock: 次曲への遷移を直列化し、二重再生を防ぐ
	"""
	__slots__ = ("guild_id", "queue", "loop", "current", "volume", "advance_lock")
	def __init__(self, guild_id: int) -> None:
		self.guild_id = guild_id
		self.queue: collections.deque[dict] = collections.deque()
		self.loop = False
		self.current: dict | None = None
		self.volume: float | None = None
		self.advance_lock = asyncio.Lock()
	def prefetch(self) -> None:
		"""キュー先頭 PREFETCH_AHEAD 曲のうち未解決のものについて解決を開始する"""
		for track in itertools.islice(self.queue, PREFETCH_AHEAD):
			if not track["stream_url"]:
				ensure_stream(track)
	def cleanup(self) -> None:
		"""進行中の解決タスクを取り消し、全状態を初期化する"""
		tracks = itertools.chain(self.queue, (self.current,) if self.current else ())
		for track in tracks:
			if (task := track["fetch_task"]) is not None and not task.done():
				task.cancel()
		self.queue.clear()
		self.current = None

# ギルドIDをキーにしたプレイヤー管理辞書
server_music_data: dict[int, GuildMusicPlayer] = {}

def get_player(guild_id: int) -> GuildMusicPlayer:
	"""guild_idに対応するGuildMusicPlayerを返す。存在しない場合は新規生成する"""
	player = server_music_data.get(guild_id)
	if player is None:
		player = server_music_data[guild_id] = GuildMusicPlayer(guild_id)
	return player

def discard_player(guild_id: int) -> None:
	"""プレイヤーを登録解除してクリーンアップする (存在しなければ何もしない)"""
	if player := server_music_data.pop(guild_id, None):
		player.cleanup()

# ==========================================
# YTDLSource (FFmpeg AudioSource ラッパー)
# ==========================================
def _build_before_options(http_headers: dict) -> str:
	"""FFmpeg の before_options を返す。HTTPヘッダーがあれば -headers を付与する"""
	before_options = FFMPEG_OPTIONS["before_options"]
	if not http_headers:
		return before_options
	# ヘッダ値に含まれる " や改行はFFmpeg引数を破壊するため除去する
	def _clean(value: object) -> str:
		return str(value).replace('"', "").replace("\r", "").replace("\n", "")
	header_str = "".join(f"{key}: {_clean(value)}\r\n" for key, value in http_headers.items())
	return f'{before_options} -headers "{header_str}"'

class YTDLSource(discord.PCMVolumeTransformer):
	"""
	解決済み track のストリームURLをFFmpegで再生するAudioSource。
	- PCMVolumeTransformerを継承してリアルタイム音量調整に対応
	- stream_url が無い track では ValueError を送出する
	"""
	def __init__(self, track: dict, volume: float) -> None:
		stream_url = track.get("stream_url")
		if not stream_url:
			raise ValueError(f"ストリームURLが存在しません: {track.get('title', 'Unknown')}")
		source = discord.FFmpegPCMAudio(
			stream_url,
			before_options=_build_before_options(track.get("http_headers") or {}),
			options=FFMPEG_OPTIONS["options"],
			stderr=sys.stderr,
		)
		super().__init__(source, volume)
		self.data = track
		self.title: str = track.get("title", "Unknown Title")
		self.display_url: str = track.get("url", "")

# ==========================================
# 再生制御
# ==========================================
async def _notify(coro: Coroutine[Any, Any, T]) -> T | None:
	"""通知メッセージを送信する。送信失敗 (権限不足など) で再生制御を止めないよう、失敗時は None を返す"""
	try:
		return await coro
	except discord.HTTPException as e:
		logger.warning(f"通知メッセージの送信に失敗しました: {e}")
		return None

def _make_after_callback(ctx: commands.Context, loop: asyncio.AbstractEventLoop):
	"""再生終了時 (FFmpegのスレッドから呼ばれる) に次曲の再生をイベントループへ投げるコールバックを返す"""
	guild_id = ctx.guild.id
	def _after_playing(error: Exception | None) -> None:
		if error:
			logger.error(f"再生時エラー (ギルド {guild_id}): {error}")
		# 終了処理でループが閉じた後に呼ばれた場合は何もしない
		if loop.is_closed():
			return
		asyncio.run_coroutine_threadsafe(play_next_song(ctx), loop)
	return _after_playing

async def play_next_song(ctx: commands.Context) -> None:
	"""
	キューから次のトラックを取り出して再生する。
	- ループ有効時は現在のトラックをキュー末尾に再追加する
	- キューが空になった場合はVCを切断してプレイヤーを破棄する
	- 解決・再生に失敗したトラックはスキップして次へ進む
	- 既に再生中・一時停止中なら何もしない (二重再生防止)
	"""
	guild = ctx.guild
	player = server_music_data.get(guild.id)
	if player is None:
		return
	try:
		async with player.advance_lock:
			await _advance(ctx, player)
	except Exception as e:
		logger.exception(f"次曲の再生処理で予期せぬエラー (ギルド {guild.id}): {e}")

async def _advance(ctx: commands.Context, player: GuildMusicPlayer) -> None:
	"""play_next_song の本体。advance_lock 取得済みで呼ぶこと"""
	guild = ctx.guild
	while True:
		vc = guild.voice_client
		# /leave や切断でプレイヤーが破棄されていれば終了する
		if server_music_data.get(guild.id) is not player:
			return
		if vc is None or not vc.is_connected():
			discard_player(guild.id)
			return
		if vc.is_playing() or vc.is_paused():
			return
		if player.loop and player.current:
			player.queue.append(requeue_track(player.current))
		if not player.queue:
			discard_player(guild.id)
			await vc.disconnect()
			await _notify(play_completed_embed(ctx))
			return
		track = player.queue.popleft()
		player.current = track
		player.prefetch()
		wait_msg: discord.Message | None = track["wait_msg"]
		if not track["stream_url"]:
			if wait_msg is None:
				wait_msg = await _notify(preparing_audio_embed(ctx))
			t_wait = time.perf_counter()
			try:
				await ensure_stream(track)
			except asyncio.CancelledError:
				# 自身ではなく解決タスクだけが取り消された場合 (cleanup 経由) は静かに終了する
				if (current_task := asyncio.current_task()) is not None and current_task.cancelling():
					raise
				return
			except Exception:
				player.current = None
				await _notify(skip_error_embed(ctx, track["title"], edit_msg=wait_msg))
				continue
			perf("stream_url待ち", (time.perf_counter() - t_wait) * 1000)
		if player.volume is None:
			player.volume = (await get_guild_settings(guild.id)).volume
		# 待機中に破棄・切断されていないか再確認する
		if server_music_data.get(guild.id) is not player:
			return
		if not vc.is_connected():
			discard_player(guild.id)
			return
		try:
			t_ff = time.perf_counter()
			source = YTDLSource(track, player.volume)
			vc.play(source, after=_make_after_callback(ctx, asyncio.get_running_loop()))
			perf("FFmpeg起動", (time.perf_counter() - t_ff) * 1000)
		except Exception as e:
			logger.error(f"再生ソース生成エラー (ギルド {guild.id}): {e}")
			player.current = None
			await _notify(playback_error_embed(ctx, track["title"]))
			continue
		if track["t_request"] is not None:
			perf("★総計 コマンド→再生開始", (time.perf_counter() - track["t_request"]) * 1000)
		await music_info_embed(ctx, source, len(player.queue), wait_msg)
		return

async def _await_quietly(task: asyncio.Task | None) -> Any:
	"""task の完了を待って結果を返す。未指定・失敗時は None (例外は spawn 側でログ済み)"""
	if task is None:
		return None
	try:
		return await task
	except Exception:
		return None

def _is_idle(player: GuildMusicPlayer, vc: discord.VoiceProtocol | None) -> bool:
	"""再生中・再生準備中の曲もキューも無い状態か"""
	return player.current is None and not player.queue and not (vc and (vc.is_playing() or vc.is_paused()))

async def play_music(
	ctx: commands.Context,
	query: str,
	*,
	defer_task: asyncio.Task | None = None,
	voice_task: asyncio.Task | None = None,
	t_request: float | None = None,
) -> None:
	"""
	URLまたは検索クエリを解析してキューに追加し、アイドル状態なら再生を開始する。
	- 応答の保留(defer_task)・待機メッセージ送信・情報取得・ギルド設定読込・VC接続(voice_task)を並行して進める
	- メッセージ送信は defer_task の完了後に行う
	- プレイリスト: playlist_limit 件まで追加 (ストリームURLは先読みで解決)
	- 単曲URL: ストリームURLまで一度に解決する
	- 検索クエリ: ytsearch1: でメタデータを取得し、ストリームURLは先読みで解決する
	"""
	guild_id = ctx.guild.id
	player = get_player(guild_id)
	is_idle = _is_idle(player, ctx.guild.voice_client)
	is_url = query.startswith(("http://", "https://"))
	is_single_url = is_url and not any(marker in query for marker in PLAYLIST_URL_MARKERS)

	async def _send_wait_msg() -> discord.Message:
		await _await_quietly(defer_task)
		return await preparing_audio_embed(ctx)

	wait_task = spawn(_send_wait_msg(), name="wait_msg") if is_idle else None
	settings_task = spawn(get_guild_settings(guild_id), name="guild_settings")
	try:
		if is_single_url:
			info = await loading_spinner(fetch_track_info(query, False), "音源の取得")
		else:
			info = await loading_spinner(fetch_track_info(query if is_url else SEARCH_PREFIX + query, True), "メタデータ検索")
		is_playlist_result = "entries" in info and is_url
		entries = info["entries"] if "entries" in info else [info]

		settings = await settings_task
		if player.volume is None:
			player.volume = settings.volume
		available = settings.queue_limit - len(player.queue)
		if available <= 0:
			raise ValueError(f"キューの上限({settings.queue_limit}曲)に達しているため追加できません。")
		limit = min(settings.playlist_limit, available) if is_playlist_result else available
		tracks = [track for entry in entries[:limit] if (track := _new_track(entry, ctx.author.id))]
		if not tracks:
			raise ValueError("再生可能な動画が見つかりませんでした。")
		# 単曲URLは解決済みのストリーム情報をそのまま使う
		if is_single_url and not is_playlist_result and info.get("url"):
			tracks[0]["stream_url"] = info["url"]
			tracks[0]["http_headers"] = info.get("http_headers") or {}
		if voice_task is not None:
			await voice_task
	except Exception as e:
		logger.error(f"play_music 解析エラー: {e}")
		await _await_quietly(defer_task)
		await _notify(load_error_embed(ctx, e, edit_msg=await _await_quietly(wait_task)))
		return

	await _await_quietly(defer_task)
	wait_msg: discord.Message | None = await _await_quietly(wait_task)
	# 解析待ちの間に別のリクエストで再生が始まっていれば、キュー追加として扱う
	start_playback = is_idle and _is_idle(player, ctx.guild.voice_client)
	if start_playback:
		tracks[0]["t_request"] = t_request
		if not is_playlist_result:
			tracks[0]["wait_msg"] = wait_msg
	player.queue.extend(tracks)
	player.prefetch()

	# 追加完了通知
	if is_playlist_result:
		await _notify(playlist_added_embed(ctx, info, len(tracks), edit_msg=wait_msg))
	elif not start_playback:
		await _notify(queue_added_embed(ctx, tracks[0], len(player.queue), edit_msg=wait_msg))
	# 何も再生されていなければ再生処理を起動する (多重起動しても advance_lock と再生中判定で1つに収束する)
	vc = ctx.guild.voice_client
	if start_playback or not (vc and (vc.is_playing() or vc.is_paused())):
		spawn(play_next_song(ctx), name=f"play_next:{guild_id}")
