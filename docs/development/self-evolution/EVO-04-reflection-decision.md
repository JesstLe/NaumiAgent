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

## 子模块

- EVO-04.1 Decision inputs：candidate、mutation receipt、Eval receipt、risk、user constraints。EVO-04.1a 已完成。
- EVO-04.2 Mechanical gate：checks、guardrails、scope、budget、integrity 先判。EVO-04.2a 已完成。
- EVO-04.3 Independent reviewer：可选不同模型/规则，看到证据但不能改结果。
- EVO-04.4 Counterfactual：是否有更小改动、改善是否来自删测试/改指标/放宽规则。
- EVO-04.5 Reward hacking detector：测试删除、阈值放宽、skip、mock 替代、数据泄漏。
- EVO-04.6 Decision state：accepted_experiment/revise/rejected/escalated。
- EVO-04.7 Reflection memory：只保存结构化经验和证据引用，禁止污染系统 Prompt。

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

EVO-04 整体仍为 partial；Decision Input 完成不代表 mechanical gate、review 或最终决策已交付。

## 明确未完成

- EVO-04.3 independent reviewer；
- counterfactual、reward-hacking detector、decision state、reflection memory 与 promotion。

## 下一步

实现 EVO-04.3a Independent Reviewer Contract：只读取 Gate authority，记录 reviewer/author 隔离、模型、
provider 与 Prompt digest；`veto` 只能被解释，不能被改写为 `pass`。
