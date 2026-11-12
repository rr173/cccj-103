"""双端读段整理：头部解析、记录校验与 3' 端质量清洗（纯函数层）。

本模块不接触文件系统与 DuckDB，便于独立测试。

支持的头部格式（'@' 之后的文本）：

    NAME                          无端标记，以清单 mate 为准
    NAME/1   NAME/2               核心标识末尾的 /1、/2 端标记
    NAME 1:N:0:INDEX              空白后第一个 token 以 "1:"/"2:" 开头（Casava 1.8 风格）
    NAME/2 2:N:0:INDEX            两种标记同时出现时必须一致，否则记录非法
    NAME 其它任意注释              注释不含端标记时以清单 mate 为准

规范化读段名 = 空白前第一个 token 去掉末尾至多一个 /1 或 /2。
配对键 = (样本, 规范化读段名)，不跨样本串配；原始头部完整保留在库中供追查。

记录约定（本批数据）：每条记录恰好四个物理行（@头 / 序列 / +行 / 质量串），
不支持折行；序列只含 A/C/G/T/N（大小写统一为大写）；质量为 Phred+33，
字符范围 ASCII 33..74，与序列等长且非空；+ 行若重复头信息必须与 '@' 后
文本完全一致。质量串本身可以以 @ 或 + 开头——解析按固定四行分块，
绝不通过"搜索下一个 @"猜测记录边界。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------- 终态
# 每个 (样本, 读段名) 键恰好落入一种终态；判定优先级即下列顺序。
STATE_REJECTED = "rejected"    # 键内存在能确定名字的非法记录
STATE_CONFLICT = "conflict"    # 同一端出现内容不同的记录（哪怕清洗后会相同）
STATE_FILTERED = "filtered"    # 两端唯一合法，但清洗后任一端短于最短保留长度
STATE_PAIRED = "paired"        # 两端唯一合法且通过清洗，进入合格 FASTQ
STATE_ORPHAN = "orphan"        # 只有一端且没有其他错误

STATES = (STATE_PAIRED, STATE_FILTERED, STATE_ORPHAN, STATE_CONFLICT, STATE_REJECTED)

SEQ_ALPHABET = frozenset("ACGTN")
QUAL_MIN = 33                  # Phred+33 下界（'!'）
QUAL_MAX = 74                  # Phred+33 上界（'J'，即 Q41）

_SUFFIX_RE = re.compile(r"^(.*)/([12])$")   # 核心标识末尾的 /1 或 /2
_CASAVA_RE = re.compile(r"^([12]):")        # 注释首 token 形如 1:N:0:INDEX
_SEQ_RE = re.compile(r"^[ACGTNacgtn]+$")    # 序列字母表（大小写）
_QUAL_RE = re.compile(r"^[!-J]+$")          # Phred+33：ASCII 33('!')..74('J')


class RecordInvalid(Exception):
    """单条记录非法。name 为能确定名字时的规范化读段名（否则 None）。"""

    def __init__(self, code: str, message: str, name: str | None = None):
        super().__init__(message)
        self.code = code
        self.name = name


@dataclass
class ParsedHeader:
    header: str          # '@' 之后的完整原始头文本
    name: str            # 规范化读段名
    marker: int | None   # 头部携带的端标记（1/2）；无标记为 None


@dataclass
class CleanRecord:
    name: str
    header: str          # 原始头文本（'@' 之后），供追查
    seq: str             # 规范化序列（大写）
    qual: str


def parse_header(text: str) -> ParsedHeader:
    """解析 '@' 之后的头文本。非法时抛 RecordInvalid。"""
    if text == "":
        raise RecordInvalid("E_HEADER_EMPTY", "头部为空（'@' 后无内容）")
    parts = text.split(None, 1)
    core = parts[0]
    comment = parts[1] if len(parts) == 2 else None

    m = _SUFFIX_RE.match(core)
    if m:
        name, suffix_marker = m.group(1), int(m.group(2))
    else:
        name, suffix_marker = core, None
    if name == "":
        raise RecordInvalid("E_NAME_EMPTY", "去掉末尾 /1|/2 后读段名为空")

    casava = None
    if comment:
        tok = comment.split(None, 1)[0]
        cm = _CASAVA_RE.match(tok)
        if cm:
            casava = int(cm.group(1))
    if suffix_marker is not None and casava is not None and suffix_marker != casava:
        raise RecordInvalid(
            "E_MARKERS_CONTRADICT",
            f"头部端标记互相矛盾：后缀 /{suffix_marker} 与注释 {casava}:... 不一致",
            name=name)
    marker = suffix_marker if suffix_marker is not None else casava
    return ParsedHeader(header=text, name=name, marker=marker)


def parse_and_validate(header_line: str, seq_line: str, plus_line: str,
                       qual_line: str, manifest_mate: int) -> CleanRecord:
    """校验一条四行记录并返回规范化结果；非法时抛 RecordInvalid。

    manifest_mate 为清单中该源的端号（1/2）。头部若带端标记，必须与清单一致。
    """
    if not header_line.startswith("@"):
        raise RecordInvalid("E_HEADER_AT",
                            "头行不以 '@' 开头（本批数据每条记录恰好四行，不支持折行）")
    ph = parse_header(header_line[1:])
    name = ph.name
    if ph.marker is not None and ph.marker != manifest_mate:
        raise RecordInvalid(
            "E_MARKER_MANIFEST",
            f"头部端标记 {ph.marker} 与清单 mate={manifest_mate} 不一致", name=name)

    seq = seq_line.upper()
    if seq == "":
        raise RecordInvalid("E_SEQ_EMPTY", "序列为空", name=name)
    if not _SEQ_RE.match(seq_line):
        bad = sorted(set(seq) - SEQ_ALPHABET)
        raise RecordInvalid(
            "E_SEQ_CHAR",
            f"序列含非法字符 {''.join(bad)!r}（只接受 A/C/G/T/N，大小写不限）",
            name=name)

    if not plus_line.startswith("+"):
        raise RecordInvalid("E_PLUS", "第三行不以 '+' 开头", name=name)
    if plus_line != "+" and plus_line[1:] != ph.header:
        raise RecordInvalid(
            "E_PLUS", "'+' 行重复的头信息与本记录头部不一致", name=name)

    if len(qual_line) != len(seq):
        raise RecordInvalid(
            "E_QUAL_LEN",
            f"质量串长度 {len(qual_line)} 与序列长度 {len(seq)} 不等", name=name)
    if not _QUAL_RE.match(qual_line):
        for i, ch in enumerate(qual_line):
            o = ord(ch)
            if o < QUAL_MIN or o > QUAL_MAX:
                raise RecordInvalid(
                    "E_QUAL_CHAR",
                    f"质量串第 {i + 1} 个字符 {ch!r}（ASCII {o}）"
                    f"超出 Phred+33 范围 33..74", name=name)
    return CleanRecord(name=name, header=ph.header, seq=seq, qual=qual_line)


def trim_3prime(seq: str, qual: str, min_qual: int) -> tuple[str, str]:
    """只从 3' 末尾连续删除质量低于 min_qual 的碱基及对应质量字符。

    遇到第一个合格碱基（质量 >= min_qual）即停止，不改中间的低质量位置。
    """
    i = len(qual)
    while i > 0 and ord(qual[i - 1]) - 33 < min_qual:
        i -= 1
    return seq[:i], qual[:i]


def low_qual_regex(min_qual: int) -> str:
    """与 trim_3prime 等价的 SQL 正则：匹配 3' 端连续低质量字符尾巴（组 1）。"""
    if min_qual <= 0:
        return "()"           # 只匹配空串：不修剪
    hi = min(QUAL_MIN + min_qual, QUAL_MAX + 1)   # 字符 < hi 为低质量
    chars = "".join(
        ("\\" + chr(c)) if chr(c) in "\\^-]" else chr(c)
        for c in range(QUAL_MIN, hi))
    return f"([{chars}]*)$"
