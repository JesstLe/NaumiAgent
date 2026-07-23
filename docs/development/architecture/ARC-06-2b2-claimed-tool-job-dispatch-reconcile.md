# ARC-06.2b2 Claimed ToolJob Dispatch 与 Reconcile

## 1. 交付目标

ARC-06.2b1 已让 ToolJob 在容量饱和后写入 `queued` receipt 并创建持久 waiter，但 Worker Registry
原子 claim 的 slot 尚未进入生产 ToolJob/Shell 边界。本切片闭合这段最小链路：

```text
queued ToolJob
  + claimed waiter
  + active scheduler reservation
  -> dispatched receipt
  -> Shell ready
  -> running 前复验 reservation
  -> payload start
  -> terminal receipt
  -> release scheduler reservation
```

它只消费已经 claim 的请求，不实现自动 scheduler、payload 持久化或 Agent/Browser Worker。

## 2. Registry Claim Snapshot

Worker Registry 新增 `get_capacity_claim_for_job()`，在同一 SQLite `BEGIN IMMEDIATE` 中：

1. 以调用方提供的权威时间机械处理 reservation TTL 与 waiter deadline；
2. 按 `(worker_id, epoch, job_id)` 精确读取唯一 waiter；
3. 要求 waiter 已为 `claimed`；
4. 验证 waiter 与 reservation 的 worker、instance、epoch、job 关联；
5. 返回 waiter 与当前 reservation 状态的同一快照。

`get_capacity_reservation()` 则为发送前生命周期边界提供 exact reservation read。它先应用 TTL，再返回
`active|released|expired|fenced` 事实，不把过期 slot 当作可用 slot。

稳定 `capacity_waiter_reservation_id(queue_id)` 由 Worker Registry 公开，ToolJob lifecycle 不再复制
scheduler reservation identity 算法。

## 3. Claimed Dispatch Authority

`ToolJobAuthority.dispatch_claimed()` 必须同时证明：

- request、ExecutionGrant、Tool lease、Worker health、能力与隔离要求仍有效；
- ToolJob 为 `queued`，或是完全相同 dispatch 的幂等重放；
- waiter 与 immutable ToolJob 的 worker incarnation、workspace digest、admission/deadline 完全一致；
- waiter 已 claimed，且关联 reservation 仍为 active；
- waiter 保存的 reservation id 与 Registry 返回的 reservation id 一致。

证明成立后，ToolJob Store 在自己的写事务中提交：

- `queued → dispatched`；
- `result_code=dispatch_committed_capacity_queue_v1`；
- `side_effect=possible`；
- 调用方提供的稳定 dispatch id。

只有首次 transition 的 `should_send_payload=true`。相同 dispatch 的重启重放返回 false；不同 dispatch
竞争同一 ToolJob 会被 lifecycle digest 拒绝，因此至多一个调用方越过发送边界。

## 4. Shell 最后安全边界

`ShellWorkerCoordinator.execute()` 读取到 `queued` ToolJob 时不再调用普通 dispatch，而只调用
`dispatch_claimed()`：

- waiting waiter、缺失 claim、terminal reservation 均在创建本地进程前失败；
- dispatch receipt 成功后，本地认证 Worker 只完成 handshake，不立即执行命令；
- transport 在发送 `start` 控制消息前调用 `mark_running()`；
- `mark_running()` 重新读取本次 dispatch 对应 reservation，要求 identity 精确且仍 active；
- reservation 在 dispatch 与 start 之间 expired/fenced/released 时，拒绝发送 `start`，Coordinator
  按既有保守语义把已写 dispatch 收口为 `unknown + possible`。

因此 queue claim 不只是 admission 时的一次快照，而是一直 fence 到真实 payload 的最后安全边界。

## 5. Terminal Release 与 Lost-Claim Reconcile

ToolJob Store 根据 dispatch receipt 区分 reservation 来源：

- 普通 dispatch 使用既有 `capacity-{job_id}`；
- queued dispatch 使用 Registry 公开算法派生的 scheduler reservation；
- legacy dispatch 不虚构 reservation。

`succeeded|failed|cancelled|unknown` 继续走统一 terminal cleanup，scheduler reservation 会以相同
ToolJob 终态原因释放；重试不重复释放。

若 claim 已经 `expired|fenced|released`，但 ToolJob 尚停在 `queued`，则
`reconcile_lost_capacity_claim()`：

1. 读取带 TTL 处理的真实 claim snapshot；
2. active claim 明确拒绝收口；
3. 仅 terminal reservation 允许 `queued → cancelled`；
4. receipt 使用 `side_effect=none` 与机械结果码 `capacity_claim_<state>`；
5. 相同 reconcile 重试幂等。

由于 ToolJob 尚未出现 dispatch receipt，这条收口可以证明 payload 从未获得发送许可。

## 6. 崩溃与竞争语义

| 中断位置 | 恢复结果 |
| --- | --- |
| claim 后、dispatch 前 | ToolJob 仍 queued；持有原请求的调度方可重试 `dispatch_claimed()` |
| dispatch receipt 前 | 无 payload 许可，可安全重试 |
| dispatch receipt 后、返回前 | 重试得到 `should_send_payload=false`，转入 reconcile，不重复发送 |
| dispatch 后、Shell start 前 claim 失效 | `mark_running()` 阻断 start，ToolJob 保守进入 unknown |
| queued 状态下 claim 已 terminal | `reconcile_lost_capacity_claim()` 以 no-side-effect cancelled 收口 |
| 两个 dispatch id 并发 | ToolJob 写事务只接受一个 transition，另一个失败关闭 |
| terminal receipt 后 release 中断 | terminal 不回滚；同终态重试继续 cleanup |

两个 SQLite Store 仍不伪装成分布式事务。安全顺序的目标是宁可短时少报 capacity 或进入 reconcile，
也不重复发送未知副作用。

## 7. 验收证据

局部测试覆盖：

- waiting ToolJob 不能调用 claimed dispatch；
- claim 后首次 dispatch 可发送，相同 dispatch 跨 Store 重开重放不可再次发送；
- queued dispatch receipt identity 与 sequence 正确；
- running/succeeded 后 scheduler reservation 被统一 terminal cleanup 释放；
- active claim 拒绝 lost-claim reconcile，TTL 后只允许 no-side-effect cancelled，且重试幂等；
- Worker takeover 将 claimed reservation fence 后，只允许 no-side-effect cancelled 收口；
- dispatch receipt 与 worker start 之间 reservation 过期时，`mark_running()` 拒绝 payload；
- 真实认证 Shell：waiting 阶段文件不存在，claim 后命令只执行一次、写入 artifact，并释放 reservation；
- 既有 direct ToolJob、capacity queue、Shell sandbox 与 Store Catalog 回归保持通过。

本轮只运行相关小模块，不运行全量测试。

## 8. 自我审视与保留边界

本切片完成了真实 claimed dispatch bridge，但仍不包含：

- 自动选择 Worker、周期 claim、唤醒与调度循环；
- claim owner lease、ack deadline 与 owner takeover；
- 原始 ToolJob 参数的受控持久 envelope；当前队列刻意只存 digest，调度方必须仍持有匹配请求；
- UI-13.1e 已交付 New UI/TUI 共用的聚合 backlog health；job 级 queued/claimed/orphan catalog 与用户操作
  仍未实现；
- priority、aging、workspace 公平、starvation 指标；
- Agent/Browser Worker contract 与 payload adapter；
- 跨 Store outbox 或通用 ARC-08 reconciler。

ARC-04.5a 已交付 embedded Agent 的 request/result contract 与双端低敏证据，ARC-04.5b1 又交付通用
Runtime payload key/envelope；ARC-04.5b2/5c 已进一步交付 Agent Job authority 与 embedded durable
dispatch。下一步需比较 Agent capacity admission 与可恢复 response publication；
不能只增加 claim lease，也不能把本桥接宣称为完整高并发或 Agent 集群。
