# HAR-08.7b Suite Mechanical Comparator

## 1. 目标

在 HAR-08.7a Identity gate 通过后，对两个 `HarnessEvalSuiteResult` 做可解释、确定性的 case
状态转换与机械指标 delta。该模块区分产品实现失败和评测基础设施不稳定，不允许把 fixture
错误、跳过或结构损坏标记为产品回归。

本切片不定义绝对门槛、guardrail 权重、延迟统计、置信区间或 flaky 判定。

## 2. 前置完整性 Gate

比较按以下顺序执行，任一前置失败都停止后续指标运算：

1. Baseline 与 Current 都必须携带 typed Baseline Identity；
2. Result 的 `suite_id/suite_sha256` 必须与自身 Identity configuration 一致；
3. HAR-08.7a Identity comparison 不得为 `incompatible`；
4. case ID 不得重复；
5. 两侧 case ID 集合必须完全一致；
6. 相同 case 必须使用相同 runner；
7. Suite 汇总状态必须与 case 状态机械一致。

Identity 缺失、不兼容或 Result/Identity 绑定矛盾返回 `incompatible`；case 集合、runner、重复 ID
或汇总状态异常返回 `inconclusive`。两类均不产生 metric delta。

## 3. Case Transition

| Baseline | Current | Transition | 结论贡献 |
| --- | --- | --- | --- |
| passed | passed | `unchanged_pass` | 无 |
| passed | implementation_failure | `regression` | 回归候选 |
| implementation_failure | passed | `improvement` | 改善候选 |
| implementation_failure | implementation_failure | `unchanged_implementation_failure` | 无 |
| 任意 | evaluation_error/skipped | `evaluation_instability` | 全局无法判断 |
| evaluation_error/skipped | 任意 | `evaluation_instability` | 全局无法判断 |

若同一 Suite 同时出现产品回归候选和 evaluation instability，最终 verdict 必须是
`inconclusive`。底层 transition 保留供诊断，但共享 renderer 不展示“回归 N”主摘要。

## 4. Mechanical Verdict

- `incompatible`：身份或 Result 绑定不成立；
- `inconclusive`：评测错误/跳过或 case 结构不可靠；
- `regressed`：至少一个 regression，且不存在 evaluation instability；
- `improved`：至少一个 improvement、没有 regression/instability；
- `unchanged`：不存在 regression、improvement 或 instability。

当回归和改善同时出现且评测稳定时，回归优先，避免平均值掩盖 guardrail 失败。后续 8.7c
再依据 case primary metric 和 guardrail 决定更细的门槛结论。

## 5. Mechanical Metrics

输出固定顺序的 `EvalMetricDelta`：

- cases；
- passed；
- implementation_failures；
- evaluation_errors；
- skipped；
- pass_rate。

每项包含 baseline、current、absolute delta 与 relative delta。Baseline 为零时 relative delta
返回 `null`，禁止生成 infinity、NaN 或伪造百分比。duration 不进入本切片，因为单样本耗时不能
支持统计结论。

## 6. 用户解释

`render_eval_suite_comparison()` 显示机械 verdict、稳定原因、短 Identity、case 总数，以及有意义的
状态转换。`inconclusive` 场景统一显示“状态变化待复核”，不会在主摘要出现“Case：回归”。

## 7. 已验证场景

- 两次全通过运行得到 unchanged 与零 pass-rate delta；
- passed→implementation failure 得到 regressed；
- implementation failure→passed 得到 improved；
- evaluation error 与 skipped 均得到 inconclusive；
- 身份缺失/不兼容、Result/Identity 绑定矛盾提前停止；
- case 集合、runner、重复 ID、汇总状态异常不产生指标；
- Baseline=0 的 relative delta 为 null；
- 回归候选与基础设施错误并存时最终仍为 inconclusive，UI 不突出回归；
- 共享中文 renderer 不把评测基础设施错误称作产品结论。

## 8. 后续模块

- HAR-08.7c：已实现；从 Suite schema 的 primary metric、绝对/相对门槛和 guardrail evidence
  生成 policy verdict，详见 `HAR-08-7c-threshold-guardrail-policy.md`；
- HAR-08.7d：重复样本、均值、离散度、置信区间与 flaky 标记；
- HAR-08 H5：存储 Result、Identity、机械比较与不可覆盖 Baseline；
- HAR-08.8：Baseline 选择、`/harness baseline`、Tool/API 与 New UI 详情页。
