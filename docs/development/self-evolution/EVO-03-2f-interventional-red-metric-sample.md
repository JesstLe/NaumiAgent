# EVO-03.2f Interventional RED Metric Sample

## 目标

把 EVO-03.2e 的 check-only H5a sample 升级为一次性持久化的完整 interventional sample：同一份
`HarnessEvalSuiteResult` 同时包含精确 Git baseline 上的 ARC-04 Profile-check cases，以及
`EvolutionMetricRunnerBinding` 授权的真实数值 metric cases。

本切片仍只执行一个 sample，不循环完整 RED cohort、不执行 candidate、不生成 Comparison Receipt。

## 为什么必须在持久化前合并

HAR-08 H5a 的 `(workspace, batch, suite, sample_index)` 是不可变键。先写 check-only sample、随后再覆盖
metric observation 会破坏不可变证据与并发幂等性。因此 executor 必须：

1. 完成 Profile checks；
2. 从同一 request 绑定的 baseline commit/tree 读取 validation files；
3. 执行 ready metric runner；
4. 组装一个复合 Suite；
5. 只调用一次 `record_eval_result()`。

## Runner 与源码身份

- 复合 Suite runner identity：`evolution_interventional_red@1`；
- Profile check case runner：`evolution_profile_check@1`；
- 当前 metric case runner：`self_review_static@1`；
- Suite digest 同时绑定 Request、Metric Binding、Validation Plan、Profile Binding 与复合 runner；
- Profile checks 由 HAR-08.4c 从精确 revision 物化；
- Self-Review metric 从同一 commit/tree 的 Git object database 加载 Plan 文件，复验 tree、blob、operation
  与路径安全，不读取候选脏工作树；
- platform、Profile、source commit/tree 与 repetition identity 仍进入原生 Harness baseline identity。

## 单次 metric runtime

既有 `run_self_review_static_repetitions()` 被无损拆分为：

- `run_self_review_static_sample()`：执行一次完整 typed scan，不持久化；
- repetition wrapper：按 requested samples 重复调用单次 runtime，保持既有静态 RED/GREEN 行为。

单次 runtime 重新校验 Binding timeout，完整扫描所有可信 Python paths，任何 parse/coverage/timeout 问题
都作为 eval infrastructure error 阻断，不生成伪造的 0。

## H5a 与幂等复验

复合 sample 写入 H5a 后，重复调用必须复验：

- Suite/Request/Profile/source identity；
- 复合 runner 与 authority-derived Suite digest；
- 全部 Profile check ID、顺序与 lifecycle receipt digest；
- metric case 数量、runner、metric name/order 与非空 typed observation；
- 不允许未知第三类 case 混入。

只有全部一致才返回相同 receipt。receipt v2 固定 `metrics_executed=true`。

EVO-03.2e 没有用户命令入口，但若调用内部 API 已产生 v1 check-only H5a，v2 不会覆盖或把它伪装成完整
sample，而是返回 `existing_sample_conflict`。调用方必须重新生成 Experiment/Plan/Request identity 后执行；
后续 cohort orchestrator 需要把这一状态渲染为“旧证据不可晋升，需重跑”，不能自动删除审计记录。

## 验收证据

- 当前候选工作树刻意写入一项 `broad_except`，绑定 baseline 不含该 finding；
- 真实 ARC-04 Profile check 从 baseline tree 读取预期内容并通过；
- 同一 H5a sample 的 `self_review.broad_except.count` observation 为 baseline 的 `0`，证明没有扫描候选；
- sample 同时包含一个 Profile-check case 与一个 metric case，二者均通过；
- 重复调用返回同一 receipt，不创建第二个 H5a 结果；
- 原静态 RED/GREEN repetition runtime 的定向回归保持通过；
- Profile trust 漂移仍在 authority/lease 获取前 fail-closed。

## 当前不足与下一步

- 只支持所有 metric binding 均 ready 且 verifier 为 `self_review_static` 的 Python Plan；blocked replay、feedback
  recurrence 不会被降级或伪造。
- 下一步实现连续 sample orchestrator：固定 sample index/seed，逐项调用本 executor，验证 H5a 连续前缀，
  最终生成完整 RED cohort completion receipt。
- candidate interventional cohort 必须复用相同复合 runner、Suite digest 维度、seed/order 与资源合同。
