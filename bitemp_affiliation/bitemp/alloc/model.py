"""分摊管线的输入模型：事项、路由边、规则集，以及精确分数与自定义摘要。

设计要点
--------
* 金额是最小货币单位的整数（可负），一律用 Python int 保存（DuckDB HUGEINT）。
* 份额是非负有理数 ``numerator / denominator``，全程用 ``fractions.Fraction``
  参与计算，只在末端一次性取整；任何中间步骤都不做浮点近似。
* 规则集的“固定摘要”只由规范化后的边集合决定：与输入顺序、JSON 排版、
  ``2/4`` vs ``1/2`` 这类等价写法无关，因此重复导入等价集合必然得到同一版本号。
* 日期区间左闭右开 ``[begin, until)``，``until`` 缺省（None/空串）= +∞。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from fractions import Fraction
from math import gcd

RULE_FORMAT = "alloc-rules/v1"


# ---------------------------------------------------------------- 解析工具

def parse_date(v, field: str = "day") -> date:
    """严格解析日期；拒绝 datetime（日期字段不允许带时间）。"""
    if isinstance(v, datetime):
        raise ValueError(f"{field} 不允许带时间: {v!r}")
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        s = v.strip()
        try:
            return date.fromisoformat(s)
        except ValueError as e:
            raise ValueError(f"{field} 不是合法 ISO 日期: {v!r}") from e
    raise ValueError(f"{field} 无法解析为日期: {v!r}")


def parse_optional_date(v, field: str) -> date | None:
    """可选日期：None 或空串表示 +∞。"""
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return None
    return parse_date(v, field)


def strict_int(v, field: str) -> int:
    """严格整数：拒绝 bool、float、数值字符串、None。"""
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError(f"{field} 必须是整数，收到 {v!r}")
    return v


def parse_asof(v) -> datetime:
    """asof 必须带时区，统一 UTC。与主模块的 recorded_at 语义一致。"""
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError as e:
            raise ValueError(f"asof 不是合法 ISO 时间: {v!r}") from e
    if not isinstance(v, datetime):
        raise ValueError(f"asof 无法解析: {v!r}")
    if v.tzinfo is None or v.utcoffset() is None:
        raise ValueError(f"asof 必须带时区: {v!r}")
    return v.astimezone(timezone.utc)


def require_text(v, field: str) -> str:
    """必填标识：非空字符串。空串/空白/None 视为悬空标识，由调用方定性。"""
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"{field} 为空或非字符串（悬空标识）: {v!r}")
    return v


def lenient_text(v, field: str) -> str:
    """解析阶段只要求是字符串；空/空白交由校验器报“悬空标识”（带边键）。"""
    if not isinstance(v, str):
        raise ValueError(f"{field} 不是字符串: {v!r}")
    return v


# ---------------------------------------------------------------- 规则边

@dataclass(frozen=True)
class Edge:
    """一条带活跃期的出边：src --(numerator/denominator)--> dst。"""
    edge_key: str
    src: str
    dst: str
    begin: date
    until: date | None
    numerator: int
    denominator: int

    @property
    def weight(self) -> Fraction:
        return Fraction(self.numerator, self.denominator)

    def reduced(self) -> "Edge":
        """约分后的等价边（用于固定摘要与落库）。"""
        if self.numerator == 0:
            return Edge(self.edge_key, self.src, self.dst, self.begin,
                        self.until, 0, 1)
        g = gcd(self.numerator, self.denominator)
        return Edge(self.edge_key, self.src, self.dst, self.begin,
                    self.until, self.numerator // g, self.denominator // g)

    def active_at(self, day: date) -> bool:
        return self.begin <= day and (self.until is None or day < self.until)

    def as_params(self, version_id: str) -> tuple:
        e = self.reduced()
        return (version_id, e.edge_key, e.src, e.dst, e.begin, e.until,
                str(e.numerator), str(e.denominator))


def overlap(a: Edge, b: Edge) -> tuple[date, date | None] | None:
    """两条边的活跃期交集；无交集返回 None。区间左闭右开。"""
    start = max(a.begin, b.begin)
    end_candidates = [x for x in (a.until, b.until) if x is not None]
    end = min(end_candidates) if end_candidates else None
    if end is not None and not (start < end):
        return None
    return (start, end)


def edge_from_row(row: dict, index: int) -> Edge:
    """解析一行边；错误信息带行号，便于定位问题输入。

    src/dst 允许为空串（结构问题），留给校验器报“悬空标识”并指明边键；
    edge_key 仍必须非空，否则无法定位问题边。
    """
    try:
        return Edge(
            edge_key=require_text(row.get("edge_key"), "edge_key"),
            src=lenient_text(row.get("src"), "src"),
            dst=lenient_text(row.get("dst"), "dst"),
            begin=parse_date(row.get("begin"), "begin"),
            until=parse_optional_date(row.get("until"), "until"),
            numerator=strict_int(row.get("numerator"), "numerator"),
            denominator=strict_int(row.get("denominator"), "denominator"),
        )
    except ValueError as e:
        raise ValueError(f"第 {index} 条路由: {e}") from e


# ---------------------------------------------------------------- 规则集摘要

def canonical_edges(edges: list[Edge]) -> list[dict]:
    """规范化：按 edge_key 排序、约分、日期 ISO 化。用于稳定摘要。"""
    out = []
    for e in sorted((e.reduced() for e in edges), key=lambda x: x.edge_key):
        out.append({
            "edge_key": e.edge_key,
            "src": e.src,
            "dst": e.dst,
            "begin": e.begin.isoformat(),
            "until": e.until.isoformat() if e.until is not None else None,
            "numerator": e.numerator,
            "denominator": e.denominator,
        })
    return out


def rule_digest(edges: list[Edge]) -> str:
    """规则集的固定摘要（sha256 hex）。等价集合 -> 同一摘要。"""
    payload = {"format": RULE_FORMAT, "edges": canonical_edges(edges)}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# ---------------------------------------------------------------- 事项

@dataclass(frozen=True)
class Item:
    item_key: str
    person_key: str
    day: date
    amount: int

    def content_hash(self) -> str:
        blob = json.dumps([self.item_key, self.person_key,
                           self.day.isoformat(), self.amount],
                          separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    def as_params(self, imported_at: datetime) -> tuple:
        return (self.item_key, self.person_key, self.day, self.amount,
                self.content_hash(), imported_at)


def item_from_row(row: dict, index: int) -> Item:
    try:
        item_key = require_text(row.get("item_key"), "item_key")
        person_key = require_text(row.get("person_key"), "person_key")
        day = parse_date(row.get("day"), "day")
        amount = strict_int(row.get("amount"), "amount")
        return Item(item_key, person_key, day, amount)
    except ValueError as e:
        raise ValueError(f"第 {index} 条事项: {e}") from e


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
