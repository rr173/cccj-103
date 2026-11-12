# 科研数据工具集（Python + DuckDB）

本仓库包含四项能力：**研究机构组织归属双时态维表**（`bitemp`）、
**经费分摊管线**（`bitemp.alloc`，含报表主次抑制）、
**测量数据处理与一阶不确定度传播**（`bitemp.metrology`）、
**双端 FASTQ 读段整理管线**（`bitemp.pefastq`，新增）。

## 双端 FASTQ 读段整理管线（`bitemp.pefastq`）

测序平台交来的双端 FASTQ 常被拆成多个分片：R1 是片段的一端，R2 是另一端。
分片内排列不保证一致，还会缺端、重传，甚至出现同名不同内容的读段。本管线
从一份清单出发，把全部源暂存进 DuckDB，**读完所有分片之后才决定每个
（样本, 读段名）键的去向**，最后为每个样本导出两个按名字 UTF-8 字节序、
逐条严格对应的 FASTQ，供后续序列分析直接消费。配对决策与导出都在有界
批量内完成（外存组织，不使用随输入增长的 Python 字典），输出顺序不取决
于分片排列与到达顺序。

### 清单（CSV）

```text
source_id,sample,mate,path
d1,DEMO1,1,shards/d1_r1.fq
d2,DEMO1,1,shards/d2_r1.fq.gz
d3,DEMO1,2,shards/d3_r2.fq
```

- `source_id` 全清单不可重复；`mate` 为 1 或 2（该源所有记录应为同一端）；
- 一个样本的同一端可以来自多个分片（多行同 sample 同 mate）；
- `path` 指向普通 FASTQ 或 `.gz`；相对路径按**清单所在位置**解析；
- `sample` 用于输出文件名，不得含路径分隔符与控制字符。

### 记录与头部约定

- 每条记录恰好四个物理行（`@头` / 序列 / `+行` / 质量串），**不支持折行**；
  解析按固定四行分块，绝不通过"搜索下一个 `@`"猜测记录边界——质量串本身
  可以以 `@` 或 `+` 开头，只有四行分块是可靠的。
- 序列只接受 A/C/G/T/N（大小写统一为大写），非空；质量为 Phred+33
  （ASCII 33..74），与序列等长。`+` 行若重复头信息，必须与 `@` 后文本一致。
- 支持的头部（`@` 之后）：`NAME`、`NAME/1`、`NAME/2`、
  `NAME 1:N:0:INDEX`（空白后首 token 以 `1:`/`2:` 开头的 Casava 风格）、
  两种标记同时出现。规范化读段名 = 空白前第一个 token 去掉末尾至多一个
  `/1` 或 `/2`。**配对键 = (样本, 读段名)，不跨样本串配**；原始头部完整
  保留在库中（`records.header`，`where` 入口可见）。
- 头部端标记（`/1`、`/2` 后缀与注释首 token）必须与清单 mate 一致；
  两处标记互相矛盾也算非法记录；无端标记时以清单为准。
- 记录错误可指回 `source_id`、记录序号（源内 1 起）与起始物理行号
  （见 `rejected.csv` 与 `where`）。

### 每个键的终态（恰好一种，判定优先级如下）

| 终态 | 含义 |
|---|---|
| `rejected` | 键内存在能确定名字的非法记录（另一端不当孤端输出） |
| `conflict` | 同一端出现内容不同的记录——哪怕清洗后会变成一样也不合并 |
| `filtered` | 两端唯一合法，但 3' 端清洗后任一端短于最短保留长度 |
| `paired`   | 两端唯一合法且通过清洗，进入合格 FASTQ |
| `orphan`   | 只有一端且没有其他错误 |

同一端反复出现且规范化后的完整序列与质量串都相同时**合并成一条**（保留
每个来源，见 `duplicates.csv`）。清洗只从每端 3' 末尾连续删除质量低于
阈值的碱基，遇到合格碱基即停止，不改中间的低质量位置；任一端清洗后短于
最短保留长度则整对进入 `filtered`，另一端不混入合格输出。后到的冲突不
可能让一对已输出的读段留在合格文件里——决策完成前不输出任何 FASTQ。

### 运行

```bash
pip install duckdb
python3 tests_pefastq.py               # 验收测试（34 项）
python3 main_pefastq.py demo           # examples/pefastq 小样本：每条读段的实际去向 + 参考程序核对
python3 main_pefastq.py run --manifest examples/pefastq/manifest.csv \
       --out out/ --min-qual 20 --min-len 8 [--db x.duckdb] [--memory-limit 1GB]
python3 main_pefastq.py where --db out/pefastq.duckdb --sample DEMO1 --name readC
python3 main_pefastq.py bigtest        # 20 万对双端远距离到达大样本 + 截断 gzip 演示
```

`run` 的输出（`--out` 目录）：

- `{sample}_R1.fastq` / `{sample}_R2.fastq`：每个样本两个文件，头部为
  `规范化名字/1`、`/2`，按名字的 UTF-8 字节序排列，逐条严格对应；
- `rejected.csv`（拒收明细：source_id、记录序号、起始行、错误、原始头部）、
  `conflict.csv`（冲突键每端的不同内容与来源）、`duplicates.csv`
  （重传合并明细与全部来源）、`orphans.csv`、`filtered.csv`
  （清洗前后长度与过短的端）、`key_states.csv`（每个键的唯一终态）；
- `stats.csv` / `stats.txt`：清洗前后的读段/碱基统计（全局与每样本）。
- 整理库（默认 `<out>/pefastq.duckdb`）保留全部明细，`where` 入口可查看
  某样本的终态分布或某条读段的去向（含落在输出文件的第几对/哪几行）。

任何源未完整读完（截断的四行记录、gzip 解压错误、文件缺失）**整次运行
失败**，退出码 2，不报告整理成功；已收集的诊断（sources 状态、已校验
记录）保留在库中。

### 统计口径（对账）

- `records_total` = 所有源读到的物理记录总数；`records_valid` +
  `records_invalid` = `records_total`。非法记录只计入 `records_invalid`
  与 `rejected.csv`，不进入任何序列/碱基统计（其规范化序列不可得）。
- `records_valid` = `distinct_ends` + `merged_copies`：同一
  （样本, 读段名, 端）内容完全相同的多份拷贝合并为一条，第一份之外计入
  `merged_copies`。
- `keys_total` = 五种终态之和（每个键恰好一种终态）。
- 清洗统计只覆盖两端均唯一合法的键（`clean_pairs` = paired + filtered），
  每端按合并后的一条计，重传拷贝不重复计读段或碱基：
  `reads_before` = 2 × clean_pairs；`bases_before` = 这些键两端清洗前
  长度之和；`reads_after` = 2 × paired；`bases_after` = 合格输出中的碱基
  总数（与输出文件逐条核对相等）；`bases_trimmed` = 合格键被删去的 3' 端
  碱基总数。`filtered` 键计入 before、不计入 after；
  orphan/conflict/rejected 键不进入清洗统计。

### 内存与顺序保证

暂存、分组、连接、排序都在 DuckDB 中进行（`--memory-limit` 控制执行内存，
超出溢写临时目录）；校验结果批量（默认 2 万条）写盘后经 COPY 入库；导出
按键集分页（每页默认 2 万对）。待配对记录从不进入随输入增长的 Python
字典。`bigtest` 在 256MB 配置下整理 20 万对（R1 全部正序分片、R2 反序
分片），进程峰值约 335MB，输出经独立参考程序逐对核对一致。

### 验证方法（避免自证）

`bitemp/pefastq/refcheck.py` 是独立的小规模内存参考程序：不调用生产管线
的任何解析/配对/清洗函数，按文档约定的语义独立重写，逐对核对输出文件
内容（字节级）、每个键的分类与统计口径。`tests_pefastq.py`（34 项）覆盖：
清单校验、头部格式、四行严格解析（质量串以 @/+ 开头、不按 @ 猜测恢复）、
截断记录与截断 gzip 整次失败且保留诊断、乱序配对与顺序无关（分片排列/
记录顺序置换后输出字节一致）、重传合并、清洗后会相同仍记冲突、跨样本
同名、缺端、非法记录拒收、3' 端清洗边界（含 SQL 清洗与 Python 参考在
随机质量串上逐条等价）、UTF-8 字节序与逐条对应、统计对账方程、where
入口、重跑重置。`bigtest` 另演示截断 gzip 被明确报错（退出码 2）。

### 目录（`bitemp/pefastq/`）

```
model.py      头部解析、记录校验、3' 端清洗（纯函数层）
fastqio.py    严格四行分块读取（普通/gzip），源不完整即 SourceFailure
manifest.py   清单装载与校验（source_id 唯一、相对路径按清单位置解析）
store.py      DuckDB 表结构
pipeline.py   三阶段管线：暂存 -> 配对决策 -> 清洗导出；where 去向查询
refcheck.py   独立内存参考程序（不调用生产函数）
datagen.py    小样本 / 双端远距离到达大样本 / 截断 gzip 生成器
main_pefastq.py  入口：run / where / demo / bigtest
tests_pefastq.py 34 项验收测试
examples/pefastq/ 小样本（11 个键覆盖全部约定情形）
```


## 测量数据处理与一阶不确定度传播（`bitemp.metrology`）

实验室把仪器读数交给数据中心后，分析人员要从长度、质量、温度等原始量计算
派生指标。本模块把不同单位的观测整理进 DuckDB，按公式计算指标值与**一阶
合成标准不确定度**，并解释结果受哪些原始量影响。它正确处理两件容易被
“每步误差各自平方相加”搞错的事：同一原始读数被多次使用（导数必须先合并），
以及两台仪器误差相关（协方差矩阵整体进入传播）。

### 输入（一个目录，四个文件）

```text
variables.json    {"variables": [{"name": "L1", "dimension": "length"}, ...]}
                  dimension ∈ length | mass | time | temperature | dimensionless
measurements.csv  specimen,variable,value,unit,u     u 与 value 同单位（标准不确定度）
correlations.csv  specimen,left,right,rho            原始变量间相关系数
formulas.json     {"indicators": [{"name": "Lsum", "expression": "L1 + L2", "unit": "m"}, ...]}
```

表达式允许：括号、数值常量、`+ - * /`、一元正负号、整数次幂（`x ** 2`、
`x ^ -1`，指数必须是有符号整数字面量）、`sqrt(x)`；其余语法（含任何其它
函数调用）明确拒绝，不使用 eval。指标可以引用原始变量或其它指标。

### 数学约定

| 约定 | 实现 |
|---|---|
| 内部坐标 | 一律在 SI 基本单位（m, s, kg, K）下计算；显示单位只换算输出的值与 u，不改变计算依据 |
| 单位变换 | 仿射：value_si = scale·value + offset；**u 只乘 scale，永不加 offset** |
| degC | 绝对摄氏温度：值 +273.15 转 K，u 不变；显示单位 degC 把指标当绝对温度处理（温差指标请用 K 显示） |
| 量纲 | 4 维 Fraction 指数向量 (L, M, T, Θ)；加减要求同量纲，乘除/幂/sqrt 相应组合；常量无量纲；sqrt 得分数次量纲 |
| 传播公式 | u(f)² = g C gᵀ；g 是 f 对每个**原始量**的偏导（前向自动微分，非数值差分），C = D R D 由 u 与 rho 合成 |
| 中间指标 | 引用中间指标时代入其对原始量的梯度；中间指标从不被当成独立新测量；同一原始量沿多条路径出现时导数在稀疏梯度字典中合并（故 `x - x` 的 u 精确为 0） |
| 缺失相关 | 未给出的非对角项按 0（本程序约定，随输出说明），对角线恒为 1 |
| 重复相关项 | 同对（含反向）rho 完全一致时合并，不一致拒收该 specimen |
| 半正定 | 整体检查最小特征值 ≥ -1e-10；落在 [-1e-10, 0) 的负特征值按 0 修正（截断重构）并在 specimen_notes 注明，不静默改出正定矩阵；更低则拒收该 specimen |
| 方差分解 | 每个 ok 指标给出梯度、对角贡献 g_i²C_ii 与各变量对两倍交叉贡献 2g_iC_ijg_j（可负，保留），相加恰为合成方差 |
| 负方差容差 | var < 0 且 \|var\| ≤ 1e-12 × Σ\|各方差贡献\| 时按 0 处理并注明；明显为负记 error，绝不取绝对值开根号 |
| 求值域 | 缺读数、除数 0、sqrt 参数 ≤ 0（含 0，一阶导数不存在）、0 的负整数次幂 → 仅依赖它的指标 unavailable，给出原因与依赖链，不用 0 补值 |
| 舍入 | 除最终展示外不舍入；库中与导出均为完整双精度 |

### 误差分层

- **配置错误**（variables.json / formulas.json）：未知量纲、重名、表达式
  语法/量纲非法、引用不存在的名字、指标循环依赖、显示单位与推导量纲不符。
  全部汇总后在读数计算前抛出（`ConfigFailure`），可定位到文件与行号。
- **逐 specimen 拒收**：重复读数、未知变量、单位与量纲不兼容、value/u 非有限、
  u 为负、相关项非法或矩阵非半正定。列全该 specimen 的拒收原因
  （带 measurements.csv / correlations.csv 行号），其余 specimen 照常处理。
- **公式局部失败**：缺读数或求值域问题只让依赖它的指标 unavailable，
  附原因与依赖链（如 `Lagain -> Lsum -> L2`）；不相关指标不受影响。

### 适用范围与限制

一阶（线性化）不确定度传播：在测值附近用一阶泰勒近似，适用于不确定度
相对较小、模型在该邻域内接近线性的场合（GUM 方法的常规适用域）。
强非线性、明显非正态或需要高阶矩的情形不在本程序范围内；sqrt 在 0 点、
除法在 0 点的一阶导数不存在，按不支持的求导边界处理（指标 unavailable）。
相关系数视为精确已知；本程序不估计 rho 自身的不确定度。

### 运行

```bash
pip install duckdb
python3 tests_metrology.py               # 验收测试（21 项）
python3 main_metrology.py demo           # examples/metrology 手工可算材料全流程
python3 main_metrology.py run --dir examples/metrology --db metrology.duckdb
python3 main_metrology.py explain --db metrology.duckdb --specimen S1 --indicator Lsum
python3 main_metrology.py export --db metrology.duckdb --out out/
```

`run` 会重建库中全部 metrology 明细表；之后可从磁盘重新打开库用
`explain`/`export` 查看已得到的明细。导出含 results.csv（值/u/可用状态/
原因/依赖链）、gradients.csv、contributions.csv、specimen_status.csv、
exceptions.json（拒收与失败明细，可追到输入行）、conventions.txt（数学约定）。

### 手工可算材料（`examples/metrology/`）

| 标本 | 演示点 | 手算结果 |
|---|---|---|
| S1 | 厘米/毫米换米、摄氏换开尔文、rho=0.5 的和与差、中间指标复用 | Lsum=2.3±√7.6e-5 m；Ldiff=0.7±√2.8e-5 m；Lagain=3.0±0.012 m；energy=11.25±0.135 J |
| S6 | 完全相关等精度测量：相减 u=0，相加 u=1 | Ldiff=0.1±0；Lsum=1.9±1 |
| S7 | 每对 rho≤0.9 但整体非半正定（λ_min=-0.8） | 整标本拒收 |
| S8 | 缺 L2：Lsum 等 unavailable，L1_m/T1_K 正常 | 依赖链可追 |
| S9 | L2=0：Lratio 除零、Lgeo 处于 sqrt 边界 | 仅二者 unavailable |
| S10 | L1=-1：Lgeo 的 sqrt 参数为负 | 仅 Lgeo unavailable |
| S11 | 零不确定度 + 奇异半正定矩阵（rho=1）+ 一致反向重复项合并 | 正常计算，u=0 |
| S12 | 反向重复项 rho 不一致（0.3 vs 0.4） | 整标本拒收，含行号 |
| S13 | 长度变量配秒单位 | 整标本拒收，含行号 |

另有 `Lself = L1 - L1` 演示同一原始量导数合并后 u 精确为 0；
`Lsum` 与 `Lsum_cm` 演示显示单位不改变 SI 计算依据。

### 目录（`bitemp/metrology/`）

```
jsonpos.py   带行号的 JSON 解析（定位配置错误，不用 eval）
units.py     量纲向量、单位注册表、显示单位解析（degC 仿射偏移）
expr.py      表达式词法/语法、量纲推导、前向自动微分
correl.py    相关矩阵装配、Jacobi 特征值、半正定校验与容差修正
inputs.py    目录装载：配置错误汇总；数据行保留文件与行号
compute.py   逐 specimen 校验与计算：方差分解、数值容差
store.py     DuckDB 表结构
service.py   高层入口：run / explain / export
```

## 研究机构组织归属 · 双时态维表

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
