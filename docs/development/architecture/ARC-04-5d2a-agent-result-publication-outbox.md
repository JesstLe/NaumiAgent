# ARC-04.5d2a Agent Result Publication Outbox Authority

## 1. 用户问题与切片边界

ARC-04.5d1 已让 Agent 的 terminal result receipt 与加密 response/error 在同一事务提交，并要求生产
manager 从 Store 重新认证恢复原文。但 Runtime 仍可能在 terminal commit 成功后、父会话或 message bus
收到结果前崩溃。此时数据库能恢复结果，却没有权威事实说明“这项结果是否仍待发布、由谁发布、是否已经
确认”。

本切片建立独立、持久、可 fencing 的 publication outbox authority：

```text
durable terminal commit
  -> atomically create pending publication
  -> FIFO claim with owner + lease + epoch
  -> authenticate terminal job and payload binding
  -> publisher attempts delivery
  -> acknowledge exact delivery digest
  -> durable published receipt
```

本切片只交付 Store authority 和恢复目录，不把 `SubAgentManager`、进程内 `AgentMessageBus` 或 UI
接成生产 publisher。因此它不声称完成自动重放，也不声称 exactly-once。生产消费属于
`ARC-04.5d2b`；该后续切片现已完成内置 durable inbox、生产 manager 在线消费与启动恢复，详见
[ARC-04.5d2b](ARC-04-5d2b-agent-result-publication-consumption.md)。

## 2. AgentJob schema v4

`AgentJobStore` 从 schema v3 升级到 v4，新增：

- `agent_job_publications`：每个有 result 的 terminal Job 至多一条当前 publication；
- `agent_job_publication_events`：append-only publication receipt chain；
- `agent_job_publications_recoverable`：按 state、claim expiry、created time 与 publication ID
  支持有界恢复扫描。

主表只保存 Job/request/result digest、publication 状态、租约事实和 receipt，不保存 response/error
明文。原文继续通过 ARC-04.5d1 的加密 terminal payload 恢复。

schema 初始化与迁移遵守以下边界：

- 新数据库在一个 schema 事务中创建 v1-v4 全部结构；
- v1/v2 先补 terminal payload 列，再创建 outbox；v3 只创建 outbox；
- v4 数据库每次 Store 首次使用时复核两张表的精确列集合和恢复索引；
- 只有一张表、列集合漂移或恢复索引缺失时 fail closed，不把部分 schema 当成可用；
- v3 既有 terminal Job 不被迁移脚本猜测性补发；调用方必须以原 owner/epoch、精确 result 和
  terminal payload 重放 `finish()`，Store 才能验证后补 `pending` publication。

最后一项避免升级过程把历史“可能已经发布”的结果误判为“从未发布”并重复发送。

## 3. 原子 publication 创建

新的有 result terminal commit 在同一个 `BEGIN IMMEDIATE` 事务中完成：

1. 复验 live AgentJob owner、claim epoch 与 running state；
2. 写 terminal lifecycle receipt、result 与加密 terminal payload；
3. 计算稳定 publication ID；
4. 写 `pending` publication 主记录；
5. 追加首条 HMAC 认证 publication receipt；
6. 一次提交全部事实。

因此新记录不会出现“terminal 已提交但 outbox 缺失”，也不会出现“outbox 可见但 terminal payload
尚不可恢复”。相同 terminal finish 重放会复验全部 result/payload 事实；publication 已存在时返回
幂等 no-op，缺失时只在完整验证后补建。

pre-claim cancelled 和 recovery `unknown` 没有可发布 result，不创建 publication。

## 4. Publication 状态机与 fencing

状态机为：

```text
pending
  -> claimed(owner, epoch + 1, attempt + 1, lease)
  -> claimed(same owner/epoch, renewed lease)
  -> pending(release)
  -> claimed(same or new owner, epoch + 1, attempt + 1, after expiry)
  -> published(exact owner/epoch, delivery digest)
```

关键不变量：

- `claim_next_publication()` 在 `BEGIN IMMEDIATE` 中按 `(created_at, publication_id)` FIFO 选择；
- 只选择 `pending` 或 lease 已过期的 `claimed`；
- 首次 claim 与 expiry takeover 都单调增加 `claim_epoch` 和 `attempt_count`；
- live owner + exact epoch 才能 renew、release 或 acknowledge；
- renewal 从 `max(now, current_expiry)` 延长，不能意外缩短已有 lease；
- release 清除 owner/expiry 并回到 pending，但保留 epoch/attempt 审计事实；
- acknowledge 清除 expiry，写入 `published_at` 和调用方提供的 `delivery_sha256`；
- 同 owner/epoch/delivery digest 的 acknowledge 重放是幂等 no-op；任何字段不同都 fail closed；
- `published` 是终态，不再进入恢复扫描。

`delivery_sha256` 只证明 publisher 声明的精确投递事实。它不能单独证明下游已经持久消费；
ARC-04.5d2b 后续以同事务写入的 result inbox delivery receipt 定义内置 sink 的 durable boundary。

## 5. 防篡改 publication receipt chain

每次 pending、claim、takeover、renew、release 或 acknowledge 都追加
`AgentJobPublicationReceipt`。receipt schema v1 绑定：

- publication/job ID 与连续 sequence；
- request/result SHA-256；
- state、owner、claim epoch/expiry、attempt count；
- delivery digest、published time、reason 与 occurred time；
- previous receipt digest、当前 receipt digest；
- 使用 Runtime payload key 域分离派生的 HMAC-SHA256 authentication。

读取 publication 时会验证：

1. 主表与 latest receipt 完全一致；
2. event 数量、sequence 与 previous receipt 链连续；
3. pending/claim/renew/takeover/release/published 转移合法；
4. takeover 的 occurred time 不早于旧 lease expiry；
5. 每条 event 的 job/request/result 绑定未漂移；
6. 绑定的 AgentJob 已是带 result 的 terminal state；
7. result digest、request digest 与 publication 一致；
8. terminal payload 仍可由 ARC-04.5d1 的 result fence 认证恢复。

攻击者即使修改 SQLite 并重算公开 digest，也无法伪造 HMAC；主表、事件链或绑定 Job 任一处漂移都会
fail closed。

## 6. 恢复目录与聚合可观测性

Store 提供两个只读 authority：

- `list_publication_recovery(limit)`：列出 pending 与 lease 已过期的 claimed，按稳定 FIFO 排序；
  默认 100，最大 1000；
- `publication_backlog()`：聚合 pending、live claimed、expired claims 与 assessed time；扫描上限
  10000，超过时明确拒绝，避免无界内存和长事务。

两个入口都重新认证 publication event chain 和绑定 AgentJob，不返回 raw response/error、task、
context、owner credential 或密钥。调用方如需实际发布，必须先 claim，再用 publication 的
`job_id/result_sha256` 调用 `recover_terminal_payload()`。

当前没有把这两个 authority 暴露给 New UI/TUI。前端不得自行扫描 SQLite 或从 terminal Job 推断
publication 状态；后续 UI 必须消费共享后端 projection。

## 7. 交付语义：at-least-once 前置，而非 exactly-once

Outbox 关闭了“terminal commit 后没有待发布事实”的 durable gap，但 transport/sink 尚未接入：

- publisher 可能完成外部发送后、acknowledge 前崩溃；
- lease 到期后新 owner 会重新投递相同 publication；
- 当前 `AgentMessageBus` 是进程内实现，没有 durable subscriber cursor 或 idempotent receipt；
- 父会话/UI 也没有稳定 publication ID 的去重合同。

所以本切片只建立可实现 at-least-once 的权威基础。ARC-04.5d2b 后续已让每次投递携带稳定
`publication_id/delivery_id`，并把同一 SQLite authority 内的幂等 result inbox 定义为 durable ACK
boundary；Bus 仍只是 best-effort notification，外部 sink 仍禁止写“exactly-once”。

## 8. 聚焦验收

- 真实 SQLite running → terminal finish 在同一提交产生 pending publication；
- close/reopen 后恢复目录仍能读取并认证该 publication；
- claim、renew、acknowledge 后 receipt sequence 连续，published 不再被恢复；
- acknowledge 精确重放幂等，不同 delivery digest 被拒绝；
- pending 不能 release，非 live owner/旧 epoch 不能改变 publication；
- lease expiry 后第二 Store 可 takeover，旧 owner 被 fencing；
- release 后可再次 claim，epoch 和 attempt 单调增加；
- 篡改 latest/event receipt 并重算公开 digest，仍因 HMAC 无效而拒绝；
- v3→v4 保留既有 Job，精确 finish replay 可验证补建 pending publication；
- v4 publication 表不完整、列漂移或恢复索引缺失时 fail closed；
- Store Catalog 使用 AgentJob schema v4；
- 只运行 AgentJob、SubAgentManager、Store Catalog、Runtime Composition 相关小模块，不运行全量测试。

## 9. 自我审视与下一依赖

本切片真实完成了 durable publication authority，但仍未完成：

- 生产 publisher loop、startup recovery 与 shutdown/drain；
- `SubAgentManager` 正常路径统一消费 outbox，而不是 commit 后直接返回；
- 对 event callback、父调用方和 message bus 的 delivery digest 定义；
- sink 侧稳定 publication ID、幂等消费 receipt 或重复可见语义；
- HAR-10.7f/7g 已补齐周期 retry/backoff、durable retry budget 与 quarantine/dead-letter；exact requeue、
  告警、retention/GC 仍未完成；
- recovery projection、人工重试/隔离动作及 New UI/TUI 一致展示；
- 跨平台打包后的 crash-point 矩阵与多 Runtime soak。

ARC-04.5d2b 已完成这里定义的 production consumption 前置。下一步应重新比较 Harness、UI 与
Supervisor 依赖，选择 durable inbox 的最小用户消费切片；UI 不得直接操作 outbox 或扫描 SQLite。
