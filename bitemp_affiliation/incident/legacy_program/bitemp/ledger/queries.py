"""查询：版本、job/attempt 明细、资源下钻、依赖路径、队列总账。

所有查询以 version_id 为界；不给版本时取最近一次导入。关闭进程重开
本地库后，任一汇总值都能沿 job -> attempt -> 运行片段 -> 核算切片 ->
原始采样窗逐级回溯。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import store


def _resolve_version(con, version_id: str | None) -> str:
    if version_id is not None:
        if not store.version_exists(con, version_id):
            raise KeyError(f"账本中不存在版本 {version_id}")
        return version_id
    latest = store.latest_version(con)
    if latest is None:
        raise KeyError("账本为空，尚未导入任何版本")
    return latest


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+00:00"


def list_versions(db_path: str) -> list[dict]:
    con = store.connect_readonly(db_path)
    try:
        rows = con.execute(
            "SELECT version_id, schema_version, created_at, n_events, "
            "n_samples, n_deps FROM versions ORDER BY created_at, version_id"
        ).fetchall()
        return [dict(version_id=r[0], schema_version=r[1], created_at=r[2],
                     n_events=r[3], n_samples=r[4], n_deps=r[5]) for r in rows]
    finally:
        con.close()


def queue_summary(db_path: str, version_id: str | None = None) -> dict:
    con = store.connect_readonly(db_path)
    try:
        vid = _resolve_version(con, version_id)
        rows = con.execute("""
            SELECT status, job_count, run_seconds_all, run_seconds_success,
                   cpu_core_seconds_all, cpu_core_seconds_success,
                   rss_byte_seconds_all, rss_byte_seconds_success
            FROM queue_summary WHERE version_id=?
            ORDER BY status
        """, [vid]).fetchall()
        return {"version_id": vid,
                "rows": [dict(zip([
                    "status", "job_count", "run_seconds_all",
                    "run_seconds_success", "cpu_core_seconds_all",
                    "cpu_core_seconds_success", "rss_byte_seconds_all",
                    "rss_byte_seconds_success"], r)) for r in rows]}
    finally:
        con.close()


def job_detail(db_path: str, job_id: str,
               version_id: str | None = None) -> dict:
    con = store.connect_readonly(db_path)
    try:
        vid = _resolve_version(con, version_id)
        jrow = con.execute(
            "SELECT job_id, status, is_terminal, success_attempt, issue, "
            "conflict_event_ids FROM jobs WHERE version_id=? AND job_id=?",
            [vid, job_id]).fetchone()
        if jrow is None:
            raise KeyError(f"版本 {vid[:12]}… 中不存在 job {job_id}")
        out = {"version_id": vid,
               "job": dict(zip(["job_id", "status", "is_terminal",
                                "success_attempt", "issue",
                                "conflict_event_ids"], jrow))}
        out["issues"] = [dict(zip(["issue_code", "detail", "conflict_event_ids"], r))
                         for r in con.execute(
            "SELECT issue_code, detail, conflict_event_ids FROM job_issues "
            "WHERE version_id=? AND job_id=? ORDER BY issue_code",
            [vid, job_id]).fetchall()]
        out["attempts"] = []
        for ar in con.execute("""
            SELECT attempt_id, seq, outcome, retry_of, run_seconds,
                   covered_seconds, coverage_ratio, cpu_core_seconds,
                   rss_byte_seconds, peak_rss_bytes
            FROM attempts WHERE version_id=? AND job_id=? ORDER BY seq
        """, [vid, job_id]).fetchall():
            aid = ar[0]
            entry = dict(zip([
                "attempt_id", "seq", "outcome", "retry_of", "run_seconds",
                "covered_seconds", "coverage_ratio", "cpu_core_seconds",
                "rss_byte_seconds", "peak_rss_bytes"], ar))
            entry["segments"] = [dict(zip([
                "segment_id", "node", "start", "end", "run_seconds",
                "start_event_id", "end_event_id"],
                (r[0], r[1], _iso(r[2]), _iso(r[3]), r[4], r[5], r[6])))
                for r in con.execute("""
                SELECT segment_id, node, start_epoch, end_epoch, run_seconds,
                       start_event_id, end_event_id FROM run_segments
                WHERE version_id=? AND job_id=? AND attempt_id=?
                ORDER BY segment_id""", [vid, job_id, aid]).fetchall()]
            entry["missing_intervals"] = [dict(zip([
                "segment_id", "start", "end", "seconds"],
                (r[0], _iso(r[1]), _iso(r[2]), r[3]))) for r in con.execute(
                "SELECT segment_id, from_epoch, to_epoch, seconds "
                "FROM missing_intervals WHERE version_id=? AND job_id=? "
                "AND attempt_id=? ORDER BY segment_id, from_epoch",
                [vid, job_id, aid]).fetchall()]
            out["attempts"].append(entry)
        # 成功尝试 vs 全部尝试的对照消耗（重试成本不藏进最终结果）
        def _sum(where: str) -> tuple[float, float, float]:
            return con.execute(
                f"SELECT COALESCE(SUM(run_seconds),0), "
                f"COALESCE(SUM(cpu_core_seconds),0), "
                f"COALESCE(SUM(rss_byte_seconds),0) FROM attempts "
                f"WHERE version_id=? AND job_id=? {where}",
                [vid, job_id]).fetchone()

        a_run, a_cpu, a_rss = _sum("")
        s_run, s_cpu, s_rss = _sum("AND outcome='succeeded'")
        out["costs"] = {
            "all_run_seconds": a_run, "all_cpu_core_seconds": a_cpu,
            "all_rss_byte_seconds": a_rss,
            "success_run_seconds": s_run,
            "success_cpu_core_seconds": s_cpu,
            "success_rss_byte_seconds": s_rss,
        }
        out["raw_events"] = [dict(zip([
            "event_id", "attempt_id", "event_type", "at_utc", "node",
            "checkpoint_id", "retry_of", "source_line"], r))
            for r in con.execute("""
            SELECT event_id, attempt_id, event_type, at_utc, node,
                   checkpoint_id, retry_of, source_line FROM raw_events
            WHERE version_id=? AND job_id=?
            ORDER BY at_epoch, event_id
        """, [vid, job_id]).fetchall()]
        return out
    finally:
        con.close()


def explain_slice(db_path: str, job_id: str, attempt_id: str,
                  segment_id: int, version_id: str | None = None) -> dict:
    """汇总下钻：某运行片段每个核算切片对应哪个采样窗（或缺测）。"""
    con = store.connect_readonly(db_path)
    try:
        vid = _resolve_version(con, version_id)
        rows = con.execute("""
            SELECT slice_no, from_epoch, to_epoch, overlap_seconds,
                   cpu_cores, peak_rss_bytes, boot_id, sample_from, sample_to,
                   sample_line, missing
            FROM accounting_slices
            WHERE version_id=? AND job_id=? AND attempt_id=? AND segment_id=?
            ORDER BY slice_no
        """, [vid, job_id, attempt_id, segment_id]).fetchall()
        if not rows:
            raise KeyError(
                f"找不到 {job_id}/{attempt_id}/片段{segment_id} 的核算切片")
        return {"version_id": vid, "slices": [dict(zip([
            "slice_no", "start", "end", "overlap_seconds", "cpu_cores",
            "peak_rss_bytes", "boot_id", "sample_from", "sample_to",
            "sample_line", "missing"],
            (r[0], _iso(r[1]), _iso(r[2]), r[3], r[4], r[5], r[6], r[7],
             r[8], r[9], bool(r[10])))) for r in rows]}
    finally:
        con.close()


def critical_paths(db_path: str, version_id: str | None = None) -> dict:
    con = store.connect_readonly(db_path)
    try:
        vid = _resolve_version(con, version_id)
        rows = con.execute("""
            SELECT path_rank, total_run_seconds, job_ids, weights, complete
            FROM critical_paths WHERE version_id=? ORDER BY path_rank
        """, [vid]).fetchall()
        issues = [dict(zip(["issue_code", "detail", "related_jobs"], r))
                  for r in con.execute(
            "SELECT issue_code, detail, related_jobs FROM dep_issues "
            "WHERE version_id=? ORDER BY issue_code", [vid]).fetchall()]
        return {"version_id": vid, "dep_issues": issues,
                "paths": [dict(zip(
                    ["path_rank", "total_run_seconds", "job_ids", "weights",
                     "complete"],
                    (r[0], r[1], r[2].split(","), r[3].split(","),
                     bool(r[4])))) for r in rows]}
    finally:
        con.close()


def to_json(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)
