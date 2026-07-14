# NaumiAgent Next Terminal UI Integration

## 目标

本阶段把终端界面从 Python 内部渲染逐步拆成独立前端：

- Python 继续拥有 `AgentEngine`、工具、记忆、安全、任务、后台任务和调试日志。
- 新终端前端只消费稳定 JSONL 事件协议，不直接访问 Python 内部对象。
- 旧 Python CLI/TUI 保留为 legacy fallback。

## 已审查的本地源码

### `/Users/lv/Workspace/claude-code`

可复用重点：

- `src/screens/REPL.tsx`：顶层交互协调，统一处理消息、权限、任务、恢复、状态栏和输入。
- `src/components/StatusLine.tsx`：底部状态栏按终端宽度截断/右对齐，避免窗口变化后覆盖正文。
- `src/components/permissions/*`：权限请求是 UI 状态，不应只作为模型文本输出。
- `src/components/FileEditToolDiff.tsx`、`src/components/HighlightedCode.tsx`、`src/components/Markdown.tsx`：代码块和 diff 有专门渲染路径，不全量灌入普通文本。
- `src/bridge/*`：前后端/远程会话通过显式事件桥接，而不是让 UI 读内部状态。

### `/Users/lv/Workspace/pi-upstream/packages/coding-agent`

可复用重点：

- `modes/rpc/jsonl.ts`：严格 JSONL 分帧，只按 `\n` 切记录。
- `modes/interactive/components/tool-execution.ts`：工具调用是可更新卡片，支持 running/success/error 和输出折叠。
- `modes/interactive/components/footer.ts`：footer 根据宽度裁剪，不占用正文渲染区域。
- `modes/interactive/components/diff.ts`：diff 使用独立 renderer，支持增删行颜色。

## NaumiAgent UI Event Protocol

第一版协议实现位于：

- `src/naumi_agent/ui/protocol.py`
- `src/naumi_agent/ui/bridge.py`

启动桥接：

```bash
uv run naumi-ui-bridge --config config.yaml
```

协议为 stdin/stdout JSONL。客户端发送：

```json
{"id":"ui-1","type":"submit","version":1,"payload":{"text":"你好"}}
```

服务端输出：

```json
{"type":"ui/message","version":1,"payload":{"type":"assistant_stream","phase":"token","content":"..."}}
```

核心服务端事件：

- `ready`
- `debug/trace`
- `runtime/status`
- `mode/changed`
- `user/message`
- `task/created`
- `workbench/snapshot`
- `ui/message`
- `engine/event`
- `permission/request`
- `permission/resolved`
- `run/started`
- `run/completed`
- `run/cancelled`
- `session/replayed`
- `error`

核心客户端事件：

- `hello`
- `submit`
- `task_submit`
- `run_cancel`
- `set_mode`
- `cycle_mode`
- `permission_response`
- `resume`
- `ping`
- `shutdown`

## 新终端前端

第一版前端位于：

- `frontend/terminal-ui/package.json`
- `frontend/terminal-ui/src/index.js`
- `frontend/terminal-ui/src/protocol.js`
- `frontend/terminal-ui/src/state.js`
- `frontend/terminal-ui/src/render.js`
- `frontend/terminal-ui/src/components/`
- `frontend/terminal-ui/src/ansi.js`

启动：

```bash
naumi --config .naumi/config.yaml
```

`naumi` 会由 Python 命令层直接拉起 `frontend/terminal-ui/src/index.js` 并连接
`naumi_agent.ui.bridge`。启动时会保留用户当前工作目录；前端 UI state、
debug log 和 Python bridge 的相对 `--config` 解析都以这个目录为准。启动状态机为：

- Node UI 返回 `0` 时正常结束；
- 返回 `130` 或 `143` 时视为用户中断或进程终止，不启动另一个界面；
- Node/资源预检失败、spawn 失败或其他非零退出时，显示安全的中文原因并只回退一次到 Textual；
- Textual 也无法启动时返回 `1`，不会递归重试；
- `naumi tui` 直接启动 Textual，不探测 Node。

开发态启动时，launcher 优先使用仓库根目录下的 `frontend/terminal-ui`；wheel/pip
安装态启动时，则使用打包进 `naumi_agent/frontend/terminal-ui` 的运行时前端资源。
wheel 只包含 `package.json` 和 `src/`，不会把前端测试文件打包进用户安装环境。
正式入口 `naumi` 会把当前 Python 解释器通过 `--bridge-command-json` 传给
前端，用 `python -m naumi_agent.ui.bridge` 启动 bridge，避免安装态依赖 `uv`
或误用其它 Python 环境。前端源码直接开发调试时，如果没有传 bridge command，
仍保留 `uv run python -m naumi_agent.ui.bridge` 作为本地 fallback。

```bash
naumi tui
```

`naumi chat`、`naumi ui` 与 `naumiagent` 暂时保留为默认入口兼容别名；根 `--tui`、
`chat --tui` 和 `ui --legacy` 暂时保留为 Textual 迁移别名。Prompt Toolkit 旧 CLI 的
源码、测试和必要依赖继续保留，但 `--classic` 已不再是公共命令。

当前能力：

- 全屏独立渲染，不和普通 stdout 抢输入区。
- 通过 JSONL 连接 Python bridge。
- assistant streaming 增量渲染。
- thinking 折叠为一行摘要。
- 工具调用使用独立卡片，支持 running/success/error。
- 每次运行使用一条 `run_activity` 卡聚合后端阶段、工具计数、权限等待和终态；完成后同一张收据移动到时间线末尾，独立工具卡仍保留证据。
- `tool_prepare` 使用 activity card 展示准备阶段；最终 tool card 会保留 prepare 摘要，避免快速工具事件被 viewport 吞掉。
- background runtime notification、subagent、team、hook、context compact、recovery、error 事件使用结构化事件卡片渲染，不再裸露 JSON。
- tool card、permission footer、todo footer、status footer、Markdown/diff 已拆成轻量组件层。
- todo 常驻 footer，完成后由 `open_count=0` 清除。
- footer 根据窗口宽度截断。
- 输入栏使用独立 input buffer，支持批量粘贴、Unicode 字符、Backspace/Delete、Left/Right、Home/End、Ctrl+A/Ctrl+E 与 Up/Down 历史导航。
- `Shift+Tab` 发送 `cycle_mode`，底栏显示 `default / plan / bypass`。
- 输入提示使用 `chat >` / `task >` 区分对话与任务意图；`Ctrl+T` 可切换，任务被 Bridge 接受后自动回到对话。
- `/task <内容>` 与 `/task create <内容>` 创建真实 Workbench Issue 和 backing Task；`/task <id>` 打开已有任务详情，`/tasks` 打开任务页。
- 任务提交与普通对话共用当前会话、权限模式、AgentEngine 和时间线；任务 ID、Mission ID、终态与错误均通过协议关联。
- 权限请求出现后可按 `y` 允许、`n` 拒绝、`b` bypass。
- 运行中第一次 `Ctrl+C` 发送 `run_cancel` 并显示“正在停止”；取消完成后保留当前 UI 与会话，第二次 `Ctrl+C` 才强制退出。
- Markdown 代码块默认只展示前 40 行。
- unified diff 使用增删行颜色。
- 代码块和 diff 使用稳定 fold key，支持通过前端 fold state 展开。
- `/folds` 列出可折叠项，`/fold [n]` 切换折叠，`/expand [n|all]` 展开，`/collapse [n|all]` 折叠。
- fold state 与 scroll offset 按 session id 保存到工作区 `.naumi/terminal-ui-state.json`，恢复会话时自动应用。
- 历史消息渲染有前端 LRU 缓存，streaming 内容、tool result、fold state、宽度变化会自动失效。
- 主屏幕采用 viewport-aware body 渲染，底部附近重绘只渲染当前可见窗口和滚动缓冲，不再全量拼接历史消息。
- Python bridge 调试日志路径通过 `debug/trace` 展示；JS terminal-ui 额外写入 `.naumi/terminal-ui-debug.jsonl`，记录输入、协议收发、stderr、render 统计和渲染异常。可用 `NAUMI_TERMINAL_UI_DEBUG_LOG=0` 禁用，或设置为自定义路径。
- `/debug` 在前端直接展示 JS terminal-ui 日志、Python bridge events/transcript、run id、消息数、工具卡片数和当前 mode，bridge 掉线时也能用于定位前端问题。
- `/resume` 和 `/load <session_id>` 可回放历史消息。

## 当前不足

- `/resume` 目前是消息级 replay，已修复历史 assistant 消息拼接问题；滚动位置和 fold state 已在前端本地持久化，但尚未写入 Python 会话数据库。
- 新前端已有轻量组件层，但尚未迁入完整 Ink/pi-tui 组件体系。
- 前端状态、协议、组件、渲染、渲染缓存、viewport 窗口渲染已经拆分为可测试模块。
- 输入栏已支持 grapheme-aware 多行编辑、项目级 `Ctrl+R` 历史搜索和键盘斜杠补全。
- Bridge v1 仍缺少跨客户端幂等键、断线增量重放和 accepted task 的服务端 resume/link 协议；当前客户端不会自动重发 uncertain 请求。
- 工具调用卡片已优先通过稳定 `tool_call_id` 关联结果，缺失时才回退到 tool name。
- 代码高亮是内置轻量关键词高亮，不等价于 Pygments/Tree-sitter。

## 第一阶段验证

目标项由 `frontend/terminal-ui/test/phase-one-requirements.test.js` 显式覆盖：

- 完整对话渲染且 footer 不覆盖正文。
- 工具调用卡片使用 `tool_call_id` 关联结果，大 diff 默认折叠。
- `mode`、权限确认、todo、status footer 同屏渲染。
- 代码块和 diff 可列出、折叠、展开。
- `/resume` typed message replay 与本地 UI snapshot 恢复。

定向验证命令：

```bash
npm --prefix frontend/terminal-ui run check
npm --prefix frontend/terminal-ui test
uv run pytest tests/unit/test_ui_bridge.py tests/unit/test_ui_message_replay.py tests/unit/test_ui_message_adapter.py -q
```
