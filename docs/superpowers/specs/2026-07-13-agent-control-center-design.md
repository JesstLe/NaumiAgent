# `/agents` Agent Control Center 设计

## 1. 目标与范围

本设计细化已批准的 Terminal UI M6 `/agents` 命令页：让用户查看当前会话内真实的 Agent、执行、工具进度与团队协作证据，并能安全请求停止一个仍在运行的子任务。新 Terminal UI 和 Textual TUI 必须读取同一份后端快照，不从时间线文本或颜色推断状态。

本切片不创建 Agent、不修改调度策略、不暴露思考链、不重写 Workbench，也不把 `/tasks`、`/workbench` 合并进 Agent 页面。

## 2. 方案比较

### 方案 A：复用 `/tasks` 文本面板

优点是改动少；缺点是 Agent 仍是混合面板中的字符串，没有稳定对象 ID、revision、详情或可靠停止语义，前端只能解析文本。它不满足权威数据与独立页面要求。

### 方案 B：专用 Agent Control Center 服务与协议（采用）

`SubAgentManager` 记录真实执行句柄和有界历史，`AgentControlService` 生成严格、版本化、会话隔离的快照；Bridge 提供快照、增量与停止动作；两个 UI 只负责展示和交互。该方案复用现有执行链，同时建立可继续演进到 Codex/Claude Agent 管理能力的稳定边界。

### 方案 C：只读取 Workbench API

Workbench 适合持久化治理对象，但并不拥有当前进程内 Agent、模型调用、工具执行和消息总线的完整生命周期。把它作为唯一来源会遗漏正在运行的真实子任务，因此不采用。

## 3. 后端执行登记

`SubAgentManager` 为每个 `SubTask.id` 建立 `AgentExecution`：

- `task_id`、`agent_name`、任务描述与状态；
- Unix 开始/结束时间、单调时钟耗时和最后活动时长；
- 当前阶段、当前工具、最近工具摘要；
- token、cost、turn、错误和停止请求状态；
- 一个只属于该次 `agent.execute()` 的 `asyncio.Task` 句柄。

`delegate()` 仍是唯一执行入口，但把 `agent.execute()` 放进独立子 Task。Bridge 请求停止时只取消这个子 Task；`delegate()` 将子 Task 的取消转换为 `AgentResult(status="cancelled")` 和终态事件。若父级运行本身正在取消，则继续传播 `CancelledError`，不能把整轮取消伪装成单个 Agent 停止。

重复的活跃 `task_id` 必须被拒绝，避免停止请求命中错误执行。终态执行进入最多 100 条的有界历史；活动执行不因历史截断而丢失。预设 Agent 不能被销毁，但其活动子任务可以停止。

工具事件由一个透明 callback 包装器观察：更新执行的阶段、工具和最后活动时间后，再原样转发给现有 Bridge。包装器不得吞掉事件、修改工具参数或另建执行链。

## 4. 权威快照

`AgentControlService.snapshot()` 输出 `schema_version=1` 的 `AgentControlSnapshot`：

- `session_id`、`revision`、`generated_at`；
- summary：总 Agent、运行、需注意、可停止执行和未读团队消息；
- agents：名称、描述、preset/dynamic、生命周期、任务数、模型 tier、能力、工具、权限级别、age 与 heartbeat age；
- executions：任务 ID、Agent、描述、状态、阶段、当前/最近工具、elapsed、heartbeat age、token、cost、turn、错误、stop_supported、stop_requested；
- team messages：发送者、接收者、topic、priority、时间和受限内容摘要；
- blackboard：key、author、version、时间和受限值摘要；
- warnings：单个数据源读取失败时保留其他内容，并给出中文可操作错误。

所有数组和文本都有上限。revision 只在规范化内容变化时增加；时间流逝本身不制造无穷 revision，前端用 `generated_at` 和后端给出的 age 值展示新鲜度。

状态只来自 `SubAgentManager`、`AgentMessageBus` 和真实执行记录。没有 Agent 活动时显示明确空状态，不生成示例对象。

## 5. Bridge 协议

客户端事件：

- `agents/request { session_id, known_revision, open }`
- `agents/stop { session_id, task_id, reason }`

服务端事件：

- `agents/snapshot`：完整快照；
- `agents/update`：连续 revision 的 changed sections；
- `agents/action`：停止请求的确定性结果，包含 `task_id`、`accepted`、`code`、`message`。

首次打开、revision 断序、跨顶层字段变化时发送完整快照。页面打开期间，`subagent_event`、`team_event`、子 Agent 工具生命周期和停止动作触发刷新。跨会话请求、缺失 ID、已终止任务、未知任务和重复停止分别返回稳定错误码；错误不能清空最后成功快照。

## 6. 新 Terminal UI

`/agents` 切换到独立全屏页面，不向对话时间线写入系统消息。页面包含：

1. 汇总栏；
2. Agent/Execution/Team 三个标签；
3. 左侧有界列表与右侧详情（窄终端改为单列详情）；
4. loading、empty、stale、error 状态与最后成功时间。

键盘行为：

- `Tab` / `Shift+Tab` 切换标签；
- `↑` / `↓` 选择，`Enter` 打开详情；
- `r` 手动刷新；
- 对可停止执行按 `x` 进入确认，再按 `y` 发送停止，`n` / `Esc` 取消确认；
- 页面未处于确认态时 `Esc` 返回对话，恢复原草稿、滚动锚点和 Inspector 状态。

权限请求始终优先于页面快捷键。停止发送后显示“正在请求停止”，直到 `agents/action` 和后续权威快照确认终态；前端不能乐观标记 cancelled。

页面状态按 session 持久化：是否打开、标签、选择 ID、详情和滚动位置。快照业务数据不写入 UI state 文件，重启后必须从 Bridge 刷新。

## 7. Textual TUI 同步

Textual 增加使用同一 `AgentControlSnapshot` formatter 的全屏 `AgentControlScreen`。`/agents` 与共享 keybinding action 打开页面，支持同样的三标签、选择、详情、刷新、两步停止确认和错误保留。Textual 不复制快照采集或停止逻辑。

## 8. 错误与并发边界

- 同时运行多个不同 task ID 时可独立停止，不串扰其他执行。
- 同一个 Agent 的并发执行分别显示；Agent 生命周期为聚合态，Execution 才是停止目标。
- 正在模型调用或工具调用的子 Task 被停止时，取消沿真实 await 链传播；完成事件只发一次。
- 停止与自然完成竞争时，以管理器锁内观察到的终态为准；最多一个请求被接受，其余返回 `already_finished` 或 `already_requested`。
- Bridge 断开或页面离开不停止 Agent。重新打开必须通过完整快照恢复。
- 数据源异常保留已成功部分；UI 刷新异常保留最近成功内容并标 stale。

## 9. 验证

测试链路按 TDD 分层：

1. 管理器真实异步测试：启动阻塞 Agent、观察工具/心跳、停止指定 task、确认另一并发 task 不受影响，并覆盖父任务取消、重复 ID、自然完成竞争。
2. 快照测试：真实 manager/message bus/blackboard，严格 schema、边界截断、revision 稳定、空状态、部分数据源失败和会话隔离。
3. Bridge 契约：快照、连续增量、断序恢复、停止错误码、事件触发刷新和协议字段严格校验。
4. 新 UI 测试：路由往返、草稿/滚动保留、三标签、宽窄布局、确认停止、权限优先、错误不清屏和 UI state 迁移。
5. Textual 测试：同源 formatter、页面交互、确认停止和刷新失败保留。
6. 真实端到端：在真实 `AgentEngine` 上注册一个本地确定性阻塞 Agent，通过 Python JSONL Bridge 打开 `/agents`、观察真实执行、从新 UI 请求停止、确认后端终态，再用同一会话快照验证 Textual 页面。不得调用外部模型或用伪造快照代替链路。

发布门禁包含 `ruff check src/`、import、定向 pytest、完整 `pytest tests/ -x`、完整 Node 测试和 `uv build`。

## 10. 明确未完成项

本切片完成后仍不包含 Agent 创建/重配、跨进程远程 Agent、持久化历史查询、Workbench Agent 映射和 `/workbench` 页面。这些能力必须在后续独立功能中设计、验证和提交。
