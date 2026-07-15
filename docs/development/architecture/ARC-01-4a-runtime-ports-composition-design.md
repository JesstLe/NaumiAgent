# ARC-01.4a Runtime Ports Composition 设计

## 范围

本切片只实现 ARC-01.4 的第一阶段：把 SessionPort、PermissionPort、ModelPort、
ToolExecutionPort 与 EventSink 的默认 adapter 构造移到唯一 Composition Root，并让所有生产入口
使用该 root。

本切片不迁移 Harness、ChatRun、长期记忆、Task、Workbench、Background、Scheduler、Goal、
Pursuit、Browser、Worktree 等 Resource/Service；它们属于 4b/4c。本切片也不删除 Engine 的测试
兼容构造入口，删除门属于 4d。

## 当前证据

- 五个 Port 均为 `@runtime_checkable Protocol`；
- 默认 adapter 分别是 `SessionStore`、`PermissionChecker`、`ModelRouter`、
  `LocalToolExecutor`、`NullEventSink`；
- Engine 已对每个显式注入对象执行完整 Protocol 校验，并正确使用 `is None`；
- 默认构造集中在 `AgentEngine.__init__` 的连续区域，但 Engine 顶部因此导入所有具体 adapter；
- TUI、旧 CLI、run、API 有 4 个直接构造点，Bridge 有 1 个默认 factory 旁路；
- 测试直接构造点为 171；
- 相关基线测试 96 passed。

## 目标

1. `runtime/dependencies.py` 提供不可变、协议化的 Port bundle 与 override bundle。
2. `runtime/composition.py` 是五个默认 adapter 的唯一生产构造点。
3. Engine 只消费 `RuntimePorts[Session]`，不导入具体 adapter。
4. 现有 individual keyword overrides 保持行为，但通过 root builder 解析。
5. 所有产品入口默认走 `create_agent_engine()`。
6. 默认构造、混合覆盖、falsey override、非法 override 与 identity 有契约测试。
7. 真实 Engine streaming 小场景证明 receipt、session、permission、model、tool 与 event 行为不变。
8. 静态审计证明没有新增 default-construction 旁路。

## 文件边界

### `src/naumi_agent/runtime/dependencies.py`

只定义：

```python
@dataclass(frozen=True, slots=True)
class RuntimePorts[SessionT]:
    session_port: SessionPort[SessionT]
    permission_port: PermissionPort
    model_port: ModelPort
    tool_execution_port: ToolExecutionPort
    event_sink: EventSink

@dataclass(frozen=True, slots=True)
class RuntimePortOverrides[SessionT]:
    session_port: SessionPort[SessionT] | None = None
    permission_port: PermissionPort | None = None
    model_port: ModelPort | None = None
    tool_execution_port: ToolExecutionPort | None = None
    event_sink: EventSink | None = None
```

`RuntimePorts.__post_init__` 逐字段执行 Protocol 校验。错误文本固定为：

```text
session_port 必须实现完整的 SessionPort 契约：create_session/save/load/list_sessions/delete/archive/close
permission_port 必须实现完整的 PermissionPort 契约：mode/set_mode/check/reset_counts
model_port 必须实现完整的 ModelPort 契约：metadata/routing/capability/discovery/reasoning/call/stream
tool_execution_port 必须实现完整的 ToolExecutionPort 契约：invoke
event_sink 必须实现完整的 EventSink 契约：emit
```

override bundle 不在 `__post_init__` 验证 None，但所有非 None 字段调用与 RuntimePorts 相同的公开
`validate_runtime_port_overrides()`。这样 Composition Root 能在创建任何默认 adapter 前失败。

该模块不得导入 `SessionStore`、`PermissionChecker`、`ModelRouter`、`LocalToolExecutor`、
`NullEventSink` 或 `AgentEngine`。

### `src/naumi_agent/runtime/composition.py`

公开：

```python
def build_runtime_ports(
    config: AppConfig,
    *,
    overrides: RuntimePortOverrides[Session] | None = None,
) -> RuntimePorts[Session]: ...

def create_agent_engine(
    config: AppConfig,
    *,
    port_overrides: RuntimePortOverrides[Session] | None = None,
) -> AgentEngine: ...
```

`build_runtime_ports()` 的确定性顺序：

1. `overrides is None` 时使用空 override bundle；
2. 校验所有非 None override；
3. 解析 `workspace_root` 与 `runtime_data_dir`，但不创建目录；
4. 对每个字段使用 `override if override is not None else default`；
5. model 默认值先加载显式 catalog path，再构造 ModelRouter；
6. permission 默认 allowed dirs 顺序保持现状：配置目录、workspace、worktree storage；
7. 构造 RuntimePorts，再次做完整 bundle 校验；
8. 返回同一 override identity。

`create_agent_engine()` 在函数体内局部导入 `AgentEngine`，避免
`engine -> dependencies` 与 `composition -> engine` 形成 import-time cycle；它只执行：

```python
ports = build_runtime_ports(config, overrides=port_overrides)
return AgentEngine(config, ports=ports)
```

不捕获、改写或吞掉构造异常。

### `src/naumi_agent/orchestrator/engine.py`

新增优先参数：

```python
def __init__(
    self,
    config: AppConfig,
    *,
    ports: RuntimePorts[Session] | None = None,
    session_port: SessionPort[Session] | None = None,
    permission_port: PermissionPort | None = None,
    model_port: ModelPort | None = None,
    tool_execution_port: ToolExecutionPort | None = None,
    event_sink: EventSink | None = None,
) -> None:
```

解析规则：

- `ports` 非 None 时，五个 legacy individual override 必须全是 None，否则抛中文 `TypeError`，避免
  同一字段有两个来源；
- `ports` 非 None 时直接使用并由 RuntimePorts 自身保证完整契约；
- `ports` 为 None 时，把五个 legacy 参数装入 `RuntimePortOverrides`，通过函数内局部 import 调用
  `build_runtime_ports()`；
- 兼容路径不得包含任何具体 adapter 构造；
- 原有 `_session_port/_permission_port/_model_port/_tool_execution_port/_event_sink` 和 property 行为不变；
- 删除 Engine 顶部对五个具体 adapter 与 catalog loader 的 import。

这条兼容路径只服务现有测试和外部调用，不是生产默认入口。4d 会删除 `ports is None` 分支和五个
individual 参数。

## 生产入口迁移

### `src/naumi_agent/main.py`

`_launch_tui`、`_chat`、`_run_task` 局部导入 `create_agent_engine` 并调用
`create_agent_engine(config)`。其他配置、日志、API key、UI 样式和 shutdown 行为不变。

### `src/naumi_agent/api/app.py`

模块导入 `create_agent_engine`，lifespan 使用 root 构造。FastAPI state 和 finally shutdown 不变。

### `src/naumi_agent/ui/bridge.py`

保留 `EngineFactory = Callable[[AppConfig], AgentEngine]` 和测试注入。`engine_factory is None` 时局部导入
`create_agent_engine` 并设为默认；显式 fake factory 不经 root 包装。

## 错误与边界情况

- falsey adapter：定义 `__bool__ -> False` 的完整 Port 必须保留；
- partial adapter：缺任一方法/属性，在默认构造前失败；
- invalid config catalog：保持 `load_provider_catalog` 原异常，不回退内置 catalog；
- workspace path：使用 `resolve_workspace_root()`，不得改 cwd；
- duplicate source：同时传 ports 与 legacy override 必须失败，不能静默选择其一；
- empty override bundle：行为与默认 root 完全一致；
- repeated root creation：每次产生独立默认 Port，不能缓存单例；
- explicit shared override：调用方主动共享时保持同一对象，不擅自 clone；
- constructor failure：4a 的默认 Port 均为惰性/无后台任务构造，不产生异步清理义务；真正资源回滚在 4b。

## 测试设计

### Contract tests

`tests/unit/test_runtime_dependencies.py`：

- 完整记录型/falsey 五 Port 可组成 bundle；
- 每个缺失契约字段分别失败并检查中文字段名；
- override None 被允许，非 None partial adapter 失败；
- dataclass frozen，字段不可替换。

`tests/unit/test_runtime_composition.py`：

- 默认对象类型分别正确；
- workspace/worktree allowed dirs 与现状一致；
- catalog path 传给 loader；
- 五种单独 override 与混合 override identity 不变；
- falsey override 不被替换；
- invalid override 在 monkeypatched 默认构造器调用前失败；
- 两次默认构造不共享 mutable Port；
- `create_agent_engine()` 把同一 RuntimePorts 传入 Engine。

### Engine compatibility tests

`tests/unit/test_engine_port_injection.py` 或现有 Port 测试：

- `AgentEngine(config, ports=ports)` 使用完整 bundle；
- ports + individual override 冲突失败；
- legacy individual override 仍保留 identity；
- monkeypatch Engine 模块，证明其中不存在五个具体 adapter symbol；
- `AgentEngine(config)` 兼容路径委托 `build_runtime_ports`，不自行构造。

### Entrypoint tests

- `tests/unit/test_main_run.py`：run 使用 root factory，并仍 shutdown；
- `tests/unit/test_ui_bridge.py`：默认 root 与显式 fake factory 两条路径；
- 新增或更新 API lifespan 小测试：root engine 被放入 app.state 并 shutdown；
- TUI/CLI 只做 constructor routing 小测试，不启动完整交互 UI。

### 真实场景

使用临时 workspace、SQLite 路径、记录型 ModelPort 与 EventSink，通过
`create_agent_engine(config, port_overrides=...)` 构造 Engine，执行一个真实 `run_streaming()`：

- SessionPort create/save 被调用；
- PermissionPort 参与工具授权；
- ModelPort 返回一轮最终响应；
- EventSink 收到 run_started/response/completion receipt；
- `engine.shutdown()` 关闭同一 SessionPort；
- result response、usage 和 receipt 与直接显式注入路径一致。

## 静态验收

以下事实必须由 AST/rg 测试锁定：

1. `orchestrator/engine.py` 不导入五个具体 adapter 或 `load_provider_catalog`；
2. Engine 源码不出现这些 adapter 的构造调用；
3. `runtime/composition.py` 是生产源码唯一同时构造五个默认 adapter 的模块；
4. `main.py` 与 `api/app.py` 没有 `AgentEngine(config)`；
5. Bridge 默认 factory 不指向 AgentEngine；
6. 测试直接 `AgentEngine(...)` 构造数不超过基线 171；
7. import-time/typing/all-static SCC 不增加。

## 定向验证命令

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/unit/test_runtime_dependencies.py \
  tests/unit/test_runtime_composition.py \
  tests/unit/test_session_port.py \
  tests/unit/test_permission_port.py \
  tests/unit/test_model_port.py \
  tests/unit/test_tool_execution_port.py \
  tests/unit/test_event_sink_port.py \
  tests/unit/test_event_sink_injection.py

PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/unit/test_main_run.py \
  tests/unit/test_ui_bridge.py \
  tests/unit/test_api_app.py

PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/integration/test_runtime_composition_streaming.py

.venv/bin/ruff check \
  src/naumi_agent/runtime/dependencies.py \
  src/naumi_agent/runtime/composition.py \
  src/naumi_agent/orchestrator/engine.py \
  src/naumi_agent/main.py \
  src/naumi_agent/api/app.py \
  src/naumi_agent/ui/bridge.py \
  tests/unit/test_runtime_dependencies.py \
  tests/unit/test_runtime_composition.py \
  tests/integration/test_runtime_composition_streaming.py

PYTHONPATH=src .venv/bin/python -m compileall -q \
  src/naumi_agent/runtime \
  src/naumi_agent/orchestrator/engine.py \
  src/naumi_agent/api/app.py \
  src/naumi_agent/ui/bridge.py
```

工作树中 `.venv` 不存在时使用主仓库绝对路径
`/Users/lv/Workspace/NaumiAgent/.venv/bin/python` 与对应 `ruff`，验证范围不变。

## 完成定义

- RuntimePorts/Overrides 契约真实、冻结、可验证，不使用 Any/dict container；
- 默认 Port 的选择与构造只有一个 owner；
- Engine 不知道具体默认 adapter，兼容路径只委托 root；
- 五个产品入口使用同一 root，测试 factory override 不受破坏；
- default/override/falsey/invalid/duplicate source/repeated construction 均有测试；
- 一个真实 streaming 任务从 root 到 receipt、session close 完成闭环；
- 小模块 pytest、Ruff、compile 与静态审计通过；
- 独立 commit 后更新总体状态为“ARC-01.4a 已实现，4b 待开发”。

## 自我审视

- 本切片确实移动了默认 adapter 构造，而不是只增加 factory 名称。
- `AgentEngine(config)` 暂时仍可运行，但它不再拥有具体构造逻辑；这是为 171 个测试设置的有界兼容，
  不是 ARC-01.4 总体完成证据。
- 本切片没有假装解决 Store/Service 生命周期；4b/4d 仍是明确退出门。
- production direct-construction 计数与 test direct-construction 上限使兼容债务可量化。
- 测试包含真实 Engine streaming，而不是只验证 import 或 dataclass。
- 唯一尚未在本切片解决的全局注入是 analysis router；它依赖 ModelPort，但属于工具 bootstrap，已明确
  归入 4c，不在 4a 偷做半套迁移。
