"""依赖图：引用/环校验与按运行秒数加权的最长依赖路径。

权重 = 一个逻辑 job 全部 attempt 的运行秒数之和。等权路径按完整
job_id 序列的 UTF-8 字节序决胜。未终结/invalid 节点不得混入完整路径，
依赖它们的结果标注不完整。
"""
from __future__ import annotations

from dataclasses import dataclass

# 可参与完整最长路径的 job 状态
COMPLETE_STATUSES = frozenset({"succeeded", "failed"})


@dataclass
class GraphResult:
    children: dict[str, list[str]]
    parents: dict[str, list[str]]
    nodes: set[str]
    cycle_nodes: set[str]
    tainted_nodes: set[str]          # 环上节点 + 其全部下游
    dangling: list[tuple[str, str, str]]   # (parent, child, 哪边未知)
    duplicate_edges: list[tuple[str, str]]
    dep_issues: list[tuple[str, str, list[str]]]


def build_graph(edges: list, known_jobs: set[str]) -> GraphResult:
    """edges: DepEdge 列表；known_jobs: 事件流中出现过的 job 集合。"""
    children: dict[str, list[str]] = {}
    parents: dict[str, list[str]] = {}
    nodes: set[str] = set()
    seen: set[tuple[str, str]] = set()
    dupes: list[tuple[str, str]] = []
    dangling: list[tuple[str, str, str]] = []
    issues: list[tuple[str, str, list[str]]] = []

    for e in edges:
        key = (e.parent_job, e.child_job)
        if key in seen:
            dupes.append(key)
            issues.append(("duplicate_dependency",
                           f"依赖 {e.parent_job} -> {e.child_job} 重复声明"
                           f"（第 {e.source_line} 行）",
                           [e.parent_job, e.child_job]))
            continue
        seen.add(key)
        nodes.add(e.parent_job)
        nodes.add(e.child_job)
        if e.parent_job not in known_jobs:
            dangling.append((e.parent_job, e.child_job, "parent_job"))
            issues.append(("unknown_dependency_job",
                           f"第 {e.source_line} 行 parent_job={e.parent_job} "
                           "未在事件中出现",
                           [e.parent_job, e.child_job]))
        if e.child_job not in known_jobs:
            dangling.append((e.parent_job, e.child_job, "child_job"))
            issues.append(("unknown_dependency_job",
                           f"第 {e.source_line} 行 child_job={e.child_job} "
                           "未在事件中出现",
                           [e.parent_job, e.child_job]))
        children.setdefault(e.parent_job, []).append(e.child_job)
        parents.setdefault(e.child_job, []).append(e.parent_job)
        # 保证只作为父/只作为子出现的节点也有完整的邻接表，
        # 否则 Kahn 入度会漏算（J_C3 曾因此被误判进环）
        parents.setdefault(e.parent_job, [])
        children.setdefault(e.child_job, [])

    # 环检测：三色 DFS 精确标记环节点（回边指向的栈内节点到栈顶）。
    # 注意 Kahn 的「未剥离集合」会把环的纯下游也算进去，无法区分
    # 真正在环上的节点与被污染的下游，故这里不用 Kahn 求环节点。
    color: dict[str, int] = {n: 0 for n in nodes}
    cycle_nodes: set[str] = set()

    def _dfs_cycle(n: str, stack: list[str]) -> None:
        color[n] = 1
        for c in sorted(children.get(n, [])):
            if color[c] == 1:
                k = stack.index(c)
                cycle_nodes.update(stack[k:])
            elif color[c] == 0:
                _dfs_cycle(c, stack + [c])
        color[n] = 2

    for n in sorted(nodes):
        if color[n] == 0:
            _dfs_cycle(n, [n])
    tainted = set(cycle_nodes)
    # 环的所有下游（不含环节点之外的纯下游误判：只沿出边扩散）
    stack = sorted(cycle_nodes)
    while stack:
        n = stack.pop()
        for c in children.get(n, []):
            if c not in tainted:
                tainted.add(c)
                stack.append(c)
    # 环上节点统一一条 issue（下游污染只体现在路径完整性，不混入环节点）
    if cycle_nodes:
        issues.append(("dependency_cycle",
                       "依赖图存在环，环上节点及其下游不参与完整路径计算",
                       sorted(cycle_nodes)))
    return GraphResult(
        children=children, parents=parents, nodes=nodes,
        cycle_nodes=cycle_nodes, tainted_nodes=tainted,
        dangling=dangling, duplicate_edges=dupes, dep_issues=issues)


@dataclass
class PathResult:
    rank: int
    job_ids: list[str]
    weights: list[float]
    total: float
    complete: bool


def longest_paths(weights: dict[str, float], statuses: dict[str, str],
                  graph: GraphResult, top_k: int = 20) -> list[PathResult]:
    """在可计算节点上做加权最长路径 DP，等权按 job_id 序列 UTF-8 决胜。

    weights: job_id -> 全部 attempt 运行秒数之和（无运行片段的为 0）。
    完整路径只允许 succeeded/failed 且不在 tainted 集合内的节点；
    含 queued/running/incomplete/invalid/环污染节点的路径单独以
    complete=0 给出最长线索。
    """
    usable = {n for n in graph.nodes
              if n in weights and n not in graph.tainted_nodes}
    # 只保留 usable 节点诱导子图的边（DAG，环上节点已被剔除）
    children = {n: [c for c in graph.children.get(n, []) if c in usable]
                for n in usable}
    parents = {n: [p for p in graph.parents.get(n, []) if p in usable]
               for n in usable}

    # 拓扑序（Kahn，同序按 UTF-8 字节序，保证可复现）
    indeg = {n: len(parents[n]) for n in usable}
    queue = sorted((n for n, d in indeg.items() if d == 0),
                   key=lambda x: x.encode("utf-8"))
    topo: list[str] = []
    while queue:
        n = queue.pop(0)
        topo.append(n)
        for c in sorted(children[n], key=lambda x: x.encode("utf-8")):
            indeg[c] -= 1
            if indeg[c] == 0:
                queue.append(c)
        queue.sort(key=lambda x: x.encode("utf-8"))

    def best_key(total: float, seq: list[str]):
        # 越大越优：total 降序；等权时完整字节序列越小越优
        return (-total, tuple(x.encode("utf-8") for x in seq))

    def _keep_tied(choices: list[tuple[float, list[str]]]):
        """保留全部并列最优路径（同 total 的不同 job_id 序列都要留下）。"""
        if not choices:
            return []
        choices.sort(key=lambda z: best_key(z[0], z[1]))
        best_total = choices[0][0]
        return [c for c in choices if c[0] == best_total]

    # 以每个节点为终点的全部并列最优路径（完整 / 不完整分开维护）
    best_complete: dict[str, list[tuple[float, list[str]]]] = {}
    best_incomplete: dict[str, list[tuple[float, list[str]]]] = {}
    for n in topo:
        w = weights[n]
        node_complete = statuses.get(n) in COMPLETE_STATUSES
        cand_c: list[tuple[float, list[str]]] = []
        cand_i: list[tuple[float, list[str]]] = []
        for p in parents[n]:
            cand_c.extend((t, seq) for t, seq in best_complete.get(p, []))
            cand_i.extend((t, seq) for t, seq in best_incomplete.get(p, []))
        solo = (w, [n])
        if node_complete:
            choices_c = [(t + w, seq + [n]) for t, seq in cand_c] + [solo]
            best_complete[n] = _keep_tied(choices_c)
            if cand_i:
                choices_i = [(t + w, seq + [n]) for t, seq in cand_i]
                best_incomplete[n] = _keep_tied(choices_i)
        else:
            # 自身不完整：任何经过它的路径都只能进不完整集合
            choices_i = [(t + w, seq + [n]) for t, seq in cand_c] \
                + [(t + w, seq + [n]) for t, seq in cand_i] + [solo]
            best_incomplete[n] = _keep_tied(choices_i)

    results: list[PathResult] = []
    # 汇总全部节点的并列最优路径，按 (总权重降序, 完整 job_id 字节序列
    # 升序) 取前 top_k 条不同路径；完整集合排在不完整集合之前
    pool_c = [c for lst in best_complete.values() for c in lst]
    pool_i = [c for lst in best_incomplete.values() for c in lst]
    pool_c.sort(key=lambda z: best_key(z[0], z[1]))
    pool_i.sort(key=lambda z: best_key(z[0], z[1]))
    rank = 0
    seen_seq: set[tuple[str, ...]] = set()
    complete_seqs = {tuple(seq) for _t, seq in pool_c}
    for t, seq in pool_c + pool_i:
        key = tuple(seq)
        if key in seen_seq:
            continue
        seen_seq.add(key)
        is_complete = key in complete_seqs
        rank += 1
        results.append(PathResult(
            rank, seq, [round(weights[x], 9) for x in seq], round(t, 9),
            is_complete))
        if rank >= top_k:
            break
    return results
