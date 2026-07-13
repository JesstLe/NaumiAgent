# NaumiAgent CLI/TUI Claude Code 化路线图

本文档规划 NaumiAgent CLI/TUI 交互层的三阶段重构路线。早期假设是不引入 Claude Code 源码；当前用户已确认本地 `/Users/lv/Workspace/claude-code` 可开源复用，因此路线调整为：审查并映射迁入 Claude Code 的终端 UI 结构、状态模型、组件设计和交互细节，同时保持 NaumiAgent 的 Python `AgentEngine/tools/memory/safety/task/background/debug_trace` 作为后端能力核心。

本轮源码审计和迁入映射见 `docs/14-claude-code-source-audit.md` 与 `frontend/terminal-ui/cc-source-map.json`。

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

- 已确认可复用的 Claude Code 源码可以作为 UI 基座或组件参考迁入。
- Python 引擎、工具、记忆、安全、任务和 debug trace 仍归 NaumiAgent 后端所有。
- 前后端通过稳定 UI Event Protocol 解耦；后续可替换 renderer，而不重写 AgentEngine。
- 第一阶段先用轻量 Node terminal frontend 跑通完整链路；后续评估是否直接引入 React/Ink renderer。
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
codex/docs-13-claude-code-roadmap
```

已完成（阶段一）：

- ✅ 统一 UI 事件协议：`UIMessage` / `EngineEventAdapter` / 完整 dispatch table。
- ✅ 共享 UI Event Protocol 契约：`frontend/terminal-ui/protocol-contract.json` 固化 client/server 事件清单和阶段一关键 `ui/message` 类型字段，Node 前端运行时校验 bridge 输出，Python 回归测试校验枚举与 UIMessage dataclass 字段一致性，并随 wheel 一起打包。
- ✅ Python bridge 进程级验证：terminal-ui 测试通过 `uv run python test/fixtures/python-bridge-fixture.py` 启动真实 `JsonlEngineBridge`，覆盖 submit、权限确认、tool card、task panel 和 resume replay 的 stdio JSONL 链路。
- ✅ Legacy fallback 命令级验证：`naumi ui` 默认启动新 terminal-ui，`naumi ui --legacy` 明确进入旧 Textual TUI；新 UI 启动失败时提示 `naumi ui --legacy` / `naumi chat --tui`。
- ✅ CLI/TUI message renderer registry：`CLIRenderer` 表驱动，新增类型不改主循环。
- ✅ 工具调用独立 card 化：`ToolCardSummary` + tool-specific extractors。
- ✅ 工具准备阶段动态可视化：terminal-ui 将 `tool_prepare_start/snapshot/end` 的真实 path、argument chars、content chars、content lines、elapsed ms 渲染为 activity card；工具完成后同一份 prepare progress 摘要保留在最终 tool card 中，避免快速写文件时用户看不到变化。
- ✅ 真实工具事件顺序与缓存稳定性：terminal-ui 支持生产链路中的 `tool_prepare_start -> snapshot -> end -> tool_use` 顺序，`prepare_end` 会保留到后续 tool card 消费；`tool_prepare.tool_call_id` 与 `tool_use.tool_call_id` 会精确匹配，ID 不一致时不会把准备摘要挂到错误工具；render cache key 纳入 activity/tool prepare phase 和 metrics，连续工具不会串用上一项准备摘要。
- ✅ 运行活动组与终态收据：Terminal UI 按真实 Bridge 事件把一次运行聚合为唯一 `run_activity` 卡，展示阶段、工具、权限和后端耗时；成功、失败或取消后释放 active pointer，并把同一张收据置于时间线末尾，长工具输出不会遮住最终结果。
- ✅ 长代码块/diff/文件写入摘要：`code_excerpt` / `file_summary_renderer`。
- ✅ 底部布局稳定化：`BottomBarState` + `clip_to_width` + output guard。
- ✅ 权限 prompt 闭环：`PermissionBubbleMessage` + y/n/Shift+Tab。
- ✅ Runtime mode 端到端语义：`default -> plan -> bypass` 通过 Shift+Tab 循环；`plan` 映射 Python 权限层 `strict` 并阻断写入类工具，`bypass` 映射 `bypass`；terminal-ui 进程测试通过真实 Python `JsonlEngineBridge` fixture 验证 `mode/changed.status.permission_mode`。
- ✅ Resume 渲染恢复：`replay_messages()` 将 session 历史转为 UIMessage 走 renderer。
- ✅ Debug trace 集成：`DebugTrace` 记录所有 engine event + UIMessage + 渲染异常。
- ✅ 真实 Engine/Bridge 工具生命周期验证：`tests/unit/test_ui_bridge.py` 使用真实 `AgentEngine.run_streaming()`、patched router 和 patched tool execution，验证 `submit -> tool_prepare -> tool_use -> tool_result -> run_completed` 能完整穿过 `JsonlEngineBridge`，不依赖外部 API。

已完成（阶段二）：

- ✅ 结构化 Task Status Renderer：todo bar / agent status / background task / summary bar。
- ✅ Command palette 增强：fuzzy search + category + readonly 标记 + arg hint。
- ✅ Virtualized CLI message history：`VirtualizedCLIHistory` + `VirtualizedHistoryControl`，CLI 输出区按行懒渲染，保留 PageUp/PageDown、live/finalize、resume replay 行为。
- ✅ Debug log viewer：`/debug` 展示当前日志路径 + 最近 debug-runs 索引，`/debug-replay` 回放结构化事件，`/copy last|error` 导出诊断片段。
- ✅ Task / Todo / Subagent 面板：`ui.task_panel` 聚合持久 todo、subagent 生命周期/事件、权限冒泡、background 任务和 browser runs；CLI `/tasks` 与 TUI 任务侧栏共用同一套快照/渲染逻辑。
- ✅ Task panel 前端选择交互：terminal-ui 从 `/tasks` 内容抽取可选任务 ID，支持 `/tasks select|next|prev|open|jump` 和空输入 `Tab` / `Enter` 选择并打开详情；`/tasks jump [id]` 展示 background output 或 browser artifacts/reports 的真实记录路径；`/tasks cancel [id]` 通过 `task_cancel` 协议真实取消 background/browser 任务，todo 删除不混作取消。
- ✅ Task panel 焦点与动作栏：任务面板聚焦时支持空输入 `Tab/n` 下一项、`p` 上一项、`Enter/o` 详情、`j` 记录、`x` 取消、`Esc` 退出焦点；失焦后这些按键恢复为普通输入。
- ✅ Task panel 轻量事件流展开：`e` 或 `/tasks expand [id]` 展开任务项结构化字段为 `event flow`，`c` 或 `/tasks collapse [id]` 折叠；展开/选中/focus 状态参与 render cache key，避免 UI 状态变化后复用旧渲染。
- ✅ Task panel 跨来源 Timeline：`ui.task_panel` 从 todo、subagent events、permission bubbles、background tasks、browser runs 生成统一 `Timeline` 区段，并继承 source/status 过滤；terminal-ui 将其作为可选、可展开任务行渲染。
- ✅ Task panel Timeline 来源折叠：terminal-ui 在 Timeline 区段显示来源计数，支持 `/tasks timeline collapse|expand|toggle <source>` 和 `/tasks timeline clear` 本地折叠高噪声来源；折叠状态参与 render cache key。
- ✅ Task panel 有界渲染：真实屏幕渲染时按 body 高度截断超长面板，保留顶部 `tasks` 标题、摘要、前部关键行和隐藏行数提示，避免 `/tasks` 新打开后视图追到面板尾部。
- ✅ Structured diff viewer：`ui.diff_viewer` 解析 git unified diff，按文件展示 hunk/additions/deletions、折叠大 diff、标出未跟踪文件；CLI/TUI 支持 `/diff [all|worktree|staged]`。

已完成（阶段三）：

- ✅ Configurable keybindings：`ui.keybindings` 统一定义快捷键动作、默认值、配置覆盖、冲突检测和帮助渲染；CLI 的 prompt_toolkit `KeyBindings` 与 TUI 的 Textual `Binding` 均从同一配置生成，支持 `/keybindings` 查看当前生效按键。
- ✅ Theme and output style system：`ui.theme` 提供 dark/minimal/high_contrast 主题、compact/detailed/debug/silent_tools 输出策略和语义色彩 token；CLI 状态/权限、TUI CSS、结构化 diff 和 `/style` 入口共用同一套配置。
- ✅ Resume history screen：`ui.history_screen` 统一历史会话列表/预览渲染，Session 持久化 workspace/git/summary 元数据，`/history <关键词>` 支持搜索，`/history preview|archive|delete <id>` 支持预览、归档和删除；TUI 历史侧栏展示模型、token、cost、workspace、git、摘要并支持归档/删除。
- ✅ Doctor diagnostics screen：`ui.doctor` 提供 Python/config/API key/model/workspace/git/rg/browser daemon/docker/MCP/debug log/terminal 的确定性检查，CLI/TUI `/doctor` 与 Agent 工具 `doctor_diagnostics` 共用同一套诊断与 Markdown 报告。
- ✅ Cache message rendering：`ui.render_cache` 提供 bounded LRU 与统计；CLI renderer 按 `message_id` 缓存 ANSI 输出，TUI renderer 对重复 message id 做幂等跳过，renderer override 会清空缓存避免旧结果污染。
- ✅ Terminal UI E2E scenarios：`tests/e2e/ui_scenarios/*.yaml` 覆盖大文件写入、权限确认、历史恢复、大 diff、subagent/team/recovery、终端 resize；`tests/e2e/test_ui_scenarios.py` 用真实 `EngineEventAdapter`、CLI/TUI renderer、virtualized history、structured diff viewer 和宽字符裁剪逻辑回放并断言关键文本/viewport 边界。

下一步：

```text
阶段三已完成。后续可进入跨终端兼容实测、性能基准和真实终端截图回归。
```
