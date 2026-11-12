# -*- coding: utf-8 -*-
"""特征层：把 data/ 里的日线 / 估值 / 财务 / 板块 / 大盘，折叠成「当日一行」特征字典。

设计原则（见 docs/DESIGN.md §3）：
- 纯函数、无网络、无副作用；所有指标只看到「当日及以前」的数据（无未来函数）。
- 键名冻结，规则层（rules.py）按这些键名写条件；缺数据给 None 而不是 0。
- 百分比统一 %数值（4.88 表示 +4.88%）；from_hi_1y 为负数。
"""
from __future__ import annotations

import bisect
import json
import os
from typing import Any, Optional

import numpy as np

from . import config as C

# ============================================================ 低层工具


def _load_json(path: str) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _f(arr: Any) -> np.ndarray:
    """转成 float64 数组，None 变 nan。"""
    out = np.empty(len(arr), dtype=np.float64)
    for i, v in enumerate(arr):
        try:
            out[i] = np.nan if v is None or v == "" else float(v)
        except (TypeError, ValueError):
            out[i] = np.nan
    return out


def load_bars(code_or_key: str, long: bool = False) -> Optional[dict]:
    """读 data/bars/{key}.json 或 data/bars_long/{key}.json，转 numpy。

    返回 {"key","name","kind","fq","date"(np.datetime64[D]), "open","close","high","low",
          "volume","turnover","amount"}；失败返回 None。
    """
    d = C.BARS_LONG if long else C.BARS
    key = code_or_key
    if not key.startswith(("sh", "sz", "bj")):
        key = ("sh" if key.startswith(("6", "5", "9", "000300", "000001", "000905")) else "sz") + key
        if code_or_key[:6] in ("000300", "000001", "000905", "000016"):
            key = "sh" + code_or_key[:6]
    j = _load_json(os.path.join(d, f"{key}.json"))
    if not j or not j.get("date"):
        return None
    out = {
        "key": key,
        "name": j.get("name", ""),
        "kind": j.get("kind", "stock"),
        "fq": j.get("fq", "qfq"),
        "date": np.array(j["date"], dtype="datetime64[D]"),
        "open": _f(j["open"]),
        "close": _f(j["close"]),
        "high": _f(j["high"]),
        "low": _f(j["low"]),
        "volume": _f(j.get("volume") or []),
        "turnover": _f(j.get("turnover") or []),
        "amount": _f(j.get("amount") or []),
    }
    n = len(out["date"])
    for k in ("open", "close", "high", "low", "volume", "turnover", "amount"):
        if len(out[k]) != n:  # 容错：长度不一致时截断到最短
            out[k] = out[k][:n] if len(out[k]) > n else np.concatenate(
                [out[k], np.full(n - len(out[k]), np.nan)])
    return out


def _last_valid(arr: np.ndarray) -> Optional[float]:
    if arr is None or len(arr) == 0:
        return None
    for v in arr[::-1]:
        if not np.isnan(v):
            return float(v)
    return None


def _ma(close: np.ndarray, n: int, i: int = -1) -> Optional[float]:
    """截至 i（含）最近 n 日均值；不足 n 根返回 None。"""
    end = len(close) if i < 0 else i + 1
    if end < n:
        return None
    seg = close[end - n:end]
    if np.isnan(seg).any():
        return None
    return float(seg.mean())


def _pct(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None or b == 0 or np.isnan(a) or np.isnan(b):
        return None
    return (a / b - 1.0) * 100.0


def percentile_of(values: np.ndarray, x: Optional[float]) -> Optional[float]:
    """x 在 values 中的百分位（0~100，越大越贵）；缺失返回 None。"""
    if x is None or values is None or len(values) == 0:
        return None
    v = values[~np.isnan(values)]
    if len(v) == 0 or np.isnan(x):
        return None
    return float((v < x).mean() * 100.0)


# ============================================================ 特征主函数

FEATURE_KEYS = [
    # 价格/趋势
    "close", "open", "high", "low", "prev_close", "chg_pct",
    "ma5", "ma10", "ma20", "ma60", "ma120", "ma250",
    "ma20_slope", "ma60_slope", "above_ma5", "above_ma20", "above_ma60", "ma5_up",
    "dist_ma20", "dist_ma60",
    # 量能
    "volume", "amount", "turnover", "vol_ratio", "amt_ratio", "vol_ratio3",
    "turnover_3d_avg", "up_candle",
    # 涨跌/位置
    "ret1", "ret3", "ret5", "ret20", "ret60", "high_1y", "from_hi_1y",
    "low_1y", "from_lo_1y", "box_high_60", "box_low_60", "box_break", "new_high_20d_3d",
    # 估值
    "pe_ttm", "pb", "peg", "pe_pct_5y", "pb_pct_5y", "pe_pct_window_years", "mktcap_yi", "div_yield",
    # 财务
    "roe", "gross", "debt", "profit_years", "rev_yoy", "net_profit_yi",
    # 板块
    "sector_name", "sector_chg_today", "sector_ret5", "sector_above_ma20",
    "sector_rank_today", "sector_pe_pct",
    # 板块 3 天验证（§10.1.1）
    "sector_cum3", "sector_rank_3ago", "sector_limit_up", "sector_top10_days",
    "sector_breadth", "sector_amount_rank", "sector_amount_n",
    "sector_n_members", "sector_thin", "sector_full",
    # 大盘
    "index_above_ma20", "index_ma20_slope", "index_chg_today",
    # 元信息
    "code", "name", "sector", "date",
]


def build_features(bars: dict, *, val: Optional[dict] = None,
                   fin: Optional[dict] = None,
                   sector_ctx: Optional[dict] = None,
                   market_ctx: Optional[dict] = None) -> dict:
    """由当日（最后一根）数据算全部特征。bars 见 load_bars。"""
    f: dict[str, Any] = {k: None for k in FEATURE_KEYS}
    f["code"] = bars.get("key", "")[2:] if bars.get("key") else ""
    f["name"] = bars.get("name", "")
    f["sector"] = None

    c = bars["close"]
    o = bars["open"]
    h = bars["high"]
    lo = bars["low"]
    v = bars["volume"]
    n = len(c)
    if n == 0:
        return f
    f["date"] = str(bars["date"][-1])
    f["close"] = _last_valid(c)
    f["open"] = _last_valid(o)
    f["high"] = _last_valid(h)
    f["low"] = _last_valid(lo)
    f["prev_close"] = float(c[-2]) if n >= 2 and not np.isnan(c[-2]) else None
    f["chg_pct"] = _pct(f["close"], f["prev_close"])

    # ---- 均线
    for k in (5, 10, 20, 60, 120, 250):
        f[f"ma{k}"] = _ma(c, k)
    if f["ma20"] is not None and n >= 26:
        m_now = _ma(c, 20)
        m_prev = _ma(c, 20, n - 6)  # 5 个交易日前
        f["ma20_slope"] = _pct(m_now, m_prev)
    if f["ma60"] is not None and n >= 66:
        f["ma60_slope"] = _pct(_ma(c, 60), _ma(c, 60, n - 6))
    f["above_ma5"] = int(f["close"] > f["ma5"]) if f["ma5"] else None
    f["above_ma20"] = int(f["close"] > f["ma20"]) if f["ma20"] else None
    f["above_ma60"] = int(f["close"] > f["ma60"]) if f["ma60"] else None
    f["dist_ma20"] = _pct(f["close"], f["ma20"])
    f["dist_ma60"] = _pct(f["close"], f["ma60"])
    if n >= 6:
        f["ma5_up"] = int(_ma(c, 5) > _ma(c, 5, n - 2)) if _ma(c, 5) is not None else None

    # ---- 量能
    f["volume"] = _last_valid(v)
    f["amount"] = _last_valid(bars["amount"])
    f["turnover"] = _last_valid(bars["turnover"])
    if n >= 6 and not np.isnan(v[-6:-1]).any():
        base = float(v[-6:-1].mean())
        f["vol_ratio"] = float(v[-1] / base) if base > 0 else None
    if n >= 6 and not np.isnan(bars["amount"][-6:-1]).any():
        amt = bars["amount"]
        base = float(amt[-6:-1].mean())
        f["amt_ratio"] = float(amt[-1] / base) if base > 0 else None
    if n >= 23:
        v3 = v[-3:]
        v20 = v[-23:-3]
        if not np.isnan(v3).any() and not np.isnan(v20).any() and v20.mean() > 0:
            f["vol_ratio3"] = float(v3.mean() / v20.mean())
    if n >= 3:
        f["turnover_3d_avg"] = float(np.nanmean(bars["turnover"][-3:])) if not np.isnan(
            bars["turnover"][-3:]).all() else None
    f["up_candle"] = int((f["close"] or 0) > (f["open"] or 0)) if f["close"] and f["open"] else None

    # ---- 涨跌 / 位置
    def _ret(k: int) -> Optional[float]:
        if n > k and not np.isnan(c[-1 - k]):
            return _pct(c[-1], c[-1 - k])
        return None
    for k in (1, 3, 5, 20, 60):
        f[f"ret{k}"] = _ret(k)
    win = min(250, n)
    f["high_1y"] = float(np.nanmax(h[-win:])) if win > 0 else None
    f["low_1y"] = float(np.nanmin(lo[-win:])) if win > 0 else None
    f["from_hi_1y"] = _pct(f["close"], f["high_1y"])
    f["from_lo_1y"] = _pct(f["close"], f["low_1y"])
    if n >= 62:
        f["box_high_60"] = float(np.nanmax(h[-61:-1]))
        f["box_low_60"] = float(np.nanmin(lo[-61:-1]))
        f["box_break"] = int(f["close"] > f["box_high_60"]) if f["close"] and f["box_high_60"] else None
    if n >= 20:
        f["new_high_20d_3d"] = int(np.nanmax(h[-3:]) >= np.nanmax(h[-20:]))

    # ---- 估值
    if val:
        dts = val.get("date") or []
        idx = len(dts) - 1
        if idx >= 0:
            f["pe_ttm"] = val["pe_ttm"][idx] if val.get("pe_ttm") else None
            f["pb"] = val["pb"][idx] if val.get("pb") else None
            pe_now = f["pe_ttm"]
            pe_hist = _f(val.get("pe_ttm") or [])
            f["pe_pct_5y"] = percentile_of(pe_hist[-1250:], pe_now)
            f["pb_pct_5y"] = percentile_of(_f(val.get("pb") or [])[-1250:], f["pb"])
            peg_arr = val.get("peg") or []
            f["peg"] = peg_arr[idx] if peg_arr else None
            f["pe_pct_window_years"] = 5
            mcap = val.get("total_mktcap") or []
            f["mktcap_yi"] = (mcap[idx] / 1e8) if idx < len(mcap) and mcap[idx] else None

    # ---- 财务
    if fin:
        lat = fin.get("latest") or {}
        ann = fin.get("annual") or []
        f["roe"] = lat.get("roe")
        f["gross"] = lat.get("gross")
        f["debt"] = lat.get("debt")
        f["rev_yoy"] = lat.get("rev_yoy")
        f["div_yield"] = lat.get("div_yield")
        npf = lat.get("net_profit")
        f["net_profit_yi"] = (npf / 1e8) if isinstance(npf, (int, float)) else None
        yrs = 0
        for row in ann:  # annual 必须按年份从新到旧
            if isinstance(row.get("net_profit"), (int, float)) and row["net_profit"] > 0:
                yrs += 1
            else:
                break
        f["profit_years"] = yrs

    # ---- 板块
    if sector_ctx:
        f["sector_name"] = sector_ctx.get("name") or sector_ctx.get("sector")
        f["sector_chg_today"] = sector_ctx.get("chg_today")
        f["sector_ret5"] = sector_ctx.get("ret5")
        f["sector_above_ma20"] = sector_ctx.get("above_ma20")
        f["sector_rank_today"] = sector_ctx.get("rank_today")
        f["sector_pe_pct"] = sector_ctx.get("pe_pct")
        # §10.1.1 板块 3 天验证
        f["sector_cum3"] = sector_ctx.get("cum3")
        f["sector_rank_3ago"] = sector_ctx.get("rank_3ago")
        f["sector_limit_up"] = sector_ctx.get("limit_up")
        f["sector_top10_days"] = sector_ctx.get("top10_days")
        f["sector_breadth"] = sector_ctx.get("breadth_ratio")
        # 行业样本数（池内该行业只有 1~2 只时无法算板块热度，A 短线不适用）
        f["sector_n_members"] = sector_ctx.get("n_members")
        f["sector_thin"] = sector_ctx.get("thin")
        # 是否全 A 行业口径（全 A 时 S3/S5 广度条件才有统计意义，按硬性条件执行）
        f["sector_full"] = sector_ctx.get("full")

    # ---- 大盘
    if market_ctx:
        f["index_above_ma20"] = market_ctx.get("above_ma20")
        f["index_ma20_slope"] = market_ctx.get("ma20_slope")
        f["index_chg_today"] = market_ctx.get("chg_today")
    return f


# ============================================================ 板块 / 大盘上下文


def _sort_by_date(bar_list: list[dict]) -> tuple[list[str], np.ndarray]:
    """取多只股票共同交易日（交集），按日期升序返回 (dates, [N,K] 收盘矩阵)。"""
    if not bar_list:
        return [], np.zeros((0, 0))
    date_sets = [set(map(str, b["date"])) for b in bar_list]
    common = sorted(set.intersection(*date_sets))
    dtype = "datetime64[D]"
    all_d = np.array(sorted(set().union(*date_sets)), dtype=dtype)
    mat = np.full((len(all_d), len(bar_list)), np.nan)
    pos = {str(d): i for i, d in enumerate(all_d)}
    for j, b in enumerate(bar_list):
        for d, cls in zip(b["date"], b["close"]):
            mat[pos[str(d)], j] = cls
    dates = np.array([str(d) for d in all_d], dtype=dtype)
    return [str(x) for x in all_d], mat


def equal_weight_index(members: list[dict]) -> Optional[dict]:
    """由成员股前复权收盘价构造等权板块指数（逐日收益等权平均，基点 1000）。"""
    dates, mat = _sort_by_date(members)
    if len(dates) == 0 or mat.shape[1] == 0:
        return None
    rets = np.full(mat.shape, np.nan)
    rets[1:] = mat[1:] / mat[:-1] - 1.0
    # 首日/停牌行没有收益：手动求均值，避免 np.nanmean 的空切片警告
    valid = ~np.isnan(rets)
    cnt = valid.sum(axis=1)
    s = np.where(valid, rets, 0.0).sum(axis=1)
    day_ret = np.where(cnt > 0, s / np.maximum(cnt, 1), 0.0)
    level = np.nancumprod(1.0 + np.nan_to_num(day_ret)) * 1000.0
    idx = {
        "key": "sector_eq",
        "name": "equal-weight",
        "kind": "index",
        "fq": "qfq",
        "date": np.array(dates, dtype="datetime64[D]"),
        "open": level.copy(), "close": level.copy(),
        "high": level.copy(), "low": level.copy(),
        "volume": np.zeros(len(dates)), "turnover": np.zeros(len(dates)),
        "amount": np.zeros(len(dates)),
    }
    return idx


def make_sector_ctx(sector_key: str, name: str, index_bars: dict,
                    rank_today: Optional[int] = None,
                    pe_pct: Optional[float] = None,
                    extras: Optional[dict] = None) -> dict:
    """板块上下文：当日涨幅、近5日涨幅、是否站上MA20 + §10.1.1 三天验证指标。"""
    c = index_bars["close"]
    n = len(c)
    chg = _pct(c[-1], c[-2]) if n >= 2 else None
    ret5 = _pct(c[-1], c[-6]) if n >= 6 else None
    ma20 = _ma(c, 20)
    ctx = {
        "name": name,
        "chg_today": chg,
        "ret5": ret5,
        "above_ma20": int(c[-1] > ma20) if ma20 else None,
        "rank_today": rank_today,
        "pe_pct": pe_pct,
        "cum3": _pct(c[-1], c[-4]) if n >= 4 else None,
    }
    if extras:
        ctx.update(extras)
    return ctx


def sector_breadth(member_bars: list[dict], date_str: Optional[str] = None) -> dict:
    """§10.1.1 板块广度：当日涨家数/跌家数/涨停家数（同花顺口径：≥9.8% 视为涨停）。"""
    up = down = flat = limit_up = 0
    for b in member_bars:
        dates = b.get("date")
        if dates is None:
            continue
        ds = [str(d) for d in dates]
        if len(ds) < 2:
            continue
        pos = None
        if date_str is not None:
            try:
                pos = ds.index(str(date_str))
            except ValueError:
                pos = None
            if pos is None:
                continue
        else:
            pos = len(ds) - 1
        if pos < 1:
            continue
        c0, c1 = b["close"][pos - 1], b["close"][pos]
        if not c0 or not c1:
            continue
        try:
            pct = (float(c1) / float(c0) - 1.0) * 100.0
        except (ZeroDivisionError, TypeError, ValueError):
            continue
        if abs(pct) < 1e-9:
            flat += 1
        elif pct > 0:
            up += 1
        else:
            down += 1
        if pct >= 9.8:
            limit_up += 1
    if down > 0:
        ratio = up / down
    else:
        ratio = float("inf") if up > 0 else None
    return {"up": up, "down": down, "flat": flat, "limit_up": limit_up,
            "breadth_ratio": ratio}


def sector_rank_series(sector_index: dict) -> dict:
    """给单个板块的指数，返回 {date: 当日涨幅%}，用于跨板块排名。"""
    c = sector_index["close"]
    out = {}
    for i, d in enumerate(sector_index["date"]):
        if i >= 1 and c[i - 1]:
            out[str(d)] = (float(c[i]) / float(c[i - 1]) - 1.0) * 100.0
    return out


def rank_history_stats(sector_chg_by_date: dict, sector_key: str, dates: list[str],
                       rank_map: dict, top10_days_min: int) -> dict:
    """从每日板块涨幅排名表里取：今天排名、3个交易日前排名、过去3日进前10的天数。

    rank_map: {date: {sector_key: rank}}；dates: 升序的最近 4 个交易日。
    """
    if not dates:
        return {}
    today = dates[-1]
    rank_today = (rank_map.get(today) or {}).get(sector_key)
    look = 3
    rank_3ago = (rank_map.get(dates[-1 - look]) or {}).get(sector_key) if len(dates) > look else None
    window = dates[-(3):]
    top10 = 0
    for d in window:
        r = (rank_map.get(d) or {}).get(sector_key)
        if r is not None and r <= 10:
            top10 += 1
    return {"rank_today": rank_today, "rank_3ago": rank_3ago, "top10_days": top10}


def make_market_ctx(index_bars: dict) -> dict:
    """大盘上下文（沪深300）：是否站上MA20、MA20 斜率、当日涨跌。"""
    c = index_bars["close"]
    n = len(c)
    ma20 = _ma(c, 20)
    slope = None
    if n >= 26 and ma20 is not None:
        slope = _pct(ma20, _ma(c, 20, n - 6))
    return {
        "above_ma20": int(c[-1] > ma20) if ma20 else None,
        "ma20_slope": slope,
        "chg_today": _pct(c[-1], c[-2]) if n >= 2 else None,
    }


def load_val(code: str) -> Optional[dict]:
    return _load_json(os.path.join(C.VAL, f"{code}.json"))


def load_fin(code: str) -> Optional[dict]:
    return _load_json(os.path.join(C.FIN, f"{code}.json"))


def load_pool() -> Optional[dict]:
    return _load_json(C.POOL)


def load_snapshot() -> Optional[dict]:
    return _load_json(C.SNAP)


def load_industry() -> dict:
    """行业映射 {code: {industry2, industry1, ...}}（东财 F10 EM2016，tools/build_industry.py 生成）。"""
    doc = _load_json(os.path.join(C.DATA, "industry.json")) or {}
    return doc.get("stocks") or {}


def load_industry_daily() -> dict:
    """全 A 行业热度 {date, members, industries, amount_rank}（tools/build_market.py 生成）。"""
    return _load_json(os.path.join(C.DATA, "industry_daily.json")) or {}


# ============================================================ 历史切片（回测/事件研究用）


def slice_bars(bars: dict, end_idx: int) -> dict:
    """把 bars 截到「第 end_idx 根（含）」——用于逐日回放特征，避免未来函数。"""
    n = end_idx + 1
    out = dict(bars)
    for k in ("date", "open", "close", "high", "low", "volume", "turnover", "amount"):
        arr = bars.get(k)
        out[k] = arr[:n] if arr is not None else np.zeros(0)
    return out


def val_upto(val: Optional[dict], date_str: str) -> Optional[dict]:
    """估值序列截到 date_str（含）当天，保持升序。

    回测里会被逐日调用上百万次，故首调用时把日期缓存成字符串列表（bisect 二分），
    缓存键 `_dates_str` 不是 list 之外的类型，切片时会被跳过，不会污染返回结构。
    """
    if not val:
        return None
    dts = val.get("date") or []
    keys = val.get("_dates_str")
    if keys is None:
        keys = [str(d) for d in dts]
        try:
            val["_dates_str"] = keys
        except Exception:  # 只读 dict 时退化为直接判断
            keys = [str(d) for d in dts]
    end = bisect.bisect_right(keys, date_str)
    if end == 0:
        return None
    out = {"date": dts[:end]}
    for k, v in val.items():
        if k == "date" or not isinstance(v, list):
            continue
        out[k] = v[:end]
    return out


def bar_index_on(bars: dict, date_str: str) -> int:
    """返回 bars 中 <= date_str 的最后一根下标；没有则 -1。"""
    dts = bars["date"]
    idx = -1
    for i in range(len(dts) - 1, -1, -1):
        if str(dts[i]) <= date_str:
            idx = i
            break
    return idx

