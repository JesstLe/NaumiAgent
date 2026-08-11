# EVO-05.7b4b Stable Promotion Outcome Decision

## 1. 依赖裁决

7b4a 只证明 current exact Population 已达到独立 Outcome 审批条件，不能把运行健康直接解释为 promoted。
本切片新增显式 post-observation Decision，并把决定权留给真实本地用户：Agent Tool 没有
`promote`、`reject` 或 `defer` 参数，只能请求用户交互或读取已有 Decision。

permission/bypass 回执只表示“允许调用工具”，不表示用户选择了某个治理结果。即使处于 bypass，Runtime 仍必须通过
Harness durable interaction 收到用户的三选一回答，不能自动代答。

## 2. 用户决策协议

每个 current Eligibility revision 提供三个固定选项：

- `promote`：确认观察结果，可供 7b4c 消费；本切片不写 promoted Outcome；
- `reject`：形成当前终态拒绝，不自动回滚、修改代码或学习；
- `defer`：形成精确 7 天冷却，到期后才允许追加下一 revision。

交互固定 `allow_custom=false`，避免自然语言被误解析为治理动作。Interaction ID 绑定 Eligibility digest suffix 与
Decision revision；问题正文绑定 Candidate、Candidate revision、Population numerator/denominator。Service 在 callback
返回后从 Harness Store 重读 answered authority，并在写入前再次确认 Eligibility 和前序 Decision 未变化。

## 3. durable Decision

`EvolutionStablePromotionOutcomeDecision` 冻结：

- exact Eligibility ID/SHA、Contract、Population Assessment；
- Workbench Session/Proposal 与 Evolution Proposal；
- Candidate ID/revision/SHA；
- append-only revision 与 previous Decision ID/SHA；
- exact Harness Interaction 与 digest；
- `answered_by`、local session、`answered_at`；
- action 与机械 `defer_until`；
- source/content SHA 与 deterministic ID。

Decision 固定：

- `independent_post_observation_decision=true`；
- `outcome_decision_authority=true`（仅在动态 View current 时成立）；
- `promoted_outcome_authority=false`；
- `learning_authority=false`；
- `promotion_authority=false`；
- `execution_authority=false`；
- `llm_generated=false`。

## 4. 双库 fence 与并发

Harness interaction 和 evolution session ledger 允许位于不同 SQLite，这是现有 Runtime 的真实组合。answered interaction
是终态不可变 authority，因此 Store 先通过 Harness API 精确重读完整 record/digest，再在 evolution DB 中使用
`BEGIN IMMEDIATE`：

1. strict 重读 exact Eligibility JSON；
2. strict 重读 revision-1 Decision；
3. 机械重建待写 Decision；
4. 以 source SHA 和 `(eligibility_id, revision)` 唯一约束 CAS 写入。

相同 source 的并发写入幂等收口；相同 source 不同内容、跳 revision、非 defer 前序、未到期 defer、不同 interaction
均失败关闭。artifact 上限 1 MiB。

## 5. 动态撤权

`inspect()` 每次重验：

- Decision durable JSON 与索引；
- 它仍是该 Eligibility 最新 Decision；
- 7b4a Eligibility 仍具 review-ready authority；
- Harness Store 中 exact answered interaction 仍存在且 digest 相同。

任何 Eligibility stale、Population authority 撤销、Decision/Interaction 损坏或新 revision 出现，都会令
`current_decision=None` 并撤销 Outcome authority。只有 current `promote` View 才投影
`promoted_outcome_ready_authority=true`，该字段仍不是 promoted Outcome。

## 6. 双通道

- Agent Tool：`evolution_stable_promotion_outcome_decision`；
- 发起：`/evolution stable-promotion-outcome-decision decide <eligibility-id>`；
- 重验：`/evolution stable-promotion-outcome-decision inspect <decision-id>`；
- Tool 与 Slash 共用同一 Service/renderer；New UI 与 Textual TUI 消费共享 Tool Result；
- permissive/moderate/strict/bypass 均不增加工具级二次确认，lockdown 拒绝；
- 用户三选一是业务治理输入，不是风险确认，bypass 不会伪造该输入。

## 7. 验收证据

- [x] strict 四成员 passing Population 形成 Eligibility 后，由真实 durable interaction 选择 promote；
- [x] promote Decision 只投影 7b4c readiness，不产生 promoted/learning/execution authority；
- [x] reject 与 defer 使用相同 exact request 机械建模；
- [x] defer 使用精确 7 天冷却，只有到期 defer 可形成下一 revision；
- [x] 相同 Decision 并发写入幂等；
- [x] Eligibility 撤权后 Decision 与 7b4c readiness 动态撤销；
- [x] Agent Tool schema 不含治理动作参数；
- [x] Engine、lazy exports、Tool、Slash、权限同源；
- [x] targeted pytest、Ruff、compile、文档治理和 diff check 通过；
- [x] 按用户要求未运行全量测试。

## 8. 自我审视与后续边界

本实现没有把原 rollout approval 冒充观察后决定，也没有把 bypass 冒充用户意图。它刻意不写 promoted ledger、
不 supersede 历史 Outcome、不更新策略、不修改代码。下一步 7b4c 只能消费 current、动态有效的 `promote` Decision，
在一个 append-only hash chain 中原子写 promoted Outcome 与 supersession；reject/defer 不能进入该写路径。
