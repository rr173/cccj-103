#!/usr/bin/env python3
"""离线作业账本入口（与仓库已有命令共存的独立子工具，无网页）。

用法：
  python3 main_ledger.py import --events events.jsonl --samples samples.csv \
                                --deps dependencies.csv --db ledger.duckdb
  python3 main_ledger.py account  --db ledger.duckdb [--version VID]
  python3 main_ledger.py versions --db ledger.duckdb
  python3 main_ledger.py jobs     --db ledger.duckdb [--version VID]
  python3 main_ledger.py job      --db ledger.duckdb --job J_PREEMPT
  python3 main_ledger.py explain  --db ledger.duckdb --job J_RETRY \
                                  --attempt rb2 --segment 1
  python3 main_ledger.py paths    --db ledger.duckdb
  python3 main_ledger.py export   --db ledger.duckdb --out-dir ./out
  python3 main_ledger.py refcheck --db ledger.duckdb \
                                  --events events.jsonl --samples samples.csv \
                                  --deps dependencies.csv
  python3 main_ledger.py demo [--work-dir D]       # 可手算小数据 + 参考对账
  python3 main_ledger.py bigtest [--work-dir D]    # 大事件流规模证据
"""
from __future__ import annotations

import argparse
import os
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

_CHILD_PYTHONPATH = os.pathsep.join(
    p for p in sys.path if p and Path(p).is_dir())

from bitemp.ledger import biggen, datagen, exporters, queries, service  # noqa: E402
from bitemp.ledger._bootstrap import ensure_vendor_duckdb  # noqa: E402
from bitemp.ledger.model import LedgerInputError  # noqa: E402
from bitemp.ledger.verify import check_against_reference  # noqa: E402

ensure_vendor_duckdb()
import duckdb  # noqa: E402


def cmd_import(args) -> int:
    try:
        r = service.import_ledger(
            args.db, args.events, args.samples, args.deps,
            batch_size=args.batch_size)
    except LedgerInputError as e:
        print("输入不合法，已放弃本次写入（库中无新增版本）：", file=sys.stderr)
        for m in e.messages:
            print(f"  - {m}", file=sys.stderr)
        return 2
    verb = "复用既有版本（内容摘要相同，未重复累加）" if r.reused \
        else "写入新版本"
    print(f"== 导入完成：{verb} ==")
    print(f"  version_id = {r.version_id}")
    print(f"  事件 {r.n_events}（折叠后）/ 采样窗 {r.n_samples} / "
          f"依赖边 {r.n_deps}")
    print(f"  逻辑 job {r.n_jobs}：invalid {r.n_invalid}，queued {r.n_queued}，"
          f"job 级问题 {r.job_issue_count} 条，依赖问题 {r.dep_issue_count} 条")
    print(f"  账本: {r.db_path}")
    return 0


def cmd_account(args) -> int:
    """从核算切片重新汇总，与 attempts 表对账，验证汇总可回溯且无漂移。"""
    con = duckdb.connect(args.db, read_only=True)
    try:
        vid = queries._resolve_version(con, args.version)
        drift = con.execute("""
            WITH rebuilt AS (
                SELECT version_id, job_id, attempt_id,
                       SUM(CASE WHEN missing=0 THEN overlap_seconds ELSE 0 END)
                           AS covered,
                       SUM(CASE WHEN missing=0
                           THEN cpu_cores * overlap_seconds ELSE 0 END) AS cpu,
                       SUM(CASE WHEN missing=0
                           THEN peak_rss_bytes * overlap_seconds ELSE 0 END) AS rss,
                       MAX(CASE WHEN missing=0 THEN peak_rss_bytes END) AS peak
                FROM accounting_slices WHERE version_id=?
                GROUP BY version_id, job_id, attempt_id)
            SELECT a.job_id, a.attempt_id,
                   a.covered_seconds - COALESCE(r.covered,0),
                   a.cpu_core_seconds - COALESCE(r.cpu,0),
                   a.rss_byte_seconds - COALESCE(r.rss,0)
            FROM attempts a LEFT JOIN rebuilt r
              ON a.version_id=r.version_id AND a.job_id=r.job_id
             AND a.attempt_id=r.attempt_id
            WHERE a.version_id=?
              AND (ABS(a.covered_seconds - COALESCE(r.covered,0)) > 1e-9
                OR ABS(a.cpu_core_seconds - COALESCE(r.cpu,0)) > 1e-9
                OR ABS(a.rss_byte_seconds - COALESCE(r.rss,0)) > 1e-9)
        """, [vid, vid]).fetchall()
        rows = con.execute("""
            SELECT status, job_count, run_seconds_all, run_seconds_success,
                   cpu_core_seconds_all, cpu_core_seconds_success,
                   rss_byte_seconds_all, rss_byte_seconds_success
            FROM queue_summary WHERE version_id=? ORDER BY status
        """, [vid]).fetchall()
    finally:
        con.close()
    if drift:
        print("核算切片与 attempt 汇总存在漂移：", file=sys.stderr)
        for d in drift:
            print(f"  {d}", file=sys.stderr)
        return 1
    print(f"== 版本 {vid} 核算一致性通过：attempt 汇总均可由核算切片重建 ==")
    print("状态  job数  运行秒(全部/成功)  CPU秒(全部/成功)  RSS字节秒(全部/成功)")
    for (st, n, ra, rs, ca, cs, ba, bs) in rows:
        print(f"{st:<11}{n:>4}  {ra:g}/{rs:g}  {ca:g}/{cs:g}  {ba:g}/{bs:g}")
    return 0


def cmd_versions(args) -> int:
    for v in queries.list_versions(args.db):
        print(f"{v['version_id']}  {v['created_at']}  "
              f"events={v['n_events']} samples={v['n_samples']} "
              f"deps={v['n_deps']} ({v['schema_version']})")
    return 0


def cmd_jobs(args) -> int:
    payload = queries.queue_summary(args.db, args.version)
    print(f"version_id = {payload['version_id']}")
    print(f"{'status':<11}{'jobs':>6}{'run_s(all)':>13}{'run_s(succ)':>14}"
          f"{'cpu_s(all)':>13}{'rss_bs(all)':>15}")
    for r in payload["rows"]:
        print(f"{r['status']:<11}{r['job_count']:>6}"
              f"{r['run_seconds_all']:>13.3f}{r['run_seconds_success']:>14.3f}"
              f"{r['cpu_core_seconds_all']:>13.3f}"
              f"{r['rss_byte_seconds_all']:>15.3f}")
    return 0


def cmd_job(args) -> int:
    try:
        payload = queries.job_detail(args.db, args.job, args.version)
    except KeyError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(queries.to_json(payload))
    return 0


def cmd_explain(args) -> int:
    try:
        payload = queries.explain_slice(
            args.db, args.job, args.attempt, args.segment, args.version)
    except KeyError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(queries.to_json(payload))
    return 0


def cmd_paths(args) -> int:
    payload = queries.critical_paths(args.db, args.version)
    if payload["dep_issues"]:
        print("依赖问题：")
        for iss in payload["dep_issues"]:
            print(f"  [{iss['issue_code']}] {iss['detail']} "
                  f"(jobs={iss['related_jobs']})")
        print()
    for p in payload["paths"]:
        flag = "完整" if p["complete"] else "不完整"
        print(f"#{p['path_rank']} [{flag}] 总运行 {p['total_run_seconds']:g}s  "
              f"{' -> '.join(p['job_ids'])}  权重={p['weights']}")
    return 0


def cmd_export(args) -> int:
    counts = exporters.export_version(args.db, args.out_dir, args.version)
    for name, n in sorted(counts.items()):
        print(f"  {name}: {n} 行/份 -> {Path(args.out_dir) / name}")
    return 0


def cmd_refcheck(args) -> int:
    problems = check_against_reference(
        args.db, args.events, args.samples, args.deps, args.version)
    if problems:
        print("参考对账发现不一致：", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print("产品结果与独立参考实现、手算期望值全部一致（容差 1e-9）")
    return 0


def _run_import_subprocess(db, events, samples, deps, work) -> str:
    out_db = str(Path(work) / db)
    cmd = [sys.executable, str(Path(__file__).resolve()), "import",
           "--events", str(events), "--samples", str(samples),
           "--deps", str(deps), "--db", out_db]
    env = dict(os.environ)
    env["PYTHONPATH"] = _CHILD_PYTHONPATH
    env["PYTHONSAFEPATH"] = "1"
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        raise SystemExit(f"子进程导入失败: {' '.join(cmd)}")
    return out_db


def cmd_demo(args) -> int:
    work = Path(args.work_dir or tempfile.mkdtemp(prefix="ledger_demo_"))
    if args.work_dir:
        work.mkdir(parents=True, exist_ok=True)
    d1 = work / "data_ordered"
    d2 = work / "data_shuffled"
    datagen.write_demo_dataset(d1, shuffle=False)
    datagen.write_demo_dataset(d2, shuffle=True)
    print(f"== 演示材料：{d1} 与行序打乱版 {d2} ==")

    db1 = _run_import_subprocess("ordered.duckdb", d1 / "events.jsonl",
                                 d1 / "samples.csv", d1 / "dependencies.csv",
                                 work)
    db2 = _run_import_subprocess("shuffled.duckdb", d2 / "events.jsonl",
                                 d2 / "samples.csv", d2 / "dependencies.csv",
                                 work)

    v1 = queries.list_versions(db1)[0]["version_id"]
    v2 = queries.list_versions(db2)[0]["version_id"]
    print(f"\n== 行序不变性：两份内容摘要 {'一致' if v1 == v2 else '不一致！'} ==")
    if v1 != v2:
        return 1

    print("\n== 队列总账（成功尝试与全部尝试分列，重试成本不隐藏）==")
    print(queries.to_json(queries.queue_summary(db1)))

    print("\n== 抢占续跑作业 J_PREEMPT 全链路 ==")
    print(queries.to_json(queries.job_detail(db1, "J_PREEMPT")))

    print("\n== 失败重试作业 J_RETRY：rb1 失败 / rb2 成功的消耗分列 ==")
    print(queries.to_json(queries.job_detail(db1, "J_RETRY")))

    print("\n== invalid 作业隔离（J_TIE_BAD/J_STALE，原始事件保留）==")
    for j in ("J_TIE_BAD", "J_STALE"):
        detail = queries.job_detail(db1, j)
        print(f"  {j}: status={detail['job']['status']} "
              f"issues={[i['issue_code'] for i in detail['issues']]} "
              f"保留原始事件 {len(detail['raw_events'])} 条")

    print("\n== 依赖最长路径（含等权字节序决胜与环提示）==")
    print(queries.to_json(queries.critical_paths(db1)))

    print("\n== 核算下钻示例：J_RETRY/rb2/片段1 每个切片对应哪个采样窗 ==")
    print(queries.to_json(queries.explain_slice(db1, "J_RETRY", "rb2", 1)))

    print("\n== 独立参考实现逐项对账（重写状态机/积分/路径，不共享产品代码）==")
    rc = cmd_refcheck(argparse.Namespace(
        db=db1, events=str(d1 / "events.jsonl"),
        samples=str(d1 / "samples.csv"), deps=str(d1 / "dependencies.csv"),
        version=None))
    if rc != 0:
        return rc

    print("\n== 同内容重复导入幂等性 ==")
    r = service.import_ledger(db1, d1 / "events.jsonl", d1 / "samples.csv",
                              d1 / "dependencies.csv")
    print(f"  reused={r.reused} version_id={r.version_id}")
    if not r.reused or len(queries.list_versions(db1)) != 1:
        print("  幂等性失败！", file=sys.stderr)
        return 1

    print("\n== 冲突 event_id 必须整体拒绝且不产生新版本 ==")
    dc = work / "conflict"
    datagen.write_conflict_dataset(dc)
    try:
        service.import_ledger(work / "conflict.duckdb",
                              dc / "events.jsonl", dc / "samples.csv",
                              dc / "dependencies.csv")
        print("  冲突材料竟被接受！", file=sys.stderr)
        return 1
    except LedgerInputError as e:
        print(f"  已拒绝：{e.messages[0][:100]}…")
    if Path(work / "conflict.duckdb").exists():
        con = duckdb.connect(str(work / "conflict.duckdb"), read_only=True)
        n = con.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
        con.close()
        if n != 0:
            print("  冲突导入留下了版本！", file=sys.stderr)
            return 1
        print("  冲突库无任何版本登记，写入已整体回滚")

    out_dir = work / "export"
    print(f"\n== 导出 -> {out_dir} ==")
    cmd_export(argparse.Namespace(db=db1, out_dir=str(out_dir), version=None))
    print(f"\n工作目录保留在 {work}（可重新打开查询/导出）")
    return 0


def cmd_bigtest(args) -> int:
    work = Path(args.work_dir or tempfile.mkdtemp(prefix="ledger_big_"))
    work.mkdir(parents=True, exist_ok=True)
    info = biggen.write_big_dataset(
        work, n_jobs=args.jobs, attempts_per_job=args.attempts, seed=args.seed)
    print(f"== 大事件流：job {info['n_jobs']}，事件 {info['n_events']}，"
          f"采样窗 {info['n_samples']}，依赖 {info['n_deps']} ==")
    db = work / "big.duckdb"
    t0 = time.time()
    r = service.import_ledger(
        db, Path(info["dir"]) / "events.jsonl",
        Path(info["dir"]) / "samples.csv",
        Path(info["dir"]) / "dependencies.csv",
        batch_size=args.batch_size)
    elapsed = time.time() - t0
    peak_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(f"  导入+核算耗时 {elapsed:.2f}s；进程峰值常驻内存约 "
          f"{peak_kb / 1024:.1f} MiB（ru_maxrss）")
    print(f"  版本 {r.version_id}：invalid={r.n_invalid}")

    con = duckdb.connect(str(db), read_only=True)
    try:
        n_events_db = con.execute(
            "SELECT n_events FROM versions WHERE version_id=?",
            [r.version_id]).fetchone()[0]
        n_attempts = con.execute(
            "SELECT COUNT(*) FROM attempts WHERE version_id=?",
            [r.version_id]).fetchone()[0]
        n_slices = con.execute(
            "SELECT COUNT(*) FROM accounting_slices WHERE version_id=?",
            [r.version_id]).fetchone()[0]
        # 抽查 1：切片积分之和与 attempt 汇总一致
        drift = con.execute("""
            WITH rb AS (
                SELECT job_id, attempt_id,
                  SUM(CASE WHEN missing=0 THEN cpu_cores*overlap_seconds ELSE 0 END) c
                FROM accounting_slices WHERE version_id=?
                GROUP BY job_id, attempt_id)
            SELECT COUNT(*) FROM attempts a JOIN rb
              ON a.job_id=rb.job_id AND a.attempt_id=rb.attempt_id
            WHERE a.version_id=? AND ABS(a.cpu_core_seconds-rb.c)>1e-9
        """, [r.version_id, r.version_id]).fetchone()[0]
        # 抽查 2：随机取 3 个 job 手算覆盖比例恒在 [0,1]
        bad_ratio = con.execute(
            "SELECT COUNT(*) FROM attempts WHERE version_id=? "
            "AND (coverage_ratio < 0 OR coverage_ratio > 1)",
            [r.version_id]).fetchone()[0]
        # 抽查 3：总账 job 数与 jobs 表一致
        q_total = con.execute(
            "SELECT SUM(job_count) FROM queue_summary WHERE version_id=?",
            [r.version_id]).fetchone()[0]
    finally:
        con.close()
    print(f"  库内事件 {n_events_db}、attempt {n_attempts}、核算切片 {n_slices}")
    print(f"  抽查：切片->attempt 积分漂移 {drift} 条；覆盖比例越界 {bad_ratio} 条；"
          f"总账 job 数 {q_total}（应等于 {info['n_jobs']}）")
    # 重复导入幂等
    t0 = time.time()
    r2 = service.import_ledger(
        db, Path(info["dir"]) / "events.jsonl",
        Path(info["dir"]) / "samples.csv",
        Path(info["dir"]) / "dependencies.csv",
        batch_size=args.batch_size)
    print(f"  二次导入 {time.time() - t0:.2f}s，reused={r2.reused}")
    ok = (drift == 0 and bad_ratio == 0 and q_total == info["n_jobs"]
          and r2.reused and n_events_db == info["n_events"])
    print("  结果：" + ("全部抽查通过" if ok else "存在失败项！"))
    print(f"  账本 {db}（关闭后可重开 versions/jobs/job/explain/paths 查询）")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("import", help="导入三份材料并完成生命周期重建与资源核算")
    p.add_argument("--events", required=True)
    p.add_argument("--samples", required=True)
    p.add_argument("--deps", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--batch-size", type=int, default=2000)
    p.set_defaults(fn=cmd_import)

    p = sub.add_parser("account", help="由核算切片重建汇总并与 attempt 对账")
    p.add_argument("--db", required=True)
    p.add_argument("--version", default=None)
    p.set_defaults(fn=cmd_account)

    p = sub.add_parser("versions", help="列出账本中的全部内容版本")
    p.add_argument("--db", required=True)
    p.set_defaults(fn=cmd_versions)

    p = sub.add_parser("jobs", help="队列总账（按状态汇总，成功/全部消耗分列）")
    p.add_argument("--db", required=True)
    p.add_argument("--version", default=None)
    p.set_defaults(fn=cmd_jobs)

    p = sub.add_parser("job", help="单个 job 的生命周期、attempt、片段与原始事件")
    p.add_argument("--db", required=True)
    p.add_argument("--job", required=True)
    p.add_argument("--version", default=None)
    p.set_defaults(fn=cmd_job)

    p = sub.add_parser("explain", help="下钻某运行片段的核算切片与采样窗来源")
    p.add_argument("--db", required=True)
    p.add_argument("--job", required=True)
    p.add_argument("--attempt", required=True)
    p.add_argument("--segment", type=int, required=True)
    p.add_argument("--version", default=None)
    p.set_defaults(fn=cmd_explain)

    p = sub.add_parser("paths", help="加权最长依赖路径与依赖问题")
    p.add_argument("--db", required=True)
    p.add_argument("--version", default=None)
    p.set_defaults(fn=cmd_paths)

    p = sub.add_parser("export", help="导出 CSV 平面文件与 summary.json")
    p.add_argument("--db", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--version", default=None)
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("refcheck", help="独立参考实现与手算期望值对账")
    p.add_argument("--db", required=True)
    p.add_argument("--events", required=True)
    p.add_argument("--samples", required=True)
    p.add_argument("--deps", required=True)
    p.add_argument("--version", default=None)
    p.set_defaults(fn=cmd_refcheck)

    p = sub.add_parser("demo", help="可手算小数据 + 参考对账 + 乱序/幂等/冲突")
    p.add_argument("--work-dir", default=None)
    p.set_defaults(fn=cmd_demo)

    p = sub.add_parser("bigtest", help="大事件流：耗时、峰值内存与抽查")
    p.add_argument("--work-dir", default=None)
    p.add_argument("--jobs", type=int, default=4000)
    p.add_argument("--attempts", type=int, default=3)
    p.add_argument("--seed", type=int, default=23)
    p.add_argument("--batch-size", type=int, default=2000)
    p.set_defaults(fn=cmd_bigtest)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
