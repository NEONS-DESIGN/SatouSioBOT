# SatouSioBOT の起動スクリプト。venv の Python を優先し、無ければ Python ランチャーで 3.14 を指定して起動する。
# 管理者として実行していなければ起動しない。同じフォルダの Bot がこのスクリプトから起動中なら二重に起動しない。
# start.bat から -NoExit 付きで呼ばれるため、Bot の終了後もウィンドウは残る。
# Bot が定期再起動のために終了コード $RestartExitCode で終わったときは、同じウィンドウで起動し直す (管理者の権限も引き継がれる)。

$PythonVersion = '3.14'
$EntryPoint = 'main.py'
# module/daily_restart.py の RESTART_EXIT_CODE / SUPERVISOR_ENV と同じ値にする
$RestartExitCode = 75
$SupervisorEnv = 'SATOUSIOBOT_SUPERVISED'
# 起動し直す前に、終了した Bot の常駐 Deno が消えるのを待つ最大秒数 (過ぎたら強制終了する)
$CleanupTimeoutSeconds = 15
# 起動し直すまでの待ち時間 (秒)
$RestartDelaySeconds = 3
# 二重起動の検知に使う Mutex 名の接頭辞と、名前に含めるフォルダのハッシュのバイト数 (管理者で動かすため全セッション共通の Global を使う)
$MutexPrefix = 'Global\SatouSioBOT-'
$MutexHashBytes = 8

function Get-LeftoverDeno {
	# このフォルダの resident_jsc.js を実行している Deno (Bot の抽出ワーカーが起動する常駐 Deno) を返す
	$script = Join-Path $PSScriptRoot 'module\resident_jsc.js'
	Get-CimInstance Win32_Process -Filter "Name='deno.exe'" -ErrorAction SilentlyContinue |
		Where-Object { $_.CommandLine -and $_.CommandLine.IndexOf($script, [StringComparison]::OrdinalIgnoreCase) -ge 0 }
}

function Stop-LeftoverDeno {
	# 終了した Bot の常駐 Deno が自然に終わるのを待ち、残っていれば強制終了する
	$deadline = (Get-Date).AddSeconds($CleanupTimeoutSeconds)
	while ((Get-Date) -lt $deadline) {
		if (-not @(Get-LeftoverDeno).Count) {
			return
		}
		Start-Sleep -Milliseconds 500
	}
	foreach ($process in @(Get-LeftoverDeno)) {
		Write-Host "終了しなかった常駐 Deno (pid $($process.ProcessId)) を停止します。" -ForegroundColor Yellow
		Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
	}
}

function Test-Administrator {
	# 管理者として実行されているかを返す
	$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
	return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-InstanceMutexName {
	# このフォルダの Bot に固有の Mutex 名を返す (別のフォルダに置いた Bot とは重ならない)
	$sha = [Security.Cryptography.SHA256]::Create()
	try {
		$bytes = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($PSScriptRoot.ToLowerInvariant()))
	} finally {
		$sha.Dispose()
	}
	$hash = -join ($bytes[0..($MutexHashBytes - 1)] | ForEach-Object { $_.ToString('x2') })
	return "$MutexPrefix$hash"
}

$mutex = $null
$ownsMutex = $false
try {
	Set-Location -LiteralPath $PSScriptRoot -ErrorAction Stop
	if (-not (Test-Administrator)) {
		Write-Host "管理者として実行されていないため起動しません。start.bat から起動してください。(管理者権限の確認が表示されます)" -ForegroundColor Red
		return
	}
	$mutex = New-Object Threading.Mutex($false, (Get-InstanceMutexName))
	try {
		$ownsMutex = $mutex.WaitOne(0)
	} catch [Threading.AbandonedMutexException] {
		# 前回のウィンドウが強制終了されて解放されずに残っていた。所有権はこちらに移っている
		$ownsMutex = $true
	}
	if (-not $ownsMutex) {
		Write-Host "このフォルダの Bot は既に別のウィンドウで起動しています。" -ForegroundColor Yellow
		return
	}
	$venvPython = Join-Path $PSScriptRoot 'venv\Scripts\python.exe'
	$useVenv = Test-Path -LiteralPath $venvPython
	if (-not $useVenv -and -not (Get-Command py -ErrorAction SilentlyContinue)) {
		Write-Host "Python が見つかりません。venv を作成するか、Python $PythonVersion をインストールしてください。" -ForegroundColor Red
		return
	}
	# Bot に、終了後に起動し直してもらえることを伝える (直接 python main.py で起動した場合は定期再起動しない)
	Set-Item -Path "Env:$SupervisorEnv" -Value '1'
	while ($true) {
		if ($useVenv) {
			& $venvPython $EntryPoint
		} else {
			py "-$PythonVersion" $EntryPoint
		}
		if ($LASTEXITCODE -ne $RestartExitCode) {
			break
		}
		Write-Host "定期再起動のため Bot を起動し直します..." -ForegroundColor Cyan
		Stop-LeftoverDeno
		Start-Sleep -Seconds $RestartDelaySeconds
	}
	if ($LASTEXITCODE -ne 0) {
		Write-Host "Bot が終了コード $LASTEXITCODE で終了しました。" -ForegroundColor Yellow
	}
} catch {
	Write-Host "起動に失敗しました: $_" -ForegroundColor Red
} finally {
	if ($ownsMutex) {
		$mutex.ReleaseMutex()
	}
	if ($mutex) {
		$mutex.Dispose()
	}
}
