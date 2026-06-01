"""规划器单元测试."""

from unittest.mock import AsyncMock

import pytest

from naumi_agent.orchestrator.planner import (
    AdaptivePlanner,
    Complexity,
    ExecutionMode,
    IntentClassifier,
)


class TestIntentClassifier:
    def test_parse_valid_json(self) -> None:
        classifier = IntentClassifier.__new__(IntentClassifier)
        result = classifier._parse_intent("""```json
        {
            "intent": "代码编写",
            "complexity": "medium",
            "requires_tools": true,
            "requires_planning": true,
            "requires_subagents": false,
            "estimated_steps": 3,
            "confidence": 0.9
        }
        ```""")
        assert result.intent == "代码编写"
        assert result.complexity == Complexity.MEDIUM
        assert result.requires_tools is True
        assert result.confidence == 0.9

    def test_parse_raw_json(self) -> None:
        classifier = IntentClassifier.__new__(IntentClassifier)
        result = classifier._parse_intent(
            '{"intent": "闲聊", "complexity": "simple", "requires_tools": false, '
            '"requires_planning": false, "requires_subagents": false, '
            '"estimated_steps": 1, "confidence": 0.95}'
        )
        assert result.intent == "闲聊"
        assert result.complexity == Complexity.SIMPLE

    def test_parse_invalid_json_fallback(self) -> None:
        classifier = IntentClassifier.__new__(IntentClassifier)
        result = classifier._parse_intent("not json at all")
        assert result.complexity == Complexity.SIMPLE
        assert result.confidence == 0.0


class TestAdaptivePlanner:
    def test_simple_plan(self) -> None:
        planner = AdaptivePlanner.__new__(AdaptivePlanner)
        intent_result = type(
            "Intent",
            (),
            {
                "intent": "闲聊",
                "complexity": Complexity.SIMPLE,
                "requires_tools": False,
                "requires_planning": False,
                "requires_subagents": False,
                "estimated_steps": 1,
                "confidence": 0.9,
            },
        )()
        plan = planner._simple_plan("你好", intent_result)
        assert len(plan.steps) == 1
        assert plan.mode == ExecutionMode.SINGLE_TURN

    @pytest.mark.asyncio
    async def test_local_fast_path_skips_classifier_for_greeting(self) -> None:
        router = AsyncMock()
        planner = AdaptivePlanner(router)
        planner._classifier.classify = AsyncMock()

        plan = await planner.plan("你好")

        assert plan.mode == ExecutionMode.SINGLE_TURN
        planner._classifier.classify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_local_fast_path_skips_classifier_for_simple_question(self) -> None:
        router = AsyncMock()
        planner = AdaptivePlanner(router)
        planner._classifier.classify = AsyncMock()

        plan = await planner.plan("这是正常的吗")

        assert plan.mode == ExecutionMode.SINGLE_TURN
        planner._classifier.classify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_local_fast_path_keeps_action_tasks_on_classifier(self) -> None:
        router = AsyncMock()
        planner = AdaptivePlanner(router)
        planner._classifier.classify = AsyncMock(
            return_value=type(
                "Intent",
                (),
                {
                    "intent": "代码修复",
                    "complexity": Complexity.SIMPLE,
                    "requires_tools": True,
                    "requires_planning": False,
                    "requires_subagents": False,
                    "estimated_steps": 1,
                    "confidence": 0.9,
                },
            )()
        )

        plan = await planner.plan("修复 CLI 渲染 bug")

        assert plan.mode == ExecutionMode.SINGLE_TURN
        planner._classifier.classify.assert_awaited_once()
