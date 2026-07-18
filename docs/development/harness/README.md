# Harness 后续开发模块册

## 当前基线

H1-H3、H4.1-H4.4、HAR-05，以及 HAR-08 的离线协议 Eval、安全 Replay Eval、
Identity/Comparator、Result Store、Baseline/Selector、Comparison receipt、HAR-10.1a 持久化 fencing lease、
HAR-10.1b Pursuit 首个生产接入、HAR-10.4a 权威 checkpoint 核心、HAR-10.4b 安全 resume executor，以及
HAR-10.5a shell/background 持久行动账本和 HAR-10.5b Background caller idempotency 已实现。
resume 支持新 lease epoch continuation，并在 in-flight 副作用不明确时进入 `reconcile_required`。
行动账本在外部派发前记录稳定 identity，关联后台 task ID，并阻止 terminal/ambiguous 行动被重复派发；完整
后台任务可通过 caller key 在同 runtime 并发与正常重启后复用；类型化外部状态 reconcile 仍在后续
HAR-10.5c。
HAR-10.8a 已把 Pursuit 每轮验证限制为目标文件/测试节点，移除 assessment 的隐式全量测试和 lint。
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
