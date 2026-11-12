#!/usr/bin/env python3
"""测量数据处理入口：装载目录 -> 计算指标与一阶合成标准不确定度 -> 解释/导出。

用法：
    python3 main_metrology.py run --dir examples/metrology --db metrology.duckdb
    python3 main_metrology.py explain --db metrology.duckdb --specimen S1 --indicator Lsum
    python3 main_metrology.py export --db metrology.duckdb --out out/
    python3 main_metrology.py demo [--db metrology_demo.duckdb]

demo 使用 examples/metrology/ 的手工可算材料，覆盖：厘米换米、摄氏换开尔文、
相关长度和差、中间指标复用、x-x 归零、完全相关相减、非半正定拒收、
缺变量时无关指标仍有结果、逐样本拒收与公式局部失败（输出可追到输入行）。
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bitemp.metrology import service  # noqa: E402
from bitemp.metrology.inputs import ConfigFailure, InputFailure  # noqa: E402

EXAMPLE_DIR = Path(__file__).resolve().parent / "examples" / "metrology"


def _print_summary(summary: dict) -> None:
    print(f"\n== 运行汇总 ==")
    print(f"  specimen 共 {summary['specimens']} 个："
          f"正常 {summary['ok']}，拒收 {summary['rejected']}")
    print(f"  指标结果：ok {summary.get('indicators_ok', 0)}，"
          f"unavailable {summary.get('indicators_unavailable', 0)}，"
          f"error {summary.get('indicators_error', 0)}")


def _print_outcomes(summary: dict) -> None:
    indicator_map = summary["config"].indicator_map
    for specimen in sorted(summary["outcomes"]):
        outcome = summary["outcomes"][specimen]
        print(f"\n-- 标本 {specimen} --")
        if outcome.status == "rejected":
            print("  拒收:")
            for r in outcome.reject_reasons:
                loc = f"{r.file}:{r.line}" if r.line else r.file
                print(f"    - [{loc}] {r.message}")
            continue
        for note in outcome.notes:
            print(f"  注记: {note}")
        for name, res in outcome.results.items():
            if res.status == "ok":
                ind = indicator_map[name]
                value = ind.display.from_si(res.value_si)
                u = ind.display.u_from_si(res.u_si)
                print(f"  {name:<8} = {value:.10g} ± {u:.6g} {ind.unit_text}")
                for note in res.notes:
                    print(f"           注记: {note}")
            else:
                chain = " -> ".join(res.chain)
                print(f"  {name:<8} [{res.status}] {res.reason}（依赖链: {chain}）")


def cmd_run(args) -> int:
    try:
        summary = service.run(args.dir, args.db)
    except (ConfigFailure, InputFailure) as e:
        print(str(e), file=sys.stderr)
        return 2
    _print_summary(summary)
    print(f"  明细已落库: {args.db}（可用 explain/export 查看）")
    return 0


def cmd_explain(args) -> int:
    try:
        text = service.explain_text(args.db, args.specimen, args.indicator)
    except KeyError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(text)
    return 0


def cmd_export(args) -> int:
    written = service.export(args.db, args.out)
    print("已导出:")
    for path in written:
        print(f"  {path}")
    return 0


def cmd_demo(args) -> int:
    from bitemp.metrology import store
    print(f"== 装载示例目录 {EXAMPLE_DIR} ==")
    con = store.connect(args.db)
    try:
        summary = service.run(EXAMPLE_DIR, args.db, con=con)
    except (ConfigFailure, InputFailure) as e:
        print(str(e), file=sys.stderr)
        return 2
    _print_summary(summary)
    _print_outcomes(summary)

    print("\n== 解释示例：S1 / Lsum（两个相关长度之和） ==")
    print(service.explain_text(args.db, "S1", "Lsum", con=con))
    print("\n== 解释示例：S1 / Lagain（中间指标被反复引用，L2 的导数合并为 0） ==")
    print(service.explain_text(args.db, "S1", "Lagain", con=con))
    print("\n== 解释示例：S9 / Lratio（公式局部失败，可追到输入行） ==")
    print(service.explain_text(args.db, "S9", "Lratio", con=con))

    out_dir = Path(tempfile.mkdtemp(prefix="metrology_export_"))
    written = service.export(args.db, out_dir, con=con)
    print(f"\n== 导出到 {out_dir} ==")
    for path in written:
        print(f"  {path.name}")

    if args.db != ":memory:":
        # 换一个新连接从磁盘重新打开，验证明细仍在
        text = service.explain_text(args.db, "S1", "energy")
        print(f"\n== 从磁盘重开库查看 S1 / energy ==")
        print(text)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="装载目录、计算并落库")
    p.add_argument("--dir", required=True, help="输入目录（含四个输入文件）")
    p.add_argument("--db", default=":memory:", help="DuckDB 路径（默认内存）")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("explain", help="解释单个 specimen/指标")
    p.add_argument("--db", default=":memory:")
    p.add_argument("--specimen", required=True)
    p.add_argument("--indicator", required=True)
    p.set_defaults(fn=cmd_explain)

    p = sub.add_parser("export", help="导出数值、u、可用状态与异常明细")
    p.add_argument("--db", default=":memory:")
    p.add_argument("--out", required=True, help="导出目录")
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("demo", help="运行 examples/metrology 手工可算材料")
    p.add_argument("--db", default=":memory:")
    p.set_defaults(fn=cmd_demo)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
