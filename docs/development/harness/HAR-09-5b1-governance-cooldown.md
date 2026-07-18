# HAR-09.5b1 Proposal Governance 与 Cooldown v1

## 目标

在 HAR-09.5a 显式队列之上建立可持久、可审计、并发安全的 Proposal 治理状态机，并让 Evolution
Candidate 后续入队真正受 reject/defer 冷却约束。本切片是 Workbench 决策 UI 与 EVO-02 Experiment
Contract 的共同前置，不直接执行 Proposal。

## 状态与动作

版本：`proposal-governance-v1`。

| 动作 | 前置状态 | 目标状态 | 附加约束 |
|---|---|---|---|
| approve | open | approved | 只表示进入下一 policy gate，不授予执行权限 |
| reject | open | rejected | Evolution Proposal 必须填写原因，固定冷却 30 天 |
| defer | open | deferred | 必须填写原因，截止时间为当前时间后 1 小时至 90 天 |
| merge | open | merged | 仅合并到同 session、同 Candidate、较高 revision 的 open Proposal |
| reopen | rejected/deferred | open | 内部动作，仅在冷却已到期时允许 |

所有治理时间必须是带 UTC offset 的 ISO 时间。状态更新使用 SQLite compare-and-swap；同一动作的并发
重试幂等返回，冲突动作返回 409 语义，不以最后写入覆盖先前人类决定。

## 持久字段

Workbench Proposal 追加：

- `source_occurrence_count`：入队 revision 的证据数；
- `reviewer`、`decision_at`、`decision_note`；
- `cooldown_until`；
- `merged_into_id`；
- `governance_policy_version`。

旧 SQLite 表通过 additive migration 增加字段。历史 rejected 记录若没有可信 cooldown 时间，策略
fail-closed，要求人工复核，不猜测本地时区或自动解除。

## 显著新证据规则

冷却期内只有 Candidate revision 增加，且满足以下任一条件，才允许创建新的 Proposal：

1. 风险等级高于被 reject/defer 的 Proposal；
2. occurrence 相对旧记录增长至少 `max(2, ceil(old_count × 50%))`。

单条新增反馈、相同 revision、仅重新打开 UI、切换 session 或 bypass 都不能越过冷却。显著新证据
只允许重新进入人工审阅，不代表实验资格或修复正确性。

冷却到期后：

- 同 session、同 Preview revision 重新 enqueue 会原地 reopen，并产生 `proposal.reopened`；
- 新 revision 或新 session 可创建新的幂等 Proposal；
- 原 reject/defer 事件继续保留在 audit chain。

## API 与审计

已有 approve/reject 路由改用同一治理 Service，并新增：

- `POST .../proposals/{id}/defer`；
- `POST .../proposals/{id}/merge`。

事件类型为 `proposal.approved/rejected/deferred/merged/reopened`，payload 保存 policy version、冷却截止、
merge 目标和非敏感决策信息。bypass 不绕过 CAS、冷却、merge source 校验或审计。

## 验收标准

- reject/defer 的期限、原因和 policy version 可持久 round-trip；
- defer 小于 1 小时、大于 90 天、无 offset、无原因均拒绝；
- 8 路并发 defer 只产生一个状态转移和一条审计事件；
- approve/reject/defer/merge 的冲突决定不能覆盖已落库决定；
- merge 目标不存在、跨 Candidate、较旧 revision 或非 open 均拒绝；
- 冷却期内单条新增反馈仍被阻断，达到证据增长阈值或风险升级才允许新 revision 入队；
- 同 revision 冷却到期后可 reopen，提前直接调用 reopen 仍被拒绝；
- 历史缺失 cooldown 的 rejected 记录 fail-closed；
- API capability 和 route template 明确公布 defer/merge。

## 明确未包含

- New UI/Workbench Proposal 列表、预览确认和键盘决策操作（HAR-09.5b2 / UI-10.6）；
- 将 Workbench cooldown 状态注入只读 Candidate Eligibility Gate 展示（HAR-09.5b2）；
- approved Proposal 到隔离 EVO-02 Experiment Contract 的转换；
- HAR-09.6 before/after outcome tracking。

因此 `approved` 仍不可执行，HAR-09 与 EVO-01 整体继续保持 partial。
