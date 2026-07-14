# CC-03 Task/Permission/Doctor 组件语义迁入

## 目标

从 Claude Code 的 TaskListV2、permissions、StatusNotices、Doctor 等区域吸收成熟交互语义，
落实到 UI-11/12/13，而不是建立第二套后端状态。

## 子模块

- CC-03.1 Behavior inventory：键位、焦点、loading、empty、error、detail、cancel。
- CC-03.2 Semantic mapping：每个 source state 映射到 Naumi protocol 字段。
- CC-03.3 Component adaptation：只消费 Bridge view model，不 import Python internals。
- CC-03.4 Divergence log：Naumi 特有 Harness/Pursuit/browser/agent cluster 行为。
- CC-03.5 Golden scenarios：source-like 行为 fixture 与 Naumi 真实 Bridge fixture。
- CC-03.6 UX audit：中文、窄屏、无色彩、TUI fallback。

## 验收标准

- 每个迁入交互在 source path 和 target test 间可追踪。
- CC 中不存在的 Naumi 状态不能被隐藏或降级，例如 Harness blocked 与 browser needs_input。
- cancel/approve 仍调用 Python service；组件不直接发 shell/Git/Tool 命令。
- source 更新导致行为变化时 CC-05 报告差异，不自动改变产品。
- 用户测试能完成 task detail/cancel、permission rule explain、doctor export 三条真实流程。
