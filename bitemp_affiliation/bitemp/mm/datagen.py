"""演示材料生成器：小而可手算的道路网与轨迹。

坐标单位为米。道路网（详见 README/demo 输出）：
  主干 E_A：N0(0,0) -> N1(100,0)，反向必须走 E_R1..E_R3 的有向环；
  平行单行线 E_P：N10(0,-30) -> N11(100,-30)（同方向、无反向边）；
  支路 E_J(100,0)->(120,0) 为死路；E_ISO(120,-30)->(140,-30) 为孤立单行线；
  回环 E_R1..E_R3：N1 绕矩形回到 N0，80+100+80=260；
  等长双路（菱形）：N2(300,0) 与 N3(450,0) 之间有
    E_H1+E_H2（经 X(375,100)）与 E_L1+E_L2（经 Y(375,-100)）两套路线，
    每条边长 125，两套总长都是 250。

统一参数：radius=25 K=4 sigma=5 beta=10 vmax=30 max_gap=15。
"""
from __future__ import annotations

import csv
from pathlib import Path

NODES = [
    ("N0", 0.0, 0.0),
    ("N1", 100.0, 0.0),
    ("N10", 0.0, -30.0),
    ("N11", 100.0, -30.0),
    ("NJ1", 120.0, 0.0),
    ("NI1", 120.0, -30.0),
    ("NI2", 140.0, -30.0),
    ("NR1", 100.0, 80.0),
    ("NR2", 0.0, 80.0),
    ("N2", 300.0, 0.0),
    ("N3", 450.0, 0.0),
    ("NX", 375.0, 100.0),
    ("NY", 375.0, -100.0),
]

EDGES = [
    ("E_A", "N0", "N1"),          # 100
    ("E_P", "N10", "N11"),        # 100，平行单行线
    ("E_J", "N1", "NJ1"),         # 20，死路
    ("E_ISO", "NI1", "NI2"),      # 20，孤立单行线
    ("E_R1", "N1", "NR1"),        # 80
    ("E_R2", "NR1", "NR2"),       # 100
    ("E_R3", "NR2", "N0"),        # 80；回环合计 260
    ("E_H1", "N2", "NX"),         # 125
    ("E_H2", "NX", "N3"),         # 125；上路合计 250
    ("E_L1", "N2", "NY"),         # 125
    ("E_L2", "NY", "N3"),         # 125；下路合计 250
]

# (track_id, obs_id, t, x, y)；datagen 可再生成一份乱序版
OBSERVATIONS = [
    # T_NEAREST：o2 最近边是死路 E_J（距离 8），只吸附最近边会在 o3 处
    # 无法连接而切段；全局最优在 o2 选第三近的 E_A，保持一个完整片段
    ("T_NEAREST", "o1", 0.0, 10.0, 3.0),
    ("T_NEAREST", "o2", 10.0, 112.0, 8.0),
    ("T_NEAREST", "o3", 20.0, 15.0, -2.0),
    # T_PARALLEL：平行单行线，等分代价下按 edge_id 列表字典序选 E_A
    ("T_PARALLEL", "p1", 0.0, 10.0, -15.0),
    ("T_PARALLEL", "p2", 10.0, 40.0, -15.0),
    # T_BACK_LOOP：同边倒退，靠回环绕行，路程 300 在 vmax*10=300 内
    ("T_BACK_LOOP", "b1", 0.0, 80.0, 0.0),
    ("T_BACK_LOOP", "b2", 10.0, 20.0, 0.0),
    # T_BACK_NOLOOP：平行路上同边倒退但无环可回 -> no_feasible，两个单点段
    ("T_BACK_NOLOOP", "q1", 0.0, 80.0, -30.0),
    ("T_BACK_NOLOOP", "q2", 10.0, 20.0, -30.0),
    # T_SPEED：路程恰为 300=vmax*dt，等于上限允许通过
    ("T_SPEED", "s1", 0.0, 80.0, 0.0),
    ("T_SPEED", "s2", 10.0, 20.0, 0.0),
    # T_GAP：间隔 20 > max_gap=15，gap_timeout 切段
    ("T_GAP", "g1", 0.0, 10.0, 0.0),
    ("T_GAP", "g2", 20.0, 40.0, 0.0),
    # T_HOLE：中间点无候选 -> unmatched 断开，之后恢复，两个片段
    ("T_HOLE", "h1", 0.0, 10.0, 0.0),
    ("T_HOLE", "h2", 10.0, 60.0, 200.0),
    ("T_HOLE", "h3", 20.0, 40.0, 0.0),
    # T_GAP_NO_CAND：同一点同时超时且无候选
    ("T_GAP_NO_CAND", "z1", 0.0, 10.0, 0.0),
    ("T_GAP_NO_CAND", "z2", 20.0, 60.0, 200.0),
    ("T_GAP_NO_CAND", "z3", 25.0, 40.0, 0.0),
    # T_SPLIT：两套等长路线，按中间 edge_id 序列字典序选 E_H1,E_H2
    ("T_SPLIT", "u1", 0.0, 305.0, 0.0),
    ("T_SPLIT", "u2", 10.0, 445.0, 0.0),
    # T_BAD：含重复 obs_id 与非有限坐标，整条拒收（不拖垮其他轨迹）
    ("T_BAD", "d1", 0.0, 10.0, 0.0),
    ("T_BAD", "d2", 10.0, 20.0, 0.0),
    ("T_BAD", "d2", 20.0, 30.0, 0.0),
    ("T_BAD", "d3", 20.0, float("nan"), 0.0),
]

PARAMS = dict(radius=25.0, k=4, sigma=5.0, beta=10.0,
              vmax=30.0, max_gap=15.0)


def write_demo_dataset(out_dir: str | Path, shuffle: bool = False) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "nodes.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["node_id", "x", "y"])
        w.writerows(NODES)
    with open(out / "edges.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["edge_id", "from_node", "to_node"])
        w.writerows(EDGES)
    rows = list(OBSERVATIONS)
    if shuffle:
        # 确定性乱序：整体逆序后奇偶交错，保证不按 t、也不按轨迹排列
        rows = rows[::-1]
        rows = rows[::2] + rows[1::2]
    with open(out / "observations.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["track_id", "obs_id", "t", "x", "y"])
        for r in rows:
            w.writerow(r)
    return {"dir": str(out), "params": PARAMS, "shuffled": shuffle}
