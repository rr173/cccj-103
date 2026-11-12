"""有向图与节点间最短路。

边长为正（加载阶段已保证），允许平行边与有向环。Dijkstra 按
(累计长度, edge_id 序列的 UTF-8 字典序) 决定最短路：等长路径之间，
比较整条中间 edge_id 序列的 UTF-8 字节字典序，取最小者。
路径查询按 (from_node, to_node) 缓存；缓存只保存节点间结果，
不保存与具体观测绑定的候选转移。
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass, field

from .geometry import Edge


@dataclass
class Path:
    distance: float
    edges: tuple[str, ...]          # 有序 edge_id 序列；空元组表示零长直达（同节点）

    def key(self) -> tuple[float, tuple[bytes, ...]]:
        return (self.distance, tuple(e.encode("utf-8") for e in self.edges))


@dataclass
class Graph:
    edges: dict[str, Edge]
    adj: dict[str, list[Edge]] = field(default_factory=dict)
    _cache: dict[tuple[str, str], Path | None] = field(default_factory=dict)

    @classmethod
    def build(cls, edge_list: list[Edge]) -> "Graph":
        adj: dict[str, list[Edge]] = {}
        for e in edge_list:
            adj.setdefault(e.from_node, []).append(e)
            adj.setdefault(e.to_node, [])
        g = cls(edges={e.edge_id: e for e in edge_list}, adj=adj)
        return g

    def shortest(self, src: str, dst: str) -> Path | None:
        """src 到 dst 的最短路；src == dst 为零长空路径；不可达返回 None。"""
        key = (src, dst)
        if key in self._cache:
            return self._cache[key]
        if src not in self.adj or dst not in self.adj:
            self._cache[key] = None
            return None
        if src == dst:
            p: Path | None = Path(0.0, ())
            self._cache[key] = p
            return p

        # 堆项：(累计长度, 路径 edge_id 字节序列, 当前节点, edge_id 元组)
        best: dict[str, tuple[float, tuple[bytes, ...]]] = {}
        heap: list[tuple[float, tuple[bytes, ...], str, tuple[str, ...]]] = [
            (0.0, (), src, ())
        ]
        result: Path | None = None
        while heap:
            dist, _bkey, node, seq = heapq.heappop(heap)
            if node == dst:
                result = Path(dist, seq)
                break
            cur = best.get(node)
            cur_key = (dist, tuple(e.encode("utf-8") for e in seq))
            if cur is not None and cur < cur_key:
                continue
            for e in self.adj.get(node, ()):
                nd = dist + e.length
                nseq = seq + (e.edge_id,)
                nb = tuple(x.encode("utf-8") for x in nseq)
                old = best.get(e.to_node)
                nk = (nd, nb)
                if old is None or nk < old:
                    best[e.to_node] = nk
                    heapq.heappush(heap, (nd, nb, e.to_node, nseq))
        self._cache[key] = result
        return result
