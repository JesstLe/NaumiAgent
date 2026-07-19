# EVO-03.2a Baseline Cohort Request

## 目标

把 EVO-03.1a Validation Plan 与 EVO-03.1b 可信 Profile Binding 编译成 HAR-08 可消费的 baseline RED
cohort 请求，固定样本数量、seed 序列、Suite/Batch identity、Git baseline、环境摘要、检查顺序和总预算。

本切片只生成不可变请求，不物化 worktree、不运行 Profile check、不写 Harness Result Store，也不产生
Baseline 或 Comparison Receipt。

## 与 HAR-08 的对齐

HAR-08 H5a/H5c 要求：

- batch ID 安全且同 cohort 唯一；
- sample index 从 0 连续递增；
- cohort 内 Identity 完全一致；
- 至少 5 次样本才能进入统计 Comparator；
- Result Store 保存每个 typed sample，Comparison Receipt 只引用两组精确 cohort。

`EvolutionBaselineCohortRequest` 因此默认请求 5 个样本，允许范围 5..100，并固定：

- `suite_id=evo_<validation-plan-digest-prefix>`；
- `batch_id=evo:red:<validation-plan-digest-prefix>`；
- `continuous_sample_indexes_required=true`；
- `runtime_identity_required=true`；
- `harness_result_store_required=true`；
- `har08_comparison_receipt_required=true`。

请求不创建平行 Result/Comparator schema，后续 adapter 必须产出既有 `HarnessEvalSuiteResult` 与 H5 artifact。

## Authority

Builder 重新验证：

- `EvolutionExperimentContract`；
- `EvolutionValidationPlan`；
- `EvolutionValidationProfileBinding`。

Contract ID/manifest、Candidate revision、seed、baseline commit、Plan ID/digest、Binding ID/digest 和 Profile
digest 必须一致。Binding 必须 ready 但仍不可执行。任何通过 `model_copy/model_construct` 注入的嵌套摘要
漂移会在构建前重新解析失败。

## Baseline source 与环境

请求绑定：

- Git baseline commit 与 tree digest；
- Profile、Experiment config 与 Toolset digest；
- materialization 固定为 `arc04_ephemeral_git_worktree`；
- network 与 dependency installation 固定关闭；
- Profile trust 必须在每次实际执行前重验。

请求不保存绝对 workspace/worktree 路径。ARC-04 worker 后续只能从精确 commit 创建短生命周期 baseline
环境，不能复用已写入 candidate 的脏 Lease worktree。

## 样本与 seed

每个 sample seed 由 Contract seed、Validation Plan digest 与 sample index 进行 SHA-256 派生，形成稳定、唯一、
有序序列。Candidate cohort 后续必须复用同一序列，禁止重新抽 seed 或挑选最好结果。

## Checks、Metrics 与预算

- Profile checks 按 Binding check ID 稳定排序；
- 每个 check 保存 spec/argv digest、timeout 与完整 path/check-kind coverage；
- Request 携带 Binding requirement digest，并复算全部 coverage，偷删一个 verifier 即失败；
- Metrics 保存 direction、target、verifier 与 procedure digest，baseline operation 固定为 `measure`；
- 所有唯一 Profile check timeout 之和乘样本数不能超过 Experiment 总时长预算。

Contract 尚未给 metric verifier 独立 timeout，因此请求固定 `metric_timeout_binding_required=true` 且
`execution_ready=false`。不能用“Profile checks 在预算内”冒充整个 cohort 已可执行。

## 固定安全状态

- `phase=red`；
- `candidate_request_allowed=false`；
- `request_ready=true`；
- `arc04_worker_required=true`；
- `execution_ready=false`。

Baseline cohort 尚未持久化完成前，不允许生成 GREEN request。

## 验收证据

- 真实 Git Mutation Receipt→Validation Plan→可信 Profile Binding 生成确定性 RED Request；
- 5 个 seed 稳定、唯一且与 Plan identity 绑定；
- Suite/Batch ID 可进入现有 HAR-08 H5 key 约束；
- 检查最坏 timeout 精确汇总，6 个样本超过 Contract 预算时拒绝；
- 少于 5 个样本、seed 篡改、coverage 缺失和嵌套 artifact 漂移全部 fail-closed；
- Request JSON 不含 argv、源码或绝对 worktree 路径；
- Builder 前后主工作树与隔离 worktree 状态不变；
- Engine 组合 Builder，3.1a/3.1b 与 Mutation Receipt 聚焦回归继续通过。

## 后续状态

- EVO-03.2b 已实现 metric verifier runner identity、fixture 类型与独立 timeout 预算绑定，详见
  `EVO-03-2b-metric-runner-binding.md`；
- Safe Replay 经审计属于非干预型 runner，不能证明代码修复效果；feedback recurrence 仍缺可信
  observation-window runner，二者均保持 blocked；
- EVO-03.2e/2f 已将单个 sample 的全部有序 Profile checks 与 ready typed metric observations 接入精确
  baseline commit/tree、Run Grant、ARC-04 Worker 与 H5a；完整 cohort 循环仍未实现；
- EVO-03.2c 已为纯 `self_review_static` Request 实现精确 Git baseline 扫描、连续 H5a sample 与防篡改
  completion receipt；执行 Profile checks 或项目代码的完整 Request 仍等待 ARC-04；
- EVO-03.3a/3.4a 已保持同 metric/seed/order/平台合同生成静态 GREEN H5a cohort，并接入 H5b2/H5c 原生
  Comparison Receipt，EVO-03.5a 已机械持久化 Failure Attribution；下一步实现连续 interventional RED cohort。
