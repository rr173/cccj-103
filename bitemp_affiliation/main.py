#!/usr/bin/env python3
"""装配入口：生成数据 -> 分批装载 -> 双时态维表 -> 查询例子 -> oracle 对拍。

用法：
    python3 main.py demo    [--db bitemp.duckdb] [--seed 7]
    python3 main.py verify  [--db :memory:]      [--seed 99] [--batches 5]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bitemp import builder, datagen, queries, schema, verify  # noqa: E402
from bitemp.model import Assertion  # noqa: E402

UTC = timezone.utc


def show_dim(con, title):
    print(f"\n== {title} ==")
    rows = con.execute(
        "SELECT researcher_id, organization, eff_from, "
        "       COALESCE(eff_to::VARCHAR, '+∞') AS eff_to, "
        "       known_from, COALESCE(known_to::VARCHAR, 'now') AS known_to "
        "FROM dim_affiliation ORDER BY researcher_id, known_from, eff_from"
    ).fetchall()
    for r in rows:
        print(f"  {r[0]}  {r[1]:<12} eff=[{r[2]}, {r[3]})  known=[{r[4]}, {r[5]})")
    print(f"  共 {len(rows)} 行")


def show_report(rep: builder.BatchReport, title):
    print(f"\n== {title} ==")
    print(f"  收到 {rep.received} 条，新增 {rep.inserted} 条，"
          f"幂等重放 {rep.duplicates} 条，重建研究者 {rep.affected_researchers}")
    for rb in rep.rebuilds:
        print(f"  [{rb.researcher_id}] 新增区间 {len(rb.added)}，"
              f"消失区间 {len(rb.removed)}，变化区域 {rb.changed_regions}")
        for f in rb.facts_to_recompute:
            print(f"    需重算事实: {f[0]} event_date={f[2]} metric={f[3]}")


def cmd_demo(args):
    con = schema.connect(args.db)
    gen = datagen.Generator(seed=args.seed, n_researchers=4)

    print("— 第一批：初始断言（无替代）—")
    b1 = gen.batch(12, supersede_prob=0.0, infinite_tail_prob=0.3)
    schema.insert_facts(con, gen.facts(30))
    show_report(builder.process_batch(con, b1), "批 1 处理报告")

    print("\n— 第二批：晚到的更正（替代链 + 重叠补丁）—")
    b2 = gen.batch(10, supersede_prob=0.6, infinite_tail_prob=0.2)
    show_report(builder.process_batch(con, b2), "批 2 处理报告")

    show_dim(con, "双时态维表（全部）")

    rid = gen.researchers[0]
    now = datetime.now(UTC)
    month_end = datetime(2025, 6, 30, 23, 59, 59, tzinfo=UTC)
    # 取该研究者当前认知下的一条真实区间作为查询日期，保证演示有内容
    row = con.execute(
        "SELECT eff_from FROM dim_affiliation "
        "WHERE researcher_id = ? AND known_to IS NULL ORDER BY eff_from LIMIT 1",
        [rid]).fetchone()
    qdate = row[0] if row else datetime(2024, 6, 1).date()
    print(f"\n== 查询例子 ==")
    print(f"Q1 当前认知下 {rid} 在 {qdate} 属于哪里: "
          f"{queries.org_at(con, rid, qdate, now)}")
    print(f"Q2 截至 2025-06-30 月结时点，我们认为 {rid} 在 {qdate} 属于哪里: "
          f"{queries.org_at(con, rid, qdate, month_end)}")
    print("Q3 事实表按当前认知关联归属（前 8 行）:")
    for row in queries.facts_with_org(con, now)[:8]:
        org = row[4] if row[4] is not None else "未知"
        print(f"    {row[0]} {row[1]} {row[2]} metric={row[3]:.1f} -> {org}")

    print("\n== oracle 对拍（逐时点直接解释原始断言 vs 维表） ==")
    all_assertions = [Assertion.from_row(r) for r in gen.history]
    mism = verify.cross_check(con, all_assertions, gen.researchers)
    print(f"  探测点数: 每研究者 端点±1日 × recorded_at±1s；"
          f"断言数 {len(all_assertions)}")
    print(f"  结果: {'全部一致' if not mism else '不一致 ' + str(mism[:5])}")
    return 0


def cmd_verify(args):
    con = schema.connect(args.db)
    gen = datagen.Generator(seed=args.seed, n_researchers=6)
    for i in range(args.batches):
        batch = gen.batch(14, supersede_prob=0.4 if i else 0.0,
                          infinite_tail_prob=0.3)
        rep = builder.process_batch(con, batch)
        all_assertions = [Assertion.from_row(r) for r in gen.history]
        mism = verify.cross_check(con, all_assertions, gen.researchers)
        print(f"批 {i + 1}: 新增 {rep.inserted} 条，"
              f"重建 {len(rep.rebuilds)} 个研究者，"
              f"对拍 {'OK' if not mism else 'FAIL'}")
        if mism:
            print("\n".join(mism[:10]))
            return 1
    print("全部批次对拍通过")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("demo", "verify"):
        p = sub.add_parser(name)
        p.add_argument("--db", default=":memory:", help="DuckDB 路径（默认内存）")
        p.add_argument("--seed", type=int, default=7)
        p.add_argument("--batches", type=int, default=5)
    args = ap.parse_args()
    return cmd_demo(args) if args.cmd == "demo" else cmd_verify(args)


if __name__ == "__main__":
    raise SystemExit(main())
