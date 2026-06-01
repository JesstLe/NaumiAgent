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
- `ui/message`
- `engine/event`
- `permission/request`
- `permission/resolved`
- `run/started`
- `run/completed`
- `session/replayed`
- `error`

核心客户端事件：

- `hello`
- `submit`
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
npm --prefix frontend/terminal-ui start -- --config config.yaml
```

当前能力：

- 全屏独立渲染，不和普通 stdout 抢输入区。
- 通过 JSONL 连接 Python bridge。
- assistant streaming 增量渲染。
- thinking 折叠为一行摘要。
- 工具调用使用独立卡片，支持 running/success/error。
- tool card、permission footer、todo footer、status footer、Markdown/diff 已拆成轻量组件层。
- todo 常驻 footer，完成后由 `open_count=0` 清除。
- footer 根据窗口宽度截断。
- `Shift+Tab` 发送 `cycle_mode`，底栏显示 `default / plan / bypass`。
- 权限请求出现后可按 `y` 允许、`n` 拒绝、`b` bypass。
- Markdown 代码块默认只展示前 40 行。
- unified diff 使用增删行颜色。
- 调试日志路径通过 `debug/trace` 展示。
- `/resume` 和 `/load <session_id>` 可回放历史消息。

## 当前不足

- `/resume` 目前是消息级 replay，尚未恢复滚动位置、折叠展开状态和工具卡内部 UI 状态。
- 新前端已有轻量组件层，但尚未迁入完整 Ink/pi-tui 组件体系。
- 前端状态、协议、组件、渲染已经拆分为可测试模块。
- 工具调用卡片已优先通过稳定 `tool_call_id` 关联结果，缺失时才回退到 tool name。
- 代码高亮是内置轻量关键词高亮，不等价于 Pygments/Tree-sitter。
