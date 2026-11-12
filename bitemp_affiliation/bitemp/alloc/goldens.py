"""金标准场景与独立 oracle。

金标准（人工可手算）
  1. 两支到同一末端：ROOT 1/2 -> A, 1/2 -> B；金额 100 -> 50/50。
  2. 三等分一分钱：ROOT 1/3 -> A,B,C；金额 1 -> 1/0/0（余数全 1/3，按标识升序）。
  3. 负数冲回：同上金额 -1 -> -1/0/0；或 1/2+1/2 金额 -101 -> -51/-50。
  4. 机构跨日换出口：ROOT 前段全给 A，后段 1/2 A / 1/2 B。
  5. 同时有效的环：真正环被拒收。
  6. 仅跨期组合的假环：A->B 活跃于早期、B->A 活跃于晚期的反向边，合并看有环，
     但任何活跃日期都无环，必须允许。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from fractions import Fraction

from ..builder import process_batch
from ..schema import connect
from .allocate import largest_remainder
from .model import Edge

ASOF = datetime(2025, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------- 独立 oracle

def path_weights(edges: list[Edge], day: date, source: str) -> dict[str, Fraction]:
    """独立参考实现：逐路径 DFS 枚举并从 source 到各自末级单元累加份额。

    只用于小图核对，故意不复用 graph.propagate 的任何逻辑。
    """
    active = [e for e in edges if e.begin <= day and (e.until is None or day < e.until)]
    out: dict[str, Fraction] = {}

    def walk(node: str, weight: Fraction, seen: frozenset):
        nxt = [e for e in active if e.src == node]
        if not nxt:
            out[node] = out.get(node, Fraction(0)) + weight
            return
        for e in nxt:
            if e.dst in seen:
                raise ValueError(f"参考实现遇到环: {e.edge_key}")
            walk(e.dst, weight * e.weight, seen | {e.dst})

    walk(source, Fraction(1), frozenset({source}))
    return out


def path_amounts(edges: list[Edge], day: date, source: str, amount: int) -> dict[str, int]:
    """用独立权重 + 同一最大余数法得到末级整数金额。"""
    return largest_remainder(amount, path_weights(edges, day, source))


# ---------------------------------------------------------------- 场景构造

def _edge(key, src, dst, begin="2024-01-01", until=None, num=1, den=1) -> dict:
    return dict(edge_key=key, src=src, dst=dst, begin=begin, until=until,
                numerator=num, denominator=den)


def gold_two_branches() -> dict:
    """1) 两支到同一末端（各自不同末级）。"""
    return {
        "edges": [_edge("E-A", "ROOT", "A", num=1, den=2),
                  _edge("E-B", "ROOT", "B", num=1, den=2)],
        "person_org": ("P1", "ROOT"),
        "item": dict(item_key="I1", person_key="P1", day="2024-02-01", amount=100),
        "expect": {"A": 50, "B": 50},
        "expect_weights": {"A": Fraction(1, 2), "B": Fraction(1, 2)},
    }


def gold_three_way_penny() -> dict:
    """2) 三等分一分钱：1 -> A(1), B(0), C(0)，余数相同按标识升序。"""
    return {
        "edges": [_edge("E-A", "ROOT", "A", num=1, den=3),
                  _edge("E-B", "ROOT", "B", num=1, den=3),
                  _edge("E-C", "ROOT", "C", num=1, den=3)],
        "person_org": ("P1", "ROOT"),
        "item": dict(item_key="I2", person_key="P1", day="2024-02-01", amount=1),
        "expect": {"A": 1, "B": 0, "C": 0},
        "expect_weights": {"A": Fraction(1, 3), "B": Fraction(1, 3),
                           "C": Fraction(1, 3)},
    }


def gold_negative_offset() -> dict:
    """3) 负数冲回：-101 在两支上反号。"""
    return {
        "edges": [_edge("E-A", "ROOT", "A", num=1, den=2),
                  _edge("E-B", "ROOT", "B", num=1, den=2)],
        "person_org": ("P1", "ROOT"),
        "item": dict(item_key="I3", person_key="P1", day="2024-02-01", amount=-101),
        "expect": {"A": -51, "B": -50},
        "expect_weights": {"A": Fraction(1, 2), "B": Fraction(1, 2)},
    }


def gold_org_switches_exit() -> dict:
    """4) 机构跨日换出口：同一机构在 6/1 起换到新出口。"""
    return {
        "edges": [_edge("E1", "ROOT", "LEGACY", begin="2024-01-01", until="2024-06-01",
                        num=1, den=1),
                  _edge("E2", "ROOT", "A", begin="2024-06-01", num=1, den=2),
                  _edge("E3", "ROOT", "B", begin="2024-06-01", num=1, den=2)],
        "person_org": ("P1", "ROOT"),
        "items": [
            dict(item_key="I4a", person_key="P1", day="2024-05-31", amount=100),
            dict(item_key="I4b", person_key="P1", day="2024-06-01", amount=100),
        ],
        "expect": {"I4a": {"LEGACY": 100}, "I4b": {"A": 50, "B": 50}},
    }


def gold_true_cycle_rejected() -> dict:
    """5) 同时有效的环：ROOT->X, X->Y, Y->X 在同一段活跃。"""
    return {
        "edges": [_edge("E1", "ROOT", "X", num=1, den=1),
                  _edge("E2", "X", "Y", num=1, den=1),
                  _edge("E3", "Y", "X", num=1, den=1)],
        "expect_rejected": "环",
    }


def gold_fake_cycle_cross_period() -> dict:
    """6) 仅跨期组合的假环：反向边生效日完全错开，任何活跃日都无环。

    A->B 活跃于 [1/1, 4/1)；B->A 活跃于 [4/1, +∞)。合并看有环，逐段无环。
    """
    return {
        "edges": [_edge("E1", "ROOT", "A", begin="2024-01-01", num=1, den=1),
                  _edge("E2", "A", "B", begin="2024-01-01", until="2024-04-01",
                        num=1, den=1),
                  _edge("E3", "B", "A", begin="2024-04-01", num=1, den=1),
                  _edge("E4", "A", "TAIL", begin="2024-04-01", num=1, den=1)],
        "person_org": ("P1", "ROOT"),
        "items": [
            dict(item_key="I6a", person_key="P1", day="2024-03-01", amount=10),
            dict(item_key="I6b", person_key="P1", day="2024-05-01", amount=10),
        ],
        # 3/1：ROOT->A->B，末端 B 得 10
        # 5/1：ROOT->A->TAIL，末端 TAIL 得 10
        "expect": {"I6a": {"B": 10}, "I6b": {"TAIL": 10}},
    }


def gold_merge_fan_in() -> dict:
    """额外：多路汇合（菱形两路汇到同一末端），份额相加。"""
    return {
        "edges": [_edge("E1", "ROOT", "L", num=1, den=2),
                  _edge("E2", "ROOT", "R", num=1, den=2),
                  _edge("E3", "L", "END", num=1, den=1),
                  _edge("E4", "R", "END", num=1, den=1)],
        "person_org": ("P1", "ROOT"),
        "item": dict(item_key="I7", person_key="P1", day="2024-02-01", amount=100),
        "expect": {"END": 100},
        "expect_weights": {"END": Fraction(1)},
        "expect_flows": {"E1": Fraction(1, 2), "E2": Fraction(1, 2),
                         "E3": Fraction(1, 2), "E4": Fraction(1, 2)},
    }


def gold_unresolved() -> dict:
    """额外：找不到起点的事项进入 unresolved（不参与分摊、不静默丢弃）。"""
    return {
        "edges": [_edge("E1", "ROOT", "A", num=1, den=1)],
        "person_org": ("P1", "ROOT"),
        "items": [
            dict(item_key="I8a", person_key="P1", day="2024-02-01", amount=7),
            dict(item_key="I8b", person_key="GHOST", day="2024-02-02", amount=9),
        ],
    }


GOLD_CASES = [
    ("两支到同一末端", gold_two_branches),
    ("三等分一分钱", gold_three_way_penny),
    ("负数冲回", gold_negative_offset),
    ("机构跨日换出口", gold_org_switches_exit),
    ("同时有效的环（拒收）", gold_true_cycle_rejected),
    ("仅跨期组合的假环（放行）", gold_fake_cycle_cross_period),
    ("多路汇合相加", gold_merge_fan_in),
]


# ---------------------------------------------------------------- 组装助手

def setup_db(case: dict):
    """在内存库中装载规则 + 一条起点归属断言，返回 (con, service, item_keys)。"""
    from .service import AllocationService
    con = connect(":memory:")
    svc = AllocationService(con)
    svc.load_rules(case["edges"])
    person, org = case.get("person_org", ("P1", "ROOT"))
    process_batch(con, [dict(assertion_id="G-A1", researcher_id=person,
                             valid_from="2023-01-01", valid_to=None,
                             organization=org,
                             recorded_at="2023-01-01T00:00:00+00:00",
                             revision=1, supersedes=None)])
    items = case.get("items") or [case["item"]]
    svc.load_items(items)
    return con, svc, [it["item_key"] for it in items]
