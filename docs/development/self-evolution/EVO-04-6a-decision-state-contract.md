# EVO-04.6a Decision State Contract

## 目标

把完整 Evolution 证据链收敛为不可变、可审计的
`accepted_experiment/revise/rejected/escalated` 四态决策，同时保证：

- Mechanical veto 永远不能被 Reviewer 或后续叙事覆盖；
- LLM Reviewer 意见保留为审计证据，但不参与终态算法；
- 缺失证据不会被静默解释为接受；
- `escalated` 生成与现有 HAR-10.6/New UI/TUI 完全兼容的 3 个选项和自定义输入；
- `accepted_experiment` 只开放后续 promotion review，不直接修改 baseline、main 或 worktree。

## 为什么入口是 Decision Input ID

若入口只接受 Reward-hacking Evidence ID，Mechanical veto 路径将永远无法形成 `rejected`，因为 veto 后按设计
不会产生 Counterfactual 或 Reward-hacking Evidence。EVO-04.6a 因此以 `Decision Input ID` 为唯一入口：

- `/evolution decision-state <decision-input-id>`
- `evolution_decision_state(decision_input_id=...)`

Executor 先从 Mechanical Gate Store 查询该 Decision Input 的唯一 Gate，再按 Gate outcome 重读后续 authority。
调用方不能提交 state、risk、reason、Reviewer recommendation、Evidence outcome 或用户选项。

## Authority 路径

### Mechanical veto

必须重读：

1. `EvolutionMechanicalGateStore`；
2. `EvolutionIndependentReviewStore` 中同 Gate 的 `blocked_by_mechanical_veto` Review。

该路径禁止出现 Counterfactual/Reward-hacking artifact，状态固定为 `rejected`。

### Mechanical pass

必须重读并逐层相等比较：

1. Mechanical Gate；
2. completed Independent Review；
3. 同 Review 的 Counterfactual Evidence；
4. 同 Counterfactual 的 Reward-hacking Evidence。

任一 Store 缺失、损坏、workspace 不一致或嵌套 authority 不相等即 fail closed，不会提前形成 Decision State。

## 固定状态优先级

Policy v1 只使用机械字段，优先级不可由 LLM 改写：

1. Gate `veto` → `rejected`，reason `mechanical_veto`；
2. Counterfactual 或 Reward-hacking `concern` → `revise`；
3. Reward-hacking `inconclusive` → `escalated`；
4. Candidate risk 为 `high/critical` → `escalated`；
5. Gate pass、两类 Evidence 均 clear、risk 为 low/medium → `accepted_experiment`。

若同时存在 concern 与证据不足/高风险，`revise` 优先，因为已有结构化风险必须先被修订。Reviewer 的
recommendation、confidence、summary、strengths 和 concerns 均不进入此函数；Review 仍作为 author/reviewer
隔离与审计 authority 被完整保存。

## Escalation 交互

`escalated` 必须包含 `EvolutionDecisionEscalationRequest`：

- 固定 3 个唯一选项；
- `allow_custom=true`，显示“其他处理要求”；
- evidence inconclusive 时推荐“补齐证据”；
- high/critical risk 且证据完整时推荐“安排人工审查”；
- 其余两个选项为“修订 Candidate”和“拒绝 Candidate”；
- 不设置隐式 timeout。

模型在构建时调用 `normalize_interaction_request()` 复核，保证其 public payload 可被现有 HAR-10.6 durable
interaction、New UI 和 Textual TUI 直接消费。非 escalated 状态禁止携带 escalation payload。

本切片生成交互合同，但不代替用户作答，也不把答案写回原有不可变 Decision State；EVO-04.6b 已以独立
Resolution authority 接入真实 HAR-10.6 回答，不会改写本 artifact。

## Artifact 与 readiness

`EvolutionDecisionState` 固化：

- Decision Input/Gate/Review/Counterfactual/Reward-hacking ID 与 digest；
- Candidate revision、risk、state、ordered reasons 与 7 条固定 checks；
- 可选 escalation request；
- 完整签名 authority；
- content-addressed decision ID/digest 与 deterministic timestamp。

accepted/revise/rejected 设置 `candidate_acceptance_decided=true`；escalated 在用户 resolution 前保持 false。
所有状态均 `promotion_executed=false`、`reviewer_advisory_only=true`、`llm_decision_authority=false`。
只有 `accepted_experiment` 同时设置：

- `candidate_accepted=true`；
- `experiment_accepted=true`；
- `promotion_review_ready=true`。

这只是开放下一 policy gate，不是 promotion permission。

## Store、并发与权限

- Store 以 Decision Input 为唯一键，`BEGIN IMMEDIATE` 保证并发重复签发收敛；
- 同一输入不可覆盖为另一状态，row index 与 JSON payload 双向校验；
- 单 artifact 上限 32 MiB；
- Agent Tool 属于 `evolution_decision_artifact` 中风险派生写入，每会话 50 次；
- permissive/moderate/strict 无逐次确认，lockdown 阻断，bypass 直接通过但不绕过 authority；
- Slash、Agent Tool 和共享 Markdown renderer 复用同一 Executor，New UI/TUI 不复制决策规则。

## 验收标准

- 纯 policy fixture 覆盖四种终态和 reason 顺序；
- 真实完整链在资源/平台证据不足时形成 `escalated`，包含 3 个选项和自定义输入；
- 真实 mechanical-veto 链不调用 Reviewer 模型且形成 `rejected`；
- 4 路并发重复签发得到同一 Decision ID；
- Reviewer recommendation 改变不影响状态算法；
- pass 路径缺 Reward-hacking、非法 ID、错误 workspace 均 fail closed；
- readiness、state、escalation、digest 或 Store index 篡改均拒绝；
- Slash 与 Agent Tool 输出同一 artifact；
- Engine composition、权限矩阵和工具 schema 聚焦测试通过；
- 不运行全量测试。

## 明确未完成

- EVO-04.6b Escalation Resolution 已完成；
- EVO-04.7a Reflection Memory 已完成；
- EVO-05 promotion/rollback 与 HAR-09.6 outcome tracking；
- `accepted_experiment` 不会自动修改 baseline、Git 分支或生产配置；
- 当前 Decision artifact 沿用既有嵌套 authority，体积较大；后续可在保持 digest 可重放的前提下引入内容寻址
  引用，但不能用无验证外键削弱自包含审计。

## 下一步

EVO-04.7a 已只保存结构化 Decision/Resolution 经验与证据引用。EVO-05.1a Promotion Package
Input、review Package、EVO-05.2a Requirement 与 EVO-05.2b Role Response 均已完成；下一步是 EVO-05.2c
身份/签名回执。用户自定义文本或 Reviewer 叙事仍不得进入系统 Prompt 或 promotion authority。
