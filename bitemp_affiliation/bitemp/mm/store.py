"""DuckDB 持久化：道路配置、候选、匹配结果、片段、转移依据全部入库。

磁盘库关闭后重新打开仍可查询每次选择的完整依据。重复运行会整体
重建结果表（reset），同一库内只保留最近一次运行的材料与结果。
"""
from __future__ import annotations

from pathlib import Path

import duckdb

from .geometry import Edge
from .loader import LoadedData, Observation
from .matcher import PointResult, Segment
from .transitions import Transition

SCHEMA_VERSION = "mm-1"

DDL = [
    "CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)",
    """
    CREATE TABLE nodes (
        node_id TEXT PRIMARY KEY, x DOUBLE NOT NULL, y DOUBLE NOT NULL)
    """,
    """
    CREATE TABLE edges (
        edge_id TEXT PRIMARY KEY, from_node TEXT NOT NULL, to_node TEXT NOT NULL,
        length DOUBLE NOT NULL, ax DOUBLE NOT NULL, ay DOUBLE NOT NULL,
        bx DOUBLE NOT NULL, "by" DOUBLE NOT NULL)
    """,
    "CREATE TABLE rejected_tracks (track_id TEXT, line_no INTEGER, reason TEXT)",
    """
    CREATE TABLE observations (
        track_id TEXT NOT NULL, obs_id TEXT NOT NULL, t DOUBLE NOT NULL,
        x DOUBLE NOT NULL, y DOUBLE NOT NULL, line_no INTEGER NOT NULL,
        seq INTEGER NOT NULL,
        PRIMARY KEY (track_id, obs_id))
    """,
    """
    CREATE TABLE candidates (
        track_id TEXT NOT NULL, obs_id TEXT NOT NULL, cand_rank INTEGER NOT NULL,
        edge_id TEXT NOT NULL, distance DOUBLE NOT NULL,
        px DOUBLE NOT NULL, py DOUBLE NOT NULL, offset_m DOUBLE NOT NULL,
        point_cost DOUBLE NOT NULL,
        PRIMARY KEY (track_id, obs_id, cand_rank))
    """,
    """
    CREATE TABLE segments (
        track_id TEXT NOT NULL, segment_id INTEGER NOT NULL,
        start_obs TEXT NOT NULL, end_obs TEXT NOT NULL,
        start_t DOUBLE NOT NULL, end_t DOUBLE NOT NULL,
        total_score DOUBLE NOT NULL, point_cost_sum DOUBLE NOT NULL,
        transition_cost_sum DOUBLE NOT NULL, n_points INTEGER NOT NULL,
        end_reason TEXT,
        PRIMARY KEY (track_id, segment_id))
    """,
    """
    CREATE TABLE matches (
        track_id TEXT NOT NULL, obs_id TEXT NOT NULL,
        state TEXT NOT NULL, segment_id INTEGER,
        edge_id TEXT, offset_m DOUBLE, px DOUBLE, py DOUBLE,
        gps_distance DOUBLE, point_cost DOUBLE, cand_rank INTEGER,
        line_no INTEGER NOT NULL,
        PRIMARY KEY (track_id, obs_id))
    """,
    """
    CREATE TABLE transitions (
        track_id TEXT NOT NULL, from_obs TEXT NOT NULL, to_obs TEXT NOT NULL,
        from_edge TEXT NOT NULL, to_edge TEXT NOT NULL,
        route_kind TEXT NOT NULL, route_distance DOUBLE NOT NULL,
        euclidean DOUBLE NOT NULL, partial_from DOUBLE NOT NULL,
        partial_to DOUBLE NOT NULL, middle_distance DOUBLE NOT NULL,
        middle_edges TEXT NOT NULL, transition_cost DOUBLE NOT NULL,
        PRIMARY KEY (track_id, from_obs, to_obs))
    """,
    """
    CREATE TABLE point_events (
        track_id TEXT NOT NULL, obs_id TEXT NOT NULL, seq INTEGER NOT NULL,
        event TEXT NOT NULL)
    """,
]

RESULT_TABLES = ["rejected_tracks", "observations", "candidates", "segments",
                 "matches", "transitions", "point_events"]


def connect(db_path: str | Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(db_path))


def connect_readonly(db_path: str | Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(db_path), read_only=True)


def reset_and_store(db_path: str | Path, data: LoadedData,
                    params: dict, track_results: dict,
                    obs_seq: dict[str, list[Observation]]) -> None:
    """整库重建并写入一次完整运行的所有材料与结果。"""
    con = connect(db_path)
    try:
        existing = {r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main'").fetchall()}
        for t in list(existing):
            con.execute(f"DROP TABLE {t}")
        for stmt in DDL:
            con.execute(stmt)

        con.executemany("INSERT INTO meta VALUES (?, ?)",
                        [(k, str(v)) for k, v in params.items()] +
                        [("schema_version", SCHEMA_VERSION)])
        con.executemany("INSERT INTO nodes VALUES (?, ?, ?)",
                        [(n.node_id, n.x, n.y)
                         for n in data.nodes.values()])
        con.executemany(
            "INSERT INTO edges VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(e.edge_id, e.from_node, e.to_node, e.length,
              e.ax, e.ay, e.bx, e.by) for e in data.edges])

        rej_rows = [(tid, None, reason)
                    for tid, reasons in sorted(data.rejected.items())
                    for reason in reasons]
        if rej_rows:
            con.executemany("INSERT INTO rejected_tracks VALUES (?, ?, ?)",
                            rej_rows)

        obs_rows = []
        for tid in sorted(obs_seq):
            for seq, o in enumerate(obs_seq[tid]):
                obs_rows.append((tid, o.obs_id, o.t, o.x, o.y,
                                 o.line_no, seq))
        if obs_rows:
            con.executemany(
                "INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?)",
                obs_rows)

        cand_rows = []
        match_rows = []
        seg_rows = []
        trans_rows = []
        event_rows = []
        for tid in sorted(track_results):
            points, segments, transitions = track_results[tid]
            seq_map = {o.obs_id: i for i, o in enumerate(obs_seq[tid])}
            for pt in points:
                o = pt.obs
                for rank, c in enumerate(pt.candidates):
                    cand_rows.append((tid, o.obs_id, rank, c.edge_id,
                                      c.distance, c.px, c.py, c.offset,
                                      c.point_cost))
                for ev in pt.events:
                    event_rows.append((tid, o.obs_id, seq_map[o.obs_id], ev))
                if pt.state == "matched":
                    c = pt.candidates[pt.chosen_idx]
                    match_rows.append((
                        tid, o.obs_id, "matched", pt.segment_id,
                        c.edge_id, c.offset, c.px, c.py, c.distance,
                        c.point_cost, pt.chosen_idx, o.line_no))
                else:
                    match_rows.append((
                        tid, o.obs_id, "unmatched", None, None, None,
                        None, None, None, None, None, o.line_no))
            for s in segments:
                seg_rows.append((
                    tid, s.segment_id, s.start_obs, s.end_obs, s.start_t,
                    s.end_t, s.total_score, s.point_cost_sum,
                    s.transition_cost_sum, s.n_points, s.end_reason))
            for (fo, to_), tr in transitions.items():
                trans_rows.append((
                    tid, fo, to_,
                    _edge_of(points, fo), _edge_of(points, to_),
                    tr.route.kind, tr.route.distance, tr.euclidean,
                    tr.route.partial_from, tr.route.partial_to,
                    tr.route.middle_distance,
                    ",".join(tr.route.middle_edges), tr.cost))
        for stmt, rows in [
            ("INSERT INTO candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", cand_rows),
            ("INSERT INTO matches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
             match_rows),
            ("INSERT INTO segments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
             seg_rows),
            ("INSERT INTO transitions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
             trans_rows),
            ("INSERT INTO point_events VALUES (?, ?, ?, ?)", event_rows),
        ]:
            if rows:
                con.executemany(stmt, rows)
    finally:
        con.close()


def _edge_of(points: list[PointResult], obs_id: str) -> str:
    for pt in points:
        if pt.obs.obs_id == obs_id and pt.chosen_idx is not None:
            return pt.candidates[pt.chosen_idx].edge_id
    raise KeyError(obs_id)
