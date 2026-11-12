# -*- coding: utf-8 -*-
"""快速刷新：只补当天日线 + 实时行情，重算信号和看板（约 6~10 秒）。

老版 `1-电脑看板.bat` 走全量 build_data（含 2000 行长历史、复权因子自检、全 A 快照），
在 150+ 只股票上要 45 秒左右，这是「电脑看板加载半天」的真正原因。

首次运行（data/val 缺失）会自动退回全量构建；之后都走快路径。
新闻/公告类数据（减持、ST 名单）不在快速路径里刷新，需要时再跑全量 tools/build_data.py。
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools import build_data  # noqa: E402


def _has_data() -> bool:
    for sub in ("bars", "val", "fin"):
        d = os.path.join(ROOT, "data", sub)
        if not os.path.isdir(d) or not os.listdir(d):
            return False
    return True


def main(argv=None) -> int:
    t0 = time.time()
    full = not _has_data()
    if full:
        print("[quick] 第一次运行，做一次全量构建（会比较慢，之后都很快）…")
        rc = build_data.main([])
    else:
        print("[quick] 快速刷新：日线 + 实时行情（约 6~10 秒）…")
        rc = build_data.main(["--skip-long", "--skip-adj", "--skip-snapshot"])
    if rc not in (0, None):
        print(f"[quick] 数据步骤返回 {rc}，继续用现有缓存生成看板。")

    # 全 A 行业热度（§10.1.1 板块验证的数据源）：首次要全量抓行业+K线，之后只用实时行情补当天
    market = os.path.join(ROOT, "data", "industry_daily.json")
    stats = os.path.join(ROOT, "data", "market_bars.json")
    args = ["--quotes"] if os.path.exists(stats) else []
    if not os.path.exists(market):
        print("[quick] 第一次需要建立全 A 行业数据（约 3~6 分钟，之后每天只更新当天）…")
    try:
        import subprocess
        subprocess.run([sys.executable, os.path.join(ROOT, "tools", "build_market.py")] + args,
                       cwd=ROOT, check=False)
    except Exception as exc:  # noqa: BLE001
        print(f"[quick] 全 A 行业数据更新失败（用现有缓存继续）：{exc}")

    print("[quick] 重算信号 + 生成看板 …")
    old_argv = sys.argv
    try:
        sys.argv = ["run_daily.py", "--skip-docx"]
        import run_daily  # noqa: E402  (延迟导入，保证上面的 sys.path 生效)
        run_daily.main()
    finally:
        sys.argv = old_argv

    sec = time.time() - t0
    print(f"[quick] 完成，用时 {sec:.1f} 秒（{datetime.now():%H:%M:%S}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
