"""带行号的 JSON 解析。

只用于把输入文件解析成 Python 对象并记住每个对象/数组出现的行号，
让配置错误能定位到交来的材料（文件 + 行号）。手写递归下降，
不借助 eval/exec；语义与 json 模块一致（对象键重复时后者覆盖前者）。

解析结果中，对象返回 JObject（dict 子类，带 .line），数组返回
JArray（list 子类，带 .line），标量就是普通 str/float/int/bool/None。
"""
from __future__ import annotations


class JObject(dict):
    __slots__ = ("line",)

    def __init__(self, line: int):
        super().__init__()
        self.line = line


class JArray(list):
    __slots__ = ("line",)

    def __init__(self, line: int):
        super().__init__()
        self.line = line


class JsonSyntaxError(ValueError):
    def __init__(self, message: str, line: int):
        super().__init__(f"第 {line} 行: {message}")
        self.message = message
        self.line = line


_HEX = "0123456789abcdefABCDEF"
_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f",
            "n": "\n", "r": "\r", "t": "\t"}


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.n = len(text)
        self.i = 0
        self.line = 1

    # ---------------------------------------------------------- 基础
    def _error(self, msg: str):
        raise JsonSyntaxError(msg, self.line)

    def _peek(self) -> str:
        return self.text[self.i] if self.i < self.n else ""

    def _advance(self) -> str:
        ch = self.text[self.i]
        self.i += 1
        if ch == "\n":
            self.line += 1
        return ch

    def _skip_ws(self):
        while self.i < self.n and self.text[self.i] in " \t\r\n":
            self._advance()

    def _expect(self, ch: str):
        if self._peek() != ch:
            self._error(f"期望 {ch!r}，实际为 {self._peek()!r}")
        self._advance()

    # ---------------------------------------------------------- 值
    def parse(self):
        self._skip_ws()
        value = self._value()
        self._skip_ws()
        if self.i != self.n:
            self._error("JSON 内容之后还有多余字符")
        return value

    def _value(self):
        ch = self._peek()
        if ch == "{":
            return self._object()
        if ch == "[":
            return self._array()
        if ch == '"':
            return self._string()
        if ch == "t":
            self._literal("true")
            return True
        if ch == "f":
            self._literal("false")
            return False
        if ch == "n":
            self._literal("null")
            return None
        if ch == "-" or ch.isdigit():
            return self._number()
        self._error(f"无法识别的值起始字符 {ch!r}")

    def _literal(self, word: str):
        if self.text[self.i:self.i + len(word)] != word:
            self._error(f"无法识别的字面量（期望 {word}）")
        for _ in word:
            self._advance()

    def _object(self) -> JObject:
        obj = JObject(self.line)
        self._expect("{")
        self._skip_ws()
        if self._peek() == "}":
            self._advance()
            return obj
        while True:
            self._skip_ws()
            if self._peek() != '"':
                self._error("对象键必须是字符串")
            key = self._string()
            self._skip_ws()
            self._expect(":")
            self._skip_ws()
            obj[key] = self._value()
            self._skip_ws()
            ch = self._peek()
            if ch == ",":
                self._advance()
                continue
            if ch == "}":
                self._advance()
                return obj
            self._error(f"对象中期望 ',' 或 '}}'，实际为 {ch!r}")

    def _array(self) -> JArray:
        arr = JArray(self.line)
        self._expect("[")
        self._skip_ws()
        if self._peek() == "]":
            self._advance()
            return arr
        while True:
            self._skip_ws()
            arr.append(self._value())
            self._skip_ws()
            ch = self._peek()
            if ch == ",":
                self._advance()
                continue
            if ch == "]":
                self._advance()
                return arr
            self._error(f"数组中期望 ',' 或 ']'，实际为 {ch!r}")

    def _string(self) -> str:
        self._expect('"')
        out = []
        while True:
            if self.i >= self.n:
                self._error("字符串未闭合")
            ch = self._advance()
            if ch == '"':
                return "".join(out)
            if ch == "\\":
                esc = self._advance() if self.i < self.n else ""
                if esc in _ESCAPES:
                    out.append(_ESCAPES[esc])
                elif esc == "u":
                    out.append(self._unicode_escape())
                else:
                    self._error(f"非法转义 \\{esc}")
            elif ord(ch) < 0x20:
                self._error("字符串中含未转义的控制字符")
            else:
                out.append(ch)

    def _unicode_escape(self) -> str:
        def hex4() -> int:
            if self.i + 4 > self.n:
                self._error("\\u 转义不足 4 位")
            s = self.text[self.i:self.i + 4]
            if any(c not in _HEX for c in s):
                self._error("\\u 转义含非十六进制字符")
            for _ in range(4):
                self._advance()
            return int(s, 16)

        code = hex4()
        if 0xD800 <= code <= 0xDBFF and self.text[self.i:self.i + 2] == "\\u":
            self._advance()
            self._advance()
            low = hex4()
            if 0xDC00 <= low <= 0xDFFF:
                code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
            else:
                # 高 surrogates 后未跟低 surrogates：按两个独立码位处理
                return chr(code) + chr(low)
        return chr(code)

    def _number(self):
        start = self.i
        if self._peek() == "-":
            self._advance()
        if self._peek() == "0":
            self._advance()
        elif self._peek().isdigit():
            while self._peek().isdigit():
                self._advance()
        else:
            self._error("数字格式非法")
        is_float = False
        if self._peek() == ".":
            is_float = True
            self._advance()
            if not self._peek().isdigit():
                self._error("小数点后缺少数字")
            while self._peek().isdigit():
                self._advance()
        if self._peek() in ("e", "E"):
            is_float = True
            self._advance()
            if self._peek() in ("+", "-"):
                self._advance()
            if not self._peek().isdigit():
                self._error("指数部分缺少数字")
            while self._peek().isdigit():
                self._advance()
        text = self.text[start:self.i]
        return float(text) if is_float else int(text)


def parse(text: str):
    """解析 JSON 文本；对象/数组带 .line 行号（1 起）。"""
    return _Parser(text).parse()
