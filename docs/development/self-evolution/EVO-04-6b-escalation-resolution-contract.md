# EVO-04.6b Escalation Resolution Contract

## 目标

把 EVO-04.6a 的静态 escalation payload 接入 HAR-10.6 持久交互 authority，并把真实用户答案固化为独立、
不可变、可审计的 `EvolutionDecisionResolution`。本切片保证：

- 问题先进入 Harness Store，再由 New UI/TUI 显示；
- option/custom 答案先通过 owner、epoch、sequence 与 lease fencing，再形成 Resolution；
- 重启后复用 pending/answered authority，不依赖原进程 Future；
- LLM、Slash 参数和 Tool 参数都不能伪造用户答案；
- 用户回答仍不能直接接受 Candidate、开放 promotion review 或执行 promotion。

## 双通道入口

- `/evolution decision-resolve <decision-input-id>`
- `evolution_decision_resolution(decision_input_id=...)`

两种入口复用同一 `EvolutionDecisionResolutionService`。调用者只能提交 Decision Input ID，不能提交
interaction ID、answer、action、outcome 或 readiness flag。

Service 从 `EvolutionDecisionStateStore` 重读唯一 Decision State。非 `escalated` 状态 fail closed，不会为了
accepted/revise/rejected 再询问用户。

## Create-before-display

Service 对每个 Decision State 使用
`ask-evolution-<decision-digest-prefix>-<attempt>` 稳定 interaction ID，并把 subject 固定为：

- `subject_kind=tool`；
- `subject_id=<decision-state-id>`；
- request 必须逐字段等于 Decision State 内嵌 escalation。

实际 create/answer 仍由 HAR-10.6 New UI/TUI adapter 完成。Engine 只允许内部可信调用方指定合法
interaction ID 与 `tool/browser/agent/runtime` subject；Pursuit Context 存在时仍强制覆盖为 pursuit，避免
旁路其 checkpoint fencing。

同进程并发调用按 Decision Input 单飞。跨进程并发即使竞争，也会由相同 attempt ID 和 Harness 唯一键阻止
出现两个可显示问题；失败方不得绕过 Store 自行返回答案。

## 恢复与冲突语义

调用前有界读取该 Decision State 的 interaction history：

1. 已有 Resolution：直接返回不可变 artifact；
2. 有唯一 answered、request/subject/ID 全部匹配：从 Store 重建 Resolution，不再询问；
3. 有 pending：返回 interaction ID，等待 New UI/TUI 重放并回答，不重复显示；
4. 只有 expired/cancelled：使用下一个 attempt ID 重新询问；
5. 多个 answered 且答案冲突：fail closed，不猜测哪个是用户最终意图。

用户 callback 返回的内存 dict 不作为证据。Service 必须在 callback 完成后从 Harness Store 重读 answered
record；若答案未提交、Store 不可用或 record 不匹配，则不生成 Resolution。

## 确定性 Resolution

option 映射固定为：

| 用户选择 | Action | Outcome | Acceptance decided |
|---|---|---|---|
| `collect_missing_evidence` | `collect_missing_evidence` | `evidence_required` | false |
| `request_human_review` | `request_human_review` | `human_review_required` | false |
| `revise_candidate` | `revise_candidate` | `revise` | true，accepted=false |
| `reject_candidate` | `reject_candidate` | `rejected` | true，accepted=false |
| custom | `custom_instruction` | `custom_follow_up` | false |

自定义文本只作为受约束后续输入，绝不能被 LLM 解释为 implicit accept。补证据和人工审查也只是下一动作，
不会把原 escalated Decision State 改写为 accepted。

所有 Resolution 固定：

- `candidate_accepted=false`；
- `experiment_accepted=false`；
- `promotion_review_ready=false`；
- `promotion_executed=false`；
- `user_authority_required=true`；
- `llm_decision_authority=false`。

## Artifact 与 Store

Artifact 绑定并内嵌：

- Decision State ID/digest、Decision Input、Candidate revision；
- answered Harness interaction 完整记录、sequence 与 digest；
- 用户 answer kind/value/label/custom text；
- 固定 action/outcome/readiness 投影；
- content-addressed Resolution ID/digest 与 answered timestamp。

Store 以 Decision State ID 为唯一键，`BEGIN IMMEDIATE` 保证重复签发幂等；同一 Decision 不可覆盖为不同答案。
row index 与 JSON payload 双向校验，单 artifact 上限 40 MiB。

## 权限与界面

- Agent Tool 属于 `evolution_decision_artifact` 中风险派生写入，每会话最多 50 次；
- permissive/moderate/strict 无二次确认，lockdown 阻断，bypass 直接通过；
- bypass 不绕过 HAR-10.6 answer fencing、Decision State digest 或 Store 冲突；
- New UI 与 Textual TUI 继续复用既有 typed interaction card、重放、timeout、cancel 和 answer adapter；
- Resolution 使用共享 Markdown renderer，不在前端复制 action 映射。

## 验收标准

- 真实完整 Evolution 链形成 escalated Decision State；
- callback 观察到 pending 已持久化，answer 后才生成 Resolution；
- 4 路并发调用只显示一次问题并返回同一 Resolution ID；
- option 四种映射与 custom 映射均固定，任何路径都不能接受或 promotion；
- pending 重启态不重复创建问题；expired/cancelled 可以使用新 attempt；
- 冲突 answered authority、非 escalated Decision、错误 workspace/ID 均 fail closed；
- Decision、interaction、readiness、digest 或 Store index 篡改均拒绝；
- Slash/Agent Tool 复用同一 Service 和 renderer；
- Engine composition、权限矩阵、内部 interaction subject 透传通过聚焦测试；
- 不运行全量测试。

## 明确未完成

- `evidence_required` 尚未自动调度新 Evaluation lane；
- `human_review_required` 尚未建立独立人工审查签名 artifact；
- custom instruction 只被记录，尚未进入受策略约束的任务规划器；
- EVO-04.7a Reflection Memory 已完成；
- EVO-05 promotion/rollback 与 HAR-09.6 outcome tracking；
- 跨 Harness/Resolution 两个 Store 仍非单事务，使用 authority-first 与重读恢复收敛；原子 outbox 属于 ARC-05。

## 下一步

EVO-04.7a 已消费本 Resolution 并形成结构化、可撤销、非注入 Reflection。EVO-05.1a Promotion
Package Input Contract 也已完成；下一步是 EVO-05.1b，仍不执行合并或发布。
