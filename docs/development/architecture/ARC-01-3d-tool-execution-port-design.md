# ARC-01.3d ToolExecutionPort 设计

## 背景

SessionPort、PermissionPort 和 ModelPort 已完成。当前工具执行仍由 `AgentEngine._execute_tool`
直接调用 `Tool.execute()`，并在 Runtime 中混合处理参数解析、权限、确认、Hook、会话切换、计时、
Harness 缓存失效和结果归一化。

同时，CLI、TUI 和 Pursuit 仍存在直接 `tool.execute(...)` 的兼容分支。它们会绕过 Engine 的
权限确认、会话切换栅栏和执行证据，违背“手动命令与 Agent 自主调用共享同一 execute 链路”的
项目约定。

本切片建立“已授权工具调用”的真实执行端口，并把所有产品表面收敛到 Engine 的公开执行入口。
它不是 Prompt 套壳，也不复制权限系统或事件系统。

## 审计结论

### 权威 Agent 链路

```text
模型 tool_calls
  -> AgentEngine._execute_tool_calls
  -> build_tool_batches / execute_tool_batch
  -> AgentEngine._execute_tool
       -> registry lookup + parse_arguments
       -> plan/session-transition/PermissionPort/grant/confirmation/Hook
       -> Tool.execute
       -> Harness knowledge invalidation + task snapshot
  -> tool_start/tool_end + Harness evidence + completion receipt
```

### 已发现的旁路

- `AgentEngine` 有 1 个原始 `Tool.execute()` 调用点；
- `GoalPursuitLoop` 有 10 个独立运行 fallback；生产装配虽然注入 Engine callable，但类型仍只是
  `Callable`；
- 共享 CLI 命令、旧 CLI 兼容处理和 TUI 命令存在多处直接调用或“私有方法不存在就直调”的分支；
- `DynamicAgent`、分析命令和通用 slash tool 命令依赖私有 `_execute_tool`；
- New UI 经 shared slash router 进入旧命令处理，因此也会继承这些旁路。

数据库对象的 `.execute()`、Agent 自身的 `.execute()` 和浏览器内部动作不属于 ToolExecutionPort。

## 目标

1. 定义 Runtime-owned、强类型、可运行时检查的 `ToolExecutionPort`。
2. Port 只接收已经完成解析和授权的工具，不得获得未授权调用。
3. 默认本地适配器真实调用工具、使用单调时钟计时、复制参数并校验字符串返回契约。
4. `AgentEngine` 支持显式注入本地、记录型、falsey 或未来远端 worker Port。
5. 所有产品表面通过 `AgentEngine.execute_tool(...)` 进入同一权限与证据链。
6. 保留 `_execute_tool` 兼容方法，旧测试和第三方集成可逐步迁移，但生产代码不再新增依赖。
7. 保持安全批次并发、失败隔离、取消传播、Harness 证据和完成回执行为不变。

## 非目标

- 不修改 `Tool`、`ToolRegistry`、`ToolCall`、`ToolResult` 的领域归属或公开格式；
- 不修改 PermissionPort、风险规则、grant 或 bypass 全权限语义；
- 不实现 EventSink，也不批量改写现有 `on_event` 71 个调用点；
- 不增加新的通用工具超时配置；Bash、浏览器和远端调用继续使用各自已验证的 deadline；
- 不改变并行批次的 read-only/concurrency-safe 屏障；
- 不把默认适配器构造迁移到 composition root；该工作属于 ARC-01.4；
- 不删除旧 CLI 代码，只让仍可达的兼容路径走统一执行入口。

## 方案比较

### 方案 A：授权后 Outbound Port（采用）

Runtime 保留查找、解析、安全策略、确认、Hook 和证据控制；只有最终实际调用交给 Port。

优点：

- 注入执行器永远收不到被 Plan、权限、确认或会话栅栏拒绝的调用；
- 可替换为进程池、远端 worker、容器或审计代理，支持后续高并发工程；
- 不把 Engine 私有状态、grant store、UI confirmer 和 Harness service 泄露给适配器；
- 失败与取消边界清晰，既有结果和事件协议不变。

代价：Runtime 暂时继续承载较长的授权管线。将授权编排抽成 Runtime service 属于 ARC-01.4，
不能与本切片混合。

### 方案 B：Port 接管完整权限与事件管线

不采用。它需要把 Engine 的会话代际、确认回调、grant、Hook 和 Harness 全部传给外部实现，
注入实现也可能在收到未授权调用后自行执行，扩大安全边界。

### 方案 C：Port 只接收 `ToolCall` 并自行查 Registry

不采用。适配器必须重复工具解析与注册表查找，且无法证明调用已经授权，容易形成第二套执行逻辑。

## Port 契约

`runtime/ports/tool_execution.py` 定义三个稳定类型：

```python
ToolEventCallback = Callable[[str, dict[str, object]], Awaitable[None]]

class ExecutableTool(Protocol):
    @property
    def name(self) -> str: ...

    async def execute(self, **kwargs: object) -> str: ...

@dataclass(frozen=True, slots=True)
class ToolExecutionOutcome:
    content: str
    duration_ms: int

@runtime_checkable
class ToolExecutionPort(Protocol):
    async def invoke(
        self,
        tool: ExecutableTool,
        arguments: Mapping[str, object],
        *,
        event_callback: ToolEventCallback | None = None,
    ) -> ToolExecutionOutcome: ...
```

契约规则：

- `arguments` 必须先复制，适配器不得修改 Engine 的权威参数；
- 仅当工具声明 `event_callback` 参数时注入 callback；
- 成功返回 UTF-8 可表示的 `str` 和非负毫秒耗时；
- 非字符串返回视为工具契约错误，不静默 `str(...)` 掩盖缺陷；
- 普通异常向 Engine 传播，由 Engine 统一转换为 `ToolResult(error)`；
- `CancelledError` 不得被吞掉，任务取消必须沿调用栈传播；
- Port 不执行权限判断、不发 tool_start/tool_end、不写 Harness、不失效缓存；
- 同一 Port 可能并发收到多个已标记 concurrency-safe 的工具调用。

`ToolEventCallback` 是 EventSink 完成前的精确兼容类型，不扩展现有事件职责。

## 默认适配器

`tools/execution.py` 提供 `LocalToolExecutor`：

- 使用 `time.perf_counter()` 计时；
- 复制 Mapping 为新的 kwargs；
- 通过函数签名决定是否注入 `event_callback`；
- 真实 `await tool.execute(**kwargs)`；
- 校验返回值是字符串；
- 返回 `ToolExecutionOutcome`；
- 不捕获普通异常或取消。

它是代码级执行适配器，不包含 LLM prompt、权限规则或 UI 文案。

## Engine 注入与唯一权威

`AgentEngine.__init__` 增加 keyword-only 参数：

```python
tool_execution_port: ToolExecutionPort | None = None
```

- 未注入时构造 `LocalToolExecutor`；
- 显式注入时使用 `is None` 判断，falsey Port 不得被替换；
- 不完整 Port 在创建 `.naumi` 数据前以中文 `TypeError` 失败；
- 唯一字段 `_tool_execution_port`，只读 `tool_executor` property 暴露 Port；
- `_execute_tool` 完成授权后调用 `_tool_execution_port.invoke(...)`；
- duration 和 content 从 outcome 构造现有 `ToolResult`；
- 成功后的知识缓存失效、任务快照和变更标记仍由 Engine 执行；
- `_execute_tool` 保留兼容，新增公共 `execute_tool` 作为 CLI/TUI/New UI/Pursuit 权威入口。

注入 Port 是可信基础设施依赖，但即使实现不可信，也只会看到已经通过 Engine 授权的调用。

## 产品表面迁移

- Engine 批次与 DynamicAgent 使用 Engine 权威执行入口；
- GoalPursuitLoop 的生产依赖改为强类型执行 facade，不再接受自由 `Any`；
- shared CLI、旧 CLI 兼容处理、TUI 和 New UI slash 命令使用 `engine.execute_tool`；
- 删除“找不到私有执行方法就直接 `tool.execute`”的生产 fallback；
- 独立 Tool 单元测试仍可直接测试 `Tool.execute`，因为那是在验证工具自身，不是产品运行路径。

## 安全、并发与错误不变量

- unknown tool、JSON 错误、Plan 阻止、PermissionPort、确认与 session generation 均发生在 Port 前；
- bypass 仍全权限通过，但会正常进入 Port；
- 安全工具批次仍最多 `max_parallel_tools` 并发，写/不安全工具保持串行；
- 一个普通失败只形成该调用的 error，不取消同批兄弟；
- 外层取消会取消整个相关 TaskGroup，不被 Port 或 Engine 普通异常处理吞掉；
- tool_start/tool_end 顺序、Harness evidence、run receipt 与 UI payload 不变；
- Port 失败不得触发 mutating success 标记或知识缓存失效。

## 测试策略

### Contract 与本地适配器

- `LocalToolExecutor` 满足完整 Port；不完整实现被拒绝；
- 使用真实 `FileWriteTool`/`FileReadTool` 在 pytest 临时目录完成写读闭环；
- 参数 Mapping 未被修改；event callback 仅对支持工具注入；
- 非字符串返回、普通异常和取消分别验证；
- duration 非负且使用真实调用结果。

### Engine 注入

- 记录型 Port 证明 unknown/parse/plan/permission 拒绝不命中 Port；
- 授权调用准确命中一次，并保留 call id/content/duration；
- falsey Port 不回退；无效 Port 在运行 I/O 前中文失败；
- Port 异常转为现有中文可理解的 ToolResult error；取消继续抛出；
- mutating 成功才触发 Harness cache invalidation。

### 真实产品闭环

- default/plan/bypass 各执行真实临时文件工具；
- 一组并行只读工具证明同一 Port 可并发，顺序和兄弟失败隔离不变；
- CLI、TUI、New UI shared slash command 均命中公开入口；
- Pursuit 的文件/命令动作命中同一入口；
- 完成回执仍记录工具成功、失败、权限和耗时。

## 架构产物

预计新增：

- `naumi_agent.runtime.ports.tool_execution`，owner=runtime；
- `naumi_agent.tools.execution`，owner=tools。

模块数预计 328→330。三类 SCC 数量不得增加，ownership issues 必须为 0。最终 baseline 与
ownership artifact 以源码 H1 SHA 互锁，并各生成两次逐字节比较。

## 验收标准

- Port 是已授权实际调用的唯一可替换边界，不复制安全与事件系统；
- 默认本地、记录型、falsey 和异常型 Port 均有真实行为测试；
- 生产产品表面不再直接调用 `Tool.execute` 或依赖私有 `_execute_tool`；
- 权限拒绝不会命中 Port，bypass 会命中且无确认；
- 真实文件读写、并行批次、取消、Harness 和 receipt 闭环通过；
- Ruff、compileall、聚焦 pytest、架构 40 测试和双扫描通过；
- 不运行全量测试，不实现 EventSink，不删除旧 CLI 源码。

## 实现自审与旁路分类

2026-07-15 对 `src/naumi_agent` 完成静态审计与聚焦运行验证，结论如下：

- `LocalToolExecutor` 是唯一直接执行 `ExecutableTool.execute(...)` 的默认生产适配器；
- `AgentEngine.execute_tool(...)` 是产品表面的公开权威入口，保留的
  `AgentEngine._execute_tool(...)` 只承载兼容实现，CLI、TUI、New UI、DynamicAgent 与 Engine
  批次均不再依赖该私有方法；
- `GoalPursuitLoop` 在 Engine 装配时明确注入 `self.execute_tool`。其中保留的直接执行分支仅供
  standalone loop/tool 单元构造使用，只有 `execute_tool_call is None` 时可达；Engine 在
  `AgentEngine.__init__` 中创建的生产 Pursuit 始终注入公开 facade；
- `tools/goal.py` 的 `pursuit.execute(...)` 是 Tool 内部组合，`subagent_manager.py` 的
  `coder.execute(...)` 是 Agent 生命周期调用，数据库、浏览器对象的 `.execute(...)` 也不属于
  ToolExecutionPort，均不构成工具执行旁路；
- PermissionPort、确认、grant、Plan/session 栅栏、Hook、Harness evidence 与 receipt 仍由
  Engine 编排，Port 只接收授权后的工具和参数，没有复制安全或事件系统；
- 记录型、falsey、异常、非字符串和取消 Port 均有契约测试；并行进入、兄弟失败隔离、外层取消、
  Permission BLOCK 不触达 Port、mutating 失败不失效缓存、bypass 无确认等边界均已聚焦验证；
- New UI 通过真实 bridge/shared slash router 测试命中 `engine.execute_tool`，CLI 与 TUI 也由各自
  聚焦测试证明使用同一 facade；旧 CLI 源码保留但不再形成第二条执行管线。

当前切片没有引入 EventSink，也没有把事件发送职责塞入 ToolExecutionPort。ARC-01.3d 完成后，
ARC-01.3 仍保持“进行中”，唯一剩余 Port 是 EventSink。

## 后续

完成后 ARC-01.3 只剩 EventSink。EventSink 必须独立审计和迁移现有事件调用点；ARC-01.4 再把
LocalToolExecutor 等默认 adapter 的构造移到 composition root。
