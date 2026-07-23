# Harness 后续开发模块册

## 当前基线

H1-H3、H4.1-H4.4、HAR-05，以及 HAR-08 的离线协议 Eval、安全 Replay Eval、
Identity/Comparator、Result Store、Baseline/Selector、Comparison receipt、HAR-10.1a 持久化 fencing lease、
HAR-10.1b Pursuit 首个生产接入、HAR-10.4a 权威 checkpoint 核心、HAR-10.4b 安全 resume executor，以及
HAR-10.5a shell/background 持久行动账本、HAR-10.5b Background caller idempotency 和 HAR-10.5c 类型化
background reconcile 已实现。resume 支持新 lease epoch continuation，在证据充分时恢复 waiting/terminal，
并在 in-flight 副作用不明确时保持 `reconcile_required`。
行动账本在外部派发前记录稳定 identity，关联后台 task ID，并阻止 terminal/ambiguous 行动被重复派发；完整
后台任务可通过 caller key 在同 runtime 并发与正常重启后复用；stale/orphan/identity/store error 均有
明确 blocker，不会被盲目重试。
HAR-10.8a 已把 Pursuit 每轮验证限制为目标文件/测试节点，移除 assessment 的隐式全量测试和 lint。
HAR-10.2a 在 Harness DB v12 建立 typed heartbeat，并接入 Pursuit lease worker 的 acquire/renew/release；
HAR-10.2b 已把 heartbeat/lease/checkpoint/reconcile 聚合到 Goal 新 UI、CLI/TUI fallback 与 Doctor health；
HAR-10.2c-10.2h 已进一步交付默认 New UI runtime producer、安全 retention authority、typed worker catalog、
独立租约协调的周期 retention core、Bridge 生命周期、真实 Agent 委派 heartbeat 和 browser execution heartbeat。跨 kind
catalog、专用 Doctor 详情和 Supervisor
仍未完成。
HAR-10.6a 在 Harness Store v13 提供 durable interaction request/answer、timeout、takeover 与
并发 fencing；HAR-10.6b 已接入 New UI Bridge 和 Pursuit stable checkpoint/reconcile，UI-18.4b 已让
Textual TUI 复用相同 authority adapter；UI-18.4c 已补齐 Goal interaction ledger 与显式 cancel。手动
takeover 已由 UI-18.4d2 以宿主绑定方式补齐；cursor 和页内详情筛选仍未完成。
HAR-10.3a 已为 New UI 增加 `/send-now` 与安全边界队列提升；HAR-10.3b1 已把 Harness Store 升级到 v14，
交付持久队列 Store 核心；HAR-10.3b2 已进一步接入 Bridge durable enqueue、RunLease claim/renew、fenced terminal
和显式 Session 恢复；HAR-10.3b3 已升级 Harness Store v15，交付 `/queue` 历史 claim 审查、审计
retry/cancel 和 New UI 即时恢复。HAR-10.2c 已把默认 New UI Bridge 接入 typed runtime heartbeat lifecycle，
HAR-10.2d 已建立 old offline/terminal runtime 的有界 retention authority；
HAR-10.2e 已建立可跨 Store 翻页的 typed runtime worker catalog；
HAR-10.2f1/2 已建立删除前精确续租、活跃保护和稳定状态的周期 retention core，并以 7 天安全默认值接入 Bridge；
HAR-10.2g 已让每次 Agent 委派产生 durable heartbeat，并在 New UI/TUI Agent Control 显示阶段和降级码；
HAR-10.2h 已让每次真实 browser run 产生 durable heartbeat，在等待指令/人工接管时保持 waiting pulse，并由 Task Panel
双端展示 phase、epoch 与降级码；
HAR-10.3b4 已让 TUI 运行中输入复用相同持久队列、claim/renew/terminal 与
`/send-now`，并修复两端在本 owner live claim 期间无法重排后缀的问题。HAR-10.3b5 又补齐未 claim 普通消息的
精确取消和双端回执。跨客户端公平与 active worker 取消传播仍未完成。
HAR-10.7a 已封住直接 Agent 委派绕过 `max_parallel_agents` 的入口，让 direct/batch/DAG 共用进程内
admission、排队计数与取消清理，并拒绝容量饱和时会自锁的嵌套委派；HAR-10.7b 又以
`max_queued_agents` 封住本地等待协程的无界增长，并提供稳定过载回执。持久 Agent Worker、跨进程队列与
公平调度仍属于 ARC-04/06 后续。ARC-04.5a 已先让现有委派在模型调用前签发请求合同、终态签发低敏结果
回执，并由 New UI/TUI 显示精确工具范围与摘要；ARC-04.5b2/5c 已进一步接入加密 durable Agent Job、
claim renewal、终态发布屏障和双端 job state/epoch 证据。独立 Worker、capacity fairness、自动恢复与
可展示 response 恢复仍未完成。
HAR-08.4e/4f 已把成组 Sandbox checks 与可恢复 Batch 状态机下沉到 Harness；HAR-08.4g 又让 Engine 内
RED/GREEN/adversarial 生产 consumer 共用 `max_parallel_sandbox_batches` / `max_queued_sandbox_batches`
容量门；HAR-08.4h 已增加原生 `sandbox` lane，以及受信 Profile + 干净 Git revision 的不可变 request
authority；HAR-08.4i 又把 request、admission、coordinator、kernel 与 H5a 组合为共享
`HarnessService.eval_sandbox()`；HAR-08.4j 已开放 `harness_eval_sandbox` Tool 与共享 Slash 命令。
HAR-08.4k 已把 coordinator checkpoint 作为闭集 Runtime event 同步到 New UI/TUI，且只显示
Store-confirmed 进度。HAR-08.4l 又把进程内 Sandbox Batch gate 升级为 Harness Store v17 的
workspace-wide durable authority：跨进程 FIFO、queued/active lease、崩溃回收、策略冲突和
owner/epoch fencing 均由 SQLite 原子状态转换负责。生产 Harness Sandbox 与 Evolution
RED/GREEN/adversarial lane 共享同一容量权威。HAR-08.4m 已把 Store-confirmed queued position、
admitted、released completed 与异常终态同步到 New UI/TUI。HAR-08.4n 已增加 owner-fenced
cancel：New UI、TUI 共享 Store v18 原子裁决，accepted/rejected 均生成 durable receipt，
跨进程 active owner 在有界 poll 内停止。HAR-08.4o1 又以 Store v19 持久化不可变
Sandbox Eval Request Manifest：新进程可按服务端 request authority 恢复原请求，同 batch 漂移、
并发覆盖与持久内容篡改均失败关闭。HAR-08.4o2 已以 Store v20 一次性消费 accepted cancel receipt，
生成可恢复的新 retry action/receipt 与 execution authority，并支持 retry chain 回溯原 request；
HAR-08.4o3a 又以 Store v21 原子 claim dispatch 与全新 admission ticket，只允许 expired ticket 的
崩溃恢复，用户取消/失败/完成均终态关闭；HAR-08.4o3b 已用新的 retry permission、execution authority、
ticket、Runtime lease 与 Run Grant 恢复原 Request Manifest 和连续 H5a，并开放共享 Tool/Slash。
HAR-08.4o3c 已让 New UI 从 accepted cancel receipt 一键 retry，Textual TUI 复用共享 Slash 与 typed
progress；HAR-08.4o3d 又提供有界 durable dispatch catalog、opaque cursor、完整 authority/H5a 校验和
共享只读 Tool/Slash；HAR-08.4o3e 已把 accepted receipt 与 pending dispatch 原子落盘，并以新的
Permission receipt 精确绑定既有 dispatch/retry receipt，通过共享 Tool/Slash 从 expired ticket 真实恢复，
不重新消费 cancel receipt；HAR-08.4o3f 又让 Bridge/TUI 启动时复用 open catalog 建立 20 项
tamper-evident 人工恢复队列，New UI/TUI 只展示精确 resume 命令，不自动 claim 或重放。
HAR-08.4o3g 进一步提供精确 dispatch 详情、连续 H5a、ticket fence、tamper-evident snapshot 与
retention 保护引用，并通过共享 Tool/Slash 同步呈现在 New UI/TUI；HAR-08.4o3h 又以显式只读事务、
oldest-first 硬边界和完整 source/current ticket/H5a 保护集合交付 retention preview；
HAR-08.4o3i 通过 Store v22、父权限回执、事务内 fence/protection refs 重校验和 accepted candidate
唯一约束签发不可变 prune receipt，明确不删除记录；HAR-08.4o3j 又以 Store v23 原子消费 accepted
authorization receipt，重新校验完整权威链与保护引用，只删除精确 dispatch、retry attempt 和无共享
引用的当前 ticket，并生成独立 tamper-evident execution receipt；cancel receipt 消费 tombstone 防止
已清理 retry 被重新授权。Request Manifest、source ticket、cancel receipt、H5a 与审计回执继续保留。
跨主机批量 prune、共享事实深度回收和三平台隔离 CI 仍未完成。
Profile/Trust/Knowledge、Completion Gate、Store、实时持久化、EvidenceCollector、确定性 Explain、
安全 Replay 与可审计评测闭环。权威代码位于
`src/naumi_agent/harness/`，状态库位于用户状态目录的 `harness.db`。
HAR-07.5b 已为 New UI 增加 `v` Evidence 焦点和 `/harness evidence`，并让 CLI/Textual TUI 复用相同
Explain authority；HAR-07.4b1 已补齐空闲 Bridge 有界重启、重新协商和精确 session 回执恢复。
ARC-02.5a 又补齐两类安全回执的持久事件身份与 session cursor；ARC-02.5b 已补齐 New UI 持久 ACK、
窗口内 cursor resend 与窗口外明确 snapshot baseline。HAR-07.5c1 已让三端共享
`/copy receipt [receipt-id|latest]`，从当前 session 的持久 Store 生成脱敏回执并保存到
`.naumi/exports`，HAR-07.5c2 已补齐完成卡直达。活动运行恢复与非回执事件的 cursor/revision/gap
合同仍未完成。

## 后续顺序

1. HAR-06 Session 生命周期：删除、归档、保留和清理一致。
2. HAR-07 Completion UI：新 UI/TUI 都展示权威回执、explain 与 replay。
3. HAR-08 Eval/Baseline：为模型、Prompt、Tool、Harness 和自进化提供量化裁判。
4. HAR-09 Feedback Promotion：重复失败变成可审查改进候选。
5. HAR-10 Long-running Orchestration：心跳、租约、恢复、分片和人工接管。

## Harness 不负责

- 不替代 TaskStore、Pursuit、PermissionChecker、Worktree 或 DebugTrace。
- 不保存原始大输出、secret 或 reasoning。
- 不让 LLM 覆盖机械检查结果。
- 不自动信任 workspace Profile，不自动提升自进化补丁。
