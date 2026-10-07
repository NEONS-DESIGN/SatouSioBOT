import asyncio
import collections
import concurrent.futures
import contextlib
import itertools
import random
import subprocess
import time
import weakref
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import aiohttp
import discord
from discord.ext import commands

from module import extractor, meta_cache
from module.embed import (
	music_info_embed, preparing_audio_embed, playlist_added_embed, queue_added_embed,
	play_completed_embed, load_error_embed, skip_error_embed, playback_error_embed, alone_leave_embed,
	voice_reconnecting_embed, voice_reconnected_embed, voice_reconnect_failed_embed,
)
from module.errors import AudioOpenError, UserFacingError, is_permanent, report_error
from module.logger import get_bot_logger, perf
from module.options import FFMPEG_OPTIONS, app_config
from module.priority import Priority, boost_playback_thread, set_priority
from module.sqlite import get_guild_settings
from module.utils import UNKNOWN_TITLE, format_duration

logger = get_bot_logger()

# キューの先頭から何曲先までストリームURLを先読みするか
PREFETCH_AHEAD = 2
# ストリームURL解決のリトライ間隔(秒)
STREAM_RETRY_DELAY = 2.0
# stream_url を使い回すときに、曲の長さに加えて有効期限まで残っていてほしい秒数
STREAM_EXPIRE_MARGIN = 300
# 検索クエリに付与する yt-dlp の検索プレフィックス (先頭1件のみ取得)
SEARCH_PREFIX = "ytsearch1:"
# URL にこれらが含まれる場合はプレイリストとして扱う
PLAYLIST_URL_MARKERS = ("list=", "playlist")
# 1回の read() で返す音声の長さ(秒) と Discord へ送る音声のサンプリングレート
FRAME_SECONDS = discord.opus.Encoder.FRAME_LENGTH / 1000
OUTPUT_SAMPLE_RATE = discord.opus.Encoder.SAMPLING_RATE
# 曲の残りがこの秒数 (実時間) を切ったら、次の曲の FFmpeg を起動して最初のフレームまで準備しておく
PRELOAD_LEAD = 15.0
# 次の曲を準備するタイミングを確認する間隔(秒)
PRELOAD_CHECK_INTERVAL = 1.0
# 設定変更でソースを差し替えた後、旧ソースを停止するまでの猶予(秒)。再生スレッドが読み込み中の旧ソースを止めないため
SOURCE_SWAP_GRACE = 0.2
# 最初のフレームが得られなかったとき、FFmpeg の終了コードを待つ最大秒数
FFMPEG_EXIT_WAIT = 1.0
# Ogg Opus の先頭に付くヘッダパケット (音声データではないため送信しない)
_OPUS_HEADER_PREFIXES = (b"OpusHead", b"OpusTags")
# 再生の途切れ検出: read() の所要時間、または前回の read() からの遅れがこの秒数を超えたら警告する
AUDIO_STALL_THRESHOLD = 0.06
# 再生タイミングの集計間隔(秒)。debug 有効時にこの間隔で最大値を出力する
AUDIO_STATS_INTERVAL = 10.0
# read() の間隔がこの秒数未満なら、遅れを取り戻すためのまとめ送りとみなす
AUDIO_BURST_GAP = 0.005
# イベントループの遅延監視: 確認間隔(秒) と、警告する遅延(秒)
LOOP_LAG_CHECK_INTERVAL = 0.1
LOOP_LAG_THRESHOLD = 0.05
# RTP タイムスタンプが 1 秒に進む量 (Opus は 48kHz) と、32bit で一周する値
RTP_CLOCK_RATE = discord.opus.Encoder.SAMPLING_RATE
RTP_TIMESTAMP_MOD = 1 << 32
# discord.py が送信を止めるとき (曲の終了・一時停止) に一度に送る無音パケットの数と、それで進む RTP タイムスタンプ
SILENCE_FRAMES = 5
SILENCE_SAMPLES = SILENCE_FRAMES * discord.opus.Encoder.SAMPLES_PER_FRAME
# 通信断で VC から切断されたときの再接続: 試行回数、n 回目の前に待つ秒数 (n 倍)、1 回の接続のタイムアウト(秒)
VOICE_RECONNECT_ATTEMPTS = 3
VOICE_RECONNECT_DELAY = 3.0
VOICE_CONNECT_TIMEOUT = 30.0
# discord.py 自身の再接続を待つ最大秒数 (1 回の試行 = 待ち数秒 + 接続 30 秒を覆う長さ) と確認間隔
VOICE_BUILTIN_RECONNECT_WAIT = 65.0
VOICE_STATE_POLL_INTERVAL = 0.5
# 曲の残りがこの秒数 (元音源) 未満で切断された場合は、その曲を再開せずに次の曲へ進む
RESUME_MIN_REMAINING = 5.0
# 終了時に抽出ワーカーの終了を待つ最大秒数
WORKER_EXIT_TIMEOUT = 5.0

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

def _cancelling_self() -> bool:
	"""実行中のタスク自身が取り消されているか (待っていた別のタスクだけが取り消された場合は False)"""
	task = asyncio.current_task()
	return task is not None and task.cancelling() > 0

async def monitor_loop_lag() -> None:
	"""イベントループ (メインスレッド) の遅延を監視し、しきい値を超えたら警告する。再生の途切れと同時刻かで原因を切り分ける"""
	while True:
		started = time.perf_counter()
		await asyncio.sleep(LOOP_LAG_CHECK_INTERVAL)
		lag = time.perf_counter() - started - LOOP_LAG_CHECK_INTERVAL
		if lag > LOOP_LAG_THRESHOLD:
			logger.warning(f"[STALL] イベントループが {lag * 1000:.0f}ms 遅延")

# ==========================================
# yt-dlp 情報取得 (プロセス分離)
# ==========================================
_process_pool: concurrent.futures.ProcessPoolExecutor | None = None
# 先読み (NEXT / LATER) が同時に使えるワーカー数。/p などの CURRENT の抽出が待たされないよう 1 つ空けておく
_prefetch_slots = asyncio.Semaphore(max(1, app_config.MAX_WORKER_THREADS - 1))
# 先読みの枠が空くのを待っている解決タスク (その曲の番になったら CURRENT で始め直す)
_queued_prefetches: set[asyncio.Task] = set()

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
		logger.debug(f"抽出ワーカー {app_config.MAX_WORKER_THREADS} 件の準備が完了しました。")
		perf("ワーカー準備", t)
	except Exception as e:
		logger.error(f"抽出ワーカーの準備に失敗しました: {e!r}")

def shutdown_process_pool() -> None:
	"""ProcessPoolExecutorを停止し、抽出中の子プロセスも終了させて終了を見届ける (終了時に呼ぶ。ブロッキング)"""
	global _process_pool
	pool, _process_pool = _process_pool, None
	if pool is None:
		return
	# terminate_workers() は内部のプロセス一覧を破棄するため、先に控えておく
	processes = list((pool._processes or {}).values())
	pool.terminate_workers()
	# 再起動したときに古いワーカー (と常駐 Deno) が新しいものと並んで残らないよう、終了を待つ
	deadline = time.monotonic() + WORKER_EXIT_TIMEOUT
	for process in processes:
		process.join(max(deadline - time.monotonic(), 0))
		if process.is_alive():
			logger.warning(f"抽出ワーカー (pid {process.pid}) が終了しませんでした。")

def _reset_broken_pool(pool: concurrent.futures.ProcessPoolExecutor) -> None:
	"""壊れたプロセスプールを破棄し、次回利用時に作り直させる"""
	global _process_pool
	if _process_pool is pool:
		_process_pool = None
		pool.shutdown(wait=False, cancel_futures=True)

async def _run_extract(query: str, is_fast: bool, priority: Priority) -> dict:
	"""extractor.extract をプロセスプールで実行する。プールが壊れていた場合は作り直して1回だけ再試行する"""
	loop = asyncio.get_running_loop()
	pool = _get_process_pool()
	try:
		return await loop.run_in_executor(pool, extractor.extract, query, is_fast, priority)
	except concurrent.futures.process.BrokenProcessPool:
		logger.warning("抽出ワーカーが異常終了したため、プロセスプールを再生成します。")
		_reset_broken_pool(pool)
		return await loop.run_in_executor(_get_process_pool(), extractor.extract, query, is_fast, priority)

@contextlib.asynccontextmanager
async def _prefetch_slot():
	"""先読みの枠を 1 つ使う。空くのを待つ間は、実行中のタスクを _queued_prefetches に入れておく"""
	task = asyncio.current_task()
	_queued_prefetches.add(task)
	try:
		await _prefetch_slots.acquire()
	finally:
		_queued_prefetches.discard(task)
	try:
		yield
	finally:
		_prefetch_slots.release()

async def fetch_track_info(query: str, is_fast: bool, priority: Priority = Priority.CURRENT) -> dict:
	"""
	extractor.extract をプロセスプール経由で非同期実行する。(毎回取得する。保存した結果を使うかは呼び出し側が決める)
	- is_fast=True : メタデータのみ (曲名検索・再生リスト) / False: ストリームURLまで解決する
	- priority: 抽出を行うワーカーの CPU 優先度。先読みは _prefetch_slots の数までしか同時に実行しない
	"""
	async with contextlib.nullcontext() if priority is Priority.CURRENT else _prefetch_slot():
		t = time.perf_counter()
		info = await _run_extract(query, is_fast, priority)
	perf("メタ抽出(yt-dlp)" if is_fast else "本抽出(yt-dlp/stream_url)", t)
	if info.pop(extractor.FALLBACK_FLAG, False):
		logger.warning(f"高速設定での抽出に失敗したため予備設定で取得しました: {query}")
	return info

async def purge_saved_meta() -> None:
	"""保存期間を過ぎた曲情報・曲名検索・再生リストの保存を消す。(起動時に呼ぶ) 失敗しても起動は続ける"""
	try:
		count = await meta_cache.purge_expired()
	except Exception as e:
		logger.warning(f"保存期間を過ぎた曲情報の削除に失敗しました: {e!r}")
		return
	if count:
		logger.info(f"保存期間を過ぎた曲情報 {count} 件を削除しました。")

# ==========================================
# トラック (plain dict)
# ==========================================
def _entry_fields(entry: dict) -> dict | None:
	"""抽出結果のエントリから track の曲の情報 (url, title, thumbnail, duration) を返す。再生用URLが無ければ None"""
	url = meta_cache.entry_url(entry)
	if not url:
		return None
	return {
		"url": url,
		"title": entry.get("title") or UNKNOWN_TITLE,
		"thumbnail": entry.get("thumbnail"),
		"duration": entry.get("duration") or 0,
	}

def _new_track(entry: dict, requester_id: int, guild_name: str, search_query: str | None = None) -> dict | None:
	"""
	抽出結果のエントリから track dict を生成する。再生用URLが無ければ None。
	- guild_name: ログで曲をどのサーバーのものか示すために持たせる
	- search_query: 保存した曲名検索の結果から作った曲なら、その検索語のキー (取得できなかったときに検索し直す)
	"""
	if (fields := _entry_fields(entry)) is None:
		return None
	return {
		**fields,
		"guild_name": guild_name,
		"search_query": search_query,
		"author_id": requester_id,
		"stream_url": None,
		"http_headers": {},
		"fetch_task": None,
		"wait_msg": None,
		"t_request": None,
		"start": 0.0,
	}

def _stream_expires_in(track: dict) -> float | None:
	"""stream_url の有効期限までの秒数を返す (YouTube などの URL に含まれる expire)。URL が無い・期限を読み取れなければ None"""
	if not track["stream_url"]:
		return None
	try:
		expire = int(parse_qs(urlsplit(track["stream_url"]).query)["expire"][0])
	except (KeyError, IndexError, ValueError):
		return None
	return expire - time.time()

def _stream_lasts(track: dict, expires_in: float) -> bool:
	"""有効期限まで expires_in 秒残っていれば、曲を最後まで再生できるか"""
	return expires_in > track["duration"] + STREAM_EXPIRE_MARGIN

def _reset_stream(track: dict) -> None:
	"""解決済みのストリームURLと解決タスクを捨て、次に再生するときに解決し直させる"""
	track.update(stream_url=None, http_headers={}, fetch_task=None)

def _drop_expiring_stream(track: dict) -> None:
	"""先読み済みの stream_url が再生中に期限切れになりそうなら破棄して解決し直させる (期限が読めない URL はそのまま使う)"""
	expires_in = _stream_expires_in(track)
	if expires_in is not None and not _stream_lasts(track, expires_in):
		_reset_stream(track)

def requeue_track(track: dict) -> dict:
	"""
	ループ・リプレイ用に、表示状態をリセットした track のコピーを返す (曲の先頭から再生する)。
	- stream_url は有効期限が十分に残っていると分かる場合だけ引き継ぎ (再抽出を省く)、それ以外は解決し直させる
	"""
	copy = track | {"fetch_task": None, "wait_msg": None, "t_request": None, "start": 0.0}
	expires_in = _stream_expires_in(track)
	if expires_in is None or not _stream_lasts(track, expires_in):
		copy.update(stream_url=None, http_headers={})
	return copy

def _cancel_fetch(track: dict | None) -> None:
	"""track の解決タスクがあれば取り消す"""
	if track is not None and (task := track["fetch_task"]) is not None:
		task.cancel()

def _tag(guild_name: str) -> str:
	"""ログの先頭に付ける、サーバー名の表記"""
	return f"[{guild_name}]"

def _elapsed(start: float) -> str:
	"""start (time.perf_counter() の値) からの経過秒数のログ用表記"""
	return f"{time.perf_counter() - start:.2f}s"

async def _search(query: str, priority: Priority = Priority.CURRENT) -> dict | None:
	"""曲名で検索し、先頭の曲のエントリを返す (見つからない・URL の無い結果なら None)"""
	info = await fetch_track_info(SEARCH_PREFIX + query, True, priority)
	entries = info.get("entries") or []
	return entries[0] if entries and meta_cache.entry_url(entries[0]) else None

async def _search_again(track: dict, priority: Priority) -> bool:
	"""
	保存した曲名検索の結果から作った曲が取得できないとき、保存を消して検索し直す。
	別の曲が見つかれば track をその曲に差し替えて保存し直し、True を返す。失敗しても例外を出さない
	"""
	query, track["search_query"] = track["search_query"], None
	await meta_cache.forget_search(query)
	try:
		entry = await _search(query, priority)
	except Exception as e:
		logger.warning(f"{_tag(track['guild_name'])} 曲の検索し直しに失敗しました:「{query}」: {e}")
		return False
	if entry is None or (fields := _entry_fields(entry))["url"] == track["url"]:
		# 同じ曲しか見つからなければ、取得できない曲を保存し直さない
		return False
	spawn(meta_cache.save_search(query, entry), name="save_search")
	logger.info(f"{_tag(track['guild_name'])} 検索し直して見つかった曲に差し替えます:「{query}」: {track['title']} → {fields['title']}")
	track.update(fields)
	return True

async def _resolve_stream(track: dict, priority: Priority) -> None:
	"""
	track の stream_url を解決する (MAX_RETRIES 回まで再試行)。最終的に失敗したら例外を送出する。
	- 開始・完了・失敗を試行回数とともにログに出す (先読みはそれと分かるようにする)
	- 保存した曲名検索の結果から作った曲は、失敗したら検索し直す (別の曲に差し替えたら待たずに再試行)。一度取得できた曲は以後検索し直さない
	- 利用者側の原因 (削除・非公開など) の失敗は再試行しない。保存した検索結果も消し、次の /p で検索し直させる
	"""
	max_retries = app_config.MAX_RETRIES
	kind = "先読み" if priority is not Priority.CURRENT else "音源"
	tag = _tag(track["guild_name"])
	label = f"{kind}ロード"
	try:
		for attempt in range(1, max_retries + 1):
			label = f"{kind}ロード ({attempt}/{max_retries})"
			logger.info(f"{tag} {label} 開始: {track['title']}")
			t = time.perf_counter()
			try:
				info = await fetch_track_info(track["url"], False, priority)
				# プレイリスト形式で返ってきた場合は先頭エントリを使用する
				if info.get("entries"):
					info = info["entries"][0]
				if not info.get("url"):
					raise ValueError("ストリームURLが取得できませんでした")
			except Exception as e:
				if attempt < max_retries and track["search_query"] is not None:
					logger.warning(f"{tag} {label} 失敗、検索し直します ({_elapsed(t)}): {track['title']}: {e}")
					if await _search_again(track, priority):
						continue
				if attempt == max_retries or is_permanent(e):
					logger.warning(f"{tag} {label} 失敗、取得を諦めます ({_elapsed(t)}): {track['title']}: {e}")
					if (query := track["search_query"]) is not None:
						track["search_query"] = None
						await meta_cache.forget_search(query)
					raise
				logger.warning(f"{tag} {label} 失敗、{STREAM_RETRY_DELAY:g} 秒後に再試行します ({_elapsed(t)}): {track['title']}: {e}")
				await asyncio.sleep(STREAM_RETRY_DELAY)
				continue
			track["stream_url"] = info["url"]
			track["http_headers"] = info.get("http_headers") or {}
			track["duration"] = info.get("duration") or track["duration"]
			# 取得できた曲は、ループなどで後から一時的に失敗しても別の曲へ差し替えない
			track["search_query"] = None
			logger.info(f"{tag} {label} 完了 ({_elapsed(t)}): {track['title']}")
			return
	except asyncio.CancelledError:
		logger.info(f"{tag} {label} 中止: {track['title']}")
		raise

def ensure_stream(track: dict, priority: Priority = Priority.CURRENT) -> asyncio.Task:
	"""
	track のストリームURL解決タスクを返す。未開始なら priority で開始する (同じ track で多重起動しない)。
	- 既に始まっている解決は、開始時の優先度のまま完了を待つ
	"""
	task = track["fetch_task"]
	if task is None:
		task = spawn(_resolve_stream(track, priority), name=f"resolve:{track['title']}", log_errors=False)
		track["fetch_task"] = task
		if priority is not Priority.CURRENT:
			_prefetch_tasks.add(task)
	return task

# 先読みの優先度で始めた解決タスク
_prefetch_tasks: weakref.WeakSet[asyncio.Task] = weakref.WeakSet()

def _restart_prefetch_for_playback(track: dict) -> None:
	"""
	先読みの枠を待っている・先読みで失敗した解決は、その曲の番では取り消して CURRENT で始め直させる
	(枠待ちで再生を遅らせない・一時的な通信断で失敗したまま飛ばさないため)
	"""
	task = track["fetch_task"]
	if task not in _prefetch_tasks:
		return
	if task in _queued_prefetches or (task.done() and not task.cancelled() and task.exception() is not None):
		task.cancel()
		track["fetch_task"] = None

# ==========================================
# GuildMusicPlayer (ギルド単位の管理クラス)
# ==========================================
@dataclass(slots=True, eq=False)
class GuildMusicPlayer:
	"""
	ギルド単位の再生状態を管理するクラス。get_player() / discard_player() を通して生成・破棄する。
	- queue: 再生待ちの track dict / current: 再生中 (または再生準備中) の track
	- speed / keep_pitch: 再生速度とピッチ維持の有無。プレイヤーの破棄 (退出・切断・再生終了) で既定値に戻る
	- text_channel: 最後に /p が実行されたテキストチャンネル (自動退出・通信断の通知先)
	- alone_task: 聴者不在時の自動退出タイマー (動作中のみ)
	- prefetch_task: 実行中の先読み (同時に1曲まで)
	- advance_lock: 次曲への遷移とソース差し替えを直列化し、二重再生を防ぐ
	- pending_requests: 処理中の /p の件数 (曲の取得に失敗したときに、他の /p を巻き込んで退出しないため)
	- watch_task: 再生中の曲の残り時間を見て、次の曲を準備するタスク
	- preload_target / preload_track / preload_task: 準備した次の曲。target は準備の基になった曲 (キュー先頭、またはループ時の現在曲)、
	  track は実際に再生する track (ループ時は現在曲のコピー)、task は FFmpeg を起動して YTDLSource を返すタスク
	- source: 再生中の曲のソース (通信断で切断されたときの再開位置に使う)
	- lost_channel: 通信断で切断された VC (再接続先)。再接続を待つ間だけ設定される
	"""
	guild_id: int
	queue: collections.deque[dict] = field(default_factory=collections.deque)
	loop: bool = False
	current: dict | None = None
	speed: float = 1.0
	keep_pitch: bool = True
	text_channel: discord.abc.Messageable | None = None
	alone_task: asyncio.Task | None = None
	prefetch_task: asyncio.Task | None = None
	advance_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
	pending_requests: int = 0
	watch_task: asyncio.Task | None = None
	preload_target: dict | None = None
	preload_track: dict | None = None
	preload_task: asyncio.Task | None = None
	source: YTDLSource | None = None
	lost_channel: discord.abc.Connectable | None = None

	def prefetch(self) -> None:
		"""
		キュー先頭 PREFETCH_AHEAD 曲のストリームURLを、先頭から1曲ずつ順に解決する。
		- 次曲 (Priority.NEXT) の解決が終わるまで、それ以降 (Priority.LATER) は始めない
		- 曲の切り替え中 (advance_lock 取得中) は始めない。現在の曲の解決と再生開始を優先し、切り替え後に呼び直される
		"""
		if self.advance_lock.locked() or self.prefetch_task is not None:
			return
		for index, track in enumerate(itertools.islice(self.queue, PREFETCH_AHEAD)):
			_drop_expiring_stream(track)
			# 解決済み・解決中・失敗済み (その曲の番で取り直す) の曲は飛ばす
			if track["stream_url"] or track["fetch_task"] is not None:
				continue
			self.prefetch_task = ensure_stream(track, Priority.NEXT if index == 0 else Priority.LATER)
			self.prefetch_task.add_done_callback(self._on_prefetched)
			return

	def _on_prefetched(self, task: asyncio.Task) -> None:
		"""先読み1曲の完了時に次の先読みへ進む (破棄済みのプレイヤーでは何もしない)"""
		self.prefetch_task = None
		if server_music_data.get(self.guild_id) is self:
			self.prefetch()

	def discard_preload(self) -> None:
		"""準備した次の曲のソースを捨てる。FFmpeg の起動中なら、起動を待ってから停止する"""
		task, self.preload_task = self.preload_task, None
		self.preload_target = self.preload_track = None
		if task is not None:
			spawn(_close_preloaded(task), name=f"close_preload:{self.guild_id}", log_errors=False)

	def cancel_alone_timer(self) -> None:
		"""自動退出タイマーを止める (タイマー自身から呼ばれた場合は取り消さない)"""
		task, self.alone_task = self.alone_task, None
		if task is not None and task is not asyncio.current_task():
			task.cancel()

	def cleanup(self) -> None:
		"""進行中の解決タスク (ループ用に準備中のコピーを含む)・自動退出タイマー・次の曲の準備を取り消し、全状態を初期化する"""
		for track in (*self.queue, self.current, self.preload_track):
			_cancel_fetch(track)
		self.cancel_alone_timer()
		if self.watch_task is not None:
			self.watch_task.cancel()
			self.watch_task = None
		self.discard_preload()
		self.queue.clear()
		self.current = None
		self.source = None
		self.lost_channel = None

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
	_rtp_idle.pop(guild_id, None)
	if player := server_music_data.pop(guild_id, None):
		player.cleanup()

async def leave_voice(guild: discord.Guild, vc: discord.VoiceProtocol) -> None:
	"""プレイヤーを破棄してから VC を切断する (停止で発火する次曲処理を動かさない。discord.py の再接続中でも切断する)"""
	discard_player(guild.id)
	await vc.disconnect(force=True)

def is_active(vc: discord.VoiceProtocol | None) -> bool:
	"""VC で再生中または一時停止中か"""
	return bool(vc and (vc.is_playing() or vc.is_paused()))

class MusicVoiceClient(discord.VoiceClient):
	"""
	切断の理由をプレイヤーに伝える VoiceClient。VC への接続はすべてこのクラスで行う。
	キック・チャンネル削除はプレイヤーを破棄し、通信断は lost_channel を記録して次の曲の処理で再接続させる
	"""
	async def on_voice_state_update(self, data: dict) -> None:
		# discord.py の外部切断の判定に「接続済み」を加え、再接続中や遅れて届いた切断通知をキックと取り違えない (内部属性が無ければキック扱い)
		if (
			data.get("channel_id") is None
			and self.is_connected()
			and not getattr(self._connection, "_expecting_disconnect", False)
		):
			logger.info(f"ギルド {self.guild.id} の VC から外部の操作で切断されました。")
			discard_player(self.guild.id)
		await super().on_voice_state_update(data)

	def cleanup(self) -> None:
		# Bot 自身による切断は先にプレイヤーを破棄するため、ここで曲が残っているのは予期しない切断だけ
		player = server_music_data.get(self.guild.id)
		if player is not None and (player.current is not None or player.queue):
			player.lost_channel = self.channel
		super().cleanup()

# ==========================================
# キュー・再生の操作 (コマンドから呼ぶ)
# ==========================================
def _restart_with(player: GuildMusicPlayer, vc: discord.VoiceClient, front: list[dict]) -> None:
	"""front の曲をキュー先頭に並べ、現在の曲を止めて先頭から再生させる (current を外すため、ループ中でも二重に追加されない)"""
	player.queue.extendleft(reversed(front))
	player.current = None
	vc.stop()

def replay(player: GuildMusicPlayer, vc: discord.VoiceClient) -> None:
	"""再生中の曲を最初から再生し直す (ストリームURLは期限内なら使い回す)。再生中・一時停止中に呼ぶこと"""
	_restart_with(player, vc, [requeue_track(player.current)])

def play_now(player: GuildMusicPlayer, vc: discord.VoiceClient, index: int) -> dict:
	"""
	キューの index 番目 (0 始まり) の曲を今すぐ再生させ、その track を返す。再生中・一時停止中に呼ぶこと。
	- 再生中の曲は最初から再生し直すコピーにして、選んだ曲の次に置く
	"""
	track = player.queue[index]
	del player.queue[index]
	_restart_with(player, vc, [track, requeue_track(player.current)])
	return track

def remove_tracks(player: GuildMusicPlayer, begin: int, stop: int) -> int:
	"""キューの [begin, stop) (0 始まり) の曲を取り除き、解決中なら取り消して件数を返す"""
	tracks = list(player.queue)
	removed = tracks[begin:stop]
	for track in removed:
		_cancel_fetch(track)
	del tracks[begin:stop]
	player.queue.clear()
	player.queue.extend(tracks)
	player.prefetch()
	return len(removed)

def shuffle_queue(player: GuildMusicPlayer) -> None:
	"""キューの順番を混ぜ、新しい先頭から先読みし直す"""
	random.shuffle(player.queue)
	player.prefetch()

def pause(vc: discord.VoiceClient) -> None:
	"""一時停止する。準備済みの次の曲は、一時停止が長引くと URL の期限が切れうるため捨てる (再開後、終わり際なら準備し直される)"""
	vc.pause()
	mark_rtp_idle(vc.guild.id)
	if player := server_music_data.get(vc.guild.id):
		player.discard_preload()

async def resume(vc: discord.VoiceClient) -> None:
	"""一時停止を解除する。止まっていた間を遅延として数えず、RTP タイムスタンプを進めてから再開する"""
	if isinstance(vc.source, YTDLSource):
		vc.source.reset_timing()
	await advance_rtp_timestamp(vc)
	vc.resume()

# ==========================================
# YTDLSource (FFmpeg AudioSource ラッパー)
# ==========================================
def _build_before_options(http_headers: dict, start: float) -> str:
	"""FFmpeg の before_options を返す。start 秒からの再生なら -ss、HTTPヘッダーがあれば -headers を付与する"""
	before_options = FFMPEG_OPTIONS["before_options"]
	if start > 0:
		before_options = f"{before_options} -ss {start:.3f}"
	if not http_headers:
		return before_options
	# ヘッダ値に含まれる " や改行はFFmpeg引数を破壊するため除去する
	def _clean(value: object) -> str:
		return str(value).replace('"', "").replace("\r", "").replace("\n", "")
	header_str = "".join(f"{key}: {_clean(value)}\r\n" for key, value in http_headers.items())
	return f'{before_options} -headers "{header_str}"'

def _build_audio_filter(volume: float, speed: float, keep_pitch: bool) -> str | None:
	"""
	音量と再生速度を変える FFmpeg の音声フィルタを返す。どちらも変えないなら None。
	- keep_pitch=True : atempo で音の高さを保ったまま速度だけ変える
	- keep_pitch=False: サンプリングレートを読み替えて、速度と音の高さを同時に変える
	"""
	filters: list[str] = []
	if volume != 1.0:
		filters.append(f"volume={volume:g}")
	if speed != 1.0:
		if keep_pitch:
			filters.append(f"atempo={speed:g}")
		else:
			rate = round(OUTPUT_SAMPLE_RATE * speed)
			filters.append(f"aresample={OUTPUT_SAMPLE_RATE},asetrate={rate},aresample={OUTPUT_SAMPLE_RATE}")
	return ",".join(filters) or None

def _build_options(volume: float, speed: float, keep_pitch: bool) -> str:
	"""FFmpeg の出力側 options を返す。音量・速度の変更があれば -af を付与する"""
	options = FFMPEG_OPTIONS["options"]
	if audio_filter := _build_audio_filter(volume, speed, keep_pitch):
		return f"{options} -af {audio_filter}"
	return options

class YTDLSource(discord.FFmpegOpusAudio):
	"""
	解決済み track のストリームURLをFFmpegで Opus にエンコードして再生するAudioSource。
	- 音量・再生速度は FFmpeg のフィルタで処理し、Bot 本体ではエンコードしない (変更時はソースを作り直す)
	- FFmpeg は再生中の処理として CPU 優先度を上げる
	- position: 元音源での次に返すフレームの位置(秒)。設定変更時・通信断からの再開位置に使う
	- stream_url が無い track では ValueError を送出する
	"""
	def __init__(self, track: dict, volume: float, *, speed: float = 1.0, keep_pitch: bool = True, start: float = 0.0) -> None:
		if not track["stream_url"]:
			raise ValueError(f"ストリームURLが存在しません: {track['title']}")
		super().__init__(
			track["stream_url"],
			before_options=_build_before_options(track["http_headers"], start),
			options=_build_options(volume, speed, keep_pitch),
		)
		set_priority(Priority.PLAYBACK, self._process.pid)
		self.data = track
		self.title: str = track["title"]
		self.volume = volume
		self.speed = speed
		self.keep_pitch = keep_pitch
		self.position = start
		self._primed: bytes | None = None
		self._in_header = True
		self._last_read_end: float | None = None
		self._clock_start: float | None = None
		self._frames = 0
		self._stats_start = 0.0
		self._max_read = self._max_gap = self._max_late = 0.0
		self._bursts = 0

	def matches(self, volume: float, speed: float, keep_pitch: bool) -> bool:
		"""このソースが指定の音量・速度設定で起動されたものか"""
		return (self.volume, self.speed, self.keep_pitch) == (volume, speed, keep_pitch)

	def _read_packet(self) -> bytes:
		"""次の Opus パケットを返す (先頭のヘッダパケットは読み飛ばす)。終端・失敗時は b"" """
		data = super().read()
		if self._in_header:
			while data.startswith(_OPUS_HEADER_PREFIXES):
				data = super().read()
			self._in_header = False
		return data

	def prime(self) -> bool:
		"""最初のフレームを先に読んで保持する (FFmpeg の接続・シーク待ちを再生開始前に済ませる)。音声が得られたら True。ブロッキング"""
		if self._primed is None:
			self._primed = self._read_packet()
		return bool(self._primed)

	def skip_to(self, position: float) -> None:
		"""元音源で position 秒の位置までフレームを読み捨てる (起動待ちの間に旧ソースが進んだ分を詰める)。ブロッキング"""
		step = FRAME_SECONDS * self.speed
		while self.prime() and self.position + step <= position:
			self.position += step
			self._primed = None

	def failure_reason(self) -> str:
		"""最初のフレームが得られなかった理由 (FFmpeg の終了コードなど) を返す。ブロッキング"""
		if self._current_error:
			return str(self._current_error)
		try:
			returncode = self._process.wait(timeout=FFMPEG_EXIT_WAIT)
		except subprocess.TimeoutExpired:
			return "FFmpeg の出力なし"
		return f"FFmpeg が終了コード {returncode} で終了"

	def read(self) -> bytes:
		"""次の 20ms 分の Opus パケットを返し、元音源での再生位置を進める。呼び出し元の送信スレッドを最優先にする"""
		boost_playback_thread()
		started = time.perf_counter()
		data = self._primed if self._primed is not None else self._read_packet()
		self._primed = None
		finished = time.perf_counter()
		if data:
			self.position += FRAME_SECONDS * self.speed
			self._record_timing(started, finished)
		self._last_read_end = finished
		return data

	def reset_timing(self) -> None:
		"""タイミング計測の基準をリセットする (一時停止の間を遅延として数えないよう、再開時に呼ぶ)"""
		self._last_read_end = None
		self._clock_start = None

	def _record_timing(self, started: float, finished: float) -> None:
		"""read() のタイミングを記録する。途切れは WARNING、AUDIO_STATS_INTERVAL ごとの最大値は DEBUG で出す"""
		read_time = finished - started
		gap = started - self._last_read_end if self._last_read_end is not None else FRAME_SECONDS
		if self._clock_start is None:
			self._clock_start = self._stats_start = started
			self._frames = 0
		# 予定時刻 (最初の read() + 20ms × 回数) からの遅れ
		late = started - (self._clock_start + self._frames * FRAME_SECONDS)
		self._frames += 1
		if read_time > AUDIO_STALL_THRESHOLD:
			logger.warning(f"[STALL] read() が {read_time * 1000:.0f}ms 停止 (FFmpeg 出力待ち): {self.title}")
		if gap > FRAME_SECONDS + AUDIO_STALL_THRESHOLD:
			logger.warning(f"[STALL] 再生スレッドが {gap * 1000:.0f}ms 遅延 (read() の間隔): {self.title}")
		self._max_read = max(self._max_read, read_time)
		self._max_gap = max(self._max_gap, gap)
		self._max_late = max(self._max_late, late)
		if gap < AUDIO_BURST_GAP:
			self._bursts += 1
		if started - self._stats_start >= AUDIO_STATS_INTERVAL:
			logger.debug(
				f"[AUDIO] read 最大 {self._max_read * 1000:.1f}ms / 間隔 最大 {self._max_gap * 1000:.1f}ms / "
				f"予定からの遅れ 最大 {self._max_late * 1000:.1f}ms / まとめ送り {self._bursts} 回: {self.title}"
			)
			self._stats_start = started
			self._max_read = self._max_gap = self._max_late = 0.0
			self._bursts = 0

def _open_source(track: dict, volume: float, speed: float, keep_pitch: bool, start: float = 0.0) -> YTDLSource:
	"""YTDLSource を生成して最初のフレームまで読み込む。音声が得られなければ例外。ブロッキングのためイベントループ外で呼ぶ"""
	source = YTDLSource(track, volume, speed=speed, keep_pitch=keep_pitch, start=start)
	try:
		if not source.prime():
			raise AudioOpenError(f"音声を読み込めませんでした ({source.failure_reason()})")
	except Exception:
		source.cleanup()
		raise
	return source

def _open_synced_source(old: YTDLSource, volume: float, speed: float, keep_pitch: bool) -> YTDLSource:
	"""old の再生位置から新しい設定のソースを起動し、起動待ちの間に old が進んだ分を読み飛ばして返す。ブロッキング"""
	new = _open_source(old.data, volume, speed, keep_pitch, old.position)
	try:
		new.skip_to(old.position)
	except Exception:
		new.cleanup()
		raise
	return new

# ==========================================
# 再生制御
# ==========================================
async def _notify[T](coro: Coroutine[Any, Any, T]) -> T | None:
	"""通知メッセージを送信する。送信失敗 (権限不足・通信断など) で再生制御を止めないよう、失敗時は None を返す"""
	try:
		return await coro
	except (discord.HTTPException, aiohttp.ClientError, OSError) as e:
		logger.warning(f"通知メッセージの送信に失敗しました: {e}")
		return None

async def _volume(guild_id: int) -> float:
	"""ギルド設定の音量 (メモリキャッシュ) を返す"""
	return (await get_guild_settings(guild_id)).volume

# ギルドごとに、送信が止まった時刻 (time.perf_counter()) と、それ以降に discord.py が無音パケットを送った回数
_rtp_idle: dict[int, tuple[float, int]] = {}

def mark_rtp_idle(guild_id: int) -> None:
	"""
	送信が止まったことを記録する。曲の終了時・一時停止時に呼ぶ。
	- 一時停止中に停止した (/skip など) 場合は、止まった時刻を一時停止時のままにして無音の回数だけ数える (discord.py は両方で無音を送る)
	"""
	since, bursts = _rtp_idle.get(guild_id, (time.perf_counter(), 0))
	_rtp_idle[guild_id] = (since, bursts + 1)

async def advance_rtp_timestamp(vc: discord.VoiceClient) -> None:
	"""
	送信が止まっていた時間だけ RTP タイムスタンプを進める (受信側に無音区間として扱わせる)。送信を再開する直前に呼ぶ。
	- 送られた無音パケットの分はタイムスタンプが進んでいるため差し引き、その分の時間がまだ経っていなければ経つまで待つ
	"""
	idle = _rtp_idle.pop(vc.guild.id, None)
	if idle is None:
		return
	since, bursts = idle
	elapsed = round((time.perf_counter() - since) * RTP_CLOCK_RATE) - bursts * SILENCE_SAMPLES
	if elapsed < 0:
		await asyncio.sleep(-elapsed / RTP_CLOCK_RATE)
		return
	vc.timestamp = (vc.timestamp + elapsed) % RTP_TIMESTAMP_MOD

def _played_until(guild_id: int, source: YTDLSource) -> float:
	"""再生を終えた位置 (元音源の秒) を返す。設定変更でソースが差し替わっていれば、差し替え後のソースの位置を使う"""
	player = server_music_data.get(guild_id)
	latest = player.source if player is not None else None
	if isinstance(latest, YTDLSource) and latest.data is source.data:
		return latest.position
	return source.position

def _make_after_callback(ctx: commands.Context, loop: asyncio.AbstractEventLoop, source: YTDLSource) -> Callable[[Exception | None], None]:
	"""再生終了時 (再生スレッドから呼ばれる) に、終了をログに出して次曲の再生をイベントループへ投げるコールバックを返す"""
	guild_id = ctx.guild.id
	track = source.data
	def _after_playing(error: Exception | None) -> None:
		mark_rtp_idle(guild_id)
		try:
			played = format_duration(_played_until(guild_id, source))
			logger.info(f"{_tag(track['guild_name'])} 再生終了 ({played} / {format_duration(track['duration'])}): {track['title']}")
		except Exception as e:
			# ログの失敗で次の曲へ進めなくならないようにする
			logger.warning(f"再生終了の記録に失敗しました (ギルド {guild_id}): {e!r}")
		if error:
			logger.error(f"再生時エラー (ギルド {guild_id}): {error}")
		try:
			loop.call_soon_threadsafe(lambda: spawn(_on_playback_end(ctx, source), name=f"play_next:{guild_id}"))
		except RuntimeError:
			# 終了処理でループが閉じた後に呼ばれた
			pass
	return _after_playing

async def _on_playback_end(ctx: commands.Context, source: YTDLSource) -> None:
	"""
	再生スレッドの終了後に次の曲へ進む。
	- discord.py は再接続の待ち (30 秒) が切れると stop() を経ずに再生スレッドを終え、is_playing() が True のまま残る。
	  その場合は停止済みにして、切れた曲を切断された位置から再生し直させる
	- 止めた曲の再生スレッドが抜ける前に次の曲が始まっていた場合は、送信停止の記録を捨てる
	"""
	vc = ctx.guild.voice_client
	playing = vc.source if vc is not None and vc.is_playing() else None
	if isinstance(playing, YTDLSource) and playing.data is not source.data:
		# 止めた曲の再生スレッドが抜ける前に次の曲が始まっていた。この曲の送信停止の記録は次の曲の補正を狂わせるため捨てる
		_rtp_idle.pop(ctx.guild.id, None)
	elif isinstance(playing, YTDLSource):
		logger.warning(f"ギルド {ctx.guild.id} の再生が VC の再接続を待ちきれずに止まったため、切れた位置から再生し直します。")
		vc.stop()
		if (player := server_music_data.get(ctx.guild.id)) is not None:
			_resume_point(player)
	await play_next_song(ctx)

async def play_next_song(ctx: commands.Context) -> None:
	"""キューの次の曲を再生し (本体は _advance)、ロック解放後に先読みを再開する"""
	guild = ctx.guild
	player = server_music_data.get(guild.id)
	if player is None:
		# 退出時の停止で遅れて記録された、送信停止の時刻を捨てる
		_rtp_idle.pop(guild.id, None)
		return
	try:
		async with player.advance_lock:
			await _advance(ctx, player)
	except Exception as e:
		logger.exception(f"次曲の再生処理で予期せぬエラー (ギルド {guild.id}): {e}")
	if server_music_data.get(guild.id) is player:
		player.prefetch()

def _is_stale(guild: discord.Guild, player: GuildMusicPlayer, vc: discord.VoiceClient) -> bool:
	"""待機中にプレイヤーが破棄された、または VC が切断されたかを返す (切断後の再接続・破棄は次の曲の処理が行う)"""
	return server_music_data.get(guild.id) is not player or not vc.is_connected()

def _put_back(player: GuildMusicPlayer, track: dict, wait_msg: discord.Message | None) -> None:
	"""再生を始められなかった track をキュー先頭に戻す (current を外すため、ループ中でも二重に追加されない)"""
	track["wait_msg"] = wait_msg
	player.queue.appendleft(track)
	player.current = None

async def _wait_message(wait_msg: discord.Message | None, notice: asyncio.Task | None) -> discord.Message | None:
	"""準備中の表示を返す。送信中 (notice) なら送信を待つ"""
	return await _await_quietly(notice) if notice is not None else wait_msg

async def _interrupted(
	guild: discord.Guild,
	player: GuildMusicPlayer,
	vc: discord.VoiceClient,
	track: dict,
	wait_msg: discord.Message | None,
	notice: asyncio.Task | None,
	source: YTDLSource | None = None,
) -> bool:
	"""
	再生の準備中にプレイヤーが破棄された・VC が切断されたかを返す。
	中断する場合は起動済みの source を停止し、track をキュー先頭に戻す (再接続できたらこの曲から再生し直す)
	"""
	if not _is_stale(guild, player, vc):
		return False
	if source is not None:
		await asyncio.to_thread(source.cleanup)
	_put_back(player, track, await _wait_message(wait_msg, notice))
	return True

async def _show_now_playing(
	ctx: commands.Context, source: YTDLSource, queue_count: int, wait_msg: discord.Message | None, notice: asyncio.Task | None,
) -> None:
	"""再生中の表示を出す (準備中の表示があれば、送信を待って置き換える)"""
	await _notify(music_info_embed(ctx, source, queue_count, await _wait_message(wait_msg, notice)))

def _resume_point(player: GuildMusicPlayer) -> None:
	"""
	再生中だった曲を、切断された位置から再生するコピーにしてキュー先頭に戻す。再生中の曲が無ければ何もしない。
	- 残りが RESUME_MIN_REMAINING 未満なら再生し終えたものとして扱う (ループ中は通常どおり末尾へ回す)
	"""
	current, source = player.current, player.source
	player.source = None
	if current is None:
		return
	player.current = None
	track = requeue_track(current)
	position = source.position if source is not None and source.data is current else 0.0
	if current["duration"] and current["duration"] - position < RESUME_MIN_REMAINING:
		if player.loop:
			player.queue.append(track)
		return
	track["start"] = position
	player.queue.appendleft(track)

async def _wait_for_builtin_reconnect(guild: discord.Guild, vc: discord.VoiceClient) -> bool:
	"""discord.py 自身の再接続 (接続オブジェクトは残ったまま未接続) が終わるのを待つ。接続し直せたら True"""
	deadline = time.monotonic() + VOICE_BUILTIN_RECONNECT_WAIT
	while time.monotonic() < deadline:
		if guild.voice_client is not vc:
			return False
		if vc.is_connected():
			return True
		await asyncio.sleep(VOICE_STATE_POLL_INTERVAL)
	return False

async def _drop_stale_vc(guild: discord.Guild, stale_vc: discord.VoiceClient | None) -> None:
	"""切断後も未接続のまま残った接続オブジェクト stale_vc を切り離す (/p などが新しく始めた接続には触れない)"""
	if stale_vc is not None and guild.voice_client is stale_vc and not stale_vc.is_connected():
		await stale_vc.disconnect(force=True)

async def _reconnect(guild: discord.Guild, player: GuildMusicPlayer, channel_id: int, stale_vc: discord.VoiceClient | None) -> str | None:
	"""
	channel_id の VC へ再接続を試みる (VOICE_RECONNECT_ATTEMPTS 回まで、回を追うごとに間隔を空ける)。
	接続できた (/p などで接続し直された場合を含む)・プレイヤーが破棄されたら None、諦めたら利用者向けの理由 (「〜ため」) を返す
	- stale_vc: 切断前の接続オブジェクト。未接続のまま残っていれば、新しく接続する前に切り離す
	"""
	for attempt in range(1, VOICE_RECONNECT_ATTEMPTS + 1):
		await asyncio.sleep(VOICE_RECONNECT_DELAY * attempt)
		if server_music_data.get(guild.id) is not player:
			return None
		vc = guild.voice_client
		if vc is not None and vc.is_connected():
			return None
		channel = guild.get_channel(channel_id)
		if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
			return "ボイスチャンネルが見つからなくなったため"
		if not _has_listener(channel):
			return "ボイスチャンネルに誰もいなくなったため"
		if vc is not None and vc is not stale_vc:
			# /p などで別の接続が進行中。次の回で接続できたか確認する
			continue
		try:
			await _drop_stale_vc(guild, stale_vc)
			await channel.connect(cls=MusicVoiceClient, timeout=VOICE_CONNECT_TIMEOUT)
			logger.info(f"ギルド {guild.id} の VC に再接続しました ({attempt}/{VOICE_RECONNECT_ATTEMPTS})。")
			return None
		except Exception as e:
			# 同時に /p などで接続された場合 (ClientException) は、次の回の確認で接続済みとして扱う
			logger.warning(f"ギルド {guild.id} の VC への再接続に失敗しました ({attempt}/{VOICE_RECONNECT_ATTEMPTS}): {e!r}")
	return "何度か再接続を試みましたが、Discord との音声接続を復旧できなかったため"

async def _recover_voice(guild: discord.Guild, player: GuildMusicPlayer, vc: discord.VoiceClient | None) -> bool:
	"""切断された VC を復旧する。続けられるなら True、できなければプレイヤーを破棄して False (想定外の例外も破棄してから送出する)"""
	try:
		return await _recover_voice_inner(guild, player, vc)
	except BaseException:
		# 再接続待ちのまま残ると on_voice_state_update が破棄しないため、ここで片付ける
		if server_music_data.get(guild.id) is player:
			discard_player(guild.id)
		raise

async def _recover_voice_inner(guild: discord.Guild, player: GuildMusicPlayer, vc: discord.VoiceClient | None) -> bool:
	"""
	_recover_voice の本体。discord.py 自身の再接続中ならその完了を待つ。通信断で切断されていれば (lost_channel あり)、
	再接続して再生中だった曲を切断された位置から流し直し、結果を text_channel に通知する
	"""
	if vc is not None and await _wait_for_builtin_reconnect(guild, vc):
		return True
	channel = player.lost_channel or (vc.channel if vc is not None else None)
	if channel is None or (player.current is None and not player.queue):
		discard_player(guild.id)
		await _drop_stale_vc(guild, vc)
		return False
	player.lost_channel = channel
	_resume_point(player)
	if not player.queue:
		# 再生中の曲が終わり際で、続きの曲も無かった
		discard_player(guild.id)
		await _drop_stale_vc(guild, vc)
		return False
	resumed = player.queue[0] if player.queue[0]["start"] > 0 else None
	_rtp_idle.pop(guild.id, None)
	logger.warning(f"ギルド {guild.id} の VC との接続が切れたため再接続します: {channel.name}")
	text_channel = player.text_channel
	notice = await _notify(voice_reconnecting_embed(text_channel, VOICE_RECONNECT_ATTEMPTS)) if text_channel is not None else None
	reason = await _reconnect(guild, player, channel.id, vc)
	if server_music_data.get(guild.id) is not player:
		# 待つ間に /leave などで停止された (停止の通知はそちらが出す)
		if notice is not None:
			await _notify(notice.delete())
		return False
	player.lost_channel = None
	if reason is not None:
		logger.warning(f"ギルド {guild.id} の VC に再接続できなかったため再生を停止しました: {reason}")
		discard_player(guild.id)
		await _drop_stale_vc(guild, vc)
		if text_channel is not None:
			await _notify(voice_reconnect_failed_embed(text_channel, reason, edit_msg=notice))
		return False
	if text_channel is not None:
		await _notify(voice_reconnected_embed(text_channel, resumed, edit_msg=notice))
	return True

# ==========================================
# 次の曲の先行準備 (曲の切り替え時の無音を無くす)
# ==========================================
def _next_target(player: GuildMusicPlayer) -> dict | None:
	"""次に再生する曲の基になる track を返す。キュー先頭、キューが空でループ中なら現在の曲。無ければ None"""
	if player.queue:
		return player.queue[0]
	if player.loop and player.current:
		return player.current
	return None

def _remaining_seconds(track: dict, source: YTDLSource) -> float | None:
	"""再生中の曲が終わるまでの実時間(秒) を返す。曲の長さが分からなければ None"""
	duration = track["duration"]
	if not duration:
		return None
	return max(0.0, (duration - source.position) / source.speed)

async def _open_preload(player: GuildMusicPlayer, track: dict) -> YTDLSource:
	"""track のストリームURLを (無ければ) 解決し、現在の設定で FFmpeg を起動して最初のフレームまで読んだソースを返す。FFmpeg の起動に失敗したら URL を捨てる"""
	_drop_expiring_stream(track)
	if not track["stream_url"]:
		await ensure_stream(track, Priority.NEXT)
	volume = await _volume(player.guild_id)
	try:
		return await asyncio.to_thread(_open_source, track, volume, player.speed, player.keep_pitch)
	except Exception:
		# URL が失効・拒否されていた可能性があるため、再生時には取得し直させる
		_reset_stream(track)
		raise

async def _close_preloaded(task: asyncio.Task) -> None:
	"""準備タスクの完了を待ち、得られたソースの FFmpeg を停止する"""
	try:
		source = await task
	except Exception:
		return
	await asyncio.to_thread(source.cleanup)

def _start_preload(player: GuildMusicPlayer, target: dict) -> None:
	"""target を基に次の曲の準備を始める。ループで現在の曲をもう一度流す場合は、そのためのコピーを準備する"""
	track = requeue_track(target) if target is player.current else target
	player.preload_target = target
	player.preload_track = track
	player.preload_task = spawn(_open_preload(player, track), name=f"preload:{track['title']}", log_errors=False)
	logger.debug(f"次の曲の準備を始めました: {track['title']}")

async def _watch_for_preload(guild: discord.Guild, player: GuildMusicPlayer) -> None:
	"""
	再生中の曲の残りが PRELOAD_LEAD を切ったら次の曲を準備する。現在の曲が変わる・プレイヤーが破棄されると終わる。
	- 準備後に次の曲が変わったら (キューの操作・ループの切り替え)、準備し直す
	- 曲の長さが分からない曲 (ライブ配信など) では準備しない
	"""
	current = player.current
	while server_music_data.get(guild.id) is player and player.current is current:
		vc = guild.voice_client
		# 一時停止中は準備しない (待っている間に URL の期限が切れうるため。/pause で準備も捨てる)
		source = vc.source if vc is not None and vc.is_playing() else None
		if isinstance(source, YTDLSource):
			remaining = _remaining_seconds(current, source)
			if remaining is None:
				return
			if remaining <= PRELOAD_LEAD:
				target = _next_target(player)
				if player.preload_target is not target:
					player.discard_preload()
					if target is not None:
						_start_preload(player, target)
		await asyncio.sleep(PRELOAD_CHECK_INTERVAL)

def _start_preload_watch(guild: discord.Guild, player: GuildMusicPlayer) -> None:
	"""再生を始めた曲について、次の曲を準備するタスクを起動し直す"""
	if player.watch_task is not None:
		player.watch_task.cancel()
	player.watch_task = spawn(_watch_for_preload(guild, player), name=f"preload_watch:{guild.id}")

def _loop_copy(player: GuildMusicPlayer) -> dict:
	"""ループでキュー末尾に戻す現在の曲のコピーを返す。準備済みのコピーがあればそれを使う"""
	if player.preload_target is player.current and player.preload_track is not None:
		return player.preload_track
	return requeue_track(player.current)

async def _claim_preloaded(player: GuildMusicPlayer, track: dict, volume: float) -> YTDLSource | None:
	"""
	track 用に準備したソースを取り出す。準備が無い・別の曲用・設定が変わった・準備に失敗した場合は None (準備は捨てる)
	- FFmpeg の起動中なら完了を待つ
	"""
	task = player.preload_task
	if task is None or player.preload_track is not track:
		player.discard_preload()
		return None
	player.preload_task = None
	player.preload_target = player.preload_track = None
	try:
		source = await task
	except asyncio.CancelledError:
		# 準備タスクだけが取り消された場合 (cleanup 経由) は、準備なしとして扱う
		if _cancelling_self():
			raise
		return None
	except Exception as e:
		logger.debug(f"次の曲の準備に失敗したため開き直します: {track['title']} ({e!r})")
		return None
	if not source.matches(volume, player.speed, player.keep_pitch):
		await asyncio.to_thread(source.cleanup)
		return None
	logger.debug(f"準備済みのソースで再生します: {track['title']}")
	return source

async def _advance(ctx: commands.Context, player: GuildMusicPlayer) -> None:
	"""
	play_next_song の本体。advance_lock 取得済みで呼ぶこと。
	- ループ有効時は現在の曲をキュー末尾に戻し、キューが空になったら VC を切断してプレイヤーを破棄する
	- 解決・再生に失敗した曲はスキップして次へ進む。既に再生中・一時停止中なら何もしない (二重再生防止)
	"""
	guild = ctx.guild
	while True:
		vc = guild.voice_client
		# /leave や切断でプレイヤーが破棄されていれば終了する
		if server_music_data.get(guild.id) is not player:
			return
		if vc is None or not vc.is_connected():
			# 通信断で切断されていれば再接続し、再生していた曲を続きから流す
			if await _recover_voice(guild, player, vc):
				continue
			return
		if is_active(vc):
			return
		if player.loop and player.current:
			player.queue.append(_loop_copy(player))
		if not player.queue:
			await leave_voice(guild, vc)
			await _notify(play_completed_embed(ctx))
			return
		track = player.queue.popleft()
		player.current = track
		wait_msg: discord.Message | None = track["wait_msg"]
		# 送信中の準備中の表示 (送信を待たずに再生の準備を進める)
		notice: asyncio.Task | None = None
		# 準備がまだ URL の解決中なら準備中と表示する
		preload_task = player.preload_task
		if player.preload_track is track and preload_task is not None and not preload_task.done() and not track["stream_url"] and wait_msg is None:
			notice = spawn(_notify(preparing_audio_embed(ctx)), name=f"preparing:{guild.id}")
		# 曲の終わり際に準備しておいたソースがあれば、FFmpeg の起動を待たずに再生する
		source = await _claim_preloaded(player, track, await _volume(guild.id))
		if source is None:
			_drop_expiring_stream(track)
			# 先読み・使い回しで取得済みの URL か (再生できなければ1回だけ取得し直す)
			resolved_earlier = bool(track["stream_url"])
			if not track["stream_url"]:
				_restart_prefetch_for_playback(track)
				fetch_task = ensure_stream(track)
				if wait_msg is None and notice is None:
					notice = spawn(_notify(preparing_audio_embed(ctx)), name=f"preparing:{guild.id}")
				t_wait = time.perf_counter()
				try:
					await fetch_task
				except asyncio.CancelledError:
					# 解決タスクだけが取り消された場合 (cleanup 経由) は静かに終了する
					if _cancelling_self():
						raise
					return
				except Exception as e:
					player.current = None
					await _notify(skip_error_embed(ctx, track["title"], e, edit_msg=await _wait_message(wait_msg, notice)))
					continue
				perf("stream_url待ち", t_wait)
			volume = await _volume(guild.id)
			if await _interrupted(guild, player, vc, track, wait_msg, notice):
				continue
			try:
				t_ff = time.perf_counter()
				# FFmpeg の起動 (CreateProcess) と最初のフレームの受信はブロッキングで、高負荷時は数十秒かかりうるためイベントループ外で行う
				source = await asyncio.to_thread(_open_source, track, volume, player.speed, player.keep_pitch, track["start"])
				perf("FFmpeg起動(初回フレームまで)", t_ff)
			except Exception as e:
				player.current = None
				if resolved_earlier:
					# 取得済みの URL が失効・拒否されていた可能性があるため、取得し直して同じ曲をもう一度試す
					logger.warning(f"取得済みのストリームURLで再生できなかったため取得し直します: {track['title']} ({e})")
					_reset_stream(track)
					_put_back(player, track, await _wait_message(wait_msg, notice))
					continue
				report_error(f"再生ソース生成エラー (ギルド {guild.id})", e)
				await _notify(playback_error_embed(ctx, track["title"], e, edit_msg=await _wait_message(wait_msg, notice)))
				continue
		if await _interrupted(guild, player, vc, track, wait_msg, notice, source):
			continue
		await advance_rtp_timestamp(vc)
		if await _interrupted(guild, player, vc, track, wait_msg, notice, source):
			continue
		try:
			vc.play(source, after=_make_after_callback(ctx, asyncio.get_running_loop(), source))
		except Exception as e:
			report_error(f"再生開始エラー (ギルド {guild.id})", e)
			await asyncio.to_thread(source.cleanup)
			player.current = None
			await _notify(playback_error_embed(ctx, track["title"], e, edit_msg=await _wait_message(wait_msg, notice)))
			continue
		player.source = source
		player.lost_channel = None
		_start_preload_watch(guild, player)
		position = f"{format_duration(track['start'])} から / " if track["start"] > 0 else ""
		waited = f"、コマンドから {_elapsed(track['t_request'])}" if track["t_request"] is not None else ""
		logger.info(f"{_tag(track['guild_name'])} 再生開始 ({position}{format_duration(track['duration'])}{waited}): {track['title']}")
		if track["t_request"] is not None:
			perf("★総計 コマンド→再生開始", track["t_request"])
		# 通知の送信を待たずにロックを放し、先読みを始めさせる
		spawn(_show_now_playing(ctx, source, len(player.queue), wait_msg, notice), name=f"now_playing:{guild.id}")
		return

async def _cleanup_later(source: discord.AudioSource) -> None:
	"""差し替え済みの旧ソースを、再生スレッドが読み終える猶予をおいてから停止する"""
	await asyncio.sleep(SOURCE_SWAP_GRACE)
	await asyncio.to_thread(source.cleanup)

async def apply_audio_settings(guild: discord.Guild, player: GuildMusicPlayer) -> None:
	"""
	再生中の曲に音量 (ギルド設定) と player の速度設定を反映する。/vol・/speed から呼ぶ。
	- 現在位置から新しい設定で FFmpeg を起動し直し、起動待ちの間に進んだ分を読み飛ばしてから差し替える (準備中は旧ソースが鳴り続ける)
	- 曲の切り替え中なら、切り替えが終わるのを待ってから反映する。何も再生していなければ次の曲から反映される
	- 失敗時は例外を送出する (旧ソースはそのまま再生を続ける)
	"""
	# 準備済みの次の曲は古い設定で起動しているため捨てる (終わり際なら監視タスクが新しい設定で準備し直す)
	player.discard_preload()
	try:
		async with player.advance_lock:
			await _swap_source(guild, player)
	finally:
		# ロック中は先読みが始まらないため、解放後に再開する
		if server_music_data.get(guild.id) is player:
			player.prefetch()

async def _swap_source(guild: discord.Guild, player: GuildMusicPlayer) -> None:
	"""apply_audio_settings の本体。advance_lock 取得済みで呼ぶこと"""
	volume = await _volume(guild.id)
	vc = guild.voice_client
	old = vc.source if is_active(vc) else None
	if not isinstance(old, YTDLSource) or old.matches(volume, player.speed, player.keep_pitch):
		return
	t = time.perf_counter()
	new = await asyncio.to_thread(_open_synced_source, old, volume, player.speed, player.keep_pitch)
	perf("設定変更(FFmpeg再起動)", t)
	# 準備中に曲が終わった・切り替わった・切断された場合は破棄する
	if _is_stale(guild, player, vc) or vc.source is not old or not is_active(vc):
		await asyncio.to_thread(new.cleanup)
		return
	was_paused = vc.is_paused()
	# 差し替えは after コールバックを呼ばないため、次の曲へは進まない
	vc.source = new
	player.source = new
	# set_source は内部で再開するため、一時停止中だった場合は止め直す
	if was_paused:
		vc.pause()
	spawn(_cleanup_later(old), name=f"cleanup_source:{guild.id}")

# ==========================================
# 聴者不在時の自動退出
# ==========================================
def _has_listener(channel: discord.abc.GuildChannel) -> bool:
	"""チャンネルに Bot 以外のメンバーがいるか。メンバー情報を取得できない場合は誤退出を避けるため「いる」とみなす"""
	for user_id in channel.voice_states:
		member = channel.guild.get_member(user_id)
		if member is None or not member.bot:
			return True
	return False

def update_alone_timer(guild: discord.Guild) -> None:
	"""Bot のいる VC に聴者がいなければ自動退出タイマーを開始し、聴者がいれば止める。VC の人の出入り・移動のたびに呼ぶ"""
	player = server_music_data.get(guild.id)
	vc = guild.voice_client
	if player is None or vc is None or not vc.is_connected() or vc.channel is None:
		return
	if _has_listener(vc.channel):
		player.cancel_alone_timer()
	elif player.alone_task is None:
		player.alone_task = spawn(_leave_when_alone(guild, player), name=f"alone_leave:{guild.id}")

async def _leave_when_alone(guild: discord.Guild, player: GuildMusicPlayer) -> None:
	"""ギルド設定の秒数だけ待ち、それでも聴者がいなければ再生を止めて退出し、通知する (0 秒設定なら何もしない)"""
	timeout = (await get_guild_settings(guild.id)).alone_timeout
	if timeout <= 0:
		player.cancel_alone_timer()
		return
	await asyncio.sleep(timeout)
	player.cancel_alone_timer()
	vc = guild.voice_client
	if server_music_data.get(guild.id) is not player or vc is None or not vc.is_connected() or _has_listener(vc.channel):
		return
	channel = player.text_channel
	await leave_voice(guild, vc)
	logger.debug(f"聴者がいないため ギルド {guild.id} のボイスチャンネルから退出しました。")
	if channel is not None:
		await _notify(alone_leave_embed(channel))

async def _await_quietly(task: asyncio.Task | None) -> Any:
	"""task の完了を待って結果を返す。未指定・失敗時は None (例外は spawn 側でログ済み)"""
	if task is None:
		return None
	try:
		return await task
	except Exception:
		return None

def is_in_use(voice_clients: Iterable[discord.VoiceProtocol]) -> bool:
	"""
	どこかのサーバーで Bot が使われているか (定期再起動してよいかの判定に使う)。
	- 再生中・一時停止中・キューあり・/p の処理中・曲の切り替え中・通信断の再接続待ちのプレイヤーがあれば使用中
	- Bot のいる VC に聴者がいれば、何も再生していなくても使用中
	"""
	for player in server_music_data.values():
		if player.current is not None or player.queue or player.pending_requests > 0 or player.advance_lock.locked() or player.lost_channel is not None:
			return True
	for vc in voice_clients:
		if not isinstance(vc, discord.VoiceClient):
			return True
		if is_active(vc) or (vc.channel is not None and _has_listener(vc.channel)):
			return True
	return False

def _is_idle(player: GuildMusicPlayer, vc: discord.VoiceProtocol | None) -> bool:
	"""再生中・再生準備中の曲もキューも無い状態か"""
	return player.current is None and not player.queue and not is_active(vc)

async def _leave_if_unused(guild: discord.Guild, player: GuildMusicPlayer, voice_task: asyncio.Task | None) -> None:
	"""
	曲の取得に失敗したとき、VC で何も再生していなければ退出してプレイヤーを破棄する。
	- このリクエストで接続 (移動) していれば、その完了を待ってから判定する
	- 別のリクエストで再生・曲の切り替えが始まっている、または他の /p が処理中なら何もしない (後の /p の失敗で退出する)
	"""
	await _await_quietly(voice_task)
	vc = guild.voice_client
	if vc is None or server_music_data.get(guild.id) is not player:
		return
	if player.pending_requests > 1 or player.advance_lock.locked() or not _is_idle(player, vc):
		return
	await leave_voice(guild, vc)

def _queue_full_error(limit: int) -> UserFacingError:
	"""キューの上限に達しているときのエラー"""
	return UserFacingError(f"キューの上限({limit}曲)に達しているため追加できません。")

async def _load_single(guild_name: str, url: str) -> dict:
	"""単曲 URL の曲情報をストリームURLまで取得する (開始・完了をログに出す)"""
	logger.info(f"{_tag(guild_name)} 音源ロード開始: {url}")
	t = time.perf_counter()
	info = await fetch_track_info(url, False)
	logger.info(f"{_tag(guild_name)} 音源ロード完了 ({_elapsed(t)}): {info.get('title') or url}")
	return info

async def _load_search(guild_name: str, query: str) -> tuple[dict, str | None]:
	"""
	曲名検索の結果を返す。保存した結果があれば検索を省き、無ければ検索して結果を保存する。
	- 2 つ目の返り値は、保存した結果を使った場合の検索語のキー。(その曲を取得できなかったときに検索し直すため track に付ける) 検索した場合は None
	"""
	key = meta_cache.normalize_query(query)
	if (entry := await meta_cache.get_search(key)) is not None:
		logger.info(f"{_tag(guild_name)} 検索結果を保存済みの情報から取得:「{query}」→ {entry['title']}")
		return {"entries": [entry]}, key
	logger.info(f"{_tag(guild_name)} 曲の検索開始:「{query}」")
	t = time.perf_counter()
	entry = await _search(query)
	logger.info(f"{_tag(guild_name)} 曲の検索完了 ({_elapsed(t)}):「{query}」→ {entry.get('title') if entry else '該当なし'}")
	if entry is None:
		return {"entries": []}, None
	spawn(meta_cache.save_search(key, entry), name="save_search")
	return {"entries": [entry]}, None

async def _load_playlist(guild_name: str, url: str, requester_id: int, start_early: bool) -> tuple[dict, dict | None]:
	"""
	再生リストの曲情報を返す。保存してから CACHE_TTL 秒以内なら、保存した結果をそのまま使う。
	- それより古い保存があり、すぐ再生を始める見込み (start_early) なら、保存した 1 曲目の音源ロードを先に始めてから最新の並びを取り直す。
	  2 つ目の返り値はその先に始めた track (呼び出し側は 1 曲目が変わっていなければ使い、変わっていれば取り消す)
	- 取り直しに失敗したら、保存した結果を使う
	"""
	cached = await meta_cache.get_playlist(url)
	if cached is not None and cached.age < app_config.CACHE_TTL:
		logger.info(f"{_tag(guild_name)} 再生リストを保存済みの情報から取得 ({len(cached.info['entries'])} 曲): {cached.info.get('title') or url}")
		return cached.info, None
	early: dict | None = None
	if cached is not None and start_early:
		early = _new_track(cached.info["entries"][0], requester_id, guild_name)
		if early is not None:
			logger.info(f"{_tag(guild_name)} 保存済みの再生リストの 1 曲目を先に読み込みます: {early['title']}")
			ensure_stream(early)
	logger.info(f"{_tag(guild_name)} 再生リストの取得開始: {url}")
	t = time.perf_counter()
	try:
		info = await fetch_track_info(url, True)
	except asyncio.CancelledError:
		_cancel_fetch(early)
		raise
	except Exception as e:
		if cached is None:
			raise
		logger.warning(f"{_tag(guild_name)} 再生リストを取得できなかったため、保存済みの情報を使います: {url}: {e}")
		return cached.info, early
	logger.info(f"{_tag(guild_name)} 再生リストの取得完了 ({_elapsed(t)}, {len(info.get('entries') or [])} 曲): {info.get('title') or url}")
	spawn(meta_cache.save_playlist(url, info), name="save_playlist")
	return info, early

def _adopt_early(tracks: list[dict], early: dict | None) -> dict | None:
	"""
	先に音源ロードを始めた保存済みの 1 曲目が、取り直した並びの 1 曲目と同じなら tracks[0] をそれに置き換える。
	使わなかった場合は取り消すべき track を返す (使った・無かった場合は None)
	"""
	if early is None:
		return None
	if not tracks or tracks[0]["url"] != early["url"]:
		logger.info(f"{_tag(early['guild_name'])} 再生リストの 1 曲目が変わっていたため、先に始めた読み込みを取り消します: {early['title']}")
		return early
	# タイトル・サムネイルは取り直した最新の情報にする (再生時間は音源ロードの結果で埋まる)
	early.update(title=tracks[0]["title"], thumbnail=tracks[0]["thumbnail"])
	tracks[0] = early
	return None

async def play_music(
	ctx: commands.Context,
	query: str,
	*,
	defer_task: asyncio.Task | None = None,
	voice_task: asyncio.Task | None = None,
	t_request: float | None = None,
) -> None:
	"""URLまたは検索クエリの曲をキューに追加して再生する。処理中の /p の件数を数えながら _play_music を実行する"""
	player = get_player(ctx.guild.id)
	player.pending_requests += 1
	try:
		await _play_music(ctx, player, query, defer_task=defer_task, voice_task=voice_task, t_request=t_request)
	finally:
		player.pending_requests -= 1

async def _play_music(
	ctx: commands.Context,
	player: GuildMusicPlayer,
	query: str,
	*,
	defer_task: asyncio.Task | None,
	voice_task: asyncio.Task | None,
	t_request: float | None,
) -> None:
	"""
	URL・検索語を解析してキューに追加し、アイドル状態なら再生を始める。
	- 応答の保留(defer_task)・待機メッセージ・情報取得・ギルド設定の読み込み・VC 接続(voice_task)は並行して進める (メッセージは defer の後)
	- プレイリストは playlist_limit 件まで追加する。単曲URLはストリームURLまで一度に解決し、曲名は先頭の 1 件を追加する
	"""
	guild_id = ctx.guild.id
	player.text_channel = ctx.channel
	is_idle = _is_idle(player, ctx.guild.voice_client)
	is_url = query.startswith(("http://", "https://"))
	is_single_url = is_url and not any(marker in query for marker in PLAYLIST_URL_MARKERS)

	async def _send_wait_msg() -> discord.Message:
		await _await_quietly(defer_task)
		return await preparing_audio_embed(ctx)

	wait_task = spawn(_send_wait_msg(), name="wait_msg") if is_idle else None
	settings_task = spawn(get_guild_settings(guild_id), name="guild_settings")
	guild_name = ctx.guild.name
	early_fetch: asyncio.Task | None = None
	# 保存済みの再生リストの 1 曲目として先に音源ロードを始めた track (まだ使うか決まっていないもの)
	early_track: dict | None = None
	try:
		search_query: str | None = None
		if is_single_url:
			info = await _load_single(guild_name, query)
		elif is_url:
			info, early_track = await _load_playlist(guild_name, query, ctx.author.id, is_idle)
		else:
			info, search_query = await _load_search(guild_name, query)
		is_playlist_result = "entries" in info and is_url
		entries = info.get("entries", [info])

		settings = await settings_task
		available = settings.queue_limit - len(player.queue)
		if available <= 0:
			raise _queue_full_error(settings.queue_limit)
		limit = min(settings.playlist_limit, available) if is_playlist_result else available
		tracks = [track for entry in entries[:limit] if (track := _new_track(entry, ctx.author.id, guild_name, search_query))]
		if not tracks:
			raise UserFacingError("再生可能な動画が見つかりませんでした。検索語や URL を変えてお試しください。")
		unused, early_track = _adopt_early(tracks, early_track), None
		_cancel_fetch(unused)
		# 単曲URLは解決済みのストリーム情報をそのまま使う
		if is_single_url and not is_playlist_result and info.get("url"):
			tracks[0]["stream_url"] = info["url"]
			tracks[0]["http_headers"] = info.get("http_headers") or {}
		# 再生する見込みなら 1 曲目の解決を今始める (VC 接続・defer・待機メッセージと並行。_advance は同じタスクを待つ)
		if is_idle and not tracks[0]["stream_url"]:
			early_fetch = ensure_stream(tracks[0])
		if voice_task is not None:
			await voice_task
	except BaseException as e:
		if early_fetch is not None:
			early_fetch.cancel()
		_cancel_fetch(early_track)
		if not isinstance(e, Exception):
			raise
		report_error("play_music 解析エラー", e)
		await _await_quietly(defer_task)
		await _notify(load_error_embed(ctx, e, edit_msg=await _await_quietly(wait_task)))
		await _leave_if_unused(ctx.guild, player, voice_task)
		return

	await _await_quietly(defer_task)
	wait_msg: discord.Message | None = await _await_quietly(wait_task)
	# 解析待ちの間に退出 (/leave・切断・自動退出) でプレイヤーが破棄された、または他の /p でキューが埋まっていれば、曲を追加せずに知らせる
	error: UserFacingError | None = None
	if server_music_data.get(guild_id) is not player:
		error = UserFacingError("曲の準備中に退出したため、追加できませんでした。")
	elif (available := settings.queue_limit - len(player.queue)) <= 0:
		error = _queue_full_error(settings.queue_limit)
	if error is not None:
		if early_fetch is not None:
			early_fetch.cancel()
		await _notify(load_error_embed(ctx, error, edit_msg=wait_msg))
		return
	del tracks[available:]
	# 解析待ちの間に別のリクエストで再生が始まっていれば、キュー追加として扱う
	start_playback = is_idle and _is_idle(player, ctx.guild.voice_client)
	if start_playback:
		tracks[0]["t_request"] = t_request
		if not is_playlist_result:
			tracks[0]["wait_msg"] = wait_msg
	player.queue.extend(tracks)
	queue_count = len(player.queue)
	# 追加の通知を待たずに再生を始める (多重起動しても advance_lock と再生中判定で 1 つに収束する)
	if start_playback or not is_active(ctx.guild.voice_client):
		spawn(play_next_song(ctx), name=f"play_next:{guild_id}")
	else:
		player.prefetch()
	if is_playlist_result:
		await _notify(playlist_added_embed(ctx, info, len(tracks), edit_msg=wait_msg))
	elif not start_playback:
		await _notify(queue_added_embed(ctx, tracks[0], queue_count, edit_msg=wait_msg))
