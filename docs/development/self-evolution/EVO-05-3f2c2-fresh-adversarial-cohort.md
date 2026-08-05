# EVO-05.3f2c2 Fresh Adversarial Platform Cohort

## 1. 目标

把指定真实平台的 Fresh Adversarial sample pair 扩展为 Runtime Contract 规定的连续 cohort。该切片建立单个平台的
完整 probe evidence，但不把单平台结果冒充 required-platform matrix。

## 2. Platform-scoped 执行

- 通过共享 `HarnessSandboxBatchAdmission` 获得并发、排队和 fencing；
- 每次 attempt 只签发一个 platform-scoped Runtime Lease/Run Grant；
- 同一 sample 的 RED/GREEN 共用 Grant，所有 sample 严格为 `0..N-1`；
- 恢复前逐个复验 sample receipt、两侧 H5a、Profile、Source、平台与授权 evidence；
- 中断后新 attempt 签发新 Grant，只执行缺失连续后缀。

## 3. Cohort Receipt

Receipt 冻结 Contract/Plan/Source、platform、RED/GREEN batch、ordered sample/result/receipt digest、所有 attempt Grant，
并从 H5a 重算每个 probe check 的 RED/GREEN 状态计数与 `exit_zero` 原始值。平台 identity、check set、scope、结果摘要
或 Grant 任一不一致均 fail closed。

Receipt 明确 `cohort_complete=true`，但 `matrix_complete=false`、`comparison_authority=false`、
`promotion_authority=false`。

## 4. 验收标准

- 当前平台七个连续 sample pair 完整持久化；
- 一个正常 attempt 只有一个已撤销的 cohort Grant；
- 在 sample 2 前中断后仅补跑 2..6，最终记录两个不同 attempt Grant；
- 重复调用不要求新的父权限，复验已有证据后幂等返回；
- Engine composition、lazy exports、文档与 registry 完整；
- 只运行 Adversarial sample/cohort 相邻测试，不运行全量测试。

## 5. 下一步

EVO-05.3f2c3 将为所有 required platform 建立 matrix receipt：本地平台可直接执行，其他平台必须通过匹配平台的
ARC-04 Worker 调度；Worker 不可用时 lane 保持 pending，不允许降级为本机模拟。所有平台 cohort 完成后，才能分别
生成 H5c 并聚合 matrix authority。
