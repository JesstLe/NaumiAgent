# ARC-04.5e3b1 / HAR-10.7h5 加密 Agent Tool RPC 内核

## 1. 依赖裁决与切片边界

ARC-04.5e3a 已让独立 Agent Worker 在 exact owner lease 和两阶段 running fence 后执行真实 Provider
调用，但只允许空 `tool_scope`。本切片只补生产调度前不可跳过的最小前置：让 child 提出工具调用，父
Runtime 按原有权威入口执行，再把结果加密返回 child。它不同时替换 `SubAgentManager`，避免把协议内核、
权限 UI 和生产路由三个故障域揉成一次交付。

权威关系固定为：

```text
child Provider response
  -> exact-scope ToolCall batch
  -> encrypted authenticated local IPC
  -> parent-injected authority callback
  -> Engine.execute_tool-compatible permission/tool path
  -> encrypted ToolResult batch
  -> child appends tool messages
  -> next bounded Provider turn
  -> existing encrypted terminal/outbox commit
```

child 不能持有 Tool 实例、权限存储或 bypass 判断逻辑。生产接入只能注入 `Engine.execute_tool()` 或保持其
完整语义的适配器；不得使用直接 `Tool.execute()`、复制权限规则或在 child 中实现“简化版确认”。

## 2. 能力与 manifest fence

Worker 新增 `agent_tool_rpc` capability，并机械依赖 `agent_model_execution`；后者继续依赖认证控制通道、
Job owner lease 和 context scope。配置了模型 profile 的独立 Worker 声明该能力，Doctor/New UI/TUI 的
共享投影明确显示“加密 Tool RPC 内核就绪、生产调度待接入”，不把内核冒充默认生产路由。

父进程在 `mark_running` 前签发 Tool manifest：

- `tool_scope` 必须去重、排序并与 durable `AgentWorkerRequest` 精确相等；
- schema 名称、顺序和数量必须与 scope 精确相等；
- 每个 schema 只接受 OpenAI function 的精确字段集合；
- manifest 最大 4 MiB，description、parameters 和整体 JSON 均有边界；
- preparation/execution ID 同时绑定 request、claim epoch、model profile 和 manifest SHA-256；
- manifest 通过进程级一次性 AES-256-GCM key 传输，AAD 绑定 Worker incarnation、Job fence 和 execution。

任一 scope/schema/digest/envelope 不一致都在 running 与 Provider 调用前失败，可安全 release 回 admitted。

## 3. Tool call/result 协议

child 将 Provider tool call 规范化为精确 `ToolCall`：

- 只接受 `id/type/function` 和 `name/arguments` 的精确字段集合；
- call ID、tool name、JSON object arguments 均校验格式；
- 单参数最大 2 MiB，单轮最多 64 个调用，call batch 总计最大 16 MiB；
- tool name 必须属于 manifest scope；
- call ID 不得重复；相同工具与语义相同参数即使空白或 call ID 不同也不得在同轮重复；
- 跨轮维护“工具名 + 规范化参数”摘要，重复操作在送达父 Runtime 前停止，避免重复副作用。

父回调逐个返回 `ToolResult`，result 必须按原 call 顺序一一对应：

- status 仅允许 `success/error/skipped/aborted`；
- 单结果最大 16 MiB，整批最大 32 MiB；
- duration 必须是有界非负整数；
- call batch digest、result batch digest、turn、execution 和 running receipt 全部进入 AAD；
- 未知字段、重排、缺失、多余、篡改或过大载荷全部 fail closed。

当前父执行器按 Provider 顺序串行执行。并行工具需要在后续切片先定义权限交互顺序、取消和部分成功语义，
不能仅以 `gather()` 替换。

## 4. 多轮模型循环

child 使用既有 `ModelRouter` 逐轮非流式调用：

1. 首轮使用 request 绑定的 system/user messages 和 manifest tools；
2. 无 tool call 时产生 terminal；
3. 有 tool call 时先检查最大轮数、scope、结构、批大小和重复副作用；
4. 发送加密 ToolCall，等待父进程加密 ToolResult，同时继续 heartbeat；
5. 追加 assistant tool_calls 与对应 tool messages；
6. 进入下一轮，累计 token/cost/turn/time budget；
7. 达到最大轮数时不再执行最后一轮待处理工具，返回 `max_turns` 安全终态。

模型轮数扩展到合同上限 1000，但生产 request 当前仍受既有最大轮数 50 约束。整个循环共享 request wall
deadline，不按轮重置；Provider timeout、畸形 usage 或预算超限都不发布未经认证的 response。

## 5. 故障与恢复语义

- prepare 前失败：没有副作用，Job 保持 claimed，可显式 release；
- Provider 越权或畸形 tool call：父执行器调用次数为零，提交 error terminal；
- 重复工具操作：第一次可完成，第二次在 child 拦截，提交 error terminal；
- 父权限/工具回调返回普通 error `ToolResult`：结果可回送模型，由模型决定下一轮；
- 父回调抛异常、超时、取消或 IPC 中断：工具副作用可能未知，Worker fail closed，Job 保持 `RUNNING`，
  slot 释放并交给既有 recovery/reconcile；绝不自动重放；
- terminal 已原子提交但 child commit ack 丢失：durable AgentJob/outbox 仍是权威；
- 所有数据库只保存既有加密 payload 或低敏摘要，不保存 API key、ToolCall 参数、ToolResult 正文。

## 6. 聚焦验收

- spawned child 真实访问本地 OpenAI-compatible HTTP endpoint 两轮；
- 第一轮 Provider tool call 经父 authority callback 执行一次，第二轮消费真实 ToolResult 并完成；
- request scope、manifest schema、Provider HTTP tools 三者精确一致；
- API key、工具参数和工具结果不以明文进入 Worker Registry、Harness 或 AgentJob DB；
- 越权工具调用不触达父执行器；
- 同轮同语义重复调用拒绝，跨轮重复副作用只执行一次；
- 父执行器异常后 AgentJob 保持 running、Worker failed、slot 释放；
- Tool RPC capability 缺少 model execution 时合同拒绝；
- model-only 旧路径保持可用；
- 只运行 Tool RPC、Agent Worker model/process、Worker contract/Supervisor、authority health/Doctor 与文档
  治理相关小模块，不运行全量测试。

## 7. 自我审视与未完成

本切片完成的是可验证协议内核，不是生产独立子 Agent 的最终切换：

- `SubAgentManager` 尚未按工具范围选择独立 Worker，当前只能由显式调用方注入父 authority callback；
- 尚未对 New UI/TUI 的真实 permission bubble 做独立 Worker 端到端交互验收；
- 工具串行执行，尚无并行批次、部分取消或并发权限交互；
- 仍是非流式 Provider 响应，thinking/tool delta 尚未实时投影；
- ToolCall/ToolResult 明文必然存在于执行内存并进入下一轮 Provider，只保证不落未加密 durable store；
- 非零模型预算仍是 usage 返回后复验；
- running 后失败依赖既有人工 recovery，不做自动重放；
- 真实进程/HTTP 验收仅覆盖当前 Darwin，Linux/Windows 仍需 CI/终端矩阵。

后续 `ARC-04.5e3b2 / HAR-10.7h6` 已完成生产 `SubAgentManager` 路由、
`Engine.execute_tool()` exact authority adapter 与 New UI/TUI permission/status 纵向验收，见
`ARC-04-5e3b2-production-agent-worker-routing.md`。下一步重新比较 streaming、并行 Tool RPC、长驻池、
scheduler fairness 与其他 Harness 用户闭环的依赖，不线性做完整 ARC。
