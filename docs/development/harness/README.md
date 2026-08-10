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
HAR-10.8b 已进一步把完成、等待、阻塞、取消和预算越界收口为内容寻址机械裁判，完整决策在
PursuitStore 持久化，并由 Goal New UI 与 TUI/CLI fallback 展示；无持久引用的 waiting 失败关闭。
HAR-10.8c 已把 checkpoint/reconcile/interaction 恢复分支接入同一裁判，升级 schema 2 并保持
schema 1 兼容；durable pending interaction 现在是可恢复 waiting，冲突恢复 authority 明确 blocked。
HAR-10.8d 已增加内容寻址、哈希链保护的 recovery attempt 账本；生产 `/pursue resume` 在 policy/bypass
下都先绑定持久权限回执，重复 ToolCall 不会重复恢复，requested/admitted/terminal 事实可在重开后复验。
HAR-10.8e 已补齐显式 terminal reconciliation：健康 heartbeat/有效 lease 失败关闭，只有取得更高
RunLease epoch 并复验准入后的 checkpoint、机械裁判与 run 状态后，才原子收口 attempt 并保存不可变回执。
HAR-10.7f 已把 Agent terminal publication 从 startup-only 恢复升级为默认启用的周期 worker：共享既有
加密 outbox/claim epoch/result inbox，提供串行 pass、有界空闲/失败退避、live failure wake 和 Engine
shutdown drain；HAR-10.7g 已补齐重试预算和 poison-record quarantine/dead-letter，ARC-04.5e1 又完成
control-only 独立 Agent Worker 前置；ARC-04.5e2/HAR-10.7h2 已完成 admitted-only Job owner lease、
物理 slot、加密 staging 与双续租；ARC-04.6a/HAR-10.7h3 已补齐 PID/create-time 进程证据、Supervisor
owner lease 与认证 pre-start fencing/requeue；ARC-04.5e3a/HAR-10.7h4 已补齐独立 Provider 调用和加密
terminal/outbox 提交；ARC-04.5e3b1/HAR-10.7h5 又补齐 exact manifest、加密 Tool RPC、多轮循环和父
Runtime authority callback 内核；ARC-04.5e3b2/HAR-10.7h6 已进一步接通生产 SubAgent 默认路由、
`Engine.execute_tool()` 权限事件与 Agent Control schema v6 双端状态。长驻池、公平 scheduler 与完整
Supervisor 运维仍未完成。详见
`HAR-10-7f-periodic-agent-publication-recovery.md`、
`HAR-10-7g-publication-quarantine-dead-letter.md` 与
`../architecture/ARC-04-5e1-independent-agent-worker-bootstrap.md`、
`../architecture/ARC-04-5e2-independent-agent-worker-owner-lease.md`、
`../architecture/ARC-04-6a-agent-worker-supervisor-owner-fencing.md`、
`../architecture/ARC-04-5e3a-independent-agent-model-execution.md` 与
`../architecture/ARC-04-5e3b1-encrypted-agent-tool-rpc.md`、
`../architecture/ARC-04-5e3b2-production-agent-worker-routing.md`。HAR-10.7g 又以 Store schema v6 的 HMAC quarantine
receipt、durable retry budget 和 Agent Control schema v5 解除 poison-record FIFO 阻塞并同步 New UI/TUI；
exact requeue、放弃和 prune 仍未完成。详见 `HAR-10-7g-publication-quarantine-dead-letter.md`。
HAR-10.8f1 又把完整终态 checkpoint 与 pending outbox、attempt 收口与 delivered 分别放入同一
PursuitStore 事务，并提供有界认证恢复目录。
HAR-10.8f2a 已在该边界上补齐 durable claim/expiry/takeover/backoff、默认 startup/periodic worker 与
shutdown drain；HAR-10.8f2b 又把有界 backlog 与 worker health 同源投影到 Bridge、New UI 和
Goal Tool/Textual TUI fallback。HAR-10.8f2c 增加 due-only 显式恢复、统一 ToolExecution 权限链和
不可变 pass 回执；push stream、dead-letter/retention 仍未完成。
UI-18.5b1 已让 New UI 通过 typed ToolExecution 消费该账本，并让 TUI fallback 显示同源动作、共享命令
和最近 attempt；前端不解析工具文案生成结果状态。
HAR-10.2a 在 Harness DB v12 建立 typed heartbeat，并接入 Pursuit lease worker 的 acquire/renew/release；
HAR-10.2b 已把 heartbeat/lease/checkpoint/reconcile 聚合到 Goal 新 UI、CLI/TUI fallback 与 Doctor health；
HAR-10.2c-10.2h 已进一步交付默认 New UI runtime producer、安全 retention authority、typed worker catalog、
独立租约协调的周期 retention core、Bridge 生命周期、真实 Agent 委派 heartbeat 和 browser execution heartbeat。跨 kind
catalog、专用 Doctor 详情和 Supervisor
仍未完成。
HAR-10.6a 在 Harness Store v13 提供 durable interaction request/answer、timeout、takeover 与
并发 fencing；HAR-10.6b 已接入 New UI Bridge 和 Pursuit stable checkpoint/reconcile，UI-18.4b 已让
Textual TUI 复用相同 authority adapter；UI-18.4c 已补齐 Goal interaction ledger 与显式 cancel。手动
takeover 已由 UI-18.4d2 以宿主绑定方式补齐；UI-18.4d3 又补齐 filter-bound opaque cursor、
New UI 页内详情和 TUI 命令式后续页。HAR-10.6c 又以 Store v24 增加不可变四级优先级、
4:2:1:1 公平轮转、旧账本哈希兼容，以及 New UI/TUI/Goal 同源投影；pending recovery cursor 仍未完成。
HAR-10.3a 已为 New UI 增加 `/send-now` 与安全边界队列提升；HAR-10.3b1 已把 Harness Store 升级到 v14，
交付持久队列 Store 核心；HAR-10.3b2 已进一步接入 Bridge durable enqueue、RunLease claim/renew、fenced terminal
和显式 Session 恢复；HAR-10.3b3 已升级 Harness Store v15，交付 `/queue` 历史 claim 审查、审计
retry/cancel 和 New UI 即时恢复。HAR-10.2c 已把默认 New UI Bridge 接入 typed runtime heartbeat lifecycle，
HAR-10.2d 已建立 old offline/terminal runtime 的有界 retention authority；
HAR-10.2e 已建立可跨 Store 翻页的 typed runtime worker catalog；
HAR-10.2f1/2 已建立删除前精确续租、活跃保护和稳定状态的周期 retention core，并以 7 天安全默认值接入 Bridge；
HAR-10.2g 已让每次 Agent 委派产生 durable heartbeat，并在 New UI/TUI Agent Control 显示阶段和降级码；
HAR-10.2h 已让每次真实 browser run 产生 durable heartbeat，在等待指令/人工接管时保持 waiting pulse，并由 Task Panel
双端展示 phase、epoch 与降级码；HAR-10.2i 又把 ARC-07.5e managed terminal identity 与 New UI/TUI runtime
heartbeat startup 原子绑定；HAR-10.2j 已让后续 heartbeat 同事务进入 append-only observation ledger，但样本账本
仍不能冒充带 exposure 与时间门槛的持续 observation window；HAR-10.2k 已让 New UI/TUI 的真实 chat run 在开始时
原子保存 task-local exact release provenance，但该来源事实仍不代表执行成功；
HAR-10.3b4 已让 TUI 运行中输入复用相同持久队列、claim/renew/terminal 与
`/send-now`，并修复两端在本 owner live claim 期间无法重排后缀的问题。HAR-10.3b5 又补齐未 claim 普通消息的
精确取消和双端回执。跨客户端公平与 active worker 取消传播仍未完成。
HAR-10.7a 已封住直接 Agent 委派绕过 `max_parallel_agents` 的入口，让 direct/batch/DAG 共用进程内
admission、排队计数与取消清理，并拒绝容量饱和时会自锁的嵌套委派；HAR-10.7b 又以
`max_queued_agents` 封住本地等待协程的无界增长，并提供稳定过载回执。ARC-04.5a 已先让现有委派在
模型调用前签发请求合同、终态签发低敏结果回执；ARC-04.5b2/5c 又接入加密 durable Agent Job、
claim renewal、终态发布屏障和双端 job state/epoch 证据。HAR-10.7c/ARC-06.2c 进一步让多个 Runtime
共用 active 上限、有界 FIFO、等待取消与 recovery-blocking 计数；HAR-10.7d 又交付逐条认证、最多
50 项的恢复目录，并通过 Agent Control schema v4 同步两端 claimed/running/unknown 与
pending/expired publication；HAR-10.7e 进一步以 request/session/receipt/epoch/expiry 五重 fence
提供 expired running → unknown 的显式 `u` 裁决，且不重放模型；HAR-10.7f 已增加 publication
startup/periodic recovery、有界退避、失败唤醒与 shutdown drain；HAR-10.7g 已补齐 durable retry budget、
quarantine/dead-letter 与双端隔离事实，并以 ARC-04.5e1 建立 control-only 独立 Agent Worker；
ARC-04.5e2/HAR-10.7h2 已补齐 pre-start owner lease、物理 slot 与加密 staging；ARC-04.6a/HAR-10.7h3
已补齐最小 Supervisor owner/fencing。
ARC-04.5e3a/HAR-10.7h4 已补齐独立模型执行，ARC-04.5e3b1/HAR-10.7h5 已补齐加密 Tool RPC 与多轮
执行内核，ARC-04.5e3b2/HAR-10.7h6 已补生产默认路由、Engine 权限链与双端执行后端投影；长驻池、
完整 Supervisor、
跨 workspace/provider fairness、自动 Job
recovery scheduler 和 quarantine requeue/prune 仍未完成；ARC-04.5d2c 已让双端 Agent Control 查看当前 session
的认证、脱敏 response 摘录，但分页、
read/ack 与完整结果导出仍未完成。
HAR-08.5a 已交付显式、有成本和时限上限的单次 Live 模型传输评测；HAR-08.5b 又增加 Profile 声明的
严格 Live Suite、5..20 次同身份样本、批次请求证据绑定、实际 Provider 模型 identity 与逐样本 H5a
不可变持久化。基础设施错误不重试，已记录成本超预算立即停止后续样本，normal 一次确认、bypass 直通。
HAR-08.5c1 已进一步把 Live batch 的真实调用、保存、成本和终态通过受控 Tool Runtime Event 同步到
New UI/TUI。HAR-08.5c2 又将 transport 用量、rate-card 成本估算及单价来源、Provider 账单状态和 response-id 摘要作为
同源证据贯穿 ModelResponse/H5a/双端 UI，不再把估算称为最终账单；专用历史、独立取消 authority、
Provider billing/cancellation API 和三平台 matrix 仍未完成。
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
UI-10.6b1 已在既有 HAR-09.5b1 Proposal governance authority 上补齐 New UI/TUI defer 入口：
原因与 1/7/30 天预设由界面收集，精确时间由 Python authority clock 生成，normal 一次确认、bypass
无二次确认。UI-10.6b2 又补齐同 Candidate 较新 open revision 的 merge 目标投影和两端选择器，最终
Service 重验、CAS 与审计保持不变。HAR-09.6a 已补齐 `rolled_back` Outcome 的 Workbench/New UI/TUI
只读投影和服务端 Contract 终态阻断。HAR-09.6b 又把原 Promotion Input 中 proposal-bound Final Evaluation
与全部 HAR-08 H5c lane 登记为 `implementation_before_after`，并同步 Workbench/New UI/TUI；它明确不是
回滚后行为恢复证明。HAR-09.6c1 已新增 fresh baseline boot 与 launcher identity verification，并同步三端；
ARC-07.5f/5g 已提供 exact installed backend 的首个 `protocol_hello@1` Eval transport 和 runtime-side identity，
HAR-09.6c2a 已将其绑定到 Outcome、6c1、Before/After 原 H5c lane 与原 baseline cohort，形成首个真实
installed-runtime fresh H5a/H5c 单平台 lane，并通过共享 Tool/Slash 同步终端三端。HAR-09.6c2a1 又冻结完整
Final Evaluation lane 覆盖契约，明确区分本机 installed baseline 与必须由目标主机重新证明的 lane，避免把
macOS slot 冒充为 Linux/Windows evidence。目标平台 baseline 解析、远程授权/结果摄入、完整跨平台行为矩阵、
长期指标与 promoted Outcome 仍未完成。
HAR-09.6c2a2a 已进一步将 missing remote lane 绑定到 exact active Worker incarnation，并机械导出
`macos/linux/windows-{arm64|x64}` target。HAR-09.6c2a2b 又从 current signed Release Channel Catalog 解析
同 version/source commit/source tree 的 target-specific Build Attestation，并动态绑定 Channel/Builder trust；健康、
容量、下载交付和执行权仍明确为未完成。
HAR-09.6c2a3a / ARC-04.1c 已进一步交付 Worker Registry v5 durable Health Report：exact active incarnation 的
heartbeat、accepting-jobs 与 active-jobs 单调持久化，并可关闭重开后驱动 admission。HAR-09.6c2a3b 又把
current Target Baseline、exact Placement、durable Health、原 Before/After lane 与 workspace Eval suite 组合为
tamper-evident queued Dispatch，并在 Registry 中原子预留 exact incarnation capacity；claim/lease、传输、执行授权
和结果摄取仍未完成。
ARC-04.1d 已补齐通用 supervisor-attested Ed25519 Worker Identity，为 claim challenge 提供 exact incarnation
签名身份。HAR-09.6c2a3c 已进一步完成 one-time challenge、Ed25519 verification、单调 lease receipt、bounded
renewal 与动态 fencing。ARC-04.1e 又补齐独立 X25519 Transport Key、连续 generation rotation、同库 Identity
绑定和动态 fencing。HAR-09.6c2a3d 已进一步完成 signed Catalog artifact 的控制面流式验证、X25519+HKDF+
AES-GCM descriptor、exact Worker Ed25519 ACK 与 durable delivery receipt；它只授予 transport delivery，自动
远端 push、execution authorization 和 result ingestion 仍未完成。
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
