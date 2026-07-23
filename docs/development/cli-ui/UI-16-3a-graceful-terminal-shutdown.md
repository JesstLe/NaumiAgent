# UI-16.3a 跨平台终端安全退出握手

## 问题

New UI 过去在 `/q`、空闲 `Ctrl+C`、`SIGINT` 或 `SIGTERM` 到达时，向 Python Bridge 写入
`shutdown` 后立即恢复终端、终止 Bridge 并退出 Node 进程。Bridge 的关闭路径实际上还需要进入 draining、
取消排队对话和交互、拒绝待确认权限、停止 Harness 任务、关闭 Engine 与 terminal runtime，最后才发出
`shutdown` 回执。前端提前杀死 Bridge 会让这条清理链失去完成证明，也可能留下不一致的持久状态。

同时，Slash 补全会在 Enter 到达时优先接受模糊候选，使精确的 `/q` 被 `/cancel-queued` 等候选截获；
“状态层认识 `/q`”并不等于真实输入链路可以退出。

## 合同

`frontend/terminal-ui/src/shutdown-controller.js` 是 New UI 的本地退出状态机：

1. `idle -> requested -> finalized` 单向转换，不允许重复发送或重复清理；
2. 首次退出请求先保存 UI snapshot、显示“正在安全关闭”，再向 Bridge 发送一次 `shutdown`；
3. Bridge 完成全部异步清理后，以同一个 `request_id` 返回 `shutdown`；
4. 前端只接受关联 ID 完全一致的在途回执；缺失或错误 ID 记为 mismatch，不能提前结束等待；
5. 正常等待上限为 1200ms，定时器创建失败、发送失败或 Bridge 提前退出都有明确终态；
6. 第二次操作系统信号视为用户强制退出，立即执行一次幂等清理；
7. Bridge 以稳定码 `runtime_shutdown_failed` 返回失败回执，或异常退出时，New UI 显示中文诊断入口并以状态码 1 退出；
8. cleanup 回调自身失败不能阻止最终退出；关闭等待期间发生 fatal error 时以状态码 1 退出，不能伪装为成功。

Python Bridge 的 `shutdown(request_id=...)` 在完成 runtime draining、队列/交互/权限清理、Harness
后台任务取消、Engine shutdown 和 terminal runtime close 之后，才发送带原始 `request_id` 的回执。
EOF 等没有客户端请求身份的关闭路径仍可不携带 ID。

## 用户入口与平台信号

- `/q`、`/quit`、`/exit` 是本地精确命令，Enter 时优先于 Slash 模糊补全，且不会进入聊天 outbox；
- 空闲 `Ctrl+C` 请求安全退出；运行中的第一次 `Ctrl+C` 仍保持既有“取消当前运行”语义；
- macOS/Linux 监听 `SIGINT`、`SIGTERM`、`SIGHUP`；
- Windows 监听 `SIGINT`、`SIGTERM`、`SIGBREAK`；
- Node `process.exit` 事件保留一次 terminal restore 兜底，但正常路径仍由状态机负责清理 Bridge、动画、
  painter 与终端控制序列。

## 可观测性

本地 debug JSONL 只记录低基数生命周期事件：

- `terminal_ui.shutdown.requested`
- `terminal_ui.shutdown.sent`
- `terminal_ui.shutdown.ack_mismatch`
- `terminal_ui.shutdown.bridge_exited`
- `terminal_ui.shutdown.timeout`
- `terminal_ui.shutdown.finalized`

记录包含来源、请求 ID、结果和退出码，不写用户输入、环境变量或凭据。`finalized` 只能出现一次。

## 验收证据

- controller 单元测试覆盖匹配/缺失/错误回执、超时、发送失败、定时器失败、Bridge 提前退出、
  remote shutdown、失败回执、异常 Bridge exit、重复信号与 cleanup 异常；
- Node 真实子进程测试让 fake Bridge 延迟 220ms 回执，证明 New UI 在 80ms 时仍存活并显示关闭状态，
  收到相同 ID 后才以 0 退出；
- `/q` 真实进程测试证明不会发送 `submit`，并防止 Slash completion 截获；
- Python Bridge 小测试证明 `shutdown` 回执保留请求 ID，且 Engine shutdown 已完成；
- shutdown failure 小测试证明异常详情不会进入协议 payload，前端收到关联失败回执后以状态码 1 退出；
- JavaScript syntax check、目标 Python ruff 和上述聚焦测试通过。

## 未完成边界

本切片没有实现 Windows GUI 关闭窗口的原生 Console Control Handler，也没有运行 Windows Terminal、
PowerShell、cmd、WSL、macOS 和 Linux 的完整真实终端矩阵；`SIGKILL` 等不可捕获信号也不可能获得握手。
Textual TUI 继续使用自身终端生命周期，不复用 Node controller。路径、换行、shell quoting、无色输出和
系统化无障碍 QA 仍由 UI-16 其他切片负责。因此 UI-16 和 UI-17 发布门继续保持 partial。
