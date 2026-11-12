"""计算引擎：起点解析 -> 拓扑传播 -> 末端最大余数法取整 -> 归因。

关键不变量
----------
* 全程 ``Fraction`` 精确计算，没有任何浮点中间量。
* 取整只在末端发生一次：把每笔事项的金额（绝对值）按末级单元的精确权重
  分配整数最小货币单位；余数相同的末级单元按标识升序优先。
* 负金额取绝对值做同样分配，再统一反号。
* 每笔事项末级金额之和严格等于原金额。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from fractions import Fraction
from typing import Iterable

from .. import queries
from .graph import RuleGraph, Propagation, Explanation, explain_terminal, propagate
from .model import Edge, Item, parse_asof, item_from_row


@dataclass
class TerminalAllocation:
    terminal: str
    amount: int
    weight: Fraction           # 该末级单元收到的精确总权重


@dataclass
class ItemAllocation:
    item: Item
    source_org: str | None
    terminals: list[TerminalAllocation]
    flows: list[tuple[Edge, Fraction]]          # 实际参与边及其精确流量权重
    propagation: Propagation | None = None
    reason: str | None = None

    @property
    def resolved(self) -> bool:
        return self.source_org is not None

    def total(self) -> int:
        return sum(t.amount for t in self.terminals)

    def explain(self, terminal: str | None = None) -> Explanation | None:
        if self.propagation is None:
            return None
        if terminal is None:
            terminal = max(self.terminals,
                           key=lambda t: (t.weight, t.terminal)).terminal
        return explain_terminal(self.propagation, terminal)


@dataclass
class ComputeResult:
    version_id: str
    digest: str
    asof: datetime
    kind: str
    items: list[ItemAllocation]
    graph: RuleGraph
    stats: dict = field(default_factory=dict)

    @property
    def unresolved(self) -> list[ItemAllocation]:
        return [a for a in self.items if not a.resolved]

    @property
    def resolved_items(self) -> list[ItemAllocation]:
        return [a for a in self.items if a.resolved]

    def explain(self, item_key: str, terminal: str | None = None) -> Explanation:
        for a in self.items:
            if a.item.item_key == item_key:
                if not a.resolved:
                    raise KeyError(f"{item_key} 未解析（未找到起点机构）")
                return a.explain(terminal)
        raise KeyError(f"未找到事项 {item_key}")


# ---------------------------------------------------------------- 取整

def largest_remainder(amount: int, weights: dict[str, Fraction]) -> dict[str, int]:
    """最大余数法：把整数 amount 按精确权重分成整数单位，合计严格等于 amount。

    * amount 的符号统一在最后应用（先取绝对值分配）；
    * 权重先按其总和归一化，保证即使调用方传入的权重和不恰为 1 也严格守恒；
    * 余数相同按末级标识升序优先。
    """
    if not weights:
        if amount == 0:
            return {}
        raise ValueError("存在非零金额但没有末级单元，无法分配")
    sign = -1 if amount < 0 else 1
    total_abs = abs(amount)

    positive = {k: w for k, w in weights.items() if w > 0}
    if not positive:
        # 全部权重为 0 时，无法守恒；这只会发生在非法网络上，明确报错
        if total_abs == 0:
            return {k: 0 for k in weights}
        raise ValueError("末级权重全为 0，无法分配非零金额")

    s = sum(positive.values(), Fraction(0))
    quotas = {k: Fraction(total_abs) * w / s for k, w in positive.items()}
    base = {k: q.numerator // q.denominator for k, q in quotas.items()}
    allocated = sum(base.values())
    leftover = total_abs - allocated
    remainders = {k: quotas[k] - base[k] for k in quotas}
    order = sorted(quotas, key=lambda k: (-remainders[k], k))
    for k in order[:leftover]:
        base[k] += 1

    out = {k: 0 for k in weights}
    for k, v in base.items():
        out[k] = sign * v
    return out


# ---------------------------------------------------------------- 单笔计算

def _memo_key(day: date, org: str) -> tuple:
    return (day, org)


def allocate_item(con, graph: RuleGraph, item: Item, asof: datetime,
                  cache: dict | None = None) -> ItemAllocation:
    """解析起点机构并做传播与取整。起点缺失 -> unresolved（绝不静默丢弃）。"""
    org = queries.org_at(con, item.person_key, item.day, asof)
    if org is None:
        return ItemAllocation(item, None, [], [], None,
                              reason="org_at 未找到起点机构")
    key = _memo_key(item.day, org)
    if cache is not None and key in cache:
        prop = cache[key]
    else:
        prop = propagate(graph.active_for_day(item.day), org)
        if cache is not None:
            cache[key] = prop

    weights = {t: prop.weight(t) for t in prop.terminals}
    amounts = largest_remainder(item.amount, weights)
    allocs = [TerminalAllocation(t, amounts[t], weights[t])
              for t in sorted(weights) if weights[t] > 0]
    return ItemAllocation(item, org, allocs, prop.flows(), prop)


# ---------------------------------------------------------------- 顶层入口

def run_allocation(con, asof, items: Iterable[Item],
                   version_edges: list[Edge], version_id: str,
                   digest: str, kind: str = "trial") -> ComputeResult:
    graph = RuleGraph(version_edges)
    cache: dict = {}
    allocations: list[ItemAllocation] = []
    node_ops = edge_ops = 0
    for item in items:
        a = allocate_item(con, graph, item, asof, cache)
        if a.propagation is not None:
            node_ops += a.propagation.node_ops
            edge_ops += a.propagation.edge_ops
        allocations.append(a)
    stats = {
        "items": len(allocations),
        "resolved": sum(1 for a in allocations if a.resolved),
        "unresolved": sum(1 for a in allocations if not a.resolved),
        "node_ops": node_ops,
        "edge_ops": edge_ops,
        "memo_slots": len(cache),
        "graph_nodes": len({n for e in version_edges for n in (e.src, e.dst)}),
        "graph_edges": len(version_edges),
    }
    return ComputeResult(version_id, digest, parse_asof(asof), kind,
                         allocations, graph, stats)


def parse_items(rows: Iterable[dict]) -> list[Item]:
    return [item_from_row(r, i) for i, r in enumerate(rows)]
