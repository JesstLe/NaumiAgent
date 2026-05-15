"""规划器单元测试."""

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
