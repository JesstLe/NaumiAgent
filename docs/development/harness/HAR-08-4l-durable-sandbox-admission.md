# HAR-08.4l Durable Sandbox Batch Admission

## 状态

已实现。

本切片把 HAR-08.4g 的进程内 `asyncio.BoundedSemaphore` 升级为生产可用的
workspace-wide SQLite admission authority。它是 HAR-08.4k typed progress 与后续
queued/cancel/retry 用户体验之间的最小持久化前置，不替代 ARC-06 Worker Registry
capacity reservation，也不冒充完整的多机调度器。

## 用户问题

旧 admission 只能约束单个 Python Runtime：

- 两个 NaumiAgent 进程可以分别取得 `max_active` 个槽，突破用户配置；
- 进程崩溃会丢失 active/queued 事实；
- 前端无法获得可信的排队位置；
- 取消等待者只能清理当前进程内计数；
- 容量配置在并行进程之间不具备一致性。

因此，HAR-08.4k 不能安全地渲染“排队中”或提供取消按钮。前端自行推导这些状态会制造
不存在的运行时事实。

## 权威边界

Sandbox admission 负责 workspace 范围内的 active/queued 上限、FIFO 排队、租约续期、
崩溃回收、owner/ticket/epoch fencing、容量策略一致性和 terminal transition。

ARC-06 Worker capacity reservation 继续负责具体 Worker incarnation 的执行容量、
ToolJob 绑定、Worker heartbeat 与 capacity epoch。Sandbox batch 在取得 batch admission
后，仍必须逐项通过 Shell Worker/ToolJob authority；两层不得合并计数。

## 持久化模型

Harness Store schema 升级为 v17。

### `harness_sandbox_admission_policies`

每个 canonical workspace 一条：

- `max_active`
- `max_queued`
- `updated_at`

首个 admission 建立策略。若有未过期 queued/active ticket，其他进程使用不同容量配置
会得到稳定的 policy conflict；只有 open ticket 清零后才能原子切换策略。

### `harness_sandbox_admission_tickets`

每个 admission request 一条：

- `ticket_id`：`hsadm_<24 hex>`
- `authority_key`：Sandbox batch immutable authority SHA-256
- `lane`：`sandbox | red | green | adversarial`
- `requested_samples`
- `owner_id`、`epoch`
- `state`：`queued | active | completed | cancelled | failed | expired`
- workspace 内单调 `sequence`
- `enqueued_at`、`lease_expires_at`、`updated_at`
- `terminal_code`
- immutable `request_sha256`

`request_sha256` 在每次读取时复算。状态与容量统计以 SQLite transaction 内的行事实为准，
不能从 UI、日志或进程内计数恢复。

## 原子状态机

```text
enqueue
  ├─ active_count < max_active ───────────────> active
  ├─ queued_count < max_queued ───────────────> queued
  └─ otherwise ───────────────────────────────> capacity_exhausted

queued
  ├─ FIFO head + active slot ─────────────────> active
  ├─ caller cancellation ─────────────────────> cancelled
  └─ lease timeout / process crash ───────────> expired

active
  ├─ batch return ─────────────────────────────> completed
  ├─ caller cancellation ─────────────────────> cancelled
  ├─ batch exception ─────────────────────────> failed
  └─ lease timeout / fence loss ──────────────> expired / execution cancelled
```

所有 enqueue、reap、count、promotion、renew 与 terminal transition 使用
`BEGIN IMMEDIATE`。独立 `HarnessStore` 实例和独立 OS 进程共享同一 SQLite 权威。

## Runtime 接线

`AgentEngine` 使用共享 `HarnessStore` 和启动 workspace 构造 durable
`HarnessSandboxBatchAdmission`。同一对象仍注入 native Harness Sandbox Eval 与
Evolution RED/GREEN/adversarial cohort，因此四条 lane 竞争同一 workspace 容量。

`HarnessSandboxBatchCoordinator` 在 H5a fast-path 检查后，把 `authority_key`、`lane`
与 `requested_samples` 交给 admission。已完整持久化的批次仍直接返回，不占容量槽。

active ticket 每 `lease_seconds / 3` 续租。续租失败或 owner/epoch 不再匹配时，authority
取消正在运行的 batch task，并返回 `sandbox_batch_admission_fence_lost`；失去容量权威的
执行不得继续提交成功结果。

未提供 Store 的 `HarnessSandboxBatchAdmission` 仍保留进程内模式，用于独立单元测试和
受控嵌入场景；生产 composition 有测试断言必须为 durable。

## 错误契约

- `sandbox_batch_capacity_exhausted`
- `sandbox_batch_nested_admission`
- `sandbox_batch_admission_policy_conflict`
- `sandbox_batch_admission_unavailable`
- `sandbox_batch_admission_fence_lost`
- `sandbox_batch_admission_cleanup_failed`
- `sandbox_batch_admission_clock_invalid`
- `sandbox_batch_admission_token_invalid`
- `sandbox_batch_admission_authority_invalid`
- `sandbox_batch_admission_lane_invalid`
- `sandbox_batch_admission_samples_invalid`

用户文案保持中文；错误 code 保持稳定英文 snake_case，供 Runtime event、New UI 与 TUI
后续闭集映射。

## 验收证据

Store 与并发测试覆盖：

- 两个独立 Store 同时 enqueue，最多一个 active、一个 queued；
- 第三个请求在 `max_active=1,max_queued=1` 时稳定拒绝；
- active terminal 后只提升 FIFO head；
- expired active 被回收，live waiter 可以恢复为 active；
- stale owner/epoch 无法 renew 或 terminal write；
- live ticket 阻止容量策略漂移；
- terminal transition 同参数重放幂等。

Runtime 测试覆盖：

- queued task 取消后 durable queued count 归零；
- active ticket 被外部 fencing 后，正在运行的 body 被取消；
- completed H5a fast-path 不进入 saturated admission；
- Engine 的 Harness/Evolution executors 共享同一 durable admission。

真实场景测试 `tests/integration/test_harness_sandbox_admission_processes.py` 启动两个独立
Python 进程，共用同一个 Harness SQLite。配置 `max_active=1` 时，第二个进程的 enter
不得早于第一个进程的 exit；结束后 durable snapshot 必须为 active=0、queued=0。

## 当前明确不包含

- priority、deadline、weighted fairness；
- operator-facing ticket catalog；
- typed queued/admitted/cancelled Runtime event；
- `/cancel` 或 UI cancel action；
- ambiguity resolution 与手动 retry；
- 多主机网络调度和外部数据库共识；
- terminal ticket retention/GC policy。

这些限制不影响跨进程容量正确性，但 UI 仍不得提前显示可交互的排队取消按钮。

## 后续进展

HAR-08.4m 已基于真实 ticket/snapshot 增加闭集 typed admission checkpoint，并同步到
New UI/TUI。queued position、capacity snapshot、admitted 与 terminal state 均来自本切片
Store transition；详见 `HAR-08-4m-sandbox-admission-typed-progress.md`。

下一切片 HAR-08.4n 应实现 owner-fenced cancel action；不应在该切片扩张
priority/deadline scheduler。
