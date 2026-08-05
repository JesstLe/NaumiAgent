# ARC-06.2c Durable Embedded Agent Capacity Admission

## 1. 目标与依赖裁决

ARC-04.5c 已让 embedded `SubAgentManager` 消费加密 AgentJob lifecycle，但每个 Runtime 仍只受自己的
`asyncio.Semaphore` 约束。两个进程各自配置 `max_parallel_agents=4` 时，最多可以同时启动 8 个模型任务，
而本地状态栏仍各自显示“4/4”，无法证明共享背压。

本切片先建立跨 Runtime 的 embedded Agent capacity authority。它刻意不把当前 Runtime 注册成
`WorkerRegistry` incarnation：embedded Runtime 不是独立 Agent daemon，并发启动的 Runtime 也没有可信的
单一进程 owner。伪造注册会导致互相 takeover/fencing，并让 Doctor 把逻辑容量池误报为存活 Worker。

因此本切片在既有 `AgentJobStore` 内组合 job claim lease 与 durable capacity policy：

```text
local bounded admission
  -> encrypted AgentJob admit
  -> atomic shared capacity decision
     -> available + FIFO empty: admitted + claimed in one transaction
     -> busy + queue available: durable admitted waiter
     -> queue full: reject without partial job
  -> exact FIFO claim
  -> recover payload / running / renew / terminal
  -> capacity becomes reusable
```

这是 HAR-10.7 与 ARC-06.2 的共同垂直切片，不等于独立 Worker、完整 scheduler 或 Agent cluster。

## 2. Store schema v2 引入的 durable policy

`AgentJobStore` 从 schema v1 迁移到 v2，新增 singleton `agent_job_capacity_policy`：

- `max_active_jobs`：1..10000；
- `max_waiters`：0..10000；
- `configured_at` 与 `updated_at`：aware ISO 时间；
- policy 首次由生产 manager 的 `max_parallel_agents/max_queued_agents` 固化；
- 存在 admitted、claimed 或 running Job 时，其他 Runtime 不能用不同配置改写共享上限；
- 非终态集合清空后允许显式配置变化，避免永久锁死合法配置升级；
- v1 原位迁移不重写加密 payload、request、receipt 或 terminal result。

ARC-04.5d1 后续把 Store 升级到 schema v3 并新增加密 terminal payload 列；ARC-04.5d2a 又升级到
schema v4，新增 publication outbox 和 event chain。本节的 capacity policy、计数与迁移语义保持不变。
ARC-04.5d2b 又把 Store 升级到 schema v5，新增幂等 result inbox；同样不改变本节 capacity
policy 与 FIFO 语义。HAR-10.7g 再升级到 schema v6，新增 publication quarantine authority；隔离结果
不再占用 publication FIFO，但同样不改变 Agent Job active/waiting capacity。

policy 不保存 workspace 路径、Prompt、模型结果、owner ID 或密钥。Store 仍使用原有显式 Runtime key 管理
加密 job payload 和认证 lifecycle receipt。

## 3. 原子 admission 与全局硬上限

`admit_for_capacity()` 在单个 SQLite `BEGIN IMMEDIATE` 中完成：

1. 验证并密封 request/payload；
2. 创建或复验 durable policy；
3. 处理相同 request 的幂等重放；
4. 读取 `admitted/claimed/running` capacity 事实；
5. 若没有 FIFO 前序且有空位，原子写 admission 和 claim；
6. 否则只在 `waiting_jobs < max_waiters` 时写 admitted waiter；
7. 队列已满时抛出稳定 `AgentJobCapacityExhaustedError`，不留下 partial Job。

这消除了“先写 Job、再观察容量”的跨事务超卖窗口。三个独立 Store 争抢
`active=1, waiters=1` 时只能得到一个 claimed、一个 admitted 和一个明确拒绝。

低级 `admit()` 在 policy 已存在时也不能无限增加 waiting；低级 `claim()` 会复用同一 capacity/FIFO
检查，不能绕过生产入口抢占后到任务。

## 4. FIFO、续租和恢复边界

`claim_for_capacity()` 只允许最老 admitted Job 在共享 active 数低于上限时 claim。排序固定为
`(admitted_at, job_id)`，多个 Runtime 的轮询顺序不会改变 durable FIFO。

capacity 计数语义：

- live `claimed`：占用 active；
- 所有 `running`：占用 active；
- expired `claimed`：计入 `reclaimable_prestart_jobs`，不冒充仍在执行；
- expired `running`：继续占用 active，并计入 `recovery_required_jobs`；
- `admitted`：计入 waiting；
- terminal：立即释放共享 active/waiting 预算。

expired running 不能因为 lease 到期就自动释放容量。模型或外部副作用可能仍在运行，必须先通过既有
`mark_recovery_unknown()` 做人工/监督器裁决。这选择“可能少调度”而不是“悄悄超卖”。

## 5. Embedded Runtime 接入

`SubAgentManager` 在本地 semaphore 后调用 durable capacity admission：

- 立即 claim 时继续 ARC-04.5c 的 payload recover 与 start fence；
- durable waiting 时 execution phase 为 `waiting_capacity`，模型尚未调用；
- 以 50ms 起步、最高 500ms 的有界退避轮询 exact Job；
- 用户停止 waiting execution 时调用 `cancel_before_claim()`，持久 Job 进入 cancelled；
- 父协程取消同样先取消 durable waiter，再传播 `CancelledError`；
- admission/后续异常会在 `_finish_execution()` 清理仍为 admitted 的 Job；
- policy 冲突、队列耗尽、取消和清理失败使用稳定低敏错误码，不回显数据库或密钥细节。

本地 semaphore 继续保护单 Runtime 的协程/对象预算；durable capacity 负责多 Runtime 共享上限。两层都
不能被 bypass 权限模式绕过。

## 6. 可观测性与前端一致性

`AgentJobCapacitySnapshot` 只输出 policy 与聚合计数：

- active / max active；
- waiting / max waiters；
- reclaimable pre-start；
- recovery required；
- available；
- assessed time。

共享 Agent Control authority 将上述计数加入 summary，并允许 execution phase
`waiting_capacity`：

- New UI 正常为空闲时使用绿色，存在 waiting/reclaimable 时使用黄色，recovery required 使用红色；
- Textual TUI 复用同一 summary，以状态图标和 Markdown 强调显示；
- `/runtime subagent` 同时区分“进程内并发”和“共享持久 capacity”；
- 前端不读取 SQLite，不显示 Job identity 列表、owner、Prompt、路径或密钥。

## 7. 聚焦验收

- 三个独立 Store 并发争抢 `1 active + 1 waiter`，结果严格为 1 claimed、1 admitted、1 rejected；
- terminal 后 FIFO waiter 可跨 Store claim，capacity 回到可复用状态；
- 后到 Job 的 raw claim 不能跳过最老 waiter；
- 活跃/等待存在时配置漂移 fail closed；全部终态后可变更 policy；
- expired running 继续阻塞 capacity，`recovery_required_jobs=1`；unknown 裁决后释放；
- v1→v2 原位迁移保留既有加密 Job；
- 两个真实 `SubAgentManager` 共用数据库时第二个模型不会提前调用，第一项结束后才启动；
- 用户取消 durable waiting 后模型调用次数为 0，Job 为 cancelled；
- Agent Control、New UI、Textual 和 `/runtime` 显示同一聚合事实；
- 只运行 AgentJob、SubAgentManager、Agent Control、TUI、Runtime status、Store Catalog 和 Node
  protocol/render 小模块，不运行全量测试。

## 8. 自我审视与未完成

本切片真实解决了 embedded 多 Runtime 超卖和无界共享等待，但没有完成：

- ARC-04.5e1 已完成独立 Agent control-only 注册，ARC-04.5e2 又完成 pre-start owner lease、物理 slot
  与加密 staging；独立模型执行、Supervisor 和 upgrade 仍未完成；
- Worker Registry 物理 slot reservation；未来独立 Worker dispatch 仍必须叠加 ARC-06.1 authority；
- 自动接管 expired claimed Job；当前仅计数并允许 exact owner/caller takeover；
- running recovery UI 动作、自动 unknown 裁决，以及 result inbox 的 UI read/ack、周期 retry；
- priority、aging、跨 workspace/user/provider 公平、token/cost reservation；
- 多主机共识、leader lease、24h soak 与 1k job 压测。

下一步不应直接扩张完整 scheduler。应重新比较：

1. `ARC-04.5d1 Durable Agent Terminal Payload` 已完成加密原文 source 与生产恢复屏障，
   `ARC-04.5d2a` 已完成 publication outbox authority，`ARC-04.5d2b` 已完成 production
   manager 在线消费、幂等 inbox 与 startup recovery；下一步应比较 recovery UI 与 Supervisor 依赖。
2. Agent recovery UI，为 reclaimable/recovery-required 提供精确人工动作；
3. `ARC-06.3 Provider Budget Reservation`，让模型并发同时受 provider/token/cost 预算约束。

选择时继续以用户可见闭环和跨文档最小依赖为准。
