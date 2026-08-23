<#
  タスクスケジューラへ夜間バッチを登録する（管理者権限で実行）。

  設計意図（要件 5.4 / 5.5）:
    - 深夜 2:00 起動。個人利用の時間帯を避ける
    - StartWhenAvailable: 起動していなかった日の分を次回起動時に自動で消化する
    - AC 電源時のみ実行し、バッテリー消耗を防ぐ
    - 低優先度で走らせ、前面アプリの操作感を損なわない
    - 実行中の通知は出さない（ログとPWA表示で足りる）
#>
param(
    [string]$TaskName = "GoReview Nightly",
    [string]$At = "02:00"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$batch = Join-Path $root "scripts\run_batch.cmd"

if (-not (Test-Path $batch)) { throw "run_batch.cmd が見つかりません: $batch" }

$action = New-ScheduledTaskAction -Execute $batch
$trigger = New-ScheduledTaskTrigger -Daily -At $At

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries:$false `
    -AllowStartIfOnBatteries:$false `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
    -MultipleInstances IgnoreNew `
    -Priority 7

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "囲碁 棋譜レビュー 夜間バッチ" -Force | Out-Null

Write-Output "登録しました: $TaskName （毎日 $At / 取りこぼしは次回起動時に実行）"
Write-Output "確認: Get-ScheduledTask -TaskName '$TaskName'"
Write-Output "手動実行: Start-ScheduledTask -TaskName '$TaskName'"
