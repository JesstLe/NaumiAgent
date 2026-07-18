# 后续开发模块注册表

状态仅允许：`implemented`、`partial`、`planned`、`blocked`、`superseded`。本文登记的是 2026-07-15
之后仍需推进的模块；已完成模块只在依赖说明中出现。

## Harness

| ID | 名称 | 状态 | 依赖 | 文档 |
| --- | --- | --- | --- | --- |
| HAR-05 | 安全 Replay 与可重复解释 | implemented | H4.1-H4.4 | `harness/HAR-05-safe-replay.md` |
| HAR-06 | Session 生命周期与派生数据清理 | implemented | HAR-05 | `harness/HAR-06-session-lifecycle.md` |
| HAR-07 | Completion Receipt UI 与恢复 | partial (7.1a, 7.1b, 7.2, 7.3, 7.4a) | HAR-05 | `harness/HAR-07-completion-ui.md` |
| HAR-08 | Eval Suite、Baseline 与回归比较 | partial (8.1a, 8.3a, 8.6a, 8.6b, 8.7a-8.7d, H5a-H5c, 8.8a-8.8e3) | HAR-05, ARC-03 | `harness/HAR-08-eval-baseline.md` |
| HAR-09 | Feedback Candidate 与受控提升 | partial (9.1a, 9.2a, 9.3a) | HAR-08, EVO-01.3a | `harness/HAR-09-feedback-promotion.md` |
| HAR-10 | 长周期 Harness Orchestration | partial (10.1a, 10.1b, 10.2a, 10.2b, 10.3a, 10.3b1, 10.3b2, 10.3b3, 10.4a, 10.4b, 10.5a, 10.5b, 10.5c, 10.6a, 10.6b, 10.8a) | HAR-06, HAR-08, ARC-06 | `harness/HAR-10-long-running.md` |

## CLI/TUI/New UI

| ID | 名称 | 状态 | 依赖 | 文档 |
| --- | --- | --- | --- | --- |
| UI-10 | `/workbench` 命令页 | partial (10.1-10.3, 10.7) | UI 协议、Runtime Inspector | `cli-ui/UI-10-workbench-page.md` |
| UI-11 | 全屏任务与 Timeline 导航 | partial (11.1a, 11.2a) | Agent Control Center | `cli-ui/UI-11-task-navigation.md` |
| UI-12 | 权限策略中心 | partial (12.1a) | permission bubbles | `cli-ui/UI-12-permission-center.md` |
| UI-13 | Doctor/Debug 全屏诊断 | partial (13.1a) | DebugTrace, heartbeat | `cli-ui/UI-13-diagnostics.md` |
| UI-14 | QuickOpen、Vim 与完整键位层 | planned | shared keybindings | `cli-ui/UI-14-navigation-input.md` |
| UI-15 | 渲染性能、虚拟化与大输出 | partial (15.1a) | render cache | `cli-ui/UI-15-performance.md` |
| UI-16 | 跨终端、无障碍与国际化 | planned | terminal capabilities | `cli-ui/UI-16-platform-accessibility.md` |
| UI-17 | New UI/TUI parity 与发布门 | planned | UI-10..16 | `cli-ui/UI-17-parity-release.md` |
| UI-18 | Goal/Pursuit 与恢复可视化 | partial (18.1, 18.4a, 18.4b, 18.4c, 18.5a) | ARC-01, HAR-10 | `cli-ui/UI-18-goal-pursuit.md` |

## Claude Code Source Alignment

| ID | 名称 | 状态 | 依赖 | 文档 |
| --- | --- | --- | --- | --- |
| CC-01 | 源码采纳治理与映射更新 | partial (1.1a) | 当前 source map | `claude-source/CC-01-source-governance.md` |
| CC-02 | React/Ink Renderer 可替换性实验 | planned | ARC-03, UI-15 | `claude-source/CC-02-ink-spike.md` |
| CC-03 | Task/Permission/Doctor 组件迁入 | planned | CC-01, UI-11..13 | `claude-source/CC-03-component-alignment.md` |
| CC-04 | Plugin/Skill/MCP 机制对齐 | planned | ARC-01 | `claude-source/CC-04-extension-alignment.md` |
| CC-05 | 上游差异监控与行为回归 | planned | CC-01 | `claude-source/CC-05-upstream-regression.md` |

## Future Architecture

| ID | 名称 | 状态 | 依赖 | 文档 |
| --- | --- | --- | --- | --- |
| ARC-01 | Domain Boundary 与依赖防火墙 | partial (1.1-1.3, 1.4a, 1.4b1, 1.4b2a-1.4b2d) | 当前 Python 单体 | `architecture/ARC-01-domain-boundaries.md` |
| ARC-02 | Runtime Service 化 | planned | ARC-01 | `architecture/ARC-02-runtime-service.md` |
| ARC-03 | 协议版本与兼容治理 | partial (3.2a, 3.2b1, 3.4a) | 当前 JSONL | `architecture/ARC-03-protocol-versioning.md` |
| ARC-04 | Tool/Browser/Agent Daemon | planned | ARC-02, ARC-03 | `architecture/ARC-04-execution-daemons.md` |
| ARC-05 | 状态 Schema 与迁移平台 | partial (5.1, 5.2a) | ARC-01 | `architecture/ARC-05-state-migrations.md` |
| ARC-06 | 高并发、背压与集群调度 | planned | ARC-02, ARC-04 | `architecture/ARC-06-concurrency-cluster.md` |
| ARC-07 | 跨平台闭源打包与更新 | planned | ARC-02, UI-17 | `architecture/ARC-07-packaging-update.md` |
| ARC-08 | 可观测性、SLO 与灾难恢复 | planned | ARC-02, ARC-05 | `architecture/ARC-08-reliability.md` |

## Self-Evolution

| ID | 名称 | 状态 | 依赖 | 文档 |
| --- | --- | --- | --- | --- |
| EVO-01 | 自我审查证据与改进候选 | partial (1.1a, 1.1b, 1.2a, 1.3a, 1.4a, 1.6a, 1.6a1) | Harness Evidence | `self-evolution/EVO-01-review-candidates.md` |
| EVO-02 | 隔离变异与补丁生成 | partial (2.1a, 2.2a, 2.3a, 2.4a, 2.4b, 2.5a, 2.5b, 2.6a) | EVO-01, Worktree | `self-evolution/EVO-02-isolated-mutation.md` |
| EVO-03 | 多层验证与 Eval 对照 | planned | EVO-02, HAR-08 | `self-evolution/EVO-03-validation-evaluation.md` |
| EVO-04 | 反思决策与防奖励投机 | planned | EVO-03 | `self-evolution/EVO-04-reflection-decision.md` |
| EVO-05 | 提升、回滚与发布治理 | planned | EVO-04, ARC-07 | `self-evolution/EVO-05-promotion-rollback.md` |
| EVO-06 | 持续学习与能力扩展 | planned | EVO-05, HAR-09 | `self-evolution/EVO-06-continuous-evolution.md` |

## 推荐关键路径

`ARC-01 → ARC-03 → HAR-05 → HAR-08 → EVO-01 → EVO-02 → EVO-03 → EVO-04 → EVO-05`

UI 可并行推进 `UI-10..16`，但 `UI-17` 必须等待全部 UI 模块；daemon 与集群路线从
`ARC-02 → ARC-04 → ARC-06` 顺序推进。
