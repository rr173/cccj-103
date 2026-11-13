"""DuckDB 持久化：原始材料、版本、生命周期、资源核算、依赖路径。

多版本共存：所有结果表带 version_id（内容摘要），同内容重复导入直接
复用既有版本不重复累加；任一记录变化产生新版本，旧版本仍可按摘要
查询。关闭进程后重开本地库，所有汇总都能经 attempt -> 运行片段 ->
核算切片 -> 采样窗回溯。
"""
from __future__ import annotations

from pathlib import Path

from ._bootstrap import ensure_vendor_duckdb

ensure_vendor_duckdb()
import duckdb  # noqa: E402

SCHEMA_VERSION = "ledger-1"

DDL = [
    # ---- 版本登记
    """
    CREATE TABLE versions (
        version_id TEXT PRIMARY KEY,
        schema_version TEXT NOT NULL,
        created_at TEXT NOT NULL,
        n_events INTEGER NOT NULL,
        n_samples INTEGER NOT NULL,
        n_deps INTEGER NOT NULL
    )
    """,
    # ---- 原始材料（折叠后的唯一事件；冲突在提交前检测，绝不入库）
    """
    CREATE TABLE raw_events (
        version_id TEXT NOT NULL, event_id TEXT NOT NULL,
        job_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
        event_type TEXT NOT NULL, at_utc TEXT NOT NULL,
        at_epoch DOUBLE NOT NULL, node TEXT NOT NULL,
        checkpoint_id TEXT NOT NULL, retry_of TEXT NOT NULL,
        source_line INTEGER NOT NULL,
        PRIMARY KEY (version_id, event_id))
    """,
    """
    CREATE TABLE raw_samples (
        version_id TEXT NOT NULL, node TEXT NOT NULL, boot_id TEXT NOT NULL,
        sample_from TEXT NOT NULL, sample_to TEXT NOT NULL,
        from_epoch DOUBLE NOT NULL, to_epoch DOUBLE NOT NULL,
        cpu_cores DOUBLE NOT NULL, peak_rss_bytes DOUBLE NOT NULL,
        source_line INTEGER NOT NULL)
    """,
    """
    CREATE TABLE raw_deps (
        version_id TEXT NOT NULL, parent_job TEXT NOT NULL,
        child_job TEXT NOT NULL, source_line INTEGER NOT NULL)
    """,
    # ---- 生命周期
    """
    CREATE TABLE jobs (
        version_id TEXT NOT NULL, job_id TEXT NOT NULL,
        status TEXT NOT NULL,            -- queued/running/succeeded/failed/incomplete/invalid
        is_terminal INTEGER NOT NULL,
        success_attempt TEXT,
        issue TEXT,
        conflict_event_ids TEXT,
        PRIMARY KEY (version_id, job_id))
    """,
    """
    CREATE TABLE attempts (
        version_id TEXT NOT NULL, job_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL, seq INTEGER NOT NULL,
        outcome TEXT NOT NULL,           -- succeeded/failed/incomplete/invalid
        retry_of TEXT NOT NULL,
        run_seconds DOUBLE NOT NULL,
        covered_seconds DOUBLE NOT NULL,
        coverage_ratio DOUBLE NOT NULL,
        cpu_core_seconds DOUBLE NOT NULL,
        rss_byte_seconds DOUBLE NOT NULL,
        peak_rss_bytes DOUBLE,
        PRIMARY KEY (version_id, job_id, attempt_id))
    """,
    """
    CREATE TABLE run_segments (
        version_id TEXT NOT NULL, job_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL, segment_id INTEGER NOT NULL,
        node TEXT NOT NULL,
        start_epoch DOUBLE NOT NULL, end_epoch DOUBLE NOT NULL,
        run_seconds DOUBLE NOT NULL,
        start_event_id TEXT NOT NULL, end_event_id TEXT,
        PRIMARY KEY (version_id, job_id, attempt_id, segment_id))
    """,
    """
    CREATE TABLE job_issues (
        version_id TEXT NOT NULL, job_id TEXT NOT NULL,
        issue_code TEXT NOT NULL, detail TEXT NOT NULL,
        conflict_event_ids TEXT NOT NULL)
    """,
    # ---- 资源核算（可回溯的最小证据单元）
    """
    CREATE TABLE accounting_slices (
        version_id TEXT NOT NULL, job_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL, segment_id INTEGER NOT NULL,
        slice_no INTEGER NOT NULL,
        from_epoch DOUBLE NOT NULL, to_epoch DOUBLE NOT NULL,
        overlap_seconds DOUBLE NOT NULL,
        cpu_cores DOUBLE,              -- NULL 表示该切片缺测
        peak_rss_bytes DOUBLE,
        boot_id TEXT,
        sample_from TEXT, sample_to TEXT, sample_line INTEGER,
        missing INTEGER NOT NULL)
    """,
    """
    CREATE TABLE missing_intervals (
        version_id TEXT NOT NULL, job_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL, segment_id INTEGER NOT NULL,
        from_epoch DOUBLE NOT NULL, to_epoch DOUBLE NOT NULL,
        seconds DOUBLE NOT NULL)
    """,
    # ---- 依赖
    """
    CREATE TABLE dep_issues (
        version_id TEXT NOT NULL, issue_code TEXT NOT NULL,
        detail TEXT NOT NULL, related_jobs TEXT NOT NULL)
    """,
    """
    CREATE TABLE critical_paths (
        version_id TEXT NOT NULL,
        path_rank INTEGER NOT NULL,        -- 1 = 全局最长
        total_run_seconds DOUBLE NOT NULL,
        job_ids TEXT NOT NULL,             -- 完整 job_id 序列
        weights TEXT NOT NULL,             -- 与 job_ids 对齐的权重
        complete INTEGER NOT NULL)         -- 0=含未终结/invalid 节点的不完整线索
    """,
    # ---- 队列总账（物化汇总，仍可经表关系下钻）
    """
    CREATE TABLE queue_summary (
        version_id TEXT NOT NULL, status TEXT NOT NULL,
        job_count INTEGER NOT NULL,
        run_seconds_all DOUBLE NOT NULL,
        run_seconds_success DOUBLE NOT NULL,
        cpu_core_seconds_all DOUBLE NOT NULL,
        cpu_core_seconds_success DOUBLE NOT NULL,
        rss_byte_seconds_all DOUBLE NOT NULL,
        rss_byte_seconds_success DOUBLE NOT NULL,
        PRIMARY KEY (version_id, status))
    """,
]

# 结果表（DROP 重建时按依赖顺序无关，整库版本化，不 DROP 旧版本）
ALL_TABLES = [
    "versions", "raw_events", "raw_samples", "raw_deps", "jobs", "attempts",
    "run_segments", "job_issues", "accounting_slices", "missing_intervals",
    "dep_issues", "critical_paths", "queue_summary",
]


def connect(db_path: str | Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(db_path))


def connect_readonly(db_path: str | Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(db_path), read_only=True)


def ensure_schema(con) -> None:
    existing = {r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='main'").fetchall()}
    for stmt in DDL:
        name = stmt.split("CREATE TABLE")[1].split("(")[0].strip()
        if name not in existing:
            con.execute(stmt)


def version_exists(con, version_id: str) -> bool:
    return con.execute(
        "SELECT 1 FROM versions WHERE version_id=? LIMIT 1",
        [version_id]).fetchone() is not None


def latest_version(con) -> str | None:
    row = con.execute(
        "SELECT version_id FROM versions ORDER BY created_at DESC, "
        "version_id LIMIT 1").fetchone()
    return row[0] if row else None


def insert_event_batch(con, version_id: str, rows: list) -> None:
    con.executemany(
        "INSERT INTO raw_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(version_id, e.event_id, e.job_id, e.attempt_id, e.event_type,
          e.at.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+00:00",
          e.at.timestamp(), e.node, e.checkpoint_id, e.retry_of,
          e.source_line) for e in rows])


def insert_sample_batch(con, version_id: str, rows: list) -> None:
    con.executemany(
        "INSERT INTO raw_samples VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(version_id, s.node, s.boot_id,
          s.sample_from.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+00:00",
          s.sample_to.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+00:00",
          s.sample_from.timestamp(), s.sample_to.timestamp(),
          s.cpu_cores, s.peak_rss_bytes, s.source_line) for s in rows])


def insert_deps(con, version_id: str, edges: list) -> None:
    con.executemany(
        "INSERT INTO raw_deps VALUES (?,?,?,?)",
        [(version_id, d.parent_job, d.child_job, d.source_line) for d in edges])
