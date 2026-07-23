# UI-13.1e Worker Queue Backlog Health

## 1. 目标与依赖

ARC-06.2a-2b2 已交付 durable Worker capacity queue、ToolJob 入队和 claimed Shell dispatch，但现有
UI-13.1d 只显示 reservation 占用，不显示等待积压。自动 scheduler 或 Agent Worker 若继续建立在不可见队列
之上，用户无法区分“模型仍在工作”“任务正在等待 capacity”“队列已满”与“过期事实尚未收口”。

本切片扩展现有只读 Worker authority health，让 New UI 与 Textual TUI 从同一 Doctor fact 看见：

- durable queue policy 上限；
- 当前仍在 deadline 内的 waiting 数；
- 仍绑定未过期 active reservation 的 claimed 数；
- 最老 live waiter 已等待多久；
- deadline/TTL 已到但写 authority 尚未收口的 waiting/claim 数。

本切片不运行 scheduler、不 claim/cancel Job、不写 Registry，也不展示 job/queue/reservation identity。

## 2. 共享只读 Authority

`inspect_worker_authority_health()` 继续使用 SQLite `mode=ro + query_only`，并在同一 Registry 快照中为每个
active Worker 组合 registration、capacity reservation、queue policy 与 backlog：

1. policy 必须精确绑定 active worker instance/epoch；
2. `max_waiters` 必须在 schema 范围内，`configured_at` 不得晚于 Doctor 评估时间；
3. waiting 行最多为 durable policy 上限，全部复用公共 waiter 反序列化验证；
4. `deadline_at <= assessed_at` 的 waiting 行只计为“待收口”，不会被 Doctor 改写为 expired；
5. live waiting 的 oldest age 由规范时间机械计算；
6. claimed waiter 与 reservation 的 worker/instance/epoch/job/id 必须完整一致；
7. 只统计 reservation 数据库状态为 active 的 claim；其 TTL 已到时计入“待收口”；
8. active claim 数受 Worker `max_concurrent_jobs` 约束，超过合同上限或关联损坏即 fail closed。

released/expired/fenced reservation 对应的历史 claimed waiter 不进入 backlog。仅凭 Registry 无法判断其
ToolJob 最终结果，本页面不会把正常历史记录误报为 orphan。

## 3. 用户可见语义

共享 Doctor 快照提供两个独立条目：

- `Worker authority`：只呈现身份、容量与心跳；
- `Worker 容量队列`：呈现等待、领取、最久等待与待收口数量。

队列条目示例：

```text
已配置队列 Worker 1 个。 tool-worker-a 等待 1/8、领取 1、最久 3.0s、待收口 1
```

从未配置 queue policy 时显示“队列未启用”，不伪造零上限。New UI、Textual TUI 与 CLI
都消费同一条 Doctor 投影，不各自读取数据库或维护队列状态。

- waiting 达到 durable policy 上限：`degraded/warn`，建议等待容量释放，避免无界重试；
- 存在到期 waiting/claim：`degraded/warn`，建议运行 scheduler reconcile；
- 容量满或队列满本身不是 Runtime corruption，不升级为 error；
- heartbeat、identity、schema、关联篡改继续拥有更高优先级并显示 error；
- 文字语义完整，不依赖颜色判断状态。

New UI 继续消费 `doctor/health` schema v1 generic runtime item；Textual TUI/CLI fallback 继续消费同一个
`DoctorCheck` Markdown。因此两端没有第二份 queue reducer、计数器或状态机。

## 4. 隐私与有界性

- 不展示或导出 job id、queue id、reservation id、workspace digest、路径或原始错误；
- waiting 扫描受 `max_waiters <= 10000` 硬界约束；
- active claim 扫描受 `max_concurrent_jobs <= 10000` 硬界约束；
- terminal waiter 历史不会被无界加载；
- Doctor 读取前后 Registry 字节保持一致；
- queue row、policy 或 claim link 被篡改时返回稳定 `registry_unreadable`，不尝试修复。

## 5. 聚焦验收

- policy=4、live waiting=1、active claim=1、expired waiting=1 显示精确 backlog、oldest age 与待收口数；
- read-only 诊断后 expired waiting 数据库状态仍是 waiting；
- waiting=2/max=2 显示 degraded，而非 runtime error；
- policy instance 与 claimed waiter/reservation 双向关联篡改均 fail closed；
- typed Doctor snapshot 保留相同 queue 文字且不包含任何内部 identity；
- New UI 在 80/120/200 列宽完整渲染 queue backlog 关键词；
- Textual/CLI Markdown 路径显示同一摘要与建议；
- 只运行 Worker Authority、Doctor Health、New UI Doctor 页面及相关 parity 小模块，不运行全量测试。

## 6. 自我审视与未完成

本切片让真实 backlog 可见，但不等于 queue catalog 或 scheduler：

- 只展示每个 active Worker 的聚合，不提供 job 级详情、筛选、分页或操作；
- 不展示 released/expired/fenced 的历史 claim；
- “待收口”只证明 Registry 时间事实到期，不推断 ToolJob 是否 terminal；
- 没有 scheduler loop、claim owner lease、自动唤醒、优先级、公平或 starvation SLO；
- Agent/Browser 仍不是持久 Worker producer；
- 多 Worker 全屏 queue 页面与趋势指标仍属于后续 ARC-06.8/UI-13 详情切片。

下一步应在有了 backlog 运维门之后，重新比较 ARC-06 自动 scheduler/claim lease 与 ARC-04.5a Agent
Worker Contract；不得用本聚合摘要冒充完整 queue catalog。
