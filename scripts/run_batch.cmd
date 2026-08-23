@echo off
rem 夜間バッチ。個人利用の操作感を損なわないよう低優先度で実行する。
rem タスクスケジューラからはこのファイルを指定する。

setlocal
cd /d "%~dp0.."

if exist ".venv\Scripts\python.exe" (
  set "PYTHON=.venv\Scripts\python.exe"
) else (
  set "PYTHON=python"
)

start "go-review" /low /wait /b "%PYTHON%" -m go_review --quiet run
endlocal
