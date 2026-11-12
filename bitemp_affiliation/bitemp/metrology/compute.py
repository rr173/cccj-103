"""按 specimen 校验读数并计算指标值与一阶合成标准不确定度。

数学约定（README 有完整说明）：
  - 内部坐标为 SI 基本单位（m, s, kg, K）；
  - 对指标 f，梯度 g 以全部原始变量为坐标（自动微分，见 expr.evaluate），
    协方差矩阵 C 由原始量标准不确定度 u 与相关系数 rho 合成（C = D R D），
    合成方差 u(f)^2 = g C g^T；
  - 方差分解：对角贡献 g_i^2 C_ii 与各变量对的两倍交叉贡献
    2 g_i g_j C_ij（可负，保留），相加恰为合成方差；
  - 浮点可能给出微小负方差：若 0 > var >= -VAR_REL_TOL * sum(|贡献|)
    则按 0 处理并注明；明显为负（超出容差）记 error，绝不取绝对值开根号。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import correl, expr
from .inputs import Config, CorrelationRow, MeasurementRow
from .units import BASE_UNITS

# 方差负值容差：|var| <= VAR_REL_TOL * sum(|各方差贡献|) 视为浮点噪声
VAR_REL_TOL = 1e-12

STATUS_OK = "ok"
STATUS_UNAVAILABLE = "unavailable"   # 缺读数 / 求值域失败（除零、sqrt 边界等）
STATUS_ERROR = "error"               # 数值异常（明显负方差、结果非有限等）


@dataclass(frozen=True)
class RejectReason:
    message: str
    file: str
    line: int | None


@dataclass
class Contribution:
    kind: str          # "diag" | "cross"
    left: str
    right: str         # diag 时与 left 相同
    amount: float      # SI 方差贡献（cross 为两倍交叉项，可负）


@dataclass
class IndicatorResult:
    indicator: str
    status: str
    value_si: float | None = None
    u_si: float | None = None
    var_si: float | None = None
    grad: dict[str, float] = field(default_factory=dict)   # SI 坐标偏导
    contributions: list[Contribution] = field(default_factory=list)
    reason: str | None = None
    chain: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class SpecimenOutcome:
    specimen: str
    status: str                       # "ok" | "rejected"
    reject_reasons: list[RejectReason] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    results: dict[str, IndicatorResult] = field(default_factory=dict)


# ---------------------------------------------------------------- 读数校验
def validate_measurements(config: Config,
                          rows: list[MeasurementRow],
                          ) -> tuple[dict[str, float], dict[str, float], list[RejectReason]]:
    """校验一个 specimen 的全部读数行，返回 (SI 值, SI 不确定度, 拒收原因)。

    任一拒收原因成立则该 specimen 整体拒收（原因尽量列全）。
    """
    values: dict[str, float] = {}
    us: dict[str, float] = {}
    reasons: list[RejectReason] = []
    seen: dict[str, int] = {}
    for row in rows:
        file = "measurements.csv"
        if not row.specimen:
            reasons.append(RejectReason("specimen 为空", file, row.line))
            continue
        var = config.variables.get(row.variable)
        if var is None:
            reasons.append(RejectReason(
                f"未知变量 {row.variable!r}（未在 variables.json 声明）", file, row.line))
            continue
        if row.variable in seen:
            reasons.append(RejectReason(
                f"变量 {row.variable!r} 重复读数（首次在第 {seen[row.variable]} 行）",
                file, row.line))
            continue
        seen[row.variable] = row.line
        unit = BASE_UNITS.get(row.unit)
        if unit is None:
            reasons.append(RejectReason(
                f"变量 {row.variable!r} 的单位 {row.unit!r} 未知"
                f"（支持: {', '.join(BASE_UNITS)}）", file, row.line))
            continue
        if unit.dim != var.dim:
            reasons.append(RejectReason(
                f"变量 {row.variable!r} 的单位 {row.unit!r} 与声明量纲 "
                f"{var.dimension} 不兼容", file, row.line))
            continue
        try:
            value = float(row.value_text)
        except ValueError:
            reasons.append(RejectReason(
                f"变量 {row.variable!r} 的 value 不是数值: {row.value_text!r}",
                file, row.line))
            continue
        try:
            u = float(row.u_text)
        except ValueError:
            reasons.append(RejectReason(
                f"变量 {row.variable!r} 的 u 不是数值: {row.u_text!r}",
                file, row.line))
            continue
        if not math.isfinite(value):
            reasons.append(RejectReason(
                f"变量 {row.variable!r} 的 value 必须有限，实际为 {row.value_text!r}",
                file, row.line))
            continue
        if not math.isfinite(u) or u < 0.0:
            reasons.append(RejectReason(
                f"变量 {row.variable!r} 的 u 必须有限且非负，实际为 {row.u_text!r}",
                file, row.line))
            continue
        values[row.variable] = unit.to_si(value)
        us[row.variable] = unit.u_to_si(u)
    return values, us, reasons


def validate_correlations(config: Config,
                          rows: list[CorrelationRow],
                          ) -> tuple[dict[tuple[str, str], float], list[RejectReason]]:
    """校验一个 specimen 的相关行，返回 (无序对 -> rho, 拒收原因)。"""
    reasons: list[RejectReason] = []
    parsed: list[tuple[str, str, float, int]] = []
    for row in rows:
        file = "correlations.csv"
        bad = False
        for name in (row.left, row.right):
            if name not in config.variables:
                reasons.append(RejectReason(
                    f"相关系数引用了未知变量 {name!r}（未在 variables.json 声明）",
                    file, row.line))
                bad = True
        try:
            rho = float(row.rho_text)
        except ValueError:
            reasons.append(RejectReason(
                f"rho 不是数值: {row.rho_text!r}", file, row.line))
            continue
        if not bad:
            parsed.append((row.left, row.right, rho, row.line))
    try:
        pairs = correl.assemble_rho(parsed)
    except correl.CorrError as e:
        reasons.append(RejectReason(str(e), "correlations.csv", e.line))
        return {}, reasons
    return pairs, reasons


# ---------------------------------------------------------------- 指标计算
def _decompose(grad: dict[str, float], var_names: list[str],
               cov: list[list[float]]) -> tuple[float, list[Contribution]]:
    """合成方差 = 对角贡献 + 各变量对两倍交叉贡献（可负，保留）。"""
    idx = {name: i for i, name in enumerate(var_names)}
    used = sorted(k for k, g in grad.items() if g != 0.0)
    contribs: list[Contribution] = []
    total = 0.0
    for a in used:
        i = idx[a]
        amount = grad[a] * grad[a] * cov[i][i]
        contribs.append(Contribution("diag", a, a, amount))
        total += amount
    for p, a in enumerate(used):
        i = idx[a]
        for b in used[p + 1:]:
            j = idx[b]
            if cov[i][j] == 0.0:
                continue  # rho=0 的变量对交叉贡献恰为 0，不列示
            amount = 2.0 * grad[a] * grad[b] * cov[i][j]
            contribs.append(Contribution("cross", a, b, amount))
            total += amount
    return total, contribs


def _combine_variance(grad: dict[str, float], var_names: list[str],
                      cov: list[list[float]],
                      ) -> tuple[float | None, float | None, list[Contribution], list[str], str | None]:
    """返回 (u_si, var_si, contributions, notes, error_reason)。"""
    var, contribs = _decompose(grad, var_names, cov)
    notes: list[str] = []
    if var < 0.0:
        scale = sum(abs(c.amount) for c in contribs)
        tol = VAR_REL_TOL * scale
        if var >= -tol:
            notes.append(
                f"合成方差为微小负值 {var:.3e}（浮点噪声，容差 "
                f"{VAR_REL_TOL:.0e}×|贡献|和），按 0 处理")
            var = 0.0
        else:
            return None, var, contribs, notes, (
                f"合成方差明显为负（{var:.6e}，超出数值容差），"
                f"不允许取绝对值开根号")
    u = math.sqrt(var)
    return u, var, contribs, notes, None


def compute_specimen(config: Config,
                     values_si: dict[str, float],
                     u_si: dict[str, float],
                     rho_pairs: dict[tuple[str, str], float],
                     ) -> SpecimenOutcome:
    """对单个已通过行级校验的 specimen 计算全部指标。"""
    # 相关矩阵（只覆盖有读数的变量；缺失的非对角项按 0，本程序约定）
    var_names = sorted(values_si)
    r = correl.correlation_matrix(var_names, rho_pairs)
    r, psd_notes = correl.check_and_fix_psd(r)
    cov = correl.covariance_from(r, [u_si[n] for n in var_names])

    n_pairs = len(var_names) * (len(var_names) - 1) // 2
    given = sum(1 for (a, b) in rho_pairs if a in values_si and b in values_si)
    notes = list(psd_notes)
    if n_pairs > given:
        notes.append(
            f"本标本 {n_pairs - given} 对变量未给出相关系数，按约定以 0 处理")

    outcome = SpecimenOutcome("", "ok", notes=notes)

    # 逐指标求值（拓扑序），中间指标失败沿依赖链传播
    memo: dict[str, tuple[float, dict[str, float]]] = {}
    failures: dict[str, tuple[str, list[str]]] = {}

    def resolve(name: str) -> tuple[float, dict[str, float]]:
        if name in memo:
            return memo[name]
        if name in failures:
            reason, chain = failures[name]
            raise expr.DomainFailure(reason, chain)
        if name in values_si:
            return values_si[name], {name: 1.0}
        raise expr.DomainFailure(f"变量 {name!r} 缺少读数", [name])

    for ind in config.indicators:
        result = IndicatorResult(ind.name, STATUS_OK)
        try:
            value, grad = expr.evaluate(ind.ast, resolve)
        except expr.DomainFailure as e:
            chain = [ind.name] + [c for c in e.chain if c != ind.name]
            failures[ind.name] = (e.reason, chain)
            result.status = STATUS_UNAVAILABLE
            result.reason = e.reason
            result.chain = chain
            outcome.results[ind.name] = result
            continue
        except OverflowError:
            # 幂运算等超出双精度范围（乘除溢出产生 inf，由下方有限性检查拦截）
            reason = "数值溢出：幂运算结果超出双精度范围"
            failures[ind.name] = (reason, [ind.name])
            result.status = STATUS_ERROR
            result.reason = reason
            result.chain = [ind.name]
            outcome.results[ind.name] = result
            continue
        # 任何失败都登记进 failures，下游指标沿依赖链传播为 unavailable
        if not math.isfinite(value) or not all(math.isfinite(g) for g in grad.values()):
            reason = f"指标值或梯度非有限数（{value!r}），可能存在数值溢出"
            failures[ind.name] = (reason, [ind.name])
            result.status = STATUS_ERROR
            result.reason = reason
            result.chain = [ind.name]
            outcome.results[ind.name] = result
            continue
        u, var, contribs, cnotes, error = _combine_variance(grad, var_names, cov)
        result.notes.extend(cnotes)
        if error is not None:
            failures[ind.name] = (error, [ind.name])
            result.status = STATUS_ERROR
            result.reason = error
            result.chain = [ind.name]
            result.grad = grad
            result.contributions = contribs
            outcome.results[ind.name] = result
            continue
        if not math.isfinite(u):
            reason = "合成标准不确定度非有限数，可能存在数值溢出"
            failures[ind.name] = (reason, [ind.name])
            result.status = STATUS_ERROR
            result.reason = reason
            result.chain = [ind.name]
            outcome.results[ind.name] = result
            continue
        memo[ind.name] = (value, grad)
        result.value_si = value
        result.u_si = u
        result.var_si = var
        result.grad = grad
        result.contributions = contribs
        outcome.results[ind.name] = result
    return outcome


def process_specimen(config: Config,
                     specimen: str,
                     mrows: list[MeasurementRow],
                     crows: list[CorrelationRow]) -> SpecimenOutcome:
    """单个 specimen 的完整处理：行级校验 -> 矩阵校验 -> 指标计算。"""
    values_si, u_si, reasons = validate_measurements(config, mrows)
    rho_pairs, creasons = validate_correlations(config, crows)
    reasons.extend(creasons)
    if not reasons:
        try:
            outcome = compute_specimen(config, values_si, u_si, rho_pairs)
        except correl.CorrError as e:
            reasons.append(RejectReason(str(e), "correlations.csv", None))
        else:
            outcome.specimen = specimen
            return outcome
    return SpecimenOutcome(specimen, "rejected", reject_reasons=reasons)
