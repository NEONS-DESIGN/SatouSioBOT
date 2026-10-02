@echo off
cd /d %~dp0
rem PowerShell 7 (pwsh) があれば使い、無ければ Windows PowerShell 5.1 で start.ps1 を実行する
where pwsh >nul 2>&1 && (set "PS_EXE=pwsh") || (set "PS_EXE=powershell")
%PS_EXE% -NoProfile -ExecutionPolicy Bypass -NoExit -File "%~dp0start.ps1"
