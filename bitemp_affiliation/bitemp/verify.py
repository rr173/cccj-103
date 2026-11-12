"""独立验证器：逐时点直接解释原始断言（oracle）。

刻意不复用 builder 的任何切分/合并逻辑：对每个 (研究者, 业务日期, 知识时点)
三元组直接按语义定义在原始断言上计算答案，再与维表内容对拍，
避免“用同一套切分逻辑自证”。

对拍时维表行从 DuckDB 一次性取出，在 Python 侧做区间成员判断
（这只是“点落在区间内”的查询语义，不涉及任何构建/切分逻辑），
因此 oracle 与装配器仍是两条独立的实现路径。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .model import Assertion, to_utc
from .queries import org_at


def oracle_org_at(assertions: list[Assertion], researcher_id: str,
                  effective_at: date, known_at: datetime) -> str | None:
    """逐时点直接解释：
      1. 已知：recorded_at <= known_at（恰等于 recorded_at 时包含）；
      2. 剔除被已知断言 supersedes 的；
      3. 覆盖 effective_at 的候选中，取 (revision, recorded_at, assertion_id)
         字典序最大者；无候选 -> None（未知）。
    """
    known = [a for a in assertions
             if a.researcher_id == researcher_id and a.recorded_at <= known_at]
    superseded = {a.supersedes for a in known if a.supersedes is not None}
    best_key = None
    best_org = None
    for a in known:
        if a.assertion_id in superseded:
            continue
        if a.valid_from <= effective_at and (a.valid_to is None or effective_at < a.valid_to):
            key = (a.revision, a.recorded_at, a.assertion_id)
            if best_key is None or key > best_key:
                best_key, best_org = key, a.organization
    return best_org


def _probe_dates(assertions: list[Assertion]) -> list[date]:
    """围绕这些断言的业务端点取探测点（前一日、当日、后一日），加远点。"""
    pts = {date(2020, 1, 1), date(2035, 1, 1)}
    for a in assertions:
        for p in (a.valid_from, a.valid_to):
            if p is not None:
                pts.update({p - timedelta(days=1), p, p + timedelta(days=1)})
    return sorted(pts)


def _probe_known_ats(assertions: list[Assertion]) -> list[datetime]:
    """围绕这些断言的 recorded_at 取探测点（前一秒、恰等于、后一秒），加远点。"""
    pts = {datetime(2000, 1, 1, tzinfo=timezone.utc),
           datetime(2035, 1, 1, tzinfo=timezone.utc)}
    for a in assertions:
        pts.update({a.recorded_at - timedelta(seconds=1),
                    a.recorded_at,
                    a.recorded_at + timedelta(seconds=1)})
    return sorted(pts)


def _dim_org_at(rows: list[tuple], effective_at: date, known_at: datetime) -> str | None:
    """对取出的维表行做成员判断（查询语义本身，非构建逻辑）。"""
    for org, f, t, kf, kt in rows:
        if (f <= effective_at and (t is None or effective_at < t)
                and to_utc(kf) <= known_at and (kt is None or known_at < to_utc(kt))):
            return org
    return None


def cross_check(con, assertions: list[Assertion], researchers: list[str]) -> list[str]:
    """全探测点对拍：维表内容必须等于 oracle 直接解释结果。

    探测点按研究者各自断言的端点生成，既覆盖所有临界情形又控制规模。
    """
    by_rid: dict[str, list[Assertion]] = {r: [] for r in researchers}
    for a in assertions:
        by_rid.setdefault(a.researcher_id, []).append(a)

    dim_rows: dict[str, list[tuple]] = {}
    for rid in researchers:
        dim_rows[rid] = con.execute(
            "SELECT organization, eff_from, eff_to, known_from, known_to "
            "FROM dim_affiliation WHERE researcher_id = ?", [rid]).fetchall()

    mismatches: list[str] = []
    for rid in researchers:
        mine = by_rid.get(rid, [])
        dates = _probe_dates(mine)
        known_ats = _probe_known_ats(mine)
        for d in dates:
            for k in known_ats:
                want = oracle_org_at(mine, rid, d, k)
                got = _dim_org_at(dim_rows[rid], d, k)
                if want != got:
                    mismatches.append(
                        f"{rid} @ eff={d} known={k}: 维表={got!r} oracle={want!r}")
    return mismatches


def cross_check_sql(con, assertions: list[Assertion], researchers: list[str],
                    sample: int | None = None) -> list[str]:
    """与 cross_check 相同，但直接对 DuckDB 维表发 SQL 查询（更慢，抽样用）。"""
    by_rid: dict[str, list[Assertion]] = {r: [] for r in researchers}
    for a in assertions:
        by_rid.setdefault(a.researcher_id, []).append(a)
    mismatches: list[str] = []
    for rid in researchers:
        mine = by_rid.get(rid, [])
        dates = _probe_dates(mine)
        known_ats = _probe_known_ats(mine)
        if sample is not None and len(dates) * len(known_ats) > sample:
            dates = dates[:: max(1, len(dates) // 12)]
            known_ats = known_ats[:: max(1, len(known_ats) // 12)]
        for d in dates:
            for k in known_ats:
                want = oracle_org_at(mine, rid, d, k)
                got = org_at(con, rid, d, k)
                if want != got:
                    mismatches.append(
                        f"{rid} @ eff={d} known={k}: 维表={got!r} oracle={want!r}")
    return mismatches
