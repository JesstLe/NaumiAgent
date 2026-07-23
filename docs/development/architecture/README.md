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
消费 admit/claim/run/renew/finish 链并建立终态发布屏障；Agent 仍不是 daemon。自动 scheduler、
可恢复 response、Agent/Browser 持久
Worker、priority/公平调度与跨主机 topology 尚未实现，因此
ARC-06 保持 partial。
