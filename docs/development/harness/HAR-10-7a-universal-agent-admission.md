# HAR-10.7a 统一的进程内 Agent Admission

## 问题

`SubAgentManager.execute_parallel()` 已通过 `max_parallel_agents` 和共享 semaphore 限制批量 Agent，
但 `delegate_task`、Pursuit、SPAR、Supervisor 等生产入口都直接调用 `delegate()`。旧实现的直接路径不经过
该 semaphore，因此多个独立调用者可以同时越过配置上限；Runtime 中显示的“活跃/排队”也只对批量入口
可信。

本切片先补齐同一进程内的统一 admission，不提前伪装 ARC-04 持久 Agent Worker 或 ARC-06 集群调度。

## 实现契约

- 所有公开 `delegate()` 调用在注册 execution、写 heartbeat、发送 started event 和调用模型前，必须取得
  `_parallel_agent_slots`；该门使用 `BoundedSemaphore`，错误的重复释放不能静默扩大上限。`bypass` 只影响
  权限，不能绕过该容量门。
- `execute_parallel()` 保留既有批次 worker 与任务顺序，但在已取得 slot 后进入共享
  `_run_admitted_delegation()`，不能再次调用公开 admission 形成双重占位。
- 直接等待者与批量未开始项共同进入 `queued_parallel_agent_count`；`/runtime subagent` 因此读取全部生产
  入口的进程内排队事实，而不是只看到 DAG/批次，并明确标为“进程内 Agent 并发”而非伪称集群权威。
- 等待任务被取消时必须准确撤销 queue 计数；已取得 slot 的执行无论完成、异常、超时或取消，都在
  `finally` 中归还 semaphore。
- admission context 使用 `ContextVar` 随当前 Agent 执行传播。当容量已经耗尽、当前 Agent 又同步等待同一
  manager 的嵌套委派时，必须返回明确错误，不能在 `max_parallel_agents=1` 时形成永久自锁；若仍有空位，
  嵌套执行可按同一总上限正常进入。

## 验收标准

- 五个并发的直接 `delegate()` 在上限 2 时峰值严格等于 2，其余 3 个可观测为排队，全部完成后计数归零。
- 批量任务和随后到达的直接任务在上限 1 时消费同一个容量门；直接任务不能与批量任务并行越界，批量
  结果顺序保持稳定。
- 取消一个尚未取得 slot 的直接等待者不会执行 Agent、不会泄漏 queue 计数，随后新任务可立即复用容量。
- 取消一个已经取得 slot 的直接执行后，替代任务可立即进入，证明 active cancellation 也会归还容量。
- 上限 1 的饱和嵌套委派在 1 秒内 fail closed，不发生死锁。
- 既有批量 FIFO、并发批次共享上限、父取消、失败隔离、执行停止、heartbeat 和 lifecycle 小模块测试继续
  通过。

## 明确保留边界

- 这是 embedded Runtime 的进程内 admission；进程重启不会恢复等待队列，也不能协调多个 Runtime 实例。
- 等待队列尚未设置持久容量、priority、deadline、aging 或 provider/workspace 维度；这些属于后续
  HAR-10.7b/ARC-06 scheduler authority，不能把本切片描述成完整背压或公平调度。
- Agent 仍不是 ARC-04 持久 Worker producer，因此本切片不写 Worker Registry reservation；持久 incarnation、
  crash fencing 和跨进程容量必须在 Agent Worker 合同建立后接入 ARC-06.1a authority。
- Browser TaskRunner 有自己的进程内 queue，本切片不改变 Browser admission。
