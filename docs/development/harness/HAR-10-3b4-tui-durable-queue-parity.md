# HAR-10.3b4 TUI Durable Conversation Queue Parity

## 目标

让 Textual fallback TUI 在模型运行期间继续接受输入，并与 New UI 复用 HAR-10.3b 的持久队列、RunLease claim、
恢复阻断和 `/send-now` 安全边界。TUI 不维护第二份内存队列，也不会为“立即发送”打断正在执行的模型事务。

## 输入与持久化

- 空闲时首条普通消息仍直接启动 `AgentEngine.run_streaming()`；
- 运行中输入框和发送按钮保持可用，普通消息先由 `DurableConversationQueueAuthority.enqueue()` 写入
  Harness Store，再显示“已排队、位置、request ID”；
- Session 创建与 queue enqueue 共用异步锁，避免首轮启动与快速第二次输入产生两个 Session；
- Store 不可用、20 条容量上限或 request conflict 都显示中文、可执行的失败信息，未持久化消息不会伪装成功；
- 模型运行中除 `/send-now` 与退出命令外，不并发执行 slash command，避免 Textual exclusive worker 意外取消
  当前模型事务。

## Claim、续租与终态

当前运行结束后，TUI 重新读取 durable queue，只领取无历史 claim 的安全前缀首项。每条排队消息：

1. 通过共享 Harness RunLease 获取精确 owner/epoch claim；
2. 执行期间按 lease 时限续租；续租丢失会取消当前 worker，不提交可能重复的终态；
3. completed/failed/cancelled 通过 `finish()` 以 owner/epoch fencing 写入并释放 lease；
4. 终态提交成功后才领取下一条；历史、过期或外部 claim 会停止自动派发并提示使用 `/queue` 核对。

因此 TUI 和 New UI 使用同一 Store 状态机，重启后不会把“可能已经派发”的消息当成从未执行。

## `/send-now` 的共享修复

无参数 `/send-now` 选择最新等待消息；显式 request ID 可选择指定项。重排只改变下一安全执行位置，不中断当前
run。共享 authority 现在允许调用方绕过“由同一 owner 持有、identity/epoch/expiry 完全匹配”的当前 live claim，
但目标必须是它后面尚未 claim 的消息；目标是 active item、owner 不同、claim 漂移/过期或后缀存在第二个 claim
时仍 fail closed。New UI Bridge 与 TUI 都传入各自当前 claim，修复效果保持一致。

## 验收证据

- Textual Pilot 在 `_agent_busy=true` 时确认输入框仍可编辑，两条中文消息真实写入 Harness SQLite；
- `/send-now` 把最新消息从第 2 位提升到第 1 位，UI 与 Store 顺序一致；
- 两条持久消息通过真实 claim 连续派发，模型调用顺序稳定，两个 Runtime lease 均进入 released；
- Session 创建失败仍关闭 spinner、清除 busy 并恢复输入，不会把 TUI 卡死；
- 同 owner live claim 可重排未 claim 后缀，但不能提升 active item；外部/历史 claim 原有阻断保持；
- New UI 原有 queued-chat promotion 回归通过；
- Ruff、编译与 19 项 queue 定向测试通过，未运行全量测试。

## 当前边界与下一步

本切片只补运行中 TUI queue parity。HAR-10.3b5 已进一步交付普通 queued item 的显式取消与双端回执；详见
`HAR-10-3b5-queued-conversation-cancel.md`。跨客户端公平/优先级、cursor/分页、retention 和启动时无 Session
的自动恢复仍未实现；这些必须继续扩展共享 Store/authority，不能在 TUI 添加本地状态。
