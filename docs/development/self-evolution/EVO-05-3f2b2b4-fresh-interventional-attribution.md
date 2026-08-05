# EVO-05.3f2b2b4 Fresh Interventional Failure Attribution

## 目标

把 Fresh Interventional cohort 与其原生 H5c 转换为可持久化的机械 failure attribution，补齐新版 Final Evaluation
所需的目标指标结论。该切片不把 `policy passed` 等同于“目标已改善”。

## 权威链

- 动态复验 Runtime Contract、current source 与完整 Fresh Interventional cohort；
- 重新执行 comparison 入口并从 Harness Store 复验 H5c 不可变事实；
- 绑定 Contract、Validation Plan、Source Snapshot、Candidate、RED/GREEN batch、样本数量与 result digests；
- 使用共享 Failure Attribution kernel 分类 improved、unchanged、regressed、failed、flaky、inconclusive 与 incompatible；
- 以 comparison ID 幂等持久化 attribution。

## 验收标准

- 缺 cohort、stale Contract、H5c 非权威或 batch/digest 漂移必须失败关闭；
- `passed + unchanged` 的目标指标必须归因为 `objective_not_improved / revise_candidate`；
- improved 才允许进入 reflection，regression/failure 必须标记候选缺陷；
- 重复执行返回相同 receipt；
- 定向 Ruff、Fresh/通用 attribution、comparison 与引擎 import 通过。

## 后续依赖

Fresh Final Evaluation 必须同时消费本 attribution、Interventional H5c、完整 Adversarial matrix、各平台 H5c/attribution
与 current source identity。任何 attribution 要求 revise/rerun/rebuild 时，最终回执仍可记录事实，但不得开放重新审批。
