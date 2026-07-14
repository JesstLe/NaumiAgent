# ARC-01.3c ModelPort 实现计划

> 对应设计：[ARC-01-3c-model-port-design.md](ARC-01-3c-model-port-design.md)

## 范围

只交付完整 ModelPort 及其消费者迁移。禁止修改 Provider transport、catalog 规则、UI 布局、
ToolExecutionPort 和 EventSink；不跑全量测试。

## 文件

新增：

- `src/naumi_agent/runtime/ports/model.py`
- `tests/unit/test_model_port.py`

修改：

- `runtime/ports/__init__.py`
- `orchestrator/engine.py`、`planner.py`、`pursuit.py`
- `memory/compactor.py`
- `agents/factory.py`
- `tools/analysis.py`
- `tools/browser/orchestrator/task_runner.py`
- `tools/browser/subagent/planner.py`
- ARC-01 状态与两份架构 artifact。

## Task 1：隔离与基线

创建 `codex/arc-01-3c-model-port` worktree。使用项目 `.venv` 跑：

- `tests/unit/test_model_router.py`；
- Engine 简单非流式、流式回执与启动延迟各一个节点；
- model surfaces、UI bridge、API tools、TUI 中各一个能力/身份节点。

先用 `--collect-only` 验证节点名，避免把未收集误报为通过。

## Task 2：14 方法 Protocol

### RED

新增 `test_model_port.py`，验证 ModelRouter 满足以下精确操作集合：

- metadata：get_model_info/get_context_window/get_max_output/get_cost_rates；
- routing：resolve_model/resolve_target/get_runtime_identity；
- capability：get_model_capability_contract；
- discovery：list_available_models；
- reasoning：get/set/reset_reasoning_effort；
- invocation：call/stream。

缺少任一操作的对象必须被 runtime Protocol 拒绝。

### GREEN

新增 `runtime/ports/model.py`，复用现有 DTO 和精确签名，不导入 ModelRouter，不包含实现逻辑。
更新 ports 导出。运行新测试、Ruff 和 compileall。

## Task 3：Engine 注入

### RED

记录型确定性 ModelPort 测试：

- 构造注入；
- `router` 与 `_router` 兼容 property 指向同一对象；
- falsey Port 不回退；
- 无效 Port 中文失败且不加载 catalog/创建 runtime 数据；
- 默认仍为 ModelRouter。

### GREEN

- 增加 keyword-only `model_port`；
- 仅 `model_port is None` 时加载 catalog 和构造 ModelRouter；
- 唯一字段 `_model_port`；
- `router`、`_router` 只读兼容；
- Engine 内部全部改用 `_model_port`。

静态检查 `self._router.` 必须无生产内部调用。

## Task 4：消费者传播

逐个 RED/GREEN，确保同一对象传播到：

1. ContextCompactor；
2. IntentClassifier/AdaptivePlanner；
3. DynamicAgentFactory；
4. GoalPursuitLoop；
5. analysis global router；
6. browser TaskRunner/LLMPlanner；
7. Subagent 与 inspector 通过 Engine.router 获取同一 Port。

将这些消费者构造参数/字段注解改为 ModelPort。Browser fallback 改为显式 `is None`。

不得通过保留 `ModelRouter` 注解加 type-ignore 交差。

## Task 5：真实运行验收

确定性 ModelPort 返回强类型 ModelResponse/StreamChunk，网络层之外走真实代码：

- `AgentEngine.run`：response、usage、会话保存完整；
- `run_streaming`：token、usage、receipt、事件顺序完整；
- Planner 非本地 fast path 调用同一 Port；
- Compactor 达到阈值后调用同一 Port并生成摘要；
- browser planner 不把 falsey Port 替换为 ModelRouter。

## Task 6：跨表面契约

聚焦验证：

- CLI/TUI/New UI/API resolve_model；
- runtime identity；
- capability contract 与上下文/输出上限；
- list_available_models；
- reasoning status/set/reset；
- 旧 `engine._router.call/stream` 方法打桩。

新测试应证明替代 Port 也可驱动这些表面，而不只证明默认 Router 可用。

## Task 7：聚焦回归

运行：

- `test_model_port.py`；
- `test_model_router.py`；
- 受影响 Planner/Compactor/Factory/Pursuit/browser 小模块；
- Engine run/run_streaming 指定节点；
- model surfaces、bridge、API、TUI 指定节点；
- Ruff 改动 Python 文件；
- compileall 改动模块。

主机缺可选依赖时使用仓库 `.venv`，不安装全局包。

## Task 8：架构产物

运行 import graph/ownership 40 个聚焦测试。真实扫描验收：

- 328 个模块；
- `naumi_agent.runtime.ports.model` owner=runtime；
- 0 ownership issues；
- SCC 数量不增加。

先提交最终源码/测试/状态为 H1；用 H1 SHA 各生成两次 baseline 与 ownership，逐字节比较、
digest 互锁、无绝对路径/时间戳；amend 为 H2，并证明 H1/H2 源码一致。

## Task 9：自审与集成

自审：

- 是否完整覆盖 14 个能力？
- 是否有消费者仍要求 ModelRouter？
- 显式注入是否完全跳过 catalog/default Router？
- falsey Port 是否贯穿 browser 等 fallback？
- 非流式、流式、能力、发现、reasoning 和 receipt 是否都真实闭环？
- 是否误改 transport/UI/其他 Port？

提交 `refactor(runtime): inject model capability port [ARC-01.3c]`，fetch 后 ff-only 合并 main，
在 main 复跑小范围验收，push、ls-remote 校验并清理 worktree/branch。

## 完成定义

- 完整 14 方法 ModelPort 可替换；
- Engine 和所有装配消费者使用同一 Port；
- 默认 ModelRouter 与替代 Port 都通过真实 run/run_streaming 和跨表面能力验收；
- 328 模块 artifact 与源码互锁；
- 聚焦验证通过并推送 main；
- ARC-01.3 继续标明 ToolExecutionPort、EventSink 未完成。
