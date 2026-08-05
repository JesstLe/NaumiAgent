# EVO-05.3f2b2b2 Fresh Interventional Cohort

## 1. 目标

把单个 Fresh RED/GREEN sample pair 扩展为 Runtime Contract 规定的连续 cohort。所有样本真实进入 Harness Sandbox、
ARC-04 Worker、current Profile checks 与绑定的 metric runner；结果形成不可变 cohort receipt，而不是由模型概括成
“评测通过”。

本切片只授予 cohort 完成事实，不授予 comparison、attribution、Final Evaluation 或 promotion authority。

## 2. 连续执行与授权

- sample index 必须严格为 `0..requested_samples-1`，缺口、重复或越界均 fail closed；
- sample receipt 以 `(contract, sample index, run scope)` 隔离；旧无 scope 表只读归档，不被重解释为新 authority；
- 一次 cohort attempt 只签发一个 cohort-scoped Run Grant，RED/GREEN 共享该 Grant；
- 每个 Profile check 的 H5a evidence 必须同时携带 `run_scope=cohort` 与完整 Run Grant digest；
- cohort 通过共享 `HarnessSandboxBatchAdmission` 获得总并发、排队和 fencing 约束；
- 总时限直接消费 Fresh Runtime Contract 的完整双 phase 预算。

## 3. 中断恢复

执行器先重读已持久化的 RED 前缀，再逐个复验对应 pair receipt、GREEN H5a、source/profile currentness 与 Run Grant
evidence。只有连续前缀可恢复；中间缺口、摘要损坏、sample scope 混入 cohort scope 均阻断。恢复 attempt 签发新的
cohort Run Grant，只执行缺失后缀，历史 Grant digest 保留在最终 receipt 中。

## 4. Cohort Receipt

Receipt 冻结：

- Contract、Validation Plan 与 immutable Source Snapshot identity；
- RED/GREEN batch、每个 sample receipt 与 H5a result digest；
- seed 序列、唯一 platform digest、全部 cohort Run Grant digest；
- 每个 metric 的配对 RED/GREEN 原始值；
- 每个 Profile check 在两侧的 passed、implementation failure、evaluation error 计数。

Receipt 自身使用 canonical JSON SHA-256 校验并事务持久化。相同 Contract 的不同 receipt 会发生冲突，而不是覆盖。

## 5. 验收标准

- 至少五个连续 paired samples 真实执行，测试场景使用七个；
- RED/GREEN 每个 sample 使用相同 cohort Grant、configuration 与 platform；
- 中断后只补跑缺失后缀，已完成样本不重复执行；
- 缺失或伪造 Run Grant digest、非连续前缀、跨 scope receipt、平台漂移均 fail closed；
- engine composition 与 lazy exports 完整；
- 聚焦 ruff、import smoke 和 sample/cohort tests 通过，不运行全量测试。

## 6. 当前边界与下一步

EVO-05.3f2b2b3 已从该 receipt 与原始 H5a 重算 paired comparison，没有相信 receipt 中预先给出的结论。随后才能
实现跨平台 Adversarial cohort、failure attribution、新 Final Evaluation、重新审批、staged rollout、监控与自动回滚。
因此本切片仍不是完整自进化闭环。
