# UI-18 Goal 与 Pursuit 可视化

## 目标

让用户在新 UI 和 TUI fallback 中直接看到长期 Goal、当前 Pursuit 阶段、成功标准、下一步、等待任务和
证据，不再依赖 `/goal status`、`/pursue status` 返回的 Markdown 文本。Python 仍拥有权威状态，Node
只拥有选择、折叠和滚动等临时界面状态。

## 子模块

- UI-18.1 Typed snapshot（已实现）：以稳定 `goal_id`、`pursuit_run_id` 输出目标、状态、阶段、criteria、等待和
  有界证据；Producer/consumer 双边校验 schema、文本长度与列表上限。
- UI-18.2 Goal page：当前目标主卡、历史目标、Pursuit 时间线、证据/等待详情和明确的空/缺失引用状态。
- UI-18.3 Actions：create/pause/resume/block/complete/cancel 与 pursue/resume；全部调用现有 ToolExecution
  权威路径，写操作展示风险和结果，不在前端改状态。
- UI-18.4 Interaction：接入 HAR-10.6 的结构化选项、自定义输入、超时和 takeover；等待用户时不伪装
  成模型运行。
- UI-18.5 Recovery UX：展示 heartbeat、lease owner、checkpoint、reconcile 与孤立 run；依赖 HAR-10，
  在后端合同完成前不得用 UI 本地计时器模拟。
- UI-18.6 TUI parity：Textual TUI 消费相同 snapshot/动作协议，以紧凑布局提供核心状态和操作。

## UI-18.1 已实现边界

本切片只实现只读 snapshot 与新 UI/TUI 的类型化状态渲染：不增加状态迁移、不实现心跳、不引入第二
个 Goal Store。缺少 Goal、Goal 未绑定 Pursuit、绑定 run 不存在、非法 schema、超长证据、未知状态和
旧客户端 capability 降级都必须有明确测试。

ARC-01.4b2e 是该切片的必要前置：Composition Root 已保证 Bridge/TUI/Engine 读取同一 Goal/Pursuit
资源，并提供重开可恢复的稳定关联。snapshot 必须在 Python 侧从这两个 Store 组装，Node 禁止解析
`format_goal()` 或 `format_run()`。

已交付：

- Python `GoalPursuitSnapshot` 显式投影 Goal、Pursuit、wait 和 evidence，状态库不存在时返回空快照且
  不触发建库；历史、等待、证据、警告和文本均有生产端上限；
- `goal_panel` / `goals/snapshot` 与 `goal_snapshot` capability 已登记到协议治理注册表；旧客户端收到
  同一 snapshot 的安全 Markdown 降级；
- Node 严格拒绝未知状态、非法/重复 ID、错误 stable link、`verified > total` 和非布尔/非数组字段，
  并只保留声明的公共字段；
- `/goal`、`/goal status`、`/goal list [--active]` 打开彩色只读页面，支持刷新、滚动和返回；创建与状态
  更新仍提交到原 ToolExecution 权威路径；
- Goal status/list Agent Tool 与 Textual TUI fallback 复用同一个 Python snapshot renderer，不从展示文本
  反向解析运行事实；
- 新进程真实场景已验证 `goal_panel → goals/snapshot → Goal/Pursuit 页面`，页面显示稳定 ID、标准进度、
  下一步和硬证据。

## 验收标准

- 同一 snapshot 能区分 active/paused/blocked/completed/cancelled Goal 与 Pursuit 全部终态；
- Goal → Pursuit 使用稳定 ID，缺失 run 显示“追踪记录不可用”，不能按目标文本猜测；
- evidence/wait/历史列表有后端和前端双重上限，unknown/private 字段不进入 UI；
- 新 UI 页面与 TUI fallback 读取同一 typed contract；旧客户端收到明确文本降级；
- 页面重开不恢复上次本地侧栏/输入草稿，只有显式 resume 才恢复持久运行事实；
- 只读 snapshot 不启动模型、不执行工具、不修改 Goal/Pursuit 数据库；
- UI-18.3 前所有按钮均不可伪装为可用动作；UI-18.5 前不展示虚假 heartbeat/lease 健康。

## 依赖与完成定义

UI-18.1-18.3 依赖 ARC-01.4b2e 与现有 ToolExecutionPort；UI-18.4-18.5 依赖 HAR-10.1-10.6；UI-18.6
随每个子模块同步，不作为最后补票。只有新 UI/TUI 均具备状态、动作、恢复和交互闭环，UI-18 才可标记
implemented。

## 当前不足

UI-18.1 是只读状态页，不包含 Goal/Pursuit 写按钮、可展开完整证据时间线或 HAR-10 恢复健康；这些分别
属于 UI-18.2/18.3/18.5。Pursuit wait/evidence 当前按快照显示最近有界集合，尚无 cursor 分页。页面也
不会伪造 lease、heartbeat 或 checkpoint。UI-18 因此保持 partial，不能标记整体完成。
