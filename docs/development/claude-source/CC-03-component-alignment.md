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

## 已完成前置

UI-13.2a 已建立 Provider 稳定诊断码，并由 Doctor Health typed payload、New UI、TUI 与 CLI 共用。
UI-13.5a 已完成 Doctor export 的 Naumi 侧产品合同：固定 bounded 文件集、预览、精确 Snapshot 摘要确认、
同一 Bundle 原子写入和本地回执。CC-03 迁入 Doctor/StatusNotices 交互时必须消费这些
code/domain/responsibility/export view model，不得解析中文错误文本、重新构造 ZIP，或把原始 Provider
异常带入组件。

CC-03.1a 已建立首批机器可校验的核心行为清单：固定 source identity 与 license scope，只以
`reimplement` 模式读取声明符号，并把 Task、Permission、Doctor 的三条 aligned 行为和三条
Naumi extension 追踪到目标实现与目标测试。详见
`CC-03-1a-core-behavior-inventory.md`。

CC-03.1b 已在 1a 治理边界上补齐 `3 × 8` 完整矩阵，覆盖 loading、empty、error、detail、cancel、
keyboard、focus、presentation；24 个单元格分别标记 aligned、Naumi extension 或明确 N/A，并由同一
失败关闭 verifier 校验真实 source/target/test 锚点。详见
`CC-03-1b-complete-behavior-matrix.md`。

因此 CC-03 现为 `partial`，CC-03.1 已完成。CC-03.2 protocol 字段语义映射、CC-03.3 组件适配、
CC-03.4 完整 divergence log、CC-03.5 同 fixture golden scenarios 与 CC-03.6 UX audit 仍未实现。
