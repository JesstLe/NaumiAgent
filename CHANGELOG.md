# Changelog

All notable changes to this project will be documented in this file.

## [0.1.1] - 2025-05-17

### Added
- **CLI 全屏布局** — 固定底部输入栏（圆角边框 `╭─╮`），对话历史上方滚动显示
- **实时流式输出** — thinking/tool 调用直接更新在输出区，不再切屏
- **斜杠命令补全菜单** — prompt_toolkit CompletionsMenu 浮层，输入 `/` 自动弹出
- **命令快捷键** — `/q` 退出、`/h` 帮助、`/t` 工具、`/n` 新会话、`/u` 用量、`/m` 模型、`/v` 版本、`/c` 清除
- **双击 Escape 强制中断** — 处理中连按两次 Escape 退出卡死状态
- **Banner 显示版本号和模型名** — 右上角 `v0.1.1`，右下角当前模型
- **CHANGELOG.md** — 版本变更追踪文档

### Fixed
- `/quit` 命令无法退出全屏 app — 补全 `cli.exit()` 调用
- thinking/response 顺序错乱 — finalize_live() 在添加 response 之前固化 thinking
- 日志噪音混入输出 — 流式阶段抑制 litellm/naumi_agent INFO 日志

## [0.1.0] - 2025-05-17

### Added
- **斜杠命令正则匹配自动补全** — CLI 模式使用 prompt_toolkit PromptSession，TUI 模式使用 Textual SuggestFromList
- **TUI `/new` 命令** — 保存当前会话并开始新对话
- **`/version` 命令** — 显示当前版本号
- **Thinking 流式输出** — kimi-for-coding 思考过程实时显示到 CLI
- **`__main__.py` 入口** — 支持 `python -m naumi_agent` 启动
- Phase E-H: 自我审查、自我修改、自我进化、工具锻造
- 热重载模块、生命周期钩子、子 Agent 系统
- 长期记忆 (ChromaDB)、会话持久化 (SQLite)
- 30+ 分析工具（chaos/scale/state/vibe/eval 等）
- MCP 协议客户端集成
- FastAPI REST + WebSocket API
- Textual TUI 界面
- 安全沙箱（Docker 容器隔离）

### Fixed
- kimi-for-coding thinking 输出空白 — rich console 与 spinner 冲突导致中文字符丢失，改用 sys.stdout.write 绕过
- `_is_kimi_thinking_model` 误匹配 — kimi-for-coding 不支持 thinking 参数，缩小匹配范围到 kimi-k2.x/kimi-latest
- TUI InputBar CSS height 导致输入框不可见
- TUI `_set_input_enabled` 类型错误（TextArea → Input）
- CLI `prompt_with_completion()` 缺少 await
- Banner ASCII art 使用 box-drawing 字符在部分终端显示异常
