# HAR-08.4g 有界 Sandbox Batch Admission

## 目标

在开放通用 `/harness eval sandbox` 之前，先约束同一 Runtime 内真实 Sandbox Eval 批次的活跃数与等待数。
HAR-08.4e 负责单 sample 的成组 Check authority，HAR-08.4f 负责单 Batch 的 lease/grant/恢复；本切片只负责
多个 Batch 之间的进程内容量，避免用户、Agent 与 Evolution 同时提交批次时无界堆积协程、Git snapshot 和
Shell Worker admission。

## 容量合同

- `safety.max_parallel_sandbox_batches` 默认 2，范围 1..32；
- `safety.max_queued_sandbox_batches` 默认 8，范围 0..10000；0 表示活跃槽位用尽后立即拒绝；
- `HarnessSandboxBatchAdmission` 在任意 `await` 前原子检查 active + queued 总预算，因此同一事件循环的并发
  提交不会超卖最后一个等待位；
- 过载返回稳定 `sandbox_batch_capacity_exhausted`，中文回执包含活跃/排队实值与上限；
- 等待任务取消会立即归还 queued 计数；成功、业务异常、取消和清理异常都会归还 active slot；
- 持有同一 gate 的 Batch 同步嵌套启动会返回 `sandbox_batch_nested_admission`，防止低容量配置下自等待；
- 已由 H5a 证明完整的 Batch 在读取权限与取得容量前幂等返回，不会因当前队列繁忙而阻断只读恢复。

## 生产组合

Engine 为一个 workspace/Runtime 只创建一个共享 gate。Interventional RED、GREEN 与 adversarial cohort 的
HAR-08.4f compatibility adapter 都消费同一对象，因此三条入口共享容量；`bypass` 只影响权限确认，不绕过
资源预算。默认构造的独立 coordinator 仍会获得本地 gate，便于隔离测试，但跨 consumer 的生产保证以 Engine
composition 为权威。

配置由 onboarding、容器示例与安全文档共同公开。当前 snapshot 可同步读取 active/queued，为后续通用
Sandbox Eval 页面和 Runtime Inspector 暴露状态保留唯一权威。

## 验收证据

- active=1、queued=1 时第三个 Batch 立即稳定拒绝；
- 等待者取消后替代 Batch 可复用队列位置，全部结束后计数归零；
- 同一任务嵌套 admission fail closed，不进入永久等待；
- gate 已饱和时，完整 5/5 H5a Batch 仍直接返回且不签发 Run Grant；
- Engine 组合测试证明 RED/GREEN/adversarial 三个生产 consumer 引用同一 gate；
- 配置默认值、上下界和 onboarding 输出通过聚焦测试；
- Ruff、编译与相关小模块测试通过，未运行全量测试。

## 当前边界与下一步

这是单 Runtime 的进程内背压，不是 ARC-06 持久集群队列：重启不会恢复等待者，多进程/多主机仍需数据库
admission、priority/deadline/fairness 和 durable overload receipt。HAR-08.4 仍为 partial；下一步可以在此
安全前置上把 gate snapshot 接入 New UI/TUI 真实排队状态，而不是复制 UI 私有计数器。HAR-08.4h/4i/4j
已补齐 native request、Service、Tool 与 Slash。Linux/Windows 隔离 CI 证据仍未完成。
