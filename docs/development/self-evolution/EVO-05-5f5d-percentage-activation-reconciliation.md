# EVO-05.5f5d Percentage Activation Reconciliation

## 目标

只消费 current [EVO-05.5f5c](EVO-05-5f5c-percentage-boot-preparation.md) Prepared Receipt，使用
[ARC-07.5a](../architecture/ARC-07-5a-installed-version-slots.md) expected-pointer CAS 将当前 selected managed
installation 原子切换到 exact candidate slot，并形成可从 immutable activation history 机械恢复的
`EvolutionRevalidationPercentageDeploymentReceipt`。

本切片只证明该 installation 的 stable launcher pointer 已切换。它不启动用户 runtime、不产生真实请求、不形成 exposure
样本，也不声明整个 percentage cohort 已完成 rollout。

## 最小 ARC 前置

ARC-07 v2 activation authority 原先只接受 opt-in Intent。5f5d 只扩展同一闭集协议，使其还接受：

- `kind=evolution_percentage_boot_preparation`；
- `authority_id=evrepercentboot_<24 hex>`；
- exact Prepared Receipt SHA-256。

kind 与 ID prefix 由模型交叉校验，不能把 opt-in ID 伪装成 percentage authority。v1 pointer JSON/digest 与既有 opt-in v2
event 保持兼容；rollback 仍禁止携带 activation authority。

## 执行链

`EvolutionRevalidationPercentageDeploymentService.deploy()`：

1. 按 Assignment ID 幂等读取既有 Deployment Receipt；
2. 读取 exact durable Intent 与 Prepared Receipt，并先查询预期 activation generation；
3. 若 generation 已存在，只允许 exact Prepared authority、previous pointer、candidate slot 与 Boot Receipt 全部匹配；
4. 若尚未激活，调用 5f5c `inspect()`，要求全部动态 activation input authority current；
5. 重读 current pointer，要求完全等于 Intent 冻结的 previous pointer；
6. 在 Intent 过期前调用 ARC-07 `activate()`，传入 expected pointer digest 与 Prepared content identity；
7. 机械验证返回的 generation、authority、previous link、slot、Boot Receipt、atomic switch 与 old-slot retention；
8. 在 Evolution DB `BEGIN IMMEDIATE` 事务内重读 exact durable Preparation 与 Intent；
9. 幂等写入 content-addressed Percentage Deployment Receipt，并返回动态 View。

参数边界只接受严格 Assignment ID；SQL 全部 parameterized。调用方不能传入 target slot、Boot Receipt、pointer digest、authority
kind、activated time 或任意命令。

## 并发与崩溃对账

`deploy()` 与 `reconcile()` 共用同一状态机：

- 多个 Service 同时观察旧 pointer 时，ARC-07 的 SQLite transaction 只允许一次 expected-pointer CAS；
- CAS 失败后必须重读预期 generation；若属于同一 Prepared authority，所有调用收敛到同一 Receipt；
- generation 被无 authority、opt-in authority 或其他 Prepared 占用时返回 `percentage_deployment_generation_conflict`；
- pointer 已切换但 Receipt 写入失败时，可从完整 hash-chain 的 historical generation 重建同一 content identity；
- 即使 Intent 随后过期或 pointer 已 rollback，历史 event 的 `activated_at` 仍必须位于原 authority window，才允许补写；
- 对账绝不重复执行 pointer switch，也不会把后续 rollback 后的 historical fact 恢复成 active authority。

Receipt 不记录“正常路径”或“恢复路径”，因为两条路径必须对同一 activation fact 形成相同 digest。

## Receipt 与动态 authority

Receipt 冻结完整 Prepared Receipt、authority-bound pointer、activation timestamp 与以下机械事实：

- selected population assignment 在激活时已执行；
- local installation pointer 已切换且 old slot 保留；
- `process_started=false`、`user_process_started=false`；
- `percentage_exposure_observed=false`；
- percentage/stable/promotion authority 均为 false。

View 将权限分为三层：

1. `deployment_fact_authority`：Receipt、Preparation、Intent durable source 与 activation chain 全部 exact；
2. `active_deployment_authority`：fact 之外，pointer 仍是 active tail，candidate bytes 与 Boot Receipt 仍 current；
3. `percentage_runtime_launch_input_authority`：active 之外，cohort Assignment、Archive Admission/build trust 与本机 target 仍
   current，才允许下一层尝试启动和记录 exposure。

Intent expiry 不抹除已经发生的历史 activation fact；但 Receipt、Preparation/Intent source 损坏、activation chain 破坏会撤销
fact authority。后续 rollback、slot 篡改或 pointer 推进只撤销 active/launch authority，不伪造历史从未发生。

## 验收结果

- 真实 signed archive → immutable slot → Percentage Intent → boot probe → Prepared → activation 全链运行；
- 两个独立 Store/Service、八个并发 deploy 调用只形成一个 Prepared-bound generation 和一个 Receipt；
- candidate runtime bytes 篡改后，pointer fact 保留但 active/launch authority 动态撤销；
- pointer 切换后注入 Receipt write failure，再 rollback 且等待 Intent 过期，reconcile 仍从历史 generation 补写同一 Receipt；
- 无 authority 的相同 candidate activation 不能被本 Intent/Preparation 认领；
- Prepared durable JSON 篡改会撤销 deployment fact authority；
- ARC-07 authority kind/ID mismatch 失败关闭，既有 opt-in v2 与 v1 pointer 行为不变；
- 只运行本模块和直接受影响 ARC-07 小测试，未运行全量测试。

本机真实链路使用 macOS/POSIX runtime fixture；Windows target/schema 映射由 ARC-07 既有测试覆盖，但 Windows 真 `.exe`
activation 仍需在 Windows CI runner 上完成端到端验证，不能由本机结果替代。

## 当前边界与下一步

5f5d 已完成 selected installation 的可对账 activation，但 stable launcher 尚未为 percentage 路线启动受管 runtime，也没有真实
exposure ledger。下一最小切片为 `EVO-05.5f5e Percentage Runtime Launch and Exposure Receipt`：消费 current
`percentage_runtime_launch_input_authority`，绑定 ARC-07.5e runtime identity 与 HAR heartbeat/provenance，记录 exact
installation 首次真实 exposure；它仍不能凭单机样本声明 cohort health 或 stage completion。
