# 科研数据工具集（Python + DuckDB）

本仓库包含六项能力：**研究机构组织归属双时态维表**（`bitemp`）、
**经费分摊管线**（`bitemp.alloc`，含报表主次抑制）、
**测量数据处理与一阶不确定度传播**（`bitemp.metrology`）、
**双端 FASTQ 读段整理管线**（`bitemp.pefastq`）、
**离线地图匹配**（`bitemp.mm`）、
**离线作业账本**（`bitemp.ledger`，新增）。

## 离线作业账本（`bitemp.ledger`）

科研计算集群会抢占长作业：一个逻辑 job 可能被调度器启动多次 attempt，
中途存检查点、换节点恢复，最后才成功或失败。日志若只按任务名看最终状态，
就无法回答一次分析跑了多久、用了多少 CPU/内存，也分不清重试消耗与成功
运行的消耗。本能力把**调度器事件、节点资源采样、任务依赖**三类离线材料
导入本地 DuckDB，重建可追查的执行过程并做资源核算。全程离线、无网页，
只读写本地 JSONL/CSV 与本地 DuckDB，保留仓库既有命令入口不变。

### 输入（三份材料）

- `events.jsonl`：每行一个 JSON 事件，字段
  `event_id, job_id, attempt_id, event_type, at, node, checkpoint_id, retry_of`；
  类型为 `submitted / started / checkpoint_saved / preempted / resumed /
  succeeded / failed`。`at` 是**含时区偏移**的 ISO 8601，导入后统一换算到
  零时区（内部以 epoch 秒存储，展示为 `…+00:00`）。`event_id` 全局唯一。
- `samples.csv`：`node, boot_id, sample_from, sample_to, cpu_cores,
  peak_rss_bytes`。`[sample_from, sample_to)` 是节点上的**半开**采样窗，
  数值列是窗内平均核心数与峰值 RSS；窗必须正长，数值有限且非负。
- `dependencies.csv`：`parent_job, child_job`，必须构成无环图，引用的
  job 都要在事件中出现。

### 状态规则（生命周期判定）

按 `(at, event_id 的 UTF-8 字节序)` 排序后，对每个逻辑 job 独立跑状态机：

- 合法过程：`submitted` 后 `started`；运行中可 `checkpoint_saved` 再
  `preempted`，随后 `resumed` **必须指回该 attempt 最新保存的
  checkpoint_id**；也可由运行态直接 `succeeded/failed`。
- 同一时刻的事件按 `event_id` 的 UTF-8 字节序定先后；若该顺序产生不可能
  的迁移，把所属 **job 标成 `invalid`** 并在 `job_issues` 列出冲突事件，
  原始事件全部保留在 `raw_events` 供排查，**同批其他 job 照常核算**。
- 重试会创建另一个 attempt，并以 `retry_of` 指回同 job 中更早、处于
  failed/preempted 的尝试；`attempt_id` 不得跨 job 复用。
- 终态：只有 `submitted` 的 job 为 `queued`（排队，不伪造执行结局）；
  日志在运行途中结束、后继被抢占而没有结局均为 `incomplete`（不猜终态）。
  **首轮失败不能压过后继仍在执行的事实**：一个失败轮次之后若还有
  running/checkpoint/preempted/fresh 的后继轮次，job 为 `incomplete`；
  只有“已失败且没有后续活跃轮次”的 job 才具有 `failed` 终态。后续尝试
  成功则 job 为 `succeeded`。
- 终态之后再出现事件、恢复引用旧检查点、同 job 运行区间相交等均隔离为
  invalid。抢占后的空档不计运行时间。

### 时间与资源单位、缺测口径

- 时间一律为**秒**（零时区 epoch）。运行秒数 = 各运行片段
  `[started/resumed, preempted/succeeded/failed)` 的半开区间长度之和；
  开放片段截到该 attempt 最后一个观测事件。
- 采样只统计与 attempt **已观测运行时段的交集**，窗被起止时刻按相交时长
  截断：`CPU core-seconds = Σ cpu_cores × 相交秒`，
  `RSS byte-seconds = Σ peak_rss_bytes × 相交秒`，峰值内存取实际覆盖到该
  attempt 的窗中最大 `peak_rss_bytes`。
- 同一 `node/boot_id` 的窗不得相交（相交为致命错误，整次导入放弃）；
  **不同 boot_id 的窗可重叠**（节点重启后的两次观测会话），重叠时刻按
  `boot_id` 的 UTF-8 字节序最小者记账，被让位的窗仍留在 `raw_samples`。
- 未被任何窗覆盖的运行时间进 `missing_intervals`，**标为缺测，绝不补零或
  向前沿用**；`coverage_ratio = 已覆盖秒 / 运行秒`。

### 重试成本不隐藏、依赖瓶颈与队列总账

- 每个 job 与 attempt 都输出运行秒、CPU、RSS、峰值内存、覆盖比例与缺测
  时段；`job` 视图同时给**全部 attempt** 与**成功 attempt** 两套合计，
  重试消耗不会被并进最终结果。
- 依赖图按逻辑 job 计算，权重 = 该 job 所有 attempt 运行秒数之和，求加权
  最长依赖路径并输出路径上的 job、各自权重与总运行秒。等权路径按完整
  `job_id` 序列的 UTF-8 字节序决胜。未终结/invalid/环污染节点不进完整
  路径，相关线索单独标 `complete=0`；依赖环写入 `dep_issues`。
  **完整性按全部前置祖先的实际结局判定**：一条链只有链上每个节点的所有
  前置祖先（含自身）都已结束（succeeded，或 failed 且无后续活跃轮次）才算
  完整。汇合节点两条父支路只有一条结束时，即使最长支路恰好是已结束的
  那条，汇合处及其后代也不得声称完整；不能用“另起一个单点结果”绕开。
  排队、未结束、非法或被环污染的祖先都会阻断，阻断沿全部后代传播。祖先
  状态只影响链的完整标注，**不改写节点自身的成败与权重**。
- 队列总账按 job 与状态汇总，且每行都能经
  `queue_summary → attempts → run_segments → accounting_slices →
  raw_samples` 下钻回具体运行片段与采样窗。

### 版本化与顺序不变性

- 三份材料按与行序无关的规范化内容计算 SHA-256 摘要作为 `version_id`。
  完全相同的输入再次导入**复用既有版本、不重复累加**；任一事件或采样值
  改动则产生新版本，**旧版本仍可按其摘要查询**，多版本在同一库内并存。
- 仅调整输入文件行序，不改变生命周期判定、资源汇总或依赖路径（摘要也
  相同）。致命错误（坏行、event_id 内容冲突、attempt 跨 job、同 boot 窗
  相交等）整体回滚，不留半成品版本。

### 内存口径

事件按批流式读入 DuckDB 暂存表，生命周期按 job 分批取数、处理完即释放，
资源核算只载入与当前片段节点/时段相交的采样窗；整个事件集不会长期驻留
Python 对象。`bigtest` 会打印导入/核算耗时与进程峰值常驻内存
（`ru_maxrss`）作为规模证据。

### 命令

```bash
# 导入三份材料并完成重建与核算（结果写入本地 DuckDB）
python3 main_ledger.py import --events events.jsonl --samples samples.csv \
                              --deps dependencies.csv --db ledger.duckdb
python3 main_ledger.py versions --db ledger.duckdb          # 全部内容版本
python3 main_ledger.py jobs     --db ledger.duckdb          # 队列总账
python3 main_ledger.py job      --db ledger.duckdb --job J_PREEMPT
python3 main_ledger.py explain  --db ledger.duckdb --job J_RETRY \
                                --attempt rb2 --segment 1   # 汇总下钻到采样窗
python3 main_ledger.py paths    --db ledger.duckdb          # 最长依赖路径
python3 main_ledger.py account  --db ledger.duckdb          # 切片重建汇总对账
python3 main_ledger.py export   --db ledger.duckdb --out-dir ./out  # CSV+JSON
# 按版本维护：只利用该版本留存的事实重算结局与计算链（--check 只读审计）
python3 main_ledger.py repair   --db ledger.duckdb --version <vid>
# 独立参考实现（重写状态机/积分/路径枚举）与手算期望值逐项对账
python3 main_ledger.py refcheck --db ledger.duckdb \
                                --events events.jsonl --samples samples.csv \
                                --deps dependencies.csv
python3 main_ledger.py demo      # 可手算小数据：乱序/幂等/冲突/隔离全覆盖
python3 main_ledger.py bigtest   # 大事件流：耗时、峰值内存与抽查
python3 tests_ledger.py          # 验收测试
python3 incident/regression_checks.py   # 事故回归（含旧库维护/回滚演示）
```

### 手工可算材料（`examples/ledger/`）

`demo` 使用的小数据集可完全手算，覆盖：抢占后从最新检查点续跑成功、失败
后另建 attempt 重试、同一秒事件按字节序排序、重复事件折叠与冲突 ID、
采样只覆盖运行时段一部分、采样窗被运行起止截断、不同 boot_id 重叠样本、
错误生命周期只影响自身 job、依赖环、同权路径字节序决胜。`refcheck.py`
刻意不 import 产品侧的状态机/积分/路径代码，只用标准库逐个 attempt 重算
运行时长与资源积分，再用 DFS 枚举小图所有简单路径作对照；`verify.py`
另含一份手算期望值，防止两套实现犯同样的错。

### 目录（`bitemp/ledger/`）

`parser.py`（流式解析与内容摘要）、`lifecycle.py`（状态机）、
`accounting.py`（窗交切与积分）、`dependencies.py`（图校验与最长路径）、
`store.py`（DuckDB 多版本表）、`service.py`（导入/核算编排，`import` 与
`repair` 共用同一 `_derive` 重建函数）、`queries.py` / `exporters.py`
（查询与导出）、`datagen.py` / `biggen.py`（小/大数据生成）、
`refcheck.py`（独立参考）、`verify.py`（对账与手算期望）。依赖 duckdb；
若系统未安装，会自动加载仓库 `.vendor/` 内随附的 wheel 解包目录。

### 按版本维护（`repair`）

已入库的版本若因归约/图算法的判断错误而留下了错误的衍生结局，无需找回
源文件或删库：

- `repair --db DB --version VID` 只读该版本在 `raw_events` / `raw_samples` /
  `raw_deps` 中留存的事实，用与导入相同的 `_derive` 重建全部衍生表
  （`jobs` / `attempts` / `run_segments` / `job_issues` /
  `accounting_slices` / `missing_intervals` / `dep_issues` /
  `critical_paths` / `queue_summary`），并**原子替换**该版本的行。
  `versions` 与三份原始事实一律不写，版本标识前后一致；其他版本一行不改。
- 识别需要重算的衍生表：`--check` 只读审计，把库中现有视图与按新的
  归约/图算法重算的结果逐项比较，报告 `jobs` / `attempts` /
  `critical_paths` / `queue_summary` 及行数发生变化的衍生表；无差异则
  说明该版本已一致。由于 `attempts` 的结局与状态机判定相关，凡是 `jobs`
  状态变化的版本通常同时需要核对 `attempts` 与 `queue_summary`。
- 一致性：`raw_events`→`attempts`→`run_segments`→`accounting_slices`→
  `raw_samples` 的积分与执行片段全部来自同一批原始事实重算；维护后
  `job` 明细、`jobs` 总账、`paths` 计算链与导出文件保持一致。
- 事务保护：整个替换在同一事务内完成（先 `DELETE` 该版本的衍生行，再全量
  重建）。人为制造写入中途故障时事务整体回滚，旧视图仍完整可读；写事务
  期间 DuckDB 持有库文件排他锁，其他进程拿不到锁会得到明确错误，而不是
  读到半新半旧的行——读写者看到的始终是某一次完整提交的视图。重复执行
  每次都从原始事实全量重算（不是增量累加），因此不会复制条目，也不会
  累计数值。

## 离线地图匹配（`bitemp.mm`）

野外生态调查沿道路采样的定位轨迹会在岔路、平行道路附近漂移。直接把每个
点吸附到最近线段，会让路线在两条不连通的道路之间来回跳。本能力**综合整段
观测与道路通行方向**做全局匹配：对每个观测保留多个候选，用分层动态规划
选择使「定位代价 + 转移代价」总和最小的候选序列，片段结束时回溯整条最优
路径，结果连同每次选择的完整依据持久化到 DuckDB。全程离线，只读取本地
CSV 与本地 DuckDB，不调用任何地图服务或在线地理接口。

### 输入（三个同目录或任意路径的 CSV，米制平面坐标）

- `nodes.csv`：`node_id,x,y`；node_id 唯一非空，坐标为有限数。
- `edges.csv`：`edge_id,from_node,to_node`；edge_id 唯一非空，端点必须存在，
  边长取欧氏距离且必须大于 0，允许平行边与有向环。每条边**只能从
  from_node 向 to_node 行驶**，反方向通行必须显式提供反向边。
- `observations.csv`：`track_id,obs_id,t,x,y`，t 为数值秒。每条轨迹内
  obs_id 唯一；按 t 排序后时间必须**严格递增**（不允许用文件行序掩盖重复
  时间或坏记录）；坐标与时间只接受有限数。

道路配置任何一处不合法即**拒绝整次运行**，错误指明文件与物理行号（表头为
第 1 行）。观测错误**隔离到 track_id**：含坏记录的轨迹整条拒收并逐条列出
原因，其他轨迹照常处理。

### 参数与单位

| 参数 | 单位 | 含义 |
| --- | --- | --- |
| `radius` | 米 | 搜索半径；点到投影点的垂直距离不超过它才成为候选 |
| `K` | 个（正整数） | 每点最多保留候选数 |
| `sigma` | 米 | 定位尺度，单点代价 = distance²/(2·sigma²) |
| `beta` | 米 | 路程尺度，转移代价 = |沿线路程 − 两点欧氏距离| / beta |
| `vmax` | 米/秒 | 最大速度；路程 > vmax·(tB−tA) 的转移不可用，**等于上限允许** |
| `max_gap` | 秒 | 相邻观测间隔大于它即结束当前片段（gap_timeout） |

六个参数都必须是正数，K 必须是正整数。

### 候选与评分（严格口径）

1. 把观测正交投影到每条有向线段；投影落在线外就夹到端点。距离 ≤ radius
   的线段才是候选，先按距离、再按 edge_id 的 **UTF-8 字节序**排序取前 K。
   每个候选保存投影坐标与沿通行方向从边起点量出的 offset，绝不只保留
   最近一条边。
2. 相邻观测的候选 A、B 只能沿道路方向连接，两种路线取更短：
   - 同一条边且 B.offset ≥ A.offset：直接走偏移差（同边倒退**不能**取
     偏移差绝对值）；
   - 走 A 的剩余边长，再沿有向图从 A 边终点到 B 边起点走最短路，最后走
     B.offset。等长时同边直达优先；节点最短路等长时按**整条中间 edge_id
     序列的 UTF-8 字节字典序**决胜。
   - 路线不存在或路程超速度预算时该转移不可用。
3. 对每个连续片段用动态规划最小化「全部单点代价 + 相邻转移代价」，并回溯
   整条片段（未来观测可以改变前面所选的边，绝不逐点贪心）。总分完全相等时
   比较整条观测序列所选 edge_id 列表的 UTF-8 字典序。浮点按 double 计算，
   参考核对容差 1e-8。

### 切段（断点本身是业务信息，判定先于候选转移尝试）

- `gap_timeout`：相邻观测间隔 > max_gap，先结束前段；
- `no_candidate`：该点没有任何候选，标 unmatched 并断开前后连接，之后有
  候选的点可以恢复为新片段；
- `no_feasible_transition`：当前层没有任何候选能从前层仍可达候选转移过来，
  回溯前段最优结果、当前层另开片段（点不丢弃，也不会因单个候选不通就过早
  切段）。

段首不收转移代价，单点段合法，不同轨迹之间从不连接。一个点允许同时记录
gap_timeout 与 no_candidate。

### 命令

```bash
# 装载并匹配（结果写入 DuckDB）
python3 main_mm.py run --nodes nodes.csv --edges edges.csv \
    --observations observations.csv --db mm.duckdb \
    --radius 25 --k 4 --sigma 5 --beta 10 --vmax 30 --max-gap 15
# 导出逐点结果 matches.csv 与片段结果 segments.csv
python3 main_mm.py export --db mm.duckdb --out-dir ./out
# 查询一条轨迹的全貌（逐点状态、片段、总分与断开原因）
python3 main_mm.py track  --db mm.duckdb --track T_NEAREST
# 查询某个观测：全部保留候选、选中边/offset/投影/定位距离、进出转移依据
python3 main_mm.py explain --db mm.duckdb --track T_NEAREST --obs o2
# 可手算小图：暴力参考逐项核对、乱序不变性、坏轨迹隔离
python3 main_mm.py demo
# 长轨迹大图：证明生产 DP 不枚举整条轨迹的候选组合
python3 main_mm.py bigtest --side 12 --tracks 4 --obs 300
```

### DuckDB 表（关闭进程后重新打开仍可查询）

`nodes`/`edges` 为道路配置；`observations` 为按 t 排序后的观测（含原始行号
与轨迹内序号）；`candidates` 保存每个点**实际保留的全部候选**（名次、边、
距离、投影、offset、单点代价）；`matches` 为逐点结果（状态、片段、所选边、
offset、投影、定位距离）；`segments` 为片段总分与定位/转移分项、起止观测
与断开原因；`transitions` 记录每次选中转移的两端候选、路线类型
（direct_same_edge / via_nodes）、中间道路列表、两段部分边长度、中间最短路
长度、总路程、原始欧氏距离与转移代价；`point_events` 记录切段事件；
`rejected_tracks` 记录被整条拒收的轨迹及逐条原因。

### 为什么不能只吸附最近道路（可手算材料）

`examples/mm/`（由 `bitemp.mm.datagen` 生成）是一张 13 节点、11 条有向边的
小图，参数 radius=25、K=4、sigma=5、beta=10、vmax=30、max_gap=15。轨迹
`T_NEAREST` 的三个观测为 o1=(10,3)、o2=(112,8)、o3=(15,−2)：

- o2 的最近边是死路 `E_J`（距离 8，单点代价 1.28），但 E_J 的终点没有任何
  出边；若每点只吸附最近边，o3 无法从 o2 连接，路线被强制切成两段。
- 全局最优在 o2 **没有选择最近的 E_J**，而是选了第二近的 E_R1（距离 12，
  第三近为 E_A 距离 14.42）：多付一点定位代价，换来一个连续片段；最终总分
  20.50083739（定位 3.14 + 转移 17.36083739），可用 `explain` 看到 o2 的
  全部 3 个候选与两次转移的路线、中间道路、部分边长度和代价算式。

同一份材料还覆盖：平行单行线（等代价按 edge_id 字典序选 E_A）、同边前进
直达、同边倒退有回环（绕 E_R1,E_R2,E_R3，路程 300）与无回环
（no_feasible 切两个单点段）、速度恰在上限（300=vmax·10 通过）与超过上限
（切段）、无候选后恢复、长间隔缺测、同点超时且无候选、两套等长菱形路线的
字典序决胜，以及含重复 obs_id/非有限坐标的坏轨迹 T_BAD 被隔离而其他轨迹
照常处理。

### 独立核对与顺序不变性

`bitemp.mm.refcheck` 是**不 import 生产最短路与回溯代码**的独立参考实现：
重写投影、DFS 枚举节点间简单路径，并对每个片段枚举全部候选组合，逐项对比
分数、选边与切段（`tests_mm.py` 全量核对，容差 1e-8）。`demo` 还把同一份
观测按确定性乱序重写文件再跑一遍：除原始行号外逐点结果与片段划分完全一致
——匹配只依赖记录内容与排序后的时间，不依赖文件行序。暴力枚举的组合数随
轨迹长度指数增长（如 4^300 ≈ 4.2×10¹⁸⁰），生产代码每层只保留至多 K 个 DP
状态、节点最短路按节点对缓存，`bigtest` 给出长轨迹的实际运行耗时与状态
规模证据。

### 目录（`bitemp/mm/`）

| 文件 | 职责 |
| --- | --- |
| `geometry.py` | 平面投影、夹取、候选生成与 UTF-8 字节序截断 |
| `graph.py` | 有向图、字典序决胜的 Dijkstra 最短路与节点对缓存 |
| `transitions.py` | 候选间沿线路线（直达/经节点）与转移代价、速度预算 |
| `matcher.py` | 分层动态规划、切段规则、全段回溯与字典序决胜 |
| `loader.py` | 三个 CSV 的严格校验（行号、坏轨迹隔离） |
| `store.py` | DuckDB 表结构、整库重建与持久化 |
| `pipeline.py` | 装载/匹配/导出/查询文本编排 |
| `refcheck.py` | 独立暴力参考实现（枚举组合，仅供小图核对） |
| `datagen.py` / `biggen.py` | 可手算演示材料 / 长轨迹网格大图 |

---

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
