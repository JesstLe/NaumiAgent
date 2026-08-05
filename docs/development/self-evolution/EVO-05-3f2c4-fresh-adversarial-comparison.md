# EVO-05.3f2c4 Fresh Adversarial Platform Comparisons

## 目标

在 required-platform matrix 完整后，对每个平台的 Fresh Adversarial RED/GREEN cohort 从原始 H5a 重新计算并持久化
HAR-08 原生 H5b2/H5c。单个平台的通过不能替代缺失平台；matrix 不完整时任何比较均不得开始。

## 实现与权威链

`EvolutionRevalidationAdversarialComparisonExecutor` 按 Runtime Contract 的平台顺序执行：

1. 动态复验 Runtime Contract 与已持久化的完整 matrix；
2. 绑定 matrix lane、platform cohort、Validation Plan、Source Snapshot、suite 与样本数；
3. 从 Harness Store 读取 ordered RED/GREEN H5a，核对 sample index 和 result digest；
4. 复验同平台 configuration、同 commit、RED clean source、GREEN immutable overlay dirty source；
5. 从每个 case 重算 status、`exit_zero` metric、probe kinds、cohort Run Grant，并与 cohort summary 比较；
6. 调用共享 `EvolutionComparisonKernel` 生成不可变 H5b2/H5c，一平台一张 comparison receipt。

## 验收标准

- matrix 任一 lane 为 runnable/pending 时必须在写入 H5c 前失败；
- 每个平台必须使用自己的 RED/GREEN batch，禁止跨平台混比；
- H5a 缺失、顺序变化、digest/identity/summary/probe/Grant 不一致必须失败关闭；
- 重复执行返回同一 Harness comparison authority；
- 所有平台 receipt 的数量和顺序必须与 required platforms 一致；
- 定向 Ruff、comparison/cohort/matrix 测试和引擎 import 通过。

## 后续依赖

下一切片聚合逐平台 H5c 并进行 failure attribution；只有归因完成且新的 Final Evaluation 同时消费 Fresh
Interventional 与全部 Fresh Adversarial 权威后，才可进入重新审批、staged promotion、监控和自动回滚。
