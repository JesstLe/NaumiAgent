# EVO-03.5c Adversarial Failure Attribution

## 目标

让 EVO-03.6e 产生的 Adversarial RED/GREEN H5c 进入既有 Failure Attribution 合同，形成可持久化、可重放、
可供后续 Reflection 审查的机械归因。本切片不复制评分器或分类表，也不把归因结果直接解释为晋升决定。

## Authority Adapter

`EvolutionAdversarialFailureAttributionBuilder` 重新反序列化 Batch Request、Validation Plan 与两张 cohort
completion receipt，并在共享归因内核之前机械验证：

- Request 与 Plan 的 ID/digest、candidate ID/revision/files digest 完全一致；
- RED/GREEN 精确绑定同一个 Request、Plan、Lease、candidate、suite、sample count 与 ordered seeds；
- 两张 receipt 分别映射到 Request 中真实存在的 RED/GREEN lane，batch、order、phase 均一致；
- RED/GREEN 必须位于同一真实平台，且不得复用同一 completion receipt；
- 两组 ordered H5a Result digests 和样本数完整进入 lane-neutral attribution authority。

共享 `EvolutionFailureAttributionAuthority` 的 receipt identity 合同已扩展为接受防篡改的
`evadvcohort_*`，但静态 Self-Review 与 Interventional 前缀和行为保持不变。

## Store 与机械分类

`EvolutionAdversarialFailureAttributionExecutor` 不信任调用方传入的 H5c wrapper。它按
workspace/suite/baseline/current batch 从 Harness Store 重读不可变事实，要求完全相等后才调用现有
`EvolutionFailureAttributionKernel`。内核继续唯一负责 sample-set digest、逐样本 GREEN digest、Policy、
statistical 与 mechanical code 校验和分类，最终写入共享 `EvolutionFailureAttributionStore`。

因此静态、Interventional 与 Adversarial 三条路径使用相同 category、reason、action 与幂等冲突规则。

## 真实验收证据

- 在临时真实 Git workspace 上执行当前平台 5 个 RED 与 5 个 GREEN Adversarial samples；
- 每个 sample 实际通过 ARC-04 Worker 执行两个 Profile checks，并形成两张完整 cohort receipt；
- H5c 得到 `statistical=unchanged`、`decision=passed`，五个 mechanical verdict 均为 `unchanged`；
- 唯一机械策略将其归因为 `objective_not_improved / objective_metric_unchanged / revise_candidate`；
- `candidate_fault=false`、`retryable=false`、`requires_rerun=false`、`reflection_eligible=false`；
- 伪造 stored H5c wrapper 与把 RED receipt 复用为 GREEN 均在 attribution 持久化前被拒绝；
- 重复执行以及新 Store 实例读取返回完全相同的 attribution receipt；
- 静态 Self-Review H5c→Attribution 与 Engine composition 聚焦回归继续通过。

## 当前边界与后续依赖

本切片只归因一对同平台 Adversarial RED/GREEN cohort，不负责 Linux/macOS/Windows matrix 汇总，不实现通用
Sandbox Eval Service/Tool/UI，也不签发候选最终 Evaluation Receipt。EVO-03.7a 已能把本切片产物签发为明确非最终的
单 lane receipt；跨平台 dispatcher、HAR-08 通用 surface 与 EVO-03.7b 最终聚合仍未完成。
