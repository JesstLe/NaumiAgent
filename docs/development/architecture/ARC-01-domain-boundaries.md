# ARC-01 Domain Boundary 与依赖防火墙

## 目标

不搬目录先定义领域 API 和依赖方向，把 `main.py`、`engine.py`、UI 和工具间的隐式调用变成
可测试端口；只有调用迁移完成后才移动文件。

## 子模块

- ARC-01.1 Import graph：生成模块依赖、循环、跨层违规和热区基线。
- ARC-01.2 Domain ownership：model/runtime/tools/memory/safety/harness/ui/tasks 的唯一 owner。
- ARC-01.3 Ports：ModelPort、ToolExecutionPort、SessionPort、EventSink、PermissionPort。
- ARC-01.4 Composition root：依赖只在启动层注入，领域模块不读取全局配置。
- ARC-01.5 Legacy adapters：旧 Engine/CLI 调用逐项适配并记录移除条件。
- ARC-01.6 Import rules CI：禁止 UI→tool implementation、tool→UI、core→frontend 等反向依赖。

## 执行状态

| 子模块 | 状态 | 权威证据 |
| --- | --- | --- |
| ARC-01.1 Import graph | 已实现 | `src/naumi_agent/architecture/import_graph.py`、`docs/architecture/arc-01-import-graph-baseline.json` |
| ARC-01.2 Domain ownership | 已实现 | `src/naumi_agent/architecture/ownership.py`、`docs/architecture/arc-01-domain-ownership.json`、[设计](ARC-01-2-domain-ownership-design.md) · [实现计划](ARC-01-2-domain-ownership-implementation-plan.md) |
| ARC-01.3 Ports | 进行中 | SessionPort：[设计](ARC-01-3a-session-port-design.md)；必须逐个 Port 独立实现，不创建空 Protocol 集合 |
| ARC-01.4 Composition root | 待开发 | 等待真实 Port 完成 |
| ARC-01.5 Legacy adapters | 待开发 | 等待 composition root 契约稳定 |
| ARC-01.6 Import rules CI | 待开发 | 消费 ARC-01.1 graph 与 ARC-01.2 ownership，不重复扫描源码 |

## 验收标准

- 依赖图可重复生成；所有现有循环列入显式 debt 或消除。
- 新端口有类型和 contract tests，不以 `Any`/dict 自由扩张。
- `AgentEngine` 的一个真实流式任务通过新端口运行，行为和 receipt 不变。
- 旧 CLI/TUI/new UI 同时通过；不进行大规模目录移动。
- import rule 只阻止新增违规，存量 debt 有逐项预算和目标模块。

## 退出门

ARC-02 只有在 Runtime 对 UI/Tool/Store 的依赖都能通过端口注入后才能开始。
