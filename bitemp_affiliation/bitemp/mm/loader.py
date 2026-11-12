"""CSV 加载与严格校验。

道路配置（nodes.csv / edges.csv）任何一处不合法即拒绝整次运行，
错误信息指明文件与物理行号（表头为第 1 行）。观测文件
（observations.csv）的错误按 track_id 隔离：一条轨迹只要含坏记录，
整条轨迹拒收并逐条列出原因，其余轨迹照常处理。所有判断只依赖记录
内容与排序后的时间，绝不依赖文件行序。
"""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from pathlib import Path

from .geometry import Edge, euclid

NODES_HEADER = ["node_id", "x", "y"]
EDGES_HEADER = ["edge_id", "from_node", "to_node"]
OBS_HEADER = ["track_id", "obs_id", "t", "x", "y"]


class RoadConfigError(Exception):
    """道路配置不合法，整次运行拒绝。messages 每条指明文件与行号。"""

    def __init__(self, messages: list[str]):
        self.messages = messages
        super().__init__("; ".join(messages))


@dataclass(frozen=True)
class Node:
    node_id: str
    x: float
    y: float


@dataclass(frozen=True)
class Observation:
    track_id: str
    obs_id: str
    t: float
    x: float
    y: float
    line_no: int


@dataclass
class LoadedData:
    nodes: dict[str, Node]
    edges: list[Edge]
    tracks: dict[str, list[Observation]]          # 已按 t 严格递增排序
    rejected: dict[str, list[str]]                # track_id -> 原因列表
    obs_line_count: int


def _open_rows(path: str | Path):
    p = Path(path)
    if not p.is_file():
        raise RoadConfigError([f"{p}: 文件不存在或不是普通文件"])
    # newline='' 让 csv 自行处理换行；utf-8-sig 容忍 BOM
    f = open(p, "r", encoding="utf-8-sig", newline="")
    try:
        rows = list(csv.reader(f))
    except UnicodeDecodeError as e:
        f.close()
        raise RoadConfigError([f"{p}: 不是合法 UTF-8 文本（{e}）"]) from None
    f.close()
    return p, rows


def _check_header(path: Path, rows: list[list[str]], expected: list[str]) -> bool:
    if not rows:
        raise RoadConfigError([f"{path}: 文件为空，缺少表头 {','.join(expected)}"])
    header = [c.strip() for c in rows[0]]
    if header != expected:
        raise RoadConfigError([
            f"{path}: 第 1 行表头应为 {','.join(expected)}，实际为 {','.join(header)}"
        ])
    return True


def _finite(token: str) -> float:
    v = float(token)
    if not math.isfinite(v):
        raise ValueError("只接受有限数")
    return v


def _load_nodes(path: str | Path) -> dict[str, Node]:
    p, rows = _open_rows(path)
    _check_header(p, rows, NODES_HEADER)
    nodes: dict[str, Node] = {}
    errors: list[str] = []
    for i, row in enumerate(rows[1:], start=2):
        if not row or all(c == "" for c in row):
            continue  # 完全空行跳过
        if len(row) != 3:
            errors.append(f"{p.name} 第 {i} 行: 应为 3 列，实际 {len(row)} 列")
            continue
        nid, sx, sy = (c.strip() for c in row)
        if not nid:
            errors.append(f"{p.name} 第 {i} 行: node_id 为空")
            continue
        if nid in nodes:
            errors.append(f"{p.name} 第 {i} 行: node_id '{nid}' 重复")
            continue
        try:
            x = _finite(sx)
            y = _finite(sy)
        except ValueError:
            errors.append(f"{p.name} 第 {i} 行: 坐标必须是有限数，得到 '{sx}','{sy}'")
            continue
        nodes[nid] = Node(nid, x, y)
    if errors:
        raise RoadConfigError(errors)
    if not nodes:
        raise RoadConfigError([f"{p.name}: 没有任何节点记录"])
    return nodes


def _load_edges(path: str | Path, nodes: dict[str, Node]) -> list[Edge]:
    p, rows = _open_rows(path)
    _check_header(p, rows, EDGES_HEADER)
    edges: list[Edge] = []
    seen: set[str] = set()
    errors: list[str] = []
    for i, row in enumerate(rows[1:], start=2):
        if not row or all(c == "" for c in row):
            continue
        if len(row) != 3:
            errors.append(f"{p.name} 第 {i} 行: 应为 3 列，实际 {len(row)} 列")
            continue
        eid, fn, tn = (c.strip() for c in row)
        if not eid:
            errors.append(f"{p.name} 第 {i} 行: edge_id 为空")
            continue
        if eid in seen:
            errors.append(f"{p.name} 第 {i} 行: edge_id '{eid}' 重复")
            continue
        if not fn or not tn:
            errors.append(f"{p.name} 第 {i} 行: from_node/to_node 不能为空")
            continue
        if fn not in nodes:
            errors.append(f"{p.name} 第 {i} 行: from_node '{fn}' 不存在于 nodes.csv")
            continue
        if tn not in nodes:
            errors.append(f"{p.name} 第 {i} 行: to_node '{tn}' 不存在于 nodes.csv")
            continue
        if fn == tn:
            errors.append(f"{p.name} 第 {i} 行: 边 '{eid}' 的起止节点相同，自环不允许")
            continue
        a, b = nodes[fn], nodes[tn]
        length = euclid(a.x, a.y, b.x, b.y)
        if not length > 0.0:
            errors.append(f"{p.name} 第 {i} 行: 边 '{eid}' 长度为 0，端点坐标重合")
            continue
        seen.add(eid)
        edges.append(Edge(eid, fn, tn, length, a.x, a.y, b.x, b.y))
    if errors:
        raise RoadConfigError(errors)
    if not edges:
        raise RoadConfigError([f"{p.name}: 没有任何边记录"])
    return edges


def _load_observations(path: str | Path) -> tuple[
        dict[str, list[Observation]], dict[str, list[str]], int]:
    p, rows = _open_rows(path)
    _check_header(p, rows, OBS_HEADER)
    raw: dict[str, list[Observation]] = {}
    # 只有确实出现坏记录的轨迹才进入 rejected
    rejected: dict[str, list[str]] = {}

    def bad(tid: str, reason: str) -> None:
        rejected.setdefault(tid, []).append(reason)

    for i, row in enumerate(rows[1:], start=2):
        if not row or all(c == "" for c in row):
            continue
        if len(row) != 5:
            tid = row[0].strip() if row else ""
            bad(tid or f"<第 {i} 行无法识别的轨迹>",
                f"observations.csv 第 {i} 行: 应为 5 列，实际 {len(row)} 列")
            continue
        tid, oid, st, sx, sy = (c.strip() for c in row)
        if not tid:
            bad(f"<第 {i} 行 track_id 为空>",
                f"observations.csv 第 {i} 行: track_id 为空")
            continue
        if not oid:
            bad(tid, f"observations.csv 第 {i} 行: obs_id 为空")
            continue
        try:
            t = _finite(st)
            x = _finite(sx)
            y = _finite(sy)
        except ValueError:
            bad(tid,
                f"observations.csv 第 {i} 行: t/x/y 必须是有限数，"
                f"得到 '{st}','{sx}','{sy}'")
            continue
        raw.setdefault(tid, []).append(Observation(tid, oid, t, x, y, i))

    good: dict[str, list[Observation]] = {}
    for tid, obs_list in raw.items():
        reasons: list[str] = []
        ids = [o.obs_id for o in obs_list]
        dup_ids = sorted({o for o in ids if ids.count(o) > 1})
        for oid in dup_ids:
            lines = [str(o.line_no) for o in obs_list if o.obs_id == oid]
            reasons.append(
                f"observations.csv 第 {','.join(lines)} 行: track '{tid}' 内 "
                f"obs_id '{oid}' 重复")
        ordered = sorted(obs_list, key=lambda o: (o.t, o.line_no))
        for prev, cur in zip(ordered, ordered[1:]):
            if cur.t <= prev.t:
                reasons.append(
                    f"observations.csv 第 {cur.line_no} 行: track '{tid}' 按 t 排序后 "
                    f"时间非严格递增（{prev.t} 后出现 {cur.t}）")
                break
        if reasons:
            rejected.setdefault(tid, []).extend(reasons)
            continue
        good[tid] = ordered
    return good, rejected, len(rows)


def load_all(nodes_csv: str | Path, edges_csv: str | Path,
             obs_csv: str | Path) -> LoadedData:
    """加载并校验三个 CSV。道路配置坏 -> RoadConfigError；坏轨迹在 rejected。"""
    nodes = _load_nodes(nodes_csv)
    edges = _load_edges(edges_csv, nodes)
    tracks, rejected, line_count = _load_observations(obs_csv)
    return LoadedData(nodes, edges, tracks, rejected, line_count)
