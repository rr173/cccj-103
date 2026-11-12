#!/usr/bin/env python3
"""双端 FASTQ 读段整理入口。

用法：
    python3 main_pefastq.py run --manifest M.csv --out DIR [--db D]
                                --min-qual 20 --min-len 8 [--memory-limit 1GB]
    python3 main_pefastq.py where --db D --sample S [--name N]
    python3 main_pefastq.py demo [--db D]
    python3 main_pefastq.py bigtest [--pairs 200000] [--memory-limit 256MB] [--keep]

run      整理一批：清单 -> DuckDB 暂存 -> 配对决策 -> 清洗 -> 每样本两个 FASTQ
         + 拒收/冲突/孤端/过滤/重复合并明细 + 统计。任何源未完整读完
         （截断记录、gzip 解压错误）整次运行失败，诊断保留在库中。
where    从整理库查看某个样本（--sample）或某条读段（加 --name）的去向。
demo     运行 examples/pefastq 小样本：乱序配对、跨分片相同重传、后到冲突、
         跨样本同名、缺端、非法端标记、质量串以 @ 开头、单端过短，
         展示每条读段的实际去向，并用独立参考程序逐对核对。
bigtest  生成双端远距离到达的大样本（全部 R1 正序分片，R2 反序分片），
         固定内存配置下运行并记录峰值占用，再用独立参考程序核对真实输出；
         最后演示一次截断 gzip 被明确报错。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bitemp.pefastq import datagen, pipeline, refcheck, store  # noqa: E402
from bitemp.pefastq.fastqio import SourceFailure  # noqa: E402
from bitemp.pefastq.manifest import ManifestFailure  # noqa: E402
from bitemp.pefastq.pipeline import PipelineFailure  # noqa: E402

EXAMPLE_DIR = Path(__file__).resolve().parent / "examples" / "pefastq"


def _print_summary(summary: dict) -> None:
    print("\n== 运行汇总 ==")
    print(f"  源 {summary['sources_total']} 个，物理记录 {summary['records_total']} 条："
          f"合法 {summary['records_valid']}，非法 {summary['records_invalid']}")
    print(f"  去重后端 {summary['distinct_ends']} 条（重传合并 {summary['merged_copies']} 份）")
    print(f"  键 {summary['keys_total']} 个：合格 {summary['keys_paired']}，"
          f"过滤 {summary['keys_filtered']}，孤端 {summary['keys_orphan']}，"
          f"冲突 {summary['keys_conflict']}，拒收 {summary['keys_rejected']}")
    print(f"  清洗前 {summary['reads_before']} 读段 / {summary['bases_before']} 碱基；"
          f"清洗后 {summary['reads_after']} 读段 / {summary['bases_after']} 碱基"
          f"（3' 端删去 {summary['bases_trimmed']}）")
    print(f"  峰值内存 {summary['peak_rss_mb']:.1f} MB")
    print(f"PEAK_RSS_MB={summary['peak_rss_mb']:.1f}")


def cmd_run(args) -> int:
    db = args.db or str(Path(args.out) / "pefastq.duckdb")
    try:
        summary = pipeline.run(args.manifest, args.out, db,
                               min_qual=args.min_qual, min_len=args.min_len,
                               memory_limit=args.memory_limit)
    except (ManifestFailure, SourceFailure, PipelineFailure) as e:
        print(f"运行失败: {e}", file=sys.stderr)
        print(f"（已收集的诊断保留在 {db}，可用 where 查看）", file=sys.stderr)
        return 2
    _print_summary(summary)
    print(f"\n输出目录 {summary['out_dir']}:")
    for p in summary["written"]:
        print(f"  {p}")
    print(f"整理库: {db}（where 入口可查看样本/读段去向）")
    return 0


def cmd_where(args) -> int:
    try:
        text = pipeline.where_text(args.db, args.sample, args.name)
    except KeyError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(text)
    return 0


def _fate_line(info: dict) -> str:
    """把 where() 的结果压成一行，展示一条读段的实际去向。"""
    key = f"{info['sample']}:{info['name']}"
    state = info["state"]
    if state == "paired":
        return (f"{key:<16} -> 合格对 #{info['record_index']}，写入 "
                f"{info['files'][0]} / {info['files'][1]} 的{info['lines']}；"
                f"清洗 R1 {info['len1_raw']}->{info['len1_after']}，"
                f"R2 {info['len2_raw']}->{info['len2_after']}")
    if state == "filtered":
        return (f"{key:<16} -> 过滤：清洗后 R1={info['len1_after']}、"
                f"R2={info['len2_after']}，短于最短保留长度")
    if state == "orphan":
        return (f"{key:<16} -> 孤端：仅有 R{info['mate_present']}"
                f"（来源 {info['sources']}）")
    if state == "conflict":
        by_mate: dict[int, int] = {}
        for c in info["contents"]:
            by_mate[c["mate"]] = by_mate.get(c["mate"], 0) + 1
        bad = ", ".join(f"R{m} 有 {n} 份不同内容"
                        for m, n in sorted(by_mate.items()) if n > 1)
        return f"{key:<16} -> 冲突：{bad}，整对拒收"
    if state == "rejected":
        bad = [r for r in info["records"] if not r["ok"]]
        r0 = bad[0]
        return (f"{key:<16} -> 拒收：{r0['source_id']}#{r0['rec_no']} "
                f"（第 {r0['start_line']} 行）{r0['err_code']}: {r0['err_msg']}")
    return f"{key:<16} -> {state}"


def cmd_demo(args) -> int:
    if not (EXAMPLE_DIR / "manifest.csv").exists():
        print(f"生成示例数据到 {EXAMPLE_DIR}")
        datagen.write_demo_dataset(EXAMPLE_DIR)
    out_dir = Path(tempfile.mkdtemp(prefix="pefastq_demo_"))
    db = args.db if args.db != ":memory:" else str(out_dir / "demo.duckdb")
    print(f"== 整理示例 {EXAMPLE_DIR}（min_qual=20, min_len=8）==")
    summary = pipeline.run(EXAMPLE_DIR / "manifest.csv", out_dir, db,
                           min_qual=20, min_len=8)
    _print_summary(summary)

    print("\n== 每条读段的去向 ==")
    con = store.connect_readonly(db)
    for sample, name in con.execute(
            "SELECT sample, name FROM key_states "
            "ORDER BY sample, name").fetchall():
        print("  " + _fate_line(pipeline.where(db, sample, name, con=con)))
    con.close()

    print("\n== 独立参考程序逐对核对 ==")
    problems = refcheck.check(EXAMPLE_DIR / "manifest.csv", out_dir, db,
                              min_qual=20, min_len=8)
    if problems:
        print("  不一致:")
        for p in problems[:10]:
            print(f"   - {p}")
        return 1
    print("  输出内容与分类全部一致")
    print(f"\n输出目录 {out_dir}（含两个 FASTQ/样本、明细 CSV、stats.txt）")
    return 0


def cmd_bigtest(args) -> int:
    work = Path(args.workdir) if args.workdir else \
        Path(tempfile.mkdtemp(prefix="pefastq_big_"))
    work.mkdir(parents=True, exist_ok=True)
    data, out, db = work / "data", work / "out", work / "big.duckdb"
    print(f"== 生成大样本（{args.pairs} 对，R1 正序分片、R2 反序分片、"
          f"含重传/孤端/过滤）==\n  目录 {data}")
    t0 = time.time()
    datagen.write_big_dataset(data, n_pairs=args.pairs, seed=args.seed)
    print(f"  生成耗时 {time.time() - t0:.1f}s")

    print(f"\n== 固定内存配置（{args.memory_limit}）下整理 ==")
    cmd = [sys.executable, str(Path(__file__).resolve()), "run",
           "--manifest", str(data / "manifest.csv"), "--out", str(out),
           "--db", str(db), "--min-qual", "20", "--min-len", "30",
           "--memory-limit", args.memory_limit]
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.time() - t0
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        print("大样本整理失败", file=sys.stderr)
        return 1
    peak = next((float(line.split("=")[1]) for line in proc.stdout.splitlines()
                 if line.startswith("PEAK_RSS_MB=")), float("nan"))
    for line in proc.stdout.splitlines():
        if line.startswith("  "):
            print(line)
    print(f"  整理耗时 {elapsed:.1f}s，峰值内存 {peak:.1f} MB"
          f"（配置上限 {args.memory_limit}）")

    print("\n== 独立参考程序核对真实输出 ==")
    t0 = time.time()
    problems = refcheck.check(data / "manifest.csv", out, db,
                              min_qual=20, min_len=30)
    if problems:
        print("  不一致:")
        for p in problems[:10]:
            print(f"   - {p}")
        return 1
    print(f"  输出内容与分类全部一致（核对耗时 {time.time() - t0:.1f}s）")

    print("\n== 截断 gzip 必须明确报错 ==")
    trunc_dir = work / "trunc"
    datagen.write_truncated_gzip_dataset(trunc_dir)
    cmd = [sys.executable, str(Path(__file__).resolve()), "run",
           "--manifest", str(trunc_dir / "manifest.csv"),
           "--out", str(work / "trunc_out"), "--db", str(work / "trunc.duckdb"),
           "--min-qual", "20", "--min-len", "30"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    print(f"  退出码: {proc.returncode}")
    print(f"  报错: {proc.stderr.splitlines()[0] if proc.stderr else '(无)'}")
    if proc.returncode != 2 or "未完整读完" not in proc.stderr:
        print("  截断 gzip 未被明确报错！", file=sys.stderr)
        return 1
    diag = store.connect_readonly(str(work / "trunc.duckdb"))
    rows = diag.execute("SELECT source_id, status, records, error FROM sources "
                        "ORDER BY source_id").fetchall()
    print("  诊断保留在库中:")
    for sid, status, n, err in rows:
        print(f"    {sid}: {status}，已读 {n} 条"
              + (f"，{err[:60]}..." if err and len(err) > 60 else
                 f"，{err}" if err else ""))
    diag.close()
    if not args.keep:
        import shutil
        shutil.rmtree(work, ignore_errors=True)
        print(f"\n（工作目录已清理；--keep 可保留 {work}）")
    else:
        print(f"\n工作目录保留在 {work}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="整理一批双端 FASTQ")
    p.add_argument("--manifest", required=True, help="清单 CSV")
    p.add_argument("--out", required=True, help="输出目录")
    p.add_argument("--db", default=None,
                   help="DuckDB 路径（默认 <out>/pefastq.duckdb）")
    p.add_argument("--min-qual", type=int, required=True,
                   help="3' 端质量阈值（Phred，低于则删）")
    p.add_argument("--min-len", type=int, required=True,
                   help="清洗后最短保留长度（任一端短于则整对过滤）")
    p.add_argument("--memory-limit", default="1GB",
                   help="DuckDB 内存上限（超出溢写临时目录）")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("where", help="查看样本/读段去向")
    p.add_argument("--db", required=True)
    p.add_argument("--sample", required=True)
    p.add_argument("--name", default=None, help="读段名（不含 /1、/2）")
    p.set_defaults(fn=cmd_where)

    p = sub.add_parser("demo", help="运行 examples/pefastq 小样本")
    p.add_argument("--db", default=":memory:")
    p.set_defaults(fn=cmd_demo)

    p = sub.add_parser("bigtest", help="双端远距离到达的大样本 + 截断 gzip 演示")
    p.add_argument("--pairs", type=int, default=200000)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--memory-limit", default="256MB")
    p.add_argument("--workdir", default=None)
    p.add_argument("--keep", action="store_true", help="保留工作目录")
    p.set_defaults(fn=cmd_bigtest)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
