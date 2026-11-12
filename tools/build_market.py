# -*- coding: utf-8 -*-
"""全 A 股行业成分 + 行业每日热度（给 §10.1.1「板块 3 天验证」用）。

为什么需要它：选股池只有 153 只，切到 EM2016 二级行业后每个行业只剩 3~19 只，
「板块内涨停家数、涨跌家数、成交额排名前 3」在这么薄的样本上没有意义
（实测 S3 涨停家数的失败率 98.6%）。手册的「板块」指的是全市场真实行业，
所以这里用全 A 股（约 5100 只）重建行业口径。

数据链条：
  1) data/snapshot.json            全 A 名单（新浪快照；symbol=sh/sz/bj 前缀）
  2) 东财 RPT_F10_BASIC_ORGINFO    EM2016 行业链（40 只/批，约 130 批）
  3) 腾讯前复权日线 90 根          算行业等权指数 / MA20 / 3 日累计 / 成交额排名

产出（都在 data/ 下）：
  market_industry.json  {updated, map: {code: {em2016, industry1, industry2, industry3}}}
  market_bars.json      {updated, count, bars: {code: {d: [...], c: [...], a: [...]}}}
  industry_daily.json   {updated, date, n_stocks, n_industries,
                         members: {code: 行业名},
                         industries: {行业名: {n, chg, up, down, limit_up, breadth,
                                              cum3, rank, rank_3ago, top10_days,
                                              above_ma20, ret5, ma20, level}},
                         amount_rank: {行业名: {code: 名次}}}

用法：
  python tools/build_market.py            # 增量（缺行业补行业、缺K线补K线）
  python tools/build_market.py --refresh  # 全量重抓
  python tools/build_market.py --quotes   # 只用实时行情更新今天这根（快，约 15 秒）
  python tools/build_market.py --stats    # 只重新聚合
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from quant import datafeed as DF  # noqa: E402

DATA = os.path.join(ROOT, "data")
SNAP = os.path.join(DATA, "snapshot.json")
IND_OUT = os.path.join(DATA, "market_industry.json")
BARS_OUT = os.path.join(DATA, "market_bars.json")
DAILY_OUT = os.path.join(DATA, "industry_daily.json")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
      "Referer": "https://emweb.securities.eastmoney.com/"}
EM_API = ("https://datacenter.eastmoney.com/securities/api/data/v1/get"
          "?reportName=RPT_F10_BASIC_ORGINFO&columns=SECUCODE,EM2016,INDUSTRYCSRC1"
          "&pageNumber=1&pageSize=100&source=HSF10&client=PC&filter=")
KLINE_N = 90
WORKERS = 8
MIN_RANK_N = 10  # 成员少于 10 只的行业不参与跨行业排名（样本太小，排名会被噪声主导）


def log(msg):
    print(msg, flush=True)


# --------------------------------------------------------------------------- #
# 1. 全 A 名单
# --------------------------------------------------------------------------- #
def load_universe():
    """从新浪快照拿沪深 A 股（排除北交所 bj），返回 [{code, symbol, name}]。"""
    with open(SNAP, "r", encoding="utf-8") as fh:
        snap = json.load(fh)
    out = []
    for r in snap.get("rows") or []:
        sym = str(r.get("symbol") or "")
        code = str(r.get("code") or "").zfill(6)
        if len(sym) < 8 or not code.isdigit() or len(code) != 6:
            continue
        prefix = sym[:2]
        if prefix not in ("sh", "sz"):  # 北交所 bj 不参与行业广度
            continue
        if code[0] not in "603" and not code.startswith("688"):
            # 只保留 60/00/30/688 开头的股票（排除 B 股等）
            continue
        out.append({"code": code, "symbol": sym, "name": r.get("name") or ""})
    return out


# --------------------------------------------------------------------------- #
# 2. 行业（东财 EM2016）
# --------------------------------------------------------------------------- #
def _em_fetch(codes):
    """一批（<=40 只）取 EM2016；返回 {code: {...}}。"""
    flt = "(SECUCODE in (%s))" % ",".join(
        '"%s.%s"' % (c, "SH" if c[0] in "69" else "SZ") for c in codes)
    url = EM_API + urllib.parse.quote(flt)
    req = urllib.request.Request(url, headers=UA)
    raw = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
    doc = json.loads(raw)
    rows = ((doc.get("result") or {}).get("data")) or []
    out = {}
    for r in rows:
        sec = str(r.get("SECUCODE") or "")
        if "." not in sec:
            continue
        code = sec.split(".")[0].zfill(6)
        chain = str(r.get("EM2016") or "").split("-")
        names = [x for x in chain if x]
        out[code] = {
            "em2016": str(r.get("EM2016") or ""),
            "industry1": names[0] if len(names) > 0 else "",
            "industry2": names[1] if len(names) > 1 else (names[0] if names else ""),
            "industry3": names[2] if len(names) > 2 else "",
            "csrc": str(r.get("INDUSTRYCSRC1") or ""),
        }
    return out


def load_industry_map(universe, refresh=False):
    cache = {"updated": "", "map": {}}
    if os.path.exists(IND_OUT) and not refresh:
        with open(IND_OUT, "r", encoding="utf-8") as fh:
            cache = json.load(fh)
    have = cache.get("map") or {}
    todo = [s["code"] for s in universe if s["code"] not in have]
    if not todo:
        log(f"[行业] 缓存命中 {len(have)} 只")
        return have
    log(f"[行业] 需补 {len(todo)} 只（已有 {len(have)}）…")
    added = 0
    for i in range(0, len(todo), 40):
        batch = todo[i:i + 40]
        for attempt in range(3):
            try:
                got = _em_fetch(batch)
                have.update(got)
                added += len(got)
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 2:
                    log(f"  ! 批次 {i // 40 + 1} 失败：{exc}")
                time.sleep(1.0 + attempt)
        time.sleep(0.05)
    cache["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    cache["map"] = have
    with open(IND_OUT, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False)
    log(f"[行业] 完成：+{added}，共 {len(have)} 只")
    return have


# --------------------------------------------------------------------------- #
# 3. K 线（腾讯前复权 90 根，并行）
# --------------------------------------------------------------------------- #
def _kline(sym):
    param = f"{sym},day,,,{KLINE_N},qfq"
    url = DF.TX_KLINE_URL.format(param=param)
    text = DF.http_get(url, headers=None, retries=2)
    doc = json.loads(text)
    data = (doc.get("data") or {}).get(sym) or {}
    rows = data.get("qfqday") or data.get("day") or []
    d, c, a = [], [], []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 9:
            continue
        date = str(row[0])[:10]
        close = DF._f(row[2])  # noqa: SLF001
        if not date or close is None or close <= 0:
            continue
        amt = DF._f(row[8]) or 0.0  # 万元
        d.append(date)
        c.append(round(close, 3))
        a.append(round(amt, 1))
    return {"d": d, "c": c, "a": a}


def load_bars(universe, refresh=False):
    cache = {"updated": "", "count": 0, "bars": {}}
    if os.path.exists(BARS_OUT) and not refresh:
        with open(BARS_OUT, "r", encoding="utf-8") as fh:
            cache = json.load(fh)
    bars = cache.get("bars") or {}
    todo = [s for s in universe if s["code"] not in bars]
    if not todo:
        log(f"[K线] 缓存命中 {len(bars)} 只")
        return bars
    log(f"[K线] 需抓 {len(todo)} 只（已有 {len(bars)}）…")
    done = 0
    fails = 0
    step = 500

    def _save():
        cache["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        cache["count"] = len(bars)
        cache["bars"] = bars
        with open(BARS_OUT, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = {pool.submit(_kline, s["symbol"]): s["code"] for s in todo}
        for fut in as_completed(futs):
            code = futs[fut]
            try:
                got = fut.result()
                if got["d"]:
                    bars[code] = got
                    done += 1
                else:
                    fails += 1
            except Exception:  # noqa: BLE001
                fails += 1
            if (done + fails) % step == 0:
                _save()
                log(f"  … 已完成 {done} 只，失败 {fails} 只（已存盘）")
    _save()
    log(f"[K线] 完成：+{done}，失败 {fails}，共 {len(bars)} 只")
    return bars


# --------------------------------------------------------------------------- #
# 4. 实时行情补今天（--quotes，快）
# --------------------------------------------------------------------------- #
def update_quotes(bars, universe):
    keys = [s["symbol"] for s in universe if s["code"] in bars]
    log(f"[行情] 抓实时 {len(keys)} 只…")
    quotes = DF.fetch_quotes(keys)
    updated = 0
    for s in universe:
        code = s["code"]
        q = quotes.get(s["symbol"])
        if not code or code not in bars or not q:
            continue
        date = q.get("date")
        if not date or (q.get("price") or 0) <= 0:
            continue
        bar = bars[code]
        rec = {"date": date, "close": round(float(q["price"]), 3),
               "amount": round(float(q.get("amount") or 0.0), 1)}
        if bar["d"] and bar["d"][-1] == date:
            bar["c"][-1] = rec["close"]
            bar["a"][-1] = rec["amount"]
        elif not bar["d"] or date > bar["d"][-1]:
            bar["d"].append(date)
            bar["c"].append(rec["close"])
            bar["a"].append(rec["amount"])
        else:
            continue
        updated += 1
    with open(BARS_OUT, "w", encoding="utf-8") as fh:
        json.dump({"updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "count": len(bars), "bars": bars}, fh, ensure_ascii=False)
    log(f"[行情] 更新 {updated} 只")
    return bars


# --------------------------------------------------------------------------- #
# 5. 聚合行业热度
# --------------------------------------------------------------------------- #
def update_snapshot(universe, bars=None):
    """用实时行情重建 data/snapshot.json（全 A 选股要「当天」的价格/涨跌幅/成交额/PE/PB/市值）。

    为什么不用新浪那 56 页快照：它慢、而且经常滞后一天（date=11-10 但行情已到 11-12），
    会让「全 A 选股」用旧涨跌幅配新 K 线，算出来的量比/换手全是错的。
    腾讯一次能查 120 只，43 个请求就够 5224 只。
    """
    keys = [s["symbol"] for s in universe]
    log(f"[快照] 用实时行情重建 {len(keys)} 只…")
    quotes = DF.fetch_quotes(keys)
    rows, dates = [], []
    for s in universe:
        q = quotes.get(s["symbol"])
        if not q or (q.get("price") or 0) <= 0:
            continue
        date = q.get("date") or ""
        if date:
            dates.append(date)
        amt_wan = q.get("amount") or 0.0
        rows.append({
            "code": s["code"], "symbol": s["symbol"], "name": q.get("name") or s["name"],
            "price": round(float(q["price"]), 3),
            "prev_close": q.get("prev_close"),
            "chg_pct": round(float(q.get("chg_pct") or 0.0), 3),
            "amount": round(float(amt_wan) * 1e4, 0),          # 元
            "turnover": q.get("turnover"),
            "pe": q.get("pe"),
            "pb": q.get("pb"),
            "mktcap_yi": q.get("mktcap"),
            "nmc_yi": q.get("float_mktcap"),
            "volume": q.get("volume"),
            "industry": None,
        })
    if not rows:
        log("  ! 实时行情全部失败，快照保持原样")
        return None
    rows.sort(key=lambda r: r["code"])
    date = max(dates) if dates else time.strftime("%Y-%m-%d")
    doc = {"date": date, "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
           "source": "tencent_quotes", "rows": rows}
    with open(SNAP, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False)
    log(f"[快照] {date}：{len(rows)} 只 → {SNAP}")
    return doc


def _limit_pct(code, name):
    if "ST" in (name or ""):
        return 4.8
    if code.startswith("30") or code.startswith("688"):
        return 19.8
    return 9.8


def aggregate(bars, ind_map, universe):
    if not bars:
        raise SystemExit("没有 K 线数据，先跑 python tools/build_market.py")
    name_of, limit = {}, {}
    for s in universe:
        name_of[s["code"]] = s["name"]
        limit[s["code"]] = _limit_pct(s["code"], s["name"])
    # 每只股票的日收益 / 成交额
    ret, amt = {}, {}
    dates = set()
    for code, bar in bars.items():
        d, c, a = bar.get("d") or [], bar.get("c") or [], bar.get("a") or []
        n = min(len(d), len(c))
        if n < 2:
            continue
        dates.update(d[:n])
        rm = {}
        for i in range(1, n):
            if c[i - 1] > 0:
                rm[d[i]] = (c[i] / c[i - 1] - 1.0) * 100.0
        ret[code] = rm
        amt[code] = {d[i]: a[i] if i < len(a) else 0.0 for i in range(n)}
    all_dates = sorted(dates)
    if not all_dates:
        raise SystemExit("K 线日期为空")
    last = all_dates[-1]
    window = all_dates[-KLINE_N:]

    # 分组：行业二级 → 成员
    groups, members = {}, {}
    for s in universe:
        code = s["code"]
        info = ind_map.get(code) or {}
        ind = info.get("industry2") or info.get("industry1") or "未分类"
        if not ind or ind == "未分类":
            continue
        if code not in ret:
            continue
        groups.setdefault(ind, []).append(code)
        members[code] = ind

    industries, ranked = {}, {}
    for ind, codes in groups.items():
        if len(codes) < 3:
            continue
        level, chg, ups, downs, limits = [], [], [], [], []
        cur = 100.0
        for d in window:
            vals = [ret[c][d] for c in codes if d in ret[c]]
            if not vals:
                level.append(round(cur, 3))
                chg.append(None)
                ups.append(0)
                downs.append(0)
                limits.append(0)
                continue
            mean = sum(vals) / len(vals)
            cur *= (1.0 + mean / 100.0)
            level.append(round(cur, 3))
            chg.append(round(mean, 3))
            ups.append(sum(1 for v in vals if v > 0))
            downs.append(sum(1 for v in vals if v < 0))
            limits.append(sum(1 for c in codes
                              if d in ret[c] and ret[c][d] >= limit.get(c, 9.8)))
        n_ok = len(level)
        ma20 = round(sum(level[-20:]) / 20, 3) if n_ok >= 20 else None
        breadth = round(ups[-1] / downs[-1], 2) if downs[-1] else (99.0 if ups[-1] else 0.0)
        industries[ind] = {
            "n": len(codes),
            "chg": chg[-1],
            "up": ups[-1],
            "down": downs[-1],
            "limit_up": limits[-1],
            "breadth": breadth,
            "cum3": round(sum(x for x in chg[-3:] if x is not None), 2) if n_ok >= 3 else None,
            "above_ma20": int(level[-1] > ma20) if ma20 else None,
            "ma20": ma20,
            "level": level[-1],
            "ret5": round((level[-1] / level[-6] - 1.0) * 100, 2) if n_ok >= 6 else None,
            "eligible": len(codes) >= MIN_RANK_N,
            "dates": window,
            "chg_series": chg,
        }
        ranked.setdefault(last, {})[ind] = chg[-1]

    # 历史排名（近 6 个交易日）+ 近 3 日上榜次数；只有成员≥MIN_RANK_N 的行业参与排名，
    # 否则 3~6 只的小行业（渔业/陶瓷等）会因为样本太小长期占据榜首，排名失去意义。
    rank_hist = {}
    eligible_inds = [ind for ind, it in industries.items() if it["eligible"]]
    for d in all_dates[-6:]:
        vals = []
        for ind in eligible_inds:
            it = industries[ind]
            i = window.index(d) if d in window else -1
            if i < 0:
                continue
            v = it["chg_series"][i]
            if v is not None:
                vals.append((v, ind))
        vals.sort(key=lambda x: -x[0])
        rank_hist[d] = {ind: i + 1 for i, (_, ind) in enumerate(vals)}
    for ind, it in industries.items():
        eligible = it["eligible"]
        it["rank"] = rank_hist.get(last, {}).get(ind) if eligible else None
        r3 = all_dates[-4] if len(all_dates) >= 4 else None
        it["rank_3ago"] = rank_hist.get(r3, {}).get(ind) if (r3 and eligible) else None
        n_top = 0
        if eligible:
            for d in all_dates[-3:]:
                if rank_hist.get(d, {}).get(ind, 999) <= 10:
                    n_top += 1
        it["top10_days"] = n_top
        it.pop("chg_series", None)

    # 今日成交额排名（行业内部）
    amount_rank = {}
    for ind, codes in groups.items():
        today = [(amt.get(c, {}).get(last) or 0.0, c) for c in codes]
        today.sort(key=lambda x: -x[0])
        amount_rank[ind] = {c: i + 1 for i, (_, c) in enumerate(today) if _ > 0}

    daily = {
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "date": last,
        "n_stocks": len(members),
        "n_industries": len(industries),
        "members": members,
        "industries": industries,
        "amount_rank": amount_rank,
    }
    with open(DAILY_OUT, "w", encoding="utf-8") as fh:
        json.dump(daily, fh, ensure_ascii=False)
    log(f"[聚合] {last}：{len(members)} 只 / {len(industries)} 个行业 → {DAILY_OUT}")
    return daily


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    refresh = "--refresh" in argv
    quotes_only = "--quotes" in argv
    stats_only = "--stats" in argv
    universe = load_universe()
    log(f"[名单] 沪深 A 股 {len(universe)} 只")
    ind_map = load_industry_map(universe, refresh=refresh)
    if stats_only:
        with open(BARS_OUT, "r", encoding="utf-8") as fh:
            bars = (json.load(fh).get("bars")) or {}
    elif quotes_only:
        with open(BARS_OUT, "r", encoding="utf-8") as fh:
            bars = (json.load(fh).get("bars")) or {}
        bars = update_quotes(bars, universe)
        update_snapshot(universe, bars)
    else:
        bars = load_bars(universe, refresh=refresh)
    aggregate(bars, ind_map, universe)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
