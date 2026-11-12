"""分摊网络：边界切分、逐段校验、拓扑动态规划与反向解释。

核心语义
--------
* 活跃期左闭右开 ``[begin, until)``，``until`` 缺省 = +∞。
* 把所有边的 ``begin`` / ``until`` 作为边界切成日期段；一个日期段内活跃边集合恒定，
  因此“某一活跃日期”上的环、守恒、重复配置只需在每段起点检查一次。
* 中间节点（在该日期段内有活跃出边）的出边权重之和必须恰为 1。
* 节点在该日期段内没有活跃出边即末级单元（同一节点可在不同日期段分别是中间节点
  与末级单元）。
* 拓扑动态规划：``forward[v] += forward[u] * w(u->v)``，以精确 ``Fraction`` 传播。
  汇合处权重自然相加，复杂度 O(V+E)，与路径条数无关。
"""
from __future__ import annotations

from bisect import bisect_right
from collections import deque
from dataclasses import dataclass, field
from datetime import date
from fractions import Fraction

from .model import Edge, overlap


# ---------------------------------------------------------------- 校验问题

@dataclass(frozen=True)
class Issue:
    """一条可定位的拒收原因。

    edge_keys 指明具体问题边；date 是该问题首次出现的可复现日期
    （通常是所在日期段的起点，必落在活跃期内）。
    """
    kind: str
    edge_keys: tuple[str, ...]
    date: date
    detail: str

    def render(self) -> str:
        keys = ",".join(self.edge_keys) if self.edge_keys else "-"
        return f"[{self.kind}] {self.date.isoformat()} 边({keys}): {self.detail}"


def _usable(e: Edge) -> bool:
    """结构上能参与图计算的边（字段问题由各自的 issue 负责）。"""
    return (e.src.strip() != "" and e.dst.strip() != ""
            and e.denominator > 0 and e.numerator >= 0
            and (e.until is None or e.begin < e.until))


def boundaries(edges: list[Edge]) -> list[date]:
    return sorted({p for e in edges for p in (e.begin, e.until) if p is not None})


def spans(edges: list[Edge]) -> list[tuple[date, date | None]]:
    """边界切出的日期段；覆盖 [最早已生效日, +∞)。"""
    bs = boundaries(edges)
    if not bs:
        return []
    out = [(bs[i], bs[i + 1]) for i in range(len(bs) - 1)]
    out.append((bs[-1], None))
    return out


def active_at(edges: list[Edge], day: date) -> list[Edge]:
    return [e for e in edges if e.active_at(day)]


# ---------------------------------------------------------------- 熔断检测

def _adjacency(edges: list[Edge]) -> dict[str, list[Edge]]:
    adj: dict[str, list[Edge]] = {}
    for e in sorted(edges, key=lambda x: x.edge_key):
        adj.setdefault(e.src, []).append(e)
    return adj


def find_cycle(edges: list[Edge]) -> list[str] | None:
    """返回一个环上的边键（按环的顺序），无环返回 None。迭代 DFS，避免递归深度。"""
    adj = _adjacency(edges)
    nodes = sorted({n for e in edges for n in (e.src, e.dst)})
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in nodes}

    for start in nodes:
        if color[start] != WHITE:
            continue
        color[start] = GRAY
        node_stack = [start]
        edge_stack: list[Edge] = []          # 进入各层节点所用的边
        iter_stack = [iter(adj.get(start, ()))]
        while node_stack:
            u = node_stack[-1]
            found_child = False
            for e in iter_stack[-1]:
                v = e.dst
                if color[v] == WHITE:
                    color[v] = GRAY
                    node_stack.append(v)
                    edge_stack.append(e)
                    iter_stack.append(iter(adj.get(v, ())))
                    found_child = True
                    break
                if color[v] == GRAY:
                    # 找到回边 e: u -> v；环 = 从 v 在栈中的位置到 u 的路径 + e
                    idx = node_stack.index(v)
                    cyc = edge_stack[idx:] + [e]
                    return [x.edge_key for x in cyc]
            if not found_child:
                color[u] = BLACK
                node_stack.pop()
                iter_stack.pop()
                if edge_stack:
                    edge_stack.pop()
    return None


# ---------------------------------------------------------------- 逐段校验

def _check_fields(edges: list[Edge]) -> list[Issue]:
    issues: list[Issue] = []
    for e in edges:
        if not e.src.strip() or not e.dst.strip():
            issues.append(Issue("悬空标识", (e.edge_key,), e.begin,
                                f"src={e.src!r} dst={e.dst!r}"))
        if e.denominator <= 0:
            issues.append(Issue("非正分母", (e.edge_key,), e.begin,
                                f"denominator={e.denominator}（份额分母必须 > 0）"))
        if e.numerator < 0:
            issues.append(Issue("负份额", (e.edge_key,), e.begin,
                                f"numerator={e.numerator}"))
        if e.until is not None and e.until <= e.begin:
            issues.append(Issue("无效区间", (e.edge_key,), e.begin,
                                f"begin={e.begin} until={e.until}（左闭右开需 begin<until）"))
    return issues


def _check_edge_keys(edges: list[Edge]) -> list[Issue]:
    """同一版本内 edge_key 必须唯一；相同键但内容不同视为标识冲突。"""
    issues: list[Issue] = []
    seen: dict[str, Edge] = {}
    for e in edges:
        prev = seen.get(e.edge_key)
        if prev is not None and prev != e:
            issues.append(Issue("标识冲突", (e.edge_key,), min(prev.begin, e.begin),
                                "同一 edge_key 出现两次且内容不同"))
        seen.setdefault(e.edge_key, e)
    return issues


def _check_duplicate_pairs(edges: list[Edge]) -> list[Issue]:
    """同一 (src,dst) 的不同边若活跃期重叠，视为重复配置。"""
    issues: list[Issue] = []
    groups: dict[tuple[str, str], list[Edge]] = {}
    for e in edges:
        groups.setdefault((e.src, e.dst), []).append(e)
    for (src, dst), group in sorted(groups.items()):
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                ov = overlap(group[i], group[j])
                if ov is not None:
                    issues.append(Issue(
                        "重复配置", (group[i].edge_key, group[j].edge_key), ov[0],
                        f"({src}->{dst}) 在 {ov[0]} 起活跃期重叠"))
    return issues


def validate_edges(edges: list[Edge]) -> list[Issue]:
    """全量校验。返回空列表才允许建立版本。"""
    issues: list[Issue] = []
    issues += _check_fields(edges)
    issues += _check_edge_keys(edges)
    issues += _check_duplicate_pairs(edges)

    usable = [e for e in edges if _usable(e)]
    for start, _end in spans(usable):
        active = active_at(usable, start)
        if not active:
            continue
        # 权重守恒：每个有活跃出边的节点，出边份额之和必须为 1
        by_src: dict[str, list[Edge]] = {}
        for e in active:
            by_src.setdefault(e.src, []).append(e)
        for src, out in sorted(by_src.items()):
            total = sum((e.weight for e in out), Fraction(0))
            if total != 1:
                issues.append(Issue(
                    "权重不守恒", tuple(sorted(e.edge_key for e in out)), start,
                    f"节点 {src} 在 {start} 的出边份额之和={total}，应为 1"))
        # 环：只要在该活跃日期段内存在即拒收
        cyc = find_cycle(active)
        if cyc:
            issues.append(Issue("环", tuple(cyc), start,
                                f"活跃网络在第 {start} 段存在环: {' -> '.join(cyc)}"))

    return sorted(issues, key=lambda it: (it.date, it.kind, it.edge_keys))


# ---------------------------------------------------------------- 版本图

class RuleGraph:
    """一个规则版本的不可变图快照 + 逐日期段活跃边缓存。"""

    def __init__(self, edges: list[Edge]):
        self.edges = sorted(edges, key=lambda e: e.edge_key)
        self.boundaries = boundaries(self.edges)
        self.spans = spans(self.edges)
        self._active = [active_at(self.edges, s) for s, _ in self.spans]

    def active_for_day(self, day: date) -> list[Edge]:
        if not self.spans:
            return []
        idx = bisect_right(self.boundaries, day) - 1
        if idx < 0:
            return []          # 早于所有边生效日：无活跃边
        return self._active[idx]

    def issues(self) -> list[Issue]:
        return validate_edges(self.edges)


# ---------------------------------------------------------------- 前向传播

@dataclass
class Propagation:
    source: str
    adj: dict[str, list[Edge]]
    forward: dict[str, Fraction]
    topo: list[str]
    terminals: list[str]
    reachable: set[str]
    node_ops: int
    edge_ops: int

    def weight(self, node: str) -> Fraction:
        return self.forward.get(node, Fraction(0))

    def flows(self) -> list[tuple[Edge, Fraction]]:
        """(边, 该边承载的精确流量权重)；只含流量 > 0 的边。"""
        out: list[tuple[Edge, Fraction]] = []
        for u in self.topo:
            f = self.forward.get(u, Fraction(0))
            if f == 0:
                continue
            for e in self.adj.get(u, ()):
                flow = f * e.weight
                if flow > 0:
                    out.append((e, flow))
        return out


def propagate(active_edges: list[Edge], source: str) -> Propagation:
    """从 source 在给定活跃边集合上做拓扑动态规划。"""
    adj = _adjacency(active_edges)
    reachable = {source}
    stack = [source]
    while stack:
        u = stack.pop()
        for e in adj.get(u, ()):
            if e.dst not in reachable:
                reachable.add(e.dst)
                stack.append(e.dst)

    indeg = {n: 0 for n in reachable}
    for u in reachable:
        for e in adj.get(u, ()):
            if e.dst in reachable:
                indeg[e.dst] += 1

    queue = deque(sorted(n for n in reachable if indeg[n] == 0))
    topo: list[str] = []
    node_ops = 0
    while queue:
        u = queue.popleft()
        topo.append(u)
        node_ops += 1
        for e in adj.get(u, ()):
            if e.dst not in reachable:
                continue
            indeg[e.dst] -= 1
            if indeg[e.dst] == 0:
                queue.append(e.dst)
    if len(topo) != len(reachable):
        raise ValueError(f"活跃网络存在环，无法拓扑传播（source={source}）")

    forward: dict[str, Fraction] = {n: Fraction(0) for n in reachable}
    forward[source] = Fraction(1)
    edge_ops = 0
    for u in topo:
        fu = forward[u]
        for e in adj.get(u, ()):
            if e.dst in reachable:
                forward[e.dst] += fu * e.weight
                edge_ops += 1

    terminals = sorted(n for n in reachable if not adj.get(n))
    return Propagation(source, adj, forward, topo, terminals, reachable,
                       node_ops, edge_ops)


# ---------------------------------------------------------------- 反向解释

@dataclass
class Explanation:
    """针对某个末级单元的反向传播解释（不枚举完整路径）。"""
    terminal: str
    forward: dict[str, Fraction]
    back: dict[str, Fraction]
    through: dict[str, Fraction]
    last_hop: list[tuple[Edge, Fraction]]      # 指向该末级单元的边及其贡献权重
    nodes: dict[str, dict] = field(default_factory=dict)


def explain_terminal(prop: Propagation, terminal: str) -> Explanation:
    """计算从各节点到 terminal 的精确权重（反向 DP）。

    对任意祖先 u，``forward[u] * back[u]`` = 经过 u 到达 terminal 的权重，
    因此可以在 O(V+E) 内解释多路汇合，而不枚举路径。
    """
    rev: dict[str, list[Edge]] = {}
    for u, out in prop.adj.items():
        for e in out:
            rev.setdefault(e.dst, []).append(e)

    back: dict[str, Fraction] = {n: Fraction(0) for n in prop.reachable}
    back[terminal] = Fraction(1)
    for u in reversed(prop.topo):
        if u == terminal:
            continue
        total = Fraction(0)
        for e in prop.adj.get(u, ()):
            if e.dst in prop.reachable:
                total += e.weight * back.get(e.dst, Fraction(0))
        back[u] = total

    last_hop: list[tuple[Edge, Fraction]] = []
    for e in rev.get(terminal, ()):
        if e.src in prop.reachable:
            contrib = prop.forward.get(e.src, Fraction(0)) * e.weight
            if contrib > 0:
                last_hop.append((e, contrib))
    last_hop.sort(key=lambda x: x[0].edge_key)

    through = {n: prop.forward.get(n, Fraction(0)) * back.get(n, Fraction(0))
               for n in prop.reachable}

    nodes = {}
    for n in prop.reachable:
        incoming = []
        for e in rev.get(n, ()):
            if e.src in prop.reachable:
                incoming.append({
                    "edge_key": e.edge_key, "src": e.src,
                    "edge_weight": e.weight,
                    "contribution": prop.forward.get(e.src, Fraction(0)) * e.weight,
                })
        incoming.sort(key=lambda d: d["edge_key"])
        outgoing = [{"edge_key": e.edge_key, "dst": e.dst, "edge_weight": e.weight}
                    for e in prop.adj.get(n, ())]
        nodes[n] = {
            "forward": prop.forward.get(n, Fraction(0)),
            "back": back.get(n, Fraction(0)),
            "through": through[n],
            "incoming": incoming,
            "outgoing": outgoing,
        }

    return Explanation(terminal, prop.forward, back, through, last_hop, nodes)
