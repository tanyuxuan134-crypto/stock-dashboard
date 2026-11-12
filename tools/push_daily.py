# -*- coding: utf-8 -*-
"""把当天选股结果推送到微信（PushPlus：国内直连，不需要科学上网）。

为什么用它
----------
网页看板放在 GitHub Pages，手机打开要科学上网；PushPlus 是国内服务，
关注一个公众号就能收到推送，完全不用梯子、不用开电脑看网页。

用法
----
    python tools\\push_daily.py              # 发送最新一天的选股消息
    python tools\\push_daily.py --date 2026-11-11
    python tools\\push_daily.py --test       # 发一条测试消息（验证 token 是否有效）
    python tools\\push_daily.py --dry-run    # 只把内容打印出来，不发送

配置
----
根目录 push_config.json：{"token": "在 pushplus.plus 登录后拿到的 token"}
（也支持环境变量 PUSHPLUS_TOKEN；push_config.json 已加入 .gitignore，不会被上传）
"""
from __future__ import annotations

import argparse
import glob
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "push_config.json")
OUT_DIR = os.path.join(ROOT, "out")
API = "https://www.pushplus.plus/send"

MODE_LABEL = {"A": "A 短线", "B": "B 中线波段", "C": "C 长线"}
MODE_HINT = {"A": "§10.1 / §10.2", "B": "§10.3", "C": "§10.4"}


# ---------------------------------------------------------------- 配置
def load_token() -> str:
    tok = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    if tok:
        return tok
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG, encoding="utf-8-sig") as fh:
                return str(json.load(fh).get("token") or "").strip()
        except Exception:
            return ""
    return ""


def latest_signal_file() -> str:
    files = sorted(glob.glob(os.path.join(OUT_DIR, "信号_*.json")))
    return files[-1] if files else ""


def _fmt_pct(v) -> str:
    try:
        return f"{float(v):+.2f}%"
    except Exception:
        return "—"


def _num(v, nd=2) -> str:
    try:
        return f"{float(v):.{nd}f}"
    except Exception:
        return "—"


def _stop_take(plan: str):
    """从 trade_plan 里抠出止损/止盈价格。"""
    if not plan:
        return "", ""
    stop = re.search(r"止损\s*([\d.]+)", plan)
    take = re.search(r"止盈\s*([\d.]+)", plan)
    return (stop.group(1) if stop else ""), (take.group(1) if take else "")


def _short_why(why: str, max_clauses: int = 2) -> str:
    """why 太长：微信里只留前两小句。"""
    if not why:
        return ""
    parts = [p.strip() for p in re.split(r"[；;]", why) if p.strip()]
    return "；".join(parts[:max_clauses])


# ---------------------------------------------------------------- 内容
def build_message(doc: dict) -> tuple:
    date = str(doc.get("date") or "")
    market = doc.get("market") or {}
    headline = doc.get("headline") or []
    na, nb, nc = (len(doc.get("signals_A") or []), len(doc.get("signals_B") or []),
                  len(doc.get("signals_C") or []))
    snap = 0
    for line in headline:
        m = re.search(r"快照口径有\s*(\d+)\s*只", str(line))
        if m:
            snap = int(m.group(1))
            break
    if not snap:
        snap = len(doc.get("snapshot_B") or [])

    title = f"交易看板 {date[5:]}｜A {na} · B {nb} · C {nc}"
    parts = []
    p = parts.append

    above = bool(market.get("above_ma20"))
    slope = market.get("ma20_slope")
    slope_txt = "上翘" if (isinstance(slope, (int, float)) and slope > 0) else "向下"
    p(f'<p><b>大盘</b>：沪深300 {_fmt_pct(market.get("chg_today"))}，'
      f'{"站上" if above else "跌破"} MA20（MA20 {slope_txt}）</p>')
    p(f'<p><b>今日符合</b>：A {na} 只 · B {nb} 只 · C {nc} 只 · '
      f'§10.6 快照 {snap} 只</p>')

    for mode in "ABC":
        items = doc.get(f"signals_{mode}") or []
        p(f'<h3>{MODE_LABEL[mode]}（{MODE_HINT[mode]}）</h3>')
        if not items:
            if mode == "B" and not above:
                reason = "大盘跌破 MA20，§10.3 的「不买」条件触发，纪律性空仓"
            else:
                reason = "今天没有同时满足条件的标的（不勉强就是纪律）"
            p(f'<p style="color:#888">0 只 —— {reason}</p>')
            continue
        for s in items:
            name = html.escape(str(s.get("name") or ""))
            code = html.escape(str(s.get("code") or ""))
            close = _num(s.get("close"))
            chg = _fmt_pct(s.get("chg_pct"))
            stop, take = _stop_take(str(s.get("trade_plan") or ""))
            extra = []
            if stop:
                extra.append(f"止损 {stop}")
            if take:
                extra.append(f"止盈 {take}")
            if s.get("hold_text"):
                extra.append(f"持有 {s.get('hold_text')}")
            p(f'<p><b>{name} {code}</b> 收盘 {close}（{chg}）<br>'
              f'<span style="color:#666">{html.escape(_short_why(str(s.get("why") or "")))}</span><br>'
              f'<span>{" / ".join(extra) if extra else ""}</span></p>')

    holding = doc.get("holdings") or {}
    rel = holding.get("cash_release")
    risk = doc.get("risk") or {}
    nv = len(risk.get("violations") or [])
    p('<hr>')
    bits = []
    if isinstance(rel, (int, float)) and rel > 0:
        bits.append(f"持仓按 §七 处置可释放约 {int(rel):,} 元")
    bits.append(f"风控违规 {nv} 条")
    p(f'<p><b>持仓 / 风控</b>：{"；".join(bits)}</p>')
    p(f'<p style="color:#999;font-size:12px">数据 {date} 收盘 · 国内直连推送，不用科学上网</p>')

    return title, "".join(parts)


def api_send(token: str, title: str, content: str, timeout: int = 25) -> dict:
    body = json.dumps({
        "token": token,
        "title": title,
        "content": content,
        "template": "html",
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(API, data=body, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------------------- 入口
def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="把选股结果推送到微信（PushPlus）")
    ap.add_argument("--date", help="指定日期 YYYY-MM-DD，默认最新一天")
    ap.add_argument("--test", action="store_true", help="发一条测试消息")
    ap.add_argument("--dry-run", action="store_true", help="只打印内容，不发送")
    args = ap.parse_args(argv)

    if args.test:
        title = "交易看板 · 微信推送测试"
        content = ('<p><b>微信推送已接通 ✅</b></p>'
                   '<p>以后每个交易日 15:10，电脑跑完自动更新后会把「今天 A/B/C 各选出哪些股票」'
                   '发到这里，不用科学上网、也不用开网页。</p>')
        if args.dry_run:
            print(title)
            print(content)
            return 0
        token = load_token()
        if not token:
            print("没有找到 token：请先双击「6-开启微信推送.bat」完成一次设置。")
            return 2
        try:
            res = api_send(token, title, content)
        except Exception as exc:
            print(f"发送失败（网络问题？）：{type(exc).__name__} {exc}")
            return 1
        print(json.dumps(res, ensure_ascii=False))
        return 0 if str(res.get("code")) == "200" else 1

    path = ""
    if args.date:
        path = os.path.join(OUT_DIR, f"信号_{args.date}.json")
        if not os.path.exists(path):
            print(f"找不到 {path}")
            return 2
    else:
        path = latest_signal_file()
        if not path:
            print("out/ 下还没有 信号_*.json，请先跑 run_daily.py")
            return 2

    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    title, content = build_message(doc)
    print(f"消息标题：{title}")
    print(f"消息长度：{len(content)} 字符（数据日期 {doc.get('date')}）")
    if args.dry_run:
        print("----- 内容预览 -----")
        print(content)
        return 0

    token = load_token()
    if not token:
        print("没有找到 token：请先双击「6-开启微信推送.bat」完成一次设置。")
        return 2
    try:
        res = api_send(token, title, content)
    except Exception as exc:
        print(f"发送失败（网络问题？）：{type(exc).__name__} {exc}")
        return 1
    ok = str(res.get("code")) == "200"
    print(("推送成功 ✅ " if ok else "推送返回异常：") + json.dumps(res, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
