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
  alloc/       分摊管线（见下节）
main.py        装配入口：demo / verify
main_alloc.py  分摊管线入口：demo / crystal（金标准）
tests_acceptance.py  14 项验收测试（双时态维表）
tests_alloc.py       29 项验收测试（分摊管线）
```

## 分摊管线（`bitemp.alloc`）

机构联盟编制科研经费统计时，一笔贡献会沿 `院系 -> 校区 -> 联合中心` 逐级分配，
组织结构还按日期调整。本模块把每笔事项按**末级报送单元**的精确份额拆成整数最小
货币单位，并留下可审计路由。它复用已交付的双时态维表：起点机构由
`queries.org_at(person_key, day, asof)` 解析，不重复实现归属装配器。

### 输入

两类 JSONL（每行一个对象；`read_jsonl` / `write_jsonl` 见 `alloc/io.py`）：

```text
事项  {"item_key","person_key","day","amount"}        amount 为整数最小货币单位，可负
路由  {"edge_key","src","dst","begin","until","numerator","denominator"}
```

`[begin, until)` 左闭右开，`until` 缺省（`null`/空）= +∞。每条出边携带一个非负有理数
份额 `numerator/denominator`。

### 语义与不变量

| 规则 | 实现 |
|---|---|
| 起点机构 | `bitemp.queries.org_at` 在给定 `asof` 下解析；解析不到 -> unresolved 明细，不丢弃 |
| 末级单元 | 在某活跃日期段内无活跃出边的节点（同一节点可在不同段分别是中间节点与末级） |
| 中间节点守恒 | 任一活跃日期的出边份额之和必须恰为 1（精确 `Fraction`） |
| 同 src/dst 多边 | 不同阶段可各用不同边；活跃期重叠视为重复配置并拒收 |
| 环 | 按边界切出的**每个日期段**分别检测，段内环拒收 |
| 仅跨期假环 | 反向边生效日完全错开时，合看有环、逐段无环 -> 允许 |
| 精确计算 | 全程 `Fraction`，只在末端一次性取整；无浮点中间量 |
| 取整 | 金额先取绝对值，按最大余数法分配整数单位；余数相同按末级标识升序；负金额反号 |
| 守恒 | 每笔事项末级金额之和严格等于原金额 |
| 多路汇合 | 拓扑 DP `forward[v] += forward[u]*w`，权重在汇合点相加；O(V+E)，不枚举路径 |
| 校验 | 边界切段、逐段校验环/守恒/负分母/悬空标识/重复配置，返回具体问题边 + 可复现日期 |
| 事务 | 规则整份导入一个事务；拒收时无半成品，上一版可用规则保留 |
| 固定摘要 | 摘要只由规范化边集合决定；等价集合（顺序/约分/排版不同）返回原版本 |
| 结果绑定 | run 绑定 `version_id + asof`；后续改规则不影响已存结果 |
| 事项幂等 | 同 `item_key` 同内容不重复累计；不同内容整批报冲突 |
| 汇总 | 按 (月份, 末级单元) 合计；unresolved 单列数量与金额 |

### 入口

```python
from bitemp.alloc import AllocationService, read_jsonl

svc = AllocationService(con)                       # con 为 bitemp.schema.connect(...)
svc.load_rules(read_jsonl("edges.jsonl"))          # 事务性装载，幂等
svc.load_items(read_jsonl("items.jsonl"))          # 幂等，冲突报错
trial = svc.trial(asof, day_range=(lo, hi))        # 试算，不落库
run_id = svc.compute(asof, day_range=(lo, hi))     # 正式计算，落库并绑定版本
svc.monthly_rollup(run_id)                         # [(月份, 末级单元, 合计, 笔数)]
svc.unresolved(run_id)                             # {count, amount, items}
svc.explain_text(run_id, "IT-001")                 # 精确权重 + 每条参与边的金额归因
```

`explain` 从落库的 run + 绑定版本重建图并做反向 DP，因此从磁盘重开数据库后
解释结果保持一致。`explain(...)["edge_amounts"]` 给出每条实际参与边的精确
金额归因（流量权重 × 金额，未取整）。

### 目录（`bitemp/alloc/`）

```
model.py     事项/边模型、精确分数、规范化与固定摘要
graph.py     边界切段、逐段校验、拓扑 DP、反向解释
allocate.py  起点解析 -> 传播 -> 最大余数法取整 -> 归因
store.py     DuckDB 表、规则/事项事务导入、结果落库
queries.py   月度合计、unresolved 明细、从磁盘解释
service.py   高层入口：装载/试算/计算/解释
goldens.py   金标准场景 + 逐路径独立 oracle
diamond.py   30 层菱形生成器、随机无环网络生成器
io.py        JSONL 读写
```

### 运行

```bash
python3 tests_alloc.py               # 分摊管线验收测试（29 项）
python3 main_alloc.py crystal        # 金标准（人工可手算）
python3 main_alloc.py demo           # 装载 -> 试算 -> 计算 -> 汇总 -> 解释 -> 拒收旧版继续
python3 main_alloc.py demo --db demo.duckdb   # 附磁盘重开解释一致性
```

金标准覆盖：两支到同一末端、三等分一分钱、负数冲回、机构跨日换出口、
同时有效的环（拒收）、仅跨期组合的假环（放行）、多路汇合相加。
另有独立逐路径小图对拍、随机无环网络守恒检查，以及 30 层菱形
（2^30 条路径、60 余节点）按拓扑 DP 不展开路径的端到端核验。

## 运行（双时态维表）

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

## 报表主次抑制（`bitemp.alloc.protection`）

科研联盟公开月度经费表时，人数少的格子必须隐藏（主抑制）；但只藏这些格子不够：
只要同一行/列还剩可算的格，读者就能用行/列合计反推出隐藏金额。因此还要额外隐藏
正常格子（次抑制），直到**整张表**的线性方程无法唯一确定任何隐藏金额。

### 表口径

| 规则 | 实现 |
|---|---|
| 行 = 月份（事项日期的 `%Y-%m`），列 = 末级机构 | `build_table` |
| 格 = 该已存批次的整数末端金额之和 | `monthly_rollup` + `alloc_run_leaf` |
| 只建模确有份额记录的格子；未出现组合 = 公开已知的结构零 | 表中不出现；公开物也不出现 |
| 同研究者同格多笔非零份额只算一个贡献人 | `_contributors` 用 `GROUP BY (月, 人, 末级)` |
| 正负冲回使金额为 0，但非零份额记录仍在 -> 人数仍计入 | 同上 |
| 找不到归属的事项单列汇总，不入表 | `unresolved_detail`，审计件里单列 |

### 保护判据（整张表一起看）

隐藏金额为可正可负的未知整数；已发布约束是行合计、列合计与每个可见格子的精确
金额。把可见值代入后，剩余条件只是“每行、每列的带符号隐藏格之和为 0”。以行/
列为二部图两侧、隐藏格为边，得到隐藏子图 **H**：

> **H 中每个隐藏金额都不能被唯一确定 ⇔ H 的每条边都在某个环上 ⇔ H 没有桥。**

桥会把 H 切成两侧，两侧各自必须收支平衡，从而桥的金额被唯一钉死。两个不相连的
矩形块各自安全，但用一条额外隐藏的桥连起来时，桥本身就会被全表方程解出——所以
“每行每列至少藏两个”的局部检查不充分（`tests_disclosure.py` 有反例）。
本判据等价于对剩余方程做有理数消元求每个未知量的自由度。

### 策略与求解

```python
Policy(min_contributors=k,           # 贡献人数 < k -> 主抑制（必须隐藏）
       costs={("2024-01","A"): 5},   # 每格额外隐藏成本（非负整数），缺省 default_cost
       default_cost=1,
       must_publish={("2024-02","C")},  # 必须公开；与主抑制冲突或切断所有方案 -> 拒绝
       max_candidates=18)            # 显式搜索规模上限；超出 -> incomplete，不宣称最优
```

* 目标按序：**总成本最小 → 额外隐藏数量最少 → 排序后的 (月份, 机构) 列表字典序最小**。
* 求解为精确最优优先搜索：目标键对“多选一格”严格单调，正好弹出第一个可行掩码
  即全局最优；结果确定，不依赖堆内同键顺序。
* 候选数超过 `max_candidates` 时返回 `status="incomplete"`（`verified=False`），
  任何导出都会拒绝。默认上限 18，验收测试覆盖 18 候选的真实精确求解与全枚举对拍。
* 主抑制被强制公开、或强制公开切断所有补环方案时，抛出 `NotPublishable`，
  不生成看似合格的报表。

### 两个出口

```python
from bitemp.alloc.protection import plan, publish, Policy
table, search = plan(con, run_id, Policy(min_contributors=2))
public, audit = publish(con, run_id, Policy(min_contributors=2))
```

* **公开件**（`format=alloc-disclosure/public/v1`）：只有 `rows`（每格的
  `{"amount": n}` 或 `"hidden"` 占位、`row_total`）、`col_totals` 与自校验
  `report_id`。不含隐藏金额、贡献人数、研究者标识、批次绑定或任何可还原明细。
* **审计件**（`format=alloc-disclosure/audit/v1`）：输入批次绑定（run/version/asof）、
  策略摘要、主/次抑制集合、目标值、整表 ledger，以及**每个隐藏格一条可机检的
  非零整数扰动见证**（环上 ±1 赋值，逐节点带符号和为 0）。

### 独立复核器（`bitemp.alloc.reportcheck`）

不调用求解器实现：

```python
reportcheck.check_free_columns(table, hidden)   # 有理数 Gauss-Jordan：每个隐藏量的自由度
reportcheck.is_protected_by_algebra(table, hidden)
reportcheck.enumerate_small(table, policy)      # 小表枚举全部合法额外隐藏集合
reportcheck.check_witnesses(table, hidden, witnesses)
reportcheck.verify_artifacts(table, public, audit)   # 结构 + 合计 + 泄露 + 见证
```

`enumerate_small` 用复核器自带的独立桥检测枚举 2^n 个掩码，最优解再由有理数消元
确认；`check_witnesses` 逐条核对环扰动。

### 运行

```bash
python3 main_disclosure.py demo    # 8 个可手算场景 + 公开/审计双出口
python3 tests_disclosure.py        # 30 项验收测试
```

演示覆盖：单独藏小群体格被行合计反推；补矩形环解除；两块环 + 桥时桥被钉住；
强制公开切断方案时拒绝；多人多笔与正负抵消的人数去重；同成本方案字典序决胜；
超上限返回 incomplete；更换最新路由后同一已存批次导出不变。
