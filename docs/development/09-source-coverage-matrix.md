# 来源覆盖与需求追踪矩阵

本文证明新模块册没有脱离现有设计另起炉灶。实现模型遇到冲突时，以真实源码和测试为当前
事实，以本目录为未来交付边界；旧文档中的历史事实、取舍依据和已完成记录仍然有效。

## 1. Harness 设计覆盖

| 原设计主题 | 当前事实 | 后续承接 | 完成判据 |
| --- | --- | --- | --- |
| H1 Profile/Trust/Knowledge | 已实现基线 | HAR-05/08 消费，不重建 | Replay/Eval 使用同一 Profile 与 Trust 语义 |
| H2 Completion Gate | 已实现基线 | HAR-07/08/EVO-03 | UI、Eval、自进化不能绕过机械 Gate |
| H3 Store/Receipt | 已实现基线 | HAR-05/06/07 | 可重放、可清理、可恢复、可解释 |
| H4.1-H4.4 Evidence/Explain | 已实现基线 | HAR-05 | 相同证据与规则产生确定结果 |
| 安全 Replay | 未完成 | HAR-05 | 不重放副作用，跨实例结果可核验 |
| Session 生命周期 | 未完成 | HAR-06 | 删除/归档/保留与派生数据一致 |
| Completion 产品闭环 | 部分完成 | HAR-07 | New UI/TUI/恢复使用同一权威回执 |
| Eval 与 baseline | 未完成 | HAR-08 | 可重复、可比较、有 guardrail |
| 反馈提升 | 部分完成（可信 intake） | HAR-09 | 重复信号只产生可审查 Proposal |
| 长周期编排 | 未完成 | HAR-10 | lease、心跳、恢复、接管、分片达到 A5 |

## 2. `13-cli-tui` 覆盖

| 原路线图剩余主题 | 后续承接 | 关键边界 |
| --- | --- | --- |
| `/workbench` | UI-10 | 只读首帧，动作回到 Python service |
| TaskList/Agent 可视化 | UI-11 | TaskStore/SubAgentManager 是权威 |
| 权限气泡与批量决策 | UI-12 | bypass 免二次确认但保留审计和硬边界 |
| Doctor/DebugTrace | UI-13 | 默认脱敏，诊断进程可取消和清理 |
| QuickOpen/Vim/键位/IME | UI-14 | 输入状态机与命令路由分离 |
| 大历史和高频输出性能 | UI-15 | 有界缓存、虚拟化、平滑滚动和背压 |
| macOS/Linux/Windows/终端差异 | UI-16 | capability detection，不靠 OS 猜测 |
| New UI 默认、TUI fallback、旧 CLI deprecated | UI-17 | golden scenario parity 与发布门 |

现有语义颜色、工作动画、心跳、任务面板、Completion Receipt 和初始化引导属于基线，不在
未来模块中重复发明；UI-10..17 只能增强现有协议和组件路径。

## 3. `14-claude-code-source` 覆盖

| 审计主题 | 后续承接 | 决策产物 |
| --- | --- | --- |
| source map 漂移与来源证明 | CC-01 | 可校验 v2 映射、commit、license、采纳状态 |
| React/Ink 是否值得迁移 | CC-02 | adopt/defer/reject ADR 与同 fixture benchmark |
| TaskList/Permission/Doctor 行为 | CC-03 | 行为契约、适配层、Naumi 差异说明 |
| Plugin/Skill/MCP 扩展机制 | CC-04 | 统一 manifest、信任、冲突和隔离规则 |
| 上游持续变化 | CC-05 | 定期差异报告和行为回归，不自动覆盖本地实现 |

Claude Code 只提供研究和机制来源，不能取得 NaumiAgent Runtime、权限、Harness 或 Store 的
权威。任何复制必须满足 CC-01 provenance 门。

## 4. `14-future-architecture` 覆盖

| 原架构域 | 后续承接 | 阶段门 |
| --- | --- | --- |
| Core/Runtime/Tools/Frontend 分层 | ARC-01 | import firewall 与 ports contract 生效 |
| Embedded → Runtime Service | ARC-02 | 本地 transport、多客户端、重连、fallback |
| JSONL/Schema 演进 | ARC-03 | 版本协商、兼容矩阵、conformance fixture |
| Browser/Tool/Agent 执行隔离 | ARC-04 | grant、幂等、取消、崩溃恢复 |
| SQLite/配置/Artifact 迁移 | ARC-05 | catalog、迁移、备份、回滚、恢复演练 |
| 高并发和 Agent 集群 | ARC-06 | admission、背压、公平、bulkhead、soak |
| 闭源产物、安装和更新 | ARC-07 | 三平台签名、SBOM、升级与回滚 |
| SLO、可观测和灾难恢复 | ARC-08 | 指标、告警、脱敏、runbook、RPO/RTO |

原文的 Rust/Go daemon 与 Ink renderer 都是量化决策项，不是预设答案。CC-02 和 ARC-04 必须
先用真实 benchmark/隔离需求证明迁移价值。

## 5. 自进化闭环覆盖

| 闭环节点 | 当前能力 | 后续承接 | 不可绕过门 |
| --- | --- | --- | --- |
| Evidence → Candidate | `self_review`、Harness Evidence | EVO-01 partial (1.1a, 1.1b, 1.2a) | Harness/AST 脱敏证据与不可执行 Draft 已落地；Store、资格和 Review Queue 仍待实现 |
| Candidate → Mutation | `self_modify`、Worktree | EVO-02 | protected scope、预算、隔离、可回滚 |
| Mutation → Evaluation | ruff/compile/pytest 基线 | EVO-03 + HAR-08 | before/after 同环境，guardrail 一票否决 |
| Evaluation → Decision | `self_evolve` 部分决策 | EVO-04 | 机械 veto、防奖励投机、反事实审查 |
| Decision → Promotion | 手工 Git 路径 | EVO-05 + ARC-07 | 审批、重放、canary、自动回滚 |
| Outcome → Feedback | Pursuit/Harness 局部记录 | EVO-06 + HAR-09 | 长期效果、能力影子注册、退役 |

## 6. 完整性审计方法

最终审核时至少检查：33 个一级 ID 与机器注册表一致；219 个子模块编号在各一级模块内从 `.1`
连续递增；所有旧文档新增权威入口回链；不存在旧主题既未标为已完成也未映射到未来模块的空洞。
发现新需求时先决定是现有子模块、现有模块扩展还是新一级模块，不允许用未编号待办绕过治理。
