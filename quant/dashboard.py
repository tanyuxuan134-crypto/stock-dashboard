# -*- coding: utf-8 -*-
"""手机看板（单文件 HTML）：把 run_daily 的 ctx 变成「每天打开一眼就知道哪些符合」的网页。

设计目标
--------
* 单文件、零外部依赖（CSS/JS/数据全部内联），断网也能打开；
* 手机竖屏优先（卡片式、点击展开、吸顶筛选条）；
* 每天由 run_daily.py 覆盖生成：out/看板_{date}.html 与 out/index.html（固定网址）；
* 数据来源 = ctx['all_stocks'] / ctx['signals_*']，与 out/信号_{date}.json 完全同源。

用法（一般不用直接调用，run_daily 会自动调）：
    from quant import dashboard as DB
    DB.write_all(ctx, "out", ctx["date"])
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone

# 云端 runner（GitHub Actions）时区是 UTC；页面里的「今天/生成时间」必须按北京时间。
_BJ_TZ = timezone(timedelta(hours=8))


def _bj_now() -> datetime:
    return datetime.now(_BJ_TZ).replace(tzinfo=None)


def _key_of(code: str) -> str:
    """A 股代码 -> 腾讯实时行情 key（sh/sz 前缀）。"""
    c = str(code or "").strip()
    if not c:
        return ""
    if c[0] in "659":
        return "sh" + c
    if c[0] in "0123":
        return "sz" + c
    return ""


def _history(out_dir: str) -> list:
    """扫描 out/ 里已生成的看板日期，倒序返回（用于页面左上角切历史）。"""
    if not out_dir or not os.path.isdir(out_dir):
        return []
    dates = []
    for fn in os.listdir(out_dir):
        m = re.match(r"^看板_(\d{4}-\d{2}-\d{2})\.html$", fn)
        if m:
            dates.append(m.group(1))
    return sorted(dates, reverse=True)


def _recent(out_dir: str, n: int = 5) -> list:
    """近 n 个交易日的选股记录（读 out/信号_{date}.json，倒序）。

    只保留代码/名称/板块这类公开信息，用于看板上的「近五日选股」区块。
    """
    if not out_dir or not os.path.isdir(out_dir):
        return []
    files = []
    for fn in os.listdir(out_dir):
        m = re.match(r"^信号_(\d{4}-\d{2}-\d{2})\.json$", fn)
        if m:
            files.append((m.group(1), os.path.join(out_dir, fn)))
    files.sort(key=lambda x: x[0], reverse=True)
    out = []
    for date, path in files[:n]:
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except Exception:
            continue
        item = {"date": date}
        for mode in "ABC":
            picks = []
            for s in doc.get("signals_" + mode) or []:
                picks.append({
                    "code": str(s.get("code") or ""),
                    "name": s.get("name") or "",
                    "sector": s.get("sector") or "",
                })
            item[mode.lower()] = picks
        out.append(item)
    return out

CSS = """
:root{
  --bg:#f5f6f8; --card:#ffffff; --fg:#1d2129; --sub:#6b7280; --line:#e5e7eb;
  --ok:#0f9d58; --okbg:#e8f6ee; --no:#9aa0a6; --nobg:#f1f2f4;
  --up:#d93025; --down:#0f9d58; --warn:#b45309; --warnbg:#fef3c7;
  --blue:#2563eb; --bluebg:#e8efff; --radius:14px;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#111315; --card:#1b1e21; --fg:#e8eaed; --sub:#9aa0a6; --line:#2b2f33;
    --ok:#34c38f; --okbg:#12301f; --no:#7a8086; --nobg:#22262a;
    --up:#ff6b5e; --down:#34c38f; --warn:#f0b429; --warnbg:#3a2d0a;
    --blue:#6ea8fe; --bluebg:#14243d;
  }
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;padding:0}
body{
  background:var(--bg); color:var(--fg);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  padding-bottom:50px; overflow-wrap:break-word;
}
.wrap{max-width:760px;margin:0 auto;padding:0 12px}
header.top{position:sticky;top:0;z-index:20;background:var(--bg);border-bottom:1px solid var(--line);padding:8px 0 6px}
.topbar{display:flex;align-items:center;justify-content:space-between;gap:8px}
.title{font-size:17px;font-weight:700;letter-spacing:.2px}
.title small{font-size:12px;color:var(--sub);font-weight:500;margin-left:6px}
.mkt{font-size:12px;padding:3px 9px;border-radius:999px;white-space:nowrap}
.mkt.good{background:var(--okbg);color:var(--ok)}
.mkt.bad{background:var(--warnbg);color:var(--warn)}
nav.tabs{display:flex;gap:6px;overflow-x:auto;padding:8px 0 2px;scrollbar-width:none}
nav.tabs::-webkit-scrollbar{display:none}
nav.tabs a{flex:0 0 auto;font-size:12.5px;padding:5px 11px;border-radius:999px;background:var(--nobg);color:var(--sub);text-decoration:none}
nav.tabs a.on{background:var(--bluebg);color:var(--blue);font-weight:600}
h2{font-size:15.5px;margin:20px 0 9px;display:flex;align-items:center;gap:7px}
h2 .no{display:inline-flex;width:20px;height:20px;border-radius:6px;background:var(--bluebg);color:var(--blue);
  font-size:12px;align-items:center;justify-content:center;font-weight:700}
h2 .hint{margin-left:auto;font-size:11.5px;color:var(--sub);font-weight:400}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:12px 13px;margin-bottom:10px}
.tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:10px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:9px 6px;text-align:center}
.tile .k{font-size:11.5px;color:var(--sub)}
.tile .v{font-size:23px;font-weight:800;line-height:1.15;margin-top:2px}
.tile.hit{border-color:color-mix(in srgb,var(--ok) 45%,var(--line));background:var(--okbg)}
.tile.hit .v{color:var(--ok)}
.bullets{margin:0;padding-left:0;list-style:none}
.bullets li{padding:6px 0 6px 16px;position:relative;font-size:13.5px;border-top:1px dashed var(--line)}
.bullets li:first-child{border-top:0}
.bullets li:before{content:"";position:absolute;left:2px;top:14px;width:5px;height:5px;border-radius:50%;background:var(--blue)}
.sig{border:1px solid var(--line);border-radius:12px;padding:10px 11px;margin-bottom:9px;background:var(--card)}
.sig .hd{display:flex;align-items:baseline;gap:7px;flex-wrap:wrap}
.sig .nm{font-weight:700;font-size:15px}
.sig .cd{color:var(--sub);font-size:12.5px;font-variant-numeric:tabular-nums}
.sig .tag{font-size:11px;color:var(--sub);background:var(--nobg);border-radius:6px;padding:1px 6px}
.sig .sc{margin-left:auto;font-size:12px;color:var(--ok);background:var(--okbg);border-radius:999px;padding:2px 9px;font-weight:700}
.sig .m{font-size:13px;color:var(--sub);margin-top:3px;font-variant-numeric:tabular-nums}
.sig .why{font-size:13px;margin-top:6px}
.sig .plan{font-size:13px;margin-top:7px;background:var(--bluebg);border-radius:9px;padding:7px 9px;line-height:1.5}
.sig .risk{font-size:12.5px;color:var(--warn);margin-top:6px}
.empty{border:1px dashed var(--line);border-radius:12px;padding:14px 13px;color:var(--sub);font-size:13.5px;background:var(--card);margin-bottom:9px}
.empty b{color:var(--fg)}
.rec-item{border-top:1px dashed var(--line);padding:9px 0 5px}
.rec-item:first-child{border-top:0;padding-top:1px}
.rec-hd{display:flex;align-items:center;gap:8px;font-size:13.5px}
.rec-hd a{color:var(--blue);font-weight:700;text-decoration:none;font-variant-numeric:tabular-nums}
.rec-hd .cnt{margin-left:auto;font-size:11.5px;color:var(--sub);font-variant-numeric:tabular-nums}
.rec-picks{display:flex;flex-wrap:wrap;gap:5px;margin-top:6px}
.pick{font-size:12.5px;border-radius:7px;padding:3px 8px;background:var(--nobg);color:var(--fg)}
.pick.A{background:var(--okbg);color:var(--ok)}
.pick.B{background:var(--bluebg);color:var(--blue)}
.pick.C{background:var(--warnbg);color:var(--warn)}
.pick.none{color:var(--sub);background:transparent;padding-left:0}
.tag{font-size:11px;color:var(--sub);background:var(--nobg);border-radius:6px;padding:1px 6px}
.filters{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}
.filters button{font:inherit;font-size:12.5px;border:1px solid var(--line);background:var(--card);color:var(--sub);
  border-radius:999px;padding:5px 11px;cursor:pointer}
.filters button.on{background:var(--blue);border-color:var(--blue);color:#fff;font-weight:600}
.morebtn{display:block;width:100%;font:inherit;font-size:13.5px;font-weight:600;border:1px dashed var(--line);
  background:var(--card);color:var(--blue);border-radius:12px;padding:11px;margin:4px 0 10px;cursor:pointer}
.search{display:flex;gap:8px;margin-bottom:9px}
.search input{flex:1;font:inherit;font-size:14px;border:1px solid var(--line);background:var(--card);color:var(--fg);
  border-radius:10px;padding:8px 11px;outline:none}
.search .cnt{font-size:12px;color:var(--sub);align-self:center;white-space:nowrap}
.row{background:var(--card);border:1px solid var(--line);border-radius:12px;margin-bottom:7px;overflow:hidden}
.row-h{display:flex;align-items:center;gap:7px;padding:9px 11px;cursor:pointer;flex-wrap:wrap}
.row-h .cd{font-size:12px;color:var(--sub);font-variant-numeric:tabular-nums}
.row-h .nm{font-weight:650;font-size:14.5px}
.row-h .sec{font-size:11px;color:var(--sub);background:var(--nobg);border-radius:5px;padding:1px 5px}
.row-h .px{margin-left:auto;font-size:14px;font-weight:650;font-variant-numeric:tabular-nums}
.row-h .chg{font-size:12.5px;font-variant-numeric:tabular-nums;min-width:56px;text-align:right}
.up{color:var(--up)} .down{color:var(--down)} .flat{color:var(--sub)}
.badges{display:flex;gap:4px;padding:0 11px 9px;flex-wrap:wrap}
.badge{font-size:11.5px;border-radius:6px;padding:2px 7px;font-weight:650;font-variant-numeric:tabular-nums}
.badge.ok{background:var(--okbg);color:var(--ok)}
.badge.no{background:var(--nobg);color:var(--no);font-weight:500}
.badge.sn{background:var(--bluebg);color:var(--blue);font-weight:500}
.row-d{display:none;border-top:1px dashed var(--line);padding:9px 12px 11px;font-size:12.5px;color:var(--sub);line-height:1.62}
.row.open .row-d{display:block}
.row-d b{color:var(--fg);font-weight:600}
.row-d .kv{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:5px;font-variant-numeric:tabular-nums}
.row-d .fail{margin-top:5px}
.row-d .fail span{display:inline-block;margin:0 8px 3px 0}
.tw{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:0 -4px}
.tbl{width:100%;border-collapse:collapse;font-size:12.5px}
.tbl th,.tbl td{padding:6px 5px;border-bottom:1px solid var(--line);text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.tbl th{color:var(--sub);font-weight:500;font-size:11.5px}
.tbl td:first-child,.tbl th:first-child{text-align:left}
.tbl tr:last-child td{border-bottom:0}
.hcard{border:1px solid var(--line);border-radius:12px;padding:10px 11px;margin-bottom:9px;background:var(--card)}
.hcard .hd{display:flex;align-items:baseline;gap:7px;flex-wrap:wrap}
.hcard .act{margin-left:auto;font-size:12px;font-weight:700;padding:2px 9px;border-radius:999px}
.hcard .act.sell{background:var(--warnbg);color:var(--warn)}
.hcard .act.hold{background:var(--nobg);color:var(--sub)}
.hcard .act.clear{background:var(--warnbg);color:var(--warn)}
.hcard .meta{font-size:12.5px;color:var(--sub);margin-top:4px;font-variant-numeric:tabular-nums;line-height:1.7}
.hcard .tip{font-size:12px;color:var(--sub);margin-top:5px;border-left:2px solid var(--line);padding-left:7px}
.note{font-size:12px;color:var(--sub);margin:-2px 0 9px}
.bar{height:8px;border-radius:99px;background:var(--nobg);overflow:hidden;margin-top:4px}
.bar i{display:block;height:100%;background:var(--blue)}
.bar.good i{background:var(--ok)}
.bar.warn i{background:var(--warn)}
.vio{font-size:13px;padding:7px 0;border-bottom:1px dashed var(--line)}
.vio:last-child{border-bottom:0}
.vio .lv{font-size:10.5px;border-radius:4px;padding:0 5px;margin-right:5px;background:var(--warnbg);color:var(--warn);font-weight:700}
.vio .fix{color:var(--sub);font-size:12px;margin-top:2px}
details{border:1px solid var(--line);border-radius:12px;background:var(--card);padding:10px 12px;margin-bottom:10px}
details summary{cursor:pointer;font-size:13.5px;font-weight:600}
details .dbody{font-size:13px;color:var(--sub);margin-top:8px;line-height:1.7}
footer{color:var(--sub);font-size:11.5px;text-align:center;padding:18px 10px 8px;line-height:1.8}
footer b{color:var(--fg)}
.refresh{position:fixed;right:14px;bottom:16px;z-index:30;background:var(--blue);color:#fff;border:0;border-radius:999px;
  font:inherit;font-size:13px;font-weight:600;padding:10px 15px;box-shadow:0 4px 14px rgba(0,0,0,.22);cursor:pointer}
.ctrl{display:flex;align-items:center;gap:8px;padding:7px 0 0;flex-wrap:wrap}
.ctrl select{font:inherit;font-size:12.5px;border:1px solid var(--line);background:var(--card);color:var(--fg);
  border-radius:9px;padding:5px 8px;outline:none}
.ctrl .upd{font:inherit;font-size:12.5px;font-weight:650;border:1px solid var(--blue);background:var(--bluebg);
  color:var(--blue);border-radius:9px;padding:6px 11px;cursor:pointer}
.ctrl .upd:disabled{opacity:.55;cursor:default}
.ctrl .live{margin-left:auto;font-size:11.5px;color:var(--sub);font-variant-numeric:tabular-nums}
.banner{margin-top:8px;border-radius:11px;padding:9px 11px;font-size:12.5px;line-height:1.6;border:1px solid transparent}
.banner.ok{background:var(--okbg);color:var(--ok);border-color:var(--okbg)}
.banner.warn{background:var(--warnbg);color:var(--warn);border-color:var(--warnbg)}
.banner.bad{background:#fde8e8;color:#b91c1c;border-color:#fbd5d5}
.banner.info{background:var(--bluebg);color:var(--blue);border-color:var(--bluebg)}
.banner.hide{display:none}
.banner b{font-weight:700}
@media (prefers-color-scheme: dark){
  .banner.bad{background:#3b1414;color:#ff9b9b;border-color:#5a1f1f}
  .banner.info{background:#10243d;color:#8fc4ff;border-color:#1b3a5c}
}
@media (max-width:360px){ .row-h .px{min-width:52px} }
"""

JS = """
const D = JSON.parse(document.getElementById('payload').textContent);
const $ = (s, r) => (r || document).querySelector(s);
const el = (tag, cls, html) => { const n = document.createElement(tag); if (cls) n.className = cls; if (html != null) n.innerHTML = html; return n; };
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const num = (x, d=2) => (x === null || x === undefined || Number.isNaN(Number(x))) ? '—' : Number(x).toFixed(d);
const pct = (x, d=2) => (x === null || x === undefined || Number.isNaN(Number(x))) ? '—' : (Number(x) > 0 ? '+' : '') + Number(x).toFixed(d) + '%';
const updown = x => (x === null || x === undefined) ? 'flat' : (Number(x) > 0 ? 'up' : (Number(x) < 0 ? 'down' : 'flat'));
const MODECN = {A:'A短线', B:'B波段', C:'C长线'};

function hero(){
  const m = D.market || {};
  const good = !!m.above_ma20;
  const mk = $('#mkt');
  mk.className = 'mkt ' + (good ? 'good' : 'bad');
  mk.textContent = good ? ('大盘站上MA20 ' + pct(m.chg_today)) : ('大盘跌破MA20 ' + pct(m.chg_today));
  const cnt = id => (D.signals[id] || []).length;
  const tiles = [
    ['A 短线', cnt('A'), cnt('A') > 0],
    ['B 波段', cnt('B'), cnt('B') > 0],
    ['C 长线', cnt('C'), cnt('C') > 0],
    ['快照B', (D.snapshot_B || []).length, false],
  ];
  const box = $('#tiles');
  box.innerHTML = '';
  tiles.forEach(([k, v, hot]) => {
    const t = el('div', 'tile' + (hot ? ' hit' : ''), '<div class="k">' + esc(k) + '</div><div class="v">' + v + '</div>');
    box.appendChild(t);
  });
  $('#dateSub').textContent = D.date || '';
}

function headline(){
  const ul = $('#headline');
  (D.headline || []).forEach(t => ul.appendChild(el('li', null, esc(t))));
}

function sigCard(s, mode){
  const c = el('div', 'sig');
  c.setAttribute('data-code', String(s.code || ''));
  c.innerHTML =
    '<div class="hd"><span class="nm">' + esc(s.name) + '</span><span class="cd">' + esc(s.code) + '</span>' +
    (s.sector ? '<span class="tag">' + esc(s.sector) + '</span>' : '') +
    '<span class="sc">' + esc(s.score_text || '') + ' 通过</span></div>' +
    '<div class="m">收盘 <span class="px">' + num(s.close) + '</span> · <span class="chg ' + updown(s.chg_pct) + '">' + pct(s.chg_pct) + '</span></div>' +
    '<div class="why"><b>为什么符合：</b>' + esc(s.why || '—') + '</div>' +
    '<div class="plan"><b>买什么：</b>' + esc(s.trade_plan || '') + '</div>' +
    '<div class="risk">止损 ' + esc(s.stop_text || '') + ' ｜ 止盈 ' + esc(s.take_text || '') + ' ｜ 持有 ' + esc(s.hold_text || '') + '</div>';
  return c;
}

function emptyBox(mode){
  const m = D.market || {};
  let txt = '';
  if (mode === 'B'){
    if (!m.above_ma20){
      txt = '<b>今天 0 只是纪律，不是漏信号。</b> 沪深300 收盘跌破 MA20，手册 §10.3「不买」条件触发，B 模式全部否决。' +
            '参考：§10.6 快照口径（站上MA20+MA60且双双上翘）今天有 <b>' + (D.snapshot_B || []).length + '</b> 只。';
    } else {
      txt = '<b>今天 0 只。</b> 没有个股同时满足「站上MA20且MA20上翘(B1) + 四信号里至少 2 个」。宁可空仓，不勉强。';
    }
  } else if (mode === 'A'){
    txt = '<b>今天 0 只。</b> 需要「板块当日涨幅前5且板块指数>1%」+ 板块 3 天验证（§10.1.1）+ 个股量能放大 + 收盘站上MA5，今天没有同时满足的。' +
          (D.a_thin ? '<br>另有 <b>' + D.a_thin + '</b> 只所属行业在池内不足 3 只（算不出板块排名/广度），A 短线按规则不适用。' : '');
  } else {
    txt = '<b>今天 0 只。</b> 需要 PE 近5年分位<30% +（股息率>4% 或 ROE>10%且PB<1.5）+ 连续5年盈利 + 距1年高点<−20%。';
  }
  const d = el('div', 'empty', txt);
  return d;
}

function signals(){
  ['A','B','C'].forEach(mode => {
    const box = $('#sig' + mode);
    const arr = D.signals[mode] || [];
    if (!arr.length){ box.appendChild(emptyBox(mode)); return; }
    arr.forEach(s => box.appendChild(sigCard(s, mode)));
  });
}

function recent(){
  const box = $('#recentBox');
  if (!box) return;
  const arr = D.recent || [];
  if (!arr.length){
    box.innerHTML = '<div class="empty">还没有历史记录：需要 out/信号_日期.json（每天跑一次就会自动累积）。</div>';
    return;
  }
  box.innerHTML = arr.map((d, i) => {
    const a = d.a || [], b = d.b || [], c = d.c || [];
    const picks = [];
    a.forEach(x => picks.push('<span class="pick A">A · ' + esc(x.name) + ' ' + esc(x.code) + '</span>'));
    b.forEach(x => picks.push('<span class="pick B">B · ' + esc(x.name) + ' ' + esc(x.code) + '</span>'));
    c.forEach(x => picks.push('<span class="pick C">C · ' + esc(x.name) + ' ' + esc(x.code) + '</span>'));
    const total = a.length + b.length + c.length;
    const today = (i === 0 && d.date === D.date) ? '<span class="tag">今天</span> ' : '';
    return '<div class="rec-item">' +
      '<div class="rec-hd">' + today +
      '<a href="看板_' + esc(d.date) + '.html">' + esc(d.date) + '</a>' +
      '<span class="cnt">A ' + a.length + ' · B ' + b.length + ' · C ' + c.length + '</span></div>' +
      (total
        ? '<div class="rec-picks">' + picks.join('') + '</div>'
        : '<div class="rec-picks"><span class="pick none">0 只（纪律：当天没有同时满足条件的）</span></div>') +
      '</div>';
  }).join('');
}

let filter = 'all', q = '';
const LIMIT0 = 40;
let LIMIT = LIMIT0;
function modeBadge(r, m){
  if (r[m + '_pass']){
    const n = r[m + '_ok'], qq = r[m + '_req'];
    const t = (n != null && qq != null) ? (n + '/' + qq) : (r[m + '_score'] != null ? String(r[m + '_score']) : '');
    return '<span class="badge ok">' + m + ' ' + t + '</span>';
  }
  return '<span class="badge no">' + m + '</span>';
}
function rowHtml(r){
  const cnts = (r.A_pass ? 1 : 0) + (r.B_pass ? 1 : 0) + (r.C_pass ? 1 : 0);
  const bd = [modeBadge(r, 'A'), modeBadge(r, 'B'), modeBadge(r, 'C')];
  if (r.snapshot_B) bd.push('<span class="badge sn">§10.6✓</span>');
  const fails = [];
  if (r.A_fail) fails.push('<b>A：</b>' + esc(r.A_fail));
  if (r.B_fail) fails.push('<b>B：</b>' + esc(r.B_fail));
  if (r.C_fail) fails.push('<b>C：</b>' + esc(r.C_fail));
  const kv = [
    ['PE分位', r.pe_pct_5y == null ? '—' : num(r.pe_pct_5y, 0) + '%'],
    ['距1年高', r.from_hi_1y == null ? '—' : pct(r.from_hi_1y, 1)],
    ['量比', num(r.vol_ratio)],
    ['MA20斜率', num(r.ma20_slope, 3)],
  ].map(([k, v]) => '<span>' + esc(k) + ' <b>' + esc(v) + '</b></span>').join('');
  return '<div class="row" data-code="' + esc(r.code) + '" data-cnt="' + cnts + '">' +
    '<div class="row-h"><span class="cd">' + esc(r.code) + '</span><span class="nm">' + esc(r.name) + '</span>' +
    (r.sector ? '<span class="sec">' + esc(r.sector) + '</span>' : '') +
    '<span class="px">' + num(r.close) + '</span><span class="chg ' + updown(r.chg_pct) + '">' + pct(r.chg_pct) + '</span></div>' +
    '<div class="badges">' + bd.join('') + '</div>' +
    '<div class="row-d"><div class="kv">' + kv + '</div>' +
    (fails.length ? '<div class="fail">' + fails.join('<br>') + '</div>' : '<div class="fail">三项全部通过 ✓</div>') +
    '</div></div>';
}

function match(r){
  if (filter === 'A' && !r.A_pass) return false;
  if (filter === 'B' && !r.B_pass) return false;
  if (filter === 'C' && !r.C_pass) return false;
  if (filter === 'SN' && !r.snapshot_B) return false;
  if (filter === 'HOLD'){
    const codes = ((D.holdings || {}).rows || []).map(h => String(h.code));
    if (!codes.includes(String(r.code))) return false;
  }
  if (q){
    const s = (String(r.code) + ' ' + String(r.name) + ' ' + String(r.sector || '')).toLowerCase();
    if (!s.includes(q.toLowerCase())) return false;
  }
  return true;
}

function renderRows(){
  const box = $('#rows');
  const rows = (D.rows || []).filter(match);
  rows.sort((a, b) => {
    const ca = (a.A_pass?1:0)+(a.B_pass?1:0)+(a.C_pass?1:0), cb = (b.A_pass?1:0)+(b.B_pass?1:0)+(b.C_pass?1:0);
    if (cb !== ca) return cb - ca;
    const sa = (a.A_score||0)+(a.B_score||0)+(a.C_score||0), sb = (b.A_score||0)+(b.B_score||0)+(b.C_score||0);
    if (sb !== sa) return sb - sa;
    return Math.abs(b.chg_pct||0) - Math.abs(a.chg_pct||0);
  });
  box.innerHTML = rows.length ? rows.slice(0, LIMIT).map(rowHtml).join('') :
    '<div class="empty">没有匹配的股票，换个筛选条件试试。</div>';
  $('#cnt').textContent = rows.length + ' 只';
  const more = $('#more');
  if (more){
    if (rows.length > LIMIT){
      more.innerHTML = '<button class="morebtn" id="morebtn">显示全部 ' + rows.length + ' 只（当前 ' + Math.min(LIMIT, rows.length) + '）</button>';
      $('#morebtn').addEventListener('click', () => { LIMIT = rows.length; renderRows(); });
    } else { more.innerHTML = ''; }
  }
  box.querySelectorAll('.row-h').forEach(h => h.addEventListener('click', () => h.parentElement.classList.toggle('open')));
  applyLive();
}

function bindFilters(){
  document.querySelectorAll('.filters button').forEach(b => {
    b.addEventListener('click', () => {
      document.querySelectorAll('.filters button').forEach(x => x.classList.remove('on'));
      b.classList.add('on'); filter = b.dataset.f; LIMIT = LIMIT0; renderRows();
    });
  });
  $('#q').addEventListener('input', e => { q = e.target.value.trim(); LIMIT = LIMIT0; renderRows(); });
}

function holdings(){
  const H = D.holdings || {}, rows = H.rows || [];
  const box = $('#holdBox');
  if (!rows.length){ box.innerHTML = '<div class="empty">当前没有持仓记录。</div>'; return; }
  const sum = (H.cash_release == null)
    ? '<div class="note">按手册 §七 执行减仓（公开版已隐藏股数和金额）。点标题可看逐只方案。</div>'
    : '<div class="note">按手册 §七 执行可释放资金约 <b>' + num(H.cash_release, 0) + ' 元</b>（用于第 4 步 ETF 底仓）。点标题可看逐只方案。</div>';
  box.innerHTML = sum + rows.map(h => {
    const cls = /清仓/.test(h.action || '') ? 'clear' : (/持有/.test(h.action || '') ? 'hold' : 'sell');
    return '<div class="hcard" data-code="' + esc(h.code) + '" data-shares="' + esc(h.shares) + '" data-cost="' + esc(h.cost) + '">' +
      '<div class="hd"><span class="nm" style="font-weight:700">' + esc(h.name) + '</span>' +
      '<span class="cd" style="color:var(--sub);font-size:12px">' + esc(h.code) + '</span>' +
      '<span class="act ' + cls + '">' + esc(h.action || '—') + '</span></div>' +
      '<div class="meta">' + holdingMeta(h, h.price) + '</div>' +
      (h.sell_shares ? '<div class="meta">卖出 ' + esc(h.sell_shares) + ' 股 · 释放 ' + num(h.release_cash, 0) + ' 元</div>' : '') +
      (h.reason ? '<div class="tip">为什么：' + esc(h.reason) + '</div>' : '') +
      (h.trigger ? '<div class="tip">触发条件：' + esc(h.trigger) + '</div>' : '') +
      '</div>';
  }).join('') + (H.same_chain_warning ? '<div class="empty">' + esc(H.same_chain_warning) + '</div>' : '');
}

function holdingMeta(h, px){
  const shares = Number(h.shares) || 0, cost = Number(h.cost) || 0;
  const p = Number(px);
  const total = Number((D.account || {}).total_asset) || 0;
  let pctTotal = null;
  if (total > 0 && p > 0 && shares > 0) pctTotal = (p * shares / total) * 100;
  else if (h.pct_of_total != null) pctTotal = Number(h.pct_of_total);
  const tail = (pctTotal == null) ? '' : ' · 占总资产 <span class="pt">' + num(pctTotal, 1) + '%</span>';
  if (shares > 0 && cost > 0){
    const pnl = (p > 0) ? (p / cost - 1) * 100 : null;
    return '持股 ' + esc(shares) + ' · 成本 ' + num(cost, 3) + ' · 现价 <span class="px">' + num(p) + '</span>' +
      ' · 盈亏 <b class="' + updown(pnl) + '">' + pct(pnl) + '</b>' + tail;
  }
  return '现价 <span class="px">' + num(p) + '</span>' + tail;
}

function volume(){
  const box = $('#volBox'), arr = (D.volume || []).slice(0, 20);
  if (!arr.length){ box.innerHTML = '<div class="empty">今天没有放量数据。</div>'; return; }
  box.innerHTML = '<div class="tw"><table class="tbl"><thead><tr><th>股票</th><th>涨跌</th><th>量倍数</th><th>额倍数</th><th>换手</th><th>真放量</th></tr></thead><tbody>' +
    arr.map(v => '<tr><td>' + esc(v.name) + '</td>' +
      '<td class="' + updown(v.chg_pct) + '">' + pct(v.chg_pct) + '</td>' +
      '<td>' + num(v.vol_ratio) + '</td><td>' + num(v.amt_ratio) + '</td>' +
      '<td>' + num(v.turnover) + '%</td>' +
      '<td>' + (v.true_volume ? '<span style="color:var(--ok);font-weight:700">✓</span>' : '—') + '</td></tr>').join('') +
    '</tbody></table></div><div class="note">§9.3 真放量四条：量倍数≥1.5、成交额≥5亿、换手 3%~15%、收阳线。</div>';
}

function layers(){
  const box = $('#layerBox'), arr = D.layers || [];
  box.innerHTML = arr.map(l => {
    const act = Number(l.actual_pct), tgt = Number(l.target_pct);
    const gap = act - tgt;
    const cls = Math.abs(gap) <= 4 ? 'good' : (gap < 0 ? '' : 'warn');
    return '<div class="hcard"><div class="hd"><b>' + esc(l.name) + '</b>' +
      '<span style="color:var(--sub);font-size:12px">' + esc(l.clause || '') + '</span>' +
      '<span class="act ' + (Math.abs(gap) <= 4 ? 'hold' : 'sell') + '">' + (gap >= 0 ? '超 ' : '欠 ') + Math.abs(gap).toFixed(1) + 'pct</span></div>' +
      '<div class="meta">目标 ' + tgt.toFixed(0) + '%' + (l.target_value != null ? '（' + num(l.target_value, 0) + ' 元）' : '') + ' · 当前 ' + act.toFixed(1) + '%' + (l.actual_value != null ? '（' + num(l.actual_value, 0) + ' 元）' : '') + '</div>' +
      '<div class="bar ' + cls + '"><i style="width:' + Math.min(act, 100) + '%"></i></div></div>';
  }).join('');
}

function risk(){
  const R = D.risk || {}, box = $('#riskBox');
  const viol = R.violations || [];
  box.innerHTML =
    '<div class="note">现金占比 <b>' + num(R.cash_pct, 1) + '%</b>（手册下限 15%）· 最大单只 <b>' + num(R.max_single_pct, 1) + '%</b>（上限 10%）· 违规 <b>' + (R.n_violations || 0) + '</b> 条</div>' +
    (viol.length ? viol.map(v => '<div class="vio"><span class="lv">' + esc(v.level || '⚠') + '</span>' + esc(v.rule || '') + '：' + esc(v.text || '') +
      (v.fix ? '<div class="fix">→ ' + esc(v.fix) + '</div>' : '') + '</div>').join('') : '<div class="empty">今天没有风控违规。</div>');
}

function repair(){
  const R = D.repair || {}, box = $('#repairBox');
  const order = D.execution_order || [];
  const steps = R.steps || [];
  let html = '';
  if (order.length){
    html += '<div class="note">手册 §七 执行顺序（先止血、再建底仓，不要跳步）</div>' +
      order.map((s, i) => '<div class="hcard"><div class="hd"><b>第 ' + (i + 1) + ' 步</b></div>' +
        '<div class="meta">' + esc(String(s).replace(/^第\\d+步[:：]?/, '')) + '</div></div>').join('');
  }
  if (steps.length){
    html += '<div class="note">其中第 4 步（建 ETF 底仓）的金额拆解</div>' +
      steps.map(s => {
        const head = (s && typeof s === 'object') ? (s.step || '') : String(s);
        const det = (s && typeof s === 'object') ? (s.detail || '') : '';
        const amt = (s && typeof s === 'object' && s.amount) ? '（约 ' + num(s.amount, 0) + ' 元）' : '';
        return '<div class="hcard"><div class="hd"><b>' + esc(head) + '</b></div>' +
          '<div class="meta">' + esc(det) + esc(amt) + '</div></div>';
      }).join('');
  }
  box.innerHTML = html || '<div class="empty">暂无修复路线。</div>';
}

function footer(){
  const M = D.meta || {};
  const sec = {};
  (D.rows || []).forEach(r => { const k = r.sector || '其他'; sec[k] = (sec[k] || 0) + 1; });
  const secTxt = Object.keys(sec).map(k => k + ' ' + sec[k]).join(' / ');
  $('#foot').innerHTML =
    '数据区间 <b>' + esc(M.start || '') + ' ~ ' + esc(M.end || '') + '</b> · 生成时间 <b>' + esc(D.generated || '') + '</b><br>' +
    '股票池 <b>' + (D.rows || []).length + '</b> 只（' + esc(secTxt) + '）· 数据源：腾讯行情 / 东财估值财务 / 新浪快照<br>' +
    '本页依据《我的交易模式与纪律手册 v1.2》自动生成，仅用于个人纪律执行与规则验证，不构成投资建议。';
}

/* ---------------- 实时行情（腾讯 qt.gtimg.cn，JSONP 风格 script 加载） ---------------- */
const LIVE = {};
function keyOf(code){
  const c = String(code == null ? '' : code).trim();
  if (!c) return '';
  if ('659'.indexOf(c[0]) >= 0) return 'sh' + c;
  if ('0123'.indexOf(c[0]) >= 0) return 'sz' + c;
  return '';
}
function parseQuote(key){
  let raw = window['v_' + key];
  if (raw == null) return null;
  raw = String(raw);
  const f = raw.split('~');
  if (f.length < 50) return null;
  const price = parseFloat(f[3]);
  if (!(price > 0)) return null;
  const t = String(f[30] || '');
  return {
    key: key, name: f[1], price: price,
    prev: parseFloat(f[4]), open: parseFloat(f[5]),
    high: parseFloat(f[33]), low: parseFloat(f[34]),
    pct: parseFloat(f[32]), amount: parseFloat(f[37]),
    date: t.length >= 8 ? t.slice(0, 4) + '-' + t.slice(4, 6) + '-' + t.slice(6, 8) : '',
    time: t.length >= 14 ? t.slice(8, 10) + ':' + t.slice(10, 12) + ':' + t.slice(12, 14) : '',
  };
}
function loadQuotes(keys, done){
  const list = (keys || []).filter(Boolean);
  const groups = [];
  for (let i = 0; i < list.length; i += 60) groups.push(list.slice(i, i + 60));
  let left = groups.length;
  if (!left){ done && done(); return; }
  groups.forEach(g => {
    const s = document.createElement('script');
    s.charset = 'gbk';
    s.src = 'https://qt.gtimg.cn/q=' + g.join(',') + '&_=' + Date.now() + Math.floor(Math.random() * 1000);
    s.onload = s.onerror = () => {
      g.forEach(k => { const q = parseQuote(k); if (q) LIVE[k] = q; });
      if (--left <= 0 && done) done();
    };
    document.head.appendChild(s);
  });
}
function normKey(code){ return keyOf(code); }
function applyLive(){
  // 1) 逐票清单
  document.querySelectorAll('#rows .row').forEach(row => {
    const q = LIVE[normKey(row.getAttribute('data-code'))];
    if (!q) return;
    const px = row.querySelector('.row-h .px'), chg = row.querySelector('.row-h .chg');
    if (px) px.textContent = num(q.price);
    if (chg){ chg.textContent = pct(q.pct); chg.className = 'chg ' + updown(q.pct); }
  });
  // 2) 今日信号卡
  document.querySelectorAll('#signals .sig').forEach(card => {
    const q = LIVE[normKey(card.getAttribute('data-code'))];
    if (!q) return;
    const px = card.querySelector('.m .px'), chg = card.querySelector('.m .chg');
    if (px) px.textContent = num(q.price);
    if (chg){ chg.textContent = pct(q.pct); chg.className = 'chg ' + updown(q.pct); }
  });
  // 3) 持仓卡（现价 / 盈亏 / 占比）
  document.querySelectorAll('#holdBox .hcard[data-code]').forEach(card => {
    const q = LIVE[normKey(card.getAttribute('data-code'))];
    if (!q) return;
    const h = { shares: card.getAttribute('data-shares'), cost: card.getAttribute('data-cost') };
    const box = card.querySelector('.meta');
    if (box) box.innerHTML = holdingMeta(h, q.price);
  });
  // 4) 大盘徽章
  const mi = LIVE['sh000300'];
  if (mi && D.market){
    const good = !!D.market.above_ma20;
    const mk = $('#mkt');
    if (mk){
      mk.className = 'mkt ' + (good ? 'good' : 'bad');
      mk.textContent = (good ? '大盘站上MA20 ' : '大盘跌破MA20 ') + pct(mi.pct);
    }
  }
  // 5) 行情时间提示
  const any = Object.values(LIVE)[0];
  const at = $('#liveAt');
  if (at && any){
    at.textContent = '行情 ' + (any.time || '') + (any.date ? '（' + any.date + '）' : '') +
      (D.date && any.date && any.date !== D.date ? ' · 看板数据 ' + D.date : '');
  }
}
function initLive(){
  const keys = [];
  (D.rows || []).forEach(r => keys.push(normKey(r.code)));
  ((D.holdings || {}).rows || []).forEach(h => keys.push(normKey(h.code)));
  keys.push('sh000300');
  loadQuotes(keys, applyLive);
}

/* ---------------- 本地服务：状态 / 手动或自动更新 ---------------- */
async function api(path, opt){
  const r = await fetch(path, Object.assign({cache: 'no-store'}, opt || {}));
  const j = await r.json();
  return {http_status: r.status, body: j};
}
async function doRefresh(){
  const btn = $('#upd');
  const old = btn ? btn.textContent : '';
  if (btn){ btn.disabled = true; btn.textContent = '更新中…'; }
  try{
    const res = await api('/api/refresh', {method: 'POST'});
    const ok = !!(res.body && res.body.ok);
    if (btn){ btn.disabled = false; btn.textContent = ok ? ('已更新 ' + (res.body.date || '') + '（' + (Number(res.body.seconds) || 0).toFixed(0) + ' 秒）') : old; }
    return ok;
  }catch(e){
    if (btn){ btn.disabled = false; btn.textContent = old; }
    return false;
  }
}
function banner(cls, html){
  const b = $('#banner');
  if (!b) return;
  b.className = 'banner ' + cls;
  b.innerHTML = html;
}
let STATUS_MODE = 'unknown';   // 'api' 本地服务 | 'static' 云端静态站 | 'local' 直接打开文件
let STATUS_INFO = null;

async function fetchStatus(){
  // 1) 本地服务（能自动重建）；2) 静态托管（云端每天定时生成 status.json）
  if (STATUS_MODE !== 'static'){
    try{
      const res = await api('/api/status');
      if (res && res.body && typeof res.body === 'object'){
        STATUS_MODE = 'api';
        return res.body;
      }
    }catch(e){}
  }
  try{
    const r = await fetch('status.json?t=' + Date.now(), {cache: 'no-store'});
    if (r.ok){
      const j = await r.json();
      if (j && typeof j === 'object'){ STATUS_MODE = 'static'; return j; }
    }
  }catch(e){}
  if (location.protocol === 'file:') STATUS_MODE = 'local';
  return null;
}

function sessionOf(){
  // 判断现在处在交易的哪个阶段（用于把「为什么还没有今天的数据」说清楚）
  const d = new Date();
  const wd = d.getDay();
  const hm = d.getHours() * 100 + d.getMinutes();
  if (wd === 0 || wd === 6) return {kind: 'offday', text: '休市'};
  if (hm < 930) return {kind: 'before', text: '盘前'};
  if (hm < 1500) return {kind: 'intraday', text: '盘中'};
  return {kind: 'after', text: '已收盘'};
}

async function initStatus(){
  const st = await fetchStatus();
  STATUS_INFO = st;
  const up = $('#upd');
  if (up && STATUS_MODE !== 'api'){ up.textContent = '重新加载'; up.title = '重新加载页面（不会重新计算选股数据）'; }
  if (!st){
    if (location.protocol === 'file:'){
      banner('warn', '这是直接打开的本机文件，数据停在 <b>' + esc(D.date || '—') +
        '</b>。<br>电脑上双击 <b>1-电脑看板.bat</b> 可重新生成；手机上想随时看，用 <b>2-手机看板.bat</b> 或云端网址。');
    } else {
      banner('warn', '没读到数据状态（status.json 或本地服务）。页面数据日期：<b>' + esc(D.date || '—') + '</b>');
    }
    return;
  }
  const today = st.today || D.today || '';
  const dd = st.data_date || D.date || '';
  const sess = sessionOf();
  if (dd && today && dd === today){
    banner('ok', '今天的收盘数据已更新（<b>' + esc(dd) + '</b>）· 生成于 ' +
      esc(String(st.built_at || D.generated || '').replace('T', ' ').slice(0, 16)) +
      (STATUS_MODE === 'static' ? ' · 云端已同步' : ''));
    setTimeout(() => { const b = $('#banner'); if (b) b.classList.add('hide'); }, 4500);
    return;
  }
  if (!st.stale){
    // 今天的预期数据还没到（盘中/盘前/休市），这是正常的「还没到更新时间」
    const hint = sess.kind === 'intraday' ? '；选股要用收盘数据，所以 15:00 收盘后才算今天'
      : sess.kind === 'before' ? '；15:00 收盘后才生成今天的数据'
      : sess.kind === 'offday' ? '；周末休市，显示最近一个交易日' : '';
    banner('info', '当前显示 <b>' + esc(dd) + '</b> 的收盘数据（最近一个交易日）· 现在 <b>' + esc(today || '今天') +
      '</b> ' + esc(sess.text) + hint +
      '。顶部行情价格是实时的，随时在跳；每个交易日 <b>15:10</b> 后电脑自动更新并上传。');
    return;
  }
  if (STATUS_MODE === 'api'){
    banner('warn', '数据还停在 <b>' + esc(st.data_date || '—') + '</b>，今天是 <b>' + esc(st.today || '') +
      '</b>。正在自动更新（约 20~60 秒），完成后页面会自动刷新…');
    const ok = await doRefresh();
    if (ok) location.reload();
    else banner('bad', '自动更新失败。可以点右上角「更新数据」重试，或手动运行 <b>1-电脑看板.bat</b>。');
    return;
  }
  // 静态托管：数据由电脑每天生成后上传，页面只能等下一次上传
  banner('warn', '云端最新数据是 <b>' + esc(st.data_date || '—') + '</b>' +
    (st.today ? '（今天 ' + esc(st.today) + '）' : '') + '，今天的还没更新。<br>常见原因：电脑没开机 / 没联网 / 计划任务没跑成。' +
    '本页每 2 分钟自动检查一次，上传后会自动刷新；也可以在电脑上双击 <b>1-电脑看板.bat</b> 手动补一次。');
}
function initHist(){  const sel = $('#histSel'), hist = D.history || [];
  if (!sel) return;
  if (!hist.length){ sel.style.display = 'none'; return; }
  sel.innerHTML = hist.map(d => '<option value="' + esc(d) + '"' + (d === D.date ? ' selected' : '') + '>' + esc(d) + '</option>').join('');
  sel.addEventListener('change', () => { location.href = '看板_' + sel.value + '.html'; });
}
function initPoll(){
  // 页面长期开着也不会一直看旧数据：每 2 分钟问一次状态；能自动重建就重建，跨日就刷新。
  setInterval(async () => {
    if (document.hidden) return;
    const st = await fetchStatus();
    if (!st) return;
    STATUS_INFO = st;
    if (STATUS_MODE === 'api' && st.stale){
      banner('warn', '数据停在 <b>' + esc(st.data_date || '—') + '</b>，正在自动更新…');
      const ok = await doRefresh();
      if (ok) location.reload();
      return;
    }
    if (st.data_date && D.date && st.data_date !== D.date) location.reload();
  }, 120000);
}
function initUpdateBtn(){
  const btn = $('#upd');
  if (!btn) return;
  btn.addEventListener('click', async () => {
    if (STATUS_MODE === 'api'){
      const ok = await doRefresh();
      if (ok) location.reload(); else alert('更新失败，请稍后再试（或查看服务窗口日志）。');
      return;
    }
    if (STATUS_MODE === 'static'){ location.reload(); return; }
    alert('当前是直接打开的本地文件（file://），不能自动更新。\\n\\n请双击「1-电脑看板.bat」重新生成，或用「2-手机看板.bat」打开本地服务版。');
  });
}

function boot(){
  initHist();
  hero(); headline(); signals(); recent(); bindFilters(); renderRows();
  holdings(); volume(); layers(); risk(); repair(); footer();
  applyLive();
  initLive();
  initUpdateBtn();
  initStatus();
  initPoll();
  $('#refresh').addEventListener('click', () => location.reload());
  document.querySelectorAll('nav.tabs a').forEach(a => a.addEventListener('click', () => {
    document.querySelectorAll('nav.tabs a').forEach(x => x.classList.remove('on')); a.classList.add('on');
  }));
  // 支持 file:// 下带 #锚点 打开（截图/分享定位用）：内容渲染完再滚到锚点
  if (location.hash){
    const t = document.querySelector(location.hash);
    if (t) setTimeout(() => t.scrollIntoView({block:'start'}), 80);
  }
}
document.addEventListener('DOMContentLoaded', boot);
"""

TPL = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta http-equiv="Cache-Control" content="no-store, max-age=0">
<meta name="theme-color" content="#2563eb">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="交易看板">
<title>交易看板 · __DATE__</title>
<style>__CSS__</style>
</head>
<body>
<header class="top">
  <div class="wrap">
    <div class="topbar">
      <div class="title">交易手册看板<small id="dateSub">__DATE__</small></div>
      <div class="mkt good" id="mkt">大盘状态</div>
    </div>
    <div class="ctrl">
      <select id="histSel" aria-label="历史日期"></select>
      <button class="upd" id="upd" type="button">更新数据</button>
      <span class="live" id="liveAt">行情加载中…</span>
    </div>
    <div class="banner hide" id="banner"></div>
    <nav class="tabs">
      <a href="#signals" class="on">今日信号</a>
      <a href="#recent">近五日</a>
      <a href="#all">逐票对照</a>
      <a href="#holdings">持仓处置</a>
      <a href="#volume">放量榜</a>
      <a href="#portfolio">仓位风控</a>
    </nav>
  </div>
</header>
<div class="wrap">
  <div class="tiles" id="tiles" style="margin-top:12px"></div>

  <h2><span class="no">1</span>今天先说结论<span class="hint">§十二 一页速查</span></h2>
  <div class="card"><ul class="bullets" id="headline"></ul></div>

  <h2 id="signals"><span class="no">2</span>今天有哪些符合条件<span class="hint">A/B/C 三套模式</span></h2>
  <div class="card">
    <div style="font-weight:700;font-size:14px;margin-bottom:8px">A 短线 <span style="color:var(--sub);font-weight:400;font-size:12px">§10.1 / §10.2 · 持有 1~5 日 · −3% 止损</span></div>
    <div id="sigA"></div>
  </div>
  <div class="card">
    <div style="font-weight:700;font-size:14px;margin-bottom:8px">B 中线波段 <span style="color:var(--sub);font-weight:400;font-size:12px">§10.3 · 2 周~3 月 · 跌破MA20减半</span></div>
    <div id="sigB"></div>
  </div>
  <div class="card">
    <div style="font-weight:700;font-size:14px;margin-bottom:8px">C 长线 <span style="color:var(--sub);font-weight:400;font-size:12px">§10.4 · 6 月~2 年 · 逻辑止损</span></div>
    <div id="sigC"></div>
  </div>

  <h2 id="recent"><span class="no">3</span>近 5 日选股记录<span class="hint">A/B/C 选出过谁，点日期回看</span></h2>
  <div class="card" id="recentBox"></div>

  <h2 id="all"><span class="no">4</span>逐票对照：谁符合、谁不符合<span class="hint">点一行展开原因</span></h2>
  <div class="filters">
    <button class="on" data-f="all">全部</button>
    <button data-f="A">A 通过</button>
    <button data-f="B">B 通过</button>
    <button data-f="C">C 通过</button>
    <button data-f="SN">快照B</button>
    <button data-f="HOLD">我的持仓</button>
  </div>
  <div class="search">
    <input id="q" type="search" placeholder="搜代码 / 名称 / 板块" autocomplete="off">
    <span class="cnt" id="cnt"></span>
  </div>
  <div id="rows"></div>
  <div id="more"></div>

  <h2 id="holdings"><span class="no">5</span>我的持仓怎么办<span class="hint">§七 处置方案</span></h2>
  <div id="holdBox"></div>

  <h2 id="volume"><span class="no">6</span>放量诊断 Top20<span class="hint">§9.3 真放量四条</span></h2>
  <div class="card" id="volBox"></div>

  <h2 id="portfolio"><span class="no">7</span>仓位 / 风控 / 修复路线<span class="hint">§二 §四 §五</span></h2>
  <div id="layerBox"></div>
  <div class="card" id="riskBox"></div>
  <div class="card" id="repairBox"></div>

  <details>
    <summary>历史回测速览（这是“规则有没有用”的体检，不是今日信号）</summary>
    <div class="dbody">
      <b>A 短线</b>（2024-09~2026-10，154 只池）：361 笔成交，胜率 36.3%（保本线 35.3%），盈亏比 1.33，单笔期望 +0.73%；
      组合按最多 5 仓重放 +1.3%，主要被“同一天信号太多、仓位不够”吃掉。<br>
      <b>B 波段</b>：1340 笔，胜率 44.6%，盈亏比 2.25，单笔期望 +6.35%；组合重放 +51.4%（年化 22%、最大回撤 −20.1%、Sharpe 1.05），
      同期池内等权买入持有 +122%——分散 + 止损的代价就是跑不满，看风险调整后的指标。<br>
      <b>事件研究</b>：A 信号出现后 20 日平均 +7.62%（全样本基线 +2.79%），有明显正向 alpha。
    </div>
  </details>

  <footer id="foot"></footer>
</div>
<button class="refresh" id="refresh">刷新</button>
<script id="payload" type="application/json">__PAYLOAD__</script>
<script>__JS__</script>
</body>
</html>
"""


def _payload(ctx: dict, out_dir: str | None = None) -> dict:
    """挑 HTML 需要的字段（与 out/信号_{date}.json 同源，不新增计算）。"""
    holdings = ctx.get("holdings") or {}
    hold_rows = []
    for h in holdings.get("rows") or []:
        item = dict(h)
        item["key"] = _key_of(h.get("code"))
        hold_rows.append(item)
    rows = []
    for r in ctx.get("all_stocks") or []:
        rows.append({
            "code": r.get("code"), "key": _key_of(r.get("code")),
            "name": r.get("name"), "sector": r.get("sector"),
            "close": r.get("close"), "chg_pct": r.get("chg_pct"),
            "pe_pct_5y": r.get("pe_pct_5y"), "from_hi_1y": r.get("from_hi_1y"),
            "vol_ratio": r.get("vol_ratio"), "ma20_slope": r.get("ma20_slope"),
            "A_pass": bool(r.get("A_pass")), "A_score": r.get("A_score"),
            "A_ok": r.get("A_ok"), "A_req": r.get("A_req"),
            "B_pass": bool(r.get("B_pass")), "B_score": r.get("B_score"),
            "B_ok": r.get("B_ok"), "B_req": r.get("B_req"),
            "C_pass": bool(r.get("C_pass")), "C_score": r.get("C_score"),
            "C_ok": r.get("C_ok"), "C_req": r.get("C_req"),
            "snapshot_B": bool(r.get("snapshot_B")),
            "A_fail": r.get("A_fail") or "", "B_fail": r.get("B_fail") or "",
            "C_fail": r.get("C_fail") or "",
        })
    return {
        "date": ctx.get("date"),
        "today": _bj_now().strftime("%Y-%m-%d"),
        "generated": _bj_now().strftime("%Y-%m-%d %H:%M"),
        "history": _history(out_dir or ""),
        "recent": _recent(out_dir or "", 5),
        "n_stocks": ctx.get("n_stocks"),
        "headline": ctx.get("headline") or [],
        "tier_text": ctx.get("tier_text") or "",
        "etf_first_text": ctx.get("etf_first_text") or "",
        "market": ctx.get("market") or {},
        "signals": {"A": ctx.get("signals_A") or [],
                    "B": ctx.get("signals_B") or [],
                    "C": ctx.get("signals_C") or []},
        "snapshot_B": ctx.get("snapshot_B") or [],
        "rows": rows,
        "a_thin": sum(1 for r in rows if "样本不足" in (r.get("A_fail") or "")),
        "holdings": {"rows": hold_rows,
                     "cash_release": holdings.get("cash_release") or 0,
                     "same_chain_warning": holdings.get("same_chain_warning") or ""},
        "layers": ctx.get("layers") or [],
        "sector_alloc": ctx.get("sector_alloc") or [],
        "risk": ctx.get("risk") or {},
        "volume": ctx.get("volume_diag") or [],
        "repair": ctx.get("repair") or {},
        "execution_order": ctx.get("execution_order") or [],
        "account": ctx.get("account") or {},
        "profile": ctx.get("profile") or "",
        "meta": ctx.get("meta") or {},
    }


def build_html(ctx: dict, out_dir: str | None = None) -> str:
    """返回单文件 HTML 字符串（内联 CSS/JS/数据）。"""
    payload = json.dumps(_payload(ctx, out_dir), ensure_ascii=False, default=str)
    # 防止数据里出现 </script> 提前闭合脚本标签
    payload = payload.replace("</", "<\\/")
    return (TPL.replace("__CSS__", CSS)
               .replace("__JS__", JS)
               .replace("__PAYLOAD__", payload)
               .replace("__DATE__", str(ctx.get("date") or "")))


def write_all(ctx: dict, out_dir: str, date: str) -> dict:
    """写 out/看板_{date}.html，并同步出一份 out/index.html（固定网址）。"""
    os.makedirs(out_dir, exist_ok=True)
    html = build_html(ctx, out_dir)
    p_day = os.path.join(out_dir, f"看板_{date}.html")
    with open(p_day, "w", encoding="utf-8") as fh:
        fh.write(html)
    p_idx = os.path.join(out_dir, "index.html")
    with open(p_idx, "w", encoding="utf-8") as fh:
        fh.write(html)
    return {"html": p_day, "index": p_idx}
