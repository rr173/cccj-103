"""输入装载：variables.json / formulas.json / measurements.csv / correlations.csv。

所有记录都带来源文件与行号，异常能定位到交来的材料。

错误分两层：
  - 配置错误（variables.json / formulas.json）：未知量纲、重名、表达式
    语法/量纲非法、引用不存在的名字、指标循环依赖、显示单位与推导量纲
    不符等。全部收集后一次性抛出 ConfigFailure，在读数计算前指出。
  - 数据错误（measurements.csv / correlations.csv 的行级问题）：不在此
    层判定，原样读入（保留原文与行号），由 compute 按 specimen 归集。
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import expr, jsonpos
from .units import DIMENSIONS, UnitError, dim_to_string, parse_display_unit, BASE_UNITS

VARIABLES_FILE = "variables.json"
FORMULAS_FILE = "formulas.json"
MEASUREMENTS_FILE = "measurements.csv"
CORRELATIONS_FILE = "correlations.csv"

_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class ConfigFailure(Exception):
    """配置错误汇总；items = [(file, line|None, message), ...]。"""

    def __init__(self, items: list[tuple[str, int | None, str]]):
        self.items = items
        lines = [f"{f}:{ln}: {m}" if ln else f"{f}: {m}" for f, ln, m in items]
        super().__init__("配置错误（在读数计算前中止）:\n" + "\n".join(lines))


class InputFailure(Exception):
    """输入文件本身无法读取（缺文件、JSON 语法错、CSV 表头错等）。"""


@dataclass(frozen=True)
class Variable:
    name: str
    dimension: str          # DIMENSIONS 的键
    dim: tuple
    line: int


@dataclass(frozen=True)
class Indicator:
    name: str
    expression: str
    unit_text: str
    ast: expr.Node
    dim: tuple
    display: object         # units.DisplayUnit
    refs: frozenset[str]    # 直接引用的指标名（不含原始变量）
    line: int


@dataclass
class Config:
    variables: dict[str, Variable]
    indicators: list[Indicator]          # 已按依赖拓扑排序
    indicator_map: dict[str, Indicator]


@dataclass(frozen=True)
class MeasurementRow:
    specimen: str
    variable: str
    value_text: str
    unit: str
    u_text: str
    line: int


@dataclass(frozen=True)
class CorrelationRow:
    specimen: str
    left: str
    right: str
    rho_text: str
    line: int


@dataclass
class Inputs:
    measurements: list[MeasurementRow] = field(default_factory=list)
    correlations: list[CorrelationRow] = field(default_factory=list)


# ---------------------------------------------------------------- JSON 配置
def _load_json(path: Path):
    try:
        text = path.read_text(encoding="utf-8-sig")  # 容忍 BOM
    except OSError as e:
        raise InputFailure(f"无法读取 {path.name}: {e}") from None
    try:
        return jsonpos.parse(text)
    except jsonpos.JsonSyntaxError as e:
        raise InputFailure(f"{path.name}: JSON 语法错误: {e}") from None


def _entry_line(obj, fallback: int) -> int:
    return getattr(obj, "line", fallback)


def load_config(directory: str | Path) -> Config:
    """读取并校验 variables.json 与 formulas.json；有错汇总抛 ConfigFailure。"""
    directory = Path(directory)
    errors: list[tuple[str, int | None, str]] = []

    # ---------------- variables.json
    variables: dict[str, Variable] = {}
    var_path = directory / VARIABLES_FILE
    var_doc = _load_json(var_path)
    entries = None
    if isinstance(var_doc, dict) and isinstance(var_doc.get("variables"), list):
        entries = var_doc["variables"]
    if entries is None:
        errors.append((VARIABLES_FILE, getattr(var_doc, "line", 1),
                       '顶层必须是 {"variables": [...]} 形式'))
        entries = []
    for pos, entry in enumerate(entries):
        line = _entry_line(entry, 1)
        if not isinstance(entry, dict):
            errors.append((VARIABLES_FILE, line, f"variables[{pos}] 必须是对象"))
            continue
        name = entry.get("name")
        dim_name = entry.get("dimension")
        if not isinstance(name, str) or not _NAME_RE.match(name):
            errors.append((VARIABLES_FILE, line,
                           f"变量名非法: {name!r}（需为标识符 [A-Za-z_][A-Za-z0-9_]*）"))
            continue
        if name == "sqrt":
            errors.append((VARIABLES_FILE, line, "变量名不能使用保留字 sqrt"))
            continue
        if dim_name not in DIMENSIONS:
            errors.append((VARIABLES_FILE, line,
                           f"变量 {name!r} 的量纲非法: {dim_name!r}"
                           f"（支持: {', '.join(DIMENSIONS)}）"))
            continue
        if name in variables:
            errors.append((VARIABLES_FILE, line,
                           f"变量 {name!r} 重复声明（首次在第 {variables[name].line} 行）"))
            continue
        variables[name] = Variable(name, dim_name, DIMENSIONS[dim_name], line)

    # ---------------- formulas.json
    raw_indicators: list[tuple[str, str, str, int]] = []  # name, expression, unit, line
    seen: dict[str, int] = {}
    formula_path = directory / FORMULAS_FILE
    formula_doc = _load_json(formula_path)
    fentries = None
    if isinstance(formula_doc, dict) and isinstance(formula_doc.get("indicators"), list):
        fentries = formula_doc["indicators"]
    if fentries is None:
        errors.append((FORMULAS_FILE, getattr(formula_doc, "line", 1),
                       '顶层必须是 {"indicators": [...]} 形式'))
        fentries = []
    for pos, entry in enumerate(fentries):
        line = _entry_line(entry, 1)
        if not isinstance(entry, dict):
            errors.append((FORMULAS_FILE, line, f"indicators[{pos}] 必须是对象"))
            continue
        name = entry.get("name")
        expression = entry.get("expression")
        unit_text = entry.get("unit")
        if not isinstance(name, str) or not _NAME_RE.match(name):
            errors.append((FORMULAS_FILE, line, f"指标名非法: {name!r}"))
            continue
        if name == "sqrt":
            errors.append((FORMULAS_FILE, line, "指标名不能使用保留字 sqrt"))
            continue
        if name in variables:
            errors.append((FORMULAS_FILE, line,
                           f"指标名 {name!r} 与原始变量重名"))
            continue
        if name in seen:
            errors.append((FORMULAS_FILE, line,
                           f"指标 {name!r} 重复定义（首次在第 {seen[name]} 行）"))
            continue
        seen[name] = line
        if not isinstance(expression, str) or not expression.strip():
            errors.append((FORMULAS_FILE, line, f"指标 {name!r} 缺少 expression 字符串"))
            continue
        if not isinstance(unit_text, str):
            errors.append((FORMULAS_FILE, line, f"指标 {name!r} 缺少 unit 字符串"))
            continue
        raw_indicators.append((name, expression, unit_text, line))

    # ---------------- 表达式解析 / 依赖 / 量纲
    parsed: dict[str, tuple[expr.Node, str, str, int]] = {}
    for name, expression, unit_text, line in raw_indicators:
        try:
            ast = expr.parse_expression(expression)
        except expr.ExprError as e:
            errors.append((FORMULAS_FILE, line, f"指标 {name!r} 表达式非法: {e}"))
            continue
        parsed[name] = (ast, expression, unit_text, line)

    # 引用检查 + 拓扑排序（Kahn），环整体报错
    refs: dict[str, set[str]] = {}
    bad_refs: set[str] = set()   # 引用了不存在/非法名字的指标（其下游一并跳过量纲推导）
    for name, (ast, _e, _u, line) in parsed.items():
        names = expr.referenced_names(ast)
        unknown = sorted(r for r in names if r not in variables and r not in parsed)
        for ref in unknown:
            if ref in seen:
                errors.append((FORMULAS_FILE, line,
                               f"指标 {name!r} 依赖的指标 {ref!r} 定义非法（见前述错误）"))
            else:
                errors.append((FORMULAS_FILE, line,
                               f"指标 {name!r} 引用了不存在的名字 {ref!r}"))
        if unknown:
            bad_refs.add(name)
        refs[name] = {r for r in names if r in parsed}

    order: list[str] = []
    remaining = dict(refs)
    while remaining:
        ready = sorted(n for n, rs in remaining.items() if not (rs & remaining.keys()))
        if not ready:
            # remaining = 环成员 ∪ 环的下游；区分两者，报出真正的环
            cyclic = set()
            for start in remaining:
                stack = list(refs[start] & remaining.keys())
                visited: set[str] = set()
                while stack:
                    cur = stack.pop()
                    if cur == start:
                        cyclic.add(start)
                        break
                    if cur in visited:
                        continue
                    visited.add(cur)
                    stack.extend(refs[cur] & remaining.keys())
            downstream = sorted(set(remaining) - cyclic)
            msg = f"指标存在循环依赖: {' -> '.join(sorted(cyclic))}"
            if downstream:
                msg += f"；以下指标依赖循环: {', '.join(downstream)}"
            errors.append((FORMULAS_FILE, None, msg))
            break
        order.extend(ready)
        for n in ready:
            del remaining[n]

    # 量纲推导（按拓扑序）+ 显示单位校验；引用已出错的指标及其下游跳过，
    # 避免同一根因重复报错
    env: dict[str, tuple] = {n: v.dim for n, v in variables.items()}
    indicators: list[Indicator] = []
    for name in order:
        if name in bad_refs or (refs[name] & bad_refs):
            bad_refs.add(name)
            continue
        ast, expression, unit_text, line = parsed[name]
        try:
            dim = expr.infer_dim(ast, env)
        except expr.ExprError as e:
            errors.append((FORMULAS_FILE, line, f"指标 {name!r} 量纲错误: {e}"))
            bad_refs.add(name)
            continue
        try:
            display = parse_display_unit(unit_text)
        except UnitError as e:
            errors.append((FORMULAS_FILE, line, f"指标 {name!r} 显示单位非法: {e}"))
            bad_refs.add(name)
            continue
        if display.dim != dim:
            errors.append((FORMULAS_FILE, line,
                           f"指标 {name!r} 显示单位 {unit_text!r} 的量纲 "
                           f"({dim_to_string(display.dim)}) 与表达式推导量纲 "
                           f"({dim_to_string(dim)}) 不符"))
            bad_refs.add(name)
            continue
        env[name] = dim
        indicators.append(Indicator(name, expression, unit_text, ast, dim,
                                    display, frozenset(refs[name]), line))

    if errors:
        raise ConfigFailure(errors)
    return Config(variables, indicators, {i.name: i for i in indicators})


# ---------------------------------------------------------------- CSV 数据
def _read_csv(path: Path, required: list[str]) -> list[tuple[dict[str, str], int]]:
    try:
        f = path.open("r", encoding="utf-8-sig", newline="")  # 容忍 BOM
    except OSError as e:
        raise InputFailure(f"无法读取 {path.name}: {e}") from None
    with f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            raise InputFailure(f"{path.name}: 空文件，缺少表头 {required}") from None
        header = [h.strip() for h in header]
        missing = [c for c in required if c not in header]
        if missing:
            raise InputFailure(f"{path.name}: 表头缺少列 {missing}（需要 {required}）")
        rows = []
        for record in reader:
            line = reader.line_num
            if len(record) != len(header):
                raise InputFailure(
                    f"{path.name} 第 {line} 行: 字段数 {len(record)} 与表头 "
                    f"{len(header)} 不一致")
            rows.append(({header[i]: record[i].strip() for i in range(len(header))}, line))
        return rows


def load_inputs(directory: str | Path) -> Inputs:
    """原样读入 measurements.csv / correlations.csv（保留原文与行号）。"""
    directory = Path(directory)
    inputs = Inputs()
    for row, line in _read_csv(directory / MEASUREMENTS_FILE,
                               ["specimen", "variable", "value", "unit", "u"]):
        inputs.measurements.append(MeasurementRow(
            row["specimen"], row["variable"], row["value"], row["unit"],
            row["u"], line))
    for row, line in _read_csv(directory / CORRELATIONS_FILE,
                               ["specimen", "left", "right", "rho"]):
        inputs.correlations.append(CorrelationRow(
            row["specimen"], row["left"], row["right"], row["rho"], line))
    return inputs
