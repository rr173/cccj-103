"""候选之间的沿道路连接与转移代价。

相邻观测的两个候选 A、B 只能沿道路方向连接，有两种路线，都可走时取更短：
  1. 同一条边且 B.offset >= A.offset：直接走 offset 差；
  2. 先走 A 的剩余边长到 A.edge 的终点，再沿有向图走节点最短路到
     B.edge 的起点，最后走 B.offset。
同边倒退不允许直接取偏移差绝对值；只有通过有向图绕一圈（方案 2）
真的能回来时才可连接。等长时方案 1 优先；方案 2 的等长最短路由
Graph.shortest 按整条 edge_id 序列的 UTF-8 字典序决胜。
"""
from __future__ import annotations

from dataclasses import dataclass

from .geometry import Candidate, Edge, euclid
from .graph import Graph, Path

DIRECT = "direct_same_edge"
VIA_NODES = "via_nodes"


@dataclass(frozen=True)
class Route:
    kind: str                    # direct_same_edge | via_nodes
    distance: float              # 总路程（米）
    middle_edges: tuple[str, ...]
    partial_from: float          # A 候选处走的部分边长度（同边直达时为偏移差）
    partial_to: float            # B 候选处走的部分边长度
    middle_distance: float       # 中间节点最短路长度


def route_between(a: Candidate, b: Candidate, graph: Graph,
                  edges: dict[str, Edge]) -> Route | None:
    """返回 A -> B 的最短可行沿线路线，不可达返回 None。"""
    options: list[Route] = []
    ea = edges[a.edge_id]
    eb = edges[b.edge_id]

    # 方案 1：同边前进（含 offset 相等的零长移动）
    if a.edge_id == b.edge_id and b.offset >= a.offset:
        delta = b.offset - a.offset
        options.append(Route(DIRECT, delta, (), delta, 0.0, 0.0))

    # 方案 2：剩余边 + 节点最短路 + B 的部分边
    tail = ea.length - a.offset
    mid: Path | None = graph.shortest(ea.to_node, eb.from_node)
    if mid is not None:
        total = tail + mid.distance + b.offset
        options.append(Route(VIA_NODES, total, mid.edges, tail,
                             b.offset, mid.distance))

    if not options:
        return None
    # 等长时方案 1（direct_same_edge）排在前面，优先选取
    options.sort(key=lambda r: (r.distance, 0 if r.kind == DIRECT else 1))
    return options[0]


@dataclass(frozen=True)
class Transition:
    route: Route
    euclidean: float             # 两个原始观测点间的欧氏距离（米）
    cost: float                  # abs(路程 - 欧氏距离) / beta
    feasible: bool               # 路程 <= vmax * (tB - tA)


def evaluate(a: Candidate, b: Candidate, ax: float, ay: float,
             bx: float, by: float, dt: float, graph: Graph,
             edges: dict[str, Edge], beta: float, vmax: float) -> Transition | None:
    """评估一次候选转移；路线不存在返回 None，存在但超速返回 feasible=False。"""
    route = route_between(a, b, graph, edges)
    if route is None:
        return None
    straight = euclid(ax, ay, bx, by)
    cost = abs(route.distance - straight) / beta
    budget = vmax * dt
    return Transition(route, straight, cost, route.distance <= budget)
