# NaumiAgent CLI/TUI Claude Code 化路线图

本文档规划 NaumiAgent CLI/TUI 交互层的三阶段重构路线。目标不是复制 Claude Code 的源码，而是吸收其成熟产品结构：事件模型、消息组件、工具生命周期、权限交互、固定布局、长历史渲染、任务面板和诊断体系，并用 NaumiAgent 当前 Python 架构重实现。

## 总体目标

把当前 CLI/TUI 从“流式打印字符串 + 少量状态栏”升级为稳定的终端应用：

- 统一 UI 事件模型。
- 统一消息组件模型。
- 工具调用有生命周期、进度和结果摘要。
- 权限确认有明确交互路径。
- todo / task / subagent / background 状态常驻可见。
- `/resume` 后 UI 状态可重建。
- 长输出、大代码块、大 diff 不炸屏。
- debug log 可以定位 UI 和事件问题。
- CLI 和 TUI 行为一致。

## 参考架构

Claude Code README 中体现出的关键结构：

- `QueryEngine.ts`：流式模型调用、工具循环、thinking、重试、token 统计。
- `Tool.ts` / `tools/`：工具 schema、权限模型、进度状态。
- `components/`：消息、状态栏、工具、diff、任务 UI 组件。
- `screens/`：Doctor、REPL、Resume 等完整页面。
- `ink/`：终端渲染层。
- `keybindings/`：快捷键配置和解析。
- `tasks/` / `coordinator/`：任务和多 agent 协调。
- `plugins/` / `skills/`：插件和技能系统。

NaumiAgent 的迁移原则：

- 不引入 Claude Code 专有源码。
- 不强行把 Python 项目改成 TypeScript / Bun / Ink。
- 把 Claude Code 的架构模式映射到 Naumi 的 `prompt_toolkit` CLI 和 Textual TUI。
- 先建立 UI message model，再做各类渲染功能。
- 每个功能独立实现、独立验证、独立提交。

## 阶段一：可用版，3-5 天

目标：解决当前真实使用中最痛的 CLI/TUI 问题。完成后，日常使用不再出现“看起来卡死、渲染覆盖、权限没路走、输出淹没输入”等问题。

### 1. 统一 UI 事件协议

当前问题：

- engine 直接发 `token`、`tool_start`、`tool_end`、`task_snapshot` 等事件。
- CLI/TUI 各自用 if/elif 临时处理，业务语义和渲染逻辑混在一起。
- 输出区、状态栏、工具调用、todo、permission 互相影响。
- `/resume` 恢复的是文本，不是 UI 状态。

改造方案：

- 定义 `UIEvent` / `UIMessage` 基础结构。
- engine 底层事件先进入 UI adapter。
- adapter 将底层事件转换为稳定消息类型：
  - `UserMessage`
  - `AssistantMessage`
  - `ThinkingMessage`
  - `ToolUseMessage`
  - `ToolResultMessage`
  - `PermissionRequestMessage`
  - `PermissionResultMessage`
  - `TodoStatusMessage`
  - `RuntimeStatusMessage`
  - `ErrorMessage`
  - `SystemNoticeMessage`
  - `ContextCompactMessage`
  - `RecoveryMessage`
- CLI/TUI 消费同一套 message model。
- 保持旧事件兼容，避免一次性重写 engine。

建议文件：

- `src/naumi_agent/ui/messages/base.py`
- `src/naumi_agent/ui/messages/events.py`
- `src/naumi_agent/ui/messages/adapter.py`
- `tests/unit/test_ui_message_adapter.py`

验收标准：

- 同一段 engine event 在 CLI/TUI 下展示语义一致。
- 新增消息类型不需要同时修改多个事件 handler。
- adapter 不保存大块工具参数正文，只保存摘要和引用。

### 2. 工具调用独立 card 化

当前状态：

- CLI 已有第一步：`tool · running/success` card。
- TUI 尚未完整 card 化。
- 工具准备、执行、结果、错误仍缺少统一模型。

目标状态：

每个工具调用有统一生命周期：

- `preparing`
- `awaiting_permission`
- `running`
- `success`
- `error`
- `skipped`
- `blocked`
- `aborted`

card 内容：

- 工具名。
- 图标。
- 主要参数摘要。
- 当前状态。
- 耗时。
- 输出摘要。
- 错误摘要。

工具专用摘要：

- `file_write`：文件路径、行数、大小。
- `file_edit`：文件路径、变更行数、diff 摘要。
- `bash_run`：命令、退出码、stdout/stderr 摘要。
- `code_execute`：语言、耗时、输出摘要。
- `web_search`：query、结果数。
- `web_fetch`：URL、响应摘要。
- `spawn_agent`：agent 名称、状态。
- `todo_write`：完成数、当前任务。

建议文件：

- `src/naumi_agent/ui/tool_activity.py`
- `src/naumi_agent/ui/tool_summary.py`
- `src/naumi_agent/cli/renderers/tool.py`
- `src/naumi_agent/tui/renderers/tool.py`
- `tests/unit/test_tool_summary.py`
- `tests/unit/test_cli_rendering.py`
- `tests/unit/test_tui.py`

验收标准：

- `file_write` 大文件生成期间持续显示准备进度。
- 工具失败时有清晰失败原因，不再只有“失败 0ms”。
- 工具输出不会直接淹没主对话。
- CLI/TUI 对同一工具显示相同摘要信息。

### 3. 长代码块、diff、文件写入摘要展示

当前问题：

- 模型输出长代码块会全量刷屏。
- 大 diff 仍然容易占满屏幕。
- `file_write` 用户只需要知道写了哪里、写了多大，不需要看到完整正文。

改造方案：

- Markdown 代码块默认摘录：
  - 展示前 40-80 行。
  - 显示隐藏行数。
  - 显示语言。
- `file_write` 默认展示：
  - 文件路径。
  - 总行数。
  - 字节数。
  - 前几行预览。
  - 是否成功写入。
- `file_edit` 默认展示：
  - 文件路径。
  - hunk 数。
  - additions / deletions。
  - 默认折叠大 diff。
- CLI 用 Rich `Panel + Syntax`。
- TUI 用 `Markdown` / `Static`，避免长内容撑坏布局。

验收标准：

- 让 agent 写 1000 行 HTML，屏幕不会刷烂。
- diff 有颜色、有边界、有摘要。
- 用户能判断写入是否成功、写入哪里、写了多大。

### 4. 底部布局稳定化

当前问题：

- 状态栏、todo、输入框、输出互相覆盖。
- 终端 resize 后底部栏宽度和位置不稳定。
- 滚动历史后底部信息被输出内容覆盖。

目标布局：

CLI 底部固定分区：

1. `activity bar`：当前工具/模型准备进度。
2. `todo bar`：当前任务进度。
3. `status bar`：mode、model、workspace、token、budget、git。
4. `input box`：输入区域。

行为规则：

- 输出区永远不能写入底部固定区域。
- resize 后重新计算宽度。
- 所有栏位使用裁剪/省略，不能溢出。
- 手动滚动历史时停止自动追底。
- 回到底部后恢复 auto-scroll。
- TUI 保持同样分区模型。

验收标准：

- 放大/缩小窗口 10 次，底部栏不乱。
- 输出大量内容后，输入框不被覆盖。
- 滚动历史时不出现双滚动条错觉。

### 5. 权限模式和 permission prompt 闭环

当前问题：

- 有些工具需要确认，但用户没有清晰 y/n 路径。
- `Shift+Tab` 模式切换已有雏形，但还需要产品化。
- 模型不知道当前模式时容易误判。

基础模式：

- `default`：高风险工具需要确认。
- `plan`：只读，不允许写文件/执行命令。
- `bypass`：自动允许。

后续可扩展：

- `auto`：低风险自动，高风险确认。

permission prompt 固定显示：

- 工具名。
- 风险级别。
- 参数摘要。
- 原因。
- 操作提示：
  - `y` allow once
  - `n` deny
  - `Shift+Tab` bypass

plan 模式拒绝时提示：

- 当前是 plan 模式。
- 只允许只读工具。
- 切换 bypass 后可继续执行。

验收标准：

- `code_execute` / `bash_run` / `file_write` 在 default 下能弹出确认。
- 用户能用 `y/n/Shift+Tab` 完成闭环。
- 模型不会一直说“我无法执行，只能告诉你”。

### 6. `/resume` 渲染恢复修复

当前问题：

- `/resume` 后再次对话会覆盖。
- 右侧出现两个进度条。
- 底部信息栏渲染异常。

改造方案：

- resume 不再只把历史文本塞回输出区。
- 从历史记录恢复成 message list：
  - user message
  - assistant message
  - tool message
  - task snapshot
  - compact boundary
- 恢复后重建 UI 状态：
  - 清空 transient live 区。
  - 清空 activity bar。
  - 根据 task store 恢复 todo bar。
  - 重新绑定 scroll state。
  - 防止重复创建 scrollbar / output window。

验收标准：

- `/resume` 后继续输入，不覆盖旧内容。
- 不出现双滚动条。
- 底部栏正常。

### 7. 结构化 debug log 接入 UI 层

需要记录：

- 原始 engine event。
- 转换后的 UIMessage。
- CLI/TUI 渲染目标。
- permission prompt lifecycle。
- resize / scroll / resume。
- 渲染异常。

验收标准：

- UI 出错时能通过 debug log 定位：
  - 哪个 engine event 触发。
  - adapter 生成了什么 UIMessage。
  - 哪个 renderer 出错。
  - 当前 layout state 是什么。

### 阶段一测试策略

不跑全量测试。每个功能跑定向测试：

- `tests/unit/test_cli_rendering.py`
- `tests/unit/test_tui.py`
- `tests/unit/test_engine.py` 中相关流式事件测试。
- 新增 `tests/unit/test_ui_message_adapter.py`。

真实场景验证：

- 创建大 HTML 文件。
- 执行失败命令。
- 权限确认。
- resize。
- resume 后继续对话。
- todo_write 后底栏常驻。

### 阶段一完成标准

- 当前截图里的渲染问题全部消失。
- CLI/TUI 日常可用。
- 每个子功能独立 commit。
- 不引入全量重构造成的主功能回归。

## 阶段二：成熟版，1-2 周

目标：从“可用”升级成“成熟终端应用”。重点是建立长期可扩展的 UI 架构。

### 1. Message Component Registry

设计：

- `UIMessage` 只表达语义，不关心 CLI/TUI。
- CLI renderer 和 TUI renderer 各自注册。
- 新消息类型只新增 renderer，不改大 if/elif。

建议结构：

```text
src/naumi_agent/ui/messages/
├── base.py
├── registry.py
├── adapter.py
└── types.py

src/naumi_agent/cli/renderers/
├── base.py
├── assistant.py
├── thinking.py
├── tool.py
├── permission.py
├── task.py
└── system.py

src/naumi_agent/tui/renderers/
├── base.py
├── assistant.py
├── thinking.py
├── tool.py
├── permission.py
├── task.py
└── system.py
```

消息类型：

- `UserMessage`
- `AssistantMessage`
- `ThinkingMessage`
- `ToolUseMessage`
- `ToolResultMessage`
- `PermissionRequestMessage`
- `PermissionResultMessage`
- `TaskSnapshotMessage`
- `SubagentEventMessage`
- `BackgroundTaskMessage`
- `ContextCompactMessage`
- `RecoveryMessage`
- `ErrorMessage`
- `SystemNoticeMessage`

验收标准：

- 新增消息类型不需要改 CLI/TUI 主循环。
- CLI/TUI renderer 行为可分别测试。
- 历史恢复可以基于 message list 重放。

### 2. Virtualized History Rendering

问题：

- 长对话会让 CLI/TUI 越来越重。
- 大量 ANSI 字符串拼接不可控。
- resume 大历史容易卡。

CLI 方案：

- 输出区维护 message list。
- 根据 scroll offset 渲染可见窗口。
- 每条 message 缓存 rendered lines。
- 宽度变化时只重新渲染受影响 message。
- auto-scroll 和 manual-scroll 状态分离。

TUI 方案：

- 长历史分段挂载。
- 大消息默认折叠。
- 只渲染可见区域附近内容。

验收标准：

- 1000 条消息历史滚动不卡。
- resume 大会话不会明显卡顿。
- 内存不随输出无限膨胀。

### 3. Command Palette / Quick Open

功能：

- `/` 命令补全增强。
- fuzzy search 命令。
- 命令显示：
  - 名称
  - 描述
  - 参数
  - 是否只读
  - 当前模式是否可用
- 可扩展到：
  - 最近文件
  - 最近任务
  - 历史会话
  - agents

CLI：

- prompt_toolkit completer 增强。

TUI：

- modal quick open。

验收标准：

- 输入 `/` 能自然查命令。
- 不需要记住几十个 slash command。
- 不可用命令有清晰原因。

### 4. Task / Todo / Subagent 面板

功能：

- 底部常驻显示当前任务摘要。
- `/todo` 或快捷键打开完整任务面板。
- todo 面板显示：
  - pending
  - in_progress
  - blocked
  - completed
- subagent 面板显示：
  - agent 名称
  - 当前任务
  - 状态
  - 最近事件
  - 输出摘要
- background task 面板显示：
  - command
  - status
  - runtime
  - latest output

验收标准：

- 多 agent 运行时，用户知道每个 agent 在干什么。
- todo 不再只是模型内部自嗨。
- background task 有可查询状态。

### 5. Permission System UI 产品化

阶段一解决“能用”，阶段二做“成熟”。

功能：

- permission history。
- 最近允许规则。
- 风险等级颜色：
  - read: green
  - write: yellow
  - execute: red
  - network: blue
- 支持 temporary allow：
  - 允许一次。
  - 允许本会话。
  - 允许该工具。
  - 切换 bypass。
- plan 模式下显示“只读锁”。

验收标准：

- 用户能理解为什么需要确认。
- 用户能快速切换模式。
- 权限状态在 UI 里始终清楚。

### 6. Diff Viewer 进阶

功能：

- file edit 后显示：
  - 文件路径
  - hunk 数
  - additions/deletions
  - 折叠/展开
- 大 diff 默认折叠。
- CLI 用单栏模拟 side-by-side 摘要。
- `/diff` 查看本轮改动。
- 支持 git dirty summary。

验收标准：

- 用户不用跑 `git diff` 也能知道 agent 改了什么。
- 大 diff 不刷屏。
- diff 渲染稳定支持中文和宽字符。

### 7. TUI 页面结构升级

目标：

TUI 不再只是 ChatPanel + StatusBar，而是完整应用。

布局：

- 顶部或侧边：session/task/status。
- 中央：message list。
- 底部：activity/todo/status/input。
- modal：
  - command palette
  - permission
  - task detail
  - debug viewer
  - history/resume
  - settings

验收标准：

- TUI 成为真正的应用，不只是美化版 CLI。
- 常用操作不用记忆命令。

### 8. Debug Log Viewer

功能：

- `/debug` 打开最近 debug runs。
- 查看：
  - engine events
  - UI messages
  - render errors
  - permission decisions
  - tool lifecycle
- 支持导出诊断包。

验收标准：

- UI 出错后，不靠截图定位。
- 用户能复制一份 debug 信息给 agent。

### 阶段二测试策略

- renderer snapshot 测试。
- fake terminal resize 测试。
- resume replay 测试。
- message registry 单测。
- Textual `run_test` 关键路径测试。

### 阶段二完成标准

- CLI/TUI 架构稳定。
- 新增 UI 类型不再到处改 handler。
- 长会话、任务、多 agent 可观察。

## 阶段三：产品级版，2-4 周

目标：接近 Claude Code 的“无感成熟”。这一阶段重点是 polish、性能、可配置性、跨平台兼容和诊断能力。

### 1. Ink-like 渲染抽象

不一定真的引入 Ink，但需要类似概念：

- layout tree
- render node
- style token
- width measurement
- ANSI diff update
- scroll state
- focus state
- keybinding dispatch

CLI 可以继续基于 prompt_toolkit，但在其上形成自己的抽象：

- `LayoutNode`
- `RenderableMessage`
- `Viewport`
- `StickyRegion`
- `FocusTarget`
- `KeyEvent`

收益：

- CLI/TUI 共享更多布局逻辑。
- 更容易修 resize / scroll / input 覆盖问题。
- 未来可替换底层渲染器。

### 2. Keybinding System

默认快捷键：

- `Shift+Tab`：mode cycle。
- `Ctrl+L`：clear。
- `Ctrl+Y`：copy transcript。
- `PageUp/PageDown`：scroll。
- `Ctrl+R`：history search。
- `Ctrl+P`：command palette。
- `Ctrl+D`：exit。
- `Esc Esc`：interrupt。

用户配置：

- YAML / TOML / JSON。
- 冲突检测。
- 快捷键帮助面板。

验收标准：

- 快捷键不散落在 CLI/TUI 各处。
- 用户能配置。
- 冲突有明确提示。

### 3. Theme / Output Style System

theme：

- dark
- minimal
- high contrast

output style：

- compact
- detailed
- debug
- silent tools

要求：

- 支持配置文件。
- 状态栏、card、diff、permission 统一色彩 token。
- CLI/TUI 使用同一组语义 token。

验收标准：

- UI 风格统一。
- 不再到处硬编码颜色。

### 4. Resume / History Screen

功能：

- `/resume` 不只是命令。
- 提供会话选择界面：
  - 时间
  - 标题
  - 模型
  - token
  - cost
  - workspace
  - git branch
  - 摘要
- 支持 search。
- 支持 preview。
- 支持删除/归档。

验收标准：

- 用户能自然恢复历史会话。
- 恢复后 UI 状态完整。

### 5. Doctor / Diagnostics

检查项：

- Python 环境。
- config 文件。
- API key。
- model provider。
- workspace 权限。
- git 状态。
- ripgrep 是否可用。
- browser daemon。
- docker。
- MCP servers。
- debug log 写入权限。
- terminal capability。
- unicode / width 支持。

输出：

- pass / warn / error。
- 修复建议。
- 可复制诊断报告。

验收标准：

- 用户遇到环境问题时先跑 `/doctor`。
- agent 自己也能调用 doctor 诊断。

### 6. Performance 优化

重点：

- 首字延迟。
- 大历史 resume。
- 大代码块渲染。
- TUI scroll。
- tool prepare progress。

措施：

- 启动预取：
  - config
  - git info
  - model capability
  - memory db
- lazy loading：
  - heavy commands
  - browser daemon
  - analytics/debug viewer
- render cache：
  - message hash
  - width-sensitive cache
  - syntax highlight cache
- throttle：
  - token render batch
  - tool prepare update
  - status bar update
- backpressure：
  - 模型流太快时 UI 批量刷新。

验收标准：

- 小任务输入后 UI 立即有反馈。
- 大输出不掉帧。
- resume 10 万行历史仍可接受。

### 7. 多平台终端兼容

覆盖：

- macOS Terminal。
- iTerm2。
- VS Code terminal。
- Cursor / Trae terminal。
- Linux terminal。
- tmux。
- 宽字符 / CJK。
- emoji width。
- 低色彩终端。

措施：

- width 测量统一。
- emoji fallback。
- ANSI capability detection。
- status line 裁剪稳定。

验收标准：

- 中文、emoji、边框不把布局撑乱。
- 状态栏不因宽字符错位。

### 8. UI E2E 测试体系

建议目录：

```text
tests/e2e/ui_scenarios/
├── large_file_write.yaml
├── permission_confirm.yaml
├── resume_replay.yaml
├── large_diff.yaml
├── subagent_events.yaml
└── terminal_resize.yaml
```

每个 scenario：

- 输入一组 engine events。
- 渲染 CLI/TUI。
- 断言关键文本和布局边界。
- 保存失败快照。

验收标准：

- UI 改动不会反复回归。
- 截图类 bug 能通过 replay 复现。

## 推荐执行顺序

严格按以下顺序做，避免继续形成散乱补丁：

1. `UIEvent -> UIMessage adapter`
2. CLI message renderer registry
3. TUI message renderer registry
4. tool card 全生命周期
5. file_write / file_edit / diff 摘要
6. permission prompt 产品化
7. sticky bottom layout 稳定
8. resume replay 修复
9. todo/task/subagent 面板
10. virtualized history
11. command palette
12. debug viewer
13. theme/keybinding/config
14. doctor
15. 性能和跨终端兼容

前 4 个是地基。没有 message model，todo、permission、diff 会继续变成一堆 if/elif 和字符串拼接。

## 提交策略

按项目准则，一个功能一个 commit。

阶段一建议 commits：

1. `refactor: add ui message event adapter`
2. `feat: add cli message renderer registry`
3. `feat: add tui message renderer registry`
4. `feat: render tool lifecycle messages`
5. `feat: summarize file write and edit output`
6. `feat: stabilize cli sticky bottom layout`
7. `feat: improve permission prompt flow`
8. `fix: replay resumed sessions as ui messages`
9. `test: add ui event replay scenarios`

阶段二建议 commits：

1. `feat: add virtualized cli message history`
2. `feat: add task and subagent panels`
3. `feat: add command palette`
4. `feat: add debug log viewer`
5. `feat: add structured diff viewer`

阶段三建议 commits：

1. `feat: add configurable keybindings`
2. `feat: add theme and output style system`
3. `feat: add resume history screen`
4. `feat: add doctor diagnostics screen`
5. `perf: cache message rendering`
6. `test: add terminal ui e2e scenarios`

## 风险与约束

### 1. prompt_toolkit 和 Textual 双 UI 维护成本高

必须用共享 message model 降低成本，否则 CLI/TUI 会长期分叉。

### 2. 过早大重构会影响主功能

阶段一只改 UI 事件和渲染，不碰工具真实逻辑。

### 3. 长历史虚拟化容易引入滚动 bug

必须有 replay 测试，否则以后会反复回归。

### 4. 中文、emoji、terminal width 是硬问题

需要统一 width 测量和裁剪，不能每个组件自己算。

## 当前状态

当前分支：

```text
feat/ui-message-adapter
```

已完成（阶段一）：

- ✅ 统一 UI 事件协议：`UIMessage` / `EngineEventAdapter` / 完整 dispatch table。
- ✅ CLI/TUI message renderer registry：`CLIRenderer` 表驱动，新增类型不改主循环。
- ✅ 工具调用独立 card 化：`ToolCardSummary` + tool-specific extractors。
- ✅ 长代码块/diff/文件写入摘要：`code_excerpt` / `file_summary_renderer`。
- ✅ 底部布局稳定化：`BottomBarState` + `clip_to_width` + output guard。
- ✅ 权限 prompt 闭环：`PermissionBubbleMessage` + y/n/Shift+Tab。
- ✅ Resume 渲染恢复：`replay_messages()` 将 session 历史转为 UIMessage 走 renderer。
- ✅ Debug trace 集成：`DebugTrace` 记录所有 engine event + UIMessage + 渲染异常。

已完成（阶段二，部分）：

- ✅ 结构化 Task Status Renderer：todo bar / agent status / background task / summary bar。
- ✅ Command palette 增强：fuzzy search + category + readonly 标记 + arg hint。

下一步：

```text
feat: add virtualized cli message history
```

或

```text
feat: add debug log viewer
```

