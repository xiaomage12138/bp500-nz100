# 注册 Windows 计划任务：每个交易日刷新两次申购额度
#
# 场外 QDII 申购当日 15:00 截止，因此安排：
#   09:40 —— 开盘后不久，取得当日生效的限购额度
#   14:00 —— 下单截止前复核一次，避免盘中调整额度导致信息过期
#
# 执行一次即可：
#   powershell -ExecutionPolicy Bypass -File register-task.ps1
# 删除任务：
#   Unregister-ScheduledTask -TaskName "QDII额度监控刷新" -Confirm:$false
$ErrorActionPreference = "Stop"

$taskName = "QDII额度监控刷新"
$scriptPath = Join-Path $PSScriptRoot "refresh.ps1"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`"" `
    -WorkingDirectory $PSScriptRoot

$days = 'Monday','Tuesday','Wednesday','Thursday','Friday'
$triggers = @(
    (New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At 09:40),
    (New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At 14:00)
)

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers `
    -Settings $settings -Description "抓取标普500/纳指100 QDII基金申购额度并推送 GitHub Pages" -Force

Write-Output "已注册计划任务 '$taskName'：周一至周五 09:40 与 14:00 各刷新一次"
Write-Output "（错过的时间点会在开机后自动补跑；抓取残缺时脚本会中止并保留上次数据）"
