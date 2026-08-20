# 每日刷新：抓取最新数据并推送到 GitHub（Pages 随之更新）
# 手动执行:  powershell -ExecutionPolicy Bypass -File refresh.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python scraper\fetch_data.py
if ($LASTEXITCODE -ne 0) {
    Write-Error "抓取失败，本次不提交"
    exit 1
}

git add docs/data.json docs/data.js

# 无变化则跳过提交
git diff --cached --quiet
if ($LASTEXITCODE -eq 0) {
    Write-Output "数据无变化，跳过提交"
    exit 0
}

git commit -m "data: refresh $(Get-Date -Format 'yyyy-MM-dd HH:mm')"

# 已配置 origin 远程则推送
$remotes = @(git remote)
if ($remotes -contains "origin") {
    git push
} else {
    Write-Output "未配置 origin 远程，仅提交到本地（见 README 的 GitHub 设置步骤）"
}
