# ARC-01.4 Composition Root 总体设计

## 背景

ARC-01.3 已完成 SessionPort、PermissionPort、ModelPort、ToolExecutionPort 与 EventSink 五个
Runtime Port，但默认实现仍由 `AgentEngine.__init__` 创建。端口因此“可以注入”，生产路径却没有真正
在启动层装配，Runtime 仍同时承担业务编排、基础设施选择、路径解析、资源构造和关闭顺序。

2026-07-15 对当前源码完成静态与运行路径审计，确认：

- `AgentEngine.__init__` 直接创建 `NullEventSink`、`SessionStore`、`PermissionChecker`、
  `ModelRouter` 与 `LocalToolExecutor`；
- 同一构造函数还创建 Harness、ChatRun、长期记忆、Task、Workbench、Background、Scheduler、
  Goal、Pursuit、Browser、Worktree 等 Store、Runner 与 Service；
- `resolve_harness_trust_db_path()`、`resolve_harness_db_path()` 等进程级路径解析在 Engine 内发生；
- Engine 的关闭逻辑知道 Session、Browser、MCP、Background、Scheduler 与 SubAgent 的具体关闭
  方法，但并不拥有一个可验证的资源登记表；
- 生产源码有 4 个直接 `AgentEngine(config)` 调用点：TUI、旧 CLI fallback、单任务 run、FastAPI；
- New UI Bridge 通过 `EngineFactory` 间接默认到 `AgentEngine`，也是一个生产装配入口；
- 测试源码有 171 个直接 `AgentEngine(...)` 构造点，不能用一次机械替换掩盖依赖边界与测试意图；
- 当前五类 Port 与 EventSink 注入基线小测试为 96 passed，说明端口契约可作为迁移起点。

如果只增加一个 `build_agent_engine()` 包装函数，而 Engine 继续创建默认依赖，ARC-01.4 的最终状态
并未成立。Composition Root 必须成为默认 adapter、运行路径和资源生命周期的唯一 owner。

## 术语

- **Composition Root**：读取已经解析好的 `AppConfig`，选择具体 adapter，构造完整对象图，并把
  对象图交给 Runtime 的唯一启动层。
- **Port**：Runtime 消费的稳定行为契约，不知道 adapter 的创建方式。
- **Adapter**：SessionStore、PermissionChecker、ModelRouter、LocalToolExecutor、NullEventSink 等
  Port 的具体实现。
- **Resource**：拥有文件、数据库、浏览器、后台任务或其他需要关闭的状态对象。
- **Service**：组合 Resource/Port 执行业务能力的长期对象，例如 WorkbenchService。
- **Transient state**：单个 Engine 实例内部的消息列表、计数器、锁、当前 session 等纯运行状态；
  它们不是外部依赖，可继续由 Engine 初始化。

## 目标

1. 所有生产入口通过一个权威 Composition Root 构造 `AgentEngine`。
2. `AgentEngine` 不导入或直接创建五个默认 Port adapter。
3. 默认路径、Store、Runner、Service 与其关闭责任逐步移出 Engine 构造函数。
4. 显式覆盖依赖时保留对象 identity；falsey 依赖不能被默认值替换。
5. 构造失败不能留下已启动后台任务、打开连接或半初始化 Engine。
6. 关闭按依赖的反向顺序、幂等执行；一个组件失败不阻止其他组件释放。
7. CLI、TUI、New UI、API、单任务 run 使用同一默认装配，不形成产品旁路。
8. 测试使用明确的测试装配或显式依赖，不长期依赖 Engine 内部“魔法默认值”。
9. 每个子阶段有独立静态门、契约测试、真实 Engine smoke 和独立 commit。
10. ARC-01.4 完成后，ARC-01.5 可以只处理 legacy API，而不再承担默认依赖迁移。

## 非目标

- 不引入 dependency-injector、punq、wired 等第三方 IoC 容器；
- 不使用全局 Service Locator、模块级 mutable singleton 或隐式 thread-local；
- 不移动大目录，不重写 ReAct 主循环、工具行为、UI 协议或模型协议；
- 不把每个纯值对象都做成 Port；预算值、计数器、列表和锁仍属于 Runtime 内部状态；
- 不在 ARC-01.4 删除旧 CLI 或 legacy EventEmitter 源码，移除条件属于 ARC-01.5；
- 不把 AppConfig 变成全局对象。入口仍负责读取配置文件并显式传入；
- 不运行项目全量测试，只运行每个子阶段覆盖到的模块测试和真实小场景。

## 方案比较

### 方案 A：只增加 Engine factory

在 `runtime/composition.py` 新增 `create_agent_engine(config)`，内部仍执行
`AgentEngine(config)`。

优点是迁移生产调用点很快，测试几乎无需变化。缺点是默认 adapter、Store 和生命周期仍由
Engine 隐式决定，factory 只是名称包装，无法证明依赖方向、覆盖语义或关闭所有权。

结论：不采用。它不能满足 ARC-01.4 的完成定义。

### 方案 B：显式依赖包 + 唯一 Composition Root + 分阶段去默认化（采用）

将路径、Port、Resource、Service 分成有类型的依赖包。Composition Root 负责构造，Engine 只消费。
先迁移五个已稳定 Port，再迁移 Store/资源、Service/工具注册，最后统一生命周期并删除 Engine 的兼容
默认入口。

优点：

- 每个阶段都让最终状态更真实，不需要一次改写 171 个测试；
- 依赖包字段是可搜索、可类型检查的显式 API，不会像 dict/Any 一样自由扩张；
- 可单独测试默认选择、覆盖注入、构造失败回滚、关闭顺序和产品入口一致性；
- Engine 的 import 边界会逐阶段收缩，ARC-01.6 能用静态规则锁定结果。

代价是迁移期需要一个明确的兼容窗口，而且最终会更新大量测试构造点。兼容窗口必须带静态计数与
移除任务，不能永久保留。

### 方案 C：全量 IoC 容器或 Service Locator

把所有类型注册到容器，Engine 在运行时按类型或字符串查找依赖。

优点是对象图扩展方便。缺点是依赖变成运行时隐式关系，构造错误更晚暴露，测试 override 容易污染
全局状态，类型检查和 import graph 也无法直接证明边界。

结论：不采用。NaumiAgent 需要可审计、自进化可理解的对象图，而不是隐藏对象图。

## 最终架构

```text
CLI/TUI/New UI/API/run entrypoint
  -> AppConfig.from_yaml() + bind_runtime_workspace()
  -> runtime.composition.create_agent_engine(config, overrides)
       -> resolve RuntimePaths
       -> build RuntimePorts
       -> build RuntimeResources
       -> build RuntimeServices
       -> register lifecycle callbacks
       -> AgentEngine(config, dependencies)
  -> AgentEngine run/run_streaming
  -> AgentEngine.shutdown()
       -> injected RuntimeLifecycle.close()
       -> reverse-order, idempotent cleanup
```

唯一允许导入具体 adapter 并决定默认实现的模块是
`src/naumi_agent/runtime/composition.py` 及它明确调用的 runtime builder 子模块。UI、API 与 Engine
不得各自复制默认构造逻辑。

## 依赖包边界

### RuntimePaths

`RuntimePaths` 是启动时一次解析、只读传递的路径值：

```python
@dataclass(frozen=True, slots=True)
class RuntimePaths:
    workspace_root: Path
    runtime_data_dir: Path
    worktree_storage_dir: Path
    harness_db_path: Path
    harness_trust_db_path: Path
    browser_data_dir: Path
    browser_daemon_log_dir: Path
```

所有字段必须是 absolute `Path`。`workspace_root` 来自
`config.resolve_workspace_root()`；运行数据路径从显式配置派生。Composition Root 不调用 `chdir()`，
也不把 bypass 解释为工作区路径改变：bypass 只改变授权结果，不改变启动目录的事实。

### RuntimePorts

`src/naumi_agent/runtime/dependencies.py` 定义协议化、不可变的五 Port 依赖包：

```python
@dataclass(frozen=True, slots=True)
class RuntimePorts[SessionT]:
    session_port: SessionPort[SessionT]
    permission_port: PermissionPort
    model_port: ModelPort
    tool_execution_port: ToolExecutionPort
    event_sink: EventSink
```

该模块只导入 Port Protocol，不导入具体 adapter。`AgentEngine` 使用
`RuntimePorts[Session]`，Composition Root 使用相同对象，不复制字段。

覆盖类型单独定义：

```python
@dataclass(frozen=True, slots=True)
class RuntimePortOverrides[SessionT]:
    session_port: SessionPort[SessionT] | None = None
    permission_port: PermissionPort | None = None
    model_port: ModelPort | None = None
    tool_execution_port: ToolExecutionPort | None = None
    event_sink: EventSink | None = None
```

`None` 是唯一“使用默认值”的信号。实现必须使用 `is None`，显式 falsey adapter 保持 identity。
所有非 None override 在创建任何默认 adapter 之前完成 Protocol 校验，错误使用中文并指出字段与完整
契约。

### RuntimeResources

ARC-01.4b 将外部状态对象放入 `RuntimeResources`。它至少覆盖：

- HarnessTrustStore、HarnessStore、ChatRunStore；
- LongTermMemory；
- TaskStore、WorkbenchStore；
- BackgroundTaskStore/Runner、SchedulerStore/Runner；
- GoalStore、PursuitStore；
- BrowserRuntime、BrowserDaemonClient；
- WorktreeManager 所需的 storage 与 task store。

资源包可以使用具体类型，因为它属于 runtime 启动层；Engine 只从包中取已经构造好的实例，不再调用
这些类的构造函数。每个有关闭行为的对象必须登记到 `RuntimeLifecycle`，无关闭行为的对象不能伪造
空 close。

### RuntimeServices

ARC-01.4c 将依赖 Port/Resource 的长期服务放入 `RuntimeServices`，至少覆盖：

- HarnessService；
- ContextCompactor；
- AdaptivePlanner；
- ValidationRunner、ReviewEvidenceCollector、WorkbenchService；
- RuntimeInspectorService；
- SkillLoader；
- 已完成默认注册的 ToolRegistry。

工具注册仍可由专用 builder 执行，但 builder 接收显式 Port/Resource/Service，不读取模块级全局配置。
分析工具当前的 `set_analysis_router()` 是存量全局注入，必须在 4c 中记录并隔离，最终移除或变成明确
adapter；不能把它当成完成后的合法 Composition Root 行为。

### EngineDependencies

最终 `AgentEngine` 只接收一个完整依赖包：

```python
@dataclass(slots=True)
class EngineDependencies[SessionT]:
    paths: RuntimePaths
    ports: RuntimePorts[SessionT]
    resources: RuntimeResources
    services: RuntimeServices
    lifecycle: RuntimeLifecycle
```

依赖包不能有默认 factory；缺字段必须在构造时失败。Engine 可以创建自身 transient state，但不得在
缺依赖时回退到具体实现。

## 构造与覆盖语义

权威 API：

```python
def build_runtime_ports(
    config: AppConfig,
    *,
    paths: RuntimePaths,
    overrides: RuntimePortOverrides[Session] | None = None,
) -> RuntimePorts[Session]: ...

def create_agent_engine(
    config: AppConfig,
    *,
    port_overrides: RuntimePortOverrides[Session] | None = None,
) -> AgentEngine: ...
```

后续阶段会把 resource/service overrides 加入一个顶层 `RuntimeOverrides`，但 4a 不提前创建空壳字段。
默认构造顺序固定为：Paths → validation of overrides → Ports → Resources → Services → Engine。构造函数
不能修改传入 config，也不能读配置文件或环境变量；配置加载只发生在入口。

同一个显式 override 必须原样出现在 `engine.session_store`、`engine._permission_port`、
`engine.router`、`engine.tool_executor`、`engine.event_sink`，不得包装、复制或 truthiness
替换。需要 adapter 包装时，调用方显式传 adapter 本身。

## 生命周期与失败语义

`RuntimeLifecycle` 在 ARC-01.4d 提供：

```python
class RuntimeLifecycle:
    def add(self, name: str, close: AsyncClose) -> None: ...
    async def close(self) -> tuple[RuntimeCloseFailure, ...]: ...
```

规则：

1. 注册顺序等于构造顺序，关闭严格反向；
2. `close()` 幂等且并发安全，重复调用不重复关闭组件；
3. `CancelledError` 在记录已完成清理后继续传播，不转换成普通错误；
4. 一个组件普通异常记录中文可诊断信息并继续关闭其余组件；
5. 构造过程中失败时，Composition Root 关闭已经登记的组件，然后重新抛出原异常；
6. Engine shutdown 先停止主动生产任务，再调用 lifecycle；
7. 不同时在 Engine 与 lifecycle 各关闭一次同一对象；每个资源只有一个 owner；
8. lifecycle 失败列表进入日志与 Inspector，不因 UI 已关闭而丢失。

SessionStore 当前延迟打开 SQLite，因此 4a 的同步 builder 不需要事件循环。真正启动异步资源的阶段
必须使用 async bootstrap 或在构造时只创建惰性对象，不能调用 `asyncio.run()` 嵌套事件循环。

## 生产入口收口

以下入口必须使用相同的 `create_agent_engine()`：

| 产品表面 | 当前位置 | ARC-01.4 目标 |
| --- | --- | --- |
| Textual TUI | `main._launch_tui` | root factory |
| 旧 CLI fallback | `main._chat` | root factory，代码保留 |
| 单任务 run | `main._run_task` | root factory |
| FastAPI | `api.app.lifespan` | root factory + lifespan shutdown |
| New UI Bridge | `ui.bridge.create_bridge` | 默认 factory 指向 root，测试 factory override 保留 |

入口仍负责配置路径解析、日志初始化、API key 用户提示和 UI 专属对象。它们不得选择 ModelRouter、
SessionStore 或权限实现。

## 兼容迁移策略

4a 期间 `AgentEngine(config, session_port=..., ...)` 暂时保留，但实现必须把这些字段转换为
`RuntimePortOverrides` 并委托 Composition Root；Engine 源码不再导入具体 adapter。生产入口不得使用
兼容路径。

兼容路径设置静态预算：

- 生产源码允许的直接 `AgentEngine(config)` 调用数从当前 4 降为 0；
- New UI 默认 factory 不再等于 `AgentEngine`；
- 测试直接构造点以当前 171 为上限，只能下降不能增加；
- 4d 完成时测试构造点迁移到 `tests/support/runtime_factory.py` 或显式完整依赖，Engine 兼容参数删除；
- 删除兼容路径是 ARC-01.4 的退出门，不推迟到 ARC-01.5。

ARC-01.5 只处理真正的 legacy 表面，例如旧 callback union、legacy emitter 与旧 CLI API；它不负责
替 ARC-01.4 清理默认构造。

## 分阶段交付

### ARC-01.4a：Runtime Ports Composition

- 新增 `RuntimePorts`、`RuntimePortOverrides` 与默认 Port builder；
- Engine 不再导入/构造五个具体 adapter；
- 五个生产表面统一使用 root factory；
- 保留有静态预算的测试兼容入口；
- 通过默认、override、falsey、invalid、真实 Engine 与入口路由测试。

### ARC-01.4b：Paths、Store 与 Resource ownership

- 一次解析 RuntimePaths；
- 把所有 Store、Browser、Runner 的默认构造移到 root builder；
- 建立资源 owner 清单和构造失败回滚测试；
- Engine 不再调用进程级路径 resolver。

### ARC-01.4c：Service 与 Tool bootstrap

- 构造 Harness、Compactor、Planner、Workbench、Inspector、Skill 与 ToolRegistry；
- 删除 Engine 内 `_register_*` 默认装配职责，保留运行时动态注册 API；
- 隔离/移除 analysis router 的模块级全局注入；
- 验证工具数量、名称、依赖 identity 与真实工具调用不变。

### ARC-01.4d：Lifecycle、入口硬切换与兼容删除

- 注入 RuntimeLifecycle，建立反向、幂等、失败继续的关闭链；
- 所有测试使用测试 root 或显式完整依赖；
- 删除 Engine 的各个 optional 默认依赖参数；
- 用 AST/import rules 证明生产与测试都不存在隐式默认构造；
- 重新生成 ARC-01 import graph 与 ownership artifact。

每个阶段单独设计、计划、TDD、真实验证和 commit；4b 不在 4a 尚未验收时提前实现。

## 可观测性与用户体验

- 启动失败必须指出阶段（路径、模型、权限、存储、服务或 Engine）与根因，不输出 Python 对象地址；
- API、CLI、TUI 和 New UI 对同一装配错误使用同一个中文诊断正文，表面只负责格式化；
- debug trace 记录 composition stage、duration 和失败组件，但不得记录 API key 或完整 secret config；
- 正常启动不增加用户可见噪音；仅在失败或 doctor/Inspector 中展示详细对象图信息；
- shutdown 某组件失败时，用户看到“已尽力释放，其余组件已继续关闭”，并能从 debug trace 定位名称。

## 安全与跨平台约束

- 所有路径使用 `pathlib.Path`，不拼接 `/`、不假设 POSIX home；
- Windows 不依赖 fork、signal-only cleanup 或 Unix socket；
- macOS/Linux/Windows 的 workspace 事实都来自启动 cwd 绑定后的 config；
- bypass 继续代表工具授权全通过，但不改变 config、workspace identity 或 resource storage root；
- debug/异常输出不得包含 provider token、Brave API key、环境变量值或完整配置序列化；
- override 只在当前 Engine 实例生效，不写回 `.naumi` 或全局配置。

## 验收标准

ARC-01.4 总体完成必须同时满足：

1. `AgentEngine` 不导入或实例化五个默认 adapter、Store、Runner、Browser、长期服务；
2. 所有生产入口只通过一个 Composition Root 构造 Engine；
3. Engine 构造要求完整 `EngineDependencies`，没有隐藏默认或 Service Locator；
4. 五个 Port 与所有 Resource/Service override 保持 identity，falsey 值不被替换；
5. 构造失败回滚、关闭反序/幂等/异常继续有真实异步测试；
6. CLI、TUI、New UI、API 与 run 的 focused startup/stream/shutdown 测试通过；
7. 一个真实 `AgentEngine.run_streaming()` 使用 root 构造，完成回执与 ARC-01.3 行为一致；
8. 171 个测试直接构造点全部迁移，不新增生产或测试旁路；
9. Ruff、compile、各子模块 pytest 与真实临时目录 smoke 通过，不以全量测试代替定向证据；
10. import graph/ownership 两次生成逐字节一致，SCC 不回归、ownership issues=0；
11. `ARC-01-domain-boundaries.md` 标记 ARC-01.4 已实现并链接最终审计证据；
12. 当前分支每个子阶段独立 commit，最后 fast-forward 合入 main 并核对 origin/main。

## 自我审视

- 本设计没有把 factory 名称当成完成证据，退出门要求 Engine 真正失去默认构造能力。
- 4a 保留兼容入口是有意的迁移机制，不是最终状态；静态预算和 4d 删除门阻止其永久化。
- RuntimeResources/RuntimeServices 的字段将在各自子阶段设计中以当前源码再次审计后锁定，避免本总体
  设计凭空冻结尚未核查的关闭方法；但“覆盖哪些已存在对象”和最终退出门已经明确。
- AppConfig 仍作为显式配置值传入 Engine；本设计禁止的是全局读取与依赖构造，不在本阶段发明另一
  套配置 schema。若后续证明 Engine 只需少量字段，可在独立 ARC 中引入配置投影。
- 当前 SessionStore 是惰性打开，4a 可安全保持同步 builder；4b 遇到真实异步启动资源时必须采用
  async bootstrap，不能用同步 close 假装回滚。
- 最大风险是 171 个测试迁移带来行为噪音，因此按 4a~4d 分批并用直接构造计数单调下降约束，不把
  测试批量改写与业务功能混在同一提交。

## 后续关系

- ARC-01.5：在稳定 root 之上移除 legacy callback、emitter 与旧构造 API；
- ARC-01.6：把本设计的唯一 root、禁止反向 import 与 direct-construction 预算固化为 CI；
- ARC-02：只有 Runtime 对 UI/Tool/Store 的依赖均可注入、生命周期可验证后才能开始目录重组；
- Harness、高并发和 Agent 集群后续新增 adapter 时必须在 root 注册，不允许回到 Engine 内条件构造。
