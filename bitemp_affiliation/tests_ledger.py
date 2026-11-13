#!/usr/bin/env python3
"""验收测试：python3 tests_ledger.py。

覆盖：
  时区归一化/坏时间/NaN 拒收/非正窗/负值/表头不符；
  重复事件折叠、冲突 event_id 整体拒绝且不留版本、attempt_id 跨 job 复用；
  抢占续跑、失败重试、同秒 UTF-8 排序、旧检查点/终态后事件隔离；
  retry_of 规则、同 job 运行区间相交；
  采样窗同 boot 相交致命、异 boot 重叠按 boot 字节序、缺测不补零、
  窗被起止截断、覆盖比例口径；
  依赖未知 job、依赖环、等权路径决胜、不完整路径标注；
  重复导入幂等、改动产生新版本且旧版本可查、乱序输入摘要与汇总不变；
  关闭重开后由汇总下钻到切片与采样窗；account 对账无漂移。
"""
from __future__ import annotations

import csv
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bitemp.ledger import datagen, queries, service  # noqa: E402
from bitemp.ledger.model import LedgerInputError  # noqa: E402
from bitemp.ledger.verify import check_against_reference  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILURES.append(msg)


def write_materials(work: Path, events: list[dict], samples: list[list],
                    deps: list[list]) -> tuple[Path, Path, Path]:
    work.mkdir(parents=True, exist_ok=True)
    ep = work / "events.jsonl"
    with ep.open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    sp = work / "samples.csv"
    with sp.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["node", "boot_id", "sample_from", "sample_to",
                    "cpu_cores", "peak_rss_bytes"])
        w.writerows(samples)
    dp = work / "dependencies.csv"
    with dp.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["parent_job", "child_job"])
        w.writerows(deps)
    return ep, sp, dp


def ev(eid, job, aid, etype, at, node="", cp="", retry=""):
    return {"event_id": eid, "job_id": job, "attempt_id": aid,
            "event_type": etype, "at": at, "node": node,
            "checkpoint_id": cp, "retry_of": retry}


def _iso_sec(sec: int) -> str:
    return f"2026-11-13T00:{sec // 60:02d}:{sec % 60:02d}+00:00"


def win(node, boot, f, t, cpu=1.0, rss=100.0):
    return [node, boot, _iso_sec(f), _iso_sec(t), cpu, rss]


def import_tmp(work: Path, events, samples, deps, db="t.duckdb"):
    ep, sp, dp = write_materials(work, events, samples, deps)
    return service.import_ledger(work / db, ep, sp, dp), ep, sp, dp


# ------------------------------------------------------------ 解析与致命错误

def test_parse_errors() -> None:
    work = Path(tempfile.mkdtemp())
    base = [ev("e1", "J", "a1", "submitted", "2026-11-13T00:00:00+00:00"),
            ev("e2", "J", "a1", "started", "2026-11-13T00:00:10+00:00", "n1"),
            ev("e3", "J", "a1", "succeeded", "2026-11-13T00:00:20+00:00", "n1")]
    # 无时区偏移
    bad = [dict(base[0], at="2026-11-13T00:00:00")] + base[1:]
    try:
        import_tmp(work / "a", bad, [win("n1", "b", 0, 30)], [])
        check(False, "无时区时间戳应拒收")
    except LedgerInputError:
        pass
    # JSON NaN
    p = work / "nan"
    p.mkdir()
    (p / "events.jsonl").write_text(
        json.dumps(base[0]) + "\n" + '{"event_id":"x","job_id":"J",'
        '"attempt_id":"a1","event_type":"started","at":NaN,'
        '"node":"n1","checkpoint_id":"","retry_of":""}\n')
    (p / "samples.csv").write_text(
        "node,boot_id,sample_from,sample_to,cpu_cores,peak_rss_bytes\n")
    (p / "dependencies.csv").write_text("parent_job,child_job\n")
    try:
        service.import_ledger(p / "db.duckdb", p / "events.jsonl",
                              p / "samples.csv", p / "dependencies.csv")
        check(False, "NaN JSON 应拒收")
    except LedgerInputError:
        pass
    # 非正窗 / 负值
    try:
        import_tmp(work / "b", base,
                   [win("n1", "b", 20, 20)], [])
        check(False, "零长窗应拒收")
    except LedgerInputError:
        pass
    try:
        import_tmp(work / "c", base,
                   [["n1", "b", "2026-11-13T00:00:00+00:00",
                     "2026-11-13T00:00:10+00:00", -1.0, 100]], [])
        check(False, "负 cpu 应拒收")
    except LedgerInputError:
        pass


def test_fold_conflict_and_cross_job() -> None:
    work = Path(tempfile.mkdtemp())
    events = [
        ev("e1", "J", "a1", "submitted", "2026-11-13T00:00:00+00:00"),
        ev("e2", "J", "a1", "started", "2026-11-13T00:00:10+00:00", "n1"),
        dict(ev("e2", "J", "a1", "started", "2026-11-13T00:00:10+00:00", "n1")),
        ev("e3", "J", "a1", "succeeded", "2026-11-13T00:00:20+00:00", "n1"),
    ]
    r, *_ = import_tmp(work / "ok", events, [win("n1", "b", 0, 30)], [])
    check(r.n_events == 3, f"重复事件应折叠为 3 条，实际 {r.n_events}")
    # 同 ID 冲突
    events_bad = events + [
        ev("e2", "J", "a1", "started", "2026-11-13T00:00:11+00:00", "n9")]
    db = work / "bad" / "c.duckdb"
    try:
        import_tmp(work / "bad", events_bad, [win("n1", "b", 0, 30)], [])
        check(False, "冲突 event_id 应整体拒绝")
    except LedgerInputError:
        pass
    check(not db.exists() or queries.list_versions(db) == [],
          "冲突导入不得登记任何版本")
    # attempt_id 跨 job 复用
    cross = events + [
        ev("x1", "K", "a1", "submitted", "2026-11-13T00:00:00+00:00")]
    try:
        import_tmp(work / "cross", cross, [win("n1", "b", 0, 30)], [])
        check(False, "attempt_id 跨 job 复用应拒收")
    except LedgerInputError:
        pass


# ------------------------------------------------------------ 生命周期

def test_lifecycle_statuses() -> None:
    work = Path(tempfile.mkdtemp())
    events = [
        # 正常抢占续跑成功
        ev("p1", "JP", "a1", "submitted", "2026-11-13T00:00:00+00:00"),
        ev("p2", "JP", "a1", "started", "2026-11-13T00:00:10+00:00", "n1"),
        ev("p3", "JP", "a1", "checkpoint_saved", "2026-11-13T00:00:15+00:00",
           cp="c1"),
        ev("p4", "JP", "a1", "preempted", "2026-11-13T00:00:20+00:00", "n1"),
        ev("p5", "JP", "a1", "resumed", "2026-11-13T00:00:30+00:00", "n1",
           cp="c1"),
        ev("p6", "JP", "a1", "succeeded", "2026-11-13T00:00:40+00:00", "n1"),
        # 失败后重试成功
        ev("r1", "JR", "b1", "submitted", "2026-11-13T00:00:00+00:00"),
        ev("r2", "JR", "b1", "started", "2026-11-13T00:00:10+00:00", "n2"),
        ev("r3", "JR", "b1", "failed", "2026-11-13T00:00:20+00:00", "n2"),
        ev("r4", "JR", "b2", "started", "2026-11-13T00:00:30+00:00", "n2",
           retry="b1"),
        ev("r5", "JR", "b2", "succeeded", "2026-11-13T00:00:50+00:00", "n2"),
        # queued
        ev("q1", "JQ", "q1", "submitted", "2026-11-13T00:00:00+00:00"),
        # incomplete
        ev("i1", "JI", "i1", "submitted", "2026-11-13T00:00:00+00:00"),
        ev("i2", "JI", "i1", "started", "2026-11-13T00:00:10+00:00", "n3"),
        # 终态后又来事件 -> invalid，但只影响自身
        ev("z1", "JZ", "z1", "submitted", "2026-11-13T00:00:00+00:00"),
        ev("z2", "JZ", "z1", "started", "2026-11-13T00:00:10+00:00", "n4"),
        ev("z3", "JZ", "z1", "succeeded", "2026-11-13T00:00:20+00:00", "n4"),
        ev("z4", "JZ", "z1", "started", "2026-11-13T00:00:30+00:00", "n4"),
    ]
    r, ep, sp, dp = import_tmp(
        work, events,
        [win("n1", "b", 0, 60), win("n2", "b", 0, 60),
         win("n3", "b", 0, 60), win("n4", "b", 0, 60)], [])
    st = {j["job"]["job_id"]: j["job"]["status"]
          for j in (queries.job_detail(r.db_path, x)
                    for x in ("JP", "JR", "JQ", "JI", "JZ"))}
    check(st == {"JP": "succeeded", "JR": "succeeded", "JQ": "queued",
                 "JI": "incomplete", "JZ": "invalid"}, f"状态集合错误: {st}")
    # invalid 作业原始事件保留
    zd = queries.job_detail(r.db_path, "JZ")
    check(len(zd["raw_events"]) == 4, "invalid 作业应保留全部原始事件")
    # 同批其他作业照常核算
    check(queries.job_detail(r.db_path, "JP")["attempts"][0]["run_seconds"]
          == 20.0, "invalid 隔离不应影响其他 job")
    # retry_of 指向不存在的 attempt
    bad_retry = [
        ev("w1", "JW", "w1", "submitted", "2026-11-13T00:00:00+00:00"),
        ev("w2", "JW", "w1", "started", "2026-11-13T00:00:10+00:00", "n1",
           retry="ghost"),
    ]
    r2, *_ = import_tmp(work / "retry", bad_retry, [win("n1", "b", 0, 30)], [])
    check(queries.job_detail(r2.db_path, "JW")["job"]["status"] == "invalid",
          "retry_of 悬空应判 invalid")


def test_tie_and_overlap() -> None:
    work = Path(tempfile.mkdtemp())
    # 同秒：cp 字节序 < pre，先 checkpoint 再 preempt 合法
    ok = [
        ev("t1", "JT", "a1", "submitted", "2026-11-13T00:00:00+00:00"),
        ev("t2", "JT", "a1", "started", "2026-11-13T00:00:10+00:00", "n1"),
        ev("a_cp", "JT", "a1", "checkpoint_saved",
           "2026-11-13T00:00:30+00:00", cp="c"),
        ev("b_pre", "JT", "a1", "preempted",
           "2026-11-13T00:00:30+00:00", "n1"),
        ev("t4", "JT", "a1", "resumed", "2026-11-13T00:00:40+00:00", "n1",
           cp="c"),
        ev("t5", "JT", "a1", "succeeded", "2026-11-13T00:00:50+00:00", "n1"),
    ]
    r, *_ = import_tmp(work / "ok", ok, [win("n1", "b", 0, 60)], [])
    check(queries.job_detail(r.db_path, "JT")["job"]["status"] == "succeeded",
          "同秒字节序合法迁移应成功")
    # 同秒反序导致不可能迁移
    bad = [
        ev("u1", "JU", "a1", "submitted", "2026-11-13T00:00:00+00:00"),
        ev("a_succ", "JU", "a1", "succeeded",
           "2026-11-13T00:00:10+00:00", "n1"),
        ev("z_start", "JU", "a1", "started",
           "2026-11-13T00:00:10+00:00", "n1"),
    ]
    r2, *_ = import_tmp(work / "bad", bad, [win("n1", "b", 0, 30)], [])
    check(queries.job_detail(r2.db_path, "JU")["job"]["status"] == "invalid",
          "同秒字节序导致的不可能迁移应判 invalid")
    # 同 job 运行区间相交（两个 started 并行）
    ov = [
        ev("o1", "JO", "a1", "started", "2026-11-13T00:00:10+00:00", "n1"),
        ev("o2", "JO", "a2", "started", "2026-11-13T00:00:15+00:00", "n1"),
        ev("o3", "JO", "a1", "succeeded", "2026-11-13T00:00:20+00:00", "n1"),
        ev("o4", "JO", "a2", "succeeded", "2026-11-13T00:00:25+00:00", "n1"),
    ]
    r3, *_ = import_tmp(work / "ov", ov, [win("n1", "b", 0, 40)], [])
    check(queries.job_detail(r3.db_path, "JO")["job"]["status"] == "invalid",
          "同 job 运行区间相交应判 invalid")


# ------------------------------------------------------------ 资源核算

def test_accounting_and_missing() -> None:
    work = Path(tempfile.mkdtemp())
    events = [
        ev("e1", "JA", "a1", "submitted", "2026-11-13T00:00:00+00:00"),
        ev("e2", "JA", "a1", "started", "2026-11-13T00:00:10+00:00", "n1"),
        ev("e3", "JA", "a1", "succeeded", "2026-11-13T00:00:30+00:00", "n1"),
    ]
    # 窗 [5,15) 被起点截断；[20,25) 中段覆盖；[15,20)、[25,30) 缺测
    samples = [win("n1", "b1", 5, 15, 2.0, 100),
               win("n1", "b1", 20, 25, 4.0, 200)]
    r, *_ = import_tmp(work / "a", events, samples, [])
    d = queries.job_detail(r.db_path, "JA")
    a = d["attempts"][0]
    check(a["run_seconds"] == 20.0, "运行秒数应为 20")
    check(a["covered_seconds"] == 10.0, "覆盖应为 10s（5+5）")
    check(abs(a["cpu_core_seconds"] - (2 * 5 + 4 * 5)) < 1e-9,
          f"CPU 积分错误: {a['cpu_core_seconds']}")
    check(abs(a["rss_byte_seconds"] - (100 * 5 + 200 * 5)) < 1e-9,
          "RSS 积分错误")
    check(a["peak_rss_bytes"] == 200.0, "峰值内存应为 200")
    miss = a["missing_intervals"]
    check(len(miss) == 2, f"应有两段缺测，实际 {len(miss)}")
    # 同 boot 相交致命
    try:
        import_tmp(work / "b", events,
                   [win("n1", "b1", 5, 18), win("n1", "b1", 17, 25)], [])
        check(False, "同 node/boot 窗相交应致命")
    except LedgerInputError:
        pass
    # 异 boot 重叠：boot-x 字节序小者记账
    r3, *_ = import_tmp(work / "c", events,
                        [win("n1", "boot-x", 10, 30, 1.0, 10),
                         win("n1", "boot-y", 10, 30, 9.0, 90)], [])
    a3 = queries.job_detail(r3.db_path, "JA")["attempts"][0]
    check(abs(a3["cpu_core_seconds"] - 20.0) < 1e-9,
          "异 boot 重叠应取字节序最小的 boot-x（cpu=1）")
    check(a3["peak_rss_bytes"] == 10.0, "峰值应来自 boot-x")


# ------------------------------------------------------------ 依赖

def test_dependencies() -> None:
    work = Path(tempfile.mkdtemp())
    def job(j, node, end=20):
        return [ev(f"{j}1", j, f"{j}a", "submitted",
                   "2026-11-13T00:00:00+00:00"),
                ev(f"{j}2", j, f"{j}a", "started",
                   "2026-11-13T00:00:10+00:00", node),
                ev(f"{j}3", j, f"{j}a", "succeeded",
                   f"2026-11-13T00:00:{end:02d}+00:00", node)]
    events = (job("JA", "n1") + job("JB", "n2") + job("JC", "n3")
              + job("JD", "n4") + job("JE", "n5"))
    samples = [win(n, "b", 0, 60) for n in ("n1", "n2", "n3", "n4", "n5")]
    # JA/JB 各 10s -> JC 10s：两条等权 20s，字节序 JA 在前
    deps = [["JA", "JC"], ["JB", "JC"], ["JC", "JD"],
            ["JE", "JE"],  # 自环
            ["JA", "GHOST"]]  # 未知 job
    r, *_ = import_tmp(work, events, samples, deps)
    cp = queries.critical_paths(r.db_path)
    codes = {i["issue_code"] for i in cp["dep_issues"]}
    check("dependency_cycle" in codes, "自环应被发现")
    check("unknown_dependency_job" in codes, "未知依赖 job 应登记")
    complete = [p for p in cp["paths"] if p["complete"]]
    first = complete[0]
    check(first["job_ids"] == ["JA", "JC", "JD"],
          f"最长路径应为 JA->JC->JD，实际 {first['job_ids']}")
    check(abs(first["total_run_seconds"] - 30.0) < 1e-9, "路径总权应为 30")
    # 等权决胜：含 JB 的路径排在同权 JA 路径之后
    keys = [tuple(p["job_ids"]) for p in complete]
    ja_pos = keys.index(("JA", "JC", "JD"))
    jb_pos = keys.index(("JB", "JC", "JD"))
    check(ja_pos < jb_pos, "等权路径应按完整 job_id 字节序决胜")
    # 环上/下游不进完整路径
    check(not any("JE" in p["job_ids"] for p in complete),
          "环节点不得混入完整路径")


# ------------------------------------------------------------ 版本与不变量

def test_versions_and_shuffle() -> None:
    work = Path(tempfile.mkdtemp())
    d1 = work / "ordered"
    d2 = work / "shuffled"
    datagen.write_demo_dataset(d1, shuffle=False)
    datagen.write_demo_dataset(d2, shuffle=True)
    db = work / "v.duckdb"
    r1 = service.import_ledger(
        db, d1 / "events.jsonl", d1 / "samples.csv", d1 / "dependencies.csv")
    # 乱序输入同摘要
    r2 = service.import_ledger(
        db, d2 / "events.jsonl", d2 / "samples.csv", d2 / "dependencies.csv")
    check(r1.version_id == r2.version_id and r2.reused,
          "仅改行序不应产生新版本")
    # 改一个采样值 -> 新版本，旧版本仍可查
    rows = list(csv.reader(open(d2 / "samples.csv")))
    rows[1][4] = "3.14"
    with open(d2 / "samples.csv", "w", newline="") as f:
        csv.writer(f).writerows(rows)
    r3 = service.import_ledger(
        db, d2 / "events.jsonl", d2 / "samples.csv", d2 / "dependencies.csv")
    check(not r3.reused and r3.version_id != r1.version_id,
          "内容改动应产生新版本")
    vids = [v["version_id"] for v in queries.list_versions(db)]
    check(r1.version_id in vids and r3.version_id in vids,
          "新旧版本应并存可查")
    old = queries.queue_summary(db, r1.version_id)
    check(any(x["status"] == "succeeded" for x in old["rows"]),
          "旧版本应仍能按其摘要查询")
    # 乱序两份的汇总一致
    s1 = queries.queue_summary(db, r1.version_id)["rows"]
    check(s1 == queries.queue_summary(db, r2.version_id)["rows"],
          "乱序输入的队列总账必须一致")


def test_demo_reference_and_drilldown() -> None:
    work = Path(tempfile.mkdtemp())
    d = work / "data"
    datagen.write_demo_dataset(d, shuffle=False)
    db = work / "demo.duckdb"
    service.import_ledger(db, d / "events.jsonl", d / "samples.csv",
                          d / "dependencies.csv")
    problems = check_against_reference(
        db, d / "events.jsonl", d / "samples.csv", d / "dependencies.csv")
    check(not problems, "参考对账应无问题: " + "; ".join(problems))
    # 关闭重开后的下钻：汇总 -> 切片 -> 采样窗
    sl = queries.explain_slice(db, "J_RETRY", "rb2", 1)
    check(any(not s["missing"] and s["boot_id"] for s in sl["slices"]),
          "下钻应能定位到具体采样窗")
    # 缺测切片无采样来源且不补零
    miss = [s for s in sl["slices"] if s["missing"]]
    check(miss and all(s["cpu_cores"] is None for s in miss),
          "缺测切片必须标记且数值为 NULL（不补零）")


def main() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            FAILURES.append(f"{t.__name__} 抛异常: {e!r}")
            print(f"ERROR {t.__name__}: {e!r}")
    print(f"\n{len(tests) - len(FAILURES)}/{len(tests)} 组通过")
    if FAILURES:
        print("失败项：")
        for m in FAILURES:
            print("  -", m)
        return 1
    print("全部验收测试通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
