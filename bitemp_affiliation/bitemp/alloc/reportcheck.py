"""独立复核器：不依赖求解器实现，重新建立整张表的线性系统并核验抑制结果。

两类复核
--------
1. :func:`check_free_columns` —— 用有理数消元（``fractions.Fraction``）对
   “隐藏金额为未知量”的齐次系统求每个未知量的自由度。构造方法与
   :mod:`protection` 里的圈空间判据完全独立：
   * 变量 = 每个隐藏格子；
   * 方程 = 每个行节点、每个列节点的带符号边权和为 0（把可见格子的固定
     金额代入行/列合计后剩下的条件）；
   * 对系数矩阵做一次有理数 Gauss-Jordan 行最简化。变量 j 被唯一确定
     ⇔ ``A d = 0 ⇒ d_j = 0`` ⇔ 单位行向量 e_j 在 A 的行空间 ⇔ e_j 所在
     主元行在其余所有列上为零。反之为自由变量（可被环扰动改变）。
2. :func:`enumerate_small` —— 对候选不超过 ``max_candidates`` 的小表，
   枚举全部 2^n 个额外隐藏集合，独立核对：
   * 主抑制判定；
   * 每个掩码的保护性（用复核器自带的独立桥检测）；最优解再用上面的消元确认；
   * 成本与决胜次序（成本 → 数量 → 排序后列表字典序）；
   * 与求解器给出的最优解一致。

复核器只从公开报表 + 审计材料的“声明集合”重建系统；它不调用
:func:`protection.solve_suppression` 或 :func:`protection.protected`。
"""
from __future__ import annotations

from fractions import Fraction

from .protection import PUBLIC_FORMAT, NotPublishable, Table, public_payload_digest


# ---------------------------------------------------------------- 有理数消元

def residual_matrix(table: Table, hidden: set[tuple[str, str]]):
    """返回 (变量名, A, 说明)：A 是行/列的带符号关联矩阵（每行对应一个节点）。

    变量 = 隐藏格子；每行系数：格子 (m,o) 对行 m 取 +1、对列 o 取 -1。
    A x = 0 的解空间就是保持所有行合计与列合计不变的整数扰动空间维度。
    """
    keys = sorted(hidden)
    var_index = {k: i for i, k in enumerate(keys)}
    rows = []
    labels = []
    for m in table.months:
        row = [0] * len(keys)
        for o in table.orgs:
            k = (m, o)
            if k in var_index:
                row[var_index[k]] = 1
        rows.append(row)
        labels.append(("row", m))
    for o in table.orgs:
        row = [0] * len(keys)
        for m in table.months:
            k = (m, o)
            if k in var_index:
                row[var_index[k]] = -1
        rows.append(row)
        labels.append(("col", o))
    return keys, rows, labels


def check_free_columns(table: Table, hidden: set[tuple[str, str]]) -> dict:
    """检查每个未知量是否真有自由度（有自由度的量不能被唯一反推出来）。

    做法：齐次系统 A d = 0（行合计零、列合计零）。变量 j 有自由度 ⇔ 存在
    齐次解 d 使 d_j ≠ 0 ⇔ 线性泛函 d ↦ d_j 在 A 的行空间里不恒为零 ⇔
    单位行向量 e_j 不在 A 的行空间 ⇔ rank([A; e_j]) > rank(A)。
    这里对 A 与追加 e_j 后的矩阵各做一次有理数 Gauss-Jordan 求秩，全程
    ``Fraction``，不用浮点。

    一个关键性质：A 是二部图的带符号关联矩阵，其零空间由环生成；某隐藏格
    有自由度 ⇔ 它不是桥。所以本判据与“每条隐藏边都在某个环上”等价，
    但与求解器的桥检测实现完全独立。
    """
    keys, rows, _labels = residual_matrix(table, hidden)
    n = len(keys)
    if n == 0:
        return {"variables": [], "free": [], "pinned": [], "rank": 0,
                "nullity": 0, "detail": {}}

    A = [[Fraction(rows[i][j]) for j in range(n)] for i in range(len(rows))]

    def rref(mat: list[list[Fraction]]):
        """有理数 Gauss-Jordan；返回 (秩, 行最简矩阵, 主元列列表)。"""
        m = len(mat)
        k = len(mat[0]) if m else 0
        work = [list(row) for row in mat]
        pivots: list[int] = []
        r = 0
        for c in range(k):
            if r == m:
                break
            piv = None
            for i in range(r, m):
                if work[i][c] != 0:
                    piv = i
                    break
            if piv is None:
                continue
            work[r], work[piv] = work[piv], work[r]
            pv = work[r][c]
            work[r] = [v / pv for v in work[r]]
            for i in range(m):
                if i != r and work[i][c] != 0:
                    f = work[i][c]
                    work[i] = [a - f * b for a, b in zip(work[i], work[r])]
            pivots.append(c)
            r += 1
        return r, work, pivots

    base_rank, reduced, pivots = rref(A)
    pivot_row = {col: i for i, col in enumerate(pivots)}
    detail = {}
    free, pinned = [], []
    for j, k in enumerate(keys):
        # e_j ∈ rowspace(A) ⇔ rank([A; e_j]) == rank(A) ⇔ 变量 j 被唯一确定。
        # 用行最简形：非主元列的 e_j 必不在行空间；主元列 j 的 e_j 在行空间
        # 当且仅当对应主元行恰为 e_j（该行在所有非主元列上为 0）。
        is_pinned = False
        if j in pivot_row:
            row = reduced[pivot_row[j]]
            is_pinned = all(v == 0 for c, v in enumerate(row) if c != j)
        detail[f"{k[0]}|{k[1]}"] = is_pinned
        (pinned if is_pinned else free).append([k[0], k[1]])
    return {
        "variables": [[k[0], k[1]] for k in keys],
        "free": free,
        "pinned": pinned,
        "rank": base_rank,
        "nullity": n - base_rank,
        "detail": detail,
    }


def is_protected_by_algebra(table: Table, hidden: set[tuple[str, str]]) -> bool:
    """独立判据：每个隐藏变量都有自由度 ⇔ 每条隐藏边都在某个环上。

    空隐藏集是平凡安全的（没有任何未知量要保护）。
    """
    if not hidden:
        return True
    report = check_free_columns(table, hidden)
    return not report["pinned"]


# ---------------------------------------------------------------- 公开物核对

_FORBIDDEN_PUBLIC_KEYS = {"contributors", "person_key", "item_key", "amount_hidden",
                          "hidden_amount", "contributor_count", "researcher_id",
                          "researcher_ids"}


def check_public_payload(payload: dict, table: Table, expected_hidden: set) -> list[str]:
    """核对公开物：结构完整、合计精确、无内部明细、占位与声明一致。"""
    problems: list[str] = []
    if payload.get("format") != PUBLIC_FORMAT:
        problems.append(f"format 应为 {PUBLIC_FORMAT}")
    # 递归查禁用字段
    def scan(obj, path=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in _FORBIDDEN_PUBLIC_KEYS:
                    problems.append(f"公开物含内部字段 {path}/{k}")
                scan(v, f"{path}/{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                scan(v, f"{path}[{i}]")
    scan(payload)
    # 公开物只允许这些顶层字段（绑定/批次信息只留在审计里）
    allowed = {"format", "rows", "col_totals", "report_id"}
    extra = set(payload) - allowed
    if extra:
        problems.append(f"公开物含不允许的顶层字段: {sorted(extra)}")
    # 自校验摘要
    if "report_id" in payload:
        body = {k: payload[k] for k in ("format", "rows", "col_totals")}
        if payload["report_id"] != public_payload_digest(payload):
            problems.append("report_id 自校验摘要不匹配")

    hidden = set()
    for row in payload.get("rows", []):
        cells = row.get("cells", {})
        for org, val in cells.items():
            if val == "hidden":
                hidden.add((row["month"], org))
            elif not (isinstance(val, dict) and set(val) == {"amount"}):
                problems.append(f"格子 {row['month']}/{org} 不是金额或 hidden 占位")
        true_sum = sum(table.cells[(row["month"], o)].amount
                       for o in cells if (row["month"], o) in table.cells)
        if true_sum != row["row_total"]:
            problems.append(f"行 {row['month']} 的真实合计与行合计不一致")
    if hidden != expected_hidden:
        problems.append("公开隐藏占位与审计声明的隐藏集合不一致")
    for (m, o) in hidden:
        if (m, o) not in table.cells:
            problems.append(f"隐藏占位 {m}/{o} 指向不存在的格子")
    # 列合计精确
    if set(payload.get("col_totals", {})) != set(table.orgs):
        problems.append("列合计的机构集合不完整")
    for org, total in payload.get("col_totals", {}).items():
        if total != table.col_totals.get(org):
            problems.append(f"列 {org} 的合计不正确")
    return problems


# ---------------------------------------------------------------- 小表全枚举

def _independent_has_bridge(months: list[str], orgs: list[str],
                            hidden: set[tuple[str, str]]) -> bool:
    """复核器自带的桥检测（迭代 Tarjan），不引用求解器实现。

    只对隐藏边建图：出现过的行、列节点加上隐藏格边。返回是否存在桥
    （存在桥 ⇔ 某个隐藏格可以被行/列合计唯一反推）。
    """
    node_ids = {}
    for m in months:
        if any(k[0] == m for k in hidden):
            node_ids[("R", m)] = len(node_ids)
    for o in orgs:
        if any(k[1] == o for k in hidden):
            node_ids[("C", o)] = len(node_ids)
    edges = []
    for (m, o) in hidden:
        edges.append((node_ids[("R", m)], node_ids[("C", o)]))
    n = len(node_ids)
    adj: list[list[tuple[int, int]]] = [[] for _ in range(n)]
    for i, (u, v) in enumerate(edges):
        adj[u].append((v, i))
        adj[v].append((u, i))
    disc = [-1] * n
    low = [0] * n
    timer = 0
    for s in range(n):
        if disc[s] != -1 or not adj[s]:
            continue
        disc[s] = low[s] = timer
        timer += 1
        stack = [(s, -1, 0)]
        while stack:
            u, pe, ni = stack[-1]
            if ni < len(adj[u]):
                v, ei = adj[u][ni]
                stack[-1] = (u, pe, ni + 1)
                if ei == pe:
                    continue
                if disc[v] == -1:
                    disc[v] = low[v] = timer
                    timer += 1
                    stack.append((v, ei, 0))
                elif disc[v] < low[u]:
                    low[u] = disc[v]
            else:
                stack.pop()
                if stack:
                    p, ppe, _ = stack[-1]
                    if low[u] > disc[p]:
                        return True            # (p,u) 是桥
                    if low[u] < low[p]:
                        low[p] = low[u]
    return False


def enumerate_small(table: Table, policy, *, max_candidates: int | None = None) -> dict:
    """枚举全部合法额外隐藏集合，核对成本与决胜次序，并给出精确最优。

    * 每个掩码的保护性用复核器自带的独立桥检测（不依赖求解器）；
    * 对最终选出的最优解再做一次有理数消元，确认每个隐藏量都有自由度；
    * 结果与求解器的最优解应满足 (成本, 数量, 排序列表) 完全相同。

    复杂度 O(2^n · (V+E))；候选超过上限时返回不可枚举。
    """
    from .protection import Policy, _lex_key  # 只借数据类与排序键，不用求解逻辑
    policy = policy if policy is not None else Policy()

    primary = sorted(k for k, c in table.cells.items()
                     if c.contributors < policy.min_contributors)
    primary_set = set(primary)
    forced = [k for k in primary if k in policy.must_publish]
    if forced:
        return {"publishable": False,
                "reason": "must_publish 含主抑制格子",
                "forced": [list(k) for k in forced]}
    candidates = sorted(k for k in table.cells
                        if k not in primary_set and k not in policy.must_publish)
    n = len(candidates)
    limit = policy.max_candidates if max_candidates is None else max_candidates
    if n > limit:
        return {"publishable": False, "reason": "候选数超过枚举上限",
                "candidate_count": n, "limit": limit}
    if not primary and n == 0:
        return {"publishable": False, "reason": "空表", "candidate_count": 0}

    costs = [policy.cost_for(*c) for c in candidates]
    best: tuple | None = None
    best_mask: int | None = None
    feasible_count = 0
    first_three: list[dict] = []
    for mask in range(1 << n):
        sel = tuple(candidates[i] for i in range(n) if mask >> i & 1)
        hidden = primary_set | set(sel)
        if _independent_has_bridge(table.months, table.orgs, hidden):
            continue
        feasible_count += 1
        cost = sum(costs[i] for i in range(n) if mask >> i & 1)
        key = (cost, len(sel), _lex_key(sel))
        if len(first_three) < 3:
            first_three.append({"mask": mask, "secondary": [list(k) for k in sel],
                                "cost": cost, "count": len(sel), "key": key})
        if best is None or key < best:
            best, best_mask = key, mask

    if best is None:
        return {"publishable": False,
                "reason": "没有满足保护条件的方案",
                "candidate_count": n, "enumerated": 1 << n, "feasible_count": 0}
    sel = tuple(candidates[i] for i in range(n) if best_mask >> i & 1)
    hidden = primary_set | set(sel)
    algebra = check_free_columns(table, hidden)       # 最优解再由消元确认一次
    return {
        "publishable": True,
        "primary": [list(k) for k in primary],
        "secondary": [list(k) for k in sel],
        "hidden": [list(k) for k in sorted(hidden)],
        "cost": best[0], "count": best[1], "key": best[2],
        "candidate_count": n,
        "enumerated": 1 << n,
        "feasible_count": feasible_count,
        "algebra_pinned": algebra["pinned"],
        "first_three": first_three,
    }


# ---------------------------------------------------------------- 见证核对

def check_witnesses(table: Table, hidden: set[tuple[str, str]],
                    witnesses: list[dict]) -> list[str]:
    """逐条机检扰动见证：环上单元格全隐藏、节点带符号和为零、目标扰动非零。"""
    from .protection import _graph_for
    problems: list[str] = []
    _nodes, edges, _adj = _graph_for(table, hidden)
    by_key = {k: (u, v) for u, v, k in edges}
    seen_targets = set()
    for w in witnesses:
        target = tuple(w.get("target", ()))
        if target not in hidden:
            problems.append(f"见证目标不在隐藏集合: {target}")
            continue
        if target in seen_targets:
            problems.append(f"同一隐藏格重复见证: {target}")
        seen_targets.add(target)
        assigns = w.get("assignments", [])
        if not assigns:
            problems.append(f"见证无赋值: {target}")
            continue
        balances: dict[str, int] = {}
        for m, o, delta in assigns:
            key = (m, o)
            if key not in hidden:
                problems.append(f"见证扰动触及可见格: {m}/{o}")
                continue
            if key not in by_key:
                problems.append(f"见证格子不在隐藏子图: {m}/{o}")
                continue
            if isinstance(delta, bool) or not isinstance(delta, int):
                problems.append(f"扰动必须为整数: {m}/{o}={delta!r}")
                continue
            u, v = by_key[key]
            balances[u] = balances.get(u, 0) + delta
            balances[v] = balances.get(v, 0) - delta
        bad = {n: b for n, b in balances.items() if b != 0}
        if bad:
            problems.append(f"见证未保持行/列合计: {target} -> {bad}")
        target_delta = next((d for m, o, d in assigns if (m, o) == target), None)
        if target_delta in (None, 0):
            problems.append(f"目标扰动为零: {target}")
    missing = hidden - seen_targets
    if missing:
        problems.append("缺少见证的隐藏格: "
                        + ", ".join(f"{m}/{o}" for m, o in sorted(missing)))
    return problems


def verify_artifacts(table: Table, public: dict, audit: dict) -> dict:
    """独立复核一对导出物：不调用求解器，只用有理数消元与见证机检。

    返回结构化结论；``ok`` 为 True 才表示公开文件确实满足保护条件。
    """
    from .protection import AUDIT_FORMAT

    problems: list[str] = []
    if audit.get("format") != AUDIT_FORMAT:
        problems.append(f"审计 format 不正确: {audit.get('format')!r}")

    hidden = {(m, o) for m, o in audit.get("hidden", [])}
    primary = {(m, o) for m, o in audit.get("primary", [])}
    secondary = {(m, o) for m, o in audit.get("secondary", [])}
    if hidden != primary | secondary or primary & secondary:
        problems.append("审计的主/次/隐藏集合不自洽")
    if not set(primary) <= set(hidden):
        problems.append("主抑制集合未完全包含于隐藏集合")

    # 1) 主抑制判定独立重算
    min_k = audit.get("policy", {}).get("min_contributors")
    if not isinstance(min_k, int) or min_k < 1:
        problems.append("审计缺少合法 min_contributors")
    else:
        for k, c in table.cells.items():
            if (c.contributors < min_k) != (k in primary):
                problems.append(f"主抑制判定与 k={min_k} 不符: {k}")

    # 2) 有理数消元：每个隐藏格都必须有自由度，且每个可见格不是隐藏
    algebra = check_free_columns(table, hidden)
    if algebra["pinned"]:
        problems.append("有理数消元发现被唯一确定的隐藏格: "
                        + ", ".join(f"{m}/{o}" for m, o in algebra["pinned"]))

    # 3) 扰动见证逐条机检
    problems += check_witnesses(table, hidden, audit.get("witnesses", []))

    # 4) 审计声明的合计与真实表一致；目标值与次抑制集合一致
    totals = audit.get("totals", {})
    if [list(x) for x in totals.get("row", [])] != [
            [m, table.row_totals[m]] for m in table.months]:
        problems.append("审计行合计与真实表不一致")
    if [list(x) for x in totals.get("col", [])] != [
            [o, table.col_totals[o]] for o in table.orgs]:
        problems.append("审计列合计与真实表不一致")
    policy_audit = audit.get("policy", {})
    costs = {tuple(k.split("|", 1)): v for k, v in
             policy_audit.get("costs", {}).items()}
    default_cost = policy_audit.get("default_cost", 1)
    cand = [tuple(x) for x in audit.get("search", {}).get("candidates", [])]
    for c in secondary:
        if c not in cand:
            problems.append(f"次抑制格不在候选集合: {c}")
    exp_cost = sum(costs.get(c, default_cost) for c in secondary)
    obj = audit.get("objective", {})
    if obj.get("cost") != exp_cost:
        problems.append("审计目标成本与次抑制集合不一致")
    if obj.get("extra_hidden_count") != len(secondary):
        problems.append("审计额外隐藏数量与次抑制集合不一致")

    # 5) 公开物结构、合计与泄露检查
    problems += check_public_payload(public, table, hidden)

    return {
        "ok": not problems,
        "problems": problems,
        "hidden": [list(k) for k in sorted(hidden)],
        "algebra": algebra,
        "witness_count": len(audit.get("witnesses", [])),
    }
