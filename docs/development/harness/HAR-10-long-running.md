# HAR-10 长周期 Harness Orchestration

## 目标

支持小时/天级目标、Agent 集群、浏览器和后台工具的可靠执行：心跳、租约、背压、恢复、
人工交互和最终证据闭环统一，不与 Pursuit 形成两个竞争循环。

## 子模块

- HAR-10.1 Run lease（partial）：
  - HAR-10.1a 已实现：workspace/run-kind 隔离的 owner、单调 epoch、expires_at、幂等 acquire/renew、
    release 后 epoch 保留，以及旧 epoch 结果拒绝审计；见 [设计](HAR-10-1a-run-lease-design.md)。
  - HAR-10.1b 已实现：Pursuit 启动 admission、keepalive、动作/结果/终态 fencing、可靠释放和 live-owner
    resume 拒绝；见 [设计](HAR-10-1b-pursuit-lease-integration.md)。
  - 未完成：browser/background/agent/runtime 的逐域接入，以及跨 Store 原子提交。
- HAR-10.2 Heartbeat（partial）：
  - HAR-10.2a 已实现：Harness DB v12 typed heartbeat、instance/epoch/sequence 单调写入、确定性健康分类，
    以及 Pursuit lease worker acquire/renew/release 生产接入；见
    [设计](HAR-10-2a-typed-heartbeat.md)。
  - HAR-10.2b 已实现：heartbeat + lease + checkpoint + reconcile 的只读 Pursuit Recovery Snapshot，并接入
    Goal 新 UI、CLI/TUI fallback 和 Doctor health；见
    [设计](HAR-10-2b-pursuit-recovery-snapshot.md)。
  - HAR-10.2c 已实现：默认 New UI Python Bridge 的 runtime lifecycle producer、周期 pulse、graceful/failed
    terminal 与单次降级提醒；见 [设计](HAR-10-2c-terminal-ui-runtime-heartbeat.md)。
  - HAR-10.2d 已实现：只删除 old offline/terminal runtime 的有界、受保护、并发安全 Store authority；见
    [设计](HAR-10-2d-runtime-heartbeat-retention-authority.md)。
  - HAR-10.2e 已实现：workspace/assessment 绑定 opaque cursor、有界 typed runtime worker catalog 与覆盖索引；见
    [设计](HAR-10-2e-runtime-worker-catalog.md)。
  - HAR-10.2f1 已实现：独立 runtime RunLease、删除前续租、活跃保护、稳定状态与有界周期 retention core；
    见
    [设计](HAR-10-2f1-runtime-heartbeat-retention-periodic-core.md)。
  - HAR-10.2f2 已实现：安全默认配置、producer 成功后的 Bridge 启动、terminal 前 graceful stop、当前 subject 保护
    和合并的 typed runtime status；见
    [设计](HAR-10-2f2-runtime-retention-bridge-lifecycle.md)。
  - HAR-10.2g 已实现：真实 Agent 委派的稳定脱敏 subject、重跑 epoch、周期 pulse、完整结果终态和 Agent Control
    双端可见降级；见 [设计](HAR-10-2g-agent-execution-heartbeat.md)。
  - HAR-10.2h 已实现：浏览器 run 的稳定脱敏 subject、并发隔离、持续 waiting pulse、resume/terminal、重启
    interrupted epoch 和 Task Panel 双端可见降级；见 [设计](HAR-10-2h-browser-execution-heartbeat.md)。
  - HAR-10.2i 已实现：ARC-07.5e managed terminal identity 与 runtime heartbeat subject/instance/epoch 的原子
    startup binding，New UI/TUI 复用同一 factory，retention 同事务清理 binding；见
    [设计](HAR-10-2i-runtime-release-identity-binding.md)。
  - UI-13.1c 已实现：New UI Doctor 展示真实 retention 调度状态，TUI fallback 明确显示不可观测边界；见
    [设计](../cli-ui/UI-13-1c-runtime-heartbeat-retention-health.md)。
  - 未完成：release-bound append-only observation history/window、retention 历史详情与控制动作、heartbeat 历史统计、跨 kind 批量查询与
    Supervisor 动作。
- HAR-10.3 Durable queue（partial）：
  - HAR-10.3a 已实现 New UI `/send-now`、明确目标协议、队列稳定重排和下一安全边界回执；见
    [设计](HAR-10-3a-safe-boundary-queue-promotion.md)。
  - HAR-10.3b1 已在 Harness DB v14 建立 workspace/session 隔离的持久队列 Store、幂等 enqueue identity、
    稳定提升/终态事务、容量上限、摘要校验和跨 Store 并发顺序；见
    [设计](HAR-10-3b1-durable-conversation-queue-store.md)。
  - HAR-10.3b2 已复用 RunLease 交付 Bridge durable enqueue、claim/renew/fenced terminal、显式 Session 恢复、
    ambiguous dispatch fail-closed 和完成事件提交屏障；见
    [设计](HAR-10-3b2-durable-queue-runtime-integration.md)。
  - HAR-10.3b3 已在 Harness DB v15 交付历史 claim 审查、live-owner 保护、过期/released claim 的审计
    retry/cancel、新 request identity 与 New UI 即时刷新；见
    [设计](HAR-10-3b3-ambiguous-queue-resolution.md)。
  - HAR-10.3b4 已交付 TUI 运行中持久 enqueue、连续 claim/renew/fenced terminal、`/send-now` 和 New UI
    live-claim 重排一致性；见 [设计](HAR-10-3b4-tui-durable-queue-parity.md)。
  - HAR-10.3b5 已交付未 claim 普通消息的 request-ID cancel、New UI/TUI 回执与“已取消 · 未派发”状态；见
    [设计](HAR-10-3b5-queued-conversation-cancel.md)。
  - 未完成：跨客户端公平性、优先级、cursor、retention 与 active/外部 worker 取消传播。
- HAR-10.4 Checkpoint（partial）：
  - HAR-10.4a 已实现：严格有界 schema、单调 sequence、canonical JSON + SHA-256、篡改拒绝、Pursuit
    安全边界写入，以及 legacy missing / verified ready 恢复判定；见
    [设计](HAR-10-4a-pursuit-checkpoint-core.md)。
  - HAR-10.4b 已实现：GoalSpec/history/budget 重建、新 lease epoch continuation、累计预算门、
    `planned/action_inflight/action_result` 阶段协议，以及副作用不明确时 fail closed；见
    [设计](HAR-10-4b-pursuit-resume-executor.md)。
- HAR-10.5 Resume/reconcile（partial）：
  - HAR-10.5a 已实现：Pursuit shell/background 的稳定行动 identity、不可变哈希事件链、单调状态机、
    派发前账本、后台 task ID 关联、终态回收、并发/篡改拒绝和重复派发阻断；见
    [设计](HAR-10-5a-pursuit-action-ledger.md)。
  - HAR-10.5b 已实现：BackgroundRunner caller idempotency key、pre-spawn reservation、同 runtime 并发去重、
    重启回执复用和 Pursuit dispatched retry；见 [设计](HAR-10-5b-background-idempotency.md)。
  - HAR-10.5c 已实现：checkpoint + action ledger + BackgroundTask 的类型化核对、提交前 lease 校验、
    waiting 重建，以及 stale/orphan/identity/store error 的 fail-closed blocker；见
    [设计](HAR-10-5c-background-reconcile.md)。
  - 未完成：同步 shell、browser、agent/API 外部状态核对，跨 Store 原子性和孤儿进程接管。
- HAR-10.6 Human interaction（partial）：
  - ARC-03.2b1 已完成实时 interaction 三事件的双端严格 payload 边界；
  - HAR-10.6a 已实现 Harness DB v13 durable interaction authority、append-only hash chain、timeout、owner
    lease/epoch takeover 和并发 answer fencing；见
    [设计](HAR-10-6a-durable-interaction-authority.md)；
  - HAR-10.6b 已实现 New UI Bridge create-before-display、answer-before-release、租约到期重放，以及 Pursuit
    stable checkpoint ref 和 answered/expired resume reconcile；见
    [设计](HAR-10-6b-interaction-runtime-integration.md)；
  - UI-18.4b 已让 Textual TUI 复用相同 durable adapter、实时 timeout 与启动 takeover/replay；
  - UI-18.4c 已交付 Goal interaction ledger、sequence-fenced 显式 cancel 和 New UI/CLI/TUI 动作闭环；
  - UI-18.4d1 已交付 Goal-linked interaction 的共享只读详情，展示选项、答案、fencing 和
    takeover 资格，且不暴露 owner ID；
  - UI-18.4d2 已交付 exact manual takeover，成功后必须绑定当前 Bridge/TUI 宿主展示、续租和
    回答提交，并拒绝 live owner、deadline 超时与并发重复卡片；
  - UI-18.4d3 已交付 workspace/subject/filter-bound opaque cursor、New UI 页内选择/详情/前后翻页，
    以及 TUI 共享 Tool 的状态筛选和命令式后续页；
  - HAR-10.6c 已交付 schema 2 不可变四级优先级、Store v24 的 v1 哈希兼容迁移、4:2:1:1
    公平轮转，以及 New UI/TUI/Goal 同源投影；
  - 未完成：pending recovery cursor、跨 Goal 搜索与跨 Store 原子提交。
- HAR-10.7 Cluster scheduling（partial）：
  - HAR-10.7a 已让所有公开 Agent `delegate()` 与批量/DAG 入口共用 `max_parallel_agents` semaphore，
    直接委派等待也进入 Runtime queue 计数；取消会清理计数，饱和嵌套委派 fail closed 而不自锁。见
    [设计](HAR-10-7a-universal-agent-admission.md)。
  - HAR-10.7b 已增加 `max_queued_agents` 进程内硬上限，direct/batch/DAG 共用活跃与等待预算；满载时返回
    明确中文过载回执，Runtime 同时展示排队上限，取消可复用容量。见
    [设计](HAR-10-7b-bounded-agent-waiting-queue.md)。
  - HAR-10.7c 已让多个 embedded Runtime 共用 AgentJob v2 引入的 capacity policy、active token 与
    有界 FIFO；`waiting_capacity` 可见、可停止，terminal 后下一项自动取得 claim，New UI/TUI 与
    `/runtime` 显示同一共享计数。ARC-04.5d1 升级到 schema v3 后继续保留该 authority。见
    [设计](HAR-10-7c-durable-agent-capacity-admission.md)。
  - HAR-10.7d 已交付 authenticated、每类有界、内容隔离的 Agent recovery catalog，并通过
    Agent Control schema v4 同步 New UI/Textual TUI“恢复”标签；claimed/running/unknown、pending/
    expired publication 被明确分类，但不自动重放或改写状态。见
    [设计](HAR-10-7d-agent-recovery-catalog.md)。
  - HAR-10.7e 已增加当前会话 expired running Job 的 exact fenced 人工裁决：request/session/receipt/
    claim epoch/expiry 在同一事务复验后才允许收口为 `unknown`；New UI 与 Textual TUI 共用 `u` 动作，
    bypass 不增加二次确认但也不能绕过事实 fencing。见
    [设计](HAR-10-7e-exact-agent-recovery-action.md)。
  - HAR-10.7f 已把 Agent terminal publication 从 startup-only 恢复升级为默认启用的周期 worker：共享
    既有加密 outbox/claim epoch/result inbox，提供串行 pass、有界空闲/失败退避、live failure wake
    和 Engine shutdown drain。最大重试预算、poison-record quarantine/dead-letter 仍未完成。见
    [设计](HAR-10-7f-periodic-agent-publication-recovery.md)。
  - HAR-10.7g 已增加 Store schema v6 的 HMAC 认证 quarantine receipt、durable retry budget 和 FIFO
    poison-record 隔离；Agent Control schema v5 将隔离计数/事实同步到 New UI/TUI。exact requeue、
    放弃和 prune 尚未完成。见 [设计](HAR-10-7g-publication-quarantine-dead-letter.md)。
  - HAR-10.7h1 / ARC-04.5e1 已建立真实 spawned Agent 控制进程、一次性 authkey/nonce 本机认证、
    注册前 PID/合同握手、child-driven heartbeat、drain/stop/revoke 与隐式 takeover 阻断；合同只声明
    `agent_control_transport` 且 `accepting_jobs=false`，Doctor/New UI/TUI 明确显示“任务调度未开放”。
    HAR-10.7h2 / ARC-04.5e2 已进一步完成 admitted-only AgentJob owner lease、物理 slot reservation、
    一次性进程密钥加密 staging、双续租和 pre-start release；HAR-10.7h3 / ARC-04.6a 又增加 exact
    PID/create-time witness、Supervisor owner lease 与认证 pre-start fencing/requeue。模型执行与完整
    Supervisor 运维仍未完成；HAR-10.7h4 / ARC-04.5e3a 进一步完成 model-only 两阶段 start、真实
    Provider 调用、加密 terminal 和 AgentJob/outbox 原子提交；HAR-10.7h5 / ARC-04.5e3b1 又完成 exact
    manifest、加密 Tool RPC、多轮模型循环和重复副作用阻断；HAR-10.7h6 / ARC-04.5e3b2 已继续接通
    生产 SubAgent 路由、`Engine.execute_tool()` 权限链和 Agent Control schema v6 双端后端状态。见
    [Bootstrap](../architecture/ARC-04-5e1-independent-agent-worker-bootstrap.md) 与
    [Owner lease](../architecture/ARC-04-5e2-independent-agent-worker-owner-lease.md)、
    [Supervisor fencing](../architecture/ARC-04-6a-agent-worker-supervisor-owner-fencing.md)、
    [Model execution](../architecture/ARC-04-5e3a-independent-agent-model-execution.md) 与
    [Tool RPC](../architecture/ARC-04-5e3b1-encrypted-agent-tool-rpc.md) 与
    [Production routing](../architecture/ARC-04-5e3b2-production-agent-worker-routing.md)。
  - ARC-04.5a 已让每次真实 Agent 委派在模型调用前绑定 task/context 摘要、精确工具/权限/模型/轮数/
    预算/超时，并在终态产生低敏 result receipt；New UI/TUI Agent Control 显示同一合同证据。见
    [设计](../architecture/ARC-04-5a-agent-worker-contract.md)。合同当前仍为进程内事实，不代表持久 Worker。
  - ARC-04.5b1 已提供显式 provision 的系统 Runtime payload key 与 bounded AES-256-GCM envelope；
    ARC-04.5b2 已用 request digest AAD 建立 durable Agent Job Store、pre-start takeover 与 running
    recovery fence；ARC-04.5c 已让 embedded Agent 生产路径执行 admit/claim/recover/run/renew/finish，
    并将未认证终态隔离在用户/message bus 发布之前。见
    [Envelope](../architecture/ARC-04-5b1-runtime-payload-envelope.md) 与
    [Embedded dispatch](../architecture/ARC-04-5c-embedded-agent-durable-dispatch.md)。
  - ARC-06.1a 已交付 worker incarnation/contract capacity 的原子 reservation authority，解决并发调度者
    基于同一健康快照超卖最后槽位的问题；见
    [设计](../architecture/ARC-06-1a-worker-capacity-reservations.md)。
  - ARC-06.1b 已让生产 ToolJob dispatch/terminal lifecycle 消费该 authority，验证容量耗尽、幂等重试、
    unknown recovery 与终态归还；见
    [设计](../architecture/ARC-06-1b-tool-job-capacity-lifecycle.md)。
  - ARC-06.2a 已建立持久、有界、incarnation-fenced 的 Worker capacity FIFO 队列，并让 claim 与
    reservation 同事务；见
    [设计](../architecture/ARC-06-2a-durable-worker-capacity-queue.md)。
  - ARC-06.2b1 已让生产 ToolJob 在容量饱和时写入 tamper-evident `queued` receipt 并绑定持久 waiter，
    同时阻断 direct dispatch、支持重启补建和 dispatch 前取消；见
    [设计](../architecture/ARC-06-2b1-tool-job-capacity-queue-admission.md)。
  - ARC-06.2b2 已让 claimed ToolJob 进入真实 Shell dispatch/start/terminal 链，并在 start 前复验
    reservation、对 lost claim 做 no-side-effect reconcile；见
    [设计](../architecture/ARC-06-2b2-claimed-tool-job-dispatch-reconcile.md)。当前 Agent Worker 已能跨 Job
    执行模型请求，且 ARC-04.5e3b2 已接入生产 SubAgent 调度；pre-start claim owner lease 已由 ARC-04.5e2 接入，ARC-04.6a 已补齐最小 crash-before-start
    fencing，ARC-04.5e3a 已补齐 `mark_running` 与 terminal publication，ARC-04.5e3b1 已补齐加密 Tool
    RPC 与多轮执行内核，ARC-04.5e3b2 已补 exact Engine authority adapter。长驻池、跨 workspace/provider
    公平 scheduler 和完整 Supervisor 尚未完成，因此仍只是 HAR-10.7 集群调度前置。
  - UI-13.1d 已把每个 active Worker 的 reservation 占用/可用槽位投影到 New UI 与 TUI Doctor，且严格
    只读、不把 reservation 冒充实际进程负载；见
    [设计](../cli-ui/UI-13-1d-worker-capacity-health.md)。
  - UI-13.1e 已继续投影 durable queue policy、live waiting、active claim、oldest wait 与到期待收口数；
    New UI/TUI 复用同一只读 authority，不暴露 job identity；见
    [设计](../cli-ui/UI-13-1e-worker-queue-backlog-health.md)。
  - 未完成：Agent/Browser 独立持久 Worker dispatch、自动 Job recovery scheduler、pre-start 自动 takeover、
    可恢复 response、
    workspace 锁、能力路由、
    亲和/反亲和、公平队列和隔离。
- HAR-10.8 Terminal decision：完成、waiting、blocked、cancelled、budget_exceeded。
  - HAR-10.8a 已实现：assessment 去除隐式全量探针，criterion 与模型 action 共用定向验证策略，广域
    pytest/ruff/tox/nox 及主流语言测试入口 fail closed；见
    [设计](HAR-10-8a-scoped-verification-policy.md)。
  - HAR-10.8b 已实现：Pursuit 主循环使用内容寻址的机械边界裁判统一完成、等待、阻塞、取消和三类预算
    越界；完整决策持久化并由 Goal New UI 与 TUI/CLI fallback 展示。没有持久任务引用的 waiting
    失败关闭；见 [设计](HAR-10-8b-mechanical-terminal-decision.md)。
  - HAR-10.8c 已实现：resume/reconcile/checkpoint-error 生产分支使用 schema 2 恢复事实与同一裁判；
    durable pending interaction 正确进入 waiting，冲突 authority 失败关闭，同时保持 schema 1
    哈希、Store 重存和 New UI 读取兼容；见
    [设计](HAR-10-8c-recovery-boundary-decisions.md)。
  - HAR-10.8d 已实现：持久权限回执绑定恢复请求身份、内容寻址 attempt ID、requested/admitted/terminal
    哈希事件链、并发幂等、机械裁判引用复验和最近恢复记录投影；见
    [设计](HAR-10-8d-recovery-attempt-ledger.md)。
  - HAR-10.8e 已实现：显式 attempt terminal reconciliation 先推进 Harness RunLease epoch，再在
    PursuitStore 单事务内复验后置 checkpoint/机械裁判/run 状态并写不可变回执；见
    [设计](HAR-10-8e-recovery-attempt-reconciliation.md)。
  - HAR-10.8f1 已实现：终态 checkpoint 与 pending outbox 同事务，普通 resolution/显式 reconcile
    与 delivered 同事务，并提供 oldest-first 有界认证恢复目录；详见
    `HAR-10-8f1-pursuit-terminal-outbox-core.md`。
  - HAR-10.8f2a 已实现：durable dispatch claim/expiry/takeover/backoff、默认启动与周期 bounded worker、
    Engine shutdown drain；自动恢复仍复用 10.8e Harness fencing。详见
    `HAR-10-8f2a-pursuit-terminal-outbox-worker.md`。
  - HAR-10.8f2b 已实现：有界 typed backlog/worker snapshot 进入 Bridge `goals/snapshot`、New UI 和
    Goal Tool/Textual TUI fallback；详见 `HAR-10-8f2b-pursuit-terminal-outbox-ui.md`。
  - HAR-10.8f2c 已实现：显式 due-only 恢复通过 Agent Tool/权限链进入 New UI `o` 键与
    `/pursue outbox run-now`，并持久化 identity-free 不可变 pass 回执；详见
    `HAR-10-8f2c-pursuit-terminal-outbox-run-now.md`。
  - 未完成：恢复控制动作、push stream、dead-letter/retention、跨 Store 原子 terminal commit 与 A5
    故障/soak。

## 与 Pursuit 的合并原则

Pursuit 提供目标分解/评估/行动；HAR-10 提供可靠运行控制和完成裁判。Pursuit 不直接持有
worker lease，Harness 不重新实现 goal planner。Goal、Pursuit、Harness Run 通过稳定 ID
关联。

ARC-03.5a 已提供长运行 New UI 的最小传输完整性前置：协商后的 JSONL 缺口不会静默渲染为成功状态，
而会隔离并回退到直接读取 Engine 的 TUI。它只保护当前连接的可见真相，不替代 HAR-10 的持久 lease、
checkpoint、reconcile，也不提供断线事件补发。

UI-15.3a 已补齐长运行可见层的最小性能前置：深历史位置收到 assistant/thinking/tool 高频更新时，
New UI 使用有界 semantic mutation journal 增量更新单卡，而不重复渲染全部历史。它只保证前端渲染有界，
不替代本模块的 durable queue、lease、checkpoint 或事件 retention。

## 已完成前置

ARC-01.4b2e 已把 GoalStore/PursuitStore 的规范路径、lazy initialization 和运行时资源所有权收口，
并验证 Goal 的 `pursuit_run_id` 在 Store 重开后仍能恢复对应 PursuitRun。下一步 UI 可以读取类型化权威
状态，不需要解析 `/goal` 或 `/pursue` 文本。

ARC-01.4b2e 只解决资源归属和稳定引用。HAR-10.1a 已在 Harness schema v11 交付通用 lease/epoch 与结果
fencing authority，HAR-10.1b 已完成 Pursuit 首个生产接入，HAR-10.4a/4b 已交付权威 checkpoint 与安全
continuation，HAR-10.5a/5b 已让 shell/background 行动在外部派发前进入持久账本并用 caller key 复用后台
task，HAR-10.5c 已让证据充分的 background action 自动恢复为 waiting/terminal/continue，HAR-10.2a/2b 已
交付 Pursuit worker heartbeat 与用户可见的 recovery 聚合，HAR-10.6a/6b 已交付 durable interaction
authority 与 New UI/Pursuit 接入，UI-18.4b 已补齐 TUI durable runtime parity，HAR-10.3a 已交付不打断
当前事务的 New UI 队列提升，HAR-10.3b1/3b2/3b3 已交付 durable queue Store、New UI Runtime 接入与
历史 claim 人工处置；HAR-10.2f1/2 已交付 runtime retention 周期核心与默认 Bridge 生命周期，HAR-10.2g 已交付
Agent 委派 heartbeat producer，HAR-10.2h 已交付 browser producer；同步 shell/runtime 逐域接入、browser lease/reconcile、Goal interaction actions、跨进程/跨 Store 原子性、heartbeat 多域
接入仍属于后续实现；TUI queue parity 已由 HAR-10.3b4 完成。HAR-10.8d 已让每次生产恢复动作先进入
可去重的审计账本，HAR-10.8e 已让中断后的 admitted attempt 通过更高 lease epoch 和机械终态证据显式
收口，UI-18.5b1 已让 New UI/TUI fallback 消费该 authority 并完成受控 resume 动作闭环；
自动 terminal outbox worker 已由 10.8f2a 接入，typed backlog/worker health 已由 10.8f2b 同步到
New UI/TUI；历史 cursor/retention 与 takeover/cleanup 用户动作仍待独立治理。

## 验收标准

- 杀死 runtime 后在新进程恢复，不重复已经完成的 destructive action。
- 三个 worker 同抢一租约，只有一个 owner；旧 epoch 的结果被拒绝并审计。
- 1k 工具任务有容量和公平性，无界队列不可出现；取消能传播到 agent/browser/process。
- 等待用户时不消耗模型轮次；立即发送消息能进入下一安全边界而非破坏当前事务。
- 网络/Store/worker 短暂失败后退避恢复，持续失败进入 blocked 并给下一步。
- A5：24 小时 soak、故障注入、重启、背压、心跳丢失和人工接管报告。
