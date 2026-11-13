# 账本事故处置报告：一次不实的“已完成”

本报告按处置顺序写成：先原样复现两个现象并保留修复前输出，再定位两处
局部判断为什么组合出错误结论，然后执行按版本维护，最后重开进程核对
单项详情、分类总账、计算链列表与导出文件的一致性。全部改动都落在
现有 `main_ledger.py` / `bitemp.ledger` 上，没有另建并行实现。

## 1. 原样复现（修复前程序，保留实际输出）

材料放在 `incident/materials/`：

- `a/`：事故 A。`a1` 提交、启动、失败；`a2` 以 `retry_of=a1` 启动并保存
  检查点 `ck1`；日志到此截止。
- `ud/`：事故 U/D。`U` 只有一条 `submitted`；`D` 提交、启动、成功；
  依赖一行 `U -> D`。
- `matrix/`：失败接失败、失败接成功、后继遭抢占、双父支路汇合、坏祖先
  旁的独立分支（见第 5 节）。

修复前的实际输出保存在 `incident/prefix_outputs/`（由未改动的程序生成）：

事故 A（`job_A.txt`、`jobs_A.txt`）：

```
job A: status=failed, is_terminal=1, success_attempt=null
attempts: a1 => failed（运行 10s），a2 => incomplete（运行 10s，仍在执行）
队列总账: failed 1 job, 运行 20s
```

即：`a2` 明明仍在执行、且日志在保存检查点后截止，A 却被宣告失败，并且
`is_terminal=1` 让下游把它当成已结束的链成员。

事故 U/D（`paths_ud.txt`）：

```
#1 [完整] 总运行 10s  D
#2 [不完整] 总运行 10s  U -> D
#3 [不完整] 总运行 0s  U
```

界面上同时出现“不完整的 U->D”与“完整的 D”，就好像把 U 从展示里裁掉，
D 的整体前置工作就结束了。修复前也没有任何一条路径提示“D 其实有未结束
的前置”——这正是完整性判断漏掉了非最长父支路（或单点另起）的表现。

## 2. 定位判断错误

### 2.1 尝试记录如何归约到逻辑任务（`bitemp/ledger/lifecycle.py`）

状态机本身没有错：`a1` 归约为 `failed`，`a2` 归约为 `incomplete`。错误在
最后的 job 级归约：

```python
elif any(a.state == ST_FAILED for a in attempts.values()):
    status = JOB_FAILED
```

它只看“历史上是否出现过失败”，完全忽略失败轮次之后是否还有活跃轮次。
`a2` 处于 `running/checkpoint`，属于未结束，却被 `a1` 的失败压掉。同理，
`preempted` 的后继、日志中途截止的后继都会被误判为 `failed` 终态。

正确口径应是按“最后一轮”判断：只有**失败且没有后续活跃轮次**的 job 才
具有 `failed` 终态；失败之后还有 `running/checkpoint/preempted/fresh`
后继时为 `incomplete`；后继后来成功则为 `succeeded`；只有 `submitted`
才是 `queued`。

### 2.2 图算法怎样产生“完整候选”（`bitemp/ledger/dependencies.py`）

`longest_paths` 在每个节点上只保留“以该节点为终点的最优父路径”，并用
节点自身的 `succeeded/failed` 决定它能否进入完整集合：

```python
node_complete = statuses.get(n) in COMPLETE_STATUSES
choices_c = [(t + w, seq + [n]) for t, seq in cand_c] + [solo]
```

DP 的 `solo` 分支允许任何节点**另起一个单点结果**，而 `cand_c` 只来自被
选中的那条父支路。于是：

- 汇合节点 H 有 P1（已结束、30s，最长）与 P2（仅排队、0s）两条父支路时，
  最长支路恰好是 P1，H 的完整候选只沿着 P1 生成，P2 从未被检查，H 与
  其后代 H2 被标为完整。
- D 有 U（仅排队）这一前置，但 D 自身成功，`solo=(10, ["D"])` 就把 D
  单点列为完整；U->D 这条线索又因为 U 未结束而单列为不完整。两条局部
  判断拼在一起，就得出“D 的整体前置已结束”的错误结论。
- 失败接失败（`failed` 是合法终态）与“失败后仍在执行”（不是终态）在
  旧归约里无法区分：前者应能进入完整链，后者必须阻断。

### 2.3 为什么两个局部判断会组合出错误结论

第一个局部判断把“出现过失败”当成 job 终态，使 `a2` 这类后继在归约层面
消失；第二个局部判断只检查“被选中的最长父支路 + 节点自身”，不再检查
全部前置祖先，使汇合处的另一条父支路和 U/D 里的 U 都从完整性判断中消失。
两者都是“从局部最优/局部状态推全局结论”，组合起来就出现了“A 已完成”“D
链条完整”这类不实提示。修复方向相应是：归约看全部轮次的最后结局；完整性
看每个节点的全部前置祖先的实际结局，阻断沿全部后代传播。

## 3. 修复（不新建并行实现）

### 3.1 生命周期归约（`lifecycle.py`）

`reconstruct_job` 的 job 级判定改为按最后一轮：

1. 任一轮成功 -> `succeeded`；
2. 全部轮次只有 `submitted` -> `queued`（不伪造执行结局）；
3. 最后一个失败轮次之后仍有 `running/checkpoint/preempted/fresh` 后继
   -> `incomplete`；
4. 全部轮次都已终结且有失败 -> `failed`；否则 `incomplete`。

`attempt` 的 `outcome()` 未变，`run_seconds` / CPU / RSS 的计算与记账口径
也未变。非法事件检查（终态后再来事件、恢复旧检查点、区间相交等）照常
生效，仍然只隔离到所属 job。

### 3.2 计算链完整性（`dependencies.py`）

新增 `ancestor_complete(graph, statuses)`，按 DAG 拓扑序对每个节点求：

```
complete[n] = n 已结束且未被环污染 且 每个直接父节点 complete[p]
```

这等价于逐点检查全部前置祖先的传递闭包，且不需要沿“被选中的路径”截断。
`longest_paths` 用该结果替换原来的 `statuses.get(n) in COMPLETE_STATUSES`，
并保留原有 DP、耗时权重、等长按完整 `job_id` 字节序决胜的全部语义。节点
自身的 `status` 与权重不被祖先状态改写；只有“能否作为完整链成员”受祖先
影响。环上/环下游（tainted）节点恒为 False，孤立节点的单点结果也必须
满足自身与祖先都 ended，不能靠“另起单点”绕开阻断。

## 4. 执行维护（旧库就地校正）

### 4.1 为什么不能只改新导入分支

同一份材料再次导入会按内容摘要复用旧版本（`reused=True`），不会重算。
因此只在导入路径修好，旧库里错误的 `jobs` / `attempts` / `critical_paths`
/ `queue_summary` 仍会原样被查询到。必须提供按版本的就地维护。

### 4.2 维护命令与作用范围

```bash
python3 main_ledger.py repair --db <DB> --version <VID>           # 重建
python3 main_ledger.py repair --db <DB> --version <VID> --check   # 只读审计
```

实现要点（`bitemp/ledger/service.py`）：

- 只读取该版本在 `raw_events` / `raw_samples` / `raw_deps` 中留存的事实，
  不要求操作者找回源文件；不读取、不写入其他版本。
- `import` 与 `repair` 共用同一个 `_derive(con, version_id, write=...)`
  函数：先落原始事实，再统一重建 `jobs` / `attempts` / `run_segments` /
  `job_issues` / `accounting_slices` / `missing_intervals` / `dep_issues` /
  `critical_paths` / `queue_summary`。没有第二套状态机或图算法。
- `versions` / `raw_events` / `raw_samples` / `raw_deps` 一律不写，
  `version_id` 与原始事实前后一致。
- 整个“删除该版本衍生行 + 全量重算”在**同一事务**里完成，见 4.4。

### 4.3 如何识别需要重算的衍生表

`repair --check` 只读审计，把库中现有视图与按修复后算法重算的结果逐项比较：

- `jobs`：状态、`is_terminal`、`success_attempt` 是否变化；
- `attempts`：结局与运行/覆盖/CPU/RSS/峰值数值（容差 1e-9）；
- `critical_paths`：完整标记与路径序列；
- `queue_summary`：各状态 job 数与各列合计；
- 各衍生表的行数（能发现缺行/多行）。

事故 A 的审计输出（`incident/demo/transcript.txt`）：

```
受影响衍生表：jobs, queue_summary
  - job A: ('failed', 1, None) -> ('incomplete', 0, None)
  - queue_summary: [('failed', 1, ...)] -> [('incomplete', 1, ...)]
```

U/D 版本审计报 `critical_paths` 变化（路径条数不变，但完整标记改变）。
矩阵版本审计报 `critical_paths, jobs, queue_summary`：
`job J_FP: ('failed', ...) -> ('incomplete', ...)`、`critical_paths: 17 条 -> 15 条`。

实操上，凡是审计报 `jobs` 变化的版本，都应当同时重算 `attempts` 与
`queue_summary`（结局和汇总都依赖状态机判定）；凡报 `critical_paths`
变化的版本，还应注意其后代的完整标记会随之变化。维护命令总是全量重算
上述衍生表，避免只改一部分造成新旧混杂。

### 4.4 事务保护与重复执行

- 单事务内先 `DELETE` 该版本全部衍生行，再全量重建；任何一步抛错整体
  `rollback`。演示：
  `incident/demo/rollback_transcript.txt` 中注入 `--fault-after-jobs 0`
  后命令返回 3，旧视图（事故后的 `incomplete` 视图）完整可读；未修复副本
  上注入故障时则完整保留旧的错误视图，不会出现半新半旧。
- 重复执行每次都从原始事实全量重算，`DELETE` 后插入，不是增量累加；审计
  比对为空即“无变化”，行数与数值都不增加。演示中第二次 `repair` 输出
  “已与原始事实一致，无变化”。
- 并发读者不会看到新旧结局混杂：DuckDB 对同一库文件的写事务持有排他锁，
  事务进行期间另一进程打开会得到明确的锁冲突错误（而不是读到半新半旧的
  行）；同一进程内另一连接也只能读到上一次已提交的完整视图。事务提交或
  回滚后，读者看到的要么是完整旧视图、要么是完整新视图。

## 5. 重开进程核对

维护后在**新进程**中重新打开库（不使用维护进程的内存状态），逐项核对：

1. **单项详情**：`job --db ... --job A --version <A>` 显示
   `status=incomplete`、`is_terminal=0`，两个 attempt 分别为 `failed` /
   `incomplete`，运行秒数与维护前一致（20s 总计）。
2. **分类总账**：`jobs --version <A>` 从 `failed 1` 变为 `incomplete 1`，
   运行秒数合计不变；U/D 版本仍是 `queued 1 / succeeded 1`。
3. **计算链列表**：`paths --version <A>` 为 0 条（A 无依赖）；U/D 版本的
   三条路径全部为“不完整”，其中 `D` 单点也不完整，`U->D` 不完整；矩阵
   版本中 `J_FF -> J_FFCHILD`、`J_P1`、`J_FF` 完整，汇合/坏祖先相关路径
   全部不完整。
4. **导出文件**：`export --version <A>` 后，`jobs.csv` 行数等于分类总账
   job 数、`jobs.csv` 的状态与单项详情一致、`critical_paths.csv` 与 `paths`
   一致、`summary.json` 的 `version_id` 与操作版本一致。

`incident/demo/transcript.txt` 保留了完整命令与输出。核对脚本
`incident/ancestor_audit.py` 还会直接检查导出文件行数与库内行数。

## 6. 场景覆盖

除两份事故材料外，`incident/regression_checks.py` 覆盖并断言：

| 场景 | 材料 | 期望 |
| --- | --- | --- |
| 失败接失败 | `J_FF` | 两轮都终结 -> `failed`，`is_terminal=1`，可成为完整链成员 |
| 失败接成功 | `J_FS` | 后继成功 -> `succeeded` |
| 后继遭抢占且无结局 | `J_FP` | -> `incomplete`，`is_terminal=0` |
| 双父支路汇合 | `J_P1`/`J_P2`->`J_H`->`J_H2` | H、H2 及其路径不完整；P1 单点完整 |
| 坏祖先旁独立分支 | `J_BAD`(invalid)、`J_QUEUE`、`J_INC` 各带成功后代；`J_OK->J_OKCHILD` | 坏祖先的后代不完整且阻断传播；独立链照常完整 |
| 记账原义 | 全量 | 权重仍为全部 attempt 运行秒数之和；等长按字节序决胜不变 |
| 节点自身状态 | 汇合处 | `J_H` 仍 `succeeded`，不被祖先改写 |

队列、未结束、非法祖先均阻断对应后代；与它们没有前置联系的另一条计算链
继续正常展示（`J_OK -> J_OKCHILD` 保持完整）。节点自身成功与“所在链是否
完整”是两个独立字段，互不覆盖。

## 7. 小图核对方法（避免共同假设掩盖错误）

`incident/ancestor_audit.py` 不 import `lifecycle` / `dependencies` /
`service` / `refcheck`，只做 SQL 取值与标准库解析，独立实现：

- 自己的状态机把 attempt 归约到 job 终态（“最后一轮”口径）；
- 对依赖图每个节点，沿**全部父边反向求传递闭包**，逐个检查祖先是否
  `succeeded/failed`；汇合节点任一支路未结束即阻断，阻断沿全部后代传播；
- 正向：产品标 `complete` 的路径，上面每个点的全部祖先都必须 ended；
- 反向：产品标不完整的路径，不能恰好满足“全部祖先 ended 且自身 ended”；
- 比较 `jobs` 状态、`attempts` 结局，并核对导出 CSV 行数与库内行数。

该脚本在修复前库上会指出“完整路径 #1 `J_P1 -> J_H -> J_H2` 经过祖先未结束
的 `J_P2`”“`J_BCHILD` 经过祖先未结束的 `J_BAD`”“`J_FF` 状态不一致”等
问题；在修复后库上通过。`refcheck.py` 也按同一口径改成了独立的全祖先
检查（显式 DFS 枚举 + 逐点传递闭包），继续为 `refcheck` / `demo` 对账。

## 8. 根因与命令作用范围

- **根因一**：job 终态归约用“历史上出现过失败”代替“最后一轮已终结”，
  首轮失败压过后继活跃轮次。
- **根因二**：最长路径的完整候选只沿被选中的最优父路径生成，且允许单点
  另起，完整性没有检查全部前置祖先的实际结局。
- **修复不改变**：耗时权重、等长决胜、记账数值、非法事件隔离、队列总账
  下钻链、版本摘要与顺序不变性。
- **`repair` 的作用范围**：只针对显式 `--version` 给出的版本，只读取该版本
  的 `raw_*` 事实并重算其衍生表；不写 `versions` 与原始事实，不改其他
  版本，不要求源文件，不删库。`--check` 不写任何行。
- **其他分析能力**：`bitemp.alloc` / `metrology` / `mm` / `pefastq` /
  `bitemp` 主包与本次修改无关。`main_ledger.py demo`、`bigtest`、
  `tests_ledger.py` 均仍可运行；仓库其他命令未改动。

## 9. 可运行回归检查

```bash
python3 tests_ledger.py                  # 既有验收测试（8/8）
python3 incident/regression_checks.py    # 本次事故回归（6/6）
python3 incident/ancestor_audit.py --db <DB> --version <VID> \
    --events ... --samples ... --deps ...   # 独立全祖先核对
```

`incident/regression_checks.py` 会用仓库 `incident/` 下的材料重建各场景，
并用 `incident/legacy_program/`（修复前程序快照）实际生成的旧库
演示：审计 -> 维护 -> 重复维护无变化 -> 注入故障回滚旧视图完整 -> 另一个
版本单独维护 -> 单项/总账/路径/导出一致 -> 独立祖先核对通过。

修复前后产物：

- `incident/db/pre_a.duckdb`、`incident/db/pre_ud.duckdb`、
  `incident/db/pre_matrix.duckdb`：修复前程序生成。
- `incident/demo/legacy.duckdb`、`legacy_matrix.duckdb`：修复前程序生成，
  已在维护演示中就地校正（含原始 A/U-D/矩阵材料与版本摘要）。
- `incident/legacy_program/`：修复前程序快照（旧 `lifecycle.py` /
  `dependencies.py`），用于生成上述旧库与回归中的对照。
- `incident/demo/transcript.txt`、`rollback_transcript.txt`、
  `matrix_transcript.txt`：命令与输出的逐字记录。
