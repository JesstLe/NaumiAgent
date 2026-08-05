# ARC-04.5e2 / HAR-10.7h2 独立 Agent Worker Job Owner Lease

## 1. 目标与依赖裁决

ARC-04.5e1 已建立真实 spawned Agent 控制进程、认证 IPC、registration、child-driven heartbeat 与
drain，但该进程不能证明自己持有任何 durable AgentJob。直接先实现 Supervisor 会把“进程存活”误当成
“可以接管 Job”，形成错误 takeover 面。

本切片先闭合一个独立 Worker 安全持有一个 **pre-start** durable Job 的证据链：

```text
active Worker incarnation
  -> exact physical slot reservation
  -> admitted-only AgentJob claim
  -> ephemeral AES-256-GCM dispatch staging
  -> authenticated child acknowledgement
  -> AgentJob claim + physical slot dual renewal
  -> child clear acknowledgement
  -> exact claim release + slot release
```

它不标记 `running`、不调用模型、不执行工具，也不自动接管 expired claim。因此 ARC-04.6 Supervisor
仍是后续独立执行与 crash takeover 的硬前置。

## 2. 能力合同

`WorkerCapability` 新增 `agent_job_owner_lease`，并机械要求同时声明
`agent_control_transport`。该能力只证明：

- Worker incarnation、物理 capacity reservation 与 AgentJob owner/epoch 已精确绑定；
- request/context 已通过进程级一次性密钥加密并由认证子进程完整解密、重新校验；
- owner lease 与物理 slot 可以持续续租并在 pre-start 阶段安全释放。

合同仍不声明 `agent_context_scope`，`health_report.accepting_jobs=false`，Doctor 显示
“Job owner lease 就绪、模型执行未开放”。任何完整 Agent admission 仍会因能力缺失和不接受任务而拒绝。

## 3. Durable claim 与 takeover 边界

`AgentJobStore.claim_admitted_for_worker()` 在单个 `BEGIN IMMEDIATE` 中复验：

1. Job ID 与 request digest；
2. 当前状态必须精确为 `admitted`；
3. 既有共享 capacity policy、FIFO 与 active 上限；
4. 新 owner ID、claim epoch 与 lease expiry。

该入口故意不复用通用 expired-claim takeover。即使旧 claim 已过期，独立 Worker 也会拒绝；只有未来
Supervisor 在核验 heartbeat、OS process、Worker incarnation 和副作用边界后才能签发显式 fencing。

`release_prestart_worker_claim()` 只允许 live exact owner/epoch 将 `claimed` 返回 `admitted`，追加
HMAC-authenticated `agent_worker_job_released` receipt，并保留单调 claim epoch。`running` Job 永远不能走
此路径，避免未来执行能力接入后把未知副作用伪装成未开始。

## 4. 物理 slot 与双续租

Worker Registry 先对 exact `worker_id/instance_id/epoch/job_id` 预留 slot，随后才 claim AgentJob。
reservation ID 和 AgentJob owner ID 都由 Worker contract identity 的 SHA-256 派生，调用方不能自由拼接。

`renew_capacity()` 复验 active incarnation、reservation owner、Job ID、TTL 和时间；到期、fenced、released
或身份漂移全部拒绝。Registry lease 与 AgentJob lease 都禁止续租缩短既有 expiry，阻断时钟回退或旧请求
覆盖较新租约。任一续租失败都会使控制进程进入 failed、终止子进程并尝试释放仍 live 的 pre-start authority。

## 5. 加密 staging 协议

进程启动时除 multiprocessing authkey 外，再生成独立 256-bit dispatch key。两者都只通过 spawn 参数进入
子进程，不持久化、不写日志、不进入 UI。

父进程从 AgentJob Store 重新认证并恢复 request/payload 后：

1. 使用既有 bounded binary framing 重新编码完整 request 与 payload；
2. 使用一次性 dispatch key 生成 AES-256-GCM envelope；
3. AAD 绑定 protocol、Worker incarnation、contract digest、Job ID、request digest、owner、claim epoch、
   初始 expiry 与 reservation ID；
4. 子进程严格校验消息字段集合、派生 identity、envelope digest、GCM tag、request digest 和 raw payload
   的 task/context/session/topic 哈希与长度；
5. 子进程只返回不含原文的 `job_bound` 回执。

release 时子进程先清除内存引用并返回 exact `job_released`，父进程随后释放 durable claim 和 slot。若 bind
发送后出现取消、超时、篡改或 EOF，父进程必须先终止子进程，再释放 pre-start authority；不能让仍可能
持有明文的进程与已重入队 Job 并存。

## 6. 并发与失败收口

- bind/release 由 command lock 串行，单个 control-only Worker 同时最多持有一个 Job；
- child pulse 与 Job ack 共用单调 sequence，并都写入同一 heartbeat authority；
- failure cleanup 使用独立锁，monitor、renewal task 与调用方并发失败时只有一个完整收口，其余调用者等待；
- 子进程崩溃时，因为合同机械禁止 `running` 和模型执行，父进程可在确认 OS 进程死亡后把 live claim
  安全返回 FIFO；
- 父进程崩溃无法执行 cleanup，claim/slot 只会到期，后续仍需 Supervisor 显式裁决，不自动重放。

## 7. 聚焦验收

- 构造 factory/process 保持惰性，不访问 Runtime key、不创建 SQLite 或进程；
- 真实 spawned child 完成加密 staging，Job/slot/health 三方显示同一 active 数；
- 续租同时推进 AgentJob receipt 与 capacity expiry；旧时间不能缩短任一 lease；
- release 先取得 child clear ack，再把 Job 返回 admitted FIFO 并释放 slot；
- live/expired foreign claim 均不能从独立 Worker 入口隐式 takeover；
- 密文或 envelope digest 篡改使 child fail closed，串行 cleanup 后 Job 回到 admitted；
- child 强制终止后只对 control-only pre-start Job执行安全 requeue；
- Doctor/New UI/TUI 共用的投影不显示 raw task/context、owner ID、dispatch key、nonce 或 envelope。

只运行 Agent Worker Process、AgentJob exact owner、Worker Registry、Worker contract/health、Runtime
Composition 与 Doctor 小模块测试，不运行全量测试。

## 8. 自我审视与未完成

本切片完成了真实独立进程的 Job 所有权和加密暂存，但仍未完成独立 Agent 执行：

- 子进程没有 ModelRouter、Tool Registry、Permission/Budget scope 或 terminal result publication producer；
- 没有 `mark_running` 协议、执行中取消、Provider request ID 或未知副作用对账；
- 没有 Supervisor 的 stale takeover、crash-loop budget、quarantine、upgrade drain；
- 没有自动 scheduler、跨 workspace/provider fairness、token/cost reservation 或多主机 leader；
- dispatch key 尚无长时间运行的 rotation；内存引用清除不能声明物理内存安全擦除；
- Windows named pipe 代码路径保持同协议，但本轮真实进程证据来自 darwin/arm64。

下一步应实现 `ARC-04.6a` 最小 Supervisor owner/fencing，先消费现有 heartbeat、registration、Job claim 与
physical reservation 事实，形成合法 stale takeover 决策。Supervisor 仍不得直接执行模型；只有其 fencing
闭环完成后，才进入独立 Worker `mark_running -> model/tool -> terminal publication` 纵向切片。
