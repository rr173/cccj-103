"""分摊管线的 DuckDB 持久层。

表
--
  alloc_rule_version      规则版本（固定摘要；同摘要复用原版本）
  alloc_rule_edge         版本内的边（已约分）
  alloc_item              事项（item_key 主键 + 内容摘要，幂等/冲突）
  alloc_run               一次计算（绑定 version_id + asof + kind + 统计）
  alloc_run_item          参与计算的事项快照（含来源机构、是否 unresolved）
  alloc_run_leaf          末级金额（整数）
  alloc_run_weight        末级精确权重（分子/分母字符串，无损）
  alloc_run_edge_flow     实际参与边的精确流量权重归因

所有“导入规则 / 记事项 / 计算”都包在事务里；拒收时不产生任何半成品，
版本表始终停留在上一版可用规则。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

from .graph import Issue, validate_edges
from .model import Edge, Item, rule_digest

DDL = [
    """
    CREATE TABLE IF NOT EXISTS alloc_rule_version (
        version_id   TEXT PRIMARY KEY,
        digest       TEXT NOT NULL UNIQUE,
        edge_count   INTEGER NOT NULL,
        imported_at  TIMESTAMPTZ NOT NULL,
        note         TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS alloc_rule_edge (
        version_id   TEXT NOT NULL,
        edge_key     TEXT NOT NULL,
        src          TEXT NOT NULL,
        dst          TEXT NOT NULL,
        begin        DATE NOT NULL,
        until        DATE,
        numerator    TEXT NOT NULL,
        denominator  TEXT NOT NULL,
        PRIMARY KEY (version_id, edge_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS alloc_item (
        item_key      TEXT PRIMARY KEY,
        person_key    TEXT NOT NULL,
        day           DATE NOT NULL,
        amount        TEXT NOT NULL,        -- 整数以文本存，避免任何精度/溢出问题
        content_hash  TEXT NOT NULL,
        imported_at   TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS alloc_run (
        run_id       TEXT PRIMARY KEY,
        version_id   TEXT NOT NULL,
        asof_at      TIMESTAMPTZ NOT NULL,
        kind         TEXT NOT NULL,
        created_at   TIMESTAMPTZ NOT NULL,
        item_count   INTEGER NOT NULL,
        resolved     INTEGER NOT NULL,
        unresolved   INTEGER NOT NULL,
        node_ops     BIGINT NOT NULL,
        edge_ops     BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS alloc_run_item (
        run_id        TEXT NOT NULL,
        item_key      TEXT NOT NULL,
        person_key    TEXT NOT NULL,
        day           DATE NOT NULL,
        amount        TEXT NOT NULL,
        source_org    TEXT,                 -- NULL = unresolved
        resolved      BOOLEAN NOT NULL,
        reason        TEXT,
        PRIMARY KEY (run_id, item_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS alloc_run_leaf (
        run_id        TEXT NOT NULL,
        item_key      TEXT NOT NULL,
        terminal      TEXT NOT NULL,
        amount        TEXT NOT NULL,
        weight_num    TEXT NOT NULL,
        weight_den    TEXT NOT NULL,
        PRIMARY KEY (run_id, item_key, terminal)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS alloc_run_weight (
        run_id        TEXT NOT NULL,
        item_key      TEXT NOT NULL,
        terminal      TEXT NOT NULL,
        weight_num    TEXT NOT NULL,
        weight_den    TEXT NOT NULL,
        PRIMARY KEY (run_id, item_key, terminal)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS alloc_run_edge_flow (
        run_id        TEXT NOT NULL,
        item_key      TEXT NOT NULL,
        edge_key      TEXT NOT NULL,
        flow_num      TEXT NOT NULL,        -- 该边承载的精确流量权重 = forward[src]*w
        flow_den      TEXT NOT NULL,
        amount_num    TEXT NOT NULL,        -- 流量权重 × 事项金额（仍为精确有理数）
        amount_den    TEXT NOT NULL,
        PRIMARY KEY (run_id, item_key, edge_key)
    )
    """,
]


# ---------------------------------------------------------------- 装载：规则

@dataclass
class RuleImportReport:
    version_id: str
    digest: str
    reused: bool              # True = 相同摘要已存在，复用原版本
    edge_count: int


class RuleRejected(Exception):
    """规则整份拒收。issues 逐条给出具体问题边与可复现日期。"""

    def __init__(self, issues: list[Issue], digest: str | None = None):
        self.issues = list(issues)
        self.digest = digest
        super().__init__("规则拒收:\n" + "\n".join(i.render() for i in self.issues))


def _new_version_id(digest: str, existing_ids: set[str]) -> str:
    base = f"RV-{digest[:12]}"
    if base not in existing_ids:
        return base
    n = 1
    while f"{base}-{n}" in existing_ids:
        n += 1
    return f"{base}-{n}"


def import_rules(con, raw_edges: list[dict], *, note: str | None = None,
                 edges: list[Edge] | None = None,
                 imported_at: datetime | None = None) -> RuleImportReport:
    """事务性导入一份规则。任何校验问题 -> RuleRejected，数据库无变化。

    等价集合（与顺序/排版/约分写法无关）返回原版本，不新建。
    """
    parse_errors: list[str] = []
    parsed: list[Edge] = []
    if edges is not None:
        parsed = list(edges)
    else:
        from .model import edge_from_row
        for i, row in enumerate(raw_edges):
            try:
                parsed.append(edge_from_row(row, i))
            except ValueError as e:
                parse_errors.append(str(e))
    if parse_errors:
        raise RuleRejected([Issue("解析失败", (), date.min, "; ".join(parse_errors))])

    # 只去掉“内容完全相同”的重复行（幂等重放友好）；内容不同的同键行保留，
    # 交给 validate_edges 报“标识冲突”。
    parsed = list(dict.fromkeys(parsed))

    digest = rule_digest(parsed)
    issues = validate_edges(parsed)
    if issues:
        raise RuleRejected(issues, digest)

    imported_at = imported_at or datetime.now(timezone.utc)
    con.execute("BEGIN")
    try:
        row = con.execute(
            "SELECT version_id, edge_count FROM alloc_rule_version WHERE digest = ?",
            [digest]).fetchone()
        if row is not None:
            con.execute("COMMIT")
            return RuleImportReport(row[0], digest, True, int(row[1]))

        existing_ids = {r[0] for r in con.execute(
            "SELECT version_id FROM alloc_rule_version").fetchall()}
        version_id = _new_version_id(digest, existing_ids)
        con.execute(
            "INSERT INTO alloc_rule_version VALUES (?, ?, ?, ?, ?)",
            [version_id, digest, len(parsed), imported_at, note])
        con.executemany(
            "INSERT INTO alloc_rule_edge VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [e.as_params(version_id) for e in sorted(parsed, key=lambda x: x.edge_key)])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return RuleImportReport(version_id, digest, False, len(parsed))


def load_edges(con, version_id: str) -> list[Edge]:
    rows = con.execute(
        "SELECT edge_key, src, dst, begin, until, numerator, denominator "
        "FROM alloc_rule_edge WHERE version_id = ? ORDER BY edge_key",
        [version_id]).fetchall()
    return [Edge(r[0], r[1], r[2], r[3], r[4], int(r[5]), int(r[6]))
            for r in rows]


def get_version(con, version_id: str) -> tuple[str, str]:
    row = con.execute("SELECT version_id, digest FROM alloc_rule_version "
                      "WHERE version_id = ?", [version_id]).fetchone()
    if row is None:
        raise KeyError(f"规则版本不存在: {version_id}")
    return row[0], row[1]


def latest_version(con) -> str | None:
    row = con.execute(
        "SELECT version_id FROM alloc_rule_version ORDER BY imported_at DESC, "
        "version_id DESC LIMIT 1").fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------- 装载：事项

@dataclass
class ItemImportReport:
    received: int
    inserted: int
    duplicates: int


class ItemConflict(Exception):
    def __init__(self, conflicts: list[str]):
        self.conflicts = conflicts
        super().__init__("; ".join(conflicts))


def import_items(con, items: list[Item],
                 imported_at: datetime | None = None) -> ItemImportReport:
    """事项幂等导入：同 key 同内容跳过；同 key 不同内容整批拒收。"""
    imported_at = imported_at or datetime.now(timezone.utc)
    seen: dict[str, Item] = {}
    conflicts: list[str] = []
    for it in items:
        prev = seen.get(it.item_key)
        if prev is not None and prev.content_hash() != it.content_hash():
            conflicts.append(f"批内 item_key 冲突（内容不同）: {it.item_key}")
        seen.setdefault(it.item_key, it)

    existing = {}
    if seen:
        placeholders = ",".join("?" for _ in seen)
        for r in con.execute(
                f"SELECT item_key, person_key, day, amount, content_hash FROM alloc_item "
                f"WHERE item_key IN ({placeholders})", list(seen)).fetchall():
            existing[r[0]] = (r[1], r[2], int(r[3]), r[4])

    new: list[Item] = []
    duplicates = 0
    for key, it in seen.items():
        old = existing.get(key)
        if old is None:
            new.append(it)
            continue
        same = (old[0] == it.person_key and old[1] == it.day
                and old[2] == it.amount and old[3] == it.content_hash())
        if same:
            duplicates += 1
        else:
            conflicts.append(f"item_key 已存在且内容不同: {key}")
    if conflicts:
        raise ItemConflict(conflicts)

    if new:
        con.execute("BEGIN")
        try:
            con.executemany("INSERT INTO alloc_item VALUES (?, ?, ?, ?, ?, ?)",
                            [it.as_params(imported_at) for it in new])
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    return ItemImportReport(len(items), len(new), duplicates)


def load_items(con, item_keys: list[str] | None = None,
               day_range: tuple[date | None, date | None] | None = None) -> list[Item]:
    sql = "SELECT item_key, person_key, day, amount FROM alloc_item"
    params: list = []
    wheres: list[str] = []
    if item_keys is not None:
        if not item_keys:
            return []
        wheres.append("item_key IN (" + ",".join("?" for _ in item_keys) + ")")
        params += item_keys
    if day_range is not None:
        lo, hi = day_range
        if lo is not None:
            wheres.append("day >= ?")
            params.append(lo)
        if hi is not None:
            wheres.append("day < ?")
            params.append(hi)
    if wheres:
        sql += " WHERE " + " AND ".join(wheres)
    sql += " ORDER BY item_key"
    return [Item(r[0], r[1], r[2], int(r[3]))
            for r in con.execute(sql, params).fetchall()]


# ---------------------------------------------------------------- 计算落库

def _fraction_parts(f):
    return str(f.numerator), str(f.denominator)


def _new_run_id(con, version_id: str, asof: datetime, kind: str) -> str:
    base = (f"RUN-{kind}-{version_id}-{asof.strftime('%Y%m%dT%H%M%SZ')}"
            f"-{len(con.execute('SELECT run_id FROM alloc_run').fetchall()):04d}")
    return base


def persist_result(con, result) -> str:
    """把 ComputeResult 完整落库（一个事务）。返回 run_id。"""
    con.execute("BEGIN")
    try:
        run_id = _new_run_id(con, result.version_id, result.asof, result.kind)
        con.execute(
            "INSERT INTO alloc_run VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [run_id, result.version_id, result.asof, result.kind,
             datetime.now(timezone.utc), len(result.items),
             len(result.resolved_items), len(result.unresolved),
             result.stats.get("node_ops", 0), result.stats.get("edge_ops", 0)])
        item_rows, leaf_rows, weight_rows, flow_rows = [], [], [], []
        for a in result.items:
            it = a.item
            item_rows.append([run_id, it.item_key, it.person_key, it.day,
                              str(it.amount), a.source_org, a.resolved,
                              a.reason])
            if a.resolved:
                for t in a.terminals:
                    leaf_rows.append([run_id, it.item_key, t.terminal,
                                      str(t.amount), *_fraction_parts(t.weight)])
                for t in a.terminals:
                    weight_rows.append([run_id, it.item_key, t.terminal,
                                        *_fraction_parts(t.weight)])
                for e, flow in a.flows:
                    # 金额归因：流量权重 × 事项金额，保持精确有理数（不取整）
                    amount_flow = flow * it.amount
                    flow_rows.append([run_id, it.item_key, e.edge_key,
                                      *_fraction_parts(flow),
                                      *_fraction_parts(amount_flow)])
        if item_rows:
            con.executemany(
                "INSERT INTO alloc_run_item VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                item_rows)
        if leaf_rows:
            con.executemany(
                "INSERT INTO alloc_run_leaf VALUES (?, ?, ?, ?, ?, ?)", leaf_rows)
        if weight_rows:
            con.executemany(
                "INSERT INTO alloc_run_weight VALUES (?, ?, ?, ?, ?)", weight_rows)
        if flow_rows:
            con.executemany(
                "INSERT INTO alloc_run_edge_flow VALUES (?, ?, ?, ?, ?, ?, ?)",
                flow_rows)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return run_id
