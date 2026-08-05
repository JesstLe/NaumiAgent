# EVO-05.3f2c3b2b3 Remote Platform Completion 原子收口

## 目标

把 EVO-05.3f2c3b2b2 已本地接受的完整 H5a/pair prefix 转换为 required-platform cohort，并机械关闭远端执行资源。
本切片只完成跨平台评测证据链，绝不把“平台评测完成”冒充“自进化完成”，也不授予 comparison、decision、rollout 或 promotion authority。

## 可信输入

`EvolutionRevalidationPlatformCompletionService.finalize()` 只接受同时满足以下条件的状态：

1. current Runtime Contract 明确要求该 platform；
2. durable Dispatch、latest authenticated Claim 和 exact Worker incarnation 仍可重建；
3. 本地 Result Store 中的 ingestion receipt 从 sample index 0 连续覆盖 `requested_samples`；
4. 每个 ingestion receipt 已由 Result Store 动态复验 manifest admission HMAC、Worker signature 和本地 H5a/pair binding；
5. Cohort Executor 从同一批本地 H5a/pair receipt 生成 content-addressed cohort，不信任 Worker 自报 summary；
6. prefix 引用的所有 execution authorization 均进入 durable revocation 终态；
7. Dispatch 预留的 Worker capacity 进入 released、expired 或 fenced 终态。

任一前置缺失时失败关闭。prefix 不完整时不会提前撤销 Run Grant、释放容量或创建 completion。

## 原子边界与恢复

Completion receipt 由 Control Plane HMAC 签署，并绑定 exact Contract、Dispatch、final Claim、Identity、Worker epoch、capacity terminal fact、
全部 authorization/revocation、result receipts、sample receipts 与 cohort。Store 在单一 SQLite `BEGIN IMMEDIATE` 事务中重读这些持久化依赖，
再写入 `(contract_id, platform)` 唯一 completion。

外部资源收口发生在 completion 事务之前，因此崩溃恢复遵循幂等顺序：

1. 已存在的 authorization revocation 按同一 reason 复用；
2. 已终态的 capacity reservation 复用首个 terminal fact；
3. completion 的 `completed_at` 取 cohort、全部 revocation 与 capacity terminal time 的最大值，而非调用者本地时钟；
4. 并发 finalizer 因而生成同一 content digest；
5. completion 已落盘但 Matrix 尚未刷新时，重试会继续 Matrix inspect。

## Matrix 防越权门禁

只要某 platform 存在 durable Dispatch，Matrix 就要求同一 platform completion 精确绑定同一 cohort id/digest：

- cohort 已形成但 completion 缺失：lane 为 `pending/platform_completion_pending`；
- completion 与 cohort 不一致：失败关闭；
- Matrix complete 写事务再次检查 completion，避免 inspect 与 persist 之间的竞态；
- 没有远端 Dispatch 的本地 lane 保持原有行为。

Completion view 中 `matrix_authority=false`、`comparison_authority=false`、`promotion_authority=false`。只有 Matrix 自身在所有 required lanes
闭合后形成 matrix authority，后续 H5b2/H5c、Attribution、Fresh Final 与 Fresh Decision 仍各自独立验权。

## 验收结果

- 7 个真实 sample pair 的 signed remote prefix 生成唯一 cohort/completion，并推动单平台 Matrix 完成；
- authorization durable revocation reason 为 `platform_result_completed`，Worker capacity 被释放；
- 1/7 prefix 被拒绝，且 authorization、capacity、cohort/completion 均不被提前收口；
- 8 路不同微秒时钟并发只形成一份 completion 与一份 Matrix；
- Result、Matrix 与 Completion 聚焦回归共 10 项通过；未运行全量测试。

## 真实自进化闭环后续

本切片补齐跨平台证据入口，但真实闭环尚未完成。下一最小用户可交付链按依赖顺序为：

1. EVO-05.4 immutable staged rollout plan 与 local canary executor；
2. EVO-05.5 runtime monitor，采集错误、性能、完成率、资源与用户撤回信号；
3. EVO-05.6 threshold-driven automatic rollback/abort executor；
4. EVO-05.7 accept/rollback Outcome authority，并回注 Candidate、Reflection、Feedback 与长期记忆；
5. EVO-06 从可信 Outcome 中发现下一机会，形成下一轮 Candidate，且必须经过同一完整门禁。

只有一次变更经历 Candidate → isolated mutation → RED/GREEN evaluation → signed decision → staged rollout → monitor → accept/rollback
Outcome → feedback/opportunity，并能从中断恢复、审计和回滚，才允许称为“自进化真实闭环”。
