# EVO-05.7b4d Stable Promotion Proposal Outcome Projection

## 1. 目标与依赖

7b4c 已持久化 Proposal-scoped promoted Outcome 与 supersession hash chain，但 Workbench 原有
`EvolutionProposalOutcomeProjection v2` 只识别 `rolled_back|rollback_recovery_observed`。因此真实 promoted
结果只能通过 Tool/Slash 查看，Reviews、新 UI 和 Textual TUI 不能展示同一权威状态。

本切片把现有 Proposal Outcome 协议升级为 v3 联合投影。它消费 7b4c current ledger head，不创建第二套 UI 状态源，
也不授予 policy learning、代码修改、发布或执行权限。

## 2. 联合终态协议

`EvolutionProposalOutcomeProjection v3` 支持三种互斥终态：

1. `rolled_back`：保留既有 rollback root；
2. `rollback_recovery_observed`：保留既有 post-rollback long-term head；
3. `promoted`：绑定 current Stable Promotion Outcome head。

Rollback 与 promoted 分支不得混用。Promoted 分支固定：

- rollback root、Rollback Receipt、breach、Before/After、Post-Rollback Matrix 均为空；
- `promoted=true`、`long_term_metrics_recorded=true`；
- `learning_authority=false`、`promotion_authority=false`、`execution_authority=false`；
- `contract_issue_allowed=false`，不能再次签发 Experiment Contract；
- `superseded=false`，因为 Workbench 只投影 current Proposal head。

## 3. Promoted lineage

投影携带经过 7b4c Service 动态重验的扁平 lineage：

- current Outcome ID/SHA 与 sequence；
- previous promoted Outcome ID；
- post-observation Decision ID；
- Outcome Eligibility ID；
- Stable Observation Contract ID；
- Population Assessment ID；
- Supersede Event ID/SHA 与 `prior_outcome_superseded`；
- `projection_head_authority`、`stable_promotion_outcome_authority`；
- 有序、去重且有界的 `invalidation_reasons`。

Sequence 1 必须没有 previous Outcome 且 `prior_outcome_superseded=false`；sequence 2+ 必须同时具备 previous
Outcome 且 `prior_outcome_superseded=true`。Authority 有效时撤权原因必须为空；authority 无效时必须给出原因。

## 4. Source arbitration 与竞态

`EvolutionStablePromotionOutcomeStore.list_heads_by_session()` 只读取每个 Proposal 的最高 sequence，单次最多 100 项。
Projection Service 随后调用 `inspect()` 重验 durable pair、完整 hash chain、Decision、Eligibility 与 current head。

- 同一 Proposal 同时存在 rollback 与 promoted Outcome：`proposal_outcome_ambiguous`，整批失败关闭；
- 读取 head 后出现新 Outcome，旧 View 已 superseded：`proposal_outcome_source_changed`；
- session 超过 100 个 Outcome：拒绝投影，不静默截断；
- Store、JSON、Event 或 authority 不可用：Workbench 显示 `Outcome source 暂不可用`；
- 历史 Outcome 不删除，当前投影通过 previous Outcome 与 Supersede Event 展示替代关系。

## 5. Bridge 与三端体验

Workbench Service 继续只绑定一个 `ProposalOutcomeReader`，因此 New UI 与 Textual TUI 消费同一 Snapshot：

- Reviews 列表把有效 promoted Outcome 显示为绿色 `promoted`；
- 详情显示 sequence、Decision、Eligibility、Observation Contract、Population Assessment；
- sequence 2+ 显示 prior Outcome 已由当前 Outcome 替代；
- 显示 Supersede Event 与 head/outcome authority；
- authority 失效时保留历史 `promoted` 状态，同时用红色 authority 与撤权原因区分当前有效性；
- 不显示 Rollback Receipt、回滚行为矩阵或 Contract 签发快捷键；
- 明确提示长期观察已记录，但 Learning / Promotion / Execution authority 仍为 false。

Node boundary 同时接受历史 v1/v2 rollback payload 与当前 v3 payload；只有 v3 可以表达 promoted。前端严格拒绝
缺失 Event、非法 ID、rollback/stable 混用、伪造 authority、错误 sequence/prior 关系或 promoted 分支提权。

## 6. 验收标准

- [x] 真实 sequence 2 SQLite ledger head 投影为 v3 promoted；
- [x] 投影保留 Decision/Eligibility/Contract/Assessment/Event lineage；
- [x] rollback、rollback recovery 与 promoted 三分支互斥；
- [x] Engine 将真实 Stable Outcome Store/Service 注入 Proposal reader；
- [x] Workbench Snapshot 输出 `outcome_status=promoted` 并关闭 Contract issuance；
- [x] Experiment Contract issuer 明确认可 promoted 为 terminal Outcome；
- [x] Node 协议兼容 v1/v2，严格验证 v3 promoted；
- [x] New UI 与 Textual TUI 展示同源字段、替代关系、authority 与颜色；
- [x] targeted Ruff、Python、Node protocol/render、真实 SQLite 测试通过；
- [x] 按用户要求未运行全量测试。

## 7. 自我审视与后续边界

本切片完成的是只读 current-head 投影，不是完整历史浏览器。Reviews 能看到 current Event 指向的 prior Outcome，但尚未提供
可展开的全链时间线；这应作为独立 UI 历史审计切片，而不能让前端直接查询 SQLite。

Promoted Outcome 仍固定 `learning_authority=false`。下一步应重新审计 EVO-06 的 policy learning 输入、离线评测、回滚门和
人工控制边界；不得因为 UI 已显示绿色 promoted 就自动修改策略或源代码。
