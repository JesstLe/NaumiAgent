# EVO-05.5f4b Durable Opt-in Liveness Assessment

## 目标

把 EVO-05.5f4a 的纯 liveness-window 合同接入真实持久层和动态 View。该切片负责从 HAR-10.2j 账本有界取证、保存
content-addressed Window Receipt，并在每次 inspect 时重新验证 active Deployment、Runtime Health、release binding 与最新
heartbeat head。它仍只证明 exact opt-in runtime 的持续存活，不把 heartbeat 冒充 completed run 或 Stage Completion。

## 持久证据边界

`EvolutionRevalidationOptInObservationWindowStore` 与 Runtime Health 共用证据数据库，并在写入前独立验证：

1. Runtime Health durable source 与窗口内嵌 Receipt exact 一致；
2. HAR 当前 Binding 与窗口内嵌 Binding exact 一致；
3. 窗口的每个 sample 都能从 HAR append-only ledger 按 sequence/cursor 读回；
4. 同一 `window_id` 只能幂等复用相同内容；
5. Receipt 上限为 8 MiB，持久表按 completion、subject、assessed time 保留 append-only 历史。

Health JSON 在 SQLite 事务内以 UTF-8 bytes 做 constant-time exact 比较。外部来源不可读、被清理或发生冲突时使用稳定
错误码拒绝写入，不把底层 SQLite/HAR 异常泄露给用户。

## 有界分页与并发

Service 先冻结当前 heartbeat head，再读取以该 head 结尾的最多 5000 个连续 HAR samples；每页最多 500，cursor 必须
前进，page 必须绑定同一 Binding。即使长寿命 runtime 的历史超过 5000 条，也会评估有界连续尾窗而非永久失败。读取结束后
再次验证：

- 最新 durable heartbeat 与最后一个 observation 的 workspace、subject、instance、epoch、sequence、phase、时间、timeout 和
  detail exact 一致；
- 分页前后的 Binding 没有变化。

同一进程内相同 completion/subject 使用锁串行评估；跨进程写入依靠 content identity 和 SQLite `BEGIN IMMEDIATE` 防止
同 ID 冲突，不同 assessed time 的合法并发历史仍可分别保留。同一 ledger head 且 status/reasons 未变化时复用最新 Receipt，
避免轮询产生无意义的时间戳历史；新样本或状态变化才 append。并发期间出现新 heartbeat 不会被旧 Receipt 隐藏：返回 View
会重新读取当前 ledger，旧 Receipt 只保留历史事实。

## 动态 View

View 同时返回持久 `receipt` 与按当前时间重建的 `current_assessment`，并机械投影：

- Window source、latest Receipt、Health source、Health authority、active Deployment、Binding、ledger 全部 current，且当前
  assessment 为 passing，才开放 `runtime_liveness_window_authority`；
- 同一组动态依赖全部 current，且当前 assessment 为 breached，才开放 pause/rollback input；
- 新 Window 出现后，旧 Receipt 的 latest authority 立即失效；
- pointer/control/enrollment/build trust 漂移通过 Runtime Health/Deployment View 传递并撤权；
- ledger 新增 failed/gap/stale 无需先生成新 Receipt，inspect 即可看到动态 breach；
- completed-run、stage completion、percentage、stable、promotion authority 始终为 false。

## 验收结果

- 使用真实 Candidate Bundle → CAS Deployment → health subprocess → runtime identity/binding → SQLite heartbeat ledger；
- 真实写入超过 500 条 samples，跨至少两页形成并持久化 passing Window；
- 构造超过 5000 条的有效 hash chain，读取结果严格保留以冻结 head 结尾的 5000 条连续尾窗；
- 6 个并发 assessment 幂等收敛为同一 Receipt；
- Receipt 形成后追加 failed heartbeat，inspect 无需写新 Receipt 即动态变为 breached；
- 再次 assess 持久化 breach 后，旧 Receipt 因 newer window 撤权；
- active pointer rollback 后，Health/Deployment 动态失效，liveness 与 pause/rollback input 同时撤权；
- ruff、compile、公共 lazy import 与小模块真实测试通过；未运行全量测试。

## 当前边界与下一步

EVO-05.5f4b 已完成“运行时是否持续存活”的持久动态闭环，但没有真实用户任务结果。
[HAR-10.2k](../harness/HAR-10-2k-release-bound-chat-run-provenance.md) 已完成下一步前置：把每个 managed terminal run
绑定到 exact deployment/release identity，但明确不授予 outcome authority。下一最小切片应建立 release-bound execution
outcome ledger：每个 terminal run 必须再绑定 completion、覆盖执行区间的 observation、真实执行起止时间、结果、成本和
不可变摘要；随后才能按 `minimum_completed_runs` 聚合独立 opt-in Stage Completion Evidence。Windows runner 的真实 health
subprocess 夹具仍需在 Windows CI 单独验收。该内部证据 Service 尚未接入 `/evolution`、New UI/TUI 或自动调度入口；应在
execution outcome 与 Stage Completion 权限链闭合后统一暴露，避免提前展示不可执行的“完成”状态。长期 Receipt retention/export
策略也仍需后续治理切片定义。
