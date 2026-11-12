# -*- coding: utf-8 -*-
"""手册 v1.2 的全部阈值 / 账户参数 / 路径常量 —— 唯一真值源（Single Source of Truth）。

约定：
- 本文件里每一条常量都标注手册条款号（§），改规则必须同时改这里的注释与 docs/规则映射表.md。
- 百分比一律用「%数值」形式表达（-3.0 表示 −3%），仓位比例用 0~1 小数（0.15 表示 15%）。
- 手册内部有冲突时，采用更严格的一侧，并在 CONFLICTS 里登记（报告会显示）。
"""
from __future__ import annotations

import os

# ---------------------------------------------------------------- 路径
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(ROOT, "out")
DOCS = os.path.join(ROOT, "docs")
BARS = os.path.join(DATA, "bars")
BARS_LONG = os.path.join(DATA, "bars_long")
ADJ = os.path.join(DATA, "adj")
VAL = os.path.join(DATA, "val")
FIN = os.path.join(DATA, "fin")
SNAP = os.path.join(DATA, "snapshot.json")
POOL = os.path.join(DATA, "pool.json")

# ---------------------------------------------------------------- 账户（§一、①结论快照 2026-10-15）
ACCOUNT = {
    "principal": 122400.0,      # 投入本金约 12.24 万（§一.1）
    "total_asset": 83479.01,    # 当前总资产（旧工作簿快照）
    "market_value": 81795.0,
    "cash": 1684.01,
    "total_pnl": -38938.61,
}
# 账户/持仓快照日期：持仓股数与成本来自旧工作簿这一天。
# 若这一天之后有买卖或除权除息，程序做两件事：
#   ① 总资产每天按「现金 + 当日收盘市值」重算（portfolio.refresh_account）
#   ② 持仓成本价按 data/adj 里的除权除息事件做调整（portfolio.adjust_holdings_for_events）
# 但「股数变了」这类真实交易信息无法自动得知，需要用户自己改 data/pool_source.json。
ACCOUNT_ASOF = "2026-10-15"

# 现有五只持仓（§七 + 旧工作簿 ⑤现有持仓处置）
HOLDINGS = [
    {"code": "600183", "name": "生益科技", "shares": 200, "cost": 113.316, "action": "持有",
     "reason": "覆铜板/PCB，AI 算力链，五只里唯一盈利（§七）",
     "trigger": "跌破 108（约100日线）减半，不破不动", "stop_level": 108.0, "delta_pct": 0.0},
    {"code": "603986", "name": "兆易创新", "shares": 100, "cost": 490.742, "action": "减 1/3",
     "reason": "存储设计，亏损43.4%，仓位33.3%严重超限（§七）",
     "trigger": "反弹到 300~320 执行", "stop_level": None, "delta_pct": -1 / 3},
    {"code": "000021", "name": "深科技", "shares": 700, "cost": 45.983, "action": "减 1/3",
     "reason": "存储封测，与兆易同涨同跌，同一笔风险下注两次（§七）",
     "trigger": "28~30 执行", "stop_level": None, "delta_pct": -1 / 3},
    {"code": "002156", "name": "通富微电", "shares": 200, "cost": 73.784, "action": "减 1/2",
     "reason": "封测，AMD 链弹性大，但同链条已三重暴露（§七）",
     "trigger": "49~52 减半；跌破 45 直接止损", "stop_level": 45.0, "delta_pct": -0.5},
    {"code": "002185", "name": "华天科技", "shares": 100, "cost": 19.788, "action": "清仓",
     "reason": "封测三线，仓位仅1.7%，留着没有意义（§七）",
     "trigger": "直接卖出（约1417元）", "stop_level": None, "delta_pct": -1.0},
]

# 执行顺序（§七 执行顺序）
EXECUTION_ORDER = [
    "第1步：清掉华天科技，止损最弱的一环，换出子弹",
    "第2步：兆易/深科技/通富 各减 1/3，任一只反弹即执行，不要等回本",
    "第3步：减出来的资金先放着，等半导体链止跌信号（连续3日缩量 + 板块指数站上20日线）",
    "第4步：按「宽基ETF → 红利ETF → 个股」的顺序建底仓",
    "第5步：任何新买入，单只 ≤ 总资产 20%，同板块合计 ≤ 40%",
]

# ---------------------------------------------------------------- 四层资产（§二）
LAYERS = [
    {"key": "core", "name": "核心仓（地基）", "pct": 0.45, "clause": "§二",
     "content": "宽基ETF 510300 / 510500", "rule": "只买不卖；每月结余定投；下跌不加杠杆"},
    {"key": "dividend", "name": "守息仓（压舱石）", "pct": 0.15, "clause": "§二",
     "content": "红利ETF 510880 / 515080", "rule": "股息率>4%才加，否则持有不动"},
    {"key": "satellite", "name": "卫星仓（进攻）", "pct": 0.25, "clause": "§二",
     "content": "ETF 15%：512480 / 588000；个股 10%，最多 2 只", "rule": "单只≤8%，破位必减"},
    {"key": "cash", "name": "现金仓（子弹）", "pct": 0.15, "clause": "§二",
     "content": "短融ETF 511360 / 货币基金", "rule": "永远不空仓；只在恐慌时打出去"},
]

ETF_ROLES = {  # §二 + §八
    "510300": ("core", "沪深300ETF"), "510500": ("core", "中证500ETF"),
    "510880": ("dividend", "红利ETF"), "515080": ("dividend", "中证红利ETF"),
    "512480": ("satellite", "半导体ETF"), "588000": ("satellite", "科创50ETF"),
    "511360": ("cash", "短融ETF"),
}
INDEX_LIST = {  # market_ctx 用
    "000300": ("sh000300", "沪深300"),
    "000001": ("sh000001", "上证指数"),
    "399006": ("sz399006", "创业板指"),
}
MARKET_INDEX = "000300"  # §10.3「大盘跌破MA20」用沪深300判定

# ---------------------------------------------------------------- 仓位纪律（§五）
POSITION_LIMITS = {
    "single_stock_cap": 0.20,       # §五① 单只个股 ≤ 总资产 20%
    "single_stock_cap_now": 0.10,   # §五①/§四 现阶段（总资产 5~10 万）个股仓位上限 10%
    "single_sector_cap": 0.40,      # §五② 同一板块合计 ≤ 40%
    "min_cash": 0.15,               # §五③ 现金/短融 ≥ 15%
    "no_averaging_down": True,      # §五④ 止损先于补仓，绝不向下补仓
    "no_add_short_term": True,      # §五⑤ 短线仓不加仓
    "must_write_stop_first": True,  # §五⑥ 买入前先写下止损价
    "must_log": True,               # §五⑦ 每笔必须记日志
}
# §四 门槛刻度：按总资产决定能配几只个股
TIER_TABLE = [
    {"asset_max": 50000, "single_cap": 0.0, "n_stocks": 0, "verdict": "不买个股，全买ETF"},
    {"asset_max": 100000, "single_cap": 0.10, "n_stocks": 2, "verdict": "个股≤10%，只买1~2只，优先低价股"},
    {"asset_max": 200000, "single_cap": 0.20, "n_stocks": 3, "verdict": "可以正式配置个股，建议3只"},
    {"asset_max": 500000, "single_cap": 0.20, "n_stocks": 6, "verdict": "4~6只个股+宽基ETF打底"},
    {"asset_max": 1e12, "single_cap": 0.20, "n_stocks": 8, "verdict": "8只以上，仍需ETF底仓"},
]
OPEN_FIRST_STOCK_ASSET = 100000.0   # §四 总资产到 10 万再开第一个个股仓
ETF_FIRST_TARGET = 45000.0          # §四 ⭐ 先把ETF底仓建到4.5万以上

# ---------------------------------------------------------------- 板块配比三档（§五）
SECTOR_PROFILES = {
    "保守": {"finance": 0.30, "consume": 0.30, "tech": 0.15, "other": 0.05, "cash": 0.20},
    "均衡": {"finance": 0.25, "consume": 0.25, "tech": 0.25, "other": 0.05, "cash": 0.20},
    "进取": {"finance": 0.15, "consume": 0.20, "tech": 0.40, "other": 0.10, "cash": 0.15},
}
DEFAULT_PROFILE = "均衡"  # 账户现处「修复期」，默认均衡档

# ---------------------------------------------------------------- 三套模式（§三）
MODES = {
    "A": {
        "name": "A 短线（题材脉冲）", "clause": "§三/§九/§10.1/§10.2",
        "hold_days": (1, 5),
        "sector_pool": ["券商", "军工", "地产", "农业"],
        "stop_pct": -3.0,          # §9.5 −3% 硬止损
        "take_profit_pct": 8.0,    # §9.5 +6~8% 全部卖出（取上沿做止盈，下沿作为减半）
        "take_profit_half_pct": 6.0,
        "single_cap": 0.10,        # §三 单只 ≤10%
        "bucket_cap": 0.20,        # §三 总短线仓 ≤ 20%
        "satellite_share": 1 / 3,  # §三 用卫星仓的 1/3
        "max_hold_days_hard": 5,
        "exit_rules": ["−3% 硬止损", "+6~8% 全部卖出", "收盘破5日线也走", "3天不涨走"],
    },
    "B": {
        "name": "B 中线波段（景气度）", "clause": "§三/§9.5/§10.3",
        "hold_days": (14, 90),
        "stop_pct": None,          # 用均线止损
        "single_cap": 0.15,        # §三 单只≤15%（§10.5 写≤20%，冲突登记见 CONFLICTS）
        "take_profit_pct": 30.0,
        "exit_rules": ["跌破MA20减半", "跌破MA60清仓", "+20~30%减1/3", "破5日线先减1/3", "3个月不动换股"],
    },
    "C": {
        "name": "C 长线持有（确定性）", "clause": "§三/§9.5/§10.4",
        "hold_days": (180, 730),
        "stop_pct": None,          # 逻辑止损，不看价格
        "single_cap": 0.20,
        "exit_rules": ["逻辑止损：季报连续2季下滑/行业逻辑破坏/立案造假", "PE分位>80%减半，>90%清仓", "每年复核1次"],
    },
}
# 手册内部冲突登记
CONFLICTS = [
    {"topic": "B 模式单只上限", "a": "§三 表：单只 ≤15%", "b": "§10.5 表：单只上限 ≤20%",
     "chosen": "取 15%（更严格）"},
    {"topic": "C 模式入场估值条件", "a": "§三 表：PE 近5年分位 <20%", "b": "§10.4 表：<30% 进入买入区",
     "chosen": "取 <30% 为买入区、<20% 为加仓区（§10.4 更细）"},
    {"topic": "B 模式「波段四条件」口径", "a": "§10.3 四信号（MA20站上/回踩/箱体突破/板块共振）",
     "b": "§10.6 实盘快照口径（站上MA20+站上MA60+MA20上翘+MA60上翘）",
     "chosen": "两套都实现：主口径 §10.3；SNAPSHOT_MODE 复刻 §10.6 以便与旧报告对账"},
]

# ---------------------------------------------------------------- §九 放量
VOLUME = {
    "multiple_warm": 1.2,      # §9.1② 1.2~1.5 温和
    "multiple_clear": 1.5,     # §9.1② 1.5~2 明显（§9.3① 阈值）
    "multiple_strong": 2.0,    # §9.1② 2~3 强势
    "multiple_abnormal": 3.0,  # §9.1② >3 异常
    "amount_min_yi": 5.0,      # §9.3② 成交额 ≥5 亿
    "amount_multiple_min": 1.5,  # §9.1③ 今日额 ÷ 前5日均额 ≥1.5
    "turnover_low": 3.0,       # §9.3③ 换手率 3%~15%
    "turnover_high": 15.0,
    "turnover_healthy_high": 8.0,   # §9.1④ 3~8% 健康
    "turnover_hot": 20.0,      # §9.1④ >20% 妖股（排除项）
    "ratio_intraday_min": 1.5,  # §9.1① 量比 >1.5（仅盘中可用，10:00 后才有效）
}

# ---------------------------------------------------------------- 三档止损止盈（§9.5 / §9.6）
STOP_TAKE = {
    "A": {"stop": -3.0, "take_full": 8.0, "take_half": 6.0, "breakeven_winrate": 35.3},
    "B": {"stop_ma": 20, "stop_ma_clear": 60, "take_trim": 30.0, "take_trim_low": 20.0},
    "C": {"pe_pct_trim": 80.0, "pe_pct_clear": 90.0},
}

# ---------------------------------------------------------------- §十 入场规则
A_SCREEN = {  # §10.1
    "sector_top_n": 5,                 # 只在当天涨幅前5的板块里选
    "sector_min_chg_pct": 1.0,         # 板块指数涨 >1%
    "vol_multiple_min": 1.5,           # 今日量比/近3日成交额放大 >1.5倍
    "vol_ratio_3d_min": 1.5,
    "require_above_ma5": True,         # 收盘>MA5 且 MA5>前一日
    "ma5_rising": True,
    "from_hi_1y_max": -30.0,           # 距1年高点 <-30% ....或者刚突破平台（二选一）
    "mktcap_min_yi": 50.0, "mktcap_max_yi": 800.0,
    "price_min": 5.0, "price_max": 50.0,
    # 排除项（一条命中即划掉）
    "exclude_st": True, "exclude_delisting": True, "exclude_loss_2y": True,
    "exclude_ret5_gt": 25.0, "exclude_reduce_announce": True, "exclude_turnover_gt": 20.0,
}
A_SECTOR = {  # §10.1.1 板块 3 天验证（2026-11-12 用户补充；判定"真启动"而不是"一日游"）
    "cum3_min": 3.0,            # 3日累计涨幅 <+3% → 没启动，不进
    "cum3_max": 10.0,           # +3%~+10% → 刚启动甜区，可做
    "cum3_heavy_max": 15.0,     # +10%~+15% → 已走一半，只能轻仓 + 止损收紧到 -2%
    "light_stop_pct": -2.0,     # 轻仓档的硬止损
    "rank_top_n": 5,            # 今天排名 vs 3个交易日前：上升、或稳定在前5
    "rank_lookback": 3,
    "limit_up_min": 2,          # 板块内涨停家数 ≥2
    "top10_days_min": 2,        # 过去3天有几天在涨幅前10，≥2天
    "top10_window": 3,
    "breadth_min": 2.0,         # 板块内涨家数÷跌家数 ≥2 → 真普涨
    "breadth_leader_min": 1.0,  # 1~2 → 结构行情，只做龙头
    "buy_amount_rank_max": 3,   # 通过验证后只买「板块内成交额排名前3」
    "watch_amount_rank_max": 8, # 4~8 跟风股：不买
    "sector_top_n": 5,          # 当日涨幅榜前5（§10.1 第1步）
    # 样本口径说明（2026-11-12）：池子只有 154 只，东财行业分组后一个行业常只有 3~19 只，
    # 但手册的「涨停家数/涨跌家数」原本是按**全行业全部成分股**统计的。
    # 在薄样本上要求「涨停≥2 只」会把信号压到几乎为 0（2024-09~2026-11 全窗口仅 12 个）。
    # 因此这两项默认作为「强度参考」（仍展示、仍计分），不阻断；扩池后可把 soft 置空恢复严格。
    "soft_checks": ["S3", "S5"],
}

A_TIMING = {  # §10.2 四个时点（日线回测只能近似 14:45~14:57 收盘确认）
    "no_trade_before": "09:45",
    "batch1": ("10:00", "11:00", "回踩分时均线(黄线)不破+缩量 → 买第1笔"),
    "batch2": ("13:30", "14:00", "突破上午高点+放量 → 加第2笔"),
    "batch3": ("14:45", "14:57", "收盘确认收阳、站稳5日线 → 买第3笔（最稳）"),
    "never_buy": ["一字板追", "已涨7%以上追", "破分时均线还接"],
    "max_chase_pct": 7.0,              # 已涨 7% 以上不追
}
B_SIGNALS = {  # §10.3 四信号至少 2 个，①必选
    "required": ["above_ma20_and_ma20_flat_or_up"],
    "signals": {
        "above_ma20_and_ma20_flat_or_up": "① 收盘>MA20 且 MA20 走平或上翘（必选）",
        "pullback_ma20_hold": "② 回踩 MA20 不破、止跌收阳（强势股首选买点）",
        "box_break_volume": "③ 放量突破近60日箱体上沿（量>1.5倍）",
        "sector_resonance": "④ 所属板块同时站上 MA20",
    },
    "min_signals_ok": 2,
    "box_window": 60,
    "box_break_vol_multiple": 1.5,
    "donot_buy": ["MA20向下", "大盘跌破MA20", "板块一日游无连续性"],
}
B_SNAPSHOT = {  # §10.6 快照口径（4 条件全满足）
    "checks": ["收盘>MA20", "收盘>MA60", "MA20上翘", "MA60上翘"],
    "ma_up_window": 5,
}
C_ENTRY = {  # §10.4
    "pe_pct_max": 30.0,         # 近5年PE分位 <30%
    "pe_pct_heavy": 20.0,       # 越低越好（加仓区）
    "div_yield_min": 4.0,       # 股息率 >4%（银行/红利类）
    "or_roe_min": 10.0, "or_pb_max": 1.5,   # 或 ROE>10% 且 PB<1.5
    "profit_years_min": 5,      # 连续 5 年盈利
    "debt_max": 70.0,           # 资产负债率 <70%
    "from_hi_1y_max": -20.0,    # 距1年高点 <-20%
    "batches": 5, "batch_pct": 0.20,
}

# ---------------------------------------------------------------- §八 估值参考线
# (细分板块, PE/PB 建仓, 重仓, 减仓, 备注)
VALUATION_LINES = [
    {"sector": "高端白酒", "build": ("PE", "<", 25), "heavy": ("PE", "<", 20), "cut": ("PE", ">", 40)},
    {"sector": "调味品/食品", "build": ("PE", "<", 25), "heavy": ("PE", "<", 20), "cut": ("PE", ">", 45)},
    {"sector": "家电", "build": ("PE", "<", 15), "heavy": ("PE", "<", 12), "cut": ("PE", ">", 25)},
    {"sector": "乳品/啤酒", "build": ("PE", "<", 22), "heavy": ("PE", "<", 18), "cut": ("PE", ">", 35)},
    {"sector": "中药/医药商业", "build": ("PE", "<", 22), "heavy": ("PE", "<", 18), "cut": ("PE", ">", 40)},
    {"sector": "银行", "build": ("PB+股息", "<", 0.7), "heavy": ("PB", "<", 0.6), "cut": ("PB", ">", 1.2),
     "note": "PB<0.7 且股息率>5%；银行不要用PE判断"},
    {"sector": "保险", "build": ("PB或PEV", "<", 1.0), "heavy": ("PB", "<", 0.8), "cut": ("PB", ">", 1.8)},
    {"sector": "券商", "build": ("PB", "<", 1.2), "heavy": ("PB", "<", 1.0), "cut": ("PB", ">", 2.5)},
    {"sector": "半导体", "build": ("PEG", "<", 1.0), "heavy": ("PEG", "<", 0.8), "cut": ("PEG", ">", 2.0),
     "note": "PE 高时买、低时卖，反着用"},
    {"sector": "宽基ETF(510300)", "build": ("指数PE分位", "<", 40), "heavy": ("指数PE分位", "<", 20),
     "cut": ("指数PE分位", ">", 80)},
]
PEG_BANDS = {"cheap": 0.8, "fair": 1.2, "bubble": 2.0}  # §八 通用铁律

# 板块（精选池口径）→ 估值锚
SECTOR_VALUATION_ANCHOR = {
    "finance": "PB+股息率+ROE（不看PE）",
    "consume": "PE 近5年历史分位",
    "tech": "PEG=PE÷净利润增速",
    "other": "PE 反着用（高PE买、低PE卖）",
}

# ---------------------------------------------------------------- 回测/事件研究参数
BACKTEST = {
    "fee_bps": 2.5, "slip_bps": 5.0,
    "init_cash": 100000.0,
    "max_positions": 5,
    "cooldown_days": 3,
    "lot": 100,
    "max_chase_pct": 7.0,        # §10.2 已涨7%以上不追（T+1 开盘高开>7% 放弃）
    "limit_up_pct": 9.8,         # 近似涨跌停（主板10%，创业板/科创板20% 需单独参数）
}
EVENT_HORIZONS = (1, 3, 5, 10, 20, 40, 60)
EVENT_BASE = "all_days"   # 基线：同一股票全部交易日的前瞻收益均值

DISCLAIMER = (
    "本文件由程序依据《个人交易模式与纪律手册 v1.2》自动生成，仅用于个人纪律执行与规则验证，"
    "不构成投资建议。编制人非持牌投资顾问；行情/财务数据来自公开网页接口，可能存在延迟、缺失或错误。"
    "回测结论基于历史数据，不代表未来收益；实盘请自行复核价格与公告。"
)
