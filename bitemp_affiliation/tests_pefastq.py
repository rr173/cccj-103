#!/usr/bin/env python3
"""验收测试：直接运行 python3 tests_pefastq.py。

覆盖：清单校验与相对路径解析；头部格式与端标记；四行严格解析（质量串以
@/+ 开头、不按 @ 猜测恢复）；截断记录与截断 gzip 整次失败且保留诊断；
乱序配对、跨分片重传合并、后到冲突、跨样本同名、缺端、非法记录拒收；
3' 端清洗边界；输出 UTF-8 字节序与逐条对应；统计对账；where 入口；
独立参考程序逐对核对；重跑重置。
"""
from __future__ import annotations

import gzip
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bitemp.pefastq import datagen, model, pipeline, refcheck, store  # noqa: E402
from bitemp.pefastq.fastqio import SourceFailure, iter_blocks  # noqa: E402
from bitemp.pefastq.manifest import ManifestFailure, load_manifest  # noqa: E402
from bitemp.pefastq.pipeline import PipelineFailure  # noqa: E402

EXAMPLE_DIR = Path(__file__).resolve().parent / "examples" / "pefastq"
PASSED = []


def check(name):
    def deco(fn):
        fn()
        PASSED.append(name)
        print(f"  ✔ {name}")
    return deco


def tmp_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="pefastq_test_"))


def write_source(path: Path, text: str, gz: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if gz:
        with gzip.open(path, "wb") as f:
            f.write(text.encode())
    else:
        path.write_text(text, encoding="utf-8")


def make_dataset(root: Path, sources: dict[str, str],
                 rows: list[tuple], gz: set[str] = frozenset()) -> Path:
    """sources: 相对路径 -> 内容；rows: (source_id, sample, mate, 相对路径)。"""
    for rel, text in sources.items():
        write_source(root / rel, text, rel in gz)
    lines = ["source_id,sample,mate,path"] + [
        f"{sid},{sample},{mate},{rel}" for sid, sample, mate, rel in rows]
    (root / "manifest.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root / "manifest.csv"


def run(manifest_path, root, **kw):
    kw.setdefault("min_qual", 20)
    kw.setdefault("min_len", 8)
    return pipeline.run(manifest_path, root / "out", str(root / "t.duckdb"), **kw)


def states(db_path) -> dict:
    con = store.connect_readonly(str(db_path))
    rows = con.execute("SELECT sample, name, state FROM key_states").fetchall()
    con.close()
    return {(s, n): st for s, n, st in rows}


def stat(db_path, scope, metric) -> int:
    con = store.connect_readonly(str(db_path))
    row = con.execute("SELECT value FROM stats WHERE scope=? AND metric=?",
                      [scope, metric]).fetchone()
    con.close()
    return row[0]


# ---------------------------------------------------------------- 清单
@check("清单：重复 source_id 拒收并报行号")
def _():
    root = tmp_dir()
    (root / "manifest.csv").write_text(
        "source_id,sample,mate,path\n"
        "a,S,1,x.fq\n"
        "b,S,2,y.fq\n"
        "a,S,1,z.fq\n", encoding="utf-8")
    try:
        load_manifest(root / "manifest.csv")
        raise AssertionError("应当拒收")
    except ManifestFailure as e:
        assert "重复" in str(e) and "第 4 行" in str(e)


@check("清单：mate 非 1/2 拒收；缺列拒收")
def _():
    root = tmp_dir()
    (root / "m1.csv").write_text("source_id,sample,mate,path\na,S,3,x.fq\n")
    try:
        load_manifest(root / "m1.csv")
        raise AssertionError("应当拒收")
    except ManifestFailure as e:
        assert "mate" in str(e)
    (root / "m2.csv").write_text("source_id,sample,path\na,S,x.fq\n")
    try:
        load_manifest(root / "m2.csv")
        raise AssertionError("应当拒收")
    except ManifestFailure as e:
        assert "缺少列" in str(e)


@check("清单：相对路径按清单所在位置解析（与 cwd 无关）")
def _():
    root = tmp_dir()
    sub = root / "sub"
    write_source(sub / "s" / "r1.fq", "@x/1\nACGTACGT\n+\nIIIIIIII\n")
    (sub / "manifest.csv").write_text(
        "source_id,sample,mate,path\na,S,1,s/r1.fq\n", encoding="utf-8")
    entries = load_manifest(sub / "manifest.csv")
    assert entries[0].resolved == sub / "s" / "r1.fq"
    summary = pipeline.run(sub / "manifest.csv", root / "out",
                           str(root / "t.duckdb"), min_qual=20, min_len=4)
    assert summary["records_total"] == 1


# ---------------------------------------------------------------- 头部与记录校验
@check("头部格式：NAME、NAME/1、Casava 注释、两者一致、无标记注释")
def _():
    h = model.parse_and_validate("@readA", "ACGT", "+", "IIII", 1)
    assert h.name == "readA"
    h = model.parse_and_validate("@readA/1", "ACGT", "+", "IIII", 1)
    assert h.name == "readA"
    h = model.parse_and_validate("@readA 1:N:0:ATCACG", "ACGT", "+", "IIII", 1)
    assert h.name == "readA"
    h = model.parse_and_validate("@readA/2 2:N:0:ATCACG", "ACGT", "+", "IIII", 2)
    assert h.name == "readA"
    h = model.parse_and_validate("@readA some other comment", "ACGT", "+",
                                 "IIII", 2)   # 注释无端标记，以清单为准
    assert h.name == "readA"


@check("'+ 行'重复头信息须与本记录一致")
def _():
    model.parse_and_validate("@r/1 1:N:0:X", "ACGT", "+r/1 1:N:0:X", "IIII", 1)
    try:
        model.parse_and_validate("@r/1 1:N:0:X", "ACGT", "+r/1", "IIII", 1)
        raise AssertionError("应当拒收")
    except model.RecordInvalid as e:
        assert e.code == "E_PLUS" and e.name == "r"


@check("质量串以 @ 或 + 开头都是合法质量，不当记录起点")
def _():
    h = model.parse_and_validate("@r/1", "ACGT", "+", "@III", 1)   # '@'=Q31
    assert h.qual == "@III"
    h = model.parse_and_validate("@r/1", "ACGT", "+", "+III", 1)   # '+'=Q10
    assert h.qual == "+III"
    # 端到端：质量串以 @ 开头的记录后面紧跟正常记录，不串行
    root = tmp_dir()
    m = make_dataset(root, {
        "a.fq": "@r1/1\nACGT\n+\n@III\n@r2/1\nTGCA\n+\nIIII\n",
        "b.fq": "@r1/2\nACGT\n+\nIIII\n@r2/2\nTGCA\n+\nIIII\n",
    }, [("a", "S", 1, "a.fq"), ("b", "S", 2, "b.fq")])
    summary = run(m, root, min_len=4)
    assert summary["records_valid"] == 4 and summary["keys_paired"] == 2


@check("非法序列字符 / 非法质量字符 / 长度不等 / 空序列")
def _():
    def bad(h, s, p, q, m=1):
        try:
            model.parse_and_validate(h, s, p, q, m)
            return None
        except model.RecordInvalid as e:
            return e.code
    assert bad("@r/1", "ACGX", "+", "IIII") == "E_SEQ_CHAR"
    assert bad("@r/1", "ACGT", "+", "IIIK") == "E_QUAL_CHAR"   # 'K'=75 > 74
    assert bad("@r/1", "ACGT", "+", "III ") == "E_QUAL_CHAR"   # 空格=32 < 33
    assert bad("@r/1", "ACGT", "+", "III") == "E_QUAL_LEN"
    assert bad("@r/1", "", "+", "") == "E_SEQ_EMPTY"
    assert bad("r/1", "ACGT", "+", "IIII") == "E_HEADER_AT"


@check("小写序列规范化为大写；N 合法")
def _():
    h = model.parse_and_validate("@r/1", "acgtNn", "+", "IIIIII", 1)
    assert h.seq == "ACGTNN"


@check("头部两处端标记互相矛盾为非法记录")
def _():
    try:
        model.parse_and_validate("@r/1 2:N:0:X", "ACGT", "+", "IIII", 1)
        raise AssertionError("应当拒收")
    except model.RecordInvalid as e:
        assert e.code == "E_MARKERS_CONTRADICT" and e.name == "r"


@check("端标记与清单 mate 不一致为非法记录")
def _():
    try:
        model.parse_and_validate("@r/2", "ACGT", "+", "IIII", 1)
        raise AssertionError("应当拒收")
    except model.RecordInvalid as e:
        assert e.code == "E_MARKER_MANIFEST" and e.name == "r"


@check("3' 端清洗：只剪末尾、遇合格即停、中间低质量保留、阈值边界")
def _():
    # '!'=Q0 '5'=Q20 'I'=Q40；阈值 20
    assert model.trim_3prime("ACGTAC", "IIII!!", 20) == ("ACGT", "IIII")
    assert model.trim_3prime("ACGTAC", "I!I!II", 20) == ("ACGTAC", "I!I!II")  # 中间不动
    assert model.trim_3prime("ACGTAC", "IIII55", 20) == ("ACGTAC", "IIII55")  # ==阈值保留
    assert model.trim_3prime("ACGTAC", "!!!!!!", 20) == ("", "")
    assert model.trim_3prime("ACGTAC", "IIIIII", 0) == ("ACGTAC", "IIIIII")   # 阈值 0 不剪


# ---------------------------------------------------------------- 源读取
@check("截断的四行记录：整次运行失败，诊断保留")
def _():
    root = tmp_dir()
    m = make_dataset(root, {
        "a.fq": "@r1/1\nACGTACGT\n+\nIIIIIIII\n@r2/1\nACGT\n",   # 第二条只有 3 行
        "b.fq": "@r1/2\nACGTACGT\n+\nIIIIIIII\n",
    }, [("a", "S", 1, "a.fq"), ("b", "S", 2, "b.fq")])
    try:
        run(m, root)
        raise AssertionError("应当失败")
    except SourceFailure as e:
        assert "记录中间结束" in str(e)
    con = store.connect_readonly(str(root / "t.duckdb"))
    row = con.execute("SELECT status, records FROM sources "
                      "WHERE source_id='a'").fetchone()
    assert row == ("failed", 1)                      # 已读 1 条保留作诊断
    n = con.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    con.close()
    assert n == 1 and not (root / "out" / "S_R1.fastq").exists()


@check("截断的 gzip：整次运行失败，不得当作正常文件结束")
def _():
    root = tmp_dir()
    payload = b"@r1/2\nACGTACGT\n+\nIIIIIIII\n"
    full = root / "full.gz"
    with gzip.open(full, "wb") as f:
        f.write(payload)
    raw = full.read_bytes()
    write_source(root / "a.fq", "@r1/1\nACGTACGT\n+\nIIIIIIII\n")
    (root / "b.fq.gz").write_bytes(raw[:len(raw) - 5])
    (root / "manifest.csv").write_text(
        "source_id,sample,mate,path\na,S,1,a.fq\nb,S,2,b.fq.gz\n",
        encoding="utf-8")
    try:
        run(root / "manifest.csv", root)
        raise AssertionError("应当失败")
    except SourceFailure as e:
        assert "未完整读完" in str(e)


@check("清单指向不存在的文件：整次运行失败")
def _():
    root = tmp_dir()
    (root / "manifest.csv").write_text(
        "source_id,sample,mate,path\na,S,1,nope.fq\n", encoding="utf-8")
    try:
        run(root / "manifest.csv", root)
        raise AssertionError("应当失败")
    except SourceFailure:
        pass


@check("不按 @ 猜测恢复：坏块独立拒收，后续四行块照常解析")
def _():
    root = tmp_dir()
    # 第 2 块的头行不是 @（该块非法）；第 3 块是完整合法记录
    m = make_dataset(root, {
        "a.fq": ("@r1/1\nACGTACGT\n+\nIIIIIIII\n"
                 "GARBAGE\nACGTACGT\n+\nIIIIIIII\n"
                 "@r2/1\nTTTTCCCC\n+\nIIIIIIII\n"),
        "b.fq": "@r1/2\nACGTACGT\n+\nIIIIIIII\n@r2/2\nTTTTCCCC\n+\nIIIIIIII\n",
    }, [("a", "S", 1, "a.fq"), ("b", "S", 2, "b.fq")])
    summary = run(m, root)
    assert summary["records_total"] == 5
    assert summary["records_invalid"] == 1
    st = states(root / "t.duckdb")
    assert st[("S", "r1")] == "paired" and st[("S", "r2")] == "paired"
    con = store.connect_readonly(str(root / "t.duckdb"))
    row = con.execute("SELECT err_code, start_line FROM records "
                      "WHERE NOT ok").fetchone()
    con.close()
    assert row == ("E_HEADER_AT", 5)                 # 指回起始行


# ---------------------------------------------------------------- 配对与分类
@check("乱序配对：输出顺序不取决于分片排列与到达顺序")
def _():
    root1, root2 = tmp_dir(), tmp_dir()
    r1 = {"s1.fq": "@c/1\nCCCCAAAA\n+\nIIIIIIII\n@a/1\nAAAACCCC\n+\nIIIIIIII\n",
          "s2.fq": "@b/1\nGGGGTTTT\n+\nIIIIIIII\n",
          "s3.fq": "@b/2\nTTTTGGGG\n+\nIIIIIIII\n@a/2\nCCCCAAAA\n+\nIIIIIIII\n",
          "s4.fq": "@c/2\nAAAACCCC\n+\nIIIIIIII\n"}
    rows = [("s1", "S", 1, "s1.fq"), ("s2", "S", 1, "s2.fq"),
            ("s3", "S", 2, "s3.fq"), ("s4", "S", 2, "s4.fq")]
    m1 = make_dataset(root1, r1, rows)
    # 清单顺序颠倒 + 分片内记录顺序颠倒 + 文件名不同
    r2 = {"x4.fq": "@c/2\nAAAACCCC\n+\nIIIIIIII\n",
          "x3.fq": "@a/2\nCCCCAAAA\n+\nIIIIIIII\n@b/2\nTTTTGGGG\n+\nIIIIIIII\n",
          "x2.fq": "@b/1\nGGGGTTTT\n+\nIIIIIIII\n",
          "x1.fq": "@a/1\nAAAACCCC\n+\nIIIIIIII\n@c/1\nCCCCAAAA\n+\nIIIIIIII\n"}
    rows2 = [("w4", "S", 2, "x4.fq"), ("w3", "S", 2, "x3.fq"),
             ("w2", "S", 1, "x2.fq"), ("w1", "S", 1, "x1.fq")]
    m2 = make_dataset(root2, r2, rows2)
    run(m1, root1)
    run(m2, root2)
    for f in ("S_R1.fastq", "S_R2.fastq", "key_states.csv"):
        assert (root1 / "out" / f).read_bytes() == (root2 / "out" / f).read_bytes()
    names = [l[1:-2] for l in (root1 / "out" / "S_R1.fastq")
             .read_text().splitlines()[::4]]
    assert names == ["a", "b", "c"]


@check("跨分片相同重传合并成一条，保留每个来源")
def _():
    root = tmp_dir()
    m = make_dataset(root, {
        "a.fq": "@r/1\nACGTACGT\n+\nIIIIIIII\n",
        "b.fq.gz": "@r/1\nacgtacgt\n+\nIIIIIIII\n",   # 小写，规范化后相同
        "c.fq": "@r/2\nTTTTGGGG\n+\nIIIIIIII\n",
    }, [("a", "S", 1, "a.fq"), ("b", "S", 1, "b.fq.gz"), ("c", "S", 2, "c.fq")],
        gz={"b.fq.gz"})
    summary = run(m, root)
    assert summary["merged_copies"] == 1 and summary["keys_paired"] == 1
    dup = (root / "out" / "duplicates.csv").read_text()
    assert "a#1,b#1" in dup
    out = (root / "out" / "S_R1.fastq").read_text()
    assert out.count("@r/1") == 1 and "ACGTACGT" in out


@check("同端不同内容记冲突，哪怕清洗后会变成一样")
def _():
    root = tmp_dir()
    m = make_dataset(root, {
        "a.fq": "@r/1\nACGTACGT\n+\nIIIIIIII\n",
        "b.fq": "@r/2\nAAAACCCCGG\n+\nIIIIIIII!!\n",   # 清洗后 AAAACCCC
        "c.fq": "@r/2\nAAAACCCCAA\n+\nIIIIIIII!!\n",   # 清洗后也是 AAAACCCC
    }, [("a", "S", 1, "a.fq"), ("b", "S", 2, "b.fq"), ("c", "S", 2, "c.fq")])
    summary = run(m, root)
    assert summary["keys_conflict"] == 1 and summary["keys_paired"] == 0
    assert not (root / "out" / "S_R1.fastq").read_text().strip()


@check("跨样本同名不串配")
def _():
    root = tmp_dir()
    m = make_dataset(root, {
        "a.fq": "@r/1\nACGTACGT\n+\nIIIIIIII\n",       # S1 的 r R1
        "b.fq": "@r/2\nTTTTGGGG\n+\nIIIIIIII\n",       # S2 的 r R2
    }, [("a", "S1", 1, "a.fq"), ("b", "S2", 2, "b.fq")])
    summary = run(m, root)
    st = states(root / "t.duckdb")
    assert st == {("S1", "r"): "orphan", ("S2", "r"): "orphan"}
    assert summary["keys_paired"] == 0


@check("缺端键单列 orphan")
def _():
    root = tmp_dir()
    m = make_dataset(root, {"a.fq": "@r/1\nACGTACGT\n+\nIIIIIIII\n"},
                     [("a", "S", 1, "a.fq")])
    run(m, root)
    assert states(root / "t.duckdb") == {("S", "r"): "orphan"}
    import csv as _csv
    with (root / "out" / "orphans.csv").open(newline="") as f:
        rows = list(_csv.DictReader(f))
    assert rows == [{"sample": "S", "name": "r", "mate_present": "1",
                     "len": "8", "copies": "1", "sources": "a#1"}]


@check("非法记录让对应键拒收，另一端不当孤端输出")
def _():
    root = tmp_dir()
    m = make_dataset(root, {
        "a.fq": "@r/2\nACGTACGT\n+\nIIIIIIII\n",       # 清单 mate=1 却标 /2
        "b.fq": "@r/2\nTTTTGGGG\n+\nIIIIIIII\n",       # 另一端完全合法
    }, [("a", "S", 1, "a.fq"), ("b", "S", 2, "b.fq")])
    summary = run(m, root)
    st = states(root / "t.duckdb")
    assert st == {("S", "r"): "rejected"}
    assert summary["keys_orphan"] == 0


@check("后到的冲突不让先前合格的一对留在合格文件里")
def _():
    root = tmp_dir()
    m = make_dataset(root, {
        "a.fq": "@r/1\nACGTACGT\n+\nIIIIIIII\n",
        "b.fq": "@r/2\nTTTTGGGG\n+\nIIIIIIII\n",       # 先到的合格 R2
        "c.fq": "@r/2\nTTTTGGGA\n+\nIIIIIIII\n",       # 后到的不同 R2 -> 冲突
    }, [("a", "S", 1, "a.fq"), ("b", "S", 2, "b.fq"), ("c", "S", 2, "c.fq")])
    summary = run(m, root)
    assert summary["keys_conflict"] == 1 and summary["keys_paired"] == 0
    assert "@r/2" not in (root / "out" / "S_R2.fastq").read_text()


@check("任一端清洗后过短则整对 filtered，另一端不混入合格输出")
def _():
    root = tmp_dir()
    m = make_dataset(root, {
        "a.fq": "@r/1\nACGTACGTAC\n+\nIIIIIIIIII\n",   # 完好的 R1
        "b.fq": "@r/2\nTTTTGGGGCC\n+\nIIIII!!!!!\n",   # 清洗后 5 < 8
    }, [("a", "S", 1, "a.fq"), ("b", "S", 2, "b.fq")])
    summary = run(m, root)
    st = states(root / "t.duckdb")
    assert st == {("S", "r"): "filtered"}
    assert (root / "out" / "S_R1.fastq").read_text() == ""
    row = (root / "out" / "filtered.csv").read_text().splitlines()[1]
    assert row.endswith(",R2")


@check("demo 数据集：11 个键的终态与预期完全一致")
def _():
    if not (EXAMPLE_DIR / "manifest.csv").exists():
        datagen.write_demo_dataset(EXAMPLE_DIR)
    root = tmp_dir()
    summary = pipeline.run(EXAMPLE_DIR / "manifest.csv", root / "out",
                           str(root / "t.duckdb"), min_qual=20, min_len=8)
    assert states(root / "t.duckdb") == datagen.DEMO_EXPECTED_STATES
    assert summary["records_total"] == 22 and summary["records_invalid"] == 3
    assert summary["merged_copies"] == 1


@check("输出按名字 UTF-8 字节序，两文件逐条严格对应")
def _():
    root = tmp_dir()
    names = ["read2", "read10", "readz", "readé"]     # 字节序: 10 < 2 < z < é
    r1 = "".join(f"@{n}/1\nACGTACGT\n+\nIIIIIIII\n" for n in names)
    r2 = "".join(f"@{n}/2\nTTTTGGGG\n+\nIIIIIIII\n" for n in reversed(names))
    m = make_dataset(root, {"a.fq": r1, "b.fq": r2},
                     [("a", "S", 1, "a.fq"), ("b", "S", 2, "b.fq")])
    run(m, root)
    l1 = (root / "out" / "S_R1.fastq").read_text().splitlines()
    l2 = (root / "out" / "S_R2.fastq").read_text().splitlines()
    heads1 = l1[::4]
    assert heads1 == ["@read10/1", "@read2/1", "@readz/1", "@readé/1"]
    assert l2[::4] == [h[:-1] + "2" for h in heads1]   # 逐条同名
    for i in range(4):
        assert l1[4 * i + 1] == "ACGTACGT" and l2[4 * i + 1] == "TTTTGGGG"


@check("统计对账：记录/键/读段/碱基各方程闭合")
def _():
    root = tmp_dir()
    m = make_dataset(root, {
        "a.fq": ("@p/1\nACGTACGTAC\n+\nIIIIIIIIII\n"
                 "@q/1\nACGTACGTAC\n+\nIIIIIIII!!\n"      # 修剪 2
                 "@x/1\nACGT\n+\nIIII\n"                  # 非法: 长度不等? 否->qual 4 ok
                 "@bad/2\nACGTACGT\n+\nIIIIIIII\n"),      # 非法: 清单 mate=1
        "b.fq": ("@p/2\nTTTTGGGGCC\n+\nIIIIIIIIII\n"
                 "@q/2\nGGGGCCCC\n+\nIIIIIIII\n"
                 "@x/2\nACGT\n+\nIIII\n"),
        "c.fq": "@p/1\nACGTACGTAC\n+\nIIIIIIIIII\n",      # 重传
    }, [("a", "S", 1, "a.fq"), ("b", "S", 2, "b.fq"), ("c", "S", 1, "c.fq")])
    summary = run(m, root)
    db = str(root / "t.duckdb")
    g = lambda k: stat(db, "*", k)
    assert g("records_total") == g("records_valid") + g("records_invalid")
    assert g("records_valid") == g("distinct_ends") + g("merged_copies")
    assert g("keys_total") == sum(g(f"keys_{s}") for s in model.STATES)
    assert g("reads_before") == 2 * (g("keys_paired") + g("keys_filtered"))
    assert g("reads_after") == 2 * g("keys_paired")
    # 输出文件中的真实读段数与碱基数
    n_rec = n_base = 0
    for f in ("S_R1.fastq", "S_R2.fastq"):
        lines = (root / "out" / f).read_text().splitlines()
        n_rec += len(lines) // 4
        n_base += sum(len(lines[i]) for i in range(1, len(lines), 4))
    assert n_rec == g("reads_after") and n_base == g("bases_after")
    assert g("bases_trimmed") == 2                       # q 的 R1 剪 2
    assert g("keys_rejected") == 1 and g("keys_paired") == 2


@check("SQL 清洗与 model.trim_3prime 在随机质量串上逐条等价")
def _():
    rng = random.Random(42)
    root = tmp_dir()
    recs1, recs2 = [], []
    for i in range(300):
        n = rng.randint(10, 40)
        q1 = "".join(chr(rng.randint(33, 74)) for _ in range(n))
        q2 = "".join(chr(rng.randint(33, 74)) for _ in range(n))
        s1 = "".join(rng.choice("ACGT") for _ in range(n))
        s2 = "".join(rng.choice("ACGT") for _ in range(n))
        recs1.append(f"@r{i:03d}/1\n{s1}\n+\n{q1}\n")
        recs2.append(f"@r{i:03d}/2\n{s2}\n+\n{q2}\n")
    for thr in (0, 20, 42):
        m = make_dataset(root / f"t{thr}", {"a.fq": "".join(recs1),
                                            "b.fq": "".join(recs2)},
                         [("a", "S", 1, "a.fq"), ("b", "S", 2, "b.fq")])
        pipeline.run(m, root / f"t{thr}" / "out", str(root / f"t{thr}.duckdb"),
                     min_qual=thr, min_len=1)
        con = store.connect_readonly(str(root / f"t{thr}.duckdb"))
        got = {n: ((s1, q1), (s2, q2)) for n, s1, q1, s2, q2 in con.execute(
            "SELECT name, seq1, qual1, seq2, qual2 FROM pairs").fetchall()}
        con.close()
        for i in range(300):
            s1 = recs1[i].splitlines()[1]
            q1 = recs1[i].splitlines()[3]
            s2 = recs2[i].splitlines()[1]
            q2 = recs2[i].splitlines()[3]
            want = (model.trim_3prime(s1, q1, thr), model.trim_3prime(s2, q2, thr))
            assert got[f"r{i:03d}"] == want, (thr, i, got[f"r{i:03d}"], want)


@check("where 入口：样本汇总与读级去向（合格行号/冲突内容/拒收原因）")
def _():
    if not (EXAMPLE_DIR / "manifest.csv").exists():
        datagen.write_demo_dataset(EXAMPLE_DIR)
    root = tmp_dir()
    pipeline.run(EXAMPLE_DIR / "manifest.csv", root / "out",
                 str(root / "t.duckdb"), min_qual=20, min_len=8)
    db = str(root / "t.duckdb")
    info = pipeline.where(db, "DEMO1")
    assert info["states"] == {"paired": 4, "filtered": 1, "orphan": 1,
                              "conflict": 1, "rejected": 3}
    g = pipeline.where(db, "DEMO1", "readG")
    assert g["state"] == "paired" and g["record_index"] == 4
    assert (g["len1_raw"], g["len1_after"]) == (10, 8)
    # 行号与真实文件一致
    lines = (root / "out" / "DEMO1_R1.fastq").read_text().splitlines()
    assert lines[4 * 4 - 4] == "@readG/1"
    c = pipeline.where(db, "DEMO1", "readC")
    assert c["state"] == "conflict" and len(c["contents"]) == 3
    f = pipeline.where(db, "DEMO1", "readF")
    assert f["state"] == "rejected"
    assert any(r["err_code"] == "E_MARKER_MANIFEST" for r in f["records"])
    o = pipeline.where(db, "DEMO2", "readD")
    assert o["state"] == "orphan" and o["mate_present"] == 1
    try:
        pipeline.where(db, "NOPE")
        raise AssertionError("应当报未知样本")
    except KeyError:
        pass


@check("独立参考程序逐对核对 demo 数据集")
def _():
    if not (EXAMPLE_DIR / "manifest.csv").exists():
        datagen.write_demo_dataset(EXAMPLE_DIR)
    root = tmp_dir()
    pipeline.run(EXAMPLE_DIR / "manifest.csv", root / "out",
                 str(root / "t.duckdb"), min_qual=20, min_len=8)
    problems = refcheck.check(EXAMPLE_DIR / "manifest.csv", root / "out",
                              str(root / "t.duckdb"), min_qual=20, min_len=8)
    assert problems == [], problems


@check("重跑重置：同数据重跑结果一致；换数据后旧键消失")
def _():
    root = tmp_dir()
    m = make_dataset(root, {"a.fq": "@r/1\nACGTACGT\n+\nIIIIIIII\n",
                            "b.fq": "@r/2\nTTTTGGGG\n+\nIIIIIIII\n"},
                     [("a", "S", 1, "a.fq"), ("b", "S", 2, "b.fq")])
    db = str(root / "t.duckdb")
    pipeline.run(m, root / "out1", db, min_qual=20, min_len=8)
    first = (root / "out1" / "S_R1.fastq").read_bytes()
    pipeline.run(m, root / "out2", db, min_qual=20, min_len=8)
    assert (root / "out2" / "S_R1.fastq").read_bytes() == first
    # 换一份没有 r 的数据进同一库
    m2 = make_dataset(root, {"c.fq": "@z/1\nACGTACGT\n+\nIIIIIIII\n"},
                      [("c", "S", 1, "c.fq")])
    (root / "manifest.csv").write_text(
        "source_id,sample,mate,path\nc,S,1,c.fq\n", encoding="utf-8")
    pipeline.run(root / "manifest.csv", root / "out3", db,
                 min_qual=20, min_len=8)
    assert states(db) == {("S", "z"): "orphan"}


@check("min_qual=0 不修剪；min_qual=42 全部修剪到空进而整对过滤")
def _():
    root = tmp_dir()
    m = make_dataset(root, {"a.fq": "@r/1\nACGTACGT\n+\nIIIIIIII\n",
                            "b.fq": "@r/2\nTTTTGGGG\n+\nIIIIIIII\n"},
                     [("a", "S", 1, "a.fq"), ("b", "S", 2, "b.fq")])
    s0 = pipeline.run(m, root / "o0", str(root / "t0.duckdb"),
                      min_qual=0, min_len=8)
    assert s0["keys_paired"] == 1 and s0["bases_trimmed"] == 0
    s42 = pipeline.run(m, root / "o42", str(root / "t42.duckdb"),
                       min_qual=42, min_len=1)
    assert s42["keys_filtered"] == 1 and s42["keys_paired"] == 0


@check("空源文件合法；无合格键的样本仍输出两个空 FASTQ")
def _():
    root = tmp_dir()
    m = make_dataset(root, {"a.fq": "", "b.fq": "@r/1\nACGTACGT\n+\nIIIIIIII\n"},
                     [("a", "S", 1, "a.fq"), ("b", "S", 1, "b.fq")])
    summary = run(m, root)
    assert summary["records_total"] == 1 and summary["keys_orphan"] == 1
    assert (root / "out" / "S_R1.fastq").read_text() == ""
    assert (root / "out" / "S_R2.fastq").read_text() == ""


@check("参数边界：min_qual/min_len 非法即拒")
def _():
    root = tmp_dir()
    m = make_dataset(root, {"a.fq": "@r/1\nACGTACGT\n+\nIIIIIIII\n"},
                     [("a", "S", 1, "a.fq")])
    for kw in ({"min_qual": -1}, {"min_qual": 43}, {"min_len": 0}):
        args = {"min_qual": 20, "min_len": 8}
        args.update(kw)
        try:
            pipeline.run(m, root / "out", str(root / "t.duckdb"), **args)
            raise AssertionError(f"应当拒收 {kw}")
        except PipelineFailure:
            pass


@check("大样本小比例：双端远距离到达 + 独立参考核对")
def _():
    root = tmp_dir()
    m = datagen.write_big_dataset(root / "data", n_pairs=3000, seed=11)
    pipeline.run(m, root / "out", str(root / "big.duckdb"),
                 min_qual=20, min_len=30, memory_limit="64MB")
    problems = refcheck.check(m, root / "out", str(root / "big.duckdb"),
                              min_qual=20, min_len=30)
    assert problems == [], problems
    st = states(root / "big.duckdb")
    n_orphan = sum(1 for i in range(3000) if datagen.big_category(i) == "orphan")
    assert sum(1 for v in st.values() if v == "orphan") == n_orphan


def main() -> int:
    print("双端读段整理管线验收测试")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
