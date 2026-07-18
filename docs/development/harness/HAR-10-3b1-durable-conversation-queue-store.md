# HAR-10.3b1 持久会话队列 Store 核心

## 目标

为 HAR-10.3a 的进程内排队与 `/send-now` 建立唯一持久化权威地基。当前切片只交付可重开、可验证、
并发安全的队列 Store，不把现有 Bridge 悄悄切换到尚无 claim lease/fencing 的半成品调度路径。

## 数据合同

Harness DB v14 新增 `harness_conversation_queue`。稳定 identity 是
`workspace_root + session_id + request_id`，记录包含：

- 原样用户消息、提交客户端、入队/更新时间；
- 不可变 payload SHA-256，重复 request id 携带不同消息时 fail closed；
- `queued/completed/cancelled/failed` 状态、当前位置和有界终态原因；
- workspace/session 复合索引，所有读取均显式隔离并限制条数。

用户消息最长 100,000 字符，单 session 同时最多 20 条 `queued`。数据库和父目录继续沿用 Harness Store 的
用户态 0600/0700 权限；该正文属于用户显式提交内容，不写 DebugTrace、reasoning 或日志。

## 事务与不变量

1. enqueue 使用 `BEGIN IMMEDIATE`；同一 identity 与 payload 重试返回原记录，不新增位置。
2. 多个独立 Store 实例争用同一 SQLite 时，position 仍为连续唯一的 `1..N`。
3. promote 在单事务内把目标移到位置 1，其他项保持相对顺序；重复提升队首幂等。
4. terminal transition 只允许从 `queued` 进入三种终态；相同终态重放幂等，不同终态拒绝覆盖。
5. 离队后剩余位置立即压缩；时间戳不能倒退；正文摘要不一致时拒绝读取。
6. v13 及更早数据库通过 additive v14 schema 升级，不重置已有 Harness 表。

## 验收证据

- 入队后关闭 Store，再用新实例读取，消息、空格、顺序与摘要保持不变；
- 20 个独立 Store 并发入队得到 20 个连续位置，第 21 条收到可纠正的容量错误；
- `first, second, third` 提升 third 后严格变为 `third, first, second`；
- completed 后剩余位置压缩，重复 completed 成功，改写为 cancelled 被拒绝；
- workspace/session 隔离、非法 ID、空正文、时钟倒退和手工篡改均有定向测试；
- v13 marker 数据在 v14 升级后仍存在，新表可立即写读。

## 当前边界与下一切片

本切片尚未接入 Bridge，因此 Naumi 进程崩溃后仍不会自动恢复现有内存队列；这不是用户闭环完成态。
`HAR-10.3b2` 必须增加 claim owner/epoch/lease、启动恢复和终态提交 fencing，再把 Bridge enqueue/promote/start
切到本 Store。跨客户端轮转公平、取消传播、cursor 和 terminal retention 仍在其后，不能由 position 字段
冒充完整集群调度。

