"""独立参考实现：枚举候选组合 + 简单 DFS 中间路径，供小图逐项核对。

本模块刻意不 import 生产的 graph.py / matcher.py / transitions.py，
几何投影、图搜索、序列枚举全部重写一遍，只共享数据类与参数口径。
仅用于点数与候选数都很小的演示材料；组合数随长度指数增长，
禁止用于长轨迹（这正是生产算法必须用动态规划的原因）。
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass

from .geometry import Edge
from .loader import Observation
from .matcher import GAP_TIMEOUT, NO_CANDIDATE, NO_FEASIBLE, Params


@dataclass(frozen=True)
class RefCandidate:
    edge_id: str
    distance: float
    px: float
    py: float
    offset: float
    point_cost: float


@dataclass
class RefSegment:
    segment_id: int
    layers: list                    # 轨迹全局层号
    choices: list                   # 每层选中的 RefCandidate
    total: float
    point_sum: float
    trans_sum: float
    end_reason: str | None


def ref_project(e: Edge, x: float, y: float):
    dx, dy = e.bx - e.ax, e.by - e.ay
    s = ((x - e.ax) * dx + (y - e.ay) * dy) / (e.length * e.length)
    s = 0.0 if s < 0.0 else (1.0 if s > 1.0 else s)
    return e.ax + s * dx, e.ay + s * dy, s * e.length


def ref_candidates(obs: Observation, edges: list[Edge], p: Params):
    out = []
    for e in edges:
        px, py, off = ref_project(e, obs.x, obs.y)
        d = math.hypot(obs.x - px, obs.y - py)
        if d <= p.radius:
            out.append(RefCandidate(
                e.edge_id, d, px, py, off, d * d / (2 * p.sigma * p.sigma)))
    out.sort(key=lambda c: (c.distance, c.edge_id.encode("utf-8")))
    return out[:p.k]


def ref_all_paths(edges: list[Edge], src: str, dst: str, cap: float):
    """DFS 枚举 src->dst 所有不重复节点、总长 <= cap 的简单路径。

    边长为正，带环路径一定比去掉环的路径长，简单路径足够覆盖最短路。
    """
    adj = {}
    for e in edges:
        adj.setdefault(e.from_node, []).append(e)
    results = []

    def dfs(node, acc, seq, visited):
        if node == dst:
            results.append((acc, tuple(x.encode("utf-8") for x in seq), seq))
            return
        for e in sorted(adj.get(node, ()), key=lambda x: x.edge_id):
            if e.to_node in visited:
                continue
            nxt = acc + e.length
            if nxt <= cap:
                dfs(e.to_node, nxt, seq + (e.edge_id,), visited | {e.to_node})

    if src == dst:
        results.append((0.0, (), ()))
    else:
        dfs(src, 0.0, (), frozenset({src}))
    return results


def ref_route(a: RefCandidate, b: RefCandidate, obs_a: Observation,
              obs_b: Observation, edges: list[Edge], p: Params):
    """返回路线描述 dict 或 None；等长时同边直达优先。"""
    emap = {e.edge_id: e for e in edges}
    ea, eb = emap[a.edge_id], emap[b.edge_id]
    budget = p.vmax * (obs_b.t - obs_a.t)
    opts = []
    if a.edge_id == b.edge_id and b.offset >= a.offset:
        d = b.offset - a.offset
        if d <= budget:
            opts.append((d, 0, {"kind": "direct_same_edge", "middle": (),
                                "partial_from": d, "partial_to": 0.0,
                                "middle_distance": 0.0}))
    cap = budget - (ea.length - a.offset) - b.offset
    if cap >= 0:
        paths = ref_all_paths(edges, ea.to_node, eb.from_node, cap)
        if paths:
            md, _b, mids = min(paths, key=lambda z: (z[0], z[1]))
            total = (ea.length - a.offset) + md + b.offset
            opts.append((total, 1, {"kind": "via_nodes", "middle": mids,
                                    "partial_from": ea.length - a.offset,
                                    "partial_to": b.offset,
                                    "middle_distance": md}))
    if not opts:
        return None
    dist, _tie, desc = min(opts, key=lambda z: (z[0], z[1]))
    straight = math.hypot(obs_a.x - obs_b.x, obs_a.y - obs_b.y)
    desc["distance"] = dist
    desc["euclidean"] = straight
    desc["cost"] = abs(dist - straight) / p.beta
    return desc


def _split_segments(obs_list, cands, p):
    """按 gap_timeout / no_candidate / no_feasible_transition 切分片段。

    no_feasible 的判定与生产 DP 同口径：一层一层扩展，只要新一层存在
    至少一个候选能从当前片段某候选转移过来，就留在同片段；否则在该层
    切段并从该层另开。返回 (片段层号列表, 事件表)。
    """
    events = {i: [] for i in range(len(obs_list))}
    segments: list[list[int]] = []
    cur: list[int] = []
    for i, obs in enumerate(obs_list):
        if i > 0 and obs.t - obs_list[i - 1].t > p.max_gap:
            events[i].append(GAP_TIMEOUT)
            if cur:
                segments.append(cur)
                cur = []
        if not cands[i]:
            events[i].append(NO_CANDIDATE)
            if cur:
                segments.append(cur)
                cur = []
            continue
        if not cur:
            cur = [i]
            continue
        feasible = any(
            ref_route(ca, cb, obs_list[cur[-1]], obs, _EDGES, p) is not None
            for ca in cands[cur[-1]] for cb in cands[i])
        if not feasible:
            events[i].append(NO_FEASIBLE)
            segments.append(cur)
            cur = [i]
        else:
            cur.append(i)
    if cur:
        segments.append(cur)
    return segments, events


_EDGES: list = []


def ref_match(obs_list: list[Observation], edges: list[Edge], p: Params):
    """完全按需求文本重写的分段枚举匹配。"""
    global _EDGES
    _EDGES = edges
    cands = [ref_candidates(o, edges, p) for o in obs_list]
    seg_layers, events = _split_segments(obs_list, cands, p)

    ref_segments: list[RefSegment] = []
    for seg_id, layers in enumerate(seg_layers, start=1):
        best = None
        for combo in itertools.product(*(cands[i] for i in layers)):
            psum = sum(c.point_cost for c in combo)
            tsum = 0.0
            routes = []
            ok = True
            for la, lb, ca, cb in zip(layers, layers[1:], combo, combo[1:]):
                desc = ref_route(ca, cb, obs_list[la], obs_list[lb], edges, p)
                if desc is None:
                    ok = False
                    break
                tsum += desc["cost"]
                routes.append(desc)
            if not ok:
                continue
            total = psum + tsum
            key = (total, tuple(c.edge_id.encode("utf-8") for c in combo))
            if best is None or key < best[0]:
                best = (key, list(combo), psum, tsum, routes)
        assert best is not None, f"片段 {seg_id} 无可行组合，切段逻辑有误"
        _key, choices, psum, tsum, _routes = best
        last = layers[-1]
        reason = events[last + 1][0] if last + 1 in events and events[last + 1] else None
        ref_segments.append(RefSegment(
            seg_id, layers, choices, psum + tsum, psum, tsum, reason))
    return cands, ref_segments, events
