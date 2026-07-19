# EVO-03.3b Interventional GREEN Cohort Request

## 目标

在执行任何 candidate 项目代码之前，把 EVO-03.2g 的完整 RED cohort、Validation Plan/Profile、Metric
Binding 与当前 active Experiment Lease 冻结为不可执行、防篡改的 GREEN 请求。该请求是后续 ARC-04
candidate cohort executor 的唯一输入合同，不自行取得权限、创建 Runtime lease、执行命令或写入 H5a。

## Authority 链

`EvolutionInterventionalGreenCohortRequestBuilder` 每次构建都会重新解析并交叉验证：

- Baseline Request、Metric Binding、Validation Plan v2 与 Profile Binding 的完整 digest 链；
- `EvolutionInterventionalRedCohortReceipt` 对上述四类 artifact、suite、batch、seed 和样本数量的绑定；
- RED receipt 已完整持久化请求规定的 sample 数，并保留相同的 ordered sample seeds；
- Experiment Lease 为 active、`worktree_ready=true`、`execution_ready=false`；
- Lease ID、Contract digest 与 baseline commit 精确匹配 Validation Plan。

任一 artifact 使用 `model_validate()` 重新计算自身 identity；调用方传入的 `model_copy()` 伪造字段不能绕过。

## Request 合同

`EvolutionInterventionalGreenCohortRequest` 防篡改绑定：

- RED Request/Completion Receipt、Metric Binding、Plan 与 Profile Binding ID/digest；
- Contract、Lease、Source Snapshot、Mutation Receipt 与 candidate ID/revision/files digest；
- 与 RED 相同的 suite、sample 数、ordered seeds、baseline commit 和执行预算；
- 独立 batch `evo:interventional-green:<plan-digest>`；
- 受管 worktree name/branch，但不保存源码、绝对路径或 secret；
- 网络和依赖安装保持禁止；
- candidate snapshot、Profile trust、相同平台、cohort Run Grant、ARC-04 Worker 与 H5a 均声明为强制前置。

请求固定 `project_code_execution_allowed=true`，表示该 lane 的目标语义；同时固定
`execution_ready=false`，表示 Request 本身不授予任何执行 authority。后续 executor 仍须重新读取 Lease、验证
候选 worktree 精确 dirty 状态并取得用户权限派生的 Run Grant。

## 验收标准

- 真实 5-sample interventional RED cohort 完成后可确定性生成 GREEN Request；
- GREEN 与 RED 的 suite、seed/order、sample 数和预算完全一致；
- Request 绑定完整 candidate provenance 与受管 Lease；
- stale/released Lease、篡改 RED receipt、篡改 Request digest 均 fail-closed；
- Engine 默认公开 Request Builder，公共 lazy import 可用；
- 构建过程没有 Runtime lease、Run Grant、Shell Worker 或 H5a 写入副作用。

## 下一步

EVO-03.3c1 已实现 candidate snapshot 的共享捕获，并让静态 GREEN 改用唯一公共实现。下一步
EVO-03.3c2 使用该 Snapshot 实现 interventional GREEN 单 sample；之后再编排连续 candidate cohort，并把
RED/GREEN 交给现有 H5b2/H5c Comparator。
