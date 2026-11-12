# -*- coding: utf-8 -*-
"""快速刷新：只补当天日线 + 实时行情，重算信号和看板（本机约 6~10 秒）。

老版 `1-电脑看板.bat` 走全量 build_data（含 2000 行长历史、复权因子自检、全 A 快照），
在 150+ 只股票上要 45 秒左右，这是「电脑看板加载半天」的真正原因。

三种运行方式：
- 本机默认：快速刷新（K 线缓存 10 分钟 TTL，通常直接命中）；
- `--cloud`：GitHub Actions 专用。已有 data/ 时**完全不抓历史 K 线**，
  只用 2~3 个实时行情请求把「今天这根」追加到 data/bars/*.json（云端一次 1~2 分钟）；
- 首次运行（data/ 为空）：自动做一次全量构建（云端约 10~15 分钟，之后都很快）。
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


def _bar_keys() -> list[str]:
    d = os.path.join(ROOT, "data", "bars")
    if not os.path.isdir(d):
        return []
    return sorted(f[:-5] for f in os.listdir(d) if f.endswith(".json"))


def _refresh_quote_bars() -> int:
    from quant import datafeed as DF  # 延迟导入，避免首次全量时重复 import

    keys = _bar_keys()
    if not keys:
        return 0
    return DF.refresh_quote_bars(keys)


def main(argv=None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    cloud = "--cloud" in argv
    t0 = time.time()
    full = not _has_data()
    if full:
        print("[quick] 第一次运行，做一次全量构建（比之后慢很多，只会发生一次）…")
        args = ["--skip-long"] if cloud else []
        rc = build_data.main(args)
    elif cloud:
        print("[quick] 云端快路径：只追加当天行情（不抓历史 K 线）…")
        try:
            added = _refresh_quote_bars()
            print(f"[quick] data/bars 追加 {added} 只的当日 bar")
        except Exception as exc:  # noqa: BLE001
            print(f"[quick] 追加当日 bar 失败（继续用现有 K 线）：{exc}")
        rc = 0
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
        print("[quick] 第一次需要建立全 A 行业数据（约 10~20 分钟，之后每天只更新当天）…")
    try:
        import subprocess
        subprocess.run([sys.executable, os.path.join(ROOT, "tools", "build_market.py")] + args,
                       cwd=ROOT, check=False)
    except Exception as exc:  # noqa: BLE001
        print(f"[quick] 全 A 行业数据更新失败（用现有缓存继续）：{exc}")

    # 全 A 选股（A/B/C 三模式从 5000+ 只里筛）；还没有行情数据时跳过
    try:
        if os.path.exists(os.path.join(ROOT, "data", "market_bars.json")):
            import subprocess
            print("[quick] 全 A 选股扫描 …")
            subprocess.run([sys.executable, os.path.join(ROOT, "tools", "screen_market.py"), "--quick"],
                           cwd=ROOT, check=False)
    except Exception as exc:  # noqa: BLE001
        print(f"[quick] 全 A 选股失败（看板用池内信号继续）：{exc}")

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
