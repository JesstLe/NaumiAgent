# ARC-06.2b1 ToolJob Capacity Queue Admission 与取消闭环

## 1. 目标与切片边界

ARC-06.2a 已提供持久、有界、incarnation-fenced 的 Worker capacity FIFO authority，但生产
ToolJob 仍只能在容量不足时失败。本切片只交付安全接入所需的第一个最小垂直切片：

- ToolJob 在真实 Worker 容量耗尽时进入持久队列；
- ToolJob lifecycle 以不可篡改事实记录 `queued`，禁止普通 `dispatch()` 绕过队列；
- 入队、重启重放和 dispatch 前取消均幂等；
- 跨 ToolJob Store 与 Worker Registry 的非原子窗口可恢复、失败关闭；
- 队列仍不保存命令、参数、Prompt、secret 或 workspace 路径。

本切片不自动 claim、发送 payload 或重试未知副作用。自动出队派发属于后续
ARC-06.2b2，不能由本切片冒充。

## 2. 权威状态与数据边界

### 2.1 ToolJob schema v3

ToolJob authority 新增单调状态：

```text
admitted ── capacity_queue_bound_v1 ──> queued
queued ── pre-dispatch cancel ────────> cancelled
```

`queued` lifecycle receipt 必须满足：

- 前态严格为 `admitted`；
- `dispatch_id` 为空；
- `side_effect=none`；
- `result_code=capacity_queue_bound_v1`；
- sequence、前序 receipt digest 与本 receipt digest 全部通过原有哈希链验证。

ToolJob schema v1 会先迁移到 v2 receipt authority，再迁移到 v3；v2 直接迁移到 v3。迁移在
SQLite 事务中重建带 `queued` CHECK constraint 的两张表，并在复制前验证已有 job 与 receipt
chain，损坏数据不会被静默带入新 schema。

### 2.2 Registry waiter 绑定

一个 ToolJob 的确定性 queue identity 由 job identity 派生。现有 waiter 重放时必须同时匹配：

- worker id、instance id 与 epoch；
- job id；
- immutable ToolJob 的 workspace digest；
- ToolJob admission 与 expiry 时间。

Registry 只保存这些调度事实。原始 ToolJob 参数仍只存在于调用边界，不进入 capacity queue
数据库。

## 3. 入队协议

`ToolJobAuthority.enqueue_for_capacity()` 执行以下检查：

1. 完整复用 dispatch 前 fencing：request digest、execution grant、Tool lease、Worker health 与
   requirements 必须仍然有效；
2. ToolJob 只能处于 `admitted` 或 `queued`；
3. 若 waiter 已存在，验证其与 immutable ToolJob 完全一致，并只在 ToolJob 同时为 `queued` 时幂等返回；
4. 首次入队重新读取 Registry capacity snapshot；仍有 slot 时拒绝制造等待项；
5. 先在 ToolJob Store 提交 `admitted → queued` receipt；
6. 再在 Worker Registry 创建有界 FIFO waiter；
7. 创建后重新读取 ToolJob；若它已被并发取消，则取消 waiting waiter并失败关闭。

普通 `dispatch()` 遇到 `queued` ToolJob 或任一关联 waiter 都会拒绝，不能借由旧调用路径跳过
FIFO authority。

## 4. 跨 Store 崩溃恢复

ToolJob Store 与 Worker Registry 是两个独立 SQLite authority，当前没有分布式事务。提交顺序刻意选择
ToolJob `queued` 在前、Registry waiter 在后：

- 若进程在第一步前崩溃，ToolJob 仍为 `admitted`，可安全重试；
- 若在 `queued` receipt 后、waiter 前崩溃，普通 dispatch 已被 ToolJob 状态阻断；同一
  `enqueue_for_capacity()` 重试会补建确定性 waiter；
- 若 waiter 已落盘，重试验证 immutable 绑定后返回同一记录；
- 若队列已满导致 waiter 创建失败，ToolJob 保持 `queued`，不会退回可直接派发状态；容量释放或运维处理后
  可用同一请求重试；
- 若 direct dispatch 已预留 slot、但最终 ToolJob 写事务观察到并发 `queued`，事务内 current-state
  fence 会拒绝 dispatch；已预留 slot 由既有短 TTL fail-safe 回收，不会执行 payload；
- 若 ToolJob 与 waiter 关系冲突、waiter 已被 claim 或任一关联被篡改，则失败关闭并要求后续 reconcile。

这保证失败不会变成旁路执行。后续 ARC-06.2b2 必须提供显式 orphan-queued catalog/reconcile，而不是
依靠调用方猜测状态。

## 5. Dispatch 前取消

`cancel_before_dispatch()` 同时接受 `admitted` 与 `queued`：

1. 精确读取该 ToolJob 的 capacity waiter；
2. waiting waiter 先写入 cancelled，再将 ToolJob 写入 cancelled；
3. claimed waiter 拒绝 pre-dispatch cancel，因为 capacity 已原子 reservation，必须由 queued
   dispatch/reconcile 协议收口；
4. 相同 ToolJob cancel 重试返回已有终态，不产生新 receipt。

先关闭 waiter 可避免 ToolJob 已 cancelled、但活跃 waiting waiter 之后仍被 claim。若在 waiter
取消后、ToolJob 取消前崩溃，重试会继续完成 ToolJob 终态，且 waiter 已无法被调度。

## 6. 验证证据

局部真实 SQLite 测试覆盖：

- 未饱和 Worker 拒绝入队，饱和后创建 durable waiter；
- 关闭并重新打开两个 Store 后幂等返回同一 waiter；
- ToolJob receipt 为 sequence 2 的 `queued` 且哈希链有效；
- queue 数据库不包含真实 shell command；
- `queued` 状态与关联 waiter 双重阻断普通 dispatch；
- 强制交错 direct dispatch 与 `queued` 提交，最终 ToolJob 写事务阻断旁路；
- 模拟崩溃窗口：只提交 `queued`、不创建 waiter，重启后补建成功；
- waiting waiter 先取消、ToolJob 后取消，并验证重试幂等；
- claimed waiter 阻断普通 dispatch 与 pre-dispatch cancel，capacity reservation 保持；
- ToolJob schema v1→v3、v2→v3 迁移保留 identity 与 receipt chain。

本轮只运行 ToolJob、Worker capacity queue、Shell Worker、Worker authority health 与 Store Catalog
相关小模块检查，不运行全量测试。

## 7. 自我审视与后续依赖

本实现把状态、背压、幂等、迁移和崩溃窗口落实在真实数据库 authority 中，不是内存队列或
Prompt 套壳。当前仍明确缺少：

- claimed waiter 到唯一 dispatch receipt 的原子/可恢复 bridge；
- claim owner lease、超时、发送前崩溃与发送后未知副作用 reconcile；
- orphan `queued` 运维 catalog、自动补偿与用户可见状态；
- priority、aging、workspace 公平与 starvation 指标；
- Agent/Browser Worker adapter。

因此下一最小切片为 ARC-06.2b2：只实现 claimed ToolJob 的 queued dispatch/reconcile，不先扩展
完整 Agent 集群或高级调度策略。
