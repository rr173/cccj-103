#!/usr/bin/env python3
"""分摊管线演示：装载 -> 试算 -> 正式计算 -> 月度合计 -> 解释，含规则拒收与旧版继续。

    python3 main_alloc.py demo  [--db :memory:]
    python3 main_alloc.py crystal [--db :memory:]

crystal 子命令跑一组金标准（人工可手算），打印每个情形的精确权重与整数金额。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bitemp import builder, schema  # noqa: E402
from bitemp.alloc import AllocationService, RuleRejected  # noqa: E402
from bitemp.alloc import goldens  # noqa: E402

UTC = timezone.utc
ASOF = datetime(2025, 6, 30, 23, 59, 59, tzinfo=UTC)


def _edge(key, src, dst, begin="2024-01-01", until=None, num=1, den=1):
    return dict(edge_key=key, src=src, dst=dst, begin=begin, until=until,
                numerator=num, denominator=den)


def seed_affiliation(con, person="P1", orgs=("ROOT", "2024-01-01")):
    """装入一条或两条归属断言：默认 P1 从 2024-01-01 起属于 ROOT。"""
    rows = [dict(assertion_id="DEMO-A1", researcher_id=person,
                 valid_from="2023-01-01", valid_to=None, organization=orgs[0],
                 recorded_at="2023-01-01T00:00:00+00:00", revision=1,
                 supersedes=None)]
    if len(orgs) > 2:
        rows.append(dict(assertion_id="DEMO-A2", researcher_id=person,
                         valid_from=orgs[1], valid_to=None, organization=orgs[2],
                         recorded_at="2023-06-01T00:00:00+00:00", revision=2,
                         supersedes=None))
    builder.process_batch(con, rows)


def cmd_crystal(args):
    print("== 金标准（人工可手算）==\n")
    for title, make in goldens.GOLD_CASES:
        case = make()
        print(f"— {title} —")
        if "expect_rejected" in case:
            con = schema.connect(":memory:")
            svc = AllocationService(con)
            try:
                svc.load_rules(case["edges"])
                print("  (未拒收，不符合预期)")
            except RuleRejected as e:
                for issue in e.issues:
                    print(f"  拒收：{issue.render()}")
            print()
            continue
        con, svc, keys = goldens.setup_db(case)
        rid = svc.compute(ASOF)
        items = con.execute(
            "SELECT item_key, source_org, CAST(amount AS HUGEINT) FROM alloc_run_item "
            "WHERE run_id = ? ORDER BY item_key", [rid]).fetchall()
        for item, org, amt in items:
            leaves = con.execute(
                "SELECT terminal, CAST(amount AS HUGEINT), weight_num, weight_den "
                "FROM alloc_run_leaf WHERE run_id = ? AND item_key = ? ORDER BY terminal",
                [rid, item]).fetchall()
            exp = case.get("expect", {})
            want = exp.get(item) if isinstance(exp.get(item), dict) else exp
            detail = ", ".join(
                f"{t}={a} (权重 {n}/{d})" for t, a, n, d in leaves)
            print(f"  {item}: 起点={org} 金额={amt} -> {detail}")
            if isinstance(want, dict):
                got = {t: a for t, a, _n, _d in leaves}
                want_nz = {k: v for k, v in want.items() if v != 0}
                assert {k: v for k, v in got.items() if v != 0} == want_nz, (item, got, want)
                assert sum(got.values()) == amt
        print()
    print("所有金标准金额均与手算一致，且逐笔守恒。")
    return 0


def cmd_demo(args):
    con = schema.connect(args.db)
    svc = AllocationService(con)
    seed_affiliation(con)

    # ---- 1) 装载一份可用规则
    good_edges = [
        _edge("D1", "ROOT", "CENTRAL", num=1, den=2),
        _edge("D2", "ROOT", "CAMPUS-N", num=1, den=2),
        _edge("C1", "CENTRAL", "JOINT", num=2, den=3),
        _edge("C2", "CENTRAL", "DEPT-A", num=1, den=3),
        _edge("C3", "CAMPUS-N", "DEPT-B", num=1, den=1),
    ]
    rep = svc.load_rules(good_edges, note="demo v1")
    print(f"== 装载规则 ==\n  version={rep.version_id} digest={rep.digest[:16]}… "
          f"边数={rep.edge_count} 复用={rep.reused}")

    svc.load_items([
        dict(item_key="IT-001", person_key="P1", day="2024-02-10", amount=10000),
        dict(item_key="IT-002", person_key="P1", day="2024-02-20", amount=101),
        dict(item_key="IT-003", person_key="P1", day="2024-03-05", amount=-505),
        dict(item_key="IT-004", person_key="P1", day="2024-03-18", amount=100),
        dict(item_key="IT-909", person_key="GHOST", day="2024-03-01", amount=777),
    ])
    print("  装载事项 5 条（含 1 条无起点）")

    # ---- 2) 试算（不落库）
    trial = svc.trial(ASOF)
    print(f"\n== 试算（不落库）==\n  事项={trial.stats['items']} "
          f"解析={trial.stats['resolved']} unresolved={trial.stats['unresolved']} "
          f"节点访问={trial.stats['node_ops']} 边访问={trial.stats['edge_ops']}")
    for a in trial.items:
        if a.resolved:
            units = ", ".join(f"{t.terminal}:{t.amount}" for t in a.terminals)
            print(f"  {a.item.item_key} 金额={a.item.amount} -> {units} 合计={a.total()}")
        else:
            print(f"  {a.item.item_key} 金额={a.item.amount} -> UNRESOLVED")
    assert all(a.total() == a.item.amount for a in trial.resolved_items)

    # ---- 3) 正式计算
    run = svc.compute(ASOF)
    print(f"\n== 正式计算 ==\n  run_id={run}")

    # ---- 4) 月度 × 末级合计 + unresolved
    print("\n== 月度 × 末级单元合计 ==")
    for month, terminal, total, n in svc.monthly_rollup(run):
        print(f"  {month}  {terminal:<10} {total:>8}  ({n} 笔)")
    u = svc.unresolved(run)
    print(f"  unresolved: {u['count']} 笔，金额合计 {u['amount']}")
    for item, person, day, amt, reason in u["items"]:
        print(f"    {item} person={person} day={day} amount={amt} ({reason})")

    # ---- 5) 解释多路汇合
    print("\n== 解释 IT-001（多级分叉 + 无汇合，看末端份额）==")
    print(svc.explain_text(run, "IT-001", "JOINT"))

    # ---- 6) 拒收一份坏规则，旧版继续可用
    print("\n== 规则拒收（旧版保留）==")
    bad_edges = [
        _edge("BAD1", "ROOT", "X", num=1, den=2),
        _edge("BAD2", "ROOT", "Y", num=1, den=4),          # ROOT 出边和 = 3/4 ≠ 1
        _edge("BAD3", "X", "Y", begin="2024-01-01", until="2024-05-01", num=1, den=1),
        _edge("BAD4", "Y", "X", begin="2024-05-01", num=1, den=1),  # 仅跨期，假环
    ]
    try:
        svc.load_rules(bad_edges, note="demo bad")
        print("  (未拒收，不符合预期)")
    except RuleRejected as e:
        for issue in e.issues:
            print(f"  {issue.render()}")
    versions = con.execute("SELECT COUNT(*) FROM alloc_rule_version").fetchone()[0]
    print(f"  版本表仍为 {versions} 个版本；用旧版继续计算：")
    run2 = svc.compute(ASOF, version_id=rep.version_id)
    leaves2 = con.execute(
        "SELECT item_key, terminal, CAST(amount AS HUGEINT) FROM alloc_run_leaf "
        "WHERE run_id = ? ORDER BY item_key, terminal", [run2]).fetchall()
    print(f"  旧版 run={run2} 末级合计 {sum(r[2] for r in leaves2)} "
          f"(应等于已解析金额之和)")

    # ---- 7) 从磁盘重开解释一致（若给了文件库）
    if args.db != ":memory:":
        exp1 = svc.explain(run, "IT-001", "JOINT")
        con.close()
        con2 = schema.connect(args.db)
        svc2 = AllocationService(con2)
        exp2 = svc2.explain(run, "IT-001", "JOINT")
        assert exp1["explanation"]["terminal_weight"] == exp2["explanation"]["terminal_weight"]
        print(f"\n== 磁盘重开 ==\n  解释一致，JOINT 权重="
              f"{exp2['explanation']['terminal_weight']}，"
              f"与落库一致={exp2['weight_matches_store']}")
        con2.close()
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("demo", "crystal"):
        p = sub.add_parser(name)
        p.add_argument("--db", default=":memory:")
    args = ap.parse_args()
    return cmd_demo(args) if args.cmd == "demo" else cmd_crystal(args)


if __name__ == "__main__":
    raise SystemExit(main())
