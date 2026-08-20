# 注册 Windows 计划任务：每个交易日 11:00 自动执行 refresh.ps1
# 以当前用户身份运行（需在登录状态下），执行一次即可：
#   powershell -ExecutionPolicy Bypass -File register-task.ps1
# 删除任务：  Unregister-ScheduledTask -TaskName "QDII额度监控刷新" -Confirm:$false
$ErrorActionPreference = "Stop"

$taskName = "QDII额度监控刷新"
$scriptPath = Join-Path $PSScriptRoot "refresh.ps1"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`"" `
    -WorkingDirectory $PSScriptRoot

# 周一到周五 11:00（A股开盘中，限购公告已生效、ETF 有实时价）
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 11:00

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "抓取标普500/纳指100 QDII基金额度数据并推送 GitHub Pages" -Force

Write-Output "已注册计划任务 '$taskName'：周一至周五 11:00 自动刷新（错过时间点会在开机后补跑）"
