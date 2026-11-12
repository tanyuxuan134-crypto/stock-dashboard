# -*- coding: utf-8 -*-
"""信号 / 条件的事件研究（DESIGN §6）：前瞻收益 + t 检验 + 消融。

    forward_returns(bars_long, dates, horizons=(1,5,10,20,40,60)) -> dict
    summarize(rows, horizons=(1,5,10,20,40,60), baseline=None)    -> dict
    ablate(events_by_condition, horizons)                          -> list[dict]

约定
----
* 前瞻收益单位是「百分数」：``10.4`` 表示 +10.4%。
* 默认口径：事件日 T 收盘价 -> T+h 收盘价（close-to-close）。
  可选 ``entry="next_open"``：以 T+1 开盘为买入价 -> T+h 收盘，避免"收盘信号按收盘价成交"
  的隐含乐观（见 docs/BACKTEST_ENGINE.md 的局限）。
* 缺失 / 越界 / 非数值一律 ``None``；**同一股票内部计算，不跨股票拼接**。
* 统计量：单样本 t = mean / (std_ddof1 / sqrt(n))；双侧 p 值用**精确 t 分布**
  （纯 Python 正则化不完全 Beta 函数实现，不依赖 scipy）。
* ``summarize`` 返回的 dict 以 **horizon 整数为键**（如 ``summ[5]``），同时提供了
  字符串别名读取（``summ["fwd_5"]`` / ``summ["5"]`` 也能取到同一个子表），方便上层报告代码。
* ``ablate`` 的 "去掉某条件" 基线 = 其它条件事件的并集（按 date 去重）；
  返回逐 (condition, horizon) 的对照行。
"""

from __future__ import annotations

import math
import statistics

DEFAULT_HORIZONS = (1, 5, 10, 20, 40, 60)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _num(x):
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _iso(value):
    return str(value)[:10]


def _norm_horizons(horizons):
    if horizons is None:
        return tuple(DEFAULT_HORIZONS)
    if isinstance(horizons, (int, float)):
        horizons = (horizons,)
    out = []
    for h in horizons:
        try:
            hi = int(round(float(h)))
        except (TypeError, ValueError):
            continue
        if hi > 0 and hi not in out:
            out.append(hi)
    return tuple(out)


def _bars_close(bars):
    if not isinstance(bars, dict):
        raise ValueError("bars 必须是 dict（DESIGN §2.2/2.3）")
    for key in ("date", "close"):
        if key not in bars or bars.get(key) is None:
            raise ValueError(f"bars 缺少必需字段: {key}")
    dates = [_iso(d) for d in bars["date"]]
    closes = []
    for v in bars["close"]:
        try:
            closes.append(float(v) if v is not None else float("nan"))
        except (TypeError, ValueError):
            closes.append(float("nan"))
    if len(dates) != len(closes):
        raise ValueError("bars.date 与 bars.close 长度不一致")
    return dates, closes


def _bars_open(bars):
    values = bars.get("open")
    if values is None:
        raise ValueError("entry='next_open' 需要 bars.open")
    out = []
    for v in values:
        try:
            out.append(float(v) if v is not None else float("nan"))
        except (TypeError, ValueError):
            out.append(float("nan"))
    return out


def _valid_price(x):
    return x is not None and math.isfinite(x) and x > 0


# ---------------------------------------------------------------------------
# 1) 前瞻收益
# ---------------------------------------------------------------------------
def forward_returns(bars_long, dates, horizons=DEFAULT_HORIZONS, entry="close"):
    """事件日的前瞻收益（%）。

    参数
    ----
    bars_long : DESIGN §2.3 的长历史日线 dict（date/open/close/...）
    dates     : 事件日期列表（ISO 字符串；不在 bars 里的日期该行全为 None）
    horizons  : 前瞻窗口（交易日数），默认 (1,5,10,20,40,60)
    entry     : ``"close"``（默认，T 收盘起算）或 ``"next_open"``（T+1 开盘起算）

    返回
    ----
    ``{"date": [...], "found": [bool,...], "fwd_1": [...], "fwd_5": [...], ...}``
    数组长度与入参 ``dates`` 严格等长、顺序一致。
    """
    horizons = _norm_horizons(horizons)
    entry = str(entry or "close").strip().lower()
    if entry not in ("close", "next_open"):
        raise ValueError("entry 只能是 'close' 或 'next_open'")
    all_dates, closes = _bars_close(bars_long)
    opens = _bars_open(bars_long) if entry == "next_open" else None

    n = len(all_dates)
    if len(closes) != n or (opens is not None and len(opens) != n):
        raise ValueError("bars_long 各列长度不一致")

    pos = {}
    for i, d in enumerate(all_dates):
        pos.setdefault(d, i)

    event_dates = [_iso(d) for d in (dates or [])]
    out: dict[str, list] = {"date": list(event_dates), "found": []}
    for h in horizons:
        out[f"fwd_{h}"] = []

    for d in event_dates:
        i = pos.get(d)
        if i is None:
            out["found"].append(False)
            for h in horizons:
                out[f"fwd_{h}"].append(None)
            continue
        out["found"].append(True)
        if entry == "close":
            base = closes[i]
        else:
            base = opens[i + 1] if i + 1 < n else float("nan")
        for h in horizons:
            j = i + h
            value = None
            if _valid_price(base) and j < n and _valid_price(closes[j]):
                value = _num((closes[j] / base - 1.0) * 100.0)
            out[f"fwd_{h}"].append(value)
    return out


def baseline_forward_returns(bars_long, horizons=DEFAULT_HORIZONS, start=None, end=None,
                             entry="close"):
    """全样本「无条件」前瞻收益的均值（%），作为 ``summarize`` 的 ``vs_baseline_pct`` 基线。

    返回 ``{1: mean_pct, 5: mean_pct, ...}``（键为 horizon 整数，值为 None 表示无样本）。
    """
    horizons = _norm_horizons(horizons)
    all_dates, _ = _bars_close(bars_long)
    start = _iso(start) if start is not None else None
    end = _iso(end) if end is not None else None
    dates = [d for d in all_dates
             if (start is None or d >= start) and (end is None or d <= end)]
    rows = forward_returns(bars_long, dates, horizons=horizons, entry=entry)
    out = {}
    for h in horizons:
        vals = [v for v in rows[f"fwd_{h}"] if v is not None]
        out[h] = _num(sum(vals) / len(vals)) if vals else None
    return out


# ---------------------------------------------------------------------------
# 2) t 分布（纯 Python，不依赖 scipy）
# ---------------------------------------------------------------------------
def _betacf(a, b, x, max_iter=200, eps=3.0e-14, fpmin=1.0e-300):
    """不完全 Beta 函数的连分式（Numerical Recipes betacf）。"""
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def _betai(a, b, x):
    """正则化不完全 Beta 函数 I_x(a,b)。"""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_beta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(log_beta + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def t_two_sided_p(t_stat, df):
    """t 分布双侧 p 值：``P(|T_df| > |t|) = I_{df/(df+t^2)}(df/2, 1/2)``。"""
    t = _num(t_stat)
    df = _num(df)
    if t is None or df is None or df <= 0:
        return None
    t = abs(t)
    if t == 0.0:
        return 1.0
    p = _betai(df / 2.0, 0.5, df / (df + t * t))
    if not math.isfinite(p):
        return None
    return min(1.0, max(0.0, p))


# ---------------------------------------------------------------------------
# 3) 汇总统计
# ---------------------------------------------------------------------------
class _HorizonTable(dict):
    """以 horizon 整数为键的 dict，额外支持 ``table["fwd_5"]`` / ``table["5"]`` 读取。"""

    def __getitem__(self, key):
        key = self._normalize_key(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        try:
            return self[key]
        except (KeyError, TypeError):
            return default

    def __contains__(self, key):
        return super().__contains__(self._normalize_key(key))

    @staticmethod
    def _normalize_key(key):
        if isinstance(key, str):
            text = key[4:] if key.startswith("fwd_") else key
            try:
                return int(text)
            except ValueError:
                return key
        return key


def _stats(values):
    """一组样本 -> 描述统计 + 单样本 t 检验（双侧）。"""
    vals = []
    for v in values:
        f = _num(v)
        if f is not None:
            vals.append(f)
    n = len(vals)
    empty = {"n": 0, "mean_pct": None, "median_pct": None, "std_pct": None,
             "t_stat": None, "win_rate_pct": None, "p_value_approx": None}
    if n == 0:
        return empty
    mean = sum(vals) / n
    median = statistics.median(vals)
    std = statistics.stdev(vals) if n >= 2 else None
    t_stat = None
    p_value = None
    if std is not None and std > 0:
        t_stat = mean / (std / math.sqrt(n))
        p_value = t_two_sided_p(t_stat, n - 1)
    win_rate = sum(1 for v in vals if v > 0) / n * 100.0
    return {
        "n": n,
        "mean_pct": _num(mean),
        "median_pct": _num(median),
        "std_pct": _num(std),
        "t_stat": _num(t_stat),
        "win_rate_pct": _num(win_rate),
        "p_value_approx": _num(p_value),
    }


def _row_value(row, horizon):
    """从事件行里取某 horizon 的前瞻收益（兼容 fwd_5 / 5 / "5" 三种写法）。"""
    if not isinstance(row, dict):
        return None
    for key in (f"fwd_{horizon}", horizon, str(horizon)):
        if key in row:
            return row[key]
    return None


def _row_date(row):
    if not isinstance(row, dict):
        return None
    for key in ("date", "_date", "event_date"):
        if row.get(key) is not None:
            return _iso(row[key])
    return None


def summarize(rows, horizons=DEFAULT_HORIZONS, baseline=None):
    """事件组的前瞻收益统计。

    返回 ``{horizon: {"n","mean_pct","median_pct","t_stat","win_rate_pct",
    "p_value_approx","vs_baseline_pct","std_pct"}, ...}``

    ``baseline`` 可选：``{horizon: mean_pct}``（可直接用 ``baseline_forward_returns``），
    提供时 ``vs_baseline_pct = mean_pct - baseline[horizon]``，否则为 None。
    """
    horizons = _norm_horizons(horizons)
    rows = list(rows or [])
    table = _HorizonTable()
    for h in horizons:
        stats = _stats([_row_value(row, h) for row in rows])
        base = None
        if isinstance(baseline, dict):
            base = _num(baseline.get(h, baseline.get(str(h), baseline.get(f"fwd_{h}"))))
        elif baseline is not None:
            base = _num(baseline)
        if base is not None and stats["mean_pct"] is not None:
            stats["vs_baseline_pct"] = _num(stats["mean_pct"] - base)
        else:
            stats["vs_baseline_pct"] = None
        table[h] = stats
    return table


# ---------------------------------------------------------------------------
# 4) 消融
# ---------------------------------------------------------------------------
def _condition_rows(events):
    """把事件列表规范成 {date_key: row}，保证同一事件只算一次。"""
    out = {}
    for idx, event in enumerate(events or []):
        if isinstance(event, dict):
            row = event
        elif event is None:
            continue
        else:
            row = {"date": event}
        key = _row_date(row)
        if key is None:
            key = f"__noid_{idx}"
        out[key] = row
    return out


def _welch_t(a, b):
    """Welch 两样本 t 检验（kept vs dropped），返回 (t, p)。"""
    if len(a) < 2 or len(b) < 2:
        return None, None
    ma = statistics.fmean(a)
    mb = statistics.fmean(b)
    va = statistics.variance(a)
    vb = statistics.variance(b)
    se2 = va / len(a) + vb / len(b)
    if se2 <= 0:
        return None, None
    t_stat = (ma - mb) / math.sqrt(se2)
    df = (se2 ** 2) / ((va / len(a)) ** 2 / (len(a) - 1) + (vb / len(b)) ** 2 / (len(b) - 1))
    return _num(t_stat), t_two_sided_p(t_stat, df)


def ablate(events_by_condition, horizons=DEFAULT_HORIZONS):
    """条件消融表：逐条保留 / 去掉某个条件后的前瞻收益对照。

    参数
    ----
    events_by_condition : ``{条件名: [事件行 dict, ...]}``（事件行含 date 与 fwd_*）
    horizons            : 前瞻窗口

    返回
    ----
    ``list[dict]``，每个 ``(condition, horizon)`` 一行，另有每个 horizon 一行
    ``condition="__baseline__"``（全体事件并集）。字段：
        condition, horizon,
        n_kept, mean_kept_pct, median_kept_pct, win_rate_kept_pct, t_stat_kept, p_value_approx,
        n_dropped, mean_dropped_pct, t_stat_dropped,
        delta_pct(=kept-dropped), vs_baseline_pct(同 delta),
        t_stat_diff, p_value_diff（Welch 两样本检验）
    """
    horizons = _norm_horizons(horizons)
    if not isinstance(events_by_condition, dict):
        raise ValueError("events_by_condition 必须是 dict[str, list[dict]]")

    per_condition = {name: _condition_rows(events)
                     for name, events in events_by_condition.items()}
    all_rows: dict[str, dict] = {}
    for rows in per_condition.values():
        for key, row in rows.items():
            all_rows.setdefault(key, row)

    names = list(per_condition.keys())
    out = []
    for h in horizons:
        base_vals = [_row_value(row, h) for row in all_rows.values()]
        base_stats = _stats(base_vals)
        baseline_row = {
            "condition": "__baseline__",
            "horizon": h,
            "n_kept": base_stats["n"],
            "mean_kept_pct": base_stats["mean_pct"],
            "median_kept_pct": base_stats["median_pct"],
            "win_rate_kept_pct": base_stats["win_rate_pct"],
            "t_stat_kept": base_stats["t_stat"],
            "p_value_approx": base_stats["p_value_approx"],
            "n_dropped": 0,
            "mean_dropped_pct": None,
            "t_stat_dropped": None,
            "delta_pct": None,
            "vs_baseline_pct": None,
            "t_stat_diff": None,
            "p_value_diff": None,
        }
        out.append(baseline_row)

        for name in names:
            kept_rows = per_condition[name]
            dropped_rows = {k: v for k, v in all_rows.items() if k not in kept_rows}
            kept_vals = [_num(_row_value(row, h)) for row in kept_rows.values()]
            kept_vals = [v for v in kept_vals if v is not None]
            dropped_vals = [_num(_row_value(row, h)) for row in dropped_rows.values()]
            dropped_vals = [v for v in dropped_vals if v is not None]
            kept_stats = _stats(kept_vals)
            dropped_stats = _stats(dropped_vals)
            delta = None
            if kept_stats["mean_pct"] is not None and dropped_stats["mean_pct"] is not None:
                delta = kept_stats["mean_pct"] - dropped_stats["mean_pct"]
            t_diff, p_diff = _welch_t(kept_vals, dropped_vals)
            out.append({
                "condition": name,
                "horizon": h,
                "n_kept": kept_stats["n"],
                "mean_kept_pct": kept_stats["mean_pct"],
                "median_kept_pct": kept_stats["median_pct"],
                "win_rate_kept_pct": kept_stats["win_rate_pct"],
                "t_stat_kept": kept_stats["t_stat"],
                "p_value_approx": kept_stats["p_value_approx"],
                "n_dropped": dropped_stats["n"],
                "mean_dropped_pct": dropped_stats["mean_pct"],
                "t_stat_dropped": dropped_stats["t_stat"],
                "delta_pct": _num(delta),
                "vs_baseline_pct": _num(delta),
                "t_stat_diff": t_diff,
                "p_value_diff": p_diff,
            })
    return out


__all__ = ["forward_returns", "summarize", "ablate", "baseline_forward_returns",
           "t_two_sided_p", "DEFAULT_HORIZONS"]
