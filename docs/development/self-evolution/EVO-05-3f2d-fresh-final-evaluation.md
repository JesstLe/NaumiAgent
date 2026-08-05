# EVO-05.3f2d Fresh Final Evaluation

## 目标

签发 target 前进后的全新、覆盖完整且 fail-closed 的 Final Evaluation authority。它不复用旧 Aggregation Contract 或
旧 Lane Receipt，而是直接绑定 Fresh Runtime Contract、current source、Interventional 与 required-platform Adversarial
的 cohort、H5c 和 attribution。

## 权威内容

- 完整嵌入 Fresh Runtime Contract，绑定 Candidate、Validation Plan、Source Snapshot、Profile/runner/probe 与平台集合；
- 绑定已持久化的完整 Adversarial matrix；
- Interventional：cohort identity、H5c identity/digest、完整 attribution；
- Adversarial：按 required-platform 顺序绑定每个平台 cohort、H5c identity/digest 与 attribution；
- 聚合 candidate fault、requires rerun 与 reflection eligibility；
- `evaluation_complete` 表示证据链完整，`reapproval_eligible` 表示所有归因允许继续，两者严格分离；
- Final receipt 永远不直接授予 candidate acceptance 或 promotion authority。

Store 在同一 SQLite 事务中复验 Runtime Contract、matrix、Interventional/Adversarial cohorts 和每张 attribution 的持久化
依赖。H5c 位于独立 Harness Store，Executor 在构建前通过各 comparison/attribution 入口重新验证。

## 验收标准

- Contract stale、matrix 不完整或任一 cohort/H5c/attribution 缺失时不得签发；
- platforms、comparison IDs、attribution IDs 的数量与顺序必须精确；
- 当前源码必须在签发时仍为 current；
- 完整但负向的评测可以形成 Final receipt，但 `reapproval_eligible=false`；
- 只有所有 attribution reflection eligible 且无 candidate fault/rerun 才可开放重新审批；
- 重复执行幂等，任何依赖漂移或 Store 绕过写入均失败关闭；
- 定向 Ruff、Fresh Final/attribution/comparison 回归、引擎 import 和 YAML 通过。

## 后续依赖

下一步实现 Fresh Final Evaluation 到 Promotion Package Input/Approval 的重新签发桥接。只有 `reapproval_eligible=true` 的
Fresh Final receipt 才能进入专业角色重新审批；当前测试 fixture 的 Interventional 指标 unchanged，因此正确地停在 revise
candidate，不会误入 rollout。
