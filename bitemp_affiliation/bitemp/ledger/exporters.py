"""CSV / JSON 导出：每个版本一套可离线核对的平面文件。"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from . import queries, store


def _write_csv(path: Path, header: list[str], rows: list[tuple]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return len(rows)


def export_version(db_path: str, out_dir: str,
                   version_id: str | None = None) -> dict[str, int]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    con = store.connect_readonly(db_path)
    counts: dict[str, int] = {}
    try:
        vid = queries._resolve_version(con, version_id)

        rows = con.execute(
            "SELECT job_id, status, is_terminal, success_attempt, issue, "
            "conflict_event_ids FROM jobs WHERE version_id=? "
            "ORDER BY job_id", [vid]).fetchall()
        counts["jobs.csv"] = _write_csv(
            out / "jobs.csv",
            ["job_id", "status", "is_terminal", "success_attempt", "issue",
             "conflict_event_ids"], rows)

        rows = con.execute("""
            SELECT job_id, attempt_id, seq, outcome, retry_of, run_seconds,
                   covered_seconds, coverage_ratio, cpu_core_seconds,
                   rss_byte_seconds, peak_rss_bytes
            FROM attempts WHERE version_id=?
            ORDER BY job_id, seq
        """, [vid]).fetchall()
        counts["attempts.csv"] = _write_csv(
            out / "attempts.csv",
            ["job_id", "attempt_id", "seq", "outcome", "retry_of",
             "run_seconds", "covered_seconds", "coverage_ratio",
             "cpu_core_seconds", "rss_byte_seconds", "peak_rss_bytes"], rows)

        rows = con.execute("""
            SELECT job_id, attempt_id, segment_id, node, start_epoch,
                   end_epoch, run_seconds, start_event_id, end_event_id
            FROM run_segments WHERE version_id=?
            ORDER BY job_id, attempt_id, segment_id
        """, [vid]).fetchall()
        counts["run_segments.csv"] = _write_csv(
            out / "run_segments.csv",
            ["job_id", "attempt_id", "segment_id", "node", "start_epoch",
             "end_epoch", "run_seconds", "start_event_id", "end_event_id"],
            rows)

        rows = con.execute("""
            SELECT job_id, attempt_id, segment_id, slice_no, from_epoch,
                   to_epoch, overlap_seconds, cpu_cores, peak_rss_bytes,
                   boot_id, sample_from, sample_to, sample_line, missing
            FROM accounting_slices WHERE version_id=?
            ORDER BY job_id, attempt_id, segment_id, slice_no
        """, [vid]).fetchall()
        counts["accounting_slices.csv"] = _write_csv(
            out / "accounting_slices.csv",
            ["job_id", "attempt_id", "segment_id", "slice_no", "from_epoch",
             "to_epoch", "overlap_seconds", "cpu_cores", "peak_rss_bytes",
             "boot_id", "sample_from", "sample_to", "sample_line",
             "missing"], rows)

        rows = con.execute("""
            SELECT job_id, attempt_id, segment_id, from_epoch, to_epoch,
                   seconds FROM missing_intervals
            WHERE version_id=?
            ORDER BY job_id, attempt_id, segment_id, from_epoch
        """, [vid]).fetchall()
        counts["missing_intervals.csv"] = _write_csv(
            out / "missing_intervals.csv",
            ["job_id", "attempt_id", "segment_id", "from_epoch", "to_epoch",
             "seconds"], rows)

        rows = con.execute(
            "SELECT status, job_count, run_seconds_all, run_seconds_success, "
            "cpu_core_seconds_all, cpu_core_seconds_success, "
            "rss_byte_seconds_all, rss_byte_seconds_success "
            "FROM queue_summary WHERE version_id=? ORDER BY status",
            [vid]).fetchall()
        counts["queue_summary.csv"] = _write_csv(
            out / "queue_summary.csv",
            ["status", "job_count", "run_seconds_all", "run_seconds_success",
             "cpu_core_seconds_all", "cpu_core_seconds_success",
             "rss_byte_seconds_all", "rss_byte_seconds_success"], rows)

        rows = con.execute(
            "SELECT path_rank, total_run_seconds, job_ids, weights, complete "
            "FROM critical_paths WHERE version_id=? ORDER BY path_rank",
            [vid]).fetchall()
        counts["critical_paths.csv"] = _write_csv(
            out / "critical_paths.csv",
            ["path_rank", "total_run_seconds", "job_ids", "weights",
             "complete"], rows)

        rows = con.execute(
            "SELECT issue_code, detail, conflict_event_ids FROM job_issues "
            "WHERE version_id=? ORDER BY job_id, issue_code", [vid]).fetchall()
        counts["job_issues.csv"] = _write_csv(
            out / "job_issues.csv",
            ["issue_code", "detail", "conflict_event_ids"], rows)

        rows = con.execute(
            "SELECT issue_code, detail, related_jobs FROM dep_issues "
            "WHERE version_id=? ORDER BY issue_code", [vid]).fetchall()
        counts["dep_issues.csv"] = _write_csv(
            out / "dep_issues.csv",
            ["issue_code", "detail", "related_jobs"], rows)
    finally:
        con.close()

    payload = {
        "version_id": vid,
        "queue_summary": queries.queue_summary(db_path, vid),
        "critical_paths": queries.critical_paths(db_path, vid),
        "versions": queries.list_versions(db_path),
    }
    with (out / "summary.json").open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    counts["summary.json"] = 1
    return counts
