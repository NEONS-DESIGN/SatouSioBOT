# SatouSioBOT の起動スクリプト。venv の Python を優先し、無ければ Python ランチャーで 3.13 を指定して起動する。
# start.bat から -NoExit 付きで呼ばれるため、Bot の終了後もウィンドウは残る。
# Bot が定期再起動のために終了コード $RestartExitCode で終わったときは、同じウィンドウで起動し直す (管理者で起動していれば権限も引き継がれる)。

$PythonVersion = '3.13'
$EntryPoint = 'main.py'
# module/daily_restart.py の RESTART_EXIT_CODE / SUPERVISOR_ENV と同じ値にする
$RestartExitCode = 75
$SupervisorEnv = 'SATOUSIOBOT_SUPERVISED'
# 起動し直す前に、終了した Bot の常駐 Deno が消えるのを待つ最大秒数 (過ぎたら強制終了する)
$CleanupTimeoutSeconds = 15
# 起動し直すまでの待ち時間 (秒)
$RestartDelaySeconds = 3

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

try {
	Set-Location -LiteralPath $PSScriptRoot -ErrorAction Stop
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
}
