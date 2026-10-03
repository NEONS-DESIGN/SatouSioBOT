import asyncio
import collections
import concurrent.futures
import itertools
import time
from collections.abc import Coroutine
from typing import Any, TypeVar
from urllib.parse import parse_qs, urlsplit

import discord
from discord.ext import commands

from module import extractor
from module.embed import (
	music_info_embed, preparing_audio_embed, playlist_added_embed, queue_added_embed,
	play_completed_embed, load_error_embed, skip_error_embed, playback_error_embed, alone_leave_embed,
)
from module.logger import get_bot_logger, perf
from module.options import FFMPEG_OPTIONS, app_config
from module.priority import Priority, set_priority
from module.sqlite import get_guild_settings
from module.utils import loading_spinner

logger = get_bot_logger()

T = TypeVar("T")

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
# メタデータキャッシュの最大件数
META_CACHE_MAX_ENTRIES = 256
# 再生速度の範囲 (FFmpeg の atempo が 1段で扱える下限が 0.5)
SPEED_MIN, SPEED_MAX = 0.5, 3.0
# 1回の read() で返す音声の長さ(秒) と Discord へ送る音声のサンプリングレート
FRAME_SECONDS = discord.opus.Encoder.FRAME_LENGTH / 1000
OUTPUT_SAMPLE_RATE = discord.opus.Encoder.SAMPLING_RATE
# 曲の残りがこの秒数 (実時間) を切ったら、次の曲の FFmpeg を起動して最初のフレームまで準備しておく
PRELOAD_LEAD = 15.0
# 次の曲を準備するタイミングを確認する間隔(秒)
PRELOAD_CHECK_INTERVAL = 1.0
# 設定変更でソースを差し替えた後、旧ソースを停止するまでの猶予(秒)。再生スレッドが読み込み中の旧ソースを止めないため
SOURCE_SWAP_GRACE = 0.2
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

async def monitor_loop_lag() -> None:
	"""イベントループ (メインスレッド) の遅延を監視し、しきい値を超えたら警告する。再生の途切れと同時刻かで原因を切り分ける"""
	while True:
		started = time.perf_counter()
		await asyncio.sleep(LOOP_LAG_CHECK_INTERVAL)
		lag = time.perf_counter() - started - LOOP_LAG_CHECK_INTERVAL
		if lag > LOOP_LAG_THRESHOLD:
			logger.warning(f"[STALL] イベントループが {lag * 1000:.0f}ms 遅延")

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
		logger.debug(f"抽出ワーカー {app_config.MAX_WORKER_THREADS} 件の準備が完了しました。")
		perf("ワーカー準備", t)
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

class _TTLCache:
	"""上限件数付きの TTL キャッシュ。期限切れは参照時に、上限を超えた分は古い順に捨てる。ttl が 0 以下なら保存しない"""
	def __init__(self, ttl: float, max_entries: int) -> None:
		self._ttl = ttl
		self._max_entries = max_entries
		self._items: collections.OrderedDict[str, tuple[float, dict]] = collections.OrderedDict()

	def get(self, key: str) -> dict | None:
		"""有効な値を返す。無い・期限切れなら None"""
		item = self._items.get(key)
		if item is None:
			return None
		expires_at, value = item
		if expires_at <= time.monotonic():
			del self._items[key]
			return None
		self._items.move_to_end(key)
		return value

	def set(self, key: str, value: dict) -> None:
		"""値を保存する"""
		if self._ttl <= 0:
			return
		self._items[key] = (time.monotonic() + self._ttl, value)
		self._items.move_to_end(key)
		while len(self._items) > self._max_entries:
			self._items.popitem(last=False)

# メタデータ専用のキャッシュ (ストリームURLのように失効しないデータのみ保持する)
_meta_cache = _TTLCache(app_config.CACHE_TTL, META_CACHE_MAX_ENTRIES)

async def fetch_track_info(query: str, is_fast: bool, priority: Priority = Priority.CURRENT) -> dict:
	"""
	extractor.extract をプロセスプール経由で非同期実行する。返り値は共有されうるため変更しないこと。
	- is_fast=True  (メタデータ): CACHE_TTL秒キャッシュする。メタデータは失効しないため安全。
	- is_fast=False (ストリームURL): キャッシュしない。URL失効による403を避けるため毎回解決する。
	- priority: 抽出を行うワーカーの CPU 優先度
	"""
	t = time.perf_counter()
	if is_fast and (hit := _meta_cache.get(query)) is not None:
		perf("メタ取得(cacheヒット)", t)
		return hit
	logger.debug(f"{'メタデータの新規取得' if is_fast else 'ストリームURLの解決'}: {query}")
	info = await _run_extract(query, is_fast, priority)
	perf("メタ抽出(yt-dlp)" if is_fast else "本抽出(yt-dlp/stream_url)", t)
	if info.pop(extractor.FALLBACK_FLAG, False):
		logger.warning(f"高速設定での抽出に失敗したため予備設定で取得しました: {query}")
	if is_fast:
		_meta_cache.set(query, info)
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

def _stream_expires_in(track: dict) -> float | None:
	"""stream_url の有効期限までの秒数を返す (YouTube などの URL に含まれる expire)。期限を読み取れなければ None"""
	try:
		expire = int(parse_qs(urlsplit(track["stream_url"]).query)["expire"][0])
	except (KeyError, IndexError, TypeError, ValueError):
		return None
	return expire - time.time()

def _stream_lasts(track: dict, expires_in: float) -> bool:
	"""有効期限まで expires_in 秒残っていれば、曲を最後まで再生できるか"""
	return expires_in > track["duration"] + STREAM_EXPIRE_MARGIN

def requeue_track(track: dict) -> dict:
	"""
	ループ・リプレイ用に、表示状態をリセットした track のコピーを返す。
	- stream_url は有効期限が十分に残っていると分かる場合だけ引き継ぎ (再抽出を省く)、それ以外は解決し直させる
	"""
	expires_in = _stream_expires_in(track) if track["stream_url"] else None
	if expires_in is not None and _stream_lasts(track, expires_in):
		return {**track, "fetch_task": None, "wait_msg": None, "t_request": None}
	return {**track, "stream_url": None, "http_headers": {}, "fetch_task": None, "wait_msg": None, "t_request": None}

def play_now(player: "GuildMusicPlayer", vc: discord.VoiceClient, index: int) -> dict:
	"""
	キューの index 番目 (0 始まり) の曲を今すぐ再生させ、その track を返す。再生中・一時停止中に呼ぶこと。
	- 再生中の曲は最初から再生し直すコピーにして、選んだ曲の次に置く
	- 停止すると再生終了コールバック経由で選んだ曲へ進む (current を外しておくため、ループ中でも二重に追加されない)
	"""
	track = player.queue[index]
	del player.queue[index]
	player.queue.appendleft(requeue_track(player.current))
	player.queue.appendleft(track)
	player.current = None
	vc.stop()
	return track

def _drop_expiring_stream(track: dict) -> None:
	"""先読み済みの stream_url が再生中に期限切れになりそうなら破棄して解決し直させる (期限が読めない URL はそのまま使う)"""
	expires_in = _stream_expires_in(track)
	if expires_in is not None and not _stream_lasts(track, expires_in):
		track.update(stream_url=None, http_headers={}, fetch_task=None)

async def _resolve_stream(track: dict, priority: Priority) -> None:
	"""track の stream_url を解決する (MAX_RETRIES 回まで再試行)。最終的に失敗したら例外を送出する"""
	max_retries = app_config.MAX_RETRIES
	for attempt in range(1, max_retries + 1):
		try:
			info = await loading_spinner(
				fetch_track_info(track["url"], False, priority),
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

def ensure_stream(track: dict, priority: Priority = Priority.CURRENT) -> asyncio.Task:
	"""
	track のストリームURL解決タスクを返す。未開始なら priority で開始する (同じ track で多重起動しない)。
	- 既に始まっている解決は、開始時の優先度のまま完了を待つ
	"""
	task = track["fetch_task"]
	if task is None:
		task = spawn(_resolve_stream(track, priority), name=f"resolve:{track['title']}", log_errors=False)
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
	- speed / keep_pitch: 再生速度とピッチ維持の有無。プレイヤーの破棄 (退出・切断・再生終了) で既定値に戻る
	- text_channel: 最後に /p が実行されたテキストチャンネル (自動退出の通知先)
	- alone_task: 聴者不在時の自動退出タイマー (動作中のみ)
	- prefetch_task: 実行中の先読み (同時に1曲まで)
	- advance_lock: 次曲への遷移とソース差し替えを直列化し、二重再生を防ぐ
	- pending_requests: 処理中の /p の件数 (曲の取得に失敗したときに、他の /p を巻き込んで退出しないため)
	- watch_task: 再生中の曲の残り時間を見て、次の曲を準備するタスク
	- preload_target / preload_track / preload_task: 準備した次の曲。target は準備の基になった曲 (キュー先頭、またはループ時の現在曲)、
	  track は実際に再生する track (ループ時は現在曲のコピー)、task は FFmpeg を起動して YTDLSource を返すタスク
	"""
	__slots__ = (
		"guild_id", "queue", "loop", "current", "speed", "keep_pitch",
		"text_channel", "alone_task", "prefetch_task", "advance_lock",
		"pending_requests", "watch_task", "preload_target", "preload_track", "preload_task",
	)
	def __init__(self, guild_id: int) -> None:
		self.guild_id = guild_id
		self.queue: collections.deque[dict] = collections.deque()
		self.loop = False
		self.current: dict | None = None
		self.speed = 1.0
		self.keep_pitch = True
		self.text_channel: discord.abc.Messageable | None = None
		self.alone_task: asyncio.Task | None = None
		self.prefetch_task: asyncio.Task | None = None
		self.advance_lock = asyncio.Lock()
		self.pending_requests = 0
		self.watch_task: asyncio.Task | None = None
		self.preload_target: dict | None = None
		self.preload_track: dict | None = None
		self.preload_task: asyncio.Task | None = None

	def prefetch(self) -> None:
		"""
		キュー先頭 PREFETCH_AHEAD 曲のストリームURLを、先頭から1曲ずつ順に解決する。
		- 次曲 (Priority.NEXT) の解決が終わるまで、それ以降 (Priority.LATER) は始めない
		- 曲の切り替え中 (advance_lock 取得中) は始めない。現在の曲の解決と再生開始を優先し、切り替え後に呼び直される
		"""
		if self.advance_lock.locked() or self.prefetch_task is not None:
			return
		for index, track in enumerate(itertools.islice(self.queue, PREFETCH_AHEAD)):
			if track["stream_url"]:
				_drop_expiring_stream(track)
			# 解決済み・解決中・失敗済み (再生時にスキップされる) の曲は飛ばす
			if track["stream_url"] or track["fetch_task"] is not None:
				continue
			self.prefetch_task = ensure_stream(track, Priority.NEXT if index == 0 else Priority.LATER)
			self.prefetch_task.add_done_callback(self._on_prefetched)
			return

	def _on_prefetched(self, _task: asyncio.Task) -> None:
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
		"""進行中の解決タスク・自動退出タイマー・次の曲の準備を取り消し、全状態を初期化する"""
		tracks = itertools.chain(self.queue, (self.current,) if self.current else ())
		for track in tracks:
			if (task := track["fetch_task"]) is not None and not task.done():
				task.cancel()
		self.cancel_alone_timer()
		if self.watch_task is not None:
			self.watch_task.cancel()
			self.watch_task = None
		self.discard_preload()
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
	_rtp_idle_since.pop(guild_id, None)
	if player := server_music_data.pop(guild_id, None):
		player.cleanup()

# ==========================================
# YTDLSource (FFmpeg AudioSource ラッパー)
# ==========================================
def _build_before_options(http_headers: dict, start: float = 0.0) -> str:
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
	- position: 元音源での次に返すフレームの位置(秒)。設定変更時の再開位置に使う
	- stream_url が無い track では ValueError を送出する
	"""
	def __init__(self, track: dict, volume: float, *, speed: float = 1.0, keep_pitch: bool = True, start: float = 0.0) -> None:
		stream_url = track.get("stream_url")
		if not stream_url:
			raise ValueError(f"ストリームURLが存在しません: {track.get('title', 'Unknown')}")
		super().__init__(
			stream_url,
			before_options=_build_before_options(track.get("http_headers") or {}, start),
			options=_build_options(volume, speed, keep_pitch),
		)
		if (process := getattr(self, "_process", None)) is not None:
			set_priority(Priority.PLAYBACK, process.pid)
		self.data = track
		self.title: str = track.get("title", "Unknown Title")
		self.display_url: str = track.get("url", "")
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

	def read(self) -> bytes:
		"""次の 20ms 分の Opus パケットを返し、元音源での再生位置を進める"""
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
		"""
		read() のタイミングを記録する。送信が途切れるほどの停止は WARNING、AUDIO_STATS_INTERVAL ごとの最大値は DEBUG で出力する。
		- read() が遅い: FFmpeg の出力 (通信) 待ち、または GIL 待ち
		- read() の間隔が空く: 再生スレッドに CPU / GIL が回ってこない、または送信が遅い
		- 予定時刻からの遅れ: 「最初の read() + 20ms × 回数」からの遅れ。discord.py は遅れた分を待たずにまとめて送る
		"""
		read_time = finished - started
		gap = started - self._last_read_end if self._last_read_end is not None else FRAME_SECONDS
		if self._clock_start is None:
			self._clock_start = self._stats_start = started
			self._frames = 0
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
			raise RuntimeError(f"音声を読み込めませんでした ({source._current_error or 'FFmpeg の出力なし'})")
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
async def _notify(coro: Coroutine[Any, Any, T]) -> T | None:
	"""通知メッセージを送信する。送信失敗 (権限不足など) で再生制御を止めないよう、失敗時は None を返す"""
	try:
		return await coro
	except discord.HTTPException as e:
		logger.warning(f"通知メッセージの送信に失敗しました: {e}")
		return None

# ギルドごとに、送信が止まった時刻 (time.perf_counter()) を保持する
_rtp_idle_since: dict[int, float] = {}

def mark_rtp_idle(guild_id: int) -> None:
	"""送信が止まった時刻を記録する。曲の終了時・一時停止時に呼ぶ"""
	_rtp_idle_since[guild_id] = time.perf_counter()

async def advance_rtp_timestamp(vc: discord.VoiceClient) -> None:
	"""
	送信が止まっていた時間の分だけ RTP タイムスタンプを進める。送信を再開する直前に呼ぶ。
	discord.py はパケットごとに 20ms 分しか進めないため、止まった後の最初のパケットを受信側は「止まっていた時間だけ遅れて届いた」と判断し、
	ジッターバッファを伸ばして (遅く聞こえる) から縮める (速く聞こえる)。実時間に合わせて進めると無音区間として扱われる
	- 止めるときに一度に送られた無音パケット (SILENCE_SAMPLES) の分は、すでにタイムスタンプが進んでいるため差し引く
	- 無音パケットの分の時間がまだ経っていなければ (準備済みの次曲をすぐ流す場合)、経つまで待つ。待たずに送ると、先に届きすぎた分を受信側が早送りで消化する
	"""
	since = _rtp_idle_since.pop(vc.guild.id, None)
	if since is None:
		return
	elapsed = round((time.perf_counter() - since) * RTP_CLOCK_RATE) - SILENCE_SAMPLES
	if elapsed < 0:
		await asyncio.sleep(-elapsed / RTP_CLOCK_RATE)
		return
	vc.timestamp = (vc.timestamp + elapsed) % RTP_TIMESTAMP_MOD

def _make_after_callback(ctx: commands.Context, loop: asyncio.AbstractEventLoop):
	"""再生終了時 (再生スレッドから呼ばれる) に次曲の再生をイベントループへ投げるコールバックを返す"""
	guild_id = ctx.guild.id
	def _after_playing(error: Exception | None) -> None:
		mark_rtp_idle(guild_id)
		if error:
			logger.error(f"再生時エラー (ギルド {guild_id}): {error}")
		# 終了処理でループが閉じた後に呼ばれた場合は何もしない
		if loop.is_closed():
			return
		asyncio.run_coroutine_threadsafe(play_next_song(ctx), loop)
	return _after_playing

async def play_next_song(ctx: commands.Context) -> None:
	"""
	キューから次のトラックを取り出して再生し、終わったら先読みを再開する。
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
	if server_music_data.get(guild.id) is player:
		player.prefetch()

def _is_stale(guild: discord.Guild, player: GuildMusicPlayer, vc: discord.VoiceClient) -> bool:
	"""待機中にプレイヤーが破棄された、または VC が切断されたかを返す。切断時はプレイヤーも破棄する"""
	if server_music_data.get(guild.id) is not player:
		return True
	if not vc.is_connected():
		discard_player(guild.id)
		return True
	return False

def _is_active(vc: discord.VoiceProtocol | None) -> bool:
	"""VC で再生中または一時停止中か"""
	return bool(vc and (vc.is_playing() or vc.is_paused()))

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
	duration = track.get("duration")
	if not duration:
		return None
	return max(0.0, (duration - source.position) / source.speed)

async def _open_preload(player: GuildMusicPlayer, track: dict) -> YTDLSource:
	"""track のストリームURLを (無ければ) 解決し、現在の設定で FFmpeg を起動して最初のフレームまで読んだソースを返す。FFmpeg の起動に失敗したら URL を捨てる"""
	if track["stream_url"]:
		_drop_expiring_stream(track)
	if not track["stream_url"]:
		await ensure_stream(track, Priority.NEXT)
	volume = (await get_guild_settings(player.guild_id)).volume
	try:
		return await asyncio.to_thread(_open_source, track, volume, player.speed, player.keep_pitch)
	except Exception:
		# URL が失効・拒否されていた可能性があるため、再生時には取得し直させる
		track.update(stream_url=None, http_headers={}, fetch_task=None)
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
	- 一時停止中は準備しない
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
		# 自身ではなく準備タスクだけが取り消された場合 (cleanup 経由) は、準備なしとして扱う
		if (current_task := asyncio.current_task()) is not None and current_task.cancelling():
			raise
		return None
	except Exception as e:
		logger.debug(f"次の曲の準備に失敗したため開き直します: {track['title']} ({e!r})")
		return None
	if (source.volume, source.speed, source.keep_pitch) != (volume, player.speed, player.keep_pitch):
		await asyncio.to_thread(source.cleanup)
		return None
	logger.debug(f"準備済みのソースで再生します: {track['title']}")
	return source

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
		if _is_active(vc):
			return
		if player.loop and player.current:
			player.queue.append(_loop_copy(player))
		if not player.queue:
			discard_player(guild.id)
			await vc.disconnect()
			await _notify(play_completed_embed(ctx))
			return
		track = player.queue.popleft()
		player.current = track
		wait_msg: discord.Message | None = track["wait_msg"]
		# 準備がまだ URL の解決中なら、従来どおり準備中と表示してから待つ
		preload_task = player.preload_task
		if player.preload_track is track and preload_task is not None and not preload_task.done() and not track["stream_url"] and wait_msg is None:
			wait_msg = await _notify(preparing_audio_embed(ctx))
		# 曲の終わり際に準備しておいたソースがあれば、FFmpeg の起動を待たずに再生する
		source = await _claim_preloaded(player, track, (await get_guild_settings(guild.id)).volume)
		if source is None:
			if track["stream_url"]:
				_drop_expiring_stream(track)
			# 先読み・使い回しで取得済みの URL か (再生できなければ1回だけ取得し直す)
			resolved_earlier = bool(track["stream_url"])
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
				perf("stream_url待ち", t_wait)
			volume = (await get_guild_settings(guild.id)).volume
			if _is_stale(guild, player, vc):
				return
			try:
				t_ff = time.perf_counter()
				# FFmpeg の起動 (CreateProcess) と最初のフレームの受信はブロッキングで、高負荷時は数十秒かかりうるためイベントループ外で行う
				source = await asyncio.to_thread(_open_source, track, volume, player.speed, player.keep_pitch)
				perf("FFmpeg起動(初回フレームまで)", t_ff)
			except Exception as e:
				player.current = None
				if resolved_earlier:
					# 取得済みの URL が失効・拒否されていた可能性があるため、取得し直して同じ曲をもう一度試す
					logger.warning(f"取得済みのストリームURLで再生できなかったため取得し直します: {track['title']} ({e})")
					track.update(stream_url=None, http_headers={}, fetch_task=None, wait_msg=wait_msg)
					player.queue.appendleft(track)
					continue
				logger.error(f"再生ソース生成エラー (ギルド {guild.id}): {e}")
				await _notify(playback_error_embed(ctx, track["title"], edit_msg=wait_msg))
				continue
		if _is_stale(guild, player, vc):
			await asyncio.to_thread(source.cleanup)
			return
		await advance_rtp_timestamp(vc)
		if _is_stale(guild, player, vc):
			await asyncio.to_thread(source.cleanup)
			return
		try:
			vc.play(source, after=_make_after_callback(ctx, asyncio.get_running_loop()))
		except Exception as e:
			logger.error(f"再生開始エラー (ギルド {guild.id}): {e}")
			await asyncio.to_thread(source.cleanup)
			player.current = None
			await _notify(playback_error_embed(ctx, track["title"], edit_msg=wait_msg))
			continue
		_start_preload_watch(guild, player)
		if track["t_request"] is not None:
			perf("★総計 コマンド→再生開始", track["t_request"])
		await music_info_embed(ctx, source, len(player.queue), wait_msg)
		return

async def _cleanup_later(source: discord.AudioSource) -> None:
	"""差し替え済みの旧ソースを、再生スレッドが読み終える猶予をおいてから停止する"""
	await asyncio.sleep(SOURCE_SWAP_GRACE)
	await asyncio.to_thread(source.cleanup)

async def apply_audio_settings(guild: discord.Guild, player: GuildMusicPlayer) -> None:
	"""
	再生中の曲に音量 (ギルド設定) と player の速度設定を反映する。/vol・/speed から呼ぶ。
	- 現在位置から新しい設定で FFmpeg を起動し直し、起動待ちの間に進んだ分を読み飛ばしてから差し替える (準備中は旧ソースが鳴り続ける)
	- 曲の切り替え中なら、切り替えが終わるのを待ってから反映する
	- 何も再生していなければ何もしない (次の曲から反映される)
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
	volume = (await get_guild_settings(guild.id)).volume
	vc = guild.voice_client
	old = vc.source if _is_active(vc) else None
	if not isinstance(old, YTDLSource) or (old.volume, old.speed, old.keep_pitch) == (volume, player.speed, player.keep_pitch):
		return
	t = time.perf_counter()
	new = await asyncio.to_thread(_open_synced_source, old, volume, player.speed, player.keep_pitch)
	perf("設定変更(FFmpeg再起動)", t)
	# 準備中に曲が終わった・切り替わった・切断された場合は破棄する
	if _is_stale(guild, player, vc) or vc.source is not old or not _is_active(vc):
		await asyncio.to_thread(new.cleanup)
		return
	was_paused = vc.is_paused()
	# 差し替えは after コールバックを呼ばないため、次の曲へは進まない
	vc.source = new
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
	# /leave と同じく、先にプレイヤーを破棄して停止時の次曲処理を動かさない
	discard_player(guild.id)
	vc.stop()
	await vc.disconnect()
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

def _is_idle(player: GuildMusicPlayer, vc: discord.VoiceProtocol | None) -> bool:
	"""再生中・再生準備中の曲もキューも無い状態か"""
	return player.current is None and not player.queue and not _is_active(vc)

async def _leave_if_unused(guild: discord.Guild, player: GuildMusicPlayer, voice_task: asyncio.Task | None) -> None:
	"""
	曲の取得に失敗したとき、このリクエストで接続 (移動) した VC で何も再生しなければ退出してプレイヤーを破棄する。
	- 接続処理の途中なら完了を待ってから判定する
	- 別のリクエストで再生・曲の切り替えが始まっている、または他の /p が処理中なら何もしない
	"""
	if voice_task is None:
		return
	await _await_quietly(voice_task)
	vc = guild.voice_client
	if vc is None or server_music_data.get(guild.id) is not player:
		return
	if player.pending_requests > 1 or player.advance_lock.locked() or not _is_idle(player, vc):
		return
	discard_player(guild.id)
	await vc.disconnect()

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
	URLまたは検索クエリを解析してキューに追加し、アイドル状態なら再生を開始する。
	- 応答の保留(defer_task)・待機メッセージ送信・情報取得・ギルド設定読込・VC接続(voice_task)を並行して進める
	- メッセージ送信は defer_task の完了後に行う
	- プレイリスト: playlist_limit 件まで追加 (ストリームURLは先読みで解決)
	- 単曲URL: ストリームURLまで一度に解決する
	- 検索クエリ: ytsearch1: でメタデータを取得し、ストリームURLは再生時または先読みで解決する
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
	early_fetch: asyncio.Task | None = None
	try:
		if is_single_url:
			info = await loading_spinner(fetch_track_info(query, False), "音源の取得")
		else:
			info = await loading_spinner(fetch_track_info(query if is_url else SEARCH_PREFIX + query, True), "メタデータ検索")
		is_playlist_result = "entries" in info and is_url
		entries = info["entries"] if "entries" in info else [info]

		settings = await settings_task
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
		# 再生を始める見込みなら、1 曲目のストリームURLの解決を今始めておく (VC 接続・応答の保留・待機メッセージの送信を待つと、その分だけ再生開始が遅れるため)。
		# 再生処理 (_advance) は同じ解決タスクを待つ
		if is_idle and not tracks[0]["stream_url"]:
			early_fetch = ensure_stream(tracks[0])
		if voice_task is not None:
			await voice_task
	except Exception as e:
		logger.error(f"play_music 解析エラー: {e}")
		if early_fetch is not None:
			early_fetch.cancel()
		await _await_quietly(defer_task)
		await _notify(load_error_embed(ctx, e, edit_msg=await _await_quietly(wait_task)))
		await _leave_if_unused(ctx.guild, player, voice_task)
		return

	await _await_quietly(defer_task)
	wait_msg: discord.Message | None = await _await_quietly(wait_task)
	# 解析待ちの間に退出 (/leave・切断・自動退出) でプレイヤーが破棄されていれば、曲を追加せずに知らせる
	if server_music_data.get(guild_id) is not player:
		if early_fetch is not None:
			early_fetch.cancel()
		await _notify(load_error_embed(ctx, ValueError("曲の準備中に退出したため、追加できませんでした。"), edit_msg=wait_msg))
		return
	# 解析待ちの間に別のリクエストで再生が始まっていれば、キュー追加として扱う
	start_playback = is_idle and _is_idle(player, ctx.guild.voice_client)
	if start_playback:
		tracks[0]["t_request"] = t_request
		if not is_playlist_result:
			tracks[0]["wait_msg"] = wait_msg
	player.queue.extend(tracks)

	# 追加完了通知
	if is_playlist_result:
		await _notify(playlist_added_embed(ctx, info, len(tracks), edit_msg=wait_msg))
	elif not start_playback:
		await _notify(queue_added_embed(ctx, tracks[0], len(player.queue), edit_msg=wait_msg))
	if start_playback or not _is_active(ctx.guild.voice_client):
		# 何も再生されていなければ再生処理を起動する (多重起動しても advance_lock と再生中判定で1つに収束する)。
		# 先頭の曲は再生処理が通常の優先度で解決し、先読みは再生開始後に始まる
		spawn(play_next_song(ctx), name=f"play_next:{guild_id}")
	else:
		player.prefetch()
