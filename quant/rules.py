# -*- coding: utf-8 -*-
"""规则引擎：把手册 §九（放量/止损）、§十（A/B/C 入场）、§八（估值线）逐条编码成可判定条件。

每条 check 都带 `clause`（手册条款号）与 `text`（中文原意），报告里会原文展示，
保证「程序给出的每个信号都能追溯到你手册的哪一条」。

对外接口（冻结，见 docs/DESIGN.md §4）：
    eval_mode_A(f) -> dict
    eval_mode_B(f) -> dict
    eval_mode_B_snapshot(f) -> dict   # §10.6 实盘快照口径（与旧报告对账）
    eval_mode_C(f) -> dict
"""
from __future__ import annotations

from typing import Any, Optional

from . import config as C


# ============================================================ 基础工具


def _chk(cid: str, clause: str, text: str, ok: Any,
         value: Any = None, need: Any = None, note: str = "") -> dict:
    """ok 允许 None（数据缺失）：None 不计入通过也不计入否决，但会在报告里单列。"""
    return {"id": cid, "clause": clause, "text": text, "ok": ok,
            "value": value, "need": need, "note": note}


def _num(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:  # nan
        return None
    return v


def _result(mode: str, checks: list[dict], *, required: list[str],
            min_ok: int, pass_extra: Optional[bool] = None,
            stop_loss=None, take_profit=None, max_hold_days=None,
            position_cap: float = 0.1, notes: Optional[list[str]] = None) -> dict:
    valid = [c for c in checks if c["ok"] is not None]
    n_ok = sum(1 for c in valid if c["ok"] is True)
    req_ok = all(any(c["id"] == r and c["ok"] is True for c in checks) for r in required)
    passed = bool(req_ok and n_ok >= min_ok and (pass_extra is not False))
    return {
        "mode": mode,
        "pass": passed,
        "score": round(n_ok / max(len(valid), 1), 3),
        "n_ok": n_ok,
        "n_req": max(min_ok, 1),
        "checks": checks,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "max_hold_days": max_hold_days,
        "position_cap": position_cap,
        "notes": notes or [],
    }


def _fmt_pct(x: Any) -> str:
    v = _num(x)
    return "—" if v is None else f"{v:+.2f}%"


# ============================================================ §九 放量判定


def volume_check(f: dict) -> dict:
    """§9.3 真放量四条 + §9.1 四口径，返回明细（报告「放量诊断」列用）。"""
    vol = _num(f.get("vol_ratio"))
    amt = _num(f.get("amount"))          # 万元
    amt_mult = _num(f.get("amt_ratio"))
    turn = _num(f.get("turnover"))
    up = f.get("up_candle")

    c1 = vol is not None and vol >= C.VOLUME["multiple_clear"]
    c2 = amt is not None and amt >= C.VOLUME["amount_min_yi"] * 1e4
    c3 = bool(amt_mult is not None and amt_mult >= C.VOLUME["amount_multiple_min"])
    c4 = turn is not None and C.VOLUME["turnover_low"] <= turn <= C.VOLUME["turnover_high"]
    c5 = up == 1
    n = sum([c1, c2, c3, c4, c5])
    if vol is None:
        level = "无数据"
    elif vol >= C.VOLUME["multiple_abnormal"]:
        level = "异常放量(>3倍，警惕出货)"
    elif vol >= C.VOLUME["multiple_strong"]:
        level = "强势放量(2~3倍)"
    elif vol >= C.VOLUME["multiple_clear"]:
        level = "明显放量(1.5~2倍)"
    elif vol >= C.VOLUME["multiple_warm"]:
        level = "温和放量(1.2~1.5倍)"
    else:
        level = "缩量"
    return {
        "level": level,
        "n_ok": n,
        # §9.3 真放量四条：①量倍数≥1.5 ②成交额≥5亿 ③换手率3~15% ④收阳线（四条全满足）
        "true_volume": bool(c1 and c2 and c4 and c5),
        "details": [
            _chk("V1", "§9.3①", "量比/量倍数 ≥1.5（明显放量）", c1, vol, ">=1.5"),
            _chk("V2", "§9.3②", "成交额 ≥5 亿", c2, round(amt / 1e4, 2) if amt else amt, ">=5亿"),
            _chk("V3", "§9.1③", "成交额 ÷ 前5日均额 ≥1.5", c3, amt_mult, ">=1.5"),
            _chk("V4", "§9.3③", "换手率在 3%~15%（健康 3~8%）", c4, turn, "3~15"),
            _chk("V5", "§9.3④", "收阳线（收盘>开盘）", c5, up, "=1"),
        ],
    }


# ============================================================ A 短线（§10.1/§10.2）


def eval_mode_A(f: dict, cfg=C) -> dict:
    """A 短线：只在「脉冲板块」里找放量突破，全部满足才买，−3% 硬止损、+6~8% 止盈。"""
    A = cfg.A_SCREEN
    m = cfg.MODES["A"]
    sector_rank = _num(f.get("sector_rank_today"))
    sector_chg = _num(f.get("sector_chg_today"))
    vol = _num(f.get("vol_ratio"))
    vol3 = _num(f.get("vol_ratio3"))
    close = _num(f.get("close"))
    ma5 = _num(f.get("ma5"))
    ma5_up = f.get("ma5_up")
    from_hi = _num(f.get("from_hi_1y"))
    box_break = f.get("box_break")
    mcap = _num(f.get("mktcap_yi"))
    turn = _num(f.get("turnover"))
    ret5 = _num(f.get("ret5"))
    name = (f.get("name") or "")
    py = _num(f.get("profit_years"))

    # §10.1.1 板块 3 天验证（2026-11-12 用户补充）
    S = cfg.A_SECTOR
    cum3 = _num(f.get("sector_cum3"))
    rank3ago = _num(f.get("sector_rank_3ago"))
    limit_up = _num(f.get("sector_limit_up"))
    top10_days = _num(f.get("sector_top10_days"))
    breadth = _num(f.get("sector_breadth"))
    amt_rank = _num(f.get("sector_amount_rank"))
    amt_n = _num(f.get("sector_amount_n"))

    # S1：3日累计涨幅必须落在「刚启动」甜区 +3%~+10%；+10~+15 只准轻仓；>+15 追高区禁止
    if cum3 is None:
        s1_ok, s1_note = None, "板块3日累计涨幅数据缺失"
    elif cum3 < S["cum3_min"]:
        s1_ok, s1_note = False, "没启动（3日累计 <+3%）"
    elif cum3 <= S["cum3_max"]:
        s1_ok, s1_note = True, "刚启动甜区（+3%~+10%）"
    elif cum3 <= S["cum3_heavy_max"]:
        s1_ok, s1_note = True, "已走一半（+10%~+15%）：只能轻仓，止损收紧到 -2%"
    else:
        s1_ok, s1_note = False, "追高区（>+15%，这是接盘位，禁止）"
    light_zone = bool(cum3 is not None and S["cum3_max"] < cum3 <= S["cum3_heavy_max"])

    # S2：今天排名上升、或稳定在前 5
    if sector_rank is None:
        s2_ok = None
    else:
        s2_ok = sector_rank <= S["rank_top_n"] and (rank3ago is None or sector_rank <= rank3ago)

    # S5：板块内涨跌比 ≥2 真普涨；1~2 结构行情只做龙头；<1 假涨不做
    if breadth is None:
        s5_ok, s5_note = None, "板块广度数据缺失"
    elif breadth >= S["breadth_min"]:
        s5_ok, s5_note = True, "真普涨（涨跌比≥2）"
    elif breadth >= S["breadth_leader_min"]:
        s5_ok = bool(amt_rank is not None and amt_rank <= S["buy_amount_rank_max"])
        s5_note = "结构行情（涨跌比1~2）：只做板块龙头"
    else:
        s5_ok, s5_note = False, "假涨（涨跌比<1，多半是权重拉的）"

    # 行业样本不足（池内该行业 <3 只）：板块排名/热度不可计算，A 短线不适用
    thin = bool(f.get("sector_thin"))
    n_mem = _num(f.get("sector_n_members"))
    thin_note = (f"该行业池内仅 {int(n_mem)} 只，算不了板块热度，A 短线不适用"
                 if thin else "")

    checks = [
        _chk("A1", "§10.1①", f"所属板块当日涨幅排名前 {A['sector_top_n']}",
             sector_rank is not None and sector_rank <= A["sector_top_n"],
             (f"样本不足（{int(n_mem)}只）" if thin else sector_rank),
             f"<={A['sector_top_n']}", note=thin_note),
        _chk("A2", "§10.1①", "板块指数当日涨幅 >1%（板块集体启动，不是个股独舞）",
             sector_chg is not None and sector_chg > A["sector_min_chg_pct"],
             _fmt_pct(sector_chg), ">+1%"),
        # ---- §10.1.1 板块 3 天验证
        _chk("S1", "§10.1.1①", "板块3日累计涨幅在 +3%~+10%（刚启动甜区）",
             s1_ok, _fmt_pct(cum3), "+3%~+10%", note=s1_note),
        _chk("S2", "§10.1.1②", "板块排名上升、或稳定在前5（对比3个交易日前）",
             s2_ok, f"今天第{int(sector_rank) if sector_rank else '—'}名 / "
                    f"3日前第{int(rank3ago) if rank3ago else '—'}名", "上升或稳定前5"),
        _chk("S3", "§10.1.1③", f"板块内涨停家数 ≥{S['limit_up_min']}",
             None if limit_up is None else limit_up >= S["limit_up_min"],
             limit_up, f">={S['limit_up_min']}"),
        _chk("S4", "§10.1.1④", f"过去{S['top10_window']}天有 ≥{S['top10_days_min']} 天在涨幅前10",
             None if top10_days is None else top10_days >= S["top10_days_min"],
             top10_days, f">={S['top10_days_min']}天"),
        _chk("S5", "§10.1.1广度", "板块内涨跌比 ≥2（真普涨；1~2 只做龙头）",
             s5_ok, f"{breadth:.2f}" if breadth not in (None, float("inf")) else "∞",
             ">=2", note=s5_note),
        _chk("A12", "§10.1.1买前3", f"板块内成交额排名前 {S['buy_amount_rank_max']}（4~8 是跟风股，不买）",
             None if amt_rank is None else amt_rank <= S["buy_amount_rank_max"],
             (f"第{int(amt_rank)}/{int(amt_n)}名" if amt_rank and amt_n
              else (f"第{int(amt_rank)}名" if amt_rank else None)),
             f"<={S['buy_amount_rank_max']}"),
        _chk("A3", "§10.1②", "量比 >1.5，或近3日成交额放大 >1.5 倍",
             (vol is not None and vol >= A["vol_multiple_min"]) or
             (vol3 is not None and vol3 >= A["vol_ratio_3d_min"]),
             f"量比{vol} / 3日{vol3}", ">=1.5"),
        _chk("A4", "§10.1③", "收盘 > MA5 且 MA5 向上",
             f.get("above_ma5") == 1 and ma5_up == 1,
             f"above5={f.get('above_ma5')} ma5_up={ma5_up}", "both=1"),
        _chk("A5", "§10.1④", "股价在低位：距1年高点 ≤−30%，或刚突破横盘平台",
             (from_hi is not None and from_hi <= A["from_hi_1y_max"]) or box_break == 1,
             f"{_fmt_pct(from_hi)} / box_break={box_break}", "<=−30% or 突破平台"),
        _chk("A6", "§10.1⑤", f"市值 {A['mktcap_min_yi']:.0f}~{A['mktcap_max_yi']:.0f} 亿（太小易被操纵，太大拉不动）",
             mcap is not None and A["mktcap_min_yi"] <= mcap <= A["mktcap_max_yi"],
             round(mcap, 1) if mcap else mcap, "50~800亿"),
        _chk("A7", "§10.1⑤", f"股价 {A['price_min']:.0f}~{A['price_max']:.0f} 元",
             close is not None and A["price_min"] <= close <= A["price_max"],
             close, "5~50元"),
        _chk("A8", "§10.1排除", "非 ST / 非退市风险 / 非连亏两年",
             ("ST" not in name and "退" not in name) and (py is None or py >= 1),
             f"name={name} 连盈{py}年", "非ST且非连亏"),
        _chk("A9", "§10.1排除", "近5日涨幅 ≤25%（不追短期爆炒）",
             ret5 is not None and ret5 <= A["exclude_ret5_gt"],
             _fmt_pct(ret5), "<=25%"),
        _chk("A10", "§10.1排除", "换手率 ≤20%（>20% 多是妖股/出货）",
             turn is None or turn <= A["exclude_turnover_gt"],
             turn, "<=20%"),
        _chk("A11", "§10.1排除", "近3个月无减持/解禁公告",
             None, None, "人工复核", note="公告数据未接入，需在交易日志人工确认"),
    ]
    notes = []
    if f.get("sector_chg_today") is not None and _num(f.get("sector_chg_today")) <= 1:
        notes.append("板块未集体启动：手册要求「板块集体启动」，单只放量追高风险大")
    if vol is not None and vol < C.VOLUME["multiple_clear"]:
        notes.append("量能未达 1.5 倍，属于脉冲不足")
    if light_zone:
        notes.append("板块3日已走一半（+10%~+15%）：按 §10.1.1 只准轻仓，止损收紧到 −2%")
    stop_a = S["light_stop_pct"] if light_zone else (m["stop_pct"] if "stop_pct" in m else -3.0)
    req = ["A1", "A2", "S1", "S2", "S3", "S4", "S5", "A12",
           "A3", "A4", "A5", "A6", "A7", "A8", "A9", "A10"]
    soft = set(S.get("soft_checks") or [])
    req = [r for r in req if r not in soft]
    if soft:
        notes.append("S3（涨停家数）/S5（涨跌比）按池内样本只能作强度参考，不阻断（池子只覆盖行业部分成分股）")
    return _result("A", checks, required=req, min_ok=len(req),
                   stop_loss=stop_a,
                   take_profit=m.get("take_profit_pct", 8.0),
                   max_hold_days=m.get("max_hold_days_hard", 5),
                   position_cap=m["single_cap"], notes=notes)


# ============================================================ B 中线波段（§10.3）


def eval_mode_B(f: dict, cfg=C) -> dict:
    """B 中线波段：四信号至少 2 个，且信号①（站上MA20且MA20走平/上翘）必选。"""
    B = cfg.B_SIGNALS
    m = cfg.MODES["B"]
    close = _num(f.get("close"))
    low = _num(f.get("low"))
    ma20 = _num(f.get("ma20"))
    ma60 = _num(f.get("ma60"))
    slope20 = _num(f.get("ma20_slope"))
    vol = _num(f.get("vol_ratio"))
    box_break = f.get("box_break")
    sector_ok = f.get("sector_above_ma20")
    idx_ok = f.get("index_above_ma20")
    up = f.get("up_candle")
    sector_chg = _num(f.get("sector_chg_today"))
    sector_ret5 = _num(f.get("sector_ret5"))

    # ① 必选：收盘 > MA20 且 MA20 走平或上翘（走平容忍 −0.1%/5日）
    s1 = (f.get("above_ma20") == 1) and (slope20 is not None and slope20 >= -0.1)
    # ② 回踩 MA20 不破 + 止跌收阳（强势股首选买点）
    pullback = (low is not None and ma20 is not None and close is not None and
                low <= ma20 * 1.01 and close >= ma20 * 0.995 and up == 1)
    # ③ 放量突破近 60 日箱体上沿
    s3 = box_break == 1 and vol is not None and vol >= B["box_break_vol_multiple"]
    # ④ 板块共振：板块同时站上 MA20
    s4 = sector_ok == 1

    checks = [
        _chk("B1", "§10.3①", "收盘 > MA20 且 MA20 走平或上翘（必选）", bool(s1),
             f"close={close} ma20={ma20} 斜率={_fmt_pct(slope20)}", "both"),
        _chk("B2", "§10.3②", "回踩 MA20 不破、止跌收阳（强势股首选买点）", bool(pullback),
             f"low={low} 收阳={up}", "low≤MA20+1% 且 close≥MA20"),
        _chk("B3", "§10.3③", "放量突破近60日箱体上沿（量>1.5倍）", bool(s3),
             f"box_break={box_break} 量比={vol}", "突破+量>1.5"),
        _chk("B4", "§10.3④", "所属板块同时站上 MA20（板块共振）", bool(s4),
             f"sector_above_ma20={sector_ok}", "=1"),
    ]
    # 否决项（§10.3 绝对不买）
    vetoes = []
    if slope20 is not None and slope20 < 0:
        vetoes.append(_chk("BX1", "§10.3绝对不买", "MA20 向下时不买", False,
                           _fmt_pct(slope20), ">=0"))
    if idx_ok == 0:
        vetoes.append(_chk("BX2", "§10.3绝对不买", "大盘（沪深300）跌破 MA20 时不买", False,
                           idx_ok, "=1"))
    if sector_chg is not None and sector_ret5 is not None and sector_chg > 0.5 and sector_ret5 <= 0:
        vetoes.append(_chk("BX3", "§10.3绝对不买", "板块一日游（仅当天涨、5日没跟上）不买", False,
                           f"当日{_fmt_pct(sector_chg)}/5日{_fmt_pct(sector_ret5)}", "板块有连续性"))
    checks += vetoes

    n_sig = sum([bool(s1), bool(pullback), bool(s3), bool(s4)])
    notes = []
    if vetoes:
        notes.append("触发「绝对不买」条款：" + "；".join(v["text"] for v in vetoes))
    if n_sig == 1:
        notes.append("只满足 1 个信号：手册要求至少 2 个，加入观察名单等第二个信号")
    if f.get("close") is not None and ma60 is not None and _num(f.get("close")) < ma60:
        notes.append("仍在 MA60 下方：波段趋势未确立，仓位减半")
    return _result("B", checks, required=["B1"], min_ok=2,
                   pass_extra=not vetoes,
                   stop_loss=None, take_profit=m.get("take_profit_pct", 30.0),
                   max_hold_days=60, position_cap=m["single_cap"], notes=notes)


def eval_mode_B_snapshot(f: dict, cfg=C) -> dict:
    """§10.6 实盘快照口径：站上MA20 + 站上MA60 + MA20上翘 + MA60上翘（四条全满足）。

    这个口径是用户旧报告（152 只筛出 28 只）用的，保留以便对账；
    主口径仍是 eval_mode_B（§10.3 四信号）。
    """
    slope20 = _num(f.get("ma20_slope"))
    slope60 = _num(f.get("ma60_slope"))
    checks = [
        _chk("BS1", "§10.6", "收盘 > MA20", f.get("above_ma20") == 1, f.get("above_ma20"), "=1"),
        _chk("BS2", "§10.6", "收盘 > MA60", f.get("above_ma60") == 1, f.get("above_ma60"), "=1"),
        _chk("BS3", "§10.6", "MA20 上翘（5日斜率>0）", slope20 is not None and slope20 > 0,
             _fmt_pct(slope20), ">0"),
        _chk("BS4", "§10.6", "MA60 上翘（5日斜率>0）", slope60 is not None and slope60 > 0,
             _fmt_pct(slope60), ">0"),
    ]
    return _result("B", checks, required=["BS1", "BS2", "BS3", "BS4"], min_ok=4,
                   take_profit=cfg.MODES["B"].get("take_profit_pct", 30.0),
                   max_hold_days=60, position_cap=cfg.MODES["B"]["single_cap"],
                   notes=["快照口径（§10.6），非 §10.3 主口径"])


# ============================================================ C 长线（§10.4）


def eval_mode_C(f: dict, cfg=C) -> dict:
    """C 长线：估值分位 + 分红/质量 + 连续盈利 + 位置，四个维度全满足才建仓。"""
    En = cfg.C_ENTRY
    pe_pct = _num(f.get("pe_pct_5y"))
    div = _num(f.get("div_yield"))
    roe = _num(f.get("roe"))
    pb = _num(f.get("pb"))
    py = _num(f.get("profit_years"))
    debt = _num(f.get("debt"))
    gross = _num(f.get("gross"))
    from_hi = _num(f.get("from_hi_1y"))

    c1 = pe_pct is not None and pe_pct < En["pe_pct_max"]
    div_route = div is not None and div > En["div_yield_min"]
    quality_route = (roe is not None and roe > En["or_roe_min"] and
                     pb is not None and pb < En["or_pb_max"])
    c2 = div_route or quality_route
    c3 = (py is not None and py >= En["profit_years_min"] and
          debt is not None and debt < En["debt_max"])
    c4 = from_hi is not None and from_hi <= En["from_hi_1y_max"]

    checks = [
        _chk("C1", "§10.4①", "PE 处于近5年历史分位 <30%（低估区）", c1,
             round(pe_pct, 1) if pe_pct is not None else None, "<30"),
        _chk("C2", "§10.4②", "股息率 >4%，或 ROE>10% 且 PB<1.5（有真实现金回报）", c2,
             f"股息{div}/ROE{roe}/PB{pb}", ">4% 或 ROE>10%且PB<1.5",
             note="银行看股息+PB；消费看ROE+PB"),
        _chk("C3", "§10.4③", "连续 5 年盈利 + 资产负债率 <70%", c3,
             f"连盈{py}年/负债{debt}%", ">=5年且<70%"),
        _chk("C4", "§10.4④", "距1年高点 ≤−20%（不在高位接盘）", c4,
             _fmt_pct(from_hi), "<=−20%"),
        _chk("C5", "§10.4③", "毛利率稳定（最新毛利率可用且 >0）",
             None if gross is None else gross > 0, gross, ">0",
             note="历史毛利率稳定性需人工复核季报"),
    ]
    notes = [
        "长线是「分5批、每批20%」建仓（§10.4）：本信号只给第1批，不是一次买满",
    ]
    if c2 and div_route:
        notes.append("走「高股息」路线：股息率>4%，适合守息仓")
    if c2 and not div_route and quality_route:
        notes.append("走「质量」路线：ROE>10% 且 PB<1.5")
    req = ["C1", "C2", "C3", "C4"] + (["C5"] if gross is not None else [])
    return _result("C", checks, required=req, min_ok=len(req),
                   stop_loss=None, take_profit=None, max_hold_days=730,
                   position_cap=cfg.MODES["C"]["single_cap"], notes=notes)


# ============================================================ 截面筛选（A 短线）

MODE_PREFERRED_SECTORS = {
    "A": {"finance", "tech", "consume", "other"},
}


def screen_A(feats: list[dict], cfg=C) -> list[dict]:
    """§10.1：先按板块强度截取「当日涨幅前5且涨幅>1%」的板块，再逐只判定 A 条件。"""
    A = cfg.A_SCREEN
    out = []
    for f in feats:
        r = eval_mode_A(f, cfg)
        if r["pass"]:
            rr = dict(f)
            rr["_rule"] = r
            out.append(rr)
    # 排序：板块排名越靠前越优先，其次量比
    def key(x):
        rk = _num(x.get("sector_rank_today"))
        vol = _num(x.get("vol_ratio")) or 0.0
        return (-(100 - (rk if rk is not None else 99)), -vol)
    return sorted(out, key=key)


def best_mode(f: dict, cfg=C) -> dict:
    """给单只股票同时跑 A/B/C，返回通过的模式里「最符合手册适用场景」的一个。"""
    res = {"A": eval_mode_A(f, cfg), "B": eval_mode_B(f, cfg), "C": eval_mode_C(f, cfg)}
    order = ["B", "C", "A"]  # 修复期优先中线波段，其次长线，最后短线
    for m in order:
        if res[m]["pass"]:
            out = dict(res[m])
            out["all"] = {k: v["pass"] for k, v in res.items()}
            return out
    out = dict(res["B"])
    out["all"] = {k: v["pass"] for k, v in res.items()}
    out["pass"] = False
    return out


# ============================================================ §八 估值线


def valuation_view(f: dict, cfg=C) -> dict:
    """按 §8 的估值参考线给出「低估/合理/高估」。细分子行业数据不全时用所属大类口径。"""
    sector = f.get("sector") or ""
    pe = _num(f.get("pe_ttm"))
    pb = _num(f.get("pb"))
    peg = _num(f.get("peg"))
    pe_pct = _num(f.get("pe_pct_5y"))
    div = _num(f.get("div_yield"))

    def _line(name: str, val, op: str, thr: float) -> dict:
        if val is None:
            return {"line": name, "value": None, "status": "无数据", "op": op, "thr": thr}
        v = _num(val)
        ok = (v < thr) if op == "<" else (v > thr)
        return {"line": name, "value": round(v, 2), "status": "满足" if ok else "不满足",
                "op": op, "thr": thr}

    if sector == "finance":
        rows = [_line("银行/PB（<0.7 建仓，<0.6 重仓，>1.2 减仓）", pb, "<", 0.7),
                _line("银行/股息率（>5% 才加）", div, ">", 5.0)]
        tag = "金融用 PB+股息率，不看 PE"
    elif sector == "tech":
        rows = [_line("半导体 PEG（<1 建仓，<0.8 重仓，>2 泡沫）", peg, "<", 1.0)]
        tag = "科技用 PEG=PE÷净利润增速（PE 高时买、低时卖）"
    elif sector == "consume":
        rows = [_line("消费 PE（<25 低估，<20 重仓，>40 减）", pe, "<", 25.0)]
        tag = "消费用 PE 近5年分位/绝对 PE"
    else:
        rows = [_line("其他 PE（反着用：高PE买、低PE警惕周期顶）", pe, ">", 30.0)]
        tag = "其他/周期类：PE 反着用"
    n_ok = sum(1 for r in rows if r["status"] == "满足")
    return {"sector": sector, "tag": tag, "rows": rows, "n_ok": n_ok,
            "pe_pct_5y": pe_pct}


# ============================================================ 汇总工具


def passes_of(results: dict[str, dict]) -> dict[str, bool]:
    return {k: bool(v.get("pass")) for k, v in results.items()}


def failed_text(result: dict, max_n: int = 6) -> str:
    """未通过条件拼成一句中文，方便报告展示。"""
    bad = [f"{c['id']} {c['text']}（{c['value']}）" for c in result.get("checks", [])
           if c["ok"] is False]
    return "；".join(bad[:max_n]) if bad else "无（全部满足）"
