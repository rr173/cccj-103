"""平面几何与候选生成（米制坐标）。

所有坐标假定在同一米制平面内，不涉及经纬度。边是节点之间的有向直线段，
长度取欧氏距离，只能从 from_node 向 to_node 行驶。

候选生成规则（严格按需求）：
  把观测点正交投影到每条有向线段；投影落在线外就夹到端点；
  垂直（即点到投影点的欧氏）距离不超过 radius 才保留；
  先按 distance、再按 edge_id 的 UTF-8 字节序排序，取前 K 个。
  候选保存投影坐标与沿通行方向从边起点量出的 offset。
"""
from __future__ import annotations

import math
from dataclasses import dataclass


def euclid(ax: float, ay: float, bx: float, by: float) -> float:
    return math.hypot(ax - bx, ay - by)


@dataclass(frozen=True)
class Edge:
    edge_id: str
    from_node: str
    to_node: str
    length: float
    ax: float
    ay: float
    bx: float
    by: float


@dataclass(frozen=True)
class Candidate:
    edge_id: str
    distance: float          # 观测点到投影点的垂直距离
    px: float                # 投影点（夹取后）x
    py: float
    offset: float            # 沿通行方向从边起点量出的距离，0 <= offset <= length
    point_cost: float        # distance^2 / (2*sigma^2)

    def rank_key(self) -> tuple[float, bytes]:
        return (self.distance, self.edge_id.encode("utf-8"))


def project(edge: Edge, x: float, y: float) -> tuple[float, float, float]:
    """把 (x,y) 正交投影到有向线段，线外夹到端点。

    返回 (投影 x, 投影 y, 沿边方向的 offset)。
    """
    dx = edge.bx - edge.ax
    dy = edge.by - edge.ay
    # length 已保证 > 0，无需处理零向量
    s = ((x - edge.ax) * dx + (y - edge.ay) * dy) / (edge.length * edge.length)
    if s <= 0.0:
        return edge.ax, edge.ay, 0.0
    if s >= 1.0:
        return edge.bx, edge.by, edge.length
    offset = s * edge.length
    return edge.ax + s * dx, edge.ay + s * dy, offset


def candidates_for(x: float, y: float, edges: list[Edge],
                   radius: float, k: int, sigma: float) -> list[Candidate]:
    """生成一个观测点的候选列表（已按规则排序并截断到 K）。"""
    out: list[Candidate] = []
    for edge in edges:
        px, py, offset = project(edge, x, y)
        dist = euclid(x, y, px, py)
        if dist <= radius:
            out.append(Candidate(
                edge_id=edge.edge_id,
                distance=dist,
                px=px,
                py=py,
                offset=offset,
                point_cost=dist * dist / (2.0 * sigma * sigma),
            ))
    out.sort(key=Candidate.rank_key)
    return out[:k]
