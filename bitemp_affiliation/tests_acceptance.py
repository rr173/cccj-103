#!/usr/bin/env python3
"""验收测试：直接运行 python3 tests_acceptance.py。

覆盖：嵌套重叠、跨时区等价时刻、无穷尾区间、晚到的替代链、同分决胜、
循环替代被拒、区间端点不重复归属、更正后仅列出真正受影响的事实、
整批拒绝原子性、幂等重放、知识时点恰等于 recorded_at、空档保持未知，
以及用逐时点直接解释原始断言的 oracle 对拍维表结果。
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bitemp import builder, datagen, queries, schema, verify  # noqa: E402
from bitemp.model import Assertion  # noqa: E402

UTC = timezone.utc
PASSED = []


def check(name):
    def deco(fn):
        fn()
        PASSED.append(name)
        print(f"  ✔ {name}")
    return deco


def fresh():
    return schema.connect(":memory:")


def dim_rows(con, rid):
    return con.execute(
        "SELECT organization, eff_from, eff_to, known_from, known_to "
        "FROM dim_affiliation WHERE researcher_id = ? ORDER BY known_from, eff_from",
        [rid]).fetchall()


def snapshot(con):
    return con.execute("SELECT * FROM dim_affiliation ORDER BY ALL").fetchall(), \
        con.execute("SELECT * FROM assertions ORDER BY ALL").fetchall()


# ---------------------------------------------------------------- 1. 嵌套重叠
@check("嵌套重叠：外层 X 被内层高 revision 的 Y 覆盖，两端仍为 X")
def _():
    con = fresh()
    builder.process_batch(con, [
        dict(assertion_id="A001", researcher_id="R1", valid_from="2024-01-01",
             valid_to="2025-01-01", organization="ORG-X",
             recorded_at="2024-01-05T00:00:00+00:00", revision=1, supersedes=None),
        dict(assertion_id="A002", researcher_id="R1", valid_from="2024-03-01",
             valid_to="2024-07-01", organization="ORG-Y",
             recorded_at="2024-01-06T00:00:00+00:00", revision=2, supersedes=None),
    ])
    now = datetime(2030, 1, 1, tzinfo=UTC)
    assert queries.org_at(con, "R1", date(2024, 2, 1), now) == "ORG-X"
    assert queries.org_at(con, "R1", date(2024, 5, 1), now) == "ORG-Y"
    assert queries.org_at(con, "R1", date(2024, 8, 1), now) == "ORG-X"
    # 当前认知下的有效区间应为三段
    cur = [r for r in dim_rows(con, "R1") if r[4] is None]
    assert [(r[0], r[1], r[2]) for r in cur] == [
        ("ORG-X", date(2024, 1, 1), date(2024, 3, 1)),
        ("ORG-Y", date(2024, 3, 1), date(2024, 7, 1)),
        ("ORG-X", date(2024, 7, 1), date(2025, 1, 1)),
    ], cur


# ---------------------------------------------------------------- 2. 跨时区等价时刻
@check("跨时区等价时刻：+08:00 与 UTC 的同一瞬间同时可见")
def _():
    con = fresh()
    # 两条 recorded_at 写法不同但表示同一 UTC 时刻 2024-02-01T00:00:00Z
    builder.process_batch(con, [
        dict(assertion_id="A001", researcher_id="R1", valid_from="2024-01-01",
             valid_to=None, organization="ORG-X",
             recorded_at="2024-02-01T08:00:00+08:00", revision=1, supersedes=None),
        dict(assertion_id="A002", researcher_id="R1", valid_from="2024-06-01",
             valid_to=None, organization="ORG-Y",
             recorded_at="2024-02-01T00:00:00+00:00", revision=1, supersedes=None),
    ])
    before = datetime(2024, 1, 31, 23, 59, 59, tzinfo=UTC)
    exact = datetime(2024, 2, 1, 0, 0, 0, tzinfo=UTC)
    assert queries.org_at(con, "R1", date(2024, 3, 1), before) is None
    # 等价时刻同时生效；revision 相同、recorded_at 相同 -> assertion_id 决胜
    assert queries.org_at(con, "R1", date(2024, 3, 1), exact) == "ORG-X"
    assert queries.org_at(con, "R1", date(2024, 7, 1), exact) == "ORG-Y"


# ---------------------------------------------------------------- 3. 无穷尾区间
@check("无穷尾区间：valid_to 为空覆盖到 +∞")
def _():
    con = fresh()
    builder.process_batch(con, [
        dict(assertion_id="A001", researcher_id="R1", valid_from="2024-01-01",
             valid_to=None, organization="ORG-X",
             recorded_at="2024-01-05T00:00:00+00:00", revision=1, supersedes=None),
    ])
    now = datetime(2030, 1, 1, tzinfo=UTC)
    assert queries.org_at(con, "R1", date(2040, 6, 1), now) == "ORG-X"
    cur = dim_rows(con, "R1")
    assert cur[0][2] is None  # eff_to 为 NULL


# ---------------------------------------------------------------- 4. 晚到的替代链
@check("晚到的替代链：认知随 recorded_at 演进，历史行不被修改")
def _():
    con = fresh()
    b1 = [dict(assertion_id="A001", researcher_id="R1", valid_from="2024-01-01",
               valid_to=None, organization="ORG-X",
               recorded_at="2024-01-10T00:00:00+00:00", revision=1, supersedes=None)]
    builder.process_batch(con, b1)
    # 晚到的更正：追溯改写 2024-03-01 起的归属
    b2 = [dict(assertion_id="A002", researcher_id="R1", valid_from="2024-03-01",
               valid_to=None, organization="ORG-Y",
               recorded_at="2024-06-10T00:00:00+00:00", revision=2,
               supersedes="A001")]
    builder.process_batch(con, b2)
    # 更晚的再更正
    b3 = [dict(assertion_id="A003", researcher_id="R1", valid_from="2024-05-01",
               valid_to=None, organization="ORG-Z",
               recorded_at="2024-09-10T00:00:00+00:00", revision=3,
               supersedes="A002")]
    builder.process_batch(con, b3)

    d = date(2024, 6, 1)
    assert queries.org_at(con, "R1", d, datetime(2024, 2, 1, tzinfo=UTC)) == "ORG-X"
    assert queries.org_at(con, "R1", d, datetime(2024, 7, 1, tzinfo=UTC)) == "ORG-Y"
    assert queries.org_at(con, "R1", d, datetime(2024, 10, 1, tzinfo=UTC)) == "ORG-Z"
    # 历史认知仍可查：A001 的区间行 known_to 被截断而非删除
    rows = dim_rows(con, "R1")
    assert any(r[0] == "ORG-X" and r[4] == datetime(2024, 6, 10, tzinfo=UTC)
               for r in rows), rows
    # 同一业务日期在不同知识时点答案不同 —— 双时态
    assert queries.org_at(con, "R1", date(2024, 4, 1),
                          datetime(2024, 2, 1, tzinfo=UTC)) == "ORG-X"
    # A3 替代 A2 后 A2 不再可见，而 A3 从 2024-05-01 起算，
    # 故 2024-04-01 在当前认知下成为空档（未知），不向前填充
    assert queries.org_at(con, "R1", date(2024, 4, 1),
                          datetime(2024, 10, 1, tzinfo=UTC)) is None


# ---------------------------------------------------------------- 5. 同分决胜
@check("同分决胜：revision、recorded_at 全同时按 assertion_id 字典序取大")
def _():
    con = fresh()
    builder.process_batch(con, [
        dict(assertion_id="A0001", researcher_id="R1", valid_from="2024-01-01",
             valid_to=None, organization="ORG-X",
             recorded_at="2024-02-01T08:00:00+08:00", revision=2, supersedes=None),
        dict(assertion_id="A0002", researcher_id="R1", valid_from="2024-01-01",
             valid_to=None, organization="ORG-Y",
             recorded_at="2024-02-01T00:00:00+00:00", revision=2, supersedes=None),
    ])
    now = datetime(2030, 1, 1, tzinfo=UTC)
    assert queries.org_at(con, "R1", date(2024, 3, 1), now) == "ORG-Y"  # A0002 胜


# ---------------------------------------------------------------- 6. 循环替代被拒
@check("循环替代被拒：整批拒绝，数据库无变化")
def _():
    con = fresh()
    gen = datagen.Generator(seed=1)
    builder.process_batch(con, gen.batch(8, supersede_prob=0))
    before = snapshot(con)
    try:
        builder.process_batch(con, gen.invalid_batch_cycle())
        raise AssertionError("应当抛出 BatchRejected")
    except builder.BatchRejected as e:
        assert any("环" in r for r in e.reasons), e.reasons
    assert snapshot(con) == before


# ---------------------------------------------------------------- 7. 区间端点不重复归属
@check("区间端点不重复归属：左闭右开，端点日只属于右侧区间")
def _():
    con = fresh()
    builder.process_batch(con, [
        dict(assertion_id="A001", researcher_id="R1", valid_from="2024-01-01",
             valid_to="2024-06-01", organization="ORG-X",
             recorded_at="2024-01-05T00:00:00+00:00", revision=1, supersedes=None),
        dict(assertion_id="A002", researcher_id="R1", valid_from="2024-06-01",
             valid_to="2024-09-01", organization="ORG-Y",
             recorded_at="2024-01-05T00:00:00+00:00", revision=1, supersedes=None),
    ])
    now = datetime(2030, 1, 1, tzinfo=UTC)
    assert queries.org_at(con, "R1", date(2024, 5, 31), now) == "ORG-X"
    assert queries.org_at(con, "R1", date(2024, 6, 1), now) == "ORG-Y"
    assert queries.org_at(con, "R1", date(2024, 9, 1), now) is None  # 右开之外
    # 任意一天至多命中一行
    dup = con.execute("""
        SELECT d.day, COUNT(*) AS n
        FROM (SELECT DATE '2024-01-01' + CAST(i AS INTEGER) AS day
              FROM range(0, 300) t(i)) d
        JOIN dim_affiliation v
          ON d.day >= v.eff_from AND (v.eff_to IS NULL OR d.day < v.eff_to)
         AND v.known_to IS NULL
        WHERE v.researcher_id = 'R1'
        GROUP BY d.day HAVING COUNT(*) > 1
    """).fetchall()
    assert dup == [], dup


# ---------------------------------------------------------------- 8. 更正后仅列出真正受影响的事实
@check("更正后仅列出真正受影响的事实，未受影响研究者不被重建")
def _():
    con = fresh()
    builder.process_batch(con, [
        dict(assertion_id="A001", researcher_id="R1", valid_from="2024-01-01",
             valid_to="2025-01-01", organization="ORG-X",
             recorded_at="2024-01-05T00:00:00+00:00", revision=1, supersedes=None),
        dict(assertion_id="A002", researcher_id="R2", valid_from="2024-01-01",
             valid_to="2025-01-01", organization="ORG-P",
             recorded_at="2024-01-05T00:00:00+00:00", revision=1, supersedes=None),
    ])
    schema.insert_facts(con, [
        dict(fact_id="F1", researcher_id="R1", event_date="2024-03-15", metric=1.0),  # 在变化区内
        dict(fact_id="F2", researcher_id="R1", event_date="2024-10-15", metric=2.0),  # 在变化区外
        dict(fact_id="F3", researcher_id="R2", event_date="2024-03-15", metric=3.0),  # 别人
    ])
    r2_dim_before = dim_rows(con, "R2")
    # 补丁式更正：R1 在 [2024-02-01, 2024-06-01) 归属由 X 改为 Y（更高 revision 覆盖）
    rep = builder.process_batch(con, [
        dict(assertion_id="A003", researcher_id="R1", valid_from="2024-02-01",
             valid_to="2024-06-01", organization="ORG-Y",
             recorded_at="2024-07-01T00:00:00+00:00", revision=2, supersedes=None),
    ])
    assert rep.affected_researchers == ["R1"]          # 只重建受影响研究者
    assert dim_rows(con, "R2") == r2_dim_before        # R2 维行原封不动
    rb = rep.rebuilds[0]
    assert rb.changed_regions == [(date(2024, 2, 1), date(2024, 6, 1))]
    hit = [f[0] for f in rb.facts_to_recompute]
    assert hit == ["F1"], hit                          # 只有真正落在变化区的事实


# ---------------------------------------------------------------- 9. 整批拒绝原子性
@check("整批拒绝：一批中混有无效断言则全部不落库")
def _():
    con = fresh()
    gen = datagen.Generator(seed=2)
    good = gen.batch(5, supersede_prob=0)
    builder.process_batch(con, good)
    before = snapshot(con)
    mixed = gen.batch(3, supersede_prob=0) + gen.invalid_batch_dangling_supersedes()
    try:
        builder.process_batch(con, mixed)
        raise AssertionError("应当抛出 BatchRejected")
    except builder.BatchRejected:
        pass
    assert snapshot(con) == before  # 无半批维表、无半批断言


# ---------------------------------------------------------------- 10. 幂等重放
@check("幂等：相同输入再次处理结果稳定")
def _():
    con = fresh()
    gen = datagen.Generator(seed=3)
    b = gen.batch(10)
    builder.process_batch(con, b)
    before = snapshot(con)
    rep = builder.process_batch(con, b)  # 原样重放
    assert rep.inserted == 0 and rep.duplicates == len(b) and rep.rebuilds == []
    assert snapshot(con) == before


# ---------------------------------------------------------------- 11. 知识时点恰等于 recorded_at
@check("知识时点恰等于 recorded_at 时包含该断言")
def _():
    con = fresh()
    rec = "2024-03-15T12:00:00+00:00"
    builder.process_batch(con, [
        dict(assertion_id="A001", researcher_id="R1", valid_from="2024-01-01",
             valid_to=None, organization="ORG-X", recorded_at=rec,
             revision=1, supersedes=None),
    ])
    k = datetime(2024, 3, 15, 12, 0, 0, tzinfo=UTC)
    assert queries.org_at(con, "R1", date(2024, 2, 1), k) == "ORG-X"
    assert queries.org_at(con, "R1", date(2024, 2, 1),
                          k - timedelta(seconds=1)) is None


# ---------------------------------------------------------------- 12. 空档保持未知
@check("空档保持未知，不向前填充")
def _():
    con = fresh()
    builder.process_batch(con, [
        dict(assertion_id="A001", researcher_id="R1", valid_from="2024-01-01",
             valid_to="2024-03-01", organization="ORG-X",
             recorded_at="2024-01-05T00:00:00+00:00", revision=1, supersedes=None),
    ])
    now = datetime(2030, 1, 1, tzinfo=UTC)
    assert queries.org_at(con, "R1", date(2024, 2, 1), now) == "ORG-X"
    assert queries.org_at(con, "R1", date(2024, 5, 1), now) is None


# ---------------------------------------------------------------- 13. 其他无效批
@check("其他无效批：跨研究者替代 / 坏区间 / 裸时间戳 / 分叉替代 均被拒")
def _():
    con = fresh()
    gen = datagen.Generator(seed=4)
    builder.process_batch(con, gen.batch(10, supersede_prob=0))
    for make in (gen.invalid_batch_cross_researcher,
                 gen.invalid_batch_bad_interval,
                 gen.invalid_batch_naive_timestamp):
        before = snapshot(con)
        try:
            builder.process_batch(con, make())
            raise AssertionError(f"{make.__name__} 应当被拒绝")
        except builder.BatchRejected:
            pass
        assert snapshot(con) == before
    # 分叉替代：两个后继 supersedes 同一断言
    target = gen.history[0]["assertion_id"]
    rid = gen.history[0]["researcher_id"]
    fork = [dict(assertion_id="FORK1", researcher_id=rid, valid_from="2024-01-01",
                 valid_to=None, organization="ORG-X",
                 recorded_at="2025-02-01T00:00:00+00:00", revision=2,
                 supersedes=target),
            dict(assertion_id="FORK2", researcher_id=rid, valid_from="2024-01-01",
                 valid_to=None, organization="ORG-Y",
                 recorded_at="2025-02-02T00:00:00+00:00", revision=2,
                 supersedes=target)]
    before = snapshot(con)
    try:
        builder.process_batch(con, fork)
        raise AssertionError("分叉替代应当被拒绝")
    except builder.BatchRejected as e:
        assert any("分叉" in r for r in e.reasons)
    assert snapshot(con) == before


# ---------------------------------------------------------------- 14. oracle 随机对拍
@check("oracle 对拍：逐时点直接解释原始断言 == 维表查询（多批含晚到更正）")
def _():
    con = fresh()
    gen = datagen.Generator(seed=2026, n_researchers=5)
    all_assertions: list[Assertion] = []
    for i in range(4):
        batch = gen.batch(14, supersede_prob=0.4 if i else 0.0,
                          infinite_tail_prob=0.3)
        builder.process_batch(con, batch)
        all_assertions = [Assertion.from_row(r) for r in gen.history]
        # 全量对拍（Python 侧成员判断）
        mism = verify.cross_check(con, all_assertions, gen.researchers)
        assert not mism, f"第 {i + 1} 批后对拍失败:\n" + "\n".join(mism[:10])
        # 抽样对拍（真实 SQL 查询路径）
        mism = verify.cross_check_sql(con, all_assertions, gen.researchers,
                                      sample=200)
        assert not mism, f"第 {i + 1} 批后 SQL 对拍失败:\n" + "\n".join(mism[:10])


if __name__ == "__main__":
    print("验收测试：")
    # 测试函数在装饰器中已逐个执行
    print(f"\n全部通过（{len(PASSED)} 项）")
