"""数据类、输入头定义与致命输入错误。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

EVENT_TYPES = frozenset({
    "submitted", "started", "checkpoint_saved", "preempted",
    "resumed", "succeeded", "failed",
})

EVENT_HEADER = [
    "event_id", "job_id", "attempt_id", "event_type", "at", "node",
    "checkpoint_id", "retry_of",
]
SAMPLE_HEADER = [
    "node", "boot_id", "sample_from", "sample_to", "cpu_cores",
    "peak_rss_bytes",
]
DEP_HEADER = ["parent_job", "child_job"]


class LedgerInputError(Exception):
    """致命输入错误：整次导入放弃，不写任何版本。

    结构/格式/全局一致性问题（坏行、event_id 冲突、attempt_id 跨 job
    复用、采样窗相交等）无法隔离到单个 job 的生命周期，按需求放弃本次
    写入；单个 job 的生命周期错误走 job_issues 隔离，不在此列。
    """

    def __init__(self, messages: list[str]):
        super().__init__("；".join(messages))
        self.messages = messages


@dataclass(frozen=True)
class RawEvent:
    event_id: str
    job_id: str
    attempt_id: str
    event_type: str
    at: datetime          # 已换算到零时区的 aware datetime
    node: str
    checkpoint_id: str
    retry_of: str
    source_line: int


@dataclass(frozen=True)
class RawSample:
    node: str
    boot_id: str
    sample_from: datetime
    sample_to: datetime
    cpu_cores: float
    peak_rss_bytes: float
    source_line: int


@dataclass(frozen=True)
class DepEdge:
    parent_job: str
    child_job: str
    source_line: int


def utc_iso(dt: datetime) -> str:
    """把已归一化到零时区的 datetime 渲染为带 +00:00 的 ISO 8601。"""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+00:00"
