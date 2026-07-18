# EVO-03.5a Failure Attribution Contract

## 1. 目标

把原生 H5c Comparison Receipt 的 verdict/code 机械归因为候选缺陷、评测基础设施、环境不兼容、波动、
证据不足或目标未改善，并给出不可篡改的下一动作。归因不调用模型、不读取自然语言解释，也不覆盖 H5c
原始结论。

## 2. Authority 与 Receipt

`EvolutionFailureAttributionExecutor` 先按 workspace/suite/baseline/current batch 从 Harness Store 重新读取同一
H5c，不接受调用方伪造的 stored wrapper；随后 Builder 重新验证并绑定：

- Validation Plan ID/digest 与 candidate ID/revision；
- RED/GREEN completion receipt ID/digest 和相互引用；
- `HarnessStoredEvalComparisonReceipt` wrapper 与内部 H5c receipt；
- suite、RED/GREEN batch、样本数和 GREEN 逐样本 Result digest。
- RED/GREEN ordered sample-set digest 与 H5c baseline/current digest。

`EvolutionFailureAttributionReceipt` 保存 H5c/Plan/RED/GREEN identity、category、reason/evidence codes、下一动作、
candidate fault、retry/rerun 和 reflection eligibility flags。canonical digest 覆盖所有字段，category 与 flags
之间还有反序列化语义校验。

## 3. 机械映射

| H5c 证据 | Category | Action | Candidate fault | Reflection eligible |
|---|---|---|---:|---:|
| passed + improved | none | continue_to_reflection | false | true |
| passed + unchanged | objective_not_improved | revise_candidate | false | false |
| failed 或允许门槛内的 statistical regressed | candidate_defect | revise_candidate | true | false |
| flaky | flaky_evidence | rerun_evaluation | false | false |
| inconclusive + 样本不足/CI 跨零 | evidence_incomplete | rerun_evaluation | false | false |
| 其他 inconclusive | evaluation_infrastructure | rerun_evaluation | false | false |
| incompatible | environment_incompatible | rebuild_environment | false | false |

因此 runner error 不会被记为 candidate defect；Policy 容忍的统计回归也不会获得 reflection eligibility；只有
真实 `improved + passed` 才能进入 EVO-04 reflection，但仍不等于自动推广。

## 4. Durable Store

`EvolutionFailureAttributionStore` 使用 comparison ID 作为唯一键，把完整 typed receipt 写入用户 session DB：

- 相同 receipt 重试返回首次事实；
- 同一 H5c comparison 的不同归因不可覆盖；
- 新 Store 实例可恢复相同 receipt；
- row identity/digest/JSON 任一篡改都会在读取时失败关闭。

该表不保存源码、路径列表、模型输出或 secret。

## 5. 验收证据

- 真实 Self-Review modify RED→GREEN→H5c 归因为 `none/verified_improvement`，可进入 reflection；
- create 的 0→0 归因为 `objective_not_improved`，不虚构改进；
- implementation failure、flaky、evaluation error、4 样本和 suite identity mismatch 分别归入不同类别；
- Store 幂等、重启恢复、伪造 H5c wrapper 与 row digest 篡改拒绝通过；
- Engine 默认组合 Builder/Store/Executor，公共 lazy export 可用。

## 6. 当前边界与下一步

- 当前 Self-Review static lane 不会产生 runner/environment failure；相应映射使用原生 H5c 合同 fixture 验证，
  后续 ARC-04 interventional runner 必须复用同一 attribution artifact；
- ARC-04.2b 已冻结 interventional ToolJob admission envelope；EVO-03.6 仍需等待 ToolJob lifecycle receipt 与
  ARC-04.3 Shell worker，不能把 admission-only Store 当作真实执行结果；
- 归因不是 EVO-04 采纳决策，也不包含 adversarial/security/platform matrix；
- 下一最小切片应跨查 ARC-04 与 EVO-03.6：优先实现能真实运行最小 Profile check 的隔离 worker 前置，
  然后再扩展 adversarial suite，避免只在静态 lane 上堆叠更多纸面 gate。
