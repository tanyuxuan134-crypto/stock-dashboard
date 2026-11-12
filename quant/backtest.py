# -*- coding: utf-8 -*-
"""事件驱动回测引擎（单标的，逐日撮合）——DESIGN §5。

    run(bars, signals, cfg=None) -> {"trades", "equity", "metrics", "warnings", ...}
    benchmark_metrics(bars_bench, start, end) -> dict

撮合铁律（实现即文档，详见 docs/BACKTEST_ENGINE.md）
----------------------------------------------------
1. 信号日 T **收盘后**产生，**T+1 开盘**成交。
2. T+1 开盘价相对前收涨幅 >= 9.8% 视为一字/接近涨停，买不到 -> 放弃该信号（warning）。
3. 持仓期内每日先判止损：``low <= stop_px`` -> 成交价 = ``min(open, stop_px)``
   （跳空低开则按更低的 open）；再判止盈：``high >= target_px`` -> 成交价 = ``max(open, target_px)``
   （跳空高开则按更高的 open）。**同日两者都触发时按止损优先**（保守）。
4. A 股 T+1：买入当日不可卖出（最早 T+1 退出）。
5. ``hold_days`` 到期当日按**收盘价**卖出。
6. 费用：买入/卖出各 ``fee_bps``；滑点 ``slip_bps`` 单边：
   买入成交价 = 原始价 × (1+slip/1e4)，卖出成交价 = 原始价 × (1-slip/1e4)。
7. 份额按 ``lot`` 向下取整（A 股 100 股一手）；买不起一手 -> 拒绝 + warning。
8. 单标的引擎：同标的同时只允许 1 笔持仓；``max_positions`` / ``cooldown_days`` 仍然
   按键校验（为多标的包装预留，组合层约束由 Lead 负责）。
9. ``stop`` / ``target`` 是相对**实际成交价 entry_px（含滑点）**的百分比；
   例如 stop=-3.0 -> ``stop_px = entry_px * (1 - 0.03)``。

返回
----
trades: 每笔
    ``entry_date, exit_date, entry_px, exit_px, ret_pct(已扣费), hold_days,
      exit_reason("stop"|"target"|"timeout"|"close"), rule, peak_pct, mae_pct``
    以及附加字段：``signal_date, size, shares, pnl, stop, target, entry_i, exit_i, raw_exit_px``。
equity: ``[{"date","equity","cash","n_pos"}, ...]``，按 bars 全区间逐日收盘盯市。
warnings: **字符串列表**（便于报告里直接 join / print）。
warnings_detail: 结构化版本 ``[{"date","kind","msg","signal_date","rule"}, ...]``。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

try:  # 正常以 `quant.backtest` 导入
    from . import metrics as _metrics
except ImportError:  # 直接 `python quant/backtest.py` 或脚本路径运行时
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from quant import metrics as _metrics  # type: ignore

# 默认配置（DESIGN §5；cfg 里缺的键用这里的值）
DEFAULT_CFG = {
    "fee_bps": 2.5,
    "slip_bps": 5.0,
    "init_cash": 100000.0,
    "max_positions": 5,
    "cooldown_days": 3,
    "lot": 100,
}

# T+1 开盘涨幅达到该百分比视为一字/接近涨停买不到
LIMIT_UP_OPEN_PCT = 9.8

ALLOWED_EXIT_REASONS = ("stop", "target", "timeout", "close")

_BARS_NUMERIC_FIELDS = ("open", "close", "high", "low", "volume", "turnover", "amount")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _num(x):
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _finite(x):
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def _iso(value):
    """日期 -> 'YYYY-MM-DD' 字符串（兼容 str / date / datetime / np.datetime64）。"""
    return str(value)[:10]


# ---------------------------------------------------------------------------
# 数据规范化
# ---------------------------------------------------------------------------
def _normalize_bars(bars):
    """校验 + 归一化 DESIGN §2.2 的 bars dict，返回 (dates, arrays)。"""
    if not isinstance(bars, dict):
        raise ValueError("bars 必须是 dict（DESIGN §2.2）")
    for key in ("date", "open", "close", "high", "low"):
        if key not in bars or bars.get(key) is None:
            raise ValueError(f"bars 缺少必需字段: {key}")
    raw_dates = list(bars["date"])
    n = len(raw_dates)
    if n == 0:
        raise ValueError("bars 为空（date 长度为 0）")
    dates = [_iso(d) for d in raw_dates]
    for i in range(n - 1):
        if dates[i] >= dates[i + 1]:
            raise ValueError(
                f"bars.date 必须严格升序且无重复：第 {i} 项 {dates[i]} >= 第 {i+1} 项 {dates[i+1]}"
            )

    arrays = {}
    for key in _BARS_NUMERIC_FIELDS:
        values = bars.get(key)
        if values is None:
            arrays[key] = None
            continue
        if len(values) != n:
            raise ValueError(
                f"bars.{key} 长度 {len(values)} 与 bars.date 长度 {n} 不一致"
            )
        out = np.empty(n, dtype=float)
        for i, v in enumerate(values):
            try:
                out[i] = float(v) if v is not None else float("nan")
            except (TypeError, ValueError):
                raise ValueError(f"bars.{key}[{i}] 不是数值: {v!r}")
        arrays[key] = out
    for key in ("open", "close", "high", "low"):
        if arrays[key] is None:
            raise ValueError(f"bars 缺少必需字段: {key}")
    return dates, arrays


def _normalize_cfg(cfg):
    """合并默认 cfg 并校验。"""
    out = dict(DEFAULT_CFG)
    if cfg:
        if not isinstance(cfg, dict):
            raise ValueError("cfg 必须是 dict")
        for key in DEFAULT_CFG:
            if key in cfg and cfg[key] is not None:
                out[key] = cfg[key]
    try:
        out["fee_bps"] = float(out["fee_bps"])
        out["slip_bps"] = float(out["slip_bps"])
        out["init_cash"] = float(out["init_cash"])
        out["max_positions"] = int(out["max_positions"])
        out["cooldown_days"] = int(out["cooldown_days"])
        out["lot"] = int(out["lot"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"cfg 字段类型非法: {exc}") from exc
    if out["fee_bps"] < 0 or out["slip_bps"] < 0:
        raise ValueError("cfg.fee_bps / cfg.slip_bps 必须 >= 0")
    if out["init_cash"] <= 0:
        raise ValueError("cfg.init_cash 必须 > 0")
    if out["max_positions"] < 1:
        raise ValueError("cfg.max_positions 必须 >= 1")
    if out["cooldown_days"] < 0:
        raise ValueError("cfg.cooldown_days 必须 >= 0")
    if out["lot"] < 1:
        raise ValueError("cfg.lot 必须 >= 1")
    return out


def _close_series(bars):
    """只取 date + close（基准函数用，要求比 _normalize_bars 宽松）。"""
    if not isinstance(bars, dict):
        raise ValueError("bars 必须是 dict（DESIGN §2.2）")
    if "date" not in bars or "close" not in bars:
        raise ValueError("bars 缺少必需字段: date / close")
    dates = [_iso(d) for d in bars["date"]]
    closes = []
    for v in bars["close"]:
        try:
            closes.append(float(v) if v is not None else float("nan"))
        except (TypeError, ValueError):
            closes.append(float("nan"))
    if len(dates) != len(closes):
        raise ValueError("bars.date 与 bars.close 长度不一致")
    return dates, np.asarray(closes, dtype=float)


# ---------------------------------------------------------------------------
# 持仓 / 信号
# ---------------------------------------------------------------------------
@dataclass
class _Position:
    entry_i: int
    entry_date: str
    signal_date: str
    entry_px: float          # 实际成交价（含滑点）
    shares: int
    cost_total: float        # 买入现金流出 = 股数 * entry_px + 买入费用
    stop_pct: float | None
    target_pct: float | None
    stop_px: float
    target_px: float
    hold_days: int           # <=0 表示不按到期处理
    rule: str
    size: float
    peak_pct: float = 0.0    # 最大浮盈（%），用日线 high 近似
    mae_pct: float = 0.0     # 最大浮亏（%），用日线 low 近似
    last_px: float = 0.0     # 最近可用收盘价（数据缺失时盯市兜底）
    extra: dict = field(default_factory=dict)


class _Warnings:
    def __init__(self):
        self.items: list[str] = []
        self.detail: list[dict] = []

    def add(self, date, kind, msg, signal=None):
        sig_date = signal.get("_date") if isinstance(signal, dict) else None
        rule = signal.get("rule") if isinstance(signal, dict) else None
        text = f"{date} [{kind}] {msg}"
        if sig_date:
            text += f"（信号日 {sig_date}"
            if rule is not None:
                text += f", rule={rule}"
            text += "）"
        self.items.append(text)
        self.detail.append({"date": date, "kind": kind, "msg": msg,
                            "signal_date": sig_date, "rule": rule})


def _prepare_signals(signals, dates, date_pos, n, cfg, warnings):
    """校验信号、补默认值、按日期稳定排序，返回规范化后的列表。"""
    if signals is None:
        return []
    if not isinstance(signals, (list, tuple)):
        raise ValueError("signals 必须是 list[dict]")
    norm = []
    for order, raw in enumerate(signals):
        if not isinstance(raw, dict):
            warnings.add("-", "signal_invalid", f"第 {order} 个信号不是 dict，已跳过")
            continue
        raw_date = raw.get("date")
        if raw_date is None:
            warnings.add("-", "signal_invalid", f"第 {order} 个信号缺少 date，已跳过")
            continue
        date_key = _iso(raw_date)
        signal_i = date_pos.get(date_key)
        if signal_i is None:
            warnings.add(date_key, "signal_date_missing",
                         f"信号日期不在 bars 中，已跳过（rule={raw.get('rule')}）")
            continue

        side = str(raw.get("side") or "buy").strip().lower()
        if side in ("long",):
            side = "buy"
        if side in ("short",):
            warnings.add(date_key, "signal_invalid", "不支持做空，已跳过该信号")
            continue
        if side not in ("buy", "sell", "exit"):
            warnings.add(date_key, "signal_invalid",
                         f"未知 side={raw.get('side')!r}，已跳过（rule={raw.get('rule')}）")
            continue

        rule = raw.get("rule")
        rule = str(rule) if rule is not None else "?"

        # 卖出/退出信号：只需 date + side，不参与 stop/target/hold_days/size 处理，
        # 避免给 Lead 的 exit 信号产生无意义的 clamp/default warning。
        if side in ("sell", "exit"):
            norm.append({
                "date": date_key,
                "_date": date_key,
                "_signal_i": int(signal_i),
                "_order": order,
                "side": side,
                "rule": rule,
                "stop": None,
                "target": None,
                "hold_days": 0,
                "size": 0.0,
                "raw": raw,
            })
            continue

        stop_pct = _num(raw.get("stop"))
        target_pct = _num(raw.get("target"))

        hold_days = raw.get("hold_days")
        hold = None
        if hold_days is not None:
            try:
                hold = int(round(float(hold_days)))
            except (TypeError, ValueError):
                hold = None
        if hold is None:
            hold = 0          # 0 = 不按持有天数退出（直到期末强制平仓）
        if hold < 1:
            hold = 1          # T+1：最少持有一天
            warnings.add(date_key, "signal_clamped",
                         f"hold_days 被 clamp 到 1（T+1 约束），rule={rule}")

        size = raw.get("size")
        size = _num(size)
        if size is None:
            size = 1.0 / max(cfg["max_positions"], 1)
            warnings.add(date_key, "signal_default",
                         f"缺少 size，默认 {size:.4f}（=1/max_positions），rule={rule}")
        if size <= 0:
            warnings.add(date_key, "signal_invalid", f"size<=0，已跳过，rule={rule}")
            continue
        if size > 1.0:
            if size <= 100.0:
                warnings.add(date_key, "signal_clamped",
                             f"size={size} 疑似百分数，按 {size/100.0:.4f} 解释，rule={rule}")
                size = size / 100.0
            else:
                warnings.add(date_key, "signal_clamped", f"size={size} 过大，按 1.0 处理，rule={rule}")
                size = 1.0

        norm.append({
            "date": date_key,
            "_date": date_key,
            "_signal_i": int(signal_i),
            "_order": order,
            "side": side,
            "rule": rule,
            "stop": stop_pct,
            "target": target_pct,
            "hold_days": hold,
            "size": float(size),
            "raw": raw,
        })
    # 稳定排序：按信号日；同一天按输入顺序
    norm.sort(key=lambda s: (s["_signal_i"], s["_order"]))
    return norm


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def run(bars, signals, cfg=None):
    """单标的逐日事件驱动回测。见模块 docstring / DESIGN §5。"""
    cfg = _normalize_cfg(cfg)
    dates, a = _normalize_bars(bars)
    n = len(dates)
    date_pos = {}
    for i, d in enumerate(dates):
        date_pos.setdefault(d, i)
    open_, close, high, low = a["open"], a["close"], a["high"], a["low"]
    volume = a["volume"]

    fee_rate = cfg["fee_bps"] / 1e4
    slip_rate = cfg["slip_bps"] / 1e4
    lot = cfg["lot"]
    init_cash = cfg["init_cash"]
    cooldown_days = cfg["cooldown_days"]
    max_positions = cfg["max_positions"]

    warnings = _Warnings()
    sig_list = _prepare_signals(signals, dates, date_pos, n, cfg, warnings)

    # 信号排期：T 日信号 -> T+1 处理
    entry_sched: dict[int, list[dict]] = {}
    exit_sched: dict[int, list[dict]] = {}
    for sig in sig_list:
        t = sig["_signal_i"]
        if t + 1 >= n:
            warnings.add(sig["_date"], "signal_no_next_bar",
                         f"信号在最后一根K线，无 T+1 可成交，已跳过，rule={sig['rule']}", sig)
            continue
        target_sched = entry_sched if sig["side"] == "buy" else exit_sched
        target_sched.setdefault(t + 1, []).append(sig)

    cash = init_cash
    positions: list[_Position] = []
    trades: list[dict] = []
    equity: list[dict] = []
    last_exit_i = None

    def warn(i, kind, msg, sig=None):
        warnings.add(dates[i], kind, msg, sig)

    def mark_equity(i):
        mv = 0.0
        for pos in positions:
            px = float(close[i])
            if _finite(px) and px > 0:
                pos.last_px = px
            else:
                px = pos.last_px
            mv += pos.shares * px
        equity.append({
            "date": dates[i],
            "equity": cash + mv,
            "cash": cash,
            "n_pos": len(positions),
        })

    def close_position(pos, i, raw_px, reason):
        nonlocal cash, last_exit_i
        if not _finite(raw_px) or float(raw_px) <= 0:
            warn(i, "exit_invalid_price",
                 f"退出价无效（{raw_px!r}），持仓保留到下一日，rule={pos.rule}")
            return False
        fill_px = float(raw_px) * (1.0 - slip_rate)
        proceeds = pos.shares * fill_px
        sell_fee = proceeds * fee_rate
        cash += proceeds - sell_fee
        pnl = proceeds - sell_fee - pos.cost_total
        ret_pct = (pnl / pos.cost_total * 100.0) if pos.cost_total > 0 else 0.0
        trades.append({
            "entry_date": pos.entry_date,
            "exit_date": dates[i],
            "entry_px": pos.entry_px,
            "exit_px": fill_px,
            "ret_pct": ret_pct,
            "hold_days": i - pos.entry_i,
            "exit_reason": reason,
            "rule": pos.rule,
            "peak_pct": pos.peak_pct,
            "mae_pct": pos.mae_pct,
            "signal_date": pos.signal_date,
            "size": pos.size,
            "shares": pos.shares,
            "pnl": pnl,
            "stop": pos.stop_pct,
            "target": pos.target_pct,
            "entry_i": pos.entry_i,
            "exit_i": i,
            "raw_exit_px": float(raw_px),
        })
        positions.remove(pos)
        last_exit_i = i
        return True

    def try_enter(i, sig):
        nonlocal cash
        # --- 1) 组合约束 ---
        if positions:
            warn(i, "reject_in_position",
                 f"已有持仓（entry_date={positions[0].entry_date}），拒绝新开仓", sig)
            return
        if len(positions) >= max_positions:
            warn(i, "reject_max_positions",
                 f"持仓数已达 max_positions={max_positions}，拒绝新开仓", sig)
            return
        if (last_exit_i is not None and cooldown_days > 0
                and 0 < i - last_exit_i <= cooldown_days):
            warn(i, "reject_cooldown",
                 f"距上次退出 {i - last_exit_i} 个交易日，仍在 cooldown_days={cooldown_days} 内，拒绝", sig)
            return

        # --- 2) 可交易性 ---
        o = float(open_[i])
        if not _finite(o) or o <= 0:
            warn(i, "reject_bad_open", f"开盘价无效（{o!r}），放弃", sig)
            return
        if volume is not None:
            v = float(volume[i])
            # 只有「明确为 0 或负」才判停牌；None/nan 视为数据缺失，不做停牌判定
            if math.isfinite(v) and v <= 0:
                warn(i, "reject_suspended", f"T+1 成交量为 0（停牌/无成交），放弃", sig)
                return
        prev_close = float(close[i - 1]) if i > 0 else float("nan")
        if _finite(prev_close) and prev_close > 0:
            gap = o / prev_close - 1.0
            if gap >= LIMIT_UP_OPEN_PCT / 100.0 - 1e-12:
                warn(i, "reject_limit_up_open",
                     f"T+1 开盘涨幅 {gap*100:.2f}% >= {LIMIT_UP_OPEN_PCT}%（一字/接近涨停），买不到，放弃", sig)
                return

        # --- 3) 仓位与份额 ---
        equity_open = cash
        for pos in positions:
            px = float(open_[i])
            if not (_finite(px) and px > 0):
                px = pos.last_px
            equity_open += pos.shares * px
        budget = sig["size"] * equity_open
        fill_px = o * (1.0 + slip_rate)
        shares = int(math.floor(budget / fill_px / lot)) * lot
        if shares <= 0:
            warn(i, "reject_lot",
                 f"预算 {budget:.2f} 买不起一手（lot={lot}，价 {fill_px:.4f}），放弃", sig)
            return
        # 现金（含手续费）够不够：不够就一手一手往下减
        while shares > 0:
            cost = shares * fill_px
            if cost * (1.0 + fee_rate) <= cash + 1e-9:
                break
            shares -= lot
        if shares <= 0:
            warn(i, "reject_cash",
                 f"现金 {cash:.2f} 不足以买入一手（价 {fill_px:.4f}），放弃", sig)
            return

        cost = shares * fill_px
        buy_fee = cost * fee_rate
        cash -= (cost + buy_fee)
        pos = _Position(
            entry_i=i,
            entry_date=dates[i],
            signal_date=sig["_date"],
            entry_px=fill_px,
            shares=shares,
            cost_total=cost + buy_fee,
            stop_pct=sig["stop"],
            target_pct=sig["target"],
            stop_px=-math.inf,
            target_px=math.inf,
            hold_days=sig["hold_days"],
            rule=sig["rule"],
            size=sig["size"],
            last_px=float(close[i]) if _finite(close[i]) and close[i] > 0 else fill_px,
        )
        if pos.stop_pct is not None:
            stop_px = pos.entry_px * (1.0 + pos.stop_pct / 100.0)
            pos.stop_px = stop_px if stop_px > 0 else -math.inf
        if pos.target_pct is not None:
            pos.target_px = pos.entry_px * (1.0 + pos.target_pct / 100.0)
        positions.append(pos)

    for i in range(n):
        # ---- 1) 卖出信号：T+1 开盘平仓（A 股卖出资金当日可用，故可同日再买）----
        for sig in exit_sched.get(i, []):
            if not positions:
                warn(i, "reject_no_position", "卖出/退出信号但没有持仓，忽略", sig)
                continue
            pos = positions[0]
            if pos.entry_i == i:
                warn(i, "reject_t_plus_1", "T+1 限制：买入当日不可卖出，忽略该退出信号", sig)
                continue
            close_position(pos, i, float(open_[i]), "close")

        # ---- 2) 买入信号：T+1 开盘成交（先于盘中止损/止盈，开盘时点不预知盘中走势）----
        for sig in entry_sched.get(i, []):
            try_enter(i, sig)

        # ---- 3) 更新浮盈浮亏 + 盘中退出（止损 -> 止盈 -> 到期收盘卖）----
        for pos in list(positions):
            hi = float(high[i])
            lo = float(low[i])
            if _finite(hi) and hi > 0 and pos.entry_px > 0:
                pos.peak_pct = max(pos.peak_pct, (hi / pos.entry_px - 1.0) * 100.0)
            if _finite(lo) and lo > 0 and pos.entry_px > 0:
                pos.mae_pct = min(pos.mae_pct, (lo / pos.entry_px - 1.0) * 100.0)

            if pos.entry_i == i:
                continue  # A 股 T+1：买入当日不可卖

            exited = False
            # 3a) 止损优先（同日 stop/target 双触发 -> stop）
            if pos.stop_px > 0 and math.isfinite(pos.stop_px) and _finite(lo) and lo <= pos.stop_px:
                raw = float(open_[i])
                if not (_finite(raw) and raw > 0):
                    raw = pos.stop_px
                raw = min(raw, pos.stop_px)   # 跳空低开 -> 按更低的 open 成交
                close_position(pos, i, raw, "stop")
                exited = True
            # 3b) 止盈（跳空高开 -> 按更高的 open 成交）
            elif math.isfinite(pos.target_px) and _finite(hi) and hi >= pos.target_px:
                raw = float(open_[i])
                if not (_finite(raw) and raw > 0):
                    raw = pos.target_px
                raw = max(raw, pos.target_px)
                close_position(pos, i, raw, "target")
                exited = True
            # 3c) 到期按收盘卖
            elif pos.hold_days > 0 and (i - pos.entry_i) >= pos.hold_days:
                close_position(pos, i, float(close[i]), "timeout")
                exited = True
            _ = exited

        # ---- 4) 盯市 ----
        mark_equity(i)

    # ---- 5) 样本末端：未平仓的按最后收盘价强制平仓（T+1 约束在末端放宽）----
    if positions:
        last_i = n - 1
        last_raw = float(close[last_i])
        if not (_finite(last_raw) and last_raw > 0):
            last_raw = positions[0].last_px
        for pos in list(positions):
            warn(last_i, "force_close_at_end",
                 f"样本末端强制平仓（T+1 约束在末端放宽），rule={pos.rule}")
            close_position(pos, last_i, last_raw, "close")
        # 注意：循环已为最后一天写入盯市行，这里就地覆盖（不能 append，否则 equity 会多出一行）
        equity[-1] = {"date": dates[last_i], "equity": cash, "cash": cash, "n_pos": 0}
    # 数值清理
    for row in equity:
        row["equity"] = float(row["equity"])
        row["cash"] = float(row["cash"])

    result_metrics = _metrics.metrics_from_equity(equity, trades, init_cash)

    return {
        "trades": trades,
        "equity": equity,
        "metrics": result_metrics,
        "warnings": warnings.items,
        "warnings_detail": warnings.detail,
        "meta": {
            "cfg": dict(cfg),
            "n_bars": n,
            "start": dates[0],
            "end": dates[-1],
            "n_signals": len(sig_list),
            "n_trades": len(trades),
            "limit_up_open_pct": LIMIT_UP_OPEN_PCT,
            "symbol": bars.get("key") or bars.get("name"),
        },
    }


# ---------------------------------------------------------------------------
# 基准：买入持有
# ---------------------------------------------------------------------------
def benchmark_metrics(bars_bench, start, end):
    """买入持有基准的绩效（用于与策略对比）。

    规则：在 ``[start, end]`` 区间取首尾收盘价做**纯价格**买入持有（不含费用，
    因为 DESIGN §5 冻结签名不带 cfg；Lead 若需要含费用基准请自行调整）。
    指标：total_return_pct / cagr_pct / annual_vol_pct / max_dd_pct /
          sharpe / sortino / calmar / n_days / start / end。
    """
    dates, closes = _close_series(bars_bench)
    start = _iso(start) if start is not None else None
    end = _iso(end) if end is not None else None

    idx = [i for i, d in enumerate(dates)
           if (start is None or d >= start) and (end is None or d <= end)]
    if not idx:
        return {
            "total_return_pct": None, "cagr_pct": None, "annual_vol_pct": None,
            "max_dd_pct": None, "sharpe": None, "sortino": None, "calmar": None,
            "n_days": 0, "start": None, "end": None,
            "init_close": None, "final_close": None, "warning": "区间内无数据",
        }
    sub_dates = [dates[i] for i in idx]
    sub_closes = np.asarray([closes[i] for i in idx], dtype=float)

    n_days = int(sub_closes.size)
    first_valid = next((i for i, v in enumerate(sub_closes) if _finite(v) and v > 0), None)
    if first_valid is None:
        return {
            "total_return_pct": None, "cagr_pct": None, "annual_vol_pct": None,
            "max_dd_pct": None, "sharpe": None, "sortino": None, "calmar": None,
            "n_days": 0, "start": None, "end": None,
            "init_close": None, "final_close": None, "warning": "收盘价全部无效",
        }
    first = float(sub_closes[first_valid])
    last = float(sub_closes[-1])
    total_ret = _metrics.total_return_pct(first, last)
    years = _metrics.span_years(sub_dates[first_valid:], n_days - first_valid)
    cagr = _metrics.cagr_pct(first, last, years)
    rets = _metrics.daily_returns(sub_closes)
    mdd = _metrics.max_drawdown(sub_closes)["max_dd_pct"]
    return {
        "total_return_pct": _num(total_ret),
        "cagr_pct": _num(cagr),
        "annual_vol_pct": _metrics.annual_volatility_pct(rets),
        "max_dd_pct": _num(mdd),
        "sharpe": _metrics.sharpe_ratio(rets),
        "sortino": _metrics.sortino_ratio(rets),
        "calmar": _metrics.calmar_ratio(cagr, mdd),
        "n_days": n_days,
        "start": sub_dates[0],
        "end": sub_dates[-1],
        "init_close": _num(first),
        "final_close": _num(last),
    }


__all__ = ["run", "benchmark_metrics", "DEFAULT_CFG", "LIMIT_UP_OPEN_PCT",
           "ALLOWED_EXIT_REASONS"]
