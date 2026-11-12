"""双端读段整理管线：清单 -> 暂存 -> 配对决策 -> 清洗 -> 导出。

阶段
----
1. ingest   逐源读取，严格四行一块校验，批量写入 records；任何源未完整读完
            （截断记录、gzip 解压错误等）则整次运行失败（SourceFailure），
            已入库的诊断记录与 sources 状态保留，供事后追查。
2. decide   全部源读完后才在 SQL 中决定每个 (样本, 读段名) 键的终态。
            决策前不输出任何 FASTQ，因此"后到的冲突"不可能让一对已输出的
            读段继续留在合格文件里；终态也不依赖分片排列与到达顺序。
3. export   按名字的 UTF-8 字节序键集分页导出每样本两个 FASTQ（逐条严格
            对应），明细 CSV 用 COPY 流式写出。全程不使用随输入增长的
            Python 字典；排序/分组/连接由 DuckDB 在 memory_limit 内完成，
            超出即溢写 temp_directory。

终态判定优先级（每个键恰好一种终态）：
  rejected  键内存在能确定名字的非法记录
  conflict  同一端出现内容不同的记录（哪怕清洗后会相同也不合并）
  filtered  两端唯一合法，但 3' 端清洗后任一端短于最短保留长度
  paired    两端唯一合法且通过清洗
  orphan    只有一端且没有其他错误
"""
from __future__ import annotations

import csv
import json
import os
import re
import resource
from pathlib import Path

from . import fastqio, manifest, model, store

CONVENTION_NOTES = [
    "每条记录恰好四个物理行（@头/序列/+行/质量串），不支持折行；解析按固定四行分块，"
    "绝不通过搜索下一个 @ 猜测记录边界（质量串本身可以以 @ 或 + 开头）",
    "序列只接受 A/C/G/T/N，统一规范化为大写；质量为 Phred+33（ASCII 33..74），与序列等长且非空",
    "'+' 行若重复头信息，必须与 '@' 后文本完全一致",
    "规范化读段名 = 头部空白前第一个 token 去掉末尾至多一个 /1 或 /2；"
    "配对键 = (样本, 读段名)，不跨样本串配",
    "头部端标记（/1、/2 后缀与空白后 1:.../2:... 首 token）必须与清单 mate 一致；"
    "两处标记互相矛盾也算非法记录；无端标记时以清单为准",
    "同一端反复出现且规范化后的完整序列与质量串都相同时合并成一条（保留每个来源）；"
    "同一端出现不同内容则整对记为冲突，哪怕它们清洗后会变成一样也不合并",
    "能确定名字的非法记录让对应键拒收（另一端不当作孤端输出）；"
    "无法确定名字的非法记录只计入 records_invalid",
    "清洗只从每端 3' 末尾连续删除质量低于阈值的碱基，遇到合格碱基即停止，"
    "不改中间的低质量位置；任一端清洗后短于最短保留长度则整对进入 filtered",
    "输出 FASTQ 头部为 规范化名字/1、/2，按名字的 UTF-8 字节序排列，逐条严格对应",
]


class PipelineFailure(Exception):
    """运行级失败（参数非法、内部不变量破坏等）。"""


_MEMORY_LIMIT_RE = re.compile(r"^\d+(\.\d+)?\s*(B|KB|MB|GB|TB)?$", re.IGNORECASE)


# ---------------------------------------------------------------- 记录校验
def _name_quietly(header_line: str) -> str | None:
    """尽力从头行取规范化名字（用于非头行出错时仍能定位键）。"""
    try:
        if not header_line.startswith("@"):
            return None
        return model.parse_header(header_line[1:]).name
    except model.RecordInvalid:
        return None


def _validate_block(entry, rec_no: int, start_line: int,
                    block: list[bytes]) -> tuple:
    """把四行字节块校验成 records 表的一行。"""
    try:
        h = block[0].decode("utf-8")
    except UnicodeDecodeError:
        return (entry.source_id, rec_no, start_line, False,
                "E_ENCODING", "头行不是合法 UTF-8",
                entry.sample, entry.mate, None, False, None, None, None)
    try:
        s = block[1].decode("utf-8")
        p = block[2].decode("utf-8")
        q = block[3].decode("utf-8")
    except UnicodeDecodeError:
        name = _name_quietly(h)
        return (entry.source_id, rec_no, start_line, False,
                "E_ENCODING", "序列/+行/质量行不是合法 UTF-8",
                entry.sample, entry.mate, name, name is not None, h, None, None)
    try:
        rec = model.parse_and_validate(h, s, p, q, entry.mate)
    except model.RecordInvalid as ex:
        return (entry.source_id, rec_no, start_line, False,
                ex.code, str(ex),
                entry.sample, entry.mate, ex.name, ex.name is not None,
                h, None, None)
    return (entry.source_id, rec_no, start_line, True, None, None,
            entry.sample, entry.mate, rec.name, True, rec.header,
            rec.seq, rec.qual)


# ---------------------------------------------------------------- 阶段 1：暂存
def _ingest_source(con, entry, batch: int, tmp_dir: Path) -> None:
    """逐块校验一个源并暂存进 records。

    校验结果先批量写入临时 CSV（内存中至多 batch 行），再用 COPY 一次性
     bulk 入库（COPY 比逐行 executemany 快约两个数量级）。源未完整读完时
    把已校验部分同样入库作诊断，标记源失败后整次运行失败。
    """
    csv_path = tmp_dir / f"records-{entry.source_id}.csv"
    n = 0
    nbad = 0
    buf: list[tuple] = []
    f = csv_path.open("w", encoding="utf-8", newline="")
    writer = csv.writer(f)
    failure: fastqio.SourceFailure | None = None
    try:
        for rec_no, start_line, block in fastqio.iter_blocks(entry.resolved):
            row = _validate_block(entry, rec_no, start_line, block)
            buf.append(row)
            n += 1
            if not row[3]:
                nbad += 1
            if len(buf) >= batch:
                writer.writerows(buf)
                buf.clear()
    except fastqio.SourceFailure as sf:
        failure = sf
    if buf:
        writer.writerows(buf)
        buf.clear()
    f.close()
    con.execute("BEGIN")
    if csv_path.stat().st_size > 0:
        con.execute(f"COPY records FROM {_lit(str(csv_path))} (HEADER false)")
    if failure is not None:
        # 已校验部分入库作诊断，标记源失败后整次运行失败
        con.execute("UPDATE sources SET status='failed', records=?, invalid=?, "
                    "error=? WHERE source_id=?",
                    [n, nbad, str(failure), entry.source_id])
        con.execute("COMMIT")
        csv_path.unlink(missing_ok=True)
        raise failure
    con.execute("UPDATE sources SET status='ok', records=?, invalid=? "
                "WHERE source_id=?", [n, nbad, entry.source_id])
    con.execute("COMMIT")
    csv_path.unlink(missing_ok=True)


# ---------------------------------------------------------------- 阶段 2：决策
def _decide(con, min_qual: int, min_len: int) -> None:
    pat = model.low_qual_regex(min_qual)
    con.execute("""
        CREATE OR REPLACE TABLE ends AS
        SELECT sample, name, mate, seq, qual, COUNT(*) AS copies,
               string_agg(source_id || '#' || rec_no::VARCHAR, ','
                          ORDER BY source_id, rec_no) AS sources
        FROM records WHERE ok
        GROUP BY sample, name, mate, seq, qual""")
    con.execute("CREATE OR REPLACE TABLE key_states "
                "(sample TEXT, name TEXT, state TEXT)")
    # rejected：键内存在能确定名字的非法记录（优先级最高）
    con.execute("""
        INSERT INTO key_states
        SELECT DISTINCT sample, name, 'rejected'
        FROM records WHERE NOT ok AND key_known""")
    # conflict：同一键同一端出现多份不同内容
    con.execute("""
        INSERT INTO key_states
        SELECT DISTINCT sample, name, 'conflict' FROM (
            SELECT sample, name, mate FROM ends
            GROUP BY sample, name, mate HAVING COUNT(*) > 1
        ) t WHERE NOT EXISTS (
            SELECT 1 FROM key_states k
            WHERE k.sample = t.sample AND k.name = t.name)""")
    # 其余键按端数分流
    con.execute("""
        CREATE OR REPLACE TABLE rest AS
        SELECT e.* FROM ends e WHERE NOT EXISTS (
            SELECT 1 FROM key_states k
            WHERE k.sample = e.sample AND k.name = e.name)""")
    con.execute("""
        CREATE OR REPLACE TABLE mate_counts AS
        SELECT sample, name, COUNT(DISTINCT mate) AS nm
        FROM rest GROUP BY sample, name""")
    con.execute("INSERT INTO key_states "
                "SELECT sample, name, 'orphan' FROM mate_counts WHERE nm = 1")
    # 两端齐全的键进入清洗（SQL 内做 3' 端修剪，与 model.trim_3prime 等价）
    con.execute("""
        CREATE OR REPLACE TABLE cleaned AS
        WITH cand AS (
            SELECT r.* FROM rest r JOIN mate_counts m USING (sample, name)
            WHERE m.nm = 2
        ), t AS (
            SELECT sample, name, mate, seq, qual,
                   length(seq) AS len_raw,
                   length(seq) - length(regexp_extract(qual, ?, 1)) AS len_keep
            FROM cand
        )
        SELECT sample, name, mate,
               substr(seq, 1, len_keep) AS seq_t,
               substr(qual, 1, len_keep) AS qual_t,
               len_raw
        FROM t""", [pat])
    con.execute("""
        CREATE OR REPLACE TABLE pairs AS
        SELECT a.sample, a.name,
               a.seq_t AS seq1, a.qual_t AS qual1,
               b.seq_t AS seq2, b.qual_t AS qual2,
               a.len_raw AS len1_raw, b.len_raw AS len2_raw
        FROM cleaned a JOIN cleaned b
          ON a.sample = b.sample AND a.name = b.name
         AND a.mate = 1 AND b.mate = 2""")
    con.execute("""
        INSERT INTO key_states
        SELECT sample, name,
               CASE WHEN length(seq1) < ? OR length(seq2) < ?
                    THEN 'filtered' ELSE 'paired' END
        FROM pairs""", [min_len, min_len])
    # 不变量：每个能确定的键恰好一种终态
    n_states = con.execute("SELECT COUNT(*) FROM key_states").fetchone()[0]
    n_keys = con.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT sample, name FROM records "
        "WHERE key_known) t").fetchone()[0]
    if n_states != n_keys:
        raise PipelineFailure(
            f"内部不变量破坏：键数 {n_keys} 与终态数 {n_states} 不一致")


# ---------------------------------------------------------------- 统计
def _scalar(con, sql: str, params=()) -> int:
    row = con.execute(sql, params).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _state_counts(con, where: str = "", params=()) -> dict[str, int]:
    rows = con.execute(
        f"SELECT state, COUNT(*) FROM key_states k {where} GROUP BY state",
        params).fetchall()
    return {s: c for s, c in rows}


def _scope_stats(con, sample: str | None) -> dict[str, int]:
    """一组（全局或单样本）口径完全相同的统计。"""
    params = () if sample is None else (sample,)
    where_r = "WHERE sample = ?" if sample else ""          # records / ends
    and_r = "AND sample = ?" if sample else ""
    where_k = "WHERE k.sample = ?" if sample else ""        # key_states 别名 k
    and_p = "AND p.sample = ?" if sample else ""            # pairs 别名 p
    g: dict[str, int] = {}
    g["records_total"] = _scalar(con,
        f"SELECT COUNT(*) FROM records {where_r}", params)
    g["records_valid"] = _scalar(con,
        f"SELECT COUNT(*) FROM records WHERE ok {and_r}", params)
    g["records_invalid"] = g["records_total"] - g["records_valid"]
    g["invalid_attributable"] = _scalar(con,
        f"SELECT COUNT(*) FROM records WHERE NOT ok AND key_known {and_r}", params)
    g["invalid_unattributable"] = g["records_invalid"] - g["invalid_attributable"]
    g["distinct_ends"] = _scalar(con,
        f"SELECT COUNT(*) FROM ends {where_r}", params)
    g["merged_copies"] = g["records_valid"] - g["distinct_ends"]
    states = _state_counts(con, where_k, params)
    for s in model.STATES:
        g[f"keys_{s}"] = states.get(s, 0)
    g["keys_total"] = sum(states.values())
    g["clean_pairs"] = g["keys_paired"] + g["keys_filtered"]
    g["reads_before"] = 2 * g["clean_pairs"]
    g["bases_before"] = _scalar(con,
        f"SELECT SUM(len1_raw + len2_raw) FROM pairs p "
        f"{'WHERE p.sample = ?' if sample else ''}", params)
    g["reads_after"] = 2 * g["keys_paired"]
    g["bases_after"] = _scalar(con,
        f"SELECT SUM(length(p.seq1) + length(p.seq2)) FROM pairs p "
        f"JOIN key_states k ON k.sample = p.sample AND k.name = p.name "
        f"WHERE k.state = 'paired' {and_p}", params)
    g["bases_trimmed"] = _scalar(con,
        f"SELECT SUM(p.len1_raw - length(p.seq1) + p.len2_raw - length(p.seq2)) "
        f"FROM pairs p JOIN key_states k ON k.sample = p.sample AND k.name = p.name "
        f"WHERE k.state = 'paired' {and_p}", params)
    return g


def _stats(con) -> None:
    con.execute("CREATE OR REPLACE TABLE stats "
                "(scope TEXT, metric TEXT, value BIGINT)")
    rows: list[tuple] = []
    g = _scope_stats(con, None)
    g["sources_total"] = _scalar(con, "SELECT COUNT(*) FROM sources")
    rows += [("*", k, v) for k, v in g.items()]
    samples = [r[0] for r in con.execute(
        "SELECT DISTINCT sample FROM sources ORDER BY sample").fetchall()]
    for sample in samples:
        s = _scope_stats(con, sample)
        s["sources_total"] = _scalar(con,
            "SELECT COUNT(*) FROM sources WHERE sample = ?", (sample,))
        rows += [(sample, k, v) for k, v in s.items()]
    con.executemany("INSERT INTO stats VALUES (?,?,?)", rows)


# ---------------------------------------------------------------- 阶段 3：导出
def _lit(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _export_fastq(con, out_dir: Path, sample: str, batch: int) -> tuple[Path, Path, int]:
    """键集分页导出一个样本的两个 FASTQ；返回 (R1 路径, R2 路径, 对数)。"""
    tmp1 = out_dir / f".tmp-{sample}_R1.fastq"
    tmp2 = out_dir / f".tmp-{sample}_R2.fastq"
    final1 = out_dir / f"{sample}_R1.fastq"
    final2 = out_dir / f"{sample}_R2.fastq"
    n_pairs = 0
    last: str | None = None
    with tmp1.open("w", encoding="utf-8", newline="\n") as f1, \
            tmp2.open("w", encoding="utf-8", newline="\n") as f2:
        while True:
            if last is None:
                rows = con.execute(
                    "SELECT p.name, p.seq1, p.qual1, p.seq2, p.qual2 "
                    "FROM pairs p JOIN key_states k "
                    "  ON k.sample = p.sample AND k.name = p.name "
                    "WHERE p.sample = ? AND k.state = 'paired' "
                    "ORDER BY p.name LIMIT ?", [sample, batch]).fetchall()
            else:
                rows = con.execute(
                    "SELECT p.name, p.seq1, p.qual1, p.seq2, p.qual2 "
                    "FROM pairs p JOIN key_states k "
                    "  ON k.sample = p.sample AND k.name = p.name "
                    "WHERE p.sample = ? AND k.state = 'paired' AND p.name > ? "
                    "ORDER BY p.name LIMIT ?", [sample, last, batch]).fetchall()
            if not rows:
                break
            for name, s1, q1, s2, q2 in rows:
                f1.write(f"@{name}/1\n{s1}\n+\n{q1}\n")
                f2.write(f"@{name}/2\n{s2}\n+\n{q2}\n")
            n_pairs += len(rows)
            last = rows[-1][0]
    os.replace(tmp1, final1)
    os.replace(tmp2, final2)
    return final1, final2, n_pairs


def _copy_csv(con, out_dir: Path, name: str, select_sql: str) -> Path:
    path = out_dir / name
    con.execute(f"COPY ({select_sql}) TO {_lit(str(path))} "
                f"(HEADER, DELIMITER ',')")
    return path


def _export_details(con, out_dir: Path, min_len: int) -> list[Path]:
    written = []
    written.append(_copy_csv(con, out_dir, "rejected.csv", """
        SELECT source_id, rec_no, start_line, err_code, err_msg,
               sample, mate, COALESCE(name, '') AS name,
               COALESCE(header, '') AS header
        FROM records WHERE NOT ok ORDER BY source_id, rec_no"""))
    written.append(_copy_csv(con, out_dir, "conflict.csv", """
        SELECT e.sample, e.name, e.mate, e.seq, e.qual, e.copies, e.sources
        FROM ends e JOIN key_states k
          ON k.sample = e.sample AND k.name = e.name
        WHERE k.state = 'conflict'
        ORDER BY e.sample, e.name, e.mate, e.seq"""))
    written.append(_copy_csv(con, out_dir, "duplicates.csv", """
        SELECT e.sample, e.name, e.mate, e.copies, e.sources,
               k.state AS key_state
        FROM ends e JOIN key_states k
          ON k.sample = e.sample AND k.name = e.name
        WHERE e.copies > 1
        ORDER BY e.sample, e.name, e.mate"""))
    written.append(_copy_csv(con, out_dir, "orphans.csv", """
        SELECT e.sample, e.name, e.mate AS mate_present,
               length(e.seq) AS len, e.copies, e.sources
        FROM ends e JOIN key_states k
          ON k.sample = e.sample AND k.name = e.name
        WHERE k.state = 'orphan'
        ORDER BY e.sample, e.name"""))
    written.append(_copy_csv(con, out_dir, "filtered.csv", f"""
        SELECT p.sample, p.name, p.len1_raw, p.len2_raw,
               length(p.seq1) AS len1_after, length(p.seq2) AS len2_after,
               CASE WHEN length(p.seq1) < {min_len} AND length(p.seq2) < {min_len}
                    THEN 'both'
                    WHEN length(p.seq1) < {min_len} THEN 'R1' ELSE 'R2'
               END AS short_end
        FROM pairs p JOIN key_states k
          ON k.sample = p.sample AND k.name = p.name
        WHERE k.state = 'filtered'
        ORDER BY p.sample, p.name"""))
    written.append(_copy_csv(con, out_dir, "key_states.csv", """
        SELECT sample, name, state FROM key_states ORDER BY sample, name"""))
    written.append(_copy_csv(con, out_dir, "stats.csv", """
        SELECT scope, metric, value FROM stats ORDER BY scope, metric"""))
    return written


def _stats_text(con) -> str:
    rows = con.execute(
        "SELECT scope, metric, value FROM stats ORDER BY scope, metric").fetchall()
    by_scope: dict[str, dict[str, int]] = {}
    for scope, metric, value in rows:
        by_scope.setdefault(scope, {})[metric] = value
    metrics = [r[0] for r in con.execute(
        "SELECT DISTINCT metric FROM stats ORDER BY metric").fetchall()]
    out = ["双端读段整理统计", "",
           f"参数: min_qual={store.get_meta(con, 'min_qual')} "
           f"(Phred 阈值，质量 < 阈值才从 3' 端删除), "
           f"min_len={store.get_meta(con, 'min_len')}",
           f"清单: {store.get_meta(con, 'manifest')}", ""]
    scopes = ["*"] + sorted(s for s in by_scope if s != "*")
    header = ["scope"] + metrics
    out.append("\t".join(header))
    for scope in scopes:
        out.append("\t".join([scope] + [str(by_scope[scope].get(m, 0))
                                        for m in metrics]))
    out += ["", "对账口径（原始记录怎样计入统计）", ""]
    out += [f"- {n}" for n in [
        "records_total：从所有源读到的物理记录总数（每条恰好 4 行）；任何源未完整读完"
        "则整次运行失败，本文件不会生成",
        "records_valid + records_invalid = records_total；非法记录只计入 records_invalid"
        " 与 rejected.csv，不进入任何序列/碱基统计（其规范化序列不可得）",
        "records_valid = distinct_ends + merged_copies：同一（样本, 读段名, 端）内容"
        "（规范化序列+质量串）完全相同的多份拷贝合并为一条，第一份之外计入 merged_copies",
        "keys_total = keys_paired + keys_filtered + keys_orphan + keys_conflict"
        " + keys_rejected：每个（样本, 读段名）键恰好一种终态",
        "清洗统计只覆盖两端均唯一合法的键（clean_pairs = keys_paired + keys_filtered），"
        "每端按合并后的一条计，重传拷贝不重复计读段或碱基",
        "reads_before = 2 × clean_pairs；bases_before = 这些键两端清洗前长度之和",
        "reads_after = 2 × keys_paired（写入合格 FASTQ 的读段数）；"
        "bases_after = 合格输出中的碱基总数",
        "bases_trimmed = 合格键在清洗中删去的 3' 端碱基总数；"
        "paired 键的 bases_before − bases_trimmed = bases_after",
        "filtered 键的读段/碱基计入 reads_before、bases_before，不计入 after；"
        "orphan/conflict/rejected 键不进入清洗统计",
    ]]
    return "\n".join(out) + "\n"


def _export(con, out_dir: Path, samples: list[str], min_len: int,
            batch: int) -> list[Path]:
    written: list[Path] = []
    for sample in samples:
        f1, f2, _ = _export_fastq(con, out_dir, sample, batch)
        written += [f1, f2]
    written += _export_details(con, out_dir, min_len)
    stats_path = out_dir / "stats.txt"
    stats_path.write_text(_stats_text(con), encoding="utf-8")
    written.append(stats_path)
    return written


# ---------------------------------------------------------------- 运行
def run(manifest_path, out_dir, db_path, *, min_qual: int, min_len: int,
        memory_limit: str = "1GB", batch: int = 20000, con=None) -> dict:
    """整理一批双端 FASTQ。任何源未完整读完时抛 fastqio.SourceFailure。"""
    if not 0 <= min_qual <= 42:
        raise PipelineFailure(f"min_qual 须在 0..42 之间，得到 {min_qual}")
    if min_len < 1:
        raise PipelineFailure(f"min_len 须 >= 1，得到 {min_len}")
    if not _MEMORY_LIMIT_RE.match(memory_limit):
        raise PipelineFailure(f"memory_limit 格式非法: {memory_limit!r}")
    entries = manifest.load_manifest(manifest_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = out_dir / ".duckdb_tmp"
    own = con is None
    if own:
        con = store.connect(str(db_path), memory_limit=memory_limit,
                            temp_directory=tmp_dir)
    else:
        tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        return _run_inner(con, entries, manifest_path, out_dir, tmp_dir,
                          min_qual=min_qual, min_len=min_len,
                          memory_limit=memory_limit, batch=batch)
    finally:
        if own:
            con.close()


def _run_inner(con, entries, manifest_path, out_dir: Path, tmp_dir: Path,
               *, min_qual: int, min_len: int, memory_limit: str,
               batch: int) -> dict:
    store.reset(con)
    store.set_meta(con, "format", "pefastq/1")
    store.set_meta(con, "manifest", str(manifest_path))
    store.set_meta(con, "min_qual", str(min_qual))
    store.set_meta(con, "min_len", str(min_len))
    store.set_meta(con, "memory_limit", memory_limit)
    store.set_meta(con, "conventions", json.dumps(CONVENTION_NOTES, ensure_ascii=False))
    store.set_meta(con, "run_completed", "no")
    con.executemany(
        "INSERT INTO sources VALUES (?,?,?,?,?,'pending',0,0,NULL)",
        [(e.source_id, e.sample, e.mate, e.path, str(e.resolved))
         for e in entries])

    for e in entries:                       # 阶段 1：任何源失败即整次失败
        _ingest_source(con, e, batch, tmp_dir)
    _decide(con, min_qual, min_len)         # 阶段 2：全部读完后才决策
    _stats(con)
    samples = [r[0] for r in con.execute(
        "SELECT DISTINCT sample FROM sources ORDER BY sample").fetchall()]
    written = _export(con, out_dir, samples, min_len, batch)   # 阶段 3
    store.set_meta(con, "run_completed", "yes")

    summary = {"out_dir": str(out_dir), "written": [str(p) for p in written],
               "samples": samples}
    for scope, metric, value in con.execute(
            "SELECT scope, metric, value FROM stats WHERE scope = '*'").fetchall():
        summary[metric] = value
    summary["peak_rss_mb"] = resource.getrusage(
        resource.RUSAGE_SELF).ru_maxrss / 1024
    return summary


# ---------------------------------------------------------------- 去向查询
def where(db_path: str, sample: str, name: str | None = None, *, con=None) -> dict:
    """从整理库查看某个样本/读段的去向。"""
    own = con is None
    if own:
        con = store.connect_readonly(str(db_path))
    try:
        completed = store.get_meta(con, "run_completed")
        info: dict = {"sample": sample, "run_completed": completed}
        sources = con.execute(
            "SELECT source_id, mate, path, status, records, invalid FROM sources "
            "WHERE sample = ? ORDER BY source_id", [sample]).fetchall()
        if not sources:
            raise KeyError(f"未知样本 {sample!r}")
        info["sources"] = [
            {"source_id": r[0], "mate": r[1], "path": r[2],
             "status": r[3], "records": r[4], "invalid": r[5]}
            for r in sources]
        if name is None:
            info["states"] = _state_counts(con, "WHERE sample = ?", (sample,))
            info["stats"] = {m: v for m, v in con.execute(
                "SELECT metric, value FROM stats WHERE scope = ?",
                [sample]).fetchall()} if completed == "yes" else {}
            return info
        info["name"] = name
        row = con.execute(
            "SELECT state FROM key_states WHERE sample = ? AND name = ?",
            [sample, name]).fetchone()
        recs = con.execute(
            "SELECT source_id, rec_no, start_line, ok, err_code, err_msg, mate, "
            "header FROM records WHERE sample = ? AND name = ? "
            "ORDER BY source_id, rec_no", [sample, name]).fetchall()
        info["records"] = [
            {"source_id": r[0], "rec_no": r[1], "start_line": r[2], "ok": r[3],
             "err_code": r[4], "err_msg": r[5], "mate": r[6], "header": r[7]}
            for r in recs]
        if row is None:
            info["state"] = None
            info["note"] = ("该键没有任何终态" if not recs else
                            "该键只有无法确定名字的非法记录" if completed == "yes"
                            else "运行未完成，尚无终态")
            return info
        state = row[0]
        info["state"] = state
        if state in (model.STATE_PAIRED, model.STATE_FILTERED):
            p = con.execute(
                "SELECT len1_raw, length(seq1), len2_raw, length(seq2) "
                "FROM pairs WHERE sample = ? AND name = ?",
                [sample, name]).fetchone()
            info["len1_raw"], info["len1_after"], info["len2_raw"], \
                info["len2_after"] = p
        if state == model.STATE_PAIRED:
            rank = _scalar(con,
                "SELECT COUNT(*) FROM pairs p JOIN key_states k "
                "  ON k.sample = p.sample AND k.name = p.name "
                "WHERE k.state = 'paired' AND p.sample = ? AND p.name < ?",
                (sample, name)) + 1
            info["record_index"] = rank
            info["files"] = [f"{sample}_R1.fastq", f"{sample}_R2.fastq"]
            info["lines"] = f"第 {4 * rank - 3}..{4 * rank} 行"
        elif state == model.STATE_CONFLICT:
            info["contents"] = [
                {"mate": r[0], "seq": r[1], "qual": r[2],
                 "copies": r[3], "sources": r[4]}
                for r in con.execute(
                    "SELECT mate, seq, qual, copies, sources FROM ends "
                    "WHERE sample = ? AND name = ? ORDER BY mate, seq",
                    [sample, name]).fetchall()]
        elif state == model.STATE_ORPHAN:
            r = con.execute(
                "SELECT mate, length(seq), copies, sources FROM ends "
                "WHERE sample = ? AND name = ?", [sample, name]).fetchone()
            info["mate_present"], info["len"], info["copies"], info["sources"] = r
        return info
    finally:
        if own:
            con.close()


def where_text(db_path: str, sample: str, name: str | None = None,
               *, con=None) -> str:
    info = where(db_path, sample, name, con=con)
    out = [f"样本 {info['sample']}（运行完成: {info['run_completed']}）"]
    for s in info["sources"]:
        out.append(f"  源 {s['source_id']} mate={s['mate']} {s['path']} "
                   f"[{s['status']}] 记录 {s['records']} 非法 {s['invalid']}")
    if name is None:
        states = info.get("states", {})
        out.append("  终态分布: " + (", ".join(
            f"{k}={v}" for k, v in sorted(states.items())) or "（无）"))
        stats = info.get("stats") or {}
        for m in ("reads_before", "bases_before", "reads_after", "bases_after",
                  "bases_trimmed"):
            if m in stats:
                out.append(f"  {m}: {stats[m]}")
        return "\n".join(out)
    out.append(f"读段 {info['name']}  终态: {info['state'] or '（无）'}")
    if info.get("note"):
        out.append(f"  {info['note']}")
    for r in info.get("records", []):
        mark = "ok" if r["ok"] else f"{r['err_code']}: {r['err_msg']}"
        out.append(f"  记录 {r['source_id']}#{r['rec_no']} "
                   f"(第 {r['start_line']} 行起, mate={r['mate']}): {mark}")
        out.append(f"    原始头部: @{r['header']}")
    if info["state"] == model.STATE_PAIRED:
        out.append(f"  清洗: R1 {info['len1_raw']}->{info['len1_after']}, "
                   f"R2 {info['len2_raw']}->{info['len2_after']}")
        out.append(f"  去向: {', '.join(info['files'])} 的{info['lines']}"
                   f"（第 {info['record_index']} 对）")
    elif info["state"] == model.STATE_FILTERED:
        out.append(f"  清洗后长度过短: R1 {info['len1_raw']}->{info['len1_after']}, "
                   f"R2 {info['len2_raw']}->{info['len2_after']}")
    elif info["state"] == model.STATE_CONFLICT:
        for c in info.get("contents", []):
            out.append(f"  冲突内容 mate={c['mate']} copies={c['copies']} "
                       f"sources={c['sources']}")
            out.append(f"    seq={c['seq']} qual={c['qual']}")
    elif info["state"] == model.STATE_ORPHAN:
        out.append(f"  仅有 mate={info['mate_present']} "
                   f"(长度 {info['len']}, 来源 {info['sources']})")
    return "\n".join(out)
