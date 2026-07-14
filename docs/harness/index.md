# NaumiAgent Harness 知识地图

本文是仓库 Harness 的权威入口。它只描述已经存在的 H1/H2 能力；未来阶段记录在
[debt.md](debt.md)，不会伪装成已实现功能。

## 快速导航

| 任务 | 首选文档 | 真实代码入口 |
|---|---|---|
| 理解 Profile、信任与诊断 | [architecture.md](architecture.md) | `src/naumi_agent/harness/profile.py`、`trust.py`、`service.py` |
| 理解仓库知识选择 | [architecture.md](architecture.md) | `src/naumi_agent/harness/knowledge.py`、`context.py` |
| 修改 Agent 临时上下文 | [golden-principles.md](golden-principles.md) | `src/naumi_agent/orchestrator/context_assembly.py`、`engine.py` |
| 修改 Terminal UI | `docs/product/terminal-ui/` | `frontend/terminal-ui/src/`、`frontend/terminal-ui/test/` |
| 修改 Mac Workbench | `apps/macos/NaumiAgentWorkbench/README.md` | `apps/macos/NaumiAgentWorkbench/Sources/` |
| 查看未实现阶段与限制 | [debt.md](debt.md) | `docs/superpowers/specs/2026-07-14-harness-engineering-design.md` |

## 当前事实

- Profile 固定在 `.naumi/harness.yaml`，只保存机械配置，不保存 API Key。
- Profile 必须通过严格解析，并由用户信任其精确 SHA-256 digest；内容变化立即失信。
- 信任记录位于用户状态目录，不进入仓库，也不会暴露为 Agent 写工具。
- H2 使用路径、文件名、文本、import、Git changed paths 和 source-test 关系进行确定性选择。
- L0 默认最多 1,000 tokens；L1 默认最多 8,000 tokens；总量还受模型窗口 15% 和 12,000 硬上限限制。
- 仓库知识只进入带 `<naumi_harness_context>` 标记的当前轮 system snapshot，不进入 `_full_history`。
- `harness_read_knowledge` 与 `/harness knowledge` 共用 `HarnessService.read_knowledge()`。
- H2 不执行 Profile 中的检查命令；命令执行属于 H3。

## 新鲜度记录

```yaml
source_paths:
  - src/naumi_agent/harness/**
  - src/naumi_agent/orchestrator/context_assembly.py
  - src/naumi_agent/orchestrator/engine.py
  - .naumi/harness.yaml
verified_on: 2026-07-14
verified_at_commit: pending-h2-feature-commit
evidence: docs/superpowers/plans/2026-07-14-harness-knowledge-plane.md
```

完成 H2 feature commit 后，必须把 `verified_at_commit` 更新为实际提交，并以独立证据提交保存，避免在同一提交中写无法成立的自引用 hash。
