# -*- coding: utf-8 -*-
"""
每日抓取中国境内跟踪 标普500(SPX) / 纳斯达克100(NDX100) 的 QDII 基金数据，
生成 docs/data.json 供静态页面展示。

数据来源：天天基金/东方财富公开接口
  - 全量基金代码表:  fund.eastmoney.com/js/fundcode_search.js
  - 基金基本信息:    fundmobapi FundMNBasicInformation (申购状态/限购/净值/规模/指数代码)
  - 基金费率明细:    fundmobapi FundMNDetailInformation (管理费/托管费/销售服务费)
  - 年化跟踪误差:    fundf10.eastmoney.com/tsdata_{code}.html
  - 场内 ETF 行情:   push2.eastmoney.com/api/qt/stock/get

用法:  python scraper/fetch_data.py
输出:  docs/data.json
"""
import json
import re
import sys
import time
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
OUT_FILE = ROOT / "docs" / "data.json"

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
MOB_PARAMS = {"deviceid": "wap", "plat": "Wap", "product": "EFund", "version": "6.2.8"}

# 只保留精确跟踪这两个指数的基金
TARGET_INDEXES = {"SPX": "标普500", "NDX100": "纳指100"}

# 名称粗筛（后续用 INDEXCODE 精筛），排除美元份额
NAME_PATTERN = re.compile(r"标普|标准普尔|纳斯达克|纳指")
EXCLUDE_PATTERN = re.compile(r"美元|美汇|美钞")

# 综合得分权重（四项均分，可自行调整，和为 1）
WEIGHTS = {"fee": 0.25, "tracking_error": 0.25, "scale": 0.25, "limit": 0.25}

# 无限购上限的基金，限额按此金额（元）参与打分
NO_LIMIT_CAP = 1_000_000

session = requests.Session()
session.headers.update(HEADERS)


def get_json(url, params=None, retries=3):
    for i in range(retries):
        try:
            r = session.get(url, params=params, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(1.5 * (i + 1))


def get_text(url, retries=3):
    for i in range(retries):
        try:
            r = session.get(url, timeout=20)
            r.raise_for_status()
            r.encoding = "utf-8"
            return r.text
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(1.5 * (i + 1))


def load_all_fund_codes():
    """全量基金列表 [code, 拼音缩写, 名称, 类型, 拼音全拼]"""
    text = get_text("https://fund.eastmoney.com/js/fundcode_search.js")
    m = re.search(r"\[\[.*\]\]", text, re.S)
    data = json.loads(m.group(0))
    return [(row[0], row[2], row[3]) for row in data]


def pick_candidates(all_funds):
    out = []
    for code, name, ftype in all_funds:
        if NAME_PATTERN.search(name) and not EXCLUDE_PATTERN.search(name):
            out.append(code)
    return out


def fetch_basic(code):
    d = get_json(
        "https://fundmobapi.eastmoney.com/FundMNewApi/FundMNBasicInformation",
        params={"FCODE": code, **MOB_PARAMS},
    )
    return d.get("Datas") or {}


def fetch_detail(code):
    d = get_json(
        "https://fundmobapi.eastmoney.com/FundMNewApi/FundMNDetailInformation",
        params={"FCODE": code, **MOB_PARAMS},
    )
    return d.get("Datas") or {}


def parse_pct(s):
    """'0.50%（每年）' / '1.30%' -> 0.50"""
    if not s:
        return None
    m = re.search(r"([\d.]+)\s*%", str(s))
    return float(m.group(1)) if m else None


def parse_limit(basic):
    """返回 (状态文本, 每日限购金额元 or None, 限购类型)
    限购类型: suspended=暂停申购, limited=限额已知, unknown=限额未知,
             open=开放申购不限额, exchange=场内交易
    """
    sgzt = (basic.get("SGZT") or "").strip()
    mark = (basic.get("SGZTMARK") or "").strip()
    if "暂停" in sgzt:
        return sgzt, 0, "suspended"
    limit = None
    m = re.search(r"上限\s*([\d,.]+)\s*(万)?元", mark)
    if m:
        limit = float(m.group(1).replace(",", ""))
        if m.group(2):
            limit *= 10000
    elif basic.get("MAXSG") not in (None, "", "--"):
        try:
            limit = float(basic["MAXSG"])
        except ValueError:
            limit = None
    if "限" in sgzt:
        return sgzt, limit, ("limited" if limit is not None else "unknown")
    if "场内" in sgzt:
        return sgzt, None, "exchange"
    if "开放" in sgzt:
        return sgzt, None, "open"
    return sgzt or "未知", limit, ("limited" if limit is not None else "unknown")


def fetch_tracking_error(code):
    """年化跟踪误差 (%, float) from F10 特色数据页"""
    try:
        html = get_text(f"https://fundf10.eastmoney.com/tsdata_{code}.html")
        idx = html.find("年化跟踪误差")
        if idx < 0:
            return None
        seg = re.sub(r"<[^>]+>", "|", html[idx: idx + 600])
        m = re.search(r"([\d.]+)%", seg)
        return float(m.group(1)) if m else None
    except Exception:
        return None


def fetch_etf_quotes_batch(secids):
    """一次批量拉取全部场内行情，返回 {code: (现价, 涨跌幅)}"""
    out = {}
    if not secids:
        return out
    hosts = ["1.push2.eastmoney.com", "2.push2.eastmoney.com",
             "push2delay.eastmoney.com", "push2.eastmoney.com"]
    for i, host in enumerate(hosts):
        try:
            r = requests.get(
                f"https://{host}/api/qt/ulist.np/get",
                params={"secids": ",".join(secids), "fields": "f2,f3,f12", "fltt": 2},
                headers={**HEADERS, "Connection": "close"}, timeout=20)
            r.raise_for_status()
            for q in (r.json().get("data") or {}).get("diff") or []:
                price = q.get("f2")
                out[q.get("f12")] = (
                    float(price) if isinstance(price, (int, float)) else None,
                    q.get("f3") if isinstance(q.get("f3"), (int, float)) else None,
                )
            return out
        except Exception as e:
            if i == len(hosts) - 1:
                print(f"   [warn] ETF批量行情失败: {e}")
            else:
                time.sleep(1.0)
    return out


def build_fund(code):
    basic = fetch_basic(code)
    index_code = (basic.get("INDEXCODE") or "").strip()
    if index_code not in TARGET_INDEXES:
        return None
    name = basic.get("SHORTNAME") or ""
    if EXCLUDE_PATTERN.search(name):
        return None

    detail = fetch_detail(code)
    mgr = parse_pct(detail.get("MGREXP"))
    trust = parse_pct(detail.get("TRUSTEXP"))
    sales = parse_pct(detail.get("SALESEXP"))
    annual_fee = None
    if mgr is not None or trust is not None or sales is not None:
        annual_fee = round((mgr or 0) + (trust or 0) + (sales or 0), 4)

    status, limit, limit_type = parse_limit(basic)
    is_etf = str(basic.get("ISEXCHG")) == "1"
    if is_etf and limit_type == "unknown":
        limit_type = "exchange"

    def to_float(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    fund = {
        "code": code,
        "name": name,
        "index": index_code,
        "is_etf": is_etf,
        "status": status,
        "status_mark": (basic.get("SGZTMARK") or "").strip(),
        "daily_limit": limit,          # 元; None 含义见 limit_type
        "limit_type": limit_type,      # suspended/limited/unknown/open/exchange
        "annual_fee": annual_fee,      # 管理+托管+销售服务, %/年
        "buy_fee": parse_pct(basic.get("RATE")),        # 折后申购费 %
        "scale": to_float(basic.get("ENDNAV")),  # 元
        "scale_date": basic.get("FEGMRQ"),
        "nav": to_float(basic.get("DWJZ")),
        "nav_date": basic.get("FSRQ"),
        "tracking_error": fetch_tracking_error(code),   # %/年
        "company": basic.get("JJGS"),
    }
    if is_etf:
        fund["listtexch"] = basic.get("LISTTEXCH")
        fund["price"] = None
        fund["price_chg"] = None
        fund["premium"] = None
    return fund


def fill_etf_quotes(funds):
    """批量抓取场内行情，并计算相对昨日净值的参考溢价率"""
    etfs = [f for f in funds if f["is_etf"]]
    secids = []
    for f in etfs:
        market = "1" if str(f.pop("listtexch", None)) == "1" else "0"  # 1=上交所 0=深交所
        secids.append(f"{market}.{f['code']}")
    quotes = fetch_etf_quotes_batch(secids)
    for f in etfs:
        price, chg = quotes.get(f["code"], (None, None))
        f["price"] = price
        f["price_chg"] = chg
        nav = f.get("nav")
        if price and nav:
            f["premium"] = round((price / nav - 1) * 100, 2)


def minmax_score(value, lo, hi, reverse=False):
    """归一化到 0~100；reverse=True 表示值越小分越高"""
    if value is None:
        return None
    if hi <= lo:
        return 50.0
    x = (value - lo) / (hi - lo)
    x = min(max(x, 0.0), 1.0)
    return round((1 - x if reverse else x) * 100, 1)


def add_scores(funds):
    """在同指数、同类型(场外/场内)分组内做归一化打分"""
    import math

    def group_key(f):
        return (f["index"], f["is_etf"])

    groups = {}
    for f in funds:
        groups.setdefault(group_key(f), []).append(f)

    for _, fs in groups.items():
        fees = [f["annual_fee"] for f in fs if f["annual_fee"] is not None]
        errs = [f["tracking_error"] for f in fs if f["tracking_error"] is not None]
        scales = [math.log10(f["scale"]) for f in fs
                  if isinstance(f["scale"], (int, float)) and f["scale"] > 0]
        def effective_limit(f):
            """打分用限额(元): 开放=封顶值, 暂停=0, 限额已知=金额, 未知/场内=None(不参与)"""
            t = f["limit_type"]
            if t == "open":
                return NO_LIMIT_CAP
            if t == "suspended":
                return 0.0
            if t == "limited":
                return min(f["daily_limit"], NO_LIMIT_CAP)
            return None

        log_limits = [math.log10(effective_limit(f) + 1) for f in fs
                      if effective_limit(f) is not None]

        for f in fs:
            s_fee = minmax_score(f["annual_fee"], min(fees), max(fees), reverse=True) if fees else None
            s_err = minmax_score(f["tracking_error"], min(errs), max(errs), reverse=True) if errs else None
            s_scale = None
            if scales and isinstance(f["scale"], (int, float)) and f["scale"] > 0:
                s_scale = minmax_score(math.log10(f["scale"]), min(scales), max(scales))
            lim = effective_limit(f)
            s_lim = (minmax_score(math.log10(lim + 1), min(log_limits), max(log_limits))
                     if lim is not None and log_limits else None)
            if f["is_etf"]:
                s_lim = None  # 场内买卖不受申购限额影响

            parts, wsum = 0.0, 0.0
            for s, w in [(s_fee, WEIGHTS["fee"]), (s_err, WEIGHTS["tracking_error"]),
                         (s_scale, WEIGHTS["scale"]), (s_lim, WEIGHTS["limit"])]:
                if s is not None:
                    parts += s * w
                    wsum += w
            f["scores"] = {"fee": s_fee, "tracking_error": s_err, "scale": s_scale, "limit": s_lim}
            f["score"] = round(parts / wsum, 1) if wsum > 0 else None


def main():
    t0 = time.time()
    print("1) 下载全量基金代码表 ...")
    all_funds = load_all_fund_codes()
    candidates = pick_candidates(all_funds)
    print(f"   共 {len(all_funds)} 只基金, 名称粗筛得到候选 {len(candidates)} 只")

    print("2) 拉取基金明细并按跟踪指数精筛 (SPX / NDX100) ...")
    funds = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(build_fund, c): c for c in candidates}
        for fut in as_completed(futures):
            code = futures[fut]
            try:
                f = fut.result()
                if f:
                    funds.append(f)
            except Exception as e:
                print(f"   [warn] {code}: {e}")

    print(f"   命中 {len(funds)} 只 (场外 {sum(1 for f in funds if not f['is_etf'])}, "
          f"场内ETF {sum(1 for f in funds if f['is_etf'])})")

    print("3) 抓取场内 ETF 行情 ...")
    fill_etf_quotes(funds)

    print("4) 计算综合得分 ...")
    add_scores(funds)
    funds.sort(key=lambda f: (f["index"], f["is_etf"], -(f["score"] or -1)))

    out = {
        "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "weights": WEIGHTS,
        "indexes": TARGET_INDEXES,
        "funds": funds,
    }
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(out, ensure_ascii=False, indent=1)
    OUT_FILE.write_text(payload, encoding="utf-8")
    # data.js: 供本地 file:// 直接打开页面使用（fetch 在 file:// 下被浏览器禁止）
    (OUT_FILE.parent / "data.js").write_text(
        "window.FUND_DATA = " + payload + ";\n", encoding="utf-8")
    print(f"5) 已写入 {OUT_FILE} 及 data.js ，耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
