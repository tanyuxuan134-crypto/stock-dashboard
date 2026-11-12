# -*- coding: utf-8 -*-
"""报告输出层：把 run_daily 的结果写成 md（人读）、xlsx（可筛选）、docx（可打印/存档）。

所有数字都来自量化计算；文案里的每一条规则都带手册条款号（§），不臆造结论。
"""
from __future__ import annotations

import os
from typing import Any, Optional

from . import config as C

# ============================================================ 小工具


def _n(x: Any, digits: int = 2, dash: str = "—") -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return dash
    if v != v:
        return dash
    return f"{v:,.{digits}f}"


def _p(x: Any, digits: int = 2) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "—"
    if v != v:
        return "—"
    return f"{v:+.{digits}f}%"


def _md_table(headers: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(str(h) for h in headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join("" if c is None else str(c) for c in r) + " |")
    return "\n".join(out)


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


# ============================================================ 上下文 → Markdown


def build_md(ctx: dict) -> str:
    d = ctx.get("date", "")
    L = []
    L.append(f"# 量化交易日报 · {d}")
    L.append("")
    L.append(f"> 依据《我的交易模式与纪律手册 v1.2》自动生成 ｜ 数据：腾讯前复权日线 + 东财估值/财务 "
             f"｜ 全市场股票池 {ctx.get('n_stocks', '—')} 只")
    L.append("")

    # ---- 0. 一句话结论
    L.append("## 0. 今天先说结论")
    L.append("")
    for line in ctx.get("headline") or []:
        L.append(f"- {line}")
    L.append("")

    # ---- 1. 账户与结构
    acc = ctx.get("account") or {}
    L.append("## 1. 账户现状（§一 / §二）")
    L.append("")
    L.append(_md_table(
        ["总资产", "投入本金", "总盈亏", "可用现金", "现金占比", "仓位"],
        [[_n(acc.get("total_asset")), _n(acc.get("principal")), _n(acc.get("total_pnl")),
          _n(acc.get("cash")),
          _p((acc.get("cash") or 0) / max(acc.get("total_asset") or 1, 1) * 100),
          _p((1 - (acc.get("cash") or 0) / max(acc.get("total_asset") or 1, 1)) * 100)]]))
    L.append("")
    L.append(f"- **手册门槛档位**：{ctx.get('tier_text', '')}")
    L.append(f"- **ETF 优先**：{ctx.get('etf_first_text', '')}")
    L.append("")

    # ---- 2. 四层资产
    L.append("## 2. 四层资产配置（§二：核心 45% / 守息 15% / 卫星 25% / 现金 15%）")
    L.append("")
    rows = []
    for r in ctx.get("layers") or []:
        rows.append([r["name"], f"{r['target_pct']:.0f}%", _n(r["target_value"], 0),
                     f"{r['actual_pct']:.1f}%", _n(r["actual_value"], 0), _n(r["gap_value"], 0),
                     r["content"]])
    L.append(_md_table(["层", "目标", "目标金额", "当前", "当前金额", "缺口", "对应标的"], rows))
    L.append("")

    # ---- 3. 板块配比
    L.append("## 3. 板块配比（§五，当前档位：{}）".format(ctx.get("profile", C.DEFAULT_PROFILE)))
    L.append("")
    rows = [[r["name"], f"{r['target_pct']:.0f}%", f"{r['actual_pct']:.1f}%", _p(r["gap_pct"])]
            for r in ctx.get("sector_alloc") or []]
    L.append(_md_table(["板块", "目标", "当前", "缺口"], rows))
    L.append("")

    # ---- 4. 持仓处置
    L.append("## 4. 现有持仓处置（§七）")
    L.append("")
    hp = (ctx.get("holdings") or {})
    rows = []
    for r in hp.get("rows") or []:
        rows.append([r["code"], r["name"], int(r["shares"]), _n(r["cost"], 3), _n(r["price"]),
                     _p(r["pnl_pct"]), f"{r['pct_of_total']:.2f}%", r["action"],
                     f"{r['sell_shares']}股" if r["sell_shares"] else "—",
                     _n(r["release_cash"], 0), r["trigger"]])
    L.append(_md_table(["代码", "名称", "股数", "成本", "现价", "盈亏", "占比", "手册动作",
                        "本次建议卖出", "释放资金", "触发条件"], rows))
    L.append("")
    if hp.get("same_chain_warning"):
        L.append(f"> ⚠️ {hp['same_chain_warning']}")
        L.append("")
    if hp.get("cash_release"):
        L.append(f"**按手册执行可释放资金约 {_n(hp['cash_release'], 0)} 元**（用于第 4 步建 ETF 底仓）。")
        L.append("")

    # ---- 5. 今日信号
    L.append("## 5. 今日信号（按手册三套模式）")
    L.append("")
    for mode, title in (("A", "A 短线（§10.1/§10.2：只在脉冲板块里做放量突破，−3% 硬止损）"),
                        ("B", "B 中线波段（§10.3：四信号至少 2 个，信号①必选）"),
                        ("C", "C 长线（§10.4：估值分位 + 分红/质量 + 连盈 + 位置，分 5 批建仓）")):
        sig = ctx.get(f"signals_{mode}") or []
        L.append(f"### {title}")
        L.append("")
        if not sig:
            L.append("- 今日无满足全部条件的标的（空仓也是纪律：手册 §六/§十 都强调「不追、不勉强」）。")
        else:
            rows = []
            for s in sig:
                rows.append([s["code"], s["name"], _n(s.get("close")), _p(s.get("chg_pct")),
                             s.get("score_text"), s.get("why"), s.get("trade_plan")])
            L.append(_md_table(["代码", "名称", "收盘", "当日", "评分", "通过的关键条件", "交易计划"], rows))
        L.append("")

    # ---- 6. 放量诊断
    L.append("## 6. 今日放量诊断（§9.3 真放量四条）")
    L.append("")
    vd = ctx.get("volume_diag") or []
    if vd:
        rows = [[v["code"], v["name"], _p(v.get("chg_pct")), _n(v.get("vol_ratio")),
                 _n(v.get("amt_ratio")), _n(v.get("turnover")), v["level"],
                 "✅" if v["true_volume"] else "—"] for v in vd]
        L.append(_md_table(["代码", "名称", "涨跌", "量倍数", "额倍数", "换手%", "放量等级", "真放量"], rows))
    else:
        L.append("- 暂无数据。")
    L.append("")

    # ---- 7. 修复路线
    L.append("## 7. 修复路线（§四「先 ETF、后个股」+ §七 执行顺序）")
    L.append("")
    for i, step in enumerate(C.EXECUTION_ORDER, 1):
        L.append(f"{i}. {step.replace('第' + str(i) + '步：', '')}")
    L.append("")
    rp = ctx.get("repair") or {}
    for step in rp.get("steps") or []:
        L.append(f"- **{step['step']}**：{step['detail']}")
    L.append("")

    # ---- 8. 风控体检
    L.append("## 8. 风控体检（§五 仓位纪律七条）")
    L.append("")
    rk = ctx.get("risk") or {}
    viol = rk.get("violations") or []
    if viol:
        rows = [[v["rule"], v["level"], v["text"], v["fix"]] for v in viol]
        L.append(_md_table(["条款", "级别", "问题", "怎么改"], rows))
    else:
        L.append("- 未发现违规。")
    L.append("")

    # ---- 9. 手册内部冲突
    L.append("## 9. 手册内部口径冲突（程序如何取舍）")
    L.append("")
    rows = [[c["topic"], c["a"], c["b"], c["chosen"]] for c in C.CONFLICTS]
    L.append(_md_table(["议题", "写法A", "写法B", "本系统采用"], rows))
    L.append("")

    # ---- 10. 数据说明
    L.append("## 10. 数据与口径")
    L.append("")
    meta = ctx.get("meta") or {}
    L.append(f"- 交易日：{d}；行情覆盖：{meta.get('start', '—')} ~ {meta.get('end', '—')}（腾讯前复权日线，640 根上限）")
    L.append(f"- 估值：东财 RPT_VALUEANALYSIS_DET（PE_TTM / PB / 总市值，2021 年起，PE 分位按近 5 年）")
    L.append(f"- 财务：东财 F10 主要指标（ROE / 毛利率 / 资产负债率 / 连续盈利年数）")
    L.append(f"- 板块：精选池所属行业等权指数（成员按前复权收盘等权加权），用于 §10.1 板块排名与 §10.3 板块共振")
    for w in meta.get("warnings") or []:
        L.append(f"- ⚠️ {w}")
    L.append("")
    L.append("---")
    L.append("")
    L.append(f"*{C.DISCLAIMER}*")
    L.append("")
    return "\n".join(L)


# ============================================================ XLSX


def build_xlsx(ctx: dict, path: str) -> str:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    hdr_fill = PatternFill("solid", fgColor="1F4E79")
    hdr_font = Font(color="FFFFFF", bold=True, size=10)
    thin = Side(style="thin", color="D9D9D9")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    wrap = Alignment(wrap_text=True, vertical="center")
    center = Alignment(horizontal="center", vertical="center")

    def _write(ws, headers, rows, widths=None, pct_cols=(), num_cols=()):
        ws.append(headers)
        for c in range(1, len(headers) + 1):
            cell = ws.cell(row=1, column=c)
            cell.fill = hdr_fill
            cell.font = hdr_font
            cell.alignment = center
            cell.border = border
        for r in rows:
            ws.append(r)
        for row in ws.iter_rows(min_row=2, max_row=ws.max_row, max_col=len(headers)):
            for cell in row:
                cell.border = border
                cell.alignment = wrap
        ws.freeze_panes = "A2"
        if widths:
            for i, w in enumerate(widths, 1):
                ws.column_dimensions[get_column_letter(i)].width = w
        ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{max(ws.max_row, 1)}"

    ws = wb.active
    ws.title = "概览"
    acc = ctx.get("account") or {}
    meta = ctx.get("meta") or {}
    rows = [
        ["日期", ctx.get("date")],
        ["股票池数量", ctx.get("n_stocks")],
        ["总资产", acc.get("total_asset")],
        ["投入本金", acc.get("principal")],
        ["总盈亏", acc.get("total_pnl")],
        ["可用现金", acc.get("cash")],
        ["门槛档位", ctx.get("tier_text")],
        ["ETF优先", ctx.get("etf_first_text")],
        ["A/B/C 信号数", f"{len(ctx.get('signals_A') or [])}/{len(ctx.get('signals_B') or [])}/{len(ctx.get('signals_C') or [])}"],
        ["持仓处置可释放资金", (ctx.get("holdings") or {}).get("cash_release")],
        ["风控违规数", len((ctx.get("risk") or {}).get("violations") or [])],
        ["数据起点", meta.get("start")],
        ["数据终点", meta.get("end")],
    ]
    _write(ws, ["项目", "值"], rows, widths=[26, 70])

    # 信号表
    for mode, title in (("A", "A短线"), ("B", "B波段"), ("C", "C长线")):
        ws = wb.create_sheet(title)
        sig = ctx.get(f"signals_{mode}") or []
        rows = [[s["code"], s["name"], s.get("sector"), s.get("close"), s.get("chg_pct"),
                 s.get("score_text"), s.get("why"), s.get("trade_plan"), s.get("stop_text"),
                 s.get("take_text"), s.get("hold_text")] for s in sig]
        _write(ws, ["代码", "名称", "板块", "收盘", "当日涨跌%", "评分", "通过关键条件", "交易计划",
                    "止损", "止盈", "持有上限"], rows,
               widths=[10, 12, 10, 10, 11, 10, 52, 40, 26, 26, 16])

    # 持仓处置
    ws = wb.create_sheet("持仓处置")
    hp = ctx.get("holdings") or {}
    rows = [[r["code"], r["name"], int(r["shares"]), r["cost"], r["price"], r["pnl_pct"],
             r["value"], r["pct_of_total"], r["action"], r["sell_shares"], r["keep_shares"],
             r["release_cash"], r["stop_px"], r["trigger"], r["reason"]] for r in hp.get("rows") or []]
    _write(ws, ["代码", "名称", "股数", "成本", "现价", "盈亏%", "市值", "占总资产%", "手册动作",
                "建议卖出股数", "保留股数", "释放资金", "止损价", "触发条件", "手册理由"], rows,
           widths=[10, 12, 9, 10, 10, 10, 12, 12, 12, 13, 11, 12, 10, 26, 40])

    # 四层配置
    ws = wb.create_sheet("四层配置")
    rows = [[r["name"], r["target_pct"] / 100, r["target_value"], r["actual_pct"] / 100,
             r["actual_value"], r["gap_value"], r["content"], r["clause"]]
            for r in ctx.get("layers") or []]
    _write(ws, ["层", "目标占比", "目标金额", "当前占比", "当前金额", "缺口", "对应标的", "手册条款"],
           rows, widths=[18, 10, 14, 10, 14, 14, 46, 10])
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row, min_col=2, max_col=2):
        for c in row:
            c.number_format = "0.0%"
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row, min_col=4, max_col=4):
        for c in row:
            c.number_format = "0.0%"

    # 板块配比
    ws = wb.create_sheet("板块配比")
    rows = [[r["name"], r["target_pct"] / 100, r["actual_pct"] / 100, r["gap_pct"] / 100]
            for r in ctx.get("sector_alloc") or []]
    _write(ws, ["板块", "目标", "当前", "缺口"], rows, widths=[14, 10, 10, 10])
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row, min_col=2, max_col=4):
        for c in row:
            c.number_format = "0.0%"

    # 放量诊断
    ws = wb.create_sheet("放量诊断")
    rows = [[v["code"], v["name"], v.get("chg_pct"), v.get("vol_ratio"), v.get("amt_ratio"),
             v.get("turnover"), v["level"], "是" if v["true_volume"] else "否"]
            for v in ctx.get("volume_diag") or []]
    _write(ws, ["代码", "名称", "涨跌%", "量倍数", "额倍数", "换手%", "放量等级", "真放量"], rows,
           widths=[10, 12, 10, 10, 10, 10, 26, 10])

    # 规则明细（全池 A/B/C + 每个 check）
    ws = wb.create_sheet("规则明细")
    rows = []
    for s in ctx.get("all_stocks") or []:
        rows.append([s["code"], s["name"], s.get("sector"), s.get("close"), s.get("chg_pct"),
                     s.get("pe_ttm"), s.get("pe_pct_5y"), s.get("from_hi_1y"),
                     s.get("ma20_slope"), s.get("vol_ratio"),
                     s.get("A_pass"), s.get("A_score"), s.get("A_fail"),
                     s.get("B_pass"), s.get("B_score"), s.get("B_fail"),
                     s.get("C_pass"), s.get("C_score"), s.get("C_fail")])
    _write(ws, ["代码", "名称", "板块", "收盘", "涨跌%", "PE", "PE分位%", "距1年高%", "MA20斜率%",
                "量倍数", "A通过", "A评分", "A未通过条件", "B通过", "B评分", "B未通过条件",
                "C通过", "C评分", "C未通过条件"], rows,
           widths=[10, 12, 9, 10, 9, 9, 10, 10, 11, 9, 8, 8, 46, 8, 8, 46, 8, 8, 46])

    # 冲突与说明
    ws = wb.create_sheet("口径冲突")
    rows = [[c["topic"], c["a"], c["b"], c["chosen"]] for c in C.CONFLICTS]
    _write(ws, ["议题", "写法A", "写法B", "本系统采用"], rows, widths=[22, 40, 46, 34])

    ws = wb.create_sheet("说明")
    _write(ws, ["条目", "内容"], [
        ["免责声明", C.DISCLAIMER],
        ["手册版本", "我的交易模式与纪律手册 v1.2（2026-10-20）"],
        ["生成程序", "Stock/run_daily.py + quant/rules.py（逐条标注 § 条款）"],
        ["数据源", "腾讯前复权日线；东财估值/财务/分红；新浪全A快照与行业映射"],
        ["风险提示", "回测与信号均基于历史数据，不构成投资建议；实盘请自行复核价格与公告"],
    ], widths=[18, 110])

    _ensure_dir(os.path.dirname(path))
    wb.save(path)
    return path


# ============================================================ DOCX


def _set_cell_font(cell, size=9, bold=False):
    from docx.oxml.ns import qn
    from docx.shared import Pt
    for para in cell.paragraphs:
        for run in para.runs:
            run.font.size = Pt(size)
            run.font.bold = bold
            run.font.name = "微软雅黑"
            run._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")


def _docx_table(doc, headers, rows, widths_cm=None):
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm

    t = doc.add_table(rows=1, cols=len(headers))
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t.autofit = False
    # fixed layout + tblGrid（python-docx 不设 tblGrid 时列宽不生效）
    tblPr = t._tbl.tblPr
    layout = OxmlElement("w:tblLayout")
    layout.set(qn("w:type"), "fixed")
    tblPr.append(layout)
    if widths_cm:
        tblW = OxmlElement("w:tblW")
        tblW.set(qn("w:type"), "dxa")
        tblW.set(qn("w:w"), str(int(sum(widths_cm) * 567)))
        tblPr.append(tblW)
        grid = t._tbl.tblGrid
        for gc, w in zip(grid.findall(qn("w:gridCol")), widths_cm):
            gc.set(qn("w:w"), str(int(w * 567)))

    hdr = t.rows[0]
    for i, h in enumerate(headers):
        c = hdr.cells[i]
        c.text = str(h)
        _set_cell_font(c, 8.5, True)
        if widths_cm:
            c.width = Cm(widths_cm[i])
    # 表头跨页重复
    trPr = hdr._tr.get_or_add_trPr()
    el = OxmlElement("w:tblHeader")
    el.set(qn("w:val"), "true")
    trPr.append(el)

    for r in rows:
        row = t.add_row()
        # 禁止单行跨页断成两半（配合表头重复，避免续表出现空壳行）
        _trPr = row._tr.get_or_add_trPr()
        _cs = OxmlElement("w:cantSplit")
        _cs.set(qn("w:val"), "true")
        _trPr.append(_cs)
        for i, v in enumerate(r):
            c = row.cells[i]
            c.text = "" if v is None else str(v)
            _set_cell_font(c, 8.5)
            if widths_cm:
                c.width = Cm(widths_cm[i])
    return t


def build_docx(ctx: dict, path: str) -> str:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor

    doc = Document()
    sec = doc.sections[0]
    sec.left_margin = sec.right_margin = Cm(1.8)
    sec.top_margin = sec.bottom_margin = Cm(1.8)

    style = doc.styles["Normal"]
    style.font.name = "微软雅黑"
    style.font.size = Pt(9.5)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")

    h = doc.add_heading(f"量化交易日报 · {ctx.get('date', '')}", level=0)
    h.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p = doc.add_paragraph("依据《我的交易模式与纪律手册 v1.2》自动生成 · 仅用于个人纪律执行与规则验证 · 不构成投资建议")
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.runs[0].font.size = Pt(8)
    p.runs[0].font.color.rgb = RGBColor(0x80, 0x80, 0x80)

    # 0 结论
    doc.add_heading("一、今天先说结论", level=1)
    for line in ctx.get("headline") or []:
        doc.add_paragraph(line, style="List Bullet")

    # 1 账户
    doc.add_heading("二、账户现状与门槛档位（§一 / §四）", level=1)
    acc = ctx.get("account") or {}
    _docx_table(doc, ["总资产", "本金", "总盈亏", "可用现金"],
                [[_n(acc.get("total_asset")), _n(acc.get("principal")),
                  _n(acc.get("total_pnl")), _n(acc.get("cash"))]], [4, 4, 4, 4])
    doc.add_paragraph(f"门槛档位：{ctx.get('tier_text', '')}")
    doc.add_paragraph(f"ETF 优先：{ctx.get('etf_first_text', '')}")

    # 2 四层
    doc.add_heading("三、四层资产配置（§二）", level=1)
    rows = [[r["name"], f"{r['target_pct']:.0f}%", _n(r["target_value"], 0),
             f"{r['actual_pct']:.1f}%", _n(r["gap_value"], 0)] for r in ctx.get("layers") or []]
    _docx_table(doc, ["层", "目标", "目标金额", "当前", "缺口"], rows, [6, 2.4, 3.2, 2.4, 3.2])

    # 3 持仓处置
    doc.add_heading("四、现有持仓处置（§七）", level=1)
    hp = ctx.get("holdings") or {}
    rows = [[r["code"], r["name"], int(r["shares"]), _n(r["cost"], 3), _n(r["price"]),
             _p(r["pnl_pct"]), f"{r['pct_of_total']:.1f}%", r["action"],
             f"{r['sell_shares']}股" if r["sell_shares"] else "—", r["trigger"]]
            for r in hp.get("rows") or []]
    _docx_table(doc, ["代码", "名称", "股数", "成本", "现价", "盈亏", "占比", "动作", "卖出", "触发条件"],
                rows, [1.8, 2.0, 1.4, 1.7, 1.6, 1.7, 1.4, 1.8, 1.6, 5.0])
    if hp.get("same_chain_warning"):
        p = doc.add_paragraph(hp["same_chain_warning"])
        p.runs[0].font.color.rgb = RGBColor(0xC0, 0x00, 0x00)
    if hp.get("cash_release"):
        doc.add_paragraph(f"按手册执行可释放资金约 {_n(hp['cash_release'], 0)} 元，用于第 4 步建 ETF 底仓。")

    # 4 信号
    doc.add_heading("五、今日信号（A/B/C 三套模式）", level=1)
    for mode, title in (("A", "A 短线（§10.1 / §10.2）"), ("B", "B 中线波段（§10.3）"),
                        ("C", "C 长线（§10.4）")):
        doc.add_heading(title, level=2)
        sig = ctx.get(f"signals_{mode}") or []
        if not sig:
            doc.add_paragraph("今日无满足全部条件的标的（不勉强就是纪律）。")
            continue
        rows = [[s["code"], s["name"], _n(s.get("close")), _p(s.get("chg_pct")),
                 s.get("score_text"), s.get("why"), s.get("trade_plan")] for s in sig]
        _docx_table(doc, ["代码", "名称", "收盘", "当日", "评分", "通过的关键条件", "交易计划"],
                    rows, [1.7, 2.0, 1.6, 1.7, 1.6, 6.2, 4.0])

    # 5 放量
    doc.add_heading("六、今日放量诊断（§9.3）", level=1)
    vd = ctx.get("volume_diag") or []
    if vd:
        rows = [[v["code"], v["name"], _p(v.get("chg_pct")), _n(v.get("vol_ratio")),
                 _n(v.get("amt_ratio")), _n(v.get("turnover")), v["level"],
                 "✅" if v["true_volume"] else "—"] for v in vd[:20]]
        _docx_table(doc, ["代码", "名称", "涨跌", "量倍数", "额倍数", "换手%", "放量等级", "真放量"],
                    rows, [1.7, 2.0, 1.7, 1.6, 1.6, 1.6, 4.2, 1.6])
    else:
        doc.add_paragraph("暂无数据。")

    # 6 修复路线 + 风控
    doc.add_heading("七、修复路线与风控体检（§四 / §五 / §七）", level=1)
    for i, step in enumerate(C.EXECUTION_ORDER, 1):
        doc.add_paragraph(step, style="List Number")
    rk = ctx.get("risk") or {}
    for v in rk.get("violations") or []:
        doc.add_paragraph(f"【{v['rule']}】{v['text']} → {v['fix']}", style="List Bullet")

    # 7 冲突
    doc.add_heading("八、手册口径冲突及取舍", level=1)
    rows = [[c["topic"], c["a"], c["b"], c["chosen"]] for c in C.CONFLICTS]
    _docx_table(doc, ["议题", "写法A", "写法B", "采用"], rows, [3.2, 5.2, 5.2, 3.4])

    # 8 免责
    doc.add_heading("九、免责声明", level=1)
    p = doc.add_paragraph(C.DISCLAIMER)
    p.runs[0].font.size = Pt(8)
    p.runs[0].font.color.rgb = RGBColor(0x80, 0x80, 0x80)

    _ensure_dir(os.path.dirname(path))
    doc.save(path)
    return path


def write_all(ctx: dict) -> dict[str, str]:
    """按 ctx['date'] 生成 md / xlsx / docx 三份文件，返回路径。"""
    d = ctx.get("date") or "latest"
    _ensure_dir(C.OUT)
    md_path = os.path.join(C.OUT, f"信号_{d}.md")
    xlsx_path = os.path.join(C.OUT, f"信号_{d}.xlsx")
    docx_path = os.path.join(C.OUT, f"量化交易日报_{d}.docx")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(build_md(ctx))
    build_xlsx(ctx, xlsx_path)
    build_docx(ctx, docx_path)
    return {"md": md_path, "xlsx": xlsx_path, "docx": docx_path}
