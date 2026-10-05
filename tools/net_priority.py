"""
SatouSioBOT の通信を OS 側で優先させる設定を切り替えるツール (Windows 専用・要管理者権限)。

使い方:
	python tools/net_priority.py on      優先設定を有効にする
	python tools/net_priority.py off     有効にする前の状態に戻す
	python tools/net_priority.py status  現在の設定を表示する
	python tools/net_priority.py check   Bot の通信に DSCP が付いているかを実際のパケットで確かめる
	引数なしで起動するとメニューを表示する。

有効にすると次を行う。
	- Bot 本体 (python) と FFmpeg の通信に QoS ポリシーで DSCP を付ける
	- ドメイン不参加の PC でも DSCP が付くよう Tcpip\\QoS の "Do not use NLA" を設定する
	- Windows Update などの配信の最適化 (Delivery Optimization) が使う帯域を制限する
DSCP はルーター側が対応していれば優先されるが、配信の最適化の帯域制限は PC の中で完結するため確実に効く。
標準ライブラリのみで動くため、venv が無くても実行できる。
"""

import argparse
import ctypes
import ipaddress
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import winreg
from collections import Counter
from datetime import datetime
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent
VENV_CFG = BOT_DIR / "venv" / "pyvenv.cfg"
STATE_FILE = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "SatouSioBOT" / "net_priority.json"
STATE_VERSION = 1

POLICY_PREFIX = "SatouSioBOT-"
# EF (Expedited Forwarding): 音声通話向けの最優先クラス
DSCP_BOT = 46
# AF41: 動画・音声ストリーミング向けのクラス
DSCP_FFMPEG = 34

QOS_REG_KEY = r"SYSTEM\CurrentControlSet\Services\Tcpip\QoS"
NLA_VALUE_NAME = "Do not use NLA"
NLA_VALUE = "1"

DO_REG_KEY = r"SOFTWARE\Policies\Microsoft\Windows\DeliveryOptimization"
# Get-DOConfig の項目名と表示名 (サービスが実際に使っている値)
DO_EFFECTIVE_FIELDS = {
	"DownBackLimitPct": "裏のダウンロードの上限",
	"DownloadForegroundLimitPct": "手動のダウンロードの上限",
}
# 回線の空き帯域に対する上限 (%)。0 は制限なし (OS の自動調整)
DO_LIMITS = {
	"DOPercentageMaxBackgroundBandwidth": 10,
	"DOPercentageMaxForegroundBandwidth": 50,
}

PS_TIMEOUT_SEC = 120
CHECK_SECONDS = 5
CAPTURE_BYTES = 96

PROTO_TCP = 6
PROTO_UDP = 17
ETHERTYPE_VLAN = 0x8100
ETHERTYPE_IPV4 = 0x0800
ETHERTYPE_IPV6 = 0x86DD
ETHERTYPE_OFFSET = 12
IPV4_MIN_HEADER_LEN = 20
VLAN_TAG_LEN = 4
IPV6_HEADER_LEN = 40
PCAPNG_SHB = 0x0A0D0D0A
PCAPNG_EPB = 6
PCAPNG_BOM_LE = b"\x4d\x3c\x2b\x1a"
PCAPNG_EPB_DATA_OFFSET = 28
PCAPNG_EPB_CAPLEN_OFFSET = 20
PCAPNG_MIN_BLOCK_LEN = 12

REG_VIEW = winreg.KEY_WOW64_64KEY
# レジストリの型ごとに winreg が返す Python の型
REG_PY_TYPES = {winreg.REG_SZ: str, winreg.REG_DWORD: int}
# PowerShell が単一引用符として扱う文字 (' と U+2018 から U+201B)
PS_SINGLE_QUOTES = ("'", "‘", "’", "‚", "‛")


class ToolError(Exception):
	"""利用者に理由を伝えて処理を止めるための例外。"""


# ---------------------------------------------------------------------------
# 共通
# ---------------------------------------------------------------------------

def _decode(raw: bytes) -> str:
	"""外部コマンドの出力を UTF-8 優先で、だめなら cp932 で文字列にする。"""
	try:
		return raw.decode("utf-8")
	except UnicodeDecodeError:
		return raw.decode("cp932", errors="replace")


def _ps_quote(text: str) -> str:
	"""PowerShell の単一引用符文字列にする。(PowerShell は ‘ ’ ‚ ‛ も単一引用符として扱うため、それらも重ねる)"""
	for quote in PS_SINGLE_QUOTES:
		text = text.replace(quote, quote * 2)
	return "'" + text + "'"


def run_ps(script: str) -> str:
	"""PowerShell でスクリプトを実行して標準出力を返す。失敗時は ToolError。"""
	command = (
		"[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
		"$ErrorActionPreference = 'Stop'; "
		"$ProgressPreference = 'SilentlyContinue'; "
		+ script
	)
	try:
		result = subprocess.run(
			["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
			capture_output=True,
			timeout=PS_TIMEOUT_SEC,
		)
	except FileNotFoundError as e:
		raise ToolError("PowerShell が見つかりません。") from e
	except subprocess.TimeoutExpired as e:
		raise ToolError(f"PowerShell の処理が {PS_TIMEOUT_SEC} 秒で終わりませんでした。") from e
	out = _decode(result.stdout)
	if result.returncode != 0:
		detail = _decode(result.stderr).strip() or out.strip()
		raise ToolError(f"PowerShell の実行に失敗しました: {detail}")
	return out


def run_ps_json(script: str) -> list[dict]:
	"""PowerShell の結果を JSON 配列で受け取る。script はオブジェクトの列を出力すること。"""
	out = run_ps(f"ConvertTo-Json -Compress -Depth 3 -InputObject @({script})").strip()
	if not out:
		return []
	try:
		data = json.loads(out)
	except ValueError as e:
		raise ToolError(f"PowerShell の出力を読み取れませんでした: {out[:200]}") from e
	return data if isinstance(data, list) else [data]


def run_cmd(args: list[str]) -> tuple[int, str]:
	"""外部コマンドを実行して (終了コード, 標準出力+標準エラー) を返す。"""
	try:
		result = subprocess.run(args, capture_output=True, timeout=PS_TIMEOUT_SEC)
	except FileNotFoundError as e:
		raise ToolError(f"{args[0]} が見つかりません。") from e
	except subprocess.TimeoutExpired as e:
		raise ToolError(f"{args[0]} の処理が {PS_TIMEOUT_SEC} 秒で終わりませんでした。") from e
	return result.returncode, (_decode(result.stdout) + _decode(result.stderr)).strip()


def is_admin() -> bool:
	"""管理者権限で動いているか。"""
	try:
		return bool(ctypes.windll.shell32.IsUserAnAdmin())
	except (AttributeError, OSError):
		return False


def require_admin() -> None:
	"""管理者権限が無ければ ToolError。"""
	if not is_admin():
		raise ToolError("管理者権限が必要です。net_priority.bat から実行するか、管理者として起動してください。")


# ---------------------------------------------------------------------------
# 対象プログラムの特定
# ---------------------------------------------------------------------------

def bot_python_path() -> Path:
	"""
	Bot の通信を行う python.exe の実体を返す。
	venv の Scripts\\python.exe は本体を子プロセスとして起動する中継役で、通信するのは pyvenv.cfg の home にある本体のため、そちらを対象にする。
	venv が無い環境では Bot がどの Python で動くか決められないため ToolError。
	"""
	try:
		lines = VENV_CFG.read_text(encoding="utf-8", errors="replace").splitlines()
	except OSError as e:
		raise ToolError(f"{VENV_CFG} を読めません。Bot の venv を作成してから実行してください。({e})") from e
	for line in lines:
		key, sep, value = line.partition("=")
		if sep and key.strip().lower() == "home":
			candidate = Path(value.strip()) / "python.exe"
			if candidate.is_file():
				# シンボリックリンクだと実行中のプロセスのパスと一致しないため実体にする
				return candidate.resolve()
	raise ToolError(f"{VENV_CFG} の home から python.exe を特定できませんでした。")


def ffmpeg_path() -> Path | None:
	"""PATH 上の ffmpeg.exe の実体を返す。見つからなければ None。(winget のリンクなどは実体に解決する)"""
	found = shutil.which("ffmpeg")
	return Path(found).resolve() if found else None


def policy_targets() -> list[tuple[str, Path, int]]:
	"""作成する QoS ポリシーの (名前, 対象 exe, DSCP) の一覧。"""
	targets = [(POLICY_PREFIX + "python", bot_python_path(), DSCP_BOT)]
	ffmpeg = ffmpeg_path()
	if ffmpeg:
		targets.append((POLICY_PREFIX + "ffmpeg", ffmpeg, DSCP_FFMPEG))
	else:
		print("[注意] ffmpeg が PATH に見つからないため、FFmpeg のポリシーは作りません。")
	return targets


# ---------------------------------------------------------------------------
# レジストリ
# ---------------------------------------------------------------------------

def reg_read(key: str, name: str) -> tuple[bool, int | None, object]:
	"""HKLM の値を (存在するか, 型, 値) で返す。"""
	try:
		with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key, 0, winreg.KEY_READ | REG_VIEW) as handle:
			value, value_type = winreg.QueryValueEx(handle, name)
			return True, value_type, value
	except FileNotFoundError:
		return False, None, None


def reg_write(key: str, name: str, value_type: int, value: object) -> None:
	"""HKLM に値を書く。キーが無ければ作る。"""
	with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, key, 0, winreg.KEY_SET_VALUE | REG_VIEW) as handle:
		winreg.SetValueEx(handle, name, 0, value_type, value)


def reg_delete(key: str, name: str) -> None:
	"""HKLM の値を消す。無ければ何もしない。"""
	try:
		with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key, 0, winreg.KEY_SET_VALUE | REG_VIEW) as handle:
			winreg.DeleteValue(handle, name)
	except FileNotFoundError:
		pass


def desired_registry() -> list[tuple[str, str, int, object]]:
	"""有効時に書き込む (キー, 名前, 型, 値) の一覧。"""
	items: list[tuple[str, str, int, object]] = [(QOS_REG_KEY, NLA_VALUE_NAME, winreg.REG_SZ, NLA_VALUE)]
	for name, percent in DO_LIMITS.items():
		items.append((DO_REG_KEY, name, winreg.REG_DWORD, percent))
	return items


# ---------------------------------------------------------------------------
# 退避ファイル
# ---------------------------------------------------------------------------

def load_state() -> dict | None:
	"""
	有効化前の退避内容を読む。無ければ None。
	壊れている・形式が違うときは、現在の値 (このツールが書いた値かもしれない) で上書きしないよう ToolError で止める。
	"""
	try:
		# メモ帳などで編集されて BOM が付いていても読めるようにする
		data = json.loads(STATE_FILE.read_text(encoding="utf-8-sig"))
	except FileNotFoundError:
		return None
	except (OSError, ValueError) as e:
		raise ToolError(f"退避ファイル {STATE_FILE} を読めません。中身を確認し、不要なら削除してから実行してください。({e})") from e
	if not isinstance(data, dict) or data.get("version") != STATE_VERSION or not isinstance(data.get("registry"), list):
		raise ToolError(f"退避ファイル {STATE_FILE} の形式が違います。中身を確認し、不要なら削除してから実行してください。")
	return data


def save_state_if_missing() -> None:
	"""初回の有効化時だけ、書き換える前の値を退避する。(有効化を繰り返しても元の値を保つ)"""
	if load_state() is not None:
		return
	registry = []
	for key, name, expected_type, _value in desired_registry():
		exists, value_type, value = reg_read(key, name)
		if exists and value_type != expected_type:
			raise ToolError(f"{key}\\{name} が想定外の型 ({value_type}) で設定済みのため、変更せずに中止しました。")
		registry.append({"key": key, "name": name, "exists": exists, "type": value_type, "value": value})
	state = {"version": STATE_VERSION, "created": datetime.now().isoformat(timespec="seconds"), "registry": registry}
	STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
	# 書き込み途中で止まっても壊れたファイルが残らないよう、一時ファイルに書いてから置き換える
	temp = STATE_FILE.with_suffix(".tmp")
	temp.write_text(json.dumps(state, ensure_ascii=False, indent="\t"), encoding="utf-8")
	os.replace(temp, STATE_FILE)


# ---------------------------------------------------------------------------
# QoS ポリシー
# ---------------------------------------------------------------------------

def list_own_policies() -> list[dict]:
	"""このツールが作った QoS ポリシーの一覧。"""
	return run_ps_json(
		f"Get-NetQosPolicy | Where-Object {{ $_.Name -like {_ps_quote(POLICY_PREFIX + '*')} }} "
		"| Select-Object Name, AppPathName, DSCPAction, NetworkProfile, IPProtocol"
	)


def remove_own_policies() -> int:
	"""このツールが作った QoS ポリシーを消し、消した件数を返す。"""
	policies = list_own_policies()
	for policy in policies:
		run_ps(f"Remove-NetQosPolicy -Name {_ps_quote(policy['Name'])} -Confirm:$false")
	return len(policies)


def create_policies(targets: list[tuple[str, Path, int]]) -> None:
	"""QoS ポリシーを作る。全ネットワーク・TCP と UDP の両方が対象。"""
	for name, exe, dscp in targets:
		run_ps(
			f"New-NetQosPolicy -Name {_ps_quote(name)} -AppPathNameMatchCondition {_ps_quote(str(exe))} "
			f"-IPProtocolMatchCondition Both -NetworkProfile All -DSCPAction {dscp} | Out-Null"
		)


# ---------------------------------------------------------------------------
# 配信の最適化
# ---------------------------------------------------------------------------

def show_delivery_optimization() -> None:
	"""配信の最適化サービスが実際に使っている帯域の上限を表示する。(ポリシーはサービスの再起動なしで読み込まれる)"""
	try:
		rows = run_ps_json(f"Get-DOConfig | Select-Object {', '.join(DO_EFFECTIVE_FIELDS)}")
	except ToolError as e:
		print(f"  実際の値を取得できませんでした: {e}")
		return
	for row in rows:
		for field, label in DO_EFFECTIVE_FIELDS.items():
			percent = row.get(field)
			shown = "制限なし (自動調整)" if percent in (0, None) else f"{percent}%"
			print(f"  {label}: {shown}")


# ---------------------------------------------------------------------------
# 操作
# ---------------------------------------------------------------------------

def warn_if_pacer_disabled() -> None:
	"""DSCP の付与に必要な QoS パケット スケジューラーが接続中のアダプターで無効なら知らせる。"""
	try:
		rows = run_ps_json(
			"Get-NetAdapter -Physical | Where-Object Status -eq 'Up' "
			"| Get-NetAdapterBinding -ComponentID ms_pacer | Select-Object Name, Enabled"
		)
	except ToolError as e:
		print(f"[注意] QoS パケット スケジューラーの状態を確認できませんでした: {e}")
		return
	for row in rows:
		if not row.get("Enabled"):
			print(
				f"[注意] {row.get('Name')} で QoS パケット スケジューラーが無効のため DSCP が付きません。"
				"アダプターのプロパティで有効にしてください。(切り替え時に通信が一瞬切れます)"
			)


def enable() -> None:
	"""優先設定を有効にする。"""
	require_admin()
	targets = policy_targets()
	save_state_if_missing()

	remove_own_policies()
	create_policies(targets)
	for name, exe, dscp in targets:
		print(f"[OK] QoS ポリシー {name}: {exe} の通信に DSCP {dscp} を付けます。")

	for key, name, value_type, value in desired_registry():
		reg_write(key, name, value_type, value)
	print(f"[OK] ドメイン不参加でも DSCP が付くよう {NLA_VALUE_NAME} を設定しました。")
	limits = " / ".join(f"{name}={percent}%" for name, percent in DO_LIMITS.items())
	print(f"[OK] 配信の最適化の帯域を制限しました。({limits})")
	warn_if_pacer_disabled()

	print("")
	print("有効にしました。Bot の再起動は不要で、再生中の通信にもすぐ反映されます。")
	print("反映を確かめるには再生中に check を実行してください。")


def disable() -> None:
	"""有効にする前の状態に戻す。"""
	require_admin()
	# 退避ファイルが壊れていれば何も変えずに止めるため、先に読む
	state = load_state()
	removed = remove_own_policies()
	print(f"[OK] QoS ポリシーを {removed} 件削除しました。")

	if state is None and removed == 0:
		# 有効化されていない状態で消すと、元から設定されていた値 (例: Do not use NLA) まで消してしまうため何もしない
		print("")
		print("すでに無効です。(変更はありません)")
		return
	if state is not None:
		# 退避ファイルは一般ユーザーも書ける場所にあるため、このツールが扱う値・型に一致するものだけ戻す
		allowed = {(key, name): value_type for key, name, value_type, _value in desired_registry()}
		for item in state.get("registry", []):
			if not isinstance(item, dict):
				continue
			key, name = item.get("key"), item.get("name")
			expected_type = allowed.get((key, name))
			if expected_type is None:
				print(f"[注意] 退避ファイルに対象外の項目があったため無視しました: {key}\\{name}")
				continue
			if not item.get("exists"):
				reg_delete(key, name)
			elif item.get("type") == expected_type and isinstance(item.get("value"), REG_PY_TYPES[expected_type]):
				reg_write(key, name, expected_type, item["value"])
			else:
				print(f"[注意] 退避された {name} の型が想定外のため戻さず削除しました。")
				reg_delete(key, name)
		print("[OK] レジストリを有効化前の値に戻しました。")
	else:
		# 退避が無いときは、このツールが書いた値のままのものだけ消す (他で設定された値は残す)
		for key, name, _type, value in desired_registry():
			exists, _cur_type, current = reg_read(key, name)
			if exists and current == value:
				reg_delete(key, name)
		print("[OK] 退避ファイルが無いため、このツールの設定値と同じものだけ削除しました。")

	try:
		STATE_FILE.unlink(missing_ok=True)
	except OSError as e:
		print(f"[注意] 退避ファイルを削除できませんでした: {e}")
	print("")
	print("無効にしました。(元の状態に戻しました)")


def status() -> None:
	"""現在の設定を表示する。"""
	print("=== QoS ポリシー ===")
	policies = list_own_policies()
	try:
		targets_now = {name: exe for name, exe, _dscp in policy_targets()}
	except ToolError as e:
		print(f"  [注意] 対象プログラムを特定できません: {e}")
		targets_now = {}
	stale = False
	if policies:
		for policy in policies:
			exe = Path(policy.get("AppPathName") or "")
			if not exe.is_file():
				mark = "  (このパスのファイルがありません。on をやり直してください)"
				stale = True
			elif policy["Name"] in targets_now and str(targets_now[policy["Name"]]).lower() != str(exe).lower():
				mark = f"  (現在の対象 {targets_now[policy['Name']]} と違います。on をやり直してください)"
				stale = True
			else:
				mark = ""
			print(f"  {policy['Name']}: DSCP {policy.get('DSCPAction')}  {exe}{mark}")
	else:
		print("  なし")

	print("=== レジストリ ===")
	matched = 0
	desired = desired_registry()
	for key, name, _type, value in desired:
		exists, _cur_type, current = reg_read(key, name)
		if exists and current == value:
			matched += 1
		shown = current if exists else "未設定"
		print(f"  {name} = {shown}")

	print("=== 配信の最適化 (実際の値) ===")
	show_delivery_optimization()

	print("=== 対象プログラム ===")
	for name, exe in targets_now.items():
		print(f"  {name.removeprefix(POLICY_PREFIX)}: {exe}")

	warn_if_pacer_disabled()

	# レジストリの値は他で設定済みのこともあるため、無効の判定はこのツールのポリシーと退避ファイルで行う
	has_state = STATE_FILE.is_file()
	if policies and matched == len(desired) and not stale:
		overall = "有効"
	elif not policies and not has_state:
		overall = "無効"
	else:
		overall = "一部だけ有効 (on か off をやり直してください)"
	print("")
	print(f"状態: {overall}")
	if not is_admin():
		print("(管理者権限が無いため、一部の項目は正しく読めていない可能性があります)")


# ---------------------------------------------------------------------------
# 動作確認 (パケットキャプチャ)
# ---------------------------------------------------------------------------

def _iter_pcapng_frames(data: bytes):
	"""pcapng の Enhanced Packet Block からフレームの中身を順に返す。途中で切れたブロックがあればそこで止める。"""
	pos = 0
	endian = "<"
	size = len(data)
	while pos + PCAPNG_MIN_BLOCK_LEN <= size:
		block_type = struct.unpack_from(endian + "I", data, pos)[0]
		if block_type == PCAPNG_SHB:
			endian = "<" if data[pos + 8:pos + 12] == PCAPNG_BOM_LE else ">"
		block_len = struct.unpack_from(endian + "I", data, pos + 4)[0]
		if block_len < PCAPNG_MIN_BLOCK_LEN or pos + block_len > size:
			break
		if block_type == PCAPNG_EPB and block_len >= PCAPNG_EPB_DATA_OFFSET:
			cap_len = struct.unpack_from(endian + "I", data, pos + PCAPNG_EPB_CAPLEN_OFFSET)[0]
			start = pos + PCAPNG_EPB_DATA_OFFSET
			yield data[start:min(start + cap_len, pos + block_len)]
		pos += block_len


def _parse_frame(frame: bytes) -> tuple[int, bytes, int, int] | None:
	"""Ethernet フレームから (プロトコル番号, 送信元 IP, 送信元ポート, DSCP) を取り出す。TCP/UDP 以外や短すぎるものは None。"""
	offset = ETHERTYPE_OFFSET
	if len(frame) < offset + 2:
		return None
	ethertype = struct.unpack_from("!H", frame, offset)[0]
	if ethertype == ETHERTYPE_VLAN:
		offset += VLAN_TAG_LEN
		if len(frame) < offset + 2:
			return None
		ethertype = struct.unpack_from("!H", frame, offset)[0]
	ip = frame[offset + 2:]
	if ethertype == ETHERTYPE_IPV4 and len(ip) >= IPV4_MIN_HEADER_LEN:
		header_len = (ip[0] & 0x0F) * 4
		dscp = ip[1] >> 2
		proto = ip[9]
		src = bytes(ip[12:16])
		l4 = ip[header_len:]
	elif ethertype == ETHERTYPE_IPV6 and len(ip) >= IPV6_HEADER_LEN:
		traffic_class = ((ip[0] & 0x0F) << 4) | (ip[1] >> 4)
		dscp = traffic_class >> 2
		proto = ip[6]
		src = bytes(ip[8:24])
		l4 = ip[IPV6_HEADER_LEN:]
	else:
		return None
	if proto not in (PROTO_TCP, PROTO_UDP) or len(l4) < 2:
		return None
	return proto, src, struct.unpack_from("!H", l4, 0)[0], dscp


def _local_addresses() -> set[bytes]:
	"""この PC の IP アドレスをバイト列の集合で返す。(受信パケットを送信として数えないために使う)"""
	rows = run_ps_json("Get-NetIPAddress | Select-Object IPAddress")
	addresses = set()
	for row in rows:
		text = str(row.get("IPAddress") or "").split("%")[0]
		try:
			addresses.add(ipaddress.ip_address(text).packed)
		except ValueError:
			continue
	return addresses


def _bot_sockets(targets: list[tuple[str, Path, int]]) -> dict[tuple[int, int], str]:
	"""対象プログラムが使っているローカルポートを {(プロトコル番号, ポート): ポリシー名} で返す。"""
	paths = ", ".join(_ps_quote(str(exe)) for _name, exe, _dscp in targets)
	rows = run_ps_json(
		f"$paths = @({paths}); "
		"$procs = Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and ($paths -contains $_.ExecutablePath) }; "
		"$map = @{}; foreach ($p in $procs) { $map[[int]$p.ProcessId] = $p.ExecutablePath }; "
		"@(Get-NetUDPEndpoint -ErrorAction SilentlyContinue | Where-Object { $map.ContainsKey([int]$_.OwningProcess) } "
		"| ForEach-Object { [pscustomobject]@{ Proto = 17; Port = [int]$_.LocalPort; Path = $map[[int]$_.OwningProcess] } }) + "
		"@(Get-NetTCPConnection -State Established -ErrorAction SilentlyContinue | Where-Object { $map.ContainsKey([int]$_.OwningProcess) } "
		"| ForEach-Object { [pscustomobject]@{ Proto = 6; Port = [int]$_.LocalPort; Path = $map[[int]$_.OwningProcess] } })"
	)
	by_path = {str(exe).lower(): name for name, exe, _dscp in targets}
	return {(int(r["Proto"]), int(r["Port"])): by_path.get(str(r["Path"]).lower(), "?") for r in rows}


def _capture(seconds: int) -> bytes:
	"""pktmon で NIC を通るパケットを記録し、pcapng のバイト列を返す。"""
	# 前回が強制終了などで記録中のまま残っていると開始できないため、先に止めておく
	run_cmd(["pktmon", "stop"])
	with tempfile.TemporaryDirectory(prefix="net_priority_") as work:
		etl = Path(work) / "capture.etl"
		pcap = Path(work) / "capture.pcapng"
		code, out = run_cmd(["pktmon", "start", "--capture", "--comp", "nics", "--pkt-size", str(CAPTURE_BYTES), "--file-name", str(etl)])
		if code != 0:
			raise ToolError(f"pktmon を開始できませんでした: {out}")
		print(f"{seconds} 秒間パケットを記録しています...")
		try:
			time.sleep(seconds)
		finally:
			run_cmd(["pktmon", "stop"])
		code, out = run_cmd(["pktmon", "etl2pcap", str(etl), "--out", str(pcap)])
		if code != 0 or not pcap.is_file():
			raise ToolError(f"記録の変換に失敗しました: {out}")
		return pcap.read_bytes()


def check() -> int:
	"""数秒間パケットを捕まえ、Bot の送信パケットに付いている DSCP を集計する。すべて期待どおりなら 0 を返す。"""
	require_admin()
	targets = policy_targets()
	sockets = _bot_sockets(targets)
	if not sockets:
		raise ToolError("Bot の通信が見つかりません。Bot が起動していて、曲を再生している間に実行してください。")
	local = _local_addresses()
	data = _capture(CHECK_SECONDS)

	counts: dict[str, Counter] = {name: Counter() for name, _exe, _dscp in targets}
	for frame in _iter_pcapng_frames(data):
		parsed = _parse_frame(frame)
		if parsed is None:
			continue
		proto, src, src_port, dscp = parsed
		if src not in local:
			continue
		owner = sockets.get((proto, src_port))
		if owner in counts:
			counts[owner][dscp] += 1

	print("")
	if not any(counts.values()):
		print("Bot の送信パケットを 1 件も記録できませんでした。曲を再生している間にもう一度実行してください。")
		return 1
	ok = True
	for name, _exe, expected in targets:
		counter = counts[name]
		total = sum(counter.values())
		if total == 0:
			print(f"  {name}: 送信パケットなし (この間は通信していませんでした)")
			continue
		detail = ", ".join(f"DSCP {d}: {n} 件" for d, n in sorted(counter.items()))
		marked = counter.get(expected, 0)
		verdict = "OK" if marked == total else "一部未適用" if marked else "未適用"
		if marked != total:
			ok = False
		print(f"  {name}: {detail}  -> [{verdict}] (期待値 DSCP {expected})")
	print("")
	if ok:
		print("記録できた通信には期待どおりの DSCP が付いていました。")
		return 0
	print("DSCP が付いていない通信があります。on を実行済みなら、有効化より前に開いた通信の残りの可能性があるため、少し待ってからもう一度確かめてください。")
	return 1


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

ACTIONS = {
	"on": (enable, "有効にする (Bot の通信を優先)"),
	"off": (disable, "無効にする (元に戻す)"),
	"status": (status, "状態を表示"),
	"check": (check, "動作確認 (再生中に実行)"),
}


def run_action(name: str) -> int:
	"""操作を実行し、終了コードを返す。"""
	func, _label = ACTIONS[name]
	try:
		return func() or 0
	except ToolError as e:
		print(f"[エラー] {e}")
	except OSError as e:
		print(f"[エラー] OS の操作に失敗しました: {e}")
	except KeyboardInterrupt:
		print("[中断] 途中で中断しました。status で状態を確認してください。")
	except Exception as e:
		print(f"[エラー] 想定外のエラーが発生しました: {type(e).__name__}: {e}")
	return 1


def menu() -> int:
	"""対話メニュー。終了を選ぶか入力が終わるまで繰り返す。"""
	keys = list(ACTIONS)
	while True:
		print("")
		print("=== SatouSioBOT ネットワーク優先設定 ===")
		for index, key in enumerate(keys, 1):
			print(f"  {index}. {ACTIONS[key][1]}")
		print("  0. 終了")
		try:
			choice = input("番号を入力してください: ").strip()
		except (EOFError, KeyboardInterrupt):
			return 0
		if choice == "0":
			return 0
		if choice.isdigit() and 1 <= int(choice) <= len(keys):
			print("")
			run_action(keys[int(choice) - 1])
		else:
			print("番号が正しくありません。")


def main() -> int:
	"""コマンドライン引数を解釈して実行する。"""
	if sys.platform != "win32":
		print("このツールは Windows 専用です。")
		return 1
	for stream in (sys.stdout, sys.stderr):
		try:
			stream.reconfigure(errors="replace")
		except AttributeError:
			pass
	parser = argparse.ArgumentParser(description="SatouSioBOT の通信を OS 側で優先させる設定を切り替えます。")
	parser.add_argument("action", nargs="?", choices=list(ACTIONS), help="省略するとメニューを表示")
	args = parser.parse_args()
	if args.action is None:
		return menu()
	return run_action(args.action)


if __name__ == "__main__":
	sys.exit(main())
