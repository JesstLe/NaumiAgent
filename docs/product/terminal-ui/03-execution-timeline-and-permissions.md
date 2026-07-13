# 03 执行时间线与权限

## 1. 目标

把“发送中然后突然回复”升级为可理解、可控制、可恢复的执行体验。用户能看到 Agent 正处于规划、调用工具、等待权限、验证还是整理结果，但不会看到内部推理原文或敏感工具参数。

## 2. 运行状态机

```text
queued -> planning -> executing <-> awaiting_permission
                      -> validating -> summarizing -> completed
                      -> cancelled
                      -> failed
                      -> interrupted -> resuming
```

每次状态变化必须由后端事件驱动，并携带 `run_id`、`seq`、时间和可本地化的展示键。前端不得按超时猜测“正在思考”。

## 3. 活动组结构

每个运行在时间线中拥有一个 `activity_group`：

1. 折叠标题显示当前阶段、耗时、工具数和验证进度。
2. 展开后按事件顺序显示计划、工具卡、权限卡、验证和子 Agent。
3. 运行中默认展开关键活动；完成后自动折叠成功的低风险工具，只保留失败、警告和审批。
4. 同一 `tool_call_id` 的 prepare/use/result 合并为一张卡，不能产生三张重复卡。

## 4. 工具卡状态

`prepared -> running -> succeeded | failed | cancelled`

工具卡字段包括工具名、用户可理解的动作摘要、目标资源、风险级别、开始/结束时间、耗时、结果摘要和可展开证据。敏感 token、完整环境变量、隐藏 prompt 和未脱敏参数不得渲染。

文件编辑类工具额外显示改动文件数和增删行；命令类工具显示命令摘要、退出码和截断标记；浏览器类工具显示域名和动作，不显示 Cookie。

## 5. 权限状态机

`requested -> focused -> allow_once | deny | bypass_session -> resolved`

- `allow_once`：仅批准当前请求。
- `deny`：拒绝并把结构化原因返回引擎。
- `bypass_session`：仅在策略允许时，对当前会话降低后续同类确认；UI 必须显示范围和可撤销入口。
- 高风险操作不得提供 bypass。
- 切页时权限请求仍保持全局可见；返回主界面后焦点恢复到该请求。

权限键 `Y/N/B` 仅在卡片聚焦且输入器无组合输入时生效。首次按键选择，`Enter` 二次确认高风险动作，避免误触。

## 6. 取消、重试和中断

- `Esc` 不直接取消运行。
- `Ctrl+C` 第一次请求取消当前运行，显示“正在停止”；再次按下才强制退出 UI。
- 可重试失败工具必须创建新的 `tool_call_id` 并关联 `retry_of`。
- Bridge 中断后将运行标为 `interrupted`，重连时通过服务端事实决定恢复、已完成或不可恢复。
- 前端不得因为失去连接将运行标为失败。

## 7. 协议增量

在现有 `engine/event` 与 `ui/message` 基础上规范以下字段：

- `run_id`、`event_id`、`seq`、`timestamp`、`phase`。
- 工具事件增加 `risk_level`、`display_summary_key`、`retry_of`。
- 权限事件增加 `choices`、`scope`、`expires_at`、`requires_double_confirm`。
- 取消请求增加客户端 `request_id`，服务端返回 accepted/rejected 结果。

字段缺失时 Bridge 负责兼容映射；前端不读取引擎私有对象。

## 8. 测试与验收

定向测试必须覆盖：工具事件合并、重复事件幂等、权限拒绝、bypass 不可用、高风险二次确认、取消竞态、断线恢复、失败重试和敏感字段脱敏。

真实验收至少运行一次只读工具、一次文件编辑、一次需要权限的命令、一次验证失败和一次用户取消。用户应始终知道系统在做什么、是否还在运行、是否需要自己行动。
