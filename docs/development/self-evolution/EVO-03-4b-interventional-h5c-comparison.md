# EVO-03.4b Interventional RED/GREEN H5c Comparison

## 目标

把真实 ARC-04 RED/GREEN completion receipts 与两组 ordered H5a records 收敛为原生 HAR-08 H5c
Comparison Receipt。该切片不建立 Evolution 私有分数、不让模型解释或覆盖 verdict，也不执行 promotion。

## 共享 H5b2/H5c 内核

`EvolutionComparisonKernel` 从既有 Self-Review comparator 抽取唯一持久化路径：

1. 将 authority 已验证的 RED batch 注册为 H5b2 `comparison_reference`；
2. 确认相同 batch 未被占用为 promotion Baseline；
3. 用 exact `sample_index + result_sha256 + typed Result` 调用原生 `build_eval_comparison_receipt()`；
4. 由 H5c Store 在事务内复核 Baseline/Candidate 外键、identity、sample-set digest 和逐样本 evidence；
5. 幂等恢复必须与刚构建 receipt 完全相同，冲突或损坏以稳定错误码失败。

静态 Self-Review comparator 已迁移到该内核并保持原有测试。Interventional comparator 只实现 lane-specific
authority gate，不复制机械、Policy、统计或持久化算法。

## Interventional Authority Gate

Executor 每次比较都重新验证：

- Baseline Request、Metric Binding、Validation Plan v2 与 Profile Binding；
- RED completion receipt 对 Request/Binding/Plan/Profile、baseline commit/tree、suite/batch、seed、sample 的绑定；
- GREEN Request 对完整 RED receipt、candidate provenance、Lease ID、suite/seed/order 的绑定；
- GREEN completion receipt 对 Request/RED/Binding/Plan/Profile/Lease/candidate identity/tree 和 ordered result
  digests 的绑定；
- 两组 H5a sample index 从 0 连续、数量精确且逐项 digest 匹配 completion receipt；
- 两组内部各自只有一个 Identity，configuration/platform 完全相同；RED source 是 clean baseline，GREEN
  source 是相同 commit 上的 dirty candidate fingerprint；
- 每个 sample 的 Profile checks、typed metrics、ARC-04 lifecycle、`run_scope=cohort` 与 Run Grant digest 完整；
- completion receipt 中 check/metric summaries、grant digest 集合和 completed_at 可由 H5a 机械重算。

所有 lane authority、H5a、identity、case 与 summary gate 都在注册 comparison reference 前完成。H5b2 与 H5c
是两个各自幂等的权威事务：若 reference 已成功注册而后续 H5c 构建或写入失败，合法 reference 会保留，重试
复用它继续生成 H5c；系统不会伪装成跨两张 authority 表的回滚事务。

## Verdict 语义

最终返回 `HarnessStoredEvalComparisonReceipt`，原样保留：

- statistical verdict/code；
- 每个 GREEN sample 的 mechanical、Policy、violation evidence；
- `passed|failed|flaky|inconclusive|incompatible` 总体 decision；
- 两组 ordered sample-set digest 与 immutable receipt digest。

“cohort 执行完成”只表示证据完整，不表示 candidate 改进。本轮真实 fixture 中 RED 的 Profile check 通过且
metric 为 0，GREEN 的 check 为 implementation failure 且 metric 为 1，因此 H5c 正确得到
`statistical=regressed`、`decision=failed`。

## 验收证据

- 真实 Git baseline、受管 candidate worktree、两组各 5 个 ARC-04 samples 后形成 H5c；
- RED 注册为 `comparison_reference`，active selector 保持为空；
- 每个 GREEN sample 的 Policy verdict 均为 failed，统计 verdict 为 regressed；
- 完整重试返回同一 reference 和 H5c receipt，不覆盖首次事实；
- 篡改 GREEN completion candidate revision 在 H5b2 注册前失败；
- 静态 Self-Review comparison 定向回归保持通过；Engine 与 lazy public exports 可用。

## 下一步

EVO-03.5b 应让现有 Failure Attribution 消费 Interventional completion receipts + 原生 H5c，而不是复制归因
映射；随后横向评估 EVO-03.6 adversarial suite 与 HAR-08.4 Suite/Batch 隔离完成度。当前不得进入自动晋升、
源码合并或 EVO-04 reflection decision。
