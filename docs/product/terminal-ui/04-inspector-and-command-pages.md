# 04 Inspector 与命令页

## 1. 目标

保持主时间线专注，同时让计划、工具、环境、任务、Agent 和 Workbench 治理信息随时可达。Inspector 是当前会话的上下文视图；命令页是跨对象的管理视图，两者不得混为一体。

## 2. Inspector 信息架构

Inspector 包含固定标签：

| 标签 | 内容 | 数据来源 |
|---|---|---|
| Plan | 当前计划、Todo、阻塞和下一步 | `todo_status`、任务事件 |
| Tools | 当前/最近工具、权限范围、失败证据 | 工具与权限事件 |
| Context | 工作目录、分支、上下文健康、模型和预算 | runtime/context 消息 |
| Changes | 变更文件、diff 摘要、未跟踪文件 | 真实 Git/工具结果 |
| Tests | 已运行、通过、失败、未验证项 | validation 事件 |

Inspector 不发明数据。某标签没有权威数据时显示“尚未产生”，而不是零值或 mock。

## 3. 响应式布局

- 宽度 `>= 120`：右侧抽屉默认 38-46 列，主时间线保留至少 72 列。
- 宽度 `100-119`：右侧抽屉 34-38 列，可覆盖部分时间线但不改变其状态。
- 宽度 `< 100`：Inspector 成为全屏独立页，`Esc` 返回原视口。
- 高度不足时标签栏固定，内容独立滚动。

终端缩放只改变布局模式，不能重建会话、丢失选中标签或改变主时间线锚点。

## 4. Inspector 交互

- `Ctrl+I` 打开/关闭。
- `[`、`]` 或左右键切换标签。
- `Enter` 展开选中条目，`Esc` 返回或关闭。
- Inspector 打开时，普通文本仍输入到 Composer；只有显式聚焦后方向键才操作 Inspector。
- 权限请求优先显示，但不会被 Inspector 遮盖。

## 5. 独立命令页

### `/tasks`

展示当前项目任务列表、状态、优先级、关联会话、工作树、验证和阻塞。支持筛选、查看详情、打开关联会话、取消可取消任务。任务创建仍从对话或 `/task` 发起，不在列表页复制复杂表单。

### `/agents`

展示活跃/最近 Agent、所属任务、当前阶段、耗时、工具和最后心跳。支持进入只读详情、跳转关联时间线和请求停止。不能直接伪造 Agent 状态或绕过引擎调度。

### `/workbench`

展示 Mission、Issue、Worktree、Validation、Approval 和事件时间线。第一版以可搜索列表和详情为主，不复制 Mac 应用画板。所有写操作沿用 Workbench API 与权限规则。

## 6. 页面路由状态

```text
RouteState
  route: chat | tasks | agents | workbench | inspector
  params: selected_id, filter, tab
  origin: previous route + scroll anchor
```

斜杠命令只改变路由，不创建新会话。返回时必须恢复来源页的选择、筛选、滚动位置和输入草稿。

## 7. 数据刷新

- 首次打开页面发送带 `request_id` 的快照请求。
- 后续通过增量事件更新，断序时请求完整快照。
- 页面离开后保留有限缓存，但仍接收与当前运行相关的关键状态。
- 手动刷新必须显示请求状态和最后成功时间。
- 数据过期时明确标记 stale，不把旧数据当作实时状态。

## 8. 异常与空状态

区分“没有对象”“仍在加载”“服务不可用”“权限不足”“数据过期”。错误页保留已有内容并提供重试，不能清空成空列表。对象已删除时返回列表并显示可理解通知。

## 9. 测试与验收

覆盖三个宽度区间、路由往返、并发事件刷新、对象删除、快照乱序、空状态和 Inspector 焦点冲突。验收时必须在运行中的对话里打开三个命令页并返回，确认输出继续、权限可处理、草稿不丢、三栏信息不会互相压住。

## 10. 当前实现（0.1.214）

M5 Runtime Inspector 与 M6 `/agents` Agent Control Center 已完成：

- 后端 `RuntimeInspectorService` 从 TaskStore、运行/工具/审批事件、完成回执和真实 Git 工作区生成版本化快照；Plan、Tools、Context、Changes、Tests 五个标签均使用权威数据，缺少证据时显示明确空状态。
- JSONL Bridge 提供 `inspector/request`、`inspector/snapshot`、`inspector/update`。增量 revision 必须连续，客户端发现断序会携带已知 revision 请求完整快照；跨会话请求被拒绝。
- 新 Terminal UI 使用 `Ctrl+I` 打开，`Tab` 显式进入/离开 Inspector 焦点，支持标签、条目和展开状态持久化。`>= 120` 列为并排抽屉，`100-119` 列为覆盖层，`< 100` 列为独立全屏页；中文宽字符和极小高度均有边界测试。
- Textual TUI 通过同一个后端快照和共享字段语义提供五标签全屏页；运行完成后刷新，刷新失败保留最近一次成功内容。权限弹窗保持最高交互优先级。
- 真实端到端测试在临时 Git 仓库中创建 Todo、编辑文件、运行成功/失败 pytest、持久化完成回执，再把同一快照依次穿过 Python Bridge、三个宽度的新 UI 和 Textual formatter；另覆盖无 Git、空计划、revision 断序恢复和会话隔离。
- 后端 `SubAgentManager` 登记真实执行句柄、阶段、工具、心跳、token/cost 和有界终态历史；`AgentControlService` 从执行管理器、消息总线与黑板生成严格、会话隔离、单调 revision 的权威快照。
- JSONL Bridge 提供 `agents/request`、`agents/snapshot`、`agents/update`、`agents/stop` 和 `agents/action`，支持连续增量、断序全量恢复、跨会话拒绝与稳定停止结果码。
- 新 Terminal UI 的 `/agents` 是独立全屏页，提供 Agent、执行、协作三标签、宽窄有界布局、UI state v5 表现状态恢复，以及 `x` 后 `y` 的非乐观停止确认；权限请求始终优先。
- Textual TUI 的 `/agents` 与 `Ctrl+G` 读取同一 `AgentControlSnapshot`，使用真实三标签列表/详情、同样的停止确认，并在刷新失败时保留最后成功快照。
- 真实 Agent E2E 同时运行两个本地确定性 Agent，经 Python Bridge 和新 UI reducer/renderer 停止指定执行，确认兄弟执行完成，再把同一最终快照交给 Textual formatter；进程测试还覆盖实际 Python Bridge 的键盘闭环。

尚未完成：Agent 创建/重配、跨进程远程 Agent、持久化执行历史、Workbench Agent 映射、`/workbench` 独立命令页、Inspector 写操作与完成回执 next action 交互。这些能力不得由客户端推断或用 mock 补齐。
