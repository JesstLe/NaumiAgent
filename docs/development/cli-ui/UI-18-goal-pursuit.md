# UI-18 Goal 与 Pursuit 可视化

## 目标

让用户在新 UI 和 TUI fallback 中直接看到长期 Goal、当前 Pursuit 阶段、成功标准、下一步、等待任务和
证据，不再依赖 `/goal status`、`/pursue status` 返回的 Markdown 文本。Python 仍拥有权威状态，Node
只拥有选择、折叠和滚动等临时界面状态。

## 子模块

- UI-18.1 Typed snapshot：以稳定 `goal_id`、`pursuit_run_id` 输出目标、状态、阶段、criteria、等待和
  有界证据；Producer/consumer 双边校验 schema、文本长度与列表上限。
- UI-18.2 Goal page：当前目标主卡、历史目标、Pursuit 时间线、证据/等待详情和明确的空/缺失引用状态。
- UI-18.3 Actions：create/pause/resume/block/complete/cancel 与 pursue/resume；全部调用现有 ToolExecution
  权威路径，写操作展示风险和结果，不在前端改状态。
- UI-18.4 Interaction：接入 HAR-10.6 的结构化选项、自定义输入、超时和 takeover；等待用户时不伪装
  成模型运行。
- UI-18.5 Recovery UX：展示 heartbeat、lease owner、checkpoint、reconcile 与孤立 run；依赖 HAR-10，
  在后端合同完成前不得用 UI 本地计时器模拟。
- UI-18.6 TUI parity：Textual TUI 消费相同 snapshot/动作协议，以紧凑布局提供核心状态和操作。

## UI-18.1 最小交付边界

下一切片只实现只读 snapshot 与新 UI/TUI 的类型化状态渲染：不增加状态迁移、不实现心跳、不引入第二
个 Goal Store。缺少 Goal、Goal 未绑定 Pursuit、绑定 run 不存在、非法 schema、超长证据、未知状态和
旧客户端 capability 降级都必须有明确测试。

ARC-01.4b2e 是该切片的必要前置：Composition Root 已保证 Bridge/TUI/Engine 读取同一 Goal/Pursuit
资源，并提供重开可恢复的稳定关联。snapshot 必须在 Python 侧从这两个 Store 组装，Node 禁止解析
`format_goal()` 或 `format_run()`。

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
