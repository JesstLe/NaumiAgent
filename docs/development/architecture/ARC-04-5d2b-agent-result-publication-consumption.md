# ARC-04.5d2b Agent Result Publication Consumption

## 1. 用户问题与切片边界

ARC-04.5d2a 已保证每个带 result 的 Agent terminal commit 在同一 SQLite 事务创建 durable
publication，但生产 `SubAgentManager` 仍直接把内存结果发送到进程内 `AgentMessageBus`。如果进程在
terminal commit 后、Bus publish 前退出，父会话无法知道仍有结果待消费；如果把 Bus publish 当作 ACK，
进程重启后又没有 durable subscriber cursor 可以证明消费成功。

本切片把可靠边界定义为同一 `AgentJobStore` 中的幂等 result inbox：

```text
terminal commit + pending outbox
  -> publication claim(owner, lease, epoch)
  -> authenticate request/result/encrypted terminal payload
  -> BEGIN IMMEDIATE
       insert idempotent durable inbox delivery
       append published outbox receipt bound to delivery digest
     COMMIT
  -> best-effort AgentMessageBus wake-up
```

因此：

- result inbox 是 durable ACK boundary；
- `AgentMessageBus` 只是低延迟唤醒与进程内协作通道；
- live manager 与 startup recovery 消费同一个 Store authority；
- New UI/TUI 后续必须读取共享 projection，不能把 Bus history 当成结果账本；
- 本切片不声称任意外部 transport、UI 或跨主机 sink 的 exactly-once。

## 2. AgentJob schema v5

`AgentJobStore` 从 schema v4 升级到 v5，新增
`agent_job_publication_deliveries`：

- `delivery_id`：由 publication 与 result 事实稳定派生；
- `publication_id`、`job_id`、`request_sha256`、`result_sha256`；
- 闭集 `sink=agent_result_inbox`；
- 使用 Runtime payload key 域分离派生的 `session_routing_hmac`；
- `delivery_sha256`、`delivered_at`；
- HMAC 认证的 delivery receipt JSON。

约束与索引：

- publication/job/result/delivery/receipt identity 均有唯一约束；
- session inbox 通过 `(session_routing_hmac, delivered_at, delivery_id)` 稳定排序；
- 不在 delivery 表保存 raw session ID、task、context、response 或 error；
- 原文仍只存在 ARC-04.5d1 的 AES-256-GCM terminal envelope 中；
- v4→v5 只增加 inbox 表和索引，不改写既有 publication；
- 当前 v5 若缺表、列集合漂移或 inbox 索引缺失则 fail closed。

session routing 使用 key-derived HMAC，而不是裸 session digest，避免数据库观察者对常见 session ID
直接建立离线字典。删除或隐藏整行仍需要更高层备份/root manifest 才能发现，本切片不伪造抗删除证明。

## 3. 原子投递与幂等 receipt

`deliver_publication_to_inbox()` 要求 live publication owner 和精确 claim epoch，在同一个
`BEGIN IMMEDIATE` 中：

1. 认证 publication 主表、HMAC event chain 和 terminal AgentJob；
2. 解密 request payload 与 terminal payload并复验 result binding；
3. 计算稳定 delivery ID、routing HMAC 与 delivery digest；
4. 插入 HMAC 认证 inbox delivery；
5. 追加 `published` publication receipt，并绑定同一个 delivery digest/time；
6. 一次提交 delivery 与 published projection。

任一步失败都回滚两边，不能出现：

- inbox 已有结果但 outbox 仍 pending；
- outbox 已 published 但 inbox delivery 缺失；
- delivery 绑定了另一个 request/result/session；
- 公开 digest 被重算后绕过 HMAC。

相同 owner、epoch 和 delivery 事实重放返回 `applied=False`；不同 owner/epoch/digest 被 fencing。
delivery 表的唯一约束阻断同一 publication 写入第二条 inbox 记录。

## 4. 读取与内容恢复

Store 提供两类读取：

- `list_result_inbox(session_id, limit)`：使用 routing HMAC 返回该 session 的低敏 delivery
  receipts，按时间和 ID 排序；
- `recover_delivered_result(delivery_id, expected_delivery_sha256)`：以精确 delivery fence
  重新认证 delivery、published receipt、AgentJob/result 和加密原文后，返回
  `AgentJobPublicationContent`。

列表不返回 raw result。调用方必须持有精确 delivery ID/digest 并通过 Store 重新认证，不能从进程内
缓存或 Bus message 恢复原文。

## 5. 生产 SubAgentManager 在线消费

正常委派完成后的顺序现在是：

1. `finish()` 原子提交 terminal receipt、加密 payload 和 pending publication；
2. manager 用 `recover_terminal_payload()` 构造对用户可见结果；
3. 按 job ID 读取并 claim 精确 publication；
4. 恢复 publication content，并复验当前 task/session/agent/request/result/topic；
5. 原子投递 durable inbox 并写 published receipt；
6. 最后发布 Bus notification。

旧的“completed result 直接 publish 到 Bus”旁路已删除。Bus notification 中携带稳定
`publication_id`、`delivery_id`、`delivery_sha256`、`result_sha256`、status、token/cost 和
`durable_inbox=true`；content 只来自 Store 认证恢复的 terminal payload。

如果 claim/recover/delivery 失败：

- 当前 manager 尝试 release claim，让后续恢复无需等待 lease 到期；
- execution history 写稳定降级码
  `agent_job_publication_delivery_failed`；
- 已认证 terminal result仍可返回给当前调用方；
- publication 保持 pending/可 takeover，不把失败冒充为已发布。

如果仅 Bus notification 失败，durable inbox 与 published receipt不回滚，当前请求仍成功；日志记录
通知失败，但 Bus 不参与可靠性判断。

## 6. 启动恢复

`AgentEngine.start_long_running_services()` 在启动 retention worker 前调用
`SubAgentManager.recover_pending_publications()`：

```text
Evolution patch-set recovery
  -> Evolution patch recovery
  -> Session reconciliation recovery
  -> bounded Agent publication recovery
  -> start periodic retention worker
```

恢复行为：

- 默认最多处理 100 条，公开上限 1000；
- 使用 outbox 的 FIFO `claim_next_publication()`；
- 每条都执行 claim → authenticate → atomic inbox delivery；
- delivery 成功后才 best-effort 通知 Bus；
- 第一条 durable delivery 失败时 release 并停止本轮，避免紧循环反复攻击同一坏记录；
- 记录 content-free `scanned/delivered/notification_failures/failed/failure_codes`
  summary。

这证明 commit-before-deliver 的进程崩溃可以在下次启动自动恢复。当前没有常驻 publication retry
worker：若 live delivery 失败后进程长期不重启，需后续周期 worker或人工动作唤醒。这一缺口不能用
startup one-shot 冒充完成。

## 7. 交付语义

本切片对内置 result inbox 达成：

- outbox publication 至少会在 live path 或后续 startup recovery 被尝试；
- inbox insert 与 outbox published ACK 原子；
- delivery identity 幂等，重复尝试不会产生第二条 inbox record；
- notification 可以丢失或重复，消费者必须以 durable inbox 为准。

它没有证明：

- 外部 Broker/WebSocket/UI 已达到 exactly-once；
- UI 已读/消费 cursor、跨设备同步或多 session fan-out；
- Bus subscriber 执行成功已经持久记录；
- 跨主机数据库复制或共识；
- 数据库整行删除可由当前 receipt chain 独立发现。

所以对外仍使用“durable idempotent inbox + best-effort notification”，禁止笼统写
“Agent result exactly-once”。

## 8. 聚焦验收

- schema v4→v5 保留既有 publication 并创建完整 inbox；
- 当前 v5 缺 delivery 表时 fail closed；
- running → terminal → pending → claimed → inbox/published 全链路通过；
- delivery insert 后 publication update 故障会回滚 inbox；
- close/reopen 后按 session 列表与精确 digest恢复同一结果；
- wrong session、wrong delivery digest 与 receipt/HMAC 篡改被拒绝；
- SQLite bytes 不包含 raw session、task、context、response/error；
- 正常委派先建立 durable inbox，再发送带稳定 identity 的 Bus notification；
- Bus publish 失败不回滚 inbox 或把 execution 标成失败；
- terminal commit 后注入 live delivery gap，第二个 manager 启动恢复成功；
- startup publication recovery 完成后才启动 retention worker；
- 只运行 AgentJob、SubAgentManager、MessageBus、Store Catalog、Runtime Composition、
  retention startup 等小模块测试，不运行全量测试。

## 9. 自我审视与下一依赖

本切片没有完成：

- 周期 retry/backoff、最大 attempt、quarantine/dead-letter；
- publication/inbox retention、GC、备份 root 与删除检测；
- 未读数、详情分页/导出、人工重试/隔离；统一只读 inbox projection 已由
  `ARC-04.5d2c-agent-result-inbox-projection.md` 完成；
- 多 session durable cursor、read/ack 或用户删除语义；
- 独立 Agent worker、Supervisor 与跨进程 shutdown/drain；
- 跨平台打包后的 crash-point/kill -9/disk-full/lock contention soak；
- 外部 sink adapter 与它自己的 idempotency receipt。

下一步不继续线性扩张完整 ARC-04。`ARC-04.5d2c` 已按上述依赖结论建立只读 shared projection 并同步
New UI/TUI；其后仍应重新审计 Harness、UI 与 Supervisor 依赖。
