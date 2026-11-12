"""生产结果与独立暴力参考实现的逐项核对（仅用于小图）。"""
from __future__ import annotations

from .graph import Graph
from .matcher import match_track
from .refcheck import ref_match, ref_route


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-8 * max(1.0, abs(a), abs(b))


def check_track(track_id: str, obs_list, edges, params) -> list[str]:
    problems: list[str] = []
    graph = Graph.build(edges)
    pts, segs, transitions = match_track(track_id, obs_list, edges, graph, params)
    rcands, rsegs, revents = ref_match(obs_list, edges, params)

    # 候选集合（边序、距离、offset、单点代价）
    for i, (pt, rc) in enumerate(zip(pts, rcands)):
        if len(pt.candidates) != len(rc):
            problems.append(f"{track_id} 第{i}层候选数 {len(pt.candidates)} != {len(rc)}")
            continue
        for c, r in zip(pt.candidates, rc):
            if c.edge_id != r.edge_id:
                problems.append(f"{track_id} 第{i}层候选边顺序不一致")
            for label, v, rv in (("距离", c.distance, r.distance),
                                 ("offset", c.offset, r.offset),
                                 ("单点代价", c.point_cost, r.point_cost)):
                if not _close(v, rv):
                    problems.append(
                        f"{track_id} 第{i}层 {c.edge_id} {label} {v} != 参考 {rv}")

    # 事件（切段原因）
    for i, pt in enumerate(pts):
        if sorted(pt.events) != sorted(revents.get(i, [])):
            problems.append(
                f"{track_id} 第{i}层事件 {pt.events} != 参考 {revents.get(i)}")

    # 片段划分、选边、分数、断开原因
    if len(segs) != len(rsegs):
        problems.append(f"{track_id} 片段数 {len(segs)} != 参考 {len(rsegs)}")
    for k, (seg, rseg) in enumerate(zip(segs, rsegs)):
        prod_layers = [i for i, pt in enumerate(pts)
                       if pt.segment_id == seg.segment_id]
        if prod_layers != rseg.layers:
            problems.append(
                f"{track_id} 片段{k}层号 {prod_layers} != 参考 {rseg.layers}")
        prod_edges = [pts[i].candidates[pts[i].chosen_idx].edge_id
                      for i in prod_layers]
        ref_edges = [c.edge_id for c in rseg.choices]
        if prod_edges != ref_edges:
            problems.append(
                f"{track_id} 片段{k}选边 {prod_edges} != 参考 {ref_edges}")
        for label, v, rv in (("总分", seg.total_score, rseg.total),
                             ("定位和", seg.point_cost_sum, rseg.point_sum),
                             ("转移和", seg.transition_cost_sum, rseg.trans_sum)):
            if not _close(v, rv):
                problems.append(
                    f"{track_id} 片段{k}{label} {v!r} != 参考 {rv!r}"
                    f"（差 {v-rv:.3e}）")
        if seg.end_reason != rseg.end_reason:
            problems.append(
                f"{track_id} 片段{k}断开原因 {seg.end_reason} != 参考 {rseg.end_reason}")

    # 选中转移的路线明细：用参考路线函数独立重算
    for (fo, to_), tr in transitions.items():
        ia = next(i for i, o in enumerate(obs_list) if o.obs_id == fo)
        ib = next(i for i, o in enumerate(obs_list) if o.obs_id == to_)
        ca = pts[ia].candidates[pts[ia].chosen_idx]
        cb = pts[ib].candidates[pts[ib].chosen_idx]
        # 用参考候选对象（同边同 offset）走参考路线
        ra = next(c for c in rcands[ia] if c.edge_id == ca.edge_id)
        rb = next(c for c in rcands[ib] if c.edge_id == cb.edge_id)
        desc = ref_route(ra, rb, obs_list[ia], obs_list[ib], edges, params)
        if desc is None:
            problems.append(f"{track_id} 转移 {fo}->{to_} 参考判定不可达")
            continue
        if desc["kind"] != tr.route.kind:
            problems.append(
                f"{track_id} 转移 {fo}->{to_} 路线类型 {tr.route.kind} != {desc['kind']}")
        if not _close(desc["distance"], tr.route.distance):
            problems.append(
                f"{track_id} 转移 {fo}->{to_} 路程 {tr.route.distance} != {desc['distance']}")
        if tuple(desc["middle"]) != tr.route.middle_edges:
            problems.append(
                f"{track_id} 转移 {fo}->{to_} 中间道路 {tr.route.middle_edges} "
                f"!= {desc['middle']}")
        if not _close(desc["cost"], tr.cost):
            problems.append(
                f"{track_id} 转移 {fo}->{to_} 转移代价 {tr.cost} != {desc['cost']}")
    return problems


def check_all(data, params) -> list[str]:
    problems: list[str] = []
    for tid in sorted(data.tracks):
        problems += check_track(tid, data.tracks[tid], data.edges, params)
    return problems
