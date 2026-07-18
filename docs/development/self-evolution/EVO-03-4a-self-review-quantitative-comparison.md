# EVO-03.4a Self-Review Quantitative Comparison

## 1. 目标

把 EVO-03.2c RED 与 EVO-03.3a GREEN 的完成回执、H5a 样本和 Validation authority 收敛为第一份真实、
不可篡改的 RED→GREEN 数值结论。最终权威直接使用 HAR-08 H5c
`HarnessEvalComparisonReceipt`，不建立 Evolution 私有评分或让模型改写 verdict。

## 2. Authority 重验

`EvolutionSelfReviewComparisonExecutor` 每次执行都会重新解析并交叉验证：

- Baseline Request、Metric Binding 与 Validation Plan v2；
- RED receipt 对 Request/Binding/Plan、suite/batch/sample 的完整绑定；
- GREEN Request 对 RED receipt、Plan、candidate、seed/order/sample 的完整绑定；
- GREEN receipt 对 Request、RED、Plan、Lease、candidate tree 和 H5a result digest 的完整绑定。

任一嵌套 artifact 的 ID、digest、candidate revision、sample count、suite 或 batch 漂移都会在注册 RED
reference 前失败。

## 3. H5a 与 Identity 对照

Executor 从 Store 重新读取两组有序样本，而不是只相信 completion receipt：

- sample index 必须从 0 连续且数量精确；
- 每个 Result digest 必须按顺序匹配 RED/GREEN receipt；
- 两组内部各自只有一个 Identity；
- configuration 和 platform 必须完全相同；
- RED source 必须是 Request baseline commit/tree 且 clean；
- GREEN source 必须是 GREEN receipt candidate HEAD/tree 且 dirty。

验证通过后，RED 通过 HAR-08.H5b2 注册为 `comparison_reference`；它不会切换 active selector。随后把两组
typed H5a samples 交给原生 H5c builder 和 Store，机械生成/持久化 statistical、mechanical、Policy 与逐样本
证据。

## 4. 结果语义

Executor 返回 `HarnessStoredEvalComparisonReceipt`：

- modify 修复 broad exception 的真实五次 RED=1/GREEN=0 得到 `statistical=improved`、`decision=passed`；
- create 空 baseline 与干净新文件均为 0，得到 `unchanged/passed`，不会虚构“改进”；
- regressed、flaky、inconclusive、incompatible 也会原样持久化，当前切片不做 promotion 判定；
- 完整重试复用相同 reference 和 H5c receipt，不覆盖首次时间或审计事实。

## 5. 验收证据

- 真实 Git worktree 完成 RED→GREEN→H5c，五次样本得到 improved/passed；
- create operation 得到 unchanged/passed；
- 删除一个 GREEN H5a sample 后在 reference 注册前拒绝；
- 篡改 GREEN receipt candidate revision 后 authority 解析失败；
- comparison reference purpose、active selector 为空和 H5c ordered digest 均可复核；
- Engine 默认组合 Executor，公共 lazy export 可用。

## 6. 当前不足与下一步

- H5c `passed` 只代表当前 Suite/Policy/统计证据允许，不等于整个变异可推广；
- Profile command、项目测试、并发、安全、跨平台等 interventional checks 仍等待 ARC-04 worker；
- 下一切片应实现 EVO-03.5a Failure Attribution Contract：把 H5c 非 passed 结论与 runner/environment/
  candidate/flaky 原因机械分类；随后 EVO-03.6 补 adversarial suites，EVO-03.7 才生成完整 Evaluation Receipt。
