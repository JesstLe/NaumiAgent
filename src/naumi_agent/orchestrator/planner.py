"""规划器 — 意图分类与自适应任务规划."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from naumi_agent.model.router import ModelRouter, ModelTier, TokenUsage

logger = logging.getLogger(__name__)

UsageCallback = Callable[[TokenUsage, str], None]


class ExecutionMode(StrEnum):
    SINGLE_TURN = "single_turn"
    PROMPT_CHAIN = "prompt_chain"
    ORCHESTRATOR = "orchestrator"


class Complexity(StrEnum):
    SIMPLE = "simple"
    MEDIUM = "medium"
    COMPLEX = "complex"


@dataclass(frozen=True)
class Intent:
    intent: str
    complexity: Complexity
    requires_tools: bool
    requires_planning: bool
    requires_subagents: bool
    estimated_steps: int
    confidence: float


@dataclass(frozen=True)
class Step:
    id: str
    description: str
    tool: str | None
    depends_on: list[str]
    parallelizable: bool
    complexity: Complexity
    risk_level: str = "low"
    success_check: str = ""


@dataclass
class Plan:
    understanding: str
    approach: str
    steps: list[Step]
    mode: ExecutionMode
    potential_issues: list[str] = field(default_factory=list)


INTENT_CLASSIFICATION_PROMPT = """\
分析用户输入，判断任务类型和复杂度。

用户输入：{user_input}

输出 JSON：
{{
    "intent": "信息查询 | 文件操作 | 代码编写 | 网页浏览 | 数据分析 | 系统操作 | 多步骤任务 | 闲聊",
    "complexity": "simple | medium | complex",
    "requires_tools": true/false,
    "requires_planning": true/false,
    "requires_subagents": true/false,
    "estimated_steps": 1-20,
    "confidence": 0.0-1.0
}}
"""

PLANNER_PROMPT = """\
你是一个任务规划器。为以下任务制定执行计划。

任务：{task}
成功标准：{success_criteria}

输出 JSON：
{{
    "understanding": "对任务的理解",
    "approach": "执行策略",
    "steps": [
        {{
            "id": "step_1",
            "description": "详细描述",
            "tool": "建议工具或 null",
            "depends_on": [],
            "parallelizable": true/false,
            "complexity": "simple | medium | complex",
            "risk_level": "low | medium | high",
            "success_check": "验证方式"
        }}
    ],
    "potential_issues": ["可能的问题"]
}}
"""

_SIMPLE_CHAT_PATTERNS = (
    re.compile(r"^\s*(hi|hello|hey|yo|oi|你好|您好|嗨|哈喽|在吗)[!！。.\s]*$", re.I),
    re.compile(r"^\s*(谢谢|多谢|thanks|thank you|ok|好的|好|嗯|行)[!！。.\s]*$", re.I),
)


class IntentClassifier:
    def __init__(
        self,
        router: ModelRouter,
        usage_callback: UsageCallback | None = None,
    ) -> None:
        self._router = router
        self._usage_callback = usage_callback

    async def classify(self, user_input: str) -> Intent:
        prompt = INTENT_CLASSIFICATION_PROMPT.format(user_input=user_input[:2000])
        try:
            response = await self._router.call(
                messages=[{"role": "user", "content": prompt}],
                tier=ModelTier.FAST,
                max_tokens=200,
            )
            self._record_usage(response.usage, response.model)
            return self._parse_intent(response.content)
        except Exception as e:
            logger.warning("Intent classification failed, defaulting to simple: %s", e)
            return Intent(
                intent="unknown",
                complexity=Complexity.SIMPLE,
                requires_tools=False,
                requires_planning=False,
                requires_subagents=False,
                estimated_steps=1,
                confidence=0.0,
            )

    def _record_usage(self, usage: TokenUsage, model: str) -> None:
        if self._usage_callback is not None:
            self._usage_callback(usage, model)

    def _parse_intent(self, content: str) -> Intent:
        try:
            # 尝试从 markdown code block 中提取 JSON
            text = content.strip()
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]

            data = json.loads(text)
            return Intent(
                intent=data.get("intent", "unknown"),
                complexity=Complexity(data.get("complexity", "simple")),
                requires_tools=data.get("requires_tools", False),
                requires_planning=data.get("requires_planning", False),
                requires_subagents=data.get("requires_subagents", False),
                estimated_steps=data.get("estimated_steps", 1),
                confidence=data.get("confidence", 0.5),
            )
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("Failed to parse intent JSON: %s", e)
            return Intent(
                intent="unknown",
                complexity=Complexity.SIMPLE,
                requires_tools=False,
                requires_planning=False,
                requires_subagents=False,
                estimated_steps=1,
                confidence=0.0,
            )


class AdaptivePlanner:
    """根据任务复杂度选择不同粒度的规划策略."""

    def __init__(
        self,
        router: ModelRouter,
        usage_callback: UsageCallback | None = None,
    ) -> None:
        self._router = router
        self._usage_callback = usage_callback
        self._classifier = IntentClassifier(router, usage_callback)

    async def plan(self, task: str, success_criteria: list[str] | None = None) -> Plan:
        local_intent = self._local_fast_path_intent(task)
        if local_intent is not None:
            logger.info("Task intent resolved locally: %s", local_intent.intent)
            return self._simple_plan(task, local_intent)

        intent = await self._classifier.classify(task)
        logger.info("Task intent: %s, complexity: %s", intent.intent, intent.complexity)

        match intent.complexity:
            case Complexity.SIMPLE:
                return self._simple_plan(task, intent)
            case Complexity.MEDIUM:
                return await self._medium_plan(task, success_criteria, intent)
            case Complexity.COMPLEX:
                return await self._complex_plan(task, success_criteria, intent)

    def _local_fast_path_intent(self, task: str) -> Intent | None:
        """Resolve clearly simple turns without paying an extra LLM round-trip."""
        text = task.strip()
        if not text:
            return Intent(
                intent="闲聊",
                complexity=Complexity.SIMPLE,
                requires_tools=False,
                requires_planning=False,
                requires_subagents=False,
                estimated_steps=1,
                confidence=1.0,
            )

        if any(pattern.match(text) for pattern in _SIMPLE_CHAT_PATTERNS):
            return Intent(
                intent="闲聊",
                complexity=Complexity.SIMPLE,
                requires_tools=False,
                requires_planning=False,
                requires_subagents=False,
                estimated_steps=1,
                confidence=0.98,
            )

        return None

    def _simple_plan(self, task: str, intent: Intent) -> Plan:
        return Plan(
            understanding=task,
            approach="直接执行",
            steps=[
                Step(
                    id="step_1",
                    description=task,
                    tool=None,
                    depends_on=[],
                    parallelizable=False,
                    complexity=Complexity.SIMPLE,
                )
            ],
            mode=ExecutionMode.SINGLE_TURN,
        )

    async def _medium_plan(self, task: str, criteria: list[str] | None, intent: Intent) -> Plan:
        prompt = PLANNER_PROMPT.format(
            task=task,
            success_criteria=criteria or ["任务完成"],
        )
        try:
            response = await self._router.call(
                messages=[{"role": "user", "content": prompt}],
                tier=ModelTier.FAST,
                max_tokens=1000,
            )
            self._record_usage(response.usage, response.model)
            plan = self._parse_plan(response.content)
            plan.mode = ExecutionMode.PROMPT_CHAIN
            return plan
        except Exception:
            return self._simple_plan(task, intent)

    async def _complex_plan(self, task: str, criteria: list[str] | None, intent: Intent) -> Plan:
        prompt = PLANNER_PROMPT.format(
            task=task,
            success_criteria=criteria or ["任务完成"],
        )
        try:
            response = await self._router.call(
                messages=[{"role": "user", "content": prompt}],
                tier=ModelTier.CAPABLE,
                max_tokens=2000,
            )
            self._record_usage(response.usage, response.model)
            plan = self._parse_plan(response.content)
            plan.mode = ExecutionMode.ORCHESTRATOR
            return plan
        except Exception:
            return self._simple_plan(task, intent)

    def _record_usage(self, usage: TokenUsage, model: str) -> None:
        if self._usage_callback is not None:
            self._usage_callback(usage, model)

    def _parse_plan(self, content: str) -> Plan:
        text = content.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]

        data = json.loads(text)
        steps = [
            Step(
                id=s.get("id", f"step_{i + 1}"),
                description=s.get("description", ""),
                tool=s.get("tool"),
                depends_on=s.get("depends_on", []),
                parallelizable=s.get("parallelizable", False),
                complexity=Complexity(s.get("complexity", "simple")),
                risk_level=s.get("risk_level", "low"),
                success_check=s.get("success_check", ""),
            )
            for i, s in enumerate(data.get("steps", []))
        ]

        return Plan(
            understanding=data.get("understanding", ""),
            approach=data.get("approach", ""),
            steps=steps,
            mode=ExecutionMode.SINGLE_TURN,
            potential_issues=data.get("potential_issues", []),
        )
