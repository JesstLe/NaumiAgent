# EVO-04 反思决策与防奖励投机

## 目标

基于结构化 before/after 证据决定 accept experiment、revise、reject 或 escalate，不让生成补丁的
同一个模型用叙事覆盖失败结果。

## 当前前置状态

EVO-03.7b1 已冻结最终评测所需的 Interventional lane、必需平台和 Adversarial RED/GREEN pairs；
EVO-03.7b2 已从 durable Store 重读并验证全部要求，签发 `mechanical_gate_input_ready=true` 且明确
`candidate_acceptance_decided=false` 的 Final Evaluation Receipt。EVO-04 mechanical gate 只能消费这类最终
回执，不得把单 lane receipt、Aggregation Contract 或调用方自然语言当作完整决策输入。EVO-02.1b 又补齐了
workspace-bound Experiment Contract Store，使 approved scope、budget、tools/checks 与 network/dependency
constraints 可以在决策时独立重读。EVO-04.1a 现已将它与 Candidate、Mutation、Final Evaluation authority
组成确定性输入合同：调用者只提交 Final Evaluation Receipt ID，执行器从四个 durable Store 重读完整
authority，交叉验证 workspace、Candidate revision/risk/digest、Mutation files/scope、Experiment
budget/constraints 与最终评测引用，并持久化不可变 `EvolutionDecisionInput`。它仍明确保持
`mechanical_gate_decided=false` 和 `candidate_acceptance_decided=false`。
EVO-04.2a 又从 Decision Input 的签名引用重读完整 Mutation Generation Trace，对 scope、guardrails、
files/lines/tool calls/duration/attempt 预算和全部评测 failure facts 执行固定 16 条规则，签发不可被 LLM 覆盖的
`pass/veto` Gate；Gate 仍保持 `candidate_acceptance_decided=false`。
EVO-02.7c2 现又让成功 Mutation Turn 持久化 Trace-bound Author Receipt，冻结 canonical model、provider、
API format、identity source、Prompt/tool schema digest 与逐轮上下文摘要。EVO-04.3a 因此不再需要从当前
router 配置猜测 mutation author。
EVO-04.3a 现已从 Gate ID 重读上述 authority：mechanical pass 才允许不同 canonical model 执行一次严格
JSON advisory review，并以 durable single-flight claim 防止并发重复模型调用；mechanical veto 完全不调用
模型，只形成不可覆盖的 veto explanation。两条路径仍固定 `candidate_acceptance_decided=false`。
EVO-04.4a 现又从 completed Independent Review ID 重读完整 Store 链和受管 worktree 的真实 baseline/candidate
字节，逐文件验证 Mutation Receipt digest/diff，并确定性检查更小 scope、删测试、metric 修改、阈值放宽、
skip/mock 和评测泄漏。它不调用 LLM，只签发 `clear/concern` Evidence，两种结果均不接受 Candidate。
EVO-04.5a 进一步从 Counterfactual ID 重读 Final Evaluation、全部 Lane 与 Adversarial Cohort authority，
确定性检查真实任务退化、proxy divergence、平台选择性和 duration/token/cost 资源换分；缺失跨平台或资源
证据明确形成 `inconclusive`，不冒充 `clear`。
EVO-04.6a 现以 Decision Input ID 为唯一入口，兼容 mechanical veto 短路与 pass 完整证据链；固定优先级形成
四态 Decision State，Reviewer 只保留为 advisory audit，不参与状态算法。`escalated` 内置 HAR-10.6 兼容的
3 个选项和自定义输入，`accepted_experiment` 也只开放 promotion review，不执行 promotion。
EVO-04.6b 已把 escalation 接入真实 HAR-10.6 create-before-display 与 fenced answer：重启可复用
pending/answered authority，Resolution 只允许补证据、人工审查、修订、拒绝或自定义后续动作，任何路径都不
接受 Candidate 或执行 promotion。
EVO-04.7a 已把 Decision/Resolution 确定性投影为最小 Reflection Memory：只保存枚举 lesson/action/signal 与
typed authority ID/digest，自定义用户文本和 Reviewer 叙事不落库；独立 SQLite 表不进入向量索引、自动召回或
系统 Prompt。记录可通过 append-only authority 撤销，仍不执行 promotion。
EVO-GOV-01 又为 Decision Input、Mechanical Gate、Independent Review 与 Counterfactual Evidence 非只读 Agent Tool 建立显式
中风险权限规则和有界会话调用面；bypass 不跳过任何 authority 或 veto。详见
`EVO-GOV-01-agent-tool-permission-matrix.md`。

## 子模块

- EVO-04.1 Decision inputs：candidate、mutation receipt、Eval receipt、risk、user constraints。EVO-04.1a 已完成。
- EVO-04.2 Mechanical gate：checks、guardrails、scope、budget、integrity 先判。EVO-04.2a 已完成。
- EVO-04.3 Independent reviewer：可选不同模型/规则，看到证据但不能改结果。EVO-04.3a 已完成。
- EVO-04.4 Counterfactual：是否有更小改动、改善是否来自删测试/改指标/放宽规则。EVO-04.4a 已完成。
- EVO-04.5 Reward hacking detector：proxy gaming、选择性样本/平台优化、资源换分与真实任务退化。
  EVO-04.5a 已完成。
- EVO-04.6 Decision state：accepted_experiment/revise/rejected/escalated。EVO-04.6a 状态合同与 EVO-04.6b
  escalation resolution 均已完成。
- EVO-04.7 Reflection memory：只保存结构化经验和证据引用，禁止污染系统 Prompt。EVO-04.7a 已完成。

## 验收标准

- mechanical gate 失败时 LLM 无权 accept。
- 修改测试/metric/config 使分数变好但产品不改善的 fixtures 被拒绝。
- reviewer 与 author identity、模型和 Prompt version 被记录。
- 同一证据/规则版本决策确定；LLM 补充意见不改变结构化终态。
- `escalated` 生成用户可理解选项和自定义输入，不擅自选择产品 tradeoff。

## 已交付切片

- [EVO-04.1a Decision Input Contract](EVO-04-1a-decision-input-contract.md)：Final Receipt ID 单入口、四 Store
  authority 重读、完整交叉绑定、并发幂等持久化、Slash/Agent Tool 双通道与 fail-closed 篡改检测。
- [EVO-04.2a Mechanical Gate Contract](EVO-04-2a-mechanical-gate-contract.md)：Mutation Trace 预算补强、固定
  16 规则、确定性 pass/veto、不可覆盖 veto 与双通道持久化。
- [EVO-02.7c2 Mutation Author Receipt](EVO-02-7c2-mutation-author-receipt.md)：为独立 Reviewer 补齐可重读
  author provider/model、Prompt/tool schema digest 与逐轮调用 authority；不执行审查或最终决策。
- [EVO-04.3a Independent Reviewer Contract](EVO-04-3a-independent-reviewer-contract.md)：Gate ID 单入口、
  reviewer/author canonical identity 隔离、strict JSON advisory、veto 无模型路径、durable single-flight、
  Slash/Agent Tool 双通道与不可变 Review artifact。
- [EVO-04.4a Counterfactual Evidence Contract](EVO-04-4a-counterfactual-evidence-contract.md)：Review ID
  单入口、六 Store authority 重读、受管 worktree 真实字节/diff 复核、直接替代解释扫描、并发幂等持久化、
  双通道与源码不落 artifact。
- [EVO-04.5a Reward-hacking Evidence Contract](EVO-04-5a-reward-hacking-evidence-contract.md)：
  Counterfactual ID 单入口、Final/Lane/Cohort authority 重读、行为退化/代理分歧/平台选择性/资源换分检查、
  `clear/concern/inconclusive` 三态、双通道与并发幂等持久化。
- [EVO-04.6a Decision State Contract](EVO-04-6a-decision-state-contract.md)：Decision Input ID 单入口、
  veto/pass 分支 authority 重读、四态确定性优先级、Reviewer advisory 隔离、HAR-10.6 兼容 escalation、
  并发幂等持久化与非 promotion 边界。
- [EVO-04.6b Escalation Resolution Contract](EVO-04-6b-escalation-resolution-contract.md)：真实
  create-before-display、answer-before-resolution、重启重读、冲突答案 fail closed、双通道与不可变非 promotion
  用户回执。
- [EVO-04.7a Reflection Memory Contract](EVO-04-7a-reflection-memory-contract.md)：Decision Input 单入口、
  Decision/Resolution exact authority 重读、八类确定性 lesson/action 投影、非向量/非自动召回安全边界、
  append-only 撤销、并发幂等和 Slash/Agent Tool 双通道。

EVO-04 的决策与 Reflection Memory 闭环已交付。它有意止于 `promotion_review_ready`；实际 promotion、发布与
rollback 属于 EVO-05，不是 EVO-04 的隐式完成条件。

## 明确未完成

- EVO-05 已交付 Package、Requirement 和 role response；专业角色身份/签名、最终聚合、rebase/revalidate、
  分阶段发布、监控与 rollback 仍未完成。

## 下一步

实现 EVO-05.2c Identity/Signature Receipt Authority；仍不合并或发布。
