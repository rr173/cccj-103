"""报表主次抑制：把某个已存计算批次的月度 × 末级机构结果变成可公开的表。

语义
====
* 行 = 月份（事项日期的 ``%Y-%m``），列 = 末级机构，格 = 该批次的整数末端金额之和。
  同一 (月份, 机构) 只建模**确有份额记录**的格子；未出现的组合是结构零（公开已知）。
* 贡献人数：同一研究者在同一格子里有多笔非零份额记录只算一个贡献人；
  正负冲回可以让金额为零，但只要存在非零份额记录，贡献人仍计入。
* 主抑制 = 贡献人数 < k 的格子（必须隐藏）。
* 次抑制 = 为解除线性反推而额外隐藏的正常格子。

保护判据（对整张表的线性方程一起考虑）
------------------------------------
把每个隐藏格子的金额看作可正可负的未知整数。已发布的约束是行合计、列合计、
以及每个可见格子的精确金额（其值为固定常数）。可见格子的方程直接固定了该格；
把固定值代入行/列合计后，剩下的未知量只剩“每行、每列的带符号隐藏格之和
为 0”这一个条件。令隐藏子图为二部图 H（行节点、列节点、隐藏格子为边）。

**定理**：H 中每个隐藏格子的金额都不被唯一确定 ⇔ H 的每条边都在某个环上
（H 没有桥）。等价地，剩余方程的解空间恰为 H 的**圈空间**（维数
E − V + 连通分量数），且某个隐藏格被钉死 ⇔ 它在 H 里是桥。

证明要点：
* 剩余约束“每个行/列节点的带符号隐藏格之和为 0”的整数解构成 H 的圈空间：
  二部图的带符号关联矩阵的零空间由环生成。
* 若某隐藏边是桥，删去它会把 H 的一侧与另一侧分开；两侧各自必须“收支平衡”，
  于是该边的值被两侧唯一固定。反之若无桥，每条边都在一个环上，环可独立
  加 1 / 减 1，给出该边的非零扰动见证（只在环上取 ±1）。
* 所以无需逐点检查“每行每列至少藏两个”：两个不相连的矩形块各自安全，
  但用一条额外隐藏的桥连起来时，桥本身会被全表方程唯一解出——本模块按
  整张表的图判定，不按行列局部计数。

候选集与搜索
------------
* 额外隐藏候选 = 所有“非主抑制”的现存格子；每格可设非负整数成本，
  未显式给成本者默认 1（见 ``Policy.cost_for``）。
* 额外隐藏数量 = 被选为次抑制的格子个数（与成本独立）。
* 目标按序：总成本最小 → 额外隐藏数量最少 → 排序后的 (月份, 机构) 列表字典序最小。
* 搜索用最优优先（对 2^n 个掩码按目标键排序）：父掩码的键严格优于任何子掩码，
  因此第一个弹出的可行掩码即全局最优；不需要额外的启发式或近似。
* ``max_candidates`` 是显式搜索规模上限：候选数超过它时返回 ``incomplete``
  （``verified=False``，``solution=None``），绝不把启发式结果冒充最优。

审计与公开两个入口
------------------
* :func:`export_public` 只产出可见金额、隐藏占位与行列合计；不含隐藏金额、
  贡献人数、研究者标识或任何可还原明细。
* :func:`build_audit` 产出输入绑定、策略摘要、主/次抑制集合、目标值、
  以及每个隐藏格子的**非零整数扰动见证**（可独立机检）。
"""
from __future__ import annotations

import heapq
import json
from dataclasses import dataclass, field
from hashlib import sha256

from .queries import leaf_amounts, monthly_rollup, run_meta, run_items, unresolved_detail

PUBLIC_FORMAT = "alloc-disclosure/public/v1"
AUDIT_FORMAT = "alloc-disclosure/audit/v1"


# ---------------------------------------------------------------- 错误

class NotPublishable(Exception):
    """策略不可发布（例如强制公开主抑制格子），或不存在满足保护条件的方案。"""

    def __init__(self, reason: str, detail: dict | None = None):
        self.reason = reason
        self.detail = detail or {}
        super().__init__(reason)


# ---------------------------------------------------------------- 策略

@dataclass(frozen=True)
class Policy:
    """发布策略。

    min_contributors: 最小贡献人数 k（>=1）；贡献人数 < k 的格子是主抑制。
    costs: 每个机构格子的额外隐藏成本（非负整数）；未给出的用 default_cost。
    must_publish: 必须公开的格子集合（(月份, 机构) 二元组）。
    max_candidates: 显式搜索规模上限；候选数超过它时不宣称最优。
    """
    min_contributors: int = 3
    costs: dict = field(default_factory=dict)
    default_cost: int = 1
    must_publish: frozenset = frozenset()
    max_candidates: int = 18

    def __post_init__(self):
        if isinstance(self.min_contributors, bool) or not isinstance(self.min_contributors, int):
            raise ValueError("min_contributors 必须是整数")
        if self.min_contributors < 1:
            raise ValueError("min_contributors 必须 >= 1")
        if isinstance(self.default_cost, bool) or not isinstance(self.default_cost, int):
            raise ValueError("default_cost 必须是整数")
        if self.default_cost < 0:
            raise ValueError("default_cost 必须是非负整数")
        if isinstance(self.max_candidates, bool) or not isinstance(self.max_candidates, int):
            raise ValueError("max_candidates 必须是整数")
        if self.max_candidates < 0:
            raise ValueError("max_candidates 必须是非负整数")
        # 成本字典统一规范成 (str, str) -> int，便于摘要与查找
        norm = {}
        for key, v in dict(self.costs).items():
            if not (isinstance(key, tuple) and len(key) == 2):
                raise ValueError(f"成本键必须是 (月份, 机构): {key!r}")
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise ValueError(f"成本必须是非负整数: {key!r} -> {v!r}")
            norm[(str(key[0]), str(key[1]))] = v
        object.__setattr__(self, "costs", norm)
        object.__setattr__(self, "must_publish",
                           frozenset((str(m), str(o)) for m, o in self.must_publish))

    def cost_for(self, month: str, org: str) -> int:
        return self.costs.get((month, org), self.default_cost)

    def as_dict(self) -> dict:
        return {
            "min_contributors": self.min_contributors,
            "costs": {f"{m}|{o}": c for (m, o), c in sorted(self.costs.items())},
            "default_cost": self.default_cost,
            "must_publish": sorted([m, o] for m, o in self.must_publish),
            "max_candidates": self.max_candidates,
        }

    def digest(self) -> str:
        blob = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8")
        return sha256(blob).hexdigest()


# ---------------------------------------------------------------- 表构建

@dataclass(frozen=True)
class Cell:
    month: str
    org: str
    amount: int
    contributors: int        # 该格中非零份额记录的去重研究者数

    @property
    def key(self) -> tuple[str, str]:
        return (self.month, self.org)


@dataclass
class Table:
    """从已存批次读出的月度 × 末级机构表（精确整数）。"""
    run_id: str
    cells: dict[tuple[str, str], Cell]          # 只含确有份额记录的格子
    row_totals: dict[str, int]
    col_totals: dict[str, int]
    months: list[str]
    orgs: list[str]
    unresolved: dict                           # 单列的找不到归属事项（不入表）
    binding: dict                              # run/version/asof 等绑定信息

    @property
    def total(self) -> int:
        return sum(self.row_totals.values())

    def matrix(self) -> dict[tuple[str, str], int]:
        return {k: c.amount for k, c in self.cells.items()}


def _contributors(con, run_id: str) -> dict[tuple[str, str], set[str]]:
    """逐格去重研究者：同研究者多笔非零份额只算一人；零金额但有非零份额仍计入。

    只看该批次真正参与分摊（resolved）的事项；同一事项在若干末级单元有
    ``weight > 0`` 的记录，但金额可能因最大余数法为 0——那仍然是“有份额记录”。
    """
    people: dict[tuple[str, str], set[str]] = {}
    rows = con.execute(
        """
        SELECT strftime(i.day, '%Y-%m') AS month, i.person_key, l.terminal
        FROM alloc_run_item i
        JOIN alloc_run_leaf l
          ON l.run_id = i.run_id AND l.item_key = i.item_key
        WHERE i.run_id = ? AND i.resolved
        GROUP BY month, i.person_key, l.terminal
        HAVING COUNT(*) > 0
        ORDER BY month, i.person_key, l.terminal
        """, [run_id]).fetchall()
    for month, person, terminal in rows:
        people.setdefault((month, terminal), set()).add(person)
    return people


def build_table(con, run_id: str) -> Table:
    """从已保存的计算批次读取月度 × 末级机构表；不重算路由、不修改批次。"""
    meta = run_meta(con, run_id)
    rollup = monthly_rollup(con, run_id)               # 已发布口径：(月, 末级, 合计, 笔数)
    people = _contributors(con, run_id)

    cells: dict[tuple[str, str], Cell] = {}
    for month, org, total, _n in rollup:
        key = (str(month), str(org))
        n_people = len(people.get(key, ()))
        cells[key] = Cell(key[0], key[1], int(total), n_people)

    months = sorted({m for m, _o in cells})
    orgs = sorted({o for _m, o in cells})
    row_totals = {m: sum(c.amount for k, c in cells.items() if k[0] == m)
                  for m in months}
    col_totals = {o: sum(c.amount for k, c in cells.items() if k[1] == o)
                  for o in orgs}

    unresolved = unresolved_detail(con, run_id)        # 单列汇总，不入表
    binding = {
        "run_id": meta["run_id"],
        "version_id": meta["version_id"],
        "asof_at": meta["asof_at"].isoformat(),
        "kind": meta["kind"],
        "item_count": meta["item_count"],
        "resolved": meta["resolved"],
        "unresolved": meta["unresolved"],
    }
    return Table(meta["run_id"], cells, row_totals, col_totals,
                 months, orgs, unresolved, binding)


def table_digest(table: Table, hidden: set[tuple[str, str]] | None = None) -> str:
    payload = {
        "run_id": table.run_id,
        "version_id": table.binding["version_id"],
        "asof": table.binding["asof_at"],
        "row_totals": sorted(table.row_totals.items()),
        "col_totals": sorted(table.col_totals.items()),
        "cells": [[m, o, table.cells[(m, o)].amount,
                   table.cells[(m, o)].contributors]
                  for (m, o) in sorted(table.cells)],
    }
    if hidden is not None:
        payload["hidden"] = sorted([m, o] for m, o in hidden)
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return sha256(blob).hexdigest()


# ---------------------------------------------------------------- 图工具

def _graph_for(table: Table, hidden: set[tuple[str, str]]):
    """隐藏子图的二部图：返回 (节点名, 边列表, 邻接表)。行/列节点名带前缀区分。"""
    nodes = [f"R:{m}" for m in table.months] + [f"C:{o}" for o in table.orgs]
    edges = [(f"R:{m}", f"C:{o}", (m, o)) for (m, o) in sorted(hidden)]
    adj: dict[str, list[int]] = {n: [] for n in nodes}
    for i, (u, v, _k) in enumerate(edges):
        adj[u].append(i)
        adj[v].append(i)
    return nodes, edges, adj


def _bridges(edges: list[tuple[str, str, tuple[str, str]]]) -> set[int]:
    """Tarjan 桥检测（迭代，避免递归深度）。返回桥的边下标集合。

    只用隐藏边建图：可见格子已被自身的常数方程固定，行/列剩余和只约束
    隐藏边的带符号和。孤立节点（全可见的行/列）没有隐藏邻边，不构成桥。
    """
    nodes = sorted({x for u, v, _k in edges for x in (u, v)})
    idx = {n: i for i, n in enumerate(nodes)}
    adj: list[list[tuple[int, int]]] = [[] for _ in nodes]
    for i, (u, v, _k) in enumerate(edges):
        a, b = idx[u], idx[v]
        adj[a].append((b, i))
        adj[b].append((a, i))

    n = len(nodes)
    disc = [-1] * n
    low = [0] * n
    timer = 0
    out: set[int] = set()
    for s in range(n):
        if disc[s] != -1 or not adj[s]:
            continue
        disc[s] = low[s] = timer
        timer += 1
        stack = [(s, -1, 0)]             # (u, parent_edge, next index)
        while stack:
            u, pe, ni = stack[-1]
            if ni < len(adj[u]):
                v, e = adj[u][ni]
                stack[-1] = (u, pe, ni + 1)
                if e == pe:
                    continue
                if disc[v] == -1:
                    disc[v] = low[v] = timer
                    timer += 1
                    stack.append((v, e, 0))
                elif disc[v] < low[u]:
                    low[u] = disc[v]
                    stack[-1] = (u, pe, ni + 1)
            else:
                stack.pop()
                if stack:
                    p, ppe, _ = stack[-1]
                    if low[u] > disc[p]:
                        out.add(pe)
                    if low[u] < low[p]:
                        low[p] = low[u]
    return out


def protected(table: Table, hidden: set[tuple[str, str]]) -> bool:
    """整张表的保护条件：隐藏子图没有任何桥（每条隐藏边都在某个环上）。"""
    _nodes, edges, _adj = _graph_for(table, hidden)
    return not _bridges(edges)


def hidden_witnesses(table: Table, hidden: set[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    """对每条隐藏边给出一个可机检的非零整数扰动见证。

    见证 = 该边所在的一个环：环上交替符号 +1/-1 的赋值使每个行/列节点的
    带符号边权和保持为 0，所以行合计、列合计与可见格子全部不变，而目标
    隐藏边的扰动为 +1（非零）。孤立的隐藏边（桥）没有环，返回空 dict，
    调用方应判定不安全。
    """
    _nodes, edges, adj = _graph_for(table, hidden)
    # 邻接：节点 -> [(另一端点, 边下标)]
    edge_index = {k: i for i, (_u, _v, k) in enumerate(edges)}
    nb: dict[str, list[tuple[str, int]]] = {n: [] for n in _nodes}
    for i, (u, v, _k) in enumerate(edges):
        nb[u].append((v, i))
        nb[v].append((u, i))
    node_of = {k: i for i, (_u, _v, k) in enumerate(edges)}

    # 对每条边找它所在的环（去掉该边后两端是否仍连通）
    witnesses: dict[tuple[str, str], dict] = {}
    for target_i, (u, v, key) in enumerate(edges):
        parent: dict[str, str | None] = {u: None}
        parent_edge: dict[str, int] = {}
        queue = [u]
        found = None
        while queue and found is None:
            x = queue.pop(0)
            for y, ei in nb[x]:
                if ei == target_i:
                    continue
                if y not in parent:
                    parent[y] = x
                    parent_edge[y] = ei
                    if y == v:
                        found = v
                        break
                    queue.append(y)
        if found is None:
            continue                        # 桥：没有环 -> 无见证
        # 回退得到 u..v 的路径（不含目标边）
        path_nodes = []
        cur = v
        while cur is not None:
            path_nodes.append(cur)
            cur = parent[cur]
        path_nodes.reverse()               # u -> ... -> v
        path_edges = [parent_edge[n] for n in path_nodes[1:]]
        cyc = path_edges + [target_i]      # 闭合回 u
        # 交替符号：从目标边 +1 开始，环上依次 -1, +1, ...
        signs = {}
        for pos, ei in enumerate(cyc):
            signs[ei] = 1 if pos % 2 == 0 else -1
        # 规约：把“节点带符号和 = 0”逐节点核对
        balances: dict[str, int] = {}
        for ei, s in signs.items():
            uu, vv, _kk = edges[ei]
            balances[uu] = balances.get(uu, 0) + s
            balances[vv] = balances.get(vv, 0) - s
        assert all(x == 0 for x in balances.values()), balances
        hidden_keys = sorted(edges[ei][2] for ei in cyc)
        witnesses[key] = {
            "target": list(key),
            "delta": 1,
            "cycle_cells": [[m, o] for m, o in hidden_keys],
            "assignments": [[edges[ei][2][0], edges[ei][2][1], signs[ei]]
                            for ei in cyc],
            "balance_ok": True,
        }
    return witnesses


# ---------------------------------------------------------------- 精确求解

@dataclass
class SearchResult:
    """一次抑制求解的完整结论。"""
    status: str                           # "optimal" / "incomplete"
    reason: str = ""
    min_contributors: int = 1
    primary: list[tuple[str, str]] = field(default_factory=list)
    secondary: list[tuple[str, str]] = field(default_factory=list)
    cost: int = 0
    extra_count: int = 0
    candidates: list[tuple[str, str]] = field(default_factory=list)
    candidates_considered: int = 0
    max_candidates: int = 0
    feasible_masks: int = 0
    nodes_searched: int = 0

    @property
    def hidden(self) -> list[tuple[str, str]]:
        return sorted([*self.primary, *self.secondary])

    @property
    def verified(self) -> bool:
        return self.status == "optimal"

    @property
    def complete(self) -> bool:
        """是否已完成求解（incomplete 表示超出搜索上限）。"""
        return self.status == "optimal"

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "reason": self.reason,
            "min_contributors": self.min_contributors,
            "primary": [list(k) for k in self.primary],
            "secondary": [list(k) for k in self.secondary],
            "hidden": [list(k) for k in self.hidden],
            "cost": self.cost,
            "extra_count": self.extra_count,
            "candidates": [list(k) for k in self.candidates],
            "candidates_considered": self.candidates_considered,
            "max_candidates": self.max_candidates,
            "feasible_masks": self.feasible_masks,
            "nodes_searched": self.nodes_searched,
        }


def _lex_key(keys) -> tuple:
    return tuple(sorted(keys))


def solve_suppression(table: Table, policy: Policy) -> SearchResult:
    """精确求解主/次抑制。

    1. 主抑制 = 贡献人数 < k 的格子。若策略强制公开其中任一格 -> 不可发布。
    2. 候选 = 所有非主抑制的现存格子；``must_publish`` 的格子从候选中剔除
       （次抑制就是要隐藏它，两者对同一格互相矛盾）。
    3. 在候选构成的 2^n 空间里做最优优先搜索：优先代价最小，代价相同优先
       数量最少，再按排序后的格子列表字典序最小。
    4. 只有“主抑制 ∪ 选中候选”满足保护的掩码才是可行解。
    5. 无可行掩码 -> 不可发布（reason=no_solution），不生成报表。

    最优性论证：优先级键 (成本, 数量, 排序列表) 对“加入一个候选格”严格单调
    （成本与数量都不减，数量必 +1），所以父掩码的键严格小于任何子掩码；
    最优优先弹出的第一个可行掩码即全局最优。键在数量相同的候选集合上唯一
    （列表就是集合本身），因此结果确定，不依赖堆内同键顺序。
    """
    primary = sorted(k for k, c in table.cells.items()
                     if c.contributors < policy.min_contributors)
    forced = [k for k in primary if k in policy.must_publish]
    if forced:
        raise NotPublishable(
            "必须公开的格子中存在主抑制格子（贡献人数 < k）",
            {"forced_public_primary": [list(k) for k in sorted(forced)],
             "min_contributors": policy.min_contributors})

    primary_set = set(primary)
    candidates = sorted(k for k in table.cells
                        if k not in primary_set and k not in policy.must_publish)
    n = len(candidates)

    if n > policy.max_candidates:
        return SearchResult(
            status="incomplete",
            reason=(f"候选数 {n} 超过显式搜索上限 {policy.max_candidates}；"
                    f"未完成求解，不宣称最优"),
            min_contributors=policy.min_contributors,
            primary=[tuple(k) for k in primary],
            candidates=candidates, candidates_considered=n,
            max_candidates=policy.max_candidates)

    # 预计算逐字节查表：键的第三项直接用“选中候选的下标元组”，因为候选已按
    # (月份, 机构) 升序排列，下标元组的字典序就是排序后格子列表的字典序。
    costs = [policy.cost_for(*c) for c in candidates]
    nbytes = (n + 7) // 8
    idx_tab = [[tuple(i for i in range(b * 8, min(n, (b + 1) * 8))
                      if v >> (i - b * 8) & 1) for v in range(256)]
               for b in range(nbytes)]
    cost_tab = [[sum(costs[b * 8 + j] for j in range(min(8, n - b * 8))
                     if v >> j & 1) for v in range(256)]
                for b in range(nbytes)]

    def key_of(mask: int) -> tuple:
        idx: tuple = ()
        cost = 0
        m = mask
        for b in range(nbytes):
            byte = m & 255
            idx += idx_tab[b][byte]
            cost += cost_tab[b][byte]
            m >>= 8
        return (cost, mask.bit_count(), idx)

    def is_feasible(mask: int) -> bool:
        hidden = primary_set | {candidates[i] for i in range(n) if mask >> i & 1}
        return protected(table, hidden)

    heap: list[tuple[tuple, int]] = [(key_of(0), 0)]
    seen = {0}
    feasible_masks = 0
    nodes = 0
    while heap:
        key, mask = heapq.heappop(heap)
        nodes += 1
        if is_feasible(mask):
            feasible_masks += 1
            sel = [candidates[i] for i in range(n) if mask >> i & 1]
            return SearchResult(
                status="optimal", min_contributors=policy.min_contributors,
                primary=[tuple(k) for k in primary],
                secondary=[tuple(k) for k in sel],
                cost=key[0], extra_count=key[1], candidates=candidates,
                candidates_considered=n, max_candidates=policy.max_candidates,
                feasible_masks=feasible_masks, nodes_searched=nodes)
        for i in range(n):
            if mask >> i & 1:
                continue
            nm = mask | (1 << i)
            if nm in seen:
                continue
            seen.add(nm)
            heapq.heappush(heap, (key_of(nm), nm))

    raise NotPublishable(
        "没有满足保护条件的次抑制方案（无法解除线性反推）",
        {"primary": [list(k) for k in primary],
         "candidates": [list(k) for k in candidates],
         "min_contributors": policy.min_contributors,
         "feasible_masks": 0,
         "nodes_searched": nodes})


# ---------------------------------------------------------------- 审计与公开导出

def _policy_effective(policy: Policy | None) -> Policy:
    return policy if policy is not None else Policy()


def build_audit(table: Table, policy: Policy, search: SearchResult) -> dict:
    """内部审计材料：输入绑定、策略摘要、主/次抑制集合、目标值与保护见证。

    这里可以含隐藏金额与人数（内部用）；公开导出物不得含这些字段。
    """
    if search.status != "optimal":
        raise NotPublishable(
            f"求解未完成或不可发布（status={search.status}），拒绝生成审计报表",
            {"status": search.status, "reason": search.reason})

    hidden = set((m, o) for m, o in search.hidden)
    witnesses = hidden_witnesses(table, hidden)
    if set(witnesses) != hidden:
        missing = sorted([list(k) for k in hidden - set(witnesses)])
        raise NotPublishable("部分隐藏格子没有环扰动见证（存在桥）",
                             {"missing_witnesses": missing})

    return {
        "format": AUDIT_FORMAT,
        "binding": dict(table.binding),
        "table_digest": table_digest(table, hidden),
        "policy": {"digest": policy.digest(), **policy.as_dict()},
        "search": search.as_dict(),
        "objective": {
            "cost": search.cost,
            "extra_hidden_count": search.extra_count,
            "tiebreak": "sorted(month, org) list ascending",
        },
        "totals": {
            "row": sorted(table.row_totals.items()),
            "col": sorted(table.col_totals.items()),
            "total": table.total,
        },
        "ledger": [
            {"month": m, "org": o, "amount": c.amount,
             "contributors": c.contributors,
             "primary": (m, o) in set(search.primary),
             "hidden": (m, o) in hidden}
            for (m, o), c in sorted(table.cells.items())
        ],
        "primary": [list(k) for k in search.primary],
        "secondary": [list(k) for k in search.secondary],
        "hidden": [list(k) for k in sorted(hidden)],
        "witnesses": [witnesses[k] for k in sorted(witnesses)],
        "unresolved": {
            "count": table.unresolved["count"],
            "amount": table.unresolved["amount"],
        },
    }


def export_public(table: Table, policy: Policy, search: SearchResult) -> dict:
    """公开导出物：只含可见金额、隐藏占位与行列合计。

    不含隐藏金额、贡献人数、研究者标识、行/列以外的批次绑定或任何可还原
    的内部明细。``report_id`` 是公开内容自身的摘要，用于自校验；它不暴露
    任何批次内部字段。
    """
    if search.status != "optimal":
        raise NotPublishable(
            f"求解未完成或不可发布（status={search.status}），拒绝导出公开报表",
            {"status": search.status, "reason": search.reason})
    hidden = set((m, o) for m, o in search.hidden)
    if not protected(table, hidden):
        raise NotPublishable("抑制集合不满足保护条件，拒绝导出", {})
    for k in policy.must_publish:
        if k in hidden:
            raise NotPublishable(f"必须公开的格子被抑制: {k}", {})

    rows = []
    for m in table.months:
        cells = {}
        for o in table.orgs:
            key = (m, o)
            if key not in table.cells:
                continue                       # 结构零：不出现（公开已知为 0）
            if key in hidden:
                cells[o] = "hidden"            # 占位符，不带金额
            else:
                cells[o] = {"amount": table.cells[key].amount}
        rows.append({"month": m, "row_total": table.row_totals[m], "cells": cells})
    payload = {
        "format": PUBLIC_FORMAT,
        "rows": rows,
        "col_totals": {o: table.col_totals[o] for o in table.orgs},
    }
    payload["report_id"] = public_payload_digest(payload)
    return payload


def public_payload_digest(payload: dict) -> str:
    """公开物自校验摘要：只覆盖公开内容（不含 report_id 自身）。"""
    body = {k: payload[k] for k in ("format", "rows", "col_totals")}
    blob = json.dumps(body, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return sha256(blob).hexdigest()


# ---------------------------------------------------------------- 策略自检

def validate_policy(table: Table, policy: Policy) -> list[str]:
    """策略层面的问题（不涉及求解）：返回人类可读问题列表，空 = 通过。"""
    problems: list[str] = []
    unknown = sorted(k for k in policy.must_publish if k not in table.cells)
    if unknown:
        # 强制公开一个结构零（没有份额记录）没有意义，通常意味着拼写错误
        problems.append("must_publish 指向不存在的格子: "
                        + ", ".join(f"{m}/{o}" for m, o in unknown))
    unknown_cost = sorted(k for k in policy.costs if k not in table.cells)
    if unknown_cost:
        problems.append("成本表指向不存在的格子: "
                        + ", ".join(f"{m}/{o}" for m, o in unknown_cost))
    return problems


# ---------------------------------------------------------------- 服务层便捷入口

def plan(con, run_id: str, policy: Policy | None = None):
    """从已保存批次读取表并求解抑制；返回 (table, search)。不改动任何批次数据。"""
    policy = _policy_effective(policy)
    table = build_table(con, run_id)
    problems = validate_policy(table, policy)
    if problems:
        raise NotPublishable("策略无效: " + "; ".join(problems),
                             {"policy_digest": policy.digest()})
    return table, solve_suppression(table, policy)


def publish(con, run_id: str, policy: Policy | None = None) -> tuple[dict, dict]:
    """一次产出 (公开文件, 内部审计文件)。

    公开文件只含可见金额、隐藏占位与行列合计；审计文件含输入绑定、策略摘要、
    主/次抑制集合、目标值与逐格扰动见证。两个出口分别导出，互不混用。
    """
    policy = _policy_effective(policy)
    table, search = plan(con, run_id, policy)
    return export_public(table, policy, search), build_audit(table, policy, search)


def write_json_report(path, payload: dict) -> None:
    """把报表写成 UTF-8 JSON（键排序、紧凑分隔符，便于比对摘要）。"""
    import pathlib
    pathlib.Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")), encoding="utf-8")
