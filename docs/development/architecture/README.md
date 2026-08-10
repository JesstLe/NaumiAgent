# Future Architecture 后续模块册

本册细化 `docs/14-future-architecture-refactor-plan.md`。路线仍坚持：先在 Python 单体内部建立
边界，再服务化 Runtime；TypeScript/Ink 和 Rust/Go daemon 只有达到量化门槛才采用。

## 目标分层

- `naumi-core`：模型、契约、领域类型、纯策略。
- `naumi-runtime`：会话、Agent 循环、任务、Harness、权限协调。
- `naumi-tools`：工具实现和执行适配。
- `naumi-frontends`：New UI、TUI、Workbench、未来 Web。
- `naumi-daemons`：浏览器、shell、重执行、集群 worker。

模块顺序：ARC-01/03/05 → ARC-02 → ARC-04 → ARC-06 → ARC-07/08。

ARC-05.1/5.2a 已建立 Store Catalog 与事务化 SQLite Migration Runner；ARC-05.3a 又补齐单 Store
预迁移空间计划、WAL 一致性快照、原子目录发布、canonical digest manifest 和独立验证。多 Store
journal、Windows ACL、restore、完整性检查、saga 与 retention 尚未完成，因此 ARC-05 保持 partial，
生产 Store 仍不能在启动时静默自动迁移。

ARC-01.4c1-4c3 已交付由 Composition Root 构造的首个 `RuntimeServices` 切片、共享 terminal runtime lifecycle
factory 与 New UI/TUI adapter 迁移；其余 Service 与全局关闭注册表仍未完成，因此 ARC-02 退出门尚未满足。
ARC-03.3a 已交付显式 registry 兼容 ledger 和未知 informational 事件的 sequence-safe、payload-free
忽略路径，为 HAR-07.4b reconnect 提供最小前置。ARC-02.5a 已交付两类安全回执的持久 Event
Journal 与稳定 session cursor；ARC-02.5b 已进一步交付客户端持久 ACK、窗口内 cursor resend 与
窗口外明确 snapshot baseline。活动运行恢复、多客户端策略、未知关键事件局部恢复和 socket transport
仍未完成，因此 ARC-02 只标记 partial。

ARC-06.1a/1b 已建立 Worker capacity reservation 及 ToolJob lifecycle 接入；ARC-06.2a 进一步建立
incarnation-fenced、有界 FIFO 的持久等待 authority，并让 claim 与 reservation 同事务。ARC-06.2b1
已让生产 ToolJob 以 schema v3 `queued` receipt 安全入队、重启补建和取消；ARC-06.2b2 又把 active
claim 接入 ToolJob/Shell dispatch-before-send、start 前复验、terminal release 与 lost-claim reconcile。
ARC-04.5a 又为真实 Agent 委派增加模型调用前 request contract、终态 result receipt 及 New UI/TUI
共享证据；ARC-04.5b1 已补充 OS credential-backed key 与 authenticated payload envelope，
ARC-04.5b2 已建立 durable Agent Job Store 与 fenced lifecycle，ARC-04.5c 已让 embedded Agent
消费 admit/claim/run/renew/finish 链并建立终态发布屏障；ARC-06.2c 又建立跨 Runtime embedded Agent
active 上限、有界 FIFO 与等待取消。ARC-04.5d1 又将 response/error 加密原子提交，并要求生产发布前
从 Store 重新认证恢复；ARC-04.5d2a 又建立 terminal transaction 原子创建的 durable publication
outbox、lease/epoch fencing、恢复目录与 HMAC receipt chain；ARC-04.5d2b 进一步建立 schema v5
幂等 result inbox、生产 manager 在线消费和有界 startup recovery；ARC-04.5d2c 又通过 Agent Control
schema v3 将当前 session 的认证、脱敏、有界结果同步到 New UI/TUI；HAR-10.7d 又通过 Agent Control
schema v4 投影 claimed/running/unknown 与 pending/expired publication 的恢复目录；HAR-10.7e 已让双端
以 exact fence 人工把当前 session 的 expired running Job 收口为 unknown，不重放模型；HAR-10.7f 又增加
publication startup/periodic recovery、有界退避、失败唤醒与 shutdown drain；HAR-10.7g 又增加 durable
retry budget、HMAC quarantine receipt 和双端隔离投影；ARC-04.5e1/HAR-10.7h1 又建立真实独立
control-only Agent 进程、认证本机传输、注册/心跳/排空/撤销；ARC-04.5e2/HAR-10.7h2 进一步把
exact Worker incarnation、物理 slot 和 admitted-only AgentJob owner lease 绑定，并完成进程级一次性密钥
加密 staging 与双续租；ARC-04.6a/HAR-10.7h3 又补齐 PID/create-time witness、Supervisor owner lease 和
认证 pre-start fencing/requeue；ARC-04.5e3a/HAR-10.7h4 又完成 model-only 两阶段 running fence、真实独立
Provider 调用和加密 terminal/outbox 提交；ARC-04.5e3b1/HAR-10.7h5 又完成精确 manifest、加密 Tool
RPC、多轮模型循环、重复副作用阻断与父 Runtime authority callback 内核；ARC-04.5e3b2/HAR-10.7h6
进一步把生产 SubAgentManager 默认路由到独立 Worker，复用 `Engine.execute_tool()` 权限链，并以 Agent
Control schema v6 在 New UI/TUI 区分独立执行与内嵌降级。当前仍是一任务一进程；quarantine requeue/prune、完整 Supervisor、
Agent/Browser 完整持久 Worker、priority/公平调度与跨主机 topology 尚未实现，因此
ARC-06 保持 partial。

ARC-04.1d/1e 已为跨主机 Harness 链分别建立 supervisor-attested Ed25519 Worker Identity 与独立 X25519
Transport Key：exact incarnation、同库持久化、代际轮换和动态 fencing 已完成，但 key artifact 不冒充 baseline
delivery。HAR-09.6c2a3d/3e 已进一步完成远端 encrypted envelope、Worker ACK 与 Worker-signed Start
authorization；自动 remote transport、真实进程 start/result ingestion 仍未完成。
