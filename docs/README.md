# NaumiAgent 文档地图

## 后续开发权威入口

大型后续工程统一从 [`development/README.md`](development/README.md) 进入。该目录包含
33 个可独立交付模块，覆盖 Harness、CLI/TUI/New UI、Claude Code 源码对齐、未来架构和
自进化闭环，并提供机器注册表、验收证据标准及最终审核清单。

这里是 NaumiAgent 当前文档的唯一总入口。根目录 [README](../README.md) 负责安装和快速开始；
本页负责区分当前使用说明、产品规格、历史工程记录、迁移资料和参考材料。

## 当前产品事实

- `naumi`、`naumi chat` 和 `naumi ui` 默认启动 Node Terminal UI。
- Node 缺失、版本过旧、资源损坏或 UI 异常退出时，启动器只自动 fallback 一次到 Textual。
- `naumi tui` 是受支持的显式 Textual 入口；`naumi ui --legacy` 只保留为带弃用提示的迁移别名。
- 旧 Prompt Toolkit CLI 的实现、测试和必要依赖继续保留，但没有公共启动入口。
- 项目配置、provider 目录和运行数据默认位于 `.naumi/config.yaml`、
  `.naumi/providers.json` 和 `.naumi/data/`；密钥只进入系统凭据库或环境变量。
- 默认权限模式可配置；`bypass` 表示工具执行全权限通过。默认预算不限，最大 Agent 轮数为 50。
- 当前品牌图标的唯一文档源是 [`assets/logo.png`](../assets/logo.png)；macOS AppIcon
  是同一图标的发布格式。旧版深色 N 标志和未采用的 logo variant 已移除。

## 当前使用说明

| 主题 | 文档 |
|---|---|
| 安装、启动、命令与目录 | [根 README](../README.md) |
| 新 Terminal UI 与 Python Bridge | [Terminal UI 集成](./terminal-ui-integration.md) |
| 模型、Provider、密钥与思考强度 | [模型配置](./15-model-provider-configuration.md) |
| Docker API 部署 | [容器化部署](./deployment.md) |
| Harness 当前架构与原则 | [Harness 知识地图](./harness/index.md) |

当文档与代码行为冲突时，以实现和定向测试为准，并修正文档；不要把日期更早的计划当作当前命令手册。

## 产品与界面规格

### Terminal UI

- [模块规格入口](./product/terminal-ui/README.md)
- [默认入口与运行壳](./product/terminal-ui/01-default-entry-and-runtime-shell.md)
- [对话时间线与输入器](./product/terminal-ui/02-conversation-timeline-and-composer.md)
- [执行时间线与权限](./product/terminal-ui/03-execution-timeline-and-permissions.md)
- [Inspector 与命令页](./product/terminal-ui/04-inspector-and-command-pages.md)
- [会话持久化与恢复](./product/terminal-ui/05-session-persistence-and-recovery.md)
- [完成收据与验证](./product/terminal-ui/06-completion-receipt-and-validation.md)
- [CLI 兼容与迁移](./product/terminal-ui/07-cli-compatibility-and-migration.md)
- [协议、测试与发布门禁](./product/terminal-ui/08-protocol-testing-and-release-gates.md)

产品规格同时包含已完成切片和目标态。每份规格的“当前证据/实施进度”决定某项能力是否已经交付，
不能仅凭标题或验收标准推断完成。

### Mac Agent Workbench

- [产品需求](./product/mac-agent-workbench-prd.md)
- [用户工作流](./product/mac-agent-workbench-user-flows.md)
- [总体架构](./design/mac-agent-workbench-architecture.md)
- [领域模型](./design/mac-agent-workbench-domain-model.md)
- [事件协议](./design/mac-agent-workbench-event-protocol.md)
- [界面规格](./design/mac-agent-workbench-interface-spec.md)
- [本地 Daemon Bridge](./design/mac-agent-workbench-local-daemon-bridge.md)
- [本地安全与 Workspace 授权](./design/mac-agent-workbench-local-security-workspace.md)
- [打包与分发路线](./design/mac-agent-workbench-packaging-distribution.md)

Workbench 文档描述 macOS 原生产品线；不能据此推断尚未合入 `main` 的 Windows 原生 Workbench 已交付。

## 架构历史与工程记录

- [早期架构总览](./01-architecture-overview.md) 至
  [优化路线图](./12-optimization-roadmap.md) 保存项目早期架构与阶段计划。
- [CLI/TUI Claude Code 化路线图](./13-cli-tui-claude-code-roadmap.md)、
  [Claude Code 源码审计](./14-claude-code-source-audit.md) 和
  [未来架构重构方案](./14-future-architecture-refactor-plan.md) 保存迁移决策与来源证据。
- [实施设计与计划索引](./superpowers/README.md) 说明按日期保存的 specs/plans 如何阅读。
- [/pursue 优化记录](./pursuit_optimization_log.md) 是阶段性实验日志，不是当前用户手册。

历史记录中的旧命令、旧预算和旧配置路径保留当时语境，不应被复制到新安装说明中。

## 迁移、质量与发布

- [Browser Debugging Daemon 迁移总览](./migration/00-overview.md)
- [Mac Workbench 测试策略](./quality/mac-agent-workbench-test-strategy.md)
- [Mac Workbench 验收标准](./quality/mac-agent-workbench-acceptance.md)
- [Mac Workbench 内部签名发布清单](./release/mac-agent-workbench-release-checklist.md)

## 示例与参考材料

- [Skill 示例](./examples/skills/README.md)
- [Mac App Agent Workbench 参考稿](./references/mac-app-agent-workbench-reference.md)
- [NaumiAgent Lite 学习与交付材料](./product/naumiagent-lite/README.md)

这些内容用于参考、教学或产品表达，不自动升级为当前运行保证。

## 文档治理

[`governance.json`](./governance.json) 为全部 Markdown 文档定义状态分类。检查器会验证分类覆盖、
规则冲突、本地链接，以及当前文档是否重新推荐退役入口：

```bash
uv run python scripts/check_docs.py
uv run pytest tests/unit/test_docs_governance.py -q
```

新增文档时必须同时确保它命中唯一治理规则。历史计划和设计只追加新记录，不为追赶当前实现而
回写旧结论；当前手册发生行为变化时，应在同一功能切片内同步更新并通过检查器。
