# EVO-05.7b4a Stable Promotion Outcome Eligibility

## 1. 依赖裁决

7b3c2 已能证明 current exact Population 全部 passing，但“运行健康”不是“治理决定”。7b1 明确要求独立审批策略，
HAR-09.5 又规定 Proposal 的 promote/reject/defer 必须由人类或治理策略决定。因此 7b4 不能从 passing Receipt 直接跳到
`promoted` Outcome。

本切片只实现 7b4 的最小前置：把 current passing Population Assessment 转换成 durable、可动态撤权的
`EvolutionStablePromotionOutcomeEligibility`。它只表示“可进入独立 Outcome 审批”，固定保持：

- `promoted=false`、`superseded=false`；
- `outcome_decision_authority=false`；
- `learning_authority=false`；
- `promotion_authority=false`；
- `execution_authority=false`。

下一步 7b4b 才能实现显式 post-observation Outcome Decision，7b4c 才能写 promoted/superseded append-only ledger。

## 2. 机械 Eligibility 条件

Service 只接受 7b3c2 动态 View 同时满足：

1. `population_long_term_observation_authority=true`；
2. current Assessment 存在且 `status=passing`；
3. `passing_count == population_denominator`；
4. assessment coverage 与 duration coverage 都为 10000 bps；
5. breached、censored、insufficient、missing 全部为 0；
6. exact Observation Contract 仍有 authority。

任何部分覆盖、缺员、删失、breach、timeout 或 stale source 都机械拒绝，不允许 Agent 通过 reason 文本解释为“足够好”。

## 3. durable artifact

Eligibility 冻结：

- Contract ID/SHA；
- Population Assessment ID/SHA/source-set；
- Population Finalization、Snapshot、sequence、denominator；
- exact passing/coverage facts；
- Workbench Session/Proposal、Proposal ID；
- Candidate ID/revision/SHA/version/target；
- 原始 rollout approval decision ID/SHA，仅作为 lineage，不冒充 post-observation decision；
- `eligible_at` 取 Population Assessment 的 deterministic `assessed_at`；
- `independent_outcome_decision_required=true`。

source SHA 绑定 Contract 与 Population Assessment identity，artifact ID/SHA 使用 canonical JSON 计算。严格拒绝额外字段、
NaN/Infinity、非 canonical workspace、无时区时间和任何 authority 提权。

## 4. writer fence 与并发

Eligibility Store 与 Contract/Population Assessment Store 必须共享 session SQLite。`record()` 使用
`BEGIN IMMEDIATE`，在同一事务内逐字节重读并 strict 解析 exact Contract JSON 与 Population Assessment JSON，随后机械重建
Eligibility 并与待写 artifact 完整比较。

同一 source SHA 唯一。两个 Service 并发记录同一 passing Assessment 幂等收口为一行；相同 source 绑定不同内容失败关闭。
artifact 上限 1 MiB，避免把完整 Population member 集合重复复制进资格回执。

## 5. 动态撤权

`inspect()` 每次重验：

- Eligibility durable JSON 与索引列；
- 它仍是该 Contract 最新 Eligibility；
- Observation Contract authority；
- exact Population Assessment 仍是 current passing authority。

7b3c2 新 Assessment、heartbeat timeout、member/source 漂移、Contract 撤权或 durable JSON 篡改都会使
`current_eligibility=None`、`outcome_review_ready_authority=false`。历史 Eligibility 保留供审计，不自动决定 promote/reject，
也不自动执行 rollback。

## 6. 双通道

- Agent Tool：`evolution_stable_promotion_outcome_eligibility`；
- 记录：`/evolution stable-promotion-outcome-eligibility record <population-assessment-id>`；
- 重验：`/evolution stable-promotion-outcome-eligibility inspect <eligibility-id>`；
- Tool 与 Slash 共用同一 Service/renderer；New UI 与 Textual TUI 继续消费共享 Tool Result；
- permissive/moderate/strict/bypass 均无需二次确认，lockdown 拒绝；
- Tool schema 没有 `promote`、`approve` 或 decision 参数，Agent 无法借此自我批准。

## 7. 验收证据

- [x] strict 四成员 passing Population、100% assessment/duration coverage 形成 Eligibility；
- [x] 两个 Service 并发记录同一 source 幂等收口；
- [x] Eligibility 明确绑定 Proposal/Candidate/原 approval lineage；
- [x] Population authority 撤销后 review-ready authority 动态撤销；
- [x] stale Population 再次 record 被机械拒绝；
- [x] forged `promoted=true` 被 strict model 拒绝；
- [x] Eligibility SQLite JSON 篡改失败关闭；
- [x] Engine、public lazy exports、Tool、Slash、权限同源；
- [x] targeted pytest、Ruff、compile、文档治理和 diff check 通过；
- [x] 按用户要求未运行全量测试。

## 8. 自我审视与后续边界

本切片刻意没有实现 promoted ledger，因为尚无 post-observation 独立 Decision。原 rollout approval 只证明“允许部署”，
不能证明“观察后确认 Outcome”。后续必须保持顺序：

1. 7b4b：独立 Outcome Decision，支持 promote/reject/defer，绑定 eligibility、决策主体、策略与审计；
2. 7b4c：只消费 current promote Decision，原子追加 promoted Outcome 与 supersede hash chain；
3. typed Proposal Outcome projection 同步 Workbench/New UI/TUI；
4. policy learning 必须再经过 EVO-06 的独立 gate，不能由 promoted Outcome 自动改代码。
