# EVO-05.7b4c Stable Promotion Outcome Ledger

## 1. 依赖裁决

7b4b 已把运行健康与治理选择分开：只有 current、动态有效的 `promote` Decision 才能进入本切片。
`reject`、`defer`、stale Decision、stale Eligibility 或仅有 bypass permission receipt 均不能形成 promoted Outcome。

本切片只记录“稳定推广已被观察后确认”的治理事实，不执行发布、Git、代码修改或策略学习。EVO-06 仍是
learning authority 的独立 gate。

## 2. Proposal-scoped projection head

Ledger 以 `(workbench_session_id, workbench_proposal_id)` 隔离。原因是 Eligibility 和 Decision 都是单次观察/审批
artifact；同一 Proposal 后续可能基于新的 Population Assessment 重新形成 Eligibility 和 promote Decision。若按单个
Eligibility 隔离，就无法表达新 Outcome 替代旧 Outcome。

每次写入原子产生一对 artifact：

1. `EvolutionStablePromotionOutcome`：不可变 `status=promoted` 历史事实；
2. `EvolutionStablePromotionOutcomeSupersedeEvent`：推进 Proposal projection head 的 append-only event。

首个 Event 的 prior 为空且 `prior_outcome_superseded=false`；后续 Event 必须同时链接 previous Event 与 prior Outcome，
并固定 `prior_outcome_superseded=true`。历史 Outcome 不删除、不覆盖，artifact 内的 `superseded=false` 表示其内容不可变；
动态 View 依据当前链头投影 `superseded=true|false`。

## 3. promoted Outcome 内容

Outcome 冻结：

- exact Decision ID/SHA 与 Eligibility ID/SHA；
- Contract、Population Assessment；
- exact Population denominator/passing 与 100% assessment/duration coverage；
- Workbench Session/Proposal、Evolution Proposal；
- Candidate ID/revision/SHA/version/target；
- sequence、prior Outcome ID/SHA；
- `promoted_at` 精确取用户 Decision 的 `decided_at`。

Outcome 固定：

- `promoted=true`；
- `post_observation_decision_verified=true`；
- `population_sustained_health_verified=true`；
- `long_term_metrics_recorded=true`；
- `learning_authority=false`；
- `promotion_authority=false`；
- `execution_authority=false`。

这里的 `promoted` 是 Outcome 状态，不是执行授权。它不会再次部署候选，也不会让 Agent 自动改代码。

## 4. 原子 writer fence

Store 与 Decision/Eligibility Store 共享 session SQLite。每次 `record()`：

1. strict 解析待写 Outcome/Event 并检查 exact pair；
2. `BEGIN IMMEDIATE`；
3. 有界重放 Proposal 下全部 Outcome/Event，验证 sequence、双 previous hash 与 pair identity；
4. 从 durable JSON 重读 exact promote Decision 与 Eligibility；
5. 确认 Decision 是该 Eligibility 最新 revision，Eligibility 是该 Contract 最新记录；
6. 机械重建 Outcome/Event，并与待写内容完整比较；
7. 在同一事务中插入 Outcome 和 Event。

Decision ID 唯一；相同 Decision 并发记录幂等收口。不同 Decision 竞争同一 sequence、链头变化、缺失 Event、索引/JSON
不一致、跳 sequence 或链长超过 10000 均失败关闭。单 artifact 上限 1 MiB。

## 5. 动态 authority 与 supersession

`inspect()` 每次重验：

- durable Outcome/Event pair；
- 完整 Proposal hash chain；
- exact Decision 仍为 current promote authority；
- exact Eligibility 仍为 current review-ready authority；
- Outcome 仍为 Proposal projection head。

只有全部成立时 `promoted_outcome_authority=true`。新 promoted Outcome 出现后，旧 Outcome View 投影
`superseded=true`、`promoted_outcome_authority=false`，同时保留完整历史。其他损坏或 source stale 只撤权，不能冒充
supersession。

## 6. 双通道

- Agent Tool：`evolution_stable_promotion_outcome`；
- 记录：`/evolution stable-promotion-outcome record <decision-id>`；
- 重验：`/evolution stable-promotion-outcome inspect <outcome-id>`；
- Tool 与 Slash 共用 Service/renderer；New UI 与 Textual TUI 消费共享 Tool Result；
- permissive/moderate/strict/bypass 无工具级二次确认，lockdown 拒绝；
- Tool schema 没有 action-level `promote` 参数，只能消费已经存在的 promote Decision。

## 7. 验收证据

- [x] strict 四成员 Population → Eligibility → durable 用户 promote Decision → promoted Outcome；
- [x] reject/defer Decision 被机械拒绝；
- [x] 相同 promote Decision 并发写入幂等；
- [x] 同一 Proposal 第二个真实 Assessment/Eligibility/Decision 形成 sequence 2；
- [x] sequence 2 同时链接 prior Outcome 与 previous Event；
- [x] 旧 Outcome 动态投影 superseded 且 authority 撤销；
- [x] Event JSON 篡改使完整 chain fail-closed；
- [x] forged `promoted=false` 被 strict model 拒绝；
- [x] Engine、lazy exports、Tool、Slash、权限同源；
- [x] targeted pytest、Ruff、compile、文档治理和 diff check 通过；
- [x] 按用户要求未运行全量测试。

## 8. 自我审视与后续边界

本切片已形成真实 promoted/superseded durable ledger，但尚未把它加入
`EvolutionProposalOutcomeProjection`。下一最小切片应升级 typed Proposal Outcome projection，并让 Workbench/New UI/Textual TUI
显示 promoted 当前态、历史 supersession 与撤权原因。随后才能进入 EVO-06 policy learning gate；不得由本 Outcome 自动学习。
