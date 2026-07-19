# EVO-03.2g Interventional RED Cohort

## 目标

把 EVO-03.2f 的单个复合 sample 编排为完整、连续、可恢复的 RED cohort。cohort 在一次执行尝试中只取得
一个 Runtime lease 与一个 Run Delegation Grant，所有缺失 sample 复用该 authority；父权限回执不需要在
每个 sample 重新签发。

## Authority 生命周期

1. 在任何 lease/permission 写入前重新解析 Plan、Profile Binding、Baseline Request 与 Metric Binding；
2. 读取 H5a 的现有 `(batch, suite)` 前缀，索引必须严格为 `0..N-1`；
3. 对每个已有 sample 重验 Suite/source/Profile/check/metric/lifecycle/run-scope identity；
4. 若已完整，直接从 H5a 重建相同 completion receipt，不读取父权限；
5. 若不完整，校验父回执和 `bash_run` 委托范围，取得唯一 owner 的 Runtime lease；
6. 签发最长覆盖 Experiment 总时长预算、受 lease fence 限制的 cohort Run Grant；
7. 缺失 sample 使用 typed `EvolutionInterventionalRedRunAuthority` 消费同一个 grant；
8. 成功、失败、取消和异常都撤销 grant 并释放 lease。

单 sample 仍保留自持有 authority 的兼容模式，但这类 `run_scope=sample` H5a 不得被 cohort completion receipt
冒充为 cohort-scoped evidence。

## 300 秒父回执窗口

Run Grant 只在 cohort 启动时要求父回执不超过 300 秒。后续 sample 的 Shell admission 校验的是仍有效、
未撤销且 lease fence 匹配的 Run Grant。因此长 cohort 可以跨过父回执窗口，不需要重复询问用户或创建
新的 policy receipt。

真实验收将时钟在第一个 sample 后推进 301 秒，第二个 sample 仍使用原 cohort grant 成功执行。

## 连续前缀与恢复

- 每个 sample 完成后立即形成独立不可变 H5a 记录；
- 中断不会回滚已完成 sample，也不会跳过失败索引；
- 下一次执行先重验已有前缀，再从第一个缺失索引继续；
- 每次恢复执行使用新的 owner/lease epoch/Run Grant；
- completion receipt 保存所有执行尝试的去重、排序 grant digest，并要求每个 check case 都明确
  `run_scope=cohort`；
- 同一执行尝试内的 sample 共享 grant，跨中断恢复允许多个 grant digest，不能伪称只有一次执行。

## Completion Receipt

`EvolutionInterventionalRedCohortReceipt` 防篡改地绑定：

- Request、Metric Binding、Plan 与 Profile Binding ID/digest；
- Suite/Batch、baseline commit/tree；
- requested/persisted sample 数、完整 seed 序列；
- ordered sample receipt/result digests；
- 所有 cohort Run Grant digests；
- 每个 metric 的 unit/direction/target 与 ordered sample values；
- 每个 Profile check 的 passed/failed/evaluation-error 总数；
- continuous index、Profile trust、exact revision、ARC-04、项目代码和 metric 执行事实。

Receipt 只表示 cohort 证据完整，不把 metric 未达标或 Profile check implementation failure 偷换成
infrastructure failure；后续 Comparator 再决定候选是否改善。

## 验收证据

- 5 个真实 macOS ARC-04 Shell Worker sample 连续写入 H5a `0..4`；
- 第一个 sample 后时钟推进 301 秒，第二个 sample 仍由原 Run Grant admission；
- 第三个 sample 前模拟进程中断，H5a 保留 `0,1`，第一 grant/lease 被回收；
- 新父回执、新 owner/epoch/grant 从索引 2 恢复到 4；
- completion receipt 保存两个 grant digest、5 个唯一 sample receipt digest、5 个 baseline metric `0`；
- 完整 cohort 再次执行不读取不存在的父权限，返回相同 receipt；
- authority artifact 篡改在取得 Runtime lease 前 fail-closed；
- 所有终态 Runtime lease released、Run Grant revoked。

## 当前不足与下一步

- 当前 interventional lane 仅支持 ready `self_review_static` Python metric；blocked verifier 继续 fail-closed。
- RED cohort 已完整，下一步设计 candidate interventional request/cohort：必须复用相同 sample seeds、复合
  Suite config、Profile checks、metric runners、平台和预算，且从 active Experiment Lease 的 candidate
  snapshot 执行。
- RED/GREEN 都完成后再接入现有 H5b2/H5c Comparator，不另造比较管线。
