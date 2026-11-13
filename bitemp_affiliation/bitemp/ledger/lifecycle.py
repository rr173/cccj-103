"""按 (at, event_id UTF-8 字节序) 重建每个逻辑 job 的生命周期。

状态机按 attempt 维护（与产品其他子工具的实现完全独立，参考核对程序
在 refcheck.py 里另写一遍）。单个 job 出现不可能的迁移即标 invalid，
保留全部原始事件与已观测片段供排查，同批其他 job 不受影响。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import RawEvent

# attempt 运行态
ST_FRESH = "fresh"            # 已登记但尚未 started
ST_RUNNING = "running"
ST_CHECKPOINT = "checkpoint"  # 最近一次事件是 checkpoint_saved
ST_PREEMPTED = "preempted"
ST_SUCCEEDED = "succeeded"
ST_FAILED = "failed"

# job 终态
JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_SUCCEEDED = "succeeded"
JOB_FAILED = "failed"
JOB_INCOMPLETE = "incomplete"
JOB_INVALID = "invalid"


@dataclass
class Segment:
    segment_id: int
    node: str
    start_epoch: float
    end_epoch: float | None        # None = 日志结束时仍在运行
    start_event_id: str
    end_event_id: str | None

    @property
    def closed(self) -> bool:
        return self.end_epoch is not None


@dataclass
class AttemptResult:
    attempt_id: str
    seq: int
    retry_of: str
    state: str
    latest_checkpoint: str
    first_event_epoch: float
    segments: list[Segment] = field(default_factory=list)

    def outcome(self) -> str:
        return {
            ST_SUCCEEDED: "succeeded",
            ST_FAILED: "failed",
            ST_PREEMPTED: "incomplete",
        }.get(self.state, "incomplete")   # running/checkpoint/fresh -> incomplete


@dataclass
class JobReconstruction:
    job_id: str
    valid: bool
    issues: list[tuple[str, str, list[str]]]   # (code, detail, event_ids)
    attempts: dict[str, AttemptResult]
    attempt_order: list[str]
    status: str
    success_attempt: str | None

    def open_segments(self) -> list[Segment]:
        out = []
        for aid in self.attempt_order:
            out.extend(s for s in self.attempts[aid].segments if not s.closed)
        return out


def _sort_events(events: list[RawEvent]) -> list[RawEvent]:
    return sorted(events, key=lambda e: (e.at, e.event_id.encode("utf-8")))


def reconstruct_job(job_id: str, events: list[RawEvent]) -> JobReconstruction:
    """对单个 job 的全部事件做状态机还原。"""
    ordered = _sort_events(events)
    issues: list[tuple[str, str, list[str]]] = []
    attempts: dict[str, AttemptResult] = {}
    attempt_order: list[str] = []
    seen_checkpoint_targets: set[str] = set()

    def invalid(code: str, detail: str, ev: RawEvent | None = None,
                extra_ids: list[str] | None = None) -> None:
        ids = []
        if ev is not None:
            ids.append(ev.event_id)
        if extra_ids:
            ids.extend(extra_ids)
        issues.append((code, detail, ids))

    # 同一 job 内 attempt_id 重复登记（跨 job 复用在入库层全局拦截）
    def get_or_create(ev: RawEvent) -> AttemptResult | None:
        a = attempts.get(ev.attempt_id)
        if a is None:
            a = AttemptResult(
                attempt_id=ev.attempt_id, seq=len(attempt_order) + 1,
                retry_of=ev.retry_of, state=ST_FRESH,
                latest_checkpoint="", first_event_epoch=ev.at.timestamp(),
            )
            attempts[ev.attempt_id] = a
            attempt_order.append(ev.attempt_id)
        return a

    def open_segment(a: AttemptResult, ev: RawEvent) -> None:
        a.segments.append(Segment(
            segment_id=len(a.segments) + 1, node=ev.node,
            start_epoch=ev.at.timestamp(), end_epoch=None,
            start_event_id=ev.event_id, end_event_id=None))

    def close_segment(a: AttemptResult, ev: RawEvent) -> None:
        for s in reversed(a.segments):
            if not s.closed:
                s.end_epoch = ev.at.timestamp()
                s.end_event_id = ev.event_id
                return
        invalid("segment_without_start",
                f"attempt {a.attempt_id} 的 {ev.event_type} 事件没有对应的运行起点",
                ev)

    aborted = False
    prev_epoch: float | None = None
    for ev in ordered:
        if aborted:
            # invalid 后继续吃完剩余事件（原始事件全部保留在 raw_events），
            # 但不再推进状态机，避免派生二次错误
            continue
        if prev_epoch is not None and ev.at.timestamp() < prev_epoch:
            invalid("time_rewind", "事件时间早于同 job 前序事件", ev)
            aborted = True
            continue
        prev_epoch = ev.at.timestamp()

        a = get_or_create(ev)
        if a is None:
            aborted = True
            continue

        if ev.event_type == "submitted":
            if a.state != ST_FRESH:
                invalid("illegal_transition",
                        f"attempt {ev.attempt_id} 在 {a.state} 态再次收到 submitted",
                        ev)
                aborted = True
                continue
            if ev.retry_of:
                invalid("bad_retry_of",
                        "submitted 事件不应携带 retry_of（重试以新 attempt 的 started 声明）",
                        ev)
                aborted = True
                continue
            # submitted 不开启运行片段（排队时间不计运行秒数）

        elif ev.event_type == "started":
            if a.state != ST_FRESH:
                invalid("illegal_transition",
                        f"attempt {ev.attempt_id} 在 {a.state} 态收到 started",
                        ev)
                aborted = True
                continue
            if not ev.node:
                invalid("empty_node", "started 事件缺少 node", ev)
                aborted = True
                continue
            if ev.retry_of:
                parent = attempts.get(ev.retry_of)
                if parent is None:
                    invalid("bad_retry_of",
                            f"retry_of={ev.retry_of} 指向的尝试不存在或出现得更晚",
                            ev)
                    aborted = True
                    continue
                if parent.attempt_id == ev.attempt_id:
                    invalid("bad_retry_of", "retry_of 不能指向自身", ev)
                    aborted = True
                    continue
                if parent.state not in (ST_FAILED, ST_PREEMPTED):
                    invalid("bad_retry_of",
                            f"retry_of 指向的 {ev.retry_of} 处于 {parent.state}，"
                            "只有失败/被抢占的尝试可被重试",
                            ev)
                    aborted = True
                    continue
                if parent.state == ST_PREEMPTED and not parent.latest_checkpoint:
                    invalid("bad_retry_of",
                            f"被抢占尝试 {ev.retry_of} 没有任何检查点，无法续体重试",
                            ev)
                    aborted = True
                    continue
                a.retry_of = ev.retry_of
            a.state = ST_RUNNING
            open_segment(a, ev)

        elif ev.event_type == "checkpoint_saved":
            if a.state not in (ST_RUNNING, ST_CHECKPOINT):
                invalid("illegal_transition",
                        f"attempt {ev.attempt_id} 在非运行态 {a.state} 保存检查点",
                        ev)
                aborted = True
                continue
            if not ev.checkpoint_id:
                invalid("missing_checkpoint_id",
                        "checkpoint_saved 事件缺少 checkpoint_id", ev)
                aborted = True
                continue
            if ev.checkpoint_id in seen_checkpoint_targets:
                invalid("duplicate_checkpoint_id",
                        f"checkpoint_id {ev.checkpoint_id} 在该 job 内重复使用", ev)
                aborted = True
                continue
            seen_checkpoint_targets.add(ev.checkpoint_id)
            a.latest_checkpoint = ev.checkpoint_id
            a.state = ST_CHECKPOINT

        elif ev.event_type == "preempted":
            if a.state not in (ST_RUNNING, ST_CHECKPOINT):
                invalid("illegal_transition",
                        f"attempt {ev.attempt_id} 在 {a.state} 态被抢占，"
                        "抢占只能发生在运行态",
                        ev)
                aborted = True
                continue
            close_segment(a, ev)
            a.state = ST_PREEMPTED

        elif ev.event_type == "resumed":
            if a.state != ST_PREEMPTED:
                invalid("illegal_transition",
                        f"attempt {ev.attempt_id} 在 {a.state} 态收到 resumed，"
                        "恢复前必须先被抢占",
                        ev)
                aborted = True
                continue
            if not ev.checkpoint_id:
                invalid("missing_checkpoint_id",
                        "resumed 事件缺少 checkpoint_id", ev)
                aborted = True
                continue
            if ev.checkpoint_id != a.latest_checkpoint:
                invalid("stale_checkpoint",
                        f"resumed 引用检查点 {ev.checkpoint_id!r}，"
                        f"但该次运行最新检查点为 {a.latest_checkpoint!r}",
                        ev)
                aborted = True
                continue
            if not ev.node:
                invalid("empty_node", "resumed 事件缺少 node", ev)
                aborted = True
                continue
            a.state = ST_RUNNING
            open_segment(a, ev)

        elif ev.event_type in ("succeeded", "failed"):
            if a.state not in (ST_RUNNING, ST_CHECKPOINT):
                invalid("terminal_from_non_running",
                        f"attempt {ev.attempt_id} 从 {a.state} 态直接进入 {ev.event_type}",
                        ev)
                aborted = True
                continue
            close_segment(a, ev)
            a.state = ST_SUCCEEDED if ev.event_type == "succeeded" else ST_FAILED
        else:  # pragma: no cover - 解析层已拦截未知类型
            invalid("unknown_event_type", f"未知事件类型 {ev.event_type}", ev)
            aborted = True
            continue

        if not aborted:
            # 同一 job 的运行区间两两不相交（含端点：半开区间，闭端允许相接）
            for other_aid in attempt_order:
                other = attempts[other_aid]
                for s_other in other.segments:
                    for s_new in a.segments:
                        if s_new is s_other:
                            continue
                        end_new = s_new.end_epoch if s_new.end_epoch is not None \
                            else ev.at.timestamp()
                        end_other = s_other.end_epoch if s_other.end_epoch is not None \
                            else ev.at.timestamp()
                        if (s_new.start_epoch < end_other
                                and s_other.start_epoch < end_new):
                            invalid("overlapping_run_segments",
                                    f"attempt {other_aid} 与 {ev.attempt_id} 的"
                                    "运行时段相交",
                                    ev)
                            aborted = True
                            break
                    if aborted:
                        break
                if aborted:
                    break

    # ---------------------------------------------------------------- 判定
    if issues:
        status = JOB_INVALID
        success = None
        valid = False
    else:
        valid = True
        success = next((aid for aid in attempt_order
                        if attempts[aid].state == ST_SUCCEEDED), None)
        if success is not None:
            status = JOB_SUCCEEDED
        elif any(a.state == ST_FAILED for a in attempts.values()):
            status = JOB_FAILED
        elif all(a.state == ST_FRESH for a in attempts.values()):
            status = JOB_QUEUED
        else:
            status = JOB_INCOMPLETE
    return JobReconstruction(
        job_id=job_id, valid=valid, issues=issues, attempts=attempts,
        attempt_order=attempt_order, status=status, success_attempt=success)
