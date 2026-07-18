# Claude Code 终端 UI 源码审计与迁入映射

> 后续开发权威入口：`docs/development/claude-source/README.md`。本文是 2026-06-02 附近的
> 审计快照；继续迁入前必须执行 `CC-01` 重新核对 source commit、dirty 状态和许可证证据。

## 结论

本地源码位置：`/Users/lv/Workspace/claude-code`。

该仓库 README 明确写明：`License: 本项目内容可自由复用、参考和学习`。因此本轮目标按“可复用源码融合”推进，不再沿用早期路线文档里的“不引入 Claude Code 源码”假设。

NaumiAgent 当前第一阶段选择的是“协议分离 + 小型 Node 终端前端 + Python bridge”的实现路线：Python 继续拥有 `AgentEngine/tools/memory/safety/task/background/debug_trace`，终端前端只消费稳定 JSONL UI Event Protocol。

## 已审查的 Claude Code 区域

| 区域 | Claude Code 入口 | NaumiAgent 对应实现 | 第一阶段状态 |
| --- | --- | --- | --- |
| CLI/REPL 入口 | `src/main.tsx`, `src/entrypoints/cli.tsx`, `src/screens/REPL.tsx` | `src/naumi_agent/main.py`, `frontend/terminal-ui/src/index.js`, `src/naumi_agent/ui/bridge.py` | 已接入 |
| Ink 渲染层 | `src/ink/*`, `src/components/FullscreenLayout.tsx`, `src/components/StatusLine.tsx` | `frontend/terminal-ui/src/render.js`, `frontend/terminal-ui/src/components/*`, `frontend/terminal-ui/src/input-buffer.js` | 已接入 |
| 消息组件 | `src/components/messages/*`, `src/components/Markdown.tsx`, `src/components/HighlightedCode.tsx` | `src/naumi_agent/ui/messages/*`, `frontend/terminal-ui/src/components/message.js`, `markdown.js`, `tool-card.js` | 已接入 |
| 状态与恢复 | `src/state/*`, `src/assistant/sessionHistory.ts`, `src/screens/ResumeConversation.tsx` | `src/naumi_agent/ui/messages/replay.py`, `frontend/terminal-ui/src/state.js`, `ui-state-store.js` | 已接入 |
| 权限与模式 | `src/components/permissions/*`, `src/types/permissions.ts`, `src/keybindings/*` | `src/naumi_agent/safety/permissions.py`, `src/naumi_agent/ui/bridge.py`, `frontend/terminal-ui/src/index.js` | 已接入 |
| 任务与后台进度 | `src/components/TaskListV2.tsx`, `src/components/tasks/*`, `src/tasks/types.ts` | `src/naumi_agent/tasks/*`, `src/naumi_agent/ui/task_status_renderer.py`, `src/naumi_agent/ui/task_panel.py`, `frontend/terminal-ui/src/state.js`, `frontend/terminal-ui/src/components/task-panel.js` | 已接入 |
| 诊断与日志 | `src/screens/Doctor.tsx`, `src/bridge/bridgeDebug.ts`, `src/components/StatusNotices.tsx` | `src/naumi_agent/debug_trace.py`, `src/naumi_agent/ui/bridge.py`, `frontend/terminal-ui/src/debug-log.js` | 已接入 |

机器可读映射见：`frontend/terminal-ui/cc-source-map.json`。来源身份与漂移校验基线见
`frontend/terminal-ui/cc-source-map.v2.json`；v1 在 CC-01 迁移期继续作为映射内容权威。
协议契约见：`frontend/terminal-ui/protocol-contract.json`，前端运行时与 Python 回归测试都以该 JSON 为事件清单来源，避免两端各自维护事件名称。

## UI Event Protocol 当前边界

客户端事件：

- `hello`
- `submit`
- `set_mode`
- `cycle_mode`
- `permission_response`
- `resume`
- `task_panel`
- `permissions_panel`
- `doctor`
- `ping`
- `shutdown`

服务端事件：

- `ready`
- `ack`
- `error`
- `pong`
- `user/message`
- `ui/message`
- `engine/event`
- `run/started`
- `run/completed`
- `session/replayed`
- `runtime/status`
- `mode/changed`
- `permission/request`
- `permission/resolved`
- `debug/trace`
- `shutdown`

协议原则：

- UI 前端不直接调用工具，不绕过 Python 权限层。
- 协议事件清单与阶段一关键 `ui/message` 字段由 `frontend/terminal-ui/protocol-contract.json` 固化；Node 前端启动时加载该契约校验 bridge 事件，Python 测试校验枚举和 UIMessage dataclass 字段与契约一致。
- 工具参数和大输出进入 adapter 后只保留摘要、路径、长度、状态和必要预览。
- `tool_prepare` 通过 `tool_call_id` 与后续 `tool_use` 精确匹配；ID 不匹配时前端不会把准备进度贴到错误工具卡。
- 图片和大工具结果在进入模型上下文前由 compactor 做瘦身或归档。
- `/resume` 通过 typed UI messages 重放，不直接把历史文本刷回屏幕。
- `todo_write` 的流式工具参数会被提取为紧凑 todo 预览，前端底栏可以在工具真正执行完成前显示阶段性进度。
- 权限请求既显示在底栏，也记录为可更新的历史 card，确认后同一张 card 原地更新状态。
- terminal-ui 对 permission message 使用专用 permission card，展示工具名、状态、原因和 y/n/b/Shift+Tab 操作路径。
- `/permissions` 通过 JSONL 协议打开只读权限面板，展示当前 mode/permission mode、待确认权限、最近权限历史、真实规则来源、风险等级、确认策略和 bypass 生效范围。
- terminal-ui 对超长 task panel 做按屏幕 body 高度的有界渲染，保留顶部标题/摘要并显示隐藏行数，避免新打开 `/tasks` 时只看到面板尾部。
- 前端 raw stdin 使用流式 escape buffer，避免触摸板/终端把方向键 SS3 序列拆包后污染输入框。

## 第一阶段已验证能力

当前窄范围测试覆盖：

- 完整对话渲染，底部不覆盖输出。
- 工具调用 card 使用 call id 区分并展示状态。
- mode 切换、权限确认、todo/status 底栏同屏渲染；`Shift+Tab` 的 `default -> plan -> bypass` 循环已通过真实 Python bridge fixture 验证，`plan` 会向 status 暴露 `permission_mode=strict`。
- bridge `runtime/status` 会携带后台任务、子 Agent、浏览器任务和待确认权限的紧凑计数；terminal-ui 底栏在有活动时常驻显示 `tasks: ...`。
- 长代码块和 diff 默认折叠，可通过 fold 命令展开。
- `/resume` 重放消息并恢复本地 fold/scroll 状态。
- JSONL bridge 事件转换、权限 round trip、恢复拒绝活跃运行。
- `tests/unit/test_ui_bridge.py` 已覆盖真实 `AgentEngine.run_streaming()` 在 patched router 下产生工具调用，完整穿过 `JsonlEngineBridge` 输出 `tool_prepare/tool_use/tool_result/run_completed`，不依赖外部 API。
- terminal-ui 进程级测试会启动真实 Python `JsonlEngineBridge` fixture，通过 stdio JSONL 覆盖 submit、权限确认、tool card、task panel 和 resume replay。
- 新 terminal UI 启动命令、Node 入口、packaged runtime asset。
- legacy fallback 命令路径：`naumi ui --legacy` 进入旧 Textual TUI；新 UI 启动失败会提示 `naumi ui --legacy` / `naumi chat --tui`。
- `/workbench` 在新 UI 与 Textual fallback 共用当前会话的 Python Workbench snapshot；fallback 提供
  只读全屏 Overview、刷新/返回、跨会话拒绝和失败保留旧快照，不复制前端权威状态。
- `/tasks` 通过 JSONL 协议打开任务面板，覆盖 todo/subagent/background/browser 任务来源，并展示 owner、依赖阻塞、后台 cwd/pid/port/output、浏览器 step/current/error 等真实运行细节。
- `/tasks` 支持只读过滤：`/tasks todo open 8`、`/tasks background running`、`/tasks source=browser status=needs_input`，过滤条件会随 pinned panel 自动刷新保持。
- `/tasks detail <id>` 支持只读详情视图，按 todo、subagent、subagent_event、permission、background、browser 类型展示真实上下文字段；detail 条件会随 pinned panel 自动刷新保持。
- `/tasks` 输出包含统一 `Timeline` 区段，后端从 todo、subagent events、permission bubbles、background、browser runs 聚合事件，并继承 source/status 过滤；前端把 Timeline 行纳入选择和展开模型。
- Timeline 区段显示来源计数，并支持 `/tasks timeline collapse|expand|toggle <source>` 与 `/tasks timeline clear` 本地折叠高噪声来源；折叠状态进入 render cache key，避免命令后复用旧画面。
- `/tasks select <id|序号>`、`/tasks next`、`/tasks prev` 和空输入下 `Tab` 支持前端任务选择；空输入下 `Enter` 或 `/tasks open` 会打开当前选中项详情。
- `/tasks jump [id]` 展示当前任务真实运行记录位置：background 使用 `output_path`，browser run 使用 `artifacts` / `reports` / `result.artifacts` 提取出的路径；前端只展示可复制路径，不伪造文件打开。
- `/tasks cancel [id]` 通过 `task_cancel` 协议接入后端真实取消：background 调用 `background_runner.cancel()`，browser run 调用 `task_runner.abort_run()`；todo 删除不等价于取消，因此不会被混用。
- 任务面板已有焦点状态与多键位动作栏：聚焦时空输入 `Tab/n` 选择下一项、`p` 选择上一项、`Enter/o` 打开详情、`j` 展示记录路径、`x` 取消、`Esc` 退出焦点；失焦后这些字母恢复普通输入。
- 任务项已有轻量展开流：`e` / `/tasks expand [id]` 展开当前任务的结构化字段为 `event flow`，`c` / `/tasks collapse [id]` 折叠；展开状态参与 renderer cache key，避免状态变化但屏幕不刷新的问题。
- terminal-ui 对 `tasks` notice 使用专用 task panel renderer，按 Todo/Subagent/Background/Browser 分区展示，避免退回普通文本块。
- `/tasks pin` 会钉住任务面板，后续 task activity 变化时通过同一条 message 原地刷新；`/tasks off` 取消钉住，避免历史区被重复面板刷屏。
- `/permissions` 面板通过专用 renderer 展示 pending/history，并基于 `TOOL_PERMISSIONS` / `PREFIX_PERMISSIONS` 推导规则来源、风险等级、确认策略和 bypass 生效范围，便于回看权限策略状态和最近决策。
- `/doctor` 通过 JSONL 协议调用 Python 诊断后端，返回可复制的环境诊断报告；ARC-05.1
  Store Catalog 同步覆盖 New UI、TUI 与 Agent Tool，不由前端重复推导状态路径。
- 图片 payload 在进入 engine 历史时就被替换为短占位符，避免截图/base64 先撑大上下文。
- `naumi ui` 启动前会校验 Node.js 20+，旧版本或无法识别的 Node 会给出中文诊断错误，而不是进入前端后失败。

## 未完成的 CC 对齐点

1. Task panel 已有 `/tasks` 协议、后端聚合、source/status 过滤、pinned refresh、统一 Timeline、Timeline 来源折叠、只读详情视图、前端键盘选择、焦点管理、多键位动作栏、轻量事件流展开、运行记录路径跳转提示和 background/browser 真实取消；但还没有达到 Claude Code `TaskListV2` / background task dialog 的完整交互能力（真正的全屏列表焦点布局、更细粒度的事件详情导航等）。
2. Renderer 仍是轻量实现，不是 React/Ink 组件树；后续如果继续“全量搬 UI”，需要评估是否直接引入 CC 的 Ink runtime。
3. Doctor/diagnostics 已有结构化 trace，但还没有形成 CC 风格的全屏诊断页面。
4. Keybindings 已支持核心模式切换和输入编辑，但 Vim mode、QuickOpen、全量快捷键配置仍未迁入。

## 下一阶段建议

优先级从高到低：

1. 继续扩展 task panel 的全屏列表焦点布局和 timeline 专用交互，并评估是否迁入 Ink/React。
2. 把 permission panel 继续扩展为 CC 风格的策略视图，展示规则来源、风险等级和 bypass 生效范围。
3. 继续迁入 CC `StatusNotices` / `Doctor` 形态，做可复制诊断报告入口。
4. 评估引入 Ink/React 的成本；若迁入，保持 Python bridge 协议不变，只替换前端 renderer。
