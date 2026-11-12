"""双时态维表装配核心。

语义规则（与需求一一对应）：
  * 业务区间左闭右开 [valid_from, valid_to)，valid_to 为空表示 +∞。
  * recorded_at 统一 UTC；知识时点 K 恰好等于 recorded_at 时该断言已知（<= 语义）。
  * 知识时点 K 下可见的断言 = 已知（recorded_at <= K）且未被任何已知断言 supersedes。
  * 可见断言重叠时，按 (revision, recorded_at, assertion_id) 字典序取大者。
  * 空档保持未知（维表中不出现行），不向前填充。
  * 无效断言整批拒绝：先完成全部校验，通过后才落库，且包在事务里。
  * 幂等：内容完全相同的断言重放不产生任何变化。

知识时间轴被所有 recorded_at 切成若干“纪元”：纪元内已知集恒定，
因此每个纪元独立求有效区间，再把相邻纪元中内容完全相同的行合并（coalesce）。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime

from .model import Assertion, to_utc


class BatchRejected(Exception):
    """整批拒绝。reasons 列出全部原因；数据库不产生任何变化。"""

    def __init__(self, reasons: list[str]):
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons))


@dataclass(frozen=True)
class DimRow:
    researcher_id: str
    organization: str
    eff_from: date
    eff_to: date | None
    known_from: datetime
    known_to: datetime | None

    def as_params(self) -> tuple:
        return (self.researcher_id, self.organization, self.eff_from,
                self.eff_to, self.known_from, self.known_to)


@dataclass
class ResearcherRebuild:
    """单个研究者重建的结果：新旧区间差异 + 需重算的事实。"""
    researcher_id: str
    added: list[DimRow]                                   # 新出现的区间
    removed: list[DimRow]                                 # 消失的区间
    changed_regions: list[tuple[date, date | None]]       # 当前认知下归属发生变化的业务区间
    facts_to_recompute: list[tuple]                       # 事件日期落在变化区间内的事实


@dataclass
class BatchReport:
    received: int
    inserted: int
    duplicates: int
    rebuilds: list[ResearcherRebuild]

    @property
    def affected_researchers(self) -> list[str]:
        return [r.researcher_id for r in self.rebuilds]


# ---------------------------------------------------------------- 校验

def _load_assertions(con, researcher_id: str | None = None) -> list[Assertion]:
    sql = ("SELECT assertion_id, researcher_id, valid_from, valid_to, organization,"
           " recorded_at, revision, supersedes FROM assertions")
    params = None
    if researcher_id is not None:
        sql += " WHERE researcher_id = ?"
        params = [researcher_id]
    out = []
    for row in con.execute(sql, params).fetchall():
        out.append(Assertion(row[0], row[1], row[2], row[3], row[4],
                             to_utc(row[5]), row[6], row[7]))
    return out


def validate_batch(con, batch: list[Assertion]):
    """返回 (new, duplicates, errors)。errors 非空则调用方必须整批拒绝。"""
    errors: list[str] = []

    # 1) 字段级约束
    for a in batch:
        if not a.assertion_id or not a.researcher_id or not a.organization:
            errors.append(f"{a.assertion_id!r}: 必填字段为空")
        if a.valid_to is not None and a.valid_to <= a.valid_from:
            errors.append(f"{a.assertion_id}: valid_to({a.valid_to}) 必须大于 "
                          f"valid_from({a.valid_from})（左闭右开区间）")
        if a.revision < 0:
            errors.append(f"{a.assertion_id}: revision 不能为负")

    # 2) 批内重复 id：内容一致视为同一行（去重），内容冲突则报错
    seen: dict[str, Assertion] = {}
    for a in batch:
        prev = seen.get(a.assertion_id)
        if prev is not None and prev != a:
            errors.append(f"批内 assertion_id 冲突（内容不同）: {a.assertion_id}")
        seen.setdefault(a.assertion_id, a)

    # 3) 与库中已有断言比对：完全相同 -> 幂等重放；id 相同内容不同 -> 拒绝
    existing = {a.assertion_id: a for a in _load_assertions(con)}
    new: list[Assertion] = []
    duplicates: list[Assertion] = []
    for a in seen.values():
        old = existing.get(a.assertion_id)
        if old is None:
            new.append(a)
        elif old == a:
            duplicates.append(a)
        else:
            errors.append(f"assertion_id 已存在且内容不同: {a.assertion_id}")

    # 4) 替代链约束（在 库 ∪ 新批 的全集上检查）
    universe = dict(existing)
    for a in new:
        universe[a.assertion_id] = a

    for a in new:
        if a.supersedes is None:
            continue
        tgt = universe.get(a.supersedes)
        if tgt is None:
            errors.append(f"{a.assertion_id}: supersedes 指向不存在的断言 {a.supersedes}")
        elif tgt.researcher_id != a.researcher_id:
            errors.append(f"{a.assertion_id}: supersedes 跨研究者 "
                          f"({a.researcher_id} -> {tgt.researcher_id})")

    indeg: dict[str, int] = {}
    for a in universe.values():
        if a.supersedes is not None:
            indeg[a.supersedes] = indeg.get(a.supersedes, 0) + 1
    for tgt, n in sorted(indeg.items()):
        if n > 1:
            errors.append(f"断言 {tgt} 被 {n} 个后继替代（替代链不允许分叉）")

    # 环检测：已有数据无环且每点出度 <= 1，新环必然经过新断言
    for a in new:
        visited = {a.assertion_id}
        cur = a
        while cur.supersedes is not None:
            nxt = universe.get(cur.supersedes)
            if nxt is None:
                break
            if nxt.assertion_id in visited:
                errors.append(f"替代链存在环，回到 {nxt.assertion_id}")
                break
            visited.add(nxt.assertion_id)
            cur = nxt

    return new, duplicates, errors


# ---------------------------------------------------------------- 区间构建

def _visible_at(assertions: list[Assertion], known_at: datetime) -> list[Assertion]:
    """知识时点 known_at 下可见的断言：已知且未被已知后继替代。"""
    known = [a for a in assertions if a.recorded_at <= known_at]
    superseded = {a.supersedes for a in known if a.supersedes is not None}
    return [a for a in known if a.assertion_id not in superseded]


def _resolve_visible(visible: list[Assertion]) -> list[tuple[date, date | None, str]]:
    """把一个纪元内的可见断言解析为互不重叠的有效区间。

    用全部端点切出基本段，每段取 (revision, recorded_at, assertion_id)
    字典序最大的断言；空档不填；相邻同组织段合并。
    """
    if not visible:
        return []
    pts = sorted({p for a in visible for p in (a.valid_from, a.valid_to) if p is not None})

    def covers(a: Assertion, s: date, e: date | None) -> bool:
        if a.valid_from > s:
            return False
        if a.valid_to is None:
            return True
        return e is not None and a.valid_to >= e

    segments: list[list] = []  # [eff_from, eff_to, org]
    bounds = [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    bounds.append((pts[-1], None))  # 尾段：最后一个有限端点到 +∞
    for s, e in bounds:
        cand = [a for a in visible if covers(a, s, e)]
        if not cand:
            continue  # 空档保持未知
        w = max(cand, key=lambda a: (a.revision, a.recorded_at, a.assertion_id))
        if segments and segments[-1][1] == s and segments[-1][2] == w.organization:
            segments[-1][1] = e
        else:
            segments.append([s, e, w.organization])
    return [(s, e, org) for s, e, org in segments]


def build_dim_rows(assertions: list[Assertion]) -> list[DimRow]:
    """由某研究者的全部断言构建双时态维行（纯函数，便于测试）。"""
    if not assertions:
        return []
    rid = assertions[0].researcher_id
    assert all(a.researcher_id == rid for a in assertions)

    breakpoints = sorted({a.recorded_at for a in assertions})
    rows: list[DimRow] = []
    open_idx: dict[tuple, int] = {}  # (eff_from, eff_to, org) -> rows 下标
    for i, kf in enumerate(breakpoints):
        kt = breakpoints[i + 1] if i + 1 < len(breakpoints) else None
        for f, t, org in _resolve_visible(_visible_at(assertions, kf)):
            key = (f, t, org)
            j = open_idx.get(key)
            if j is not None and rows[j].known_to == kf:
                rows[j] = replace(rows[j], known_to=kt)  # 相邻纪元内容相同，延长知识区间
            else:
                rows.append(DimRow(rid, org, f, t, kf, kt))
                open_idx[key] = len(rows) - 1
    return sorted(rows, key=lambda r: (r.known_from, r.eff_from))


# ---------------------------------------------------------------- 差异与事实

def _org_at_point(rows: list[tuple], p: date) -> str | None:
    for f, t, org in rows:
        if f <= p and (t is None or p < t):
            return org
    return None


def _changed_regions(old: list[DimRow], new: list[DimRow]) -> list[tuple[date, date | None]]:
    """当前认知（known_to 为空）下，新旧有效区间归属不一致的业务区间。"""
    oldc = [(r.eff_from, r.eff_to, r.organization) for r in old if r.known_to is None]
    newc = [(r.eff_from, r.eff_to, r.organization) for r in new if r.known_to is None]
    pts = sorted({p for r in oldc + newc for p in (r[0], r[1]) if p is not None})
    regions: list[list] = []
    for i in range(len(pts) - 1):
        s, e = pts[i], pts[i + 1]
        if _org_at_point(oldc, s) != _org_at_point(newc, s):
            regions.append([s, e])
    if pts:
        s = pts[-1]
        if _org_at_point(oldc, s) != _org_at_point(newc, s):
            regions.append([s, None])  # 到 +∞ 的尾段
    merged: list[list] = []
    for s, e in regions:
        if merged and merged[-1][1] == s:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def _facts_in_regions(con, rid: str, regions: list[tuple[date, date | None]]) -> list[tuple]:
    if not regions:
        return []
    con.execute("CREATE OR REPLACE TEMP TABLE _changed_regions (f DATE, t DATE)")
    con.executemany("INSERT INTO _changed_regions VALUES (?, ?)", regions)
    return con.execute(
        """
        SELECT f.fact_id, f.researcher_id, f.event_date, f.metric
        FROM facts f
        JOIN _changed_regions c
          ON f.event_date >= c.f AND (c.t IS NULL OR f.event_date < c.t)
        WHERE f.researcher_id = ?
        ORDER BY f.fact_id
        """,
        [rid],
    ).fetchall()


def _fetch_dim(con, rid: str) -> list[DimRow]:
    rows = con.execute(
        "SELECT researcher_id, organization, eff_from, eff_to, known_from, known_to"
        " FROM dim_affiliation WHERE researcher_id = ?",
        [rid],
    ).fetchall()
    return [DimRow(r, o, f, t, to_utc(kf), to_utc(kt) if kt is not None else None)
            for r, o, f, t, kf, kt in rows]


def _rebuild_researcher(con, rid: str) -> ResearcherRebuild:
    """只重建一个研究者：全量取其断言重算维行，算差异与事实重算清单。"""
    old = _fetch_dim(con, rid)
    new = build_dim_rows(_load_assertions(con, rid))

    con.execute("DELETE FROM dim_affiliation WHERE researcher_id = ?", [rid])
    con.executemany("INSERT INTO dim_affiliation VALUES (?, ?, ?, ?, ?, ?)",
                    [r.as_params() for r in new])

    old_set, new_set = set(old), set(new)
    added = [r for r in new if r not in old_set]
    removed = [r for r in old if r not in new_set]
    regions = _changed_regions(old, new)
    facts = _facts_in_regions(con, rid, regions)
    return ResearcherRebuild(rid, added, removed, regions, facts)


# ---------------------------------------------------------------- 批处理入口

def process_batch(con, raw_rows: list[dict]) -> BatchReport:
    """装配入口：校验 -> 落库 -> 只重建受影响研究者 -> 差异与事实清单。

    任何一行无效则整批拒绝（BatchRejected），数据库不产生半批结果。
    """
    parsed: list[Assertion] = []
    parse_errors: list[str] = []
    for i, r in enumerate(raw_rows):
        try:
            parsed.append(Assertion.from_row(r))
        except Exception as e:  # noqa: BLE001 - 任何解析错误都整批拒绝
            parse_errors.append(f"第 {i} 行: {e}")
    if parse_errors:
        raise BatchRejected(parse_errors)

    new, duplicates, errors = validate_batch(con, parsed)
    if errors:
        raise BatchRejected(errors)

    rebuilds: list[ResearcherRebuild] = []
    con.execute("BEGIN")
    try:
        if new:
            con.executemany("INSERT INTO assertions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            [a.as_params() for a in new])
            for rid in sorted({a.researcher_id for a in new}):
                rebuilds.append(_rebuild_researcher(con, rid))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise

    return BatchReport(received=len(raw_rows), inserted=len(new),
                       duplicates=len(duplicates), rebuilds=rebuilds)
