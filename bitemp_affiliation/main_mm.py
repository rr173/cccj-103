#!/usr/bin/env python3
"""离线地图匹配入口（与仓库已有功能共存的独立子工具）。

用法：
  python3 main_mm.py run --nodes nodes.csv --edges edges.csv \
                         --observations observations.csv --db mm.duckdb \
                         --radius 25 --k 4 --sigma 5 --beta 10 \
                         --vmax 30 --max-gap 15
  python3 main_mm.py export --db mm.duckdb --out-dir ./out
  python3 main_mm.py track  --db mm.duckdb --track T_NEAREST
  python3 main_mm.py explain --db mm.duckdb --track T_NEAREST --obs o2
  python3 main_mm.py demo [--work-dir D]      # 可手算小图 + 独立参考核对
  python3 main_mm.py bigtest [--work-dir D]   # 长轨迹：DP 不枚举组合的证据

全程离线：只读取本地 CSV 与本地 DuckDB，不调用任何地图服务或在线接口。
"""
from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# -I 子进程不读 PYTHONPATH 环境变量之外的注入方式；把本进程实际可用的
# site-package 路径（如本地解包的 duckdb）显式传给子进程。
_CHILD_PYTHONPATH = os.pathsep.join(
    p for p in sys.path if p and Path(p).is_dir())

from bitemp.mm import biggen, datagen, pipeline, verify  # noqa: E402
from bitemp.mm.loader import RoadConfigError, load_all  # noqa: E402
from bitemp.mm.matcher import Params  # noqa: E402


def _params_from_args(a) -> dict:
    return dict(radius=a.radius, k=a.k, sigma=a.sigma, beta=a.beta,
                vmax=a.vmax, max_gap=a.max_gap)


def cmd_run(args) -> int:
    try:
        summary = pipeline.run(
            args.nodes, args.edges, args.observations, args.db,
            **_params_from_args(args))
    except RoadConfigError as e:
        print("道路配置不合法，拒绝运行：", file=sys.stderr)
        for m in e.messages:
            print(f"  - {m}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"参数错误: {e}", file=sys.stderr)
        return 2
    print("== 装载与匹配汇总 ==")
    print(f"  轨迹 {summary.tracks_total} 条：正常处理 {summary.tracks_matched}，"
          f"整条拒收 {summary.tracks_rejected}")
    print(f"  观测点 {summary.observations_total} 个：匹配 "
          f"{summary.points_matched}，unmatched {summary.points_unmatched}；"
          f"片段 {summary.segments_total} 个")
    if summary.rejected:
        print("  拒收轨迹原因：")
        for tid, reasons in sorted(summary.rejected.items()):
            for r in reasons:
                print(f"    [{tid}] {r}")
    print(f"  结果库: {summary.db_path}")
    return 0


def cmd_export(args) -> int:
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    m = out / "matches.csv"
    s = out / "segments.csv"
    n1 = pipeline.export_matches(args.db, str(m))
    n2 = pipeline.export_segments(args.db, str(s))
    print(f"逐点结果 {n1} 行 -> {m}")
    print(f"片段结果 {n2} 行 -> {s}")
    return 0


def cmd_track(args) -> int:
    try:
        print(pipeline.track_text(args.db, args.track))
    except KeyError as e:
        print(str(e), file=sys.stderr)
        return 2
    return 0


def cmd_explain(args) -> int:
    try:
        print(pipeline.explain_text(args.db, args.track, args.obs))
    except KeyError as e:
        print(str(e), file=sys.stderr)
        return 2
    return 0


def _run_subprocess_db(db, nodes, edges, obs, params, work) -> str:
    out_db = str(work / f"{Path(db).stem}.duckdb")
    # 用 PYTHONSAFEPATH=1（等价 -P）防止脚本目录/cwd 被塞进 sys.path 造成
    # 同名模块注入，同时保留 PYTHONPATH 以便找到本地依赖（如 duckdb）。
    cmd = [sys.executable, str(Path(__file__).resolve()), "run",
           "--nodes", str(nodes), "--edges", str(edges),
           "--observations", str(obs),
           "--db", out_db]
    for key, val in params.items():
        cmd += [f"--{key.replace('_', '-')}", str(val)]
    env = dict(os.environ)
    env["PYTHONPATH"] = _CHILD_PYTHONPATH
    env["PYTHONSAFEPATH"] = "1"
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        raise SystemExit(f"子进程运行失败: {' '.join(cmd)}")
    return out_db


def cmd_demo(args) -> int:
    work = Path(args.work_dir or tempfile.mkdtemp(prefix="mm_demo_"))
    if args.work_dir:
        work.mkdir(parents=True, exist_ok=True)
    d1 = work / "data_ordered"
    d2 = work / "data_shuffled"
    info1 = datagen.write_demo_dataset(d1, shuffle=False)
    info2 = datagen.write_demo_dataset(d2, shuffle=True)
    params = info1["params"]
    print(f"== 演示材料已生成：{d1}（另存乱序版 {d2}）==")

    db1 = _run_subprocess_db("ordered", d1 / "nodes.csv", d1 / "edges.csv",
                             d1 / "observations.csv", params, work)
    db2 = _run_subprocess_db("shuffled", d2 / "nodes.csv", d2 / "edges.csv",
                             d2 / "observations.csv", params, work)

    print("\n== 逐点结果（正常顺序输入）==")
    print(pipeline.track_text(db1, "T_NEAREST"))
    print("\n-- 为什么不能只吸附最近道路：explain T_NEAREST/o2 --")
    print(pipeline.explain_text(db1, "T_NEAREST", "o2"))

    # 反例对照：逐点贪心只取最近候选，检查相邻点能否沿道路连通
    from bitemp.mm.geometry import candidates_for
    from bitemp.mm.graph import Graph
    from bitemp.mm.transitions import route_between
    data_n = load_all(d1 / "nodes.csv", d1 / "edges.csv",
                      d1 / "observations.csv")
    g_n = Graph.build(data_n.edges)
    emap_n = {e.edge_id: e for e in data_n.edges}
    p_n = Params(**params)
    greedy = [candidates_for(o.x, o.y, data_n.edges, p_n.radius,
                             p_n.k, p_n.sigma)[0]
              for o in data_n.tracks["T_NEAREST"]]
    print("\n-- 对照：若每点只吸附最近边（贪心，不做全局选择）--")
    for o, c in zip(data_n.tracks["T_NEAREST"], greedy):
        print(f"  {o.obs_id} -> {c.edge_id}（距离 {c.distance:g}）")
    for oa, ob, ca, cb in zip(data_n.tracks["T_NEAREST"],
                              data_n.tracks["T_NEAREST"][1:],
                              greedy, greedy[1:]):
        r = route_between(ca, cb, g_n, emap_n)
        budget = p_n.vmax * (ob.t - oa.t)
        ok = r is not None and r.distance <= budget
        print(f"  {oa.obs_id}->{ob.obs_id} {ca.edge_id}->{cb.edge_id}: "
              + (f"连通，路程 {r.distance:g}" if ok else "无沿道路通路 -> 贪心被迫切段"))
    print("  全局 DP 则保持 1 个连续片段（见上），这就是必须保留多候选的原因")

    print("\n== 全部轨迹片段概览 ==")
    import duckdb
    con = duckdb.connect(db1, read_only=True)
    for tid, in con.execute(
            "SELECT DISTINCT track_id FROM segments ORDER BY track_id").fetchall():
        print(pipeline.track_text(db1, tid))
        print()
    con.close()

    print("== 独立暴力参考实现逐项核对（枚举候选组合 + DFS 中间路径）==")
    data = load_all(d1 / "nodes.csv", d1 / "edges.csv",
                    d1 / "observations.csv")
    p = Params(**params)
    problems = verify.check_all(data, p)
    if problems:
        for x in problems:
            print(f"  不一致: {x}")
        return 1
    print("  分数、道路选择、切段、事件与参考实现全部一致（容差 1e-8）")

    print("\n== 打乱文件行序不变性核对 ==")
    pipeline.export_matches(db1, str(work / "m1.csv"))
    pipeline.export_matches(db2, str(work / "m2.csv"))
    t1 = Path(work / "m1.csv").read_text(encoding="utf-8").splitlines()
    t2 = Path(work / "m2.csv").read_text(encoding="utf-8").splitlines()
    # 去掉 source_line 列再比较（原始行号允许随输入变化）
    def drop_line(text):
        rd = csv.reader(text)
        header = next(rd)
        idx = header.index("source_line")
        return sorted(tuple(v for i, v in enumerate(r) if i != idx) for r in rd)
    if drop_line(t1) != drop_line(t2):
        print("  乱序输入改变了匹配结果！", file=sys.stderr)
        return 1
    print("  两份输入除原始行号外逐点结果完全一致")

    print("\n== 坏轨迹隔离核对 ==")
    print(pipeline.track_text(db1, "T_BAD"))
    print("  （其余轨迹照常处理，见上方片段概览）")

    print(f"\n工作目录保留在 {work}（库可用 track/explain 重新打开查询）")
    return 0


def cmd_bigtest(args) -> int:
    work = Path(args.work_dir or tempfile.mkdtemp(prefix="mm_big_"))
    work.mkdir(parents=True, exist_ok=True)
    info = biggen.write_big_dataset(
        work, n_nodes_side=args.side, n_tracks=args.tracks,
        n_obs=args.obs, seed=args.seed)
    print(f"== 网格大图：节点 {info['n_nodes']}，边 {info['n_edges']}，"
          f"观测 {info['n_obs']} ==")
    print(f"  若枚举整条轨迹候选组合，单条轨迹至少 {info['naive_combos']:.3e} "
          f"种组合（4^{args.obs}），生产 DP 每层只保留 {info['params']['k']} 个状态")
    db = work / "big.duckdb"
    t0 = time.time()
    summary = pipeline.run(work / "data" / "nodes.csv",
                           work / "data" / "edges.csv",
                           work / "data" / "observations.csv",
                           str(db), **info["params"])
    elapsed = time.time() - t0
    print(f"  生产 DP 运行耗时 {elapsed:.2f}s：匹配 {summary.points_matched} 点，"
          f"unmatched {summary.points_unmatched}，片段 {summary.segments_total}")
    # 状态规模证据：实际候选总数
    import duckdb
    con = duckdb.connect(str(db), read_only=True)
    cand_total = con.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    state_total = con.execute(
        "SELECT COUNT(*) FROM candidates c JOIN matches m "
        "ON c.track_id=m.track_id AND c.obs_id=m.obs_id "
        "WHERE m.state='matched'").fetchone()[0]
    print(f"  库内候选（=DP 状态规模）共 {cand_total} 条；"
          f"暴力枚举需要 ~{info['naive_combos']:.2e} 组合/轨迹")
    # 抽查：每个转移路程不超速度预算
    bad = con.execute(
        "SELECT COUNT(*) FROM transitions t JOIN observations a "
        "ON t.track_id=a.track_id AND t.from_obs=a.obs_id "
        "JOIN observations b ON t.track_id=b.track_id AND t.to_obs=b.obs_id "
        "WHERE t.route_distance > ? * (b.t-a.t) + 1e-9",
        [info["params"]["vmax"]]).fetchone()[0]
    con.close()
    if bad:
        print(f"  存在 {bad} 条超速转移！", file=sys.stderr)
        return 1
    print("  抽查：所有选中转移均满足 路程 <= vmax*dt")
    print(f"  结果库 {db}（关闭进程后可重新打开 track/explain）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="装载三个 CSV 并执行离线地图匹配")
    p.add_argument("--nodes", required=True)
    p.add_argument("--edges", required=True)
    p.add_argument("--observations", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--radius", type=float, required=True, help="搜索半径（米）")
    p.add_argument("--k", type=int, required=True, help="每点最多候选数（正整数）")
    p.add_argument("--sigma", type=float, required=True, help="定位尺度（米）")
    p.add_argument("--beta", type=float, required=True, help="路程尺度（米）")
    p.add_argument("--vmax", type=float, required=True, help="最大速度（米/秒）")
    p.add_argument("--max-gap", type=float, required=True,
                   help="最长连续采样间隔（秒）")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("export", help="导出逐点与片段 CSV")
    p.add_argument("--db", required=True)
    p.add_argument("--out-dir", required=True)
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("track", help="查询一条轨迹的匹配全貌")
    p.add_argument("--db", required=True)
    p.add_argument("--track", required=True)
    p.set_defaults(fn=cmd_track)

    p = sub.add_parser("explain", help="查询某个观测的候选与选中转移依据")
    p.add_argument("--db", required=True)
    p.add_argument("--track", required=True)
    p.add_argument("--obs", required=True)
    p.set_defaults(fn=cmd_explain)

    p = sub.add_parser("demo", help="可手算小图 + 暴力参考核对 + 乱序不变性")
    p.add_argument("--work-dir", default=None)
    p.set_defaults(fn=cmd_demo)

    p = sub.add_parser("bigtest", help="长轨迹大图：DP 不枚举组合的运行证据")
    p.add_argument("--work-dir", default=None)
    p.add_argument("--side", type=int, default=12)
    p.add_argument("--tracks", type=int, default=4)
    p.add_argument("--obs", type=int, default=300)
    p.add_argument("--seed", type=int, default=11)
    p.set_defaults(fn=cmd_bigtest)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
