@echo off
setlocal
rem Start the bot as administrator in a new Windows Terminal window.
rem Falls back to this console when Windows Terminal is not installed. Administrator rights are required.
rem Keep this file ASCII only: cmd parses batch files in the OEM code page, so UTF-8 text can break lines.
cd /d "%~dp0"

set "ELEVATED="
if /i "%~1"=="/elevated" set "ELEVATED=1"

rem fltmc succeeds only as administrator and, unlike "net session", does not depend on the Server service.
fltmc >nul 2>&1
if not errorlevel 1 goto :admin
if defined ELEVATED (
	echo [ERROR] Administrator rights could not be obtained. The bot requires administrator rights.
	pause
	exit /b 1
)
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '/elevated' -Verb RunAs"
if errorlevel 1 (
	echo [ERROR] Elevation was cancelled or failed. The bot requires administrator rights.
	pause
	exit /b 1
)
exit /b 0

:admin
rem Use PowerShell 7 (pwsh) when available, otherwise Windows PowerShell 5.1.
where pwsh >nul 2>&1 && (set "PS_EXE=pwsh") || (set "PS_EXE=powershell")
set "PS_ARGS=-NoProfile -ExecutionPolicy Bypass -NoExit -File "%~dp0start.ps1""

where wt >nul 2>&1
if errorlevel 1 goto :console
rem Windows Terminal started from an elevated process is elevated too. "-w new" keeps it apart from other windows.
wt.exe -w new new-tab --title SatouSioBOT --suppressApplicationTitle -d "%~dp0." %PS_EXE% %PS_ARGS%
if not errorlevel 1 exit /b 0
echo [WARN] Windows Terminal could not be started. Starting in this window instead.

:console
%PS_EXE% %PS_ARGS%
exit /b %ERRORLEVEL%
