"""测量数据处理与一阶不确定度传播（GUM 法）。

子模块：
  jsonpos   带行号的 JSON 解析（定位配置错误）
  units     量纲向量与单位（SI 内部坐标，degC 仿射偏移）
  expr      表达式解析 / 量纲推导 / 前向自动微分
  correl    相关矩阵装配、Jacobi 特征值、半正定校验与修正
  inputs    目录装载（配置错误汇总，数据行保留文件与行号）
  compute   按 specimen 校验与计算（方差分解、数值容差）
  store     DuckDB 表结构与落库
  service   高层入口：run / explain / export
"""
from . import compute, correl, expr, inputs, jsonpos, service, store, units  # noqa: F401
