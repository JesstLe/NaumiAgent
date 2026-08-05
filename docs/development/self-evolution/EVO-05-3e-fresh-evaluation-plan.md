# EVO-05.3e Fresh Evaluation Plan

## 1. 目标

把 current `validated` Revalidation Outcome 转换为可执行但尚未执行的完整重新评估合同。禁止把单次 Harness check
直接冒充 Final Evaluation，也禁止跳过原有 Interventional、Adversarial、跨平台和 failure-attribution 覆盖。

## 2. 输入 authority

Service 只接受 current、rollout-candidate-eligible 的 EVO-05.3d Outcome，并重新读取：

- Outcome 动态 current 状态；
- Outcome 精确绑定的 Harness Validation Receipt 与 overlay source digest；
- 被 Outcome 失效的 Promotion Input；
- `evolution_promotion_authority_invalidations` 中旧 Final Evaluation 的 durable 失效记录。

任一 digest 不一致、Outcome failed/stale、Validation Receipt 被更新、Promotion Input 缺失或失效账本缺失都会 fail
closed。

## 3. 完整重新评估矩阵

`EvolutionRevalidationEvaluationPlan` 固定：

- 一条 fresh Interventional lane；
- 每个原始 required platform 各一条 fresh Adversarial RED/GREEN lane；
- 每条 lane 都必须重新形成 comparison、failure attribution 与 lane receipt；
- 新 Validation Plan、Aggregation Contract 和 Final Evaluation Receipt 全部必须重新签发；
- 所有新 evidence 的时间必须晚于 Revalidation Outcome；
- Outcome target/tree、overlay source、Candidate identity 和旧 Final Evaluation identity 均进入摘要。

Plan 本身 `promotion_authority=false`，不执行模型评测、Git 写入、merge、push 或 publish。

## 4. 双通道

Agent Tool：

```text
evolution_revalidation_evaluation_plan(outcome_id=...)
```

手动入口：

```text
/evolution revalidation-evaluation-plan <revalidation-outcome-id>
```

两者共用同一 Service；权限为 medium，无高风险二次确认。

## 5. 验收标准

- validated Outcome 产生 deterministic、幂等、durable Plan；
- lane 顺序为 Interventional 后按 frozen platform 顺序排列的 Adversarial lanes；
- overlay digest 来自真实 Validation Receipt，不从 ID 推测或重新拼接；
- failed/stale Outcome 不可签发；
- 缺少 SQLite invalidation ledger 时不可签发；
- Harness Profile 漂移会使已签发 Plan 动态 stale；
- Tool 与 Slash 共享同一结果；
- 只运行本模块和相邻 registry 测试，不运行全量测试。

## 6. 后续依赖

[EVO-05.3f1](EVO-05-3f1-immutable-evaluation-source.md) 已把 exact target + overlay 捕获为可脱离 Candidate Lease 的
content-addressed immutable source。EVO-05.3f2a 仍需先重绑 current-target RED baseline、immutable GREEN source、
seed/metrics/checks/预算，EVO-05.3f2b 再让完整评估执行器消费它并重建 cohort/lane matrix，最终签发时间下界、target、
overlay 和 Plan digest 全部匹配的新 Final Evaluation Receipt。随后才允许重新走
Decision/Reflection/Package/专业签名与 Approval，EVO-05.4 staged rollout 仍未开放。
