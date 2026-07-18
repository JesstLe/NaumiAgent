# EVO-02.7c1 受控 Mutation Turn Runner

## 目标

把生产 `ModelPort` 的工具调用真正接入 EVO-02.7b1 的隔离内存 Mutation Generation Session，使模型可以
读取完整 approved baseline、调用虚拟 `file_edit/file_write`，最终产出可进入 Guard v2、Writer v3 与
Mutation Receipt v2 的不可变 Trace。

本切片只负责“模型 turn 如何受控生成变异”，不开放 Slash/Agent Tool，不直接写磁盘，不运行 HAR-08，也
不赋予模型新的目录权限。

## 为什么是当前最小前置

横向审计 Harness、EVO-02 与 EVO-03 后确认：

- HAR-08 已有离线协议 Eval、结果 Store、Baseline selector、统计 Comparator 和 Comparison Receipt，可作为
  EVO-03 后续唯一机械裁判；
- EVO-02.7b1/2 已完成虚拟工具 Trace 与写入/Receipt 强绑定；
- 实际缺口是调用方仍需手工构造 `ToolCall`，生产模型尚未进入这条受控边界；
- 继续扩张 Writer、UI 或 ARC 表层都不能替代模型调用到 Trace 的真实闭环。

因此 EVO-02.7c1 复用已有 `ModelPort` 和 `RuntimeEventPublisher`，不创建平行模型路由或私有事件总线。

## 输入与 Authority

`EvolutionMutationTurnRunner.run()` 接收：

- Contract、active Lease、Source Snapshot、Mutation Plan；
- `run_id` 与 Plan attempt；
- 可选精确 model、`MutationTurnBudget`、取消事件与 Runtime Event publisher。

Runner 首先调用现有 `EvolutionMutationGenerationService.begin()`。Contract/Lease/Snapshot/Plan、baseline
digest、attempt 和同 attempt Trace 唯一性仍由 Generation Service 机械验证，Runner 不复制 Authority。

## Prompt 与源码边界

Generation Session 提供只读、不可变的 `prompt_baseline_contents()`：

- 只包含 Plan approved paths；
- modify 文件提供真实 UTF-8 baseline，create 文件为 `null`；
- prompt 包含 finding、scope、hypothesis、change mode 和 approved path 列表；
- baseline、模型正文和 reasoning 只存在于当前内存消息，不进入 Trace、Runtime Event 或 SQLite；
- Runner 绝不截断某个 approved source。上下文放不下时返回 `mutation_turn_prompt_oversized`，避免模型基于
  残缺源码生成看似合法的补丁。

Prompt byte 上限同时受显式预算和模型 context metadata 约束，并为输出及协议保留空间。无效或过小的模型
context/max-output metadata fail-closed。

## 工具协议

Runner 只向模型声明两个 JSON Schema function：

- `file_edit(path, old_text, new_text)`；
- `file_write(path, content)`。

`path` 是 Mutation Plan authorized files 的精确 enum，`additionalProperties=false`。模型返回的 tool call
必须严格包含 `id/type/function/name/arguments`，额外字段、缺字段或错误类型在进入 Session 前被拒绝。

多个 tool calls 按模型顺序串行执行，保持逐文件 digest chain。成功/失败 ToolResult 会作为标准 tool message
回送下一 turn；Kimi 等协议需要的 `reasoning_content` 在内存消息中保留，但不会进入持久 artifact。

每批工具执行后 Runner 尝试 finalize：完整 scope 已成功更新则立即结束，不额外消耗一个“确认完成”模型
turn；scope 不完整但错误可恢复则继续下一轮；fatal Session error 立即终止。

## 预算、超时与取消

`MutationTurnBudget` 的硬边界：

- `max_turns`：1..50，默认 50；
- 总 `timeout_seconds`：0.01..1800，默认 300 秒；
- 每次最大输出、累计 Token 和 prompt bytes 均有上限；
- Plan 自身的 `max_tool_calls` 继续由 Generation Session 强制执行。

每次 ModelPort call 都与显式 `cancel_event` 和全局 deadline 竞速：

- 取消事件触发会取消并 await 正在运行的 model task；
- deadline 到期返回 `mutation_turn_timeout`，同样回收 model task；
- 调用方 task cancellation 原样传播 `CancelledError`，但先取消/await 内层 model task；
- 取消、超时和协议失败都不会 finalize Trace，也不会触碰 Lease worktree。

Provider 返回的 Token usage 必须非负、有限且 `total=input+output`；超过累计预算时在执行该响应的工具前停止。

## Runtime Event

Runner 使用现有 typed event vocabulary，不新增 UI 私有协议：

- `turn_start`：Plan、attempt、轮数和超时预算；
- `tool_start/tool_end/tool_error`：工具名、hashed call ID、状态和有限 error code；
- `response_end`：Trace identity、turn/tool/token 计数；
- `error`：有限 typed code。

Event 不包含源码、模型正文、reasoning、原始 call ID 或绝对路径。finalize 前 Event Sink 失败会 fail-closed；
Trace 已持久化后的最终 `response_end` 若交付失败，Runner 仍返回 proposed contents，并明确设置
`event_delivery_failed=true`，避免因观测面故障丢失不可恢复的内存草稿。

Runner 没有伪造 Harness heartbeat：当前没有独立 Mutation Run Lease/epoch authority，不能用普通时间戳冒充
可接管心跳。若未来让 Mutation Turn 后台化，应先建立对应 lease/fencing，再接 HAR-10 heartbeat。

## 验收证据

- 真实 Git Contract/Lease/Snapshot/Plan 上，经确定性 ModelPort 协议端生成完整虚拟 Trace；
- worktree 在生成阶段保持 clean，随后同一结果真实通过 Guard v2、Writer v3 和 Mutation Receipt v2；
- Runtime Event sequence 严格递增，event payload 不含 raw call ID 或源码；
- recoverable edit 失败进入下一模型 turn，reasoning/tool result 配对保持协议完整；
- 显式取消、总超时、调用方 cancellation 均实际回收阻塞 model task，不留下 Trace 或磁盘改动；
- malformed tool call、累计 Token 超限、turn 上限和 prompt 超限均 typed fail-closed；
- 最终 Event Sink 故障不丢失已 finalize 的 proposed contents；
- Engine 组合真实 `ModelPort` 与 Generation Service，原 Generation/Receipt 聚焦回归保持通过。

## 当前不足与下一步

- 自动化测试使用确定性 ModelPort 协议端，真实 Git/worktree/Guard/Writer/Receipt 均为生产实现；未调用付费或
  需要本机密钥的 live provider，因此不同供应商 tool-call 细节仍需显式 opt-in 的集成矩阵；
- protocol/cancel/timeout 失败仍没有不可变失败审计 artifact，只有 typed error 与 best-effort Runtime Event；
- Runner 暂不持久化 prompt 或 proposed contents，Trace finalize 后、Writer 前崩溃仍须下一 attempt 重生成；
- 没有公开 Slash/Agent Tool，避免在 EVO-03 验证门完成前形成可绕过治理的自修改入口；
- 下一开发选择应重新比较“失败审计 artifact”与“EVO-03.1 Validation Plan”的依赖价值，优先选择能让
  Mutation Receipt v2 被 HAR-08 真实消费的最小闭环，而不是增加更多生成工具。
