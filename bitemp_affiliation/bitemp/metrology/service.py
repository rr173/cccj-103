"""测量不确定度处理的高层入口：装载目录 -> 计算 -> 落库 -> 解释/导出。

典型用法
--------
    summary = run("examples/metrology", "metrology.duckdb")   # 装载+计算+落库
    info = explain("metrology.duckdb", "S1", "Lsum")          # 单指标解释
    export("metrology.duckdb", "out/")                        # 导出明细
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from . import compute, inputs, store
from .compute import IndicatorResult, SpecimenOutcome
from .units import dim_to_string

CONVENTION_NOTES = [
    "内部坐标为 SI 基本单位（m, s, kg, K）；显示单位只影响输出数值与 u，不改变计算依据",
    "degC 为绝对摄氏温度：值加 273.15 转换为 K，u 不加偏移",
    "未给出的非对角相关系数按 0 处理（本程序约定），对角线恒为 1",
    f"相关矩阵半正定容差: 最小特征值 >= -{compute.correl.EIG_TOL:.0e}；"
    "容差内负特征值按 0 修正并在 specimen_notes 注明",
    f"合成方差负值容差: |var| <= {compute.VAR_REL_TOL:.0e} × 各方差贡献绝对值之和时按 0 处理并注明；"
    "明显负值记 error，不取绝对值",
]


# ---------------------------------------------------------------- 运行
def run(directory: str | Path, db_path: str = ":memory:",
        *, con=None) -> dict:
    """装载目录、计算全部 specimen、落库。配置错误抛 ConfigFailure。"""
    directory = Path(directory)
    config = inputs.load_config(directory)          # 配置错误在此抛出（读数计算前）
    data = inputs.load_inputs(directory)

    if con is None:
        con = store.connect(str(db_path))

    # 按 specimen 归集（measurements 与 correlations 的并集）
    by_specimen: dict[str, dict[str, list]] = {}
    for r in data.measurements:
        by_specimen.setdefault(r.specimen, {"m": [], "c": []})["m"].append(r)
    for r in data.correlations:
        by_specimen.setdefault(r.specimen, {"m": [], "c": []})["c"].append(r)

    # 先算完（纯内存），再一次事务落库：失败不留半成品
    outcomes: dict[str, SpecimenOutcome] = {}
    for specimen in sorted(by_specimen):
        rows = by_specimen[specimen]
        outcomes[specimen] = compute.process_specimen(
            config, specimen, rows["m"], rows["c"])

    con.execute("BEGIN")
    try:
        store.reset(con)
        store.set_meta(con, "input_dir", str(directory))
        store.set_meta(con, "conventions",
                       json.dumps(CONVENTION_NOTES, ensure_ascii=False))
        store.set_meta(con, "eig_tol", repr(compute.correl.EIG_TOL))
        store.set_meta(con, "var_rel_tol", repr(compute.VAR_REL_TOL))
        for v in config.variables.values():
            con.execute("INSERT INTO variables VALUES (?, ?, ?)",
                        [v.name, v.dimension, v.line])
        for ind in config.indicators:
            con.execute("INSERT INTO indicators VALUES (?, ?, ?, ?, ?)",
                        [ind.name, ind.expression, ind.unit_text,
                         dim_to_string(ind.dim), ind.line])
        for r in data.measurements:
            con.execute("INSERT INTO raw_measurements VALUES (?, ?, ?, ?, ?, ?)",
                        [r.specimen, r.variable, r.value_text, r.unit, r.u_text, r.line])
        for r in data.correlations:
            con.execute("INSERT INTO raw_correlations VALUES (?, ?, ?, ?, ?)",
                        [r.specimen, r.left, r.right, r.rho_text, r.line])
        for specimen in sorted(outcomes):
            _persist_outcome(con, config, outcomes[specimen])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise

    summary = {"specimens": 0, "rejected": 0, "ok": 0,
               "indicators_ok": 0, "indicators_unavailable": 0,
               "indicators_error": 0, "outcomes": outcomes, "config": config}
    for specimen, outcome in outcomes.items():
        summary["specimens"] += 1
        if outcome.status == "rejected":
            summary["rejected"] += 1
        else:
            summary["ok"] += 1
            for res in outcome.results.values():
                key = f"indicators_{res.status}"
                summary[key] = summary.get(key, 0) + 1
    return summary


def _persist_outcome(con, config: inputs.Config, outcome: SpecimenOutcome) -> None:
    if outcome.status == "rejected":
        reasons = [{"message": r.message, "file": r.file, "line": r.line}
                   for r in outcome.reject_reasons]
        con.execute("INSERT INTO specimen_status VALUES (?, 'rejected', ?)",
                    [outcome.specimen, json.dumps(reasons, ensure_ascii=False)])
        return
    con.execute("INSERT INTO specimen_status VALUES (?, 'ok', '[]')",
                [outcome.specimen])
    for note in outcome.notes:
        con.execute("INSERT INTO specimen_notes VALUES (?, ?)",
                    [outcome.specimen, note])
    for ind in config.indicators:
        res = outcome.results[ind.name]
        if res.status == compute.STATUS_OK:
            value_disp = ind.display.from_si(res.value_si)
            u_disp = ind.display.u_from_si(res.u_si)
            con.execute(
                "INSERT INTO results VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
                [outcome.specimen, ind.name, "ok", res.value_si, res.u_si,
                 value_disp, u_disp, ind.unit_text,
                 json.dumps(res.notes, ensure_ascii=False)])
            for var_name in sorted(res.grad):
                con.execute("INSERT INTO gradients VALUES (?, ?, ?, ?)",
                            [outcome.specimen, ind.name, var_name,
                             res.grad[var_name]])
            for c in res.contributions:
                con.execute("INSERT INTO contributions VALUES (?, ?, ?, ?, ?, ?)",
                            [outcome.specimen, ind.name, c.kind, c.left, c.right,
                             c.amount])
        else:
            con.execute(
                "INSERT INTO results VALUES (?, ?, ?, NULL, NULL, NULL, NULL, ?, ?, ?, ?)",
                [outcome.specimen, ind.name, res.status, ind.unit_text,
                 res.reason, json.dumps(res.chain, ensure_ascii=False),
                 json.dumps(res.notes, ensure_ascii=False)])
            # error 状态可能已算出梯度与分解（如明显负方差），一并落库便于诊断
            for var_name in sorted(res.grad):
                con.execute("INSERT INTO gradients VALUES (?, ?, ?, ?)",
                            [outcome.specimen, ind.name, var_name,
                             res.grad[var_name]])
            for c in res.contributions:
                con.execute("INSERT INTO contributions VALUES (?, ?, ?, ?, ?, ?)",
                            [outcome.specimen, ind.name, c.kind, c.left, c.right,
                             c.amount])


# ---------------------------------------------------------------- 解释
def explain(db_path: str, specimen: str, indicator: str, *, con=None) -> dict:
    """解释单个 specimen/指标：值、u、梯度、方差分解、状态与原因。"""
    if con is None:
        con = store.connect(str(db_path))
    ind = con.execute(
        "SELECT name, expression, unit, dim, line FROM indicators WHERE name = ?",
        [indicator]).fetchone()
    if ind is None:
        raise KeyError(f"未知指标 {indicator!r}")
    status_row = con.execute(
        "SELECT status, reasons FROM specimen_status WHERE specimen = ?",
        [specimen]).fetchone()
    if status_row is None:
        raise KeyError(f"未知标本 {specimen!r}")
    info = {
        "specimen": specimen,
        "indicator": indicator,
        "expression": ind[1],
        "unit": ind[2],
        "dim": ind[3],
        "formula_line": ind[4],
        "specimen_status": status_row[0],
        "specimen_reject_reasons": json.loads(status_row[1]),
        "specimen_notes": [r[0] for r in con.execute(
            "SELECT note FROM specimen_notes WHERE specimen = ? ORDER BY note",
            [specimen]).fetchall()],
    }
    if status_row[0] == "rejected":
        info["status"] = "rejected"
        return info
    row = con.execute(
        "SELECT status, value_si, u_si, value_display, u_display, reason, chain, notes "
        "FROM results WHERE specimen = ? AND indicator = ?",
        [specimen, indicator]).fetchone()
    if row is None:
        raise KeyError(f"标本 {specimen!r} 没有指标 {indicator!r} 的结果")
    info["status"] = row[0]
    info["notes"] = json.loads(row[7])
    if row[0] == "ok":
        info.update(value_si=row[1], u_si=row[2],
                    value=row[3], u=row[4],
                    gradients=[(r[0], r[1]) for r in con.execute(
                        "SELECT variable, grad_si FROM gradients "
                        "WHERE specimen = ? AND indicator = ? ORDER BY variable",
                        [specimen, indicator]).fetchall()],
                    contributions=[(r[0], r[1], r[2], r[3]) for r in con.execute(
                        "SELECT kind, left_var, right_var, amount_si FROM contributions "
                        "WHERE specimen = ? AND indicator = ? "
                        "ORDER BY CASE kind WHEN 'diag' THEN 0 ELSE 1 END, "
                        "left_var, right_var",
                        [specimen, indicator]).fetchall()])
        info["variance_si"] = row[2] ** 2
        info["contribution_sum"] = sum(c[3] for c in info["contributions"])
    else:
        info["reason"] = row[5]
        info["chain"] = json.loads(row[6])
    return info


def explain_text(db_path: str, specimen: str, indicator: str, *, con=None) -> str:
    info = explain(db_path, specimen, indicator, con=con)
    out = [f"标本 {info['specimen']}  指标 {info['indicator']}"
           f"  （formulas.json 第 {info['formula_line']} 行: "
           f"{info['expression']}，显示单位 {info['unit']}，量纲 {info['dim']}）"]
    if info["specimen_status"] == "rejected":
        out.append("  标本已被拒收:")
        for r in info["specimen_reject_reasons"]:
            loc = f"{r['file']}:{r['line']}" if r.get("line") else r["file"]
            out.append(f"    - [{loc}] {r['message']}")
        return "\n".join(out)
    for note in info["specimen_notes"]:
        out.append(f"  标本注记: {note}")
    status = info["status"]
    if status == "ok":
        out.append(f"  值: {info['value']!r} {info['unit']}  ±  {info['u']!r} {info['unit']}")
        out.append(f"  （SI: {info['value_si']!r} ± {info['u_si']!r}）")
        out.append("  对原始量的偏导（SI 坐标）:")
        for name, g in info["gradients"]:
            out.append(f"    ∂/∂{name} = {g!r}")
        out.append("  方差分解（SI，diag=对角贡献，cross=两倍交叉贡献，可负）:")
        for kind, left, right, amount in info["contributions"]:
            if kind == "diag":
                out.append(f"    diag  {left}: {amount!r}")
            else:
                out.append(f"    cross {left} × {right}: {amount!r}")
        out.append(f"  贡献之和 = {info['contribution_sum']!r}，"
                   f"合成方差 u² = {info['variance_si']!r}")
    else:
        out.append(f"  状态: {status}")
        out.append(f"  原因: {info['reason']}")
        out.append(f"  依赖链: {' -> '.join(info['chain'])}")
    for note in info.get("notes", []):
        out.append(f"  指标注记: {note}")
    return "\n".join(out)


# ---------------------------------------------------------------- 导出
def export(db_path: str, out_dir: str | Path, *, con=None) -> list[Path]:
    """导出数值、u、可用状态与异常明细；返回写出的文件列表。"""
    if con is None:
        con = store.connect(str(db_path))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def write_csv(name: str, header: list[str], rows) -> None:
        path = out_dir / name
        with path.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
        written.append(path)

    write_csv("results.csv",
              ["specimen", "indicator", "status", "value", "u", "unit",
               "value_si", "u_si", "reason", "chain"],
              con.execute(
                  "SELECT specimen, indicator, status, value_display, u_display, "
                  "unit, value_si, u_si, reason, chain FROM results "
                  "ORDER BY specimen, indicator").fetchall())
    write_csv("gradients.csv",
              ["specimen", "indicator", "variable", "grad_si"],
              con.execute(
                  "SELECT specimen, indicator, variable, grad_si FROM gradients "
                  "ORDER BY specimen, indicator, variable").fetchall())
    write_csv("contributions.csv",
              ["specimen", "indicator", "kind", "left_var", "right_var", "amount_si"],
              con.execute(
                  "SELECT specimen, indicator, kind, left_var, right_var, amount_si "
                  "FROM contributions ORDER BY specimen, indicator, kind, left_var, right_var").fetchall())
    write_csv("specimen_status.csv",
              ["specimen", "status"],
              con.execute("SELECT specimen, status FROM specimen_status "
                          "ORDER BY specimen").fetchall())

    exceptions = {
        "rejected_specimens": [
            {"specimen": r[0],
             "reasons": json.loads(r[1])}
            for r in con.execute(
                "SELECT specimen, reasons FROM specimen_status "
                "WHERE status = 'rejected' ORDER BY specimen").fetchall()],
        "failed_indicators": [
            {"specimen": r[0], "indicator": r[1], "status": r[2],
             "reason": r[3], "chain": json.loads(r[4]),
             "formula_line": r[5]}
            for r in con.execute(
                "SELECT r.specimen, r.indicator, r.status, r.reason, r.chain, i.line "
                "FROM results r JOIN indicators i ON i.name = r.indicator "
                "WHERE r.status <> 'ok' ORDER BY r.specimen, r.indicator").fetchall()],
        "specimen_notes": [
            {"specimen": r[0], "note": r[1]}
            for r in con.execute(
                "SELECT specimen, note FROM specimen_notes ORDER BY specimen, note").fetchall()],
    }
    path = out_dir / "exceptions.json"
    path.write_text(json.dumps(exceptions, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    written.append(path)

    conventions = store.get_meta(con, "conventions")
    path = out_dir / "conventions.txt"
    path.write_text("本程序数学约定\n" + "\n".join(
        f"- {n}" for n in json.loads(conventions)) + "\n", encoding="utf-8")
    written.append(path)
    return written
