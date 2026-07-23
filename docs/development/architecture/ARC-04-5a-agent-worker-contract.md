# ARC-04.5a Agent Worker Request/Result Contract

## 1. 目标与依赖判断

HAR-10.2g 已让真实 Agent 委派产生持久 heartbeat，HAR-10.7a/7b 已提供进程内并发与等待上限，但模型调用
此前仍直接消费可变的 `SubTask`、Agent 配置和临时消息上下文。它没有一个能回答以下问题的不可变边界：

- 模型调用前到底绑定了哪个任务和上下文；
- Agent 实际获得了哪些工具、权限、模型层级、轮数、费用和超时；
- 终态是否属于同一个请求，资源用量是否有效；
- New UI/TUI 展示的执行是否拥有可校验的 Worker 证据。

本切片先建立 ARC-04.5 的最小真实前置：现有 `SubAgentManager` 必须在调用模型前签发
`AgentWorkerRequest`，结束时签发 `AgentWorkerResult`。它不启动跨进程 daemon，也不冒充 ARC-06
集群 scheduler。

## 2. 请求合同

`AgentWorkerRequest` 是 frozen、slots、schema v1、canonical JSON SHA-256 合同，绑定：

- 由 session、task ID 和 Agent 名机械导出的稳定 request ID；
- task ID、session ID、任务正文、最终组装上下文和 completion topic 的 SHA-256；
- task/context UTF-8 字节数及硬上限（分别为 2MB/16MB）；
- 实际 `BaseAgent.tool_names` 只读属性的唯一排序工具范围；
- permission mode、model tier、`max_turns`、USD 微单位费用上限和毫秒超时；
- aware `issued_at` 与覆盖全部字段的 `request_sha256`。

合同不保存原始任务、上下文、session ID、topic、路径、凭据或消息正文。工具名是执行授权面的一部分，
允许在 Agent Control 中显示；task/context 只显示摘要。

`SubAgentManager` 在 blackboard 与待处理消息完成组装后、`_register_execution()` 和模型调用前签发合同。
无效模型层级、权限、预算、超时、工具名、过大内容或摘要篡改会产生中文失败回执，且不会调用模型。

## 3. 终态合同

`AgentWorkerResult` 必须绑定精确 `request_sha256`，并机械映射：

| Agent 状态 | reason code |
| --- | --- |
| completed | `agent_completed` |
| error | `agent_failed` |
| timeout | `agent_timeout` |
| max_turns | `agent_max_turns` |
| cancelled | `agent_cancelled` |

结果只保留 response/error SHA-256、response 字节数、token、微 USD、turns、aware completion time 和
`result_sha256`。response/error 合同输入分别限制为 16MB/1MB，不保留模型响应或原始异常。

如果第三方/动态 Agent 返回负 token、非有限费用等畸形指标，不会伪造 result digest；ARC-04.5c
建立 durable publication barrier 后，这类未经认证的结果也不会再返回给调用方或发布到 message bus，
执行记录同时显示 `agent_worker_result_invalid` 和 `agent_job_terminal_receipt_invalid`。

## 4. New UI / TUI 共享投影

Agent Control schema v2 的 execution descriptor 以 additive informational fields 增加：

- `worker_request_sha256`
- `worker_result_sha256`
- `worker_tool_scope`
- `worker_contract_failure_code`

New UI 与 Textual TUI 都从同一 `AgentControlService` 快照显示工具范围、请求/结果摘要前 12 位及降级码。
active 执行显示“结果待生成”，不会伪造终态。两端都不重新计算合同。

## 5. 聚焦验收

- 相同输入产生相同请求合同，改变轮数或结果 token 后旧 digest 校验失败；
- task/context/response/error/topic 原文不出现在合同序列化结果；
- 五种终态生成精确 reason code，并绑定请求摘要；
- 无效请求在模型调用前拒绝，不进入 active/history；
- 真实完成执行同时产生请求与结果摘要，active 执行只有请求摘要；
- 畸形终态指标显示降级码且不生成虚假结果摘要；
- heartbeat、停止、tool progress 与进程内 admission 行为不回退；
- Python Agent Control、New UI protocol/render 和 Textual TUI 显示同一事实；
- 只运行 Agent Worker、SubAgentManager、Agent heartbeat/control、New UI Agent 页面和架构所有权小模块。

## 6. 自我审视与未完成

本切片是执行合同，不是完整 Agent daemon：

- ARC-04.5c 已让 embedded 路径消费加密 Agent Job authority，request/context 可在 live claim 下恢复；
- ARC-04.5d1 已将 response/error 加密绑定到 terminal result，并要求生产发布前重新认证恢复；
- publication outbox 与父进程崩溃后的自动重放仍未完成；
- Agent 仍由 embedded Runtime 直接调用模型，不是注册到 Worker Registry 的持久 incarnation；
- 尚未消费 Worker capacity reservation/FIFO、claim owner lease 或 workspace/provider fairness；
- message bus 仍是 session-scoped 内存实现；
- 没有 Supervisor、crash takeover、跨主机身份或 100 并发 soak 证据。

ARC-04.5b1 已交付 OS credential-backed key 与 bounded AES-256-GCM envelope，ARC-04.5b2 已建立
加密 Agent Job Store，ARC-04.5c 已把生产委派接入该 authority，ARC-04.5d1 已补齐 durable terminal
payload source。下一步必须跨 Harness/ARC-06/UI
重新选择最小用户价值切片，不默认继续线性扩展 ARC-04。
