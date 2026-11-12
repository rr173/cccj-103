#!/usr/bin/env python3
"""验收测试：python3 tests_mm.py。

覆盖：
  候选投影夹取/距离排序/K 截断/UTF-8 字节序；单点代价公式；
  同边前进直达、同边倒退有回环/无回环、平行单行线字典序决胜、
  两套等长路线的中间 edge_id 序列决胜、速度恰在上限/超过上限；
  gap_timeout、no_candidate 恢复、同点超时且无候选、no_feasible 切段不丢点；
  总分手工核对（1e-8）、生产 DP 与暴力参考全量一致；
  坏道路配置拒绝运行并指出文件行号；坏轨迹隔离不拖垮正常轨迹；
  非严格时间/重复 obs_id/非有限数拒收；乱序输入结果不变；
  DuckDB 关闭重开可查；参数校验；不枚举整条轨迹组合（长轨迹证据）。
"""
from __future__ import annotations

import math
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bitemp.mm import biggen, datagen, pipeline, verify  # noqa: E402
from bitemp.mm.geometry import Edge, candidates_for, euclid  # noqa: E402
from bitemp.mm.graph import Graph  # noqa: E402
from bitemp.mm.loader import RoadConfigError, load_all  # noqa: E402
from bitemp.mm.matcher import (GAP_TIMEOUT, NO_CANDIDATE, NO_FEASIBLE,  # noqa: E402
                               Params, match_track)
from bitemp.mm.store import connect_readonly  # noqa: E402

PASSED = []


def check(name):
    def deco(fn):
        fn()
        PASSED.append(name)
        print(f"  ✔ {name}")
    return deco


def _edge(eid, a, b):
    (na, ax, ay), (nb, bx, by) = a, b
    return Edge(eid, na, nb, math.hypot(bx - ax, by - ay), ax, ay, bx, by)


# ------------------------------------------------------------ 几何与候选

@check("投影落在线外夹到端点，offset 与距离正确")
def _():
    e = _edge("E", ("A", 0.0, 0.0), ("B", 10.0, 0.0))
    c = candidates_for(12.0, 3.0, [e], radius=5.0, k=10, sigma=5.0)
    assert len(c) == 1 and c[0].edge_id == "E"
    assert c[0].px == 10.0 and c[0].py == 0.0 and c[0].offset == 10.0
    assert abs(c[0].distance - math.hypot(2, 3)) < 1e-12
    c2 = candidates_for(-3.0, 1.0, [e], radius=5.0, k=10, sigma=5.0)
    assert c2[0].offset == 0.0 and c2[0].px == 0.0


@check("半径外不候选；先距离后 edge_id UTF-8 字节序，截断到 K")
def _():
    e1 = _edge("E_A", ("A", 0.0, 0.0), ("B", 10.0, 0.0))
    e2 = _edge("E_B", ("A", 0.0, 0.0), ("C", 10.0, 0.0))  # 平行同几何
    edges = [e2, e1]
    c = candidates_for(5.0, 3.0, edges, radius=100.0, k=1, sigma=5.0)
    assert len(c) == 1 and c[0].edge_id == "E_A"        # 同距按字节序，K=1
    c_all = candidates_for(5.0, 3.0, edges, radius=100.0, k=10, sigma=5.0)
    assert [x.edge_id for x in c_all] == ["E_A", "E_B"]
    none = candidates_for(5.0, 99.0, edges, radius=5.0, k=10, sigma=5.0)
    assert none == []
    # 单点代价 = d^2/(2 sigma^2)
    assert abs(c_all[0].point_cost - 9.0 / 50.0) < 1e-12


@check("UTF-8 字节序：多字节字符按字节而非码点直觉排序")
def _():
    # "é"(UTF-8 c3 a9) 字节序在 "z"(7a) 之后
    e1 = _edge("z", ("A", 0.0, 0.0), ("B", 10.0, 0.0))
    e2 = _edge("é", ("A", 0.0, 0.0), ("B", 10.0, 0.0))
    c = candidates_for(5.0, 0.0, [e2, e1], 100.0, 10, 5.0)
    assert [x.edge_id for x in c] == ["z", "é"]


# ------------------------------------------------------------ 图与转移

@check("有向图只沿 from->to；平行边与有向环；等长路按 edge_id 序列决胜")
def _():
    nodes = {"A": (0, 0), "B": (10, 0), "C": (10, 10), "D": (0, 10),
             "X": (5, 0), "S": (20, 20)}
    edges = [
        _edge("e1", ("A", 0, 0), ("X", 5, 0)),
        _edge("e2", ("X", 5, 0), ("B", 10, 0)),
        _edge("eA", ("A", 0, 0), ("B", 10, 0)),       # 与 e1+e2 等长 10
        _edge("e3", ("B", 10, 0), ("C", 10, 10)),
        _edge("e4", ("C", 10, 10), ("D", 0, 10)),
        _edge("e5", ("D", 0, 10), ("A", 0, 0)),
        _edge("eS", ("C", 10, 10), ("S", 20, 20)),    # S 为无出边汇点
    ]
    g = Graph.build(edges)
    p = g.shortest("A", "B")
    assert p.distance == 10.0 and p.edges == ("e1", "e2")  # (e1,e2) < (eA,)
    # 汇点 S 没有任何出边 -> 反向不可达（有向性）
    assert g.shortest("S", "A") is None
    # 绕有向环 B->C->D->A 可回到 A
    loop = g.shortest("B", "A")
    assert loop is not None and loop.edges == ("e3", "e4", "e5")
    # 缓存命中返回同对象
    assert g.shortest("A", "B") is p


@check("同边倒退不取偏移差绝对值；无环不可连接")
def _():
    edges = [_edge("E", ("A", 0, 0), ("B", 100, 0))]
    g = Graph.build(edges)
    from bitemp.mm.geometry import Candidate
    from bitemp.mm.transitions import route_between
    a = Candidate("E", 0, 80, 0, 80, 0.0)
    b = Candidate("E", 0, 20, 0, 20, 0.0)
    assert route_between(a, b, g, {e.edge_id: e for e in edges}) is None
    fwd = Candidate("E", 0, 90, 0, 90, 0.0)
    r = route_between(a, fwd, g, {e.edge_id: e for e in edges})
    assert r is not None and r.kind == "direct_same_edge" and r.distance == 10.0


# ------------------------------------------------------------ 演示小图全量

def _demo_loaded():
    d = tempfile.mkdtemp()
    info = datagen.write_demo_dataset(d)
    data = load_all(f"{d}/nodes.csv", f"{d}/edges.csv",
                    f"{d}/observations.csv")
    return d, info, data


@check("演示小图：生产 DP 与暴力参考实现全部一致")
def _():
    _d, info, data = _demo_loaded()
    problems = verify.check_all(data, Params(**info["params"]))
    assert not problems, problems


@check("T_NEAREST：最近边是死路，全局解在 o2 选非最近候选且保持一个片段")
def _():
    _d, info, data = _demo_loaded()
    g = Graph.build(data.edges)
    p = Params(**info["params"])
    pts, segs, _tr = match_track("T_NEAREST", data.tracks["T_NEAREST"],
                                 data.edges, g, p)
    assert len(segs) == 1
    nearest = pts[1].candidates[0].edge_id
    chosen = pts[1].candidates[pts[1].chosen_idx].edge_id
    assert nearest == "E_J" and chosen != "E_J"
    # 若每点只取最近边：o2=E_J，E_J 终点 NJ1 无任何出边 -> o3 必切段
    # 手工核对总分（见 README 手算表）
    assert abs(segs[0].total_score - 20.5008373889) < 1e-8


@check("平行单行线：等代价按整条 edge_id 列表字典序选 E_A")
def _():
    _d, info, data = _demo_loaded()
    g = Graph.build(data.edges)
    pts, segs, _ = match_track("T_PARALLEL", data.tracks["T_PARALLEL"],
                               data.edges, g, Params(**info["params"]))
    assert [pt.candidates[pt.chosen_idx].edge_id for pt in pts] == ["E_A", "E_A"]


@check("同边倒退有回环：路程 300，速度恰等于上限允许通过")
def _():
    _d, info, data = _demo_loaded()
    g = Graph.build(data.edges)
    pts, segs, tr = match_track("T_SPEED", data.tracks["T_SPEED"],
                                data.edges, g, Params(**info["params"]))
    assert len(segs) == 1
    t = tr[("s1", "s2")]
    assert abs(t.route.distance - 300.0) < 1e-12
    assert t.feasible and t.route.middle_edges == ("E_R1", "E_R2", "E_R3")


@check("超过速度上限：转移不可用并触发 no_feasible_transition 切段")
def _():
    _d, _info, data = _demo_loaded()
    g = Graph.build(data.edges)
    # 候选对之间最短也有 260（经 E_R1/E_R3 的回环），预算 vmax*10：
    # vmax=25 -> 250 < 260，所有候选转移全部超速，必须切段
    p = Params(radius=25.0, k=4, sigma=5.0, beta=10.0,
               vmax=25.0, max_gap=15.0)
    pts, segs, tr = match_track("T_SPEED", data.tracks["T_SPEED"],
                                data.edges, g, p)
    assert len(segs) == 2
    assert NO_FEASIBLE in pts[1].events
    assert not tr  # 没有任何被选中的转移
    # 恰在上限 vmax=30：回环总路程 300 = 预算，等于上限允许通过
    p_ok = Params(radius=25.0, k=4, sigma=5.0, beta=10.0,
                  vmax=30.0, max_gap=15.0)
    _pts2, segs2, tr2 = match_track("T_SPEED", data.tracks["T_SPEED"],
                                    data.edges, g, p_ok)
    assert len(segs2) == 1 and ("s1", "s2") in tr2
    assert abs(tr2[("s1", "s2")].route.distance - 300.0) < 1e-12


@check("无候选点 unmatched 断开，之后有候选恢复为新片段")
def _():
    _d, info, data = _demo_loaded()
    g = Graph.build(data.edges)
    pts, segs, _ = match_track("T_HOLE", data.tracks["T_HOLE"],
                               data.edges, g, Params(**info["params"]))
    assert pts[1].state == "unmatched" and pts[1].segment_id is None
    assert NO_CANDIDATE in pts[1].events
    assert [s.segment_id for s in segs] == [1, 2]
    assert pts[0].segment_id == 1 and pts[2].segment_id == 2


@check("超时与无候选在同一点同时记录；超时先于转移尝试")
def _():
    _d, info, data = _demo_loaded()
    g = Graph.build(data.edges)
    pts, segs, _ = match_track("T_GAP_NO_CAND",
                               data.tracks["T_GAP_NO_CAND"],
                               data.edges, g, Params(**info["params"]))
    assert GAP_TIMEOUT in pts[1].events and NO_CANDIDATE in pts[1].events
    assert len(segs) == 2


@check("长间隔 max_gap 切段；同边前进走直接偏移差")
def _():
    _d, info, data = _demo_loaded()
    g = Graph.build(data.edges)
    pts, segs, tr = match_track("T_GAP", data.tracks["T_GAP"],
                                data.edges, g, Params(**info["params"]))
    assert GAP_TIMEOUT in pts[1].events and len(segs) == 2
    # 同边前进的直达路径
    _p, _s, tr2 = match_track("T_PARALLEL", data.tracks["T_PARALLEL"],
                              data.edges, g, Params(**info["params"]))
    r = tr2[("p1", "p2")]
    assert r.route.kind == "direct_same_edge" and abs(r.route.distance - 30.0) < 1e-12


@check("两套等长路线：按整条 edge_id 列表字典序决胜（H 路胜 L 路）")
def _():
    _d, info, data = _demo_loaded()
    g = Graph.build(data.edges)
    pts, segs, tr = match_track("T_SPLIT", data.tracks["T_SPLIT"],
                                data.edges, g, Params(**info["params"]))
    # 节点级等长最短路自身的字典序决胜
    sp = g.shortest("N2", "N3")
    assert sp.distance == 250.0 and sp.edges == ("E_H1", "E_H2")
    # 观测选边：u1=E_H1、u2=E_H2（H 字节序 < L），两套路线总分完全相等
    assert pts[0].candidates[pts[0].chosen_idx].edge_id == "E_H1"
    assert pts[1].candidates[pts[1].chosen_idx].edge_id == "E_H2"
    t = tr[("u1", "u2")]
    assert abs(t.route.distance - 244.0) < 1e-12
    # 选中 H 路后，u1 终点 NX 即 u2 起点 -> 中间最短路为空（零长）
    assert t.route.middle_edges == ()
    # 手工总分：定位 0.32+0.32；转移 |244 - 140|/10 = 10.4
    straight = euclid(305, 0, 445, 0)
    expect = 0.64 + abs(244.0 - straight) / 10.0
    assert abs(segs[0].total_score - expect) < 1e-8


@check("中间路径非空时的等长最短路字典序决胜")
def _():
    # 两条等长平行路径，边序列不同且都非空
    edges = [
        _edge("a1", ("S", 0, 0), ("M", 10, 0)),
        _edge("a2", ("M", 10, 0), ("T", 20, 0)),
        _edge("b1", ("S", 0, 0), ("Q", 10, 10)),
        _edge("b2", ("Q", 10, 10), ("T", 20, 0)),
    ]
    g = Graph.build(edges)
    sp = g.shortest("S", "T")
    assert sp.edges == ("a1", "a2") and abs(sp.distance - 20.0) < 1e-12


@check("无回环倒退：两个单点段，断开原因 no_feasible_transition")
def _():
    _d, info, data = _demo_loaded()
    g = Graph.build(data.edges)
    pts, segs, tr = match_track("T_BACK_NOLOOP",
                                data.tracks["T_BACK_NOLOOP"],
                                data.edges, g, Params(**info["params"]))
    assert len(segs) == 2 and all(s.n_points == 1 for s in segs)
    assert segs[0].end_reason == NO_FEASIBLE


# ------------------------------------------------------------ 校验与隔离

@check("道路配置坏：拒绝运行并指出文件与行号（nodes 阶段）")
def _():
    d = Path(tempfile.mkdtemp())
    (d / "nodes.csv").write_text(
        "node_id,x,y\nA,0,0\nB,10,0\nA,5,5\n", encoding="utf-8")
    (d / "edges.csv").write_text(
        "edge_id,from_node,to_node\nE1,A,B\n", encoding="utf-8")
    (d / "observations.csv").write_text(
        "track_id,obs_id,t,x,y\nT,o1,0,1,1\n", encoding="utf-8")
    try:
        load_all(d / "nodes.csv", d / "edges.csv", d / "observations.csv")
    except RoadConfigError as e:
        msg = " ".join(e.messages)
        assert "nodes.csv 第 4 行" in msg and "重复" in msg
    else:
        raise AssertionError("坏道路配置未被拒绝")


@check("道路配置坏：边引用不存在的端点，指出 edges 文件行号")
def _():
    d = Path(tempfile.mkdtemp())
    (d / "nodes.csv").write_text(
        "node_id,x,y\nA,0,0\nB,10,0\n", encoding="utf-8")
    (d / "edges.csv").write_text(
        "edge_id,from_node,to_node\nE1,A,B\nE2,B,GHOST\n", encoding="utf-8")
    (d / "observations.csv").write_text(
        "track_id,obs_id,t,x,y\nT,o1,0,1,1\n", encoding="utf-8")
    try:
        load_all(d / "nodes.csv", d / "edges.csv", d / "observations.csv")
    except RoadConfigError as e:
        msg = " ".join(e.messages)
        assert "edges.csv 第 3 行" in msg and "GHOST" in msg
    else:
        raise AssertionError("坏边配置未被拒绝")


@check("nodes 阶段：非有限坐标拒绝并指行号")
def _():
    d = Path(tempfile.mkdtemp())
    (d / "nodes.csv").write_text(
        "node_id,x,y\nA,0,0\nB,0,0\nC,10,inf\n", encoding="utf-8")
    (d / "edges.csv").write_text(
        "edge_id,from_node,to_node\nE1,A,B\n", encoding="utf-8")
    (d / "observations.csv").write_text(
        "track_id,obs_id,t,x,y\nT,o1,0,1,1\n", encoding="utf-8")
    try:
        load_all(d / "nodes.csv", d / "edges.csv", d / "observations.csv")
    except RoadConfigError as e:
        assert "有限数" in " ".join(e.messages)
    else:
        raise AssertionError("非有限坐标未拒绝")


@check("edges 阶段：零边长、自环、空 edge_id 均拒绝")
def _():
    d = Path(tempfile.mkdtemp())
    (d / "nodes.csv").write_text(
        "node_id,x,y\nA,0,0\nB,0,0\nC,10,5\n", encoding="utf-8")
    (d / "edges.csv").write_text(
        "edge_id,from_node,to_node\nE1,A,B\n,A,C\nE3,C,C\n",
        encoding="utf-8")
    (d / "observations.csv").write_text(
        "track_id,obs_id,t,x,y\nT,o1,0,1,1\n", encoding="utf-8")
    try:
        load_all(d / "nodes.csv", d / "edges.csv", d / "observations.csv")
    except RoadConfigError as e:
        msg = " ".join(e.messages)
        assert "长度为 0" in msg and "edge_id 为空" in msg and "自环" in msg
    else:
        raise AssertionError("未拒绝")


@check("文件不存在拒绝运行")
def _():
    d = Path(tempfile.mkdtemp())
    try:
        load_all(d / "nope.csv", d / "edges.csv", d / "observations.csv")
    except RoadConfigError:
        pass
    else:
        raise AssertionError("缺文件未拒绝")


@check("观测错误隔离到 track_id：重复 obs_id、时间不递增、非有限数")
def _():
    d = Path(tempfile.mkdtemp())
    info = datagen.write_demo_dataset(d)
    # 再追加几条坏轨迹到文件
    with open(Path(d) / "observations.csv", "a", encoding="utf-8") as f:
        f.write("BAD1,x1,5,1,1\nBAD1,x1,6,2,2\n")          # 重复 obs_id
        f.write("BAD2,y1,9,1,1\nBAD2,y2,9,2,2\n")          # 同 t 非严格
        f.write("BAD3,z1,1,,1\n")                          # 非有限
    data = load_all(f"{d}/nodes.csv", f"{d}/edges.csv",
                    f"{d}/observations.csv")
    assert "BAD1" in data.rejected and "BAD2" in data.rejected
    assert "BAD3" in data.rejected
    # 正常轨迹不受影响
    assert "T_NEAREST" in data.tracks and "T_SPLIT" in data.tracks


@check("文件行序不改变匹配；原始行号允许变化")
def _():
    d1 = Path(tempfile.mkdtemp())
    d2 = Path(tempfile.mkdtemp())
    datagen.write_demo_dataset(d1, shuffle=False)
    datagen.write_demo_dataset(d2, shuffle=True)
    a = load_all(d1 / "nodes.csv", d1 / "edges.csv", d1 / "observations.csv")
    b = load_all(d2 / "nodes.csv", d2 / "edges.csv", d2 / "observations.csv")
    g1, g2 = Graph.build(a.edges), Graph.build(b.edges)
    p = Params(**datagen.PARAMS)
    for tid in a.tracks:
        pa, sa, _ = match_track(tid, a.tracks[tid], a.edges, g1, p)
        pb, sb, _ = match_track(tid, b.tracks[tid], b.edges, g2, p)
        ea = [(x.candidates[x.chosen_idx].edge_id if x.chosen_idx is not None
               else None, x.segment_id, x.state) for x in pa]
        eb = [(x.candidates[x.chosen_idx].edge_id if x.chosen_idx is not None
               else None, x.segment_id, x.state) for x in pb]
        assert ea == eb, tid
        assert [z.total_score for z in sa] == [z.total_score for z in sb]


# ------------------------------------------------------------ 持久化与参数

@check("DuckDB 入库、导出、关闭后重新打开可查全部依据")
def _():
    d, info, data = _demo_loaded()
    db = Path(d) / "mm.duckdb"
    summary = pipeline.run(f"{d}/nodes.csv", f"{d}/edges.csv",
                           f"{d}/observations.csv", str(db), **info["params"])
    assert summary.points_matched > 0 and summary.tracks_rejected == 1
    out = Path(d) / "export"
    n1 = pipeline.export_matches(str(db), str(out / "m.csv"))
    n2 = pipeline.export_segments(str(db), str(out / "s.csv"))
    assert n1 == summary.observations_total and n2 == summary.segments_total
    # 关闭后只读重开
    con = connect_readonly(str(db))
    n_cand = con.execute(
        "SELECT COUNT(*) FROM candidates WHERE track_id='T_NEAREST' "
        "AND obs_id='o2'").fetchone()[0]
    assert n_cand == 3                       # 该点实际保留的全部候选
    # T_SPLIT 选中 H 路后中间最短路为零长（空串）；选边在 matches 表核对
    n_mid = con.execute(
        "SELECT middle_edges FROM transitions "
        "WHERE track_id='T_SPLIT'").fetchone()[0]
    assert n_mid == ""
    chosen = con.execute(
        "SELECT obs_id, edge_id FROM matches WHERE track_id='T_SPLIT' "
        "ORDER BY obs_id").fetchall()
    assert chosen == [("u1", "E_H1"), ("u2", "E_H2")]
    # 回环倒退的中间道路非空且完整持久化
    loop_mid = con.execute(
        "SELECT middle_edges FROM transitions "
        "WHERE track_id='T_BACK_LOOP'").fetchone()[0]
    assert loop_mid == "E_R1,E_R2,E_R3"
    con.close()
    txt = pipeline.explain_text(str(db), "T_NEAREST", "o2")
    assert "E_J" in txt and "候选名次" in txt and "转移代价" in txt
    ttxt = pipeline.track_text(str(db), "T_BAD")
    assert "整条拒收" in ttxt


@check("参数必须为正数，K 必须为正整数")
def _():
    for kw in [dict(radius=0, k=2, sigma=1, beta=1, vmax=1, max_gap=1),
               dict(radius=1, k=0, sigma=1, beta=1, vmax=1, max_gap=1),
               dict(radius=1, k=1.5, sigma=1, beta=1, vmax=1, max_gap=1),
               dict(radius=1, k=1, sigma=-1, beta=1, vmax=1, max_gap=1),
               dict(radius=1, k=1, sigma=1, beta=float("nan"),
                    vmax=1, max_gap=1)]:
        try:
            pipeline.validate_params(**kw)
        except ValueError:
            continue
        raise AssertionError(f"非法参数未拒绝: {kw}")


# ------------------------------------------------------------ 长轨迹证据

@check("长轨迹：生产 DP 不枚举组合，状态规模有界且结果满足速度约束")
def _():
    d = Path(tempfile.mkdtemp())
    info = biggen.write_big_dataset(d, n_nodes_side=8, n_tracks=2,
                                    n_obs=120, seed=3)
    db = str(d / "big.duckdb")
    summary = pipeline.run(str(d / "data" / "nodes.csv"),
                           str(d / "data" / "edges.csv"),
                           str(d / "data" / "observations.csv"),
                           db, **info["params"])
    # 暴力组合数 4^120 ≈ 1.4e72，而 DP 每层至多 K=6 个状态
    assert info["naive_combos"] == 4 ** 120
    con = connect_readonly(db)
    per_layer = con.execute(
        "SELECT MAX(c) FROM (SELECT track_id, obs_id, COUNT(*) c "
        "FROM candidates GROUP BY 1,2)").fetchone()[0]
    assert per_layer <= info["params"]["k"]
    bad = con.execute(
        "SELECT COUNT(*) FROM transitions t JOIN observations a "
        "ON t.track_id=a.track_id AND t.from_obs=a.obs_id "
        "JOIN observations b ON t.track_id=b.track_id AND t.to_obs=b.obs_id "
        "WHERE t.route_distance > ? * (b.t-a.t) + 1e-9",
        [info["params"]["vmax"]]).fetchone()[0]
    con.close()
    assert bad == 0 and summary.points_matched > 0


def main() -> int:
    print("== bitemp.mm 验收测试 ==（@check 在定义时即执行）")
    print(f"\n通过 {len(PASSED)} 项：")
    for n in PASSED:
        print(f"  - {n}")
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
