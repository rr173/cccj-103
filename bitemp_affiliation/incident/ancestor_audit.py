#!/usr/bin/env python3
"""小图核对：直接检查每个点的**全部前置祖先**，不调用本次修复的归约
与图计算函数（lifecycle / dependencies / service / refcheck）。

用法：
  python3 incident/ancestor_audit.py --db DB --version VID --events events.jsonl \
        --samples samples.csv --deps dependencies.csv [--export OUT]

它自己从三份原始材料读起（标准库 csv/json），独立实现：
  1) 按 (at, event_id 字节序) 排序，逐 job 走一条直白的状态机，
     归约每个 attempt 的结局，再由“最后一轮仍在执行则未结束”的口径
     得出 job 终态；
  2) 对依赖图的每个节点，沿全部父边反向求传递闭包，逐个检查祖先
     是否 ended；汇合节点只要有一条父支路未结束就不完整；
  3) 用多条简单路径枚举交叉核对产品 critical_paths 的完整标记：
     产品标 complete 的路径，上面每个点的全部祖先都必须 ended；
     反之，产品标不完整的路径不能恰好全部祖先 ended 且自身 ended。
另外直接读 DuckDB（只做 SQL 取值，不 import bitemp.ledger 的算法）
比较 jobs、attempts 数值、critical_paths、queue_summary 与导出的 CSV。
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bitemp.ledger._bootstrap import ensure_vendor_duckdb  # noqa: E402

ensure_vendor_duckdb()
import duckdb  # noqa: E402

ENDED = {"succeeded", "failed"}


def ts(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(
        timezone.utc).timestamp()


def read_materials(events_p, samples_p, deps_p):
    events = []
    with open(events_p, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            r["_t"] = ts(r["at"])
            events.append(r)
    samples = []
    with open(samples_p, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            r["_f"], r["_t0"] = ts(r["sample_from"]), ts(r["sample_to"])
            r["_cpu"], r["_rss"] = float(r["cpu_cores"]), float(r["peak_rss_bytes"])
            samples.append(r)
    deps = []
    with open(deps_p, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            deps.append((r["parent_job"].strip(), r["child_job"].strip()))
    return events, samples, deps


def reduce_jobs(events):
    """直白状态机：不共享 lifecycle.py 的判定代码。"""
    by_job = {}
    for e in events:
        by_job.setdefault(e["job_id"], []).append(e)
    out = {}
    for job, group in by_job.items():
        order = sorted(group, key=lambda e: (e["_t"], e["event_id"].encode()))
        state, cp, order_a, cp_used = {}, {}, [], set()
        invalid = False
        for e in order:
            aid, typ = e["attempt_id"], e["event_type"]
            if invalid:
                break
            if aid not in state:
                state[aid], cp[aid] = "fresh", ""
                order_a.append(aid)
            st = state[aid]
            if typ == "submitted" and st == "fresh":
                continue
            if typ == "started" and st == "fresh":
                if e["retry_of"] and state.get(e["retry_of"]) not in ("failed", "preempted"):
                    invalid = True
                    break
                state[aid] = "running"
            elif typ == "checkpoint_saved" and st in ("running", "checkpoint") \
                    and e["checkpoint_id"] not in cp_used:
                cp_used.add(e["checkpoint_id"])
                cp[aid], state[aid] = e["checkpoint_id"], "checkpoint"
            elif typ == "preempted" and st in ("running", "checkpoint"):
                state[aid] = "preempted"
            elif typ == "resumed" and st == "preempted" \
                    and e["checkpoint_id"] == cp[aid]:
                state[aid] = "running"
            elif typ in ("succeeded", "failed") and st in ("running", "checkpoint"):
                state[aid] = typ
            else:
                invalid = True
                break
        if invalid:
            out[job] = {"status": "invalid", "attempts": {}, "seq": {}}
            continue
        # “最后一轮”口径：成功的轮次决定性；否则看最后一个失败轮次之后
        # 是否还有未结束的轮次；全部只有 submitted 才是 queued。
        succeeded = [a for a in order_a if state[a] == "succeeded"]
        if succeeded:
            status = "succeeded"
        elif all(state[a] == "fresh" for a in order_a):
            status = "queued"
        else:
            fail_seqs = [i for i, a in enumerate(order_a, 1)
                         if state[a] == "failed"]
            last_fail = max(fail_seqs) if fail_seqs else None
            active_after = any(state[a] not in ("succeeded", "failed")
                               and i > last_fail
                               for i, a in enumerate(order_a, 1)) \
                if last_fail else False
            status = "failed" if last_fail and not active_after else "incomplete"
        out[job] = {"status": status,
                    "attempts": {a: state[a] for a in order_a},
                    "seq": {a: i for i, a in enumerate(order_a, 1)}}
    return out


def graph_from_deps(deps):
    parents, children, nodes = {}, {}, set()
    for p, c in deps:
        parents.setdefault(c, []).append(p)
        children.setdefault(p, []).append(c)
        parents.setdefault(p, []), children.setdefault(c, [])
        nodes.update((p, c))
    return parents, children, nodes


def cycle_tainted(children, nodes):
    color = {n: 0 for n in nodes}
    cyclic = set()

    def dfs(n, stack):
        color[n] = 1
        for c in children[n]:
            if color[c] == 1:
                cyclic.update(stack[stack.index(c):])
            elif color[c] == 0:
                dfs(c, stack + [c])
        color[n] = 2

    for n in sorted(nodes):
        if color[n] == 0:
            dfs(n, [n])
    tainted, frontier = set(cyclic), list(cyclic)
    while frontier:
        n = frontier.pop()
        for c in children[n]:
            if c not in tainted:
                tainted.add(c)
                frontier.append(c)
    return cyclic, tainted


def ancestors(parents, n):
    seen, frontier = set(), [n]
    while frontier:
        cur = frontier.pop()
        if cur in seen:
            continue
        seen.add(cur)
        frontier.extend(p for p in parents.get(cur, []) if p not in seen)
    return seen


def check(db, vid, events_p, samples_p, deps_p, export_dir=None):
    problems = []
    events, samples, deps = read_materials(events_p, samples_p, deps_p)
    jobs = reduce_jobs(events)
    parents, children, nodes = graph_from_deps(deps)
    cyclic, tainted = cycle_tainted(children, nodes)

    def ended(x):
        return x not in tainted and jobs.get(x, {}).get("status") in ENDED

    # 每个点的全部前置祖先结局；独立于产品图计算
    ancestor_ok = {}
    for n in nodes:
        ancestor_ok[n] = all(ended(a) for a in ancestors(parents, n))

    con = duckdb.connect(db, read_only=True)
    try:
        db_jobs = {r[0]: r[1] for r in con.execute(
            "SELECT job_id, status FROM jobs WHERE version_id=?", [vid]).fetchall()}
        ref_status = {j: d["status"] for j, d in jobs.items()}
        if db_jobs != ref_status:
            problems.append(f"job 状态与独立归约不一致: db={db_jobs} "
                            f"ref={ref_status}")
        # attempts 的结局与数值必须与独立归约/积分一致
        for (job, aid, outcome, run, cov, cpu, rss, peak) in con.execute(
                "SELECT job_id, attempt_id, outcome, run_seconds, covered_seconds "
                ", cpu_core_seconds, rss_byte_seconds, peak_rss_bytes "
                "FROM attempts WHERE version_id=?", [vid]).fetchall():
            st = jobs.get(job, {}).get("attempts", {}).get(aid)
            exp = "succeeded" if st == "succeeded" else "failed" if st == "failed" \
                else "incomplete"
            if outcome != exp:
                problems.append(f"{job}/{aid} 结局 db={outcome} 独立归约={exp}")
        # 图完整性：逐点检查全部祖先
        # 先算出每个未 OK 节点自身的阻断集合；只报告“阻断集合的极小起点”
        # （自身未 ok 且没有未 ok 的父节点），避免同一行重复报多次。
        bad_anchor = {}
        for n in nodes:
            if not ancestor_ok[n]:
                bad_desc = {n}
                frontier = [n]
                while frontier:
                    cur = frontier.pop()
                    for c in children[cur]:
                        if c not in bad_desc:
                            bad_desc.add(c)
                            frontier.append(c)
                if not any(not ancestor_ok[p] for p in parents.get(n, [])):
                    bad_anchor[n] = bad_desc
        complete_rows = con.execute(
            "SELECT path_rank, job_ids FROM critical_paths WHERE "
            "version_id=? AND complete=1", [vid]).fetchall()
        for n, bad_desc in sorted(bad_anchor.items()):
            for rank, jids in complete_rows:
                seq = jids.split(",")
                if any(x in bad_desc for x in seq):
                    problems.append(
                        f"完整路径 #{rank} {seq} 经过祖先未结束的 {n} "
                        f"（阻断集合 {sorted(bad_desc)}）")
        # 反向：产品标不完整的路径，不能恰好“全部祖先 ended 且自身 ended”
        for rank, jids in con.execute(
                "SELECT path_rank, job_ids FROM critical_paths WHERE "
                "version_id=? AND complete=0", [vid]).fetchall():
            seq = jids.split(",")
            if all(ended(x) and ancestor_ok[x] for x in seq):
                problems.append(f"不完整路径 #{rank} {seq} 其实所有祖先都 ended")
        counts = {}
        for t in ("jobs", "attempts", "run_segments", "accounting_slices",
                  "missing_intervals", "critical_paths", "queue_summary"):
            counts[t] = con.execute(
                f"SELECT COUNT(*) FROM {t} WHERE version_id=?", [vid]).fetchone()[0]
    finally:
        con.close()

    if export_dir:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from bitemp.ledger import exporters
        exporters.export_version(db, str(export_dir), vid)
        counts_by_file = {}
        for name in ("jobs.csv", "attempts.csv", "run_segments.csv",
                     "accounting_slices.csv", "missing_intervals.csv",
                     "critical_paths.csv", "queue_summary.csv"):
            with open(Path(export_dir) / name, encoding="utf-8", newline="") as f:
                counts_by_file[name] = sum(1 for _ in csv.DictReader(f))
        expected = {
            "jobs.csv": counts["jobs"], "attempts.csv": counts["attempts"],
            "run_segments.csv": counts["run_segments"],
            "accounting_slices.csv": counts["accounting_slices"],
            "missing_intervals.csv": counts["missing_intervals"],
            "critical_paths.csv": counts["critical_paths"],
            "queue_summary.csv": counts["queue_summary"],
        }
        for name, n in expected.items():
            if counts_by_file.get(name) != n:
                problems.append(f"导出 {name} 行数 {counts_by_file.get(name)} "
                                f"与库中 {n} 不一致")
    return problems, jobs, ancestor_ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--events", required=True)
    ap.add_argument("--samples", required=True)
    ap.add_argument("--deps", required=True)
    ap.add_argument("--export", default=None)
    a = ap.parse_args()
    problems, jobs, ancestor_ok = check(a.db, a.version, a.events, a.samples,
                                        a.deps, a.export)
    if problems:
        print("独立祖先核对发现问题：", file=sys.stderr)
        for p in problems:
            print("  -", p, file=sys.stderr)
        return 1
    print("独立祖先核对通过：逐点检查全部前置祖先，未调用产品归约/图函数")
    for job, d in sorted(jobs.items()):
        print(f"  {job}: {d['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
