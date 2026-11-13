"""流式输入解析：events.jsonl / samples.csv / dependencies.csv。

设计约束（见 README「内存口径」）：
- 逐行读取，按批吐给入库层；除解析当前行所需对象外不保留整个事件集，
  调用方可控制批量大小，峰值常驻对象数与输入总行数无关。
- 每行同步喂给 hasher，做与行序无关的规范化内容摘要。
- 结构/格式错误累计到 LedgerInputError，带物理行号（表头为第 1 行）。
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from .model import (DEP_HEADER, EVENT_HEADER, EVENT_TYPES, SAMPLE_HEADER,
                    DepEdge, LedgerInputError, RawEvent, RawSample)


# ------------------------------------------------------------ 内容摘要

class CanonicalHasher:
    """与行序无关的规范化内容摘要。

    对每条记录渲染统一的规范化行，按记录键排序后逐行 update 哈希；
    仅调整文件行序时规范化行集合不变，摘要完全一致。摘要基于已归一化
    的内容（时间统一零时区、数值按输入字符串保留），而非原始字节。
    """

    def __init__(self) -> None:
        # 用集合：完全相同的重复到达（折叠）不影响摘要
        self._rows: set[str] = set()

    def add(self, canonical_row: str) -> None:
        self._rows.add(canonical_row)

    def digest(self) -> tuple[str, int]:
        h = hashlib.sha256()
        for row in sorted(self._rows):
            h.update(row.encode("utf-8"))
            h.update(b"\n")
        return h.hexdigest(), len(self._rows)


# ------------------------------------------------------------ 时间解析

def parse_iso_utc(value: str, what: str, line_no: int,
                  errors: list[str]) -> datetime | None:
    """解析含时区偏移的 ISO 8601，统一换算至零时区；缺失/错误记为错误。"""
    if value is None or value == "":
        errors.append(f"第 {line_no} 行: {what} 时间为空")
        return None
    text = value.strip()
    # datetime.fromisoformat 在 3.11+ 支持 'Z' 结尾；显式替换以防旧写法。
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"第 {line_no} 行: {what} 不是合法 ISO 8601 时间: {value!r}")
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        errors.append(f"第 {line_no} 行: {what} 缺少时区偏移（要求含零时区或其他偏移）: {value!r}")
        return None
    return dt.astimezone(timezone.utc)


def _require_nonempty(rec: dict, key: str, line_no: int,
                      errors: list[str]) -> str:
    val = rec.get(key)
    if val is None or str(val).strip() == "":
        errors.append(f"第 {line_no} 行: 字段 {key} 不能为空")
        return ""
    return str(val).strip()


def _finite_float(value: str, what: str, line_no: int,
                  errors: list[str]) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        errors.append(f"第 {line_no} 行: {what} 不是数值: {value!r}")
        return None
    if not math.isfinite(x):
        errors.append(f"第 {line_no} 行: {what} 必须为有限数: {value!r}")
        return None
    if x < 0:
        errors.append(f"第 {line_no} 行: {what} 不能为负: {value!r}")
        return None
    return x


def _check_header(actual: list[str], expected: list[str], path: Path,
                  errors: list[str]) -> None:
    if actual != expected:
        errors.append(
            f"{path}: 表头应为 {','.join(expected)}，实际为 {','.join(actual)}")


# ------------------------------------------------------------ events.jsonl

def iter_events(path: str | Path, hasher: CanonicalHasher,
                batch_size: int = 1000,
                ) -> Iterator[list[RawEvent]]:
    """逐行解析 events.jsonl，按批产出 RawEvent。

    JSON 不允许 NaN/Infinity（parse_constant 拦截）；未知 event_type、
    坏时间、空字段累计错误，最后由调用方统一抛出。
    """
    p = Path(path)
    errors: list[str] = []
    batch: list[RawEvent] = []

    def _bad_const(token: str) -> None:
        raise ValueError(f"禁止 JSON 非常量数值 {token}")

    with p.open("r", encoding="utf-8") as f:
        line_no = 0
        for raw_line in f:
            line_no += 1
            if raw_line.strip() == "":
                continue
            try:
                rec = json.loads(raw_line, parse_constant=_bad_const)
            except (json.JSONDecodeError, ValueError) as e:
                errors.append(f"第 {line_no} 行: JSON 解析失败: {e}")
                continue
            if not isinstance(rec, dict):
                errors.append(f"第 {line_no} 行: 事件必须是 JSON 对象")
                continue
            missing = [k for k in EVENT_HEADER if k not in rec]
            extra = [k for k in rec if k not in EVENT_HEADER]
            if missing or extra:
                if missing:
                    errors.append(f"第 {line_no} 行: 缺少字段 {','.join(missing)}")
                if extra:
                    errors.append(f"第 {line_no} 行: 存在未知字段 {','.join(extra)}")
                continue
            ev_id = _require_nonempty(rec, "event_id", line_no, errors)
            job_id = _require_nonempty(rec, "job_id", line_no, errors)
            attempt_id = _require_nonempty(rec, "attempt_id", line_no, errors)
            etype = _require_nonempty(rec, "event_type", line_no, errors)
            if etype and etype not in EVENT_TYPES:
                errors.append(f"第 {line_no} 行: 未知 event_type {etype!r}")
                etype = ""
            at = parse_iso_utc(str(rec.get("at", "")), "at", line_no, errors)
            node = str(rec.get("node") or "").strip()
            checkpoint = str(rec.get("checkpoint_id") or "").strip()
            retry = str(rec.get("retry_of") or "").strip()
            # 本行已有任何格式/结构错误：登记后跳过，不产出半成品记录
            if any(m.startswith(f"第 {line_no} 行") for m in errors):
                continue
            ev = RawEvent(ev_id, job_id, attempt_id, etype, at, node,
                          checkpoint, retry, line_no)
            hasher.add("\x1f".join([
                "event", ev_id, job_id, attempt_id, etype,
                at.strftime("%Y-%m-%dT%H:%M:%S.%f"), node, checkpoint, retry,
            ]))
            batch.append(ev)
            if len(batch) >= batch_size:
                yield batch
                batch = []
    if errors:
        raise LedgerInputError(errors)
    if batch:
        yield batch


# ------------------------------------------------------------ samples.csv

def iter_samples(path: str | Path, hasher: CanonicalHasher,
                 batch_size: int = 1000) -> Iterator[list[RawSample]]:
    p = Path(path)
    errors: list[str] = []
    batch: list[RawSample] = []
    with p.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            raise LedgerInputError([f"{p}: samples.csv 为空，缺少表头"])
        _check_header(header, SAMPLE_HEADER, p, errors)
        line_no = 1
        for row in reader:
            line_no += 1
            if not row or all(v.strip() == "" for v in row):
                continue
            if len(row) != len(SAMPLE_HEADER):
                errors.append(
                    f"第 {line_no} 行: 列数 {len(row)} 与表头 {len(SAMPLE_HEADER)} 不一致")
                continue
            node, boot, sf, st, cpu, rss = row
            node = node.strip()
            boot = boot.strip()
            if not node:
                errors.append(f"第 {line_no} 行: node 不能为空")
            if not boot:
                errors.append(f"第 {line_no} 行: boot_id 不能为空")
            t_from = parse_iso_utc(sf, "sample_from", line_no, errors)
            t_to = parse_iso_utc(st, "sample_to", line_no, errors)
            cpu_v = _finite_float(cpu.strip(), "cpu_cores", line_no, errors)
            rss_v = _finite_float(rss.strip(), "peak_rss_bytes", line_no, errors)
            if t_from and t_to and t_to <= t_from:
                errors.append(
                    f"第 {line_no} 行: 采样窗必须为正长（sample_to > sample_from）")
            if not (node and boot and t_from and t_to and
                    cpu_v is not None and rss_v is not None):
                continue
            s = RawSample(node, boot, t_from, t_to, cpu_v, rss_v, line_no)
            hasher.add("\x1f".join([
                "sample", node, boot,
                t_from.strftime("%Y-%m-%dT%H:%M:%S.%f"),
                t_to.strftime("%Y-%m-%dT%H:%M:%S.%f"),
                repr(cpu_v), repr(rss_v),
            ]))
            batch.append(s)
            if len(batch) >= batch_size:
                yield batch
                batch = []
    if errors:
        raise LedgerInputError(errors)
    if batch:
        yield batch


# ------------------------------------------------------------ dependencies.csv

def iter_dependencies(path: str | Path, hasher: CanonicalHasher,
                      ) -> list[DepEdge]:
    """依赖通常很小，直接物化（每条边都要参与全图校验与路径计算）。"""
    p = Path(path)
    errors: list[str] = []
    edges: list[DepEdge] = []
    with p.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            raise LedgerInputError([f"{p}: dependencies.csv 为空，缺少表头"])
        _check_header(header, DEP_HEADER, p, errors)
        line_no = 1
        for row in reader:
            line_no += 1
            if not row or all(v.strip() == "" for v in row):
                continue
            if len(row) != 2:
                errors.append(f"第 {line_no} 行: 依赖表必须恰好两列")
                continue
            parent, child = row[0].strip(), row[1].strip()
            if not parent:
                errors.append(f"第 {line_no} 行: parent_job 不能为空")
            if not child:
                errors.append(f"第 {line_no} 行: child_job 不能为空")
            if not parent or not child:
                continue
            edges.append(DepEdge(parent, child, line_no))
            hasher.add("\x1f".join(["dep", parent, child]))
    if errors:
        raise LedgerInputError(errors)
    return edges
