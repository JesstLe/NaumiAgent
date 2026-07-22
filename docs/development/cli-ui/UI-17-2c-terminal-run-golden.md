# UI-17.2c Terminal Run Lifecycle Golden Scenario

## 1. 目标

用一个共享 fixture 锁定 New UI 与 Textual TUI 的基本运行闭环：用户提交、工具准备、工具开始/结束、类型化完成回执和
Ctrl+C 取消。它验证的是跨表面的公开语义，不替代 Engine、Harness 或 JSONL 各自已有的详细测试。

## 2. 权威合同

`tests/fixtures/ui17/terminal-run-lifecycle-golden.json` 是唯一测试输入，包含：

- 稳定的 submission request id、文本、client event 与初始 delivery status；
- `tool_prepare_start/end`、`tool_start/end` 原始 Engine 事件和去除内部字段后的规范 UIMessage；
- 完成回执的输入与 `CompletionReceipt.from_dict()` 补全默认字段后的完整公开结构；
- Ctrl+C 取消原因、client event、cancel request id 和 `cancelled` 终态。

Python adapter、TUI renderer 和 Node reducer 都消费该文件，不在测试内复制第二套期望值。tuple/list 等语言内部差异在
JSON 边界规范化，业务字段不得因此被省略或放宽。

## 3. TUI 取消行为

此前 TUI capability manifest 声明 `run_cancel`，但没有用户可触发的绑定。本切片补齐真实行为：

- 运行中首次 Ctrl+C 取消 Textual Worker，并明确显示“正在停止”；Worker 收到 cancellation 后显示“已取消当前运行”；
- 取消请求尚未完成时再次 Ctrl+C 可强制退出，避免失控任务困住终端；
- 空闲时 Ctrl+C 不退出，提示使用 Ctrl+Q；因此误触不会关闭 UI；
- 每次新运行开始与结束都会复位 cancel pending，不能污染下一轮。

## 4. 验收证据

- Python adapter 对四个工具事件和完成回执逐字段匹配共享 fixture；
- TUI renderer 对相同 UIMessage 产生准备、工具结果和完成回执状态；
- 真实 Textual Pilot 提交 fixture 文本，阻塞中的 `run_streaming` 收到 Ctrl+C cancellation，UI 恢复 idle；
- Node reducer 完成 submit→run started→tool lifecycle→receipt→run completed，并验证 delivery、tool 与 receipt 终态；
- Node Ctrl+C 发送 fixture 指定的 `run_cancel`，随后清除 pending 并保留取消活动记录；
- Python 定向子集 20 项与 Node 定向测试 2 项通过；Ruff 与 Python compile 通过；未运行全量测试。

## 5. 边界与下一步

本切片不宣称 UI-17.2 或 HAR-07.6 全部完成。流式 token 合并、相关 error 断流与本地发送 retry 后续已由
[UI-17.2e](UI-17-2e-terminal-stream-recovery-golden.md) 锁定；进程断开后的 uncertain 恢复、权限中断后的恢复，以及
Harness Receipt/Explain/Replay 完整字段 snapshot 仍需独立 golden。
