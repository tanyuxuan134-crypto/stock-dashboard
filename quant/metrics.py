# -*- coding: utf-8 -*-
"""绩效指标（纯 numpy / 纯函数，不依赖 pandas、scipy）。

DESIGN §5 约定的入口：
    metrics_from_equity(equity, trades, init_cash) -> dict

约定
----
* 所有 ``*_pct`` 字段单位是「百分数」：``12.3`` 表示 +12.3%。
* 无定义 / 样本不足 / 除零的指标一律返回 ``None``（JSON 安全，不产生 inf / nan）。
* ``equity`` 可以是 ``[{"date","equity","cash","n_pos"}, ...]``（回测引擎的输出），
  也可以是纯 ``[float, ...]`` 净值序列（小工具函数都兼容这两种）。
* 年化按 A 股常用口径：一年 252 个交易日；CAGR 的时间跨度优先用日历日 / 365.25
  （有 ``date`` 时），没有日期时退化为 ``(n-1)/252`` 年。

小工具
------
    max_drawdown(series)            -> {"max_dd_pct","peak_index","trough_index",...}
    max_drawdown_pct(series)        -> float
    drawdown_series(series)         -> list[float]
    total_return_pct(initial, final)
    cagr_pct(initial, final, years)
    span_years(dates, n_points)
    daily_returns(values)
    annual_volatility_pct(returns)
    sharpe_ratio(returns, rf_annual=0.0, periods=252)
    sortino_ratio(returns, rf_annual=0.0, periods=252)
    calmar_ratio(cagr_pct_value, max_dd_pct_value)
"""

from __future__ import annotations

import math
from datetime import date, datetime

import numpy as np

PERIODS_PER_YEAR = 252          # A 股一年交易日（年化用）
DAYS_PER_YEAR = 365.25          # CAGR 的日历日年化
RISK_FREE_ANNUAL = 0.0          # 无风险利率：0（DESIGN §5 未给，按 0 处理）


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _num(x):
    """转成 JSON 安全的 float；None / nan / inf -> None。"""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x):
        return None
    return x


def _equity_values(series):
    """把 equity（list[dict] / list[float] / ndarray）统一成 float ndarray。"""
    if series is None:
        return np.asarray([], dtype=float)
    if isinstance(series, np.ndarray):
        return np.asarray(series, dtype=float).ravel()
    vals = []
    for item in series:
        if isinstance(item, dict):
            vals.append(item.get("equity"))
        else:
            vals.append(item)
    out = []
    for v in vals:
        try:
            out.append(float(v) if v is not None else float("nan"))
        except (TypeError, ValueError):
            out.append(float("nan"))
    return np.asarray(out, dtype=float)


def _finite_values(values):
    """去除 None / nan / inf。"""
    if values is None:
        return np.asarray([], dtype=float)
    if not isinstance(values, np.ndarray):
        values = np.asarray(list(values), dtype=float)
    values = np.asarray(values, dtype=float).ravel()
    return values[np.isfinite(values)]


# ---------------------------------------------------------------------------
# 区间 / 年化
# ---------------------------------------------------------------------------
def _parse_iso_date(value):
    """ISO 日期字符串 / date / datetime / np.datetime64 -> date；失败返回 None。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, np.datetime64):
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def span_years(dates, n_points=None, periods_per_year=PERIODS_PER_YEAR):
    """时间跨度（年）。

    优先用首尾日历日 / 365.25；日期不可解析时退化为 ``(n_points-1)/periods_per_year``。
    返回 None 表示无法估计。
    """
    if dates:
        seq = list(dates)
        if seq:
            d0 = _parse_iso_date(seq[0])
            d1 = _parse_iso_date(seq[-1])
            if d0 is not None and d1 is not None:
                days = (d1 - d0).days
                if days > 0:
                    return days / DAYS_PER_YEAR
    if n_points is not None and n_points >= 2:
        return (int(n_points) - 1) / float(periods_per_year)
    return None


def total_return_pct(initial, final):
    """总收益率（%）。"""
    initial = _num(initial)
    final = _num(final)
    if initial is None or final is None or initial <= 0:
        return None
    return _num((final / initial - 1.0) * 100.0)


def cagr_pct(initial, final, years):
    """复合年化收益率（%）。"""
    initial = _num(initial)
    final = _num(final)
    years = _num(years)
    if initial is None or final is None or years is None:
        return None
    if initial <= 0 or final <= 0 or years <= 0:
        return None
    growth = final / initial
    return _num((growth ** (1.0 / years) - 1.0) * 100.0)


def calmar_ratio(cagr_value, max_dd_value):
    """Calmar = CAGR / |MaxDD|。MaxDD 为 0 或缺失时返回 None。"""
    c = _num(cagr_value)
    d = _num(max_dd_value)
    if c is None or d is None or d >= 0:
        return None
    return _num(c / abs(d))


# ---------------------------------------------------------------------------
# 回撤
# ---------------------------------------------------------------------------
def drawdown_series(series):
    """逐点回撤（%），负值；以历史最高净值为峰值。"""
    vals = _equity_values(series)
    if vals.size == 0:
        return []
    out = []
    peak = float("-inf")
    for v in vals:
        if not math.isfinite(v):
            out.append(None)
            continue
        if v > peak:
            peak = v
        out.append(_num((v / peak - 1.0) * 100.0) if peak > 0 else 0.0)
    return out


def max_drawdown(series):
    """最大回撤。

    返回 ``{"max_dd_pct", "peak_index", "trough_index", "peak_value", "trough_value"}``。
    ``max_dd_pct`` 为负值（0 表示从未回撤）。
    """
    vals = _equity_values(series)
    if vals.size == 0:
        return {"max_dd_pct": None, "peak_index": None, "trough_index": None,
                "peak_value": None, "trough_value": None}
    peak = float("-inf")
    peak_i = 0
    best = 0.0
    best_peak_i = 0
    best_trough_i = 0
    for i, v in enumerate(vals):
        v = float(v)
        if not math.isfinite(v):
            continue
        if v > peak:
            peak = v
            peak_i = i
        if peak > 0:
            dd = v / peak - 1.0
            if dd < best:
                best = dd
                best_peak_i = peak_i
                best_trough_i = i
    best_peak_i = int(best_peak_i)
    best_trough_i = int(best_trough_i)
    return {
        "max_dd_pct": _num(best * 100.0),
        "peak_index": best_peak_i,
        "trough_index": best_trough_i,
        "peak_value": _num(vals[best_peak_i]),
        "trough_value": _num(vals[best_trough_i]),
    }


def max_drawdown_pct(series):
    """最大回撤（%，负值）的便捷封装。"""
    return max_drawdown(series)["max_dd_pct"]


# ---------------------------------------------------------------------------
# 收益序列 -> 风险指标
# ---------------------------------------------------------------------------
def daily_returns(values):
    """净值序列 -> 日收益率 ndarray（长度 n-1；无法计算的位为 nan）。"""
    vals = _equity_values(values)
    if vals.size < 2:
        return np.asarray([], dtype=float)
    prev = vals[:-1]
    cur = vals[1:]
    out = np.full(prev.shape, np.nan, dtype=float)
    mask = np.isfinite(prev) & np.isfinite(cur) & (prev > 0)
    with np.errstate(invalid="ignore", divide="ignore"):
        out[mask] = cur[mask] / prev[mask] - 1.0
    return out


def annual_volatility_pct(returns, periods=PERIODS_PER_YEAR):
    """年化波动率（%），日收益标准差 × sqrt(252)。"""
    r = _finite_values(returns)
    if r.size < 2:
        return None
    return _num(float(np.std(r, ddof=1)) * math.sqrt(periods) * 100.0)


def sharpe_ratio(returns, rf_annual=RISK_FREE_ANNUAL, periods=PERIODS_PER_YEAR):
    """夏普比率（rf=0，日频 ×sqrt(252)）。样本不足或零波动返回 None。"""
    r = _finite_values(returns)
    if r.size < 2:
        return None
    excess = r - float(rf_annual) / periods
    sd = float(np.std(excess, ddof=1))
    if sd <= 0 or not math.isfinite(sd):
        return None
    return _num(float(np.mean(excess)) / sd * math.sqrt(periods))


def sortino_ratio(returns, rf_annual=RISK_FREE_ANNUAL, periods=PERIODS_PER_YEAR):
    """索提诺比率：均值 / 下行波动（下行 = min(r,0) 的均方根）× sqrt(252)。

    没有下行样本（全为正收益）时返回 None（无法定义），这是有意为之，不做 inf。
    """
    r = _finite_values(returns)
    if r.size < 2:
        return None
    excess = r - float(rf_annual) / periods
    downside = np.minimum(excess, 0.0)
    dd = float(math.sqrt(float(np.mean(downside * downside))))
    if dd <= 0 or not math.isfinite(dd):
        return None
    return _num(float(np.mean(excess)) / dd * math.sqrt(periods))


# ---------------------------------------------------------------------------
# 主入口：由 equity + trades 计算全部指标
# ---------------------------------------------------------------------------
def _trade_returns(trades):
    out = []
    for t in trades or []:
        if not isinstance(t, dict):
            continue
        v = _num(t.get("ret_pct"))
        if v is not None:
            out.append(v)
    return out


def _trade_pnls(trades):
    out = []
    for t in trades or []:
        if isinstance(t, dict) and t.get("pnl") is not None:
            v = _num(t.get("pnl"))
            if v is not None:
                out.append(v)
    return out


def metrics_from_equity(equity, trades, init_cash):
    """由净值曲线 + 成交明细算绩效表（DESIGN §5 的 metrics）。

    返回键（冻结，DESIGN §5.6）：
        total_return_pct, cagr_pct, max_dd_pct, sharpe, sortino, win_rate_pct,
        avg_win_pct, avg_loss_pct, profit_factor, n_trades, avg_hold_days,
        expectancy_pct, exposure_pct, calmar
    额外附带：annual_vol_pct, n_wins, n_losses, n_breakeven, final_equity,
        init_cash, start, end, n_days, avg_trade_pnl。
    """
    equity = equity or []
    trades = trades or []
    init_cash = _num(init_cash)
    vals = _equity_values(equity)

    n_points = int(vals.size)
    final_equity = _num(vals[-1]) if n_points else init_cash
    dates = [e.get("date") for e in equity if isinstance(e, dict)]
    years = span_years(dates, n_points) if dates else span_years(None, n_points)

    total_ret = total_return_pct(init_cash, final_equity) if init_cash else None
    cagr = cagr_pct(init_cash, final_equity, years) if init_cash else None
    rets = daily_returns(vals)
    vol = annual_volatility_pct(rets)
    sharpe = sharpe_ratio(rets)
    sortino = sortino_ratio(rets)
    mdd = max_drawdown(vals)["max_dd_pct"]
    calmar = calmar_ratio(cagr, mdd)

    t_rets = _trade_returns(trades)
    n_trades = len(t_rets)
    wins = [x for x in t_rets if x > 0]
    losses = [x for x in t_rets if x < 0]
    n_wins = len(wins)
    n_losses = len(losses)
    n_breakeven = n_trades - n_wins - n_losses
    win_rate = (n_wins / n_trades * 100.0) if n_trades else None
    avg_win = (sum(wins) / len(wins)) if wins else None
    avg_loss = (sum(losses) / len(losses)) if losses else None
    expectancy = (sum(t_rets) / n_trades) if n_trades else None

    # profit factor 优先用绝对盈亏（更稳），没有 pnl 时退化为按 ret_pct 加权
    pnls = _trade_pnls(trades)
    if pnls:
        gross_win = sum(p for p in pnls if p > 0)
        gross_loss = abs(sum(p for p in pnls if p < 0))
    else:
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
    if gross_loss > 0:
        profit_factor = gross_win / gross_loss
    else:
        profit_factor = None  # 没有亏损样本：不做 inf

    holds = [h for h in (_num(t.get("hold_days")) for t in trades if isinstance(t, dict)) if h is not None]
    avg_hold = (sum(holds) / len(holds)) if holds else None

    # 暴露度：有持仓（n_pos > 0）的交易日占比
    n_pos_seq = [e.get("n_pos") for e in equity if isinstance(e, dict) and "n_pos" in e]
    if n_pos_seq:
        exposure = sum(1 for v in n_pos_seq if (v or 0) > 0) / len(n_pos_seq) * 100.0
    else:
        exposure = None

    start = dates[0] if dates else None
    end = dates[-1] if dates else None
    return {
        # --- DESIGN §5.6 冻结键 ---
        "total_return_pct": _num(total_ret),
        "cagr_pct": _num(cagr),
        "max_dd_pct": _num(mdd),
        "sharpe": _num(sharpe),
        "sortino": _num(sortino),
        "win_rate_pct": _num(win_rate),
        "avg_win_pct": _num(avg_win),
        "avg_loss_pct": _num(avg_loss),
        "profit_factor": _num(profit_factor),
        "n_trades": n_trades,
        "avg_hold_days": _num(avg_hold),
        "expectancy_pct": _num(expectancy),
        "exposure_pct": _num(exposure),
        "calmar": _num(calmar),
        # --- 附加信息 ---
        "annual_vol_pct": _num(vol),
        "n_wins": n_wins,
        "n_losses": n_losses,
        "n_breakeven": n_breakeven,
        "avg_trade_pnl": _num((sum(pnls) / len(pnls)) if pnls else None),
        "final_equity": _num(final_equity),
        "init_cash": init_cash,
        "start": start,
        "end": end,
        "n_days": n_points,
    }
