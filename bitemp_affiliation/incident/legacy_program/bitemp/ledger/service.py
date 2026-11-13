"""导入与核算编排：流式解析 -> 暂存 -> 全局校验 -> 落原始事实 -> 重建衍生。

内存口径：events.jsonl 按批读入 DuckDB 暂存表，生命周期重建按 job
分批从库里取事件（每批处理完即释放），资源核算只载入与当前 attempt
节点/时段相关的采样窗。整个事件集不会长期驻留 Python 对象。

同一套归约同时服务两条路径：

- ``import_ledger``：校验输入后把原始事实（raw_events/raw_samples/
  raw_deps）与版本登记一并写入，再由 :func:`_derive` 从原始事实重建
  全部衍生表（jobs/attempts/run_segments/accounting_slices/...）。
- :func:`repair_version`：对已入库的某个版本，只读取该版本留存的原始
  事实，重算并原子替换它的衍生记录；版本标识、原始事实、采样积分与
  执行片段保持前后一致。修复不再依赖源文件，也不走另一套算法。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import store
from ._bootstrap import ensure_vendor_duckdb
from .accounting import account_segment
from .dependencies import build_graph, longest_paths
from .lifecycle import reconstruct_job
from .model import DepEdge, LedgerInputError, RawEvent, RawSample, utc_iso
from .parser import CanonicalHasher, iter_dependencies, iter_events, iter_samples

ensure_vendor_duckdb()

STAGING_EVENTS = "_stage_events"
STAGING_SAMPLES = "_stage_samples"

# 衍生表（全部可由 raw_* 重算）；维护命令只覆盖这些表
DERIVED_TABLES = [
    "jobs", "attempts", "run_segments", "job_issues", "accounting_slices",
    "missing_intervals", "dep_issues", "critical_paths", "queue_summary",
]


@dataclass
class ImportResult:
    db_path: str
    version_id: str
    reused: bool
    n_events: int
    n_samples: int
    n_deps: int
    n_jobs: int
    n_invalid: int
    n_queued: int
    fatal_errors: list[str]
    job_issue_count: int
    dep_issue_count: int


@dataclass
class DerivedDigest:
    """衍生结果的紧凑摘要，用于判断维护前后哪些表实际发生变化。"""
    jobs: dict[str, tuple] = field(default_factory=dict)
    attempts: dict[tuple[str, str], tuple] = field(default_factory=dict)
    critical_paths: list[tuple] = field(default_factory=list)
    queue_summary: list[tuple] = field(default_factory=list)
    table_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class RebuildStats:
    n_jobs: int = 0
    n_invalid: int = 0
    n_queued: int = 0
    n_issues: int = 0
    dep_issue_count: int = 0
    digest: DerivedDigest = field(default_factory=DerivedDigest)


@dataclass
class RepairResult:
    db_path: str
    version_id: str
    check_only: bool
    tables_changed: list[str]
    changes: list[str]
    new_counts: dict[str, int]


def _create_staging(con) -> None:
    con.execute(f"""
        CREATE TEMP TABLE {STAGING_EVENTS} (
            event_id TEXT, job_id TEXT, attempt_id TEXT, event_type TEXT,
            at_utc TEXT, at_epoch DOUBLE, node TEXT, checkpoint_id TEXT,
            retry_of TEXT, source_line INTEGER)
    """)
    con.execute(f"""
        CREATE TEMP TABLE {STAGING_SAMPLES} (
            node TEXT, boot_id TEXT, sample_from TEXT, sample_to TEXT,
            from_epoch DOUBLE, to_epoch DOUBLE, cpu_cores DOUBLE,
            peak_rss_bytes DOUBLE, source_line INTEGER)
    """)


def _stage_inputs(con, events_path, samples_path, deps_path,
                  hasher: CanonicalHasher, batch_size: int):
    n_events = n_samples = 0
    for batch in iter_events(events_path, hasher, batch_size):
        con.executemany(
            f"INSERT INTO {STAGING_EVENTS} VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(e.event_id, e.job_id, e.attempt_id, e.event_type,
              utc_iso(e.at), e.at.timestamp(), e.node, e.checkpoint_id,
              e.retry_of, e.source_line) for e in batch])
        n_events += len(batch)
    for batch in iter_samples(samples_path, hasher, batch_size):
        con.executemany(
            f"INSERT INTO {STAGING_SAMPLES} VALUES (?,?,?,?,?,?,?,?,?)",
            [(s.node, s.boot_id, utc_iso(s.sample_from), utc_iso(s.sample_to),
              s.sample_from.timestamp(), s.sample_to.timestamp(),
              s.cpu_cores, s.peak_rss_bytes, s.source_line) for s in batch])
        n_samples += len(batch)
    deps = iter_dependencies(deps_path, hasher)
    n_deps = len(deps)
    return n_events, n_samples, n_deps, deps


def _global_event_checks(con) -> list[str]:
    """event_id 折叠/冲突、attempt_id 跨 job 复用。返回致命错误列表。"""
    errors: list[str] = []
    # 同 event_id 内容冲突：除 source_line 外任一业务字段不同即冲突；
    # 完全相同的记录折叠（暂存表可能含多行重复）。
    conflicts = con.execute(f"""
        SELECT event_id, COUNT(*) AS n_versions FROM (
            SELECT DISTINCT event_id, job_id, attempt_id, event_type,
                   at_utc, node, checkpoint_id, retry_of
            FROM {STAGING_EVENTS})
        GROUP BY event_id HAVING COUNT(*) > 1
        ORDER BY event_id
    """).fetchall()
    for ev_id, _ in conflicts:
        rows = con.execute(f"""
            SELECT DISTINCT job_id, attempt_id, event_type, at_utc, node,
                   checkpoint_id, retry_of, source_line
            FROM {STAGING_EVENTS} WHERE event_id=?
            ORDER BY source_line
        """, [ev_id]).fetchall()
        detail = " | ".join(
            f"line{ln}: job={j} attempt={a} type={t} at={at} node={n} "
            f"cp={cp} retry={r}"
            for j, a, t, at, n, cp, r, ln in rows)
        errors.append(
            f"event_id {ev_id} 出现内容冲突的重复到达，放弃本次写入；{detail}")

    cross = con.execute(f"""
        SELECT attempt_id, COUNT(DISTINCT job_id) AS jn,
               string_agg(DISTINCT job_id, ', ' ORDER BY job_id)
        FROM {STAGING_EVENTS}
        GROUP BY attempt_id HAVING COUNT(DISTINCT job_id) > 1
        ORDER BY attempt_id
    """).fetchall()
    for aid, _, jobs in cross:
        errors.append(
            f"attempt_id {aid} 跨 job 复用（{jobs}），attempt_id 不得跨 job 复用")
    return errors


def _global_sample_checks(con) -> list[str]:
    """同一 node/boot_id 的半开采样窗不得相交。"""
    errors: list[str] = []
    bad = con.execute(f"""
        SELECT node, boot_id, source_line, from_epoch, to_epoch,
               prev_to, prev_line FROM (
            SELECT *, LAG(to_epoch) OVER (
                PARTITION BY node, boot_id ORDER BY from_epoch, to_epoch,
                source_line) AS prev_to,
                LAG(source_line) OVER (
                PARTITION BY node, boot_id ORDER BY from_epoch, to_epoch,
                source_line) AS prev_line
            FROM {STAGING_SAMPLES})
        WHERE prev_to IS NOT NULL AND from_epoch < prev_to
        ORDER BY node, boot_id, source_line
    """).fetchall()
    for node, boot, line, frm, to_, prev_to, prev_line in bad:
        errors.append(
            f"node={node} boot_id={boot} 第 {line} 行采样窗与第 {prev_line} 行"
            f"相交（半开区间 [{frm}, {to_}) 早于前窗结束 {prev_to}）")
    return errors


# ------------------------------------------------------- 原始事实读取

def _raw_job_ids(con, version_id: str) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT DISTINCT job_id FROM raw_events WHERE version_id=? "
        "ORDER BY job_id", [version_id]).fetchall()]


def _raw_events_for_job(con, version_id: str, job_id: str):
    return con.execute("""
        SELECT event_id, job_id, attempt_id, event_type, at_utc, at_epoch,
               node, checkpoint_id, retry_of, MIN(source_line) AS source_line
        FROM raw_events
        WHERE version_id=? AND job_id=?
        GROUP BY event_id, job_id, attempt_id, event_type, at_utc, at_epoch,
                 node, checkpoint_id, retry_of
        ORDER BY at_epoch, event_id
    """, [version_id, job_id]).fetchall()


def _raw_deps(con, version_id: str) -> list[DepEdge]:
    return [DepEdge(p, c, line) for p, c, line in con.execute(
        "SELECT parent_job, child_job, source_line FROM raw_deps "
        "WHERE version_id=? ORDER BY source_line", [version_id]).fetchall()]


def _row_to_event(row) -> RawEvent:
    (ev_id, job_id, aid, etype, at_utc, at_epoch, node, cp, retry,
     line) = row
    dt = datetime.fromtimestamp(at_epoch, tz=timezone.utc)
    return RawEvent(ev_id, job_id, aid, etype, dt, node, cp, retry, line)


def _load_windows_for_node(con, version_id: str, node: str,
                           lo: float, hi: float) -> list[RawSample]:
    rows = con.execute("""
        SELECT node, boot_id, sample_from, sample_to, from_epoch, to_epoch,
               cpu_cores, peak_rss_bytes, source_line
        FROM raw_samples
        WHERE version_id=? AND node=? AND from_epoch < ? AND to_epoch > ?
    """, [version_id, node, hi, lo]).fetchall()
    out = []
    for (node_, boot, sf, st, fe, te, cpu, rss, line) in rows:
        out.append(RawSample(node_, boot,
                             datetime.fromtimestamp(fe, tz=timezone.utc),
                             datetime.fromtimestamp(te, tz=timezone.utc),
                             cpu, rss, line))
    return out


def _persist_version_and_raw(con, version_id: str, n_events: int,
                             n_samples: int, n_deps: int,
                             deps: list[DepEdge]) -> None:
    """把版本登记与三份原始事实写入库；衍生表由 _derive 重建。"""
    con.execute(
        "INSERT INTO versions VALUES (?,?,?,?,?,?)",
        [version_id, store.SCHEMA_VERSION,
         datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
         + "+00:00", n_events, n_samples, n_deps])
    con.execute(
        f"INSERT INTO raw_events "
        f"SELECT ?, event_id, job_id, attempt_id, event_type, at_utc, "
        f"at_epoch, node, checkpoint_id, retry_of, MIN(source_line) "
        f"FROM {STAGING_EVENTS} GROUP BY ALL",
        [version_id])
    con.execute(
        f"INSERT INTO raw_samples SELECT ?, node, boot_id, sample_from, "
        f"sample_to, from_epoch, to_epoch, cpu_cores, peak_rss_bytes, "
        f"source_line FROM {STAGING_SAMPLES}", [version_id])
    if deps:
        con.executemany(
            "INSERT INTO raw_deps VALUES (?,?,?,?)",
            [(version_id, d.parent_job, d.child_job, d.source_line)
             for d in deps])


# ------------------------------------------------------- 衍生重建（唯一实现）

def _derive(con, version_id: str, *, write: bool,
            flush_jobs: int = 200,
            fail_after_jobs: int | None = None) -> RebuildStats:
    """从 raw_events / raw_samples / raw_deps 重建该版本的全部衍生表。

    write=True 时按批写入库（调用方负责事务与删除旧行）；write=False
    时只计算并返回摘要（审计模式），不写任何行。import 与 repair 共用
    此函数，不另写一套生命周期 / 图算法。
    """
    stats = RebuildStats()
    job_ids = _raw_job_ids(con, version_id)
    stats.n_jobs = len(job_ids)
    deps = _raw_deps(con, version_id)

    buf_jobs: list = []
    buf_issues: list = []
    buf_segments: list = []
    buf_slices: list = []
    buf_missing: list = []
    buf_attempts: list = []
    counts = {t: 0 for t in DERIVED_TABLES}

    def flush() -> None:
        if not write:
            buf_jobs.clear(); buf_issues.clear(); buf_segments.clear()
            buf_slices.clear(); buf_missing.clear(); buf_attempts.clear()
            return
        if buf_jobs:
            con.executemany("INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
                            buf_jobs)
            buf_jobs.clear()
        if buf_issues:
            con.executemany("INSERT INTO job_issues VALUES (?,?,?,?,?)",
                            buf_issues)
            buf_issues.clear()
        if buf_segments:
            con.executemany("INSERT INTO run_segments VALUES (?,?,?,?,?,?,?,?,?,?)",
                            buf_segments)
            buf_segments.clear()
        if buf_slices:
            con.executemany("INSERT INTO accounting_slices "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", buf_slices)
            buf_slices.clear()
        if buf_missing:
            con.executemany("INSERT INTO missing_intervals VALUES (?,?,?,?,?,?,?)",
                            buf_missing)
            buf_missing.clear()
        if buf_attempts:
            con.executemany("INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            buf_attempts)
            buf_attempts.clear()

    job_weights: dict[str, float] = {}
    job_statuses: dict[str, str] = {}

    for jpos, job_id in enumerate(job_ids):
        if fail_after_jobs is not None and jpos >= fail_after_jobs:
            raise RuntimeError(
                f"注入的写入中途故障：已完成 {jpos} 个 job，事务必须回滚")
        rows = _raw_events_for_job(con, version_id, job_id)
        events = [_row_to_event(r) for r in rows]
        recon = reconstruct_job(job_id, events)
        if recon.status == "invalid":
            stats.n_invalid += 1
        if recon.status == "queued":
            stats.n_queued += 1
        job_statuses[job_id] = recon.status
        job_weights[job_id] = 0.0
        conflict_ids = sorted({eid for _, _, ids in recon.issues for eid in ids})
        stats.digest.jobs[job_id] = (
            recon.status, int(recon.status in ("succeeded", "failed")),
            recon.success_attempt)
        buf_jobs.append([
            version_id, job_id, recon.status,
            int(recon.status in ("succeeded", "failed")),
            recon.success_attempt,
            recon.issues[0][1] if recon.issues else None,
            ",".join(conflict_ids) if conflict_ids else None])
        counts["jobs"] += 1
        for code, detail, ids in recon.issues:
            stats.n_issues += 1
            buf_issues.append([version_id, job_id, code, detail,
                               ",".join(sorted(ids))])
            counts["job_issues"] += 1

        # ---------------- 逐 attempt 资源核算
        for aid in recon.attempt_order:
            a = recon.attempts[aid]
            run_sec = covered = cpu_s = rss_s = 0.0
            peak: float | None = None
            for seg in a.segments:
                observed_end = seg.end_epoch
                if observed_end is None:
                    observed_end = max(
                        e.at.timestamp() for e in events if e.attempt_id == aid)
                windows = _load_windows_for_node(
                    con, version_id, seg.node, seg.start_epoch, observed_end)
                acc = account_segment(seg, observed_end, windows)
                buf_segments.append([
                    version_id, job_id, aid, seg.segment_id, seg.node,
                    seg.start_epoch, observed_end, acc.run_seconds,
                    seg.start_event_id, seg.end_event_id])
                counts["run_segments"] += 1
                for i, sl in enumerate(acc.slices, start=1):
                    buf_slices.append([
                        version_id, job_id, aid, seg.segment_id, i,
                        sl.from_epoch, sl.to_epoch, sl.overlap_seconds,
                        sl.cpu_cores, sl.peak_rss_bytes, sl.boot_id,
                        sl.sample_from, sl.sample_to, sl.sample_line,
                        int(sl.missing)])
                    counts["accounting_slices"] += 1
                for mlo, mhi in acc.missing:
                    buf_missing.append([version_id, job_id, aid,
                                        seg.segment_id, mlo, mhi, mhi - mlo])
                    counts["missing_intervals"] += 1
                run_sec += acc.run_seconds
                covered += acc.covered_seconds
                cpu_s += acc.cpu_core_seconds
                rss_s += acc.rss_byte_seconds
                if acc.peak_rss_bytes is not None:
                    peak = acc.peak_rss_bytes if peak is None \
                        else max(peak, acc.peak_rss_bytes)
            ratio = covered / run_sec if run_sec > 0 else 0.0
            outcome = a.outcome()
            job_weights[job_id] += run_sec
            stats.digest.attempts[(job_id, aid)] = (
                outcome, run_sec, covered, cpu_s, rss_s, peak)
            buf_attempts.append([
                version_id, job_id, aid, a.seq, outcome,
                a.retry_of, run_sec, covered, ratio, cpu_s, rss_s, peak])
            counts["attempts"] += 1

        if (jpos + 1) % flush_jobs == 0:
            flush()
    flush()

    # ---------------- 依赖图与最长路径（与 import 同一实现）
    known = set(job_ids)
    graph = build_graph(deps, known)
    for code, detail, ids in graph.dep_issues:
        if write:
            con.execute("INSERT INTO dep_issues VALUES (?,?,?,?)",
                        [version_id, code, detail, ",".join(ids)])
    counts["dep_issues"] = len(graph.dep_issues)
    stats.dep_issue_count = len(graph.dep_issues)
    paths = longest_paths(job_weights, job_statuses, graph)
    for pr in paths:
        if write:
            con.execute("INSERT INTO critical_paths VALUES (?,?,?,?,?,?)",
                        [version_id, pr.rank, pr.total,
                         ",".join(pr.job_ids),
                         ",".join(f"{x:g}" for x in pr.weights),
                         int(pr.complete)])
    counts["critical_paths"] = len(paths)
    stats.digest.critical_paths = [
        (pr.rank, round(pr.total, 9), tuple(pr.job_ids),
         tuple(round(x, 9) for x in pr.weights), int(pr.complete))
        for pr in paths]

    # ---------------- 队列总账（job 级去重计数，JOIN 后不能用 COUNT(*)）
    if write:
        q_rows = con.execute("""
            SELECT j.status, COUNT(DISTINCT j.job_id),
                   SUM(a.run_seconds),
                   SUM(CASE WHEN a.outcome='succeeded' THEN a.run_seconds ELSE 0 END),
                   SUM(a.cpu_core_seconds),
                   SUM(CASE WHEN a.outcome='succeeded' THEN a.cpu_core_seconds ELSE 0 END),
                   SUM(a.rss_byte_seconds),
                   SUM(CASE WHEN a.outcome='succeeded' THEN a.rss_byte_seconds ELSE 0 END)
            FROM jobs j LEFT JOIN attempts a
              ON j.version_id=a.version_id AND j.job_id=a.job_id
            WHERE j.version_id=?
            GROUP BY j.status ORDER BY j.status
        """, [version_id]).fetchall()
        con.executemany("INSERT INTO queue_summary VALUES (?,?,?,?,?,?,?,?,?)",
                        [(version_id,) + tuple(r) for r in q_rows])
        counts["queue_summary"] = len(q_rows)
    else:
        q_rows = _queue_rows_in_memory(stats)
        counts["queue_summary"] = len(q_rows)
    stats.digest.queue_summary = [
        (r[0], r[1], round(r[2] or 0.0, 9), round(r[3] or 0.0, 9),
         round(r[4] or 0.0, 9), round(r[5] or 0.0, 9),
         round(r[6] or 0.0, 9), round(r[7] or 0.0, 9)) for r in q_rows]
    stats.digest.table_counts = counts
    return stats


def _queue_rows_in_memory(stats: RebuildStats) -> list[tuple]:
    """审计模式（write=False）下按与 SQL 相同口径在内存中汇总队列总账。

    jobs/attempts 未写入库，故不能再用上面的 INSERT..SELECT；这里用已
    收集的 job 状态与 attempt 结局/数值做等价的分组求和，仅用于判断
    queue_summary 是否漂移。
    """
    statuses = {j: v[0] for j, v in stats.digest.jobs.items()}
    acc: dict[str, list] = {
        st: [0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0] for st in set(statuses.values())}
    for (job, _aid), v in stats.digest.attempts.items():
        outcome, run, _cov, cpu, rss, _peak = v
        row = acc[statuses[job]]
        row[1] += run
        if outcome == "succeeded":
            row[2] += run
            row[4] += cpu
            row[6] += rss
        row[3] += cpu
        row[5] += rss
    rows = []
    for st in sorted(acc):
        n_jobs = sum(1 for s in statuses.values() if s == st)
        rows.append((st, n_jobs) + tuple(acc[st][1:]))
    return rows


def _read_digest(con, version_id: str) -> DerivedDigest:
    """读取库中该版本现有的衍生结果（旧视图）。"""
    d = DerivedDigest()
    for job, status, term, succ in con.execute(
            "SELECT job_id, status, is_terminal, success_attempt FROM jobs "
            "WHERE version_id=?", [version_id]).fetchall():
        d.jobs[job] = (status, term, succ)
    for (job, aid, outcome, run, cov, cpu, rss, peak) in con.execute(
            "SELECT job_id, attempt_id, outcome, run_seconds, covered_seconds, "
            "cpu_core_seconds, rss_byte_seconds, peak_rss_bytes FROM attempts "
            "WHERE version_id=?", [version_id]).fetchall():
        d.attempts[(job, aid)] = (outcome, run, cov, cpu, rss, peak)
    d.critical_paths = [
        (rank, round(total, 9), tuple(job_ids.split(",")),
         tuple(_weight_tuple(weights)), int(complete))
        for rank, total, job_ids, weights, complete in con.execute(
            "SELECT path_rank, total_run_seconds, job_ids, weights, complete "
            "FROM critical_paths WHERE version_id=? ORDER BY path_rank",
            [version_id]).fetchall()]
    d.queue_summary = [
        (st, n, round(ra or 0.0, 9), round(rs or 0.0, 9),
         round(ca or 0.0, 9), round(cs or 0.0, 9),
         round(ba or 0.0, 9), round(bs or 0.0, 9))
        for st, n, ra, rs, ca, cs, ba, bs in con.execute(
            "SELECT status, job_count, run_seconds_all, run_seconds_success, "
            "cpu_core_seconds_all, cpu_core_seconds_success, "
            "rss_byte_seconds_all, rss_byte_seconds_success "
            "FROM queue_summary WHERE version_id=? ORDER BY status",
            [version_id]).fetchall()]
    for t in DERIVED_TABLES:
        d.table_counts[t] = con.execute(
            f"SELECT COUNT(*) FROM {t} WHERE version_id=?",
            [version_id]).fetchone()[0]
    return d


def _weight_tuple(text: str) -> list[float]:
    return [float(x) for x in text.split(",")] if text else []


def _num_close(a: float, b: float) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))


def _diff_digests(old: DerivedDigest, new: DerivedDigest
                  ) -> tuple[list[str], list[str]]:
    """比较旧视图与新算结果，返回 (变化的表清单, 人类可读的变化明细)。"""
    changed: set[str] = set()
    notes: list[str] = []

    for job in sorted(set(old.jobs) | set(new.jobs)):
        o, n = old.jobs.get(job), new.jobs.get(job)
        if o != n:
            changed.add("jobs")
            notes.append(f"job {job}: {o} -> {n}")

    for key in sorted(set(old.attempts) | set(new.attempts)):
        o, n = old.attempts.get(key), new.attempts.get(key)
        if o is None or n is None:
            changed.add("attempts")
            notes.append(f"attempt {key[0]}/{key[1]}: {o} -> {n}")
            continue
        if o[0] != n[0] or not all(
                _num_close(float(x) if x is not None else None,
                           float(y) if y is not None else None)
                for x, y in zip(o[1:], n[1:])):
            changed.add("attempts")
            notes.append(f"attempt {key[0]}/{key[1]}: {o} -> {n}")

    if old.critical_paths != new.critical_paths:
        changed.add("critical_paths")
        notes.append(f"critical_paths: {len(old.critical_paths)} 条 -> "
                     f"{len(new.critical_paths)} 条")
    if _queue_diff(old.queue_summary, new.queue_summary):
        changed.add("queue_summary")
        notes.append(f"queue_summary: {old.queue_summary} -> "
                     f"{new.queue_summary}")

    for t in DERIVED_TABLES:
        if old.table_counts.get(t) != new.table_counts.get(t):
            changed.add(t)
            notes.append(f"{t}: 行数 {old.table_counts.get(t)} -> "
                         f"{new.table_counts.get(t)}")
    return sorted(changed), notes


def _queue_diff(old: list[tuple], new: list[tuple]) -> bool:
    if len(old) != len(new):
        return True
    old_by = {r[0]: r for r in old}
    new_by = {r[0]: r for r in new}
    if set(old_by) != set(new_by):
        return True
    for st in old_by:
        o, n = old_by[st], new_by[st]
        if o[1] != n[1] or not all(
                _num_close(float(x or 0.0) if x is not None else 0.0,
                           float(y or 0.0) if y is not None else 0.0)
                for x, y in zip(o[2:], n[2:])):
            return True
    return False


def _delete_derived(con, version_id: str) -> None:
    for t in DERIVED_TABLES:
        con.execute(f"DELETE FROM {t} WHERE version_id=?", [version_id])


# ------------------------------------------------------- 导入

def import_ledger(db_path: str | Path, events_path: str | Path,
                  samples_path: str | Path, deps_path: str | Path,
                  batch_size: int = 2000) -> ImportResult:
    hasher = CanonicalHasher()
    con = store.connect(db_path)
    try:
        store.ensure_schema(con)
        _create_staging(con)
        con.begin()
        try:
            # 解析阶段即可能抛 LedgerInputError（坏行/坏窗/坏时间），
            # 配合 rollback 保证本次导入不留任何半成品
            n_events, n_samples, n_deps, deps = _stage_inputs(
                con, events_path, samples_path, deps_path, hasher, batch_size)

            fatal = _global_event_checks(con) + _global_sample_checks(con)
            if fatal:
                raise LedgerInputError(fatal)

            # 折叠后的唯一事件数（暂存表中完全相同的重复到达只算一条）
            n_events = con.execute(
                f"SELECT COUNT(*) FROM (SELECT DISTINCT event_id, job_id, "
                f"attempt_id, event_type, at_utc, node, checkpoint_id, "
                f"retry_of FROM {STAGING_EVENTS})").fetchone()[0]
            version_id, n_rows = hasher.digest()
            assert n_rows == n_events + n_samples + n_deps
        except Exception:
            con.rollback()
            raise
        if store.version_exists(con, version_id):
            con.rollback()  # 暂存内容丢弃，既有版本原封不动
            return ImportResult(
                str(db_path), version_id, True, n_events, n_samples, n_deps,
                n_jobs=con.execute(
                    "SELECT COUNT(*) FROM jobs WHERE version_id=?",
                    [version_id]).fetchone()[0],
                n_invalid=con.execute(
                    "SELECT COUNT(*) FROM jobs WHERE version_id=? AND status='invalid'",
                    [version_id]).fetchone()[0],
                n_queued=con.execute(
                    "SELECT COUNT(*) FROM jobs WHERE version_id=? AND status='queued'",
                    [version_id]).fetchone()[0],
                fatal_errors=[], job_issue_count=0, dep_issue_count=0)

        # ---------------- 先落原始事实，再由同一函数重建全部衍生表
        try:
            _persist_version_and_raw(con, version_id, n_events, n_samples,
                                     n_deps, deps)
            stats = _derive(con, version_id, write=True)
        except Exception:
            con.rollback()
            raise
        con.commit()
        return ImportResult(
            str(db_path), version_id, False, n_events, n_samples, n_deps,
            n_jobs=stats.n_jobs, n_invalid=stats.n_invalid,
            n_queued=stats.n_queued, fatal_errors=[],
            job_issue_count=stats.n_issues,
            dep_issue_count=stats.dep_issue_count)
    finally:
        con.close()


# ------------------------------------------------------- 维护（按版本重算）

def repair_version(db_path: str | Path, version_id: str, *,
                   check_only: bool = False,
                   fault_after_jobs: int | None = None) -> RepairResult:
    """只利用该版本留存的原始事实，重算并替换其全部衍生记录。

    - 只处理显式给出的 version_id；其他版本一行不改。
    - raw_events / raw_samples / raw_deps / versions 一律不写，版本标识
      与底层事实保持一致。
    - 全部替换（DELETE + 重建）在同一事务中完成：中途故障回滚后旧视图
      仍完整可读；重复执行不会复制条目，也不会累计数值（每次都从原始
      事实全量重算，而非在原值上累加）。
    - check_only=True 时只读审计，不开启写事务。
    """
    if check_only:
        con = store.connect_readonly(db_path)
        try:
            if not store.version_exists(con, version_id):
                raise KeyError(f"账本中不存在版本 {version_id}")
            old = _read_digest(con, version_id)
            new = _derive(con, version_id, write=False,
                          fail_after_jobs=fault_after_jobs)
            changed, notes = _diff_digests(old, new.digest)
            return RepairResult(str(db_path), version_id, True, changed, notes,
                                new.digest.table_counts)
        finally:
            con.close()

    con = store.connect(db_path)
    try:
        store.ensure_schema(con)
        if not store.version_exists(con, version_id):
            raise KeyError(f"账本中不存在版本 {version_id}")
        con.begin()
        try:
            # 事务内读取旧视图 -> 删除 -> 全量重算；任何一步失败整体回滚，
            # 读者看到的要么是完整的旧视图，要么是完整的新视图。
            old = _read_digest(con, version_id)
            _delete_derived(con, version_id)
            new = _derive(con, version_id, write=True,
                          fail_after_jobs=fault_after_jobs)
            changed, notes = _diff_digests(old, new.digest)
            con.commit()
        except Exception:
            con.rollback()
            raise
        return RepairResult(str(db_path), version_id, False, changed, notes,
                            new.digest.table_counts)
    finally:
        con.close()
