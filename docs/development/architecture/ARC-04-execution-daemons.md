# ARC-04 Tool、Browser、Agent 执行 Daemon

## 目标

将高风险、长寿命或资源密集执行隔离为 daemon worker；Runtime 负责计划和权限，daemon 负责
受限执行、心跳、日志引用和取消。

## 子模块

- ARC-04.1 Worker contract：capabilities、platform、resource、version、health。
- ARC-04.2 Tool job：immutable request、permission grant id、workspace lease、idempotency key。
- ARC-04.3 Shell worker：PTY/非 PTY、cwd/env allowlist、process tree cancel、artifact log。
- ARC-04.4 Browser worker：profile isolation、tab/run ownership、human takeover、cleanup。
- ARC-04.5 Agent worker：context bundle、tool scope、budget、message channel、terminal result。
- ARC-04.6 Supervisor：heartbeat、crash loop、quarantine、drain、upgrade。
- ARC-04.7 Audit adapter：规范化事件回 Runtime/Harness，不回传 secret/raw 大输出。

## 当前实现状态

- ARC-04.1a 已完成 Worker 身份/版本、跨平台事实、能力与隔离声明、资源容量、健康绑定和 fail-closed
  admission 合同，详见 `ARC-04-1a-worker-contract.md`。
- ARC-04.1b 已完成 Runtime-owned SQLite registration authority、最高 epoch incarnation fencing、撤销、
  authority-only admission 与 Store Catalog/Composition Root 装配，详见
  `ARC-04-1b-worker-registration-authority.md`。
- ARC-04.2a 已完成 execution-scoped grant authority：绑定参数 digest、幂等键、Tool run lease、active Worker
  epoch、权限来源与短期 expiry，并可在消费前重新 fencing，详见
  `ARC-04-2a-scoped-execution-grant-authority.md`。
- ARC-04.2b 已完成 immutable ToolJob admission：同时消费 execution grant、active Worker、实时
  heartbeat/capacity、能力/隔离要求与 Tool lease，并在 dispatch 前重新 fencing，详见
  `ARC-04-2b-immutable-tool-job-admission.md`。
- ARC-04.2c 已完成 ToolJob schema v2 单调 lifecycle receipt、dispatch-before-send、Worker incarnation fencing、
  并发终态幂等、unknown 副作用边界与 v1 migration，详见 `ARC-04-2c-tool-job-lifecycle-receipts.md`。
- ARC-06.1b 已把 ARC-06.1a capacity authority 接入上述真实 ToolJob dispatch/terminal lifecycle；ToolJob
  transport 前占位，终态释放，跨 Store 失败由 TTL fail-safe 收敛，详见
  `ARC-06-1b-tool-job-capacity-lifecycle.md`。
- ARC-06.2b1 已把 ToolJob authority 升级到 schema v3：容量饱和后用 tamper-evident `queued` receipt
  绑定 ARC-06.2a waiter，阻断 direct dispatch，并闭合重启补建与 dispatch 前取消；claimed waiter
  的发送由下一切片接管，详见 `ARC-06-2b1-tool-job-capacity-queue-admission.md`。
- ARC-06.2b2 已让 claimed ToolJob 进入现有 Shell dispatch-before-send：active scheduler reservation
  绑定唯一 dispatch receipt，在真实 `start` 前复验，终态统一释放，lost claim 以 no-side-effect
  cancelled 收口；自动 scheduler 与 claim owner lease 尚未实现，详见
  `ARC-06-2b2-claimed-tool-job-dispatch-reconcile.md`。
- ARC-04.3a 已完成认证本地 non-PTY transport、默认断网 OS sandbox、process-tree cancel、资源上限、artifact
  digest，并由 Coordinator 消费 ARC-04.2b/2c 权威链，详见
  `ARC-04-3a-authenticated-non-pty-shell-worker.md`。
- ARC-04.3b 已完成一次性 Shell admission composer，组合精确子授权、Worker incarnation、snapshot Tool lease、
  delegated grant、ToolJob admission 与终态 cleanup，详见 `ARC-04-3b-ephemeral-shell-admission-composer.md`。
- UI-12.3b3 已补充独立的有界 Run Delegation authority：长运行不再依赖放宽父回执 freshness，而是绑定
  父 digest、下游 scope 和 Harness Run Lease fence 并持续复验；ARC-04 下一切片需把该 authority 接入
  短期子回执与 Shell admission，不能让消费者自行续签或拼接授权链。
- UI-12.3b4 已让 schema v4 child receipt 与 ExecutionGrant 在签发和 dispatch 阶段消费 Run Grant，并将
  ExecutionGrant expiry 截断到 child expiry；
- ARC-04.3c 已将共享 Run Grant Store/Authority 接入 Composition Root 与 Shell admission composer，并用真实
  sandbox 命令证明成功路径、用撤销证明 payload 前阻断；详见 `ARC-04-3c-run-delegated-shell-admission.md`。
- ARC-04.5a 已让现有 Agent 委派在模型调用前签发 tamper-evident request contract，绑定 task/context
  摘要、精确工具/权限/模型/轮数/预算/超时，并在终态签发不含原文的 result receipt；无效合同在模型前
  fail closed，New UI/TUI Agent Control 显示同一工具范围与请求/结果摘要。详见
  `ARC-04-5a-agent-worker-contract.md`。
- ARC-04.5b1 已建立显式 provision、OS credential-backed 的 256-bit Runtime payload key，以及 bounded
  AES-256-GCM envelope；wrong key/AAD/tamper fail closed，禁止明文和项目内 key fallback。它是 durable
  Agent Job Store 的安全前置，详见 `ARC-04-5b1-runtime-payload-envelope.md`。
- ARC-04.5b1a 已提供 `naumi runtime-key status|init` 显式管理入口，missing/ready、首次创建与幂等
  replay 均有不泄密文案；普通启动仍不创建 key。
- ARC-04.5b2 已建立 schema v1 加密 Agent Job Store、FIFO claim、epoch/lease fencing、pre-start
  takeover、running recovery unknown 和 append-only receipt chain，并进入 Runtime Resources/Store
  Catalog，详见 `ARC-04-5b2-durable-agent-job-authority.md`。
- ARC-04.5c 已让生产 `SubAgentManager` 消费 admit/claim/recover/running/renew/finish 全链路；
  缺 key、start fence、续租和 terminal commit 失败均 fail closed，未认证模型结果不会发布，
  New UI/TUI Agent Control 显示相同 job state/epoch/降级码。详见
  `ARC-04-5c-embedded-agent-durable-dispatch.md`。
- ARC-06.2c 已在相同 AgentJob authority 上增加跨 Runtime embedded active 上限、有界 FIFO、
  `waiting_capacity` 停止和 recovery-blocking 计数；它不把 embedded Runtime 冒充独立 Worker。
  详见 `ARC-06-2c-durable-embedded-agent-capacity.md`。
- ARC-04.5d1 已把 response/error 作为与 result digest 强绑定的 AEAD terminal payload，在同一
  terminal transaction 原子提交；生产 manager 只发布从 Store 重新认证恢复的内容，恢复失败继续隔离。
  详见 `ARC-04-5d1-durable-agent-terminal-payload.md`。
- ARC-04.5d2a 已将 AgentJob authority 升级到 schema v4：每个带 result 的 terminal commit 在同一
  事务原子创建 `pending` publication，提供 FIFO claim、lease/epoch fencing、renew/release/ack、
  HMAC receipt chain、重启恢复目录与 backlog 聚合。ARC-04.5d2b 又升级到 schema v5，用同一事务
  原子写入幂等 result inbox 与 published receipt，接入生产 manager 在线消费及有界 startup recovery；
  HAR-10.7g 又升级到 schema v6，增加认证 quarantine authority；
  Bus 只作通知，不声称外部 exactly-once。详见
  `ARC-04-5d2a-agent-result-publication-outbox.md` 与
  `ARC-04-5d2b-agent-result-publication-consumption.md`。
- ARC-04.5d2c 已让 `SubAgentManager` 通过精确 delivery fence 认证读取当前 session 的 result inbox，
  并以 Agent Control schema v3 的有界脱敏 projection 同步 New UI/TUI“结果”标签；两端不读取
  SQLite 或解密原文。详见 `ARC-04-5d2c-agent-result-inbox-projection.md`。
- HAR-10.7d/7e 已让 Agent Control 读取认证恢复目录，并以 request/session/receipt/claim epoch/expiry
  exact fence 将当前会话的 expired running Job 人工收口为 `unknown`；该动作不重放模型，也不是
  ARC-04.6 Supervisor。详见 `../harness/HAR-10-7e-exact-agent-recovery-action.md`。
- ARC-04.5e1 / HAR-10.7h1 已建立真实 spawned Agent 控制进程、一次性 authkey/nonce 本机认证、
  registration-before-running、child-driven heartbeat、drain/stop/revoke 和隐式 takeover 阻断。合同只
  声明 `agent_control_transport`，不声明 `agent_context_scope`，并固定 `accepting_jobs=false`；Doctor
  明确显示“控制通道就绪、任务调度未开放”。详见
  `ARC-04-5e1-independent-agent-worker-bootstrap.md`。
- ARC-04.5e2 / HAR-10.7h2 已将 exact Worker incarnation、物理 slot reservation 与 admitted-only
  AgentJob owner lease 绑定，以独立一次性密钥完成 AES-GCM request/context staging，并对 claim/slot
  双续租；密文篡改、子进程崩溃和 release 均按 control-only pre-start 语义 fail closed。它仍不声明
  `agent_context_scope`、不 `mark_running`、不调用模型。详见
  `ARC-04-5e2-independent-agent-worker-owner-lease.md`。
- ARC-04.6a / HAR-10.7h3 已增加 durable PID/create-time witness、Supervisor owner lease、heartbeat + OS
  process + expired claim/slot 联合裁决，以及 HMAC fencing receipt 驱动的幂等 pre-start requeue；running
  副作用未知时 fail closed。Doctor/New UI/TUI 消费同一低敏聚合。详见
  `ARC-04-6a-agent-worker-supervisor-owner-fencing.md`。
- ARC-04.5e3a / HAR-10.7h4 已让具备空 tool scope 的独立 Worker 通过两阶段 start fence 执行真实 Provider
  调用，并将 AES-GCM terminal 在同一 AgentJob 事务提交到终态与 publication outbox；Provider 异常脱敏，
  running 后进程丢失保持 recovery-required。详见 `ARC-04-5e3a-independent-agent-model-execution.md`。
- ARC-04.5e3b1 / HAR-10.7h5 已补齐 exact Tool manifest、加密 call/result RPC、多轮模型循环、跨轮重复
  副作用阻断与父 Runtime authority callback；越权调用不触达执行器，执行中断保持 running unknown。详见
  `ARC-04-5e3b1-encrypted-agent-tool-rpc.md`。
- ARC-04.5e3b2 / HAR-10.7h6 已把生产 `SubAgentManager` 默认路由到独立 Worker，并让 ToolCall 复用
  `Engine.execute_tool()` 权限事件链；Agent Control schema v6 在 New UI/TUI 显示独立/内嵌后端。
  详见 `ARC-04-5e3b2-production-agent-worker-routing.md`。
- 当前 Tool Worker 与生产 Agent Worker 都仍是每 Job 一个短寿命进程。PTY、长驻池、完整 Supervisor、
  全局公平调度、并发背压与 Windows 隔离后端仍未完成；未注入 model-capable factory 时保留显式 embedded
  fallback。结果 outbox 已有在线消费、
  startup recovery 和只读 UI projection；HAR-10.7f 已增加周期 publication recovery、有界退避、失败
  唤醒与 shutdown drain；HAR-10.7g 已补齐 durable retry budget、HMAC quarantine receipt 和双端隔离
  投影。exact requeue/prune、UI read/ack、retention 和外部 sink 仍未完成。
  因此 ARC-04 保持 partial；下一切片需重新比较长驻池/公平调度、流式事件、并行 Tool RPC 与 Harness
  可恢复性需求，不能仅按 ARC 编号线性扩张。

## 验收标准

- 无有效 permission grant 的 job 被 daemon 拒绝；bypass grant 仍有 scope/run/expiry。
- 同 idempotency key 重试不重复 destructive action。
- 取消 shell 清理整个进程树；取消 browser 清理 owned tabs/profile；不影响其他 job。
- worker crash 后 Runtime 判断已执行/未执行/未知，不盲重试未知副作用。
- 100 并发 job 资源上限生效，日志进入 artifact 而非内存堆积。
- daemon 版本不兼容时 drain 并提示升级，不接受新任务。

## 下游硬依赖

- HAR-08.4 Sandbox Eval 只能消费 ARC-04 提供的显式隔离能力合同：临时 workspace/worktree、
  默认断网、环境变量 allowlist、资源上限、进程树取消、artifact digest 与可审计退出状态。
- 现有 `ValidationExecutor` 仍不能证明 Sandbox Eval 的 `no_host_side_effect`。HAR-08.4a/4b 已使生产
  `/harness check` 和 Agent Tool 消费 ARC-04.3a/3b 的真实隔离与委托链；HAR-08.4e 又提供共享的成组
  Check execution kernel，HAR-08.4f 负责连续 Batch lease/grant/恢复/清理。通用 surface 仍不得直接复用旧
  subprocess 路径冒充 Sandbox。

## 已完成的最小前置

HAR-10.5b 已在本地 BackgroundRunner 接通 caller idempotency key、pre-spawn reservation、同 runtime 并发
去重和重启回执复用。这一早期前置只验证了 identity 形状；ARC-04.2a-2c 现已补齐 grant、lease、immutable
admission 与 lifecycle receipt，但 `tasks.json` 仍不是 daemon authority，禁止把 BackgroundRunner 标记为
Tool daemon。

HAR-10.5c 又验证了本地 reconcile contract：只有当前 Runner 同时持有活进程与 watcher 才能报告 managed
active；PID 存在但所有权丢失必须报告 orphan 并 fail closed。该合同可作为 ARC-04.6 Supervisor 的输入形状，
但尚不具备 daemon 接管、心跳、跨进程 fencing 或孤儿清理能力。

HAR-10.2a 已提供可复用的 heartbeat snapshot（instance/epoch/sequence/phase/timeout）与机械健康分类，并接入
Pursuit lease worker。这是 ARC-04.1/04.6 的最小协议前置，但还没有 daemon producer、历史丢包统计、
crash-loop/quarantine/drain 或 supervisor 动作；在 ARC-04.1a 交付前，ARC-04 因此保持 planned。

ARC-04.1a 在该 heartbeat 之上增加了能力、平台、资源、隔离和容量合同，并验证 worker/instance/epoch 与
heartbeat generation 一致。它没有复制 liveness 状态机，也没有放宽上述 daemon producer 与 supervisor 缺口；
ARC-04 当前状态为 partial (4.1a, 4.1b, 4.2a, 4.2b, 4.2c, 4.3a, 4.3b, 4.3c, 4.5a, 4.5b1,
4.5b1a, 4.5b2, 4.5c, 4.5d1, 4.5d2a, 4.5d2b, 4.5d2c, 4.5e1, 4.5e2, 4.5e3a,
4.6a)。
