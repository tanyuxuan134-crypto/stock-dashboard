# -*- coding: utf-8 -*-
"""风控与仓位：把手册 §四（门槛刻度）与 §五（仓位纪律七条）变成可执行的约束。

设计目标：不管规则层给出多少个"通过"的标的，最终下单数量都要过这里，
一条不过就拒绝，并把理由写成人话（报告里展示）。
"""
from __future__ import annotations

from typing import Any, Optional

from . import config as C


def _num(x: Any) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if v != v else v


# 板块 key → 中文名（报告里直接显示，不暴露 tech/finance 这类内部键）
SECTOR_CN = {"finance": "大金融", "consume": "消费", "tech": "科技", "other": "其他", "cash": "现金"}


def _sec_cn(sec: Any) -> str:
    return SECTOR_CN.get(str(sec), str(sec))


# ---------------------------------------------------------------- §四 门槛刻度


def tier_of(total_asset: float) -> dict:
    """按总资产返回当前适用的门槛档位（§四）。"""
    for t in C.TIER_TABLE:
        if total_asset < t["asset_max"]:
            return dict(t)
    return dict(C.TIER_TABLE[-1])


def stock_cap_now(total_asset: float) -> float:
    """现阶段单只个股可用上限：修复期（总资产<10万）取 10%，否则 20%（§四/§五①）。"""
    if total_asset < C.OPEN_FIRST_STOCK_ASSET:
        return C.POSITION_LIMITS["single_stock_cap_now"]
    return C.POSITION_LIMITS["single_stock_cap"]


def can_open_new_stock(total_asset: float) -> tuple[bool, str]:
    """§四：总资产到 10 万再开第一个个股仓；之前只买 ETF。"""
    if total_asset < C.OPEN_FIRST_STOCK_ASSET:
        return False, (f"总资产 {total_asset:,.0f} 元 < 10 万：手册 §四要求「总资产到 10 万再开第一个个股仓」，"
                       f"现阶段只买宽基ETF，先把 ETF 底仓建到 {C.ETF_FIRST_TARGET:,.0f} 元（⭐关键一步）")
    return True, "总资产已过 10 万，可以正式配置个股"


# ---------------------------------------------------------------- §五 仓位纪律


def check_portfolio(positions: list[dict], total_asset: float,
                    sector_map: Optional[dict[str, str]] = None,
                    cash: Optional[float] = None) -> dict:
    """检查现有组合是否违反 §五 纪律。

    positions: [{"code","name","sector","value"}]（value=市值，元）
    """
    sector_map = sector_map or {}
    total = max(total_asset, 1.0)
    cash = total - sum(_num(p.get("value")) or 0 for p in positions) if cash is None else cash
    viol = []

    cap_now = stock_cap_now(total)
    for p in positions:
        v = _num(p.get("value")) or 0.0
        pct = v / total
        if pct > cap_now + 1e-9:
            viol.append({
                "rule": "§五①", "level": "高",
                "text": f"{p.get('name') or p.get('code')} 占总资产 {pct:.1%} > 上限 {cap_now:.0%}",
                "fix": f"减到 {cap_now:.0%} 以内（约卖 {v - total * cap_now:,.0f} 元）"})

    by_sector: dict[str, float] = {}
    for p in positions:
        sec = p.get("sector") or sector_map.get(str(p.get("code")), "other")
        by_sector[sec] = by_sector.get(sec, 0.0) + (_num(p.get("value")) or 0.0)
    for sec, v in by_sector.items():
        pct = v / total
        if pct > C.POSITION_LIMITS["single_sector_cap"] + 1e-9:
            viol.append({
                "rule": "§五②", "level": "高",
                "text": f"「{_sec_cn(sec)}」板块合计 {pct:.1%} > 40% 上限",
                "fix": f"减到 40% 以内（约减 {v - total * 0.4:,.0f} 元）"})

    cash_pct = cash / total
    if cash_pct < C.POSITION_LIMITS["min_cash"] - 1e-9:
        viol.append({
            "rule": "§五③", "level": "中",
            "text": f"现金/短融 {cash_pct:.1%} < 15% 下限（子弹打光了）",
            "fix": f"卖出或赎回约 {total * 0.15 - cash:,.0f} 元补到 15%"})

    n_etf = sum(1 for p in positions if str(p.get("code", "")).startswith(("51", "58")))
    if n_etf == 0:
        viol.append({
            "rule": "§二/§四", "level": "高",
            "text": "组合里没有任何 ETF 底仓（核心仓+守息仓应为 60%）",
            "fix": f"先用减仓资金买入宽基ETF，目标 {C.ETF_FIRST_TARGET:,.0f} 元以上"})

    return {"violations": viol, "n_violations": len(viol), "cash_pct": round(cash_pct * 100, 2),
            "max_single_pct": round(max([(_num(p.get('value')) or 0) / total for p in positions] or [0]) * 100, 2),
            "by_sector": {k: round(v / total * 100, 2) for k, v in by_sector.items()}}


def check_buy_intent(intent: dict, *, total_asset: float, cash: float,
                     positions: list[dict], mode: str = "B",
                     is_averaging_down: bool = False) -> dict:
    """检查一笔买入意图是否违反 §五。

    intent: {"code","name","sector","price","shares"}；shares 可 None（由 size 决定）
    """
    reasons, warns = [], []
    price = _num(intent.get("price")) or 0.0
    shares = int(intent.get("shares") or 0)
    cost = price * shares
    total = max(total_asset, 1.0)
    cap = min(stock_cap_now(total), C.MODES.get(mode, {}).get("single_cap", 0.20))

    if is_averaging_down:
        reasons.append("§五④ 绝不向下补仓：止损先于补仓——已经亏钱的仓位不能加")
    if shares <= 0:
        reasons.append("股数为 0（资金不足或价格超出）")
    if cost > cash + 1e-6:
        reasons.append(f"可用资金 {cash:,.0f} 元 < 买入金额 {cost:,.0f} 元")
    existing = next((p for p in positions if str(p.get("code")) == str(intent.get("code"))), None)
    cur_val = _num(existing.get("value")) if existing else 0.0
    if (cur_val + cost) / total > cap + 1e-9:
        reasons.append(f"§五① 买入后该股占总资产 {(cur_val + cost) / total:.1%} > {cap:.0%} 上限")
    if mode == "A" and existing:
        reasons.append("§五⑤ 短线仓不加仓（同一只短线标的不允许补第二笔）")
    if mode == "A":
        a_total = sum(_num(p.get("value")) or 0 for p in positions if p.get("mode") == "A")
        if (a_total + cost) / total > C.MODES["A"]["bucket_cap"] + 1e-9:
            reasons.append(f"§三 A 模式总短线仓 {(a_total + cost) / total:.1%} > 20% 上限")
    sec = intent.get("sector") or "other"
    sec_val = sum(_num(p.get("value")) or 0 for p in positions
                  if (p.get("sector") or "other") == sec)
    if (sec_val + cost) / total > C.POSITION_LIMITS["single_sector_cap"] + 1e-9:
        reasons.append(f"§五② 「{_sec_cn(sec)}」板块买入后 {(sec_val + cost) / total:.1%} > 40% 上限")
    if cost > 0 and (cash - cost) / total < C.POSITION_LIMITS["min_cash"]:
        warns.append(f"§五③ 买完现金占比会降到 {(cash - cost) / total:.1%}，低于 15% 下限")
    if mode in ("B", "C") and total < C.OPEN_FIRST_STOCK_ASSET:
        warns.append("§四 总资产 <10 万，手册建议现阶段先买 ETF，个股只做观察")
    return {"ok": not reasons, "cost": round(cost, 2), "reasons": reasons, "warns": warns,
            "cap_pct": round(cap * 100, 1)}


def size_position(price: float, budget: float, lot: int = 100) -> int:
    """按预算和整手规则算股数（向下取整到 100 股）。"""
    if not price or price <= 0 or budget <= 0:
        return 0
    return int(budget / price / lot) * lot


# ---------------------------------------------------------------- §9.5 止损止盈


def stop_take_for(mode: str, entry_px: float, f: Optional[dict] = None) -> dict:
    """按模式算止损价/止盈价（A 用固定百分比；B 用均线；C 只做逻辑止损）。"""
    entry_px = float(entry_px)
    f = f or {}
    if mode == "A":
        st = C.STOP_TAKE["A"]
        return {"mode": "A", "stop_px": round(entry_px * (1 + st["stop"] / 100), 2),
                "take_px": round(entry_px * (1 + st["take_full"] / 100), 2),
                "trim_px": round(entry_px * (1 + st["take_half"] / 100), 2),
                "desc": f"−{abs(st['stop']):.0f}% 硬止损 / +{st['take_full']:.0f}% 全部卖出"
                        f"（+{st['take_half']:.0f}% 可先减半）/ 收盘破5日线也走 / 3天不涨走"}
    if mode == "B":
        ma20 = _num(f.get("ma20"))
        ma60 = _num(f.get("ma60"))
        st = C.STOP_TAKE["B"]
        return {"mode": "B",
                "stop_px": round(ma20 * 0.995, 2) if ma20 else None,
                "clear_px": round(ma60 * 0.995, 2) if ma60 else None,
                "take_px": round(entry_px * (1 + st["take_trim_low"] / 100), 2),
                "take_px_high": round(entry_px * (1 + st["take_trim"] / 100), 2),
                "desc": (f"跌破 MA20（≈{ma20:.2f}）减半 / 跌破 MA60（≈{ma60:.2f}）清仓 / "
                         f"+{st['take_trim_low']:.0f}~{st['take_trim']:.0f}% 减 1/3 / 破5日线先减1/3")
                if ma20 and ma60 else "均线数据不足，无法给出均线止损位"}
    st = C.STOP_TAKE["C"]
    return {"mode": "C", "stop_px": None,
            "take_px": None,
            "desc": (f"逻辑止损（季报连续2季下滑/行业逻辑破坏/立案造假即卖，不看价格）；"
                     f"PE分位>{st['pe_pct_trim']:.0f}% 减半、>{st['pe_pct_clear']:.0f}% 清仓")}


# ---------------------------------------------------------------- 组合风险指标


def risk_summary(positions: list[dict], total_asset: float) -> dict:
    """简单风险体检：集中度、板块分布、最大回撤敏感度（用止损位估算）。"""
    total = max(total_asset, 1.0)
    rows = []
    for p in positions:
        v = _num(p.get("value")) or 0.0
        rows.append({
            "code": p.get("code"), "name": p.get("name"),
            "pct": round(v / total * 100, 1),
            "stop_px": p.get("stop_px"), "price": p.get("price"),
        })
    rows.sort(key=lambda r: -r["pct"])
    hhi = sum((_num(p.get("value")) or 0) ** 2 for p in positions) / (total ** 2)
    return {"positions": rows, "hhi": round(hhi, 4), "n_positions": len(positions)}
