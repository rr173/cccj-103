"""长轨迹与网格大图生成器：给出生产算法不枚举组合的实际运行证据。"""
from __future__ import annotations

import csv
import math
import random
from pathlib import Path


def write_big_dataset(out_dir: str | Path, n_nodes_side: int = 12,
                      n_tracks: int = 4, n_obs: int = 300,
                      seed: int = 11) -> dict:
    """生成有向方格网（每格两条显式反向边）与多条长轨迹。

    节点 G_r_c 位于 (100c, 100r)；横向边 H_r_c、纵向边 V_r_c 均成对
    给出正反两条，满足"反方向通行必须显式交反向边"。
    """
    out = Path(out_dir)
    data_dir = out / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    nodes, edges = [], []
    for r in range(n_nodes_side):
        for c in range(n_nodes_side):
            nodes.append((f"G_{r}_{c}", float(c * 100), float(r * 100)))
    for r in range(n_nodes_side):
        for c in range(n_nodes_side - 1):
            edges.append((f"H_{r}_{c}", f"G_{r}_{c}", f"G_{r}_{c+1}"))
            edges.append((f"HB_{r}_{c}", f"G_{r}_{c+1}", f"G_{r}_{c}"))
    for r in range(n_nodes_side - 1):
        for c in range(n_nodes_side):
            edges.append((f"V_{r}_{c}", f"G_{r}_{c}", f"G_{r+1}_{c}"))
            edges.append((f"VB_{r}_{c}", f"G_{r+1}_{c}", f"G_{r}_{c}"))

    obs_rows = []
    combos = 1
    for tk in range(n_tracks):
        r = rng.randrange(n_nodes_side)
        c = rng.randrange(n_nodes_side)
        t = 0.0
        track_combos = 1
        for i in range(n_obs):
            # 随机游走一格，卡在边界就反向；观测在真实位置上加噪声
            moves = [(-1, 0), (1, 0), (0, -1), (0, 1)]
            dr, dc = rng.choice(moves)
            if not (0 <= r + dr < n_nodes_side and 0 <= c + dc < n_nodes_side):
                dr, dc = -dr, -dc
            r, c = r + dr, c + dc
            t += rng.uniform(8.0, 12.0)
            x = c * 100 + rng.uniform(-12.0, 12.0)
            y = r * 100 + rng.uniform(-12.0, 12.0)
            obs_rows.append((f"LONG{tk}", f"o{i:04d}", round(t, 4),
                             round(x, 4), round(y, 4)))
            # 网格内点周围通常有 4 条候选边，组合数按 4^n 增长
            track_combos *= 4
        combos *= track_combos

    with open(data_dir / "nodes.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["node_id", "x", "y"])
        w.writerows(nodes)
    with open(data_dir / "edges.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["edge_id", "from_node", "to_node"])
        w.writerows(edges)
    with open(data_dir / "observations.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["track_id", "obs_id", "t", "x", "y"])
        w.writerows(obs_rows)
    return {"dir": str(out), "n_nodes": len(nodes), "n_edges": len(edges),
            "n_obs": len(obs_rows),
            "naive_combos": 4 ** n_obs,
            "params": dict(radius=20.0, k=6, sigma=8.0, beta=12.0,
                           vmax=20.0, max_gap=30.0)}
