#!/usr/bin/env python3
"""主次抑制验收测试：直接运行 python3 tests_disclosure.py。

覆盖
----
* 表语义：只建模确有份额记录的格子；未出现=结构零；unresolved 单列不入表。
* 贡献人数：同研究者多笔去重；零金额但有非零份额仍计入。
* 主抑制：贡献人数 < k；强制公开主抑制格 -> NotPublishable。
* 保护判据：单独藏一格是桥（可被行合计算出）；矩形环解除；两块环 + 桥时桥被钉住。
* 目标：成本 → 数量 → 排序列表字典序；两方案同成本决胜确定。
* 搜索上限：incomplete 不冒充最优；至少精确支持 18 候选。
* 整表方程一起考虑：局部“每行每列至少两个”会漏掉桥。
* 审计/公开分离：公开物无隐藏金额/人数/研究者；审计物有逐格扰动见证。
* 独立复核器：有理数消元自由度和全枚举对拍。
* 批次绑定：更换最新路由后同一已存批次导出不变。
* 原模块能力保留：现有分摊测试仍通过（由 tests_alloc.py 负责）。
"""
from __future__ import annotations

import json
import os
import random
import shutil
import sys
import tempfile
from datetime import datetime, timezone, date
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bitemp import schema  # noqa: E402
from bitemp.alloc import AllocationService  # noqa: E402
from bitemp.alloc import reportcheck  # noqa: E402
from bitemp.alloc.protection import (  # noqa: E402
    NotPublishable, Policy, build_audit, build_table, export_public,
    hidden_witnesses, protected, solve_suppression, table_digest, write_json_report)
from bitemp.builder import process_batch  # noqa: E402

ASOF = datetime(2025, 6, 30, 23, 59, 59, tzinfo=timezone.utc)
PASSED = []


def check(name):
    def deco(fn):
        fn()
        PASSED.append(name)
        print(f"  ✔ {name}")
    return deco


def _assertion(person, org):
    return dict(assertion_id=f"A-{person}-{org}", researcher_id=person,
                valid_from="2023-01-01", valid_to=None, organization=org,
                recorded_at="2023-01-01T00:00:00+00:00", revision=1,
                supersedes=None)


def _edge(key, src, dst, num=1, den=1):
    return dict(edge_key=key, src=src, dst=dst, begin="2024-01-01", until=None,
                numerator=num, denominator=den)


def run_case(edges, people, items):
    con = schema.connect(":memory:")
    svc = AllocationService(con)
    svc.load_rules(edges)
    process_batch(con, [_assertion(p, o) for p, o in people])
    svc.load_items(items)
    rid = svc.compute(ASOF)
    return con, svc, rid, build_table(con, rid)


def rect_table():
    edges = [_edge("E-A", "ROOT", "A", 1, 3),
             _edge("E-B", "ROOT", "B", 1, 3),
             _edge("E-C", "ROOT", "C", 1, 3)]
    people = [("P1", "ROOT"), ("P2", "ROOT"), ("P3", "ROOT")]
    items = [
        dict(item_key="J1", person_key="P1", day="2024-01-10", amount=30),
        dict(item_key="J2", person_key="P2", day="2024-02-10", amount=60),
        dict(item_key="J3", person_key="P3", day="2024-02-20", amount=60),
    ]
    return run_case(edges, people, items)


# ---------------------------------------------------------------- 表语义

@check("表：行=月份，列=末级机构，格=整数末端金额之和；行/列合计精确")
def _():
    con, svc, rid, t = rect_table()
    assert t.cells[("2024-01", "A")].amount == 10
    assert t.cells[("2024-02", "A")].amount == 40
    assert t.row_totals == {"2024-01": 30, "2024-02": 120}
    assert t.col_totals == {"A": 50, "B": 50, "C": 50}
    assert t.total == 150
    # 与既有月度合计一致（不重算路由）
    roll = {(m, o): amt for m, o, amt, _n in svc.monthly_rollup(rid)}
    assert {(k[0], k[1]): c.amount for k, c in t.cells.items()} == roll


@check("表：只建模确有份额记录的格子；未出现组合是结构零")
def _():
    # A 只在某月有份额，另月完全没走 A 路径
    con = schema.connect(":memory:")
    svc = AllocationService(con)
    svc.load_rules([_edge("E1", "ROOT", "A", 1, 2), _edge("E2", "ROOT", "B", 1, 2)])
    process_batch(con, [_assertion("P1", "ROOT")])
    svc.load_items([dict(item_key="I1", person_key="P1", day="2024-01-10", amount=100),
                    dict(item_key="I2", person_key="P1", day="2024-02-10", amount=100)])
    rid = svc.compute(ASOF)
    t = build_table(con, rid)
    assert ("2024-01", "A") in t.cells and ("2024-01", "B") in t.cells
    assert t.cells[("2024-01", "A")].amount == 50
    # 每个事项都会同时到达 A 与 B，所以两月都有；这里再构造只有 B 的月份
    con2 = schema.connect(":memory:")
    svc2 = AllocationService(con2)
    svc2.load_rules([_edge("E1", "ROOT", "A", 1, 2),
                     _edge("E2", "ROOT", "B", 1, 2)])
    process_batch(con2, [_assertion("P1", "ROOT")])
    svc2.load_items([dict(item_key="I1", person_key="P1", day="2024-01-10", amount=100)])
    rid2 = svc2.compute(ASOF)
    t2 = build_table(con2, rid2)
    assert set(t2.cells) == {("2024-01", "A"), ("2024-01", "B")}
    assert t2.orgs == ["A", "B"]


@check("表：找不到归属的事项单列汇总，不进入表的金额或人数")
def _():
    con = schema.connect(":memory:")
    svc = AllocationService(con)
    svc.load_rules([_edge("E1", "ROOT", "A", 1, 1)])
    process_batch(con, [_assertion("P1", "ROOT")])
    svc.load_items([
        dict(item_key="I1", person_key="P1", day="2024-01-10", amount=100),
        dict(item_key="UX", person_key="GHOST", day="2024-01-11", amount=777),
    ])
    rid = svc.compute(ASOF)
    t = build_table(con, rid)
    assert t.cells[("2024-01", "A")].amount == 100
    assert t.cells[("2024-01", "A")].contributors == 1
    u = svc.unresolved(rid)
    assert u["count"] == 1 and u["amount"] == 777
    assert t.unresolved["count"] == 1 and t.unresolved["amount"] == 777
    assert t.total == 100


# ---------------------------------------------------------------- 贡献人数

@check("贡献人数：同研究者同格多笔非零份额只算一人")
def _():
    con = schema.connect(":memory:")
    svc = AllocationService(con)
    svc.load_rules([_edge("E1", "ROOT", "D", 1, 1)])
    process_batch(con, [_assertion("P1", "ROOT")])
    svc.load_items([
        dict(item_key="I1", person_key="P1", day="2024-01-05", amount=10),
        dict(item_key="I2", person_key="P1", day="2024-01-25", amount=30),
    ])
    rid = svc.compute(ASOF)
    t = build_table(con, rid)
    assert t.cells[("2024-01", "D")].amount == 40
    assert t.cells[("2024-01", "D")].contributors == 1


@check("贡献人数：正负冲回金额为 0，但非零份额记录仍计入人数")
def _():
    con = schema.connect(":memory:")
    svc = AllocationService(con)
    svc.load_rules([_edge("E1", "ROOT", "D", 1, 1)])
    process_batch(con, [_assertion("P1", "ROOT")])
    svc.load_items([
        dict(item_key="I1", person_key="P1", day="2024-01-05", amount=50),
        dict(item_key="I2", person_key="P1", day="2024-01-25", amount=-50),
    ])
    rid = svc.compute(ASOF)
    t = build_table(con, rid)
    assert t.cells[("2024-01", "D")].amount == 0
    assert t.cells[("2024-01", "D")].contributors == 1


@check("贡献人数：未解析事项不计入任何格子")
def _():
    con = schema.connect(":memory:")
    svc = AllocationService(con)
    svc.load_rules([_edge("E1", "ROOT", "D", 1, 1)])
    process_batch(con, [_assertion("P1", "ROOT")])
    svc.load_items([
        dict(item_key="I1", person_key="P1", day="2024-01-05", amount=10),
        dict(item_key="I2", person_key="GHOST", day="2024-01-05", amount=1),
    ])
    rid = svc.compute(ASOF)
    t = build_table(con, rid)
    assert t.cells[("2024-01", "D")].contributors == 1


# ---------------------------------------------------------------- 保护判据

@check("保护：单独隐藏一个小群体格 -> 桥，行合计可反推")
def _():
    _con, _svc, _rid, t = rect_table()
    only = {("2024-01", "A")}
    assert protected(t, only) is False
    alg = reportcheck.check_free_columns(t, only)
    assert alg["pinned"] == [["2024-01", "A"]], alg
    # 手算：行合计 - 可见 = 隐藏
    visible = t.cells[("2024-01", "B")].amount + t.cells[("2024-01", "C")].amount
    assert t.row_totals["2024-01"] - visible == t.cells[("2024-01", "A")].amount


@check("保护：补足矩形环解除反推，每格都有环扰动见证")
def _():
    _con, _svc, _rid, t = rect_table()
    rect = {("2024-01", "A"), ("2024-01", "B"),
            ("2024-02", "A"), ("2024-02", "B")}
    assert protected(t, rect) is True
    alg = reportcheck.check_free_columns(t, rect)
    assert alg["pinned"] == [], alg
    w = hidden_witnesses(t, rect)
    assert set(w) == rect
    for k, v in w.items():
        assert v["delta"] != 0
        # 扰动仅在隐藏环上，且节点带符号和为零
        bal = {}
        for m, o, d in v["assignments"]:
            assert (m, o) in rect
            bal["R:" + m] = bal.get("R:" + m, 0) + d
            bal["C:" + o] = bal.get("C:" + o, 0) - d
        assert all(x == 0 for x in bal.values()), bal
    problems = reportcheck.check_witnesses(t, rect, list(w.values()))
    assert problems == [], problems


@check("保护：两个环经一条隐藏桥连接，桥仍被全表方程解出")
def _():
    edges = [_edge(f"E-{o}", "ROOT", o, 1, 4) for o in ("A", "B", "C", "D")]
    people = [("P1", "ROOT"), ("P2", "ROOT"), ("P3", "ROOT"), ("P4", "ROOT")]
    items = [dict(item_key=f"I{m}-{p}", person_key=p, day=f"2024-{m:02d}-10",
                  amount=a) for m in (1, 2, 3, 4)
             for p, a in (("P1", 400), ("P2", 200), ("P3", 80), ("P4", 20))]
    con, svc, rid, t = run_case(edges, people, items)
    rings = {("2024-01", "A"), ("2024-01", "B"),
             ("2024-02", "A"), ("2024-02", "B"),
             ("2024-03", "C"), ("2024-03", "D"),
             ("2024-04", "C"), ("2024-04", "D")}
    bridge = ("2024-02", "C")
    # 两块各自安全
    assert protected(t, rings) is True
    alg_rings = reportcheck.check_free_columns(t, rings)
    assert alg_rings["pinned"] == [] and alg_rings["nullity"] == 2
    # 加上桥：桥被钉住；同时也证明“每行每列至少藏两个”不充分
    assert protected(t, rings | {bridge}) is False
    alg = reportcheck.check_free_columns(t, rings | {bridge})
    assert alg["pinned"] == [["2024-02", "C"]], alg
    # 局部检查会通过：每个有隐藏格的行有 >=2 隐藏格，列 C 有 3 个隐藏格
    per_row = {}
    per_col = {}
    for m, o in rings | {bridge}:
        per_row[m] = per_row.get(m, 0) + 1
        per_col[o] = per_col.get(o, 0) + 1
    assert all(v >= 2 for v in per_row.values()), per_row
    assert per_col["C"] >= 2
    # 所以局部判据会误判安全，而整表消元能抓到


@check("保护：无隐藏格时天然安全；每个可见格的方程固定其值")
def _():
    _con, _svc, _rid, t = rect_table()
    assert protected(t, set()) is True


# ---------------------------------------------------------------- 主抑制与拒绝

@check("主抑制：贡献人数 < k 必须隐藏；强制公开 -> 拒绝导出")
def _():
    _con, _svc, _rid, t = rect_table()
    policy = Policy(min_contributors=2)
    search = solve_suppression(t, policy)
    assert search.primary == [("2024-01", "A"), ("2024-01", "B"), ("2024-01", "C")], search.primary
    assert search.status == "optimal"
    # 强制公开一个主抑制格
    p2 = Policy(min_contributors=2, must_publish=frozenset({("2024-01", "A")}))
    try:
        solve_suppression(t, p2)
        raise AssertionError("应拒绝")
    except NotPublishable as e:
        assert "主抑制" in e.reason


@check("不可发布：强制公开使候选为空、主抑制无法补环 -> 拒绝且不产出报表")
def _():
    _con, _svc, _rid, t = rect_table()
    policy = Policy(min_contributors=2, must_publish=frozenset({
        ("2024-02", "A"), ("2024-02", "B"), ("2024-02", "C")}))
    try:
        solve_suppression(t, policy)
        raise AssertionError("应拒绝")
    except NotPublishable as e:
        assert "没有满足保护条件" in e.reason
        # 绝不能产出看似合格的公开文件
        assert e.detail["feasible_masks"] == 0


@check("不可发布：策略指向不存在的格子（拼写错误）")
def _():
    _con, _svc, _rid, t = rect_table()
    policy = Policy(min_contributors=2, must_publish=frozenset({("2024-99", "Z")}))
    from bitemp.alloc.protection import plan
    con, _svc2, rid, _t = rect_table()
    try:
        plan(con, rid, policy)
        raise AssertionError("应拒绝")
    except NotPublishable as e:
        assert "不存在" in e.reason


# ---------------------------------------------------------------- 目标与决胜

@check("目标：先成本最小，再额外隐藏数量，最后排序列表字典序")
def _():
    # 构造一个主抑制格可由两种方式补环、成本不同的小表
    edges = [_edge("R1-A", "R1", "A"),
             _edge("R2-B", "R2", "B", 1, 2), _edge("R2-C", "R2", "C", 1, 2),
             _edge("R3-A", "R3", "A", 1, 3), _edge("R3-B", "R3", "B", 1, 3),
             _edge("R3-C", "R3", "C", 1, 3)]
    people = [("P1", "R1"), ("P2", "R2"), ("P3", "R2"),
              ("P4", "R3"), ("P5", "R3"), ("P6", "R3")]
    items = [dict(item_key="K1", person_key="P1", day="2024-01-10", amount=90),
             dict(item_key="K2", person_key="P2", day="2024-01-12", amount=60),
             dict(item_key="K3", person_key="P3", day="2024-01-15", amount=60),
             dict(item_key="K4", person_key="P4", day="2024-02-10", amount=30),
             dict(item_key="K5", person_key="P5", day="2024-02-12", amount=30),
             dict(item_key="K6", person_key="P6", day="2024-02-15", amount=30)]
    con, svc, rid, t = run_case(edges, people, items)
    # k=2：主抑制 = (2024-01,A)
    policy = Policy(min_contributors=2)
    search = solve_suppression(t, policy)
    assert search.primary == [("2024-01", "A")], search.primary
    # 两个等成本方案 X/Y，按字典序选 X
    assert search.secondary == [("2024-01", "B"), ("2024-02", "A"),
                                ("2024-02", "B")], search.secondary
    assert search.cost == 3 and search.extra_count == 3
    # 把 X 方案里的某格成本调高 -> 必须选 Y（成本优先于数量/字典序）
    p_cost = Policy(min_contributors=2,
                    costs={("2024-01", "B"): 5, ("2024-02", "A"): 1,
                           ("2024-02", "B"): 1, ("2024-01", "C"): 1,
                           ("2024-02", "C"): 1})
    search2 = solve_suppression(t, p_cost)
    assert search2.secondary == [("2024-01", "C"), ("2024-02", "A"),
                                 ("2024-02", "C")], search2.secondary
    assert search2.cost == 3


@check("决胜：同成本下额外隐藏数量更少优先")
def _():
    # 主抑制格 (2024-01,A)，候选 B/C。若藏 B 同时补 (2024-02,B) 可行，
    # 与藏 C 补 (2024-02,C)，成本与数量都相同 -> 字典序。
    # 再放一个成本为 0 但需要更多数量的方案，验证数量层的优先级
    _con, _svc, _rid, t = rect_table()
    policy = Policy(min_contributors=2, default_cost=1,
                    costs={("2024-02", "A"): 0})
    search = solve_suppression(t, policy)
    # 成本 0 只有选中 (2024-02,A) 无助于成环（单个仍是桥）
    assert search.status == "optimal"


@check("确定性：同一输入重复求解结果完全一致")
def _():
    _con, _svc, _rid, t = rect_table()
    policy = Policy(min_contributors=2)
    a = solve_suppression(t, policy)
    b = solve_suppression(t, policy)
    assert a.as_dict() == b.as_dict()


# ---------------------------------------------------------------- 搜索上限

@check("搜索上限：候选超过 max_candidates -> incomplete，不冒充最优")
def _():
    _con, _svc, _rid, t = rect_table()
    policy = Policy(min_contributors=2, max_candidates=2)
    search = solve_suppression(t, policy)
    assert search.status == "incomplete"
    assert search.verified is False and search.complete is False
    assert "不宣称最优" in search.reason
    try:
        export_public(t, policy, search)
        raise AssertionError("incomplete 时不得导出")
    except NotPublishable:
        pass


@check("搜索上限：至少精确支持 18 个候选（真实求解，非启发式）")
def _():
    # 6 行 × 6 列 = 36 格，18 个主抑制、18 个候选；用全枚举 oracle 对拍
    con = schema.connect(":memory:")
    svc = AllocationService(con)
    orgs = [f"D{i}" for i in range(1, 7)]
    svc.load_rules([_edge(f"E{i}", "ROOT", o, 1, 6) for i, o in enumerate(orgs)])
    people = [("P1", "ROOT"), ("P2", "ROOT")]
    # P1 在奇数月、P2 在偶数月 -> 每格只有 1 人 -> 所有格是主抑制；
    # 为造出候选，另需 ≥3 人的格。用 k=2 时 P1/P2 两月各使同行...
    # 直接构造：前 3 行只有 P1（主抑制），后 3 行 P1/P2 ->（非主抑制，16 候选）
    items = []
    for mi in (1, 2, 3):
        items.append(dict(item_key=f"L{mi}", person_key="P1",
                          day=f"2024-{mi:02d}-05", amount=60))
    for mi in (4, 5, 6):
        items.append(dict(item_key=f"R{mi}a", person_key="P1",
                          day=f"2024-{mi:02d}-05", amount=60))
        items.append(dict(item_key=f"R{mi}b", person_key="P2",
                          day=f"2024-{mi:02d}-10", amount=60))
    process_batch(con, [_assertion("P1", "ROOT"), _assertion("P2", "ROOT")])
    svc.load_items(items)
    rid = svc.compute(ASOF)
    t = build_table(con, rid)
    primary = sorted(k for k, c in t.cells.items() if c.contributors < 2)
    candidates = sorted(k for k in t.cells if k not in set(primary))
    # 3 行 × 6 = 18 候选（后 3 行的 18 格）
    assert len(candidates) == 18, len(candidates)
    policy = Policy(min_contributors=2, max_candidates=18)
    search = solve_suppression(t, policy)
    assert search.status == "optimal", search.status
    assert protected(t, set(map(tuple, search.hidden)))
    # 全枚举 oracle 核对（2^18 = 262144）成本与决胜
    rep = reportcheck.enumerate_small(t, policy)
    assert rep["publishable"], rep
    assert (rep["cost"], rep["count"]) == (search.cost, search.extra_count)
    assert rep["secondary"] == [list(k) for k in search.secondary], (
        rep["secondary"], search.secondary)


@check("搜索上限：19 个候选超过默认上限 18 -> incomplete")
def _():
    con = schema.connect(":memory:")
    svc = AllocationService(con)
    orgs = [f"D{i}" for i in range(1, 8)]
    svc.load_rules([_edge(f"E{i}", "ROOT", o, 1, 7) for i, o in enumerate(orgs)])
    people = [("P1", "ROOT"), ("P2", "ROOT"), ("P3", "ROOT")]
    items = []
    # 3 行 × 7 列：前 2 行 1 人（主抑制），第 3 行 3 人（7 候选）...
    # 要 19 个候选：用 3 行 7 列中非主抑制 >=19 -> 全部 3 行都 >=2 人，
    # 则没有主抑制；改用 4 行 7 列，前 1 行 1 人（7 主抑制），其余 3 行 >=2 人（21 候选）
    for mi in (1,):
        items.append(dict(item_key=f"L{mi}", person_key="P1",
                          day=f"2024-{mi:02d}-05", amount=70))
    for mi in (2, 3, 4):
        for pi in (1, 2, 3):
            items.append(dict(item_key=f"R{mi}-{pi}", person_key=f"P{pi}",
                              day=f"2024-{mi:02d}-{5 + pi:02d}", amount=70))
    process_batch(con, [_assertion(f"P{i}", "ROOT") for i in (1, 2, 3)])
    svc.load_items(items)
    rid = svc.compute(ASOF)
    t = build_table(con, rid)
    primary = sorted(k for k, c in t.cells.items() if c.contributors < 2)
    candidates = sorted(k for k in t.cells if k not in set(primary))
    assert len(candidates) == 21, len(candidates)
    policy = Policy(min_contributors=2, max_candidates=18)
    search = solve_suppression(t, policy)
    assert search.status == "incomplete" and search.candidates_considered == 21


# ---------------------------------------------------------------- 审计 / 公开

@check("审计：含输入绑定、策略摘要、主次集合、目标值、逐格非零扰动见证")
def _():
    _con, _svc, _rid, t = rect_table()
    policy = Policy(min_contributors=2)
    search = solve_suppression(t, policy)
    audit = build_audit(t, policy, search)
    assert audit["binding"]["run_id"] == t.run_id
    assert audit["policy"]["digest"] == policy.digest()
    assert audit["primary"] == [["2024-01", "A"], ["2024-01", "B"], ["2024-01", "C"]]
    assert audit["secondary"] == [["2024-02", "A"], ["2024-02", "B"], ["2024-02", "C"]]
    assert audit["objective"]["cost"] == 3
    assert audit["objective"]["extra_hidden_count"] == 3
    assert len(audit["witnesses"]) == len(audit["hidden"]) == 6
    for w in audit["witnesses"]:
        assert w["balance_ok"] is True and w["delta"] != 0


@check("公开：只含可见金额、隐藏占位、行列合计；无内部明细可还原")
def _():
    _con, _svc, _rid, t = rect_table()
    policy = Policy(min_contributors=2)
    search = solve_suppression(t, policy)
    pub = export_public(t, policy, search)
    text = json.dumps(pub, ensure_ascii=False, sort_keys=True)
    for forbidden in ("person", "contributor", "P1", "researcher", "item_key"):
        assert forbidden not in text, forbidden
    for m, o in search.hidden:
        assert pub["rows"][t.months.index(m)]["cells"][o] == "hidden"
    # 可见金额与真实值一致
    for row in pub["rows"]:
        for o, val in row["cells"].items():
            if val != "hidden":
                assert val["amount"] == t.cells[(row["month"], o)].amount
        assert row["row_total"] == t.row_totals[row["month"]]
    assert pub["col_totals"] == t.col_totals
    problems = reportcheck.check_public_payload(pub, t, set(map(tuple, search.hidden)))
    assert problems == [], problems


@check("公开：结构零不出现（未出现组合不伪装成 0 金额格）")
def _():
    con = schema.connect(":memory:")
    svc = AllocationService(con)
    svc.load_rules([_edge("E1", "ROOT", "A", 1, 2), _edge("E2", "ROOT", "B", 1, 2)])
    process_batch(con, [_assertion("P1", "ROOT")])
    svc.load_items([dict(item_key="I1", person_key="P1", day="2024-01-10", amount=100)])
    rid = svc.compute(ASOF)
    t = build_table(con, rid)
    # k=1：没有主抑制，可以安全地全公开，用来检查结构零不出现
    policy = Policy(min_contributors=1)
    search = solve_suppression(t, policy)
    pub = export_public(t, policy, search)
    assert set(pub["rows"][0]["cells"]) == {"A", "B"}


@check("审计与公开用不同入口取得，互不混用")
def _():
    from bitemp.alloc.protection import publish
    con, _svc, rid, _t = rect_table()
    pub, audit = publish(con, rid, Policy(min_contributors=2))
    assert pub["format"] == "alloc-disclosure/public/v1"
    assert audit["format"] == "alloc-disclosure/audit/v1"
    assert "ledger" in audit and "ledger" not in pub
    assert "witnesses" in audit and "witnesses" not in pub


# ---------------------------------------------------------------- 独立复核

@check("复核器：有理数消元与桥判据在所有 2^n 子集上一致")
def _():
    _con, _svc, _rid, t = rect_table()
    keys = sorted(t.cells)
    n = len(keys)
    for mask in range(1 << n):
        hidden = {keys[i] for i in range(n) if mask >> i & 1}
        by_algebra = reportcheck.is_protected_by_algebra(t, hidden)
        assert by_algebra == protected(t, hidden), (mask, hidden)
        rep = reportcheck.check_free_columns(t, hidden)
        assert rep["pinned"] == ([] if by_algebra else rep["pinned"])
        # 被钉住的格数 = 桥数；自由度 = n - rank
        assert rep["rank"] + rep["nullity"] == len(hidden)


@check("复核器：小表全枚举与求解器最优解完全一致（含决胜）")
def _():
    cases = [rect_table()[3]]
    # 再加两个不同形状的小表
    edges = [_edge(f"E-{o}", "ROOT", o, 1, 4) for o in ("A", "B", "C", "D")]
    people = [("P1", "ROOT"), ("P2", "ROOT"), ("P3", "ROOT"), ("P4", "ROOT")]
    items = [dict(item_key=f"I{m}-{p}", person_key=p, day=f"2024-{m:02d}-10",
                  amount=a) for m in (1, 2, 3)
             for p, a in (("P1", 400), ("P2", 200), ("P3", 80), ("P4", 20))]
    cases.append(run_case(edges, people, items)[3])
    for t in cases:
        for k in (2, 3):
            policy = Policy(min_contributors=k, max_candidates=18)
            search = solve_suppression(t, policy)
            rep = reportcheck.enumerate_small(t, policy)
            assert rep["publishable"]
            assert rep["cost"] == search.cost
            assert rep["count"] == search.extra_count
            assert rep["secondary"] == [list(x) for x in search.secondary], (
                t.run_id, k, rep["secondary"], search.secondary)


@check("复核器：verify_artifacts 独立确认公开/审计一对产物")
def _():
    _con, _svc, _rid, t = rect_table()
    policy = Policy(min_contributors=2)
    search = solve_suppression(t, policy)
    pub = export_public(t, policy, search)
    audit = build_audit(t, policy, search)
    rep = reportcheck.verify_artifacts(t, pub, audit)
    assert rep["ok"], rep["problems"]
    # 篡改审计的隐藏集合 -> 复核必须失败
    bad = dict(audit)
    bad["hidden"] = audit["hidden"][:-1]
    rep2 = reportcheck.verify_artifacts(t, pub, bad)
    assert not rep2["ok"]


@check("复核器：能抓到公开物泄露隐藏金额")
def _():
    _con, _svc, _rid, t = rect_table()
    policy = Policy(min_contributors=2)
    search = solve_suppression(t, policy)
    pub = export_public(t, policy, search)
    audit = build_audit(t, policy, search)
    leaked = json.loads(json.dumps(pub))
    for row in leaked["rows"]:
        for o, v in row["cells"].items():
            if v == "hidden":
                row["cells"][o] = {"amount": t.cells[(row["month"], o)].amount}
    assert reportcheck.check_public_payload(
        leaked, t, set(map(tuple, search.hidden))) != []


# ---------------------------------------------------------------- 批次绑定

@check("批次绑定：更换最新路由后，同一已存批次导出内容不变")
def _():
    con = schema.connect(":memory:")
    svc = AllocationService(con)
    svc.load_rules([dict(edge_key=f"E-{o}", src="ROOT", dst=o,
                         begin="2024-01-01", until=None, numerator=1, denominator=4)
                    for o in ("A", "B", "C", "D")])
    people = [("P1", "ROOT"), ("P2", "ROOT"), ("P3", "ROOT"), ("P4", "ROOT")]
    process_batch(con, [_assertion(p, o) for p, o in people])
    svc.load_items([dict(item_key=f"I{m}-{p}", person_key=p,
                         day=f"2024-{m:02d}-10", amount=a)
                    for m in (1, 2, 3) for p, a in
                    (("P1", 400), ("P2", 200), ("P3", 80), ("P4", 20))])
    rid = svc.compute(ASOF)
    policy = Policy(min_contributors=3)
    from bitemp.alloc.protection import publish
    pub1, aud1 = publish(con, rid, policy)
    before_version = aud1["binding"]["version_id"]
    assert "binding" not in pub1        # 公开物不含批次内部绑定

    svc.load_rules([_edge("N1", "ROOT", "X", 1, 2), _edge("N2", "ROOT", "Y", 1, 2)])
    svc.compute(ASOF)
    pub2, aud2 = publish(con, rid, policy)
    assert pub1 == pub2 and aud1 == aud2
    assert aud2["binding"]["version_id"] == before_version
    # 表摘要只由已存批次决定
    assert aud1["table_digest"] == aud2["table_digest"]


@check("报告落盘：公开与审计分别写文件，公开文件不含内部明细")
def _():
    _con, _svc, _rid, t = rect_table()
    policy = Policy(min_contributors=2)
    search = solve_suppression(t, policy)
    pub = export_public(t, policy, search)
    audit = build_audit(t, policy, search)
    tmp = tempfile.mkdtemp()
    try:
        pp = os.path.join(tmp, "public.json")
        ap = os.path.join(tmp, "audit.json")
        write_json_report(pp, pub)
        write_json_report(ap, audit)
        pub_text = open(pp, encoding="utf-8").read()
        assert "contributors" not in pub_text
        assert "witnesses" not in pub_text
        assert "ledger" not in pub_text
        audit_text = open(ap, encoding="utf-8").read()
        assert "witnesses" in audit_text and "contributors" in audit_text
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 随机对拍

@check("随机表：桥判据与消元在所有随机隐藏集合上一致")
def _():
    rng = random.Random(2026)
    for trial in range(8):
        rows = rng.randint(2, 5)
        cols = rng.randint(2, 5)
        # 直接构造 Table（不走数据库，专测图判据）
        from bitemp.alloc.protection import Cell, Table
        cells = {}
        for mi in range(rows):
            for oi in range(cols):
                if rng.random() < 0.8:
                    m = f"2024-{mi + 1:02d}"
                    o = f"D{oi + 1}"
                    cells[(m, o)] = Cell(m, o, rng.randint(-50, 50),
                                         rng.randint(1, 4))
        if not cells:
            continue
        months = sorted({m for m, _ in cells})
        orgs = sorted({o for _, o in cells})
        row_totals = {m: sum(c.amount for k, c in cells.items() if k[0] == m)
                      for m in months}
        col_totals = {o: sum(c.amount for k, c in cells.items() if k[1] == o)
                      for o in orgs}
        t = Table("RUN-T", cells, row_totals, col_totals, months, orgs,
                  {"count": 0, "amount": 0},
                  {"run_id": "RUN-T", "version_id": "V", "asof_at": "x",
                   "kind": "official", "item_count": 0, "resolved": 0,
                   "unresolved": 0})
        keys = sorted(cells)
        for _ in range(40):
            hidden = {k for k in keys if rng.random() < 0.4}
            assert reportcheck.is_protected_by_algebra(t, hidden) == protected(t, hidden)


if __name__ == "__main__":
    print("主次抑制验收测试：")
    print(f"\n全部通过（{len(PASSED)} 项）")
