# HAR-08.4m Sandbox Admission Typed Progress

## 状态

已实现。

本切片把 HAR-08.4l 的 durable ticket transition 投影到现有
`harness_sandbox_eval_progress` 事件、Bridge `harness/eval-batch` payload、New UI 和
TUI。它只提供只读状态，不同时引入 cancel action、priority 或 retry scheduler。

## 设计原则

1. UI 不计算排队位置，不根据等待时间猜测 queued。
2. `queued -> admitted` 只来自 Store ticket 的原子状态转换。
3. active/queued 计数来自同一 transaction 的 snapshot。
4. admission 丢失时，UI 只显示 Store-confirmed terminal state。
5. progress callback 失败不能改变 admission；SQLite ticket 仍是恢复权威。
6. 本地 fallback gate 不发布 durable admission stage。

## Checkpoint 契约

`HarnessSandboxBatchCheckpoint` 保持 schema version 1，并扩展闭集 stage：

- `queued`
- `admitted`
- `recovering`
- `acquiring`
- `executing`
- `completed`
- `failed`
- `cancelled`
- `expired`

新增 admission snapshot：

- `admission_ticket_id`
- `admission_epoch`
- `admission_state`
- `queue_position`
- `max_active`
- `max_queued`
- `active_count`
- `queued_count`

全部字段进入 checkpoint SHA-256。严格校验包括：

- 无 ticket 时所有 admission 字段必须为空或零；
- `queued` 必须有 ticket，且 `1 <= queue_position <= queued_count`；
- 非 queued state 的 queue position 必须为零；
- active/queued count 不得超过 policy；
- `queued` 只能对应 Store `queued`；
- `admitted/recovering/acquiring/executing` 只能对应 Store `active`；
- `cancelled/expired` 必须对应相同 Store terminal state；
- `failed` 可表示尚待 terminal write 的 active failure 或已写入的 failed；
- `completed` 的最终事件对应 Store `completed`，且 H5a 必须完整。

## 事件顺序

直接取得容量的成功批次：

```text
admitted
  -> recovering
  -> acquiring
  -> executing(1..N)
  -> completed
```

排队批次：

```text
queued(position=N)
  -> queued(position=N-1)  # 仅 snapshot 发生变化时
  -> admitted
  -> recovering
  -> ...
  -> completed
```

durable 模式下，Coordinator 不再提前发布一个仍为 active 的 completed。最终 completed
由 admission context 在 Store 完成 `active -> completed` 后发布，因此最终 snapshot 的
`active_count` 已释放。进程内 fallback 仍由 Coordinator 发布原有 completed。

取消、异常与过期：

```text
queued/active -> cancelled
active        -> failed
queued/active -> expired
```

若续租发现 ticket 已被外部终止，authority 会读取 ticket 终态后再投影；无法读取时只返回
稳定后端错误，不伪造 terminal UI 状态。

## Bridge 与双端渲染

### Bridge

沿用既有 Runtime event `harness_sandbox_eval_progress`，Bridge 继续投影到
`ServerEventType.HARNESS_EVAL_BATCH`，并保留原始 request id。没有新增重复事件词汇。

### New UI

`normalizeHarnessSandboxEvalProgress()`：

- 接受旧 checkpoint 缺少 admission 字段的 recovering/executing/completed 回放；
- 对新 admission stage 强制完整 ticket snapshot；
- 拒绝 stage/state、terminal/code、position/count 不一致；
- 仅在首个真实 Sandbox payload 后打开 typed page。

Sandbox 页面新增“容量权威”区：

- Ticket 与 epoch；
- 当前 admission state；
- queued position；
- active/queued 容量。

queued 使用黄色，admitted 使用青色，cancelled 使用黄色，expired/failed 使用红色。

### TUI

共享 Slash frontend 状态栏增加：

- `Sandbox Eval 排队`
- `Sandbox Eval 已准入`
- `Sandbox Eval 已取消`
- `Sandbox Eval 租约过期`
- queued position 或 active capacity。

TUI 不解析展示文本来恢复状态，仍消费同一 typed payload。

## 验收证据

- durable Coordinator checkpoint 顺序包含 admitted 与 Store-released completed；
- 所有 checkpoint 使用同一 ticket/epoch；
- final completed 为 `admission_state=completed` 且 `active_count=0`；
- queued task 取消投影 queued、cancelled，position 来自 Store；
- checkpoint digest 覆盖 admission snapshot；
- Python public serializer 保留字段且不暴露 workspace/owner；
- JS protocol 拒绝越界 position 和 stage/state 冲突；
- New UI 在多种宽度下渲染 ticket 与容量且不超宽；
- TUI 状态栏展示真实队列位置；
- Bridge request correlation 不变；
- 真实 5-sample Shell Worker 流观测到
  `admitted -> recovering -> acquiring -> executing(1..5) -> completed`。

## 当前明确不包含

- 用户发起的 ticket cancel action；
- admission ticket catalog/list/detail；
- resume 后的 admission event replay；
- priority、deadline、weighted fairness；
- terminal ticket retention/GC；
- 多机外部数据库调度；
- Linux/Windows 真实终端视觉 CI。

## 下一依赖切片

HAR-08.4n 应实现 owner-fenced cancel action：

1. 取消必须精确绑定 workspace、ticket、调用者可见 revision/epoch；
2. queued cancel 与 active execution cancel 分开定义；
3. active cancel 必须联动 Runtime lease、Run Grant 与正在执行 task；
4. Bridge action 需要 typed request/receipt；
5. New UI 与 TUI 使用同一 cancel authority；
6. bypass 省略二次确认，但不能绕过 ticket/epoch fencing；
7. retry 必须在 cancel receipt 已 durable 后另起 immutable authority，不复用旧 ticket。
