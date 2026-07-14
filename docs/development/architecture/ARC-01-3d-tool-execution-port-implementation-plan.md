# ARC-01.3d ToolExecutionPort 实现计划

> 对应设计：[ARC-01-3d-tool-execution-port-design.md](ARC-01-3d-tool-execution-port-design.md)

## 范围

只交付“授权后实际工具调用”Port、默认本地适配器、Engine 注入与产品表面统一执行入口。
不修改权限规则、不实现 EventSink、不新增通用超时、不删除旧 CLI，不运行全量测试。

## 预期文件

新增：

- `src/naumi_agent/runtime/ports/tool_execution.py`
- `src/naumi_agent/tools/execution.py`
- `tests/unit/test_tool_execution_port.py`

主要修改：

- `src/naumi_agent/runtime/ports/__init__.py`
- `src/naumi_agent/orchestrator/engine.py`
- `src/naumi_agent/orchestrator/pursuit.py`
- `src/naumi_agent/tools/pursuit.py`
- `src/naumi_agent/agents/base.py`
- `src/naumi_agent/cli/commands_analysis.py`
- `src/naumi_agent/cli/commands_meta.py`
- `src/naumi_agent/main.py`
- `src/naumi_agent/tui/app.py`
- 受影响的小模块测试和测试 double
- `docs/development/architecture/ARC-01-domain-boundaries.md`
- 两个 `docs/architecture/arc-01-*.json` 确定性产物

不得顺手改 UI 布局、Tool schema、PermissionPort、EventSink 或无关工具行为。

## Task 1：建立 RED contract

在 `test_tool_execution_port.py` 先写失败测试：

1. 导入 `ExecutableTool`、`ToolExecutionOutcome`、`ToolExecutionPort`；
2. Port 的公开方法集合精确为 `invoke`；
3. `LocalToolExecutor` 结构上满足 Port；
4. 缺少 `invoke` 的对象不满足 Port；
5. outcome 是 frozen/slots 强类型值对象；
6. 不允许 Port 返回自由 dict 或字符串冒充 outcome。

运行该单文件，记录因模块不存在失败；不得先写生产文件。

## Task 2：实现 Port 与本地适配器

### 2.1 Runtime Port

在 `runtime/ports/tool_execution.py`：

- 定义 `ToolEventCallback` 精确回调类型；
- 定义只包含 `name` 与异步 `execute` 的 `ExecutableTool` Protocol；
- 定义 `ToolExecutionOutcome(content: str, duration_ms: int)`；
- 在 `__post_init__` 拒绝非字符串 content、bool/负数 duration；
- 定义 runtime-checkable `ToolExecutionPort.invoke(...)`；
- 从 `runtime/ports/__init__.py` 导出稳定公共类型。

值对象校验必须有中文错误，不能信任远端/第三方 Port 的任意返回。

### 2.2 LocalToolExecutor

在 `tools/execution.py`：

- `arguments_copy = dict(arguments)`；
- 仅当 callback 非空且签名包含 `event_callback` 时注入；
- 使用 `perf_counter()` 前后差值并向下取非负整数毫秒；
- `await tool.execute(**arguments_copy)`；
- 非字符串返回抛中文 `TypeError`；
- 返回 `ToolExecutionOutcome`；
- 不捕获 `Exception` 或 `CancelledError`。

### 2.3 真实适配器测试

使用真实 `FileWriteTool` 和 `FileReadTool` 操作 pytest 临时目录：

- 写入真实 UTF-8 文件；
- 再读取并证明内容闭环；
- 原始 Mapping 保持不变；
- outcome duration 非负；
- 一个自定义支持 callback 的工具收到同一 callback；
- 不支持 callback 的工具不收到额外参数；
- 非字符串、普通异常、取消分别验证。

完成后运行 `test_tool_execution_port.py`、Ruff 两个新增生产文件和 compileall。

## Task 3：Engine 构造注入

先增加 RED tests：

- `AgentEngine(..., tool_execution_port=port)` 暴露同一 `tool_executor`；
- explicit falsey Port 不被默认适配器替换；
- 无效 Port 以中文 TypeError 失败且不创建 `.naumi`；
- 默认路径为 `LocalToolExecutor`。

生产修改：

- 构造参数只允许 keyword；
- 使用 `is None`；
- 在 Harness/SQLite 等运行 I/O 前校验 Protocol；
- 保存唯一 `_tool_execution_port`；
- 增加只读 `tool_executor` property；
- 不添加无意义 `close`，本地执行器无独立资源生命周期。

## Task 4：迁移单工具授权后调用

### 4.1 Engine 核心

在 `_execute_tool` 中保持以下顺序不变：

1. registry lookup；
2. parse arguments；
3. session transition gate；
4. Plan gate；
5. before permission Hook；
6. PermissionPort.check；
7. after permission Hook；
8. confirmation/grant/session generation recheck；
9. `tool_execution_port.invoke`；
10. mutating success 标记、Harness cache invalidation、task snapshot；
11. `ToolResult`。

删除直接 `tool.execute` 和 wall-clock 计时，使用 outcome。普通异常继续转为 error；
`CancelledError` 必须继续抛出。不要把未知、解析或权限错误交给 Port。

### 4.2 公共 facade

新增：

```python
async def execute_tool(
    self,
    tool_call: ToolCall,
    *,
    on_event: EventCallback | None = None,
    agent_name: str | None = None,
) -> ToolResult
```

它委托现有 `_execute_tool`，不复制管线。`_execute_tool` 暂时保留为兼容入口。

### 4.3 Engine RED/GREEN 场景

- unknown tool、非法 JSON、Plan 写入拒绝、Permission BLOCK 均不命中记录 Port；
- bypass 下真实临时文件写入命中 Port 一次且不确认；
- 授权读取保持 content/call_id/duration；
- Port 普通异常形成 error；
- 取消从 `execute_tool` 抛出；
- 失败不失效 Harness cache，写成功才失效。

## Task 5：保持批次与事件闭环

Engine `_execute_tool_calls` 继续使用现有 batch builder，只把单调用执行指向权威 facade。

测试：

- 两个 concurrency-safe 真实/记录工具同时进入 Port；
- unsafe 工具形成串行 barrier；
- 一个 Port 普通失败不取消兄弟；
- 外层取消取消批次；
- tool_start/tool_end 一一配对，索引和原模型调用顺序不变；
- Hook abort 不命中 Port；
- Harness evidence 和 receipt 保留 content length、duration、permission 状态。

只运行 `test_tool_batches.py`、Engine 对应节点、`test_harness_evidence.py` 对应节点和
`test_run_receipts.py` 对应节点。

## Task 6：迁移 Engine 装配消费者

### DynamicAgent

- 改用 `engine.execute_tool`；
- 删除对私有 `_execute_tool` 的生产依赖；
- 保持现有子 Agent 额外 Hook/权限行为不变，本切片不重构重复判定。

### GoalPursuitLoop

- 将自由 `Callable` 收窄为明确的 Engine tool-call facade Protocol/Callable 类型；
- Engine 注入公共 `execute_tool`；
- `tools/pursuit.py` 同步类型；
- 生产装配下所有文件、bash、background、schedule、worktree 动作命中 facade；
- standalone fallback 仅可保留在明确的独立测试/工具构造路径，产品路径不得使用。

聚焦运行 `test_pursuit.py` 和 `test_subagent_manager.py` 受影响节点。

## Task 7：迁移 CLI、TUI 与 New UI

### Shared CLI 与 main

- `commands_analysis.py` 使用公共 `execute_tool`；
- `commands_meta.py` 的所有工具命令统一构造 `ToolCall` 并走公共入口；
- `main.py` 通用 slash tool、分析、pursuit、worktree、background、schedule、self-review、
  evolve、forge、browser daemon 等可达处理使用公共入口；
- 删除 `getattr(engine, "_execute_tool")` + 直调 fallback；
- 旧 CLI 源码保留，行为与中文输出不删除。

### TUI

- 增加一个私有辅助方法，只负责构造唯一 call id、JSON arguments 并调用
  `self.engine.execute_tool`；
- pursuit/worktree/background/schedule/browser daemon 等处理复用该方法；
- error `ToolResult` 转成现有中文状态/Markdown，不吞掉失败。

### New UI

New UI bridge 已通过 shared slash router 进入 main handler。增加跨表面测试证明 New UI、TUI 和
CLI 的同一 slash command 都命中公共 facade，而非直接工具。

## Task 8：静态旁路审计

用 `rg` 和人工分类验证：

- `AgentEngine` 仅默认本地适配器文件包含生产 `tool.execute`；
- CLI/TUI/main/agents 不依赖 `._execute_tool`；
- 产品表面不直接 `await tool.execute(...)`；
- 允许保留：工具自身单元测试、Tool 内部组合能力、数据库 `.execute()`、Agent 生命周期
  `.execute()`、浏览器运行时动作；
- Pursuit 如保留 standalone fallback，必须有注释、测试和明确不可达于 Engine 装配的证据。

将分类结果写入设计自审或测试注释，不用脆弱的仓库全文字符串断言替代代码审查。

## Task 9：聚焦回归

运行且只运行相关模块：

- `test_tool_execution_port.py`；
- `test_tool_batches.py`；
- Engine 权限、确认、会话切换、工具调用、并发、取消指定节点；
- `test_permission_port.py`；
- `test_pursuit.py` 受影响节点；
- CLI slash/TUI/UI bridge 受影响节点；
- Harness evidence/receipt 受影响节点；
- Ruff 所有改动 Python 文件；
- compileall 所有改动模块。

禁止 `pytest tests/`、禁止全量测试、禁止无关网络模型调用。

## Task 10：自审

必须逐项回答：

- Port 是否只看到已授权调用？
- 是否仍有产品表面直调 Tool.execute？
- 是否错误地把 Permission/EventSink/Harness 放进 Port？
- falsey/异常/取消/非字符串实现是否真实验证？
- 并行安全屏障和兄弟失败隔离是否保持？
- bypass 是否仍全权限且无确认？
- mutating 失败是否错误触发缓存失效？
- CLI/TUI/New UI/Pursuit 是否确实走同一路径？

发现问题先补 RED test 再修，不能只在文档声明。

## Task 11：提交与架构产物

先提交源码/测试/状态为 H1：

```text
refactor(runtime): inject authorized tool execution port [ARC-01.3d]
```

用 H1 SHA 各生成两次 import graph baseline 与 ownership：

- 预期 330 个模块；
- `runtime.ports.tool_execution` owner=runtime；
- `tools.execution` owner=tools；
- ownership issues=0；
- import_time/typing/all_static SCC 数量不得超过 0/1/2；
- 无绝对路径、时间戳；
- baseline report digest 与 ownership import graph digest 一致；
- 两次输出逐字节一致。

amend 为 H2，并证明 H1/H2 的 `src/`、`tests/` 和开发文档逐字一致。

## Task 12：集成 main

1. 确认 feature worktree clean；
2. 主仓库 fetch `origin`，确认本地/远端 main；
3. 若远端前进，先在 feature 上合并或 rebase 并复跑相关测试；
4. 在 main 执行 `git merge --ff-only`；
5. main 复跑 Port + Engine 真实文件 + 架构 40 测试与 artifact cmp；
6. push main；
7. `git ls-remote` 核对远端 SHA；
8. 删除 feature worktree/branch 并 prune。

## 完成定义

- 授权后工具调用有可替换、强类型、真实本地实现的 ToolExecutionPort；
- Engine 保持安全权威，拒绝调用不触达 Port；
- 默认/记录型/falsey/异常/取消 Port 全部验证；
- CLI、TUI、New UI、DynamicAgent、Pursuit 的生产路径统一走公共 Engine facade；
- 真实文件、并发、Harness、receipt 和 bypass 闭环无回归；
- 330 模块架构产物与源码 H1 互锁；
- 聚焦验证通过并推送 main；
- ARC-01.3 诚实标明只剩 EventSink。
