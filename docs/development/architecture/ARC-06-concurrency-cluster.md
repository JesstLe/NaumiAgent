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
- ARC-06.2 Scheduler：priority、deadline、fair queue、dependency DAG、affinity。
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
