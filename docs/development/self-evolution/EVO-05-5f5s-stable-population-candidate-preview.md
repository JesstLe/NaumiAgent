# EVO-05.5f5s Stable Population Candidate Preview

## 目标

把 [EVO-05.5f5r](EVO-05-5f5r-stable-completed-run-aggregation.md) 已持久化的单 installation member
Stable Stage Completion receipts，投影为第一个跨安装成员、用户可直接查看的 Population candidate preview。

本切片回答“哪些 Population 成员已留下 passing、breached 或 insufficient 候选，完整 Population 还缺多少成员，
是否存在重复 Intent 或 lineage 冲突”。它不把历史 receipt 上的 authority 当成当前动态 authority，也不形成 stable
rollout 或 promotion。

## Durable source 与读取边界

1. 读取 Evolution evidence SQLite 中每个 Intent 最新的 Stable Stage Completion row；
2. 可指定 `relpopsnapshot_...`，未指定时选择最新完成 receipt 对应的 Snapshot；
3. 每条 `evidence_json` 重新通过 5f5r typed model、source-set digest 与 content identity 校验；
4. SQL row 的 Evidence ID、SHA-256、Intent ID 与 assessed time 必须和 receipt 精确一致；
5. Population 上限 10000，单 receipt 上限 512 KiB，本轮总读取上限 64 MiB；超限、损坏或 SQLite 不可读均失败关闭；
6. 使用 SQLite `mode=ro` 与 `query_only`，预演前后 evidence DB bytes 保持不变。

预演不读取 prompt、工具参数/输出、assistant 正文、API key、环境变量或 installation 私钥。

## 跨成员聚合

同一 Snapshot ID 下，所有 receipt 必须共享 Snapshot SHA-256/sequence/denominator、candidate version/target、rollout
plan ID/SHA-256 与 canonical workspace。按 privacy-bounded `installation_member_id` 去重；一个 member 对应多个当前
Intent 时记录 `duplicate_member_intents` 冲突，而不是任选其一授予资格。

状态优先级：

1. 无 receipt：`empty`；
2. lineage/duplicate conflict：`conflicted`；
3. 任一无冲突 member recorded breached：`breached`；
4. denominator 全覆盖且全部 recorded passing：`candidate_complete`；
5. 其余：`partial`。

`candidate_complete` 只是 durable candidate source 集的机械事实。由于 5f5r Service 的动态 source 重验尚未进入生产
Engine 组合，本切片固定 `dynamic_revalidation_authority=false`、`stable_rollout_authority=false` 和
`promotion_authority=false`。

## 用户与 Agent 双通道

- Agent Tool：`evolution_stable_population_candidate_preview`；
- Slash：`/evolution stable-population-preview [population-snapshot-id] [limit]`；
- CLI、Textual TUI 和 New UI 复用同一个 Slash Router 与 renderer；
- 所有 permission mode 可只读调用，无确认；bypass 不增加确认；
- 默认展开 50 个成员、最大 100，其余通过 `hidden_items` 明示。

Preview 使用 canonical JSON + SHA-256 生成 `evstablepoppreview_...` receipt，source-set digest 绑定本轮全部 durable
Evidence ID/SHA，而不仅是展开页。

## 验收结果

- 两成员 passing Snapshot 得到 `candidate_complete`，但 rollout/promotion authority 仍为 false；
- 同 member 第二个 Intent 形成 `conflicted`，不重复计入 passing；
- receipt JSON digest 篡改失败关闭，非法 Snapshot ID/limit 在查询前拒绝；
- 真实 Stable Intent → Window → 80 个 managed ChatRun → 5f5q Outcome → 5f5r Completion → Preview 得到诚实的
  `partial/insufficient`；
- Agent Tool 与共享 Slash 返回同一 Preview；
- 仅运行相关小模块测试，不运行全量测试。

## 自我审视与下一步

本切片已提供跨安装成员可观测性，但没有证明 Snapshot 仍是 current trusted Population，也没有逐 member 调用 5f5r
`inspect()` 动态复验 Plan、Baseline、Window、Outcome、ChatRun 与 heartbeat source，因此刻意命名“候选预演”。

下一独立切片必须补 percentage/stable evidence 的生产只读组合，加载 current Population trust policy，并逐 member 动态
调用 5f5r；只有 exact current Population 全员通过时，才可设计 Stable Population Completion Authority。在此之前不得接
promotion executor。
