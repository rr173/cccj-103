"""DuckDB 表结构与落库。

表：
  meta              运行约定与容差参数（键值）
  variables         原始变量声明（含 variables.json 行号）
  indicators        指标定义（含 formulas.json 行号、推导量纲）
  raw_measurements  measurements.csv 原文（含行号）
  raw_correlations  correlations.csv 原文（含行号）
  specimen_status   每个 specimen 的状态（ok / rejected）与拒收原因
  specimen_notes    specimen 级注记（相关矩阵修正、缺失相关按 0 等）
  results           指标结果（值/u 的 SI 与显示单位两套、状态、原因、依赖链）
  gradients         指标对每个原始变量的偏导（SI 坐标）
  contributions     方差分解明细（对角贡献 + 两倍交叉贡献，可负）

重复运行会整体重建这些表（DROP 后 CREATE），磁盘库可重新打开查看明细。
"""
from __future__ import annotations

import json

import duckdb

DDL = [
    "CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)",
    """
    CREATE TABLE variables (
        name TEXT PRIMARY KEY,
        dimension TEXT NOT NULL,
        line INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE indicators (
        name TEXT PRIMARY KEY,
        expression TEXT NOT NULL,
        unit TEXT NOT NULL,
        dim TEXT NOT NULL,
        line INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE raw_measurements (
        specimen TEXT NOT NULL,
        variable TEXT NOT NULL,
        value TEXT NOT NULL,
        unit TEXT NOT NULL,
        u TEXT NOT NULL,
        line INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE raw_correlations (
        specimen TEXT NOT NULL,
        left_var TEXT NOT NULL,
        right_var TEXT NOT NULL,
        rho TEXT NOT NULL,
        line INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE specimen_status (
        specimen TEXT PRIMARY KEY,
        status TEXT NOT NULL,          -- ok | rejected
        reasons TEXT NOT NULL          -- JSON: [{message, file, line}]
    )
    """,
    """
    CREATE TABLE specimen_notes (
        specimen TEXT NOT NULL,
        note TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE results (
        specimen TEXT NOT NULL,
        indicator TEXT NOT NULL,
        status TEXT NOT NULL,          -- ok | unavailable | error
        value_si DOUBLE,
        u_si DOUBLE,
        value_display DOUBLE,
        u_display DOUBLE,
        unit TEXT,
        reason TEXT,
        chain TEXT,                    -- JSON 数组：依赖链
        notes TEXT,                    -- JSON 数组：指标级注记
        PRIMARY KEY (specimen, indicator)
    )
    """,
    """
    CREATE TABLE gradients (
        specimen TEXT NOT NULL,
        indicator TEXT NOT NULL,
        variable TEXT NOT NULL,
        grad_si DOUBLE NOT NULL,
        PRIMARY KEY (specimen, indicator, variable)
    )
    """,
    """
    CREATE TABLE contributions (
        specimen TEXT NOT NULL,
        indicator TEXT NOT NULL,
        kind TEXT NOT NULL,            -- diag | cross
        left_var TEXT NOT NULL,
        right_var TEXT NOT NULL,
        amount_si DOUBLE NOT NULL
    )
    """,
]

TABLES = ["meta", "variables", "indicators", "raw_measurements",
          "raw_correlations", "specimen_status", "specimen_notes",
          "results", "gradients", "contributions"]


def connect(path: str = ":memory:") -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(path)
    for stmt in DDL:
        con.execute(stmt.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1))
    return con


def reset(con: duckdb.DuckDBPyConnection) -> None:
    for table in TABLES:
        con.execute(f"DROP TABLE IF EXISTS {table}")
    for stmt in DDL:
        con.execute(stmt)


def set_meta(con: duckdb.DuckDBPyConnection, k: str, v: str) -> None:
    con.execute("DELETE FROM meta WHERE k = ?", [k])
    con.execute("INSERT INTO meta VALUES (?, ?)", [k, v])


def get_meta(con: duckdb.DuckDBPyConnection, k: str) -> str | None:
    row = con.execute("SELECT v FROM meta WHERE k = ?", [k]).fetchone()
    return row[0] if row else None
