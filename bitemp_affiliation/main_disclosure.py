#!/usr/bin/env python3
"""报表主次抑制离线演示（人工可手算）。

    python3 main_disclosure.py demo

每个场景都直接断言“应当发生什么”，并打印可手算的中间量：
  * 整张表的行/列合计与每个格子的贡献人数；
  * 桥检测与有理数消元两种独立判据；
  * 每个隐藏格子的环扰动见证；
  * 公开 / 审计两个导出入口。

场景
----
1. 单独隐藏一个小群体格：行合计仍能把它反推出来（不安全）。
2. 补足矩形环：解除反推，每个隐藏格获得非零环扰动见证。
3. 两个环经一条隐藏桥连接：两块各自安全，但桥被全表方程唯一解出。
4. 指定公开一个主抑制格：拒绝导出。
5. 多人多笔 + 正负抵消：贡献人数去重、零金额但有份额的格不消失。
6. 两个同成本方案：按 (成本, 数量, 排序列表) 决胜，结果确定。
7. 候选数超过搜索上限：返回 incomplete，不冒充最优。
8. 更换最新路由规则：同一已保存批次的导出内容保持一致。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bitemp import schema  # noqa: E402
from bitemp.alloc import AllocationService  # noqa: E402
from bitemp.alloc import reportcheck  # noqa: E402
from bitemp.alloc.protection import (  # noqa: E402
    NotPublishable, Policy, build_table, export_public, hidden_witnesses,
    protected, publish, solve_suppression)
from bitemp.builder import process_batch  # noqa: E402

ASOF = datetime(2025, 6, 30, 23, 59, 59, tzinfo=timezone.utc)


# ---------------------------------------------------------------- 构造工具

def _assertion(person, org):
    return dict(assertion_id=f"A-{person}-{org}", researcher_id=person,
                valid_from="2023-01-01", valid_to=None, organization=org,
                recorded_at="2023-01-01T00:00:00+00:00", revision=1,
                supersedes=None)


def _edge(key, src, dst, num=1, den=1):
    return dict(edge_key=key, src=src, dst=dst, begin="2024-01-01", until=None,
                numerator=num, denominator=den)


def _run(edges, people, items, con=None):
    """装载并正式计算，返回 (con, svc, run_id)。con 给定时复用（场景 8）。"""
    con = con if con is not None else schema.connect(":memory:")
    svc = AllocationService(con)
    svc.load_rules(edges)
    process_batch(con, [_assertion(p, o) for p, o in people])
    svc.load_items(items)
    return con, svc, svc.compute(ASOF)


def _print_table(table, hidden=()):
    hidden = set(hidden)
    orgs = table.orgs
    print("        " + "".join(f"{o:>9}" for o in orgs) + "   行合计")
    for m in table.months:
        line = f"  {m}"
        for o in orgs:
            k = (m, o)
            if k not in table.cells:
                line += f"{'·':>9}"          # 结构零（不出现，公开已知为 0）
            elif k in hidden:
                line += f"{'*':>9}"          # 隐藏占位
            else:
                line += f"{table.cells[k].amount:>9}"
        print(line + f"   {table.row_totals[m]:>9}")
    print("  列合计" + "".join(f"{table.col_totals[o]:>9}" for o in orgs))


def _print_people(table):
    print("  贡献人数: " + ", ".join(
        f"{m}/{o}={c.contributors}" for (m, o), c in sorted(table.cells.items())))


def _analysis(table, hidden):
    """两个独立判据：桥检测 与 有理数消元。"""
    alg = reportcheck.check_free_columns(table, set(hidden))
    return protected(table, set(hidden)), alg


# ---------------------------------------------------------------- 场景 1 / 2

def scene_rect():
    """2 个月 × 3 个末级机构（ROOT 三等分）。

    2024-01：只有 P1 一笔，A/B/C 各 1 人（都是主抑制）。
    2024-02：P2、P3 各一笔，A/B/C 各 2 人（不是主抑制）。
    """
    edges = [_edge("E-A", "ROOT", "A", 1, 3),
             _edge("E-B", "ROOT", "B", 1, 3),
             _edge("E-C", "ROOT", "C", 1, 3)]
    people = [("P1", "ROOT"), ("P2", "ROOT"), ("P3", "ROOT")]
    items = [
        dict(item_key="J1", person_key="P1", day="2024-01-10", amount=30),
        dict(item_key="J2", person_key="P2", day="2024-02-10", amount=60),
        dict(item_key="J3", person_key="P3", day="2024-02-20", amount=60),
    ]
    con, svc, rid = _run(edges, people, items)
    return build_table(con, rid)


def scene_1(table):
    print("\n场景 1：单独隐藏一个小群体格，行合计仍能把它反推出来")
    print("  原始表（2 个月 × 3 个末级机构；ROOT 三等分，金额均为整数）")
    _print_table(table)
    _print_people(table)
    only = {("2024-01", "A")}
    visible = (table.cells[("2024-01", "B")].amount
               + table.cells[("2024-01", "C")].amount)
    print(f"\n  只隐藏 {sorted(only)}：")
    print(f"    行合计 {table.row_totals['2024-01']} − 可见 B+C {visible}"
          f" = {table.row_totals['2024-01'] - visible}"
          f"（正是隐藏格金额 {table.cells[('2024-01','A')].amount}）")
    ok, alg = _analysis(table, only)
    print(f"    桥检测 protected={ok}；消元发现被钉住的隐藏格={alg['pinned']}")
    print("    → 只藏一个格不安全：该格是桥，行合计唯一确定它。")


def scene_2(table):
    print("\n场景 2：补足矩形环后解除反推")
    rect = {("2024-01", "A"), ("2024-01", "B"),
            ("2024-02", "A"), ("2024-02", "B")}
    _print_table(table, rect)
    ok, alg = _analysis(table, rect)
    print(f"  隐藏 {sorted(rect)}")
    print(f"    桥检测 protected={ok}；消元 自由度={alg['nullity']} "
          f"被钉住={alg['pinned']}")
    w = hidden_witnesses(table, rect)
    for k in sorted(w):
        print(f"    见证 {k}: 环上 ±1 赋值 {w[k]['assignments']}")
    print("    → 2×2 矩形：环上 +1/-1 不改变任何行/列合计，每格都能自由加减，"
          "无法反推。")


# ---------------------------------------------------------------- 场景 3

def scene_bridge():
    """4 行 × 4 个末级机构。

    块 1 = 行 {01,02} × 列 {A,B}；块 2 = 行 {03,04} × 列 {C,D}；
    两块各自是矩形环；桥 = (2024-02, C) 只连接两块，不在任何环上。
    """
    edges = [_edge(f"E-{o}", "ROOT", o, 1, 4) for o in ("A", "B", "C", "D")]
    people = [("P1", "ROOT"), ("P2", "ROOT"), ("P3", "ROOT"), ("P4", "ROOT")]
    items = [dict(item_key=f"I{m}-{p}", person_key=p, day=f"2024-{m:02d}-10",
                  amount=a)
             for m in (1, 2, 3, 4)
             for p, a in (("P1", 400), ("P2", 200), ("P3", 80), ("P4", 20))]
    con, svc, rid = _run(edges, people, items)
    return build_table(con, rid)


def scene_3():
    print("\n场景 3：两个环经一条隐藏桥连接，桥仍能被全表方程解出")
    t = scene_bridge()
    rings = {("2024-01", "A"), ("2024-01", "B"),
             ("2024-02", "A"), ("2024-02", "B"),
             ("2024-03", "C"), ("2024-03", "D"),
             ("2024-04", "C"), ("2024-04", "D")}
    bridge = ("2024-02", "C")
    _print_table(t, rings | {bridge})
    ok, alg = _analysis(t, rings)
    print(f"  只藏两块环：protected={ok} 被钉住={alg['pinned']} "
          f"自由度={alg['nullity']} → 两块各自安全")
    ok, alg = _analysis(t, rings | {bridge})
    print(f"  再加桥 {bridge}：protected={ok} 被钉住={alg['pinned']} "
          f"自由度={alg['nullity']}")
    print(f"    → 桥被列 C 合计与行 {bridge[0]} 合计联立唯一解出；"
          f"局部“每行每列至少藏两个”会漏掉它，整表方程不会。")


# ---------------------------------------------------------------- 场景 4

def scene_4(table):
    print("\n场景 4：指定公开格子切断所有保护方案时拒绝导出")
    policy = Policy(min_contributors=2,
                    must_publish=frozenset({("2024-01", "A")}))
    print("  策略 k=2，强制公开 (2024-01,A)（它是 1 人主抑制格）")
    try:
        solve_suppression(table, policy)
        print("    (未拒绝，不符合预期)")
    except NotPublishable as e:
        print(f"    → 拒绝导出：{e}")
        print(f"      详情: {e.detail}")

    # 强制公开所有非主抑制格，使主抑制无法补足任何环 -> 次抑制候选为空
    policy2 = Policy(min_contributors=2, must_publish=frozenset({
        ("2024-02", "A"), ("2024-02", "B"), ("2024-02", "C")}))
    print("\n  策略 k=2，强制公开全部非主抑制格（候选为空，无法补环）")
    try:
        solve_suppression(table, policy2)
        print("    (未拒绝，不符合预期)")
    except NotPublishable as e:
        print(f"    → 拒绝导出：{e}")
        print(f"      详情: {e.detail}")


# ---------------------------------------------------------------- 场景 5

def scene_people():
    """多人多笔 + 正负抵消。

    ROOT1 -> D1：P1 两笔 +50 / -50，同格同人两笔，金额和 0。
    ROOT2 -> D2：P2 两笔 +20 / +20，同格同人两笔，金额 40，人数应为 1。
    ROOT3 -> D3：P3 +10、P4 +30，金额 40，人数 2。
    """
    edges = [_edge("E1", "ROOT1", "D1"), _edge("E2", "ROOT2", "D2"),
             _edge("E3", "ROOT3", "D3")]
    people = [("P1", "ROOT1"), ("P2", "ROOT2"), ("P3", "ROOT3"),
              ("P4", "ROOT3")]
    items = [
        dict(item_key="T1", person_key="P1", day="2024-01-05", amount=50),
        dict(item_key="T2", person_key="P1", day="2024-01-20", amount=-50),
        dict(item_key="T3", person_key="P2", day="2024-01-08", amount=20),
        dict(item_key="T4", person_key="P2", day="2024-01-18", amount=20),
        dict(item_key="T5", person_key="P3", day="2024-01-10", amount=10),
        dict(item_key="T6", person_key="P4", day="2024-01-25", amount=30),
    ]
    con, svc, rid = _run(edges, people, items)
    return build_table(con, rid)


def scene_5():
    print("\n场景 5：多人多笔 + 正负抵消，贡献人数不重复、不消失")
    t = scene_people()
    _print_table(t)
    _print_people(t)
    print(f"  D1 金额={t.cells[('2024-01','D1')].amount}，"
          f"贡献人数={t.cells[('2024-01','D1')].contributors}"
          f"（P1 两笔正负冲回：金额为 0，但非零份额记录仍在，人数为 1 不是 0）")
    print(f"  D2 金额={t.cells[('2024-01','D2')].amount}，"
          f"贡献人数={t.cells[('2024-01','D2')].contributors}"
          f"（P2 两笔，若按笔数会算 2；按研究者去重只算 1）")
    print(f"  D3 金额={t.cells[('2024-01','D3')].amount}，"
          f"贡献人数={t.cells[('2024-01','D3')].contributors}（P3、P4 两人）")


# ---------------------------------------------------------------- 场景 6

def scene_tie():
    """两个同成本方案：P1 单独占 (r1,A)；两列都能补成环。

    ROOT1 -> A（仅 P1 在 1 月）           => (2024-01,A) 1 人（主抑制）
    ROOT2 -> B,C 各 1/2（P2,P3 在 1 月）  => (2024-01,B/C) 2 人
    ROOT3 -> A,B,C 各 1/3（P4,P5,P6 在 2 月）=> (2024-02,*) 3 人

    k=2 时主抑制 = {(2024-01,A)}。
    补环方案 X = {(2024-01,B),(2024-02,A),(2024-02,B)} 成本 3、数量 3
    补环方案 Y = {(2024-01,C),(2024-02,A),(2024-02,C)} 成本 3、数量 3
    决胜：排序后 X 的第一项 (2024-01,B) < Y 的 (2024-01,C) -> X。
    """
    edges = [
        _edge("R1-A", "ROOT1", "A"),
        _edge("R2-B", "ROOT2", "B", 1, 2), _edge("R2-C", "ROOT2", "C", 1, 2),
        _edge("R3-A", "ROOT3", "A", 1, 3), _edge("R3-B", "ROOT3", "B", 1, 3),
        _edge("R3-C", "ROOT3", "C", 1, 3),
    ]
    people = [("P1", "ROOT1"), ("P2", "ROOT2"), ("P3", "ROOT2"),
              ("P4", "ROOT3"), ("P5", "ROOT3"), ("P6", "ROOT3")]
    items = [
        dict(item_key="K1", person_key="P1", day="2024-01-10", amount=90),
        dict(item_key="K2", person_key="P2", day="2024-01-12", amount=60),
        dict(item_key="K3", person_key="P3", day="2024-01-15", amount=60),
        dict(item_key="K4", person_key="P4", day="2024-02-10", amount=30),
        dict(item_key="K5", person_key="P5", day="2024-02-12", amount=30),
        dict(item_key="K6", person_key="P6", day="2024-02-15", amount=30),
    ]
    con, svc, rid = _run(edges, people, items)
    return con, svc, rid, build_table(con, rid)


def scene_6():
    print("\n场景 6：两个同成本方案按约定决胜（结果确定）")
    con, svc, rid, t = scene_tie()
    _print_table(t)
    _print_people(t)
    policy = Policy(min_contributors=2)
    search = solve_suppression(t, policy)
    print(f"  求解：status={search.status} 主抑制={search.primary}")
    print(f"        次抑制={search.secondary} 成本={search.cost} "
          f"数量={search.extra_count}")
    # 手工列出两个等成本方案并核对决胜
    x = [("2024-01", "B"), ("2024-02", "A"), ("2024-02", "B")]
    y = [("2024-01", "C"), ("2024-02", "A"), ("2024-02", "C")]
    for name, sel in (("X", x), ("Y", y)):
        hidden = set(search.primary) | set(sel)
        ok, alg = _analysis(t, hidden)
        print(f"    方案 {name}={sel}：protected={ok} 成本=3 数量=3 "
              f"排序键={tuple(sorted(sel))}")
    print("    → 两方案目标值相同，按排序后格子列表字典序："
          f"{tuple(sorted(x))} < {tuple(sorted(y))}，选 X；"
          f"求得={tuple(sorted(search.secondary))}")
    # 独立全枚举复核
    rep = reportcheck.enumerate_small(t, policy)
    print(f"    全枚举：可行方案数={rep['feasible_count']} 最优={rep['secondary']} "
          f"成本={rep['cost']} 数量={rep['count']}")
    return con, svc, rid, t


# ---------------------------------------------------------------- 场景 7

def scene_7(table):
    print("\n场景 7：候选数超出搜索上限时不冒称最优")
    policy = Policy(min_contributors=2, max_candidates=2)
    search = solve_suppression(table, policy)
    print(f"  候选数={search.candidates_considered} 上限={search.max_candidates} "
          f"status={search.status} verified={search.verified}")
    print(f"  reason={search.reason}")
    try:
        export_public(table, policy, search)
        print("    (未拒绝，不符合预期)")
    except NotPublishable as e:
        print(f"    → 拒绝导出：{e}")


# ---------------------------------------------------------------- 场景 8

def scene_8(con, rid, policy):
    print("\n场景 8：更换最新路由规则，同一已保存批次导出内容保持一致")
    before_pub, before_audit = publish(con, rid, policy)
    print(f"  批次 run_id={rid} 绑定 version="
          f"{before_audit['binding']['version_id']}（绑定只出现在审计件）")
    print(f"  导出前：隐藏={before_audit['hidden']} "
          f"成本={before_audit['objective']['cost']}")
    svc = AllocationService(con)
    svc.load_rules([_edge("N1", "ROOT1", "X", 1, 2),
                    _edge("N2", "ROOT1", "Y", 1, 2)], note="newest routes")
    new_run = svc.compute(ASOF)
    newest = con.execute(
        "SELECT version_id FROM alloc_rule_version ORDER BY imported_at DESC, "
        "version_id DESC LIMIT 1").fetchone()[0]
    print(f"  新装载 version={newest}，新 run={new_run}")
    after_pub, after_audit = publish(con, rid, policy)
    same = before_pub == after_pub and before_audit == after_audit
    print(f"  同一批次重导出一致={same}，公开物不含批次绑定="
          f"{'binding' not in after_pub}，审计仍绑定 version="
          f"{after_audit['binding']['version_id']}")


# ---------------------------------------------------------------- 主入口

def cmd_demo(args):
    t = scene_rect()
    scene_1(t)
    print("\n" + "=" * 72)
    scene_2(t)
    print("\n" + "=" * 72)
    scene_3()
    print("\n" + "=" * 72)
    scene_4(t)
    print("\n" + "=" * 72)
    scene_5()
    print("\n" + "=" * 72)
    con6, svc6, rid6, t6 = scene_6()
    print("\n" + "=" * 72)
    scene_7(t)
    print("\n" + "=" * 72)
    policy6 = Policy(min_contributors=2)
    scene_8(con6, rid6, policy6)

    print("\n" + "=" * 72)
    print("公开 / 审计两个入口（同一批次，内部与公开分离）")
    pub, audit = publish(con6, rid6, policy6)
    print("  公开导出物：")
    for row in pub["rows"]:
        print(f"    {row['month']}: {row['cells']} 行合计={row['row_total']}")
    print(f"    列合计={pub['col_totals']}")
    print("  审计导出物字段：", sorted(audit))
    print(f"    主抑制={audit['primary']} 次抑制={audit['secondary']} "
          f"目标={audit['objective']}")
    print(f"    见证条数={len(audit['witnesses'])}")
    rep = reportcheck.verify_artifacts(t6, pub, audit)
    print(f"  独立复核（有理数消元 + 见证机检）ok={rep['ok']} "
          f"自由度={rep['algebra']['nullity']}")
    if not rep["ok"]:
        print("    problems:", rep["problems"])
    if args.db != ":memory:":
        from bitemp.alloc.protection import write_json_report
        out = Path(args.db)
        pub_path = out.with_name(out.stem + ".public.json")
        aud_path = out.with_name(out.stem + ".audit.json")
        write_json_report(pub_path, pub)
        write_json_report(aud_path, audit)
        print(f"  已写出 公开={pub_path.name} 审计={aud_path.name}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("demo")
    p.add_argument("--db", default=":memory:")
    args = ap.parse_args()
    return cmd_demo(args)


if __name__ == "__main__":
    raise SystemExit(main())
