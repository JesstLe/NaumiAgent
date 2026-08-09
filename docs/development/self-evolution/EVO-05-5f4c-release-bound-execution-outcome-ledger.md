# EVO-05.5f4c Release-bound Execution Outcome Ledger

## 目标

把 managed New UI/TUI 中真实结束的 chat run，转化为可追溯到 exact opt-in candidate release 的不可变执行结果。
该切片填补 liveness 与 `minimum_completed_runs` 之间的证据空白：heartbeat 证明运行时存活，Execution Outcome 则证明
某个具体用户运行在该 release 上产生了什么终态、用量和完整运行区间。

## 输入合同

每个 Outcome 必须同时消费并机械核对：

1. EVO-05.5f4b 当前 passing 的 liveness Window Receipt；
2. HAR-10.2k 在 chat run 开始时写入的 exact `RunReleaseProvenance`；
3. 同一 `ChatRunStore` 中的真实持久终态 `CompletionReceipt`；
4. Engine 会话累计计数器在 run 开始/结束时的结构化增量 `RunUsage`；
5. HAR-10.2j 中从运行开始前到运行完成后的连续 heartbeat observation slice。

任一来源缺失、跨 Session、跨 workspace、binding 不同、时间倒置、账本 head 不一致或 coverage 尚未闭合时，不生成 Outcome。

## 实现

### 单次运行用量

- `RunUsageTotals` 冻结 run 开始前与结束后的累计 input/output/cache token、turn 和模型上报费用；
- `RunUsage` 只保存非负增量，以 12 位小数确定性量化 reported cost，并形成 content-addressed identity；
- 该费用明确是模型路由上报值，`billing_authority=false`，不冒充供应商账单；
- usage 与 `CompletionReceipt` 在 `ChatRunStore.finish_run()` 的同一事务中写入；旧数据库自动增加 `usage_id/usage_json`；
- chat run 一旦进入终态，其 status、completed time、receipt 和 usage 不可被不同值改写；完全相同的重复收口幂等，
  仅允许为旧终态记录补齐此前为空的 assistant/receipt/usage 字段；
- 计数倒退、NaN 或非法来源时，用户运行和完成回执仍正常收口，但该 run 不具备 Outcome 准入条件。

### Heartbeat 区间覆盖

- 从当前 HAR head 向前读取最多 5000 个连续样本；
- 选择最后一个 `observed_at <= execution_started_at` 的前置样本，以及第一个
  `observed_at >= execution_completed_at` 的后置样本；
- slice 内只接受 `running/waiting`，要求 sequence、previous hash、timeout 与最大 gap 全部连续；
- 分页前后重新读取 binding 和 heartbeat head，并要求 head 的 phase、时间、timeout、detail 与末样本完全一致；
- 运行过长导致 5000 样本无法覆盖、完成后的下一 heartbeat 尚未来到，均返回 `coverage_pending`，不推断成功。

### Outcome 与持久账本

- `EvolutionRevalidationOptInExecutionOutcome` 冻结 completion/window/health/deployment/binding 引用、完整 provenance、
  completion receipt、usage、coverage samples、执行起止时间、结果和 recorded time；
- completed/partial/failed/cancelled 都可成为真实 execution outcome；只有 `CompletionReceipt.outcome=completed` 才具有
  `successful_completed_run_authority`；
- outcome ID/SHA-256 由 canonical JSON 生成，固定 Stage Completion、percentage、stable、promotion authority 为 false；
- SQLite ledger 对 `run_id` 施加唯一约束，同一真实运行不能重复计数或绑定到其他 completion；并发登记幂等收敛；
- View 动态重验 Outcome row、ChatRun、Window、Release Binding 与 exact coverage slice；来源篡改后立即撤销 authority。

## 验收

- 真实 Candidate → Deployment → Health → passing Window → managed ChatRun → Outcome 全链路通过；
- 6 个并发登记收敛为同一 Outcome；
- completed run 取得 success authority，failed run 只保留真实 outcome authority；
- completion 后缺少后置 heartbeat 时明确返回 `coverage_pending`；
- usage ID、Outcome digest 或 durable source 被篡改时读取失败关闭或 View 动态撤权；
- legacy chat run 没有 provenance/usage 时不能补造 Outcome；
- ruff、compile、公共 lazy import 与相关小模块测试通过；不运行全量测试。

## 当前边界与下一步

EVO-05.5f4c 只建立“一次真实运行”的历史事实。[EVO-05.5f4d](EVO-05-5f4d-opt-in-completed-run-aggregation.md)
已对同一 completion 的不同 `run_id` 逐条动态 inspect，并把 current passing liveness、GREEN Baseline、冻结阈值与
current Outcome 集合聚合为独立 Stage Completion Evidence。下一步仍需 exact `opt_in → percentage` Advance
Authorization；在该 authority 完成前，不向 `/evolution`、New UI/TUI 或自动调度暴露可执行的“进入 percentage”状态。
