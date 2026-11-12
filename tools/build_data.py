#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/build_data.py — 一条命令生成 data/ 全部数据文件（T1）。

用法（在项目根目录执行）:
    python tools/build_data.py                  # 全量：154 池股 + 7 ETF + 3 指数 + 快照
    python tools/build_data.py --limit 12       # 调试：只做前 12 只池股（ETF/指数仍做）
    python tools/build_data.py --skip-long      # 不写 bars_long（调试）
    python tools/build_data.py --skip-adj       # 跳过复权因子
    python tools/build_data.py --skip-snapshot  # 跳过新浪全 A 快照
    python tools/build_data.py --refresh        # 忽略 data/_cache，全部重抓
    python tools/build_data.py --check          # 构建后跑落盘结构自检（推荐）

产物: data/pool.json, data/bars/*.json, data/bars_long/*.json, data/adj/*.json,
      data/val/*.json, data/fin/*.json, data/snapshot.json, data/meta.json
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:  # Windows 控制台默认 GBK，强制 UTF-8 输出，避免中文 print 崩溃
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001 - 老环境无 reconfigure
    pass

from quant import datafeed  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成 data/ 全部数据文件（腾讯/东财/新浪）")
    parser.add_argument("--limit", type=int, default=0,
                        help="只做前 N 只池内个股（调试用；ETF/指数不受影响）")
    parser.add_argument("--skip-long", action="store_true", help="不写 bars_long（2000 行不复权）")
    parser.add_argument("--skip-adj", action="store_true", help="跳过 adj 复权因子")
    parser.add_argument("--skip-snapshot", action="store_true", help="跳过新浪全 A 快照")
    parser.add_argument("--refresh", action="store_true", help="忽略 data/_cache，全部重新抓取")
    parser.add_argument("--workers", type=int, default=8, help="并发线程数（默认 8）")
    parser.add_argument("--check", action="store_true", help="构建后运行落盘结构自检")
    args = parser.parse_args(argv)

    print("=" * 72)
    print(f"build_data  root={ROOT}")
    print(f"  limit={args.limit} skip_long={args.skip_long} skip_adj={args.skip_adj} "
          f"skip_snapshot={args.skip_snapshot} refresh={args.refresh} workers={args.workers}")
    print("=" * 72)

    try:
        meta = datafeed.build_all(
            limit=args.limit or None,
            skip_long=args.skip_long,
            skip_adj=args.skip_adj,
            skip_snapshot=args.skip_snapshot,
            refresh=args.refresh,
            workers=args.workers,
        )
    except Exception as exc:  # noqa: BLE001 - CLI 兜底
        print(f"[FATAL] {type(exc).__name__}: {exc}")
        return 2

    counts = meta.get("counts") or {}
    print("\n" + "=" * 72)
    print("构建完成 counts =", json.dumps(counts, ensure_ascii=False))
    print(f"built_at={meta.get('built_at')} trading_day={meta.get('trading_day')} "
          f"duration={meta.get('duration_sec')}s")
    warnings = meta.get("warnings") or []
    print(f"warnings={len(warnings)}  excluded={len(meta.get('excluded') or [])}")
    for msg in warnings[:25]:
        print("  [WARN]", msg)
    if len(warnings) > 25:
        print(f"  ... 其余 {len(warnings) - 25} 条见 data/meta.json")
    for item in (meta.get("excluded") or [])[:20]:
        print("  [EXCLUDED]", json.dumps(item, ensure_ascii=False))

    if args.check:
        result = datafeed.validate(limit=args.limit or None)
        print("\n" + "=" * 72)
        print("落盘自检:", "PASS" if result["ok"] else "FAIL")
        print("stats =", json.dumps(result["stats"], ensure_ascii=False))
        for err in result["errors"][:40]:
            print("  [FAIL]", err)
        if len(result["errors"]) > 40:
            print(f"  ... 其余 {len(result['errors']) - 40} 条")
        print("=" * 72)
        return 0 if result["ok"] else 1

    print("=" * 72)
    return 0 if counts.get("bars") else 1


if __name__ == "__main__":
    sys.exit(main())
