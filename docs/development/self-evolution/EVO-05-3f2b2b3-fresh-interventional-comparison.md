# EVO-05.3f2b2b3 Fresh Interventional Comparison

## 1. 目标

把 Fresh Interventional cohort 的 RED/GREEN 原始 H5a 转换成 HAR-08 原生 H5b2 comparison reference 与 H5c
Comparison Receipt。比较结论由机械策略和统计实现产生，模型不能改写 verdict。

## 2. Fresh Authority Gate

执行前重新读取并验证：

- Runtime Contract 仍为 current/ready；
- cohort receipt 的 Contract、Validation Plan、Source Snapshot、suite 和 sample count 完全匹配；
- RED/GREEN H5a 都是连续 `0..N-1`，且每项 result digest 与 cohort receipt 一致；
- 两组各自 Identity 统一，configuration、platform、commit 相同；RED 为 clean source，GREEN 为 dirty overlay；
- cohort 的每个 metric 值和 check 状态计数可由当前 H5a 完整重算。

所有 gate 都在注册 H5b2 reference 前完成。缺失、篡改或 stale evidence 不会产生半成品比较 authority。

## 3. 原生 H5c

通过共享 `EvolutionComparisonKernel`：

1. 把 RED batch 注册成 `comparison_reference`，不改变 active promotion baseline；
2. 用 ordered `sample_index + result_sha256 + typed result` 构造 H5c；
3. 原样保留 statistical verdict、mechanical/policy evidence、violation codes 与最终 decision；
4. 重试必须返回相同 H5b2/H5c，冲突时 fail closed。

## 4. 验收标准

- 七对真实 Fresh H5a 生成一个可幂等恢复的原生 H5c；
- active baseline 不被 comparison reference 污染；
- stale Contract、缺一条 H5a、摘要/identity/summary 不一致均在 H5b2 注册前阻断；
- Engine composition 与 lazy exports 完整；
- 只运行 comparison/cohort/sample 聚焦测试，不运行全量测试。

## 5. 当前边界

H5c decision 只表示 Interventional lane 的机械比较结论，不等于完整 Final Evaluation 或 promotion。下一切片必须
执行 Fresh Adversarial probes 与 required platform lanes；之后才可进行 failure attribution、Final Evaluation、重新审批、
staged rollout、监控与自动回滚。
