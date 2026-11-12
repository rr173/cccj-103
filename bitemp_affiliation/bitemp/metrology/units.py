"""物理量纲与单位。

内部一律在 SI 基本单位（米 m、秒 s、千克 kg、开尔文 K）坐标下计算；
显示单位只影响最终输出的数值与不确定度，不改变计算依据。

量纲用 4 元 Fraction 指数向量表示，顺序 (L, M, T, Θ)：
长度 length、质量 mass、时间 time、温度 temperature。sqrt 会产生
分数次量纲（如 sqrt(m) -> L^(1/2)），因此指数用 Fraction 而不是 int。

单位是仿射变换：value_si = scale * value + offset。
  - degC 是绝对摄氏温度：scale=1, offset=273.15（值加偏移，u 不加）；
  - cm/mm/ms/g 等是纯缩放单位：offset=0；
  - 转换不确定度永远只用 scale：u_si = scale * u，绝不加偏移。
带偏移的单位（degC）不允许与其他单位组合成复合单位。
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

# 量纲指数向量顺序：长度 L、质量 M、时间 T、温度 Θ
DIM_NAMES = ("L", "M", "T", "Θ")
_F0 = Fraction(0)
_F1 = Fraction(1)

DIMENSIONLESS: tuple = (_F0, _F0, _F0, _F0)

DIMENSIONS: dict[str, tuple] = {
    "dimensionless": DIMENSIONLESS,
    "length": (_F1, _F0, _F0, _F0),
    "mass": (_F0, _F1, _F0, _F0),
    "time": (_F0, _F0, _F1, _F0),
    "temperature": (_F0, _F0, _F0, _F1),
}


def dim_add(a: tuple, b: tuple) -> tuple:
    return tuple(x + y for x, y in zip(a, b))


def dim_sub(a: tuple, b: tuple) -> tuple:
    return tuple(x - y for x, y in zip(a, b))


def dim_scale(a: tuple, n: Fraction) -> tuple:
    return tuple(x * n for x in a)


def dim_to_string(dim: tuple) -> str:
    if dim == DIMENSIONLESS:
        return "dimensionless"
    parts = []
    for name, exp in zip(DIM_NAMES, dim):
        if exp == 0:
            continue
        if exp == 1:
            parts.append(name)
        elif exp.denominator == 1:
            parts.append(f"{name}^{exp.numerator}")
        else:
            parts.append(f"{name}^({exp.numerator}/{exp.denominator})")
    return "*".join(parts)


class UnitError(ValueError):
    """单位串非法（未知单位、指数非法、偏移单位参与组合等）。"""


@dataclass(frozen=True)
class Unit:
    name: str
    dim: tuple
    scale: float          # value_si = scale * value + offset
    offset: float = 0.0

    def to_si(self, value: float) -> float:
        return self.scale * value + self.offset

    def u_to_si(self, u: float) -> float:
        # 不确定度只随刻度缩放，永远不加偏移（degC 的 273.15 不进入 u）
        return self.scale * u

    def from_si(self, value_si: float) -> float:
        return (value_si - self.offset) / self.scale

    def u_from_si(self, u_si: float) -> float:
        return u_si / self.scale


BASE_UNITS: dict[str, Unit] = {
    "m": Unit("m", DIMENSIONS["length"], 1.0),
    "cm": Unit("cm", DIMENSIONS["length"], 0.01),
    "mm": Unit("mm", DIMENSIONS["length"], 0.001),
    "s": Unit("s", DIMENSIONS["time"], 1.0),
    "ms": Unit("ms", DIMENSIONS["time"], 0.001),
    "kg": Unit("kg", DIMENSIONS["mass"], 1.0),
    "g": Unit("g", DIMENSIONS["mass"], 0.001),
    "K": Unit("K", DIMENSIONS["temperature"], 1.0),
    # 绝对摄氏温度：值加 273.15，u 不加（见 Unit.u_to_si）
    "degC": Unit("degC", DIMENSIONS["temperature"], 1.0, 273.15),
}


@dataclass(frozen=True)
class DisplayUnit:
    """复合显示单位：dim 必须与被显示量的推导量纲一致（调用方校验）。"""
    text: str
    dim: tuple
    scale: float
    offset: float = 0.0

    def from_si(self, value_si: float) -> float:
        return (value_si - self.offset) / self.scale

    def u_from_si(self, u_si: float) -> float:
        return u_si / self.scale


def _parse_factor(token: str) -> tuple[Unit, int]:
    """解析一个单位因子：NAME 或 NAME^整数。"""
    if "^" in token:
        name, _, exp_text = token.partition("^")
        if not exp_text or exp_text in ("+", "-"):
            raise UnitError(f"单位指数非法: {token!r}")
        try:
            exp = int(exp_text)
        except ValueError:
            raise UnitError(f"单位指数必须是整数: {token!r}") from None
    else:
        name, exp = token, 1
    unit = BASE_UNITS.get(name)
    if unit is None:
        raise UnitError(f"未知单位: {name!r}（支持: {', '.join(BASE_UNITS)}）")
    if exp == 0:
        raise UnitError(f"单位指数不允许为 0: {token!r}")
    return unit, exp


def parse_display_unit(text: str) -> DisplayUnit:
    """解析显示单位串，如 "m"、"cm"、"m^2"、"kg*m^2/s^2"、"m/s"、"1"。

    语法：factor (('*'|'/') factor)*，factor = NAME 或 NAME^有符号整数。
    带偏移的单位（degC）只能单独出现（指数 1、不与其它因子组合）。
    """
    raw = text
    text = text.strip()
    if text in ("", "1"):
        return DisplayUnit(raw, DIMENSIONLESS, 1.0, 0.0)

    # 以 * 和 / 切分，保留运算符
    tokens: list[str] = []
    ops: list[str] = []
    buf = ""
    for ch in text:
        if ch in "*/":
            tokens.append(buf.strip())
            ops.append(ch)
            buf = ""
        else:
            buf += ch
    tokens.append(buf.strip())
    if any(t == "" for t in tokens):
        raise UnitError(f"显示单位语法非法: {raw!r}")

    dim = DIMENSIONLESS
    scale = 1.0
    offset = 0.0
    offset_used = False
    n_factors = 0
    for idx, token in enumerate(tokens):
        sign = 1 if idx == 0 else (1 if ops[idx - 1] == "*" else -1)
        if token == "1":
            continue  # 无量纲因子（如 "1/kg"）
        unit, exp = _parse_factor(token)
        exp *= sign
        n_factors += 1
        if unit.offset != 0.0:
            if n_factors > 1 or exp != 1:
                raise UnitError(
                    f"带偏移的单位 {unit.name!r} 不能参与组合或取幂: {raw!r}")
            offset_used = True
            offset = unit.offset
        dim = dim_add(dim, dim_scale(unit.dim, Fraction(exp)))
        scale *= unit.scale ** exp
    if offset_used and (dim != DIMENSIONS["temperature"]):
        raise UnitError(f"带偏移单位组合非法: {raw!r}")
    return DisplayUnit(raw, dim, scale, offset)
