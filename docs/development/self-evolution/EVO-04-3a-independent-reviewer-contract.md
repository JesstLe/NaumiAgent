# EVO-04.3a Independent Reviewer Contract

## 目标

只接受 durable Mechanical Gate ID，从 Store 重读 Gate 与 Trace-bound Mutation Author Receipt，使用与
mutation author canonical identity 不同的模型产生严格结构化、仅供建议的独立审查。Reviewer 不能覆盖
Mechanical Gate、不能接受或拒绝 Candidate，也不能批准发布。

Mechanical Gate 为 `veto` 时不调用任何模型，只签发确定性的 veto explanation artifact。

## 权威输入

调用者只能提交：

- `gate_id`；
- 可选 `reviewer_model`，空值使用现有 reasoning tier。

Executor 不接受自然语言证据、author identity、Gate outcome 或 Candidate 内容。它从
`EvolutionMechanicalGateStore` 重读 Gate，再按 Gate 的 Mutation Trace ID 从
`EvolutionMutationAuthorReceiptStore` 重读 Author Receipt，并机械验证：

- Trace ID/SHA-256、run、Mutation Plan ID/SHA-256 与 attempt；
- model/tool-call 总数与 Generation Trace；
- Author Receipt 仍是 `author_identity_ready=true`、`reviewer_identity_bound=false`；
- workspace、Candidate revision 与 Gate authority 未漂移。

缺失、损坏、跨工作区或引用漂移全部失败关闭。

## pass 与 veto 两条路径

### Mechanical pass

只有 `gate.outcome=pass` 且 `independent_review_ready=true` 才解析 Reviewer model、组装 Prompt 并调用
`ModelPort`。完成 artifact 固定：

- `status=completed`；
- `gate_outcome=pass`、`gate_outcome_preserved=true`；
- `reviewer_author_isolated=true`；
- `reviewer_advisory_only=true`、`llm_override_allowed=false`；
- `candidate_acceptance_decided=false`、`promotion_ready=false`；
- `counterfactual_review_ready=true`，只允许进入 EVO-04.4。

### Mechanical veto

`gate.outcome=veto` 时不解析模型配置、不读取模型 capability、不调用 ModelPort。Artifact 复制不可变
`veto_codes` 与 `required_actions`，固定：

- `status=blocked_by_mechanical_veto`；
- reviewer、Prompt digest、model response、usage 与 opinion 全部为空；
- `model_called=false`、`counterfactual_review_ready=false`；
- `llm_override_allowed=false`、`candidate_acceptance_decided=false`。

这条路径不是让 Reviewer “同意否决”，而是证明 Reviewer 从未获得改写否决的机会。

## Reviewer identity 与模型能力

Reviewer 通过 `ModelPort.get_runtime_identity()` 和 `get_model_capability_contract()` 解析。调用前必须满足：

- requested/canonical/upstream model、provider、API format 和 identity source 完整；
- requested model 与 runtime identity 精确一致；
- reviewer canonical model 不等于 author canonical model；
- capability contract 的 canonical model/provider 与 runtime identity 一致；
- contract 状态为 `verified` 或 `partial`；
- `supports_structured_output=true`；
- 上下文至少 4096 tokens，允许输出至少 512 tokens。

Provider 可以相同，但 canonical model 必须不同；两侧 provider/model/API format/source 都进入不可变
Review artifact。能力未验证或 identity 相同会在模型调用前拒绝。

## 结构化证据与输出

Reviewer Prompt 只包含结构化投影：Candidate ID/revision/risk/finding/scope、Mutation 文件 digest、Mechanical
checks、预算、最终评测 failure/resource facts、author identity 与允许引用的 evidence refs。它不包含源码、
Mutation Prompt 正文、reasoning、凭据或 proposed contents。

模型调用固定 `temperature=0`、`response_format=json`、thinking disabled，且只允许一次调用。返回值必须是无
代码围栏、无额外字段的 `IndependentReviewOpinion`：

- summary、strengths、concerns；
- 只能引用 Prompt 明示的 evidence refs；
- recommendation 仅允许 `continue_to_counterfactual`、`revise_before_counterfactual` 或
  `escalate_for_human_review`；
- confidence 为 low/medium/high；
- `mechanical_gate_override_requested=false`；
- `candidate_acceptance_decided=false`。

Tool call、空/超大响应、非法 JSON、额外字段、伪造 evidence ref、无效 Token usage、响应模型漂移和超时均
typed fail-closed。Artifact 只保存结构化 opinion、Prompt/响应 digest 与 usage，不保存原始 reasoning。

## 并发与恢复

模型输出不是确定性函数，不能沿用“并发后比较两个 artifact 是否相同”的策略。Review Store 因此提供 durable
single-flight claim：

- SQLite `BEGIN IMMEDIATE` 原子获取每个 Gate 的 owner token 与 lease；
- 同进程和跨进程竞争者轮询现有 artifact，不重复调用模型；
- owner 完成后必须持 claim 才能写入；失去或被接管的 owner 无权落盘；
- 模型异常、JSON 失败、timeout 或 caller cancellation 都释放 claim；
- 进程崩溃后 lease 到期可被新 owner 接管；
- 每个 Gate 永久只允许一个不可变 Review artifact，正文和索引列双重校验。

## 双通道

- 用户：`/evolution independent-review <gate-id> [reviewer-model]`；
- Agent：`evolution_independent_review(gate_id=..., reviewer_model=...)`。

两者调用同一个 Executor、Builder、Store 和 renderer。Renderer 明确显示 author/reviewer identity、Gate 保持
不变、advisory recommendation，以及“尚未接受 Candidate”。

## 权限治理补充

`evolution_independent_review` 现由 EVO-GOV-01 显式定为中风险派生写入：strict 可用、lockdown 阻断、
normal 无逐次确认、每会话最多 20 次。较低上限约束不同 Gate 的模型调用面；同一 Gate 的并发仍由 durable
single-flight 收敛。bypass 不能绕过 author/reviewer identity 隔离或 mechanical veto。详见
`EVO-GOV-01-agent-tool-permission-matrix.md`。

## 验收证据

- 真实 Candidate→Mutation→RED/GREEN→Final Receipt→Decision Input→Mechanical Gate authority 上完成审查；
- 四个并发请求只发生一次 Reviewer ModelPort call，并返回同一个 Store artifact；
- Slash 与 Agent Tool 输出同一个 Review；
- author/reviewer canonical identity 相同和 structured-output capability 未验证时调用前拒绝；
- malformed JSON 失败后 claim 可立即重试，timeout 后也可由新 Reviewer 接管；
- veto Gate 产生无模型 artifact，Reviewer call count 保持 0；
- Author Receipt 缺失、跨 workspace、非法 evidence ref 与 Store 索引篡改失败关闭；
- Ruff、compile/import、Engine composition 与相关聚焦 pytest 通过。

## 后续状态

- EVO-04.4a counterfactual 已完成：真实 baseline/candidate/diff 复核，以及更小改动、删测试、metric/threshold、
  skip/mock 和评测泄漏替代解释扫描；
- EVO-04.5a reward-hacking evidence 已完成；
- EVO-04.6 accept/revise/reject/escalate 最终状态；
- EVO-04.7 reflection memory 与 EVO-05 promotion；
- 需要真实 provider 密钥的多供应商 structured-output 集成矩阵仍是显式 opt-in 验证，不在单元测试中触发。

## 下一步

EVO-04.4a 与 EVO-04.5a 已实现，详见对应 Counterfactual/Reward-hacking 文档。下一步实现 EVO-04.6a
Decision State；Reviewer 意见仍不能直接成为最终 decision。
