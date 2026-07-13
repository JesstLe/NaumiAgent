# Todo Lifecycle Reconciliation Design

## Goal

让 Agent 通过现有 `todo_write` 和 `task_update` 主动维护真实 Todo 状态，并由后端在最终回答前执行终态对账，避免遗留虚假的 `in_progress`。

## Existing Behavior

- `TodoWriteTool` 已支持 `merge`、`replace`、稳定 ID、依赖关系和批量状态更新。
- `TaskUpdateTool` 已支持按 ID 更新状态。
- 每次任务工具执行后，Engine 已能发送 `task_snapshot`。
- 当前 ReAct 循环允许 Agent 在仍有 `in_progress` Todo 时直接给出最终回答。

## State Invariants

1. 一个会话最多存在一个 `in_progress` Todo。
2. `completed` Todo 不允许回退。
3. Agent 必须显式调用任务工具声明完成，后端不得根据普通工具成功自动推断完成。
4. 最终回答前若存在 `in_progress`，Engine 必须发起一次对账回合。
5. 对账回合仍未消除 `in_progress` 时，后端将其改为 `blocked`，原因固定为“Agent 结束前未完成状态对账”，并允许输出最终回答。
6. 对账最多发生一次，不能形成无限循环或额外消耗不受控的模型轮次。

## Architecture

新增 `TodoReconciliationResult`，由 Engine 的 `_reconcile_todos_before_final()` 读取 `TaskStore`。首次发现残留时返回一条系统指令，ReAct 循环将指令加入上下文并进入下一轮；第二次仍残留时调用 `TaskStore.block_unreconciled_tasks()` 原子更新状态，并发送 `task_snapshot`。

非流式和流式 ReAct 循环使用同一底层对账方法。流式循环必须在发送 `response_start` 前完成判断，避免先向用户展示一个随后被撤回的最终回答。

## Agent Contract

系统提示明确要求：复杂工作开始前建立 Todo；每完成一步立即更新；最终回答前将 `in_progress` 更新为 `completed`、`blocked` 或 `pending`。工具返回继续包含完整任务列表，使模型拿到稳定 ID。

## Error Handling

- TaskStore 读取失败时不阻断最终回答，但记录 warning，并发送可见的 `task_reconciliation_warning` 事件。
- 原子阻塞更新失败时保留原状态，最终结果标记为 `partial`，错误写入 debug trace。
- 预算或最大轮次已耗尽时不再请求额外模型回合，直接执行阻塞兜底。

## Verification

- 单元测试覆盖首次残留触发对账、第二次残留转 blocked、无残留直接完成、已完成任务不变化、存储失败降级。
- 非流式和流式循环各有一条模型双回合测试。
- 使用临时 SQLite 跑真实 TaskStore，不以纯 mock 代替状态持久化验证。

