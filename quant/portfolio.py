# -*- coding: utf-8 -*-
"""组合层：四层资产配置（§二）、板块配比（§五）、现有持仓处置（§七）与「ETF 优先」修复路线（§四）。

这里不产生新信号，只回答两个问题：
1）我现在这个账户，按手册应该长什么样（目标结构）？
2）要走到目标结构，下一步具体做什么、卖多少、买多少？
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from . import config as C


def _num(x: Any) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if v != v else v


# ---------------------------------------------------------------- 账户与持仓


def load_account() -> dict:
    """从 data/pool_source.json 读账户快照（旧工作簿口径），缺省用 config。"""
    path = os.path.join(C.DATA, "pool_source.json")
    acc = dict(C.ACCOUNT)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            src = json.load(fh)
        a = (src.get("sections") or {}).get("account") or {}
        for k, v in a.items():
            if _num(v) is not None:
                acc[k] = float(v)
            else:
                acc[k] = v
    return acc


def load_holdings() -> list[dict]:
    """读旧工作簿的 5 只持仓；失败则用 config.HOLDINGS。"""
    path = os.path.join(C.DATA, "pool_source.json")
    out = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            src = json.load(fh)
        rows = (src.get("sections") or {}).get("holdings") or []
        seen = set()
        for r in rows:
            code = str(r.get("代码") or r.get("code") or "").zfill(6)
            shares = _num(r.get("holding") or r.get("持股数") or r.get("shares"))
            if not code or code == "000000" or code in seen or not shares:
                continue
            seen.add(code)
            out.append({
                "code": code,
                "name": r.get("名称") or r.get("name") or "",
                "shares": shares,
                "cost": _num(r.get("成本") or r.get("cost")) or None,
                "loss_pct": _num(r.get("盈亏") or r.get("loss_pct")),
            })
    if not out:
        out = [{"code": h["code"], "name": h["name"], "shares": h["shares"], "cost": h["cost"]}
               for h in C.HOLDINGS]
    # 合并 §七 的处置意见
    advice = {h["code"]: h for h in C.HOLDINGS}
    for h in out:
        a = advice.get(h["code"], {})
        h["action"] = a.get("action", "持有观察")
        h["reason"] = a.get("reason", "")
        h["trigger"] = a.get("trigger", "")
        h["stop_level"] = a.get("stop_level")
        h["delta_pct"] = a.get("delta_pct", 0.0)
    return out


# ---------------------------------------------------------------- 持仓处置（§七）


def holdings_plan(holdings: list[dict], prices: dict[str, float],
                  total_asset: float) -> dict:
    """逐只给出：现价、市值、盈亏、手册动作、本次建议卖出股数、释放资金、止损价。"""
    total = max(total_asset, 1.0)
    rows, cash_release = [], 0.0
    for h in holdings:
        px = _num(prices.get(h["code"]))
        shares = _num(h.get("shares")) or 0
        cost = _num(h.get("cost"))
        value = (px or 0) * shares
        pnl_pct = ((px / cost - 1) * 100) if px and cost else None
        delta = _num(h.get("delta_pct")) or 0.0
        sell_shares = int(abs(delta) * shares / 100) * 100 if delta else 0
        if delta and sell_shares == 0 and shares > 0:
            sell_shares = int(shares)  # 零碎股：整笔卖出
        stop = None
        if h.get("stop_level"):
            stop = h["stop_level"]
        elif px and h.get("action") == "持有":
            stop = round(px * 0.97, 2)
        action = h.get("action")
        reason = h.get("reason")
        # 条件触发：手册写「跌破止损线减半」的持有仓，价格已破位时由「持有」升级为「减半」
        if action == "持有" and h.get("stop_level") and px and px < h["stop_level"]:
            action = "减半（已跌破止损线）"
            sell_shares = max(int(shares // 2 // 100) * 100, 100) if shares >= 200 else int(shares)
            reason = (f"现价 {px:.2f} 低于止损线 {h['stop_level']:.2f}，触发手册「跌破减半」条款"
                      + (f"；原注：{h.get('reason')}" if h.get("reason") else ""))
        released = sell_shares * (px or 0)
        cash_release += released
        rows.append({
            "code": h["code"], "name": h["name"], "shares": int(shares),
            "cost": cost, "price": px, "value": round(value, 2),
            "pct_of_total": round(value / total * 100, 2),
            "pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
            "action": action, "reason": reason,
            "trigger": h.get("trigger"), "stop_px": stop,
            "sell_shares": sell_shares, "release_cash": round(released, 2),
            "keep_shares": int(shares - sell_shares),
        })
    rows.sort(key=lambda r: -(r["pct_of_total"] or 0))
    return {"rows": rows, "cash_release": round(cash_release, 2),
            "n": len(rows),
            "same_chain_warning": _same_chain_warning(rows)}


def _same_chain_warning(rows: list[dict]) -> Optional[str]:
    """五只半导体同链是手册 §一 点名的最大错误，这里做实时提醒。"""
    semi = [r for r in rows if r["code"] in {"600183", "603986", "000021", "002156", "002185"}]
    if len(semi) >= 3:
        tot = sum(r["value"] for r in semi)
        return (f"五只半导体同产业链（覆铜板/存储/封测）合计 {tot:,.0f} 元，"
                f"同一笔风险暴露 5 次——这正是手册 §一 的第 3 条前提所批评的结构")
    return None


# ---------------------------------------------------------------- 四层资产（§二）


def layer_targets(total_asset: float) -> list[dict]:
    out = []
    for layer in C.LAYERS:
        out.append({**layer, "target_value": round(total_asset * layer["pct"], 2)})
    return out


def classify_code(code: str, sector_map: Optional[dict] = None) -> str:
    """代码 → 四层归属：ETF 按 ETF_ROLES；个股按板块归入卫星仓。"""
    code = str(code)
    if code in C.ETF_ROLES:
        return C.ETF_ROLES[code][0]
    if code.startswith(("51", "58", "56", "15", "16")):
        return "core" if code in ("510300", "510500") else "satellite"
    return "satellite"


def layer_status(holdings: list[dict], prices: dict[str, float],
                 total_asset: float) -> list[dict]:
    """当前四层实际占比 vs 目标占比，给出缺口。"""
    actual = {l["key"]: 0.0 for l in C.LAYERS}
    for h in holdings:
        px = _num(prices.get(h["code"])) or 0.0
        layer = classify_code(h["code"])
        actual[layer] = actual.get(layer, 0.0) + px * (_num(h.get("shares")) or 0)
    # 现金层：账户可用资金
    acc = load_account()
    actual["cash"] = _num(acc.get("cash")) or 0.0
    total = max(total_asset, 1.0)
    rows = []
    for layer in C.LAYERS:
        cur = actual.get(layer["key"], 0.0)
        rows.append({
            "key": layer["key"], "name": layer["name"], "clause": layer["clause"],
            "target_pct": layer["pct"] * 100,
            "actual_pct": round(cur / total * 100, 2),
            "target_value": round(total * layer["pct"], 2),
            "actual_value": round(cur, 2),
            "gap_value": round(total * layer["pct"] - cur, 2),
            "content": layer["content"],
        })
    return rows


# ---------------------------------------------------------------- 板块配比（§五）

SECTOR_CN = {"finance": "大金融", "consume": "消费", "tech": "科技", "other": "其他", "cash": "现金"}


def sector_targets(profile: str = C.DEFAULT_PROFILE) -> dict:
    prof = C.SECTOR_PROFILES.get(profile) or C.SECTOR_PROFILES[C.DEFAULT_PROFILE]
    return {k: v * 100 for k, v in prof.items()}


def sector_status(holdings: list[dict], prices: dict[str, float],
                  sector_map: dict[str, str], total_asset: float,
                  profile: str = C.DEFAULT_PROFILE) -> list[dict]:
    actual = {k: 0.0 for k in SECTOR_CN}
    for h in holdings:
        sec = sector_map.get(str(h["code"]), "other")
        px = _num(prices.get(h["code"])) or 0.0
        actual[sec] = actual.get(sec, 0.0) + px * (_num(h.get("shares")) or 0)
    acc = load_account()
    actual["cash"] = _num(acc.get("cash")) or 0.0
    total = max(total_asset, 1.0)
    tgt = sector_targets(profile)
    rows = []
    for k, cn in SECTOR_CN.items():
        rows.append({
            "key": k, "name": cn,
            "target_pct": tgt.get(k, 0.0),
            "actual_pct": round(actual.get(k, 0.0) / total * 100, 2),
            "gap_pct": round(tgt.get(k, 0.0) - actual.get(k, 0.0) / total * 100, 2),
        })
    return rows


# ---------------------------------------------------------------- 修复路线（§四 / §七）

ETF_CORE_BUY_ORDER = ["510300", "510500"]
ETF_DIV_BUY_ORDER = ["510880", "515080"]


def repair_plan(total_asset: float, cash_after_trim: float,
                profile: str = C.DEFAULT_PROFILE) -> dict:
    """ETF 优先：先用减仓资金把宽基 ETF 建到 ≥4.5 万，再考虑红利，最后才是个股。"""
    targets = layer_targets(total_asset)
    core_gap = max(0.0, targets[0]["target_value"] - 0.0)   # 当前 core 近似 0
    steps = []
    cash = cash_after_trim
    need = C.ETF_FIRST_TARGET
    if cash < need:
        steps.append({"step": "还差资金", "detail": f"可用资金 {cash:,.0f} 元（原有现金 + 减仓释放）"
                                                   f" < 单只ETF底仓目标 {need:,.0f} 元；先执行 §七 第1、2 步"
                                                   f"（清华天 + 三只各减1/3），不足部分由现金仓补足"})
    buy_core = min(cash, max(need, core_gap))
    steps.append({"step": "第4步-A 宽基ETF", "detail": f"买入沪深300ETF(510300) 约 {buy_core:,.0f} 元"
                                                       f"（先 510300，不足再 510500）",
                 "amount": round(buy_core, 2), "etfs": ETF_CORE_BUY_ORDER})
    steps.append({"step": "第4步-B 红利ETF", "detail": f"核心仓建到 {need:,.0f} 元后，"
                                                       f"剩余资金买红利ETF(510880)，股息率>4% 才加",
                  "amount": round(max(0.0, cash - buy_core), 2), "etfs": ETF_DIV_BUY_ORDER})
    steps.append({"step": "第5步 才是个股", "detail": "ETF 底仓到位后，才允许用 ≤10% 单只上限开第一个个股仓"
                                                    "（且总资产需 ≥10 万）"})
    return {"targets": targets, "steps": steps,
            "profile": profile, "profile_targets": sector_targets(profile),
            "total_asset": round(total_asset, 2), "cash_after_trim": round(cash_after_trim, 2)}


def summarize(holdings: list[dict], prices: dict[str, float],
              sector_map: dict[str, str], total_asset: float,
              profile: str = C.DEFAULT_PROFILE) -> dict:
    plan = holdings_plan(holdings, prices, total_asset)
    acc = load_account()
    cash = (_num(acc.get("cash")) or 0.0) + plan["cash_release"]
    return {
        "account": acc,
        "holdings": plan,
        "layers": layer_status(holdings, prices, total_asset),
        "sectors": sector_status(holdings, prices, sector_map, total_asset, profile),
        "repair": repair_plan(total_asset, cash, profile),
        "profile": profile,
    }
