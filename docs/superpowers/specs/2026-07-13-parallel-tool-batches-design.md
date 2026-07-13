# Parallel Tool Batches Design

## Goal

让同一模型回合返回的独立工具调用真正并行执行，同时以串行屏障保护写操作、权限确认和非并发安全工具。

## Scheduling Policy

1. 只有 `Tool.metadata.concurrency_safe=True` 的工具可进入并行批次。
2. 连续安全调用组成一个并行批次，最大并发数由 `safety.max_parallel_tools` 控制，默认 4，范围 1 至 16。
3. 非并发安全调用是串行屏障，必须等待前一批结束，并在完成后才允许下一批开始。
4. 调用解析、重复调用检测和 Hook start 均在各调用实际执行前完成。
5. 每个工具独立处理权限、超时和错误；一个并行调用失败不会取消同批其他调用。
6. Engine 按模型原始调用顺序将 tool messages 写回上下文，不能按完成顺序重排。

## Architecture

新增 `orchestrator/tool_batches.py`：

- `ScheduledToolCall` 保存原始序号和 `ToolCall`。
- `ToolBatch` 表示一个并行批次或单个串行屏障。
- `build_tool_batches()` 根据 ToolRegistry 元数据构建稳定批次。
- `execute_tool_batch()` 使用 `asyncio.TaskGroup` 和 Semaphore，返回按原始序号排序的结果。

Engine 提供统一 `_execute_tool_calls()`，非流式和流式循环共享调度、Hook、重复检测和消息写回逻辑；流式版本通过可选 `on_event` 发送每个调用独立的 start/end 事件。

## Cancellation And Permissions

- Engine 被取消时，TaskGroup 取消所有仍运行的工具，并等待清理完成。
- 权限请求仍由 `_execute_tool()` 内部处理。要求确认的工具默认不标记 `concurrency_safe`，因此不会出现多个同时等待用户确认的弹窗。
- Hook 中止只影响对应调用；串行屏障被中止后仍允许后续批次执行，除非 Hook 明确设置整轮 abort。

## Configuration

`SafetyConfig.max_parallel_tools: int = 4`。配置小于 1 或大于 16 时启动校验失败。设置为 1 时保持完全串行，作为兼容和故障排查开关。

## Observability

工具事件增加 `batch_id`、`batch_size`、`parallel`。现有 TUI 可以同时保留多张 activity card；结果卡仍按调用 ID 更新。调试日志记录批次开始、结束和墙钟耗时。

## Verification

- 两个真实异步安全工具通过 barrier 证明同时运行，墙钟时间接近单个调用。
- 安全、安全、非安全、安全的序列必须形成三批，且非安全调用前后无重叠。
- 同批一个失败、一个成功时两个结果都写回。
- 消息和 tool_end 事件顺序按原始调用顺序稳定。
- `max_parallel_tools=1` 保持串行。
- 非流式和流式 ReAct 路径均覆盖。

