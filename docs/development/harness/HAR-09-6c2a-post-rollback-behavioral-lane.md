# HAR-09.6c2a Post-Rollback Behavioral Lane

## 状态

已实现。

## 目标

在 Proposal-bound `rolled_back` Outcome、HAR-09.6c1 fresh Runtime Verification 和 HAR-09.6b
Before/After Evidence 均有效后，对其中一个原始 Final Evaluation lane 的 **exact installed baseline**
重新执行原生 HAR-08 H5a/H5c 流程。

本切片回答：“回滚后当前 active baseline 的真实安装产物，在当前平台上是否恢复了这个原始 H5c lane 的
行为？”它不是完整的 Post-Rollback Behavioral Evaluation。一个本地进程不能替代 Linux、macOS、Windows
的全部 Final Evaluation lanes，因此单 lane artifact 固定 `behavioral_evaluation_recorded=false`，不得授予
policy learning 或 promotion authority。

## 最小依赖链

```text
Proposal-bound rolled_back Outcome
  -> HAR-09.6c1 Runtime Verification
  -> exact active baseline slot
  -> HAR-09.6b Before/After lane
  -> original HAR-08 H5c Comparison
  -> original H5b2 baseline cohort
  -> ARC-07.5f/5g installed-runtime Eval channel + runtime identity
  -> fresh installed-runtime H5a samples
  -> fresh native HAR-08 H5c Comparison
  -> Post-Rollback Behavioral Lane artifact
```

Service 在执行前、执行后和读取时重新验证 Outcome、6c1、Before/After、Release、Harness 三个 authority
store。任一 lineage、摘要、active pointer、slot bytes、原 H5c 或 fresh receipt 漂移，都撤销 lane authority。

## 原始 H5c 基线重载

调用方只提交：

- `request_id`：`evrerollbackreq_*`；
- `comparison_id`：HAR-09.6b 中某个 Final Evaluation lane 的原 H5c ID。

Service 不接受自由路径、runner、命令或 repetitions。它从 durable authority 反向加载并验证：

1. Comparison 必须唯一属于同一 Proposal 的 Before/After lanes；
2. 原 H5c receipt ID/SHA 必须与 lane 精确一致；
3. H5b2 baseline registration、batch、样本数和 sample-set SHA 必须完整一致；
4. repetitions 继承原 baseline cohort，范围固定为 `5..100`；
5. 当前仅支持受信任 Suite registry 中唯一的 `protocol_hello@1` Suite；
6. Suite SHA、policy SHA、profile SHA、runner version、no-model identity 必须与原 H5a identity 兼容；
7. lane platform 必须等于 installed slot target 的 OS 前缀。

找不到唯一 Suite、runner 不支持、identity 不兼容或 baseline cohort 不完整时失败关闭；禁止退回当前
workspace import、伪造父进程结果或用自然语言判断替代 H5c。

## Fresh installed-runtime H5a

每个 repetition 都调用 `ReleaseSlotStore.evaluate_runtime_protocol()`：

- 固定执行 exact installed backend 的 `--runtime-eval-json`；
- 输入为 answer-stripped Process Request，不把 expected outcome 交给被测进程；
- Receipt 绑定 slot、manifest、backend binary、Suite/fixture、Process Request、Response 和 runtime
  platform/version identity；
- 输出、输入、case 数、单 case 时间与总进程时间均有硬上限；
- 不联网、不调用模型、不运行自由命令、不接受路径参数。

转换层不发明第二套指标。它严格复用原 H5a 的 case set、runner、expected、primary metric、空
metric-observation 合同与 guardrail 名称，并用 installed response 填充 `actual`、status、duration 和已验证的
`no_model`/`no_side_effect`。原合同出现未知形态时直接拒绝。

每个 fresh result 通过共享 `HarnessStore.record_eval_result()` 持久化，形成标准 H5a record；batch ID 绑定
6c1 Verification、lane order 和所有 Release Runtime Receipt ID，避免不同真实执行被误合并。

## Fresh native H5c 与恢复判定

fresh cohort 继续调用 HAR-08 `build_eval_comparison_receipt()`，不是本模块自写弱比较器。它复用：

- 原 H5b2 baseline ID、batch 和 sample-set SHA；
- HAR-08 identity compatibility gate；
- case mechanical transitions 与 policy gate；
- repetitions、flakiness、95% confidence interval 与统计 verdict；
- content-addressed H5c receipt 和 Harness Store 动态复核。

`recovery_status` 的机械映射为：

| H5c 事实 | Lane 状态 |
| --- | --- |
| identity 或 sample mechanical incompatible | `incompatible` |
| statistical inconclusive/flaky 或 sample inconclusive | `inconclusive` |
| statistical `unchanged` 且全部 sample mechanical `unchanged` | `recovered` |
| 其余可比较结果，包括 improved/regressed | `changed` |

这里刻意不把“更快”或“看起来更好”自动改名为“已恢复”。完整矩阵聚合器可以在后续 HAR-09.6c2b
制定跨 lane policy；6c2a 只保存事实，不扩大结论。

## Artifact 与权限

`EvolutionPostRollbackBehavioralLane` 冻结：

- Outcome、Proposal、6c1 Verification、Before/After Evidence 的 ID/SHA；
- baseline slot/manifest/source/version/target/backend binary identity；
- 原 lane order/kind/platform、原 H5c 和原 baseline cohort；
- 完整 Runtime Eval Request 与每个真实进程 Receipt；
- fresh H5a batch/sample-set SHA 与完整 native H5c receipt；
- recovery status 与 evaluated timestamp。

固定权限位：

- `lane_evaluation_recorded=true`；
- `behavioral_evaluation_recorded=false`；
- `long_term_metrics_recorded=false`；
- `learning_authority=false`；
- `promotion_authority=false`。

单 lane 不能修改 Workbench 的总体 behavioral flag，不能覆盖 HAR-09.6b 实施前后事实，也不能进入 EVO-06
学习闭环。

## 持久化与并发边界

Evolution、Harness、Release 分属三个 SQLite authority store，不宣称跨库原子事务。

- Evolution Store 使用参数化 SQL、`BEGIN IMMEDIATE`、Outcome+原 H5c 唯一键和 2 MiB artifact 上限；
- 单 Service 实例按 Request+Comparison 加锁，重复调用先重载 durable artifact，不重复运行进程；
- 多实例竞争保持安全：三库内容寻址与 Evolution 唯一键阻止覆盖或混绑；竞争执行可能留下未被最终 lane
  引用的合法 Release/H5a/H5c 事实，后续 retention 模块负责按引用关系回收；
- 写入前后 authority 变化会拒绝提交；读取时任一依赖篡改会将 `lane_authority=false`；
- 历史 lane 可保留，但 baseline 不再 active 时 `active_baseline_authority=false`。

## 双通道与终端 UI

- Agent Tool：`evolution_post_rollback_behavioral_lane(request_id, comparison_id)`；
- 共享 Slash：`/evolution outcome-verify-behavior <rollback-request-id> <comparison-id>`；
- CLI、Textual TUI 与 New UI 复用共享 Slash Router 和同一 Tool/Service；
- 回执显示 Proposal、Outcome、Suite/platform、原/新 H5c、samples、recovery 和 lane authority；
- 回执必须继续显示“行为级总体评测尚未完成”，不能因单 lane 成功而省略。

该动作运行 exact installed baseline 的固定 eval 子进程，但不接受自由 shell、路径或网络目标。normal 与 bypass
均无需二次确认；lockdown 仍由统一 PermissionChecker 控制。

## 验收证据

- 临时真实 Git workspace 加载仓库受信任 `protocol-hello-core` Suite；
- 生成原生 5-sample H5a baseline/candidate 与原 H5c；
- 组装、安装、boot、activate 一个 POSIX release bundle；
- exact installed runtime 子进程连续执行 5 次，形成 5 个不同 Release Receipt；
- fresh H5a/H5c 使用原 baseline cohort，结果为 `recovered/unchanged`；
- Service、Agent Tool、共享 Slash 三次读取同一 lane，Release Receipt 总数仍为 5；
- Release receipt row 篡改后动态撤销 fresh runtime/lane authority；
- forged comparison 在执行进程前失败关闭；
- Evolution Tool registry、Engine import composition、Ruff、py_compile、diff check 与相关小模块测试通过。

当前真实进程 fixture 覆盖 POSIX host；Windows `.exe` lane 需要由 Windows CI/host 产生，不能在 macOS 上伪造。

## 后续切片

1. `HAR-09.6c2b Post-Rollback Behavioral Matrix`：收集原 Final Evaluation 的全部平台/lane，验证集合完整性，
   聚合后才能令 `behavioral_evaluation_recorded=true`；
2. `HAR-09.6d Long-Term Outcome Window`：定义窗口、覆盖率、censoring、持续健康和撤权；
3. Workbench typed projection：在 Reviews 详情展示各 lane 与总体矩阵，但继续复用本 authority；
4. promoted Outcome、supersede ledger 与 ARC-07.6 配置/数据恢复；
5. 三库未引用竞争事实的 retention/reference scan。
