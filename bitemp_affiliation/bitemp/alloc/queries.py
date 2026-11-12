"""查询入口：按月 × 末级单元合计、unresolved 明细、以及从磁盘重新解释结果。

所有读取只依赖已落库的 run（绑定 version_id + asof），因此后续改规则不会
污染历史结果；从磁盘重开数据库后解释结果保持一致。
"""
from __future__ import annotations

from fractions import Fraction

from .graph import RuleGraph, propagate
from .store import get_version, load_edges


def run_exists(con, run_id: str) -> bool:
    return con.execute("SELECT 1 FROM alloc_run WHERE run_id = ?",
                       [run_id]).fetchone() is not None


def run_meta(con, run_id: str) -> dict:
    row = con.execute(
        "SELECT run_id, version_id, asof_at, kind, created_at, item_count, resolved, "
        "unresolved, node_ops, edge_ops FROM alloc_run WHERE run_id = ?",
        [run_id]).fetchone()
    if row is None:
        raise KeyError(f"run 不存在: {run_id}")
    keys = ["run_id", "version_id", "asof_at", "kind", "created_at", "item_count",
            "resolved", "unresolved", "node_ops", "edge_ops"]
    return dict(zip(keys, row))


def monthly_rollup(con, run_id: str) -> list[tuple]:
    """按 (月份, 末级单元) 合计整数金额。月份取自事项 day。"""
    return con.execute(
        """
        SELECT strftime(i.day, '%Y-%m') AS month, l.terminal,
               SUM(CAST(l.amount AS HUGEINT)) AS total_amount,
               COUNT(*) AS n
        FROM alloc_run_item i
        JOIN alloc_run_leaf l
          ON l.run_id = i.run_id AND l.item_key = i.item_key
        WHERE i.run_id = ? AND i.resolved
        GROUP BY month, l.terminal
        ORDER BY month, l.terminal
        """, [run_id]).fetchall()


def unresolved_detail(con, run_id: str) -> dict:
    """unresolved 单列：条数、金额合计，以及逐条明细。"""
    rows = con.execute(
        "SELECT item_key, person_key, day, amount, reason FROM alloc_run_item "
        "WHERE run_id = ? AND NOT resolved ORDER BY item_key",
        [run_id]).fetchall()
    amount = sum((int(r[3]) for r in rows), 0)
    return {"count": len(rows), "amount": amount,
            "items": [(r[0], r[1], r[2], int(r[3]), r[4]) for r in rows]}


def run_items(con, run_id: str) -> list[dict]:
    rows = con.execute(
        "SELECT item_key, person_key, day, amount, source_org, resolved, reason "
        "FROM alloc_run_item WHERE run_id = ? ORDER BY item_key", [run_id]).fetchall()
    return [{"item_key": r[0], "person_key": r[1], "day": r[2],
             "amount": int(r[3]), "source_org": r[4], "resolved": r[5],
             "reason": r[6]} for r in rows]


def leaf_amounts(con, run_id: str, item_key: str) -> list[tuple]:
    rows = con.execute(
        "SELECT terminal, amount, weight_num, weight_den FROM alloc_run_leaf "
        "WHERE run_id = ? AND item_key = ? ORDER BY terminal",
        [run_id, item_key]).fetchall()
    return [(r[0], int(r[1]), Fraction(int(r[2]), int(r[3]))) for r in rows]


# ---------------------------------------------------------------- 从磁盘解释

def explain_from_store(con, run_id: str, item_key: str,
                       terminal: str | None = None) -> dict:
    """仅用已落库的 run + version 重建图并做反向解释。

    结果与“计算时”的解释完全一致：图取自绑定的 version，asof 只影响快照字段。
    """
    meta = run_meta(con, run_id)
    version_id = meta["version_id"]
    version_id, digest = get_version(con, version_id)
    graph = RuleGraph(load_edges(con, version_id))

    item_row = con.execute(
        "SELECT item_key, person_key, day, amount, source_org, resolved "
        "FROM alloc_run_item WHERE run_id = ? AND item_key = ?",
        [run_id, item_key]).fetchone()
    if item_row is None:
        raise KeyError(f"run {run_id} 中没有事项 {item_key}")

    stored_leaves = leaf_amounts(con, run_id, item_key)
    stored_weights = {t: w for t, _a, w in stored_leaves}
    if terminal is None and stored_leaves:
        terminal = max(stored_leaves, key=lambda x: (x[2], x[0]))[0]
    if terminal is not None and terminal not in stored_weights:
        raise KeyError(
            f"末级单元 {terminal} 未在该事项的结果中出现；"
            f"可选: {sorted(stored_weights)}")

    payload = {
        "run_id": run_id,
        "version_id": version_id,
        "digest": digest,
        "asof": meta["asof_at"],
        "item_key": item_key,
        "day": item_row[2],
        "amount": int(item_row[3]),
        "source_org": item_row[4],
        "resolved": bool(item_row[5]),
        "terminal": terminal,
        "stored_leaves": [
            {"terminal": t, "amount": a, "weight_num": w.numerator,
             "weight_den": w.denominator} for t, a, w in stored_leaves],
        "edges": [],
        "nodes": [],
    }
    if not item_row[5] or terminal is None:
        payload["explanation"] = None
        return payload

    prop = propagate(graph.active_for_day(item_row[2]), item_row[4])
    from .graph import explain_terminal
    exp = explain_terminal(prop, terminal)
    # 末级单元没有出边，back 恒为 1；其精确总权重是前向传播值 forward[terminal]，
    # 也就是所有到达路径份额之和（多路汇合在此自然相加）。
    recomputed = prop.weight(terminal)
    payload["explanation"] = {
        "terminal": terminal,
        "terminal_weight": recomputed,
        "nodes": {n: {
            "forward": info["forward"],
            "back": info["back"],
            "through": info["through"],
            "incoming": info["incoming"],
            "outgoing": info["outgoing"],
        } for n, info in exp.nodes.items()},
    }
    # 末级权重必须与落库值一致（可审计不变量）
    payload["weight_matches_store"] = (
        terminal in stored_weights and recomputed == stored_weights[terminal])
    flows = {e.edge_key: flow for e, flow in prop.flows()}
    amount = int(item_row[3])
    payload["edges"] = [{
        "edge_key": e.edge_key, "src": e.src, "dst": e.dst,
        "weight_num": e.weight.numerator, "weight_den": e.weight.denominator,
        "flow_num": flows.get(e.edge_key, Fraction(0)).numerator,
        "flow_den": flows.get(e.edge_key, Fraction(0)).denominator,
        # 金额归因 = 流量权重 × 事项金额（精确有理数，未取整）
        "amount_num": (flows.get(e.edge_key, Fraction(0)) * amount).numerator,
        "amount_den": (flows.get(e.edge_key, Fraction(0)) * amount).denominator,
    } for e in graph.active_for_day(item_row[2])]
    # 与落库的边金额归因逐条核对（供审计展示）
    payload["edge_amounts"] = [
        {"edge_key": r[0], "flow": Fraction(int(r[1]), int(r[2])),
         "amount": Fraction(int(r[3]), int(r[4]))
         } for r in con.execute(
            "SELECT edge_key, flow_num, flow_den, amount_num, amount_den "
            "FROM alloc_run_edge_flow WHERE run_id = ? AND item_key = ? "
            "ORDER BY edge_key", [run_id, item_key]).fetchall()]
    return payload


def explain_text(con, run_id: str, item_key: str,
                 terminal: str | None = None) -> str:
    """人类可读的解释文本（演示/审计用）。"""
    p = explain_from_store(con, run_id, item_key, terminal)
    lines = [
        f"run={p['run_id']} version={p['version_id']} asof={p['asof']}",
        f"事项 {p['item_key']} day={p['day']} amount={p['amount']} "
        f"起点={p['source_org']} 末级={p['terminal']}",
    ]
    if p["explanation"] is None:
        lines.append("  (unresolved，无可解释的路径)")
        return "\n".join(lines)
    exp = p["explanation"]
    lines.append(f"末级权重={exp['terminal_weight']}（与落库一致={p['weight_matches_store']}）")
    for e in p["edges"]:
        lines.append(
            f"  边 {e['edge_key']}: {e['src']}->{e['dst']} 份额={e['weight_num']}/{e['weight_den']} "
            f"流量={e['flow_num']}/{e['flow_den']} "
            f"金额归因={e['amount_num']}/{e['amount_den']}")
    for t in p["stored_leaves"]:
        lines.append(f"  末级 {t['terminal']}: 金额={t['amount']} 权重={t['weight_num']}/{t['weight_den']}")
    return "\n".join(lines)
