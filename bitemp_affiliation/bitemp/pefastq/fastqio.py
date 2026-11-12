"""FASTQ 源读取：严格四物理行一块，普通文件与 gzip 均可。

不支持的恢复方式：绝不通过"搜索下一个 @"猜测记录边界——质量串本身
可以以 @ 或 + 开头，只有固定的四行分块是可靠的。源文件未完整读到
末尾（缺文件、截断的四行记录、gzip 解压错误、其它 IO 错误）时抛
SourceFailure，整次运行必须失败，不得当作正常文件结束。
"""
from __future__ import annotations

import gzip
import zlib
from pathlib import Path


class SourceFailure(Exception):
    """源文件未能完整读到末尾。"""


def iter_blocks(path):
    """产出 (rec_no, start_line, [4 条字节行])；源不完整时抛 SourceFailure。

    rec_no      记录在本源内的序号（1 起）
    start_line  本记录第一行在（解压后）流中的物理行号（1 起）
    """
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    buf: list[bytes] = []
    rec_no = 0
    lineno = 0
    block_start = 0
    try:
        with opener(path, "rb") as f:
            for raw in f:
                lineno += 1
                if not buf:
                    block_start = lineno
                buf.append(raw.rstrip(b"\r\n"))
                if len(buf) == 4:
                    rec_no += 1
                    yield rec_no, block_start, buf
                    buf = []
    except (OSError, EOFError, zlib.error) as e:
        raise SourceFailure(
            f"源文件 {path} 未完整读完（已读 {lineno} 行）: {e}") from e
    if buf:
        raise SourceFailure(
            f"源文件 {path} 在记录中间结束：第 {block_start} 行起只剩 "
            f"{len(buf)} 个物理行（每条记录应为 4 行）")
