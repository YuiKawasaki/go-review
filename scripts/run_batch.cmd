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

rem 最新の web/data を Cloudflare Pages へ配信する（アプリ本体+データを1つのプロジェクトで公開）
if exist "%APPDATA%\npm\wrangler.cmd" (
  "%APPDATA%\npm\wrangler.cmd" pages deploy web --project-name=go-review --branch=main --commit-dirty=true >> "%~dp0..\logs\deploy.log" 2>&1
)
endlocal
