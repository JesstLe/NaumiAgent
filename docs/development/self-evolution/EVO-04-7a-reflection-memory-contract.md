# EVO-04.7a Reflection Memory Contract

## 目标

把不可变 `EvolutionDecisionState` 与可选 `EvolutionDecisionResolution` 投影成最小、结构化、可撤销的
Reflection Memory，供未来显式 policy review 使用。该记录不是聊天长期记忆，不进入向量索引、自动召回或
系统 Prompt，也不授予 Candidate acceptance、promotion 或 Git 写权限。

## Authority 边界

唯一创建入口是 `decision_input_id`。Executor 必须从 durable Store 重读：

1. 与该 Decision Input 绑定的 exact `EvolutionDecisionState`；
2. 仅当状态为 `escalated` 时，重读与 Decision ID、digest、workspace 完整绑定的 Resolution；
3. 非 `escalated` 路径出现 Resolution 或 `escalated` 路径缺少 Resolution 时 fail closed；
4. Builder 再次验证嵌套 authority、digest 和状态投影，调用者不能提交 lesson、signal 或 action。

Reflection 只保存 authority ID/digest，不复制 Gate、Reviewer narrative、Counterfactual findings、Reward-hacking
findings、用户自定义输入、源代码、Prompt、stdout 或绝对 worktree 路径。Reviewer 意见仍是 advisory，不会
借由 Reflection 变成决策事实。

## 确定性投影

| Decision / Resolution | Lesson | Required action | Acceptance decided | Policy learning |
| --- | --- | --- | --- | --- |
| `accepted_experiment` | `validated_experiment` | `review_for_promotion` | 是 | 是 |
| `revise` | `structured_revision` | `revise_candidate` | 是 | 是 |
| `rejected` | `mechanical_rejection` | `terminate_candidate` | 是 | 是 |
| `escalated/evidence_required` | `evidence_gap` | `collect_evidence` | 否 | 是 |
| `escalated/human_review_required` | `risk_escalation` | `human_review` | 否 | 是 |
| `escalated/revise` | `user_directed_revision` | `revise_candidate` | 是 | 是 |
| `escalated/rejected` | `user_directed_rejection` | `terminate_candidate` | 是 | 是 |
| `escalated/custom_follow_up` | `custom_follow_up` | `constrained_custom_follow_up` | 否 | 否 |

`custom_follow_up` 只记录枚举信号 `user_custom_follow_up`；自定义文本既不落 artifact，也不具备 policy-learning
资格。`accepted_experiment` 只设置 `promotion_review_ready=true`，所有 Reflection 都固定
`promotion_executed=false`、`promotion_authority=false`。

## Artifact 与完整性

`EvolutionReflectionMemory` 包含：

- canonical workspace、Candidate ID/revision/risk；
- Decision Input、Decision State 与可选 Resolution 的 ID/digest；
- lesson、required action、结构化 signals；
- 4 至 7 个 typed evidence refs；
- changed files/lines 与 acceptance/readiness flags；
- 明确的非注入、非向量、非 LLM、可撤销 flags；
- aware timestamp、内容 SHA-256 和由摘要前 24 位派生的稳定 ID。

Validator 不只检查摘要，还重新验证以下投影：

- state/outcome 与 lesson/action/acceptance/learning 的完整映射；
- direct、revise、escalated signals 的允许集合与确定顺序；
- veto 路径只含 4 个必要 refs，pass 路径必须有 Counterfactual/Reward-hacking，escalated 还必须有 Resolution；
- evidence kind 与 authority ID 前缀匹配，Decision/Resolution ref 与顶层 ID/digest 一致；
- workspace canonical、安全字符、时区和重复 kind/signal。

Store 在 `memory.session_db_path` 的独立表保存 artifact，不调用 `LongTermMemory.store()`，因此不会写 Chroma、
触发 `recall_for_session()` 或被 `_inject_relevant_memories()` 注入上下文。同一 Decision State 只能对应一个
Reflection；并发/重复调用幂等收敛，不同内容冲突 fail closed，表索引与 JSON artifact 读取时交叉验证。

## 撤销语义

撤销不删除或覆盖原记录。`EvolutionReflectionMemoryRevocation` 是第二个 append-only、摘要绑定的 authority：

- reason 只能是 `incorrect_evidence`、`superseded`、`privacy`、`user_request` 或 `policy_change`；
- 绑定 exact reflection ID/digest/workspace；
- 同一 Reflection 只能有一条撤销记录，并发撤销幂等收敛；
- revoked 记录只保留审计，不参与未来 policy learning 或 promotion；
- Store index 和 JSON 内容不一致时 fail closed。

创建 Reflection 是中风险有界派生写入，normal 无逐次确认；撤销是高风险治理写入，normal 需要确认。
`bypass` 按产品语义全权限直接通过，不触发二次确认；`lockdown` 阻断两者。权限绕过不绕过 authority、digest、
workspace、状态或冲突验证。

## 双通道

- 用户：`/evolution reflection <decision-input-id>`
- 用户撤销：`/evolution reflection-revoke <reflection-id> <reason>`
- Agent Tool：`evolution_reflection_memory`
- Agent Tool 撤销：`evolution_revoke_reflection_memory`

四个入口复用同一 Executor/Revoker 和 renderer。Slash 与 Agent Tool 不实现第二套状态算法。

## 验收证据

- 八种 Decision/Resolution 投影均有固定矩阵测试，不可能组合被拒绝；
- 真实 Final Evaluation → Decision Input → Gate → Independent Review → Counterfactual → Reward-hacking →
  Decision State → Resolution → Reflection 链路通过；
- 同一 Decision 4 路并发只产生一个 Reflection；同一记录 4 路撤销只产生一个 revocation；
- escalated 路径缺 Resolution、workspace 不匹配、非法 ID、artifact flag 篡改和 Store 索引篡改均 fail closed；
- Reviewer summary/concerns 与用户 custom text 不出现在序列化 Reflection；
- mechanical veto 路径不要求下游 Evidence/Resolution，并生成 `mechanical_rejection`；
- Slash/Agent Tool 输出一致；Engine composition 使用独立 Store，而非 `LongTermMemory`；
- Ruff、Python compile 和相关小模块 pytest 通过，不以全量测试冒充本切片证据。

## 明确未完成

- 本切片不创建 Promotion Package、不审批、不 rebase/revalidate、不发布、不监控、不回滚；
- active Reflection 目前只能被显式读取或撤销，没有自动 policy learner；
- 不提供自由文本搜索或向量召回，这是安全边界而非缺失实现；
- Outcome tracking 仍属于 HAR-09.6，promotion/rollback 属于 EVO-05。

## 下一依赖

跨文档最小后续不是直接完成整个 EVO-05，而是先定义 EVO-05.1a Promotion Package Input Contract：仅允许
`accepted_experiment` 且 active 的 Reflection 进入显式 promotion review，并冻结 patch、baseline、全部
receipts、risk、migration 与 rollback plan 引用；它仍不得合并、推送或发布。
