# EVO-04.1a Decision Input Contract

## 目标

为 EVO-04 mechanical gate 提供唯一、完整、可重放且防篡改的输入 authority。调用者只提交当前工作区和
`Final Evaluation Receipt ID`；执行器从已签名引用推导其余 ID，并分别从 Candidate Store、Mutation Receipt
Store、Experiment Contract Store 与 Final Evaluation Receipt Store 重读事实。自然语言、单 lane 回执和调用方
拼接的 artifact ID 不属于可信输入。

本切片只冻结决策输入，不产生 `pass/veto`，不接受、拒绝或推广 Candidate。

## Authority 组成

### EVO-04.1a.1 Candidate materialization

- 从 workspace-bound `EvolutionCandidateStore` 重读当前 revision；
- 将 Candidate Draft、revision、risk、draft SHA-256 与 Store 时间戳冻结为
  `EvolutionDecisionCandidateAuthority`；
- 重新计算 Draft canonical JSON digest，不信任调用方提供的 risk 或 Candidate digest。

### EVO-04.1a.2 Mutation facts

- 从 Final Receipt 内嵌 Batch Request 推导 `mutation_receipt_id`；
- 从 `EvolutionMutationReceiptStore` 重读完整文件事实、scope、required metrics、attempt 与 changed lines；
- 要求 receipt ID/digest、Candidate binding、Contract binding 和 candidate files digest 与评测链完全一致。

### EVO-04.1a.3 User-approved constraints

- 从 Batch Request 推导 Experiment Contract ID；
- 从 workspace-bound `EvolutionExperimentContractStore` 重读完整 Authority；
- 投影 approved scope/files、预算、tools/checks、network 和 dependency constraints；
- 重新按 Candidate risk policy 校验 Experiment budget cap。

### EVO-04.1a.4 Complete evaluation

- `EvolutionFinalEvaluationReceiptStore.get_by_receipt_id()` 允许以不可变 Receipt ID 重读最终回执；
- Final Receipt 必须保持 `mechanical_gate_input_ready=true`、
  `candidate_acceptance_decided=false` 和 `promotion_ready=false`；
- Aggregation Contract、Validation Plan、Mutation Receipt 与 Experiment Contract 的引用必须闭合。

### EVO-04.1a.5 Immutable Decision Input

`EvolutionDecisionInput` 内嵌上述四类完整 authority，并生成 canonical SHA-256 与 `evdin_*` identity。
`EvolutionDecisionInputStore` 对一个 Final Receipt 只允许一个不可变 artifact；并发重放返回相同内容，索引列与
JSON 任一漂移都会 fail closed。

## 调用面

- 用户：`/evolution decision-input <final-evaluation-receipt-id>`；
- Agent Tool：`evolution_decision_input(final_evaluation_receipt_id=...)`；
- 两者调用同一个 `EvolutionDecisionInputExecutor`，并使用同一个 renderer；
- 输出明确声明尚未执行 mechanical gate，也没有接受或发布决定。

## 机械不变量

1. workspace 必须在 Candidate、Experiment 与 Final Receipt 间完全一致；
2. Candidate ID、revision 与 draft digest 必须在四类 authority 间一致；
3. Mutation Contract ID/digest 与 Experiment Authority、Batch Request 一致；
4. Mutation file paths 必须精确等于 approved files，files digest 必须等于评测 Candidate snapshot digest；
5. Candidate scope、Mutation scope 与 Experiment impact scope 完全一致；
6. Mutation required metrics 必须精确等于 approved checks；
7. attempt、changed files 与 changed lines 不得超过 approved budget；
8. approved budget 不得超过 Candidate risk policy cap；
9. Final Receipt ID/digest、非决策状态和 Decision Input identity 不得被覆盖。

## 权限治理补充

`evolution_decision_input` 现由 EVO-GOV-01 显式定为中风险派生写入：strict 可用、lockdown 阻断、
normal 无逐次确认、每会话最多 50 次；bypass 不能跳过四 Store authority 重读。详见
`EVO-GOV-01-agent-tool-permission-matrix.md`。

## 验收证据

- 真实 Candidate→Experiment→Mutation→Validation→Final Evaluation 链可生成并从 Store 精确重读；
- 四个并发调用产生同一个 Decision Input；
- Slash 与 Agent Tool 输出来自同一 authority；
- Final Receipt 缺失、Mutation authority 缺失、跨工作区读取与 Candidate revision 漂移均拒绝；
- 模型字段篡改与 SQLite 投影列篡改均拒绝；
- 相关模块 Ruff、import/compile 与聚焦 pytest 通过。

## 明确未完成

- EVO-04.2a mechanical `pass/veto` gate 已完成；
- EVO-04.3a independent reviewer 已完成；
- EVO-04.4a Counterfactual 与 EVO-04.5a Reward-hacking Evidence 已完成；
- EVO-04.6 accept/revise/reject/escalate state；
- EVO-04.7 reflection memory 与 EVO-05 promotion。

## 下一步

EVO-04.2a 至 EVO-04.5a 已实现。下一步实现 EVO-04.6a Decision State Contract；Reviewer、Counterfactual
与 Reward-hacking Evidence 均无权覆盖 mechanical veto。
