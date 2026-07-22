# EVO-04.2a Mechanical Gate Contract

## 目标

只消费 durable `EvolutionDecisionInput ID`，产生确定性、可重放且不可被 LLM 覆盖的 `pass/veto` authority。
门禁不接收自然语言判断，也不接受调用方传入 Mutation Trace ID；执行器从 Decision Input 内嵌 Mutation Receipt
的签名引用推导 Trace ID，再从 Store 重读完整 tool-call 与时间事实。

本切片完成机械门禁，不产生 Candidate `accept/reject`，不执行 independent reviewer 或 promotion。

## 输入补强

EVO-04.1a 已覆盖 Candidate、Mutation Receipt、Experiment constraints 与 Final Evaluation，但 Mutation Receipt
只引用 Generation Trace ID/digest。为了真正复核 `max_tool_calls` 与 `max_duration_seconds`，EVO-04.2a 额外从
`EvolutionMutationGenerationTraceStore` 重读完整 Trace，并验证：

- Trace ID/digest 与 Mutation Receipt 完全一致；
- Contract、Lease、Source Snapshot、Mutation Plan、attempt/max_attempts 全部一致；
- Trace final files 与 Mutation Receipt paths/after SHA-256 完全一致；
- total tool calls 不超过 Trace Plan 与 Experiment Contract 两级上限；
- generation 起止时间，加 Final Evaluation 全部 RED/GREEN lane 的观察耗时，不超过 Experiment 总时限。

缺失、损坏或引用漂移的 Trace 不会生成一个“veto artifact”，而是以 authority read/binding error 失败关闭。

## 固定机械规则

Gate v1 固定并按顺序执行 16 条规则：

1. `authority_integrity`；
2. `generation_trace_bound`；
3. `scope_exact`；
4. `guardrail_chain_complete`；
5. `required_metrics_exact`；
6. `changed_files_within_budget`；
7. `changed_lines_within_budget`；
8. `tool_calls_within_budget`；
9. `duration_within_budget`；
10. `attempts_within_budget`；
11. `evaluation_coverage_complete`；
12. `no_candidate_fault`；
13. `no_rerun_required`；
14. `all_lanes_reflection_eligible`；
15. `failure_categories_clear`；
16. `failure_actions_continue`。

只有全部通过才产生 `outcome=pass` 和 `independent_review_ready=true`。任一失败都产生 `outcome=veto`，冻结
有序 `veto_codes` 和由 Failure Attribution 推导的 `revise_candidate`、`rerun_evaluation` 或
`rebuild_environment`。LLM 没有 override 字段；模型固定 `llm_override_allowed=false`。

## Authority 与持久化

`EvolutionMechanicalGate` 内嵌完整 Decision Input 和 Mutation Trace，并冻结：

- `evgate_*` identity 与 canonical SHA-256；
- 16 个有序 checks 及 evidence refs；
- observed files、lines、tool calls 与总耗时；
- veto codes、required actions 和 pass/veto outcome；
- `candidate_acceptance_decided=false`、`promotion_ready=false`。

`EvolutionMechanicalGateStore` 对每个 Decision Input 只允许一个不可变 Gate。并发重复执行返回同一 artifact；
Gate JSON、digest、workspace、Decision Input ID、outcome 或 created_at 任一索引漂移都会失败关闭。

## 双通道

- 用户：`/evolution mechanical-gate <decision-input-id>`；
- Agent：`evolution_mechanical_gate(decision_input_id=...)`。

两者调用同一 Executor、Builder、Store 和 renderer。输出明确区分“机械通过/机械否决”和“Candidate 最终决定”。

## 权限治理补充

`evolution_mechanical_gate` 现由 EVO-GOV-01 显式定为中风险派生写入：strict 可用、lockdown 阻断、
normal 无逐次确认、每会话最多 50 次；任何权限模式都不能覆盖 mechanical veto。详见
`EVO-GOV-01-agent-tool-permission-matrix.md`。

## 验收证据

- 真实 Candidate→Mutation→Evaluation→Decision Input 链产生 16/16 `pass`；
- 四个并发调用得到同一 Gate 并可从 Store 精确重读；
- 使用真实仍失败的 GREEN cohorts 构造第二条完整签名链，得到 `veto`、`revise_candidate` 与不可覆盖状态；
- 缺失 Mutation Trace、跨工作区访问、模型字段篡改和 SQLite outcome 篡改全部失败关闭；
- Slash 与 Agent Tool 输出同一 Gate authority；
- Ruff、compile/import、组合根和相关聚焦 pytest 通过。

## 明确未完成

- EVO-04.3a independent reviewer identity/model/prompt binding 已完成；
- EVO-04.4a counterfactual 已完成；
- EVO-04.5a reward-hacking evidence 已完成；
- EVO-04.6a Decision State 与 EVO-04.6b Escalation Resolution 已完成；
- EVO-04.7 reflection memory 和 EVO-05 promotion。

## 下一步

EVO-02.7c2 已补齐 Trace-bound Mutation Author Receipt；EVO-04.3a 已实现 Independent Reviewer Contract；
EVO-04.4a 至 EVO-04.6b 已实现下游 Evidence、Decision State 与持久用户 Resolution。下一步进入 EVO-04.7a
Reflection Memory；任何后续阶段仍不能覆盖 Mechanical veto。
