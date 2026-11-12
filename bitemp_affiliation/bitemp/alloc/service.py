"""分摊管线的高层入口：装载 / 试算 / 正式计算 / 解释。

典型用法
--------
    svc = AllocationService(con)
    svc.load_rules(raw_edge_rows)                       # 事务性、固定摘要、幂等
    svc.load_items(raw_item_rows)                       # 同 key 同内容跳过
    trial = svc.trial(asof, day_range=(...))            # 试算，不落库
    run_id = svc.compute(asof, day_range=(...))         # 正式计算，落库并绑定版本
    svc.explain(run_id, "ITEM-1")                       # 从落库结果解释
"""
from __future__ import annotations

from datetime import date

from . import queries as q
from . import store
from .allocate import ComputeResult, parse_items, run_allocation
from .model import parse_asof


class AllocationService:
    def __init__(self, con, ensure_schema: bool = True):
        self.con = con
        if ensure_schema:
            self.ensure_schema()

    def ensure_schema(self) -> None:
        for stmt in store.DDL:
            self.con.execute(stmt)

    # ---------------------------------------------------------- 装载
    def load_rules(self, raw_edges: list[dict], *, note: str | None = None) -> store.RuleImportReport:
        return store.import_rules(self.con, raw_edges, note=note)

    def load_items(self, raw_items: list[dict]) -> store.ItemImportReport:
        return store.import_items(self.con, parse_items(raw_items))

    def load_rules_jsonl(self, path, *, note: str | None = None) -> store.RuleImportReport:
        from .io import read_jsonl
        return self.load_rules(read_jsonl(path), note=note)

    def load_items_jsonl(self, path) -> store.ItemImportReport:
        from .io import read_jsonl
        return self.load_items(read_jsonl(path))

    # ---------------------------------------------------------- 计算
    def _prepare(self, asof, version_id: str | None, item_keys, day_range,
                 kind: str):
        asof_dt = parse_asof(asof)
        if version_id is None:
            version_id = store.latest_version(self.con)
            if version_id is None:
                raise KeyError("尚无已装载的规则版本")
        version_id, digest = store.get_version(self.con, version_id)
        edges = store.load_edges(self.con, version_id)
        items = store.load_items(self.con, item_keys=item_keys, day_range=day_range)
        return asof_dt, version_id, digest, edges, items

    def trial(self, asof, *, version_id: str | None = None,
              item_keys: list[str] | None = None,
              day_range: tuple[date | None, date | None] | None = None
              ) -> ComputeResult:
        """试算：完整计算但不落库，不改变任何表。"""
        asof_dt, vid, digest, edges, items = self._prepare(
            asof, version_id, item_keys, day_range, "trial")
        return run_allocation(self.con, asof_dt, items, edges, vid, digest,
                              kind="trial")

    def compute(self, asof, *, version_id: str | None = None,
                item_keys: list[str] | None = None,
                day_range: tuple[date | None, date | None] | None = None
                ) -> str:
        """正式计算：落库并返回 run_id；结果永久绑定 version_id + asof。"""
        asof_dt, vid, digest, edges, items = self._prepare(
            asof, version_id, item_keys, day_range, "official")
        result = run_allocation(self.con, asof_dt, items, edges, vid, digest,
                               kind="official")
        return store.persist_result(self.con, result)

    # ---------------------------------------------------------- 解释
    def explain(self, run_id: str, item_key: str, terminal: str | None = None) -> dict:
        return q.explain_from_store(self.con, run_id, item_key, terminal)

    def explain_text(self, run_id: str, item_key: str,
                     terminal: str | None = None) -> str:
        return q.explain_text(self.con, run_id, item_key, terminal)

    def monthly_rollup(self, run_id: str) -> list[tuple]:
        return q.monthly_rollup(self.con, run_id)

    def unresolved(self, run_id: str) -> dict:
        return q.unresolved_detail(self.con, run_id)


def allocate_preview(raw_items: list[dict], raw_edges: list[dict], asof,
                     org_resolver) -> ComputeResult:
    """无数据库场景的纯内存计算（金标准/单元测试用）。

    org_resolver(item) -> 起点机构或 None，模拟 org_at。
    """
    from .graph import RuleGraph
    from .model import edge_from_row
    parsed_edges = [edge_from_row(r, i) for i, r in enumerate(raw_edges)]
    graph = RuleGraph(parsed_edges)
    issues = graph.issues()
    if issues:
        from .store import RuleRejected
        raise RuleRejected(issues)
    items = parse_items(raw_items)
    asof_dt = parse_asof(asof)
    from .graph import propagate
    from .allocate import largest_remainder, TerminalAllocation, ItemAllocation

    allocations: list[ItemAllocation] = []
    for it in items:
        org = org_resolver(it)
        if org is None:
            allocations.append(ItemAllocation(it, None, [], [], None,
                                              reason="未找到起点机构"))
            continue
        prop = propagate(graph.active_for_day(it.day), org)
        weights = {t: prop.weight(t) for t in prop.terminals}
        amounts = largest_remainder(it.amount, weights)
        allocs = [TerminalAllocation(t, amounts[t], weights[t])
                  for t in sorted(weights) if weights[t] > 0]
        allocations.append(ItemAllocation(it, org, allocs, prop.flows(), prop))
    return ComputeResult("(memory)", "(memory)", asof_dt, "preview",
                         allocations, graph)
