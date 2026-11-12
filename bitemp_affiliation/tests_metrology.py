#!/usr/bin/env python3
"""验收测试：直接运行 python3 tests_metrology.py。

覆盖：厘米换米、摄氏换开尔文、相关长度的和与差、中间指标复用、x-x 归零、
完全相关等精度相减、非半正定拒收、缺变量时无关指标仍有结果、
数值差分核对梯度（不复用生产求导代码）、由 rho/u 直接计算参考方差、
方差分解还原、容差内负特征值修正、逐样本拒收与公式局部失败可追到输入行、
配置错误（未知引用/循环依赖/显示单位不符/非法语法）、磁盘重开一致性。
"""
from __future__ import annotations

import json
import math
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bitemp.metrology import compute, correl, expr, inputs, service, store  # noqa: E402

EXAMPLE_DIR = Path(__file__).resolve().parent / "examples" / "metrology"
PASSED = []


def check(name):
    def deco(fn):
        fn()
        PASSED.append(name)
        print(f"  ✔ {name}")
    return deco


def approx(a, b, rel=1e-12, abs_tol=1e-15):
    return math.isclose(a, b, rel_tol=rel, abs_tol=abs_tol)


def write_dir(root: Path, variables: str, formulas: str,
              measurements: str, correlations: str) -> Path:
    (root / "variables.json").write_text(variables, encoding="utf-8")
    (root / "formulas.json").write_text(formulas, encoding="utf-8")
    (root / "measurements.csv").write_text(measurements, encoding="utf-8")
    (root / "correlations.csv").write_text(correlations, encoding="utf-8")
    return root


def tmp_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="metrology_test_"))


def run_examples():
    con = store.connect(":memory:")
    summary = service.run(EXAMPLE_DIR, con=con)
    return summary, con


def get(summary, specimen, indicator):
    return summary["outcomes"][specimen].results[indicator]


# 共享一次示例运行，避免每个用例重复装载
_SUMMARY, _CON = None, None


def summary():
    global _SUMMARY, _CON
    if _SUMMARY is None:
        _SUMMARY, _CON = run_examples()
    return _SUMMARY, _CON


# ---------------------------------------------------------------- 1. 厘米换米
@check("厘米换米：150 cm ± 0.6 cm -> 1.5 m ± 0.006 m")
def _():
    s, _con = summary()
    r = get(s, "S1", "L1_m")
    assert r.status == "ok"
    assert approx(r.value_si, 1.5) and approx(r.u_si, 0.006)


# ---------------------------------------------------------------- 2. 摄氏换开尔文
@check("摄氏换开尔文：25 degC ± 0.1 -> 298.15 K ± 0.1（u 不加偏移）")
def _():
    s, _con = summary()
    r = get(s, "S1", "T1_K")
    assert approx(r.value_si, 298.15) and approx(r.u_si, 0.1)
    # 显示回 degC 的指标：值减偏移，u 不变
    r2 = get(s, "S1", "Tavg_C")
    assert approx(r2.value_si, 295.65)
    assert approx(r2.u_si, math.sqrt(0.1 ** 2 + 0.1 ** 2) / 2)


# ---------------------------------------------------------------- 3. 相关长度的和与差
@check("相关长度的和与差：rho=0.5 进入交叉项，和差方差不同")
def _():
    s, _con = summary()
    r_sum = get(s, "S1", "Lsum")
    r_diff = get(s, "S1", "Ldiff")
    assert approx(r_sum.value_si, 2.3)
    assert approx(r_sum.var_si, 7.6e-5)
    assert approx(r_sum.u_si, math.sqrt(7.6e-5))
    assert approx(r_diff.value_si, 0.7)
    assert approx(r_diff.var_si, 2.8e-5)
    # 显示单位只改输出：Lsum_cm 的 SI 与 Lsum 完全一致
    r_cm = get(s, "S1", "Lsum_cm")
    assert r_cm.value_si == r_sum.value_si and r_cm.u_si == r_sum.u_si


# ---------------------------------------------------------------- 4. 中间指标复用
@check("中间指标复用：Lagain = Lsum + Ldiff，L2 的导数合并为 0，u = 2*u(L1)")
def _():
    s, _con = summary()
    r = get(s, "S1", "Lagain")
    assert approx(r.value_si, 3.0)
    assert approx(r.grad["L1"], 2.0) and approx(r.grad["L2"], 0.0)
    assert approx(r.var_si, 4 * 0.006 ** 2)
    assert approx(r.u_si, 0.012)


# ---------------------------------------------------------------- 5. x - x 归零
@check("x - x 的不确定度精确归零（同一原始量导数合并，不按独立测量处理）")
def _():
    s, _con = summary()
    r = get(s, "S1", "Lself")
    assert r.status == "ok"
    assert r.value_si == 0.0 and r.u_si == 0.0 and r.var_si == 0.0


# ---------------------------------------------------------------- 6. 完全相关等精度相减
@check("完全相关的等精度测量相减：u(Ldiff)=0；相加 u(Lsum)=1")
def _():
    s, _con = summary()
    r_diff = get(s, "S6", "Ldiff")
    r_sum = get(s, "S6", "Lsum")
    assert approx(r_diff.value_si, 0.1) and r_diff.u_si == 0.0
    assert approx(r_sum.value_si, 1.9) and approx(r_sum.u_si, 1.0)


# ---------------------------------------------------------------- 7. 非半正定拒收
@check("每对 rho 合法但整体非半正定：拒收该 specimen，原因可追溯")
def _():
    s, _con = summary()
    outcome = s["outcomes"]["S7"]
    assert outcome.status == "rejected"
    text = " ".join(r.message for r in outcome.reject_reasons)
    assert "半正定" in text and "特征值" in text


# ---------------------------------------------------------------- 8. 缺变量时无关指标仍有结果
@check("缺少 L2：依赖它的指标 unavailable 且带依赖链，无关指标正常")
def _():
    s, _con = summary()
    assert get(s, "S8", "L1_m").status == "ok"
    assert get(s, "S8", "T1_K").status == "ok"
    r = get(s, "S8", "Lsum")
    assert r.status == "unavailable"
    assert "L2" in r.reason and r.chain == ["Lsum", "L2"]
    # 经由中间指标的间接依赖也带完整链
    r2 = get(s, "S8", "Lagain")
    assert r2.status == "unavailable" and r2.chain[0] == "Lagain"
    assert "L2" in r2.chain


# ---------------------------------------------------------------- 9. 逐样本拒收与公式局部失败
@check("公式局部失败：除零 / sqrt(0) / sqrt(负) 只影响依赖指标，可追到输入行")
def _():
    s, _con = summary()
    # S9: L2 = 0 -> Lratio 除零、Lgeo sqrt(0)；Lsum 不受影响
    r = get(s, "S9", "Lratio")
    assert r.status == "unavailable" and "除数" in r.reason
    r = get(s, "S9", "Lgeo")
    assert r.status == "unavailable" and "sqrt" in r.reason
    assert get(s, "S9", "Lsum").status == "ok"
    # S10: L1 = -1 -> Lgeo sqrt(负)；Lratio 正常
    r = get(s, "S10", "Lgeo")
    assert r.status == "unavailable" and "sqrt" in r.reason
    assert get(s, "S10", "Lratio").status == "ok"
    # S12: 反向重复相关不一致 -> 拒收，原因含行号
    outcome = s["outcomes"]["S12"]
    assert outcome.status == "rejected"
    text = " ".join(r.message for r in outcome.reject_reasons)
    assert "不一致" in text and "第" in text and "行" in text
    # S13: 单位与量纲不兼容 -> 拒收，可追到 measurements.csv 行号
    outcome = s["outcomes"]["S13"]
    assert outcome.status == "rejected"
    reason = outcome.reject_reasons[0]
    assert "不兼容" in reason.message and reason.file == "measurements.csv"
    assert reason.line is not None
    # S11: 一致的反向重复项合并；零不确定度 + 奇异半正定矩阵可计算
    assert get(s, "S11", "Ldiff").status == "ok"
    assert get(s, "S11", "Ldiff").u_si == 0.0


# ---------------------------------------------------------------- 10. 方差分解还原
@check("方差分解：对角贡献 + 两倍交叉贡献之和还原合成方差（负交叉保留）")
def _():
    s, _con = summary()
    for specimen, outcome in s["outcomes"].items():
        if outcome.status != "ok":
            continue
        for name, r in outcome.results.items():
            if r.status != "ok":
                continue
            total = sum(c.amount for c in r.contributions)
            assert approx(total, r.var_si, rel=1e-9, abs_tol=1e-30), \
                f"{specimen}/{name}: {total} != {r.var_si}"
    # S1/Ldiff 的交叉贡献必须为负（rho>0 且导数异号）
    r = get(s, "S1", "Ldiff")
    cross = [c for c in r.contributions if c.kind == "cross"]
    assert len(cross) == 1 and cross[0].amount < 0.0
    assert approx(cross[0].amount, -2.4e-5)


# ---------------------------------------------------------------- 11. 数值差分核对梯度 + 参考方差
@check("数值差分核对梯度（独立实现），并由 rho/u 直接计算参考方差")
def _():
    root = tmp_dir()
    write_dir(
        root,
        '{"variables": ['
        '{"name": "a", "dimension": "length"},'
        '{"name": "b", "dimension": "length"},'
        '{"name": "c", "dimension": "mass"},'
        '{"name": "d", "dimension": "time"}]}',
        '{"indicators": ['
        '{"name": "f1", "expression": "a * b + b ** 2", "unit": "m^2"},'
        '{"name": "f2", "expression": "sqrt(f1) / d", "unit": "m/s"},'
        '{"name": "f3", "expression": "(a + b) / (a - b)", "unit": "1"},'
        '{"name": "f4", "expression": "c * f2 ** 2", "unit": "kg*m^2/s^2"}]}',
        "specimen,variable,value,unit,u\n"
        "P1,a,1.6,m,0.02\nP1,b,0.9,m,0.015\nP1,c,2.0,kg,0.03\nP1,d,4.0,s,0.05\n",
        "specimen,left,right,rho\nP1,a,b,0.3\nP1,c,d,-0.4\n")
    config = inputs.load_config(root)
    base = {"a": 1.6, "b": 0.9, "c": 2.0, "d": 4.0}
    u_si = {"a": 0.02, "b": 0.015, "c": 0.03, "d": 0.05}
    rho = {("a", "b"): 0.3, ("c", "d"): -0.4}

    outcome = compute.compute_specimen(config, base, u_si, rho)
    prod = {name: (r.value_si, r.u_si, dict(r.grad))
            for name, r in outcome.results.items() if r.status == "ok"}
    assert len(prod) == 4

    # --- 数值差分（只用生产的“值”，不用生产的求导代码）
    num_grad = {name: {} for name in prod}
    for var in base:
        h = 1e-6 * max(1.0, abs(base[var]))
        plus = dict(base, **{var: base[var] + h})
        minus = dict(base, **{var: base[var] - h})
        out_p = compute.compute_specimen(config, plus, u_si, rho)
        out_m = compute.compute_specimen(config, minus, u_si, rho)
        for name in prod:
            df = (out_p.results[name].value_si - out_m.results[name].value_si) / (2 * h)
            num_grad[name][var] = df
    for name, (value, u, grad) in prod.items():
        for var, g in grad.items():
            assert approx(g, num_grad[name][var], rel=1e-6, abs_tol=1e-9), \
                f"{name} d/d{var}: {g} vs {num_grad[name][var]}"

    # --- 参考方差：由输入 rho 与 u 直接构造 C，g 用数值梯度
    names = sorted(base)
    idx = {n: i for i, n in enumerate(names)}
    c_ref = [[0.0] * len(names) for _ in names]
    for i, ni in enumerate(names):
        c_ref[i][i] = u_si[ni] ** 2
    for (x, y), r in rho.items():
        i, j = idx[x], idx[y]
        c_ref[i][j] = c_ref[j][i] = r * u_si[x] * u_si[y]
    for name, (value, u, grad) in prod.items():
        var_ref = 0.0
        for i, ni in enumerate(names):
            gi = num_grad[name].get(ni, 0.0)
            for j, nj in enumerate(names):
                var_ref += gi * c_ref[i][j] * num_grad[name].get(nj, 0.0)
        assert approx(u ** 2, var_ref, rel=1e-6, abs_tol=1e-18), \
            f"{name}: u^2={u ** 2} vs ref={var_ref}"


# ---------------------------------------------------------------- 12. 容差内负特征值修正
@check("最小特征值在 [-1e-10, 0) 内：按 0 修正、注明，不静默；明显为负：拒收")
def _():
    # 直接构造 lambda_min = -4e-11 的矩阵（容差内）
    r = [[1.0, 1.0 + 4e-11], [1.0 + 4e-11, 1.0]]
    fixed, notes = correl.check_and_fix_psd(r)
    assert notes and "修正" in notes[0]
    evals, _ = correl.jacobi_eigh(fixed)
    assert min(evals) >= -1e-15
    # 明显非半正定
    try:
        correl.check_and_fix_psd([[1.0, 1.0001], [1.0001, 1.0]])
        assert False, "应当抛出 CorrError"
    except correl.CorrError as e:
        assert "半正定" in str(e)


# ---------------------------------------------------------------- 13. 明显负方差记 error
@check("合成方差明显为负：记 error 并说明，不取绝对值开根号")
def _():
    u, var, contribs, notes, error = compute._combine_variance(
        {"a": 1.0}, ["a"], [[-1.0]])
    assert error is not None and "负" in error
    # 浮点噪声级负方差：按 0 处理并注明
    u, var, contribs, notes, error = compute._combine_variance(
        {"a": 1.0, "b": 1.0}, ["a", "b"],
        [[1.0, -1.0], [-1.0, 1.0 - 1e-14]])
    # var = 1 + (1-1e-14) - 2 = -1e-14，|贡献|和 ≈ 4，容差 4e-12 -> 按 0
    assert error is None and var == 0.0 and u == 0.0 and notes


# ---------------------------------------------------------------- 14. 配置错误
@check("配置错误：未知引用 / 循环依赖 / 显示单位不符 / 非法语法 / 非整数幂")
def _():
    variables = '{"variables": [{"name": "a", "dimension": "length"},' \
                '{"name": "b", "dimension": "length"}]}'
    header = "specimen,variable,value,unit,u\n"
    corr = "specimen,left,right,rho\n"

    def expect_config_error(formulas, needle):
        root = tmp_dir()
        write_dir(root, variables, formulas, header, corr)
        try:
            inputs.load_config(root)
        except inputs.ConfigFailure as e:
            assert needle in str(e), f"{needle} not in {e}"
            return
        raise AssertionError(f"应当报配置错误: {needle}")

    expect_config_error('{"indicators": [{"name": "f", "expression": "a + nope", "unit": "m"}]}',
                        "不存在的名字")
    expect_config_error('{"indicators": [{"name": "f", "expression": "g + 1", "unit": "m"},'
                        '{"name": "g", "expression": "f + 1", "unit": "m"}]}',
                        "循环依赖")
    expect_config_error('{"indicators": [{"name": "f", "expression": "a + b", "unit": "s"}]}',
                        "不符")
    expect_config_error('{"indicators": [{"name": "f", "expression": "a + 1", "unit": "m"}]}',
                        "量纲")
    expect_config_error('{"indicators": [{"name": "f", "expression": "a ** 0.5", "unit": "m"}]}',
                        "整数")
    expect_config_error('{"indicators": [{"name": "f", "expression": "__import__(os)", "unit": "m"}]}',
                        "未知函数")
    expect_config_error('{"indicators": [{"name": "f", "expression": "a; b", "unit": "m"}]}',
                        "非法")
    expect_config_error('{"indicators": [{"name": "f", "expression": "a", "unit": "furlong"}]}',
                        "未知单位")
    expect_config_error('{"indicators": [{"name": "f", "expression": "a * b", "unit": "degC*m"}]}',
                        "偏移")
    # 多重错误一次性汇总
    root = tmp_dir()
    write_dir(root, variables,
              '{"indicators": [{"name": "f", "expression": "a + nope", "unit": "m"},'
              '{"name": "g", "expression": "a + 1", "unit": "m"}]}',
              header, corr)
    try:
        inputs.load_config(root)
        assert False
    except inputs.ConfigFailure as e:
        assert len(e.items) == 2


# ---------------------------------------------------------------- 15. 行级数据校验
@check("行级校验：重复读数 / 未知变量 / 负 u / 非有限值按 specimen 拒收并带行号")
def _():
    root = tmp_dir()
    write_dir(
        root,
        '{"variables": [{"name": "a", "dimension": "length"}]}',
        '{"indicators": [{"name": "f", "expression": "a", "unit": "m"}]}',
        "specimen,variable,value,unit,u\n"
        "B1,a,1.0,m,0.1\n"
        "B1,a,2.0,m,0.1\n"          # 重复读数（第 3 行）
        "B2,zz,1.0,m,0.1\n"          # 未知变量（第 4 行）
        "B3,a,1.0,m,-0.5\n"          # 负 u（第 5 行）
        "B4,a,inf,m,0.1\n"           # 非有限值（第 6 行）
        "B5,a,1.0,m,0.1\n",          # 正常
        "specimen,left,right,rho\n")
    config = inputs.load_config(root)
    data = inputs.load_inputs(root)
    by_spec = {}
    for r in data.measurements:
        by_spec.setdefault(r.specimen, []).append(r)
    o1 = compute.process_specimen(config, "B1", by_spec["B1"], [])
    assert o1.status == "rejected" and "重复读数" in o1.reject_reasons[0].message
    assert o1.reject_reasons[0].line == 3
    o2 = compute.process_specimen(config, "B2", by_spec["B2"], [])
    assert o2.status == "rejected" and "未知变量" in o2.reject_reasons[0].message
    o3 = compute.process_specimen(config, "B3", by_spec["B3"], [])
    assert o3.status == "rejected" and "非负" in o3.reject_reasons[0].message
    o4 = compute.process_specimen(config, "B4", by_spec["B4"], [])
    assert o4.status == "rejected" and "有限" in o4.reject_reasons[0].message
    o5 = compute.process_specimen(config, "B5", by_spec["B5"], [])
    assert o5.status == "ok" and o5.results["f"].value_si == 1.0


# ---------------------------------------------------------------- 16. 配置错误可定位到行
@check("配置错误定位：JSON 语法与重复声明给出文件与行号")
def _():
    root = tmp_dir()
    dup_variables = (
        '{\n'
        '  "variables": [\n'
        '    {"name": "a", "dimension": "length"},\n'
        '    {"name": "a", "dimension": "mass"}\n'
        '  ]\n'
        '}\n')
    write_dir(root, dup_variables,
              '{"indicators": []}',
              "specimen,variable,value,unit,u\n",
              "specimen,left,right,rho\n")
    try:
        inputs.load_config(root)
        assert False
    except inputs.ConfigFailure as e:
        hit = [it for it in e.items if "重复声明" in it[2]]
        assert hit and hit[0][0] == "variables.json" and hit[0][1] == 4
    # JSON 语法错误
    root2 = tmp_dir()
    write_dir(root2, '{"variables": [}',
              '{"indicators": []}',
              "specimen,variable,value,unit,u\n",
              "specimen,left,right,rho\n")
    try:
        inputs.load_config(root2)
        assert False
    except inputs.InputFailure as e:
        assert "variables.json" in str(e) and "语法" in str(e)


# ---------------------------------------------------------------- 17. 表达式细节
@check("表达式：sqrt 边界、0 的负幂、x**0、优先级；sqrt 之外的调用拒绝")
def _():
    ev = expr.evaluate
    resolve_const = lambda env: lambda name: (env[name], {name: 1.0})
    env = {"x": 4.0}
    assert ev(expr.parse_expression("sqrt(x)"), resolve_const(env))[0] == 2.0
    assert ev(expr.parse_expression("x ** 0"), resolve_const(env))[0] == 1.0
    assert ev(expr.parse_expression("2 * x ** 2"), resolve_const(env))[0] == 32.0
    assert ev(expr.parse_expression("-x ** 2"), resolve_const(env))[0] == -16.0
    assert ev(expr.parse_expression("x ^ 3"), resolve_const(env))[0] == 64.0
    for bad in ("sqrt(x", "x **", "x ** 1.5", "sin(x)", "x @ 2", ""):
        try:
            expr.parse_expression(bad)
            raise AssertionError(f"应当拒绝: {bad!r}")
        except expr.ExprError:
            pass
    # 域失败
    for bad_expr, needle in (("x / (x - x)", "除数"),
                             ("sqrt(x - x)", "sqrt"),
                             ("(x - x) ** -1", "负整数次幂")):
        try:
            ev(expr.parse_expression(bad_expr), resolve_const(env))
            raise AssertionError(f"应当域失败: {bad_expr}")
        except expr.DomainFailure as e:
            assert needle in e.reason


# ---------------------------------------------------------------- 18. 磁盘重开一致性
@check("落库后可从磁盘重开：explain 与导出明细和内存结果一致")
def _():
    db = tmp_dir() / "metrology.duckdb"
    summary1 = service.run(EXAMPLE_DIR, str(db))
    # 从磁盘重开
    info = service.explain(str(db), "S1", "Lsum")
    r = get(summary1, "S1", "Lsum")
    assert info["status"] == "ok"
    assert info["value_si"] == r.value_si and info["u_si"] == r.u_si
    assert approx(info["contribution_sum"], r.u_si ** 2)
    grad = dict(info["gradients"])
    assert approx(grad["L1"], 1.0) and approx(grad["L2"], 1.0)
    # 导出
    out = tmp_dir() / "out"
    written = service.export(str(db), out)
    names = {p.name for p in written}
    assert {"results.csv", "gradients.csv", "contributions.csv",
            "specimen_status.csv", "exceptions.json", "conventions.txt"} <= names
    exceptions = json.loads((out / "exceptions.json").read_text(encoding="utf-8"))
    rejected = {x["specimen"] for x in exceptions["rejected_specimens"]}
    assert {"S7", "S12", "S13"} <= rejected
    failed = {(x["specimen"], x["indicator"]) for x in exceptions["failed_indicators"]}
    assert ("S9", "Lratio") in failed
    conventions = (out / "conventions.txt").read_text(encoding="utf-8")
    assert "按 0 处理" in conventions  # 缺失相关按 0 的约定随输出说明


# ---------------------------------------------------------------- 19. 手工值终核
@check("手工值终核：speed / energy / Lratio / Tavg_C 全链路数值")
def _():
    s, _con = summary()
    r = get(s, "S1", "speed")
    assert approx(r.value_si, 3.0) and approx(r.var_si, 2.88e-4)
    r = get(s, "S1", "energy")
    assert approx(r.value_si, 11.25) and approx(r.u_si, 0.135)
    r = get(s, "S1", "Lratio")
    assert approx(r.value_si, 1.875) and approx(r.var_si, 7.3828125e-5)
    # energy 的梯度（SI）：m1 -> 4.5, L1 -> 15, t1 -> -45
    r = get(s, "S1", "energy")
    assert approx(r.grad["m1"], 4.5)
    assert approx(r.grad["L1"], 15.0)
    assert approx(r.grad["t1"], -45.0)


# ---------------------------------------------------------------- 20. 奇异矩阵触发容差修正（集成）
@check("四变量全相关：Jacobi 浮点给出容差内负特征值，修正并注明后可计算")
def _():
    root = tmp_dir()
    write_dir(
        root,
        '{"variables": ['
        '{"name": "a", "dimension": "length"},'
        '{"name": "b", "dimension": "length"},'
        '{"name": "c", "dimension": "length"},'
        '{"name": "d", "dimension": "length"}]}',
        '{"indicators": [{"name": "total", "expression": "a + b + c + d", "unit": "m"}]}',
        "specimen,variable,value,unit,u\n"
        "F1,a,1.0,m,0.1\nF1,b,1.0,m,0.1\nF1,c,1.0,m,0.1\nF1,d,1.0,m,0.1\n",
        "specimen,left,right,rho\n"
        "F1,a,b,1\nF1,a,c,1\nF1,a,d,1\nF1,b,c,1\nF1,b,d,1\nF1,c,d,1\n")
    config = inputs.load_config(root)
    outcome = compute.compute_specimen(
        config, {"a": 1.0, "b": 1.0, "c": 1.0, "d": 1.0},
        {"a": 0.1, "b": 0.1, "c": 0.1, "d": 0.1},
        {("a", "b"): 1.0, ("a", "c"): 1.0, ("a", "d"): 1.0,
         ("b", "c"): 1.0, ("b", "d"): 1.0, ("c", "d"): 1.0})
    # 容差内负特征值按 0 修正并注明（不静默），结果照常计算
    assert any("修正" in n for n in outcome.notes), outcome.notes
    r = outcome.results["total"]
    assert r.status == "ok"
    assert approx(r.value_si, 4.0)
    # 完全相关等精度相加：u = 4 × 0.1
    assert approx(r.u_si, 0.4)


# ---------------------------------------------------------------- 21. demo 端到端
@check("demo 命令（内存库与磁盘库）端到端可运行")
def _():
    import subprocess
    root = Path(__file__).resolve().parent
    r = subprocess.run([sys.executable, str(root / "main_metrology.py"), "demo"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "Lagain" in r.stdout and "拒收" in r.stdout
    db = tmp_dir() / "demo.duckdb"
    r = subprocess.run([sys.executable, str(root / "main_metrology.py"), "demo",
                        "--db", str(db)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "从磁盘重开库" in r.stdout


print("测量数据处理验收测试：")
print(f"\n全部 {len(PASSED)} 项通过")
