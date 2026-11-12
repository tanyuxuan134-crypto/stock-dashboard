# -*- coding: utf-8 -*-
"""quant/datafeed.py — 数据层：抓取 / 解析 / 缓存 / 复权因子（T1）。

对外接口（DESIGN §1 / §2）：
    http_get(url, headers=None, retries=3) -> str
    build_pool(write=True)               -> dict   data/pool.json
    build_bars(key, name, kind)          -> dict   data/bars/{key}.json      (腾讯 640 行前复权)
    build_bars_long(key, name, kind)     -> dict   data/bars_long/{key}.json (腾讯 2000 行不复权)
    build_adj(code, key=None)            -> dict   data/adj/{code}.json     (前复权累计因子)
    build_val(code)                      -> dict   data/val/{code}.json     (东财估值 1400 行)
    build_fin(code)                      -> dict   data/fin/{code}.json     (东财 F10 财务)
    build_snapshot()                     -> dict   data/snapshot.json        (新浪全 A 快照)
    build_all(...)                       -> dict   全量构建 + data/meta.json
    validate(...)                        -> dict   落盘结构自检

所有落盘 JSON 均为 UTF-8、ensure_ascii=False。
缓存目录 data/_cache（原始响应文本），--refresh 时忽略缓存。

实测结论（2026-10-28，见 docs/DATA.md）：
1) 腾讯 ``qfq`` 不是严格等比复权：纯现金分红段内近似 qfq_close = raw_close - C（等差），
   送转按 (x - D) / (1 + B) 链式；但部分股票腾讯口径与事件链模型有 0.5%~1% 偏差，
   且存在没有事件字典的非分红除权。因此 adj/segments 以腾讯真前复权为基准**校准**：
   重叠区间逐日 factor = qfq/raw（精确复现，P99≈0.01%）；重叠起点之前用事件链式外推
   并按边界比例缩放（收益由事件比例决定）；model_p99 记录纯事件模型的偏差。
2) 腾讯 raw 行第 6 列自带分红送转事件字典（cqr 除权日 / FHcontent / fh_sh），
   用它重建事件比东财分红表更贴合腾讯口径；东财 RPT_SHAREBONUS_DET 仅做交叉校验。
3) ETF 的腾讯 raw 行没有事件字典；且品种无 adj 需求，本模块只为股票生成 adj。
4) 指数无 qfqday，接口回落为 day（不复权，因子恒 1）。
"""

from __future__ import annotations

import gzip
import json
import math
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from bisect import bisect_left, bisect_right
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

# 所有「今天是哪天 / 现在几点」的判断都必须用北京时间。
# 本机在 +08:00 没问题，但云端 runner（GitHub Actions）默认 UTC：
# 若用 datetime.now()，北京时间 15:20 会被当成 UTC 07:20 → 误判「还没收盘」，
# 把当天真正的收盘 bar 当成未收盘数据剔掉。
BJ_TZ = timezone(timedelta(hours=8))


def bj_now():
    """当前北京时间（naive，便于和 strftime 一起用）。"""
    return datetime.now(BJ_TZ).replace(tzinfo=None)


__all__ = [
    "DataError", "http_get",
    "build_pool", "build_bars", "build_bars_long", "build_adj",
    "build_val", "build_fin", "build_snapshot", "build_all", "validate",
    "ROOT", "DATA", "BARS", "BARS_LONG", "ADJ", "VAL", "FIN", "SNAPSHOT", "POOL", "META",
]

# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
CACHE = os.path.join(DATA, "_cache")
BARS = os.path.join(DATA, "bars")
BARS_LONG = os.path.join(DATA, "bars_long")
ADJ = os.path.join(DATA, "adj")
VAL = os.path.join(DATA, "val")
FIN = os.path.join(DATA, "fin")
DOCS = os.path.join(ROOT, "docs")
POOL_SOURCE = os.path.join(DATA, "pool_source.json")
POOL = os.path.join(DATA, "pool.json")
SNAPSHOT = os.path.join(DATA, "snapshot.json")
META = os.path.join(DATA, "meta.json")

# --------------------------------------------------------------------------- #
# 数据源常量
# --------------------------------------------------------------------------- #
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

TX_KLINE_URL = ("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
                "?param={param}")
TX_QUOTE_URL = "https://qt.gtimg.cn/q={codes}"
EM_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
SINA_HQ_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               "Market_Center.getHQNodeData?page={page}&num={num}&sort=symbol&asc=1&node={node}")
SINA_COUNT_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                  "Market_Center.getHQNodeStockCount?node={node}")
SINA_HY_URL = "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"

SINA_HEADERS = {"Referer": "https://finance.sina.com.cn",
                "Accept": "*/*"}
EM_HEADERS = {"Referer": "https://data.eastmoney.com/",
              "Accept": "application/json, text/plain, */*"}

MIN_INTERVAL = 0.05          # 全局限速：两次请求最小间隔（秒）
HTTP_TIMEOUT = 25            # 单请求超时（秒）

# 缓存有效期（秒）。None=永不过期；K 线必须短 TTL，否则收盘后重跑会一直命中旧缓存。
KLINE_TTL = 600              # 腾讯 K 线 10 分钟
EM_TTL = 6 * 3600            # 东财估值 / 财务 6 小时
SINA_TTL = 3600              # 新浪全 A 快照 / 行业映射 1 小时

SECTORS = ("consume", "tech", "finance", "other")

# 固定成分（与 quant/config.py 的 ETF_ROLES / INDEX_LIST 一致）
ETF_DEFAULT = (
    ("510300", "沪深300ETF", "core"),
    ("510500", "中证500ETF", "core"),
    ("510880", "红利ETF", "dividend"),
    ("515080", "中证红利ETF", "dividend"),
    ("512480", "半导体ETF", "satellite"),
    ("588000", "科创50ETF", "satellite"),
    ("511360", "短融ETF", "cash"),
)
INDEX_DEFAULT = (
    ("000300", "沪深300", "sh000300"),
    ("000001", "上证指数", "sh000001"),
    ("399006", "创业板指", "sz399006"),
)

# 复权硬性自检阈值
ADJ_P99_MAX = 0.005          # 与腾讯真前复权相对误差 P99 < 0.5%（校准后）
ADJ_MODEL_P99_MAX = 0.02     # 纯事件链模型偏差 >2% 视为"模型解释不了"，前置区间不可信 -> 剔除
ADJ_MIN_OVERLAP = 20         # 自检最少重叠交易日（新上市股票全历史即重叠）


class DataError(RuntimeError):
    """数据源不可用 / 解析失败 / 样本不足。"""


# --------------------------------------------------------------------------- #
# HTTP + 限速 + 缓存
# --------------------------------------------------------------------------- #
_rate_lock = threading.Lock()
_rate_last = [0.0]


def _throttle():
    with _rate_lock:
        now = time.time()
        wait = _rate_last[0] + MIN_INTERVAL - now
        if wait > 0:
            time.sleep(wait)
            now = time.time()
        _rate_last[0] = max(now, _rate_last[0] + MIN_INTERVAL)


def http_get(url, headers=None, retries=3, encoding=None):
    """GET 文本；自动 gzip/deflate 解压、自动识别编码、失败重试。

    encoding 显式指定时强制按该编码解码（新浪 gbk 页面）。
    """
    hdrs = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "close",
    }
    if headers:
        hdrs.update(headers)
    last_err = None
    for attempt in range(max(1, int(retries))):
        try:
            _throttle()
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                raw = resp.read()
                enc = (resp.headers.get("Content-Encoding") or "").lower()
                if "gzip" in enc:
                    raw = gzip.decompress(raw)
                elif "deflate" in enc:
                    try:
                        raw = zlib.decompress(raw)
                    except zlib.error:
                        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                charset = encoding or resp.headers.get_content_charset() or "utf-8"
                cs = charset.lower().replace("-", "").replace("_", "")
                if cs in ("gb2312", "gbk", "gb18030", "gb231280"):
                    charset = "gb18030"
                return raw.decode(charset, errors="replace")
        except Exception as exc:                      # noqa: BLE001 - 网络层兜底
            last_err = exc
            if attempt < retries - 1:
                time.sleep(0.5 * (attempt + 1) + random.random() * 0.3)
    raise DataError(f"HTTP GET 失败 url={url[:150]} err={last_err!r}")


def _cache_path(name):
    return os.path.join(CACHE, name)


def cache_read(name):
    path = _cache_path(name)
    try:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
    except OSError:
        return None
    return None


def cache_write(name, text):
    os.makedirs(CACHE, exist_ok=True)
    path = _cache_path(name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _cache_age(name):
    try:
        return max(0.0, time.time() - os.path.getmtime(_cache_path(name)))
    except OSError:
        return float("inf")


def cached_text(name, url, headers=None, refresh=False, encoding=None, ttl=None):
    """带磁盘缓存的 GET。缓存里存的是解码后的 UTF-8 文本。

    ttl（秒）：缓存文件超过该年龄视为过期并重新抓取；None 表示永不过期。
    """
    if not refresh:
        hit = cache_read(name)
        if hit is not None and (ttl is None or _cache_age(name) <= ttl):
            return hit
    text = http_get(url, headers=headers, encoding=encoding)
    if text and len(text) > 2:
        cache_write(name, text)
    return text


def _json(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError) as exc:
        snippet = (text or "")[:120]
        raise DataError(f"JSON 解析失败: {exc!r} head={snippet!r}") from None


def _load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_json(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    os.replace(tmp, path)


def _f(value):
    """宽松转 float：空串 / '-' / 'null' / nan / inf -> None。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        num = float(value)
    else:
        text = str(value).strip().replace(",", "")
        if text in ("", "-", "--", "None", "null", "nan", "NaN"):
            return None
        try:
            num = float(text)
        except ValueError:
            return None
    if math.isnan(num) or math.isinf(num):
        return None
    return num


# --------------------------------------------------------------------------- #
# 代码 -> 腾讯 symbol
# --------------------------------------------------------------------------- #
def symbol_of(code):
    """股票/ETF 6 位代码 -> 带交易所前缀的腾讯 symbol。"""
    code = str(code).strip().zfill(6)
    if code[:1] in ("6", "5", "9"):
        return "sh" + code
    return "sz" + code


def index_symbol_of(code):
    """指数 6 位代码 -> 腾讯 symbol（中证/上证 000xxx -> sh，国证 399xxx -> sz）。"""
    code = str(code).strip().zfill(6)
    return ("sz" if code.startswith("39") else "sh") + code


def secucode_of(code):
    """6 位股票代码 -> 东财 SECUCODE（带 .SH/.SZ/.BJ 后缀）。"""
    code = str(code).strip().zfill(6)
    first = code[:1]
    if first == "6" or code[:2] in ("68", "69"):
        return code + ".SH"
    if first in ("0", "3"):
        return code + ".SZ"
    if first in ("4", "8"):
        return code + ".BJ"
    if first == "9":
        return code + ".BJ"
    return code + ".SH"


# --------------------------------------------------------------------------- #
# 腾讯 K 线
# --------------------------------------------------------------------------- #
def tx_kline(sym, n=640, fq="qfq", refresh=False):
    """腾讯日线原始行列表。fq='qfq' 取前复权 640 行；fq='' 取不复权 2000 行。"""
    param = f"{sym},day,,,{int(n)},{fq}"
    url = TX_KLINE_URL.format(param=param)
    tag = "qfq" if fq else "raw"
    name = f"tx_{tag}_{sym}_{int(n)}.txt"
    text = cached_text(name, url, refresh=refresh, ttl=KLINE_TTL)
    payload = _json(text)
    data = (payload.get("data") or {}).get(sym) or {}
    rows = data.get("qfqday") or data.get("day") or []
    if not rows and fq:
        # 指数没有 qfqday，接口回落为 day
        rows = data.get("day") or []
    return rows


def parse_tx_rows(rows):
    """腾讯行 -> 列式 dict（按日期升序、去重）。行结构：
    [date, open, close, high, low, volume(手), {}, turnover(%), amount(万元), ...]
    部分品种行尾多 1~2 个 '0.00' 列，统一取前 9 列。
    """
    recs = {}
    for row in rows or []:
        if not isinstance(row, (list, tuple)) or len(row) < 9:
            continue
        date = str(row[0])[:10]
        if len(date) != 10:
            continue
        close = _f(row[2])
        if close is None or close <= 0:
            continue
        recs[date] = {
            "date": date,
            "open": _f(row[1]),
            "close": close,
            "high": _f(row[3]),
            "low": _f(row[4]),
            "volume": _f(row[5]),
            "turnover": _f(row[7]),
            "amount": _f(row[8]),
        }
    out = {k: [] for k in ("date", "open", "close", "high", "low", "volume", "turnover", "amount")}
    for date in sorted(recs):
        for key in out:
            out[key].append(recs[date][key])
    return out


def _bars_doc(key, name, kind, fq, source, parsed):
    if not parsed["date"]:
        raise DataError(f"K 线为空 key={key}")
    doc = {
        "key": key,
        "name": name or key,
        "kind": kind,
        "fq": fq,
        "source": source,
        "start": parsed["date"][0],
        "end": parsed["date"][-1],
    }
    doc.update(parsed)
    _strip_unclosed_bar(doc)
    return doc


# 收盘前腾讯 K 线有时会带上「今天这根还没走完的 bar」——必须落盘前剔掉，
# 否则盘中跑（或计划任务在交易时段跑）会用半日数据污染均线/量比/信号。
CLOSED_HHMM = 1505  # 15:05 之后才把「今天」当成已收盘
_BAR_KEYS = ("date", "open", "close", "high", "low", "volume", "turnover", "amount")


def _strip_unclosed_bar(doc):
    """去掉末根的「未收盘 bar」（今天且现在未到 15:05）或未来日期 bar。返回是否剔除过。"""
    changed = False
    dates = doc.get("date") or []
    while dates:
        last = str(dates[-1])
        now = bj_now()
        today = now.strftime("%Y-%m-%d")
        closed = now.strftime("%H%M") >= str(CLOSED_HHMM)
        if last < today or (last == today and closed):
            break
        for key in _BAR_KEYS:
            series = doc.get(key)
            if isinstance(series, list) and series:
                series.pop()
        changed = True
        dates = doc.get("date") or []
    if changed:
        doc["stripped_intraday"] = True
        if doc.get("date"):
            doc["end"] = doc["date"][-1]
    return changed


def build_bars(key, name=None, kind="stock", refresh=False, write=True, quotes=None):
    """data/bars/{key}.json —— 腾讯前复权日线（640 行，约 2.6 年）。

    若传入 quotes（fetch_quotes 的结果）且实时行情日期比 K 线最后一天新，
    则把当日 OHLCV 追加成最新一根 bar。原因：腾讯 640 行 K 线接口收盘后常滞后
    一个交易日才落地，而 qt.gtimg.cn 实时接口已有当日收盘数据。
    """
    rows = tx_kline(key, 640, "qfq", refresh=refresh)
    parsed = parse_tx_rows(rows)
    doc = _bars_doc(key, name, kind, "qfq", "tencent", parsed)
    quote = (quotes or {}).get(key)
    if quote:
        merge_quote_bar(doc, quote)
    if write:
        _write_json(os.path.join(BARS, f"{key}.json"), doc)
    return doc


def build_bars_long(key, name=None, kind="stock", refresh=False, write=True):
    """data/bars_long/{key}.json —— 腾讯不复权长历史（2000 行，约 8 年）。"""
    rows = tx_kline(key, 2000, "", refresh=refresh)
    parsed = parse_tx_rows(rows)
    doc = _bars_doc(key, name, kind, "none", "tencent-long", parsed)
    if write:
        _write_json(os.path.join(BARS_LONG, f"{key}.json"), doc)
    return doc


# --------------------------------------------------------------------------- #
# 腾讯实时行情（收盘后补当日 bar）
# --------------------------------------------------------------------------- #
QUOTE_BATCH = 60             # qt.gtimg.cn 单次最多查询只数（URL 长度限制）


def _quote_fields(text):
    """解析 qt.gtimg.cn 返回文本 -> {key: fields 列表}。"""
    out = {}
    for chunk in (text or "").split(";"):
        chunk = chunk.strip()
        if not chunk.startswith("v_") or "=" not in chunk:
            continue
        head, body = chunk.split("=", 1)
        key = head[2:].strip()
        body = body.strip().strip('"')
        if not key or not body:
            continue
        out[key] = body.split("~")
    return out


def _quote_record(key, fields):
    """腾讯实时字段 -> 统一结构（单位与 data/bars 对齐：volume=手、amount=万元、turnover=%）。"""
    if len(fields) < 50:
        return None
    price = _f(fields[3])
    if price is None or price <= 0:
        return None
    stamp = str(fields[30] or "")
    date = (f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}"
            if len(stamp) >= 8 and stamp[:8].isdigit() else None)
    volume = _f(fields[36]) if _f(fields[36]) is not None else _f(fields[6])
    return {
        "key": key,
        "name": str(fields[1] or ""),
        "date": date,
        "time": stamp,
        "price": price,
        "prev_close": _f(fields[4]),
        "open": _f(fields[5]),
        "high": _f(fields[33]),
        "low": _f(fields[34]),
        "volume": volume,
        "amount": _f(fields[37]),
        "turnover": _f(fields[38]),
        "chg": _f(fields[31]),
        "chg_pct": _f(fields[32]),
        "pe": _f(fields[39]),
        "pb": _f(fields[46]),
        "mktcap": _f(fields[45]),
        "float_mktcap": _f(fields[44]),
        "vol_ratio": _f(fields[49]),
    }


def fetch_quotes(keys, batch=QUOTE_BATCH):
    """批量取腾讯实时行情 -> {key: record}。

    行情接口不做磁盘缓存（只 3 个请求，必须实时），失败的分批静默跳过。
    """
    keys = [k for k in dict.fromkeys(keys or []) if k]
    out = {}
    for i in range(0, len(keys), max(1, int(batch))):
        part = keys[i:i + max(1, int(batch))]
        url = TX_QUOTE_URL.format(codes=",".join(part))
        try:
            text = http_get(url, encoding="gb18030", retries=2)
        except DataError:
            continue
        for key, fields in _quote_fields(text).items():
            rec = _quote_record(key, fields)
            if rec and rec.get("date"):
                out[key] = rec
    return out


def _quote_bar(quote):
    """实时行情 -> 一根 bar（缺 OHLC 时返回 None）。"""
    if not quote:
        return None
    date = quote.get("date")
    open_ = quote.get("open")
    close = quote.get("price")
    high = quote.get("high")
    low = quote.get("low")
    if not date or None in (open_, close, high, low):
        return None
    if min(open_, close, high, low) <= 0:
        return None
    return {
        "date": date,
        "open": open_,
        "close": close,
        "high": high,
        "low": low,
        "volume": quote.get("volume"),
        "turnover": quote.get("turnover"),
        "amount": quote.get("amount"),
    }


# 收盘后才允许用实时行情补「当日 bar」：盘中（时间是 09:30~15:00）的行情只是半日数据，
# 拿它当收盘 bar 会让 K 线/均线/量比全部失真（用户中午点「更新」时尤其明显）。
QUOTE_BAR_EARLIEST_TIME = "150000"  # 15:00:00 收盘集合竞价结束


def _quote_is_closed(quote):
    """行情时间戳是否已经收盘（YYYYMMDDHHMMSS，早于 15:00 视为盘中半日数据）。"""
    stamp = str((quote or {}).get("time") or "")
    hhmmss = stamp[8:14]
    return len(hhmmss) == 6 and hhmmss.isdigit() and hhmmss >= QUOTE_BAR_EARLIEST_TIME


def merge_quote_bar(doc, quote):
    """把实时行情当日 bar 追加到 bars doc。

    只在「新交易日 + 已收盘」时追加；重复调用无副作用。盘中不补 bar（见上方说明）。
    """
    if not _quote_is_closed(quote):
        return False
    bar = _quote_bar(quote)
    if not bar:
        return False
    dates = doc.get("date") or []
    if dates and bar["date"] <= dates[-1]:
        return False
    for key in ("date", "open", "close", "high", "low", "volume", "turnover", "amount"):
        doc.setdefault(key, []).append(bar[key])
    doc["end"] = doc["date"][-1]
    doc["quote_bar"] = True
    return True


# --------------------------------------------------------------------------- #
# 分红送转事件解析
# --------------------------------------------------------------------------- #
_FH_RE = re.compile(r"(送|转增|转|派)\s*(\d+(?:\.\d+)?)")


def _parse_fh_content(text):
    """'10送4.0059股, 10派4.3565元' -> (dps=0.43565 每股派息, bonus=0.40059 每股送转)。"""
    text = text or ""
    dps = 0.0
    bonus = 0.0
    found = False
    for match in _FH_RE.finditer(text):
        kind, raw = match.group(1), float(match.group(2))
        found = True
        if kind == "派":
            dps += raw
        else:
            bonus += raw
    if not found:
        return None, None
    return round(dps / 10.0, 10), round(bonus / 10.0, 10)


def parse_tencent_events(rows):
    """从腾讯 raw 行第 6 列的事件字典重建分红送转事件。

    返回 (events, raw_events)：
      events:    {生效日: {"dps","bonus","content"}}，生效日 = 首个 >= 除权日的交易日
      raw_events: 原始事件明细（含 row_date / cqr / content），供回退变体使用
    """
    dates = [str(r[0])[:10] for r in rows or [] if isinstance(r, (list, tuple)) and r]
    if not dates:
        return {}, []
    first, last = dates[0], dates[-1]
    events = {}
    raw_events = []
    seen = set()
    for row in rows or []:
        if not isinstance(row, (list, tuple)) or len(row) < 7:
            continue
        info = row[6]
        if not isinstance(info, dict) or not info:
            continue
        content = str(info.get("FHcontent") or "")
        cqr = str(info.get("cqr") or "")[:10]
        row_date = str(row[0])[:10]
        dps, bonus = _parse_fh_content(content)
        if dps is None:
            fh = _f(info.get("fh_sh"))
            if fh:
                dps, bonus = fh / 10.0, 0.0
        if dps is None:
            continue
        if not dps and not bonus:
            continue
        dedupe_key = (cqr, row_date, round(dps, 10), round(bonus, 10))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        # 生效日：首个 >= 除权日的交易日（停牌时除权日不交易，顺延到复牌日）
        if cqr and cqr > first:
            eff = _first_ge(dates, cqr)
            if eff is None:              # 除权日在序列结束之后：不影响历史，跳过
                continue
        elif cqr and cqr <= first:
            # 除权日早于序列起点：除非该事件就落在首行（首行为除权日，无更早行受影响），否则无视
            if row_date != first:
                continue
            eff = first
        else:
            eff = row_date if row_date >= first else None
            if eff is None:
                continue
        item = events.setdefault(eff, {"dps": 0.0, "bonus": 0.0, "content": content})
        item["dps"] += dps
        item["bonus"] += bonus
        raw_events.append({"row_date": row_date, "cqr": cqr, "content": content,
                           "dps": dps, "bonus": bonus, "eff": eff})
    return events, raw_events


def em_dividends(code, refresh=False):
    """东财 RPT_SHAREBONUS_DET -> {除权日: {"dps","bonus"}}（每股口径）。仅作交叉校验/回退。"""
    code = str(code).strip().zfill(6)
    rows = _em_query("RPT_SHAREBONUS_DET", 100, "EX_DIVIDEND_DATE", -1,
                     f'(SECURITY_CODE="{code}")', f"em_div_{code}.json", refresh=refresh)
    events = {}
    for row in rows:
        progress = str(row.get("ASSIGN_PROGRESS") or "")
        ex_date = str(row.get("EX_DIVIDEND_DATE") or "")[:10]
        if len(ex_date) != 10:
            continue
        if progress and "实施" not in progress:
            continue
        dps = _f(row.get("PRETAX_BONUS_RMB")) or 0.0
        bonus = _f(row.get("BONUS_IT_RATIO"))
        if bonus is None:
            bonus = (_f(row.get("BONUS_RATIO")) or 0.0) + (_f(row.get("IT_RATIO")) or 0.0)
        if not dps and not bonus:
            continue
        events.setdefault(ex_date, []).append((round(dps / 10.0, 10), round(bonus / 10.0, 10)))
    out = {}
    for ex_date, pairs in events.items():
        # 同日多行（分红/送转分开披露）取覆盖度最大的一组，避免重复累计
        dps, bonus = max(pairs, key=lambda p: (p[0] + p[1], p[0]))
        out[ex_date] = {"dps": dps, "bonus": bonus}
    return out


# --------------------------------------------------------------------------- #
# 复权因子（等差口径）
# --------------------------------------------------------------------------- #
def _first_ge(dates, target):
    idx = bisect_left(dates, target)
    return dates[idx] if idx < len(dates) else None


def _map_events(events_raw, dates):
    """把 {除权日: event} 映射到 {首个>=除权日的交易日: event}（只保留序列内、起点之后的）。"""
    out = {}
    for ex_date, event in (events_raw or {}).items():
        if not ex_date or ex_date <= dates[0]:
            continue
        eff = _first_ge(dates, ex_date)
        if eff is None:
            continue
        slot = out.setdefault(eff, {"dps": 0.0, "bonus": 0.0})
        slot["dps"] += event["dps"]
        slot["bonus"] += event["bonus"]
    return out


def _additive_factors(dates, closes, events):
    """按腾讯等差口径计算每日累计因子：f(d) = 链式复权价 / 原始价。"""
    ordered = sorted(events.items())
    factors = []
    for date, close in zip(dates, closes):
        if close is None or close <= 0:
            factors.append(None)
            continue
        price = close
        for eff, event in ordered:
            if eff > date:
                price = (price - event["dps"]) / (1.0 + event["bonus"])
        factors.append(price / close if price > 0 else None)
    return factors


def _segments(dates, factors, last_event_date):
    """压成 segments：事件有效期前逐日给因子，之后收成单条 (首日, 1.0)。"""
    if not last_event_date:
        return [[dates[0], 1.0]]
    segs = []
    for date, factor in zip(dates, factors):
        if date <= last_event_date:
            value = 1.0 if factor is None else round(factor, 6)
            segs.append([date, value])
        else:
            segs.append([date, 1.0])
            break
    if not segs:
        segs = [[dates[0], 1.0]]
    return segs


_seg_cache = {}


def _seg_factor(segments, date):
    """取 date 所在段（生效日 <= date 的最后一段）的累计因子（带日期索引缓存，O(log n)）。"""
    cache_key = id(segments)
    entry = _seg_cache.get(cache_key)
    if entry is None or entry[0] is not segments:
        entry = (segments, [seg[0] for seg in segments])
        _seg_cache[cache_key] = entry
    seg_dates = entry[1]
    idx = bisect_right(seg_dates, date) - 1
    if idx < 0:
        return None
    return segments[idx][1]


def _overlap_errs(raw_dates, raw_closes, factors, qfq):
    adj_map = {}
    for date, close, factor in zip(raw_dates, raw_closes, factors):
        if factor is None or not close:
            continue
        adj_map[date] = close * factor
    errs = []
    for date, close in zip(qfq["date"], qfq["close"]):
        adj = adj_map.get(date)
        if adj is None or not close or close <= 0:
            continue
        errs.append(abs(adj - close) / close)
    return errs


def _p99(errs):
    if not errs:
        return None
    ordered = sorted(errs)
    idx = int(round(0.99 * (len(ordered) - 1)))
    return ordered[min(idx, len(ordered) - 1)]


def _ratio_at_event(dates, closes, eff):
    idx = bisect_left(dates, eff)
    if idx <= 0:
        return None
    prev = closes[idx - 1]
    if not prev or prev <= 0:
        return None
    return prev


def _items_from_events(dates, closes, events):
    items = []
    for eff in sorted(events):
        event = events[eff]
        prev = _ratio_at_event(dates, closes, eff)
        ratio = None
        if prev:
            ratio = round((prev - event["dps"]) / (prev * (1.0 + event["bonus"])), 6)
        items.append({
            "date": eff,
            "ratio": ratio,
            "dps": round(event["dps"], 6),
            "bonus": round(event["bonus"], 6),
        })
    return items


def build_adj(code, key=None, name=None, refresh=False, write=True):
    """data/adj/{code}.json —— 前复权累计因子 + 事件明细。

    主口径：腾讯 raw 行内事件字典；回退变体：事件行日期 / 东财分红 / 二者并集，
    取与腾讯真前复权重叠区间 P99 误差最小者；P99 >= 0.5% 标 valid=False 并剔除。
    """
    code = str(code).strip().zfill(6)
    key = key or symbol_of(code)
    warns = []
    raw_rows = tx_kline(key, 2000, "", refresh=refresh)
    raw = parse_tx_rows(raw_rows)
    if not raw["date"]:
        raise DataError(f"长历史为空 {code}")
    qfq_rows = tx_kline(key, 640, "qfq", refresh=refresh)
    qfq = parse_tx_rows(qfq_rows)
    if not qfq["date"]:
        raise DataError(f"前复权为空 {code}")

    dates, closes = raw["date"], raw["close"]
    events_tx, raw_events = parse_tencent_events(raw_rows)

    candidates = [("tencent", events_tx)]
    if raw_events:
        by_rowdate = {}
        for ev in raw_events:
            eff = ev["row_date"]
            if eff < dates[0] or eff > dates[-1]:
                continue
            slot = by_rowdate.setdefault(eff, {"dps": 0.0, "bonus": 0.0})
            slot["dps"] += ev["dps"]
            slot["bonus"] += ev["bonus"]
        if by_rowdate:
            candidates.append(("tencent_rowdate", by_rowdate))

    em_events = None
    try:
        em_raw = em_dividends(code, refresh=refresh)
        em_events = _map_events(em_raw, dates)
    except Exception as exc:                       # noqa: BLE001 - 交叉校验可失败
        warns.append(f"{code} 东财分红获取失败：{exc}")
    if em_events is not None and em_events:
        candidates.append(("em", em_events))
        merged = {eff: dict(event) for eff, event in events_tx.items()}
        for eff, event in em_events.items():
            if eff in merged:
                merged[eff]["dps"] = merged[eff].get("dps", 0.0)
                continue
            merged[eff] = dict(event)
        candidates.append(("tencent+em", merged))

    best = None
    for source, events in candidates:
        factors = _additive_factors(dates, closes, events)
        errs = _overlap_errs(dates, closes, factors, qfq)
        p99 = _p99(errs)
        score = p99 if p99 is not None else 9.99
        if best is None or score < best["p99"] - 1e-12:
            best = {"source": source, "events": events, "factors": factors,
                    "errs": errs, "p99": p99}

    # ---- 以腾讯真前复权为基准校准最终因子 ------------------------------- #
    # 重叠区间（腾讯 qfq 640 行 ∩ 长历史）逐日 factor = qfq/raw，精确对齐腾讯；
    # 重叠起点之前用事件链式模型外推，并按边界比例缩放保证曲线连续。
    # model_p99 记录"纯事件链模型"相对腾讯的偏差，用于判定前置区间是否可信。
    qfq_map = {d: c for d, c in zip(qfq["date"], qfq["close"])}
    common = [d for d in dates if d in qfq_map]
    model_factors = best["factors"] if best else [1.0] * len(dates)
    model_p99 = best["p99"] if best else None
    pre_overlap_days = 0
    factor_map = {}
    if common:
        first_common = common[0]
        idx_first = dates.index(first_common)
        pre_overlap_days = idx_first
        ref_model = model_factors[idx_first] or 1.0
        ref_tencent = qfq_map[first_common] / closes[idx_first]
        scale = ref_tencent / ref_model
        for i, date in enumerate(dates):
            if date < first_common:
                base = model_factors[i]
                factor_map[date] = None if base is None else base * scale
            else:
                q = qfq_map.get(date)
                factor_map[date] = (q / closes[i]) if (q is not None and closes[i]) else None
    else:
        for date, factor in zip(dates, model_factors):
            factor_map[date] = factor
    calibrated = bool(model_p99 is not None and model_p99 >= ADJ_P99_MAX)
    tail = common[-1] if common else (max(best["events"]) if best and best["events"] else None)
    segments = _segments(dates, [factor_map.get(d) for d in dates], tail)
    final_factors = [_seg_factor(segments, date) for date in dates]
    final_errs = _overlap_errs(dates, closes, final_factors, qfq)
    p99_final = _p99(final_errs)
    recon_ok = (pre_overlap_days == 0) or (model_p99 is not None and model_p99 < ADJ_MODEL_P99_MAX)
    valid = bool(p99_final is not None and p99_final < ADJ_P99_MAX
                 and len(final_errs) >= ADJ_MIN_OVERLAP and recon_ok)
    items = _items_from_events(dates, closes, best["events"]) if best else []

    # 与东财口径交叉校验（仅记录 warning）
    if em_events:
        tx_by_cqr = {ev["cqr"] for ev in raw_events if ev.get("cqr")}
        for ex_date, em_ev in sorted(em_raw.items()):
            if ex_date <= dates[0] or ex_date > dates[-1]:
                continue
            tx_match = None
            for ev in raw_events:
                if ev.get("cqr") == ex_date:
                    tx_match = ev
                    break
            if tx_match is None:
                warns.append(f"{code} 东财分红 {ex_date} 在腾讯事件中缺失（dps={em_ev['dps']}）")
            elif abs(tx_match["dps"] - em_ev["dps"]) > 0.0055 or abs(tx_match["bonus"] - em_ev["bonus"]) > 0.0055:
                warns.append(f"{code} 分红口径不一致 {ex_date}: 腾讯 dps={tx_match['dps']:.4f} bonus={tx_match['bonus']:.4f} / 东财 dps={em_ev['dps']:.4f} bonus={em_ev['bonus']:.4f}")
        for cqr in sorted(tx_by_cqr):
            if cqr <= dates[0] or cqr > dates[-1] or cqr not in em_raw:
                warns.append(f"{code} 腾讯事件 {cqr} 在东财分红表中缺失")
    if best and best["source"] != "tencent":
        warns.append(f"{code} 复权事件回退变体={best['source']}")
    if any("配" in (ev.get("content") or "") for ev in raw_events):
        warns.append(f"{code} 存在配股事件，等差口径可能不准")
    if calibrated:
        warns.append(f"{code} 腾讯 qfq 与事件链模型偏差 model_p99={model_p99:.4f}，已按腾讯真前复权校准（前置 {pre_overlap_days} 天用事件外推）")
    if not valid:
        if len(final_errs) < ADJ_MIN_OVERLAP:
            reason = "重叠样本不足"
        elif not recon_ok:
            reason = "事件模型无法解释腾讯前复权(model_p99>=2%)"
        else:
            reason = "自检未通过"
        warns.append(f"{code} 复权自检失败 {reason} P99={p99_final} model_p99={model_p99} -> valid=false 剔除")

    doc = {
        "code": code,
        "basis_date": qfq["date"][-1],
        "items": items,
        "segments": segments,
        "valid": valid,
        "p99_err": None if p99_final is None else round(p99_final, 6),
        "model_p99": None if model_p99 is None else round(model_p99, 6),
        "pre_overlap_days": pre_overlap_days,
        "calibrated": calibrated,
        "n_overlap": len(final_errs),
        "events_source": best["source"] if best else None,
        "warnings": warns,
    }
    if write:
        _write_json(os.path.join(ADJ, f"{code}.json"), doc)
    return doc


# --------------------------------------------------------------------------- #
# 东财估值 / 财务
# --------------------------------------------------------------------------- #
def _em_query(report, page_size, sort_col, sort_type, flt, cache_name,
              refresh=False, pages=1):
    """东财 datacenter-web 查询，返回 result.data 列表。"""
    out = []
    for page in range(1, max(1, pages) + 1):
        params = [
            ("reportName", report),
            ("columns", "ALL"),
            ("pageSize", str(page_size)),
            ("pageNumber", str(page)),
            ("sortColumns", sort_col),
            ("sortTypes", str(sort_type)),
            ("filter", flt),
            ("source", "WEB"),
            ("client", "WEB"),
            ("_", str(int(time.time() * 1000))),
        ]
        url = EM_URL + "?" + urllib.parse.urlencode(params)
        name = cache_name if page == 1 else cache_name.replace(".json", f"_p{page}.json")
        text = cached_text(name, url, headers=EM_HEADERS, refresh=refresh, ttl=EM_TTL)
        payload = _json(text)
        data = ((payload.get("result") or {}).get("data")) or []
        out.extend(data)
        if len(data) < page_size:
            break
    return out


def build_val(code, refresh=False, write=True):
    """data/val/{code}.json —— 东财估值序列（约 1400 行，2021 起）。"""
    code = str(code).strip().zfill(6)
    secucode = secucode_of(code)
    rows = _em_query("RPT_VALUEANALYSIS_DET", 1400, "TRADE_DATE", -1,
                     f'(SECUCODE="{secucode}")', f"em_val_{code}.json",
                     refresh=refresh, pages=5)
    recs = {}
    for row in rows:
        date = str(row.get("TRADE_DATE") or "")[:10]
        if len(date) != 10:
            continue
        recs[date] = {
            "close": _f(row.get("CLOSE_PRICE")),
            "pe_ttm": _f(row.get("PE_TTM")),
            "pb": _f(row.get("PB_MRQ")),
            "peg": _f(row.get("PEG_CAR")),
            "total_mktcap": _f(row.get("TOTAL_MARKET_CAP")),
            "free_shares": _f(row.get("FREE_SHARES_A")),
        }
    if not recs:
        raise DataError(f"估值序列为空 {code}")
    dates = sorted(recs)
    doc = {"code": code, "secucode": secucode, "date": dates}
    for field in ("close", "pe_ttm", "pb", "peg", "total_mktcap", "free_shares"):
        doc[field] = [recs[d][field] for d in dates]
    if write:
        _write_json(os.path.join(VAL, f"{code}.json"), doc)
    return doc


def build_fin(code, refresh=False, write=True):
    """data/fin/{code}.json —— 东财 F10 主要财务指标（最新一期 + 近 5 年年报）。"""
    code = str(code).strip().zfill(6)
    secucode = secucode_of(code)
    rows = _em_query("RPT_F10_FINANCE_MAINFINADATA", 40, "REPORT_DATE", -1,
                     f'(SECUCODE="{secucode}")', f"em_fin_{code}.json", refresh=refresh)
    recs = []
    for row in rows:
        date = str(row.get("REPORT_DATE") or "")[:10]
        if len(date) != 10:
            continue
        recs.append({
            "report_date": date,
            "roe": _f(row.get("ROEJQ")),
            "gross": _f(row.get("XSMLL")),
            "debt": _f(row.get("ZCFZL")),
            "net_profit": _f(row.get("PARENTNETPROFIT")),
            "rev_yoy": _f(row.get("TOTALOPERATEREVETZ")),
        })
    if not recs:
        raise DataError(f"财务指标为空 {code}")
    recs.sort(key=lambda r: r["report_date"], reverse=True)
    latest = {"report_date": recs[0]["report_date"]}
    for field in ("roe", "gross", "debt", "net_profit", "rev_yoy"):
        latest[field] = recs[0][field]
    annual = []
    for rec in recs:
        if rec["report_date"].endswith("12-31"):
            annual.append({
                "year": int(rec["report_date"][:4]),
                "roe": rec["roe"],
                "gross": rec["gross"],
                "debt": rec["debt"],
                "net_profit": rec["net_profit"],
                "rev_yoy": rec["rev_yoy"],
            })
        if len(annual) >= 5:
            break
    doc = {"code": code, "secucode": secucode, "latest": latest, "annual": annual}
    if write:
        _write_json(os.path.join(FIN, f"{code}.json"), doc)
    return doc


# --------------------------------------------------------------------------- #
# 新浪全 A 快照 + 新浪行业
# --------------------------------------------------------------------------- #
def _sina_count(node, refresh=False):
    url = SINA_COUNT_URL.format(node=node)
    text = cached_text(f"sina_count_{node}.txt", url, headers=SINA_HEADERS,
                       refresh=refresh, encoding="gb18030", ttl=SINA_TTL)
    nums = re.findall(r"\d+", text or "")
    return int(nums[0]) if nums else 0


def _sina_page(page, node, refresh=False, cache_prefix="sina_hs_a"):
    url = SINA_HQ_URL.format(page=page, num=100, node=node)
    text = cached_text(f"{cache_prefix}_{node}_{page}.json", url, headers=SINA_HEADERS,
                       refresh=refresh, encoding="gb18030", ttl=SINA_TTL)
    text = (text or "").strip()
    if not text or text in ("null", "[]"):
        return []
    try:
        payload = json.loads(text)
    except ValueError:
        return []
    return payload if isinstance(payload, list) else []


def _sina_industry_map(refresh=False, workers=8, logger=print):
    """newSinaHy.php 行业清单 + 各行业成分页 -> {symbol: 行业名}。"""
    text = cached_text("sina_hy.txt", SINA_HY_URL, headers=SINA_HEADERS,
                       refresh=refresh, encoding="gb18030", ttl=SINA_TTL)
    text = text or ""
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise DataError("newSinaHy.php 解析失败（无 JSON 对象）")
    payload = json.loads(text[start:end + 1])
    nodes = {}
    for node_code, csv in payload.items():
        parts = str(csv).split(",")
        if len(parts) >= 2 and parts[1].strip():
            nodes[node_code] = parts[1].strip()
    if not nodes:
        raise DataError("newSinaHy.php 行业节点为空")
    logger(f"  [snapshot] 新浪行业节点 {len(nodes)} 个")

    def _count_task(item):
        node_code = item[0]
        try:
            return _sina_count(node_code, refresh=refresh)
        except Exception:                          # noqa: BLE001
            return 0

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        counts = list(pool.map(_count_task, list(nodes.items())))

    page_tasks = []
    for (node_code, name), total in zip(nodes.items(), counts):
        for page in range(1, min(max((total + 99) // 100, 1), 30) + 1):
            page_tasks.append((node_code, name, page))

    def _page_task(task):
        node_code, name, page = task
        try:
            return name, _sina_page(page, node_code, refresh=refresh,
                                    cache_prefix="sina_node")
        except Exception:                          # noqa: BLE001
            return name, []

    mapping = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for name, rows in pool.map(_page_task, page_tasks):
            for row in rows:
                symbol = str(row.get("symbol") or "")
                if symbol and symbol not in mapping:
                    mapping[symbol] = name
    logger(f"  [snapshot] 行业映射覆盖 {len(mapping)} 只")
    return mapping


def build_snapshot(trading_day=None, refresh=False, workers=8, logger=print, write=True):
    """data/snapshot.json —— 新浪全 A 当日快照（含行业映射）。amount 单位=元。"""
    total = _sina_count("hs_a", refresh=refresh)
    if total <= 0:
        raise DataError("新浪 hs_a 股票计数失败")
    pages = min(max((total + 99) // 100, 1), 120)
    logger(f"  [snapshot] hs_a 共 {total} 只，分 {pages} 页")

    def _page_task(page):
        try:
            return _sina_page(page, "hs_a", refresh=refresh)
        except Exception as exc:                   # noqa: BLE001
            raise DataError(f"snapshot page {page} 失败: {exc}") from None

    stocks = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for rows in pool.map(_page_task, range(1, pages + 1)):
            stocks.extend(rows)
    if not stocks:
        raise DataError("新浪快照为空")

    industry = _sina_industry_map(refresh=refresh, workers=workers, logger=logger)
    out = []
    seen = set()
    for row in stocks:
        symbol = str(row.get("symbol") or "")
        code = str(row.get("code") or "").strip()
        if not symbol or not code:
            continue
        if symbol in seen:
            continue
        seen.add(symbol)
        pe = _f(row.get("per"))
        pb = _f(row.get("pb"))
        mktcap = _f(row.get("mktcap"))
        nmc = _f(row.get("nmc"))
        out.append({
            "code": code.zfill(6),
            "symbol": symbol,
            "name": str(row.get("name") or "").strip(),
            "price": _f(row.get("trade")),
            "chg_pct": _f(row.get("changepercent")),
            "amount": _f(row.get("amount")),
            "turnover": _f(row.get("turnoverratio")),
            "pe": pe if pe else None,
            "pb": pb if pb else None,
            "mktcap_yi": round(mktcap / 10000.0, 4) if mktcap is not None else None,
            "nmc_yi": round(nmc / 10000.0, 4) if nmc is not None else None,
            "industry": industry.get(symbol),
        })
    date = trading_day or _trading_day_from_bars() or bj_now().strftime("%Y-%m-%d")
    doc = {"date": date, "rows": out}
    if write:
        _write_json(SNAPSHOT, doc)
    logger(f"  [snapshot] 写入 {len(out)} 行，行业缺失 {sum(1 for r in out if not r['industry'])} 行")
    return doc


def _trading_day_from_bars():
    for name in ("sh000300.json", "sh600183.json", "sh510300.json"):
        path = os.path.join(BARS, name)
        if os.path.exists(path):
            try:
                doc = _load_json(path)
                if doc.get("end"):
                    return doc["end"]
            except (OSError, ValueError):
                continue
    return None


# --------------------------------------------------------------------------- #
# 组合池
# --------------------------------------------------------------------------- #
def _pool_doc(updated=None):
    src_path = POOL_SOURCE
    if not os.path.exists(src_path):
        raise DataError(f"缺少池来源文件 {src_path}")
    src = _load_json(src_path)
    sections = src.get("sections") or {}
    raw_pool = sections.get("pool") or []
    if not raw_pool:
        raise DataError("pool_source.json 中 sections.pool 为空")

    updated = updated or src.get("snapshot_date") or bj_now().strftime("%Y-%m-%d")
    stocks = []
    seen = set()
    for item in raw_pool:
        code = str(item.get("code") or "").strip().zfill(6)
        if len(code) != 6 or code in seen:
            continue
        seen.add(code)
        sector = str(item.get("sector") or "other").strip().lower()
        if sector not in SECTORS:
            sector = "other"
        symbol = symbol_of(code)
        stocks.append({
            "code": code,
            "key": symbol,
            "name": str(item.get("name") or "").strip(),
            "sector": sector,
            "index_symbol": symbol,
            "is_etf": False,
            "kind": "stock",
        })
    etfs = []
    for code, name, role in ETF_DEFAULT:
        symbol = symbol_of(code)
        etfs.append({
            "code": code,
            "key": symbol,
            "name": name,
            "role": role,
            "index_symbol": symbol,
            "is_etf": True,
            "kind": "etf",
        })
    indices = []
    for code, name, symbol in INDEX_DEFAULT:
        indices.append({
            "code": code,
            "key": symbol,
            "name": name,
            "symbol": symbol,
            "index_symbol": symbol,
            "role": "market",
            "is_etf": False,
            "kind": "index",
        })
    return {"updated": updated, "stocks": stocks, "etfs": etfs, "indices": indices}


def build_pool(write=True, updated=None):
    """data/pool.json —— 154 只精选池 + 7 ETF + 3 指数（DESIGN §2.1，附加 key/is_etf/kind）。"""
    doc = _pool_doc(updated)
    if write:
        _write_json(POOL, doc)
    return doc


# --------------------------------------------------------------------------- #
# 并行构建
# --------------------------------------------------------------------------- #
def _call(fn, task):
    try:
        return True, fn(task)
    except Exception as exc:                       # noqa: BLE001 - 单任务失败不中断
        return False, f"{task!r}: {type(exc).__name__}: {exc}"


def _parallel(tasks, fn, workers, label, warnings, logger):
    results = []
    if not tasks:
        return 0, 0, results
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        futures = {pool.submit(_call, fn, task): task for task in tasks}
        done = 0
        for future in as_completed(futures):
            done += 1
            ok, payload = future.result()
            results.append((ok, payload))
            if not ok:
                warnings.append(f"{label}: {payload}")
            if done % 50 == 0 or done == len(tasks):
                logger(f"  [{label}] {done}/{len(tasks)}")
    ok_count = sum(1 for ok, _ in results if ok)
    return ok_count, len(results) - ok_count, results


def holdings_extra():
    """data/pool_source.json 里的持仓（去重后 5 只）：不在 154 精选池内，但 run_daily 估值需要日线。"""
    path = os.path.join(DATA, "pool_source.json")
    if not os.path.exists(path):
        return []
    try:
        doc = _load_json(path)
    except Exception:                              # noqa: BLE001
        return []
    rows = ((doc.get("sections") or {}).get("holdings") or [])
    out, seen = [], set()
    for row in rows:
        code = str(row.get("code") or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        out.append({"code": code, "name": row.get("name") or code,
                    "key": symbol_of(code)})
    return out


def build_all(limit=None, skip_long=False, skip_snapshot=False, skip_adj=False,
              refresh=False, workers=8, logger=print):
    """一条命令的完整构建：pool -> bars -> bars_long -> val/fin -> adj -> snapshot -> meta。"""
    started = time.time()
    warnings = []
    excluded = []
    for path in (BARS, BARS_LONG, ADJ, VAL, FIN):
        os.makedirs(path, exist_ok=True)
    os.makedirs(CACHE, exist_ok=True)

    pool = build_pool(write=False)
    stocks = pool["stocks"]
    etfs = pool["etfs"]
    indices = pool["indices"]
    if limit:
        stocks = stocks[: int(limit)]
    counts = {"stocks": len(stocks), "etfs": len(etfs), "indices": len(indices),
              "limit": int(limit) if limit else None}
    logger(f"[1/6] 池：stocks={len(stocks)} etfs={len(etfs)} indices={len(indices)}")

    bar_tasks = ([(s["key"], s["name"], "stock") for s in stocks]
                 + [(e["key"], e["name"], "etf") for e in etfs]
                 + [(i["key"], i["name"], "index") for i in indices])

    # 收盘后腾讯 640 行 K 线接口经常还停在前一交易日，用实时行情把「当日 bar」补上。
    quote_map = {}
    try:
        quote_map = fetch_quotes([t[0] for t in bar_tasks])
    except Exception as exc:                          # noqa: BLE001 - 行情失败不阻断
        warnings.append(f"quotes: {type(exc).__name__}: {exc}")
    logger(f"[1.5/6] 实时行情 {len(quote_map)}/{len(bar_tasks)} 只（用于补当日 bar）")

    ok, fail, bar_results = _parallel(
        bar_tasks,
        lambda t: build_bars(t[0], t[1], t[2], refresh=refresh, quotes=quote_map),
        workers, "bars", warnings, logger)
    counts["bars"] = ok
    counts["bars_fail"] = fail
    counts["quote_bars"] = sum(
        1 for ok_flag, doc in bar_results
        if ok_flag and isinstance(doc, dict) and doc.get("quote_bar"))
    logger(f"[1.5/6] 当日 bar 已补 {counts['quote_bars']} 只"
           f"（K 线接口自行带出当日的则无需补）")

    if skip_long:
        counts["bars_long"] = 0
        logger("[2/6] bars_long：跳过（--skip-long）")
    else:
        ok, fail, _ = _parallel(bar_tasks,
                                lambda t: build_bars_long(t[0], t[1], t[2], refresh=refresh),
                                workers, "bars_long", warnings, logger)
        counts["bars_long"] = ok
        counts["bars_long_fail"] = fail

    trading_day = _trading_day_from_bars() or bj_now().strftime("%Y-%m-%d")
    pool["updated"] = trading_day
    _write_json(POOL, pool)
    logger(f"[3/6] trading_day={trading_day}；pool.json 已写 ({len(pool['stocks'])} 股/{len(pool['etfs'])} ETF/{len(pool['indices'])} 指数)")

    def _fund_worker(stock):
        code = stock["code"]
        out = {"code": code, "val": False, "fin": False, "err": []}
        try:
            build_val(code, refresh=refresh)
            out["val"] = True
        except Exception as exc:                   # noqa: BLE001
            out["err"].append(f"val: {type(exc).__name__}: {exc}")
        try:
            build_fin(code, refresh=refresh)
            out["fin"] = True
        except Exception as exc:                   # noqa: BLE001
            out["err"].append(f"fin: {type(exc).__name__}: {exc}")
        return out

    fund_ok = 0
    val_ok = fin_ok = 0
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as executor:
        futures = [executor.submit(_fund_worker, stock) for stock in stocks]
        for done, future in enumerate(as_completed(futures), 1):
            res = future.result()
            val_ok += 1 if res["val"] else 0
            fin_ok += 1 if res["fin"] else 0
            for msg in res["err"]:
                warnings.append(f"{res['code']} {msg}")
            if done % 25 == 0 or done == len(stocks):
                logger(f"  [val+fin] {done}/{len(stocks)} val_ok={val_ok} fin_ok={fin_ok}")
    counts["val"] = val_ok
    counts["fin"] = fin_ok
    logger(f"[4/6] val={val_ok}/{len(stocks)} fin={fin_ok}/{len(stocks)}")

    if skip_adj:
        counts["adj"] = 0
        counts["adj_valid"] = 0
        counts["adj_invalid"] = 0
        logger("[5/6] adj：跳过（--skip-adj）")
    else:
        def _adj_worker(stock):
            return build_adj(stock["code"], key=stock["key"], name=stock["name"],
                             refresh=refresh)

        ok, fail, results = _parallel([s for s in stocks], _adj_worker,
                                      workers, "adj", warnings, logger)
        valid_count = 0
        for ok_flag, payload in results:
            if not ok_flag:
                continue
            if payload.get("valid"):
                valid_count += 1
            else:
                excluded.append({
                    "code": payload["code"],
                    "p99_err": payload.get("p99_err"),
                    "model_p99": payload.get("model_p99"),
                    "reason": "事件模型无法解释腾讯前复权(model_p99>=2%) 或自检未通过",
                })
            for msg in payload.get("warnings") or []:
                if any(tag in msg for tag in ("自检失败", "校准", "回退变体", "配股", "口径不一致")):
                    warnings.append(msg)
        counts["adj"] = ok
        counts["adj_valid"] = valid_count
        counts["adj_invalid"] = len(excluded)
        logger(f"[5/6] adj={ok}/{len(stocks)} valid={valid_count} invalid={len(excluded)}")

    # 持仓（池外 5 只）补充数据：run_daily 的组合估值/持仓打分会用 load_bars(code)，
    # 它们不在 154 精选池里，故单独补齐 bars/bars_long/val/fin/adj（不计入池统计）。
    extras = holdings_extra()
    extra_quotes = fetch_quotes([item["key"] for item in extras]) if extras else {}
    extras_ok = 0
    for item in extras:
        try:
            build_bars(item["key"], item["name"], "stock", refresh=refresh,
                       quotes=extra_quotes)
            if not skip_long:
                build_bars_long(item["key"], item["name"], "stock", refresh=refresh)
            build_val(item["code"], refresh=refresh)
            build_fin(item["code"], refresh=refresh)
            if not skip_adj:
                build_adj(item["code"], key=item["key"], name=item["name"], refresh=refresh)
            extras_ok += 1
        except Exception as exc:                   # noqa: BLE001
            warnings.append(f"持仓 {item['code']} {type(exc).__name__}: {exc}")
    counts["holdings_extra"] = extras_ok
    logger(f"[5.5/6] 持仓补充：{extras_ok}/{len(extras)}")

    if skip_snapshot:
        counts["snapshot_rows"] = 0
        counts["snapshot_pages"] = 0
        logger("[6/6] snapshot：跳过（--skip-snapshot）")
    else:
        try:
            snap = build_snapshot(trading_day=trading_day, refresh=refresh,
                                  workers=workers, logger=logger)
            counts["snapshot_rows"] = len(snap["rows"])
            counts["snapshot_pages"] = min(120, max(1, (len(snap["rows"]) + 99) // 100))
        except Exception as exc:                   # noqa: BLE001
            counts["snapshot_rows"] = 0
            counts["snapshot_pages"] = 0
            warnings.append(f"snapshot: {type(exc).__name__}: {exc}")
            logger(f"[6/6] snapshot 失败：{exc}")

    counts["warnings"] = len(warnings)
    counts["excluded"] = len(excluded)
    meta = {
        "built_at": bj_now().strftime("%Y-%m-%dT%H:%M:%S") + "+08:00",
        "trading_day": trading_day,
        "counts": counts,
        "warnings": warnings[:500],
        "excluded": excluded,
        "options": {
            "limit": int(limit) if limit else None,
            "skip_long": bool(skip_long),
            "skip_adj": bool(skip_adj),
            "skip_snapshot": bool(skip_snapshot),
            "refresh": bool(refresh),
            "workers": int(workers),
        },
        "duration_sec": round(time.time() - started, 1),
        "python": "3.12",
    }
    _write_json(META, meta)
    logger(f"[done] 用时 {meta['duration_sec']}s，warnings={len(warnings)}，excluded={len(excluded)}")
    return meta


# --------------------------------------------------------------------------- #
# 结构自检（落盘文件 -> 断言）
# --------------------------------------------------------------------------- #
def _check_bars_file(path, label, errors):
    try:
        doc = _load_json(path)
    except (OSError, ValueError) as exc:
        errors.append(f"{label} 无法读取 {path}: {exc}")
        return None
    keys = ("date", "open", "close", "high", "low", "volume", "turnover", "amount")
    lengths = {k: len(doc.get(k) or []) for k in keys}
    if not doc.get("date"):
        errors.append(f"{label} date 为空 {path}")
        return None
    if len(set(lengths.values())) != 1:
        errors.append(f"{label} 列长度不一致 {path}: {lengths}")
    if doc["date"][0] != doc.get("start") or doc["date"][-1] != doc.get("end"):
        errors.append(f"{label} start/end 与 date 首尾不一致 {path}")
    if doc["date"] != sorted(doc["date"]):
        errors.append(f"{label} date 非升序 {path}")
    if len(set(doc["date"])) != len(doc["date"]):
        errors.append(f"{label} date 有重复 {path}")
    if any(v is not None and v <= 0 for v in (doc.get("close") or [])):
        errors.append(f"{label} close 含非正值 {path}")
    return doc


def validate(limit=None):
    """校验已落盘数据：结构/长度/排序/pool 覆盖/adj 硬自检重算。返回 {ok, errors, stats}。"""
    errors = []
    stats = {}
    if not os.path.exists(POOL):
        return {"ok": False, "errors": ["缺少 data/pool.json"], "stats": stats}
    pool = _load_json(POOL)
    stocks = pool.get("stocks") or []
    etfs = pool.get("etfs") or []
    indices = pool.get("indices") or []
    if limit:
        stocks = stocks[: int(limit)]
    stats["pool"] = {"stocks": len(stocks), "etfs": len(etfs), "indices": len(indices)}
    if set(pool) < {"stocks", "etfs", "indices", "updated"}:
        errors.append("pool.json 缺少 DESIGN 键")
    for stock in stocks:
        if not {"code", "name", "sector", "index_symbol"} <= set(stock):
            errors.append(f"pool stock 缺键 {stock}")
            break

    bars_map = {}
    for key in ([s["key"] for s in stocks] + [e["key"] for e in etfs] + [i["key"] for i in indices]):
        path = os.path.join(BARS, f"{key}.json")
        doc = _check_bars_file(path, "bars", errors)
        if doc:
            bars_map[key] = doc
    stats["bars"] = len(bars_map)

    long_map = {}
    if not os.path.exists(os.path.join(BARS_LONG, "sh000300.json")):
        stats["bars_long"] = 0
    else:
        for key in ([s["key"] for s in stocks] + [e["key"] for e in etfs] + [i["key"] for i in indices]):
            path = os.path.join(BARS_LONG, f"{key}.json")
            if not os.path.exists(path):
                errors.append(f"bars_long 缺失 {key}")
                continue
            doc = _check_bars_file(path, "bars_long", errors)
            if doc:
                long_map[key] = doc
        stats["bars_long"] = len(long_map)

    adj_ok = adj_bad = adj_valid = 0
    p99_list = []
    for stock in stocks:
        code = stock["code"]
        path = os.path.join(ADJ, f"{code}.json")
        if not os.path.exists(path):
            errors.append(f"adj 缺失 {code}")
            continue
        doc = _load_json(path)
        segs = doc.get("segments") or []
        if not segs:
            errors.append(f"adj segments 为空 {code}")
            continue
        seg_dates = [s[0] for s in segs]
        if seg_dates != sorted(seg_dates) or len(set(seg_dates)) != len(seg_dates):
            errors.append(f"adj segments 未升序/重复 {code}")
        if abs(segs[-1][1] - 1.0) > 1e-9:
            errors.append(f"adj 最新段因子!=1.0 {code} ({segs[-1][1]})")
        bars = bars_map.get(stock["key"])
        long_doc = long_map.get(stock["key"])
        if not bars or not long_doc:
            continue
        qmap = dict(zip(bars["date"], bars["close"]))
        errs = []
        for date, close in zip(long_doc["date"], long_doc["close"]):
            q = qmap.get(date)
            factor = _seg_factor(segs, date)
            if q is None or factor is None or not close:
                continue
            errs.append(abs(close * factor - q) / q)
        p99 = _p99(errs)
        if p99 is None or p99 >= ADJ_P99_MAX:
            adj_bad += 1
            errors.append(f"adj 自检失败 {code} P99={p99}")
        else:
            adj_ok += 1
            p99_list.append(p99)
        model_p99 = doc.get("model_p99")
        pre_days = doc.get("pre_overlap_days") or 0
        expect_valid = bool(p99 is not None and p99 < ADJ_P99_MAX and
                            (pre_days == 0 or (model_p99 is not None and model_p99 < ADJ_MODEL_P99_MAX)))
        if bool(doc.get("valid")) is not expect_valid:
            errors.append(f"adj valid 标记与重算不一致 {code} (recomputed={expect_valid})")
        if expect_valid:
            adj_valid += 1
    stats["adj"] = {"ok": adj_ok, "bad": adj_bad, "valid": adj_valid,
                    "invalid": len(stocks) - adj_valid,
                    "p99_median": None if not p99_list else round(sorted(p99_list)[len(p99_list) // 2], 6),
                    "p99_max": None if not p99_list else round(max(p99_list), 6)}

    val_ok = fin_ok = 0
    for stock in stocks:
        code = stock["code"]
        try:
            doc = _load_json(os.path.join(VAL, f"{code}.json"))
            lens = [len(doc.get(k) or []) for k in
                    ("date", "close", "pe_ttm", "pb", "peg", "total_mktcap", "free_shares")]
            if len(set(lens)) != 1 or not lens[0]:
                errors.append(f"val 列长度异常 {code}: {lens}")
            else:
                val_ok += 1
        except (OSError, ValueError):
            errors.append(f"val 缺失/损坏 {code}")
        try:
            doc = _load_json(os.path.join(FIN, f"{code}.json"))
            if not (doc.get("latest") or {}).get("report_date"):
                errors.append(f"fin.latest 缺 report_date {code}")
            else:
                fin_ok += 1
        except (OSError, ValueError):
            errors.append(f"fin 缺失/损坏 {code}")
    stats["val"] = val_ok
    stats["fin"] = fin_ok

    if os.path.exists(SNAPSHOT):
        snap = _load_json(SNAPSHOT)
        rows = snap.get("rows") or []
        stats["snapshot"] = {"rows": len(rows), "date": snap.get("date")}
        if len(rows) < 4000:
            errors.append(f"snapshot 行数偏少: {len(rows)}")
        missing_hy = sum(1 for r in rows if not r.get("industry"))
        stats["snapshot"]["missing_industry"] = missing_hy
        for row in rows[:200]:
            if not isinstance(row.get("amount"), (int, float)) or row["amount"] is None:
                continue
        amt_zero = sum(1 for r in rows if not r.get("amount"))
        stats["snapshot"]["amount_zero"] = amt_zero
    else:
        errors.append("缺少 data/snapshot.json")

    if os.path.exists(META):
        meta = _load_json(META)
        for field in ("built_at", "trading_day", "counts", "warnings"):
            if field not in meta:
                errors.append(f"meta.json 缺字段 {field}")
    else:
        errors.append("缺少 data/meta.json")

    return {"ok": not errors, "errors": errors, "stats": stats}
