"""DuckDB 连接与表结构。

表：
  assertions      追加式原始断言（不可变，主键 assertion_id）
  dim_affiliation 双时态维表（按研究者整体重建）
  facts           依赖归属数据的事实表（事件日期落在业务时间内）
"""
from __future__ import annotations

import duckdb

DDL = [
    """
    CREATE TABLE IF NOT EXISTS assertions (
        assertion_id  TEXT PRIMARY KEY,
        researcher_id TEXT NOT NULL,
        valid_from    DATE NOT NULL,
        valid_to      DATE,                 -- NULL = +∞
        organization  TEXT NOT NULL,
        recorded_at   TIMESTAMPTZ NOT NULL, -- 一律 UTC
        revision      INTEGER NOT NULL,
        supersedes    TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dim_affiliation (
        researcher_id TEXT NOT NULL,
        organization  TEXT NOT NULL,
        eff_from      DATE NOT NULL,
        eff_to        DATE,                 -- NULL = +∞；[eff_from, eff_to) 左闭右开
        known_from    TIMESTAMPTZ NOT NULL,
        known_to      TIMESTAMPTZ           -- NULL = 直到当前仍成立
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS facts (
        fact_id       TEXT PRIMARY KEY,
        researcher_id TEXT NOT NULL,
        event_date    DATE NOT NULL,
        metric        DOUBLE
    )
    """,
]


def connect(path: str = ":memory:") -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(path)
    con.execute("SET TimeZone = 'UTC'")
    for stmt in DDL:
        con.execute(stmt)
    return con


def insert_facts(con: duckdb.DuckDBPyConnection, rows: list[dict]) -> None:
    con.executemany(
        "INSERT INTO facts VALUES (?, ?, ?, ?)",
        [(r["fact_id"], r["researcher_id"], r["event_date"], r["metric"]) for r in rows],
    )
