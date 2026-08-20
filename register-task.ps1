# 注册 Windows 计划任务：每个交易日按北京时间刷新两次申购额度
#
# 计划任务只能按「机器本地时间」触发，而这台机器不一定在中国时区。
# 本脚本以北京时间为准设定目标时刻，再换算成对应的本地时刻与星期（可能跨日）。
#
# 目标（北京时间，A股交易时段内）：
#   10:30 —— 开盘后，当日限购额度已生效，ETF/LOF 有实时价
#   13:30 —— 场外申购 15:00 截止前复核，留足下单时间
# 这两个时刻各留有 ±1 小时余量，因此即便本地夏令时切换导致偏移一小时，
# 仍落在交易时段内且早于 15:00 截止；切换后重跑本脚本即可精确校正。
#
# 执行一次即可：
#   powershell -ExecutionPolicy Bypass -File register-task.ps1
# 删除任务：
#   Unregister-ScheduledTask -TaskName "QDII额度监控刷新" -Confirm:$false
$ErrorActionPreference = "Stop"

$taskName = "QDII额度监控刷新"
$scriptPath = Join-Path $PSScriptRoot "refresh.ps1"

# 北京时间的目标时刻（时, 分）
$beijingTargets = @(@{h = 10; m = 30}, @{h = 13; m = 30})

$bjTz = [System.TimeZoneInfo]::FindSystemTimeZoneById('China Standard Time')
$localTz = [System.TimeZoneInfo]::Local

# 取下一个周一作基准日，把北京时刻换算为本地时刻与跨日偏移
function ConvertTo-LocalTrigger($hour, $minute) {
    $probe = (Get-Date).Date.AddDays(7)
    while ($probe.DayOfWeek -ne [DayOfWeek]::Monday) { $probe = $probe.AddDays(1) }
    $bjDt = [datetime]::new($probe.Year, $probe.Month, $probe.Day, $hour, $minute, 0,
                            [System.DateTimeKind]::Unspecified)
    $utc = [System.TimeZoneInfo]::ConvertTimeToUtc($bjDt, $bjTz)
    $loc = [System.TimeZoneInfo]::ConvertTimeFromUtc($utc, $localTz)
    return @{
        Local     = $loc
        DayShift  = [int]($loc.Date - $bjDt.Date).TotalDays   # -1 / 0 / +1
    }
}

# 北京时间周一~周五，按跨日偏移映射到本地星期
function Get-LocalDays($shift) {
    $names = @('Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday')
    return (1..5 | ForEach-Object { $names[(($_ + $shift) % 7 + 7) % 7] })
}

$triggers = @()
$summary = @()
foreach ($t in $beijingTargets) {
    $c = ConvertTo-LocalTrigger $t.h $t.m
    $days = Get-LocalDays $c.DayShift
    $timeStr = $c.Local.ToString('HH:mm')
    $triggers += New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At $timeStr
    $summary += ("  北京 {0:00}:{1:00} 周一~周五  →  本地 {2} {3}" -f `
                 $t.h, $t.m, ($days[0] + "~" + $days[-1]), $timeStr)
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`"" `
    -WorkingDirectory $PSScriptRoot

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers `
    -Settings $settings -Description "抓取标普500/纳指100 QDII基金申购额度并推送 GitHub Pages" -Force | Out-Null

Write-Output "已注册计划任务 '$taskName'"
Write-Output "本机时区：$($localTz.Id)"
Write-Output "触发时刻换算："
$summary | ForEach-Object { Write-Output $_ }
Write-Output ""
Write-Output "（错过的时间点会在开机后自动补跑；抓取残缺时脚本会中止并保留上次数据）"
Write-Output "（本地夏令时切换后重跑本脚本，可将触发时刻重新对准北京时间）"
