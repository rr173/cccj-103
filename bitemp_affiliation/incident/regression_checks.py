#!/usr/bin/env python3
"""事故回归检查：python3 incident/regression_checks.py [--work-dir D]。

用修复后的产品重建一组小材料，逐项断言：

  事故 A        首轮失败后后继仍在执行 -> incomplete；后继后来成功 -> succeeded
  事故 U/D      U 仅提交（queued）与 D 已结束之间的 U->D 不得标完整
  失败接失败    两轮都已终结 -> failed（allowed 终态）
  失败接成功    后继成功 -> succeeded
  后继遭抢占    后继 preempted 无结局 -> incomplete
  双父支路汇合  P1 是最长支路且已结束，P2 仅排队 -> H、H2 不完整；H 自身仍 succeeded
  坏祖先旁分支  invalid / queued / incomplete 祖先各自阻断后代；
                与之无前置联系的独立分支照常完整
  记账原义      权重、等长决胜、耗时数值不变；节点自身成败不被祖先改写
  维护命令      旧库审计 -> 重建 -> 重复执行无变化；事务回滚旧视图完整；
                其他版本不被顺带更新；原始事实与版本标识不变

每项图完整性都再用 incident/ancestor_audit.py 的“逐点全部祖先”口径
交叉核对（该口径不调用本次修复的归约与图计算函数）。
"""
from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bitemp.ledger import queries, service  # noqa: E402
from bitemp.ledger._bootstrap import ensure_vendor_duckdb  # noqa: E402

ensure_vendor_duckdb()
import duckdb  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILURES.append(msg)


def ev(i, job, aid, typ, sec, node="", cp="", retry=""):
    return {"event_id": i, "job_id": job, "attempt_id": aid, "event_type": typ,
            "at": f"2026-11-13T00:{sec // 60:02d}:{sec % 60:02d}+00:00",
            "node": node, "checkpoint_id": cp, "retry_of": retry}


def write_materials(work: Path, events, samples, deps):
    work.mkdir(parents=True, exist_ok=True)
    ep, sp, dp = work / "events.jsonl", work / "samples.csv", work / "dependencies.csv"
    with ep.open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    with sp.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["node", "boot_id", "sample_from", "sample_to",
                    "cpu_cores", "peak_rss_bytes"])
        w.writerows(samples)
    with dp.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["parent_job", "child_job"])
        w.writerows(deps)
    return ep, sp, dp


def import_and_assert(work: Path, events, samples, deps, *, dbname="t.duckdb"):
    ep, sp, dp = write_materials(work, events, samples, deps)
    r = service.import_ledger(work / dbname, ep, sp, dp)
    return r, ep, sp, dp


def statuses(db, vid=None) -> dict:
    con = duckdb.connect(db, read_only=True)
    try:
        vid = vid or queries._resolve_version(con, None)
        return {j: s for j, s in con.execute(
            "SELECT job_id, status FROM jobs WHERE version_id=?", [vid]).fetchall()}
    finally:
        con.close()


def paths(db, vid=None):
    return queries.critical_paths(db, vid)["paths"]


def run_ancestor_audit(db, vid, ep, sp, dp, export_dir=None) -> tuple[int, str]:
    args = [sys.executable, str(ROOT / "incident" / "ancestor_audit.py"),
            "--db", str(db), "--version", vid,
            "--events", str(ep), "--samples", str(sp), "--deps", str(dp)]
    if export_dir is not None:
        args += ["--export", str(export_dir)]
    proc = subprocess.run(args, capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


# ---------------------------------------------------------------- 事故 A
def test_incident_a(work: Path) -> None:
    events = [
        ev("a1", "A", "a1", "submitted", 0),
        ev("a2", "A", "a1", "started", 10, "n1"),
        ev("a3", "A", "a1", "failed", 20, "n1"),
        ev("a4", "A", "a2", "started", 30, "n1", retry="a1"),
        ev("a5", "A", "a2", "checkpoint_saved", 40, "n1", cp="ck1"),
    ]
    r, ep, sp, dp = import_and_assert(work / "a", events, [], [])
    check(queries.job_detail(r.db_path, "A")["job"]["status"] == "incomplete",
          "事故 A：后继仍在执行时首轮失败不得压过后继 -> incomplete")
    detail = queries.job_detail(r.db_path, "A")
    check(detail["job"]["is_terminal"] == 0, "事故 A：incomplete 不是终态")
    check([a["outcome"] for a in detail["attempts"]] == ["failed", "incomplete"],
          "事故 A：attempt 结局应为 failed / incomplete")
    # 后继后来成功 -> 正常汇总为成功
    events2 = events + [ev("a6", "A", "a2", "succeeded", 50, "n1")]
    r2, ep2, sp2, dp2 = import_and_assert(work / "a2", events2, [], [], dbname="u.duckdb")
    check(queries.job_detail(r2.db_path, "A")["job"]["status"] == "succeeded",
          "事故 A：后继成功后应汇总为 succeeded")
    # 后继日志在运行中途截止（不保存检查点）也须 incomplete
    events3 = events[:4]
    r3, ep3, sp3, dp3 = import_and_assert(work / "a3", events3, [], [], dbname="v.duckdb")
    check(queries.job_detail(r3.db_path, "A")["job"]["status"] == "incomplete",
          "事故 A：日志停在执行中须保留未结束含义")
    # 只有提交 -> queued，不伪造执行结局
    r4, ep4, sp4, dp4 = import_and_assert(
        work / "a4", [ev("q", "Q", "q1", "submitted", 0)], [], [], dbname="w.duckdb")
    check(queries.job_detail(r4.db_path, "Q")["job"]["status"] == "queued",
          "只有提交的记录仍属于排队")


# ---------------------------------------------------------------- 事故 U/D
def test_incident_ud(work: Path) -> None:
    events = [
        ev("u1", "U", "u1", "submitted", 0),
        ev("d1", "D", "d1", "submitted", 0),
        ev("d2", "D", "d1", "started", 10, "nD"),
        ev("d3", "D", "d1", "succeeded", 20, "nD"),
    ]
    r, ep, sp, dp = import_and_assert(
        work / "ud", events, [], [["U", "D"]])
    vid = r.version_id
    p = paths(r.db_path)
    complete = [x["job_ids"] for x in p if x["complete"]]
    incomplete = [x["job_ids"] for x in p if not x["complete"]]
    check(["U", "D"] not in complete, "事故 U/D：U->D 不得标完整")
    check(["D"] not in complete,
          "事故 U/D：把 U 从展示裁掉也不能证明 D 的整体前置已结束")
    check(["U", "D"] in incomplete and ["D"] in incomplete,
          f"事故 U/D：U->D 与 D 均应作为不完整线索出现，实际 {incomplete}")
    check(any(x["job_ids"] == ["U"] and not x["complete"] for x in p),
          "事故 U/D：U 自身排队，单点也不完整")
    st = statuses(r.db_path, vid)
    check(st == {"U": "queued", "D": "succeeded"},
          f"事故 U/D：节点自身状态不得被祖先改写，实际 {st}")
    rc, out = run_ancestor_audit(r.db_path, vid, ep, sp, dp)
    check(rc == 0, f"事故 U/D 独立祖先核对失败:\n{out}")


# ---------------------------------------------------------------- 失败矩阵
def test_failure_matrix(work: Path) -> None:
    events = [
        # 失败接失败：全部轮次终结 -> failed 具有终态
        ev("ff1", "J_FF", "a1", "submitted", 0),
        ev("ff2", "J_FF", "a1", "started", 10, "n1"),
        ev("ff3", "J_FF", "a1", "failed", 20, "n1"),
        ev("ff4", "J_FF", "a2", "started", 30, "n1", retry="a1"),
        ev("ff5", "J_FF", "a2", "failed", 40, "n1"),
        # 失败接成功
        ev("fs1", "J_FS", "b1", "submitted", 0),
        ev("fs2", "J_FS", "b1", "started", 10, "n2"),
        ev("fs3", "J_FS", "b1", "failed", 20, "n2"),
        ev("fs4", "J_FS", "b2", "started", 30, "n2", retry="b1"),
        ev("fs5", "J_FS", "b2", "succeeded", 50, "n2"),
        # 后继遭抢占且无结局
        ev("fp1", "J_FP", "c1", "submitted", 0),
        ev("fp2", "J_FP", "c1", "started", 10, "n3"),
        ev("fp3", "J_FP", "c1", "failed", 20, "n3"),
        ev("fp4", "J_FP", "c2", "started", 30, "n3", retry="c1"),
        ev("fp5", "J_FP", "c2", "checkpoint_saved", 40, "n3", cp="pck"),
        ev("fp6", "J_FP", "c2", "preempted", 50, "n3"),
    ]
    r, ep, sp, dp = import_and_assert(work / "fm", events, [], [])
    st = statuses(r.db_path)
    check(st["J_FF"] == "failed", "失败接失败：全部轮次终结 -> failed")
    check(st["J_FS"] == "succeeded", "失败接成功 -> succeeded")
    check(st["J_FP"] == "incomplete", "后继遭抢占无结局 -> incomplete")
    d = queries.job_detail(r.db_path, "J_FF")
    check(d["job"]["is_terminal"] == 1,
          "已失败且无后续活跃轮次的 job 具有终态")
    d = queries.job_detail(r.db_path, "J_FP")
    check(d["job"]["is_terminal"] == 0, "被抢占的后继不得被判终态")
    # 权重仍是全部 attempt 运行秒数之和（J_FF: 10s + 10s = 20s）
    con = duckdb.connect(r.db_path, read_only=True)
    try:
        w = con.execute(
            "SELECT SUM(run_seconds) FROM attempts WHERE job_id='J_FF'"
        ).fetchone()[0]
    finally:
        con.close()
    check(abs(w - 20.0) < 1e-9, f"J_FF 全部轮次运行秒数应为 20，实际 {w}")
    rc, out = run_ancestor_audit(r.db_path, r.version_id, ep, sp, dp)
    check(rc == 0, f"失败矩阵独立祖先核对失败:\n{out}")


# ---------------------------------------------------------------- 汇合与坏祖先
def test_merge_and_bad_ancestor(work: Path) -> None:
    events = [
        # 双父支路：P1 成功且 30s（最长支路），P2 仅排队（0s）。H 汇合，H2 后代。
        ev("p1a", "J_P1", "p1a1", "submitted", 0),
        ev("p1b", "J_P1", "p1a1", "started", 10, "n4"),
        ev("p1c", "J_P1", "p1a1", "succeeded", 40, "n4"),
        ev("p2a", "J_P2", "p2a1", "submitted", 0),
        ev("ha", "J_H", "ha1", "submitted", 0),
        ev("hb", "J_H", "ha1", "started", 10, "n5"),
        ev("hc", "J_H", "ha1", "succeeded", 20, "n5"),
        ev("h2a", "J_H2", "h2a1", "submitted", 0),
        ev("h2b", "J_H2", "h2a1", "started", 10, "n5"),
        ev("h2c", "J_H2", "h2a1", "succeeded", 20, "n5"),
        # 坏祖先：invalid / queued / incomplete 各自的成功后代
        ev("ba1", "J_BAD", "ba1", "submitted", 0),
        ev("ba2", "J_BAD", "ba1", "succeeded", 10, "n6"),
        ev("ba3", "J_BAD", "ba1", "started", 10, "n6"),
        ev("bc1", "J_BCHILD", "bc1", "submitted", 0),
        ev("bc2", "J_BCHILD", "bc1", "started", 10, "n6"),
        ev("bc3", "J_BCHILD", "bc1", "succeeded", 20, "n6"),
        ev("qu1", "J_QUEUE", "q1", "submitted", 0),
        ev("qc1", "J_QCHILD", "qc1", "submitted", 0),
        ev("qc2", "J_QCHILD", "qc1", "started", 10, "n7"),
        ev("qc3", "J_QCHILD", "qc1", "succeeded", 20, "n7"),
        ev("in1", "J_INC", "i1", "submitted", 0),
        ev("in2", "J_INC", "i1", "started", 10, "n8"),
        ev("ic1", "J_ICHLILD", "ic1", "submitted", 0),
        ev("ic2", "J_ICHLILD", "ic1", "started", 10, "n8"),
        ev("ic3", "J_ICHLILD", "ic1", "succeeded", 20, "n8"),
        # 无前置联系的独立正常链
        ev("ok1", "J_OK", "ok1", "submitted", 0),
        ev("ok2", "J_OK", "ok1", "started", 10, "n9"),
        ev("ok3", "J_OK", "ok1", "succeeded", 40, "n9"),
        ev("okc1", "J_OKCHILD", "okc1", "submitted", 0),
        ev("okc2", "J_OKCHILD", "okc1", "started", 10, "n9"),
        ev("okc3", "J_OKCHILD", "okc1", "succeeded", 20, "n9"),
    ]
    deps = [["J_P1", "J_H"], ["J_P2", "J_H"], ["J_H", "J_H2"],
            ["J_BAD", "J_BCHILD"], ["J_QUEUE", "J_QCHILD"],
            ["J_INC", "J_ICHLILD"], ["J_OK", "J_OKCHILD"]]
    r, ep, sp, dp = import_and_assert(work / "merge", events, [], deps)
    p = paths(r.db_path)
    complete = [x["job_ids"] for x in p if x["complete"]]
    incomplete = [x["job_ids"] for x in p if not x["complete"]]
    # 仅看与坏支路有关的点
    for bad in (["J_H"], ["J_H2"], ["J_P1", "J_H"], ["J_P1", "J_H", "J_H2"],
                ["J_BCHILD"], ["J_BAD", "J_BCHILD"],
                ["J_QCHILD"], ["J_QUEUE", "J_QCHILD"],
                ["J_ICHLILD"], ["J_INC", "J_ICHLILD"]):
        check(bad not in complete, f"汇合/坏祖先阻断：{bad} 不得标完整")
    check(["J_P1"] in complete, "最长支路自身 P1 已结束，单点应完整")
    check(["J_OK", "J_OKCHILD"] in complete,
          "与坏祖先无前置联系的独立链应继续完整")
    st = statuses(r.db_path)
    check(st["J_H"] == "succeeded" and st["J_H2"] == "succeeded",
          "节点自身成功不应被祖先状态改写")
    check(st["J_P1"] == "succeeded" and st["J_P2"] == "queued",
          "父支路自身状态保持原义")
    check(st["J_BAD"] == "invalid", "非法事件检查照常生效")
    rc, out = run_ancestor_audit(r.db_path, r.version_id, ep, sp, dp)
    check(rc == 0, f"汇合/坏祖先独立祖先核对失败:\n{out}")


# ---------------------------------------------------------------- 维护命令
LEGACY = Path(__file__).resolve().parent / "legacy_program"


def _have_legacy() -> bool:
    return (LEGACY / "main_ledger.py").is_file()


def test_repair_with_legacy_db(work: Path) -> None:
    if not _have_legacy():
        print(f"  (跳过：修复前程序快照 {LEGACY} 不存在)")
        return
    # 用修复前程序生成一份真实旧库（事故 A + 另一版本）
    src_a = ROOT / "incident" / "materials" / "a"
    src_ud = ROOT / "incident" / "materials" / "ud"
    db = work / "legacy.duckdb"
    for src in (src_a, src_ud):
        proc = subprocess.run(
            [sys.executable, str(LEGACY / "main_ledger.py"), "import",
             "--events", str(src / "events.jsonl"),
             "--samples", str(src / "samples.csv"),
             "--deps", str(src / "dependencies.csv"), "--db", str(db)],
            capture_output=True, text=True)
        check(proc.returncode == 0, f"旧程序导入失败：{proc.stderr}")
    vids = [v["version_id"] for v in queries.list_versions(db)]
    check(len(vids) == 2, "旧库应含两个版本")
    # 保留一份修复前的旧库产物，便于人工用 CLI 复演维护与回滚
    shutil.copy(db, work / "legacy_cli.duckdb")
    # 找出 A 的版本
    con = duckdb.connect(str(db), read_only=True)
    try:
        vid_a = con.execute(
            "SELECT version_id FROM jobs WHERE job_id='A'").fetchone()[0]
        vid_ud = next(v for v in vids if v != vid_a)
        before_ud = con.execute(
            "SELECT COUNT(*) FROM critical_paths WHERE version_id=?",
            [vid_ud]).fetchone()[0]
        before_raw = con.execute(
            "SELECT COUNT(*) FROM raw_events WHERE version_id=?", [vid_a]).fetchone()[0]
    finally:
        con.close()

    # 1) 审计（只读）：报告 jobs/queue_summary 需要重算，不写库
    audit = service.repair_version(db, vid_a, check_only=True)
    check(set(audit.tables_changed) == {"jobs", "queue_summary"},
          f"审计应定位 jobs/queue_summary，实际 {audit.tables_changed}")
    check(statuses(db, vid_a)["A"] == "failed", "审计不得写库")

    # 2) 维护：只更新该版本；A 变为 incomplete
    rep = service.repair_version(db, vid_a)
    check(statuses(db, vid_a)["A"] == "incomplete", "维护后 A 应为 incomplete")
    check(statuses(db, vid_ud) == {"U": "queued", "D": "succeeded"},
          "其他版本的状态不得被顺带更新")
    con = duckdb.connect(str(db), read_only=True)
    try:
        after_ud = con.execute(
            "SELECT COUNT(*) FROM critical_paths WHERE version_id=?",
            [vid_ud]).fetchone()[0]
        after_raw = con.execute(
            "SELECT COUNT(*) FROM raw_events WHERE version_id=?", [vid_a]).fetchone()[0]
        check(after_ud == before_ud, "其他版本的衍生行数不得变化")
        check(after_raw == before_raw, "维护不得改动原始事实")
    finally:
        con.close()

    # 3) 重复维护：无变化、不复制条目不累计数值
    before_counts = _table_counts(db)
    rep2 = service.repair_version(db, vid_a)
    check(rep2.tables_changed == [],
          f"重复维护应无变化，实际 {rep2.tables_changed}")
    check(_table_counts(db) == before_counts, "重复维护不得复制条目或累计数值")

    # 4) 事务回滚：注入写入中途故障，旧视图完整可读
    snap = work / "legacy_snapshot.duckdb"
    shutil.copy(db, snap)
    before = _snapshot(db)
    try:
        service.repair_version(db, vid_a, fault_after_jobs=0)
        check(False, "注入故障应抛出")
    except RuntimeError:
        pass
    check(_snapshot(db) == before,
          "事务回滚后旧视图必须与故障前完全一致")
    check(statuses(db, vid_ud) == {"U": "queued", "D": "succeeded"},
          "回滚不得影响其他版本")

    # 4b) 另造一份未修复的旧库，在其中执行维护并注入故障：
    #     回滚必须保住旧的（即使错误的）视图，不留下半新半旧。
    legacy = work / "legacy_buggy.duckdb"
    for src in (src_a, src_ud):
        proc = subprocess.run(
            [sys.executable, str(LEGACY / "main_ledger.py"), "import",
             "--events", str(src / "events.jsonl"),
             "--samples", str(src / "samples.csv"),
             "--deps", str(src / "dependencies.csv"), "--db", str(legacy)],
            capture_output=True, text=True)
        check(proc.returncode == 0, f"旧程序导入失败：{proc.stderr}")
    check(statuses(legacy, vid_a)["A"] == "failed",
          "未修复副本应先显示旧的错误结局")
    bad_before = _snapshot(legacy)
    try:
        service.repair_version(legacy, vid_a, fault_after_jobs=0)
        check(False, "未修复副本上注入故障应抛出")
    except RuntimeError:
        pass
    check(_snapshot(legacy) == bad_before,
          "回滚后未修复副本必须完整保留旧的错误视图（不留下半新半旧）")
    check(statuses(legacy, vid_a)["A"] == "failed", "回滚后结局仍是旧值")
    # 再执行维护成功，旧视图整体切到新视图
    rep_buggy = service.repair_version(legacy, vid_a)
    check(set(rep_buggy.tables_changed) == {"jobs", "queue_summary"},
          "未修复副本维护应只改 jobs/queue_summary")
    check(statuses(legacy, vid_a)["A"] == "incomplete", "未修复副本维护后应为 incomplete")

    # 回滚后仍可正常维护
    rep3 = service.repair_version(db, vid_a)
    check(rep3.tables_changed == [], "回滚后再次维护应无变化")

    # 5) 第二个版本同样按显式版本标识单独维护（演示作用范围）
    before_a_counts = _table_counts(db)
    rep_ud = service.repair_version(db, vid_ud)
    check(rep_ud.tables_changed == ["critical_paths"],
          f"U/D 版本应只需重算计算链，实际 {rep_ud.tables_changed}")
    ud_paths = paths(db, vid_ud)
    check(all(not x["complete"] for x in ud_paths),
          "U/D 版本维护后所有路径都不完整（U 未结束）")
    check(_table_counts(db)["critical_paths"] ==
          before_a_counts["critical_paths"],
          "维护 U/D 版本不得改变总行数（只替换该版本的行）")

    # 6) 单项详情 / 总账 / 路径 / 导出四者一致
    _assert_cross_surface_consistent(db, vid_a)
    rc, out = run_ancestor_audit(
        db, vid_a, src_a / "events.jsonl", src_a / "samples.csv",
        src_a / "dependencies.csv", export_dir=work / "export_a")
    check(rc == 0, f"维护后独立祖先核对失败:\n{out}")
    rc2, out2 = run_ancestor_audit(
        db, vid_ud, src_ud / "events.jsonl", src_ud / "samples.csv",
        src_ud / "dependencies.csv", export_dir=work / "export_ud")
    check(rc2 == 0, f"另一版本独立祖先核对失败:\n{out2}")


def _table_counts(db) -> dict:
    con = duckdb.connect(str(db), read_only=True)
    try:
        return {t: con.execute(
            f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("jobs", "attempts", "run_segments", "accounting_slices",
                      "missing_intervals", "critical_paths", "queue_summary",
                      "raw_events", "raw_samples", "raw_deps", "versions")}
    finally:
        con.close()


def _snapshot(db) -> list:
    con = duckdb.connect(str(db), read_only=True)
    try:
        return [
            con.execute(f"SELECT * FROM {t} ORDER BY 1,2").fetchall()
            for t in ("jobs", "attempts", "run_segments", "accounting_slices",
                      "missing_intervals", "critical_paths", "queue_summary")
        ]
    finally:
        con.close()


def _assert_cross_surface_consistent(db, vid: str) -> None:
    from bitemp.ledger import exporters
    out = Path(db).parent / "export_cross"
    exporters.export_version(str(db), str(out), vid)
    summary = queries.queue_summary(db, vid)
    jobs_rows = list(csv.DictReader(open(out / "jobs.csv", encoding="utf-8")))
    # 分类总账 job 数 = jobs.csv 行数
    check(sum(1 for _ in jobs_rows) == sum(r["job_count"] for r in summary["rows"]),
          "分类总账的 job 数应等于 jobs.csv 行数")
    # 单项详情与 jobs.csv 一致
    for r in jobs_rows:
        d = queries.job_detail(db, r["job_id"], vid)["job"]
        check(d["status"] == r["status"], f"{r['job_id']} 单项详情与导出不一致")
    # 计算链列表与 critical_paths.csv 一致
    cp_rows = list(csv.DictReader(open(out / "critical_paths.csv", encoding="utf-8")))
    api = queries.critical_paths(db, vid)["paths"]
    check(len(cp_rows) == len(api), "计算链列表与导出条数不一致")
    for row, p in zip(cp_rows, api):
        check(row["job_ids"].split(",") == p["job_ids"]
              and (row["complete"] == "True") == p["complete"],
              "计算链列表与导出内容不一致")


# ---------------------------------------------------------------- 等长决胜原义
def test_tie_break_unchanged(work: Path) -> None:
    events = []
    for j, n in (("J_ALPHA", "nA"), ("J_BETA", "nB"), ("J_OMEGA", "nO")):
        events += [ev(f"{j}-1", j, f"{j}a", "submitted", 0),
                   ev(f"{j}-2", j, f"{j}a", "started", 10, n),
                   ev(f"{j}-3", j, f"{j}a", "succeeded", 40, n)]
    deps = [["J_ALPHA", "J_OMEGA"], ["J_BETA", "J_OMEGA"]]
    r, ep, sp, dp = import_and_assert(work / "tie", events, [], deps)
    p = paths(r.db_path)
    complete = [(x["total_run_seconds"], x["job_ids"]) for x in p if x["complete"]]
    check(complete[:2] == [(60.0, ["J_ALPHA", "J_OMEGA"]),
                           (60.0, ["J_BETA", "J_OMEGA"])],
          f"等长路径应保持字节序决胜原义，实际 {complete[:2]}")
    check(all(x["total_run_seconds"] == sum(30.0 for _ in x["job_ids"])
              for x in p if len(x["job_ids"]) == 2),
          "路径总权仍为各节点全部 attempt 运行秒数之和")


def main() -> int:
    work = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(
        prefix="ledger_incident_"))
    work.mkdir(parents=True, exist_ok=True)
    tests = [test_incident_a, test_incident_ud, test_failure_matrix,
             test_merge_and_bad_ancestor, test_tie_break_unchanged,
             test_repair_with_legacy_db]
    for t in tests:
        try:
            t(work)
            print(f"PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            FAILURES.append(f"{t.__name__} 抛异常: {e!r}")
            print(f"ERROR {t.__name__}: {e!r}")
    print(f"\n{len(tests) - len({f.split(' ')[0] for f in FAILURES})}/{len(tests)}"
          f" 组通过（工作目录 {work}）")
    if FAILURES:
        print("失败项：")
        for m in FAILURES:
            print("  -", m)
        return 1
    print("事故回归检查全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
