"""菱形结构生成器与随机无环网络守恒检查。

30 层菱形：每层两个节点，相邻层完全连接（4 条边，每条 1/4），
完整路径数 = 2^(29)（约 5.37 亿），而图只有 60 个节点、约 118 条边。
本实现用拓扑 DP 在 O(V+E) 内得到精确末端权重，绝不展开路径。
"""
from __future__ import annotations

import random
from fractions import Fraction

from .model import Edge


def diamond_edges(layers: int, weight: str | Fraction = Fraction(1, 2),
                  begin: str = "2024-01-01") -> list[Edge]:
    """生成 `layers` 层菱形（每层 Lk、Rk 两节点）。返回带 edge_key 的边列表。

    层 0 是起点 SRC；随后每层 (L_i, R_i)。相邻层完全二分连接。
    每个非末级节点恰有 2 条出边，每条权重 1/2，故逐节点守恒。
    """
    w = Fraction(weight)
    edges: list[Edge] = []

    def add(src, dst, idx):
        edges.append(Edge(f"D{idx:04d}", src, dst,
                          _date(begin), None, w.numerator, w.denominator))

    prev = ["SRC"]
    idx = 0
    for i in range(layers):
        cur = [f"L{i}", f"R{i}"]
        for s in prev:
            for d in cur:
                add(s, d, idx)
                idx += 1
        prev = cur
    ends = ["END-L", "END-R"]
    for s in prev:
        for d in ends:
            add(s, d, idx)
            idx += 1
    return edges


def _date(s: str):
    from .model import parse_date
    return parse_date(s)


def count_paths(layers: int) -> int:
    """菱形从 SRC 到某个指定末级的完整路径条数。

    SRC->layer0 有 2 条；层间每一步在 2 个后继中选 1，共 layers-1 步；
    最后一步到指定末级唯一。故每个末级有 2^layers 条路径。
    """
    return 2 ** layers


def count_total_paths(layers: int) -> int:
    """到所有末级的路径总数（两个末级各 2^layers）。"""
    return 2 ** (layers + 1)


# ---------------------------------------------------------------- 随机无环网络

def random_dag(seed: int, n_nodes: int = 8, n_edges: int | None = None,
               begin: str = "2024-01-01") -> tuple[list[Edge], list[str], list[str]]:
    """随机分层 DAG：边总从小到大（按节点序号），保证无环。

    每个中间节点的出边份额随机但归一为 1（精确有理数）。
    返回 (edges, roots, terminals)；roots 为入度 0 节点，terminals 为末级（出度 0）。
    """
    rng = random.Random(seed)
    nodes = [f"N{i:02d}" for i in range(n_nodes)]
    if n_edges is None:
        n_edges = n_nodes * 2

    pairs: set[tuple[int, int]] = set()
    attempts = 0
    while len(pairs) < n_edges and attempts < n_edges * 50:
        attempts += 1
        i = rng.randrange(0, n_nodes - 1)
        j = rng.randrange(i + 1, n_nodes)
        pairs.add((i, j))

    by_src: dict[int, list[int]] = {}
    for i, j in sorted(pairs):
        by_src.setdefault(i, []).append(j)

    edges: list[Edge] = []
    for i, dests in by_src.items():
        # 随机整数权重，归一化为精确份额
        parts = [rng.randrange(1, 6) for _ in dests]
        total = sum(parts)
        for k, j in enumerate(dests):
            edges.append(Edge(f"R{i:02d}-{j:02d}-{k}", nodes[i], nodes[j],
                              _date(begin), None, parts[k], total))
    used = {e.src for e in edges} | {e.dst for e in edges}
    indeg = {n: 0 for n in nodes}
    outdeg = {n: 0 for n in nodes}
    for e in edges:
        indeg[e.dst] += 1
        outdeg[e.src] += 1
    roots = sorted(n for n in used if indeg[n] == 0)
    terminals = sorted(n for n in used if outdeg[n] == 0)
    return edges, roots, terminals
