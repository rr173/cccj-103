"""alloc：机构联盟科研经费分摊管线（末级整数份额 + 可审计路由）。

复用 bitemp 的双时态维表：每个事项的起点机构由 ``bitemp.queries.org_at`` 在给定
``asof`` 下解析，再沿带日期活跃期的有理数份额网络做精确拓扑传播，只在末端一次性
按最大余数法取整。
"""
from .allocate import (ComputeResult, ItemAllocation, TerminalAllocation,
                       largest_remainder, run_allocation)
from .graph import (Explanation, Issue, Propagation, RuleGraph,
                    explain_terminal, propagate, validate_edges)
from .io import read_jsonl, write_jsonl
from .model import Edge, Item, parse_asof, rule_digest
from .queries import (explain_from_store, explain_text, monthly_rollup,
                      unresolved_detail)
from .service import AllocationService, allocate_preview
from .store import (ItemConflict, ItemImportReport, RuleImportReport,
                    RuleRejected, import_items, import_rules)

__all__ = [
    "AllocationService", "allocate_preview", "ComputeResult", "ItemAllocation",
    "TerminalAllocation", "largest_remainder", "run_allocation",
    "Explanation", "Issue", "Propagation", "RuleGraph", "explain_terminal",
    "propagate", "validate_edges", "Edge", "Item", "parse_asof", "rule_digest",
    "explain_from_store", "explain_text", "monthly_rollup",
    "unresolved_detail", "ItemConflict", "ItemImportReport",
    "RuleImportReport", "RuleRejected", "import_items", "import_rules",
    "read_jsonl", "write_jsonl",
]
