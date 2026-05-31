"""Tests for browser subagent LLMPlanner."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from naumi_agent.model.router import ModelResponse
from naumi_agent.tools.browser.subagent.planner import (
    LLMPlanner,
    _extract_json_object,
    _try_parse_json,
)

# ---------------------------------------------------------------------------
# JSON extraction helpers
# ---------------------------------------------------------------------------


class TestTryParseJson:
    def test_valid_dict(self):
        assert _try_parse_json('{"a": 1}') == {"a": 1}

    def test_valid_array_returns_none(self):
        assert _try_parse_json("[1, 2]") is None

    def test_invalid_json(self):
        assert _try_parse_json("not json") is None

    def test_empty_string(self):
        assert _try_parse_json("") is None

    def test_number_returns_none(self):
        assert _try_parse_json("42") is None


class TestExtractJsonObject:
    def test_plain_json(self):
        assert _extract_json_object('{"x": 3}') == {"x": 3}

    def test_json_wrapped_in_whitespace(self):
        assert _extract_json_object('  \n{"x": 3}\n  ') == {"x": 3}

    def test_json_inside_text(self):
        result = _extract_json_object(
            'Here is the response:\n{"status": "ok"}\nDone.'
        )
        assert result == {"status": "ok"}

    def test_no_json_returns_none(self):
        assert _extract_json_object("plain text only") is None

    def test_empty_string(self):
        assert _extract_json_object("") is None

    def test_nested_braces(self):
        text = '{"outer": {"inner": 1}}'
        assert _extract_json_object(text) == {"outer": {"inner": 1}}

    def test_malformed_json_returns_none(self):
        assert _extract_json_object("{broken json") is None


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------


def _make_planner() -> LLMPlanner:
    router = AsyncMock()
    return LLMPlanner(router, tier="capable")


class TestDecisionPrompts:
    def test_system_prompt_contains_action_types(self):
        p = _make_planner()
        prompt = p.build_decision_system_prompt()
        for action in [
            "goto", "click", "type", "hover", "keypress",
            "scroll", "finish", "fail", "ask_main_agent",
        ]:
            assert action in prompt

    def test_system_prompt_no_captcha_by_default(self):
        p = _make_planner()
        prompt = p.build_decision_system_prompt()
        assert "CAPTCHA" not in prompt

    def test_system_prompt_with_captcha_mode(self):
        p = _make_planner()
        prompt = p.build_decision_system_prompt(captcha_mode=True)
        assert "CAPTCHA SOLVING PROTOCOL ACTIVE" in prompt

    def test_captcha_system_prompt_includes_checkbox(self):
        p = _make_planner()
        prompt = p.build_captcha_system_prompt()
        assert "checkbox" in prompt.lower()

    def test_user_prompt_structure(self):
        p = _make_planner()
        inp = {
            "taskInstruction": "Search for cats",
            "page": {"url": "https://example.com"},
            "elements": [{"id": 1, "tag": "input"}],
            "history": [],
            "debugState": {"recentErrors": ["err1"]},
            "tabs": [{"index": 0, "url": "https://example.com"}],
        }
        result = json.loads(p.build_decision_user_prompt(inp))
        assert result["task"] == "Search for cats"
        assert result["current_page"]["url"] == "https://example.com"
        assert result["observed_elements"] == [{"id": 1, "tag": "input"}]
        assert result["debug_state"]["recentErrors"] == ["err1"]
        assert len(result["tabs"]) == 1

    def test_user_prompt_handles_none_debug_state(self):
        p = _make_planner()
        inp = {"taskInstruction": "do thing"}
        result = json.loads(p.build_decision_user_prompt(inp))
        assert result["debug_state"]["recentErrors"] is None

    def test_user_prompt_includes_captcha_hint(self):
        p = _make_planner()
        inp = {"taskInstruction": "do thing", "captchaHint": "recaptcha"}
        result = json.loads(p.build_decision_user_prompt(inp))
        assert result["captcha_hint"] == "recaptcha"

    def test_user_prompt_has_output_schema(self):
        p = _make_planner()
        inp = {"taskInstruction": "x"}
        result = json.loads(p.build_decision_user_prompt(inp))
        assert "required_output_schema" in result
        assert "thinking" in result["required_output_schema"]


class TestVerificationPrompts:
    def test_system_prompt_contains_verifier(self):
        p = _make_planner()
        prompt = p.build_verification_system_prompt()
        assert "verifier" in prompt.lower()

    def test_user_prompt_structure(self):
        p = _make_planner()
        inp = {
            "taskInstruction": "Buy item",
            "page": {"url": "https://shop.com/checkout"},
            "elements": [],
            "lastAction": {"type": "click", "id": 5},
            "lastActionResult": {"success": True},
            "history": [{"step": 1}],
        }
        result = json.loads(p.build_verification_user_prompt(inp))
        assert result["task"] == "Buy item"
        assert result["last_action"]["type"] == "click"
        assert result["required_output_schema"]["goal_status"]


# ---------------------------------------------------------------------------
# Model requests
# ---------------------------------------------------------------------------


class TestRequestJson:
    @pytest.mark.asyncio
    async def test_successful_request(self):
        router = AsyncMock()
        router.call.return_value = ModelResponse(
            content='{"status": "continue", "next_action": {"type": "click", "id": 1}}'
        )
        planner = LLMPlanner(router, tier="capable")
        result = await planner._request_json("sys", "user")
        assert result["status"] == "continue"
        assert result["next_action"]["type"] == "click"

    @pytest.mark.asyncio
    async def test_non_json_response_raises(self):
        router = AsyncMock()
        router.call.return_value = ModelResponse(
            content="I cannot do that."
        )
        planner = LLMPlanner(router, tier="capable", max_retries=0)
        with pytest.raises(ValueError, match="non-JSON"):
            await planner._request_json("sys", "user")

    @pytest.mark.asyncio
    async def test_retry_on_failure(self):
        router = AsyncMock()
        call_count = 0

        async def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("network fail")
            return ModelResponse(content='{"status": "ok"}')

        router.call.side_effect = _side_effect
        planner = LLMPlanner(
            router, tier="capable", max_retries=2,
            base_retry_delay_ms=10,
        )
        result = await planner._request_json("sys", "user")
        assert result["status"] == "ok"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_retry_exhausted_raises(self):
        router = AsyncMock()
        router.call.side_effect = ConnectionError("persistent fail")
        planner = LLMPlanner(
            router, tier="capable", max_retries=1,
            base_retry_delay_ms=10,
        )
        with pytest.raises(ConnectionError):
            await planner._request_json("sys", "user")

    @pytest.mark.asyncio
    async def test_json_with_surrounding_text(self):
        router = AsyncMock()
        router.call.return_value = ModelResponse(
            content='Here is the plan:\n{"status": "continue"}\nDone.'
        )
        planner = LLMPlanner(router, tier="capable", max_retries=0)
        result = await planner._request_json("sys", "user")
        assert result["status"] == "continue"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class TestDecide:
    @pytest.mark.asyncio
    async def test_decide_passes_captcha_mode(self):
        router = AsyncMock()
        router.call.return_value = ModelResponse(
            content='{"status": "continue", "next_action": {"type": "click", "id": 1}}'
        )
        planner = LLMPlanner(router, tier="fast", max_retries=0)

        result = await planner.decide({
            "taskInstruction": "Solve CAPTCHA",
            "captchaHint": "recaptcha detected",
        })
        assert result["status"] == "continue"

        messages = router.call.call_args[0][0]
        system_msg = messages[0]["content"]
        assert "CAPTCHA SOLVING PROTOCOL" in system_msg

    @pytest.mark.asyncio
    async def test_decide_without_captcha(self):
        router = AsyncMock()
        router.call.return_value = ModelResponse(
            content='{"status": "continue"}'
        )
        planner = LLMPlanner(router, tier="fast", max_retries=0)
        await planner.decide({"taskInstruction": "Click button"})

        messages = router.call.call_args[0][0]
        assert "CAPTCHA" not in messages[0]["content"]


class TestVerify:
    @pytest.mark.asyncio
    async def test_verify_returns_result(self):
        router = AsyncMock()
        router.call.return_value = ModelResponse(
            content='{"goal_status": "completed", "confidence": "high", "summary": "Done"}'
        )
        planner = LLMPlanner(router, tier="fast", max_retries=0)
        result = await planner.verify({
            "taskInstruction": "Do thing",
            "page": {"url": "https://example.com"},
        })
        assert result["goal_status"] == "completed"
        assert result["confidence"] == "high"

    @pytest.mark.asyncio
    async def test_verify_uses_verification_prompt(self):
        router = AsyncMock()
        router.call.return_value = ModelResponse(
            content='{"goal_status": "incomplete"}'
        )
        planner = LLMPlanner(router, tier="fast", max_retries=0)
        await planner.verify({"taskInstruction": "x"})

        messages = router.call.call_args[0][0]
        assert "verifier" in messages[0]["content"].lower()
