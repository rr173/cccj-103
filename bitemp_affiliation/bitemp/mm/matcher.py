"""分段在线（全段回溯）地图匹配。

对每条轨迹按 t 排序后的观测逐层推进，每层保留该点的全部候选状态，
层间做候选两两转移的动态规划；片段结束时沿父指针回溯整条最优序列，
因此未来观测可以改变前面所选的边——绝不逐点贪心。

切段规则（在尝试候选转移之前先判定前两项）：
  gap_timeout              相邻观测间隔 > max_gap；
  no_candidate             该点半径内没有任何候选，点状态为 unmatched；
  no_feasible_transition   当前层没有任何候选能从前层可达候选转移过来。
一个点可同时记录 gap_timeout 与 no_candidate。段首不收转移代价，
单点段合法；不同轨迹之间从不连接。

总代价完全相等时，比较整条观测序列所选 edge_id 列表的 UTF-8 字典序。
实现用"每层按 (总代价, 路径字典序) 重排名次"把字典序比较降为 O(1)
整数比较，不枚举整条轨迹的候选组合。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .geometry import Candidate, Edge, candidates_for
from .graph import Graph
from .loader import Observation
from .transitions import Transition, evaluate

GAP_TIMEOUT = "gap_timeout"
NO_CANDIDATE = "no_candidate"
NO_FEASIBLE = "no_feasible_transition"


@dataclass
class Params:
    radius: float
    k: int
    sigma: float
    beta: float
    vmax: float
    max_gap: float


@dataclass
class PointResult:
    obs: Observation
    candidates: list[Candidate]
    state: str = "unmatched"        # matched | unmatched
    segment_id: int | None = None
    chosen_idx: int | None = None   # 在 candidates 中的下标
    events: list[str] = field(default_factory=list)


@dataclass
class _State:
    layer: int                      # 在片段内的层号（0 起）
    cand_idx: int                   # 本层 candidates 的下标
    total: float                    # 截至本层的最小总代价
    parent: "_State | None"
    edge_bytes: bytes               # 本层 edge_id 的 UTF-8 字节
    path_rank: int = 0              # 本层路径字典序名次（越小越优）
    trans: Transition | None = None  # 与父候选之间的转移（段首为 None）
    seg_id: int = 0


@dataclass
class Segment:
    segment_id: int
    track_id: str
    start_obs: str
    end_obs: str
    start_t: float
    end_t: float
    total_score: float
    point_cost_sum: float
    transition_cost_sum: float
    n_points: int
    end_reason: str | None          # 正常结束的断点原因；最后一段为 None


def _assign_ranks(states: list[_State]) -> None:
    """按 (总代价, 父名次, 本层 edge_id 字节序) 重排名次。

    父名次来自上一层 path_rank，等价于比较完整前缀 edge_id 序列；
    再拼上本层 edge_id 即得到完整序列的字典序。
    """
    states.sort(key=lambda s: (
        s.total,
        s.parent.path_rank if s.parent is not None else -1,
        s.edge_bytes,
    ))
    for rank, s in enumerate(states):
        s.path_rank = rank


def match_track(track_id: str, obs_list: list[Observation],
                edges: list[Edge], graph: Graph, p: Params):
    """匹配一条轨迹。

    返回 (points, segments, chosen_transitions)：
      points             与 obs_list 等长、同序的逐点结果（含全部候选）；
      segments           片段列表，片段号在轨迹内从 1 起；
      chosen_transitions {(前层 obs_id, 本层 obs_id): Transition}，只含选中转移。
    """
    edge_map = {e.edge_id: e for e in edges}
    points: list[PointResult] = []
    segments: list[Segment] = []
    chosen_transitions: dict[tuple[str, str], Transition] = {}

    cand_lists = [candidates_for(o.x, o.y, edges, p.radius, p.k, p.sigma)
                  for o in obs_list]

    seg_seq = 0
    active: list[_State] = []
    prev_obs: Observation | None = None
    prev_active_layer: int | None = None  # 上一个仍在当前片段内的层号

    def open_segment(layer: int) -> list[_State]:
        nonlocal seg_seq
        seg_seq += 1
        cands = cand_lists[layer]
        states = [
            _State(layer=0, cand_idx=i, total=c.point_cost, parent=None,
                   edge_bytes=c.edge_id.encode("utf-8"), seg_id=seg_seq)
            for i, c in enumerate(cands)
        ]
        _assign_ranks(states)
        return states

    def close_active(reason: str | None) -> None:
        nonlocal active
        if not active:
            return
        best = min(active, key=lambda s: (s.total, s.path_rank))
        chain: list[_State] = []
        node = best
        while node is not None:
            chain.append(node)
            node = node.parent
        chain.reverse()
        seg_id = chain[0].seg_id
        for s in chain:
            pt = points[layer_of[s.layer]]
            pt.state = "matched"
            pt.segment_id = seg_id
            pt.chosen_idx = s.cand_idx
        first_obs = obs_list[layer_of[chain[0].layer]]
        last_obs = obs_list[layer_of[chain[-1].layer]]
        segments.append(Segment(
            segment_id=seg_id,
            track_id=track_id,
            start_obs=first_obs.obs_id,
            end_obs=last_obs.obs_id,
            start_t=first_obs.t,
            end_t=last_obs.t,
            total_score=best.total,
            point_cost_sum=0.0,
            transition_cost_sum=0.0,
            n_points=len(chain),
            end_reason=reason,
        ))
        active = []

    # 片段内层号 -> 轨迹全局层号（同一时间只有一个活动片段，可直接映射）
    layer_of: dict[int, int] = {}

    for layer, obs in enumerate(obs_list):
        cands = cand_lists[layer]
        pt = PointResult(obs=obs, candidates=cands)
        points.append(pt)

        timed_out = (prev_obs is not None and
                     obs.t - prev_obs.t > p.max_gap)
        if timed_out:
            pt.events.append(GAP_TIMEOUT)
            close_active(GAP_TIMEOUT)
            layer_of = {}
            prev_active_layer = None

        if not cands:
            pt.events.append(NO_CANDIDATE)
            close_active(NO_CANDIDATE)
            layer_of = {}
            prev_active_layer = None
            prev_obs = obs
            continue

        if not active:
            # 新片段第一层（含 gap/无候选/no_feasible 后另开）：段首无转移代价
            layer_of = {0: layer}
            active = open_segment(layer)
            prev_active_layer = layer
            prev_obs = obs
            continue

        # 常规层：与片段内上一个有候选的层做候选两两转移
        assert prev_active_layer is not None
        prev_layer = prev_active_layer
        dt = obs.t - obs_list[prev_layer].t
        in_layer_no = active[0].layer + 1
        layer_of[in_layer_no] = layer
        new_states: list[_State] = []
        feasible_any = False
        for j, c in enumerate(cands):
            best_prev: _State | None = None
            best_total = float("inf")
            best_trans: Transition | None = None
            for ps in active:
                pc = cand_lists[prev_layer][ps.cand_idx]
                tr = evaluate(pc, c, obs_list[prev_layer].x,
                              obs_list[prev_layer].y, obs.x, obs.y, dt,
                              graph, edge_map, p.beta, p.vmax)
                if tr is None or not tr.feasible:
                    continue
                feasible_any = True
                tot = ps.total + c.point_cost + tr.cost
                key = (tot, ps.path_rank)
                if best_prev is None or key < (best_total, best_prev.path_rank):
                    best_total = tot
                    best_prev = ps
                    best_trans = tr
            if best_prev is not None:
                new_states.append(_State(
                    layer=in_layer_no, cand_idx=j, total=best_total,
                    parent=best_prev, edge_bytes=c.edge_id.encode("utf-8"),
                    trans=best_trans, seg_id=best_prev.seg_id))

        if not feasible_any:
            pt.events.append(NO_FEASIBLE)
            close_active(NO_FEASIBLE)
            layer_of = {0: layer}
            active = open_segment(layer)
        else:
            _assign_ranks(new_states)
            active = new_states
        prev_active_layer = layer
        prev_obs = obs

    close_active(None)

    # 按回溯后的选中链重算各片段代价分项，并登记选中转移
    for seg in segments:
        layers = [i for i, pt in enumerate(points)
                  if pt.segment_id == seg.segment_id]
        pc_sum = 0.0
        tc_sum = 0.0
        for li in layers:
            pt = points[li]
            pc_sum += pt.candidates[pt.chosen_idx].point_cost
        for la, lb in zip(layers, layers[1:]):
            oa, ob = obs_list[la], obs_list[lb]
            ca = points[la].candidates[points[la].chosen_idx]
            cb = points[lb].candidates[points[lb].chosen_idx]
            tr = evaluate(ca, cb, oa.x, oa.y, ob.x, ob.y,
                          ob.t - oa.t, graph, edge_map, p.beta, p.vmax)
            assert tr is not None and tr.feasible
            tc_sum += tr.cost
            chosen_transitions[(oa.obs_id, ob.obs_id)] = tr
        seg.point_cost_sum = pc_sum
        seg.transition_cost_sum = tc_sum
        seg.total_score = pc_sum + tc_sum

    return points, segments, chosen_transitions
