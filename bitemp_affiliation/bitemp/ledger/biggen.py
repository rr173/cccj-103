"""较大事件流生成器：规模证据与流式处理的内存口径验证。

生成 n_jobs 个逻辑 job，每个含若干 attempt（含抢占/检查点/失败重试），
事件按随机顺序写出；采样窗按节点生成并制造部分缺测。配套 run_bigtest
记录导入/核算耗时、进程峰值常驻内存（resource.getrusage），并抽查
部分汇总，证明规模增大时整个事件集不会长期驻留 Python 对象——
事件按批进 DuckDB 暂存表，重建阶段每次只载入一个 job 的事件。
"""
from __future__ import annotations

import csv
import json
import random
from pathlib import Path


def write_big_dataset(out_dir: str | Path, n_jobs: int = 4000,
                      attempts_per_job: int = 3, seed: int = 23) -> dict:
    out = Path(out_dir)
    data = out / "data"
    data.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    # 节点被多个 job 复用，但每个 job 用全局唯一 boot_id：同一
    # node/boot_id 至多一个作业的窗，天然不相交；不同 boot_id 的窗
    # 允许重叠，覆盖「节点重启后多次观测会话」的场景。
    n_nodes = max(8, (n_jobs + 3) // 4)
    base = 1_700_000_000  # 固定基准 epoch，避免随墙钟漂移

    events: list[dict] = []
    samples: list[list] = []
    t_cursor = base
    eid_seq = 0

    def next_eid() -> str:
        nonlocal eid_seq
        eid_seq += 1
        return f"big-e{eid_seq:08d}"

    for j in range(n_jobs):
        job = f"JOB{j:06d}"
        t = t_cursor
        t_cursor += attempts_per_job * 120 + rng.randrange(0, 60)
        events.append({"event_id": next_eid(), "job_id": job,
                       "attempt_id": f"{job}-a0", "event_type": "submitted",
                       "at": _iso(t), "node": "", "checkpoint_id": "",
                       "retry_of": ""})
        node = f"node{j % n_nodes:05d}"
        prev = ""
        win_seq = 0
        for k in range(attempts_per_job):
            aid = f"{job}-a{k}"
            t += rng.randrange(5, 30)
            events.append({"event_id": next_eid(), "job_id": job,
                           "attempt_id": aid, "event_type": "started",
                           "at": _iso(t), "node": node, "checkpoint_id": "",
                           "retry_of": prev})
            seg_start = t
            # 中段随机保存检查点并抢占（最后一次尝试直接收尾）
            if k < attempts_per_job - 1:
                t += rng.randrange(10, 40)
                cp = f"{job}-cp{k}"
                events.append({"event_id": next_eid(), "job_id": job,
                               "attempt_id": aid,
                               "event_type": "checkpoint_saved",
                               "at": _iso(t), "node": node,
                               "checkpoint_id": cp, "retry_of": ""})
                t += rng.randrange(5, 20)
                events.append({"event_id": next_eid(), "job_id": job,
                               "attempt_id": aid, "event_type": "preempted",
                               "at": _iso(t), "node": node,
                               "checkpoint_id": "", "retry_of": ""})
                prev = aid
            else:
                t += rng.randrange(20, 60)
                etype = "succeeded" if rng.random() < 0.9 else "failed"
                events.append({"event_id": next_eid(), "job_id": job,
                               "attempt_id": aid, "event_type": etype,
                               "at": _iso(t), "node": node,
                               "checkpoint_id": "", "retry_of": ""})
            # 每个节点生成可能不连续的采样窗（含缺测）。
            # 每个窗用唯一 boot_id：同 node/boot_id 永不相交，
            # 同节点不同 boot_id 的窗可重叠（节点重启后的多次观测会话）
            if rng.random() < 0.85:
                wf = seg_start
                wt = t
                # 窗起点在运行起点附近抖动，制造截断/缺测
                f = wf - rng.randrange(0, 8)
                to = wt + rng.randrange(-6, 8)
                if to > f:
                    win_seq += 1
                    samples.append([
                        node, f"boot-{j:06d}-{win_seq:02d}",
                        _iso(f), _iso(to),
                        round(rng.uniform(0.5, 16.0), 3),
                        rng.randrange(10_000, 8_000_000_000)])

    rng.shuffle(events)
    rng.shuffle(samples)
    with (data / "events.jsonl").open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    with (data / "samples.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["node", "boot_id", "sample_from", "sample_to",
                    "cpu_cores", "peak_rss_bytes"])
        w.writerows(samples)
    # 无环的链式依赖（每 4 个 job 一条小链），避免污染主路径
    deps = []
    for j in range(0, n_jobs - 1, 4):
        deps.append([f"JOB{j:06d}", f"JOB{j + 1:06d}"])
    with (data / "dependencies.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["parent_job", "child_job"])
        w.writerows(deps)
    return {"dir": str(data), "n_events": len(events),
            "n_samples": len(samples), "n_deps": len(deps),
            "n_jobs": n_jobs}


def _iso(epoch: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00")
