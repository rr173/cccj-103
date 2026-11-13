"""可手算小数据集：覆盖需求列举的全部边界场景。

时间均为 2026-11-13 零点（UTC）附近的整 5 秒/整 10 秒；部分时间戳故意
写成 +08:00 偏移，验证导入统一换算零时区。行序由 shuffle 开关打乱，
生命周期与核算结果不得变化。

场景对照：
  J_PREEMPT   抢占 -> 从最新检查点续跑 -> 成功；两段运行中间有空档
  J_RETRY     b1 失败，b2 以 retry_of=b1 另建尝试并成功（重试成本单列）
  J_TIE_OK    同一秒两个事件，按 event_id UTF-8 字节序才得到合法迁移
  J_TIE_BAD   同一秒字节序导致不可能迁移 -> invalid，仅自身被隔离
  J_STALE     resumed 引用旧检查点 -> invalid，原始事件全部保留
  J_QUEUED    只有 submitted -> queued
  J_INCOMPLETE 日志在运行途中结束 -> incomplete，不猜终态
  J_ALPHA/J_BETA/J_OMEGA 等权依赖路径，按完整 job_id 字节序决胜
  J_TAIL      J_INCOMPLETE -> J_TAIL 证明未结束祖先会阻断后代的完整声明；
              J_TAIL 自身成功仍照常展示，只是其计算链不完整
  J_C1/J_C2/J_C3 依赖环 + 下游污染
  重复事件 e_dup 完全相同地到达两次 -> 折叠
采样场景：
  n1    窗被运行起点截断、运行末段缺测
  n2    不同 boot_id 重叠窗（boot-A/boot-B，字节序小者记账）
  n3    失败/重试两个 attempt 各自部分覆盖
  nA    窗同时被运行起止两端截断且全覆盖
"""
from __future__ import annotations

import csv
import json
import random
from pathlib import Path

# ---------------------------------------------------------------- 事件

def _ev(eid, job, aid, etype, sec, node="", cp="", retry=""):
    return {
        "event_id": eid, "job_id": job, "attempt_id": aid,
        "event_type": etype,
        "at": f"2026-11-13T00:{sec // 60:02d}:{sec % 60:02d}+00:00",
        "node": node, "checkpoint_id": cp, "retry_of": retry,
    }


def _ev_cn(eid, job, aid, etype, sec_utc, node="", cp="", retry=""):
    """用 +08:00 偏移书写同一 UTC 时刻，验证时区归一化。"""
    e = _ev(eid, job, aid, etype, sec_utc, node, cp, retry)
    e["at"] = (f"2026-11-13T08:{sec_utc // 60:02d}:{sec_utc % 60:02d}+08:00")
    return e


def build_events() -> list[dict]:
    ev: list[dict] = []

    # J_PREEMPT：[10,25) n1 15s + [35,50) n2 15s = 30s，空档 [25,35)
    ev += [
        _ev("p01", "J_PREEMPT", "pa1", "submitted", 0),
        _ev("p02", "J_PREEMPT", "pa1", "started", 10, "n1"),
        _ev_cn("p03", "J_PREEMPT", "pa1", "checkpoint_saved", 20, cp="cp-a1"),
        _ev("p04", "J_PREEMPT", "pa1", "preempted", 25, "n1"),
        _ev("p05", "J_PREEMPT", "pa1", "resumed", 35, "n2", cp="cp-a1"),
        _ev("p06", "J_PREEMPT", "pa1", "succeeded", 50, "n2"),
    ]

    # J_RETRY：b1 [10,20) 10s 失败；b2 [30,50) 20s 成功；合计 30s
    ev += [
        _ev("r01", "J_RETRY", "rb1", "submitted", 0),
        _ev("r02", "J_RETRY", "rb1", "started", 10, "n3"),
        _ev("r03", "J_RETRY", "rb1", "failed", 20, "n3"),
        _ev("r04", "J_RETRY", "rb2", "started", 30, "n3", retry="rb1"),
        _ev("r05", "J_RETRY", "rb2", "succeeded", 50, "n3"),
    ]

    # J_TIE_OK：:30 同秒 e_cp < e_pre（UTF-8），先存检查点再抢占才合法
    # 两段运行 [10,30) 与 [40,60)，合计 40s，中间空档 10s
    ev += [
        _ev("t01", "J_TIE_OK", "ta1", "submitted", 0),
        _ev("t02", "J_TIE_OK", "ta1", "started", 10, "nT"),
        _ev("e_cp", "J_TIE_OK", "ta1", "checkpoint_saved", 30, cp="cp-t1"),
        _ev("e_pre", "J_TIE_OK", "ta1", "preempted", 30, "nT"),
        _ev("t04", "J_TIE_OK", "ta1", "resumed", 40, "nT", cp="cp-t1"),
        _ev("t05", "J_TIE_OK", "ta1", "succeeded", 60, "nT"),
    ]

    # J_TIE_BAD：:10 同秒 a_succ < z_start，先处理 succeeded -> 不可能迁移
    ev += [
        _ev("u01", "J_TIE_BAD", "ua1", "submitted", 0),
        _ev("a_succ", "J_TIE_BAD", "ua1", "succeeded", 10, "nX"),
        _ev("z_start", "J_TIE_BAD", "ua1", "started", 10, "nX"),
    ]

    # J_STALE：resumed 引用 cp1，但最新检查点已是 cp2 -> invalid
    ev += [
        _ev("s01", "J_STALE", "sa1", "submitted", 0),
        _ev("s02", "J_STALE", "sa1", "started", 10, "nS"),
        _ev("s03", "J_STALE", "sa1", "checkpoint_saved", 15, cp="cp-s1"),
        _ev("s04", "J_STALE", "sa1", "checkpoint_saved", 20, cp="cp-s2"),
        _ev("s05", "J_STALE", "sa1", "preempted", 25, "nS"),
        _ev("s06", "J_STALE", "sa1", "resumed", 30, "nS", cp="cp-s1"),
        _ev("s07", "J_STALE", "sa1", "succeeded", 40, "nS"),
    ]

    # J_QUEUED：仅 submitted
    ev.append(_ev("q01", "J_QUEUED", "qa1", "submitted", 0))

    # J_INCOMPLETE：日志在运行途中结束，开放片段截到最后观测 :20
    ev += [
        _ev("i01", "J_INCOMPLETE", "ia1", "submitted", 0),
        _ev("i02", "J_INCOMPLETE", "ia1", "started", 10, "nI"),
        _ev("i03", "J_INCOMPLETE", "ia1", "checkpoint_saved", 20, cp="cp-i1"),
    ]

    # J_TAIL：自身成功，但前置 J_INCOMPLETE 尚未结束 -> 计算链不完整
    ev += [
        _ev("y01", "J_TAIL", "ja1", "submitted", 0),
        _ev("y02", "J_TAIL", "ja1", "started", 10, "nY"),
        _ev("y03", "J_TAIL", "ja1", "succeeded", 20, "nY"),
    ]

    # 三个 10s 短作业 + 两个 30s 作业（依赖决胜用）
    for job, node in [("J_C1", "nC"), ("J_C2", "nC"), ("J_C3", "nC")]:
        ev += [
            _ev(f"{job.lower()}-1", job, f"{job.lower()}a", "submitted", 0),
            _ev(f"{job.lower()}-2", job, f"{job.lower()}a", "started", 10, node),
            _ev(f"{job.lower()}-3", job, f"{job.lower()}a", "succeeded", 20, node),
        ]
    for job, node in [("J_ALPHA", "nA"), ("J_BETA", "nB"), ("J_OMEGA", "nO")]:
        ev += [
            _ev(f"{job.lower()}-1", job, f"{job.lower()}a", "submitted", 0),
            _ev(f"{job.lower()}-2", job, f"{job.lower()}a", "started", 10, node),
            _ev(f"{job.lower()}-3", job, f"{job.lower()}a", "succeeded", 40, node),
        ]

    # 完全相同的重复事件（不同行位置）-> 折叠，不重复计数
    ev.append(_ev("r02", "J_RETRY", "rb1", "started", 10, "n3"))
    return ev


# ---------------------------------------------------------------- 采样

def _win(node, boot, fsec, tsec, cpu, rss):
    f = f"2026-11-13T00:{fsec // 60:02d}:{fsec % 60:02d}+00:00"
    t = f"2026-11-13T00:{tsec // 60:02d}:{tsec % 60:02d}+00:00"
    return [node, boot, f, t, cpu, rss]


def build_samples() -> list[list]:
    rows = []
    # n1 / boot-1：窗 [5,20) 被运行起点 :10 截断；[20,35) 与 [10,25) 交 [20,25)
    rows += [
        _win("n1", "boot-1", 5, 20, 2.0, 100),
        _win("n1", "boot-1", 20, 35, 4.0, 200),
    ]
    # n2：两个不同 boot_id 的重叠窗；boot-A 字节序更小，重叠处记 boot-A
    rows += [
        _win("n2", "boot-A", 45, 65, 8.0, 500),
        _win("n2", "boot-B", 40, 55, 16.0, 900),
    ]
    # n3：b1 [10,20) 部分覆盖；b2 [30,50) 两段覆盖中间缺测
    rows += [
        _win("n3", "boot-1", 5, 15, 2.0, 50),
        _win("n3", "boot-1", 25, 40, 6.0, 300),
        _win("n3", "boot-1", 45, 55, 10.0, 400),
    ]
    # nA：单窗 [5,45) 被运行 [10,40) 两端截断但全覆盖
    rows.append(_win("nA", "boot-1", 5, 45, 1.0, 10))
    # nY：J_TAIL 的完整覆盖窗
    rows.append(_win("nY", "boot-1", 5, 25, 1.0, 10))
    return rows


def build_deps() -> list[list]:
    return [
        ["J_RETRY", "J_PREEMPT"],          # 30+30=60
        ["J_ALPHA", "J_OMEGA"],            # 30+30=60
        ["J_BETA", "J_OMEGA"],             # 30+30=60（三路等权，字节序决胜）
        ["J_INCOMPLETE", "J_OMEGA"],       # 经未终结节点 -> 不完整路径
        ["J_INCOMPLETE", "J_TAIL"],        # 未结束的前置阻断后代完整声明
        ["J_C1", "J_C2"],                  # 环
        ["J_C2", "J_C1"],
        ["J_C2", "J_C3"],                  # 环下游污染
    ]


def write_demo_dataset(out_dir: str | Path, shuffle: bool = False,
                       seed: int = 7) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    events = build_events()
    samples = build_samples()
    deps = build_deps()
    if shuffle:
        rng = random.Random(seed)
        rng.shuffle(events)
        rng.shuffle(samples)
        rng.shuffle(deps)
    with (out / "events.jsonl").open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    with (out / "samples.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["node", "boot_id", "sample_from", "sample_to",
                    "cpu_cores", "peak_rss_bytes"])
        w.writerows(samples)
    with (out / "dependencies.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["parent_job", "child_job"])
        w.writerows(deps)
    return {"dir": str(out), "n_events": len(events),
            "n_samples": len(samples), "n_deps": len(deps)}


def write_conflict_dataset(out_dir: str | Path) -> dict:
    """同 event_id 内容冲突的坏材料：导入必须整体拒绝且不产生版本。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    good = [e for e in build_events() if e["event_id"] != "r02"][:6]
    dup_a = _ev("x1", "J_RETRY", "rb1", "started", 10, "n3")
    dup_b = _ev("x1", "J_RETRY", "rb1", "started", 10, "n9")  # node 冲突
    with (out / "events.jsonl").open("w", encoding="utf-8") as f:
        for e in good + [dup_a, dup_b]:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    with (out / "samples.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["node", "boot_id", "sample_from", "sample_to",
                    "cpu_cores", "peak_rss_bytes"])
        w.writerows(build_samples())
    with (out / "dependencies.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["parent_job", "child_job"])
        w.writerows(build_deps())
    return {"dir": str(out)}
