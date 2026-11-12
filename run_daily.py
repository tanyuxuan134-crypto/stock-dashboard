# -*- coding: utf-8 -*-
"""每日运行入口：读 design 冻结的 data/ 缓存 → 算特征 → 跑手册 A/B/C 规则 → 出信号与报告。

用法：
    python run_daily.py                # 用最新交易日
    python run_daily.py --date 2026-10-28
    python run_daily.py --skip-docx    # 跳过 docx（调试更快）

不联网：数据由 tools/build_data.py 预先落到 data/。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from quant import config as C          # noqa: E402
from quant import dashboard as DB      # noqa: E402
from quant import features as F        # noqa: E402
from quant import portfolio as PF      # noqa: E402
from quant import report as RP         # noqa: E402
from quant import risk as RK           # noqa: E402
from quant import rules as RL          # noqa: E402

SECTOR_CN = {"finance": "金融", "consume": "消费", "tech": "科技", "other": "其他"}
MODE_CN = {"A": "A短线", "B": "B波段", "C": "C长线"}


def _load_json(path: str):
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if v != v else v


# ---------------------------------------------------------------- 池子


def load_universe() -> dict:
    """优先 data/pool.json（T1 产出）；缺失时回退到 data/pool_source.json。"""
    pool = _load_json(C.POOL)
    if pool and pool.get("stocks"):
        return pool
    src = _load_json(os.path.join(C.DATA, "pool_source.json")) or {}
    sec = src.get("sections") or {}
    stocks, etfs, seen = [], [], set()
    for row in sec.get("pool") or []:
        code = str(row.get("code") or "").zfill(6)
        if not code or code in seen:
            continue
        seen.add(code)
        stocks.append({
            "code": code,
            "name": row.get("name") or "",
            "sector": row.get("sector") or "other",
            "industry": row.get("industry"),
        })
    for code, (role, name) in C.ETF_ROLES.items():
        etfs.append({"code": code, "name": name, "role": role})
    indices = [{"code": k, "name": v[1], "symbol": v[0], "role": "market"}
               for k, v in C.INDEX_LIST.items()]
    return {"updated": (src.get("snapshot_date") or ""), "stocks": stocks, "etfs": etfs,
            "indices": indices}


def load_snapshot_map() -> dict:
    snap = _load_json(C.SNAP) or {}
    out = {}
    for r in snap.get("rows") or []:
        out[str(r.get("code"))] = r
    return out


# ---------------------------------------------------------------- 板块（行业）上下文


def build_sector_context(pool: dict, snap_map: dict, stocks_bars: dict) -> tuple[dict, list]:
    """按「行业」等权指数算板块当日涨幅与排名（§10.1 板块排名 / §10.3 板块共振）。

    行业优先用东财 F10 的 EM2016 二级行业（data/industry.json，43 个行业），
    缺失时依次回退到新浪行业、池子大类——只有真实行业板块才能算
    §10.1.1「板块内成交额前 3 / 涨停家数 / 涨跌家数」。
    """
    ind_map = F.load_industry()
    groups: dict[str, list[dict]] = {}
    for s in pool.get("stocks") or []:
        code = str(s["code"])
        row = snap_map.get(code) or {}
        ind = ((ind_map.get(code) or {}).get("industry2")
               or row.get("industry") or s.get("industry")
               or SECTOR_CN.get(s.get("sector"), "其他"))
        s["industry_name"] = ind
        groups.setdefault(ind, []).append(s)

    stats = []
    for ind, members in groups.items():
        member_bars = [stocks_bars[m["code"]] for m in members
                       if m["code"] in stocks_bars and stocks_bars[m["code"]]]
        if not member_bars:
            continue
        idx = F.equal_weight_index(member_bars)
        if not idx:
            continue
        ctx = F.make_sector_ctx(ind, ind, idx)
        ctx["n_members"] = len(member_bars)
        # 池内该行业只有 1~2 只 → 无法算板块热度（§10.1.1 需要全行业广度）
        ctx["thin"] = len(member_bars) < 3
        stats.append({"group": ind, "members": members, "ctx": ctx, "index": idx,
                      "n_members": len(member_bars), "bars": member_bars})
    # 按当日涨幅排名（只统计成员≥3 的组，避免单票噪音）
    valid = [s for s in stats if s["n_members"] >= 3]
    valid.sort(key=lambda s: -(s["ctx"].get("chg_today") or -99))
    for i, s in enumerate(valid, 1):
        s["ctx"]["rank_today"] = i
    for s in stats:
        s["ctx"].setdefault("rank_today", None)

    # ---- §10.1.1 板块 3 天验证：3日累计涨幅、排名变化、涨停家数、上榜天数、广度
    if valid:
        all_dates = sorted({str(d) for s in valid for d in s["index"]["date"]})
        recent = all_dates[-(C.A_SECTOR["rank_lookback"] + 2):] if len(all_dates) else []
        chg_by_sector = {s["group"]: F.sector_rank_series(s["index"]) for s in valid}
        rank_map: dict[str, dict[str, int]] = {}
        for d in recent:
            vals = []
            for s in valid:
                chg = chg_by_sector[s["group"]].get(d)
                if chg is not None:
                    vals.append((chg, s["group"]))
            vals.sort(key=lambda x: -x[0])
            rank_map[d] = {g: k + 1 for k, (_, g) in enumerate(vals)}
        last_date = str(valid[0]["index"]["date"][-1])
        for s in valid:
            extras = F.rank_history_stats(chg_by_sector[s["group"]], s["group"],
                                          recent, rank_map, C.A_SECTOR["top10_days_min"])
            extras.update(F.sector_breadth(s["bars"], last_date))
            s["ctx"].update(extras)
    sector_ctx_by_stock = {}
    for s in stats:
        for m in s["members"]:
            sector_ctx_by_stock[m["code"]] = s["ctx"]
    return sector_ctx_by_stock, stats


# ---------------------------------------------------------------- 单票特征 + 规则


def eval_stock(code: str, name: str, sector: str, *, asof: str,
               val_all: dict, fin_all: dict, sector_ctx: dict, market_ctx: dict,
               bars_long: bool = False) -> dict | None:
    bars = F.load_bars(code, long=bars_long)
    if not bars or len(bars["date"]) < 60:
        return None
    idx = F.bar_index_on(bars, asof)
    if idx < 60:
        return None
    sb = F.slice_bars(bars, idx)
    d = str(sb["date"][-1])
    f = F.build_features(sb, val=F.val_upto(val_all.get(code), d),
                         fin=fin_all.get(code), sector_ctx=sector_ctx,
                         market_ctx=market_ctx)
    f["name"] = name
    f["sector"] = sector
    f["date"] = d
    return f


def _trade_plan(f: dict, res: dict, account: dict, mode: str) -> str:
    """把规则结果翻译成一句可执行的交易计划（含买入金额/止损/止盈/持有期）。"""
    total = _num(account.get("total_asset")) or 0.0
    cash = _num(account.get("cash")) or 0.0
    price = _num(f.get("close"))
    cap = min(res.get("position_cap") or 0.1, RK.stock_cap_now(total))
    budget = min(total * cap, cash if cash > 0 else total * cap)
    shares = RK.size_position(price or 0, budget)
    st = RK.stop_take_for(mode, price or 0, f)
    pieces = []
    if shares <= 0:
        pieces.append(f"预算 {budget:,.0f} 元不够 1 手（100 股 = {(price or 0) * 100:,.0f} 元），先等资金")
    else:
        pieces.append(f"买入 ≤{shares} 股（约 {shares * (price or 0):,.0f} 元，占总资产 "
                      f"{shares * (price or 0) / max(total, 1):.1%}）")
    if st.get("stop_px"):
        pieces.append(f"止损 {st['stop_px']:.2f}")
    if st.get("clear_px"):
        pieces.append(f"清仓线 {st['clear_px']:.2f}")
    if st.get("take_px"):
        tp = f"止盈 {st['take_px']:.2f}"
        if st.get("take_px_high"):
            tp += f"~{st['take_px_high']:.2f}（减1/3）"
        pieces.append(tp)
    if res.get("max_hold_days"):
        pieces.append(f"最长持有 {res['max_hold_days']} 交易日")
    return "；".join(pieces)


def _why(res: dict, keys: list[str], n: int = 4) -> str:
    out = []
    for c in res.get("checks", []):
        if c["ok"] is True and any(c["id"].startswith(k) for k in keys):
            out.append(f"{c['id']} {c['text']}")
    return "；".join(out[:n]) if out else "—"


def run(asof: str | None = None, skip_docx: bool = False) -> dict:
    pool = load_universe()
    snap_map = load_snapshot_map()

    # 大盘：沪深300
    bench_code, bench_name = C.INDEX_LIST[C.MARKET_INDEX]
    bench_bars = F.load_bars(bench_code)
    if not bench_bars:
        bench_bars = F.load_bars(bench_code, long=True)
    if not bench_bars:
        raise SystemExit("缺少沪深300日线 data/bars/sh000300.json —— 先运行 python tools/build_data.py")
    last_date = str(bench_bars["date"][-1])
    asof = asof or last_date
    bidx = F.bar_index_on(bench_bars, asof)
    market_ctx = F.make_market_ctx(F.slice_bars(bench_bars, max(bidx, 0)))

    # 池内个股日线
    stocks = pool.get("stocks") or []
    stocks_bars = {}
    for s in stocks:
        b = F.load_bars(s["code"])
        if b:
            stocks_bars[s["code"]] = b

    sector_ctx_by_stock, sector_stats = build_sector_context(pool, snap_map, stocks_bars)
    val_all = {s["code"]: F.load_val(s["code"]) for s in stocks}
    fin_all = {s["code"]: F.load_fin(s["code"]) for s in stocks}

    feats, all_rows = [], []
    sig_A, sig_B, sig_C, snap_B = [], [], [], []
    # 第一遍：算特征（先不判定，因为 §10.1.1 的「板块内成交额排名」要等全池算完才能排）
    for s in stocks:
        code, name, sector = s["code"], s.get("name") or "", s.get("sector") or "other"
        sctx = sector_ctx_by_stock.get(code)
        f = eval_stock(code, name, sector, asof=asof, val_all=val_all, fin_all=fin_all,
                       sector_ctx=sctx, market_ctx=market_ctx)
        if not f:
            continue
        f["_sector_cn"] = SECTOR_CN.get(sector, sector)
        feats.append(f)

    # §10.1.1：通过板块验证后，只买「板块内成交额排名前3」；4~8 是跟风股，不买
    by_ind: dict[str, list[dict]] = {}
    for f in feats:
        by_ind.setdefault(f.get("sector_name") or "—", []).append(f)
    for arr in by_ind.values():
        arr.sort(key=lambda x: -(x.get("amount") or 0))
        for i, f in enumerate(arr, 1):
            f["sector_amount_rank"] = i
            f["sector_amount_n"] = len(arr)

    # 第二遍：三套模式判定 + 生成交易计划
    for f in feats:
        code = f.get("code") or ""
        name = f.get("name") or ""
        sector = f.get("sector") or "other"
        rA = RL.eval_mode_A(f)
        rB = RL.eval_mode_B(f)
        rC = RL.eval_mode_C(f)
        rBS = RL.eval_mode_B_snapshot(f)
        row = {
            "code": code, "name": name, "sector": f.get("_sector_cn") or sector,
            "industry": f.get("sector_name"),
            "close": f.get("close"), "chg_pct": f.get("chg_pct"),
            "pe_ttm": f.get("pe_ttm"), "pe_pct_5y": f.get("pe_pct_5y"),
            "from_hi_1y": f.get("from_hi_1y"), "ma20_slope": f.get("ma20_slope"),
            "vol_ratio": f.get("vol_ratio"),
            "A_pass": "✓" if rA["pass"] else "", "A_score": rA["score"],
            "A_ok": rA["n_ok"], "A_req": rA["n_req"],
            "A_fail": "" if rA["pass"] else RL.failed_text(rA),
            "B_pass": "✓" if rB["pass"] else "", "B_score": rB["score"],
            "B_ok": rB["n_ok"], "B_req": rB["n_req"],
            "B_fail": "" if rB["pass"] else RL.failed_text(rB),
            "C_pass": "✓" if rC["pass"] else "", "C_score": rC["score"],
            "C_ok": rC["n_ok"], "C_req": rC["n_req"],
            "C_fail": "" if rC["pass"] else RL.failed_text(rC),
            "snapshot_B": rBS["pass"],
        }
        all_rows.append(row)

        vol = _num(f.get("vol_ratio"))
        amt = _num(f.get("amount"))
        if rA["pass"]:
            sig_A.append({"code": code, "name": name, "sector": f.get("sector_name"),
                          "close": f.get("close"), "chg_pct": f.get("chg_pct"),
                          "score_text": f"{rA['n_ok']}/{rA['n_req']}",
                          "why": _why(rA, ["A1", "A2", "S", "A12", "A3", "A4", "A5"]),
                          "trade_plan": _trade_plan(f, rA, PF.load_account(), "A"),
                          "stop_text": "−3% 硬止损",
                          "take_text": "+6~8% 全部卖出",
                          "hold_text": "1~5 交易日"})
        if rB["pass"]:
            sig_B.append({"code": code, "name": name, "sector": f.get("sector_name"),
                          "close": f.get("close"), "chg_pct": f.get("chg_pct"),
                          "score_text": f"{rB['n_ok']}/{rB['n_req']}",
                          "why": _why(rB, ["B1", "B2", "B3", "B4"]),
                          "trade_plan": _trade_plan(f, rB, PF.load_account(), "B"),
                          "stop_text": (f"跌破MA20({_num(f.get('ma20')) or 0:.2f})减半 / "
                                        f"破MA60({_num(f.get('ma60')) or 0:.2f})清仓"),
                          "take_text": "+20~30% 减 1/3",
                          "hold_text": "2周~3月（≤60交易日）"})
        if rC["pass"]:
            sig_C.append({"code": code, "name": name, "sector": f.get("sector_name"),
                          "close": f.get("close"), "chg_pct": f.get("chg_pct"),
                          "score_text": f"{rC['n_ok']}/{rC['n_req']}",
                          "why": _why(rC, ["C1", "C2", "C3", "C4"]),
                          "trade_plan": _trade_plan(f, rC, PF.load_account(), "C") +
                                        "；分 5 批、每批 20%（本次只买第 1 批）",
                          "stop_text": "逻辑止损（季报恶化/逻辑破坏/造假）",
                          "take_text": "PE分位>80%减半 / >90%清仓",
                          "hold_text": "6月~2年"})
        if rBS["pass"]:
            snap_B.append({"code": code, "name": name})

    def _score_key(x):
        try:
            return -float(str(x.get("score_text", "0/0")).split("/")[0].replace("✓", ""))
        except ValueError:
            return 0.0
    for arr in (sig_A, sig_B, sig_C):
        arr.sort(key=_score_key)

    # 放量诊断 top20
    diag_src = sorted([f for f in feats if _num(f.get("vol_ratio")) is not None],
                      key=lambda x: -(x["vol_ratio"] or 0))[:20]
    volume_diag = []
    for f in diag_src:
        vc = RL.volume_check(f)
        volume_diag.append({"code": f["code"], "name": f.get("name"), "chg_pct": f.get("chg_pct"),
                            "vol_ratio": f.get("vol_ratio"), "amt_ratio": f.get("amt_ratio"),
                            "turnover": f.get("turnover"), "level": vc["level"],
                            "true_volume": vc["true_volume"]})

    # ---------------------------------------------------------- 组合层
    account = PF.load_account()
    holdings = PF.load_holdings()
    # 持仓可能不在精选池里（如 600183 生益科技），必须单独按代码加载日线取最新收盘价
    prices = {}
    for h in holdings:
        b = stocks_bars.get(h["code"]) or F.load_bars(h["code"])
        prices[h["code"]] = _num((b or {}).get("close", [np.nan])[-1]) if b else None
    total_asset = _num(account.get("total_asset")) or (
        sum((_num(prices.get(h["code"])) or 0) * h["shares"] for h in holdings) +
        (_num(account.get("cash")) or 0))
    sector_map = {h["code"]: "tech" for h in holdings}  # 五只均为半导体链（§一）
    port = PF.summarize(holdings, prices, sector_map, total_asset)
    positions = [{"code": r["code"], "name": r["name"], "sector": sector_map.get(r["code"], "other"),
                  "value": r["value"], "price": r["price"], "stop_px": r["stop_px"], "mode": "B"}
                 for r in port["holdings"]["rows"]]
    risk_rep = RK.check_portfolio(positions, total_asset, sector_map, cash=account.get("cash"))
    tier = RK.tier_of(total_asset)
    ok_new, etf_text = RK.can_open_new_stock(total_asset)
    tier_text = (f"总资产 {total_asset:,.0f} 元 → {tier['verdict']}；"
                 f"单只个股上限 {min(tier['single_cap'], RK.stock_cap_now(total_asset)):.0%}")
    etf_first_text = etf_text

    # ---------------------------------------------------------- headline
    top_sec = [s for s in sector_stats if s["ctx"].get("rank_today")][:3]
    top_sec_text = "、".join(f"{s['group']}({_num(s['ctx'].get('chg_today')) or 0:+.2f}%)"
                             for s in top_sec) or "—"
    headline = [
        f"三模式信号：A短线 {len(sig_A)} 只 ／ B波段 {len(sig_B)} 只 ／ C长线 {len(sig_C)} 只；"
        f"另按 §10.6 快照口径有 {len(snap_B)} 只满足「站上MA20+MA60且双双上翘」。",
        f"板块最强：{top_sec_text}。手册 §10.1 要求在脉冲板块里做短线，不在弱势板块抢反弹。",
        f"账户结构：总资产 {total_asset:,.0f} 元，现金占比 {risk_rep['cash_pct']:.1f}%（手册下限 15%），"
        f"风控违规 {risk_rep['n_violations']} 条。",
        f"下一步动作：{'先执行 §七 的持仓处置（释放约 ' + format(port['holdings']['cash_release'], ',.0f') + ' 元）' if port['holdings']['cash_release'] else '按 §四 顺序执行'}，"
        f"再把资金按「宽基ETF → 红利ETF → 个股」建底仓。",
    ]

    meta_path = os.path.join(C.DATA, "meta.json")
    meta = _load_json(meta_path) or {}
    ctx = {
        "date": asof,
        "n_stocks": len(feats),
        "account": account,
        "tier_text": tier_text,
        "etf_first_text": etf_first_text,
        "headline": headline,
        "layers": port["layers"],
        "sector_alloc": port["sectors"],
        "holdings": port["holdings"],
        "repair": port["repair"],
        "execution_order": list(C.EXECUTION_ORDER),
        "risk": risk_rep,
        "signals_A": sig_A, "signals_B": sig_B, "signals_C": sig_C,
        "volume_diag": volume_diag,
        "all_stocks": all_rows,
        "sector_stats": [{"group": s["group"], "n": s["n_members"],
                          "chg": s["ctx"].get("chg_today"), "ret5": s["ctx"].get("ret5"),
                          "above_ma20": s["ctx"].get("above_ma20"),
                          "rank": s["ctx"].get("rank_today"),
                          # §10.1.1 板块 3 天验证
                          "cum3": s["ctx"].get("cum3"),
                          "rank_3ago": s["ctx"].get("rank_3ago"),
                          "limit_up": s["ctx"].get("limit_up"),
                          "top10_days": s["ctx"].get("top10_days"),
                          "breadth": s["ctx"].get("breadth_ratio"),
                          "up": s["ctx"].get("up"), "down": s["ctx"].get("down")}
                         for s in sector_stats],
        "market": market_ctx,
        "snapshot_B": snap_B,
        "profile": port["profile"],
        "meta": {"start": str(bench_bars["date"][0]), "end": last_date,
                 "warnings": meta.get("warnings") or []},
    }
    paths = {"md": None, "xlsx": None, "docx": None}
    if skip_docx:
        RP._ensure_dir(C.OUT)
        md_path = os.path.join(C.OUT, f"信号_{asof}.md")
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write(RP.build_md(ctx))
        xlsx_path = os.path.join(C.OUT, f"信号_{asof}.xlsx")
        RP.build_xlsx(ctx, xlsx_path)
        paths = {"md": md_path, "xlsx": xlsx_path, "docx": None}
    else:
        paths = RP.write_all(ctx)

    # 附加：JSON 机读输出（给后续程序/审计用）
    json_path = os.path.join(C.OUT, f"信号_{asof}.json")
    slim = {k: ctx[k] for k in ("date", "n_stocks", "headline", "tier_text", "etf_first_text",
                                "layers", "sector_alloc", "holdings", "repair", "execution_order",
                                "risk", "signals_A", "signals_B", "signals_C", "volume_diag",
                                "sector_stats", "market", "profile", "meta")}
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(slim, fh, ensure_ascii=False, indent=1, default=str)
    paths["json"] = json_path

    # 手机看板（单文件 HTML）：out/看板_{date}.html + out/index.html（固定网址）
    paths.update(DB.write_all(ctx, C.OUT, asof))
    return {"ctx": ctx, "paths": paths}


def main():
    import warnings as _w
    _w.filterwarnings("ignore", category=RuntimeWarning)
    np.seterr(all="ignore")
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--skip-docx", action="store_true")
    args = ap.parse_args()
    t0 = datetime.now()
    out = run(asof=args.date, skip_docx=args.skip_docx)
    ctx = out["ctx"]
    print(f"[run_daily] {ctx['date']}  股票池 {ctx['n_stocks']} 只")
    print(f"  信号 A/B/C = {len(ctx['signals_A'])}/{len(ctx['signals_B'])}/{len(ctx['signals_C'])}"
          f"  §10.6快照B = {len(ctx['snapshot_B'])}")
    print(f"  风控违规 {ctx['risk']['n_violations']} 条；持仓处置可释放 "
          f"{ctx['holdings']['cash_release']:,.0f} 元")
    for k, v in out["paths"].items():
        if v:
            print(f"  {k}: {v}")
    print(f"[run_daily] 耗时 {(datetime.now() - t0).total_seconds():.1f}s")


if __name__ == "__main__":
    main()
