# EVO-05.3f2c5 Fresh Adversarial Failure Attribution

## 目标

对完整 required-platform matrix 的每一张 Fresh Adversarial H5c 做机械归因并持久化，形成新 Final Evaluation 可消费的
逐平台事实。matrix、cohort 或 H5c 任一不完整时，不得产生 attribution。

## 领域语义

Adversarial lane 是安全守护指标，不是必须持续上升的目标指标：

- `passed + unchanged` 表示候选保持 guardrail，归因为 `none / adversarial_guardrail_preserved`，允许继续；
- improved 继续沿用通用 `verified_improvement`；
- failed/regressed、flaky、inconclusive、identity incompatible 沿用共享 Failure Attribution kernel 的候选缺陷、
  证据不足、评测基础设施或环境不兼容分类。

这一区分防止把“安全能力未退化”误判为“候选没有改善”。

## 权威链

- 动态复验 Runtime Contract 和完整 matrix；
- 重新执行逐平台 comparison authority 加载，并从 Harness Store 复验其不可变事实；
- 精确绑定 platform lane、cohort receipt、RED/GREEN batch、样本 digest、Candidate 与 Validation Plan；
- 每个平台形成独立、幂等的 `EvolutionFailureAttributionReceipt`；
- receipt 数量与顺序必须等于 Contract required-platform lanes。

## 验收标准

- 不完整 matrix 在 attribution 前失败；
- 传入非 Harness Store 当前 H5c、错平台或错 batch 必须失败关闭；
- unchanged passing guardrail 必须继续，不得要求无意义地修改 Candidate；
- 真正的 regression/failure 不得被 guardrail 特例吞掉；
- 重复执行返回相同 attribution receipts；
- 定向 Ruff、通用 attribution 回归、Fresh attribution 和引擎 import 通过。

## 后续依赖

下一切片生成新的 Final Evaluation authority，同时消费 Fresh Interventional H5c、全部 Fresh Adversarial H5c/attribution、
Runtime Contract 和 current source identity。之后才进入重新审批与 staged promotion。
