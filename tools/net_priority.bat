@echo off
setlocal
rem Toggle the network priority settings for the bot.
rem No argument shows a menu. Arguments: on / off / status / check
rem Keep this file ASCII only: cmd parses batch files in the OEM code page, so UTF-8 text can break lines.

set "ELEVATED="
if /i "%~1"=="/elevated" (
	set "ELEVATED=1"
	shift /1
)

rem fltmc succeeds only as administrator and, unlike "net session", does not depend on the Server service.
fltmc >nul 2>&1
if not errorlevel 1 goto :run
if defined ELEVATED (
	echo [ERROR] Administrator rights could not be obtained.
	pause
	exit /b 1
)
if "%~1"=="" (
	powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '/elevated' -Verb RunAs"
) else (
	powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '/elevated %~1' -Verb RunAs"
)
if errorlevel 1 (
	echo [ERROR] Elevation was cancelled or failed.
	pause
	exit /b 1
)
exit /b 0

:run
cd /d "%~dp0.."
set "PY_EXE=venv\Scripts\python.exe"
if not exist "%PY_EXE%" set "PY_EXE=py"
"%PY_EXE%" "%~dp0net_priority.py" %1
set "RC=%ERRORLEVEL%"

rem The elevated window closes on exit, so pause to let the result be read. Always pause on failure.
if not "%RC%"=="0" (
	pause
) else if defined ELEVATED if not "%~1"=="" pause
exit /b %RC%
