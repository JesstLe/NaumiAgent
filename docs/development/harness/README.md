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
browser/agent/runtime producer 和 Supervisor 仍未完成。
HAR-10.6a 在 Harness Store v13 提供 durable interaction request/answer、timeout、takeover 与
并发 fencing；HAR-10.6b 已接入 New UI Bridge 和 Pursuit stable checkpoint/reconcile，UI-18.4b 已让
Textual TUI 复用相同 authority adapter；UI-18.4c 已补齐 Goal interaction ledger 与显式 cancel。手动
takeover、cursor 和详情筛选仍未完成。
HAR-10.3a 已为 New UI 增加 `/send-now` 与安全边界队列提升；队列本身仍是 Bridge 内存状态，持久化、重启恢复、
跨客户端公平和取消传播留给 HAR-10.3b。HAR-10.3b1 已把 Harness Store 升级到 v14，并交付尚未接入 Bridge 的
持久队列 Store 核心；HAR-10.3b2 已进一步接入 Bridge durable enqueue、RunLease claim/renew、fenced terminal
和显式 Session 恢复；HAR-10.3b3 已升级 Harness Store v15，交付 `/queue` 历史 claim 审查、审计
retry/cancel 和 New UI 即时恢复。HAR-10.3b4 已让 TUI 运行中输入复用相同持久队列、claim/renew/terminal 与
`/send-now`，并修复两端在本 owner live claim 期间无法重排后缀的问题。HAR-10.3b5 又补齐未 claim 普通消息的
精确取消和双端回执。跨客户端公平与 active worker 取消传播仍未完成。
Profile/Trust/Knowledge、Completion Gate、Store、实时持久化、EvidenceCollector、确定性 Explain、
安全 Replay 与可审计评测闭环。权威代码位于
`src/naumi_agent/harness/`，状态库位于用户状态目录的 `harness.db`。

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
