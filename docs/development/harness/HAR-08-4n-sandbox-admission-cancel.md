# HAR-08.4n Sandbox Admission Owner-fenced Cancel

## 状态

已实现，2026-07-23。

本切片只解决一个问题：用户如何取消一个已经获得 durable admission ticket 的
Sandbox Eval Batch，并确保取消对象、取消时刻与取消结果都可验证。

本切片不包含 retry、优先级调整、批量取消或管理员强制终止。

## 用户结果

- New UI 的 Sandbox Eval 页面在 ticket 仍为 `queued` 或 `active` 时显示 `C` 取消操作。
- `bypass` 模式下取消不触发二次确认，但不会绕过 ticket、authority、epoch 或 state fencing。
- TUI 可通过同一底层 authority 取消另一个终端或进程中的精确 ticket。
- 用户会收到 durable cancel receipt；若页面状态已经变化，系统拒绝旧请求并返回当前事实，
  不执行 best-effort 误杀。

## 权威请求

一次取消必须同时绑定：

| 字段 | 约束 | 用途 |
|---|---|---|
| `action_id` | `hsac_<24 hex>` | 幂等键；不同请求不得复用 |
| `ticket_id` | `hsadm_<24 hex>` | 精确容量 claim |
| `authority_key` | SHA-256 | 精确 Eval/Checkpoint authority |
| `epoch` | 正整数 | ticket owner generation fence |
| `expected_state` | `queued` 或 `active` | 防止旧页面跨状态取消 |
| `actor_id` | 非空、有界 | 审计来源 |
| `reason` | 非空、最多 500 字符 | 用户可理解的操作理由 |

工作区不由客户端提交。Bridge、TUI 和 admission instance 都从当前 Engine 的 canonical
workspace 获取，避免跨工作区注入。

## Store v18

新增 `harness_sandbox_admission_cancel_attempts`：

- 同时保存 accepted 与 rejected 尝试；
- `(workspace_root, action_id)` 为主键；
- 保存 request digest 与 receipt digest；
- 不设置 ticket 外键，因为“不存在的 ticket”拒绝也必须留下审计事实；
- action 重放只有在所有 immutable request 字段一致时才返回原回执，否则冲突关闭。

原子事务顺序：

1. `BEGIN IMMEDIATE`；
2. 回收过期 ticket；
3. 检查 action 幂等性；
4. 读取 ticket；
5. 依次检查 ticket、authority、epoch、terminal state、expected state；
6. accepted 时把 ticket 更新为 `cancelled`；
7. 在同一事务写入 tamper-evident receipt；
8. 返回 receipt 与当前 ticket snapshot。

拒绝码：

- `sandbox_batch_cancel_ticket_not_found`
- `sandbox_batch_cancel_authority_mismatch`
- `sandbox_batch_cancel_epoch_mismatch`
- `sandbox_batch_cancel_clock_rollback`
- `sandbox_batch_cancel_already_terminal`
- `sandbox_batch_cancel_state_changed`

接受码：

- `sandbox_batch_cancelled_by_user`

## 运行时终止语义

`HarnessSandboxBatchAdmission` 维护当前进程的 `ticket_id -> asyncio.Task` 索引。

- Store 接受取消后，若 owner task 在当前进程，立即 `task.cancel()`；
- queued owner 最多在原有 250ms poll 周期内观察 durable `cancelled`；
- active owner 的 renewal 周期上限改为 1 秒，因此其他进程发起取消后最多约 1 秒观察 fence；
- coordinator 的既有 `finally` 继续负责撤销 Run Grant、释放 Runtime lease；
- admission `finally` 读取 Store 已有 `cancelled` 终态并发布 typed terminal progress；
- cancel receipt 写入成功才允许 UI 声明“已接受”。

## UI 协议

客户端事件为 `harness/eval-sandbox/cancel`，服务端事件为
`harness/eval-sandbox/cancel-result`。

服务端回执不暴露 `workspace_root` 或 `owner_id`。`current` 只包含刷新页面所需的 ticket、
authority、epoch、state、queue/capacity 与更新时间。

New UI 对回执执行闭集校验：

- receipt/action/ticket identity 格式；
- SHA-256 格式；
- decision 与 observed state 闭集；
- `missing` 与 `current=null` 一致；
- current 必须绑定同一 ticket、authority 与 observed state。

TUI 命令：

```text
/harness eval sandbox cancel <ticket> --authority <sha256> \
  --epoch <n> --state <queued|active> [--reason <原因>]
```

## 验收证据

- Store schema/table 精确检查；
- stale state 拒绝且 ticket 保持 active；
- accepted cancel 与同 action 重放返回相同 receipt digest；
- 同进程 active owner 即时取消并释放容量；
- 独立 admission/store instance 在 1.5 秒内观察 active cancel；
- Python client payload 拒绝无效 action/ticket/authority/epoch/state；
- Bridge 只投影公开字段；
- New UI `C` 提交当前页面的 exact ticket/authority/epoch/state；
- New UI 应用 durable receipt 后刷新 capacity snapshot；
- Python lint/import、JavaScript syntax 与聚焦模块测试通过。

## 自我审视与限制

已确认：

- 没有通过客户端 workspace 或 owner_id 取得取消权；
- bypass 只移除二次确认，没有扩大 authority；
- rejected action 也可审计；
- 取消不是只改 UI 状态，而会中断本地 task，并通过 durable fence 终止其他进程。

仍未实现：

- retry：必须等待 durable cancel receipt 后创建全新 ticket/authority；
- ticket catalog：当前 UI 只能操作自己已收到 typed progress 的 ticket；
- 管理员强制取消与批量取消；
- 跨主机低延迟 push；当前其他进程依赖至多 1 秒的 durable poll。

## 下一切片

HAR-08.4o 应实现 cancel 后的显式 retry authority。retry 必须引用 accepted cancel receipt，
生成新 `action_id`、新 ticket 与新 execution authority；不得复活旧 ticket，也不得把 retry
隐式合并进 cancel。
