# ARC-01.3e EventSink 设计

## 背景

ARC-01.3 已完成 SessionPort、PermissionPort、ModelPort 与 ToolExecutionPort，唯一剩余边界是
EventSink。当前 Runtime 仍通过 `Callable[[str, dict], Awaitable[None]]` 逐层传递事件，Engine、
Agent、SubAgent、Tool、CLI、TUI、API 与 New UI 都能直接调用 callback。事件名字、payload、运行
身份、顺序、背压与失败语义没有一个权威契约。

这不是单纯的类型重复问题。EventSink 是 Runtime 向 UI、持久化、Inspector、API transport 和未来
远端 worker 输出执行事实的唯一出口；如果只把 callback 改名为 Protocol，仍然无法证明跨表面事件
一致、回执先持久化后展示、并发事件有序、payload 可序列化或慢消费者不会静默改变执行语义。

## 真实代码审计

2026-07-15 对 `src/naumi_agent` 的 AST 与调用链完成审计：

- `on_event`、`event_callback` 等文本引用共 202 处；
- 可识别的真实异步 callback 调用 55 处，其中 Engine 占 50 处；
- 发现 30 个字面量运行事件名和 5 个动态转发点；
- `EventCallback` 在 Engine、Agent、SubAgent manager、team protocol、subagent tool 中重复定义，
  `ToolEventCallback` 又在 ToolExecutionPort 单独定义；
- `AgentEngine.emitter = EventEmitter()` 会被构造，但生产源码没有任何 `self.emitter.emit(...)` 或
  subscribe 消费者，不能把这条未接线的旧事件总线误认为权威实现；
- `run_streaming()` 目前用局部 `recorded_event()` 串联 Inspector、ChatRunRecorder 与调用方 callback，
  completion receipt 的正常、异常和取消分支又各自复制了一次发送逻辑；
- API 把 raw Engine event 转成另一套 `StreamEvent/EventType`。未显式映射的事件会退化为
  `TURN_END`，因此 `harness_knowledge`、`runtime_notification`、`recovery_event`、性能事件等会丢失
  原始语义；
- New UI 能看到 raw event，TUI/旧 CLI 各自解释 callback，API/SSE/WebSocket 又执行一次转换，
  同一运行事实没有跨表面稳定 identity 与 sequence。

当前 30 个字面量事件为：

```text
completion_receipt, context_compacted, error,
harness_completion_correction, harness_completion_receipt,
harness_knowledge, harness_knowledge_invalidated, hook_trace,
latency_metric, perf_phase, permission_bubble, recovery_event,
response_end, response_start, run_started, runtime_notification,
subagent_event, task_reconciliation_warning, task_snapshot, team_event,
thinking_delta, thinking_end, thinking_start, token,
tool_end, tool_prepare_end, tool_prepare_snapshot, tool_prepare_start,
tool_start, turn_start
```

`tool_error` 作为现有 recorder/inspector 兼容事件保留在权威枚举中，但 Engine 当前使用
`tool_end(status=error)` 表达失败，不在本切片改变该产品协议。

## 目标

1. 定义 Runtime-owned、runtime-checkable 的 `EventSink`，只接收强类型 `RuntimeEvent`。
2. 每个运行事件具有稳定 event id、run/session identity、单调 sequence、turn 与时区完整时间戳。
3. payload 只能包含递归 JSON 值，构造时复制并冻结，任意 Sink 不得修改权威事实。
4. 建立 run-scoped `RuntimeEventPublisher`，集中完成上下文注入、顺序分配和严格有序背压。
5. Inspector、ChatRunRecorder、注入 Sink 与调用表面通过确定性组合顺序消费同一个事件对象。
6. Engine 内部不再直接调用 `on_event(name, dict)`，所有生产事件通过 Publisher→EventSink。
7. CLI、TUI、New UI、SSE 与 WebSocket 通过显式 adapter 消费同一权威事件；旧 callback 仅作为
   有移除条件的兼容 adapter。
8. API 不再把已知 Runtime event 静默降级成 `TURN_END`，必须保留 source event identity。
9. 保持工具批次并发、取消传播、权限交互、Harness evidence 与完成回执行为；不引入第二套队列。

## 非目标

- 不重做 UI JSONL 的 ClientEvent/ServerEvent 协议；它是 EventSink 之后的 transport；
- 不把 `StreamEvent`、`UIMessage` 或 Textual Message 变成 Runtime 领域对象；
- 不为 31 个事件一次性建立 31 个 payload dataclass；本切片用严格 JSON 值与事件枚举锁定边界；
- 不让 EventSink 负责权限、工具执行、会话持久化、Harness 判定或完成回执生成；
- 不增加 Kafka、Redis、NATS、进程间总线或后台 dispatcher；远端 fan-out 属于后续 adapter；
- 不把高频 token/thinking 事件改成丢弃、采样或合并；用户可见流式语义保持不变；
- 不删除 `streaming.EventEmitter` 源码或旧 CLI 源码。未接线的 legacy emitter 在 ARC-01.5 处理；
- 不移动大目录，不实现 ARC-01.4 composition root 或 ARC-01.6 import rules CI；
- 不运行全量测试。

## 方案比较

### 方案 A：强类型 Event + run-scoped Publisher + 组合 Sink（采用）

Runtime 产生 `RuntimeEvent`，Publisher 为每轮运行分配上下文和 sequence，多个 Sink 依次消费同一
对象。旧 callback 通过 adapter 进入，不再作为内部依赖。

优点：

- event identity、顺序和 payload 在所有表面一致；
- recorder 与 UI 不会各自生成不同 event id；
- 可注入记录型、falsey、失败型和未来远端 Sink；
- 严格 await 保留现有背压，不需要隐藏的后台任务或无界队列；
- Engine 事件生产与 transport/UI 转换解耦；
- 可以逐层迁移，同时用静态审计证明 callback 只残留在兼容 adapter 和 Tool API 边界。

代价：Engine 内 50 个发送点需要逐项迁移并做事件顺序回归，不能机械替换。

### 方案 B：直接把现有 EventEmitter 注入 Engine

不采用。现有 EventEmitter 使用 `EventType/StreamEvent` transport 模型，只覆盖部分 Engine 事件，
队列满时丢弃最旧事件，而且当前生产链从未接线。把它直接当 Port 会改变严格背压、丢失 raw event
语义，并把 transport fan-out 混入 Runtime 权威边界。

### 方案 C：保留 `(name, dict)`，只增加 EventSink Protocol

不采用。这只能消除别名重复，无法阻止未知事件、不可序列化 payload、数据突变、跨表面不同 id、
并发乱序或 completion receipt 重复发送，属于“看起来像架构”的简易实现。

## 权威类型契约

`src/naumi_agent/runtime/ports/events.py` 定义：

```python
JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]
LegacyEventCallback = Callable[[str, dict[str, object]], Awaitable[None]]

class RuntimeEventType(StrEnum):
    RUN_STARTED = "run_started"
    # 完整枚举审计确认的 31 个事件，禁止自由字符串

@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    id: str
    type: RuntimeEventType
    data: Mapping[str, JsonValue]
    timestamp: str
    session_id: str = ""
    run_id: str = ""
    turn: int = 0
    sequence: int = 0

@runtime_checkable
class EventSink(Protocol):
    async def emit(self, event: RuntimeEvent) -> None: ...
```

契约规则：

- `RuntimeEvent.type` 必须是枚举，未知字符串以中文 `ValueError` 失败；
- data key 必须是非空字符串，值必须递归属于 JSON 类型；拒绝 bytes、Path、set、NaN、Infinity、
  dataclass 或任意对象，不能用 `str(value)` 掩盖生产缺陷；
- 构造时递归复制 list/tuple/mapping，并以 tuple/read-only Mapping 保存；
- `thaw_event_data()` 为 callback/JSON transport 生成新 dict/list，消费者修改不反向污染事件；
- id 非空，timestamp 必须是包含 UTC offset 的 ISO-8601；turn/sequence 是非负 int，bool 不算 int；
- Sink 普通异常向上抛出；`CancelledError` 原样传播；不得在 Port 层吞错；
- EventSink 可能被并发调用，但 Engine 的权威 Publisher 保证同一 run 内有序串行送达。

## RuntimeEventPublisher

`src/naumi_agent/streaming/publisher.py` 提供 run-scoped publisher：

```python
class RuntimeEventPublisher:
    async def publish(
        self,
        event_type: RuntimeEventType,
        data: Mapping[str, object],
        *,
        turn: int = 0,
    ) -> RuntimeEvent: ...

    def legacy_callback(self) -> LegacyEventCallback: ...
```

Publisher 的职责严格限定为：

1. 在 `asyncio.Lock` 内分配从 1 开始的 sequence；
2. 生成一个 event id 和带 UTC offset 的 timestamp；
3. 注入固定 session_id/run_id；
4. 构造、校验并冻结 `RuntimeEvent`；
5. 在同一锁内 `await sink.emit(event)`，保证同一 run 的消费顺序与 sequence 一致；
6. 返回已经送达的 event，便于测试和必要的调用方关联。

锁只包围事件构造与 Sink 发送，不包围模型或工具执行。它提供严格背压，但不会把并行工具调用本身
串行化。不同 run 使用不同 Publisher，不共享锁或 sequence。

`legacy_callback()` 只供仍要求 `(name, data)` 的 Tool/Agent 兼容接口。它严格解析
`RuntimeEventType(name)` 后调用 `publish()`；未知事件不会绕过枚举。

## Sink adapters 与 fan-out

`src/naumi_agent/streaming/sinks.py` 提供：

- `NullEventSink`：默认无外部消费者时显式接收，不用 `None` 判断散落在 Engine；
- `CallbackEventSink`：把同一个 `RuntimeEvent` thaw 成旧 `(name, data)`，并把 `event_id`、
  `run_id`、`session_id`、`sequence`、`turn` 合并到 payload；
- `CompositeEventSink`：按构造顺序、逐个 await；复制 sink tuple，拒绝空/无效对象；
- `InspectorEventSink`：把事件交给 `RuntimeInspectorTracker.observe()`；
- `ChatRunRecorderEventSink`：把事件交给 `ChatRunRecorder.observe()`。

每轮运行的组合顺序固定：

```text
RuntimeEventPublisher
  -> InspectorEventSink
  -> ChatRunRecorderEventSink
  -> Engine 注入的基础 EventSink
  -> 当前调用表面的 EventSink
```

顺序的意义是：即使 UI transport 断开，Inspector 和 durable recorder 已先看到该事件。调用表面
Sink 失败仍会终止本轮，保持当前“前台流断开即取消/失败”的可观测语义；但完成回执必须先通过
`recorder.finish()` 持久化，再尝试作为 terminal event 输出，不能因 UI 失败丢失 durable receipt。

`CompositeEventSink` 不提供 best-effort 标志。需要容错的遥测 adapter 必须在自身内部明确实现并
测试，权威 Runtime 不静默吞掉基础设施错误。

## Engine 注入与运行链路

`AgentEngine.__init__` 增加 keyword-only `event_sink: EventSink | None = None`：

- `None` 构造 `NullEventSink`；显式 falsey Sink 不得被替换；
- Protocol 校验发生在 `.naumi`、SQLite、Chroma、browser 等 I/O 前；
- `_event_sink` 是唯一字段，`event_sink` property 只读暴露；
- `self.emitter` 暂时保留为 legacy compatibility surface，但明确不参与权威运行事件链；
- ARC-01.4 再把默认 Null adapter 的构造移到 composition root，ARC-01.5 决定 legacy emitter 的
  移除条件。

`run_streaming()` 创建 recorder 后组合 run sinks，再创建 Publisher。Engine 内部方法逐步把
`on_event: EventCallback | None` 改为 `events: RuntimeEventPublisher | None`，发送统一为
`events.publish(RuntimeEventType.X, payload)`。

公共 `run_streaming()` 在本切片保留接收 `EventSink | LegacyEventCallback` 的兼容 union，但所有
仓库内产品表面必须显式传 EventSink；callback 自动包装只服务第三方和存量测试，列入 ARC-01.5。
`run()` 无流式调用方时使用 Null sink，不产生 `if callback is not None` 分支扩散。

completion receipt 的正常、异常、取消路径收敛到一个 helper：

1. `recorder.finish(status, summary)`；
2. 给 result 附加 receipt（正常路径）；
3. 通过同一 Publisher 发布 `COMPLETION_RECEIPT`，只发布一次；
4. 取消继续抛出 `CancelledError`，普通异常保留原异常；
5. 若 terminal Sink 失败，receipt 已持久化，错误不覆盖原始运行错误的诊断上下文。

## Tool、Agent 与 SubAgent 兼容边界

ToolExecutionPort 的工具 API 目前通过 `event_callback` 向 delegate/subagent tool 传递运行事件。
本切片不修改所有 Tool schema，而是：

- `ToolEventCallback`、Agent/Team/SubAgent 的重复 alias 统一导入 `LegacyEventCallback`；
- Engine 调用 ToolExecutionPort 时只传 `publisher.legacy_callback()`；
- Agent/SubAgent manager 的生产装配优先接受 Publisher/EventSink，只有工具执行签名边界转回 callback；
- team/subagent 的动态事件名必须先转换为 `RuntimeEventType`，未知值失败，不允许自由扩张；
- 独立 Tool 单元测试可以直接传 callback，因为它在测试兼容 API，不是第二条产品事件总线。

静态验收允许 callback 直接调用只存在于 `CallbackEventSink`、`legacy_callback()` 和明确的 Tool API
适配点；Engine、CLI、TUI、New UI、API transport 不得直接 `await on_event(...)`。

## 产品表面与 transport

- 旧 CLI、TUI：用 `CallbackEventSink(existing_handler)` 保留现有中文渲染；
- New UI bridge：用 `CallbackEventSink(handle_engine_event)`，raw event 名与 payload 不变，并获得稳定
  event_id/sequence；
- FastAPI SSE/WebSocket：新增 `StreamEventSink`，消费 `RuntimeEvent` 后转换 transport envelope；
- `StreamEvent` 增加可选 `source_event`、`event_id`、`sequence`，同一 Runtime event 在 SSE 与 WS
  保持 identity；
- `_engine_event_to_stream_event` 改为显式穷尽 `RuntimeEventType`。没有专属 `EventType` 的内部事件
  使用新增 `EventType.RUNTIME_EVENT`，payload 为 `{event, data}`，不再退化为 `TURN_END`；
- New UI 的 `EngineEventAdapter` 继续负责可见消息转换，EventSink 不做颜色、Markdown 或布局判断。

## 背压、并发与失败语义

- 同一 run：Publisher 锁保证严格递增 sequence 和严格送达顺序；
- 不同 run：不同 Publisher 可并发；
- Tool batch：并行工具仍并发，只在产生事件时短暂排队；
- 慢 Sink：生产者 await，形成有界的自然背压，不积累无界内存；
- Sink 普通失败：传播并结束当前运行；之前的 recorder 写入不回滚；
- cancellation：Publisher、Composite、adapter 均不得捕获 `CancelledError`；
- payload 失败：在任何 Sink 收到前失败，避免不同消费者看到不同事实；
- fan-out：同一对象依次传给所有 typed Sink；旧 callback 每次得到独立 thaw copy；
- completion receipt：先 durable finish、后事件发送，且每轮最多一个 receipt event；
- legacy EventEmitter 的 drop-oldest 策略不得进入权威运行链。

## 安全与隐私

- EventSink 不新增持久化；持久化仍由 ChatRunRecorder/Store 控制；
- RuntimeEvent 不自动 stringify 非 JSON 对象，避免 Path、异常对象、模型对象或凭据被意外泄露；
- 现有 OutputGuardrail/recorder redaction 保留；API 与 UI adapter 继续执行自己的公开字段收敛；
- event id、run id、session id 有长度上限，避免外部注入超大标识；
- callback adapter 返回新容器，恶意/有 bug 的 UI consumer 不能修改 recorder/Inspector 已消费事实；
- 不把 permission confirmer 或 interaction response 放入 EventSink；它们是 Runtime 入站端口。

## 测试策略

### Contract/value

- EventSink 精确只有 `emit`；Null/Callback/Composite 结构满足 Protocol；
- falsey Sink 保留，无效 Sink 在任何运行 I/O 前中文失败；
- 31 个枚举值与审计 manifest 一致；未知事件失败；
- JSON payload 深冻结、thaw 独立、非字符串 key、bytes/Path/set/NaN/Infinity 拒绝；
- id/timestamp/turn/sequence 的空值、时区和 bool 边界覆盖。

### Publisher/fan-out

- 单 run 从 sequence=1 严格递增；两个 run 各自从 1 开始；
- 50 个并发 publish 仍按 Sink 观察顺序对应连续 sequence；
- 所有 Sink 收到同一 event identity；callback 修改 payload 不污染后续；
- Composite 顺序、普通失败短路、外层取消传播；
- 慢 Sink 证明 publish 不提前返回，不创建后台遗留任务。

### Engine/真实运行

- 记录型与 falsey EventSink 注入；默认 Null；无效实现 pre-I/O 失败；
- 无外部 API 的真实 Engine 流式任务发布 run/turn/token/response/receipt 闭环；
- tool success/error、permission、Harness 与 runtime notification 使用同一 run/session/id/sequence；
- 两个并行工具事件 sequence 唯一，tool_start/tool_end 配对不变；
- callback/Sink 普通失败、取消与 completion receipt durable-first 行为；
- ChatRunRecorder 和 Inspector 对同一 event id 去重且状态一致。

### 产品与 transport

- CLI、TUI、New UI bridge 的真实 handler 通过 CallbackEventSink 收到原事件；
- SSE 与 WebSocket 从同一 RuntimeEvent 产生相同 source_event/event_id/sequence；
- 31 个已知事件全部显式转换，非专属事件进入 RUNTIME_EVENT，不出现隐式 TURN_END；
- 旧 callback 调用路径保持兼容，但仓库产品代码不使用 union fallback。

## 实现验收与自审（2026-07-15）

ARC-01.3e 已按本设计完成，当前静态调用链与针对性验证证明 EventSink 是运行事实的唯一产品输出
边界。验收没有使用全量测试。

### 静态审计结果

- `RuntimeEventType` 固定为 31 个值；Engine 的事件生产点全部调用
  `RuntimeEventPublisher.publish(RuntimeEventType, data)`，没有自由字符串生产或
  `await on_event(name, data)`；
- CLI 两处、共享 CLI skill、TUI、New UI bridge 两处、SSE 与 WebSocket 共 8 个
  `run_streaming()` 产品调用点全部显式传入 `CallbackEventSink` 或 `StreamEventSink`；
- SSE 与 WebSocket 不再导入或调用 `_engine_event_to_stream_event`，也不存在
  `.get(event, EventType.TURN_END)` 默认降级；31 个 Runtime 类型由完整映射表约束；
- `AgentEngine.emitter` 仍按非目标保留兼容字段，但生产源码没有 `engine.emitter`、
  `self.emitter.emit()` 或 subscribe 使用；它不是第二条产品总线；
- 直接 legacy callback 调用只剩 4 个明确适配点：`CallbackEventSink`、SubAgent manager 的
  child-event 验证/转发与 `subagent_event` 发送、team protocol 的 `team_event` 发送。后 3 个入口
  只能接收 `RuntimeEventType` 已知值，并由 Engine 传入 `publisher.legacy_callback()` 回到同一
  Publisher sequence；ToolExecutionPort 只透传 callback 参数，不自行生产事件；
- `LegacyEventCallback` 只有 Runtime Port 中一个权威 type alias，`ToolEventCallback` 重复别名为 0。

### 聚焦验收证据

| 验收组 | 结果 | 覆盖内容 |
| --- | ---: | --- |
| Port、Sink、Publisher、注入 | 56 passed | JSON 冻结、falsey Sink、并发顺序、fan-out、失败/取消、注入前置校验 |
| Engine、Harness、receipt | 12 passed | 正常/异常/取消、权限、Hook、tool prepare、Harness knowledge/invalidation、durable-first receipt |
| Agent、SubAgent、Team、Tool adapter | 110 passed | 已知事件转发、未知事件拒绝、失败/取消、Agent 与 Tool 兼容签名 |
| CLI、TUI、New UI | 6 passed | 显式 Sink、可见 runtime notification、TUI 创建与终端噪声隔离 |
| StreamEvent、SSE、WebSocket、API | 71 passed | 31/31 映射、identity parity、脱敏、背压、失败/取消与 API 持久化 |

Ruff 对本切片变更 Python 文件无错误，compileall 通过。运行 Engine Hook 节点时仅出现 ChromaDB
对 Python 3.14 `asyncio.iscoroutinefunction` 的第三方弃用警告，与 EventSink 行为无关。

### 语义逐项自审

- **falsey/error/cancel：** `event_sink is None` 才选择 `NullEventSink`，falsey 实现不会被替换；
  普通 Sink/transport 错误向上抛出，`CancelledError` 不被捕获；完成回执发送失败不会抹掉已持久化
  receipt。
- **sequence/backpressure：** 每个 run 的 Publisher 在同一锁内分配并投递连续 sequence；50 个并发
  publish 的观察顺序连续。Composite 与 StreamEventSink 逐层 await，没有隐式任务、drop-oldest 或
  新增第二队列；SSE 只保留原有一个响应 queue，并继续 await `queue.put()`，WebSocket 直接 await
  `send_text()`。
- **receipt durability：** recorder/Inspector 先于注入和调用表面 Sink；正常、异常、取消各最多产生
  一个 completion receipt，且 `recorder.finish()` 先完成，之后才 publish terminal event。
- **transport parity：** 同一 RuntimeEvent 经 SSE/WS 得到相同 `id`、`event_id`、`source_event`、
  `run_id`、`session_id`、`turn`、`sequence`、timestamp 与 data。无专属类型的事件严格编码为
  `runtime_event` 的 `{event, data}`，不会伪装为 `turn_end`；旧 v1 StreamEvent 的新增字段默认省略。
- **所有产品表面：** CLI/TUI/New UI 只通过 CallbackEventSink 复用既有中文渲染；SSE/WebSocket 只
  通过 StreamEventSink 做 transport 转换。EventSink 不负责颜色、Markdown、权限入站或业务判定。

### 诚实保留项

- `run_streaming()` 的 legacy callback union、`AgentEngine.emitter` 字段，以及 Tool/Agent callback
  兼容签名仍按本设计保留；它们已不形成产品旁路，移除条件与迁移范围属于 ARC-01.5；
- 架构 baseline/ownership artifact 尚需在源码提交 H1 后按 Task 12 双生成并绑定最终 digest；完成
  该步骤前不能声称 ARC-01 整体完成，但 ARC-01.3 五个 Port 的实现门已通过；
- EventSink 只保证单 run 严格顺序；跨 run 全局排序、远端 telemetry 与独立丢弃策略属于后续高并发
  adapter，不应在本地权威链中偷偷增加队列。
- SSE 的既有 `asyncio.Queue` 缓冲策略本切片没有改成 socket 级有界背压；Runtime/StreamEventSink
  的 awaited 契约已成立，但慢 HTTP 客户端的内存上限仍应由 ARC-08 API reliability 独立设计并压测，
  不能把“调用了 `await queue.put()`”夸大为已经完成端到端限流。

## 架构产物与验收门

预计新增：

- `naumi_agent.runtime.ports.events`，owner=runtime；
- `naumi_agent.streaming.publisher`，owner=runtime；
- `naumi_agent.streaming.sinks`，owner=runtime；
- recorder/inspector adapter 优先放在各自既有模块，除非测试证明拆分更清晰。

模块数预计 330→333（若不新增 recorder/inspector 模块）。ownership issues 必须为 0；
import_time/typing/all_static SCC 数量不得超过 0/1/2。baseline 与 ownership artifact 绑定源码 H1，
各生成两次逐字节一致，两个 digest 必须互锁。

完成定义：

- EventSink 是 Runtime 输出执行事实的唯一可替换端口；
- Engine 内无直接 callback 发送，所有事件由 run-scoped Publisher 产生；
- 事件类型、identity、顺序、JSON payload、背压和失败语义有真实 contract；
- Inspector、recorder、注入 Sink 与 UI 表面消费同一个事件；
- completion receipt durable-first、exactly-once event 通过正常/异常/取消测试；
- CLI、TUI、New UI、SSE、WebSocket 无产品旁路，API 不再静默丢失事件语义；
- 工具/Agent callback 只保留为明确兼容 adapter，不形成第二套事件系统；
- 聚焦 pytest、Ruff、compile、架构 40 测试与双扫描通过；不跑全量测试；
- ARC-01.3 五个 Port 全部完成，ARC-01.4 可以开始 composition root。

## 后续

- ARC-01.4 把 Null/Local adapter、stores、model、permission、tool executor 与 EventSink 的默认构造
  移到 composition root；
- ARC-01.5 盘点 `run_streaming(callback)` union、`self.emitter` 与 Tool event callback 的移除条件；
- ARC-01.6 用 import graph + ownership 阻止 Runtime 反向依赖 UI transport；
- 高并发阶段可新增远端/批量 telemetry Sink，但必须作为独立 adapter 并明确自己的队列与丢弃策略。
