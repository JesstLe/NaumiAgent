# UI-11.1a 类型化 Task View Model

## 目标与边界

本切片让 Todo、子智能体执行、后台命令和浏览器任务通过同一份严格视图模型进入新 UI，
停止由 Node 从 Python 生成的 ANSI 文本中反向解析任务 ID、状态和来源。它只提供只读快照与
结构化渲染，不提前实现 cancel/retry/takeover，也不替代各来源的权威 Store。

## 统一字段

每个 `TaskViewItem` 包含稳定的 `source:task_id` view id、来源、来源内 ID、规范状态、原始状态、
标题、owner、priority、依赖、children、创建/更新时间、age、详情摘要和 artifact 引用。规范状态
固定为 `pending|running|blocked|completed|failed|cancelled`；原始生命周期仍保留，前端不反推
后端终态。

子智能体视图优先读取 `SubagentManager.list_executions()` 的真实执行记录，而不是把 Agent preset
或一条自然语言事件当作执行；兼容旧 manager 时才从最近事件按 task id 有界降级。Todo、后台和
浏览器均继续读取现有权威对象。

## 协议与渲染

- Python Bridge 对 `/tasks` 发出 `tasks/snapshot` schema v1，包含 filters、最多 200 个 item、
  最多 200 个 timeline event 和有界警告；事件登记在 ARC-03 治理注册表。
- producer 与 Node consumer 双边限制文本、依赖和 artifact 数量；Node 严格拒绝未知来源、状态、
  schema 或错误类型，并丢弃未声明私有字段。
- 新 UI 直接按 typed item 分组和着色，子智能体显示 owner、执行状态与 age；选择使用 view id，
  详情和取消仍传来源内 task id，避免跨来源 ID 碰撞。
- Textual TUI 继续调用同一个 `build_task_panel_snapshot()`，保留现有降级页面，不建立第二套查询。
- 协商了 `task_snapshot` 的当前 UI 使用 typed event；旧客户端未声明该 capability 时 Bridge
  明确降级为同一 snapshot 的 `ui/message` 文本，不会突然收到未知事件。

## 验收证据

- 四种来源生成统一字段；blocked dependency、子智能体 owner、artifact 和规范状态可机械断言；
- Bridge 发出 typed snapshot，不再发 ANSI system notice；
- Node 对 205 项输入截断到 200，拒绝非法状态并丢弃 private reasoning；
- typed reducer 不解析展示文本即可稳定选择，并保留来源内 task id；
- 结构化 renderer 在无 ANSI 语义下仍显示“子智能体”、owner、状态、依赖和 Detail；
- 协议事件注册表在 Python/Node 两端保持精确覆盖。

## 未完成项

UI-11.1b 仍需把 priority 接到真正的任务调度事实，并定义跨来源 parent/child graph；UI-11.3 才提供
完整 timeline/detail 双栏，UI-11.4 处理 revisioned 增量刷新，UI-11.5 才建立按来源限制的动作矩阵。
