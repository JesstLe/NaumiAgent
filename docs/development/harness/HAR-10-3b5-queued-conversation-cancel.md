# HAR-10.3b5 Queued Conversation Cancel

## 目标

允许用户取消一条尚未跨过派发边界的普通排队对话，并让 New UI、Textual TUI 与 Harness SQLite 对“已取消、
从未执行”形成同一事实。取消不能终止当前模型运行，也不能把已 claim 或结果不明确的消息伪装成未执行。

## Authority 语义

`DurableConversationQueueAuthority.cancel_unclaimed_request()` 接受精确 request ID，重新读取当前 workspace/session
的 durable queue，并只把仍为 `queued` 且没有任何 RunLease claim 的消息提交为：

- `state=cancelled`；
- `terminal_reason=user_cancelled_before_dispatch`；
- 剩余消息按稳定顺序重新编号。

不存在、已完成或已离开队列的 request ID 返回稳定错误。只要存在 claim，无论 owner 是否为当前 UI，取消都
fail closed；用户必须等待终态或使用 `/queue` 审查历史 claim，不能借取消绕过副作用不确定性。

无 claim 校验与 terminal update 位于同一个 `BEGIN IMMEDIATE` 事务。Claim 取得 RunLease 后也会重新读取 queue
item；若 cancel 先赢，claim 会释放刚取得的 lease 并拒绝派发。这样 claim/cancel 竞争只能有一个赢家，不存在
“Store 已标记未执行，但 worker 仍拿到 claim”的窗口。

## 双端协议与体验

JSONL 协议新增：

- Client `queue_cancel {target_request_id}`；
- Server `run/queue_cancelled {target_request_id, queued, reason}`。

二者进入 protocol contract/event governance registry，target ID 严格必填且有长度上限，reason 按敏感字段规则
处理。New UI 提供 `/cancel-queued [request-id]`，省略 ID 时选择最新 scheduled 用户消息；Bridge 先提交 durable
cancel，再删除内存镜像、刷新剩余位置并发送回执。状态层把原消息保留为“已取消 · 未派发”，不会改变当前
`running`、不会显示 `/retry`，也不会把它写回 outbox。

Textual TUI 使用同名命令和相同 authority，可在模型运行中或空闲时取消。成功后显示精确 request ID；Store
失败、目标不存在或已 claim 均显示中文原因。没有当前 Session 时命令只提示“没有可取消消息”，不会为了查询
队列创建空会话。`/send-now` 同样遵守这一无 Session 边界。

## 验收证据

- Store 中精确取消第 2 条后，第 1 条保持 queued 并回到位置 1；取消结果绑定稳定 terminal reason；
- 对已 claim 消息调用 request cancel 被“已跨派发边界”阻断；
- 并发 claim/cancel 只有一个成功；cancel 胜出时没有 active lease，claim 胜出时消息保持 queued；
- 真实 Bridge + Harness SQLite 在 active run 期间取消一条 durable queued message，当前 run 与另一条消息继续
  执行，被取消文本从未进入模型；
- protocol model、JSON contract 和 event registry 完整覆盖新双向事件，非法 target 被拒绝；
- New UI 状态保持 `running=true`，消息渲染“已取消 · 未派发”，无重试误导；
- Textual Pilot 真实执行 `/cancel-queued`，并证明无 Session 时不会创建空会话；
- 20 个 Store 实例并发初始化时，v16 additive migration 可识别另一实例已添加的 `purpose` 列；同一并发测试
  连续 3 次通过，不再因 duplicate column 错误中止；
- Ruff、compileall、38 项 Python queue 定向测试、5 项 Node 定向测试与 95 个 JS 文件语法检查通过；未运行
  Python 或 Node 全量测试。

## 当前边界与下一步

本切片只取消从未 claim 的普通对话。它不取消 active run、不处置 ambiguous claim，也未提供队列详情页的
cursor/分页、terminal retention、跨客户端公平或持久优先级。下一条应先横向检查 CLI/TUI、Harness 与
Future Architecture，再决定是交付双端 queue detail/cursor，还是切换到更高优先级的 browser/agent heartbeat；
不得在 UI 中创建第二套取消状态机。
