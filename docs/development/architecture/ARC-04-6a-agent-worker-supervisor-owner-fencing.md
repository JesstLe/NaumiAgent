# ARC-04.6a / HAR-10.7h3 Agent Worker Supervisor Owner Fencing

## 1. 目标与依赖裁决

ARC-04.5e2 已让独立 control-only Agent Worker 持有 exact `AgentJob` pre-start claim 与物理 slot，
但父进程硬崩溃后只会留下到期 authority。heartbeat 只是诊断事实；仅凭 stale/offline 自动接管可能让仍
存活或已进入副作用阶段的进程与重放 Job 并行执行。

本切片实现最小合法 takeover，且刻意停在执行前：

```text
Supervisor owner lease
  + exact Worker registration/incarnation
  + actionable heartbeat
  + durable PID/create-time witness
  + OS process dead/reused/zombie observation
  + expired pre-start AgentJob claim
  + expired/fenced physical reservation
  -> atomic Worker fencing receipt
  -> authenticated idempotent AgentJob requeue
```

它不调用模型、不执行工具、不把 Job 标记为 `running`，也不处理副作用未知的 running Job。该最小前置
解除独立执行纵向切片的 crash-before-start 风险，而不提前扩张完整 ARC-04.6。

## 2. 进程身份证据

Agent child 在 registration 后、首个 heartbeat 前写入 `worker_process_witnesses`：

- exact `worker_id / instance_id / epoch / contract_sha256`；
- OS PID；
- 由 `psutil.Process(pid).create_time()` 获取并量化为微秒的进程出生时间；
- 带时区的 witnessed timestamp。

Supervisor 重新观测 PID 与出生时间，将结果机械分类为 `alive / dead / reused / zombie / unverifiable`。
`alive` 必须拒绝 fencing；权限不足、平台观测异常等 `unverifiable` 也必须 fail closed。PID 相同但出生时间
不同按 `reused` 处理，避免 PID 重用把已死亡 incarnation 误判为存活。该实现使用 psutil 的统一进程 API，
但本切片的真实进程验收运行于当前 Darwin 主机；Linux/Windows 仍需各自 CI 或真实主机矩阵证明。

## 3. Supervisor owner lease

Worker Registry schema v4 增加每个 `worker_id` 唯一的 Supervisor lease：

- `owner_id + epoch + expiry` 共同形成 owner fence；
- live foreign owner 存在时，竞争者只返回 `standby`，不得读取后继续修改；
- takeover 必须分配更高 epoch；
- fencing 前再次续租并在 Registry 事务内复验 exact live owner/epoch；
- reconcile 结束显式 release，过期 lease 仍可由后续 owner 安全接管。

因此多个 Runtime 或恢复进程并发观察到同一 stale Worker 时，只有一个 owner 可以签发有效 fencing。

## 4. takeover 证据门

一次自动 fencing 必须同时通过：

1. active registration 是 exact Agent Worker incarnation；
2. contract 具备 `agent_control_transport + agent_job_owner_lease`，且不具备
   `agent_context_scope`，即仍是 control-only pre-start 边界；
3. heartbeat identity 与 registration 完全一致，健康度为 stale/offline/stopped/failed；
4. durable process witness 与 registration 一致，OS 观测为 dead/reused/zombie；
5. 若 owner 持有 AgentJob，只允许 `claimed`；`running` 一律返回
   `agent_job_running_side_effect_unknown`，不得自动 requeue；
6. claim expiry 不晚于裁决时刻；
7. exact physical reservation 已 expired/fenced，缺失或仍 active 均拒绝；
8. Supervisor lease 在写入事务中仍为 exact live epoch。

这些条件采用全取或全拒绝语义。heartbeat 不健康、进程死亡、lease 到期中的任意单一事实都不足以授权
接管。bypass 权限模式也不能绕过机械 fencing。

## 5. 原子 fencing 与跨 Store 收口

Worker Registry 在单个 `BEGIN IMMEDIATE` 中重新验证全部 Registry 事实，随后原子执行：

- revoke exact active Worker registration；
- fence 该 incarnation 的 active reservation/waiter；
- 保存 canonical evidence；
- 使用 Runtime payload key 对 fencing receipt 做 HMAC-SHA256 认证；
- 以 operation digest 保证同一证据幂等。

`AgentJobStore.requeue_expired_prestart_worker_claim()` 只消费认证通过的 receipt，并再次复验 Job ID、request
digest、owner、claim epoch、claim expiry 和最新 receipt digest，随后把 exact `claimed` 返回 `admitted`。
两 Store 无法共享事务，因此顺序固定为“先永久 fencing Worker，再 requeue Job”。若进程在两步之间崩溃，
下次 Supervisor 读取最新认证 receipt 并幂等补完 requeue；绝不先重放 Job 再尝试关闭旧执行者。

持久 JSON 解析要求顶层与 evidence 字段集合精确匹配，未知、缺失、超大、摘要不符或 HMAC 不符全部拒绝。

## 6. Runtime 与用户可观测性

Composition Root 提供惰性的 `AgentWorkerSupervisorFactory`，构造 Engine 不启动循环、不访问 key，也不创建
进程。Supervisor fencing 数量、最近时间与最近安全退回 Job 进入共享 `WorkerAuthoritySnapshot`；Doctor
在无 active Worker 时可显示“Supervisor 已完成显式 fencing”。New UI 与 Textual TUI 继续消费同一 Doctor
authority，不直接查询 SQLite、不解析 receipt，也不显示 owner、PID、task/context、key 或 HMAC。

## 7. 聚焦验收

- 真实子进程 heartbeat 过期但 PID/create-time 仍存活时拒绝 fencing；
- 真实子进程死亡且 claim/slot 到期后，registration 被撤销、slot 收口、Job 安全回到 admitted；
- 同一 receipt 重放不增加 Job transition sequence；
- foreign Supervisor 持有 live lease 时竞争者返回 standby；
- running Job 即使 heartbeat 不健康、进程死亡、authority 到期，也因副作用未知而保持 running；
- receipt 增加未知字段或认证不匹配时 fail closed；
- Doctor/New UI/Textual TUI 只显示共享低敏聚合；
- schema v1/v2/v3 Registry 可迁移到 v4，重复 migration 幂等。

仅运行 Agent Worker Supervisor、Worker Registry/process、AgentJob、authority health、Runtime Composition、
Doctor 与文档治理小模块测试，不运行全量测试。

## 8. 自我审视与未完成

本切片完成的是 control-only crash-before-start fencing，不是完整 Supervisor：

- 没有周期后台 reconcile、退避、crash-loop budget、quarantine、自动重启或告警；
- 不处理 running/model/tool 副作用恢复，也不提供 unknown 的自动重放；
- 没有跨主机 process witness、leader election 或远程 kill；
- 没有 upgrade drain、版本迁移、滚动发布和多 Worker 调度；
- Registry 与 AgentJob 是两个 SQLite authority，依靠 fencing-first + receipt replay 收口，不宣称跨 Store
  exactly-once；
- Runtime payload key provider 配置不一致会认证失败并停在“Worker 已 fenced、Job 未 requeue”的安全状态，
  后续需通过统一 key 配置或运维修复；
- 当前真实 OS 进程证据来自 Darwin，Linux/Windows 仍需平台矩阵。

`ARC-04.5e3a / HAR-10.7h4` 已在该基础上完成 model-only 的 exact
`prepare -> mark_running -> Provider call -> encrypted terminal -> publication`，并把 running 后进程丢失明确
留在 recovery-required，详见
[`ARC-04-5e3a-independent-agent-model-execution.md`](ARC-04-5e3a-independent-agent-model-execution.md)。
ARC-04.5e3b1 已补齐加密 Tool RPC 内核；下一步只接生产 SubAgent 路由与 Engine authority adapter，不应
先把 ARC-04.6 的所有运维能力一次做完。
