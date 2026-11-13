"""产品 DuckDB 结果与独立参考实现的逐项对账，附手算期望值。

不涉及任何产品侧算法代码：通过 queries/store 只读库表，与 refcheck 的
独立重算结果比较；另含一份 datagen 小数据集的手算期望值，防止两套
实现同时犯同样的错。
"""
from __future__ import annotations

from . import queries, store
from .refcheck import ref_run

TOL = 1e-9


def _close(a: float, b: float) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(a - b) <= TOL * max(1.0, abs(a), abs(b))


# datagen 小数据集的手算期望值（秒 / core-seconds / byte-seconds / bytes）
# 缺测时段以 (相对 2026-11-13T00:00:00Z 的起始秒, 结束秒) 书写，
# 对账时把库中的 epoch 减去数据集基准 epoch 后比较。
BASE_EPOCH = 1794528000.0  # 2026-11-13T00:00:00+00:00

HAND_EXPECTED = {
    # n1: [10,20)cpu2/rss100 + [20,25)cpu4/rss200
    # n2: [35,40)缺测；[40,45) boot-A/boot-B 重叠，boot-A 字节序更小 ->
    #     cpu8/rss500；[45,50) 仅 boot-B -> cpu16/rss900
    "J_PREEMPT": {
        "status": "succeeded", "attempts": {
            "pa1": {"run": 30.0, "covered": 25.0, "cpu": 160.0,
                    "rss": 9000.0, "peak": 900.0,
                    "missing": [(35.0, 40.0)]}}},
    # rb1: [10,15)cpu2/rss50，[15,20)缺测
    # rb2: [30,40)cpu6/rss300，[40,45)缺测，[45,50)cpu10/rss400
    "J_RETRY": {
        "status": "succeeded", "attempts": {
            "rb1": {"run": 10.0, "covered": 5.0, "cpu": 10.0,
                    "rss": 250.0, "peak": 50.0, "missing": [(15.0, 20.0)]},
            "rb2": {"run": 20.0, "covered": 15.0, "cpu": 110.0,
                    "rss": 5000.0, "peak": 400.0,
                    "missing": [(40.0, 45.0)]}}},
    # 无 nT 采样窗：两段运行 [10,30)、[40,60) 全部缺测
    "J_TIE_OK": {"status": "succeeded", "attempts": {
        "ta1": {"run": 40.0, "covered": 0.0, "cpu": 0.0,
                "rss": 0.0, "peak": None,
                "missing": [(10.0, 30.0), (40.0, 60.0)]}}},
    "J_TIE_BAD": {"status": "invalid"},
    "J_STALE": {"status": "invalid"},
    "J_QUEUED": {"status": "queued"},
    "J_INCOMPLETE": {"status": "incomplete", "attempts": {
        "ia1": {"run": 10.0, "covered": 0.0, "cpu": 0.0,
                "rss": 0.0, "peak": None, "missing": [(10.0, 20.0)]}}},
    # nA 单窗 [5,45) 被运行 [10,40) 两端截断，全覆盖
    "J_ALPHA": {"status": "succeeded", "attempts": {
        "j_alphaa": {"run": 30.0, "covered": 30.0, "cpu": 30.0,
                     "rss": 300.0, "peak": 10.0, "missing": []}}},
    "J_BETA": {"status": "succeeded", "attempts": {
        "j_betaa": {"run": 30.0, "covered": 0.0, "cpu": 0.0,
                    "rss": 0.0, "peak": None,
                    "missing": [(10.0, 40.0)]}}},
    "J_OMEGA": {"status": "succeeded", "attempts": {
        "j_omegaa": {"run": 30.0, "covered": 0.0, "cpu": 0.0,
                    "rss": 0.0, "peak": None,
                    "missing": [(10.0, 40.0)]}}},
}

# 完整最长路径：三条总权 60s，按完整 job_id 字节序
HAND_PATHS = [
    (60.0, ["J_ALPHA", "J_OMEGA"]),
    (60.0, ["J_BETA", "J_OMEGA"]),
    (60.0, ["J_RETRY", "J_PREEMPT"]),
]


def check_against_reference(db_path: str, events_path: str, samples_path: str,
                            deps_path: str,
                            version_id: str | None = None) -> list[str]:
    problems: list[str] = []
    ref = ref_run(events_path, samples_path, deps_path)
    con = store.connect_readonly(db_path)
    try:
        vid = queries._resolve_version(con, version_id)

        # ---- job 状态
        db_jobs = {r[0]: r[1] for r in con.execute(
            "SELECT job_id, status FROM jobs WHERE version_id=?",
            [vid]).fetchall()}
        ref_jobs = {j: d["status"] for j, d in ref["jobs"].items()}
        if db_jobs != ref_jobs:
            problems.append(f"job 状态不一致: db={db_jobs} ref={ref_jobs}")

        # ---- 逐 attempt 核算
        for (job_id, aid), ra in sorted(ref["accounting"].items()):
            row = con.execute("""
                SELECT run_seconds, covered_seconds, coverage_ratio,
                       cpu_core_seconds, rss_byte_seconds, peak_rss_bytes,
                       outcome FROM attempts
                WHERE version_id=? AND job_id=? AND attempt_id=?
            """, [vid, job_id, aid]).fetchone()
            if row is None:
                problems.append(f"库中缺 attempt {job_id}/{aid}")
                continue
            run, cov, ratio, cpu, rss, peak, outcome = row
            for label, v, rv in (
                    ("run_seconds", run, ra["run_seconds"]),
                    ("covered_seconds", cov, ra["covered_seconds"]),
                    ("cpu_core_seconds", cpu, ra["cpu_core_seconds"]),
                    ("rss_byte_seconds", rss, ra["rss_byte_seconds"]),
                    ("peak_rss_bytes", peak, ra["peak_rss_bytes"]),
                    ("outcome", outcome, ra["outcome"])):
                if isinstance(rv, float):
                    if not _close(float(v), rv):
                        problems.append(
                            f"{job_id}/{aid} {label}: db={v} ref={rv}")
                elif v != rv:
                    problems.append(
                        f"{job_id}/{aid} {label}: db={v} ref={rv}")
            if not _close(ratio, ra["coverage_ratio"]):
                problems.append(
                    f"{job_id}/{aid} coverage_ratio: db={ratio} "
                    f"ref={ra['coverage_ratio']}")
            # 缺测时段并集长度
            db_miss = con.execute(
                "SELECT COALESCE(SUM(seconds),0) FROM missing_intervals "
                "WHERE version_id=? AND job_id=? AND attempt_id=?",
                [vid, job_id, aid]).fetchone()[0]
            ref_miss = sum(b - a for a, b in ra["missing"])
            if not _close(db_miss, ref_miss):
                problems.append(
                    f"{job_id}/{aid} 缺测总时长 db={db_miss} ref={ref_miss}")

        # ---- 依赖路径：参考枚举全部简单路径；产品只保留每个终点的最优
        # （含并列）路径。正确口径：产品每条路径都必须出现在参考枚举集合
        # 中，且产品内部顺序与参考的全局排序一致
        db_paths = [(r[1], r[2].split(",")) for r in con.execute(
            "SELECT path_rank, total_run_seconds, job_ids FROM critical_paths "
            "WHERE version_id=? AND complete=1 ORDER BY path_rank",
            [vid]).fetchall()]
        ref_paths = ref["paths"]["complete"]
        # 参考元素为 (total, [job_id...])；以 (total, tuple(seq)) 为键
        ref_set = {(t, tuple(seq)) for t, seq in ref_paths}
        db_keys = [(t, tuple(seq)) for t, seq in db_paths]
        if any(k not in ref_set for k in db_keys):
            problems.append(
                f"产品路径存在参考枚举之外的项: db={db_paths}")
        ref_order = {k: i for i, k in enumerate(ref_set)}
        # 产品序列应与参考全局 (total 降序, job_id 字节序列升序) 一致
        if db_keys != sorted(db_keys,
                             key=lambda k: (-k[0],
                                           tuple(x.encode("utf-8") for x in k[1]))):
            problems.append(
                f"产品路径排序与参考全局决胜顺序不一致: {db_paths}")

        # ---- 环：产品 dep_issues 只登记环上节点（下游污染由路径完整性体现）
        db_cycle = {r[2] for r in con.execute(
            "SELECT issue_code, detail, related_jobs FROM dep_issues "
            "WHERE version_id=? AND issue_code='dependency_cycle'",
            [vid]).fetchall()}
        ref_cycle_set = ref["paths"]["cyclic"]
        db_cycle_jobs: set[str] = set()
        for blob in db_cycle:
            db_cycle_jobs.update(blob.split(","))
        if db_cycle_jobs != ref_cycle_set:
            problems.append(
                f"依赖环节点不一致: db={db_cycle_jobs} ref={ref_cycle_set}")
        # 环的下游（J_C3）不得出现在任何完整路径里
        tainted_on_complete = con.execute(
            "SELECT COUNT(*) FROM critical_paths WHERE version_id=? "
            "AND complete=1 AND (job_ids LIKE '%J_C1%' OR job_ids LIKE '%J_C2%' "
            "OR job_ids LIKE '%J_C3%')", [vid]).fetchone()[0]
        if tainted_on_complete != 0:
            problems.append("环上/环下游节点混入了完整最长路径")
    finally:
        con.close()

    problems += _check_hand_expected(db_path, vid)
    return problems


def _check_hand_expected(db_path: str, vid: str) -> list[str]:
    problems: list[str] = []
    con = store.connect_readonly(db_path)
    try:
        for job_id, exp in HAND_EXPECTED.items():
            row = con.execute(
                "SELECT status FROM jobs WHERE version_id=? AND job_id=?",
                [vid, job_id]).fetchone()
            if row is None:
                problems.append(f"手算核对：库中缺 job {job_id}")
                continue
            if row[0] != exp["status"]:
                problems.append(
                    f"手算 {job_id} 状态={row[0]}，期望 {exp['status']}")
                continue
            for aid, ea in exp.get("attempts", {}).items():
                ar = con.execute("""
                    SELECT run_seconds, covered_seconds, cpu_core_seconds,
                           rss_byte_seconds, peak_rss_bytes
                    FROM attempts WHERE version_id=? AND job_id=? AND attempt_id=?
                """, [vid, job_id, aid]).fetchone()
                if ar is None:
                    problems.append(f"手算 {job_id}/{aid} 缺失")
                    continue
                run, cov, cpu, rss, peak = ar
                for label, v, key in (
                        ("run", run, "run"), ("covered", cov, "covered"),
                        ("cpu", cpu, "cpu"), ("rss", rss, "rss"),
                        ("peak", peak, "peak")):
                    ev = ea[key]
                    if ev is None:
                        if v is not None:
                            problems.append(
                                f"手算 {job_id}/{aid} {label}={v} 期望 None")
                    elif not _close(float(v), ev):
                        problems.append(
                            f"手算 {job_id}/{aid} {label}={v} 期望 {ev}")
                db_miss = sorted(
                    (a - BASE_EPOCH, b - BASE_EPOCH) for a, b in con.execute(
                        "SELECT from_epoch, to_epoch FROM missing_intervals "
                        "WHERE version_id=? AND job_id=? AND attempt_id=? "
                        "ORDER BY from_epoch",
                        [vid, job_id, aid]).fetchall())
                if db_miss != sorted(ea["missing"]):
                    problems.append(
                        f"手算 {job_id}/{aid} 缺测时段={db_miss} "
                        f"期望={sorted(ea['missing'])}")

        db_paths = [(r[1], r[2].split(",")) for r in con.execute(
            "SELECT path_rank, total_run_seconds, job_ids FROM critical_paths "
            "WHERE version_id=? AND complete=1 ORDER BY path_rank",
            [vid]).fetchall()]
        if db_paths[:len(HAND_PATHS)] != HAND_PATHS:
            problems.append(
                f"手算最长路径={db_paths[:len(HAND_PATHS)]} 期望={HAND_PATHS}")
    finally:
        con.close()
    return problems
