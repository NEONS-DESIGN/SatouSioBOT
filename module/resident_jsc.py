"""
YouTube の JS チャレンジ (n / sig) を、抽出ワーカーに常駐させた Deno で解く yt-dlp のプロバイダ。
yt-dlp 標準の Deno プロバイダは解読のたびに Deno を起動し、解読スクリプトと前処理済みプレイヤー (約 4MB) を渡し直す。
常駐させてプレイヤーを保持すると、2 回目以降は数 ms で済む。
- 抽出ワーカーの子プロセスでのみ import する (yt-dlp を読み込むため)。import するとプロバイダが登録される
- 失敗時は JsChallengeProviderError を送出し、yt-dlp が標準の Deno プロバイダで解き直す。Deno は次の解読で起動し直す
"""
import collections
import contextlib
import hashlib
import json
import pathlib
import queue
import subprocess
import threading
import time
import urllib.parse
from typing import Any

from yt_dlp.extractor.youtube.jsc._builtin.deno import DenoJCP
from yt_dlp.extractor.youtube.jsc._builtin.ejs import ScriptVariant
from yt_dlp.extractor.youtube.jsc.provider import (
	JsChallengeProvider,
	JsChallengeProviderError,
	JsChallengeProviderRejectedRequest,
	JsChallengeProviderResponse,
	JsChallengeRequest,
	JsChallengeResponse,
	JsChallengeType,
	NChallengeOutput,
	SigChallengeOutput,
	register_preference,
	register_provider,
)
from yt_dlp.utils import Popen

from module.extractor import PLAYER_CACHE_KEY_PREFIX, player_cache_files
from module.priority import Priority, set_priority

# Deno 側の処理 (このファイルと同じフォルダに置く)
SERVER_SCRIPT = pathlib.Path(__file__).with_name("resident_jsc.js")
# 応答を待つ上限秒数。超えたら Deno を終了して標準のプロバイダに任せる (スクリプトの評価・プレイヤーの読み込みを含む要求 / 保持済みのプレイヤーで解く要求)
SOLVE_TIMEOUT_SECONDS = 30
CACHED_SOLVE_TIMEOUT_SECONDS = 10
# 強制終了の後、読み込みスレッドが終了を知らせるまで待つ余裕の秒数
READER_GRACE_SECONDS = 5
# 通信の失敗がこの回数続いたら、最後の失敗から FAILURE_RESET_SECONDS の間は常駐 Deno を使わない (標準の Deno プロバイダで解く)
MAX_CONSECUTIVE_FAILURES = 3
FAILURE_RESET_SECONDS = 600
# エラーの説明に添える Deno の標準エラーの行数と文字数
STDERR_TAIL_LINES = 20
STDERR_MESSAGE_LIMIT = 500
# 標準の Deno プロバイダ (優先度 1000。継承しているためこのプロバイダにも加算される) より先に使わせるための加点
PREFERENCE_BONUS = 1000
# 常駐 Deno の起動オプション。プロンプト・外部取得・設定ファイル・npm を使わない (標準の Deno プロバイダの npm 無し版と同じ方針)。
# --optimize-for-size は常駐中のメモリを抑える (解読の速さは変わらない)
_DENO_OPTIONS = (
	"--no-prompt", "--no-remote", "--no-lock", "--node-modules-dir=none", "--no-config", "--no-npm", "--cached-only",
	"--v8-flags=--optimize-for-size",
)

class _SolverError(JsChallengeProviderError):
	"""Deno は正常に応答したが、解読に失敗した (Deno を起動し直す必要はない)"""

class _ResidentDeno:
	"""
	ワーカーに 1 つだけ常駐させる Deno プロセス。1 行 1 JSON で要求を送り、応答を 1 行受け取る。
	- 最初の要求で起動し、解読スクリプトを評価させる (スクリプトが変われば起動し直す)
	- 1 プロセスで扱うプレイヤーは 1 版だけにする (プレイヤーのコードが書き換えたグローバルな状態を別の版に持ち込まないため)
	- 標準出力・標準エラーはそれぞれ専用スレッドが読む。標準出力の終了 (EOF) は None で知らせ、標準エラーは末尾だけ残す
	- 通信の失敗が MAX_CONSECUTIVE_FAILURES 回続いたら、しばらく使わない (毎回タイムアウトまで待たせないため)
	"""
	def __init__(self) -> None:
		self._lock = threading.Lock()
		self._process: subprocess.Popen | None = None
		self._lines: queue.Queue[bytes | None] | None = None
		self._stderr: collections.deque[str] = collections.deque(maxlen=STDERR_TAIL_LINES)
		self._script_hash: str | None = None
		self._has_player = False
		self._failures = 0
		self._last_failure = 0.0

	@property
	def pid(self) -> int | None:
		"""稼働中の Deno のプロセス ID (起動していなければ None)"""
		process = self._process
		return process.pid if process is not None and process.poll() is None else None

	@property
	def available(self) -> bool:
		"""通信の失敗が続いていない、または最後の失敗から FAILURE_RESET_SECONDS たっていれば True"""
		if self._failures >= MAX_CONSECUTIVE_FAILURES and time.monotonic() - self._last_failure >= FAILURE_RESET_SECONDS:
			self._failures = 0
		return self._failures < MAX_CONSECUTIVE_FAILURES

	def request(
		self, command: list[str], env: dict[str, str], script: str, message: dict[str, Any], *, new_player: bool = False,
	) -> dict[str, Any]:
		"""
		solve の message を送って応答 (result / missing) を返す。必要なら起動して script を評価させる。
		- new_player: message でプレイヤーを渡す。別の版のプレイヤーを読み込み済みなら、起動し直してから送る
		- 解読の失敗は _SolverError (Deno は残す)。通信の失敗 (異常終了・タイムアウト・想定外の応答) では Deno を終了し、
		  JsChallengeProviderError を送出する (次の要求で起動し直す)
		"""
		script_hash = hashlib.sha256(script.encode()).hexdigest()
		with self._lock:
			try:
				if self.pid is None or self._script_hash != script_hash or (new_player and self._has_player):
					self._start(command, env)
					try:
						self._exchange({"type": "init", "code": script}, ("ok",), SOLVE_TIMEOUT_SECONDS)
					except _SolverError as e:
						# 評価できないスクリプトは何度送っても同じため、通信の失敗と同じく数えて止める
						raise self._failure(str(e)) from e
					self._script_hash = script_hash
				timeout = SOLVE_TIMEOUT_SECONDS if new_player else CACHED_SOLVE_TIMEOUT_SECONDS
				response = self._exchange(message, ("result", "missing"), timeout)
			except _SolverError:
				raise
			except JsChallengeProviderError:
				self._failures += 1
				self._last_failure = time.monotonic()
				self._stop()
				raise
			self._failures = 0
			if new_player:
				self._has_player = True
			return response

	def _start(self, command: list[str], env: dict[str, str]) -> None:
		"""Deno を起動し、標準出力・標準エラーを読むスレッドを開始する"""
		self._stop()
		try:
			process = Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
		except OSError as e:
			raise JsChallengeProviderError(f"常駐 Deno を起動できませんでした: {e}") from e
		lines: queue.Queue[bytes | None] = queue.Queue()
		self._stderr.clear()
		threading.Thread(target=self._read_lines, args=(process, lines), name="resident-deno-stdout", daemon=True).start()
		threading.Thread(target=self._read_stderr, args=(process,), name="resident-deno-stderr", daemon=True).start()
		self._process, self._lines = process, lines

	@staticmethod
	def _read_lines(process: subprocess.Popen, lines: queue.Queue) -> None:
		"""Deno の標準出力を 1 行ずつ lines に入れる (読み込みスレッドで実行)。終了時は必ず None を入れる"""
		try:
			for line in process.stdout:
				lines.put(line)
		except (OSError, ValueError):
			pass
		finally:
			lines.put(None)

	def _read_stderr(self, process: subprocess.Popen) -> None:
		"""Deno の標準エラーを読み、末尾の STDERR_TAIL_LINES 行だけ残す (エラーの説明に添える)"""
		try:
			for line in process.stderr:
				self._stderr.append(line.decode(errors="replace").rstrip())
		except (OSError, ValueError):
			pass

	def _failure(self, reason: str) -> JsChallengeProviderError:
		"""通信の失敗を表す例外を作る (Deno の標準エラーの末尾を添える)"""
		# 読み込みスレッドが append している最中に反復すると "deque mutated during iteration" になるため、複製を連結する
		if stderr := " / ".join(line for line in self._stderr.copy() if line):
			reason = f"{reason} (stderr: {stderr[-STDERR_MESSAGE_LIMIT:]})"
		return JsChallengeProviderError(reason)

	def _exchange(self, message: dict[str, Any], expected: tuple[str, ...], timeout: float) -> dict[str, Any]:
		"""
		1 件送って応答を 1 件受け取る。応答の type が expected のどれでもなければ通信の失敗として扱う (要求と応答の対応がずれるのを防ぐ)。
		timeout 秒を超えたら Deno を強制終了して失敗にする
		"""
		process, lines = self._process, self._lines
		timed_out = threading.Event()
		def kill_on_timeout() -> None:
			timed_out.set()
			process.kill()
		# 書き込み (4MB のプレイヤーはパイプの容量を超える) と読み込みのどちらで止まっても、強制終了で抜けられる
		watchdog = threading.Timer(timeout, kill_on_timeout)
		watchdog.daemon = True
		watchdog.start()
		try:
			process.stdin.write(json.dumps(message).encode() + b"\n")
			process.stdin.flush()
			line = lines.get(timeout=timeout + READER_GRACE_SECONDS)
		except (OSError, ValueError) as e:
			if timed_out.is_set():
				raise self._failure(f"常駐 Deno が {timeout:g} 秒以内に応答しませんでした") from e
			raise self._failure(f"常駐 Deno への送信に失敗しました: {e}") from e
		except queue.Empty as e:
			raise self._failure("常駐 Deno の出力を読めなくなりました") from e
		finally:
			watchdog.cancel()
		if line is None:
			if timed_out.is_set():
				raise self._failure(f"常駐 Deno が {timeout:g} 秒以内に応答しませんでした")
			try:
				returncode = process.wait(timeout=READER_GRACE_SECONDS)
			except subprocess.TimeoutExpired:
				returncode = None
			raise self._failure(f"常駐 Deno が応答せずに終了しました (returncode: {returncode})")
		try:
			response = json.loads(line)
		except ValueError as e:
			raise self._failure(f"常駐 Deno の応答を読めませんでした: {line[:200]!r}") from e
		response_type = response.get("type") if isinstance(response, dict) else None
		if response_type == "error":
			raise _SolverError(f"常駐 Deno での解読に失敗しました: {response.get('error')}")
		if response_type not in expected:
			raise self._failure(f"常駐 Deno から想定外の応答がありました: {line[:200]!r}")
		return response

	def _stop(self) -> None:
		"""Deno を終了する (起動していなければ何もしない)"""
		process, self._process, self._lines, self._script_hash = self._process, None, None, None
		self._has_player = False
		if process is None:
			return
		try:
			process.kill()
			process.wait(timeout=SOLVE_TIMEOUT_SECONDS)
		except (OSError, subprocess.TimeoutExpired):
			pass
		for pipe in (process.stdin, process.stdout, process.stderr):
			with contextlib.suppress(OSError):
				pipe.close()

_server = _ResidentDeno()

def apply_priority(priority: Priority) -> None:
	"""常駐 Deno の CPU 優先度を、ワーカーの現在の抽出の優先度に合わせる (起動していなければ何もしない)"""
	if (pid := _server.pid) is not None:
		set_priority(priority, pid)

@register_provider
class ResidentDenoJCP(DenoJCP):
	"""常駐 Deno で解く JS チャレンジプロバイダ。Deno の検出・解読スクリプトの取得と検証は標準の Deno プロバイダのものを使う"""
	PROVIDER_NAME = "resident-deno"

	def close(self) -> None:
		"""常駐 Deno はワーカー内の全 YoutubeDL で共有するため、YoutubeDL の終了では止めない"""

	def is_available(self) -> bool:
		"""Deno が使え、常駐 Deno の通信の失敗が続いていなければ True"""
		return _server.available and super().is_available()

	def _real_bulk_solve(self, requests: list[JsChallengeRequest]):
		"""プレイヤーごとにまとめて常駐 Deno で解き、要求ごとの応答を返す"""
		if self._lib_script.variant == ScriptVariant.DENO_NPM:
			# npm から読み込む版は import 文を含み、実行時に評価できない
			raise JsChallengeProviderRejectedRequest("npm 版の解読スクリプトは常駐 Deno では評価できません")
		grouped: dict[str, list[JsChallengeRequest]] = collections.defaultdict(list)
		for request in requests:
			grouped[request.input.player_url].append(request)
		for player_url, grouped_requests in grouped.items():
			output = self._solve(player_url, grouped_requests)
			responses = output.get("responses")
			if not isinstance(responses, list) or len(responses) != len(grouped_requests):
				raise JsChallengeProviderError(f"常駐 Deno の応答の件数が要求と一致しません: {str(output)[:200]}")
			for request, response_data in zip(grouped_requests, responses, strict=True):
				data = response_data.get("data") if isinstance(response_data, dict) else None
				if not isinstance(data, dict) or response_data.get("type") != "result":
					error = response_data.get("error") if isinstance(response_data, dict) else response_data
					yield JsChallengeProviderResponse(request, None, JsChallengeProviderError(f"常駐 Deno での解読に失敗しました: {error}"))
					continue
				yield JsChallengeProviderResponse(request, JsChallengeResponse(request.type, (
					NChallengeOutput(data) if request.type is JsChallengeType.N else SigChallengeOutput(data)
				)))

	def _solve(self, player_url: str, requests: list[JsChallengeRequest]) -> dict[str, Any]:
		"""
		1 つのプレイヤーの要求を解き、jsc() の出力を返す。
		まずプレイヤーを送らずに解かせ、Deno が保持していなければ、前処理済みプレイヤーのキャッシュか取得したプレイヤーを送る
		"""
		message = {
			"type": "solve",
			"player_url": player_url,
			"requests": [{"type": request.type.value, "challenges": request.input.challenges} for request in requests],
		}
		output = self._request(message)
		if output["type"] != "missing":
			return output
		cache_key = f"{PLAYER_CACHE_KEY_PREFIX}{player_url}"
		if self._ENABLE_PREPROCESSED_PLAYER_CACHE and (preprocessed := self.ie.cache.load(self._CACHE_SECTION, cache_key)):
			return self._request({**message, "preprocessed_player": preprocessed}, new_player=True)
		video_id = next((request.video_id for request in requests), None)
		player = self._get_player(video_id, player_url)
		output = self._request(
			{**message, "player": player, "return_preprocessed": self._ENABLE_PREPROCESSED_PLAYER_CACHE}, new_player=True)
		if preprocessed := output.pop("preprocessed_player", None):
			self.ie.cache.store(self._CACHE_SECTION, cache_key, preprocessed)
		return output

	def _request(self, message: dict[str, Any], *, new_player: bool = False) -> dict[str, Any]:
		"""常駐 Deno に要求を送る (起動していなければ起動して解読スクリプトを評価させる)"""
		command = [self.runtime_info.path, "run", *_DENO_OPTIONS, str(SERVER_SCRIPT)]
		# core の jsc を明示的にグローバルへ置く (宣言が var でなくなっても init で参照できるように)
		script = f"{self._lib_script.code}\nObject.assign(globalThis, lib);\n{self._core_script.code}\nglobalThis.jsc = jsc;\n"
		# 標準エラーをエラーの説明に添えるため、色付けの制御文字を出させない
		env = {**self._get_env_options(), "NO_COLOR": "1"}
		return _server.request(command, env, script, message, new_player=new_player)

def _latest_cached_player_url(ydl: Any) -> str | None:
	"""yt-dlp のキャッシュにある前処理済みプレイヤーのうち、最も新しいもののプレイヤー URL を返す (無ければ None)"""
	files = player_cache_files(ydl)
	if not files:
		return None
	# ファイル名は、キーを URL エンコードして % を , に置き換えたもの
	return urllib.parse.unquote(files[0].stem.replace(",", "%"))[len(PLAYER_CACHE_KEY_PREFIX):]

def warm_up(ydl: Any) -> bool:
	"""
	常駐 Deno を起動し、キャッシュにある最新のプレイヤーを読み込ませておく (各ワーカーの最初の解読の待ちを無くす)。
	プレイヤーが更新されていれば、最初の解読で新しい版を読み込ませるだけなので、失敗しても抽出には影響しない。成功時 True
	"""
	try:
		ie = ydl.get_info_extractor("Youtube")
		ie.initialize()
		provider = next(
			(provider for provider in ie._jsc_director.providers.values() if isinstance(provider, ResidentDenoJCP)), None)
		player_url = _latest_cached_player_url(ydl)
		if provider is None or player_url is None or not provider.is_available():
			return False
		provider._solve(player_url, [])
	except Exception:
		return False
	return True

@register_preference(ResidentDenoJCP)
def _preference(provider: JsChallengeProvider, requests: list[JsChallengeRequest]) -> int:
	"""常駐 Deno のプロバイダを標準の Deno プロバイダより優先する"""
	return PREFERENCE_BONUS
