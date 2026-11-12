"""清单装载与校验。

清单为 CSV，表头必须包含 source_id,sample,mate,path 四列（允许多余列）：

    source_id   源标识，全清单不可重复
    sample      样本名（用于输出文件名，不得含路径分隔符与控制字符）
    mate        1 或 2（该源所有记录应为同一端）
    path        普通 FASTQ 或 .gz；相对路径按清单所在位置解析

一个样本的同一端可以来自多个分片（多行同 sample 同 mate）。
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

REQUIRED_COLUMNS = ["source_id", "sample", "mate", "path"]


class ManifestFailure(Exception):
    """清单非法（缺列、缺值、source_id 重复、mate 非 1/2 等）。"""


@dataclass
class ManifestEntry:
    source_id: str
    sample: str
    mate: int
    path: str          # 清单中的原始写法
    resolved: Path     # 按清单所在位置解析后的路径
    line: int          # 清单中的行号（供报错定位）


def _sample_problem(sample: str) -> str | None:
    if sample in (".", ".."):
        return "样本名非法"
    if any(c in sample for c in "/\\") or any(ord(c) < 32 for c in sample):
        return "样本名含路径分隔符或控制字符"
    return None


def load_manifest(path) -> list[ManifestEntry]:
    path = Path(path)
    try:
        f = path.open("r", encoding="utf-8", newline="")
    except OSError as e:
        raise ManifestFailure(f"无法读取清单 {path}: {e}") from e
    problems: list[str] = []
    entries: list[ManifestEntry] = []
    with f:
        reader = csv.DictReader(f)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ManifestFailure(
                f"清单缺少列 {missing}（需要 {REQUIRED_COLUMNS}）")
        seen: dict[str, int] = {}
        for row in reader:
            line = reader.line_num
            sid = (row.get("source_id") or "").strip()
            sample = (row.get("sample") or "").strip()
            mate_s = (row.get("mate") or "").strip()
            p = (row.get("path") or "").strip()
            ok = True
            if not sid:
                problems.append(f"第 {line} 行: source_id 为空")
                ok = False
            elif sid in seen:
                problems.append(
                    f"第 {line} 行: source_id {sid!r} 与第 {seen[sid]} 行重复")
                ok = False
            else:
                seen[sid] = line
            if not sample:
                problems.append(f"第 {line} 行: sample 为空")
                ok = False
            elif (prob := _sample_problem(sample)):
                problems.append(f"第 {line} 行: {prob}: {sample!r}")
                ok = False
            if mate_s not in ("1", "2"):
                problems.append(f"第 {line} 行: mate 必须为 1 或 2，得到 {mate_s!r}")
                ok = False
            if not p:
                problems.append(f"第 {line} 行: path 为空")
                ok = False
            if ok:
                p_obj = Path(p)
                resolved = p_obj if p_obj.is_absolute() else (path.parent / p_obj)
                entries.append(ManifestEntry(
                    sid, sample, int(mate_s), p, resolved, line))
    if problems:
        raise ManifestFailure("清单校验失败:\n" + "\n".join(problems))
    if not entries:
        raise ManifestFailure("清单为空（没有任何数据行）")
    return entries
