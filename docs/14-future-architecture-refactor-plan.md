# NaumiAgent 未来架构重构方案

> 后续开发权威入口：`docs/development/architecture/README.md`。本文保留总体架构论证；
> 实施以 `ARC-01` 至 `ARC-08` 的独立模块、依赖门和验收标准为准。

本文档从 NaumiAgent 的终极产品形态出发，规划未来架构重构路线，并回答一个核心问题：

> NaumiAgent 是否有必要重构？是否应该换语言？

结论：

- 有必要重构。
- 不建议现在全项目换语言。
- 应优先完成架构协议化和模块边界重构。
- 核心 Agent Runtime 继续以 Python 为主。
- CLI/TUI 未来可以独立演进为 TypeScript + Ink 前端。
- 高风险执行层未来可以拆分为 Rust / Go / Node daemon。

长期目标不是把 NaumiAgent 变成“一个 Python 项目”或“一个 TypeScript 项目”，而是把它演进为：

```text
协议化 Agent Runtime + 多前端 + 多执行后端 + 可自我演进的工具系统
```

## 1. 终极产品形态

NaumiAgent 的终极形态应该接近一个 Agent OS，而不是单一 CLI 工具。

它应该具备：

- 可长期运行的 Agent Runtime。
- 可替换的 CLI / TUI / Web / IDE / API 前端。
- 可替换的工具执行后端。
- 可观察、可回放、可诊断的会话系统。
- 可扩展的插件、技能、MCP、子 agent、任务系统。
- 可自我审查、自我修改、自我验证、自我进化的闭环。

理想结构：

```text
┌─────────────────────────────────────────────────────────────┐
│                         Frontends                           │
│ CLI / TUI / Web / IDE / Mobile / API / Remote Session       │
└──────────────────────────────┬──────────────────────────────┘
                               │
                               │ UI Protocol
                               │ UserInput / UIMessage / ControlEvent
                               ▼
┌─────────────────────────────────────────────────────────────┐
│                      Agent Runtime                          │
│ Session / Context / Planner / ReAct / Memory / Budget       │
└──────────────────────────────┬──────────────────────────────┘
                               │
                               │ Tool Protocol
                               │ ToolCall / ToolResult / Permission / Progress
                               ▼
┌─────────────────────────────────────────────────────────────┐
│                     Execution Layer                         │
│ File / Shell / Browser / MCP / Sandbox / SubAgent / Daemon  │
└──────────────────────────────┬──────────────────────────────┘
                               │
                               │ State Protocol
                               │ EventLog / SessionDB / TaskDB / DebugTrace
                               ▼
┌─────────────────────────────────────────────────────────────┐
│                       Durable State                         │
│ SQLite / Vector DB / Logs / Artifacts / Worktrees           │
└─────────────────────────────────────────────────────────────┘
```

这个形态下，语言不是核心。核心是协议边界：

- UI 不直接依赖 engine 内部对象。
- engine 不直接依赖 CLI/TUI 组件。
- 工具执行不直接绑死在 orchestrator 进程内。
- 记忆、任务、调试日志可以被任何前端读取和回放。

## 2. 当前架构主要问题

### 2.1 UI 和 engine 仍然耦合

当前 CLI/TUI 仍然大量依赖 engine 事件的即时 if/elif 处理：

- `token`
- `thinking_start`
- `thinking_delta`
- `tool_start`
- `tool_end`
- `task_snapshot`
- `permission_bubble`
- `runtime_notification`

问题：

- 事件语义和 UI 渲染混在一起。
- CLI/TUI 两边重复处理。
- resume 很难恢复 UI 状态。
- 渲染 bug 难以定位。
- 新增一个消息类型要改多个地方。

未来应该改成：

```text
EngineEvent -> UIEventAdapter -> UIMessage -> Renderer
```

### 2.2 工具系统还不够协议化

当前工具已经有 `Tool` 基类和 `execute()`，但工具生命周期还不够完整。

缺少稳定协议：

- tool preparing
- permission pending
- permission decision
- tool running
- progress update
- result summary
- artifact output
- retry metadata
- cancellation metadata

未来应该改成：

```text
ToolCall
ToolProgress
ToolPermissionRequest
ToolPermissionDecision
ToolResult
ToolArtifact
```

### 2.3 CLI/TUI 不应该长期承担业务语义

CLI/TUI 应该只做：

- 输入。
- 显示。
- 快捷键。
- 选择。
- 权限确认。
- 调试查看。

不应该直接理解：

- 工具参数结构。
- todo 状态优先级。
- subagent 调度规则。
- compact recovery 细节。
- permission mode 内部策略。

这些应该先被 runtime 转换成 UIMessage。

### 2.4 Python 适合做大脑，但不适合独自承担全部产品层

Python 的优势：

- AI / LLM / 自动化生态强。
- 工具开发快。
- Agent 自我修改门槛低。
- 文件、浏览器、脚本、数据处理集成容易。
- 当前项目已有大量 Python 沉淀。

Python 的劣势：

- 终端 UI 组件模型不如 React + Ink 自然。
- 大型前端状态管理容易变复杂。
- 类型系统对大规模重构保护有限。
- 高并发 daemon / sandbox / process manager 不是强项。

所以长期看：

```text
Python 适合 Agent brain
TypeScript 适合 terminal/web frontend
Rust/Go 适合 sandbox/process/daemon
```

## 3. 是否换语言

### 3.1 不建议现在全项目换语言

原因：

1. 核心 runtime 仍在快速演进。
2. UI protocol 尚未稳定。
3. 工具协议尚未稳定。
4. 测试覆盖还不足以保护跨语言迁移。
5. 当前主要问题来自架构耦合，不是 Python 本身。
6. 全量重写会中断现有自进化路线。

现在如果直接换语言，容易变成：

```text
架构没拆清楚 + 语言迁移 + UI 重写 + 工具重写
```

这会导致风险叠加。

### 3.2 应该先重构架构边界

优先级：

1. UI protocol。
2. Tool protocol。
3. Session/event log protocol。
4. Runtime control protocol。
5. 执行层 process boundary。

这些边界稳定后，语言替换才会变便宜。

### 3.3 可以局部换语言

未来推荐的语言分工：

| 模块 | 推荐语言 | 原因 |
|---|---|---|
| Agent Runtime | Python | LLM、工具、自动化生态强，自我修改容易 |
| CLI/TUI Frontend | TypeScript + Ink | 组件模型成熟，终端应用体验好 |
| Web Frontend | TypeScript + React | 标准选择 |
| IDE Extension | TypeScript | VS Code / JetBrains 生态适配 |
| Browser Daemon | TypeScript / Node 或 Rust | 浏览器协议生态与长期进程管理 |
| Sandbox Executor | Rust / Go | 安全、并发、进程控制、二进制分发 |
| File Watcher | Rust / Go | 性能和跨平台稳定性 |
| MCP Gateway | Python / TypeScript | 取决于生态和部署目标 |
| Debug Log Viewer | Python TUI 或 Web | 先简单，后续可 Web 化 |

## 4. 目标架构

### 4.1 分层目标

未来 NaumiAgent 应拆成五层：

```text
naumi-core
naumi-runtime
naumi-tools
naumi-frontends
naumi-daemons
```

### 4.2 naumi-core

职责：

- 数据模型。
- 协议定义。
- session schema。
- event schema。
- tool schema。
- permission schema。
- task schema。
- debug trace schema。

特点：

- 尽量无副作用。
- 尽量少依赖。
- 可被 runtime、CLI、TUI、API、测试共同使用。

建议目录：

```text
src/naumi_agent/core/
├── events.py
├── messages.py
├── tools.py
├── permissions.py
├── sessions.py
├── tasks.py
├── artifacts.py
└── protocol.py
```

### 4.3 naumi-runtime

职责：

- 模型调用。
- ReAct loop。
- planner。
- memory。
- context compaction。
- budget tracking。
- permission decision。
- tool orchestration。
- subagent orchestration。
- session persistence。

它不应该负责：

- 具体 CLI/TUI 渲染。
- 终端布局。
- 按键处理。
- Rich/Textual 组件细节。

建议目录：

```text
src/naumi_agent/runtime/
├── engine.py
├── controller.py
├── session_runner.py
├── event_stream.py
├── control_plane.py
└── replay.py
```

### 4.4 naumi-tools

职责：

- 工具注册。
- 工具 schema。
- 工具执行。
- 工具权限声明。
- 工具结果摘要。
- artifact 生成。

未来每个工具应暴露：

- `name`
- `description`
- `parameters_schema`
- `risk_profile`
- `permission_policy`
- `progress_schema`
- `execute()`
- `summarize_result()`
- `render_artifacts()`

建议目录：

```text
src/naumi_agent/tools/
├── base.py
├── registry.py
├── summaries.py
├── file/
├── shell/
├── browser/
├── memory/
├── task/
└── subagent/
```

### 4.5 naumi-frontends

职责：

- CLI。
- TUI。
- Web。
- IDE。
- API clients。

前端只通过协议和 runtime 通信：

```text
Frontend -> RuntimeControl
Runtime -> UIMessageStream
```

建议目录：

```text
src/naumi_agent/frontends/
├── cli/
├── tui/
├── api/
└── shared/
```

短期可以不移动目录，但要朝这个边界重构。

### 4.6 naumi-daemons

职责：

- 长期运行任务。
- browser daemon。
- sandbox executor。
- process runner。
- file watcher。
- remote session。

未来可以独立为外部进程：

```text
daemons/
├── browser-daemon/
├── sandbox-executor/
├── process-runner/
└── file-watcher/
```

通信方式：

- stdio JSON-RPC。
- Unix socket。
- HTTP/WebSocket。
- gRPC。

短期建议用 JSON Lines / WebSocket，简单、可调试。

## 5. 核心协议设计

### 5.1 UI Protocol

目的：

让 CLI/TUI/Web/IDE 都能消费同一套运行时消息。

输入：

```json
{
  "type": "user_input",
  "session_id": "sess_x",
  "content": "帮我实现功能",
  "mode": "default"
}
```

输出：

```json
{
  "type": "ui_message",
  "message": {
    "kind": "tool_use",
    "id": "msg_123",
    "tool_call_id": "call_123",
    "tool_name": "file_write",
    "status": "running",
    "summary": "写入 src/app.py",
    "metadata": {
      "path": "src/app.py",
      "content_lines": 120
    }
  }
}
```

基础消息类型：

- `user`
- `assistant_text`
- `assistant_thinking`
- `tool_use`
- `tool_result`
- `permission_request`
- `permission_result`
- `task_snapshot`
- `subagent_event`
- `background_task`
- `context_compacted`
- `runtime_notice`
- `error`

### 5.2 Runtime Control Protocol

前端向 runtime 发送控制事件：

- `submit_input`
- `interrupt`
- `resume_session`
- `switch_mode`
- `permission_decision`
- `scroll_state`
- `open_panel`
- `run_command`

示例：

```json
{
  "type": "permission_decision",
  "request_id": "perm_123",
  "decision": "allow_once"
}
```

### 5.3 Tool Protocol

工具调用：

```json
{
  "type": "tool_call",
  "id": "call_123",
  "name": "bash_run",
  "arguments": {
    "command": "pytest tests/unit/test_cli_rendering.py -q"
  },
  "risk": {
    "level": "medium",
    "capabilities": ["execute"]
  }
}
```

工具进度：

```json
{
  "type": "tool_progress",
  "tool_call_id": "call_123",
  "phase": "running",
  "message": "执行 pytest",
  "elapsed_ms": 1200
}
```

工具结果：

```json
{
  "type": "tool_result",
  "tool_call_id": "call_123",
  "status": "success",
  "duration_ms": 2400,
  "summary": "3 passed",
  "artifacts": []
}
```

### 5.4 Session Event Log Protocol

每个 session 应保存完整事件流：

- 用户输入。
- 模型响应。
- thinking。
- tool call。
- permission。
- tool result。
- task update。
- runtime notice。
- errors。

要求：

- 可 replay。
- 可 debug。
- 可迁移。
- 可被不同前端重建 UI。

### 5.5 Artifact Protocol

工具不应该把大输出都塞进消息正文。

大内容应该变成 artifact：

- 文件内容。
- diff。
- stdout/stderr。
- 截图。
- HTML preview。
- report。
- trace。

消息只引用 artifact：

```json
{
  "kind": "tool_result",
  "summary": "已生成报告",
  "artifacts": [
    {
      "id": "artifact_123",
      "type": "html",
      "path": "data/artifacts/report.html"
    }
  ]
}
```

## 6. 未来模块拆分路线

### 6.1 第一层：内部边界重构

仍在一个 Python 包内完成：

```text
src/naumi_agent/
├── core/
├── runtime/
├── ui/
├── frontends/
├── tools/
├── daemons/
└── storage/
```

目标：

- 先不跨语言。
- 先把依赖方向理顺。
- 先把协议定下来。

依赖方向：

```text
frontends -> ui -> core
runtime -> core
tools -> core
daemons -> core
storage -> core
```

禁止：

- core 依赖 frontends。
- tools 直接依赖 CLI/TUI。
- runtime 直接拼 ANSI。
- frontends 直接解析复杂 tool arguments。

### 6.2 第二层：runtime 服务化

增加 runtime server：

```text
naumi runtime serve
```

提供：

- WebSocket UI stream。
- HTTP session API。
- permission decision endpoint。
- debug event endpoint。
- artifact endpoint。

CLI/TUI 可选择：

- embedded mode：当前进程内 runtime。
- remote mode：连接 runtime server。

### 6.3 第三层：TypeScript CLI/TUI 实验

在协议稳定后新增：

```text
frontends/cli-ink/
```

职责：

- React + Ink 渲染。
- 快捷键。
- command palette。
- permission modal。
- task panel。
- diff viewer。
- resume screen。

不负责：

- LLM 推理。
- 工具执行。
- 记忆。
- 安全策略。
- session storage。

这样失败成本低：

- TS 前端失败，Python CLI/TUI 仍可用。
- Python runtime 不受影响。

### 6.4 第四层：执行层 daemon 化

可拆出的执行层：

- `sandbox-executor`
- `browser-daemon`
- `process-runner`
- `file-watcher`
- `mcp-gateway`

拆分原则：

- 有明确协议。
- 有独立测试。
- 有健康检查。
- 有日志。
- 有超时和取消。
- 有版本协商。

## 7. 分阶段实施计划

## Phase A：架构地基，1-2 周

目标：

建立协议和边界，不大规模搬目录。

交付：

1. `core` schema 初版。
2. `UIMessage` / `UIEvent`。
3. `ToolProgress` / `ToolArtifact`。
4. `RuntimeControlEvent`。
5. event log replay 基础能力。
6. CLI/TUI adapter 接入。

验收：

- CLI/TUI 可通过 UIMessage 渲染主要消息。
- tool lifecycle 不再散落在事件 handler 里。
- debug log 能记录 UIMessage。
- resume 可以基于 event log 重放。

建议 commits：

1. `refactor: add core runtime protocol models`
2. `refactor: add ui message adapter`
3. `refactor: add tool lifecycle protocol`
4. `feat: replay sessions from event log`

## Phase B：UI 前端协议化，2-3 周

目标：

让 CLI/TUI 成为真正的前端，而不是 runtime 的字符串输出端。

交付：

1. CLI renderer registry。
2. TUI renderer registry。
3. tool card 全生命周期。
4. permission prompt 产品化。
5. task/subagent/background 面板。
6. virtualized history。
7. command palette。
8. debug viewer。

验收：

- CLI/TUI 行为一致。
- 长历史不卡。
- 大 diff / 大代码块默认折叠。
- permission 有完整闭环。
- task/subagent 可观察。

建议 commits：

1. `feat: add cli message renderer registry`
2. `feat: add tui message renderer registry`
3. `feat: add task and subagent panels`
4. `feat: add debug event viewer`
5. `perf: virtualize message history`

## Phase C：runtime 服务化，2-4 周

目标：

让 runtime 可以作为长期服务运行，被多个前端连接。

交付：

1. `naumi runtime serve`。
2. WebSocket UI stream。
3. HTTP session API。
4. control event API。
5. artifact API。
6. health check。
7. auth/token 基础保护。

验收：

- CLI/TUI 可以连接 remote runtime。
- Web/API 可以消费同一套消息。
- session 可跨前端恢复。
- debug log 可通过 API 查看。

建议 commits：

1. `feat: add runtime websocket server`
2. `feat: add runtime control API`
3. `feat: expose session and artifact API`
4. `feat: support remote frontend mode`

## Phase D：TypeScript + Ink 前端实验，2-4 周

目标：

验证是否值得将 CLI/TUI 前端长期迁移到 TypeScript + Ink。

交付：

1. `frontends/cli-ink` 最小可用前端。
2. UIMessage stream client。
3. input / permission / mode switch。
4. tool card。
5. status bar。
6. task panel。
7. diff viewer。
8. resume screen。

验收：

- 能完成真实 coding task。
- 体验优于 Python CLI/TUI。
- 不影响 Python runtime。
- 可独立发布或作为可选前端。

继续推进的条件：

- 首字反馈明显更好。
- 滚动和布局稳定性明显更好。
- 组件开发效率明显更高。
- 维护成本可接受。

停止条件：

- 跨语言调试成本过高。
- 协议频繁变化导致前端跟不上。
- 体验没有明显超过 Python TUI。

## Phase E：执行层 daemon 化，1-2 月

目标：

将高风险、高并发、长生命周期执行能力从 runtime 拆出去。

优先级：

1. browser daemon。
2. process runner。
3. sandbox executor。
4. file watcher。
5. MCP gateway。

验收：

- runtime 崩溃不带崩 daemon。
- daemon 有 health check。
- tool call 可取消。
- stdout/stderr 可流式回传。
- 权限边界更清晰。

## Phase F：自进化闭环，持续演进

目标：

让 NaumiAgent 可以安全地修改自身。

能力：

- 自我审查。
- 自我修复。
- 自我测试。
- 自我 benchmark。
- 自动生成 migration plan。
- 自动生成 rollback plan。
- worktree 隔离实验。
- agent 自己比较方案优劣。

要求：

- 每次自修改必须有 event log。
- 每次修改必须有测试证据。
- 每次修改必须可回滚。
- 高风险修改必须进入 review/permission 流程。

## 8. 换语言决策门槛

### 8.1 可以考虑 TypeScript CLI/TUI 的条件

同时满足：

- UI Protocol 稳定。
- Python CLI/TUI 已完成 message model。
- 已有足够 UI replay 测试。
- Ink 原型体验明显更好。
- 前端不需要理解 runtime 内部对象。
- 构建、分发、配置成本可控。

### 8.2 可以考虑 Rust/Go 执行层的条件

同时满足：

- 某个执行模块成为稳定边界。
- Python 实现存在明确性能或安全瓶颈。
- 协议稳定。
- 有独立测试和 health check。
- 有清晰 fallback。

候选模块：

- sandbox executor。
- process runner。
- file watcher。
- terminal IO manager。

### 8.3 不应该换语言的信号

- 只是因为“别人的项目用了某语言”。
- 当前问题可以通过架构边界解决。
- 协议还在频繁变化。
- 测试不足。
- 迁移后无法复用现有 session/tool/memory。
- 需要一次性冻结产品开发太久。

## 9. 数据和状态迁移

未来重构必须保护这些资产：

- session history。
- task store。
- memory store。
- debug logs。
- config.yaml。
- permissions。
- worktree metadata。
- browser daemon runs。
- artifacts。

### 9.1 Schema versioning

所有持久化状态应有版本：

```json
{
  "schema_version": 3,
  "kind": "session_event",
  "payload": {}
}
```

### 9.2 Migration runner

提供：

```text
naumi migrate status
naumi migrate apply
naumi migrate rollback
naumi migrate verify
```

### 9.3 Backward compatibility

至少保证：

- 新 runtime 能读旧 session。
- 新 UI 能展示旧消息。
- 旧 debug log 不丢。
- migration 可 dry-run。

## 10. 测试体系

### 10.1 Unit tests

覆盖：

- protocol model。
- adapter。
- renderer。
- permission policy。
- tool summary。
- event replay。

### 10.2 Integration tests

覆盖：

- engine event -> UIMessage -> CLI render。
- engine event -> UIMessage -> TUI render。
- tool call -> permission -> result。
- resume replay。
- debug trace。

### 10.3 E2E scenarios

建议目录：

```text
tests/e2e/scenarios/
├── simple_chat.yaml
├── large_file_write.yaml
├── permission_default_mode.yaml
├── plan_mode_blocks_write.yaml
├── bypass_mode_allows_write.yaml
├── resume_replay.yaml
├── subagent_task.yaml
├── background_task.yaml
└── large_diff.yaml
```

### 10.4 Performance tests

指标：

- startup time。
- time to first visible feedback。
- token render latency。
- large history replay time。
- large diff render time。
- memory usage。

### 10.5 Compatibility tests

覆盖：

- macOS Terminal。
- iTerm2。
- VS Code terminal。
- tmux。
- Linux terminal。
- CJK width。
- emoji width。
- low color terminal。

## 11. 目录结构目标

短期目标：

```text
src/naumi_agent/
├── core/
├── runtime/
├── ui/
│   ├── messages/
│   ├── renderers/
│   └── protocol.py
├── cli/
├── tui/
├── tools/
├── memory/
├── tasks/
├── safety/
└── api/
```

中期目标：

```text
src/naumi_agent/
├── core/
├── runtime/
├── storage/
├── tools/
├── frontends/
│   ├── cli_prompt_toolkit/
│   ├── tui_textual/
│   └── api_fastapi/
├── daemons/
└── integrations/
```

长期目标：

```text
packages/
├── naumi-core-python/
├── naumi-runtime-python/
├── naumi-tools-python/
├── naumi-cli-ink/
├── naumi-web/
├── naumi-sandbox-executor/
└── naumi-browser-daemon/
```

## 12. 工程治理

### 12.1 重构规则

- 不做一次性大爆炸重写。
- 一个边界一个边界拆。
- 一个功能一个 commit。
- 每个迁移都有兼容层。
- 每个迁移都有 rollback 路径。
- 每个协议变更写 schema version。

### 12.2 文档要求

每个新协议必须有：

- 目的。
- schema。
- 示例。
- 兼容性说明。
- 迁移说明。
- 测试说明。

### 12.3 Review checklist

每个重构 PR 检查：

- 是否减少耦合。
- 是否有测试。
- 是否可回滚。
- 是否破坏旧 session。
- 是否影响 CLI/TUI 一致性。
- 是否引入新的跨层依赖。
- 是否有 debug 可观察性。

## 13. 风险分析

### 13.1 最大风险：重构范围失控

缓解：

- 按协议边界拆。
- 每阶段有验收标准。
- 不同时做 UI、runtime、tool、storage 的大迁移。

### 13.2 第二风险：双前端维护成本

Python CLI/TUI 和未来 TS Ink 前端可能长期并存。

缓解：

- 共享 UI Protocol。
- 共享 golden scenario。
- 不让前端理解 runtime 内部对象。

### 13.3 第三风险：状态迁移失败

缓解：

- schema version。
- migration dry-run。
- old session replay tests。
- backup before migration。

### 13.4 第四风险：性能优化过早

缓解：

- 先测量。
- 先建立 event log 和 profiler。
- 只优化真实瓶颈。

### 13.5 第五风险：自进化修改失控

缓解：

- worktree isolation。
- permission gates。
- tests required。
- rollback plan。
- debug trace required。

## 14. 推荐路线

推荐路线：

```text
第一步：Python 内部协议化
第二步：CLI/TUI message model
第三步：runtime service mode
第四步：TypeScript Ink 前端实验
第五步：执行层 daemon 化
第六步：自进化闭环
```

不推荐路线：

```text
直接全量改 TypeScript
直接全量改 Rust
同时重写 CLI/TUI/runtime/tools
不建协议就拆进程
不建 event log 就做 resume
```

## 15. 近期行动清单

优先执行：

1. 完成 UIMessage adapter。
2. 完成 CLI/TUI renderer registry。
3. 完成 tool lifecycle protocol。
4. 完成 resume replay。
5. 完成 event log schema。
6. 完成 runtime control event。
7. 完成 protocol docs。

暂缓：

- 全量 TypeScript 前端。
- Rust sandbox。
- runtime server。
- 多包 monorepo。

这些应等协议稳定后再做。

## 16. 最终判断

NaumiAgent 需要重构，但不应该现在全量换语言。

最健康的未来形态是：

```text
Python 做大脑
TypeScript 做脸
Rust/Go 做手脚
SQLite/Postgres 做长期状态
JSON/WebSocket/IPC 做神经系统
```

换语言不是目标，协议化才是目标。

当协议边界稳定后，语言可以局部替换；在此之前，全量换语言只会把架构问题搬到另一种语言里。
