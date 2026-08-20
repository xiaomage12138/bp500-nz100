# QDII 额度监控 · 标普500 / 纳指100

每天刷新一次，监控中国境内跟踪 **标普500（SPX）** 和 **纳斯达克100（NDX100）** 的全部 QDII 基金：

- **场外基金**：申购状态（开放 / 限大额 / 暂停）、每日限购金额、年持有成本（管理+托管+销售服务费）、折后申购费、年化跟踪误差、规模
- **场内 ETF**：实时价格、参考溢价率（现价 vs 最近一日净值）、费率、跟踪误差、规模
- 每组内按"值得长期持有"**综合得分**排序（年持有成本 25% + 跟踪误差 25% + 规模 25% + 限额宽松度 25%，场内 ETF 不计限额项），所有列可点击表头重新排序

基金列表**自动发现**：每次刷新从天天基金全量基金表按名称粗筛，再按官方标注的跟踪指数代码（SPX / NDX100）精筛，新发行的基金会自动进入监控，无需手动维护名单。

## 文件结构

```
scraper/fetch_data.py   抓取脚本 → 生成 docs/data.json 和 docs/data.js
docs/index.html         展示页面（GitHub Pages 站点根目录）
docs/data.json          结构化数据（含更新时间）
docs/data.js            同一份数据，供本地双击 index.html 直接查看
refresh.ps1             刷新 + 提交 + 推送，一键脚本
register-task.ps1       注册 Windows 计划任务（周一~周五 11:00 自动刷新）
```

## 使用

**手动刷新**（任何时候想看最新数据）：

```powershell
powershell -ExecutionPolicy Bypass -File refresh.ps1
```

**每日自动刷新**（注册一次即可）：

```powershell
powershell -ExecutionPolicy Bypass -File register-task.ps1
```

**本地查看**：直接双击 `docs/index.html`（无需服务器，数据从 data.js 读取）。

## 发布到 GitHub Pages（一次性设置）

1. 在 GitHub 新建仓库（例如 `bp500-nz100`，Public——Pages 免费版要求公开仓库）
2. 在本目录执行：
   ```powershell
   git remote add origin https://github.com/<你的用户名>/bp500-nz100.git
   git push -u origin main
   ```
3. 仓库 **Settings → Pages → Build and deployment**：Source 选 `Deploy from a branch`，Branch 选 `main`，目录选 `/docs`，保存
4. 约 1 分钟后访问 `https://<你的用户名>.github.io/bp500-nz100/`

之后每次 `refresh.ps1` 推送数据，网页 1~2 分钟内自动更新，链接不变。

## 数据说明与免责声明

- 数据来源：天天基金 / 东方财富公开接口，字段含义以基金公司公告为准
- ETF"参考溢价率"以最近一日净值为基准，含隔夜美股涨跌影响，与盘中实时 IOPV 溢价有差异
- 本项目仅为信息整理，不构成投资建议
