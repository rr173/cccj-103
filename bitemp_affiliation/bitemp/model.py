"""输入断言模型与解析规范化。

断言（assertion）是追加式输入的一行：
    assertion_id  全局唯一标识
    researcher_id 研究者
    valid_from    业务区间起点（左闭）
    valid_to      业务区间终点（右开），None 表示 +∞
    organization  归属机构
    recorded_at   记录时点（必须带时区，统一规范化为 UTC）
    revision      修订号（重叠时参与决胜）
    supersedes    显式替代的旧断言 id，None 表示非更正
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone


def to_utc(dt: datetime) -> datetime:
    """把任何 datetime 规范化为带 UTC 时区的 datetime（naive 视为 UTC）。"""
    if dt.tzinfo is None or dt.utcoffset() is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_date(v) -> date:
    if isinstance(v, datetime):
        raise ValueError(f"日期字段不允许带时间: {v!r}")
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        return date.fromisoformat(v)
    raise ValueError(f"无法解析日期: {v!r}")


def _to_utc_strict(v) -> datetime:
    """解析 recorded_at；naive（无时区）一律拒绝。"""
    if isinstance(v, str):
        v = datetime.fromisoformat(v.replace("Z", "+00:00"))
    if not isinstance(v, datetime):
        raise ValueError(f"无法解析时间: {v!r}")
    if v.tzinfo is None or v.utcoffset() is None:
        raise ValueError(f"recorded_at 必须带时区: {v!r}")
    return v.astimezone(timezone.utc)


@dataclass(frozen=True)
class Assertion:
    assertion_id: str
    researcher_id: str
    valid_from: date
    valid_to: date | None          # None = +∞（区间左闭右开的开端）
    organization: str
    recorded_at: datetime          # 一律 UTC
    revision: int
    supersedes: str | None

    @classmethod
    def from_row(cls, row: dict) -> "Assertion":
        return cls(
            assertion_id=str(row["assertion_id"]),
            researcher_id=str(row["researcher_id"]),
            valid_from=_to_date(row["valid_from"]),
            valid_to=None if row.get("valid_to") in (None, "") else _to_date(row["valid_to"]),
            organization=str(row["organization"]),
            recorded_at=_to_utc_strict(row["recorded_at"]),
            revision=int(row["revision"]),
            supersedes=(row.get("supersedes") or None),
        )

    def as_params(self) -> tuple:
        return (self.assertion_id, self.researcher_id, self.valid_from, self.valid_to,
                self.organization, self.recorded_at, self.revision, self.supersedes)
