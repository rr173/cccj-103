"""查询接口：effective_at × known_at 联合查询。

空档保持未知：查询不到行即“未知”，不做任何向前填充。
"""
from __future__ import annotations

from datetime import date, datetime

# 某研究者在 effective_at 实际归属、且该认知在 known_at 时点成立
ORG_AT_SQL = """
SELECT organization
FROM dim_affiliation
WHERE researcher_id = ?
  AND eff_from <= ? AND (eff_to IS NULL OR ? < eff_to)
  AND known_from <= ? AND (known_to IS NULL OR ? < known_to)
"""

# 事实表按 known_at 时点的认知关联归属（LEFT JOIN：空档 -> organization 为 NULL）
FACTS_AS_OF_SQL = """
SELECT f.fact_id, f.researcher_id, f.event_date, f.metric, d.organization
FROM facts f
LEFT JOIN dim_affiliation d
  ON d.researcher_id = f.researcher_id
 AND f.event_date >= d.eff_from
 AND (d.eff_to IS NULL OR f.event_date < d.eff_to)
 AND d.known_from <= ?
 AND (d.known_to IS NULL OR ? < d.known_to)
ORDER BY f.fact_id
"""


def org_at(con, researcher_id: str, effective_at: date, known_at: datetime) -> str | None:
    """在 effective_at 实际属于哪里，按 known_at 时点的认知。未知返回 None。"""
    row = con.execute(
        ORG_AT_SQL,
        [researcher_id, effective_at, effective_at, known_at, known_at],
    ).fetchone()
    return row[0] if row else None


def facts_with_org(con, known_at: datetime) -> list[tuple]:
    """全部事实按 known_at 时点认知的归属（联合查询例子）。"""
    return con.execute(FACTS_AS_OF_SQL, [known_at, known_at]).fetchall()
