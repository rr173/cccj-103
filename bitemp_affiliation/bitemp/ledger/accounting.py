"""资源核算：把节点采样窗与 attempt 已观测运行时段求交并积分。

口径（README 同步说明）：
- 采样窗是 node/boot_id 上的半开区间 [sample_from, sample_to)；同一
  node/boot_id 内窗不相交（入库前已校验，相交为致命错误），不同
  boot_id 的窗允许重叠（节点重启后的两次观测会话）。
- 仅统计与 attempt *已观测* 运行片段的交集；窗被起止时刻截断。
- 不同 boot_id 的窗在同一时刻重叠时，取 boot_id 的 UTF-8 字节序最小者
  记账，保证确定且可复算；被让位的窗仍保留在 raw_samples 可查。
- 未被任何窗覆盖的运行时间进入 missing_intervals，绝不补零或沿用。
- CPU = Σ 平均核心数 × 相交秒数；RSS byte-seconds = Σ peak_rss × 秒；
  峰值内存 = 实际覆盖到该 attempt 的窗中最大的 peak_rss（缺测段不猜）。
"""
from __future__ import annotations

from dataclasses import dataclass

from .lifecycle import Segment
from .model import RawSample


@dataclass
class Slice:
    from_epoch: float
    to_epoch: float
    overlap_seconds: float
    cpu_cores: float | None
    peak_rss_bytes: float | None
    boot_id: str | None
    sample_from: str | None
    sample_to: str | None
    sample_line: int | None
    missing: bool


@dataclass
class SegmentAccounting:
    segment: Segment
    observed_end: float
    slices: list[Slice]
    missing: list[tuple[float, float]]
    run_seconds: float
    covered_seconds: float
    cpu_core_seconds: float
    rss_byte_seconds: float
    peak_rss_bytes: float | None


def _merge_intervals(parts: list[tuple[float, float]]) -> float:
    """并集长度（用于覆盖比例，重叠不重复计）。"""
    if not parts:
        return 0.0
    parts = sorted(parts)
    total = 0.0
    lo, hi = parts[0]
    for a, b in parts[1:]:
        if a <= hi:
            hi = max(hi, b)
        else:
            total += hi - lo
            lo, hi = a, b
    total += hi - lo
    return total


def account_segment(segment: Segment, observed_end: float,
                    windows: list[RawSample]) -> SegmentAccounting:
    """对单个运行片段做窗交切与积分。

    windows 已按 node 过滤、且时间上与片段有交集；不同 boot_id 重叠时
    以 boot_id UTF-8 字节序最小者为准。
    """
    seg_start = segment.start_epoch
    seg_end = observed_end  # 开放片段截到该 attempt 最后一个观测事件
    run_seconds = seg_end - seg_start
    if run_seconds < 0:
        raise ValueError("运行片段长度为负，事件排序异常")

    # 落在本片段节点、时间相交的窗
    hits = [w for w in windows
            if w.node == segment.node
            and w.sample_to.timestamp() > seg_start
            and w.sample_from.timestamp() < seg_end]
    # 边界点集合：片段端点 + 相交窗端点（截断到片段内）
    bounds = {seg_start, seg_end}
    for w in hits:
        wf = max(seg_start, w.sample_from.timestamp())
        wt = min(seg_end, w.sample_to.timestamp())
        if wt > wf:
            bounds.add(wf)
            bounds.add(wt)
    grid = sorted(bounds)

    slices: list[Slice] = []
    missing: list[tuple[float, float]] = []
    covered_parts: list[tuple[float, float]] = []
    cpu_sec = rss_sec = 0.0
    peak: float | None = None
    for lo, hi in zip(grid, grid[1:]):
        if hi <= lo:
            continue
        mid = (lo + hi) / 2.0
        covering = [w for w in hits
                    if w.sample_from.timestamp() <= mid
                    < w.sample_to.timestamp()]
        dur = hi - lo
        if not covering:
            slices.append(Slice(lo, hi, dur, None, None, None, None, None,
                                None, True))
            missing.append((lo, hi))
            continue
        win = min(covering, key=lambda w: w.boot_id.encode("utf-8"))
        slices.append(Slice(
            lo, hi, dur, win.cpu_cores, win.peak_rss_bytes, win.boot_id,
            win.sample_from.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+00:00",
            win.sample_to.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+00:00",
            win.source_line, False))
        covered_parts.append((lo, hi))
        cpu_sec += win.cpu_cores * dur
        rss_sec += win.peak_rss_bytes * dur
        peak = win.peak_rss_bytes if peak is None else max(peak, win.peak_rss_bytes)

    covered = _merge_intervals(covered_parts)
    return SegmentAccounting(
        segment=segment, observed_end=seg_end, slices=slices, missing=missing,
        run_seconds=run_seconds, covered_seconds=covered,
        cpu_core_seconds=cpu_sec, rss_byte_seconds=rss_sec,
        peak_rss_bytes=peak)
