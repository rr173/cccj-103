"""离线地图匹配（bitemp.mm）。

把沿道路采样、在岔路与平行道路附近漂移的定位轨迹，综合整段观测与
道路通行方向匹配到有向道路网，结果（含每次选择的完整依据）持久化
在 DuckDB 中，全程无需地图服务或在线接口。

子模块：
  geometry    平面投影与候选生成（距离排序 + UTF-8 字节序截断 K）
  graph       有向图、字典序决胜的节点最短路与缓存
  transitions 候选间沿道路连接与转移代价
  matcher     分层动态规划、切段规则、全段回溯
  loader      三个 CSV 的严格校验（道路坏配置拒绝运行，坏轨迹隔离）
  store       DuckDB 表结构与持久化
  pipeline    装载/匹配/导出/查询编排
  refcheck    独立暴力参考实现（枚举组合，仅供小图核对）
  datagen     可手算演示材料与长轨迹生成
"""
from .matcher import (GAP_TIMEOUT, NO_CANDIDATE, NO_FEASIBLE,  # noqa: F401
                      Params)

__all__ = ["Params", "GAP_TIMEOUT", "NO_CANDIDATE", "NO_FEASIBLE"]
