# 第五部分：规划与推理

## 1. 规划系统设计

通用 Agent 需要处理开放性任务，无法预定义所有执行路径。规划器是核心：理解用户意图 → 分解为步骤 → 执行 → 根据反馈调整。

### 1.1 规划层次

```
┌─────────────────────────────────────────────────┐
│                 任务理解层                        │
│  输入：用户原始描述                               │
│  输出：结构化任务定义 + 意图分类                   │
├─────────────────────────────────────────────────┤
│                 战略规划层                        │
│  输入：结构化任务                                 │
│  输出：高层执行计划（步骤列表 + 依赖关系）         │
├─────────────────────────────────────────────────┤
│                 战术执行层                        │
│  输入：单个步骤                                   │
│  输出：具体的工具调用序列                          │
├─────────────────────────────────────────────────┤
│                 反馈调整层                        │
│  输入：执行结果 + 原始计划                        │
│  输出：调整后的计划或继续执行                     │
└─────────────────────────────────────────────────┘
```

## 2. 任务理解

### 2.1 意图分类

```python
# src/naumi_agent/orchestrator/planner.py

INTENT_CLASSIFICATION_PROMPT = """分析用户输入，判断任务类型和复杂度。

用户输入：{user_input}

输出 JSON：
{
    "intent": "信息查询 | 文件操作 | 代码编写 | 网页浏览 | 数据分析 | 系统操作 | 多步骤任务 | 闲聊",
    "complexity": "simple | medium | complex",
    "requires_tools": true/false,
    "requires_planning": true/false,
    "requires_subagents": true/false,
    "estimated_steps": 1-20,
    "confidence": 0.0-1.0
}
"""

class IntentClassifier:
    def __init__(self, model_router: ModelRouter):
        self.model_router = model_router

    async def classify(self, user_input: str) -> Intent:
        response = await self.model_router.call(
            messages=[
                SystemMessage(INTENT_CLASSIFICATION_PROMPT.format(user_input=user_input)),
            ],
            model_tier="fast",  # 分类用 Haiku
            response_format="json",
            max_tokens=200,
        )
        return Intent.model_validate_json(response.content)
```

### 2.2 任务结构化

```python
TASK_STRUCTURE_PROMPT = """将用户任务转化为结构化定义。

用户输入：{user_input}

输出 JSON：
{
    "task_type": "primary type",
    "description": "对任务目标的精确描述",
    "success_criteria": ["完成条件 1", "完成条件 2"],
    "constraints": ["约束 1", "约束 2"],
    "related_files": ["可能涉及的文件路径（如果有）"],
    "context_needed": ["需要的前置信息"]
}

示例：
用户输入："帮我重构 main.py 中的数据处理逻辑，把函数拆小并加上单元测试"

输出：
{
    "task_type": "代码重构 + 测试编写",
    "description": "重构 main.py 中的数据处理函数，拆分为小函数，并编写单元测试",
    "success_criteria": [
        "数据处理逻辑被拆分为多个单一职责的函数",
        "每个函数都有对应的单元测试",
        "所有测试通过"
    ],
    "constraints": ["不改变外部接口行为", "保持向后兼容"],
    "related_files": ["main.py", "test_main.py"],
    "context_needed": ["当前 main.py 的内容", "项目的测试框架"]
}
"""
```

## 3. 战略规划

### 3.1 自适应规划

根据任务复杂度，选择不同粒度的规划策略：

```python
class AdaptivePlanner:
    def __init__(self, model_router: ModelRouter):
        self.model_router = model_router

    async def plan(self, task: TaskDefinition) -> Plan:
        intent = await self.classifier.classify(task.description)

        match intent.complexity:
            case "simple":
                return self._simple_plan(task, intent)
            case "medium":
                return await self._medium_plan(task, intent)
            case "complex":
                return await self._complex_plan(task, intent)

    def _simple_plan(self, task: TaskDefinition, intent: Intent) -> Plan:
        """简单任务 — 不需要显式规划，直接执行"""
        return Plan(
            steps=[Step(
                id="step_1",
                description=task.description,
                tool=None,  # 让 LLM 自己决定
                depends_on=[],
                parallelizable=False,
                complexity="simple",
            )],
            mode=ExecutionMode.SINGLE_TURN,
        )

    async def _medium_plan(self, task: TaskDefinition, intent: Intent) -> Plan:
        """中等任务 — 生成步骤列表"""
        response = await self.model_router.call(
            messages=[
                SystemMessage(PLANNER_PROMPT),
                UserMessage(f"任务：{task.description}\n\n成功标准：{task.success_criteria}"),
            ],
            model_tier="fast",
            response_format="json",
        )
        plan = Plan.model_validate_json(response.content)
        plan.mode = ExecutionMode.PROMPT_CHAIN
        return plan

    async def _complex_plan(self, task: TaskDefinition, intent: Intent) -> Plan:
        """复杂任务 — 详细规划 + 子任务识别"""
        # 第一步：生成高层计划
        high_level = await self.model_router.call(
            messages=[
                SystemMessage(COMPLEX_PLANNER_PROMPT),
                UserMessage(f"任务：{task.description}\n\n成功标准：{task.success_criteria}\n\n约束：{task.constraints}"),
            ],
            model_tier="capable",  # 复杂规划用 Sonnet
            response_format="json",
        )
        plan = Plan.model_validate_json(high_level.content)

        # 第二步：识别需要子 Agent 的复杂步骤
        for step in plan.steps:
            if step.complexity == "complex":
                step.subtask_agent = self._select_subagent(step)

        plan.mode = ExecutionMode.ORCHESTRATOR
        return plan
```

### 3.2 复杂规划提示词

```python
COMPLEX_PLANNER_PROMPT = """你是一个高级任务规划器。为复杂任务制定详细执行计划。

原则：
1. 每个步骤必须是原子性的 — 可以独立验证成功/失败
2. 明确标注步骤间的数据依赖
3. 识别可并行的步骤组
4. 标记高风险步骤（需要用户确认）
5. 复杂步骤标记为 "complex"，将被派发给专门的子Agent

输出 JSON：
{
    "understanding": "对任务的深度理解",
    "approach": "总体策略说明",
    "steps": [
        {
            "id": "step_1",
            "description": "详细描述",
            "tool": "建议使用的工具（或 null）",
            "depends_on": ["依赖的步骤 id"],
            "parallelizable": true/false,
            "complexity": "simple | medium | complex",
            "risk_level": "low | medium | high",
            "estimated_tokens": 1000,
            "success_check": "如何验证这步成功了"
        }
    ],
    "estimated_total_tokens": 10000,
    "estimated_turns": 8,
    "potential_issues": ["可能遇到的问题 1", "问题 2"]
}
"""
```

## 4. 推理增强

### 4.1 Chain-of-Thought（思维链）

在关键决策点强制 LLM 展示推理过程：

```python
COT_INSTRUCTION = """
在执行关键操作前，用 <thinking> 标签展示你的推理过程：

<thinking>
1. 当前目标：...
2. 可选方案：...
3. 选择理由：...
4. 预期结果：...
5. 回退方案：...
</thinking>
"""
```

### 4.2 自我反思（Self-Reflection）

每完成一个关键步骤后，评估结果质量：

```python
SELF_REFLECTION_PROMPT = """评估你刚才的执行结果。

原始目标：{step_description}
执行结果：{result}

请回答：
1. 结果是否完全达成了目标？（是/否/部分）
2. 如果不是，差距在哪里？
3. 需要重试还是可以继续？
4. 如果需要重试，应该怎么调整？

输出 JSON：
{
    "goal_met": true/false,
    "gap": "差距描述（如果有的话）",
    "action": "continue | retry | replan | ask_user",
    "adjustment": "调整建议（如果需要重试）"
}
"""
```

### 4.3 任务进度追踪

```python
@dataclass
class TaskProgress:
    plan: Plan
    completed_steps: list[str]
    current_step: str | None
    failed_steps: list[StepFailure]
    total_tokens_used: int

    def progress_percentage(self) -> float:
        total = len(self.plan.steps)
        completed = len(self.completed_steps)
        return (completed / total) * 100 if total > 0 else 0

    def next_steps(self) -> list[Step]:
        """获取下一个可执行的步骤"""
        return [
            s for s in self.plan.steps
            if s.id not in self.completed_steps
            and s.id not in [f.step_id for f in self.failed_steps]
            and all(dep in self.completed_steps for dep in s.depends_on)
        ]

    def summary(self) -> str:
        return f"进度：{len(self.completed_steps)}/{len(self.plan.steps)} 步完成 ({self.progress_percentage():.0f}%)"
```

## 5. ReAct 循环（Reasoning + Acting）

通用 Agent 的核心循环 — 结合推理和行动：

```
用户任务 → [推理：理解任务] → [行动：调用工具] → [观察：获取结果]
                                    ↑                        │
                                    └── [推理：分析结果] ←────┘
                                              │
                                    [推理：任务是否完成？]
                                         │           │
                                        是           否
                                         │           │
                                    返回结果    继续循环
```

```python
class ReActEngine:
    """ReAct 循环 — 推理 + 行动交替"""

    async def run(self, task: str, session: Session) -> AgentResult:
        session.messages.append(UserMessage(content=task))

        for turn in range(self.config.max_turns):
            # === Reasoning Phase ===
            thought = await self._think(session)

            # 检查是否可以直接回答
            if thought.is_final_answer:
                return AgentResult(status="completed", response=thought.answer)

            # === Acting Phase ===
            if thought.tool_call:
                result = await self._act(thought.tool_call, session)

                # === Observation Phase ===
                session.messages.append(ToolResultMessage(result))

                # Self-reflection on tool result
                reflection = await self._reflect(session, thought, result)
                if reflection.needs_replan:
                    await self._replan(session, reflection)
            else:
                # LLM 没有调用工具但也没有给出最终答案 — 异常情况
                session.messages.append(AssistantMessage(thought.response))
                continue

        return AgentResult(status="max_turns_reached")

    async def _think(self, session: Session) -> Thought:
        """推理阶段 — 让 LLM 思考下一步"""
        response = await self.model_router.call(
            messages=session.messages,
            tools=self.tool_registry.get_schemas(),
            system_prompt=self._build_system_prompt(session),
        )
        return Thought.from_response(response)

    async def _act(self, tool_call: ToolCall, session: Session) -> ToolResult:
        """行动阶段 — 执行工具调用"""
        tool = self.tool_registry.get(tool_call.name)
        return await tool.execute(**tool_call.args)

    async def _reflect(
        self, session: Session, thought: Thought, result: ToolResult
    ) -> Reflection:
        """反思阶段 — 评估结果，决定下一步"""
        response = await self.model_router.call(
            messages=[
                SystemMessage(SELF_REFLECTION_PROMPT.format(
                    step_description=thought.reasoning,
                    result=result.content[:2000],
                )),
            ],
            model_tier="fast",
            response_format="json",
        )
        return Reflection.model_validate_json(response.content)
```

## 6. 规划缓存与复用

```python
class PlanCache:
    """缓存常见任务的规划模板"""

    def __init__(self, long_term_memory: LongTermMemory):
        self.memory = long_term_memory

    async def find_similar_plan(self, task: str) -> Plan | None:
        """查找相似任务的历史规划"""
        entries = await self.memory.recall(
            query=task,
            category="plan_template",
            top_k=1,
        )

        if entries and entries[0].metadata.get("similarity", 0) > 0.85:
            return Plan.model_validate_json(entries[0].content)
        return None

    async def save_plan_template(self, task: str, plan: Plan) -> None:
        """保存成功的规划作为模板"""
        await self.memory.store(MemoryEntry(
            id=f"plan_{uuid4().hex[:8]}",
            content=plan.model_dump_json(),
            category="plan_template",
            embedding=[],
            metadata={"task": task, "steps_count": len(plan.steps)},
            created_at=datetime.now().isoformat(),
        ))
```
