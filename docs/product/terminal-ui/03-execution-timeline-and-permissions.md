# 03 执行时间线与权限

## 实施状态（2026-07-14）

已完成“运行内安全取消”和“后端驱动运行活动组”两个切片。

运行内安全取消：

- 新增 `run_cancel -> ack -> run/cancelled` JSONL 生命周期，取消请求与目标提交使用独立 request ID 关联。
- 运行中第一次 `Ctrl+C` 只请求取消当前 run，状态栏显示“运行: 正在停止”，时间线给出可行动反馈；第二次才强制退出 UI。
- 空闲时 `Ctrl+C` 保持退出语义；取消完成后 Terminal UI、Bridge、会话和 Composer 均继续可用。
- Bridge 直接取消其持有的唯一 `_run_task` 并 await 终止，不在前端模拟取消，也不改造 AgentEngine 为第二条执行路径。
- Workbench 任务取消后，现有 cancellation 分支把 backing Task 从 `in_progress` 置为 `blocked`、刷新真实快照，并在 `run/cancelled` 返回任务与 Mission 身份。
- 普通对话取消不会伪造失败；权限卡、live tool prepare 和 pending cancellation 表现状态在终态事件中统一清理。
- 空闲取消返回 `no_active_run` 且无副作用；取消后继续提交的真实 Bridge 测试和取消后执行 `/doctor` 的双进程测试均通过。
- 定向验证：Terminal reducer/组件/协议/进程 133 项、Python Bridge 49 项、Ruff 全部通过。

后端驱动运行活动组：

- 每次 Bridge v1 运行只创建一条持久 `run_activity` 消息，阶段严格来自 `run/started`、`runtime_status`、assistant 边界、工具、权限和运行终态事件，不使用计时器伪造“思考中”。
- 活动组以“准备运行、生成响应、执行工具、等待权限、整理结果、完成/失败/取消”明文展示；不输出隐藏推理，也不从 `pytest` 等工具名猜测验证结论。
- `tool_prepare/use/result` 优先按 `tool_call_id` 去重；缺失 ID 时使用有界顺序回退，连续同名工具仍保持独立，单次运行的性能阶段和权限去重记录均有上限。
- 权限请求原地切换为“等待权限”，解决后回到工具执行；终态释放 active pointer，并把同一张完成收据移到时间线末尾，长工具输出后仍立即可见。
- 活动卡展示回合、模型、工具完成/失败数、权限请求数和后端测得的最近阶段耗时；独立工具卡继续保留可检查证据。
- 会话重放、`/clear`、失败和取消会清理旧运行指针；就地阶段更新同时失效渲染缓存，不会显示陈旧状态。
- 定向验证覆盖 reducer、组件、render cache、完整事件流和真实 Node 终端进程的提交、权限、工具与完成链路。

权限切片：

- `allow_once` 仅放行当前调用；中风险工具可提供 `grant_session`，授权按会话与工具族隔离并支持显式撤销。
- 高风险工具在 default 模式只确认一次，不再进入 token/二次确认流程，也不能创建会话授权。
- `bypass` 是明确的全权限运行模式：当前及后续工具直接放行，不执行未知工具、路径沙箱、危险命令、调用次数和确认检查。
- 新 Terminal UI 与 Textual TUI 使用同一后端语义；`b/Shift+Tab` 或 Bypass 按钮会切换全局模式并执行当前调用。
- 权限卡和面板明确展示允许一次、会话授权、拒绝、全权限四种结果；授权撤销通过 JSONL 协议同步到新 UI。

本模块仍未整体完成。下一切片是结构化 validation 事件，把真实验证进度纳入活动组。

权威实现计划与边界见：

- `docs/superpowers/plans/2026-07-13-terminal-run-cancel.md`
- `docs/superpowers/plans/2026-07-13-terminal-activity-group.md`

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

`requested -> focused -> allow_once | grant_session | deny | bypass -> resolved`

- `allow_once`：仅批准当前请求。
- `deny`：拒绝并把结构化原因返回引擎。
- `grant_session`：仅在策略允许时，对当前会话授权同一工具族；UI 必须显示范围和可撤销入口。
- `bypass`：切换为全权限模式，放行当前及后续工具；UI 必须明确提示其全局影响。
- 切页时权限请求仍保持全局可见；返回主界面后焦点恢复到该请求。

权限键 `Y/N/G/B` 仅在卡片聚焦且输入器无组合输入时生效。`Y` 允许一次，`N` 拒绝，`G` 仅在后端提供时创建会话授权，`B` 切换 bypass 全权限模式；高风险动作只确认一次。

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
- 权限事件增加 `choices`、`scope`、`expires_at`；兼容字段 `requires_double_confirm` 固定为 `false`。
- 取消请求增加客户端 `request_id`，服务端返回 accepted/rejected 结果。

字段缺失时 Bridge 负责兼容映射；前端不读取引擎私有对象。

## 8. 测试与验收

定向测试必须覆盖：工具事件合并、重复事件幂等、权限拒绝、会话授权与撤销、bypass 对未知工具/路径/危险命令/次数限制的全放行、高风险单次确认、取消竞态、断线恢复、失败重试和敏感字段脱敏。

真实验收至少运行一次只读工具、一次文件编辑、一次需要权限的命令、一次验证失败和一次用户取消。用户应始终知道系统在做什么、是否还在运行、是否需要自己行动。
