# ARC-06.2a 持久 Worker Capacity FIFO Queue Authority

## 1. 目标与依赖

ARC-06.1a 已能在 Worker Registry 中原子预留一个 incarnation 的执行槽位，ARC-06.1b 已让真实
ToolJob dispatch/terminal lifecycle 消费和归还该 reservation。HAR-10.7a/7b 则只约束当前 Python
进程内的 Agent 活跃数与等待数。两者之间此前没有持久等待 authority：

- 容量耗尽时，调用方只能立即失败或在各自进程内自行排队；
- 多个 Runtime 无法共享 FIFO 顺序或统一硬上限；
- “从等待队列取出”与“占用最后一个 Worker slot”不是同一事务，存在超卖窗口；
- Worker takeover 后，旧 incarnation 的等待项没有统一 fencing 事实。

本切片把 Worker Registry 从 schema v2 升级到 v3，交付 exact-incarnation、deadline、有界 FIFO
等待队列，并让 claim 与既有 capacity reservation 在同一个 SQLite 写事务中完成。它是
Tool/Agent/Browser 持久 scheduler 的共同前置，不提前实现完整 ARC-06。

## 2. 权威数据模型

### 2.1 Durable queue policy

`worker_capacity_queue_policies` 以 `(worker_id, epoch)` 为主键保存：

- 精确 `instance_id`；
- `max_waiters`，范围 `0..10000`；
- 首次配置时间。

第一个可信 Runtime 为该 incarnation 固化队列上限。后续进程只能使用相同上限；不同值 fail
closed，不能通过重新打开 Store 或提高单次请求参数绕过背压。`0` 表示容量不足时不允许持久等待。

### 2.2 Waiter

`worker_capacity_waiters` 只保存调度事实，不保存 Tool 参数、Prompt、用户正文或 workspace 路径：

- `queue_id`、`worker_id`、`instance_id`、`epoch`、`job_id`；
- 仅保存 `workspace_sha256` 作为隔离键；
- `enqueued_at`、`deadline_at`；
- `waiting | claimed | cancelled | expired | fenced`；
- claimed 后绑定确定性的 capacity `reservation_id`。

同一 `(worker_id, epoch, job_id)` 只能出现一次。相同 `queue_id` 的完全一致重试幂等返回；不同事实
复用该 ID 会被拒绝。

## 3. 状态机

```text
waiting ── atomic claim + reserve ──> claimed
   ├──── operator cancel ───────────> cancelled
   ├──── deadline reached ──────────> expired
   └──── revoke / higher epoch ─────> fenced
```

- 所有终态不可重新进入 waiting。
- `claimed` 必须关联确定性 reservation，且 claim 时间必须早于 deadline。
- 读取 claimed waiter 时重新读取 reservation，并核对 worker/instance/epoch/job；关系缺失或被篡改时
  整体读取失败。
- Worker revoke 或更高 epoch 注册会在同一 Registry 事务中同时 fence 活跃 reservation 与所有旧
  waiting 项。

## 4. 原子 FIFO claim

`claim_next_capacity_waiter()` 在单个 `BEGIN IMMEDIATE` 中依次：

1. 重新读取 active Worker contract，核对 instance/epoch；
2. 终结过期 reservation 与 deadline 已到的 waiter；
3. 读取真实活跃 reservation 数量；
4. 容量已满时返回 `None`，不改变 FIFO；
5. 按 `(enqueued_at, queue_id)` 选择最老 waiting 项；
6. 用 waiter deadline 与 Worker `max_wall_seconds` 共同截断 reservation TTL；
7. 插入 capacity reservation；
8. 将 waiter 更新为 claimed 并绑定该 reservation；
9. 一次提交两项事实。

因此多个进程同时争抢最后一个 slot 时，最多一个进程得到 claim；其余进程观察到容量已满，不会
超卖，也不会跳过 FIFO 头部。

## 5. 取消、过期与可恢复读取

- `cancel_capacity_waiter()` 精确绑定 queue/worker/instance/epoch/job；相同原因重试幂等，不同原因、
  claimed 或其他终态均拒绝。
- enqueue、cancel、claim、catalog 均使用调用方提供的权威评估时间机械处理 deadline，不依赖隐藏
  wall-clock。
- `get_capacity_waiter()` 提供 exact recovery read；`list_capacity_waiters()` 最多返回 200 项、
  oldest-first，并验证 claimed→reservation 关系。
- workspace 只以 SHA-256 进入 Registry，队列不复制执行请求或 secret。

## 6. 验证证据

局部真实 SQLite 测试覆盖：

- 两个独立 Store 实例并发 claim 一个槽位，仅 FIFO 第一项得到 reservation；
- release 后第二项继续 claim，重开 Store 后状态与关联仍可读取；
- 三个进程竞争上限 2 的等待队列，只接受两个；
- 不同进程尝试改变 durable `max_waiters` 被拒绝；
- `max_waiters=0` 立即背压；
- enqueue 幂等、ID 漂移、claimed 后取消、deadline 过期、Worker takeover fencing；
- v1→v3、v2→v3 幂等迁移；
- workspace digest 与 claimed reservation 关系篡改均失败关闭；
- 既有 Worker Registry、ToolJob capacity lifecycle 测试继续通过。

本轮只运行上述相关模块，没有运行全量测试。

## 7. 自我审视与保留边界

已确认本实现不是内存队列或 Prompt 套壳：排序、硬上限、状态机、迁移、并发 fencing 与 slot
reservation 都由真实 SQLite 事务执行。

当前仍明确不包含：

- ARC-06.2b1 已另行交付 ToolJob 生产入队、`queued` receipt、直接派发旁路阻断与 dispatch 前取消；
  本 authority 自身仍不负责 ToolJob payload 或 lifecycle；
- claim owner lease、超时后重新投递、dispatch-before-send 的队列级回执；
- priority、aging、跨 workspace 公平、cursor、deadline 调度策略与 starvation 指标；
- Agent/Browser 持久 Worker contract、capability routing 或跨主机 leader；
- 用户可见的 queue catalog/cancel 页面。

因此 ARC-06.2 与 HAR-10.7 继续保持 `partial`。ARC-06.2b1 已证明 ToolJob 可安全进入本队列，
ARC-06.2b2 已完成 claimed ToolJob 的 dispatch/reconcile bridge；ARC-06.2c 又在 AgentJob authority
内交付跨 Runtime embedded active 上限与 durable FIFO。独立 Agent Worker 的物理 slot reservation、
自动 recovery scheduler 与 catalog 动作仍未完成；不得用任一切片宣称完整 Agent 集群。
