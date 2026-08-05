# ARC-04.5e3b2 / HAR-10.7h6 生产 Agent Worker 路由

## 1. 依赖裁决与边界

ARC-04.5e3b1 已完成独立 Worker 的模型循环、加密 Tool RPC 与父 Runtime callback 内核，但生产
`SubAgentManager` 仍只走内嵌 `BaseAgent.execute()`。本切片只闭合生产纵向链路：在 durable admission
之前选择执行后端，让独立 Worker 成为配置了模型 profile 时的默认路径，并把每个 ToolCall 交回同一
`Engine.execute_tool()` 权限权威。

本切片不提前实现长驻 Worker 池、跨 workspace 公平调度、并行 Tool RPC 或模型流式事件。

## 2. 生产路由与所有权

路由必须发生在 Job admission 前，避免同一 Job 同时被 embedded owner 与独立 Worker claim：

```text
SubAgentManager.delegate
  -> local bounded admission
  -> choose backend before durable admission
     -> independent: AgentJobStore.admit only
        -> spawned Worker reserves physical slot
        -> Worker claims admitted Job and marks running
        -> Worker commits terminal + publication outbox
     -> embedded fallback: existing admit/capacity/claim/run path
```

生产 `AgentEngine` 将 composition root 持有的 `AgentWorkerProcessFactory` 注入 manager。factory 存在且
包含模型 profile 时选择 `independent`；显式独立构造但未注入 model-capable factory 的 manager 保留
`embedded` fallback，供兼容、测试和受限运行环境使用。fallback 不是静默伪装：Agent Control schema v6
将 `worker_backend` 严格限制为 `independent|embedded`，New UI 与 Textual TUI 分别显示“独立 Agent
Worker”或“内嵌降级”。

当前每次 delegation 创建一个容量为 1 的独立 Worker incarnation，执行完成后主动 drain/close。这样已
提供真实进程隔离和精确 owner fence，但还不是可复用的长驻池。

## 3. 精确上下文与 Tool authority

独立请求绑定：

- Agent 自身 system prompt；
- task context 与调用方 extra context；
- request 中的精确 tool scope；
- 由父 Runtime 当前 Tool Registry 生成的 exact schemas；
- request 既有的模型、轮数、预算与 wall timeout。

父 callback 对每个 Worker ToolCall 执行以下链路：

1. 复验回调中的 agent identity；
2. 发送既有 SubAgent Tool hook，更新 Agent Control 当前/最近工具；
3. 调用 `Engine.execute_tool(call, on_event=..., agent_name=...)`；
4. 原样转发 permission request/resolution Runtime Event；
5. 将权威 `ToolResult` 加密返回 child。

因此 normal/strict 权限继续产生同一交互气泡，bypass 继续按全权限策略直通；child 不持有权限数据库，
也不能绕过 scope、grant、审计或 Engine Tool Registry。

## 4. 取消、失败与发布

- 用户停止会取消 parent wait 和 Worker 控制任务；如果 Job 已 running，结果保持 recovery-required，不能
  伪造 cancelled terminal 或自动重放可能已有副作用的模型/工具调用；
- prepare/start 前失败可释放回 admitted，再由 manager 按无副作用路径收口；
- Worker terminal receipt 与 AgentJob/outbox 是结果权威，manager 只恢复、校验并投递现有 publication；
- `_finish_execution` 不覆盖 Worker 已签发的 result，也不为独立 running unknown 合成 result digest；
- Worker 状态同步失败只产生稳定降级码，不把未认证状态发布成成功。

## 5. 聚焦验收

- 使用真实本地 OpenAI-compatible 两轮 HTTP endpoint；
- 生产 `AgentEngine` 默认选择独立 Worker，不依赖手工调用 process API；
- child 首轮请求一个受权限控制的工具，父 `Engine.execute_tool()` 只执行一次；
- permission needs-confirmation/confirmed 事件沿现有 Runtime Event 路径出现；
- 第二轮模型消费真实 ToolResult 并提交 authenticated terminal/outbox；
- request system message 同时包含任务上下文和 Agent system prompt；
- AgentJob、Worker Registry 与 Harness DB 不出现 API key、Tool 参数和 ToolResult 明文；
- running 中断保持 durable Job running/recovery-required，不产生伪终态摘要；
- Agent Control schema v6、New UI、Textual TUI 显示精确执行后端；
- embedded fallback 的现有 manager 行为继续通过聚焦回归；
- 只运行 Agent routing、Agent Control、TUI/New UI 协议渲染和 Runtime composition 小模块测试。

## 6. 自我审视与未完成

本切片完成了生产默认路由和权限/状态纵向闭环，但仍有明确边界：

- 当前是一任务一进程，不是长驻 Worker 池，启动成本与高并发吞吐尚未优化；
- 本地 manager semaphore 与 Worker Registry slot 已限制并发，但尚无跨 workspace/provider 的 priority、
  affinity、fairness scheduler；
- Tool RPC 仍按 Provider 顺序串行，尚无并行批次、部分取消和多 permission request 排序合同；
- child Provider 调用仍非流式，thinking/token/tool delta 尚未实时投影到父 UI；
- running unknown 仍依赖恢复目录和显式裁决，不自动重放；
- 真实 spawned-process HTTP 验收当前只在 Darwin 本机执行，Linux/Windows 需要 CI/终端矩阵。

下一切片不默认继续扩张 ARC-04。应重新比较：长驻 Worker 池与公平 scheduler、子模型流式事件、并行
Tool RPC，以及 HAR-10 的用户可恢复性模块，选择能解锁下一个用户闭环的最小共同前置。
