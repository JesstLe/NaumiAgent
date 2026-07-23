# ARC-06.1b ToolJob Capacity 生命周期闭环

## 交付目标

ARC-06.1a 提供了原子 Worker slot reservation，但尚未进入真实执行路径。本切片让
`ToolJobAuthority.dispatch()` 在越过 transport boundary 前占位，并让 ToolJob 终态与恢复收口释放该占位。
它完成第一个生产消费者闭环，不提前实现公平队列或通用 Agent/Browser scheduler。

## 权威顺序

1. dispatch 重新验证 ExecutionGrant、Worker incarnation、heartbeat、能力、资源与隔离合同；
2. 读取 ToolJob 生命周期，终态或非法状态直接由生命周期 authority 拒绝，不消耗容量；
3. 使用确定性的 `capacity-{job_id}` 在 Worker Registry 原子 reserve；
4. reserve 成功后以 `dispatch_committed_capacity_v1` receipt 持久化 `dispatched`，只有首次 transition
   可以发送 payload；
5. `succeeded/failed/cancelled/unknown` 持久化后释放同一 Worker incarnation 的 reservation；
6. 同一 dispatch 重试复用 active reservation，终态重试复用 receipt 和 release，不重复计数。

reservation TTL 取 Worker `max_wall_seconds` 与 ToolJob 剩余授权期的较小值。`bypass` 只影响权限决策，
不能绕过 slot、TTL 或 incarnation fencing。

## 跨 Store 失败语义

ToolJob 与 Worker Registry 是两个独立 SQLite authority，当前不伪装成分布式事务：

- reserve 成功、dispatch 落盘失败时，reservation 保持 active 并受 TTL 上限约束；同一合法重试可复用；
- ToolJob 终态已落盘、release 调用失败时，终态事实不回滚；同一终态重试会继续执行 release；
- 升级前 `dispatch_committed` receipt 被识别为 legacy in-flight Job，可正常写终态但不会虚构 reservation；
- worker takeover/revoke 会先 fencing reservation；迟到终态不能借 release 改写 fencing 审计原因；
- reservation 已因 TTL 到期或 incarnation fencing 终结时，终态 cleanup 接受该既有终态但不复活或覆写它；
- capacity exhaustion 原样抛出 `WorkerCapacityExhaustedError`，供后续 scheduler 排队，而非退化为无界派发。

这是一种 fail-safe、容量可能暂时少报但绝不超卖的选择。跨 Store outbox/reconcile 属于 ARC-05/ARC-08
后续模块。

## 聚焦验证

- capacity=1 被另一 Job 占满时，ToolJob dispatch 被原子拒绝且生命周期保持 admitted；
- 空位释放后首次 dispatch 占一格，同一 dispatch 在更晚时间重试不延长 TTL、不重复占位；
- success/failed/unknown 终态释放容量，终态 retry 保持幂等；
- 已进入 unknown 的 Job 再 dispatch 返回生命周期冲突，不产生幽灵 reservation；
- 并发终态仍只追加一个 receipt，release 同样收敛为一个终态事实；
- Worker takeover/revoke fencing 后，旧 Worker 无法提交终态。

相关验证仅运行 Worker Registry、ToolJob 与真实 Shell Worker 小模块，不运行全量测试。

## 明确未完成

- Agent 与 Browser 当前只有进程内执行/heartbeat 形态，还没有满足 ARC-04 合同的持久 Worker producer，
  因此未伪造接入；
- 尚无 capacity waiting queue、priority/deadline、公平与 starvation aging；
- 尚无 workspace、provider、browser profile、token/cost 等多维 reservation；
- crash 后主动回收仍依赖 TTL 或 Worker fencing，尚无跨 Store outbox/reconciler；UI-13.1d 已显示当前
  reservation 占用/可用，但 queue wait、orphan age、利用率趋势仍未交付。
