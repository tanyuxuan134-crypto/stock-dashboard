# -*- coding: utf-8 -*-
"""全 A 选股扫描（A 短线 / B 波段 / C 长线）—— 2026-11-12 新增。

为什么要单独一层：
  手册的三套模式原本只跑「154 只精选池」，池子里每行业只有 3~19 只，很多条件失真。
  这里把范围扩到**全 A 5224 只**：用 `data/market_bars.json`（90 日前复权收盘+成交额）
  + `data/snapshot.json`（价格/涨跌幅/成交额/换手/PE/PB/市值）+ `data/industry_daily.json`
  （全行业热度）做粗筛，输出每天的全市场候选清单。

诚实说明（也是代码里写死的限制）：
  * 全 A 只有 90 日 K 线，所以「距 1 年高点」用 90 日高点代理、量比用「成交额倍数」代理、
    「收阳」用当日涨跌 > 0 代理（精确的 open/close 需要 640 根 K 线，全市场抓不现实）；
  * C 长线需要「PE 近 5 年分位 / ROE / 连续盈利 / 股息率」，全 A 批量取不到 → 只做**粗筛**，
    候选里标注「需逐只补财务」；
  * 结果是「候选清单」，不是自动下单信号；真正的精细判定仍在池内 154 只上做。

用法：
  python tools/screen_market.py            # 全量扫描（约 3~8 秒）
  python tools/screen_market.py --quick    # 每天跟随 quick_refresh 跑
  python tools/screen_market.py --top 20   # 每个模式保留多少条
输出：data/screen_market.json（看板第 8 节读取）
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from typing import Optional

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from quant import config as C  # noqa: E402

DATA = os.path.join(ROOT, "data")
OUT_PATH = os.path.join(DATA, "screen_market.json")
MAX_TOP = 30


# --------------------------------------------------------------------------- #
# 数据加载
# --------------------------------------------------------------------------- #
def _load(name: str) -> dict:
    path = os.path.join(DATA, name)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _nanmean(arr) -> Optional[float]:
    v = np.asarray(arr, dtype="float64")
    v = v[~np.isnan(v)]
    return float(v.mean()) if v.size else None


def _pct(new, old) -> Optional[float]:
    if new is None or old in (None, 0):
        return None
    return (float(new) / float(old) - 1.0) * 100.0


class Bars:
    """把 compact K 线（d/c/a）包成好用的对象。"""

    __slots__ = ("dates", "close", "amount")

    def __init__(self, doc: dict):
        self.dates = [str(x) for x in (doc.get("d") or [])]
        c = doc.get("c") or []
        a = doc.get("a") or []
        self.close = np.asarray(c, dtype="float64") if c else np.zeros(0)
        self.amount = np.asarray(a, dtype="float64") if a else np.zeros(0)
        n = min(len(self.dates), len(self.close))
        self.dates, self.close = self.dates[:n], self.close[:n]

    def __len__(self) -> int:
        return len(self.close)

    def ma(self, n: int) -> Optional[float]:
        if len(self.close) < n:
            return None
        return float(self.close[-n:].mean())

    def ret(self, n: int) -> Optional[float]:
        if len(self.close) <= n:
            return None
        return _pct(self.close[-1], self.close[-1 - n])

    def amount_avg(self, n: int) -> Optional[float]:
        if len(self.amount) < n + 1:
            return None
        return _nanmean(self.amount[-n - 1:-1])

    def high(self, n: int) -> Optional[float]:
        if len(self.close) < n:
            return None
        return float(self.close[-n:].max())

    def ma_series(self, n: int, step: int = 5) -> Optional[float]:
        """n 日均线在 step 个交易日前的值（判断走平/上翘）。"""
        if len(self.close) < n + step:
            return None
        return float(self.close[-n - step:-step].mean())


# --------------------------------------------------------------------------- #
# 大盘 / 行业上下文
# --------------------------------------------------------------------------- #
def market_ctx() -> dict:
    """沪深300 是否站上 MA20（§10.3 大盘跌破 MA20 否决 B）。"""
    bars = Bars(_load("market_bars.json").get("bars", {}).get("sh000300") or
                _load("market_bars.json").get("bars", {}).get("000300") or {})
    # 指数不在 market_bars 里：退回到 data/bars/sh000300.json（164 只池子那套）
    if len(bars) == 0:
        path = os.path.join(DATA, "bars", "sh000300.json")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
            c = np.asarray(doc.get("close") or [], dtype="float64")
            if c.size:
                bars = Bars({"d": doc.get("date") or [], "c": c.tolist(), "a": (doc.get("amount") or [])})
        except (OSError, ValueError):
            pass
    if len(bars) < 20:
        return {"above_ma20": None, "chg": None}
    ma20 = bars.ma(20)
    return {"above_ma20": int(bars.close[-1] > ma20),
            "chg": _pct(bars.close[-1], bars.close[-2])}


def build_industry_ctx(mk: dict) -> tuple[dict, dict]:
    """industry_daily.json -> ({code: industry}, {industry: metrics})。"""
    members = mk.get("members") or {}
    inds = mk.get("industries") or {}
    amounts = mk.get("amount_rank") or {}
    ctx: dict = {}
    for name, it in inds.items():
        ctx[name] = {
            "n": it.get("n"),
            "chg": it.get("chg"),
            "rank": it.get("rank"),
            "rank_3ago": it.get("rank_3ago"),
            "cum3": it.get("cum3"),
            "limit_up": it.get("limit_up"),
            "top10_days": it.get("top10_days"),
            "breadth": it.get("breadth"),
            "above_ma20": it.get("above_ma20"),
            "eligible": it.get("eligible", True),
            "amount_rank": amounts.get(name) or {},
        }
    return members, ctx


# --------------------------------------------------------------------------- #
# A 短线（§10.1 + §10.1.1）
# --------------------------------------------------------------------------- #
def scan_A(universe: dict, member_of: dict, ind: dict, mkt: dict,
           funnel: Optional[dict] = None) -> tuple[list[dict], list[dict]]:
    s = C.A_SCREEN
    sec = C.A_SECTOR
    out, watch = [], []
    if funnel is None:
        funnel = {}
    for code, row in universe.items():
        name = row.get("name") or code
        if s["exclude_st"] and ("ST" in name.upper() or "退" in name):
            continue
        price = row.get("price")
        mktcap = row.get("mktcap_yi")
        if not price or price < s["price_min"] or price > s["price_max"]:
            continue
        if not mktcap or mktcap < s["mktcap_min_yi"] or mktcap > s["mktcap_max_yi"]:
            continue
        if s["exclude_turnover_gt"] and (row.get("turnover") or 0) > s["exclude_turnover_gt"]:
            continue
        bars = universe[code].get("_bars")
        if bars is None or len(bars) < 25:
            continue
        ret5 = bars.ret(5)
        if s["exclude_ret5_gt"] and ret5 is not None and ret5 > s["exclude_ret5_gt"]:
            continue
        funnel["基础条件通过"] = funnel.get("基础条件通过", 0) + 1
        # 行业（§10.1.1 用全 A 行业热度）
        ind_name = member_of.get(code) or row.get("industry")
        ic = ind.get(ind_name) or {}
        if not ic or not ic.get("rank"):
            continue  # 行业不可排名（<10 只）→ A 短线不适用
        funnel["行业可排名"] = funnel.get("行业可排名", 0) + 1
        ok, fail = [], []

        def chk(tag, cond, text):
            (ok if cond else fail).append(f"{tag} {text}")
            if not cond:
                funnel[tag] = funnel.get(tag, 0) + 1
            return cond

        # §10.1 板块：当日涨幅排名前 5 且行业指数涨 > 1%
        chk("A1", ic.get("rank", 99) <= s["sector_top_n"],
            f"行业排名第{ic.get('rank')}（要求前{s['sector_top_n']}）")
        chk("A2", (ic.get("chg") or -99) > s["sector_min_chg_pct"],
            f"行业涨幅{ic.get('chg')}%（要求>{s['sector_min_chg_pct']}%）")
        # §10.1.1 S1~S5
        cum3 = ic.get("cum3")
        zone = None
        if cum3 is not None:
            if cum3 < sec["cum3_min"]:
                zone = "cold"
            elif cum3 <= sec["cum3_max"]:
                zone = "sweet"
            elif cum3 <= sec["cum3_heavy_max"]:
                zone = "heavy"
            else:
                zone = "chase"
        chk("S1", zone in ("sweet", "heavy"), f"行业3日累计{cum3}%（甜区{sec['cum3_min']}~{sec['cum3_max']}%）")
        r3 = ic.get("rank_3ago")
        chk("S2", (r3 is None) or (ic.get("rank", 99) <= r3) or (ic.get("rank", 99) <= sec["rank_top_n"]),
            f"行业排名 今天{ic.get('rank')} vs 3日前{r3}")
        chk("S3", (ic.get("limit_up") or 0) >= 2, f"行业涨停{ic.get('limit_up')}家（要求≥2）")
        chk("S4", (ic.get("top10_days") or 0) >= sec["top10_days_min"],
            f"近3日上榜{ic.get('top10_days')}天（要求≥{sec['top10_days_min']}）")
        breadth = ic.get("breadth")
        chk("S5", breadth is not None and breadth >= sec["breadth_min"],
            f"行业涨跌比{breadth}（要求≥{sec['breadth_min']}）")
        # A3 量能（成交额倍数代理量比）
        amt = row.get("amount") or 0.0        # 元
        amt5 = bars.amount_avg(5)             # 万元
        multiple = (amt / 1e4 / amt5) if amt5 else None
        chk("A3", multiple is not None and multiple >= s["vol_multiple_min"],
            f"成交额倍数{round(multiple, 2) if multiple else None}（要求≥{s['vol_multiple_min']}，量比代理）")
        # A4 均线：收盘>MA5 且 MA5 上翘
        ma5, ma5_prev = bars.ma(5), bars.ma_series(5, 1)
        chk("A4", ma5 is not None and bars.close[-1] > ma5 and ma5_prev is not None and ma5 > ma5_prev,
            f"收盘{round(float(bars.close[-1]), 2)} vs MA5 {round(ma5, 2) if ma5 else None}，MA5上翘")
        # A5 位置：距 90 日高点 ≤ −30% **或** 刚突破平台（§10.1 原文是「或」）
        hi = bars.high(90)
        from_hi = _pct(bars.close[-1], hi) if hi else None
        box_hi60 = float(bars.close[-61:-1].max()) if len(bars) >= 62 else None
        breakout = bool(box_hi60 and bars.close[-1] > box_hi60)
        chk("A5", (from_hi is not None and from_hi <= s["from_hi_1y_max"]) or breakout,
            f"距90日高{round(from_hi, 1) if from_hi is not None else None}%（代理1年高点，要求≤{s['from_hi_1y_max']}%）或刚突破平台={breakout}")
        # A12 行业成交额排名前 3
        ar = (ic.get("amount_rank") or {}).get(code)
        chk("A12", ar is not None and ar <= 3, f"行业成交额排名第{ar}（要求前3）")
        row_out = {
            "code": code, "name": name, "industry": ind_name,
            "price": round(float(bars.close[-1]), 2),
            "chg": row.get("chg_pct"),
            "sector_rank": ic.get("rank"),
            "sector_chg": ic.get("chg"),
            "cum3": cum3,
            "amount_rank": ar,
            "light_zone": zone == "heavy",
            "score": f"{len(ok)}/{len(ok) + len(fail)}",
            "why": ok,
            "fail": fail,
            "date": bars.dates[-1] if len(bars) else "",
        }
        if len(fail) == 0:
            row_out.pop("fail", None)
            out.append(row_out)
        elif (ic.get("rank", 99) <= s["sector_top_n"]) and ((ic.get("chg") or -99) > s["sector_min_chg_pct"]):
            # 行业今天确实在前 5 且涨幅>1%，只是个股没到位 → 收进「观察」并附没过的条件
            watch.append(row_out)
    out.sort(key=lambda r: (r["sector_rank"] or 99, r["amount_rank"] or 99))
    watch.sort(key=lambda r: (len(r["fail"]), r["sector_rank"] or 99))
    return out, watch[:10]


# --------------------------------------------------------------------------- #
# B 波段（§10.3 四信号取 2，必须含①）
# --------------------------------------------------------------------------- #
def scan_B(universe: dict, member_of: dict, ind: dict, mkt: dict,
           max_n: int = MAX_TOP, funnel: Optional[dict] = None) -> tuple[list[dict], list[dict]]:
    """返回 (通过, 被否决但信号数≥2 的观察名单)。

    大盘跌破 MA20 时 B 会被全部否决（§10.3 纪律），但用户仍想知道「哪些股本来最接近」，
    所以额外给一份 watch 名单，页面上标注「被大盘否决，不是买入信号」。
    """
    bs = C.B_SIGNALS
    out, watch = [], []
    if funnel is None:
        funnel = {}
    for code, row in universe.items():
        name = row.get("name") or code
        if "ST" in name.upper() or "退" in name:
            continue
        bars = universe[code].get("_bars")
        if bars is None or len(bars) < bs["box_window"] + 6:
            continue
        close = bars.close[-1]
        ma20 = bars.ma(20)
        ma20_prev = bars.ma_series(20, 5)
        if ma20 is None or ma20_prev is None:
            continue
        sig = {}
        # ① 收盘>MA20 且 MA20 走平或上翘（必选）
        sig["above_ma20_and_ma20_flat_or_up"] = bool(close > ma20 and ma20 >= ma20_prev * 0.999)
        # ② 回踩 MA20 不破、止跌收阳（用「收盘离 MA20 ≤3% 且当日上涨」代理）
        chg = row.get("chg_pct")
        near_ma20 = abs(close / ma20 - 1.0) <= 0.03
        sig["pullback_ma20_hold"] = bool(close >= ma20 and near_ma20 and (chg or 0) > 0)
        # ③ 放量突破 60 日箱体上沿
        box_hi = float(bars.close[-bs["box_window"] - 1:-1].max())
        amt5 = bars.amount_avg(5)
        amt_today = (row.get("amount") or 0.0) / 1e4
        sig["box_break_volume"] = bool(
            close > box_hi and amt5 and amt_today / amt5 >= bs["box_break_vol_multiple"])
        # ④ 板块共振（行业站上 MA20）
        ind_name = member_of.get(code) or row.get("industry")
        ic = ind.get(ind_name) or {}
        sig["sector_resonance"] = bool(ic.get("above_ma20") == 1)
        n_ok = sum(1 for v in sig.values() if v)
        if n_ok >= bs["min_signals_ok"]:
            funnel["信号数≥2"] = funnel.get("信号数≥2", 0) + 1
        veto = []
        if not sig["above_ma20_and_ma20_flat_or_up"]:
            veto.append("MA20向下/收盘在MA20下")
        if mkt.get("above_ma20") == 0:
            veto.append("大盘跌破MA20")
        if ic.get("chg") is not None and ic.get("chg") > 0 and (ic.get("cum3") or 0) < 0:
            veto.append("板块一日游（今天涨但3日累计为负）")
        if n_ok < bs["min_signals_ok"]:
            continue
        rec = {
            "code": code, "name": name, "industry": ind_name,
            "price": round(float(close), 2), "chg": chg,
            "signals": [k for k, v in sig.items() if v],
            "n_signals": n_ok,
            "ma20": round(ma20, 2),
            "sector_rank": ic.get("rank"),
            "veto": veto,
        }
        if veto:
            if "MA20向下/收盘在MA20下" not in veto:
                watch.append(rec)
        else:
            out.append(rec)
    out.sort(key=lambda r: (-r["n_signals"], r["sector_rank"] or 99))
    watch.sort(key=lambda r: (-r["n_signals"], r["sector_rank"] or 99))
    return out[:max_n], watch[:10]


# --------------------------------------------------------------------------- #
# C 长线（§10.4 粗筛：PE/PB/位置；财务类条件需逐只补数据）
# --------------------------------------------------------------------------- #
def scan_C(universe: dict, member_of: dict, ind: dict, max_n: int = MAX_TOP,
           funnel: Optional[dict] = None) -> list[dict]:
    ce = C.C_ENTRY
    out = []
    if funnel is None:
        funnel = {}
    for code, row in universe.items():
        name = row.get("name") or code
        if "ST" in name.upper() or "退" in name:
            continue
        pe, pb = row.get("pe"), row.get("pb")
        price, mktcap = row.get("price"), row.get("mktcap_yi")
        if not price or price < 2 or not mktcap or mktcap < 50:
            continue
        if not pe or pe <= 0 or pe > 25:      # 手册消费/医药类建仓线 15~25，先按 25 粗筛
            continue
        if pb is None or pb > ce["or_pb_max"]:
            continue
        bars = universe[code].get("_bars")
        if bars is None or len(bars) < 60:
            continue
        from_hi = _pct(bars.close[-1], bars.high(90))
        funnel["PE≤25且PB≤1.5"] = funnel.get("PE≤25且PB≤1.5", 0) + 1
        if from_hi is None or from_hi > ce["from_hi_1y_max"]:
            continue
        funnel["距90日高≤-20%"] = funnel.get("距90日高≤-20%", 0) + 1
        ind_name = member_of.get(code) or row.get("industry")
        out.append({
            "code": code, "name": name, "industry": ind_name,
            "price": round(float(bars.close[-1]), 2),
            "chg": row.get("chg_pct"),
            "pe": pe, "pb": pb, "mktcap_yi": mktcap,
            "from_hi": round(from_hi, 1),
            "note": "粗筛：PE 5年分位/股息率/连盈/负债率需逐只补财务后再判定",
        })
    out.sort(key=lambda r: (r["pe"], -r["mktcap_yi"]))
    return out[:max_n]


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    top = MAX_TOP
    if "--top" in argv:
        try:
            top = max(5, int(argv[argv.index("--top") + 1]))
        except (ValueError, IndexError):
            top = MAX_TOP
    snap = _load("snapshot.json")
    mb = _load("market_bars.json")
    mk = _load("industry_daily.json")
    if not mb.get("bars"):
        print("[screen] 缺 data/market_bars.json，先跑 python tools/build_market.py")
        return 1
    member_of, ind = build_industry_ctx(mk) if mk else ({}, {})
    universe: dict = {}
    for row in snap.get("rows") or []:
        code = str(row.get("code") or "")
        if not code:
            continue
        universe[code] = dict(row)
    for code, bar in (mb.get("bars") or {}).items():
        if code in universe:
            universe[code]["_bars"] = Bars(bar)
    n_bars = sum(1 for r in universe.values() if r.get("_bars") is not None)
    mkt = market_ctx()
    print(f"[screen] 全A {len(universe)} 只（其中有K线 {n_bars} 只）；快照日 {snap.get('date')}；"
          f"行业 {len(ind)} 个；沪深300 站上MA20={mkt.get('above_ma20')}")

    fa: dict = {}
    a, a_watch = scan_A(universe, member_of, ind, mkt, fa)
    fb: dict = {}
    b, b_watch = scan_B(universe, member_of, ind, mkt, top, fb)
    fc: dict = {}
    c = scan_C(universe, member_of, ind, top, fc)
    doc = {
        "date": (snap.get("date") or datetime.now().strftime("%Y-%m-%d")),
        "generated": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "universe": len(universe),
        "with_bars": n_bars,
        "market": mkt,
        "A": {"n": len(a), "rows": a[:top], "watch": a_watch, "funnel": fa,
              "checks": ["A1 行业排名前5", "A2 行业涨幅>1%", "S1 3日累计涨幅甜区",
                         "S2 排名上升或稳定前5", "S3 涨停≥2", "S4 近3日上榜≥2天",
                         "S5 涨跌比≥2", "A3 量能>1.5倍", "A4 收盘>MA5且MA5上翘",
                         "A5 距高点≤-30%", "A12 行业成交额前3"]},
        "B": {"n": len(b), "rows": b[:top], "watch": b_watch, "funnel": fb,
              "veto": ("大盘跌破MA20，B 模式全部否决（§10.3）" if mkt.get("above_ma20") == 0 else None)},
        "C": {"n": len(c), "rows": c[:top], "funnel": fc},
        "notes": [
            "全 A 只有 90 日 K 线：距1年高点用90日高点代理、量比用成交额倍数代理、收阳用当日涨跌代理",
            "C 长线是粗筛（PE/PB/距高点），PE 5年分位、股息率、连盈、负债率需要逐只补财务后才能定论",
            "A/B 的精细判定仍在池内 154 只上按完整条件执行；全 A 清单用于发现池外机会",
        ],
    }
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"[screen] A={len(a)}(观察 {len(a_watch)}) / B={len(b)}(观察 {len(b_watch)}) / C={len(c)} → {OUT_PATH}")
    print("[screen] A 漏斗：" + "，".join(f"{k} {v}" for k, v in fa.items()))
    for mode, rows in (("A", a[:8]), ("B", b[:8]), ("C", c[:8])):
        print(f"[screen] {mode}: " + "，".join(f"{r['name']}({r['code']})" for r in rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
