# EVO-03 多层验证与 Eval 对照

## 目标

证明变异不仅“能编译”，还在目标指标上优于 baseline 且没有破坏 guardrail；所有结论由 HAR-08
Eval 和真实 Harness 检查产生。

## 子模块

- EVO-03.1 Validation plan：按改动语言/模块选择 lint、compile、unit、contract、smoke。
- EVO-03.2 Baseline run：相同 fixture/环境/预算先测 baseline。
- EVO-03.3 Candidate run：隔离 worktree、相同 seed/order/resource。
- EVO-03.4 Comparator：主指标改善、guardrail 无退化、噪声/置信度。
- EVO-03.5 Failure attribution：candidate defect、environment、eval error、flaky/unknown。
- EVO-03.6 Adversarial suite：边界、并发、安全、恢复、跨平台、奖励投机探针。
- EVO-03.7 Evaluation receipt：before/after、sample、成本、失败、artifact digest。

## 验收标准

- baseline/candidate 环境 identity 不同则结果不可直接比较。
- 定向测试必须包含新 RED/GREEN；仅 import 成功永远不足。
- 主指标改善但安全/正确性 guardrail 退化时自动拒绝。
- flaky case 重复并报告分布，不允许挑选最好一次。
- Eval runner 自身失败不算 candidate 失败，进入 blocked/needs_rerun。
- A4：至少一个真实 NaumiAgent 小模块改进在 macOS/Linux 比较；平台特有变更有专属矩阵。
