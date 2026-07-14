# 第六部分：多 Agent 协作

## 1. 设计哲学

基于 Anthropic 研究发现：**多 Agent 架构在并行子 Agent 协调下，比单 Agent 最高可提升 90% 性能。**

但遵循 Anthropic 的核心原则 — **只在简单方案不够用时才增加复杂度**：

```
优先级：
1. 单 Agent + 工具     → 能解决 80% 的问题
2. 单 Agent + 子任务   → 解决 15% 的复杂问题
3. 多 Agent 协作       → 解决最后 5% 的极端复杂问题
```

## 2. Agent 类型

### 2.1 Agent 基类

```python
# src/naumi_agent/agents/base.py

from dataclasses import dataclass, field
from enum import Enum

class AgentCapability(Enum):
    FILE_OPS = "file_operations"
    CODE_EXEC = "code_execution"
    WEB_BROWSE = "web_browsing"
    WEB_SEARCH = "web_search"
    SHELL_EXEC = "shell_execution"
    DATA_ANALYSIS = "data_analysis"

@dataclass
class AgentConfig:
    name: str
    description: str
    capabilities: list[AgentCapability]
    model_tier: str = "capable"  # "fast" | "capable" | "reasoning"
    system_prompt: str = ""
    max_turns: int = 20
    max_budget_usd: float = 1.0
    tools: list[str] = field(default_factory=list)  # 允许的工具列表
    permission_level: str = "moderate"

class BaseAgent:
    """所有 Agent 的基类"""

    def __init__(self, config: AgentConfig, engine: AgentEngine):
        self.config = config
        self.engine = engine
        self.tool_registry = self._build_tool_registry()

    def _build_tool_registry(self) -> ToolRegistry:
        """根据 Agent 能力构建专属工具集"""
        registry = ToolRegistry()

        tool_map = {
            AgentCapability.FILE_OPS: ["file_read", "file_write", "file_edit", "file_list", "file_search"],
            AgentCapability.CODE_EXEC: ["code_execute", "code_install"],
            AgentCapability.WEB_BROWSE: ["browser_goto", "browser_click", "browser_screenshot", "browser_observe"],
            AgentCapability.WEB_SEARCH: ["web_search", "web_fetch"],
            AgentCapability.SHELL_EXEC: ["bash_run"],
            AgentCapability.DATA_ANALYSIS: ["code_execute", "file_read", "file_write"],
        }

        for cap in self.config.capabilities:
            for tool_name in tool_map.get(cap, []):
                tool = self.engine.tool_registry.get(tool_name)
                if tool:
                    registry.register(tool)

        return registry

    async def execute(self, task: str, context: str = "") -> AgentResult:
        """执行子任务"""
        messages = []

        if context:
            messages.append(SystemMessage(f"## 前置上下文\n{context}"))

        messages.append(SystemMessage(self.config.system_prompt))
        messages.append(UserMessage(task))

        # 运行 Agent 循环
        return await self.engine.run_with_tools(
            messages=messages,
            tools=self.tool_registry.get_schemas(),
            max_turns=self.config.max_turns,
            max_budget=self.config.max_budget_usd,
        )
```

### 2.2 专用 Agent 定义

```python
# src/naumi_agent/agents/coder.py

CODER_CONFIG = AgentConfig(
    name="coder",
    description="编程 Agent — 编写、修改、调试代码",
    capabilities=[
        AgentCapability.FILE_OPS,
        AgentCapability.CODE_EXEC,
        AgentCapability.SHELL_EXEC,
    ],
    model_tier="capable",
    system_prompt="""你是 NaumiAgent 的编程专家。

## 你的职责
- 编写、修改、重构代码
- 调试和修复 bug
- 编写单元测试
- 代码审查

## 工作原则
1. 先阅读相关代码，理解上下文
2. 修改前确认理解正确
3. 小步修改，每步验证
4. 写测试验证修改
5. 保持代码风格一致

## 输出规范
- 修改文件时使用 file_edit，不要重写整个文件
- 新建文件时使用 file_write
- 运行测试验证修改效果
""",
    max_turns=15,
    max_budget_usd=0.5,
)

# src/naumi_agent/agents/researcher.py

RESEARCHER_CONFIG = AgentConfig(
    name="researcher",
    description="研究 Agent — 搜索、阅读、分析信息",
    capabilities=[
        AgentCapability.WEB_SEARCH,
        AgentCapability.WEB_BROWSE,
        AgentCapability.FILE_OPS,  # 保存研究结果
    ],
    model_tier="capable",
    system_prompt="""你是 NaumiAgent 的研究专家。

## 你的职责
- 搜索网络信息
- 浏览和分析网页内容
- 提取和整理关键信息
- 撰写研究报告

## 工作原则
1. 先明确需要研究的问题
2. 从多个来源搜索
3. 交叉验证关键信息
4. 区分事实和观点
5. 引用信息来源
""",
    max_turns=20,
    max_budget_usd=0.5,
)

# src/naumi_agent/agents/browser.py

BROWSER_CONFIG = AgentConfig(
    name="browser",
    description="浏览器 Agent — 自动化网页操作",
    capabilities=[
        AgentCapability.WEB_BROWSE,
        AgentCapability.WEB_SEARCH,
    ],
    model_tier="capable",
    system_prompt="""你是 NaumiAgent 的浏览器操作专家。

## 你的职责
- 导航到指定网页
- 与页面元素交互（点击、输入、滚动）
- 提取页面内容
- 截图和信息收集

## 工作原则
1. 操作前先截图了解页面状态
2. 使用 CSS 选择器精确定位元素
3. 每步操作后验证结果
4. 处理弹窗和对话框
5. 超时或加载失败时重试
""",
    max_turns=25,
    max_budget_usd=0.3,
)
```

## 3. 子 Agent 调度

### 3.1 编排器（Orchestrator）

```python
# src/naumi_agent/orchestrator/subagent_manager.py

class SubAgentManager:
    """管理和调度子 Agent"""

    def __init__(self, model_router: ModelRouter):
        self.model_router = model_router
        self._agents: dict[str, BaseAgent] = {}
        self._register_builtins()

    def _register_builtins(self) -> None:
        """注册内置 Agent"""
        from .coder import CODER_CONFIG
        from .researcher import RESEARCHER_CONFIG
        from .browser import BROWSER_CONFIG

        # 实际的 Agent 实例会在运行时创建（需要 engine 注入）

    def get_agent(self, name: str) -> BaseAgent | None:
        return self._agents.get(name)

    def select_agent(self, step: Step) -> str | None:
        """根据步骤特征选择最合适的 Agent"""
        AGENT_SELECTION = {
            "code": "coder",
            "write_code": "coder",
            "debug": "coder",
            "test": "coder",
            "refactor": "coder",
            "research": "researcher",
            "search": "researcher",
            "analyze": "researcher",
            "browse": "browser",
            "navigate": "browser",
            "fill_form": "browser",
            "scrape": "browser",
        }

        description_lower = step.description.lower()
        for keyword, agent_name in AGENT_SELECTION.items():
            if keyword in description_lower:
                return agent_name
        return None

    async def delegate(
        self,
        step: Step,
        session: Session,
        context: str = "",
    ) -> StepResult:
        """将步骤委派给子 Agent"""
        agent_name = self.select_agent(step)
        agent = self.get_agent(agent_name)

        if not agent:
            # 没有合适的子 Agent — 用主 Agent 直接处理
            return StepResult(
                step_id=step.id,
                status="skipped",
                content="No suitable sub-agent found, will handle directly",
            )

        # 构建子任务的上下文
        sub_context = self._build_sub_context(session, step, context)

        # 执行子任务
        result = await agent.execute(
            task=step.description,
            context=sub_context,
        )

        return StepResult(
            step_id=step.id,
            status="completed" if result.status == "completed" else "error",
            content=result.response,
            tokens_used=result.usage.total_tokens,
        )

    def _build_sub_context(
        self, session: Session, step: Step, extra: str
    ) -> str:
        """构建子 Agent 的上下文"""
        parts = []

        if extra:
            parts.append(f"## 父任务上下文\n{extra}")

        # 相关的历史步骤结果
        for dep_id in step.depends_on:
            dep_result = session.get_step_result(dep_id)
            if dep_result:
                parts.append(f"## 前置步骤 {dep_id} 的结果\n{dep_result.content[:2000]}")

        return "\n\n".join(parts)
```

### 3.2 并行子任务执行

当前实现由 `SubAgentManager.execute_parallel()` 统一调度，不再对整个列表直接
`asyncio.gather()`。每个批次只创建有限 worker，多个同时批次共享
`safety.max_parallel_agents`（默认 4，范围 1-32）的 Semaphore。任务按输入顺序领取，
结果按输入顺序返回；普通异常只影响对应任务，父级取消会停止活跃 worker，尚未领取的任务
不会启动。`execute_dag()` 的每一层也复用同一调度器。

运行 `/runtime subagent` 可以查看当前“活跃/上限”和排队数。

## 4. Agent 间通信

### 4.1 上下文传递

子 Agent 之间不直接通信，通过父 Agent 的会话状态间接传递：

```
父 Agent (Orchestrator)
  │
  ├─── 子 Agent A (Researcher)
  │     输入：研究任务 + 父上下文
  │     输出：研究结果摘要
  │
  │    （结果通过父 Agent 的会话状态传递）
  │
  └─── 子 Agent B (Coder)
        输入：编码任务 + Agent A 的结果摘要 + 父上下文
        输出：代码修改结果
```

### 4.2 上下文传递协议

```python
@dataclass
class SubAgentContext:
    """子 Agent 的输入上下文"""
    parent_task: str              # 父任务描述
    step_description: str         # 当前步骤描述
    dependency_results: list[str] # 依赖步骤的结果摘要
    constraints: list[str]        # 约束条件
    max_output_tokens: int        # 最大输出长度限制

    def to_prompt(self) -> str:
        parts = [f"## 父任务：{self.parent_task}"]
        parts.append(f"## 你的任务：{self.step_description}")

        if self.dependency_results:
            parts.append("## 前置步骤结果：")
            for i, r in enumerate(self.dependency_results, 1):
                parts.append(f"### 步骤 {i}\n{r}")

        if self.constraints:
            parts.append(f"## 约束：\n" + "\n".join(f"- {c}" for c in self.constraints))

        return "\n\n".join(parts)
```

## 5. 协作模式

### 5.1 管道模式（Pipeline）

```
[Researcher] ──搜索结果──▶ [Coder] ──代码──▶ [Tester]
```

```python
async def execute_pipeline(
    self, steps: list[Step], session: Session
) -> list[StepResult]:
    """顺序管道执行"""
    results = []
    accumulated_context = ""

    for step in steps:
        result = await self.manager.delegate(
            step, session, context=accumulated_context
        )
        results.append(result)
        accumulated_context += f"\n{step.description} → {result.content}"

    return results
```

### 5.2 并行分治模式（Map-Reduce）

```
[Researcher A] ──┐
[Researcher B] ──┼──▶ [Orchestrator] ──综合结果──▶ 输出
[Researcher C] ──┘
```

```python
async def execute_map_reduce(
    self, task: str, subtasks: list[str], session: Session
) -> str:
    """并行分治 — 多个子任务并行，然后综合"""
    # Map: 使用有界集群调度，不直接创建无界协程
    results = await self.manager.execute_parallel([
        SubTask(id=f"map_{i}", description=subtask)
        for i, subtask in enumerate(subtasks)
    ])

    # Reduce: 综合结果
    combined = "\n\n---\n\n".join(
        f"### 子任务 {i+1}\n{r.response}"
        for i, r in enumerate(results)
    )

    synthesis = await self.model_router.call(
        messages=[
            SystemMessage("将以下子任务结果综合为统一的最终输出。"),
            UserMessage(f"原始任务：{task}\n\n子任务结果：\n{combined}"),
        ],
        model_tier="capable",
    )

    return synthesis.content
```

### 5.3 评审循环模式（Review Loop）

```
[Coder] ──代码──▶ [Reviewer] ──反馈──▶ [Coder] ──修改──▶ ... ──通过──▶ 输出
```

```python
async def execute_review_loop(
    self, task: str, session: Session, max_rounds: int = 3
) -> str:
    """代码编写 + 审查循环"""
    for round_num in range(max_rounds):
        # 编写/修改代码
        coder_result = await self.manager.delegate(
            Step(id=f"code_round_{round_num}", description=task),
            session,
        )

        # 审查代码
        review_prompt = f"""
        审查以下代码变更：
        {coder_result.content}

        原始需求：{task}

        请检查：
        1. 功能正确性
        2. 代码质量
        3. 边界情况处理
        4. 安全性

        如果一切良好，回复 "APPROVED"。
        如果有问题，给出具体的修改建议。
        """

        review_result = await self.model_router.call(
            messages=[UserMessage(review_prompt)],
            model_tier="capable",
        )

        if "APPROVED" in review_result.content:
            return coder_result.content

        # 将反馈加入任务上下文，下一轮修改
        task = f"{task}\n\n审查反馈（第 {round_num + 1} 轮）：\n{review_result.content}"

    return coder_result.content
```

## 6. 资源隔离

每个子 Agent 有独立的资源限制：

```python
@dataclass
class SubAgentBudget:
    max_tokens: int = 50000
    max_turns: int = 15
    max_time_seconds: int = 120
    max_usd: float = 0.5

class BudgetEnforcer:
    """强制子 Agent 的预算限制"""

    async def execute_with_budget(
        self, agent: BaseAgent, task: str, budget: SubAgentBudget
    ) -> AgentResult:
        try:
            result = await asyncio.wait_for(
                agent.execute(task),
                timeout=budget.max_time_seconds,
            )
            return result
        except asyncio.TimeoutError:
            return AgentResult(
                status="timeout",
                response=f"子 Agent 超时（{budget.max_time_seconds}秒）",
            )
```
