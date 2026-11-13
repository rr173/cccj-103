"""导入与核算编排：流式解析 -> 暂存 -> 全局校验 -> 逐 job 重建与核算。

内存口径：events.jsonl 按批读入 DuckDB 暂存表，生命周期重建按 job
分批从库里取事件（每批处理完即释放），资源核算只载入与当前 attempt
节点/时段相关的采样窗。整个事件集不会长期驻留 Python 对象。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import store
from ._bootstrap import ensure_vendor_duckdb
from .accounting import account_segment
from .dependencies import build_graph, longest_paths
from .lifecycle import reconstruct_job
from .model import LedgerInputError, RawSample, utc_iso
from .parser import CanonicalHasher, iter_dependencies, iter_events, iter_samples

ensure_vendor_duckdb()

STAGING_EVENTS = "_stage_events"
STAGING_SAMPLES = "_stage_samples"


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


def _folded_events_for_job(con, job_id: str):
    return con.execute(f"""
        SELECT event_id, job_id, attempt_id, event_type, at_utc, at_epoch,
               node, checkpoint_id, retry_of, MIN(source_line) AS source_line
        FROM {STAGING_EVENTS}
        WHERE job_id=?
        GROUP BY event_id, job_id, attempt_id, event_type, at_utc, at_epoch,
                 node, checkpoint_id, retry_of
        ORDER BY at_epoch, event_id
    """, [job_id]).fetchall()


def _row_to_event(row) -> "object":
    from .model import RawEvent
    (ev_id, job_id, aid, etype, at_utc, at_epoch, node, cp, retry,
     line) = row
    dt = datetime.fromtimestamp(at_epoch, tz=timezone.utc)
    return RawEvent(ev_id, job_id, aid, etype, dt, node, cp, retry, line)


def _load_windows_for_node(con, node: str, lo: float, hi: float) -> list[RawSample]:
    rows = con.execute(f"""
        SELECT node, boot_id, sample_from, sample_to, from_epoch, to_epoch,
               cpu_cores, peak_rss_bytes, source_line
        FROM {STAGING_SAMPLES}
        WHERE node=? AND from_epoch < ? AND to_epoch > ?
    """, [node, hi, lo]).fetchall()
    out = []
    for (node_, boot, sf, st, fe, te, cpu, rss, line) in rows:
        out.append(RawSample(node_, boot,
                             datetime.fromtimestamp(fe, tz=timezone.utc),
                             datetime.fromtimestamp(te, tz=timezone.utc),
                             cpu, rss, line))
    return out


def _persist_version(con, version_id: str, n_events: int, n_samples: int,
                     n_deps: int) -> None:
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


def _job_weights(con, version_id: str) -> tuple[dict, dict]:
    rows = con.execute("""
        SELECT job_id, status, SUM(run_seconds)
        FROM attempts JOIN jobs USING (version_id, job_id)
        WHERE version_id=? GROUP BY job_id, status
    """, [version_id]).fetchall()
    weights: dict[str, float] = {}
    statuses: dict[str, str] = {}
    for j, st, secs in rows:
        weights[j] = weights.get(j, 0.0) + float(secs)
        statuses[j] = st
    # 没有任何 attempt 的 job（只有 submitted）权重为 0
    for (j, st) in con.execute(
            "SELECT job_id, status FROM jobs WHERE version_id=?",
            [version_id]).fetchall():
        statuses.setdefault(j, st)
        weights.setdefault(j, 0.0)
    return weights, statuses


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

        # ---------------- 逐 job 生命周期重建（每 job 独立核算）
        job_ids = [r[0] for r in con.execute(f"""
            SELECT DISTINCT job_id FROM {STAGING_EVENTS}
            ORDER BY job_id
        """).fetchall()]
        try:
            n_invalid = n_queued = n_issues = 0
            # 结果按表攒批、定期 executemany：生命周期仍逐 job 重建，
            # 常驻 Python 对象只与单批 job 数相关，不随总事件量增长
            buf_jobs: list = []
            buf_issues: list = []
            buf_segments: list = []
            buf_slices: list = []
            buf_missing: list = []
            buf_attempts: list = []
            FLUSH_JOBS = 200

            def flush() -> None:
                if buf_jobs:
                    con.executemany("INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
                                    buf_jobs)
                    buf_jobs.clear()
                if buf_issues:
                    con.executemany(
                        "INSERT INTO job_issues VALUES (?,?,?,?,?)", buf_issues)
                    buf_issues.clear()
                if buf_segments:
                    con.executemany(
                        "INSERT INTO run_segments VALUES (?,?,?,?,?,?,?,?,?,?)",
                        buf_segments)
                    buf_segments.clear()
                if buf_slices:
                    con.executemany(
                        "INSERT INTO accounting_slices "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", buf_slices)
                    buf_slices.clear()
                if buf_missing:
                    con.executemany(
                        "INSERT INTO missing_intervals VALUES (?,?,?,?,?,?,?)",
                        buf_missing)
                    buf_missing.clear()
                if buf_attempts:
                    con.executemany(
                        "INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        buf_attempts)
                    buf_attempts.clear()

            for jpos, job_id in enumerate(job_ids):
                rows = _folded_events_for_job(con, job_id)
                events = [_row_to_event(r) for r in rows]
                recon = reconstruct_job(job_id, events)
                if recon.status == "invalid":
                    n_invalid += 1
                if recon.status == "queued":
                    n_queued += 1
                conflict_ids = sorted(
                    {eid for _, _, ids in recon.issues for eid in ids})
                buf_jobs.append([
                    version_id, job_id, recon.status,
                    int(recon.status in ("succeeded", "failed")),
                    recon.success_attempt,
                    recon.issues[0][1] if recon.issues else None,
                    ",".join(conflict_ids) if conflict_ids else None])
                for code, detail, ids in recon.issues:
                    n_issues += 1
                    buf_issues.append(
                        [version_id, job_id, code, detail,
                         ",".join(sorted(ids))])

                # ---------------- 逐 attempt 资源核算
                for aid in recon.attempt_order:
                    a = recon.attempts[aid]
                    run_sec = covered = cpu_s = rss_s = 0.0
                    peak: float | None = None
                    for seg in a.segments:
                        # 开放片段：截到该 attempt 最后一个观测事件
                        observed_end = seg.end_epoch
                        if observed_end is None:
                            observed_end = max(
                                e.at.timestamp() for e in events
                                if e.attempt_id == aid)
                        windows = _load_windows_for_node(
                            con, seg.node, seg.start_epoch, observed_end)
                        acc = account_segment(seg, observed_end, windows)
                        buf_segments.append([
                            version_id, job_id, aid, seg.segment_id, seg.node,
                            seg.start_epoch, observed_end, acc.run_seconds,
                            seg.start_event_id, seg.end_event_id])
                        for i, sl in enumerate(acc.slices, start=1):
                            buf_slices.append([
                                version_id, job_id, aid, seg.segment_id, i,
                                sl.from_epoch, sl.to_epoch, sl.overlap_seconds,
                                sl.cpu_cores, sl.peak_rss_bytes, sl.boot_id,
                                sl.sample_from, sl.sample_to, sl.sample_line,
                                int(sl.missing)])
                        for mlo, mhi in acc.missing:
                            buf_missing.append([
                                version_id, job_id, aid, seg.segment_id,
                                mlo, mhi, mhi - mlo])
                        run_sec += acc.run_seconds
                        covered += acc.covered_seconds
                        cpu_s += acc.cpu_core_seconds
                        rss_s += acc.rss_byte_seconds
                        if acc.peak_rss_bytes is not None:
                            peak = acc.peak_rss_bytes if peak is None \
                                else max(peak, acc.peak_rss_bytes)
                    ratio = covered / run_sec if run_sec > 0 else 0.0
                    buf_attempts.append([
                        version_id, job_id, aid, a.seq, a.outcome(),
                        a.retry_of, run_sec, covered, ratio, cpu_s, rss_s, peak])

                if (jpos + 1) % FLUSH_JOBS == 0:
                    flush()
            flush()

            # ---------------- 依赖图与最长路径
            known = set(job_ids)
            graph = build_graph(deps, known)
            for code, detail, ids in graph.dep_issues:
                con.execute("INSERT INTO dep_issues VALUES (?,?,?,?)",
                            [version_id, code, detail, ",".join(ids)])
            weights, statuses = _job_weights(con, version_id)
            # 仅事件里出现的 job 参与路径；依赖表中引用的未知 job 不在权重表
            paths = longest_paths(weights, statuses, graph)
            for pr in paths:
                con.execute("INSERT INTO critical_paths VALUES (?,?,?,?,?,?)",
                            [version_id, pr.rank, pr.total,
                             ",".join(pr.job_ids),
                             ",".join(f"{x:g}" for x in pr.weights),
                             int(pr.complete)])

            # ---------------- 队列总账（job 级去重计数，JOIN 后不能用 COUNT(*)）
            con.execute("""
                INSERT INTO queue_summary
                SELECT ?, j.status, COUNT(DISTINCT j.job_id),
                       SUM(a.run_seconds),
                       SUM(CASE WHEN a.outcome='succeeded' THEN a.run_seconds ELSE 0 END),
                       SUM(a.cpu_core_seconds),
                       SUM(CASE WHEN a.outcome='succeeded' THEN a.cpu_core_seconds ELSE 0 END),
                       SUM(a.rss_byte_seconds),
                       SUM(CASE WHEN a.outcome='succeeded' THEN a.rss_byte_seconds ELSE 0 END)
                FROM jobs j LEFT JOIN attempts a
                  ON j.version_id=a.version_id AND j.job_id=a.job_id
                WHERE j.version_id=?
                GROUP BY j.status
            """, [version_id, version_id])

            _persist_version(con, version_id, n_events, n_samples, n_deps)
            if deps:
                con.executemany(
                    "INSERT INTO raw_deps VALUES (?,?,?,?)",
                    [(version_id, d.parent_job, d.child_job, d.source_line)
                     for d in deps])
        except Exception:
            con.rollback()
            raise
        con.commit()
        return ImportResult(
            str(db_path), version_id, False, n_events, n_samples, n_deps,
            n_jobs=len(job_ids), n_invalid=n_invalid, n_queued=n_queued,
            fatal_errors=[], job_issue_count=n_issues,
            dep_issue_count=len(graph.dep_issues))
    finally:
        con.close()
