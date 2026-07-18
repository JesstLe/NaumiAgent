# HAR-10.3a 安全边界队列提升

## 目标

让用户在模型正在工作时，把一条已经排队的普通对话提升为“下一条执行”，同时不取消、不打断当前模型调用、
工具调用或完成回执事务。该切片交付立即发送的用户闭环和严格的队列重排语义，不宣称完成持久化队列。

## 用户协议

- New UI 使用 `/send-now [request-id]`；省略 ID 时选择最近一条仍处于 `scheduled` 的用户消息。
- 前端发送 `queue_promote`，必须携带明确的 `target_request_id`，Bridge 不接受模糊目标。
- 成功后 Bridge 发出 `run/queue_promoted`，包含新位置、队列长度和
  `boundary=after_current_run`，随后重发所有排队位置。
- 目标已经开始、完成、取消或不存在时返回 `queue_item_not_found`，不改变队列。

## 调度不变量

1. 当前运行永远不被队列提升打断。
2. 被选消息移动到队首，其余消息保持原相对顺序。
3. 重复提升队首是幂等重排，不产生重复消息。
4. Bridge 仍执行既有容量上限；提升不能绕过准入或创建新队列项。
5. 当前运行无论成功还是受控失败，提升项都在终态清理后的下一安全边界启动。

## 验收证据

- Python 协议只保留有界的 `target_request_id`，拒绝空值与超长值。
- Bridge 真实异步运行证明执行顺序从 `active, first, second, third` 变为
  `active, third, first, second`，且提升前当前任务未被取消。
- 未知目标返回类型化错误并保持原队列顺序。
- Node 协议能发送提升请求并规范化回执；状态层能选择最近或指定的排队消息，并展示安全边界反馈。

## 已知边界与后续

HAR-10.3b1/3b2 已在本切片之后交付 durable queue Store、幂等 enqueue、RunLease claim/fencing 和 New UI
显式 Session 恢复；未 claim 的等待项不再因进程崩溃丢失。历史 claim 人工 resolve、TUI 运行中输入、跨客户端
公平性、持久优先级、取消传播、retention 和 cursor 仍未完成。
