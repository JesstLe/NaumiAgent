# EVO-03.5b Interventional Failure Attribution

## 目标

让真实 ARC-04 Interventional RED/GREEN completion receipts 与原生 H5c Comparison 进入既有 Failure
Attribution 合同。该切片不复制分类表、不引入模型判断、不把归因等同于反思或晋升。

## 统一归因内核

`EvolutionFailureAttributionKernel` 只消费已经解析完成的 lane-neutral authority：

- Validation Plan、candidate ID/revision；
- RED/GREEN completion receipt ID/digest；
- suite、RED/GREEN batch、样本数与 ordered H5a Result digests；
- Harness Store 中不可变 H5c wrapper 与内部 receipt。

内核重新验证 wrapper/internal identity、batch、sample-set digest 和逐样本 GREEN digest，然后调用唯一的机械
`_classify()` 映射，生成既有 `EvolutionFailureAttributionReceipt`。静态 Self-Review Builder 已迁移到同一内核，
所以两个 lane 不存在独立 Policy、reason code 或 action flags。

## Interventional Authority Adapter

`EvolutionInterventionalFailureAttributionBuilder` 严格反序列化并验证：

- RED/GREEN receipt 自身 canonical digest 与 identity；
- 两者绑定同一 Validation Plan ID/digest；
- GREEN 精确引用 RED receipt ID/digest；
- GREEN candidate ID/revision 与 Plan 一致；
- RED/GREEN suite 一致。

`EvolutionInterventionalFailureAttributionExecutor` 按 workspace/suite/baseline/current batch 从 Harness Store
重新读取 H5c。调用方提供的 wrapper 只要与当前不可变事实不完全相同，就在 Builder 和 Store 写入前失败。
最终 receipt 仍写入共享 `EvolutionFailureAttributionStore`，以 comparison ID 唯一约束保证跨重试、跨进程幂等。

## 真实验收证据

- 在真实 Git baseline 与受管 candidate worktree 上执行 5 个 RED 和 5 个 GREEN ARC-04 samples；
- H5c 得到 `statistical=regressed`、`decision=failed`，全部 GREEN Policy verdict 为 failed；
- 同一机械映射得到 `candidate_defect / candidate_policy_failed / revise_candidate`；
- `candidate_fault=true`，`retryable=false`，`requires_rerun=false`，`reflection_eligible=false`；
- 篡改 H5c stored wrapper 和 GREEN candidate revision 均在持久化前被拒绝；
- 重复执行及新 Store 实例读取返回同一 attribution receipt；
- 静态 Self-Review H5c→Attribution 回归继续通过，Engine 默认组合两个 lane 的 executor。

## 当前边界与后续依赖

本切片只完成 H5c 后的可信归因，不实现 adversarial/security/platform matrix、最终 Evaluation Receipt、
EVO-04 reflection decision 或自动晋升。下一步应横向核对 EVO-03.6 与 HAR-08.4 Suite/Batch 隔离：先补能让
adversarial suite 复用同一 H5a/H5c authority 的最小前置，再实现 adversarial evidence，不另造 Evolution runner。
