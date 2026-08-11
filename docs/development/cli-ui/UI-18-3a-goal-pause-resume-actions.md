# UI-18.3a Goal 暂停与恢复动作

## 目标

让 New UI 的 Goal 页面可以直接暂停进行中的 Goal、恢复已暂停的 Goal，同时保持 CLI、Textual TUI 和
Agent Tool 已有的 `/goal pause`、`/goal resume` 能力共用同一 `goal_update` ToolExecution。前端只发出
意图并展示回执，不直接修改 Goal 快照，也不把工具返回文案当成状态权威。

## 协议

- capability：`goal_lifecycle_actions`；旧 Bridge 没有协商该能力时，页面明确降级到 `/goal pause` 或
  `/goal resume`。
- client event：`goal/lifecycle/update`，只接受稳定 `goal_id` 和 `pause|resume`，未知字段失败关闭。
- server event：`goal/lifecycle/action_result` schema v1，返回 action、结果状态、稳定 code、中文 message
  和重新读取的 `goal_status`。
- 新旧 event registry 摘要进入兼容账本；client/server 事件均声明 control/audit 策略，结果 message
  经过既有敏感字段脱敏通道。

## 权威与并发边界

- `pause` 只允许 `active → paused`，`resume` 只允许 `paused → active`。blocked、completed、cancelled
  在 ToolExecution 前拒绝；block/complete/cancel 由后续独立切片处理。
- Bridge 在后台任务中调用 `goal_update`，因此 moderate 模式可展示并等待统一 Permission 请求，bypass
  模式沿既有权限规则直接通过，不增加第二次确认。
- 执行前读取并透传原 Goal note，暂停/恢复不会意外清空用户说明。
- ToolExecution 结束或异常后必须重新读取 GoalStore。只有持久状态达到目标状态才能报告 completed；
  不解析 `ToolResult.content` 推断成功。
- 同一 request ID 单飞，总并发上限为 4。不同 request ID 的并发同向请求以最终 SQLite 状态幂等收口。
- 动作完成后 Bridge 发送新的 `goals/snapshot`；Node 不在 action result 中就地改 Goal 状态。

## 用户体验

- New UI Goal 页面用 `m` 对所选 Goal 执行上下文相关动作；active 显示“暂停”，paused 显示“恢复”。
- 页面用黄色展示权限/执行中状态，绿色展示成功回执，红色展示冲突或失败。
- terminal/blocked 状态不展示伪按钮，并说明页内可逆操作不可用。
- TUI fallback/Agent Tool 详情显示共享命令：active 为 `/goal pause`，paused 为 `/goal resume`。

## 验收标准

- 协议拒绝非法 ID、complete 等越权 action 和未知字段；Node 拒绝 completed 回执与目标状态不一致。
- active Goal 经 Bridge、ToolExecution、GoalStore 后成为 paused；原 note 保持不变，并刷新 typed snapshot。
- terminal Goal 在调用工具前返回 state_conflict；未协商 capability 时 New UI 不发送事件。
- 两个并发 pause 请求不会让状态回滚或重复迁移，最终均以 paused 权威事实收口。
- action result 只清除完全匹配 goal/request 的 pending 状态，不直接修改旧 snapshot。
- Python/Node 定向测试、Ruff、语法、协议注册表、YAML 和真实临时 SQLite 场景通过。

## 诚实边界

本切片只交付可逆 pause/resume。Goal create、需要原因输入的 block、终态 complete/cancel、撤销终态、
跨实例主动状态推送与完整审计时间线仍未实现。UI-18.3 和 UI-18 整体继续保持 partial。
