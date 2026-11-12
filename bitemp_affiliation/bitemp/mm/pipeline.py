"""管线编排：校验 -> 匹配 -> 入库 -> 导出 / 查询。"""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path

from .graph import Graph
from .loader import LoadedData, RoadConfigError, load_all
from .matcher import Params, match_track
from .store import connect_readonly, reset_and_store


@dataclass
class RunSummary:
    db_path: str
    tracks_total: int
    tracks_rejected: int
    tracks_matched: int
    observations_total: int
    points_matched: int
    points_unmatched: int
    segments_total: int
    rejected: dict
    params: dict


def validate_params(radius, k, sigma, beta, vmax, max_gap) -> Params:
    vals = {"radius": radius, "sigma": sigma, "beta": beta,
            "vmax": vmax, "max_gap": max_gap}
    bad = [name for name, v in vals.items()
           if not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0]
    if not isinstance(k, int) or isinstance(k, bool) or k <= 0:
        bad.append("k")
    if bad:
        raise ValueError("参数必须均为正数，K 为正整数；非法参数: " + ", ".join(bad))
    return Params(float(radius), k, float(sigma), float(beta),
                  float(vmax), float(max_gap))


def run(nodes_csv, edges_csv, obs_csv, db_path: str,
        radius: float, k: int, sigma: float, beta: float,
        vmax: float, max_gap: float) -> RunSummary:
    params = validate_params(radius, k, sigma, beta, vmax, max_gap)
    data = load_all(nodes_csv, edges_csv, obs_csv)
    graph = Graph.build(data.edges)

    track_results = {}
    n_obs = n_match = n_unmatch = n_seg = 0
    for tid in sorted(data.tracks):
        obs_list = data.tracks[tid]
        points, segments, transitions = match_track(
            tid, obs_list, data.edges, graph, params)
        track_results[tid] = (points, segments, transitions)
        n_obs += len(obs_list)
        n_match += sum(1 for pt in points if pt.state == "matched")
        n_unmatch += sum(1 for pt in points if pt.state == "unmatched")
        n_seg += len(segments)

    param_dict = {"radius": radius, "k": k, "sigma": sigma, "beta": beta,
                  "vmax": vmax, "max_gap": max_gap}
    reset_and_store(db_path, data, param_dict, track_results, data.tracks)
    return RunSummary(
        db_path=str(db_path),
        tracks_total=len(data.tracks) + len(data.rejected),
        tracks_rejected=len(data.rejected),
        tracks_matched=len(data.tracks),
        observations_total=n_obs,
        points_matched=n_match,
        points_unmatched=n_unmatch,
        segments_total=n_seg,
        rejected=dict(data.rejected),
        params=param_dict,
    )


# ---------------------------------------------------------------- 导出

def export_matches(db_path: str, out_csv: str) -> int:
    """逐点匹配结果导出，按 (track_id, 观测时间序) 稳定排序。"""
    con = connect_readonly(db_path)
    try:
        rows = con.execute(
            "SELECT m.track_id, o.seq, m.obs_id, o.t, o.x, o.y, m.state, "
            "m.segment_id, m.edge_id, m.offset_m, m.px, m.py, "
            "m.gps_distance, m.point_cost, m.cand_rank, m.line_no "
            "FROM matches m JOIN observations o "
            "ON m.track_id=o.track_id AND m.obs_id=o.obs_id "
            "ORDER BY m.track_id, o.seq").fetchall()
    finally:
        con.close()
    header = ["track_id", "seq", "obs_id", "t", "x", "y", "state",
              "segment_id", "edge_id", "offset_m", "proj_x", "proj_y",
              "gps_distance", "point_cost", "cand_rank", "source_line"]
    Path(out_csv).parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return len(rows)


def export_segments(db_path: str, out_csv: str) -> int:
    con = connect_readonly(db_path)
    try:
        rows = con.execute(
            "SELECT track_id, segment_id, start_obs, end_obs, start_t, end_t, "
            "n_points, total_score, point_cost_sum, transition_cost_sum, "
            "end_reason FROM segments ORDER BY track_id, segment_id").fetchall()
    finally:
        con.close()
    header = ["track_id", "segment_id", "start_obs", "end_obs", "start_t",
              "end_t", "n_points", "total_score", "point_cost_sum",
              "transition_cost_sum", "end_reason"]
    Path(out_csv).parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return len(rows)


# ---------------------------------------------------------------- 查询

def _fmt(v, digits=10):
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.{digits}g}"
    return str(v)


def track_text(db_path: str, track_id: str) -> str:
    con = connect_readonly(db_path)
    try:
        exists = con.execute(
            "SELECT 1 FROM observations WHERE track_id=? LIMIT 1",
            [track_id]).fetchone()
        rej = con.execute(
            "SELECT reason FROM rejected_tracks WHERE track_id=? ORDER BY reason",
            [track_id]).fetchall()
        if not exists:
            if rej:
                lines = [f"轨迹 {track_id}：已整条拒收，原因："]
                lines += [f"  - {r[0]}" for r in rej]
                return "\n".join(lines)
            raise KeyError(f"库中不存在轨迹 {track_id}")
        lines = [f"轨迹 {track_id}"]
        segs = con.execute(
            "SELECT segment_id, start_obs, end_obs, n_points, total_score, "
            "point_cost_sum, transition_cost_sum, end_reason "
            "FROM segments WHERE track_id=? ORDER BY segment_id",
            [track_id]).fetchall()
        lines.append(f"片段 {len(segs)} 个：")
        for sid, so, eo, n, tot, pc, tc, reason in segs:
            lines.append(
                f"  #{sid} {so}..{eo}（{n} 点）总分={_fmt(tot)} "
                f"定位={_fmt(pc)} 转移={_fmt(tc)}"
                f" 断开原因={reason or '轨迹结束'}")
        pts = con.execute(
            "SELECT o.seq, m.obs_id, o.t, o.x, o.y, m.state, m.segment_id, "
            "m.edge_id, m.offset_m, m.gps_distance "
            "FROM matches m JOIN observations o "
            "ON m.track_id=o.track_id AND m.obs_id=o.obs_id "
            "WHERE m.track_id=? ORDER BY o.seq", [track_id]).fetchall()
        lines.append("逐点：")
        for seq, oid, t, x, y, state, sid, eid, off, dist in pts:
            if state == "matched":
                lines.append(
                    f"  {seq:>2} {oid:<6} t={_fmt(t):>6} ({_fmt(x)},{_fmt(y)}) "
                    f"-> {eid} offset={_fmt(off)} 定位距离={_fmt(dist)} 片段#{sid}")
            else:
                ev = [r[0] for r in con.execute(
                    "SELECT event FROM point_events WHERE track_id=? AND obs_id=? "
                    "ORDER BY seq", [track_id, oid]).fetchall()]
                lines.append(
                    f"  {seq:>2} {oid:<6} t={_fmt(t):>6} ({_fmt(x)},{_fmt(y)}) "
                    f"-> unmatched（{', '.join(ev)}）")
        return "\n".join(lines)
    finally:
        con.close()


def explain_text(db_path: str, track_id: str, obs_id: str) -> str:
    con = connect_readonly(db_path)
    try:
        row = con.execute(
            "SELECT o.t, o.x, o.y, m.state, m.segment_id, m.edge_id, "
            "m.offset_m, m.px, m.py, m.gps_distance, m.point_cost, m.cand_rank "
            "FROM observations o JOIN matches m "
            "ON o.track_id=m.track_id AND o.obs_id=m.obs_id "
            "WHERE o.track_id=? AND o.obs_id=?", [track_id, obs_id]).fetchone()
        if row is None:
            raise KeyError(f"库中不存在 {track_id}/{obs_id}")
        t, x, y, state, sid, eid, off, px, py, dist, pc, crank = row
        lines = [f"观测 {track_id}/{obs_id}  t={_fmt(t)} 原始定位=({_fmt(x)},{_fmt(y)})",
                 f"状态={state} 片段={sid}"]
        if state == "matched":
            lines.append(
                f"所选边={eid} offset={_fmt(off)} 投影=({_fmt(px)},{_fmt(py)}) "
                f"定位距离={_fmt(dist)} 单点代价={_fmt(pc)} 候选名次={crank}")
        cands = con.execute(
            "SELECT cand_rank, edge_id, distance, px, py, offset_m, point_cost "
            "FROM candidates WHERE track_id=? AND obs_id=? ORDER BY cand_rank",
            [track_id, obs_id]).fetchall()
        lines.append(f"实际保留候选 {len(cands)} 个（按距离、edge_id UTF-8 字节序）：")
        for rank, ce, cd, cpx, cpy, coff, cpc in cands:
            mark = " <- 选中" if ce == eid and state == "matched" else ""
            lines.append(
                f"  #{rank} {ce:<8} 距离={_fmt(cd):>8} offset={_fmt(coff):>8} "
                f"投影=({_fmt(cpx)},{_fmt(cpy)}) 单点代价={_fmt(cpc)}{mark}")
        events = con.execute(
            "SELECT event FROM point_events WHERE track_id=? AND obs_id=? ORDER BY seq",
            [track_id, obs_id]).fetchall()
        if events:
            lines.append("事件：" + ", ".join(e[0] for e in events))
        if state == "matched":
            tin = con.execute(
                "SELECT from_obs, to_obs, from_edge, to_edge, route_kind, "
                "route_distance, euclidean, partial_from, partial_to, "
                "middle_distance, middle_edges, transition_cost "
                "FROM transitions WHERE track_id=? AND to_obs=?",
                [track_id, obs_id]).fetchone()
            if tin:
                lines.append("")
                lines.append("进入本点的转移依据：")
                _append_route(lines, tin)
            tout = con.execute(
                "SELECT from_obs, to_obs, from_edge, to_edge, route_kind, "
                "route_distance, euclidean, partial_from, partial_to, "
                "middle_distance, middle_edges, transition_cost "
                "FROM transitions WHERE track_id=? AND from_obs=?",
                [track_id, obs_id]).fetchone()
            if tout:
                lines.append("")
                lines.append("离开本点的转移依据：")
                _append_route(lines, tout)
        return "\n".join(lines)
    finally:
        con.close()


def _append_route(lines: list[str], r) -> None:
    (fo, to_, fe, te, kind, rd, eu, pf, pto, md, mids, tc) = r
    lines.append(f"  {fo} -> {to_}：候选 {fe} -> {te}")
    if kind == "direct_same_edge":
        lines.append(f"  路线：直接同边前进，部分边长度={_fmt(pf)}")
    else:
        mid_text = mids if mids else "无（A 边终点即 B 边起点，零长最短路）"
        lines.append(
            f"  路线：经过节点；A 剩余边长={_fmt(pf)}，"
            f"中间道路=[{mid_text}] 长度={_fmt(md)}，"
            f"B 部分边={_fmt(pto)}")
    lines.append(
        f"  总路程={_fmt(rd)} 原始两点欧氏距离={_fmt(eu)} "
        f"转移代价=|路程-欧氏|/beta={_fmt(tc)}")
