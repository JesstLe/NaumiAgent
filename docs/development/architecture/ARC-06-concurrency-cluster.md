# ARC-06 高并发、背压与 Agent 集群调度

## 目标

在工具、Agent、浏览器和模型调用上提供有界并发、公平调度、隔离、背压和可观测性，避免“并发
越高越快”的无界资源消耗。

## 子模块

- ARC-06.1 Admission control：全局/用户/workspace/provider/tool 多级容量。
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
