# UI-18.2a Goal 历史选择与有界详情

## 目标

把 UI-18.1 已有的 Goal/Pursuit 只读快照从“所有目标平铺”收口为可导航页面。用户可以选择当前
快照中的历史 Goal，并在 New UI 或 TUI fallback 中查看同一 Python 权威投影；前端不得按目标文本、
数组位置或缓存推测关联关系。

## 协议与权威边界

- `goals/snapshot` 在 schema v2 上新增可选 `selected_goal_id`；该 ID 必须为空或精确存在于 `goals`。
  这是可安全忽略的展示选择字段，旧客户端继续读取 v2 并忽略它，避免为加法字段制造协议断层。
- `goal_panel` 请求接受严格的 `selected_goal_id`，只允许稳定 ID 字符集和 128 字符上限。
- Python `GoalStore.get()` 负责解析显式历史选择；目标不在默认页时，将其加入有界快照，而不是让
  Node 从旧快照伪造详情。
- 无显式选择时优先当前未完成 Goal；没有当前 Goal 时选择目录第一项。非法或不存在的 ID 返回中文
  warning 并回退，不泄露数据库异常。
- 仍复用同一 GoalStore/PursuitStore，不创建详情 Store，不启动模型、不执行工具、不修改运行事实。

## New UI

- 页面先显示紧凑目标目录，再显示唯一的“目标详情”区域；`▶` 表示 Python 已确认的选择。
- `←/→` 与 `[/]` 在目录内移动；每次移动都重新发送 typed `goal_panel` 请求，加载期间禁止重复导航。
- 详情展示 Goal 状态、会话、说明、时间、Pursuit 阶段、成功标准、下一步、裁判、恢复健康、全部有界
  wait 和当前快照中的全部 evidence。
- evidence 不再二次裁剪为最后 5 条；最多展示 Python 允许的最近 20 条，并明确标注快照上限。强证据、
  辅助证据、来源和时间使用不同语义文案与颜色。
- `/goal detail <goal-id>` 与 `/goal status <goal-id>` 可直接打开同一 typed 页面。

## TUI fallback 与 Agent Tool

- `render_goal_pursuit_snapshot()` 输出紧凑目标目录和所选目标的完整有界详情。
- `/goal detail <goal-id>` 复用 `goal_status` ToolExecution；`goal_status` 按 ID 读取时也必须经过共享
  snapshot builder，不能降级为只含 Goal 字段的 `format_goal()`。
- Agent 自主调用 `goal_status(goal_id=...)` 与用户斜杠命令读取同一 Pursuit、wait、evidence 和恢复事实。

## 验收证据

- 选择位于默认列表之外的历史 Goal 时，快照包含该稳定 ID、保留当前 Goal，并将详情绑定到所选 ID。
- 缺少选择字段的 schema v1/v2 快照回退到当前 Goal；新客户端拒绝缺失目录引用、非法 ID 和未知 schema。
- New UI 键盘选择只发送明确 `selected_goal_id`，不会更新 GoalStore；20 条 evidence 全部渲染。
- TUI/Agent Tool 按历史 ID 展示 Goal/Pursuit 详情、等待项与证据，不解析 Markdown 反推状态。
- 定向 Python、Node、Ruff、语法、YAML 与真实临时 SQLite 场景通过。

## 诚实边界

本切片只完成当前快照内的 Goal 选择和详情。Goal 历史仍受 50 项目录上限约束，Pursuit evidence/wait
仍是最近 20 项的有界投影；跨页 Goal 历史 cursor、完整不可变 evidence 时间线 cursor、自动推送和
Goal 写动作分别留给 UI-18.2 后续切片、UI-18.3 与 Recovery UX。UI-18 继续保持 `partial`。
