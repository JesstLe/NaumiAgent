# HAR-10.8f2d Pursuit 终态 Outbox 可验证死信权威

## 目标

HAR-10.8f2a 的 capped backoff 会永久保留不可恢复记录，但 poison record 会被无限认领。8f2d 在不改写
既有 outbox/dispatch v1 规范化 JSON 的前提下，增加 append-only failure authority：安全等待继续退避，
暂时性基础设施故障按独立失败预算重试，机械不变量破坏或预算耗尽后停止自动领取并进入可审计死信。

## 权威模型

### `pursuit_terminal_outbox_failures`

每个失败事件包含：

- 精确 `outbox_id` 与 pending outbox SHA-256；
- 精确 claimed dispatch SHA-256、claim 对应的 `attempt_count`；
- 单 outbox 连续 sequence 与前一失败摘要；
- bounded `failure_code`；
- `retryable`、`retry_exhausted` 或 `permanent` disposition；
- `automatic_retry_authority`、`dead_letter_authority`、`manual_review_required` 三个互斥机械事实；
- 失败时间，以及仅 retryable 允许携带的 `retry_at`。

`pursuit_terminal_outbox_failure_heads` 另存链尾 sequence、摘要和 dead-letter 标志，作为领取查询的稳定
权威指针。Store 读取时重新计算 payload 摘要、哈希链、事件 ID，并验证 head 与链尾一致、事件引用的
dispatch 确实是该 outbox 的
CLAIMED 事件、attempt count 一致且失败发生在 claim 有效期内。任何篡改、断链、跨 outbox 引用或在死信后
继续追加都会失败关闭。

失败事件追加与 dispatch 从 CLAIMED 释放为 IDLE 位于同一 `BEGIN IMMEDIATE` 事务中。retryable 事件把
`next_attempt_at` 写为未来时间；dead-letter 事件不授予 retry authority，领取 SQL 通过 `NOT EXISTS`
机械排除。原 pending outbox 不删除，仍作为人工审查和后续受控处置的事实根。

## 分类与失败预算

| 分类 | 代表代码 | 行为 |
|---|---|---|
| 安全等待 | `grace_period_active`、`live_heartbeat`、`live_lease`、`lease_claim_conflict` | durable backoff，但不写 failure event、不消耗失败预算 |
| 可重试故障 | `authority_unavailable`、`authority_read_failed`、`authority_mutation_failed`、`reconcile_store_failed`、时钟回退 | 写 retryable failure；达到 `max_attempts` 时写 `retry_exhausted` |
| 永久不变量破坏 | 缺失/无效 lease、fence 未推进、terminal evidence 缺失或不一致，以及未知新代码 | 首次即写 `permanent`，要求人工审查 |

未知代码默认永久隔离，避免未来新增结果码在没有分类审查的情况下形成无限重试。失败预算配置为
`harness.pursuit_terminal_outbox.max_attempts`，默认 8，范围 1..1000。该预算只计算 failure event，
不使用 dispatch 的总认领次数，因此长时间存活的正常执行者不会被误判为 poison record。

## New UI 与 TUI

Goal terminal-outbox projection 升级到 schema v2，新增：

- `counts.dead_letter`：当前 pending outbox 中拥有死信权威的数量；
- `dead_lettered_count`：当前 worker 生命周期累计进入死信的数量；
- `dead_letter_present` failure code 和中文“自动重试已停止，请人工审查”提示。

Bridge/Python projection 是唯一事实来源；New UI 不扫描 SQLite。前端可读取旧 schema v1，并规范化为 v2
且补零，因此升级期间不会因旧 Bridge 记录崩溃。Goal Tool/Textual fallback 与 New UI 使用同一投影和颜色：
死信为 degraded/red，普通退避仍为 yellow。

## 验收证据

- retryable failure 第一次授予未来 retry，达到预算后形成 `retry_exhausted`；
- 重启 Store 后失败链、dead-letter backlog 和领取排除保持一致；
- failure payload 篡改被摘要校验拒绝，删除事件链尾被独立 head 检出且仍不可重领；
- 真实 live RunLease 只产生 safe wait，不消耗 `max_attempts=1` 的预算；
- 缺失历史 RunLease 的真实 reconcile 场景首次即形成 permanent dead letter；
- Python Goal projection、New UI protocol v1→v2 兼容和终端渲染均显示死信；
- 仅运行 dead-letter、Goal projection、config 和相关前端协议小模块，不运行全量测试。

## 自我审视与后续边界

- 本切片只建立“停止自动重试并可见”的 durable authority；后续 HAR-10.8f2f 已提供 manual requeue 与
  权限化处置回执，accept/abandon 和 operator note 仍未实现。
- pending outbox 与 failure history 尚无 retention/prune；在受控处置和引用完整性设计完成前不能删除。
- explicit run v1 回执继续用 `failures + failure_codes` 表达本轮产生死信，尚未增加独立 dead-letter count；
  Goal v2 health 已提供累计和 backlog 事实。
- HarnessStore fencing 与 PursuitStore 仍是跨库 at-least-once 收敛，而非跨库原子提交。
- 后续 HAR-10.8f2e 已先实现认证、identity-redacted 的死信审查目录，HAR-10.8f2f 再以稳定
  `dead_letter_id` 实现权限化 exact requeue 回执。retention preview/apply 仍须等待 accept/abandon 权威，
  不能先 prune 尚未被运维确认的事实。
