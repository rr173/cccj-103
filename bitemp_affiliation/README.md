# 研究机构组织归属 · 双时态维表

Python + DuckDB 实现。研究者的归属会**迟报**、也会被**追溯更正**；分析人员既能问
“在某日实际属于哪里”（`effective_at`），也能问“截至上次月结，我们当时认为属于哪里”
（`known_at`）。

## 语义规则

| 规则 | 实现 |
|---|---|
| 业务区间左闭右开 `[valid_from, valid_to)`，`valid_to` 为空 = +∞ | `builder._resolve_visible` |
| `recorded_at` 带时区，统一 UTC | `model._to_utc_strict`（裸时间戳整批拒绝） |
| 更正 = 新断言显式 `supersedes` 旧断言，历史行不改 | 断言表只追加；维表按研究者整体重建 |
| 替代链中研究者必须相同、无环、无分叉 | `builder.validate_batch` |
| 知识时点 K：已知（`recorded_at <= K`，恰等于时包含）且未被已知后继替代 | `builder._visible_at` |
| 重叠时按 `(revision, recorded_at, assertion_id)` 字典序取大 | `builder._resolve_visible` |
| 空档保持未知，不向前填充 | 维表无行即未知；查询返回 `None` |
| 无效断言整批拒绝 | `process_batch` 先全量校验，通过后事务落库 |
| 相同输入重放结果稳定 | 完全相同的断言视为幂等重放，跳过 |

知识时间轴被所有 `recorded_at` 切成若干纪元，纪元内已知集恒定；每个纪元独立求
有效区间，再把相邻纪元中内容相同的区间合并（`build_dim_rows` 的 coalesce）。

## 目录

```
bitemp/
  model.py     断言模型与解析（时区规范化）
  schema.py    DuckDB 表结构：assertions / dim_affiliation / facts
  builder.py   校验、批处理、区间解析、差异与事实重算清单
  queries.py   effective_at × known_at 联合查询
  verify.py    独立 oracle：逐时点直接解释原始断言，对拍维表
  datagen.py   可复现的随机数据生成器（含无效批生成器）
main.py        装配入口：demo / verify
tests_acceptance.py  14 项验收测试
```

## 运行

```bash
pip install duckdb pytz
python3 tests_acceptance.py          # 验收测试（14 项）
python3 main.py demo                 # 生成数据 -> 两批装载 -> 维表 -> 查询例子 -> 对拍
python3 main.py verify --batches 5   # 多批随机数据 + 每批后 oracle 对拍
```

## 查询例子

```python
from bitemp import queries
# Q1: 当前认知下，R001 在某日实际属于哪里
queries.org_at(con, "R001", date(2024, 6, 1), datetime.now(timezone.utc))
# Q2: 截至月结时点，我们当时认为 R001 属于哪里
queries.org_at(con, "R001", date(2024, 6, 1), datetime(2025, 6, 30, 23, 59, 59, tzinfo=timezone.utc))
# Q3: 事实表按某知识时点认知关联归属（空档 -> organization 为 NULL）
queries.facts_with_org(con, month_end)
```

对应 SQL（`bitemp/queries.py`）：

```sql
SELECT organization FROM dim_affiliation
WHERE researcher_id = ?
  AND eff_from <= ? AND (eff_to IS NULL OR ? < eff_to)   -- 左闭右开
  AND known_from <= ? AND (known_to IS NULL OR ? < known_to);
```

## 增量与差异

`process_batch` 只重建批中涉及的研究者，返回 `BatchReport`：

- `rebuilds[i].added / removed`：新旧双时态区间差异清单；
- `rebuilds[i].changed_regions`：当前认知下归属发生变化的业务区间；
- `rebuilds[i].facts_to_recompute`：事件日期落在变化区间内的事实（SQL 区间连接）；
- `rebuilds[i].researcher_id` 之外的研究者维行原封不动。

## 验证方法（避免自证）

`verify.oracle_org_at` 是独立的逐时点解释器：对任意 `(研究者, 业务日期, 知识时点)`
直接在原始断言上按语义定义计算答案（已知 → 剔除被替代 → 取字典序最大），
与维表查询结果对拍。探测点取每个断言端点的 ±1 日、每个 `recorded_at` 的 ±1 秒
（含恰等于），覆盖所有临界情形。验收测试含：嵌套重叠、跨时区等价时刻、无穷尾
区间、晚到的替代链、同分决胜、循环替代被拒、区间端点不重复归属、更正后仅列出
真正受影响的事实、整批拒绝原子性、幂等重放、空档保持未知、随机多批对拍。
