#!/usr/bin/env python3
"""分摊管线验收测试：直接运行 python3 tests_alloc.py。

覆盖需求全部要点：金标准手算、多路汇合、精确取整、unresolved、
规则边界切分校验（环/守恒/负分母/悬空/重复配置/跨期假环）、事务拒收、
固定摘要幂等、结果绑定版本与 asof、事项幂等/冲突、月度合计、落库解释一致、
独立逐路径 oracle 对拍、随机 DAG 守恒、30 层菱形不展开路径。
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, datetime, timezone
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bitemp import schema  # noqa: E402
from bitemp.builder import process_batch  # noqa: E402
from bitemp.alloc import AllocationService, RuleRejected  # noqa: E402
from bitemp.alloc.allocate import largest_remainder  # noqa: E402
from bitemp.alloc.diamond import (count_paths, diamond_edges, random_dag)  # noqa: E402
from bitemp.alloc import goldens  # noqa: E402
from bitemp.alloc.graph import RuleGraph, propagate, validate_edges  # noqa: E402

UTC = timezone.utc
ASOF = datetime(2025, 1, 1, tzinfo=UTC)
PASSED = []


def check(name):
    def deco(fn):
        fn()
        PASSED.append(name)
        print(f"  ✔ {name}")
    return deco


def _assertion(person="P1", org="ROOT", valid_from="2023-01-01", valid_to=None):
    return dict(assertion_id=f"AS-{person}-{org}", researcher_id=person,
                valid_from=valid_from, valid_to=valid_to, organization=org,
                recorded_at="2023-01-01T00:00:00+00:00", revision=1,
                supersedes=None)


def _edge(key, src, dst, begin="2024-01-01", until=None, num=1, den=1):
    return dict(edge_key=key, src=src, dst=dst, begin=begin, until=until,
                numerator=num, denominator=den)


def setup(case):
    con, svc, keys = goldens.setup_db(case)
    return con, svc, keys


# ---------------------------------------------------------------- 金标准

@check("金标准：两支到同一末端（各自末级）50/50")
def _():
    case = goldens.gold_two_branches()
    con, svc, keys = setup(case)
    rid = svc.compute(ASOF)
    leaves = con.execute(
        "SELECT terminal, CAST(amount AS HUGEINT) FROM alloc_run_leaf "
        "WHERE run_id = ? ORDER BY terminal", [rid]).fetchall()
    assert dict(leaves) == case["expect"], leaves
    weights = {t: Fraction(int(n), int(d)) for t, n, d in con.execute(
        "SELECT terminal, weight_num, weight_den FROM alloc_run_weight "
        "WHERE run_id = ? ORDER BY terminal", [rid]).fetchall()}
    assert weights == case["expect_weights"], weights


@check("金标准：三等分一分钱（最大余数法 + 余数相同按标识升序）")
def _():
    case = goldens.gold_three_way_penny()
    con, svc, keys = setup(case)
    rid = svc.compute(ASOF)
    leaves = dict(con.execute(
        "SELECT terminal, CAST(amount AS HUGEINT) FROM alloc_run_leaf "
        "WHERE run_id = ? ORDER BY terminal", [rid]).fetchall())
    assert leaves == case["expect"], leaves
    # 1 分钱严格守恒
    assert sum(leaves.values()) == 1
    # 权重相等 -> 升序 A 得到那 1 个单位
    assert leaves == {"A": 1, "B": 0, "C": 0}


@check("金标准：负数冲回（负金额按相同结果反号，严格守恒）")
def _():
    case = goldens.gold_negative_offset()
    con, svc, keys = setup(case)
    rid = svc.compute(ASOF)
    leaves = dict(con.execute(
        "SELECT terminal, CAST(amount AS HUGEINT) FROM alloc_run_leaf "
        "WHERE run_id = ? ORDER BY terminal", [rid]).fetchall())
    assert leaves == case["expect"], leaves
    assert sum(leaves.values()) == -101
    # 与正数情形逐项反号
    pos = largest_remainder(101, {"A": Fraction(1, 2), "B": Fraction(1, 2)})
    neg = largest_remainder(-101, {"A": Fraction(1, 2), "B": Fraction(1, 2)})
    assert {k: -v for k, v in pos.items()} == neg
    assert sum(neg.values()) == -101


@check("金标准：机构跨日换出口（切段后旧新出口各归其位）")
def _():
    case = goldens.gold_org_switches_exit()
    con, svc, keys = setup(case)
    rid = svc.compute(ASOF)
    actual = {}
    for item, terminal, amount in con.execute(
            "SELECT item_key, terminal, CAST(amount AS HUGEINT) "
            "FROM alloc_run_leaf WHERE run_id = ? ORDER BY item_key, terminal",
            [rid]).fetchall():
        actual.setdefault(item, {})[terminal] = amount
    assert actual == case["expect"], actual


@check("同一节点在不同日期段可分别是末级与中间节点")
def _():
    # 1-6 月：ROOT -> A（A 无出边 -> 末级）
    # 6 月起：A -> B、C（A 成为中间节点）
    # 两个事项：1 月的到 A，7 月的经 A 到 B/C
    con = schema.connect(":memory:")
    svc = AllocationService(con)
    svc.load_rules([
        _edge("E1", "ROOT", "A", begin="2024-01-01", until="2024-06-01", num=1, den=1),
        _edge("E2", "ROOT", "A", begin="2024-06-01", num=1, den=1),
        _edge("E3", "A", "B", begin="2024-06-01", num=1, den=2),
        _edge("E4", "A", "C", begin="2024-06-01", num=1, den=2),
    ])
    process_batch(con, [_assertion()])
    svc.load_items([
        dict(item_key="W1", person_key="P1", day="2024-02-01", amount=100),
        dict(item_key="W2", person_key="P1", day="2024-08-01", amount=100),
    ])
    rid = svc.compute(ASOF)
    got = {}
    for item, terminal, amount in con.execute(
            "SELECT item_key, terminal, CAST(amount AS HUGEINT) FROM alloc_run_leaf "
            "WHERE run_id = ? ORDER BY item_key, terminal", [rid]).fetchall():
        got.setdefault(item, {})[terminal] = amount
    assert got == {"W1": {"A": 100}, "W2": {"B": 50, "C": 50}}, got


@check("JSONL 装载：两类文件读入并端到端计算")
def _():
    import tempfile, shutil
    from bitemp.alloc import write_jsonl
    tmp = tempfile.mkdtemp()
    try:
        write_jsonl(os.path.join(tmp, "edges.jsonl"), [
            _edge("E1", "ROOT", "A", num=1, den=2),
            _edge("E2", "ROOT", "B", num=1, den=2),
        ])
        write_jsonl(os.path.join(tmp, "items.jsonl"), [
            dict(item_key="J1", person_key="P1", day="2024-02-01", amount=101),
        ])
        con = schema.connect(":memory:")
        svc = AllocationService(con)
        svc.load_rules_jsonl(os.path.join(tmp, "edges.jsonl"))
        process_batch(con, [_assertion()])
        r = svc.load_items_jsonl(os.path.join(tmp, "items.jsonl"))
        assert (r.inserted, r.duplicates) == (1, 0)
        rid = svc.compute(ASOF)
        assert dict(con.execute("SELECT terminal, CAST(amount AS HUGEINT) "
                                "FROM alloc_run_leaf WHERE run_id = ?", [rid]).fetchall()
                    ) == {"A": 51, "B": 50}
        # 重复导入事项：幂等
        assert svc.load_items_jsonl(os.path.join(tmp, "items.jsonl")).duplicates == 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@check("规则内完全重复的边行去重；同键不同内容报标识冲突")
def _():
    con = schema.connect(":memory:")
    svc = AllocationService(con)
    e = _edge("E1", "ROOT", "A", num=1, den=1)
    rep = svc.load_rules([e, dict(e)])          # 完全重复 -> 去重，成功
    assert rep.edge_count == 1
    try:
        svc.load_rules([e, _edge("E1", "ROOT", "A", num=2, den=2)])  # 同键不同内容
        raise AssertionError("应拒收标识冲突")
    except RuleRejected as ex:
        assert any(i.kind == "标识冲突" for i in ex.issues), ex.issues


@check("金标准：同时有效的环被拒收")
def _():
    case = goldens.gold_true_cycle_rejected()
    con = schema.connect(":memory:")
    svc = AllocationService(con)
    try:
        svc.load_rules(case["edges"])
        raise AssertionError("应拒收")
    except RuleRejected as e:
        assert any(i.kind == "环" for i in e.issues), e.issues
        cyc = [i for i in e.issues if i.kind == "环"][0]
        assert cyc.date == date(2024, 1, 1)  # 可复现日期
        assert set(cyc.edge_keys) == {"E2", "E3"}  # 环上的边（X->Y->X）


@check("假环且逐段守恒：反向边错期 + 各段守恒，必须放行并正常计算")
def _():
    edges = [
        _edge("E1", "ROOT", "A", begin="2024-01-01", num=1, den=1),
        _edge("E2", "A", "B", begin="2024-01-01", until="2024-04-01", num=1, den=1),
        _edge("E3", "B", "A", begin="2024-04-01", num=1, den=1),
        _edge("E4", "A", "TAIL", begin="2024-04-01", num=1, den=1),
    ]
    con = schema.connect(":memory:")
    svc = AllocationService(con)
    svc.load_rules(edges)               # 不抛异常
    process_batch(con, [_assertion()])
    svc.load_items([dict(item_key="I1", person_key="P1", day="2024-05-01", amount=10)])
    rid = svc.compute(ASOF)
    assert dict(con.execute("SELECT terminal, CAST(amount AS HUGEINT) FROM alloc_run_leaf "
                            "WHERE run_id = ?", [rid]).fetchall()) == {"TAIL": 10}
    from bitemp.alloc.graph import find_cycle
    from bitemp.alloc.model import Edge as _E
    all_e = [_E(e["edge_key"], e["src"], e["dst"], date.fromisoformat(e["begin"]),
                None if e["until"] is None else date.fromisoformat(e["until"]),
                e["numerator"], e["denominator"]) for e in edges]
    assert find_cycle(all_e) is not None     # 整图合看确有环


@check("每条实际参与边落库精确金额归因（流量权重 × 金额，不取整）")
def _():
    con = schema.connect(":memory:")
    svc = AllocationService(con)
    svc.load_rules([_edge("E1", "ROOT", "A", num=1, den=3),
                    _edge("E2", "ROOT", "B", num=2, den=3)])
    process_batch(con, [_assertion()])
    svc.load_items([dict(item_key="I1", person_key="P1", day="2024-02-01", amount=100)])
    rid = svc.compute(ASOF)
    rows = con.execute(
        "SELECT edge_key, flow_num, flow_den, amount_num, amount_den "
        "FROM alloc_run_edge_flow WHERE run_id = ? ORDER BY edge_key", [rid]).fetchall()
    got = {r[0]: (Fraction(int(r[1]), int(r[2])), Fraction(int(r[3]), int(r[4])))
           for r in rows}
    assert got == {"E1": (Fraction(1, 3), Fraction(100, 3)),
                   "E2": (Fraction(2, 3), Fraction(200, 3))}, got
    assert sum((a for _f, a in got.values()), Fraction(0)) == 100
    p = svc.explain(rid, "I1", "B")
    assert p["edge_amounts"][0]["amount"] == Fraction(100, 3)


@check("金标准：仅跨期组合的假环被放行，逐段计算正确")
def _():
    case = goldens.gold_fake_cycle_cross_period()
    con, svc, keys = setup(case)
    rid = svc.compute(ASOF)
    actual = {}
    for item, terminal, amount in con.execute(
            "SELECT item_key, terminal, CAST(amount AS HUGEINT) "
            "FROM alloc_run_leaf WHERE run_id = ? ORDER BY item_key, terminal",
            [rid]).fetchall():
        actual.setdefault(item, {})[terminal] = amount
    assert actual == case["expect"], actual
    # 合并图确有环，但按日期切段后无环 —— 证明“合看有环仍被允许”
    all_edges = RuleGraph([__import__("bitemp.alloc.model", fromlist=["Edge"]).Edge(
        e["edge_key"], e["src"], e["dst"], date.fromisoformat(e["begin"]),
        None if e["until"] is None else date.fromisoformat(e["until"]),
        e["numerator"], e["denominator"]) for e in case["edges"]]).edges
    from bitemp.alloc.graph import find_cycle
    assert find_cycle(all_edges) is not None       # 整图（忽略时间）有环
    assert validate_edges(all_edges) == []         # 逐段校验通过


@check("多路汇合：菱形两路到同一末端，份额相加为 1，解释可见汇合")
def _():
    case = goldens.gold_merge_fan_in()
    con, svc, keys = setup(case)
    rid = svc.compute(ASOF)
    leaves = dict(con.execute(
        "SELECT terminal, CAST(amount AS HUGEINT) FROM alloc_run_leaf "
        "WHERE run_id = ?", [rid]).fetchall())
    assert leaves == case["expect"], leaves
    flows = {k: Fraction(int(n), int(d)) for k, n, d in con.execute(
        "SELECT edge_key, flow_num, flow_den FROM alloc_run_edge_flow "
        "WHERE run_id = ? ORDER BY edge_key", [rid]).fetchall()}
    assert flows == case["expect_flows"], flows
    exp = svc.explain(rid, "I7", "END")
    nodes = exp["explanation"]["nodes"]
    assert nodes["L"]["forward"] == Fraction(1, 2)
    assert nodes["R"]["forward"] == Fraction(1, 2)
    # END 的前向权重 = 1/2 + 1/2，解释里两条入边贡献都可看到
    assert nodes["END"]["forward"] == Fraction(1)
    assert [x["edge_key"] for x in nodes["END"]["incoming"]] == ["E3", "E4"]
    assert sum(x["contribution"] for x in nodes["END"]["incoming"]) == Fraction(1)


@check("unresolved：找不到起点的事项单列，不参与分摊也不丢弃")
def _():
    case = goldens.gold_unresolved()
    con, svc, keys = setup(case)
    rid = svc.compute(ASOF)
    u = svc.unresolved(rid)
    assert u["count"] == 1 and u["amount"] == 9, u
    assert u["items"][0][0] == "I8b"
    # 解析成功的一条正常分摊
    assert con.execute("SELECT COUNT(*) FROM alloc_run_leaf WHERE run_id = ?",
                       [rid]).fetchone()[0] == 1


# ---------------------------------------------------------------- 校验负例

def _reject_reasons(edges):
    con = schema.connect(":memory:")
    svc = AllocationService(con)
    try:
        svc.load_rules(edges)
    except RuleRejected as e:
        return e.issues
    return None


@check("拒收：权重不守恒（具体问题边 + 日期）")
def _():
    issues = _reject_reasons([
        _edge("E1", "ROOT", "A", num=1, den=2),
        _edge("E2", "ROOT", "B", num=1, den=3),
    ])
    assert issues is not None
    it = [i for i in issues if i.kind == "权重不守恒"][0]
    assert set(it.edge_keys) == {"E1", "E2"}
    assert it.date == date(2024, 1, 1)


@check("拒收：负分母（含分母为 0）")
def _():
    issues = _reject_reasons([_edge("E1", "ROOT", "A", num=1, den=-2)])
    assert issues and any(i.kind == "非正分母" for i in issues)
    issues0 = _reject_reasons([_edge("E1", "ROOT", "A", num=1, den=0)])
    assert issues0 and any(i.kind == "非正分母" for i in issues0)


@check("拒收：悬空标识（空 src/dst 或不可达问题边仍可定位）")
def _():
    issues = _reject_reasons([
        _edge("E1", "ROOT", "  ", num=1, den=1),
    ])
    assert issues and any(i.kind == "悬空标识" for i in issues)
    assert issues[0].edge_keys == ("E1",)


@check("拒收：同一 (src,dst) 活跃期重叠的重复配置")
def _():
    issues = _reject_reasons([
        _edge("E1", "ROOT", "A", begin="2024-01-01", until="2024-06-01", num=1, den=1),
        _edge("E2", "ROOT", "A", begin="2024-03-01", until="2024-12-01", num=1, den=1),
    ])
    assert issues and any(i.kind == "重复配置" for i in issues)
    it = [i for i in issues if i.kind == "重复配置"][0]
    assert set(it.edge_keys) == {"E1", "E2"} and it.date == date(2024, 3, 1)


@check("同一 (src,dst) 活跃期不重叠时允许（不同阶段不同边）")
def _():
    con = schema.connect(":memory:")
    svc = AllocationService(con)
    rep = svc.load_rules([
        _edge("E1", "ROOT", "A", begin="2024-01-01", until="2024-06-01", num=1, den=1),
        _edge("E2", "ROOT", "A", begin="2024-06-01", num=1, den=1),
    ])
    assert rep.edge_count == 2


@check("拒收：跨期边组合后仍满足逐段守恒（只按段校验）")
def _():
    # 早期 ROOT->A 权重 1；晚期 ROOT->B 权重 1。合并看 ROOT 出边和=2，但分段各=1。
    issues = _reject_reasons([
        _edge("E1", "ROOT", "A", begin="2024-01-01", until="2024-04-01", num=1, den=1),
        _edge("E2", "ROOT", "B", begin="2024-04-01", num=1, den=1),
    ])
    assert issues in (None, []), issues


# ---------------------------------------------------------------- 事务与版本

@check("规则导入事务：拒收后保留旧版可用规则")
def _():
    con = schema.connect(":memory:")
    svc = AllocationService(con)
    good = svc.load_rules([_edge("E1", "ROOT", "A", num=1, den=1)])
    before_versions = con.execute("SELECT COUNT(*) FROM alloc_rule_version").fetchone()[0]
    try:
        svc.load_rules([_edge("E2", "ROOT", "B", num=1, den=2)])
        raise AssertionError("应拒收")
    except RuleRejected:
        pass
    after_versions = con.execute("SELECT COUNT(*) FROM alloc_rule_version").fetchone()[0]
    assert before_versions == after_versions == 1
    assert svc.load_rules([_edge("E1", "ROOT", "A", num=1, den=1)]).version_id == good.version_id
    # 旧版仍可计算
    process_batch(con, [_assertion()])
    svc.load_items([dict(item_key="I1", person_key="P1", day="2024-02-01", amount=10)])
    rid = svc.compute(ASOF, version_id=good.version_id)
    assert dict(con.execute(
        "SELECT terminal, CAST(amount AS HUGEINT) FROM alloc_run_leaf "
        "WHERE run_id = ?", [rid]).fetchall()) == {"A": 10}


@check("固定摘要：等价集合（顺序/约分不同）返回同一版本")
def _():
    con = schema.connect(":memory:")
    svc = AllocationService(con)
    a = svc.load_rules([
        _edge("E1", "ROOT", "A", num=2, den=4),
        _edge("E2", "ROOT", "B", num=1, den=2),
    ])
    b = svc.load_rules([
        _edge("E2", "ROOT", "B", num=3, den=6),
        _edge("E1", "ROOT", "A", num=1, den=2),
    ])
    assert b.reused and b.version_id == a.version_id
    assert con.execute("SELECT COUNT(*) FROM alloc_rule_version").fetchone()[0] == 1


@check("结果绑定版本 + asof：后改规则不污染已存结果")
def _():
    con = schema.connect(":memory:")
    svc = AllocationService(con)
    process_batch(con, [_assertion()])
    svc.load_items([dict(item_key="I1", person_key="P1", day="2024-02-01", amount=100)])
    v1 = svc.load_rules([_edge("E1", "ROOT", "A", num=1, den=1)])
    run1 = svc.compute(ASOF, version_id=v1.version_id)

    v2 = svc.load_rules([_edge("F1", "ROOT", "B", num=1, den=1)])
    # 新规则计算得到不同结果
    run2 = svc.compute(ASOF, version_id=v2.version_id)
    assert dict(con.execute("SELECT terminal, CAST(amount AS HUGEINT) "
                            "FROM alloc_run_leaf WHERE run_id = ?", [run2]).fetchall()
                ) == {"B": 100}
    # 旧 run 仍完好，结果仍是 A
    assert dict(con.execute("SELECT terminal, CAST(amount AS HUGEINT) "
                            "FROM alloc_run_leaf WHERE run_id = ?", [run1]).fetchall()
                ) == {"A": 100}
    meta = con.execute("SELECT version_id FROM alloc_run WHERE run_id = ?",
                       [run1]).fetchone()
    assert meta[0] == v1.version_id


@check("事项幂等：同 key 同内容不重复累计；不同内容报冲突")
def _():
    con = schema.connect(":memory:")
    svc = AllocationService(con)
    svc.load_rules([_edge("E1", "ROOT", "A", num=1, den=1)])
    process_batch(con, [_assertion()])
    r1 = svc.load_items([dict(item_key="I1", person_key="P1", day="2024-02-01", amount=10)])
    assert (r1.inserted, r1.duplicates) == (1, 0)
    r2 = svc.load_items([dict(item_key="I1", person_key="P1", day="2024-02-01", amount=10)])
    assert (r2.inserted, r2.duplicates) == (0, 1)
    assert con.execute("SELECT COUNT(*) FROM alloc_item").fetchone()[0] == 1
    from bitemp.alloc import ItemConflict
    try:
        svc.load_items([dict(item_key="I1", person_key="P1", day="2024-02-01", amount=11)])
        raise AssertionError("应冲突")
    except ItemConflict as e:
        assert "I1" in str(e)
    # 冲突整批不落库
    assert con.execute("SELECT COUNT(*) FROM alloc_item").fetchone()[0] == 1


@check("试算不落库；正式计算落库")
def _():
    con = schema.connect(":memory:")
    svc = AllocationService(con)
    svc.load_rules([_edge("E1", "ROOT", "A", num=1, den=1)])
    process_batch(con, [_assertion()])
    svc.load_items([dict(item_key="I1", person_key="P1", day="2024-02-01", amount=10)])
    res = svc.trial(ASOF)
    assert res.items[0].total() == 10
    assert con.execute("SELECT COUNT(*) FROM alloc_run").fetchone()[0] == 0
    svc.compute(ASOF)
    assert con.execute("SELECT COUNT(*) FROM alloc_run").fetchone()[0] == 1


# ---------------------------------------------------------------- 精确性

@check("精确分数：1/3 + 1/3 + 1/3 之和为 1，无浮点误差，100 分不丢钱")
def _():
    con = schema.connect(":memory:")
    svc = AllocationService(con)
    svc.load_rules([
        _edge("E1", "ROOT", "A", num=1, den=3),
        _edge("E2", "ROOT", "B", num=1, den=3),
        _edge("E3", "ROOT", "C", num=1, den=3),
    ])
    process_batch(con, [_assertion()])
    svc.load_items([dict(item_key="I1", person_key="P1", day="2024-02-01", amount=100)])
    rid = svc.compute(ASOF)
    leaves = dict(con.execute("SELECT terminal, CAST(amount AS HUGEINT) "
                              "FROM alloc_run_leaf WHERE run_id = ?", [rid]).fetchall())
    assert sum(leaves.values()) == 100
    assert leaves["A"] == 34 and leaves["B"] == 33 and leaves["C"] == 33, leaves


@check("每笔事项末级金额之和严格等于原金额（含负数、含多路汇合）")
def _():
    con = schema.connect(":memory:")
    svc = AllocationService(con)
    svc.load_rules([
        _edge("E1", "ROOT", "L", num=1, den=2),
        _edge("E2", "ROOT", "R", num=1, den=2),
        _edge("E3", "L", "END1", num=2, den=3),
        _edge("E4", "L", "END2", num=1, den=3),
        _edge("E5", "R", "END1", num=1, den=1),
    ])
    process_batch(con, [_assertion()])
    svc.load_items([
        dict(item_key="P", person_key="P1", day="2024-02-01", amount=1000),
        dict(item_key="N", person_key="P1", day="2024-02-02", amount=-777),
        dict(item_key="Z", person_key="P1", day="2024-02-03", amount=0),
    ])
    rid = svc.compute(ASOF)
    rows = con.execute("""
        SELECT i.item_key, CAST(i.amount AS HUGEINT) AS amt,
               COALESCE(SUM(CAST(l.amount AS HUGEINT)), 0) AS leaf_sum
        FROM alloc_run_item i
        LEFT JOIN alloc_run_leaf l ON l.run_id = i.run_id AND l.item_key = i.item_key
        WHERE i.run_id = ? GROUP BY i.item_key, i.amount ORDER BY i.item_key
    """, [rid]).fetchall()
    for key, amt, leaf_sum in rows:
        assert amt == leaf_sum, (key, amt, leaf_sum)


# ---------------------------------------------------------------- 月度与解释

@check("按月 × 末级单元合计；unresolved 单列数量与金额")
def _():
    con = schema.connect(":memory:")
    svc = AllocationService(con)
    svc.load_rules([_edge("E1", "ROOT", "A", num=1, den=2),
                    _edge("E2", "ROOT", "B", num=1, den=2)])
    process_batch(con, [_assertion()])
    svc.load_items([
        dict(item_key="J1", person_key="P1", day="2024-01-15", amount=10),
        dict(item_key="F2", person_key="P1", day="2024-02-10", amount=20),
        dict(item_key="F3", person_key="P1", day="2024-02-20", amount=30),
        dict(item_key="U4", person_key="NOPE", day="2024-02-25", amount=99),
    ])
    rid = svc.compute(ASOF)
    roll = svc.monthly_rollup(rid)
    got = {(m, t): (amt, n) for m, t, amt, n in roll}
    # 10 -> 5/5；20 -> 10/10；30 -> 15/15
    assert got[("2024-01", "A")] == (5, 1) and got[("2024-01", "B")] == (5, 1)
    assert got[("2024-02", "A")] == (25, 2) and got[("2024-02", "B")] == (25, 2)
    u = svc.unresolved(rid)
    assert u["count"] == 1 and u["amount"] == 99


@check("从磁盘重新打开后解释结果保持一致")
def _():
    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, "alloc.duckdb")
    try:
        con = schema.connect(path)
        svc = AllocationService(con)
        svc.load_rules([_edge("E1", "ROOT", "L", num=1, den=2),
                        _edge("E2", "ROOT", "R", num=1, den=2),
                        _edge("E3", "L", "END", num=1, den=1),
                        _edge("E4", "R", "END", num=1, den=1)])
        process_batch(con, [_assertion()])
        svc.load_items([dict(item_key="I1", person_key="P1", day="2024-02-01", amount=101)])
        rid = svc.compute(ASOF)
        exp_before = svc.explain(rid, "I1", "END")
        con.close()

        con2 = schema.connect(path)
        svc2 = AllocationService(con2)
        exp_after = svc2.explain(rid, "I1", "END")
        assert exp_before["explanation"]["terminal_weight"] == Fraction(1)
        assert exp_after["explanation"]["terminal_weight"] == Fraction(1)
        assert exp_after["weight_matches_store"] is True
        assert exp_after["digest"] == exp_before["digest"]
        assert exp_after["explanation"]["nodes"]["L"]["forward"] == Fraction(1, 2)
        con2.close()
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------- 独立核验

@check("独立逐路径 oracle：小图（含分叉/汇合/多级）逐路径枚举对拍")
def _():
    edges = [
        goldens._edge("E1", "ROOT", "A", num=1, den=2),
        goldens._edge("E2", "ROOT", "B", num=1, den=2),
        goldens._edge("E3", "A", "X", num=2, den=5),
        goldens._edge("E4", "A", "Y", num=3, den=5),
        goldens._edge("E5", "B", "Y", num=1, den=3),
        goldens._edge("E6", "B", "Z", num=2, den=3),
        goldens._edge("E7", "Y", "END", num=1, den=1),
    ]
    parsed = [__import__("bitemp.alloc.model", fromlist=["Edge"]).Edge(
        e["edge_key"], e["src"], e["dst"], date.fromisoformat(e["begin"]),
        None, e["numerator"], e["denominator"]) for e in edges]
    day = date(2024, 2, 1)
    want = goldens.path_weights(parsed, day, "ROOT")
    got_prop = propagate(parsed, "ROOT")
    got = {t: got_prop.weight(t) for t in got_prop.terminals}
    assert want == got, (want, got)
    assert sum(got.values(), Fraction(0)) == 1
    # 金额也逐路径一致
    for amount in (100, 101, -101, 7, -7):
        want_amt = goldens.path_amounts(parsed, day, "ROOT", amount)
        con = schema.connect(":memory:")
        svc = AllocationService(con)
        svc.load_rules(edges)
        process_batch(con, [_assertion()])
        svc.load_items([dict(item_key="I", person_key="P1", day="2024-02-01",
                             amount=amount)])
        rid = svc.compute(ASOF)
        got_amt = dict(con.execute(
            "SELECT terminal, CAST(amount AS HUGEINT) FROM alloc_run_leaf "
            "WHERE run_id = ?", [rid]).fetchall())
        want_amt = {k: v for k, v in want_amt.items() if v != 0}
        assert got_amt == want_amt, (amount, want_amt, got_amt)


@check("随机无环网络守恒检查：末端权重和 = 1，且与逐路径枚举一致")
def _():
    for seed in range(12):
        edges, roots, terminals = random_dag(seed, n_nodes=9, n_edges=14)
        day = date(2024, 2, 1)
        for root in roots:
            prop = propagate(edges, root)
            total = sum((prop.weight(t) for t in prop.terminals), Fraction(0))
            assert total == 1, (seed, root, total)
            want = goldens.path_weights(edges, day, root)
            got = {t: prop.weight(t) for t in prop.terminals}
            assert want == got, (seed, root, want, got)


@check("30 层菱形：指数条路径不展开，按拓扑 DP 传播并合并权重")
def _():
    layers = 30
    edges = diamond_edges(layers, Fraction(1, 2))
    assert count_paths(layers) == 2 ** 30
    # 图规模小：节点数、边数线性
    nodes = {n for e in edges for n in (e.src, e.dst)}
    assert len(nodes) == 2 * layers + 3
    # 校验通过（每节点出边和 = 1）
    assert validate_edges(edges) == []
    prop = propagate(edges, "SRC")
    # 两个末级各得 1/2；且两个末级的总权重和为 1
    assert sorted(prop.terminals) == ["END-L", "END-R"]
    for t in prop.terminals:
        assert prop.weight(t) == Fraction(1, 2), (t, prop.weight(t))
    assert sum((prop.weight(t) for t in prop.terminals), Fraction(0)) == 1
    # 工作量是线性量级，远小于路径数
    assert prop.node_ops <= 2 * layers + 3
    assert prop.edge_ops < 10 * layers
    # 落库端到端：大额金额也严格守恒
    con = schema.connect(":memory:")
    svc = AllocationService(con)
    svc.load_rules([dict(edge_key=e.edge_key, src=e.src, dst=e.dst,
                         begin=e.begin.isoformat(), until=None,
                         numerator=e.numerator, denominator=e.denominator)
                    for e in edges])
    process_batch(con, [_assertion(person="P1", org="SRC")])
    svc.load_items([dict(item_key="BIG", person_key="P1", day="2024-02-01",
                         amount=999999999)])
    rid = svc.compute(ASOF)
    leaves = dict(con.execute("SELECT terminal, CAST(amount AS HUGEINT) "
                              "FROM alloc_run_leaf WHERE run_id = ?", [rid]).fetchall())
    assert sum(leaves.values()) == 999999999
    assert leaves == {"END-L": 500000000, "END-R": 499999999}
    meta = con.execute("SELECT node_ops, edge_ops FROM alloc_run WHERE run_id = ?",
                       [rid]).fetchone()
    assert meta[0] < 200 and meta[1] < 400


@check("跨日期段：校验在每段起点进行，问题日期落在活跃期")
def _():
    # 早期守恒、晚期不守恒 -> 报错日期为晚期段起点
    issues = _reject_reasons([
        _edge("E1", "ROOT", "A", begin="2024-01-01", until="2024-07-01", num=1, den=1),
        _edge("E2", "ROOT", "B", begin="2024-07-01", num=1, den=2),
        _edge("E3", "ROOT", "C", begin="2024-07-01", num=1, den=4),
    ])
    it = [i for i in issues if i.kind == "权重不守恒"][0]
    assert it.date == date(2024, 7, 1)
    assert set(it.edge_keys) == {"E2", "E3"}


@check("反向解释不变量：back[source] == forward[terminal]，末跳贡献之和 == 权重")
def _():
    from bitemp.alloc.model import Edge as _E
    edges = [_E("1", "S", "A", date(2024, 1, 1), None, 1, 2),
             _E("2", "S", "B", date(2024, 1, 1), None, 1, 2),
             _E("3", "A", "C", date(2024, 1, 1), None, 2, 3),
             _E("4", "A", "D", date(2024, 1, 1), None, 1, 3),
             _E("5", "B", "C", date(2024, 1, 1), None, 1, 1),
             _E("6", "C", "X", date(2024, 1, 1), None, 1, 2),
             _E("7", "C", "Y", date(2024, 1, 1), None, 1, 2),
             _E("8", "D", "Y", date(2024, 1, 1), None, 1, 1)]
    from bitemp.alloc.graph import explain_terminal
    p = propagate(edges, "S")
    for t in p.terminals:
        e = explain_terminal(p, t)
        assert e.back["S"] == p.weight(t), (t, e.back["S"], p.weight(t))
        assert sum((c for _edge, c in e.last_hop), Fraction(0)) == p.weight(t)
        assert e.through["S"] == p.weight(t)
    assert sum((p.weight(t) for t in p.terminals), Fraction(0)) == 1


if __name__ == "__main__":
    print("分摊管线验收测试：")
    print(f"\n全部通过（{len(PASSED)} 项）")
