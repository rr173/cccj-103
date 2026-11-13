"""独立参考核对程序：只使用标准库，从三份原始材料重算一切。

刻意不 import 产品侧的 lifecycle.py / accounting.py / dependencies.py /
service.py：状态迁移、资源积分、图路径全部按需求文本另写一遍（解析口径
只共享 csv/json/datetime 这类标准库能力）。verify.py 用本模块的结果与
DuckDB 中的产品结果逐项对账。仅面向演示级小数据：依赖路径采用显式
DFS 枚举所有简单路径（小图），不做 DP 剪枝。
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone

# ------------------------------------------------------------ 材料读取

def _ts(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(
        timezone.utc).timestamp()


def read_materials(events_path: str, samples_path: str, deps_path: str):
    events: list[dict] = []
    with open(events_path, encoding="utf-8") as f:
        for ln, line in enumerate(f, start=1):
            if not line.strip():
                continue
            rec = json.loads(line)
            rec["_line"] = ln
            rec["_t"] = _ts(rec["at"])
            events.append(rec)
    samples: list[dict] = []
    with open(samples_path, encoding="utf-8", newline="") as f:
        for ln, rec in enumerate(csv.DictReader(f), start=2):
            rec["_line"] = ln
            rec["_f"] = _ts(rec["sample_from"])
            rec["_t0"] = _ts(rec["sample_to"])
            rec["_cpu"] = float(rec["cpu_cores"])
            rec["_rss"] = float(rec["peak_rss_bytes"])
            samples.append(rec)
    deps: list[tuple[str, str]] = []
    with open(deps_path, encoding="utf-8", newline="") as f:
        for rec in csv.DictReader(f):
            deps.append((rec["parent_job"].strip(), rec["child_job"].strip()))
    return events, samples, deps


def fold_events(events: list[dict]):
    """重复事件折叠；返回 (按 event_id 折叠后的事件表, 冲突列表)。"""
    by_id: dict[str, list[dict]] = {}
    for e in events:
        by_id.setdefault(e["event_id"], []).append(e)
    folded: dict[str, dict] = {}
    conflicts: list[str] = []
    for eid, group in by_id.items():
        sigs = {(g["job_id"], g["attempt_id"], g["event_type"], g["_t"],
                 g["node"], g["checkpoint_id"], g["retry_of"]) for g in group}
        if len(sigs) > 1:
            conflicts.append(eid)
        else:
            folded[eid] = group[0]
    return list(folded.values()), conflicts


# ------------------------------------------------------------ 生命周期（重写）

def ref_lifecycle(events: list[dict]):
    """返回 jobs: {job_id: {status, attempts: {aid: dict}, issues: [...]}}。"""
    by_job: dict[str, list[dict]] = {}
    for e in events:
        by_job.setdefault(e["job_id"], []).append(e)

    jobs: dict[str, dict] = {}
    for job_id, group in by_job.items():
        ordered = sorted(group, key=lambda e: (e["_t"],
                                               e["event_id"].encode("utf-8")))
        state: dict[str, str] = {}
        latest_cp: dict[str, str] = {}
        segs: dict[str, list[list]] = {}   # aid -> [ [start, end_or_None, ...] ]
        order: list[str] = []
        retry_of: dict[str, str] = {}
        issues: list[str] = []
        used_cp: set[str] = set()
        bad = False

        def fail(msg: str) -> None:
            nonlocal bad
            issues.append(msg)
            bad = True

        for e in ordered:
            if bad:
                break
            aid = e["attempt_id"]
            if aid not in state:
                state[aid] = "fresh"
                latest_cp[aid] = ""
                segs[aid] = []
                order.append(aid)
            st = state[aid]
            typ = e["event_type"]

            if typ == "submitted":
                if st != "fresh":
                    fail(f"{aid}: submitted in {st}")
            elif typ == "started":
                if st != "fresh":
                    fail(f"{aid}: started in {st}")
                    continue
                if e["retry_of"]:
                    parent = e["retry_of"]
                    if parent not in state:
                        fail(f"{aid}: retry_of missing {parent}")
                        continue
                    if state[parent] not in ("failed", "preempted"):
                        fail(f"{aid}: retry_of parent state {state[parent]}")
                        continue
                retry_of[aid] = e["retry_of"]
                state[aid] = "running"
                segs[aid].append([e["_t"], None, e["node"], e["event_id"]])
            elif typ == "checkpoint_saved":
                if st not in ("running", "checkpoint"):
                    fail(f"{aid}: checkpoint in {st}")
                    continue
                if not e["checkpoint_id"] or e["checkpoint_id"] in used_cp:
                    fail(f"{aid}: bad checkpoint {e['checkpoint_id']}")
                    continue
                used_cp.add(e["checkpoint_id"])
                latest_cp[aid] = e["checkpoint_id"]
                state[aid] = "checkpoint"
            elif typ == "preempted":
                if st not in ("running", "checkpoint"):
                    fail(f"{aid}: preempt in {st}")
                    continue
                segs[aid][-1][1] = e["_t"]
                state[aid] = "preempted"
            elif typ == "resumed":
                if st != "preempted":
                    fail(f"{aid}: resume in {st}")
                    continue
                if e["checkpoint_id"] != latest_cp[aid]:
                    fail(f"{aid}: stale checkpoint {e['checkpoint_id']} "
                         f"!= {latest_cp[aid]}")
                    continue
                state[aid] = "running"
                segs[aid].append([e["_t"], None, e["node"], e["event_id"]])
            elif typ in ("succeeded", "failed"):
                if st not in ("running", "checkpoint"):
                    fail(f"{aid}: {typ} in {st}")
                    continue
                segs[aid][-1][1] = e["_t"]
                state[aid] = typ

        if bad:
            status = "invalid"
            success = None
        else:
            success = next((a for a in order if state[a] == "succeeded"), None)
            if success is not None:
                status = "succeeded"
            elif any(state[a] == "failed" for a in order):
                status = "failed"
            elif all(state[a] == "fresh" for a in order):
                status = "queued"
            else:
                status = "incomplete"
        jobs[job_id] = {
            "status": status, "success": success, "issues": issues,
            "state": state, "segments": segs, "order": order,
            "retry_of": retry_of,
            "last_obs": max(e["_t"] for e in ordered),
        }
    return jobs


# ------------------------------------------------------------ 资源积分（重写）

def ref_account(jobs: dict, events: list[dict], samples: list[dict]):
    """逐 attempt 复算运行时长、CPU core-seconds、RSS byte-seconds、缺测。"""
    last_ev: dict[tuple[str, str], float] = {}
    for e in events:
        key = (e["job_id"], e["attempt_id"])
        last_ev[key] = max(last_ev.get(key, e["_t"]), e["_t"])

    results: dict[tuple[str, str], dict] = {}
    for job_id, j in jobs.items():
        if j["status"] == "invalid":
            continue
        for aid in j["order"]:
            run = covered = cpu_s = rss_s = 0.0
            peak = None
            missing: list[tuple[float, float]] = []
            for idx, seg in enumerate(j["segments"][aid], start=1):
                lo, hi0, node, _eid = seg
                hi = hi0 if hi0 is not None else last_ev[(job_id, aid)]
                dur = hi - lo
                run += dur
                # 网格点：片段端点 + 窗端点
                grid = {lo, hi}
                wins = [w for w in samples if w["node"] == node
                        and w["_f"] < hi and w["_t0"] > lo]
                for w in wins:
                    grid.add(max(lo, w["_f"]))
                    grid.add(min(hi, w["_t0"]))
                grid = sorted(x for x in grid if lo <= x <= hi)
                for a, b in zip(grid, grid[1:]):
                    if b <= a:
                        continue
                    mid = (a + b) / 2
                    cover = [w for w in wins
                             if w["_f"] <= mid < w["_t0"]]
                    d = b - a
                    if not cover:
                        missing.append((a, b))
                        continue
                    w = min(cover, key=lambda x: x["boot_id"].encode("utf-8"))
                    covered += d
                    cpu_s += w["_cpu"] * d
                    rss_s += w["_rss"] * d
                    peak = w["_rss"] if peak is None else max(peak, w["_rss"])
            results[(job_id, aid)] = {
                "run_seconds": run, "covered_seconds": covered,
                "coverage_ratio": (covered / run if run > 0 else 0.0),
                "cpu_core_seconds": cpu_s,
                "rss_byte_seconds": rss_s,
                "peak_rss_bytes": peak,
                "missing": missing,
                "outcome": j["state"][aid]
                if j["state"][aid] in ("succeeded", "failed")
                else "incomplete",
            }
    return results


# ------------------------------------------------------------ 依赖路径（重写）

def ref_critical_paths(jobs: dict, accounting: dict, deps: list[tuple[str, str]]):
    children: dict[str, list[str]] = {}
    parents: dict[str, list[str]] = {}
    nodes: set[str] = set()
    for p, c in deps:
        children.setdefault(p, []).append(c)
        children.setdefault(c, [])
        parents.setdefault(c, []).append(p)
        parents.setdefault(p, [])
        nodes.update((p, c))

    # 权重：一个 job 全部 attempt 运行秒数之和
    weight: dict[str, float] = {n: 0.0 for n in nodes}
    for (job_id, aid), r in accounting.items():
        if job_id in weight:
            weight[job_id] += r["run_seconds"]
    # 事件中出现但不在依赖表的节点不参与路径；仅图内节点
    status = {n: jobs[n]["status"] if n in jobs else "unknown" for n in nodes}

    # 环检测：三色 DFS；闭合边指向栈内节点时，该节点到栈顶构成一个环
    color: dict[str, int] = {n: 0 for n in nodes}
    cyclic: set[str] = set()

    def dfs_cycle(n: str, stack: list[str]) -> None:
        color[n] = 1
        for c in children[n]:
            if color[c] == 1:
                k = stack.index(c)
                cyclic.update(stack[k:])
            elif color[c] == 0:
                dfs_cycle(c, stack + [c])
        color[n] = 2

    for n in sorted(nodes):
        if color[n] == 0:
            dfs_cycle(n, [n])
    tainted: set[str] = set(cyclic)
    frontier = list(cyclic)
    while frontier:
        n = frontier.pop()
        for c in children[n]:
            if c not in tainted:
                tainted.add(c)
                frontier.append(c)

    # 显式枚举所有简单路径（小图）
    all_paths: list[tuple[float, list[str]]] = []

    def walk(n: str, path: list[str], acc: float, seen: frozenset) -> None:
        all_paths.append((acc, path))
        for c in sorted(children[n], key=lambda x: x.encode("utf-8")):
            if c in seen:
                continue
            walk(c, path + [c], acc + weight[c], seen | {c})

    for n in sorted(nodes, key=lambda x: x.encode("utf-8")):
        walk(n, [n], weight[n], frozenset({n}))

    # 完整性口径（独立实现，不调用产品侧依赖图代码）：对路径上的每个
    # 节点，沿全部父边反向收集它的所有前置祖先（传递闭包），要求每个
    # 祖先（含节点自身）都处于 ended 终态且不在环污染集合。不能只看
    # 被选中的父支路——汇合处任一父支路未结束，汇合点及其后代都不完整。
    def _ancestors(n: str) -> set[str]:
        seen_a: set[str] = set()
        frontier = [n]
        while frontier:
            cur = frontier.pop()
            if cur in seen_a:
                continue
            seen_a.add(cur)
            for p in parents.get(cur, []):
                if p not in seen_a:
                    frontier.append(p)
        return seen_a

    def _ended(x: str) -> bool:
        return x not in tainted and status.get(x) in ("succeeded", "failed")

    def _chain_ended(seq: list[str]) -> bool:
        if not all(x not in tainted and status.get(x) in
                   ("succeeded", "failed") for x in seq):
            return False
        for x in seq:
            if not all(_ended(a) for a in _ancestors(x)):
                return False
        return True

    complete = [(-t, tuple(x.encode("utf-8") for x in p), t, p)
                for t, p in all_paths
                if _chain_ended(p)]
    incomplete = [(-t, tuple(x.encode("utf-8") for x in p), t, p)
                  for t, p in all_paths
                  if not _chain_ended(p)]
    complete.sort()
    incomplete.sort()
    return {
        "cyclic": cyclic, "tainted": tainted,
        "complete": [(t, p) for _k, _b, t, p in complete[:10]],
        "incomplete": [(t, p) for _k, _b, t, p in incomplete[:10]],
        "weights": weight, "status": status,
    }


def ref_run(events_path: str, samples_path: str, deps_path: str) -> dict:
    raw_events, samples, deps = read_materials(events_path, samples_path, deps_path)
    folded, conflicts = fold_events(raw_events)
    jobs = ref_lifecycle(folded)
    accounting = ref_account(jobs, folded, samples)
    paths = ref_critical_paths(jobs, accounting, deps)
    return {"jobs": jobs, "accounting": accounting, "paths": paths,
            "conflicts": conflicts}
