# ARC-06 高并发、背压与 Agent 集群调度

## 目标

在工具、Agent、浏览器和模型调用上提供有界并发、公平调度、隔离、背压和可观测性，避免“并发
越高越快”的无界资源消耗。

## 子模块

- ARC-06.1 Admission control（partial）：
  - ARC-06.1a 已在 Runtime Worker Registry v2 交付 incarnation-fenced、TTL 有界、SQLite 原子提交的
    worker slot reservation；并发调度者不能超卖 `max_concurrent_jobs`。见
    [设计与验证](ARC-06-1a-worker-capacity-reservations.md)。
  - ARC-06.1b 已让真实 ToolJob dispatch-before-send 原子占位，并在成功、失败、取消和 unknown 收口释放；
    同一 dispatch 重试不重复占位，跨 Store 中断以 TTL fail-safe 收敛。见
    [设计与验证](ARC-06-1b-tool-job-capacity-lifecycle.md)。
  - HAR-10.7a 已先封住 embedded Runtime 的直接 Agent 委派旁路：所有 direct/batch/DAG 入口共用进程内
    semaphore，并对等待取消和饱和嵌套自锁 fail closed。它不是持久 Worker reservation，不替代本模块。
  - HAR-10.7b 已为同一进程内 admission 增加有界等待预算和明确 overload response，避免 embedded
    Runtime 在持久 scheduler 落地前无界积压；它仍不提供跨进程公平、恢复或 reservation authority。
  - 未完成：Agent/Browser worker dispatch、全局/用户/workspace/provider 多级容量与等待调度。
- ARC-06.2 Scheduler（partial）：
  - ARC-06.2a 已在 Worker Registry v3 交付 exact-incarnation、deadline、有界 FIFO 的持久等待队列；
    `max_waiters` 按 worker epoch 固化为 durable policy，FIFO claim 与 capacity reservation 在同一
    SQLite 事务提交，Worker takeover 会 fence 旧等待项。见
    [设计与验证](ARC-06-2a-durable-worker-capacity-queue.md)。
  - ARC-06.2b1 已让生产 ToolJob 在真实容量饱和后进入该队列，以 ToolJob schema v3 `queued` receipt
    阻断直接派发旁路，并闭合重启补建 waiter 与 dispatch 前取消。见
    [设计与验证](ARC-06-2b1-tool-job-capacity-queue-admission.md)。
  - ARC-06.2b2 已让 claimed waiter 的 active reservation 进入 ToolJob dispatch-before-send 与真实
    Shell start fence；terminal 统一释放 reservation，lost claim 可证据化 no-side-effect 收口。见
    [设计与验证](ARC-06-2b2-claimed-tool-job-dispatch-reconcile.md)。
  - UI-13.1e 已把 durable policy、live waiting、active claim、oldest wait 与到期待收口事实投影到
    New UI/TUI 共用的只读 Doctor authority，为自动 scheduler 提供最小运维门。见
    [设计与验证](../cli-ui/UI-13-1e-worker-queue-backlog-health.md)。
  - ARC-04.5a 已为 embedded Agent 签发真实 request/result contract 并在 New UI/TUI 投影低敏摘要；
    但合同仍未持久化，不能由 scheduler 在重启后恢复。见
    [设计与验证](ARC-04-5a-agent-worker-contract.md)。
  - ARC-04.5b1 已提供 OS credential-backed Runtime payload key 与 authenticated envelope；
    ARC-04.5b2 已建立 durable Agent Job Store、FIFO claim、epoch/lease fencing、pre-start takeover
    与 running recovery fence。embedded Agent 和自动 scheduler 尚未消费。见
    [设计与验证](ARC-04-5b2-durable-agent-job-authority.md)。
  - 未完成：自动 scheduler loop、claim owner lease、queue catalog、priority、aging、跨 workspace
    公平、dependency DAG、affinity、cursor 与 starvation 指标。
- ARC-06.3 Budget reservation：token/cost/time/CPU/memory/browser slots 预留与归还。
- ARC-06.4 Backpressure：producer pause、bounded queue、drop/coalesce policy、overload response。
- ARC-06.5 Isolation：workspace lock、browser profile、env、artifact namespace、rate limit。
- ARC-06.6 Failure containment：单 job/worker/provider 熔断、重试预算、bulkhead。
- ARC-06.7 Cluster topology：leader/worker lease、capability routing、heartbeat、drain。
- ARC-06.8 Observability：queue wait、service time、utilization、starvation、retry、cancel latency。

## 验收标准

- 1k jobs 压测队列有界；高优先级可前移但低优先级不永久饥饿。
- 同 workspace destructive job 串行，只读安全工具可按 metadata 并行。
- provider 429 触发共享退避，不形成重试风暴；其他 provider 不被连带阻塞。
- 浏览器任务 profile/tab 隔离；失败清理不关闭其他任务资源。
- Agent 集群消息按 run/agent id 隔离，终态只写一次。
- A5：负载阶梯、故障注入、24h soak 和资源泄漏报告。

## 明确限制

并发上限必须可配置且有安全默认；bypass 不绕过容量和操作系统资源限制。
