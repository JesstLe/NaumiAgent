# HAR-10.8f1 Pursuit 恢复终态 Outbox 核心

## 目标

HAR-10.8e 可以显式 fencing 并对账停留在 `admitted` 的恢复请求，但生产恢复路径仍存在一个
持久化窗口：后置机械裁判与 checkpoint 已经提交，进程却在 recovery attempt 收口前退出。
此前数据库只能通过扫描 `admitted` attempt 猜测哪些记录可能需要恢复，没有一条和完整终态
checkpoint 同事务产生的“仍待收口”事实。

本切片增加 `PursuitTerminalOutboxRecord` 及其 append-only 事件链：

```text
terminal run + boundary decision 已持久化
  -> save_checkpoint BEGIN IMMEDIATE
     -> 写 terminal checkpoint
     -> 认证唯一 admitted attempt 与后置 boundary
     -> 原子创建 pending outbox
  -> 正常 resolve 或 HAR-10.8e reconcile BEGIN IMMEDIATE
     -> 写 attempt terminal 事件/快照
     -> 写 reconciliation receipt（显式对账路径）
     -> 原子把 outbox 标为 delivered
```

因此，只要完整终态 checkpoint 已提交，就必然同时存在 pending 或 delivered outbox；不会出现
“终态证据完整但没有待恢复事实”，也不会出现“attempt 已收口但 outbox 仍伪装 pending”。

## 边界

本切片只交付 Store authority 与有界恢复目录，不启动自动 worker，不新增 UI 操作，也不声称
跨 `HarnessStore`/`PursuitStore` exactly-once。HAR-10.8f2 将消费该目录，复用 10.8e 的
heartbeat、RunLease、higher-epoch fence 与机械对账，不在前端直接扫描 SQLite。

## 数据合同

`PursuitTerminalOutboxRecord` 为 frozen schema v1，状态只有：

- `pending`：sequence 1，绑定 admitted attempt digest、run、后置 boundary decision 与 checkpoint；
- `delivered`：sequence 2，额外绑定 terminal attempt digest 与 delivered 时间。

`outbox_id = ptout-<sha256(canonical immutable facts)>`。主表每个 attempt 唯一，事件表使用连续
sequence、previous payload digest 和 payload digest。读取时复验：

1. 主表快照与事件链末端一致；
2. sequence、state、previous digest 连续；
3. admission 事件序列 2 的 digest 与 outbox 绑定一致；
4. delivered 时 attempt 必须存在序列 3 终态，digest 必须一致；
5. 引用的 boundary decision 必须仍存在。

当前 `PursuitStore` 与既有 recovery ledger 一样使用 SHA-256 防意外损坏和局部篡改检测，没有独立
HMAC 密钥，因此不得把它描述为可抵抗拥有数据库完全写权限的攻击者。

## 原子创建规则

`save_checkpoint()` 只在以下事实同时成立时创建 outbox：

- checkpoint 状态为 `waiting/blocked/completed/cancelled/budget_exceeded`；
- 同一 run 恰好存在一个 `admitted` attempt；
- checkpoint 时间晚于 admission，且不是 admission checkpoint；
- run 状态与 checkpoint 状态相同；
- run 指向的 boundary decision 存在；
- `admitted_at < boundary.recorded_at <= checkpoint.created_at`。

非终态 checkpoint 或尚未产生后置 checkpoint 时不会猜测创建；已 admitted 后提交终态 checkpoint
却缺少同状态机械裁判时，整个 checkpoint 事务失败关闭。多个 admitted attempt 同样属于权威冲突。
相同 checkpoint 并发/重放复验同一 outbox identity，
不会追加重复事件。

## 原子 delivered 规则

普通 `resolve_recovery_attempt()` 和 HAR-10.8e 的
`reconcile_admitted_recovery_attempt()` 都在原事务内投递 outbox。只有 `resolved/failed` attempt
可以形成 delivered；终态 digest 不一致时失败关闭。同一终态重放返回原记录，不产生第三条事件。

outbox 写入失败会使 checkpoint 或 attempt/reconciliation receipt 整个事务回滚。故障不能留下
“checkpoint 有而 pending 无”或“attempt terminal 但 delivered 无”的半状态。

## 有界恢复目录

`list_pending_terminal_outbox(limit=...)`：

- oldest-first，排序为 `(created_at, outbox_id)`；
- 默认 100，合法范围 1..1000；
- 逐条重新认证快照、事件链、admission 与 boundary；
- 只返回 opaque ID/digest/timestamp，不返回目标文本、模型内容、工具输出、owner 或 secret。

HAR-10.8f2 必须在此基础上增加 cursor、claim/backoff、启动与周期扫描、shutdown drain 和 typed
可观测性；不得把单次 `LIMIT` 查询包装成无界轮询。

## 验收标准

- 真实 SQLite terminal checkpoint 提交同时产生 pending outbox；关闭 Store 后重开仍可认证；
- 注入 outbox INSERT 失败时 checkpoint 保持旧版本且没有 pending 记录；
- 普通 attempt resolution 与 outbox delivered 同事务；
- 注入 delivered event 失败时 attempt 保持 admitted，outbox 保持 pending；
- 10.8e reconciliation receipt、attempt terminal 与 outbox delivered 同事务；
- 16 路相同 checkpoint 重放只产生一个 outbox 和一条 pending 事件；
- 主表或事件摘要篡改后读取失败关闭；
- limit 拒绝 bool、非整数、0 和大于 1000；
- 只运行 Pursuit recovery/checkpoint 相关小模块，不运行全量测试。

## 自我审视与未完成项

- run/boundary 的保存仍早于 checkpoint 事务。若进程恰好在两者之间崩溃，尚不存在完整终态
  checkpoint，本切片不会伪造 outbox；后续恢复仍按 checkpoint 权威处理。
- 还没有自动 scanner、cursor、claim lease、退避、dead-letter、retention 或 backlog 指标；这些是
  HAR-10.8f2，而不是本切片的隐藏完成项。
- Harness fencing 与 PursuitStore outbox 分属两个 SQLite 事务域，仍是 at-least-once 收敛设计。
- kill-at-every-write-point 矩阵、Linux/Windows 进程终止测试及 24 小时 soak 尚未完成。
