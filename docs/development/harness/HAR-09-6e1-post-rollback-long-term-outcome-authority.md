# HAR-09.6e1 Post-Rollback Long-Term Outcome Authority

## 目标

把 `HAR-09.6d3` 当前有效的 passing Assessment 与 exact `rolled_back` Outcome、`HAR-09.6d1` Observation
Contract 及 recovered Behavioral Matrix 组合成新的内容寻址 Outcome revision，并在同一 SQLite 事务内追加
Outcome supersede event。本切片首次把长期指标写入 Outcome 链，但不修改历史回滚事实，不把恢复后的 baseline
误报为 Candidate promotion，也不授予 learning、promotion 或 execution authority。

## Artifact 模型

### Long-Term Outcome revision

`EvolutionPostRollbackLongTermOutcome` 固定状态为 `rollback_recovery_observed`，并冻结：

- root `rolled_back` Outcome ID/SHA 与 Rollback Request；
- revision sequence、prior Outcome kind/ID/SHA；
- Observation Contract、Behavioral Matrix、Assessment 与 Runtime Admission；
- Workbench Session/Proposal、Candidate identity 与 revision；
- baseline slot/version/target 与 managed runtime identity/binding；
- Assessment ledger head、持续秒数与 operational sample 数；
- `rollback_fact_preserved=true`、`behavioral_recovery_verified=true`；
- `long_term_metrics_recorded=true`、`baseline_sustained_health_verified=true`；
- `candidate_promoted=false`、`promoted=false`；
- `learning_authority=false`、`promotion_authority=false`、`execution_authority=false`。

Artifact ID 为 canonical JSON SHA-256 的 24 字节前缀；workspace 必须 canonical，时间必须带 offset，NaN/Infinity、
额外字段和不一致 identity 均拒绝。

### Outcome supersede event

`EvolutionPostRollbackOutcomeSupersedeEvent` 是独立内容寻址 ledger：

- sequence 1 从 immutable `rolled_back` Outcome 指向首个 Long-Term Outcome；
- 后续 sequence 从上一 Long-Term Outcome 指向新 revision；
- previous event ID/SHA 形成 hash chain；
- transition 固定为 `rollback_recovery_observed`；
- reason 固定为 `behavioral_and_long_term_baseline_recovery_verified`；
- `projection_head_changed=true`、`rollback_fact_deleted=false`；
- promotion、learning、execution authority 始终 false。

历史 `EvolutionRevalidationRollbackOutcome` 不做原地更新，其 `status=rolled_back` 和 `superseded=false` 仍是签发时事实。
“supersede”只表示新的治理投影 head，由独立 append-only event 证明，不能静默改写历史 artifact。

## 原子 Store 与并发规则

Session SQLite 新增 Outcome revision 表和 supersede event 表。写入流程固定为：

1. 在内存中完整复验 Outcome、Event 与 root 的 content identity；
2. `BEGIN IMMEDIATE`；
3. 逐字节重验 durable root Outcome、Observation Contract 和 passing Assessment JSON；
4. 读取当前 Outcome head 与 event head；
5. 对账 revision sequence、prior ID/SHA 和 previous event ID/SHA；
6. 在同一事务写入 Outcome 与 event 后 commit。

同一 Assessment ID 唯一绑定一个 Outcome/Event pair；完全相同的并发请求幂等收敛。若另一 Assessment 已推进 head，
旧写入返回 `post_rollback_long_term_outcome_head_changed`，Service 最多重新读取并重建三次；不采用 last-write-wins。
每次写入和 inspect 都从 immutable root 顺序重放完整 Outcome/Event 链，逐项验证 sequence、prior、previous-event hash
和 pair identity；链长硬上限为 10000。所有索引列在 restore 时与 JSON 逐字段比对，任一历史 event 删除、row/JSON
篡改或双 head 均失败关闭。

## 动态 authority

`EvolutionPostRollbackLongTermOutcomeView.outcome_authority` 每次 inspect 重新计算，只有以下条件同时成立才为 true：

1. durable Outcome/Event pair 完整一致；
2. root `rolled_back` Outcome authority 当前有效；
3. Observation Contract authority 当前有效；
4. Outcome 绑定的 passing Assessment artifact 仍是 exact durable source；
5. 同一 Admission 的最新 Harness ledger 仍产生当前 long-term health authority；
6. 该 revision 仍是 request 的 projection head。

新 Assessment 推进后旧 revision 自动失去 projection-head authority；heartbeat stale、failed sample、上游撤权、
durable source 变化或篡改都会动态撤销当前 Outcome authority。撤权不删除任何历史记录，也不自动执行 rollback。

## 双通道

- Agent Tool：`evolution_post_rollback_long_term_outcome`；
- 共享 Slash：`/evolution outcome-record-long-term <rollback-request-id> <runtime-subject-id>`；
- Tool 与 Slash 共用同一 Service；
- 该操作只写治理 artifact，不改 workspace、不启动进程，Moderate 与 Bypass 均无需二次确认。

回执展示 revision、prior Outcome、Assessment、Runtime、持续健康样本、supersede event、projection-head authority，
并显式声明历史 rollback fact 保留和三类高权限为 false。

## 验收标准

- [x] 真实 Harness startup + 13 个连续 operational samples 产生 passing Assessment 后签发 revision 1；
- [x] insufficient Assessment 被机械拒绝，不能签发 Outcome；
- [x] revision 1 的 prior 精确指向 immutable `rolled_back` Outcome；
- [x] Outcome/Event 在单事务落盘，root Outcome 保持逐字节不变；
- [x] 同一 Assessment 并发签发收敛；
- [x] 新 heartbeat head 产生新 Assessment 后追加 revision 2 与 previous-event hash chain；
- [x] revision 2 生效后 revision 1 动态失去 projection-head authority；
- [x] heartbeat 超时后当前 revision 动态撤权；
- [x] 删除历史 supersede event 后，即使当前 pair 完整也会因全链不连续而撤权；
- [x] durable event row 篡改失败关闭；
- [x] content-addressed 但错绑 Contract/Assessment 字段的 Outcome 在 Store 边界被拒绝；
- [x] Tool/Slash 共用 Service，Moderate/Bypass 无确认和二次确认；
- [x] 局部 pytest、ruff、compile、文档治理与 diff check 通过；未运行全量测试。

## 自我审视与当前边界

本切片没有把 passing baseline 恢复冒充 Candidate promotion，也没有用 mutable `superseded` 布尔值覆盖历史。当前仍有：

1. Workbench/New UI/TUI 的 Proposal Outcome 聚合尚未消费新 head；下一切片应扩展 typed projection，而不是让 UI
   直接查询 SQLite；
2. 这是 rollback recovery observation Outcome，不是成功 rollout 的 `promoted` Outcome；
3. 配置/数据 rollback、真实 Windows/Linux daemon 生产证据仍需独立完成；
4. policy learning 必须等待独立 promotion policy、approval 与长期效果反馈门禁，不能由本 artifact 直接触发。

下一独立切片为 `HAR-09.6e2 Long-Term Outcome Projection Parity`：把 root + supersede ledger + current revision
统一投影到 Workbench/New UI/Textual TUI，并在 head 不唯一、event chain 损坏或 authority 撤销时失败关闭。
