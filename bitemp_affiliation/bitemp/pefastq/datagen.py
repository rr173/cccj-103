"""测试数据生成：小样本（覆盖全部约定情形）、双端远距离到达的大样本、
截断 gzip 失败样本。

大样本的记录内容由 (样本, 读段名, 端) 经 SHA-256 确定，核对程序可以不
依赖生产代码重新推出应有内容。
"""
from __future__ import annotations

import gzip
import hashlib
from pathlib import Path

# ---------------------------------------------------------------- 小样本
# 覆盖：乱序配对、跨分片相同重传、后到冲突、跨样本同名、缺端、非法端标记、
# 质量串以 @ 开头、单端过短。配套参数：min_qual=20, min_len=8。

DEMO_MIN_QUAL = 20
DEMO_MIN_LEN = 8

_D1_R1 = """\
@readA/1 1:N:0:ATCACG
ACGTACGTAC
+readA/1 1:N:0:ATCACG
IIIIIIIIII
@readB/1
GGGGCCCCAA
+
IIIIIIIIII
@readC/1 1:N:0:ATCACG
CCCCCGGGGT
+
IIIIIIIIII
@readD/1
ACACACACAC
+
IIIIIIIIII
@readE
NNNNACGTAC
+
IIIIIIIIII
@readF/2
ACGTACGTAA
+
IIIIIIIIII
@readG 1:N:0:ATCACG
ACGTACGTAC
+
@@@@@@@@!!
@readH/1
ACGTACGTAC
+
IIIIIIIIII
@readI/1 2:N:0:ATCACG
ACGTACGTAC
+
IIIIIIIIII
@readJ/1
ACGTACGTAC
+readJ/2
IIIIIIIIII
"""

# readB 的 R1 重传（gzip，序列小写，规范化后与 d1 中那份完全相同 -> 合并）
_D2_R1_GZ = """\
@readB/1
ggggccccaa
+
IIIIIIIIII
"""

# R2 反序到达（与 R1 相距很远）
_D3_R2 = """\
@readJ/2
TGCATGCATG
+
IIIIIIIIII
@readI/2
TGCATGCATG
+
IIIIIIIIII
@readH/2 2:N:0:ATCACG
GGGGGAAAAA
+
IIIII!!!!!
@readG 2:N:0:ATCACG
TTTTGGGGCC
+
5555555555
@readF/2
TGCATGCATG
+
IIIIIIIIII
@readD/2
GTGTGTGTGT
+
IIIIIIIIII
@readC/2 2:N:0:ATCACG
AAAACCCCGG
+
IIIIIIII!!
@readB/2
TTGGCCAATT
+
IIIIIIIIII
@readA/2 2:N:0:ATCACG
tgcatgcatg
+
IIIIIIIIII
"""

# readC 的 R2 后到冲突：只在会被 3' 端修剪掉的尾巴上不同，仍必须记冲突
_D4_R2_GZ = """\
@readC/2 2:N:0:ATCACG
AAAACCCCAA
+
IIIIIIII!!
"""

# 跨样本同名：DEMO2 的 readD 与 DEMO1 的 readD 互不相干；DEMO2 缺 R2 -> 孤端
_D5_DEMO2_R1 = """\
@readD/1
TTTTCCCCAA
+
IIIIIIIIII
"""

_DEMO_MANIFEST = """\
source_id,sample,mate,path
d1,DEMO1,1,shards/d1_r1.fq
d2,DEMO1,1,shards/d2_r1.fq.gz
d3,DEMO1,2,shards/d3_r2.fq
d4,DEMO1,2,shards/d4_r2.fq.gz
d5,DEMO2,1,shards/d5_demo2_r1.fq
"""

# 每个键的预期终态（供演示与测试对账）
DEMO_EXPECTED_STATES = {
    ("DEMO1", "readA"): "paired",     # 乱序配对；R2 序列小写被规范化
    ("DEMO1", "readB"): "paired",     # R1 跨分片相同重传，合并（d1#2 + d2#1）
    ("DEMO1", "readC"): "conflict",   # R2 后到冲突（仅修剪尾巴不同也不合并）
    ("DEMO1", "readD"): "paired",     # 与 DEMO2 的同名 readD 互不相干
    ("DEMO1", "readE"): "orphan",     # 缺 R2
    ("DEMO1", "readF"): "rejected",   # R1 头部 /2 与清单 mate=1 矛盾
    ("DEMO1", "readG"): "paired",     # 质量串以 @ 开头；R1 修剪 2 碱基
    ("DEMO1", "readH"): "filtered",   # R2 清洗后 5 < min_len=8
    ("DEMO1", "readI"): "rejected",   # 头部 /1 与注释 2:N:0:... 互相矛盾
    ("DEMO1", "readJ"): "rejected",   # '+' 行重复的头与本记录不一致
    ("DEMO2", "readD"): "orphan",     # 跨样本同名，各算各的
}


def write_demo_dataset(root) -> Path:
    root = Path(root)
    shards = root / "shards"
    shards.mkdir(parents=True, exist_ok=True)
    (shards / "d1_r1.fq").write_text(_D1_R1, encoding="utf-8")
    with gzip.open(shards / "d2_r1.fq.gz", "wb") as f:
        f.write(_D2_R1_GZ.encode())
    (shards / "d3_r2.fq").write_text(_D3_R2, encoding="utf-8")
    with gzip.open(shards / "d4_r2.fq.gz", "wb") as f:
        f.write(_D4_R2_GZ.encode())
    (shards / "d5_demo2_r1.fq").write_text(_D5_DEMO2_R1, encoding="utf-8")
    (root / "manifest.csv").write_text(_DEMO_MANIFEST, encoding="utf-8")
    return root / "manifest.csv"


# ---------------------------------------------------------------- 大样本
BIG_SAMPLE = "BIG"
BIG_MIN_QUAL = 20
BIG_MIN_LEN = 30


def _bytes(seed: str, n: int) -> bytes:
    out = b""
    counter = 0
    while len(out) < n:
        out += hashlib.sha256(f"{seed}|{counter}".encode()).digest()
        counter += 1
    return out[:n]


def big_content(name: str, mate: int, i: int,
                len_lo: int = 60, len_hi: int = 100) -> tuple[str, str]:
    """由 (读段名, 端, 序号) 确定性地生成 (序列, 质量)。

    类别（按 i % 100）：
      0,1   孤端（不生成 R2）
      2     R2 几乎全低质量 -> 清洗后过短 -> filtered
      3,4,5 3' 端带低质量尾巴 -> 修剪后仍合格
      6,7   R1 会被额外分片重传（内容相同 -> 合并）
      其余  普通合格对
    """
    raw = _bytes(f"{BIG_SAMPLE}|{name}|{mate}", len_hi + 8)
    length = len_lo + raw[0] % (len_hi - len_lo + 1)
    seq = "".join("ACGT"[b & 3] for b in raw[1:1 + length])
    qual = ["I"] * length
    cat = i % 100
    if cat == 2 and mate == 2:
        qual = ["I"] * 5 + ["!"] * (length - 5)      # 清洗后只剩 5 < min_len
    elif cat in (3, 4, 5):
        tail = 3 + raw[-1] % 6                        # 3..8 个低质量尾巴
        qual[-tail:] = ["!"] * tail
    return seq, "".join(qual)


def big_category(i: int) -> str:
    cat = i % 100
    if cat in (0, 1):
        return "orphan"
    if cat == 2:
        return "filtered"
    return "paired"


def write_big_dataset(root, n_pairs: int = 200000, seed: int = 7,
                      n_shards: int = 3) -> Path:
    """双端远距离到达：R1 分片按名字正序，R2 分片按名字反序。"""
    root = Path(root)
    shards = root / "shards"
    shards.mkdir(parents=True, exist_ok=True)
    manifest_lines = ["source_id,sample,mate,path"]

    def record_lines(i: int, mate: int) -> str:
        name = f"read{i:08d}"
        seq, qual = big_content(name, mate, i)
        return f"@{name}/{mate}\n{seq}\n+\n{qual}\n"

    # R1：正序写入 n_shards 个分片（.fq 与 .fq.gz 交替）
    per = (n_pairs + n_shards - 1) // n_shards
    for s in range(n_shards):
        lo, hi = s * per, min((s + 1) * per, n_pairs)
        if lo >= hi:
            break
        gz = s % 2 == 1
        fname = f"big_r1_{s}.fq" + (".gz" if gz else "")
        text = "".join(record_lines(i, 1) for i in range(lo, hi))
        if gz:
            with gzip.open(shards / fname, "wb") as f:
                f.write(text.encode())
        else:
            (shards / fname).write_text(text, encoding="utf-8")
        manifest_lines.append(f"r1_{s},{BIG_SAMPLE},1,shards/{fname}")

    # R2：反序写入（最后生成的名字最先到达），孤端跳过
    order = [i for i in reversed(range(n_pairs)) if big_category(i) != "orphan"]
    per2 = (len(order) + n_shards - 1) // n_shards
    for s in range(n_shards):
        chunk = order[s * per2:(s + 1) * per2]
        if not chunk:
            break
        gz = s % 2 == 1
        fname = f"big_r2_{s}.fq" + (".gz" if gz else "")
        text = "".join(record_lines(i, 2) for i in chunk)
        if gz:
            with gzip.open(shards / fname, "wb") as f:
                f.write(text.encode())
        else:
            (shards / fname).write_text(text, encoding="utf-8")
        manifest_lines.append(f"r2_{s},{BIG_SAMPLE},2,shards/{fname}")

    # 重传分片：cat 为 6/7 的 R1 再交一份（gzip，内容完全相同 -> 合并）
    dup = [i for i in range(n_pairs) if i % 100 in (6, 7)]
    text = "".join(record_lines(i, 1) for i in dup)
    with gzip.open(shards / "big_r1_dup.fq.gz", "wb") as f:
        f.write(text.encode())
    manifest_lines.append(f"r1_dup,{BIG_SAMPLE},1,shards/big_r1_dup.fq.gz")

    (root / "manifest.csv").write_text("\n".join(manifest_lines) + "\n",
                                       encoding="utf-8")
    return root / "manifest.csv"


# ---------------------------------------------------------------- 截断 gzip
def write_truncated_gzip_dataset(root) -> Path:
    """一个源是截断的 gzip：整次运行必须失败。"""
    root = Path(root)
    shards = root / "shards"
    shards.mkdir(parents=True, exist_ok=True)
    good = "@ok1/1\nACGTACGTAC\n+\nIIIIIIIIII\n@ok2/1\nACGTACGTAC\n+\nIIIIIIIIII\n"
    (shards / "good_r1.fq").write_text(good, encoding="utf-8")
    payload = ("@t1/2\nACGTACGTAC\n+\nIIIIIIIIII\n"
               "@t2/2\nACGTACGTAC\n+\nIIIIIIIIII\n").encode()
    tmp = shards / "._full.gz"
    with gzip.open(tmp, "wb") as f:
        f.write(payload)
    raw = tmp.read_bytes()
    tmp.unlink()
    (shards / "trunc_r2.fq.gz").write_bytes(raw[:len(raw) - 7])  # 截断
    (root / "manifest.csv").write_text(
        "source_id,sample,mate,path\n"
        "g1,TRUNC,1,shards/good_r1.fq\n"
        "g2,TRUNC,2,shards/trunc_r2.fq.gz\n", encoding="utf-8")
    return root / "manifest.csv"
