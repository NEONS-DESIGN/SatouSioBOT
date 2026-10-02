# SatouSioBOT の起動スクリプト。venv の Python を優先し、無ければ Python ランチャーで 3.13 を指定して起動する。
# start.bat から -NoExit 付きで呼ばれるため、Bot の終了後もウィンドウは残る。

$PythonVersion = '3.13'
$EntryPoint = 'main.py'

try {
	Set-Location -LiteralPath $PSScriptRoot -ErrorAction Stop
	$venvPython = Join-Path $PSScriptRoot 'venv\Scripts\python.exe'
	if (Test-Path -LiteralPath $venvPython) {
		& $venvPython $EntryPoint
	} elseif (Get-Command py -ErrorAction SilentlyContinue) {
		py "-$PythonVersion" $EntryPoint
	} else {
		Write-Host "Python が見つかりません。venv を作成するか、Python $PythonVersion をインストールしてください。" -ForegroundColor Red
		return
	}
	if ($LASTEXITCODE -ne 0) {
		Write-Host "Bot が終了コード $LASTEXITCODE で終了しました。" -ForegroundColor Yellow
	}
} catch {
	Write-Host "起動に失敗しました: $_" -ForegroundColor Red
}
