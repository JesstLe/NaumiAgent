# HAR-10.2g Agent Execution Heartbeat Producer

## 目标

让真实子 Agent 委派执行成为 HAR-10.2 typed heartbeat 的生产者。此前 Agent Control 中的
`heartbeat_age_ms` 只是进程内事件更新时间：进程退出后不可读取，也不能区分正常完成、用户取消、超时、模型失败与
心跳存储降级。本切片复用 Harness heartbeat authority，不创建第二套表或 UI 私有状态。

## 权威边界

每个 `SubAgentManager.delegate()` 执行在注册成功后、模型开始执行前创建一个 `agent` heartbeat lifecycle：

1. `starting/agent_starting` 与 `running/agent_running` 在派发前落入 Harness Store；
2. 执行期间默认每 10 秒写 `running/agent_alive`，timeout 为 30 秒；
3. 收尾先写 `draining/agent_draining`；
4. `completed` 写 `stopped/agent_completed`；
5. 用户停止或父任务取消写 `stopped/agent_cancelled`；
6. `timeout`、`max_turns` 和其他错误分别写 `failed/agent_timeout`、
   `failed/agent_max_turns`、`failed/agent_failed`。

Heartbeat 只表达可观测存活与终态，不授予执行、takeover、结果提交或恢复权限。Agent RunLease 与结果 fencing 仍属于
HAR-10.1 的后续逐域接入。

## Identity、epoch 与并发

- `subject_id` 由 workspace、session、task ID 和 agent name 计算 SHA-256，稳定且不暴露任务正文；
- `instance_id` 每次实际委派随机生成，区分同一逻辑执行的不同实例；
- 同一 subject 重跑时读取上一 heartbeat 并使用 `epoch + 1`，sequence 从 1 重新开始；
- 同一进程的重复 active task ID 仍由 SubAgentManager 拒绝；跨进程同时争用相同 subject 时，Harness 的
  epoch/instance 单调约束会让其中一个 heartbeat fail closed，而不是互相覆盖；
- 多个不同任务共享同一 Agent 实例时，每个执行持有独立 lifecycle。终态收尾不得覆盖 Agent 本身的内存 lifecycle；
  12 路并发测试覆盖了这一边界。

## 降级与用户体验

Heartbeat 是诊断能力，不改变已经产生的 Agent 业务结果：

- startup 写入失败时，Agent 仍可执行，Agent Control 显示
  `agent_heartbeat_start_failed`；
- 周期写入失败时 producer 停止刷新，最后一条 durable 记录自然进入 stale/offline，并通过
  `heartbeat_write_failed` 暴露；
- terminal 写入失败时保留原 Agent result，显示 `agent_heartbeat_terminal_failed`；
- 错误码是固定、无 secret 的机械代码，数据库路径和底层异常正文不会进入协议。

Agent Control schema 升级为 v2，`ExecutionDescriptor` 新增：

- `heartbeat_subject_id`
- `heartbeat_phase`
- `heartbeat_failure_code`

New UI 与 Textual TUI 使用同一后端快照：New UI 在执行详情中合并显示持久心跳阶段/降级，TUI 显示同样的字段；颜色
只是辅助，阶段和错误码始终保留文字。

## 验收证据

- 真实 SQLite 跨 Store reopen 可读取 `starting → running → draining → stopped/failed` 最终 snapshot；
- 同一 subject 二次执行使用新 instance 和递增 epoch；
- completed、cancelled、timeout、max_turns、error 的 phase/detail code 都有确定性测试；
- startup Store 故障不改变 Agent completed result，且不泄漏底层错误正文；
- 12 路真实异步委派得到 12 个独立 durable subject 和完整终态；
- Agent Control v2 的 Python 严格模型、Bridge JS 规范化、New UI 和 TUI 渲染均有聚焦测试；
- Ruff、Python compile、相关小模块测试通过；不运行全量测试。

## 当前边界

本切片没有实现 agent RunLease、跨 kind worker catalog、agent heartbeat retention/history、跨进程取消传播、自动重启或
Supervisor 动作。硬崩溃/SIGKILL 无法写 terminal，最后一个 running heartbeat 依照 timeout 进入 stale/offline，后续
恢复不得仅凭 heartbeat 自动接管。下一步应在 browser producer、跨 kind catalog/history 或 agent lease/fencing 中根据
跨文档依赖选择一个最小闭环，不直接扩张为完整集群调度器。
