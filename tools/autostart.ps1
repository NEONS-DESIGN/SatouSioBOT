# サインイン時に Bot (start.bat) を管理者として自動で起動するタスクを登録・解除する。
# 引数なしでメニュー、on / off / status を直接指定可。要管理者 (最上位の特権で動くタスクの登録に必要)。
# PC の再起動後に誰もサインインしなくても起動させるには、Windows の自動サインインを別途有効にしておく。

param(
	[ValidateSet('on', 'off', 'status')]
	[string]$Action
)

$TaskName = 'SatouSioBOT'
$TaskDescription = 'サインイン時に SatouSioBOT (start.bat) を管理者として起動する'
# サインイン直後はネットワークの準備ができていないことがあるため、起動を遅らせる
$LogonDelay = [TimeSpan]::FromSeconds(30)
# タスクの既定の優先度 (7) は通常未満で、CPU・I/O の優先度が下がるため通常 (4) にする
$TaskPriority = 4
$WinlogonKey = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
$PasswordLessKey = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$StartBat = Join-Path $RepoRoot 'start.bat'

function Test-Administrator {
	# 管理者として実行されているかを返す
	$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
	return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-RegistryValue([string]$Path, [string]$Name) {
	# レジストリの値を返す。キーや値が無ければ $null
	$item = Get-ItemProperty -LiteralPath $Path -Name $Name -ErrorAction SilentlyContinue
	if ($null -eq $item) {
		return $null
	}
	return $item.$Name
}

function Write-AutoLogonState {
	# 自動サインインの状態と、有効でないときの影響を表示する
	$enabled = (Get-RegistryValue $WinlogonKey 'AutoAdminLogon') -eq '1'
	if ($enabled) {
		$user = Get-RegistryValue $WinlogonKey 'DefaultUserName'
		Write-Host "自動サインイン: 有効 ($user)" -ForegroundColor Green
		return
	}
	Write-Host '自動サインイン: 無効' -ForegroundColor Yellow
	Write-Host '  PC の再起動後は、誰かがサインインするまで Bot は起動しません。'
	Write-Host '  再起動だけで起動させるには、Sysinternals の Autologon で自動サインインを有効にしてください。'
	if ((Get-RegistryValue $PasswordLessKey 'DevicePasswordLessBuildVersion') -eq 2) {
		Write-Host '  (「Microsoft アカウントには Windows Hello サインインのみを許可する」が有効なため、先に設定のアカウント > サインイン オプションで無効にしてください)'
	}
}

function Enable-AutoStart {
	# 現在のユーザーのサインイン時に start.bat を最上位の特権で起動するタスクを登録する (既にあれば置き換える)
	if (-not (Test-Path -LiteralPath $StartBat)) {
		throw "start.bat が見つかりません: $StartBat"
	}
	$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
	$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
	$trigger.Delay = [Xml.XmlConvert]::ToString($LogonDelay)
	# Interactive: サインインしたユーザーのデスクトップにウィンドウを出す (パスワードは保存しない)
	$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
	# Bot は常駐するため実行時間の上限を外し、既に動いていれば二重に起動しない
	$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -Priority $TaskPriority
	$action = New-ScheduledTaskAction -Execute "`"$StartBat`"" -WorkingDirectory $RepoRoot
	Register-ScheduledTask -TaskName $TaskName -Description $TaskDescription -Trigger $trigger -Principal $principal -Settings $settings -Action $action -Force -ErrorAction Stop | Out-Null
	Write-Host "タスク $TaskName を登録しました。($user のサインイン時、$([int]$LogonDelay.TotalSeconds) 秒後に管理者として起動)" -ForegroundColor Green
	Write-AutoLogonState
}

function Disable-AutoStart {
	# タスクを削除する。動いている Bot は止めない
	if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
		Write-Host "タスク $TaskName は登録されていません。"
		return
	}
	Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
	Write-Host "タスク $TaskName を削除しました。(起動中の Bot はそのまま動き続けます)" -ForegroundColor Green
}

function Show-AutoStartStatus {
	# タスクの登録内容・前回の実行結果と、自動サインインの状態を表示する
	$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
	if (-not $task) {
		Write-Host "タスク ${TaskName}: 未登録" -ForegroundColor Yellow
	} else {
		$info = $task | Get-ScheduledTaskInfo
		Write-Host "タスク ${TaskName}: 登録済み (状態: $($task.State))" -ForegroundColor Green
		Write-Host "  ユーザー: $($task.Principal.UserId) / 権限: $($task.Principal.RunLevel)"
		Write-Host "  実行: $($task.Actions[0].Execute)"
		if ($info.LastRunTime -and $info.LastRunTime.Year -gt 2000) {
			Write-Host "  前回の起動: $($info.LastRunTime) (結果: 0x$('{0:X}' -f $info.LastTaskResult))"
		}
	}
	Write-AutoLogonState
}

function Invoke-AutoStartAction([string]$Name) {
	# 操作を 1 つ実行し、成功なら 0、失敗なら 1 を返す
	try {
		switch ($Name) {
			'on' { Enable-AutoStart }
			'off' { Disable-AutoStart }
			'status' { Show-AutoStartStatus }
		}
		return 0
	} catch {
		Write-Host "[エラー] $_" -ForegroundColor Red
		return 1
	}
}

function Show-Menu {
	# 対話メニュー。終了を選ぶまで繰り返す
	$items = @(
		@('on', 'サインイン時に Bot を自動で起動する'),
		@('off', '自動起動をやめる'),
		@('status', '現在の設定を表示する')
	)
	while ($true) {
		Write-Host ''
		Write-Host '=== SatouSioBOT 自動起動設定 ==='
		for ($i = 0; $i -lt $items.Count; $i++) {
			Write-Host "  $($i + 1). $($items[$i][1])"
		}
		Write-Host '  0. 終了'
		$choice = Read-Host '番号を入力してください'
		if ($null -eq $choice -or $choice.Trim() -eq '0') {
			return
		}
		$index = 0
		if ([int]::TryParse($choice.Trim(), [ref]$index) -and $index -ge 1 -and $index -le $items.Count) {
			Write-Host ''
			Invoke-AutoStartAction $items[$index - 1][0] | Out-Null
		} else {
			Write-Host '番号が正しくありません。'
		}
	}
}

if (-not (Test-Administrator)) {
	Write-Host '[エラー] 管理者として実行してください。' -ForegroundColor Red
	exit 1
}
if ($Action) {
	exit (Invoke-AutoStartAction $Action)
}
Show-Menu
exit 0
