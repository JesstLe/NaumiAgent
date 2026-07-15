# Harness 后续开发模块册

## 当前基线

H1-H3、H4.1-H4.4 与 HAR-05 已实现：Profile/Trust/Knowledge、Completion Gate、Store、
实时持久化、EvidenceCollector、确定性 Explain 与安全 Replay。权威代码位于
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
