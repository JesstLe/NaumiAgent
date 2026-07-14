# 第二部分：核心引擎

## 1. Agent 主循环

核心是 Anthropic 描述的 **"LLM 在循环中使用工具，根据环境反馈行动"** 模式。不引入复杂的框架抽象，直接实现清晰的主循环。

### 1.1 主循环设计

```python
# src/naumi_agent/orchestrator/engine.py

class AgentEngine:
    """Agent 核心引擎 — 基于工具反馈的自主循环"""

    def __init__(self, config: AgentConfig):
        self.config = config
        self.model_router = ModelRouter(config.models)
        self.tool_registry = ToolRegistry()
        self.memory = MemorySystem(config.memory)
        self.guardrails = Guardrails(config.safety)
        self.tracer = Tracer(config.observability)
        self.budget = BudgetTracker(config.safety.max_budget_usd)

    async def run(self, task: str, session_id: str | None = None) -> AgentResult:
        """执行任务的主入口"""
        session = await self._load_or_create_session(session_id)

        # 1. 输入校验
        task = await self.guardrails.validate_input(task)

        # 2. 添加到记忆
        session.messages.append(UserMessage(content=task))

        # 3. 主循环
        for turn in range(self.config.max_turns):
            # 检查预算
            if self.budget.is_exceeded():
                return AgentResult(status="budget_exceeded", ...)

            # 3.1 压缩上下文（如果接近窗口上限）
            await self.memory.compact_if_needed(session)

            # 3.2 调用 LLM
            response = await self.model_router.call(
                messages=session.messages,
                tools=self.tool_registry.get_schemas(),
                system_prompt=self._build_system_prompt(session),
            )
            self.budget.track(response.usage)

            # 3.3 处理响应
            if response.has_tool_calls:
                # 执行工具调用
                results = await self._execute_tool_calls(
                    response.tool_calls, session
                )
                session.messages.append(AssistantMessage(response))
                session.messages.extend(results)
            else:
                # 最终响应 — 评估后返回
                validated = await self.guardrails.validate_output(
                    response.content
                )
                session.messages.append(AssistantMessage(response))
                await self.memory.save_session(session)
                return AgentResult(
                    status="completed",
                    response=validated,
                    usage=self.budget.get_summary(),
                    turns=turn + 1,
                )

        # 超过最大轮次
        return AgentResult(status="max_turns_reached", ...)

    async def _execute_tool_calls(
        self, tool_calls: list[ToolCall], session: Session
    ) -> list[ToolResult]:
        """并行执行独立的工具调用"""
        # 分析依赖关系：无依赖的调用并行执行
        independent = [tc for tc in tool_calls if not tc.depends_on_prior]
        dependent = [tc for tc in tool_calls if tc.depends_on_prior]

        results = []

        # 并行执行独立调用
        if independent:
            parallel_results = await asyncio.gather(*[
                self._execute_single_tool(tc, session)
                for tc in independent
            ])
            results.extend(parallel_results)

        # 顺序执行有依赖的调用
        for tc in dependent:
            result = await self._execute_single_tool(tc, session)
            results.append(result)

        return results

    async def _execute_single_tool(
        self, call: ToolCall, session: Session
    ) -> ToolResult:
        """执行单个工具调用，带追踪和错误处理"""
        tool = self.tool_registry.get(call.name)

        # 权限检查
        if not await self.guardrails.check_permission(call, session):
            return ToolResult(
                call_id=call.id,
                status="denied",
                error=f"Tool '{call.name}' not permitted in current mode",
            )

        with self.tracer.span("tool_call", name=call.name, args=call.args):
            try:
                result = await tool.execute(**call.args)
                return ToolResult(
                    call_id=call.id, status="success", content=result
                )
            except ToolExecutionError as e:
                return ToolResult(
                    call_id=call.id, status="error", error=str(e)
                )
            except Exception as e:
                # 意外错误 — 记录并返回友好消息
                self.tracer.record_exception(e)
                return ToolResult(
                    call_id=call.id,
                    status="error",
                    error=f"Unexpected error: {type(e).__name__}",
                )
```

### 1.2 主循环流程图

```
┌─────────────┐
│  接收任务    │
└──────┬──────┘
       │
       ▼
┌─────────────┐    超过
│ 检查预算/轮次│───上限 ──▶ 返回终止状态
└──────┬──────┘
       │ 未超
       ▼
┌─────────────┐
│ 压缩上下文  │──── 接近窗口上限时触发
└──────┬──────┘
       │
       ▼
┌─────────────┐
│ 调用 LLM   │
│ (含工具schema)│
└──────┬──────┘
       │
       ├──▶ 纯文本响应 ──▶ 输出审计 ──▶ 返回结果
       │
       ▼
  有工具调用？
       │
       ├──▶ 权限检查 ──▶ 并行/顺序执行 ──▶ 收集结果 ──▶ 回到顶部
       │
       └──▶ 权限拒绝 ──▶ 返回拒绝信息 ──▶ 回到顶部
```

## 2. 规划器（Planner）

规划器负责将复杂任务分解为可执行的子步骤。基于 Anthropic 的 **Orchestrator-Workers** 模式。

### 2.1 规划策略

```python
# src/naumi_agent/orchestrator/planner.py

PLANNER_PROMPT = """你是一个任务规划器。给定用户任务，将其分解为具体的执行步骤。

输出格式（JSON）：
{
    "understanding": "对用户意图的理解",
    "steps": [
        {
            "id": "step_1",
            "description": "步骤描述",
            "tool": "tool_name | null",
            "depends_on": [],
            "parallelizable": true/false,
            "complexity": "simple | medium | complex"
        }
    ],
    "estimated_turns": 5
}

原则：
- 每个步骤应该是原子性的 — 要么完全成功，要么完全失败
- 明确标注步骤间的依赖关系
- 标记可并行的步骤
- 复杂步骤（complexity=complex）应考虑拆分或派生子Agent
"""

class Planner:
    def __init__(self, model_router: ModelRouter):
        self.model_router = model_router

    async def plan(self, task: str, context: Session) -> Plan:
        """生成执行计划"""
        # 用 Haiku 做快速规划（节省成本）
        response = await self.model_router.call(
            messages=[
                SystemMessage(PLANNER_PROMPT),
                *context.recent_messages(n=5),
                UserMessage(f"请为以下任务制定执行计划：\n{task}"),
            ],
            model_tier="fast",  # 路由到 Haiku
            response_format="json",
        )

        plan = Plan.model_validate_json(response.content)

        # 验证计划可行性
        if plan.estimated_turns > context.remaining_budget():
            plan = await self._simplify_plan(plan, context)

        return plan

    async def replan(
        self, original_plan: Plan, failed_step: Step, error: str
    ) -> Plan:
        """步骤失败时重新规划"""
        response = await self.model_router.call(
            messages=[
                SystemMessage(PLANNER_PROMPT),
                UserMessage(f"""
                原计划：{original_plan.model_dump_json(indent=2)}
                失败步骤：{failed_step.id} — {failed_step.description}
                错误信息：{error}

                请调整计划，绕过失败步骤或提供替代方案。
                """),
            ],
            model_tier="fast",
            response_format="json",
        )
        return Plan.model_validate_json(response.content)
```

### 2.2 执行模式选择

根据任务特征自动选择 Anthropic 推荐的 5 种工作流模式之一：

```python
class ExecutionMode(Enum):
    SINGLE_TURN = "single_turn"       # 单次调用，无需工具
    PROMPT_CHAIN = "prompt_chain"      # 顺序链式调用
    ROUTING = "routing"                # 分类路由
    PARALLEL = "parallel"              # 并行子任务
    ORCHESTRATOR = "orchestrator"      # 编排器-工人模式

def select_execution_mode(plan: Plan) -> ExecutionMode:
    steps = plan.steps

    if len(steps) == 1 and steps[0].tool is None:
        return ExecutionMode.SINGLE_TURN
    elif len(steps) <= 3 and all(not s.parallelizable for s in steps):
        return ExecutionMode.PROMPT_CHAIN
    elif len(steps) == 1 and steps[0].tool == "router":
        return ExecutionMode.ROUTING
    elif any(s.parallelizable for s in steps):
        return ExecutionMode.PARALLEL
    else:
        return ExecutionMode.ORCHESTRATOR
```

## 3. 执行器（Executor）

### 3.1 执行流程

```python
# src/naumi_agent/orchestrator/executor.py

class Executor:
    def __init__(
        self,
        tool_registry: ToolRegistry,
        guardrails: Guardrails,
        tracer: Tracer,
    ):
        self.tools = tool_registry
        self.guardrails = guardrails
        self.tracer = tracer

    async def execute_plan(
        self, plan: Plan, session: Session
    ) -> list[StepResult]:
        """按计划执行所有步骤"""
        mode = select_execution_mode(plan)
        results = []

        match mode:
            case ExecutionMode.SINGLE_TURN:
                results = await self._execute_single(plan, session)
            case ExecutionMode.PROMPT_CHAIN:
                results = await self._execute_chain(plan, session)
            case ExecutionMode.PARALLEL:
                results = await self._execute_parallel(plan, session)
            case ExecutionMode.ORCHESTRATOR:
                results = await self._execute_orchestrated(plan, session)

        return results

    async def _execute_chain(
        self, plan: Plan, session: Session
    ) -> list[StepResult]:
        """链式执行 — 每步输出作为下步输入"""
        results = []
        context = ""

        for step in plan.steps:
            result = await self._execute_step(
                step, session, extra_context=context
            )
            results.append(result)

            if result.status == "error":
                break  # 链式执行遇错即停

            context = result.content
        return results

    async def _execute_parallel(
        self, plan: Plan, session: Session
    ) -> list[StepResult]:
        """并行执行 — 拓扑排序后并行运行无依赖步骤"""
        results = {}
        executed = set()

        # 拓扑排序
        sorted_steps = topological_sort(plan.steps)

        for level in sorted_steps:
            # 同一层级的步骤并行执行
            tasks = [
                self._execute_step(step, session)
                for step in level
                if all(dep in executed for dep in step.depends_on)
            ]
            level_results = await asyncio.gather(*tasks)

            for step, result in zip(level, level_results):
                results[step.id] = result
                executed.add(step.id)

        return [results[s.id] for s in plan.steps]

    async def _execute_orchestrated(
        self, plan: Plan, session: Session
    ) -> list[StepResult]:
        """编排执行 — 复杂步骤派生子Agent"""
        results = []

        for step in plan.steps:
            if step.complexity == "complex":
                # 派生子 Agent 处理
                result = await self._delegate_to_subagent(
                    step, session
                )
            else:
                result = await self._execute_step(step, session)

            results.append(result)
        return results
```

## 4. 评估器（Evaluator）

基于 Anthropic 的 **Evaluator-Optimizer** 模式，在关键步骤后验证结果。

```python
# src/naumi_agent/orchestrator/evaluator.py

EVALUATOR_PROMPT = """你是任务执行的质量评估器。

原始任务：{task}
执行结果：{result}

请评估：
1. 结果是否完整地完成了任务？
2. 结果是否准确？
3. 是否有明显的遗漏或错误？

输出 JSON：
{
    "score": 0-10,
    "is_satisfactory": true/false,
    "feedback": "具体反馈（如需改进）",
    "missing_aspects": ["遗漏的方面"]
}
"""

class Evaluator:
    def __init__(self, model_router: ModelRouter):
        self.model_router = model_router

    async def evaluate(
        self, task: str, result: str, original_plan: Plan | None = None
    ) -> Evaluation:
        """评估执行结果是否满足任务要求"""
        response = await self.model_router.call(
            messages=[
                SystemMessage(EVALUATOR_PROMPT.format(
                    task=task, result=result
                )),
            ],
            model_tier="fast",
            response_format="json",
        )
        return Evaluation.model_validate_json(response.content)

    async def should_continue(
        self, evaluation: Evaluation, turn: int, max_turns: int
    ) -> bool:
        """决定是否继续优化"""
        if evaluation.is_satisfactory:
            return False
        if evaluation.score >= 8:
            return False  # 足够好，不再优化
        if turn >= max_turns:
            return False
        return True
```

## 5. System Prompt 构建

System Prompt 由命名分段组合，不再在 `engine.py` 中维护单个巨型常量。稳定的行为规则位于 `orchestrator/system_prompt.py`；工作区、权限模式、工具与 Skill 摘要由 Engine 在运行时加入。当前日期、时间、任务、后台运行和 Harness 状态不写入持久化基础提示词，而是在每次模型调用前通过临时 Harness 快照更新。

```python
# src/naumi_agent/orchestrator/system_prompt.py

SYSTEM_PROMPT_MARKER = '<naumi_system_prompt version="sections-v2">'

DEFAULT_PROMPT_SECTIONS = (
    PromptSection("identity", IDENTITY_SECTION),
    PromptSection("capabilities", CAPABILITY_SECTION),
    PromptSection("knowledge_freshness", KNOWLEDGE_FRESHNESS_SECTION),
    # 任务、上下文、输出、工具发现、UI 和完成纪律等稳定分段
)

def build_system_prompt(context: PromptAssemblyInput | None = None) -> str:
    parts = [SYSTEM_PROMPT_MARKER]
    parts.extend(section.content.strip() for section in DEFAULT_PROMPT_SECTIONS)
    if runtime_section := _runtime_section(context):
        parts.append(runtime_section)
    return "\n\n".join(parts)
```

知识时效规则要求模型先区分稳定事实和易变化事实。涉及最新版本、价格、计划、法规、模型/API 能力、兼容性或新闻时，必须优先检查当前工作区源码、配置、锁文件、运行时元数据或权威一手来源；无法验证时明确标注“未验证、可能过时”。静态提示词、训练记忆和旧会话摘要不能作为当前状态的证据。

```python
# src/naumi_agent/orchestrator/engine.py

def _build_system_prompt(self) -> str:
    return build_system_prompt(PromptAssemblyInput(
        workspace_root=str(self.workspace_root),
        permission_mode=self._config.safety.permission_mode,
        tool_names=tuple(sorted(self._tool_registry.names)),
        skill_names=tuple(sorted(skill.name for skill in self.skill_loader.all())),
    ))
```

Engine 在每轮开始时刷新带合法 Naumi 标记的生成提示词，因此旧 `sections-v1` 会自动迁移到 v2。用户自定义 system prompt 不含起始标记，不会被覆盖。当前时间仍以 `HarnessContextAssembler` 生成的 `### 当前环境` 为准，避免持久化提示词自身随时间老化。

## 6. 会话管理

```python
# src/naumi_agent/orchestrator/session.py

@dataclass
class Session:
    id: str
    created_at: datetime
    messages: list[Message]
    metadata: dict[str, Any]
    user_preferences: dict[str, str]
    token_usage: TokenUsage

    def remaining_budget(self) -> int:
        """剩余可用轮次"""
        return self.max_turns - len([
            m for m in self.messages if isinstance(m, AssistantMessage)
        ])

    def recent_messages(self, n: int = 10) -> list[Message]:
        """获取最近 n 条消息"""
        return self.messages[-n:]

    def tool_history(self) -> list[ToolCall]:
        """获取所有工具调用历史"""
        return [
            tc for msg in self.messages
            if isinstance(msg, AssistantMessage)
            for tc in msg.tool_calls
        ]
```

## 7. 错误恢复

```python
class AgentEngine:
    async def _handle_error(
        self, error: Exception, session: Session, step: Step | None = None
    ) -> AgentResult:
        """分层错误处理"""

        if isinstance(error, BudgetExceededError):
            return AgentResult(
                status="budget_exceeded",
                response="任务执行超出预算限制，请增加预算或简化任务。",
                usage=self.budget.get_summary(),
            )

        if isinstance(error, ToolExecutionError):
            # 工具执行失败 — 尝试替代方案
            if step:
                alt_plan = await self.planner.replan(
                    session.current_plan, step, str(error)
                )
                session.current_plan = alt_plan
                return None  # 继续执行新计划
            return AgentResult(
                status="tool_error",
                response=f"工具执行失败：{error}",
            )

        if isinstance(error, ModelAPIError):
            # 模型调用失败 — 切换备选模型
            self.model_router.fallback()
            return None  # 用备选模型重试

        # 未知错误 — 记录并返回
        self.tracer.record_exception(error)
        return AgentResult(
            status="error",
            response=f"执行遇到意外错误：{type(error).__name__}",
        )
```
