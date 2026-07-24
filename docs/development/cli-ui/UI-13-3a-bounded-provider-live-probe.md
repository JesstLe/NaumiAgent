# UI-13.3a 受控 Provider 在线探测

## 问题

UI-13.2a 已提供稳定 Provider 诊断码，但实时连接只能通过顶层 `naumi doctor --live` 验证。New UI 与
Textual TUI 缺少显式入口、请求预算、超时、取消和终态回执；若直接让 Doctor 页面加载时联网，则会产生
意外费用、重复请求和钥匙串访问，也无法区分“本地刷新”和“真实模型调用”。

本切片把实时连接收拢为一个共享、单次、有界且可取消的动作。它不改变 `/doctor` 的本地只读语义，也不
引入第二套 Provider 探测器。

## 权威合同

`naumi_agent.ui.doctor_probe` 是 CLI、New UI Bridge、Textual TUI 和 Agent Tool 共用的动作 authority：

- 每次显式动作最多调用 Provider **1 次**；
- 请求最多生成 **8 个输出 token**；
- 默认超时 **15000 ms**，公开可选范围为 `1000..60000 ms`；
- 不做自动重试，不把 429/5xx 转换为隐式第二次请求；
- Provider 本地前置检查失败时请求数为 `0`，返回 `blocked`；
- 超时、认证、404、429、5xx、连接错误继续复用 UI-13.2a 的稳定诊断码；
- `CancelledError` 穿过共享 authority，由所属界面生成 `cancelled` 终态，不伪装成 Provider 故障；
- 原始异常正文、response body、API key、完整 URL、prompt 与 reasoning 不进入公开回执。

打开 `/doctor` 或按 `r` 只运行本地 Doctor。只有 `/doctor probe [timeout-ms]`、Doctor 页按 `p`、
顶层 `naumi doctor --live`，或 Agent 明确调用 `doctor_live_probe` 时才会发送真实请求。

## Typed 协议

能力 `doctor_live_probe` 绑定：

- client `doctor/probe`：只允许一个 `timeout_ms`；
- client `doctor/probe/cancel`：只允许一个精确 `target_request_id`；
- server `doctor/probe/result`：返回 schema、终态、稳定诊断码、中文详情/建议、实际请求数、请求上限、
  token 上限、请求耗时、超时和对应 Health snapshot 摘要。

Bridge 在后台 Task 中运行探测，因此取消和其他控制事件不会被 Provider 请求阻塞。相同 Bridge 同时只允许
一个探测；第二个请求返回 `doctor_probe_busy`。取消只接受当前精确 request ID，并分别返回原动作的
`cancelled` 终态和取消控制的 ACK。Bridge shutdown 会取消并 join 探测 Task，不在关闭后自动重试或写出
伪终态。

协议注册表摘要覆盖上述事件和 capability binding；变更前摘要已进入兼容账本。旧 Bridge 未声明能力时，
New UI 显示兼容提示且不发送模型请求。

## 界面

### New UI

- `/doctor probe [timeout-ms]` 直接进入 Doctor 页并启动一次显式探测；
- Doctor 页始终显示 `1 request / 8 output tokens / timeout / no retry`；
- `p` 启动，`c` 取消；运行期间 `r` 和 `e` 不制造并发 Doctor/Export 动作；
- `doctor/health.live_probe=true` 明确标记快照含真实连接证据；
- 终态按通过、失败、阻止、取消分别着色，同时保留文字标签和诊断码，不只依赖颜色；
- 新会话 replay 清除 probe 本地状态，不把上次探测恢复成正在运行。

### Textual TUI

- `/doctor probe [timeout-ms]` 使用同一个共享 authority；
- `/doctor probe cancel` 取消当前 Worker；
- 请求前显示精确预算，终态显示同一报告与回执；
- 本地 `/doctor` 和导出流程保持零 Provider 流量。

### CLI 与 Agent Tool

- `naumi doctor --live` 改为共享受控 authority；
- 旧 CLI slash `/doctor probe [timeout-ms]` 经 Engine policy 调用 `doctor_live_probe`；
- `doctor_live_probe` 是独立 Tool，避免普通 `doctor_diagnostics` 被 Agent 误解为会产生外部请求；
- bypass 不产生二次确认；其他模式仍由既有 ToolExecution 权限策略决定是否允许网络诊断。

## 验收证据

- 成功路径只调用一次 probe，并回显 `1/1`、`8 tokens` 与精确 timeout；
- 1000 ms 超时只调用一次并返回 `provider_timeout`，没有 retry；
- Provider/模型配置冲突在网络前阻止，请求数为 `0`；
- 运行中的 probe 可取消，Bridge 保持响应，原请求与取消 ACK 精确关联；
- 非法 timeout、额外 payload 字段、错误 target、重复运行和非法 server receipt 均失败关闭；
- Provider 异常中的 secret-shaped 文本不进入 receipt；
- New UI 在 80/120/200 列展示本地零网络边界、预算、状态、诊断码和下一步；
- 真实 terminal-ui 子进程通过 `/doctor probe 12000` 只发送一个 typed 请求并展示终态；
- 仅运行 Doctor/协议/Bridge/TUI/Tool/New UI 定向测试，不运行全量测试。

## 自我审视与保留边界

- 取消可以停止当前 Python await 与 HTTP client 调用，但不同 Provider SDK 是否立即关闭底层 socket 仍由
  LiteLLM/httpx 取消语义决定；当前没有跨进程 Provider request ID，因此不能对已被上游接收的请求做远端撤销。
- 探测证明的是该时刻一次最小请求可达，不等价于吞吐、SLO、最大上下文、工具调用或流式能力验证。
- 当前动作不持久化 probe 历史或聚合可用率；这些属于 UI-13.4/ARC-08，不能从单次结果推导服务健康趋势。
- UI-13.4 Trace viewer、UI-13.6 可逆修复动作和 UI-17 对本能力的独立断连 golden 仍未实现，因此 UI-13
  与 UI-17 保持 `partial`。
