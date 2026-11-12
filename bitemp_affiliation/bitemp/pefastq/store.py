"""DuckDB 表结构与连接。

表：
  meta        运行参数与约定（键值）
  sources     清单中的每个源：状态（pending/ok/failed）、记录数、非法数、错误
  records     每条物理记录一行（含非法记录）：可指回 source_id、记录序号、起始行
  ends        合法记录按 (样本, 读段名, 端, 规范化序列, 质量串) 去重后的"端"
  key_states  每个 (样本, 读段名) 键的唯一终态
  pairs       两端齐全进入清洗的键（清洗后的序列/质量 + 原始长度）
  stats       读段/碱基统计（scope='*' 为全局，否则为样本名）

重复运行会整体重建这些表（reset）。磁盘库可重新打开查看明细（where 入口）。
"""
from __future__ import annotations

from pathlib import Path

import duckdb


def _sql_lit(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"

DDL = [
    "CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)",
    """
    CREATE TABLE sources (
        source_id TEXT PRIMARY KEY,
        sample TEXT NOT NULL,
        mate INTEGER NOT NULL,
        path TEXT NOT NULL,
        resolved_path TEXT NOT NULL,
        status TEXT NOT NULL,          -- pending | ok | failed
        records INTEGER NOT NULL,      -- 已读到的物理记录数
        invalid INTEGER NOT NULL,      -- 其中非法记录数
        error TEXT                     -- status='failed' 时的原因
    )
    """,
    """
    CREATE TABLE records (
        source_id TEXT NOT NULL,
        rec_no INTEGER NOT NULL,       -- 记录在本源内的序号（1 起）
        start_line INTEGER NOT NULL,   -- 本记录第一行的物理行号（1 起）
        ok BOOLEAN NOT NULL,
        err_code TEXT,                 -- E_* 错误码（ok 时为 NULL）
        err_msg TEXT,
        sample TEXT NOT NULL,          -- 清单口径（冗余，便于追查）
        mate INTEGER NOT NULL,
        name TEXT,                     -- 规范化读段名（无法确定为 NULL）
        key_known BOOLEAN NOT NULL,    -- 是否能确定 (样本, 读段名) 键
        header TEXT,                   -- 原始头行文本（'@' 之后；无法解码为 NULL）
        seq TEXT,                      -- 规范化序列（非法记录为 NULL）
        qual TEXT
    )
    """,
    """
    CREATE TABLE ends (
        sample TEXT NOT NULL,
        name TEXT NOT NULL,
        mate INTEGER NOT NULL,
        seq TEXT NOT NULL,             -- 规范化完整序列（未清洗）
        qual TEXT NOT NULL,
        copies INTEGER NOT NULL,       -- 内容完全相同的记录份数
        sources TEXT NOT NULL          -- 各份来源 "source_id#rec_no"，逗号分隔
    )
    """,
    """
    CREATE TABLE key_states (
        sample TEXT NOT NULL,
        name TEXT NOT NULL,
        state TEXT NOT NULL,           -- paired | filtered | orphan | conflict | rejected
        PRIMARY KEY (sample, name)
    )
    """,
    """
    CREATE TABLE pairs (
        sample TEXT NOT NULL,
        name TEXT NOT NULL,
        seq1 TEXT NOT NULL,            -- 清洗后的 R1
        qual1 TEXT NOT NULL,
        seq2 TEXT NOT NULL,
        qual2 TEXT NOT NULL,
        len1_raw INTEGER NOT NULL,     -- 清洗前长度
        len2_raw INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE stats (
        scope TEXT NOT NULL,           -- '*' 或样本名
        metric TEXT NOT NULL,
        value BIGINT NOT NULL
    )
    """,
]

TABLES = ["meta", "sources", "records", "ends", "key_states", "pairs", "stats"]


def connect(path: str = ":memory:", *, memory_limit: str | None = None,
            temp_directory: str | Path | None = None) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(path)
    if memory_limit:
        con.execute(f"SET memory_limit = {_sql_lit(memory_limit)}")
    if temp_directory:
        Path(temp_directory).mkdir(parents=True, exist_ok=True)
        con.execute(f"SET temp_directory = {_sql_lit(str(temp_directory))}")
    for stmt in DDL:
        con.execute(stmt.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1))
    return con


def connect_readonly(path: str) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(path, read_only=True)


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
