# HAR-10.8f2c Pursuit 终态 Outbox 显式恢复控制

## 目标

在 HAR-10.8f2a 自动 worker 和 HAR-10.8f2b 只读投影之上，提供一次用户可见、可授权、可审计的
“立即恢复”动作。动作只执行一个现有 worker 的有界 pass，不改变退避时间，不抢占有效 claim，也不
新增第二套恢复算法。

## 权威链

1. New UI `o` 键或 `/pursue outbox run-now` 进入
   `pursuit_terminal_outbox_run_now` Agent Tool；
2. Tool 由统一 ToolExecution/PermissionChecker 执行，default 模式服从权限交互，bypass 直接放行；
3. Engine 调用现有 `PursuitTerminalOutboxWorker.run_once()`；worker 的 single-flight lock 串行化本进程
   pass，Store claim epoch/lease 栅栏其他扫描者；
4. `claim_next_terminal_outbox()` 只选择 due idle claim 或 expired claim，因此显式动作不穿透持久退避；
5. Engine 将结果写入 `pursuit_terminal_outbox_run_receipts`，Bridge 只从该权威回执生成 typed result。

Bridge 不直接调用 worker，也不把 Tool 文案解析为状态。

## 不可变回执

`PursuitTerminalOutboxRunReceipt` schema v1 包含请求摘要派生的 identity-free `receipt_id`、执行状态、
pass 前后积压、claimed/delivered/retry/failure 计数、类型化 failure code、UTC 时间与完整 SHA-256。
同一请求摘要采用 first-write-wins；Bridge 不投影原始请求、请求摘要、claim owner、outbox/attempt
identity 或内部异常文本。

## 三端体验

- New UI Goal 页：`o 恢复终态队列`，显示 ToolExecution 等待态和类型化结果；
- JSONL：`pursuit/terminal-outbox/run_now` → `pursuit/terminal-outbox/action_result`，沿用
  `pursuit_recovery_actions` capability；
- CLI 与 Textual TUI：`/pursue outbox run-now`，调用同一 Agent Tool；
- 动作结束后 Bridge 刷新 `goals/snapshot`，队列/worker 仍以 HAR-10.8f2b 投影为权威。

## 验收标准

- disabled、并发 action、ToolExecution 拒绝和回执缺失均失败关闭；
- no-due pass 不伪造 claimed/delivered，重复 source request 不重复执行本进程 worker；
- 回执摘要或 payload 被篡改时 Store 拒绝读取；
- New UI 拒绝状态与回执不一致、非法计数、非法 digest 和缺失回执；
- New UI、CLI、Textual TUI 均不绕过 ToolExecution；
- 只运行 Store/Engine/Bridge/协议/Goal 页面/命令分发小模块测试，不运行全量测试。

## 自我审视与后续

- worker 完成后、回执写入前崩溃时，重试是 at-least-once；per-record claim、fencing 和 reconcile
  幂等保证安全，但 action 尚无 prepare/terminal 双事件账本；
- single-flight 只覆盖单进程；多主机可处理互不重叠记录，但没有全局 action lease；
- 不提供强制跳过 backoff、pause、dead-letter、prune、历史分页或 push progress；
- kill-at-every-write-point、跨主机时钟漂移、三平台进程杀死与 24 小时 soak 仍未完成。
