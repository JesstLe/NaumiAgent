# ARC-01.3c ModelPort 设计

## 背景

SessionPort 与 PermissionPort 已完成。当前 `AgentEngine` 仍直接构造 `ModelRouter`，并把具体
Router 传播给 ContextCompactor、AdaptivePlanner、DynamicAgentFactory、GoalPursuitLoop、分析
工具和浏览器子 Agent。只替换 Engine 中的 `call/stream` 会留下大部分具体依赖，不构成真实
Port。

## 审计结论

### Engine 直接能力

- `call`、`stream`；
- `resolve_model`；
- `get_context_window`、`get_max_output`。

### CLI/TUI/New UI/API 能力

- `resolve_target`；
- `get_model_capability_contract`；
- `get_runtime_identity`；
- `list_available_models`；
- `get_reasoning_effort_status`；
- `set_reasoning_effort`、`reset_reasoning_effort`。

### 完整公开元数据能力

- `get_model_info`；
- `get_cost_rates`。

因此 ModelPort 必须覆盖 14 个公开操作，不能只为 Engine 当前 12 个文本调用点制作窄接口。

## 目标

1. 定义覆盖调用、流式、路由、能力、发现与思考强度的完整 ModelPort。
2. 让 Engine 与所有由 Engine 装配的模型消费者依赖 ModelPort，而非 ModelRouter。
3. 默认继续使用 ModelRouter，Provider catalog、发现和 LiteLLM 行为不变。
4. 保留 `engine.router` 与 `engine._router` 兼容读取/方法打桩。
5. 支持 falsey、记录型和完全替代型 Port，不发生隐式 concrete fallback。
6. 用真实 Engine 流式与非流式任务证明同一个注入 Port 贯穿所有消费者。

## 非目标

- 不修改 Provider 协议、模型 catalog 合并规则、LiteLLM transport 或定价计算。
- 不重新设计 ModelResponse、StreamChunk、能力 DTO 或 reasoning DTO。
- 不修改模型选择 UI 的布局和文案。
- 不实现 ToolExecutionPort 或 EventSink。
- 不把默认 ModelRouter 构造迁移到 composition root；属于 ARC-01.4。

## 方案

采用 Runtime-owned 结构化 Protocol，复用 Model 领域现有强类型 DTO。Port 导入
ModelResponse、StreamChunk、ResolvedModelTarget、ModelCapabilityContract、
ModelRuntimeIdentity、ReasoningEffortStatus 和 ProviderModelListing，但不得导入 ModelRouter。

不采用窄调用 Port：它无法承载 UI/API 已使用的模型发现、能力可信度和思考强度。

不复制 DTO：两套能力/身份对象会造成上下文长度、上游模型和参数支持状态不一致。

## ModelPort 契约

### 元数据与目标

- `get_model_info(model)`；
- `get_context_window(model)`；
- `get_max_output(model)`；
- `get_cost_rates(model)`；
- `get_model_capability_contract(model=None)`；
- `resolve_model(tier)`；
- `resolve_target(model)`；
- `get_runtime_identity(model)`。

### 发现与思考强度

- `list_available_models(provider_id=None, refresh=False)`；
- `get_reasoning_effort_status(model=None)`；
- `set_reasoning_effort(value, model=None)`；
- `reset_reasoning_effort(model=None)`。

### 推理调用

- `call(messages, model/tier/tools/max_tokens/temperature/response_format/thinking)`；
- `stream(messages, model/tier/tools/max_tokens/temperature/thinking)`。

返回值使用现有强类型对象。消息和工具 schema 保持现有 JSON-compatible 精确签名，避免本切片
顺带迁移整个 Tool schema 类型系统。

## 注入与唯一权威

`AgentEngine.__init__` 增加 keyword-only `model_port: ModelPort | None`：

- 未注入时才加载 catalog 并构造 ModelRouter；
- 显式注入时不得加载 catalog 或构造/探测默认 Router；
- 使用 `is None`，falsey Port 必须保留；
- 缺少任一操作时中文 TypeError，且在其他服务初始化前失败；
- 唯一字段 `_model_port`；
- `router` 与 `_router` 都是只读 compatibility property；
- Engine 内部和装配路径只传递 `_model_port`。

## 消费者迁移

以下类型注解与保存字段必须改为 ModelPort：

- ContextCompactor；
- IntentClassifier、AdaptivePlanner；
- DynamicAgentFactory；
- GoalPursuitLoop；
- browser LLMPlanner；
- analysis global router setter/getter。

Browser TaskRunner 当前使用 `options.get("model_router") or ModelRouter(...)`。必须改为显式
`is None`，否则 falsey 合法 Port 会被静默替换。

旧 `main.py`、测试和兼容命令对 `engine._router.call` 的打桩通过 property 继续工作，但生产代码
不得新增私有兼容入口依赖。

## 行为不变量

- 非流式调用的 response、usage、model、tool_calls 不变；
- 流式 token/thinking/tool_call/usage/finish_reason 顺序不变；
- Planner、Compactor、Pursuit 和 browser 使用与 Engine 相同的 Port 实例；
- UI/TUI/API 能继续读取能力可信度、运行身份、上下文长度和思考强度；
- catalog 只在默认 adapter 路径加载；
- Provider 错误、上下文恢复和流式取消继续向现有上层传播。

## 测试策略

### Contract

- ModelRouter 满足全部 14 个操作；
- 不完整对象被拒绝；
- 公开操作集合精确；
- call 与 stream 的 typed response/chunk 契约成立。

### 注入与传播

记录型 Port 包装真实/确定性实现，验证 Engine、Planner、Compactor、Factory、Pursuit、analysis
和 browser planner 收到同一对象；falsey Port 不回退；无效 Port 在运行 I/O 前中文失败。

### 真实运行

- 一个非流式 Engine.run 通过注入 Port完成并保存会话；
- 一个 Engine.run_streaming 通过同一 Port 产生 token、usage 和 completion receipt；
- Compactor 与 Planner 的真实调用命中注入 Port；
- UI bridge/TUI/API 能从注入 Port 读取 identity、capability、reasoning status；
- reasoning set/reset 通过兼容表面生效。

网络调用用确定性本地 Port 替代，但 Engine、会话、Planner/Compactor、事件与 receipt 走真实代码。

## 架构产物

新增 `runtime.ports.model`，模块数预期 327→328，owner=runtime。消费者只改变类型依赖，不移动
目录。三类 SCC 数量不得增加；import graph 和 ownership artifact 继续以源码 H1 SHA 互锁。

## 验收标准

- ModelPort 覆盖 14 个真实公开能力，不是 call/stream 套壳；
- Engine 不再保存具体 ModelRouter；
- 所有 Engine 装配消费者依赖并收到同一 ModelPort；
- falsey 与替代实现可完成真实非流式/流式运行；
- CLI/TUI/New UI/API 能力、身份、上下文和 reasoning 行为无回归；
- 默认 ModelRouter 聚焦测试、Port 测试、Engine/Bridge/API/TUI 节点通过；
- Ruff、compileall、架构测试和双扫描通过；
- 不运行全量测试，不修改 ToolExecutionPort/EventSink。

## 后续

完成后 ARC-01.3 仍剩 ToolExecutionPort 与 EventSink。EventSink 71 个调用点必须单独设计，不能
借 ModelPort 迁移顺手改写事件系统。
