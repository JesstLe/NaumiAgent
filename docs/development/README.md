# NaumiAgent 后续开发文档中心

本目录是 2026-07-15 之后所有大型开发工作的权威入口。它把现有 Harness、终端 UI、
Claude Code 源码对齐、未来架构和自进化路线拆成可独立领取、实现、验证、提交和审核的
模块。旧文档继续保存历史、研究和已完成事实；新开发必须以本目录的模块 ID 和验收门禁
为准。

## 文档地图

| 文档 | 用途 |
| --- | --- |
| [00-program-charter.md](00-program-charter.md) | 总目标、范围、阶段和不可违背的工程原则 |
| [01-module-registry.md](01-module-registry.md) | 全部未来模块、状态、依赖和建议顺序 |
| [02-delivery-review-protocol.md](02-delivery-review-protocol.md) | 其他模型交付、证据包与最终审核流程 |
| [03-acceptance-evidence-standard.md](03-acceptance-evidence-standard.md) | 统一验收等级、测试层级与真实场景要求 |
| [04-cross-program-dependencies.md](04-cross-program-dependencies.md) | 五个项目域之间的接口和关键路径 |
| [05-execution-waves.md](05-execution-waves.md) | 按依赖组织的实施波次、提交和阶段门 |
| [06-final-audit-checklist.md](06-final-audit-checklist.md) | 最终审核者逐模块复核清单 |
| [07-code-ownership-map.md](07-code-ownership-map.md) | 每个模块的建议生产文件、测试文件与禁止越界区 |
| [08-model-handoff-prompts.md](08-model-handoff-prompts.md) | 其他模型领取、交付和最终审核提示词模板 |
| [09-source-coverage-matrix.md](09-source-coverage-matrix.md) | 旧路线图、源码审计与 Harness 设计到新模块的覆盖追踪 |
| [10-module-execution-record.md](10-module-execution-record.md) | 每个模块从领取到交付必须维护的实施记录模板 |
| [11-decision-risk-register.md](11-decision-risk-register.md) | 跨模块 ADR、风险、假设与阻塞项台账规范 |
| [module-registry.yaml](module-registry.yaml) | 供其他模型和自动化读取的模块注册表 |
| [harness/README.md](harness/README.md) | Harness H4.5-H7 模块册 |
| [cli-ui/README.md](cli-ui/README.md) | 新 UI、TUI fallback 与 CLI 兼容模块册 |
| [claude-source/README.md](claude-source/README.md) | Claude Code 源码迁入与持续对齐模块册 |
| [architecture/README.md](architecture/README.md) | `14-future-architecture` 后续架构模块册 |
| [self-evolution/README.md](self-evolution/README.md) | 自审查→变异→验证→反思→提升闭环模块册 |

## 现有权威依据

- Harness：`docs/superpowers/specs/2026-07-14-harness-engineering-design.md`
- CLI/TUI：`docs/13-cli-tui-claude-code-roadmap.md` 与 `docs/product/terminal-ui/`
- Claude Code：`docs/14-claude-code-source-audit.md`、`frontend/terminal-ui/cc-source-map.json`
- 未来架构：`docs/14-future-architecture-refactor-plan.md`
- 自进化现状：`src/naumi_agent/tools/self_evolve.py`、`self_modify.py`、
  `orchestrator/pursuit.py` 与 `docs/pursuit_optimization_log.md`

## 使用方法

1. 从注册表选择一个 `planned` 模块，确认所有依赖已 `implemented`。
2. 阅读模块文档中的目标文件、接口、非目标和验收矩阵。
3. 按 TDD 先观察 RED，再实现 GREEN；只运行该模块相关小测试。
4. 生成交接证据包，独立 commit，不把相邻模块揉入同一提交。
5. 由最终审核者按模块 ID 对照文档、diff、测试和真实场景证据复核。

当前计划包含 33 个一级模块和 219 个连续编号的子模块。子模块 ID 是交付证据的最小追踪
单位，但 commit 与审核结论仍以一级模块为边界，避免把不可独立运行的半成品直接合入 main。

任何模型不得仅凭“代码已写”或“测试看起来会过”把模块标为完成。
