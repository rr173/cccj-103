"""独立的小规模内存参考程序：逐对核对生产管线的输出内容与分类。

有意不导入 bitemp.pefastq 的任何生产模块（解析、配对、清洗全部按文档
约定的语义在此独立重写），以避免"自己证明自己"。仅适合小规模：所有
键都留在内存字典里。

用法：
    mismatches = check(manifest, out_dir, db_path, min_qual=..., min_len=...)
    # 返回不一致说明的列表；空列表 = 生产管线输出与参考程序完全一致
"""
from __future__ import annotations

import csv
import gzip
import re
from pathlib import Path

import duckdb

# ------------------------------------------------------------ 独立解析
_SUFFIX = re.compile(r"^(.*)/([12])$")
_CASAVA = re.compile(r"^([12]):")


def _parse_header(text):
    """返回 (name, marker)；无法解析返回 (None, None) 并置 error。"""
    if text == "":
        return None, None, "头部为空"
    parts = text.split(None, 1)
    core = parts[0]
    comment = parts[1] if len(parts) == 2 else None
    m = _SUFFIX.match(core)
    if m:
        name, sm = m.group(1), int(m.group(2))
    else:
        name, sm = core, None
    if name == "":
        return None, None, "去掉 /1|/2 后名字为空"
    cm = None
    if comment:
        tok = comment.split(None, 1)[0]
        c = _CASAVA.match(tok)
        if c:
            cm = int(c.group(1))
    if sm is not None and cm is not None and sm != cm:
        return name, None, "头部端标记互相矛盾"
    return name, (sm if sm is not None else cm), None


def _validate(h, s, p, q, mate):
    """返回 (name 或 None, seq, qual, error 或 None)。独立重写的校验。"""
    if not h.startswith("@"):
        return None, None, None, "头行不以 @ 开头"
    name, marker, err = _parse_header(h[1:])
    if err:
        return name, None, None, err
    if marker is not None and marker != mate:
        return name, None, None, "端标记与清单 mate 不一致"
    seq = s.upper()
    if seq == "":
        return name, None, None, "序列为空"
    if set(seq) - set("ACGTN"):
        return name, None, None, "序列含非法字符"
    if not p.startswith("+"):
        return name, None, None, "第三行不以 + 开头"
    if p != "+" and p[1:] != h[1:]:
        return name, None, None, "+ 行重复的头不一致"
    if len(q) != len(seq):
        return name, None, None, "质量串与序列长度不等"
    if any(not (33 <= ord(c) <= 74) for c in q):
        return name, None, None, "质量串字符越界"
    return name, seq, q, None


def _trim(seq, qual, min_qual):
    """独立重写的 3' 端修剪。"""
    i = len(qual)
    while i > 0 and ord(qual[i - 1]) - 33 < min_qual:
        i -= 1
    return seq[:i], qual[:i]


def _read_source(path, mate):
    """独立重写的四行分块读取；返回 (records, source_error)。

    records: (name|None, seq|None, qual|None, error|None) 列表。
    """
    opener = gzip.open if str(path).endswith(".gz") else open
    lines = []
    try:
        with opener(path, "rb") as f:
            for raw in f:
                lines.append(raw.rstrip(b"\r\n"))
    except Exception as e:  # 参考程序从宽：任何读源异常都视为源不完整
        return [], f"源未完整读完: {e}"
    if len(lines) % 4 != 0:
        return [], f"行数 {len(lines)} 不是 4 的倍数（截断记录）"
    out = []
    for i in range(0, len(lines), 4):
        block = lines[i:i + 4]
        try:
            h, s, p, q = (b.decode("utf-8") for b in block)
        except UnicodeDecodeError:
            out.append((None, None, None, "非 UTF-8"))
            continue
        name, seq, qual, err = _validate(h, s, p, q, mate)
        out.append((name, seq, qual, err))
    return out, None


def _load_manifest(path):
    path = Path(path)
    entries = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            p = Path(row["path"])
            entries.append({
                "source_id": row["source_id"], "sample": row["sample"],
                "mate": int(row["mate"]),
                "resolved": p if p.is_absolute() else path.parent / p,
            })
    return entries


# ------------------------------------------------------------ 独立分类
def _classify(entries, min_qual, min_len):
    """返回 (states, ends, pairs_trimmed, counters, source_error)。"""
    counters = {"records_total": 0, "records_valid": 0, "records_invalid": 0,
                "invalid_attributable": 0, "merged_copies": 0}
    bad_keys = set()
    contents = {}   # (sample, name, mate) -> {(seq, qual): copies}
    for e in entries:
        records, src_err = _read_source(e["resolved"], e["mate"])
        if src_err:
            return None, None, None, None, f"{e['source_id']}: {src_err}"
        for name, seq, qual, err in records:
            counters["records_total"] += 1
            if err is not None:
                counters["records_invalid"] += 1
                if name is not None:
                    counters["invalid_attributable"] += 1
                    bad_keys.add((e["sample"], name))
                continue
            counters["records_valid"] += 1
            slot = contents.setdefault((e["sample"], name, e["mate"]), {})
            slot[(seq, qual)] = slot.get((seq, qual), 0) + 1
    counters["distinct_ends"] = sum(len(v) for v in contents.values())
    counters["merged_copies"] = (counters["records_valid"]
                                 - counters["distinct_ends"])

    keys = {}
    for (sample, name, mate), slot in contents.items():
        keys.setdefault((sample, name), {})[mate] = slot
    for sample, name in bad_keys:
        keys.setdefault((sample, name), {})

    states = {}
    pairs = {}
    for (sample, name), mates in keys.items():
        if (sample, name) in bad_keys:
            states[(sample, name)] = "rejected"
            continue
        if any(len(slot) > 1 for slot in mates.values()):
            states[(sample, name)] = "conflict"
            continue
        if set(mates) == {1} or set(mates) == {2}:
            states[(sample, name)] = "orphan"
            continue
        (s1, q1), = mates[1].keys()
        (s2, q2), = mates[2].keys()
        t1, u1 = _trim(s1, q1, min_qual)
        t2, u2 = _trim(s2, q2, min_qual)
        if len(t1) < min_len or len(t2) < min_len:
            states[(sample, name)] = "filtered"
        else:
            states[(sample, name)] = "paired"
            pairs[(sample, name)] = (t1, u1, t2, u2)
    return states, contents, pairs, counters, None


# ------------------------------------------------------------ 核对
def check(manifest_path, out_dir, db_path, *, min_qual, min_len) -> list[str]:
    """逐对核对生产管线输出；返回不一致说明列表（空 = 全部一致）。"""
    out_dir = Path(out_dir)
    problems: list[str] = []
    entries = _load_manifest(manifest_path)
    states, contents, pairs, counters, src_err = _classify(
        entries, min_qual, min_len)
    if src_err:
        return [f"参考程序读源失败: {src_err}"]

    # 1) 输出 FASTQ 逐条核对（流式，不整读）
    samples = sorted({e["sample"] for e in entries})
    for sample in samples:
        expect = sorted(((name, *pairs[(sample, name)])
                         for (s, name) in pairs if s == sample),
                        key=lambda r: r[0].encode("utf-8"))
        for mate, fname in ((1, f"{sample}_R1.fastq"), (2, f"{sample}_R2.fastq")):
            path = out_dir / fname
            if not path.exists():
                problems.append(f"缺少输出文件 {fname}")
                continue
            with path.open(encoding="utf-8") as f:
                got = [line.rstrip("\n") for line in f]
            if len(got) != 4 * len(expect):
                problems.append(
                    f"{fname}: 行数 {len(got)} != 4×{len(expect)}")
                continue
            for idx, (name, t1, u1, t2, u2) in enumerate(expect):
                seq, qual = (t1, u1) if mate == 1 else (t2, u2)
                want = [f"@{name}/{mate}", seq, "+", qual]
                got_rec = got[4 * idx:4 * idx + 4]
                if got_rec != want:
                    problems.append(
                        f"{fname} 第 {idx + 1} 条: 期望 {want} 实得 {got_rec}")
                    break

    # 2) 库中终态逐键核对
    con = duckdb.connect(str(db_path), read_only=True)
    db_states = {(s, n): st for s, n, st in con.execute(
        "SELECT sample, name, state FROM key_states").fetchall()}
    if db_states != states:
        only_db = set(db_states) - set(states)
        only_ref = set(states) - set(db_states)
        diff = [k for k in set(db_states) & set(states)
                if db_states[k] != states[k]]
        if only_db:
            problems.append(f"key_states 多出: {sorted(only_db)[:5]}")
        if only_ref:
            problems.append(f"key_states 缺少: {sorted(only_ref)[:5]}")
        for k in diff[:5]:
            problems.append(f"键 {k}: 库={db_states[k]} 参考={states[k]}")

    # 3) 统计口径核对
    stats = {(scope, m): v for scope, m, v in con.execute(
        "SELECT scope, metric, value FROM stats").fetchall()}
    for metric, want in counters.items():
        got = stats.get(("*", metric))
        if got != want:
            problems.append(f"stats(*,{metric}): 库={got} 参考={want}")
    for state in ("paired", "filtered", "orphan", "conflict", "rejected"):
        want = sum(1 for v in states.values() if v == state)
        got = stats.get(("*", f"keys_{state}"))
        if got != want:
            problems.append(f"stats(*,keys_{state}): 库={got} 参考={want}")
    clean = sum(1 for v in states.values() if v in ("paired", "filtered"))
    if stats.get(("*", "reads_before")) != 2 * clean:
        problems.append("stats(*,reads_before) 与参考不符")
    bases_before = 0
    bases_after = 0
    for (sample, name), st in states.items():
        if st in ("paired", "filtered"):
            (s1, q1), = contents[(sample, name, 1)].keys()
            (s2, q2), = contents[(sample, name, 2)].keys()
            bases_before += len(s1) + len(s2)
            if st == "paired":
                t1, u1, t2, u2 = pairs[(sample, name)]
                bases_after += len(t1) + len(t2)
    for metric, want in (("bases_before", bases_before),
                         ("bases_after", bases_after),
                         ("reads_after", 2 * sum(1 for v in states.values()
                                                 if v == "paired"))):
        got = stats.get(("*", metric))
        if got != want:
            problems.append(f"stats(*,{metric}): 库={got} 参考={want}")
    con.close()

    # 4) 明细文件核对
    ks_csv = out_dir / "key_states.csv"
    if ks_csv.exists():
        with ks_csv.open(newline="", encoding="utf-8") as f:
            rows = {(r["sample"], r["name"]): r["state"]
                    for r in csv.DictReader(f)}
        if rows != states:
            problems.append("key_states.csv 与参考分类不一致")
    else:
        problems.append("缺少 key_states.csv")
    return problems
