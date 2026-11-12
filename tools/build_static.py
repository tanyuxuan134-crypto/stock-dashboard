# -*- coding: utf-8 -*-
"""把 out/ 里的看板打包成「可以直接托管的静态站」（dist/）。

用法：
    python tools/build_static.py            # 用 out/ 里最新一天
    python tools/build_static.py --date 2026-11-10

产出 dist/：
    index.html               最新一天的看板（固定网址）
    看板_YYYY-MM-DD.html      历史日期（页面顶部下拉可直接切换）
    status.json              数据状态：页面靠它判断「云端数据是不是最新」
    .nojekyll                GitHub Pages 用，避免忽略下划线文件
    README.txt               这个目录是自动生成的说明

dist/ 是纯静态文件，可以放到任何静态托管（GitHub Pages / Cloudflare Pages /
腾讯云 CloudBase 静态托管 / Cloud Studio 预览 / 自己的服务器）。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "out")
DIST = os.path.join(ROOT, "dist")

# 云端 runner（GitHub Actions）是 UTC：所有「今天/现在几点」判断都用北京时间，
# 否则北京时间 15:20 的定时任务会被当成「还没到 15:30」，status 会误判过期。
BJ_TZ = timezone(timedelta(hours=8))


def _bj_now() -> datetime:
    return datetime.now(BJ_TZ)


def _today() -> str:
    return _bj_now().strftime("%Y-%m-%d")


def _expected_latest() -> str:
    """最近一个「应该有数据」的交易日（工作日 + 15:30 之后算当天；周末/盘前算上一个工作日）。

    不处理法定节假日：节假日会显示「等云端更新」，不影响看数据。
    """
    now = _bj_now()
    d = now.date()
    if d.weekday() >= 5 or now.hour * 60 + now.minute < 15 * 60 + 30:
        d = d - timedelta(days=1)
    while d.weekday() >= 5:
        d = d - timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def _latest_date() -> str:
    dates = []
    for p in glob.glob(os.path.join(OUT, "看板_*.html")):
        name = os.path.basename(p)[len("看板_"):-len(".html")]
        if len(name) == 10 and name[4] == "-" and name[7] == "-":
            dates.append(name)
    if not dates:
        return ""
    return sorted(dates)[-1]


def _meta_date() -> str:
    p = os.path.join(ROOT, "data", "meta.json")
    try:
        with open(p, encoding="utf-8") as fh:
            return str(json.load(fh).get("trading_day") or "")
    except Exception:
        return ""


# ---------------------------------------------------------------- 公开版脱敏
# 推到公网（GitHub Pages）的页面必须去掉：股数、成本、盈亏金额、账户总额、
# 释放/预算金额、四层金额。保留：代码、名称、现价、比例、动作、止损止盈价。
SENSITIVE_NUM_KEYS = {
    # 英文键
    "principal", "total_asset", "market_value", "cash", "cash_release", "release_cash",
    "target_value", "actual_value", "gap_value", "value", "shares", "cost", "sell_shares",
    "keep_shares", "pnl", "pnl_pct", "weight_amount", "amount", "buy_amount", "budget",
    "position_value", "total_pnl",
    # 中文键（account / holdings 里直接用了中文）
    "总资产", "总市值", "总盈亏", "可用资金", "本金", "仓位", "持仓市值", "持仓成本", "市值",
}
AMOUNT_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(元|万元|万)")
_PAYLOAD_RE = re.compile(
    r'(<script id="payload" type="application/json">)(.*?)(</script>)', re.S
)


def _scrub_str(s: str) -> str:
    return AMOUNT_RE.sub(lambda m: "…" + m.group(2), s)


def _sanitize(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in SENSITIVE_NUM_KEYS and not isinstance(v, (dict, list)):
                out[k] = None
            else:
                out[k] = _sanitize(v)
        return out
    if isinstance(obj, list):
        return [_sanitize(x) for x in obj]
    if isinstance(obj, str):
        return _scrub_str(obj)
    return obj


def _sanitize_html(path: str) -> bool:
    """把页面里内嵌的 payload JSON 脱敏后写回；返回是否改过。"""
    with open(path, encoding="utf-8") as fh:
        html = fh.read()
    m = _PAYLOAD_RE.search(html)
    if not m:
        return False
    try:
        data = json.loads(m.group(2))
    except Exception:
        return False
    clean = _sanitize(data)
    payload = json.dumps(clean, ensure_ascii=False, default=str).replace("</", "<\\/")
    html = html[:m.start(2)] + payload + html[m.end(2):]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return True


def _public_fixes(path: str) -> bool:
    """公开版专属修补：把本机才有的「修改我的持仓」入口换成说明文字。

    公开站是静态页面，没有 /api/holdings 服务，点进去会是 404；
    改成提示用户回本机用 8-填写我的持仓.bat。
    """
    try:
        with open(path, encoding="utf-8") as fh:
            html = fh.read()
    except Exception:
        return False
    changed = False
    link_start = '<a href="holdings_edit.html"'
    if link_start in html:
        idx = html.index(link_start)
        end = html.index("</a>", idx)
        html = html[:idx] + (
            '<span style="color:var(--sub);font-weight:600">✏️ 修改我的持仓：'
            "回你自己电脑上双击「8-填写我的持仓.bat」录入（公开版没有修改入口）</span>"
        ) + html[end + len("</a>"):]
        changed = True
    html = html.replace(
        "自己填一次，以后换股只要改这里；不填就一直用 2026-10-15 的旧快照。",
        "公开版只显示脱敏后的持仓（隐藏股数/成本/金额）；你自己的录入只在电脑本地生效。",
    )
    if changed:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html)
    return changed


def _fix_history(path: str, dates) -> bool:
    """把页面内嵌 payload 的 history 改成 dist 里实际存在的日期（倒序）。

    run_daily 生成当天页面时，当天文件还没写出来，所以 history 里往往少了「今天」；
    打包成静态站后要按 dist 里真实的文件名补齐，否则顶部日期下拉选不中今天。
    """
    try:
        with open(path, encoding="utf-8") as fh:
            html = fh.read()
    except Exception:
        return False
    m = _PAYLOAD_RE.search(html)
    if not m:
        return False
    try:
        obj = json.loads(m.group(2))
    except Exception:
        return False
    obj["history"] = list(dates)
    payload = json.dumps(obj, ensure_ascii=False, default=str).replace("</", "<\\/")
    html = html[:m.start(2)] + payload + html[m.end(2):]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return True


def build(date: str | None = None, clean: bool = True, public: bool = True) -> dict:
    date = date or _latest_date() or _meta_date()
    if not date:
        raise SystemExit("out/ 里没有看板文件，先跑 python run_daily.py")

    if clean and os.path.isdir(DIST):
        shutil.rmtree(DIST)
    os.makedirs(DIST, exist_ok=True)

    # 1) 所有历史看板（含最新一天）
    copied = []
    for src in sorted(glob.glob(os.path.join(OUT, "看板_*.html"))):
        shutil.copy2(src, os.path.join(DIST, os.path.basename(src)))
        copied.append(os.path.basename(src))

    # 2) index.html = 最新一天
    idx_src = os.path.join(OUT, "index.html")
    if not os.path.isfile(idx_src):
        idx_src = os.path.join(OUT, f"看板_{date}.html")
    shutil.copy2(idx_src, os.path.join(DIST, "index.html"))

    # 2.5) 公开版脱敏（隐藏股数/成本/金额）
    scrubbed = 0
    if public:
        for name in ["index.html"] + copied:
            if _sanitize_html(os.path.join(DIST, name)):
                scrubbed += 1
            _public_fixes(os.path.join(DIST, name))

    # 2.6) 补全日期下拉：history 用 dist 里真实存在的日期（含今天）
    dates = sorted(
        {re.sub(r"^看板_|\.html$", "", n) for n in copied if n.startswith("看板_")},
        reverse=True,
    )
    if dates:
        for name in ["index.html"] + copied:
            _fix_history(os.path.join(DIST, name), dates)

    # 3) status.json：前端优先读本地服务，读不到就读这个文件
    expect = _expected_latest()
    _stamp = _bj_now().strftime("%Y-%m-%dT%H:%M:%S")
    status = {
        "today": _today(),
        "data_date": date,
        "expected": expect,
        "built_at": _stamp,
        "stale": date < expect,
        "source": "static",
        "html_mtime": _stamp,
        "index_exists": True,
    }
    with open(os.path.join(DIST, "status.json"), "w", encoding="utf-8") as fh:
        json.dump(status, fh, ensure_ascii=False, indent=1)

    # 4) 静态托管需要的杂项
    with open(os.path.join(DIST, ".nojekyll"), "w", encoding="utf-8") as fh:
        fh.write("")
    with open(os.path.join(DIST, "README.txt"), "w", encoding="utf-8") as fh:
        fh.write(
            "本目录由 tools/build_static.py 自动生成，请勿手动修改。\n"
            f"数据日期：{date}\n生成时间：{status['built_at']}\n"
            "整站是纯静态文件，可直接放到任意静态托管。\n"
        )

    return {"dist": DIST, "date": date, "files": copied, "status": status,
            "public": public, "scrubbed": scrubbed}


def main(argv=None):
    ap = argparse.ArgumentParser(description="把 out/ 看板打包成静态站 dist/")
    ap.add_argument("--date", default=None, help="指定日期（默认 out/ 里最新一天）")
    ap.add_argument("--keep", action="store_true", help="不清理旧 dist/")
    ap.add_argument("--full", action="store_true", help="不脱敏（本机自用；不要上传到公开网站）")
    args = ap.parse_args(argv)
    res = build(args.date, clean=not args.keep, public=not args.full)
    print(f"[build_static] dist/ 就绪：{res['dist']}")
    print(f"  数据日期 {res['date']}，共 {len(res['files'])} 个历史页面")
    print(f"  公开版脱敏：{'是（已隐藏股数/成本/金额）' if res['public'] else '否（含真实持仓，别公开）'}，处理 {res['scrubbed']} 个页面")
    print(f"  状态：{json.dumps(res['status'], ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
