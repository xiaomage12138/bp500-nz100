# -*- coding: utf-8 -*-
"""
每日抓取中国境内跟踪 标普500(SPX) / 纳斯达克100(NDX100) 的 QDII 基金数据，
生成 docs/data.json 供静态页面展示。

数据来源（均为天天基金/东方财富公开接口）:
  - 全量基金代码表:  fund.eastmoney.com/js/fundcode_search.js
  - 基金基本信息:    fundmobapi FundMNBasicInformation (申购状态/限购/净值/规模/指数代码)
  - 基金费率明细:    fundmobapi FundMNDetailInformation (管理费/托管费/销售服务费)
  - 申购状态交叉验证: fundapi fundtradenew.aspx (批量，独立于上面的接口)
  - 年化跟踪误差:    fundf10.eastmoney.com/tsdata_{code}.html  ← 该域名对并发敏感，必须限速
  - 场内行情:        push2 ulist.np/get (ETF 与 LOF)

设计要点:
  1. 申购额度是本项目的核心数据，必须是当日最新：多次重试 + 批量接口交叉验证 +
     与上次结果对比记录变动；任何一环残缺就中止本次发布，绝不publish旧数据充新。
  2. 缺失的指标不会通过“权重重分配”变相加分——改用同组中位数补位并显式标注。

用法:  python scraper/fetch_data.py       (失败时返回非 0，refresh.ps1 会跳过提交)
输出:  docs/data.json, docs/data.js, docs/history.json
"""
import json
import math
import re
import sys
import time
import datetime
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
OUT_FILE = ROOT / "docs" / "data.json"
HISTORY_FILE = ROOT / "docs" / "history.json"

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
MOB_PARAMS = {"deviceid": "wap", "plat": "Wap", "product": "EFund", "version": "6.2.8"}
PUSH_HOSTS = ["1.push2.eastmoney.com", "2.push2.eastmoney.com",
              "push2delay.eastmoney.com", "push2.eastmoney.com"]

TARGET_INDEXES = {"SPX": "标普500", "NDX100": "纳指100"}

# 名称粗筛（后续用官方 INDEXCODE 精筛）。美元/现汇份额需外汇账户，不在监控范围。
NAME_PATTERN = re.compile(r"标普|标准普尔|纳斯达克|纳指")
EXCLUDE_PATTERN = re.compile(r"美元|美汇|美钞")
# 场内证券简称里出现这些词，才认定该代码确为本基金的场内份额（排除同号债券）
EXCHANGE_NAME_PATTERN = re.compile(r"纳斯达克|纳指|标普|标准普尔")

WEIGHTS = {"fee": 0.25, "tracking_error": 0.25, "scale": 0.25, "limit": 0.25}
NO_LIMIT_CAP = 1_000_000          # 不限购的基金按此金额参与限额打分
MIN_FUNDS_FLOOR = 50              # 收录数量低于此值一定是抓取出了问题
MIN_FUNDS_RATIO = 0.90            # 或低于上次成功结果的九成
F10_WORKERS = 2                   # fundf10 域名限流严格，必须低并发
F10_DELAY = 0.35                  # 每次请求之间的间隔（秒）

# 中国自 1991 年起不再实行夏令时，北京时间恒为 UTC+8，用固定偏移即可精确换算。
# 机器可能不在中国，而额度属于哪个交易日只能由北京时间判定，故两个时间都记录。
BEIJING_TZ = datetime.timezone(datetime.timedelta(hours=8))

session = requests.Session()
session.headers.update(HEADERS)


class DataIncomplete(Exception):
    """数据残缺，本次不应发布。"""


# --------------------------------------------------------------------------
# 基础请求
# --------------------------------------------------------------------------
def get_json(url, params=None, retries=4, backoff=1.5):
    last = None
    for i in range(retries):
        try:
            r = session.get(url, params=params, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            if i < retries - 1:
                time.sleep(backoff * (i + 1))
    raise last


def get_text(url, retries=4, backoff=1.5, session_=None):
    sess = session_ or session
    last = None
    for i in range(retries):
        try:
            r = sess.get(url, timeout=20)
            r.raise_for_status()
            r.encoding = "utf-8"
            return r.text
        except Exception as e:
            last = e
            if i < retries - 1:
                time.sleep(backoff * (i + 1))
    raise last


# --------------------------------------------------------------------------
# 基金发现
# --------------------------------------------------------------------------
def load_all_fund_codes():
    text = get_text("https://fund.eastmoney.com/js/fundcode_search.js")
    data = json.loads(re.search(r"\[\[.*\]\]", text, re.S).group(0))
    return [(row[0], row[2], row[3]) for row in data]


def pick_candidates(all_funds):
    return [code for code, name, _ in all_funds
            if NAME_PATTERN.search(name) and not EXCLUDE_PATTERN.search(name)]


def fetch_basic(code):
    d = get_json("https://fundmobapi.eastmoney.com/FundMNewApi/FundMNBasicInformation",
                 params={"FCODE": code, **MOB_PARAMS})
    return d.get("Datas") or {}


def fetch_detail(code):
    d = get_json("https://fundmobapi.eastmoney.com/FundMNewApi/FundMNDetailInformation",
                 params={"FCODE": code, **MOB_PARAMS})
    return d.get("Datas") or {}


# --------------------------------------------------------------------------
# 申购额度解析
# --------------------------------------------------------------------------
def parse_pct(s):
    """'0.50%（每年）' -> 0.50"""
    if not s:
        return None
    m = re.search(r"([\d.]+)\s*%", str(s))
    return float(m.group(1)) if m else None


def parse_limit_amount(text):
    """从『单日累计购买上限X元』中取出金额（元）"""
    if not text:
        return None
    m = re.search(r"上限\s*([\d,.]+)\s*(万)?元", str(text))
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    return v * 10000 if m.group(2) else v


def parse_limit(basic):
    """返回 (状态文本, 每日限额元, 限额类型)

    限额类型: suspended=暂停申购 / limited=限额已知 / unknown=限额未公布
             / open=开放不限额 / exchange=场内交易
    金额优先取结构化字段 TRADEMARKLIST，回退到 SGZTMARK 文本。
    """
    sgzt = (basic.get("SGZT") or "").strip()
    marks = basic.get("TRADEMARKLIST") or []
    mark_text = " ".join(marks) if isinstance(marks, list) else str(marks)
    sgzt_mark = (basic.get("SGZTMARK") or "").strip()

    if "暂停" in sgzt:
        return sgzt, 0, "suspended"

    limit = parse_limit_amount(mark_text)
    if limit is None:
        limit = parse_limit_amount(sgzt_mark)
    if limit is None and basic.get("MAXSG") not in (None, "", "--"):
        try:
            limit = float(basic["MAXSG"])
        except (TypeError, ValueError):
            limit = None

    if "限" in sgzt:
        return sgzt, limit, ("limited" if limit is not None else "unknown")
    if "场内" in sgzt:
        return sgzt, None, "exchange"
    if "开放" in sgzt:
        return sgzt, None, "open"
    return sgzt or "未知", limit, ("limited" if limit is not None else "unknown")


def fetch_trade_status_map():
    """批量拉取交易端申购状态，作为独立于 FundMNBasicInformation 的第二数据源。

    返回 {code: True/False}，True = 可申购。字段 [22] 为可申购标志。
    """
    out = {}
    for pi in range(1, 8):
        try:
            r = session.get("https://fundapi.eastmoney.com/fundtradenew.aspx",
                            params={"ft": "qdii", "sc": "1n", "st": "desc", "pi": pi, "pn": 100,
                                    "cp": "", "ct": "", "cd": "", "ms": "", "fr": "", "plevel": "",
                                    "fst": "", "ftype": "", "fr1": "", "fl": 0, "isab": 1},
                            headers={**HEADERS, "Referer": "https://fund.eastmoney.com/trade/"},
                            timeout=25)
            r.raise_for_status()
            m = re.search(r"datas:\[(.*?)\],allRecords", r.text, re.S)
            if not m:
                break
            items = re.findall(r'"([^"]*)"', m.group(1))
            if not items:
                break
            for it in items:
                p = it.split("|")
                if len(p) > 22 and p[0]:
                    out[p[0]] = (p[22] == "1")
        except Exception as e:
            print(f"   [warn] 交叉验证接口第 {pi} 页失败: {e}")
            break
    return out


# --------------------------------------------------------------------------
# 跟踪误差（fundf10 限流严格：低并发 + 间隔 + 重试，失败必须显式暴露）
# --------------------------------------------------------------------------
def fetch_tracking_error(code):
    """年化跟踪误差 (%)。返回 (值, 是否确定)。抓取失败抛异常，绝不静默返回 None。"""
    sess = requests.Session()
    sess.headers.update({**HEADERS, "Connection": "close"})
    last = None
    for i in range(4):
        try:
            html = get_text(f"https://fundf10.eastmoney.com/tsdata_{code}.html",
                            retries=1, session_=sess)
            idx = html.find("年化跟踪误差")
            if idx < 0:
                # 页面正常但确实没有该指标（新基金不足一年）→ 不是抓取失败
                if "跟踪指数" in html or "特色数据" in html:
                    return None, True
                raise ValueError("页面内容异常（疑似被限流）")
            seg = re.sub(r"<[^>]+>", "|", html[idx: idx + 600])
            m = re.search(r"([\d.]+)%", seg)
            if not m:
                raise ValueError("未能解析跟踪误差数值")
            return float(m.group(1)), True
        except Exception as e:
            last = e
            time.sleep(1.2 * (i + 1))
        finally:
            if i == 3:
                sess.close()
    raise RuntimeError(f"跟踪误差抓取失败: {last}")


def fill_tracking_errors(funds):
    """限速抓取跟踪误差。返回失败的基金代码列表。"""
    failed = []
    lock_delay = [0.0]

    def one(f):
        time.sleep(F10_DELAY)
        try:
            te, _ = fetch_tracking_error(f["code"])
            f["tracking_error"] = te
            return None
        except Exception as e:
            f["tracking_error"] = None
            return (f["code"], str(e)[:70])

    with ThreadPoolExecutor(max_workers=F10_WORKERS) as ex:
        for fut in as_completed([ex.submit(one, f) for f in funds]):
            r = fut.result()
            if r:
                failed.append(r)
                print(f"   [warn] 跟踪误差 {r[0]}: {r[1]}")
    return failed


# --------------------------------------------------------------------------
# 场内行情（ETF 与 LOF）
# --------------------------------------------------------------------------
def fetch_quotes_batch(secids):
    """批量行情，返回 {code: (现价, 涨跌幅, 证券简称)}"""
    out = {}
    if not secids:
        return out
    for i, host in enumerate(PUSH_HOSTS):
        try:
            r = requests.get(f"https://{host}/api/qt/ulist.np/get",
                             params={"secids": ",".join(secids),
                                     "fields": "f2,f3,f12,f14", "fltt": 2},
                             headers={**HEADERS, "Connection": "close"}, timeout=25)
            r.raise_for_status()
            for q in (r.json().get("data") or {}).get("diff") or []:
                price = q.get("f2")
                out[q.get("f12")] = (
                    float(price) if isinstance(price, (int, float)) else None,
                    q.get("f3") if isinstance(q.get("f3"), (int, float)) else None,
                    q.get("f14") or "",
                )
            return out
        except Exception as e:
            if i == len(PUSH_HOSTS) - 1:
                print(f"   [warn] 场内行情批量请求失败: {e}")
            else:
                time.sleep(1.0)
    return out


def fill_etf_quotes(funds):
    etfs = [f for f in funds if f["is_etf"]]
    secids = []
    for f in etfs:
        market = "1" if str(f.pop("listtexch", None)) == "1" else "0"
        secids.append(f"{market}.{f['code']}")
    quotes = fetch_quotes_batch(secids)
    missing = []
    for f in etfs:
        price, chg, _ = quotes.get(f["code"], (None, None, ""))
        f["price"], f["price_chg"] = price, chg
        if price and f.get("nav"):
            f["premium"] = round((price / f["nav"] - 1) * 100, 2)
        else:
            missing.append(f["code"])
    return missing


def fill_lof_quotes(funds):
    """识别场外基金里实际可在交易所买卖的 LOF，附上场内价与溢价。

    以交易所返回的证券简称是否含指数关键词为准，避免与同号债券混淆。
    """
    otc = [f for f in funds if not f["is_etf"]]
    secids = []
    for f in otc:
        secids += [f"0.{f['code']}", f"1.{f['code']}"]
    quotes = {}
    for i in range(0, len(secids), 40):
        quotes.update(fetch_quotes_batch(secids[i:i + 40]))
        time.sleep(0.4)

    found = 0
    for f in otc:
        price, chg, nm = quotes.get(f["code"], (None, None, ""))
        if not (nm and EXCHANGE_NAME_PATTERN.search(nm)):
            continue
        if not (isinstance(price, (int, float)) and price > 0):
            continue
        f["lof_name"] = nm
        f["lof_price"] = price
        f["lof_chg"] = chg
        if f.get("nav"):
            f["lof_premium"] = round((price / f["nav"] - 1) * 100, 2)
        found += 1
    return found


# --------------------------------------------------------------------------
# 组装
# --------------------------------------------------------------------------
def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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

    return {
        "code": code,
        "name": name,
        "index": index_code,
        "is_etf": is_etf,
        "status": status,
        "status_mark": (basic.get("SGZTMARK") or "").strip(),
        "daily_limit": limit,
        "limit_type": limit_type,
        "limit_crosscheck": None,      # confirmed / mismatch / unavailable
        "annual_fee": annual_fee,
        "buy_fee": parse_pct(basic.get("RATE")),
        "scale": to_float(basic.get("ENDNAV")),
        "scale_date": basic.get("FEGMRQ"),
        "nav": to_float(basic.get("DWJZ")),
        "nav_date": basic.get("FSRQ"),
        "tracking_error": None,        # 稍后限速填充
        "company": basic.get("JJGS"),
        "listtexch": basic.get("LISTTEXCH"),
    }


def crosscheck_limits(funds, trade_map):
    """用交易端批量接口验证申购/暂停状态。返回 (确认数, 冲突列表)"""
    conflicts = []
    for f in funds:
        if f["is_etf"]:
            f["limit_crosscheck"] = "n/a"
            continue
        buyable_2nd = trade_map.get(f["code"])
        if buyable_2nd is None:
            f["limit_crosscheck"] = "unavailable"
            continue
        buyable_1st = f["limit_type"] in ("limited", "open", "unknown")
        if buyable_1st == buyable_2nd:
            f["limit_crosscheck"] = "confirmed"
        else:
            f["limit_crosscheck"] = "mismatch"
            conflicts.append((f["code"], f["name"], f["status"], buyable_2nd))
    confirmed = sum(1 for f in funds if f["limit_crosscheck"] == "confirmed")
    return confirmed, conflicts


# --------------------------------------------------------------------------
# 打分（缺失项用同组中位数补位，绝不通过权重重分配变相加分）
# --------------------------------------------------------------------------
def minmax_score(value, lo, hi, reverse=False):
    if value is None:
        return None
    if hi <= lo:
        return 50.0
    x = min(max((value - lo) / (hi - lo), 0.0), 1.0)
    return round((1 - x if reverse else x) * 100, 1)


def add_scores(funds):
    groups = {}
    for f in funds:
        groups.setdefault((f["index"], f["is_etf"]), []).append(f)

    for _, fs in groups.items():
        fees = [f["annual_fee"] for f in fs if f["annual_fee"] is not None]
        errs = [f["tracking_error"] for f in fs if f["tracking_error"] is not None]
        scales = [math.log10(f["scale"]) for f in fs
                  if isinstance(f["scale"], (int, float)) and f["scale"] > 0]

        def effective_limit(f):
            t = f["limit_type"]
            if t == "open":
                return NO_LIMIT_CAP
            if t == "suspended":
                return 0.0
            if t == "limited":
                return min(f["daily_limit"], NO_LIMIT_CAP)
            return None

        lims = [effective_limit(f) for f in fs]
        log_lims = [math.log10(x + 1) for x in lims if x is not None]

        raw = []
        for f in fs:
            s_fee = minmax_score(f["annual_fee"], min(fees), max(fees), reverse=True) if fees else None
            s_err = minmax_score(f["tracking_error"], min(errs), max(errs), reverse=True) if errs else None
            s_scale = None
            if scales and isinstance(f["scale"], (int, float)) and f["scale"] > 0:
                s_scale = minmax_score(math.log10(f["scale"]), min(scales), max(scales))
            lim = effective_limit(f)
            s_lim = (minmax_score(math.log10(lim + 1), min(log_lims), max(log_lims))
                     if lim is not None and log_lims else None)
            if f["is_etf"]:
                s_lim = None      # 场内买卖不受申购限额约束，该项对 ETF 不适用
            raw.append({"fee": s_fee, "tracking_error": s_err, "scale": s_scale, "limit": s_lim})

        # 各项的同组中位数，用于给缺失值补位（中性，不奖不罚）
        medians = {}
        for k in WEIGHTS:
            vals = [r[k] for r in raw if r[k] is not None]
            medians[k] = statistics.median(vals) if vals else None

        for f, r in zip(fs, raw):
            estimated, parts, wsum = [], 0.0, 0.0
            for k, wt in WEIGHTS.items():
                v = r[k]
                if v is None:
                    if k == "limit" and f["is_etf"]:
                        continue          # ETF 本就不适用该项，不算缺失
                    if medians[k] is None:
                        continue
                    v = medians[k]
                    estimated.append(k)
                parts += v * wt
                wsum += wt
            f["scores"] = r
            f["score_estimated"] = estimated
            f["score"] = round(parts / wsum, 1) if wsum > 0 else None


# --------------------------------------------------------------------------
# 额度变动追踪
# --------------------------------------------------------------------------
def limit_label(f):
    t = f["limit_type"]
    if t == "suspended":
        return "暂停申购"
    if t == "open":
        return "不限额"
    if t == "limited":
        v = f["daily_limit"]
        return f"{v:g} 元/日"
    if t == "exchange":
        return "场内交易"
    return "额度未公布"


def diff_limits(funds, prev_funds):
    """对比上次结果，标出额度变动。返回变动列表。"""
    prev = {p["code"]: p for p in prev_funds}
    changes = []
    for f in funds:
        if f["is_etf"]:
            continue
        p = prev.get(f["code"])
        if not p:
            continue
        same_type = p.get("limit_type") == f["limit_type"]
        same_amt = (p.get("daily_limit") or 0) == (f["daily_limit"] or 0)
        if same_type and same_amt:
            continue

        old_v = p.get("daily_limit")
        new_v = f["daily_limit"]
        if f["limit_type"] == "suspended":
            direction = "suspended"
        elif p.get("limit_type") == "suspended":
            direction = "reopened"
        elif f["limit_type"] == "open":
            direction = "loosened"
        elif p.get("limit_type") == "open":
            direction = "tightened"
        elif old_v is not None and new_v is not None:
            direction = "loosened" if new_v > old_v else "tightened"
        else:
            direction = "changed"

        f["limit_change"] = {
            "direction": direction,
            "prev_label": limit_label(p),
            "curr_label": limit_label(f),
        }
        changes.append({
            "code": f["code"], "name": f["name"], "index": f["index"],
            "direction": direction,
            "from": limit_label(p), "to": limit_label(f),
        })
    return changes


def load_prev():
    if not OUT_FILE.exists():
        return None
    try:
        return json.loads(OUT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def append_history(changes, when):
    hist = {"records": []}
    if HISTORY_FILE.exists():
        try:
            hist = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    for c in changes:
        hist["records"].insert(0, {**c, "at": when})
    hist["records"] = hist["records"][:300]
    HISTORY_FILE.write_text(json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")
    return hist


# --------------------------------------------------------------------------
def main():
    t0 = time.time()
    now = datetime.datetime.now().astimezone()
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    now_bj = now.astimezone(BEIJING_TZ)
    stamp_bj = now_bj.strftime("%Y-%m-%d %H:%M:%S")
    same_tz = now.utcoffset() == now_bj.utcoffset()
    print(f"   采集时刻：北京时间 {stamp_bj}" +
          ("" if same_tz else f"（本机本地时间 {stamp}）"))

    print("1) 下载全量基金代码表 ...")
    all_funds = load_all_fund_codes()
    candidates = pick_candidates(all_funds)
    print(f"   共 {len(all_funds)} 只基金，名称粗筛得到候选 {len(candidates)} 只")

    print("2) 拉取基金明细并按官方跟踪指数精筛 (SPX / NDX100) ...")
    funds, build_failed = [], []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(build_fund, c): c for c in candidates}
        for fut in as_completed(futures):
            code = futures[fut]
            try:
                f = fut.result()
                if f:
                    funds.append(f)
            except Exception as e:
                build_failed.append(code)
                print(f"   [warn] {code} 抓取失败: {str(e)[:70]}")
    n_otc = sum(1 for f in funds if not f["is_etf"])
    n_etf = len(funds) - n_otc
    print(f"   命中 {len(funds)} 只（场外 {n_otc}，场内ETF {n_etf}），失败 {len(build_failed)} 只")

    # --- 数量完整性闸门：宁可不发布，也不发布残缺数据 ---
    prev = load_prev()
    prev_funds = (prev or {}).get("funds", [])
    if len(funds) < MIN_FUNDS_FLOOR:
        raise DataIncomplete(f"仅收录 {len(funds)} 只，低于下限 {MIN_FUNDS_FLOOR}，疑似被限流")
    if prev_funds and len(funds) < len(prev_funds) * MIN_FUNDS_RATIO:
        raise DataIncomplete(
            f"仅收录 {len(funds)} 只，不足上次 {len(prev_funds)} 只的 {MIN_FUNDS_RATIO:.0%}")

    print("3) 交叉验证申购状态（独立的交易端批量接口）...")
    trade_map = fetch_trade_status_map()
    confirmed, conflicts = crosscheck_limits(funds, trade_map)
    print(f"   第二数据源覆盖 {len(trade_map)} 只；场外基金中 {confirmed} 只状态一致，"
          f"冲突 {len(conflicts)} 只")
    for c in conflicts:
        print(f"   [!] 状态冲突 {c[0]} {c[1][:24]}：主源={c[2]}，交易端可申购={c[3]}")

    print("4) 抓取年化跟踪误差（fundf10 限速模式）...")
    te_failed = fill_tracking_errors(funds)
    have_te = sum(1 for f in funds if f["tracking_error"] is not None)
    print(f"   取得 {have_te}/{len(funds)} 只；抓取失败 {len(te_failed)} 只")

    print("5) 抓取场内行情（ETF + LOF）...")
    etf_missing = fill_etf_quotes(funds)
    n_lof = fill_lof_quotes(funds)
    print(f"   ETF 行情缺失 {len(etf_missing)} 只；识别出场内可交易 LOF {n_lof} 只")

    print("6) 计算综合得分（缺失项用同组中位数补位）...")
    add_scores(funds)
    n_est = sum(1 for f in funds if f.get("score_estimated"))
    if n_est:
        print(f"   [!] {n_est} 只基金存在补位估算项，页面将显式标注")

    print("7) 对比上次结果，记录额度变动 ...")
    changes = diff_limits(funds, prev_funds) if prev_funds else []
    print(f"   本次额度变动 {len(changes)} 起")
    for c in changes:
        print(f"   * {c['code']} {c['name'][:26]}：{c['from']} → {c['to']}")

    funds.sort(key=lambda f: (f["index"], f["is_etf"], -(f["score"] or -1)))
    for f in funds:
        f.pop("listtexch", None)

    hist = append_history(changes, stamp_bj)   # 变动归属哪个交易日按北京时间记

    unresolved = [f["code"] for f in funds
                  if not f["is_etf"] and f["limit_type"] == "unknown"]
    out = {
        "updated_at": stamp,
        "limit_captured_at": stamp,     # 额度与本次采集同批，页面显式展示
        # 额度属于哪个交易日由北京时间决定；机器不在中国时本地时间会误导，故一并给出
        "updated_at_beijing": stamp_bj,
        "limit_captured_at_beijing": stamp_bj,
        "local_tz_matches_beijing": same_tz,
        "weights": WEIGHTS,
        "indexes": TARGET_INDEXES,
        "funds": funds,
        "changes": changes,
        "history": hist["records"][:40],
        "quality": {
            "fund_count": len(funds),
            "build_failed": build_failed,
            "tracking_error_failed": [c for c, _ in te_failed],
            "etf_quote_missing": etf_missing,
            "limit_unresolved": unresolved,
            "limit_crosscheck_confirmed": confirmed,
            "limit_crosscheck_conflicts": [c[0] for c in conflicts],
            "score_estimated_count": n_est,
            "lof_count": n_lof,
        },
    }

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(out, ensure_ascii=False, indent=1)
    OUT_FILE.write_text(payload, encoding="utf-8")
    # data.js 供本地 file:// 直接打开（浏览器禁止 file:// 下的 fetch）
    (OUT_FILE.parent / "data.js").write_text(
        "window.FUND_DATA = " + payload + ";\n", encoding="utf-8")
    print(f"8) 已写入 {OUT_FILE} 及 data.js，耗时 {time.time() - t0:.0f}s")


if __name__ == "__main__":
    try:
        main()
    except DataIncomplete as e:
        print(f"\n[中止] 数据残缺，本次不发布：{e}")
        print("      已保留上一次的 data.json，请稍后重试。")
        sys.exit(1)
    except Exception as e:
        print(f"\n[错误] {type(e).__name__}: {e}")
        sys.exit(1)
