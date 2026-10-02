@echo off
cd /d %~dp0
rem venv があればそれを使い、無ければ Python ランチャーで 3.13 を指定して起動する
if exist "venv\Scripts\python.exe" (
	"venv\Scripts\python.exe" main.py
) else (
	py -3.13 main.py
)
pause
