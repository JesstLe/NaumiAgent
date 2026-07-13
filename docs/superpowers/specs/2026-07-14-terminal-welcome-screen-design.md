# Terminal UI 启动欢迎页设计

## 状态

- 日期：2026-07-14
- 方案：渲染层专用空会话欢迎页
- 用户决定：选择方案 1；启动时显示，首条消息提交后自动收起
- 实施边界：一个独立功能、独立定向测试、独立提交

## 目标

新 Terminal UI 进入 alternate screen 后立即展示具有产品识别度的启动欢迎页。页面中央使用响应式巨大 `NAUMI` 字标；Python Bridge ready 后，从后端权威状态展示 NaumiAgent 版本、工作区、当前模型、runtime mode 和 permission mode。欢迎页不进入聊天消息、不写入会话存储，也不污染重放、搜索、完成收据或输入历史。

## 非目标

本切片不实现以下内容：

- Textual TUI 欢迎页；当前需求明确指向新 Terminal UI。
- 模型工作中的动态加载图像或动画；它是后续独立切片。
- 默认入口、旧 CLI 废弃或 TUI fallback；它们属于运行壳切片。
- 字体主题系统和全局配色重构；本切片只定义欢迎页所需的语义颜色。
- 预算默认值、模型供应商或工具能力改造。

## 当前实现证据

- `frontend/terminal-ui/src/index.js` 启动 Bridge、进入 alternate screen、发送 `hello` 并立即渲染。
- `frontend/terminal-ui/src/render.js` 负责按 body/footer 高度渲染对话主视口，但当前没有空状态组件。
- `frontend/terminal-ui/src/state.js` 在 `ready` 时合并状态，并额外写入“新终端 UI 已连接 Python bridge”系统消息。
- `src/naumi_agent/ui/bridge.py::status_payload()` 已提供工作区、模型、runtime mode、permission mode、预算、上下文和 Git 信息，但没有提供 NaumiAgent 版本。
- `src/naumi_agent/__init__.py` 的 `__version__` 是当前后端版本权威来源。

因此欢迎页必须复用现有 ready/status 数据流，不能让 Node 读取 `pyproject.toml`、硬编码版本或建立第二套运行状态。

## 用户体验

### 生命周期

欢迎页使用以下表现状态：

```text
booting -> ready_empty -> dismissed
    |            |           ^
    +-> error ---+-----------+
```

1. Terminal UI 初始化时进入 `booting`，立即显示巨大 `NAUMI` 和“正在启动本地运行时…”。
2. 收到 Bridge `ready` 后进入 `ready_empty`，显示后端返回的真实版本、工作区、模型和模式。
3. 用户提交第一条普通对话或任务消息时进入 `dismissed`；本次进程内不再自动出现。
4. 加载或重放任意历史会话时进入 `dismissed`，即使该历史会话没有消息，也不把会话切换误当成应用启动。
5. `/clear` 只清理对话，不恢复欢迎页。
6. 在 ready 前出现需要用户关注的 Bridge warning/error 时，欢迎页立即让位给时间线错误消息，不能遮挡诊断信息。
7. 欢迎页状态不进入 UI snapshot。应用重启后重新展示，符合“启动欢迎界面”语义。

提交失败不恢复欢迎页。失败的用户消息及重试入口比品牌空状态更重要。

### 响应式布局

欢迎页只占主视口 body，不覆盖持久 footer、Composer、权限提示、Inspector 或 Agent 页面。

| 等级 | 条件 | 字标 | 信息布局 |
|---|---|---|---|
| Wide | `width >= 100` 且 `bodyHeight >= 16` | 7 行完整块状 `NAUMI` | 居中四行：版本、工作区、模型、模式 |
| Medium | `width >= 56` 且 `bodyHeight >= 10` | 5 行紧凑块状 `NAUMI` | 居中两至三行，工作区按 ANSI 可见宽度截断 |
| Compact | 其他正常终端 | 单行加粗 `NAUMI` | 版本/状态一行，模型/模式一行 |
| Minimal | `width < 24` 或 `bodyHeight < 4` | 单行 `NAUMI` | 只显示“启动中”或“已就绪” |

所有行必须满足 ANSI-aware 可见宽度不超过视口宽度。垂直居中只使用现有 body 高度，不通过额外换行挤压 Composer。resize 时直接根据当前尺寸重新选择等级，不保存布局等级。

### 字标与颜色

- 字标使用项目内固定 glyph 数据和块字符构造，不引入 Figlet、外部字体文件、网络资源或运行时子进程。
- Wide 和 Medium 必须真正拼出可读的 `NAUMI`，不能只把普通字符串放大边框。
- 默认字标使用 bright cyan；启动状态使用 yellow；ready 状态使用 green；字段名和次要分隔使用 dim。
- bypass mode 使用 yellow 文本提示，其余模式使用普通强调色。
- 每个颜色状态同时提供中文文字，关闭颜色或使用屏幕阅读器时信息仍完整。
- 不使用闪烁控制码，避免终端兼容性和可访问性问题。

### 展示内容

`ready_empty` 按以下顺序展示：

1. `NaumiAgent v<version>`
2. `工作区 <workspace_root>`
3. `模型 <model>`
4. `模式 <runtime_mode> · 权限 <permission_mode>`

Wide 模式尽量显示完整工作区；空间不足时复用现有 `shortPath()` 与 ANSI-safe 截断。字段缺失时显示“未解析”，不得伪造默认模型、版本或路径。`booting` 阶段不显示模型占位表格，只显示启动状态，避免把尚未到达的数据误认为真实配置。

## 架构与组件边界

### Bridge 权威字段

`JsonlEngineBridge.status_payload()` 增加：

```json
{
  "version": "0.1.214"
}
```

值直接来自 `naumi_agent.__version__`。该字段随 `ready` 和 `runtime/status` 复用同一 payload 生成路径。协议规范化与 fake/Python bridge fixtures 同步支持该字段。

### 前端状态

`createInitialState()` 增加短生命周期表现状态：

```js
welcome: {
  phase: "booting",
  dismissed: false,
}
```

`ready` 事件只负责合并权威状态并将 `phase` 置为 `ready_empty`。当前自动插入的“新终端 UI 已连接 Python bridge”系统消息删除：ready 已由欢迎页和 footer 表达，该消息会在欢迎页收起后制造无价值历史噪声。

欢迎页是否可见由一个纯函数决定，至少检查：conversation route、Inspector 关闭、未 dismissed、尚未提交用户消息。Agent 页面和 Inspector 保持现有专用布局，不在其上叠加欢迎页。

### 渲染组件

新增 `frontend/terminal-ui/src/components/welcome-screen.js`，提供纯组件和可测试的布局选择函数：

- 输入：`state.status`、`state.mode`、`state.welcome`、`width`、`bodyHeight`、`env`。
- 输出：正好 `bodyHeight` 行或由调用方补齐的 body 行；不得修改 state。
- 依赖：现有 ANSI、component core、`shortPath()` 和可见宽度工具。
- 无定时器、无磁盘读取、无子进程、无网络调用。

`renderMainViewport()` 在欢迎页可见时渲染该组件，否则沿用现有时间线窗口、滚动锚点和缓存路径。欢迎页不创建 message ID，因此不会参与 timeline scroll offset、render cache、outbox 或 completion receipt 排序。

### 收起事件

收起逻辑放在状态层的单一 helper 中，避免各 UI 输入分支分别改字段。以下事件调用该 helper：

- 本地普通消息成功进入 queued outbox。
- 本地任务消息成功进入 queued outbox。
- 收到后端 `user/message` 或 `task/created`，用于兼容外部客户端或本地 optimistic 状态缺失。
- 收到 `session/replayed`。
- 收到需要展示的 warning/error 系统消息。

仅移动输入光标、输入草稿、打开补全、切换 mode 或收到普通 status 不收起欢迎页。

## 错误处理

- Bridge ready payload 缺少 `version`、`model` 或 `workspace_root` 时，欢迎页显示“未解析”，同时保持 Composer 可用。
- 非法字段由现有协议规范化拒绝；欢迎组件只消费规范化后的字符串。
- Bridge stderr 中已被过滤的 LiteLLM 噪声不影响欢迎页；真正 warning/error 会进入时间线并收起欢迎页。
- Bridge 在 ready 前退出时沿用现有退出恢复路径，欢迎页不能吞掉错误消息或阻止终端恢复。
- glyph 渲染在任意宽高下不得抛错；极小视口必须降级而不是截断控制码。

## 性能与稳定性

- 欢迎页是无副作用纯渲染，resize 的复杂度仅与固定字标行数相关。
- 不增加启动 I/O、依赖安装、进程、网络请求或 Bridge 往返。
- glyph 为常量；派生布局可纳入现有 render cache key，也可以因固定规模直接计算，但不得引入全屏每帧动画。
- 欢迎页收起后完全退出主时间线渲染路径，不增加长期会话成本。

## 定向测试

不运行全量测试。实施时只运行以下小模块：

### Node 单元测试

- Welcome component：Wide、Medium、Compact、Minimal 四档；每行可见宽度和总高度受限；无颜色时仍包含 `NAUMI` 与字段标签。
- State reducer：booting -> ready；普通/任务提交收起；后端用户事件兜底收起；replay 收起；`/clear` 不恢复；mode/status 更新不误收起。
- Render：欢迎页不进入 messages，不影响 footer；Inspector/Agent 页面优先；resize 选择正确等级。
- Protocol：ready/status 的 `version` 被规范化并保留，非法对象不能渗入展示字段。

### Python 单元测试

- Bridge `status_payload()` 的 version 等于 `naumi_agent.__version__`。
- `emit_ready()` 同时携带 version、workspace_root、model、mode 和 permission_mode。

### 真实场景

- 用真实 Python JSONL Bridge 启动 Node Terminal UI，确认先出现启动态，再出现后端真实版本/工作区/模型/模式。
- 提交一条不会产生外部副作用的真实命令或本地对话，确认欢迎页收起、用户消息和后续结果可见。
- resize 至 Wide/Medium/Compact，确认无行溢出、Composer 始终可输入、退出后光标与 alternate screen 正常恢复。

所有 Python 命令设置 `NAUMI_MODELS__API_KEY=unit-test-placeholder`，避免测试配置回退到 macOS Keychain。测试使用 `uv run python -m pytest`，防止新 worktree 缺少 pytest 时误用系统 Python。

## 验收标准

1. 新 Terminal UI 启动后立即出现可读的巨大 `NAUMI`，Bridge 慢启动时仍有明确状态。
2. ready 后展示后端权威版本、工作区、模型、runtime mode 和 permission mode。
3. 第一条 chat/task 消息提交后欢迎页自动收起，本次进程内 `/clear` 不恢复。
4. 欢迎页不进入消息、会话、重放、UI snapshot、搜索或完成收据。
5. 四档终端尺寸均不溢出，极小终端可用，颜色关闭后语义完整。
6. Bridge warning/error 不被欢迎页遮挡；退出路径仍恢复终端。
7. 定向 Node/Python 测试和真实 Bridge 场景通过，无新增启动依赖或后台进程。

## 预计改动面

- `src/naumi_agent/ui/bridge.py`
- `tests/unit/test_ui_bridge.py`
- `frontend/terminal-ui/src/components/welcome-screen.js`（新增）
- `frontend/terminal-ui/src/state.js`
- `frontend/terminal-ui/src/render.js`
- `frontend/terminal-ui/src/protocol.js`
- `frontend/terminal-ui/test/components.test.js`
- `frontend/terminal-ui/test/state.test.js`
- `frontend/terminal-ui/test/render.test.js`
- `frontend/terminal-ui/test/protocol.test.js`
- `frontend/terminal-ui/test/index-process.test.js`
- 必要的 fake/Python Bridge fixture

不修改旧 CLI、Textual TUI、模型路由、预算系统或工具注册表。
