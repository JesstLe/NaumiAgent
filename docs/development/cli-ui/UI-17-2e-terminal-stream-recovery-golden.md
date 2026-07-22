# UI-17.2e Terminal Stream Recovery Golden Scenario

## 1. 目标

用一个共享 fixture 锁定最终回复的流式合并、相关运行错误后的断流收尾，以及 New UI 本地发送失败后的安全重试。
本切片修复终端已经失败但界面仍保留活跃回复或显示成功状态的问题，不把模型执行失败误当作可直接重放的发送失败。

## 2. 权威合同

`tests/fixtures/ui17/terminal-stream-recovery-golden.json` 是唯一测试输入，包含：

- `response_start → token → token → response_end` 的 Engine 原始事件、规范 `assistant_stream` 消息和合并结果；
- 未收到 `response_end` 时的部分正文、相关 `error`、稳定 request/run identity 和 `interrupted` 终态；
- 本地 Bridge 写入失败、`/retry <request-id>`、新 request identity、原消息 identity 和 attempt 递增；
- 已被 `run/started` 接受的用户消息在执行错误后仍是 `accepted`，不得重新进入发送 outbox。

Python adapter、Textual TUI renderer 和 Node reducer 都消费该文件。测试不得复制第二套 token、错误文案或 retry identity。

## 3. 流式与错误不变量

- 同一回复的 token 只追加到一个 assistant message；正常 `response_end` 将其标记为 `completed`。
- 相关运行错误或取消必须关闭 `activeAssistant` 和活跃 thinking；已接收的部分正文保留，并显示“回复流已中断”。
- 相关运行错误同时清理工具准备、运行阶段、权限与待处理交互，避免后续一轮继承失效状态。
- 不相关 request 的错误只能产生自己的错误反馈，不能停止当前运行或关闭当前回复流。
- Textual TUI 的类型化错误必须把状态栏切换为“执行失败”；`AgentResult.status=error` 的最终状态使用 `❌`，不得显示 `✅`。

## 4. Retry 边界

New UI 的 `/retry` 只处理 `failed|uncertain` 的本地发送状态：复用原用户消息、增加 attempt、清空旧错误，并分配新 request id。
Bridge 已用 `run/started` 接受的消息即使随后执行失败，也不具备这一 retry 资格，避免重复产生工具副作用。

Textual TUI 直接调用 Engine，不存在 JSONL Bridge 写入失败或不确定 outbox，因此不伪造相同的 `/retry` 状态机；执行失败后用户可用输入历史
明确重新提交，从而创建一轮有新身份的运行。两端共享的是“发送失败可恢复、已接受执行不得静默重复”的语义，而不是虚构相同 transport。

## 5. 验收证据

- Adapter 对 start/token/end/error 逐字段匹配共享 fixture；
- TUI renderer 消费相同消息并进入失败状态，真实 Textual Pilot 对 error result 显示 `❌`；
- New UI 将正常 token 合并为一条 completed 回复；
- 相关 error 关闭 partial stream、保留正文和 accepted 用户消息，不相关 error 不影响运行；
- `/retry` 复用同一消息 id，request id 从 `submit-1` 变为 `submit-2`，attempt 变为 2；
- UI-17.2c 工具结果 fixture 同步当前分页/摘要公开字段，旧 golden 不再漂移；
- Python 聚焦测试 62 项（含真实 Textual Pilot）及 Node 聚焦测试 9 项通过；未运行全量测试。

## 6. 边界与下一步

本切片不证明 UI-17.2 或 UI-17 发布门完成。Bridge 进程断开后的 `uncertain` 持久恢复、权限中断后续流、Harness
Receipt/Explain/Replay 完整 snapshot，以及通用 capability negotiation 仍需独立切片。下一步应重新比较 CC-05、HAR-10
后续与 UI-17.3 的依赖，不继续线性扩张 UI-17。
