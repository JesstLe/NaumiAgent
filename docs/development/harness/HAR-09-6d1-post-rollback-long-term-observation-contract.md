# HAR-09.6d1 Post-Rollback Long-Term Observation Contract

## 目标

在 `HAR-09.6c2b1` 的完整 Post-Rollback Behavioral Matrix 与 `HAR-09.6c1` 的 fresh installed-runtime
verification 之上，建立长期观察的第一份不可变 policy artifact。该契约回答“哪一个已恢复 baseline、从何时开始、
按什么样本和 censoring 规则观察”，但不提前读取 heartbeat ledger，也不把契约本身冒充长期健康结论。

## 前置证据与精确绑定

`EvolutionPostRollbackLongTermObservationContract` 必须精确绑定：

1. rolled-back Outcome ID/SHA 与 Rollback Request；
2. recovery verdict 为 `recovered` 的完整 Behavioral Matrix ID/SHA；
3. Matrix 内的 Before/After Evidence 与原 Final Evaluation ID/SHA；
4. Post-Rollback Runtime Verification ID/SHA；
5. baseline slot、manifest、version、target、rollback pointer generation 与 backend binary SHA；
6. Verification 自身的 runtime identity digest。

这里有意保留两种 identity 语义：Verification digest 由 rollback recovery probes 的公共字段生成，而后续
Harness `ReleaseRuntimeIdentity` 还包含 runtime path、install root 等 managed-runtime 字段。HAR-09.6d2 admission
必须逐字段验证共同的 slot/pointer/binary baseline，不能错误地直接比较两个不同 schema 的 digest。

## 冻结的 v1 观察规则

- 可接受 runtime surface：`new_ui` 或 `tui`；
- observation chain 必须是真实 startup origin，origin sequence 固定为 1；
- operational phase：`running`、`waiting`；
- breach phase：`failed`；
- censoring phase：`draining`、`stopped`；
- 最短观察时间：3600 秒；
- 最少 operational sample：12；
- 单契约最多消费 5000 个 sample；
- sample 不得早于 Matrix/Runtime Verification 两者中较晚的 evidence time；
- 相邻样本最大间隙与最新样本年龄均不得超过该 chain 冻结的 heartbeat `timeout_seconds`；
- runtime binding、sequence 与 previous-sample hash 必须在窗口内保持 exact/连续。

固定时长和固定样本数同时成立，避免仅靠高频 pulse 快速“刷满”窗口，也避免一小时内只有首尾两个样本仍被误判为
持续健康。timeout 规则保留不同主流终端的合法 heartbeat cadence，同时由后续评估机械应用而不是由模型解释。

## 持久化与并发

Store 与 Matrix、Runtime Verification 共用 session SQLite：

- `BEGIN IMMEDIATE` 内重新读取 exact Matrix/Verification ID 与 SHA；
- 每个 Outcome、Request、Matrix、Verification 只能绑定一个契约；
- 完全相同的并发写入必须收敛到相同 content-addressed artifact；
- lineage 或 policy 不同则 conflict，不能 last-write-wins；
- durable row 的索引字段与 JSON content identity 任一不一致均失败关闭。

Service 在记录前后动态调用 Matrix/Runtime Verification 的 `inspect()`。任一上游 authority 因 active baseline、
签名证据、lane 或 durable source 变化而失效，契约 view 立即变为 `stale`，不得继续作为窗口输入。

## 双通道与权限

- Agent Tool：`evolution_post_rollback_observation_contract`；
- 共享 Slash：`/evolution outcome-observation-contract <rollback-request-id>`；
- CLI、New UI 与 TUI 继续走同一 Slash/Tool/Service；
- 该操作只生成治理 artifact，不启动进程、不发送远端任务，Moderate 与 Bypass 均不二次确认。

用户可见回执明确展示 Outcome、Matrix、Runtime Verification、baseline、surface、时长、样本、gap、censoring，
并明确标示 long-term metrics、window、learning、promotion、execution authority 均为 false。

## 验收标准

- [x] 模型严格拒绝未知字段、非 canonical workspace、缺失 surface/phase 规则和 policy 篡改；
- [x] contract ID/SHA 由完整 canonical payload 内容寻址；
- [x] 只接受 `recovered` Matrix 与 exact Runtime Verification lineage；
- [x] Store 同事务复验 durable Matrix/Verification；
- [x] 并发独立 Service 实例写入收敛为单行；
- [x] durable row 篡改读取失败关闭；
- [x] Matrix 或 Runtime Verification 动态撤权后 contract authority 同步撤销；
- [x] Agent Tool 与 Slash 共用 Service，Moderate/Bypass 无二次确认；
- [x] 相关小模块 pytest、ruff、compile 与 import 验证通过；未运行全量测试。

## Authority 边界与下一步

本切片固定 `observation_contract_recorded=true`，同时固定：

- `long_term_metrics_recorded=false`；
- `observation_window_authority=false`；
- `learning_authority=false`；
- `promotion_authority=false`；
- `execution_authority=false`。

`HAR-09.6d2` 已建立 managed runtime admission：把契约 baseline 逐字段绑定到 exact
`HarnessRuntimeReleaseBinding`，只接纳 startup-origin 的 append-only heartbeat chain，并生成可撤权 observation input。
下一独立切片 `HAR-09.6d3` 再按本契约聚合 duration、coverage、gap、breach/censoring 与 sustained-health verdict；任何长期结论都不得
回写或扩大原 Behavioral Matrix。
