# EVO-03.3c2c1 Shared Interventional Cohort Governance Kernel

## 目标

在实现 GREEN 连续 cohort 前，把 RED 已经真实验证过的高权限 cohort 编排收敛为唯一内核，避免 RED/GREEN
各自维护 Runtime lease、Run Grant、连续 H5a 前缀、恢复和清理状态机。该切片不新增评测结论，也不扩大
candidate 权限。

## 共用边界

`EvolutionInterventionalCohortKernel` 只负责以下 phase-neutral 治理事实：

1. 校验 RED/GREEN phase、SHA-256 authority key、5..100 样本和 60..3600 秒总预算；
2. 加载 H5a records，要求 sample index 是从 0 开始的连续前缀；
3. 调用 phase-specific validator 重验已有 sample receipt，并要求每个 receipt 的 `sample_index` 与 H5a
   record 一一对应；
4. 完整 cohort 在读取父权限前幂等返回；未完成 cohort 才要求父回执显式委托 `bash_run`；
5. 用一个 Runtime lease 和一个可撤销 Run Grant 执行本轮缺失 samples；跨进程恢复使用更高 lease epoch 和
   新 grant，已有前缀保持不可变；
6. 无论成功、受控失败还是 sample 异常，都撤销本轮 grant 并释放 Runtime lease；
7. 完成后重新加载 H5a、重验连续前缀、sample receipts 和 phase-specific run evidence，再构建 completion
   receipt。

内核不理解 RED baseline、GREEN candidate、Profile checks、metrics 或 Snapshot；这些仍由 phase executor
提供的回调验证和执行。因此抽取不会把 candidate authority 下沉到通用 Runtime，也不会形成新的旁路。

## RED 迁移

`EvolutionInterventionalRedCohortExecutor` 继续负责完整 RED authority、H5a record 内容、cohort-scoped grant
evidence 与 completion receipt；Runtime lease/Run Grant/恢复循环改由共用内核承载。既有 owner/idempotency
identity、错误码、`cohort_finished` 撤销原因和幂等行为保持稳定。

## 验收证据

- 真实 RED cohort 在第 3 个 sample 前中断后保留 `[0,1]`，301 秒后用新父回执、新 lease epoch/grant
  完成 `[0,1,2,3,4]`；完成态无需父权限即可幂等重放。
- 两轮 cohort grant 均撤销，Runtime lease 均释放，RED check/metric/lifecycle evidence 不变。
- naive clock、非法 owner token 在 Runtime lease 前以稳定中文错误码失败。
- receipt 数量正确但 `sample_index` 错位时，在读取父权限前失败。

## 下一步

EVO-03.3c2c2 以该内核编排 GREEN 连续 samples：每次执行仍由 GREEN sample executor 重验完整 RED H5a、
active Lease 与 Candidate Snapshot；cohort completion receipt 还必须证明所有 GREEN sample 使用同一 candidate
identity、同一 configuration/platform/seed/order，并支持中断前缀安全续跑。完成后才进入 interventional
H5c comparator。
