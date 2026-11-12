"""指标表达式：词法/语法分析、量纲推导、一阶自动微分求值。

允许的语法（其余一律拒绝，不使用 eval/exec）：
  - 数值常量：123、1.5、.5、1e-3 等（无量纲）
  - 名字：原始变量或其它指标
  - 二元运算：+ - * /
  - 一元正负号：-x、+x
  - 整数次幂：x ** 2、x ** -1、x ^ 3（指数必须是有符号整数字面量）
  - 平方根：sqrt(x)
  - 括号

量纲规则：加减要求两侧量纲相同；乘除按指数向量加减；整数次幂按指数
缩放；sqrt 量纲减半；数值常量无量纲（因此 L1 + 1 这类是量纲错误，
除非 L1 本身无量纲）。

求值采用前向自动微分：每个节点返回 (值, {原始变量: 偏导})，梯度以
原始变量为坐标稀疏累加——同一原始变量沿不同依赖路径出现时，偏导在
字典相加时自然合并；引用其它指标时直接代入该指标对原始量的梯度，
中间指标从不被当成相互独立的新测量量。
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .units import DIMENSIONLESS, dim_add, dim_sub, dim_scale, dim_to_string


class ExprError(ValueError):
    """表达式语法或量纲错误（配置错误）。"""


class DomainFailure(Exception):
    """求值域失败（运行时、按指标局部失败）：除零、sqrt 参数 <= 0、
    0 的负整数次幂等。chain 记录指标依赖链（不含出问题的名字本身，
    由调用方逐层补上）。"""

    def __init__(self, reason: str, chain: list[str] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.chain = chain if chain is not None else []


# ---------------------------------------------------------------- 词法
@dataclass(frozen=True)
class _Token:
    kind: str   # "num" | "name" | "op"
    text: str
    pos: int


def _tokenize(src: str) -> list[_Token]:
    tokens: list[_Token] = []
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if ch.isdigit() or (ch == "." and i + 1 < n and src[i + 1].isdigit()):
            j = i
            while j < n and (src[j].isdigit() or src[j] == "."):
                j += 1
            if src[i:j].count(".") > 1:
                raise ExprError(f"数值常量非法: {src[i:j]!r}")
            if j < n and src[j] in "eE":
                k = j + 1
                if k < n and src[k] in "+-":
                    k += 1
                if k >= n or not src[k].isdigit():
                    raise ExprError(f"数值常量指数部分非法: {src[i:k]!r}")
                while k < n and src[k].isdigit():
                    k += 1
                j = k
            tokens.append(_Token("num", src[i:j], i))
            i = j
            continue
        if ch.isalpha() or ch == "_":
            j = i
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            tokens.append(_Token("name", src[i:j], i))
            i = j
            continue
        if src.startswith("**", i):
            tokens.append(_Token("op", "**", i))
            i += 2
            continue
        if ch in "+-*/^()":
            tokens.append(_Token("op", ch, i))
            i += 1
            continue
        raise ExprError(f"表达式含非法字符 {ch!r}（位置 {i}）")
    tokens.append(_Token("op", "<eof>", n))
    return tokens


# ---------------------------------------------------------------- 语法树
class Node:
    __slots__ = ()


@dataclass(frozen=True)
class Num(Node):
    value: float


@dataclass(frozen=True)
class Ref(Node):
    name: str


@dataclass(frozen=True)
class Bin(Node):
    op: str          # "+" | "-" | "*" | "/"
    left: Node
    right: Node


@dataclass(frozen=True)
class Neg(Node):
    arg: Node


@dataclass(frozen=True)
class Pow(Node):
    base: Node
    exponent: int    # 整数字面量指数


@dataclass(frozen=True)
class Sqrt(Node):
    arg: Node


def _parse(tokens: list[_Token]) -> Node:
    pos = 0

    def peek() -> _Token:
        return tokens[pos]

    def take() -> _Token:
        nonlocal pos
        tok = tokens[pos]
        pos += 1
        return tok

    def expect_op(text: str):
        tok = take()
        if tok.kind != "op" or tok.text != text:
            raise ExprError(f"期望 {text!r}，实际为 {tok.text!r}")

    def parse_expr() -> Node:
        node = parse_term()
        while peek().kind == "op" and peek().text in ("+", "-"):
            op = take().text
            node = Bin(op, node, parse_term())
        return node

    def parse_term() -> Node:
        node = parse_unary()
        while peek().kind == "op" and peek().text in ("*", "/"):
            op = take().text
            node = Bin(op, node, parse_unary())
        return node

    def parse_unary() -> Node:
        tok = peek()
        if tok.kind == "op" and tok.text == "-":
            take()
            return Neg(parse_unary())
        if tok.kind == "op" and tok.text == "+":
            take()
            return parse_unary()
        return parse_power()

    def parse_power() -> Node:
        base = parse_atom()
        tok = peek()
        if tok.kind == "op" and tok.text in ("**", "^"):
            take()
            sign = 1
            if peek().kind == "op" and peek().text in ("-", "+"):
                if take().text == "-":
                    sign = -1
            num = take()
            if num.kind != "num":
                raise ExprError("幂指数必须是整数字面量（如 x ** 2、x ** -1）")
            value = float(num.text)
            if not value.is_integer():
                raise ExprError(f"幂指数必须是整数，实际为 {num.text!r}")
            exponent = sign * int(value)
            return Pow(base, exponent)
        return base

    def parse_atom() -> Node:
        tok = take()
        if tok.kind == "num":
            return Num(float(tok.text))
        if tok.kind == "name":
            if peek().kind == "op" and peek().text == "(":
                take()
                arg = parse_expr()
                expect_op(")")
                if tok.text != "sqrt":
                    raise ExprError(f"未知函数 {tok.text!r}（仅支持 sqrt）")
                return Sqrt(arg)
            if tok.text == "sqrt":
                raise ExprError("sqrt 必须以 sqrt(表达式) 形式调用")
            return Ref(tok.text)
        if tok.kind == "op" and tok.text == "(":
            node = parse_expr()
            expect_op(")")
            return node
        raise ExprError(f"此处不允许出现 {tok.text!r}")

    node = parse_expr()
    if peek().kind != "op" or peek().text != "<eof>":
        raise ExprError(f"表达式在 {peek().text!r} 处无法继续解析")
    return node


def parse_expression(src: str) -> Node:
    if not src or not src.strip():
        raise ExprError("表达式为空")
    return _parse(_tokenize(src))


# ---------------------------------------------------------------- 量纲推导
def infer_dim(node: Node, env: dict[str, tuple]) -> tuple:
    """推导 node 的量纲；env 给出名字（原始变量/指标）的量纲。"""
    if isinstance(node, Num):
        return DIMENSIONLESS
    if isinstance(node, Ref):
        if node.name not in env:
            raise ExprError(f"引用了不存在的名字: {node.name!r}")
        return env[node.name]
    if isinstance(node, Neg):
        return infer_dim(node.arg, env)
    if isinstance(node, Bin):
        dl = infer_dim(node.left, env)
        dr = infer_dim(node.right, env)
        if node.op in ("+", "-"):
            if dl != dr:
                raise ExprError(
                    f"加减两侧量纲必须相同: {dim_to_string(dl)} 与 "
                    f"{dim_to_string(dr)}")
            return dl
        if node.op == "*":
            return dim_add(dl, dr)
        return dim_sub(dl, dr)
    if isinstance(node, Pow):
        return dim_scale(infer_dim(node.base, env), Fraction(node.exponent))
    if isinstance(node, Sqrt):
        return dim_scale(infer_dim(node.arg, env), Fraction(1, 2))
    raise ExprError(f"未知语法节点: {node!r}")


def referenced_names(node: Node) -> set[str]:
    if isinstance(node, Ref):
        return {node.name}
    if isinstance(node, (Num,)):
        return set()
    if isinstance(node, (Neg, Sqrt)):
        return referenced_names(node.arg)
    if isinstance(node, Bin):
        return referenced_names(node.left) | referenced_names(node.right)
    if isinstance(node, Pow):
        return referenced_names(node.base)
    raise ExprError(f"未知语法节点: {node!r}")


# ---------------------------------------------------------------- 求值（前向自动微分）
def evaluate(node: Node, resolve) -> tuple[float, dict[str, float]]:
    """对 node 求值，返回 (值, {原始变量: 偏导})。

    resolve(name) -> (value, grad_dict)：原始变量返回 (v, {name: 1.0})，
    指标返回其已算出的 (值, 对原始量的梯度)。缺读数/下层失败由 resolve
    抛 DomainFailure 向上传播。
    """
    if isinstance(node, Num):
        return node.value, {}
    if isinstance(node, Ref):
        return resolve(node.name)
    if isinstance(node, Neg):
        v, g = evaluate(node.arg, resolve)
        return -v, {k: -x for k, x in g.items()}
    if isinstance(node, Bin):
        v1, g1 = evaluate(node.left, resolve)
        v2, g2 = evaluate(node.right, resolve)
        if node.op == "+":
            g = dict(g1)
            for k, x in g2.items():
                g[k] = g.get(k, 0.0) + x
            return v1 + v2, g
        if node.op == "-":
            g = dict(g1)
            for k, x in g2.items():
                g[k] = g.get(k, 0.0) - x
            return v1 - v2, g
        if node.op == "*":
            g = dict()
            for k, x in g1.items():
                g[k] = g.get(k, 0.0) + x * v2
            for k, x in g2.items():
                g[k] = g.get(k, 0.0) + v1 * x
            return v1 * v2, g
        # "/"
        if v2 == 0.0:
            raise DomainFailure("除数为 0")
        g = dict()
        for k, x in g1.items():
            g[k] = g.get(k, 0.0) + x / v2
        for k, x in g2.items():
            g[k] = g.get(k, 0.0) - v1 * x / (v2 * v2)
        return v1 / v2, g
    if isinstance(node, Pow):
        v, g = evaluate(node.base, resolve)
        n = node.exponent
        if n == 0:
            return 1.0, {k: 0.0 for k in g}
        if v == 0.0 and n < 0:
            raise DomainFailure(f"0 的负整数次幂（{n}）无定义")
        factor = n * v ** (n - 1)
        return v ** n, {k: factor * x for k, x in g.items()}
    if isinstance(node, Sqrt):
        v, g = evaluate(node.arg, resolve)
        if v < 0.0:
            raise DomainFailure(f"sqrt 参数为负（{v!r}）")
        if v == 0.0:
            # 参数为 0 时值虽存在，但一阶导数不存在；本程序按不支持的
            # 求导边界处理，指标记为 unavailable
            raise DomainFailure("sqrt 参数为 0，一阶导数不存在（求导边界）")
        root = v ** 0.5
        factor = 0.5 / root
        return root, {k: factor * x for k, x in g.items()}
    raise ExprError(f"未知语法节点: {node!r}")
