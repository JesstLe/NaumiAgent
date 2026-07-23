# UI-18.4d1 Goal Interaction 详情权威

## 目标

在开放手动 takeover 之前，先让用户能从 New UI、CLI 和 Textual TUI 查看同一份
`HarnessInteractionRecord` 详情：问题、选项、自定义输入能力、终态答案、时序、sequence/owner
epoch 以及当前的租约健康。详情必须是只读投影，不暴露 owner ID，不从列表文本反向猜测
authority 状态。

## 权威链路

1. `goal_interaction_detail` 以 Harness Store `get_interaction()` 作为唯一记录来源。
2. Tool 读取 Goal Store 的稳定 `pursuit_run_id` 集合，只允许查看当前 workspace Goal 已关联
   Pursuit 的 interaction。非 Pursuit、未关联、未知或非法 ID 都 fail closed。
3. 共享 `/goal interaction detail <id>` dispatcher 经 `Engine.execute_tool()` 执行；CLI 和
   Textual TUI 直接消费该结果，New UI Bridge 对 slash command 使用同一 dispatcher。
4. Goal snapshot 只保留列表所需的公开字段，每条记录给出 detail 命令；完整选项、答案不复制
   到长驻 snapshot。
5. 详情渲染器在读取时比较问题 deadline 和 owner lease deadline，分别呈现“租约生效”、
   “租约过期可接管”或“问题已到期应先 expire”，不把过期问题误报为可接管。

## 用户体验

- pending、answered、expired、cancelled 均可查看详情；只有 pending 显示取消命令。
- 选项同时显示 label、value 和 description；自定义回答使用既有有界、控制字符清理与脱敏后文本。
- 终态显示已选选项或自定义答案、回答时间；未回答终态不伪造答案。
- 页面显示 sequence 和 owner epoch 供并发诊断，但不显示 owner ID、session 内部身份或原始字典。
- 当租约过期且问题仍有效时，页面只声明“可接管”事实，明确告知手动动作尚未开放。

## 验收标准

- 真实 SQLite Harness Store 记录能通过 Tool 读取，且 Tool 不修改 interaction sequence/state。
- 未关联 Pursuit、不存在记录和非法 ID 均有中文失败结果，不泄露其他 Goal 的问题内容。
- 渲染器可确定性覆盖 live lease、expired lease 和 expired question，deadline 优先于 takeover 资格。
- New UI Goal 页和 Markdown fallback 为所有状态显示 detail 命令，只为 pending 显示 cancel。
- `/goal` 共享 dispatcher 将 detail/cancel 路由到不同 Tool，非法子命令不进入执行器。
- 定向 ruff、Python Tool/panel/dispatcher 测试和 Node Goal 组件测试通过。

## 保留边界与下一步

- 本切片不开放 takeover 写动作。手动 takeover 必须和当前 Bridge/TUI 宿主的 Future/Modal、
  keepalive 以及 replay 绑定后一次性提交，否则新 owner 没有任何界面承接问题。
- 本切片提供的是命令驱动详情 surface，还不是 Goal 页内的独立可展开详情页。
- interaction 列表仍是最近 50 项；cursor/筛选与优先级应与 HAR-10.3 durable queue 协调设计。
- 下一最小切片应先实现“宿主绑定 takeover-and-display”，而不是单独暴露 Store takeover 按钮。
