from __future__ import annotations

import json
from pathlib import Path

import pytest

from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.tools.user_interaction import RequestUserInputTool
from naumi_agent.user_interaction import (
    UserInteractionUnavailableError,
    normalize_interaction_request,
    normalize_interaction_response,
)


def _options() -> list[dict[str, str]]:
    return [
        {"value": "safe", "label": "安全方案", "description": "保留兼容路径"},
        {"value": "fast", "label": "快速方案", "description": "优先交付速度"},
    ]


def test_interaction_request_normalizes_public_fields_and_control_text() -> None:
    request = normalize_interaction_request(
        {
            "header": "实现\x1b策略",
            "question": "请选择\x00方案",
            "options": _options(),
            "allow_custom": True,
            "custom_label": "其他方案",
        }
    )

    assert request.header == "实现策略"
    assert request.question == "请选择方案"
    assert request.options[0].value == "safe"
    assert request.allow_custom is True


@pytest.mark.parametrize(
    ("updates", "error"),
    [
        ({"question": ""}, "问题不能为空"),
        ({"options": _options()[:1]}, "必须提供 2 到 3 个选项"),
        ({"options": [*_options(), _options()[0]]}, "选项 value 不能重复"),
        ({"header": "x" * 41}, "标题最多 40 个字符"),
        ({"question": "x" * 2001}, "问题最多 2000 个字符"),
    ],
)
def test_interaction_request_rejects_invalid_boundaries(
    updates: dict[str, object],
    error: str,
) -> None:
    payload: dict[str, object] = {
        "header": "确认方案",
        "question": "请选择",
        "options": _options(),
    }
    payload.update(updates)

    with pytest.raises(ValueError, match=error):
        normalize_interaction_request(payload)


def test_interaction_response_rejects_unknown_choice_and_disabled_custom() -> None:
    request = normalize_interaction_request({
        "header": "确认方案",
        "question": "请选择",
        "options": _options(),
        "allow_custom": False,
    })

    with pytest.raises(ValueError, match="不属于当前问题"):
        normalize_interaction_response(request, {"kind": "option", "value": "unknown"})
    with pytest.raises(ValueError, match="不允许自定义输入"):
        normalize_interaction_response(
            request,
            {"kind": "custom", "custom_text": "绕过限制"},
        )


@pytest.mark.asyncio
async def test_request_user_input_tool_returns_selected_option() -> None:
    class Engine:
        async def request_user_input(self, payload):
            assert payload["question"] == "请选择"
            return {"kind": "option", "value": "fast", "label": "快速方案"}

    result = await RequestUserInputTool(Engine()).execute(
        header="确认方案",
        question="请选择",
        options=_options(),
        allow_custom=True,
    )

    assert json.loads(result) == {
        "kind": "option",
        "value": "fast",
        "label": "快速方案",
        "custom_text": "",
    }


@pytest.mark.asyncio
async def test_request_user_input_tool_returns_custom_text() -> None:
    class Engine:
        async def request_user_input(self, _payload):
            return {"kind": "custom", "custom_text": "只在当前工作区保存"}

    result = await RequestUserInputTool(Engine()).execute(
        header="确认方案",
        question="请选择",
        options=_options(),
        allow_custom=True,
    )

    assert json.loads(result)["custom_text"] == "只在当前工作区保存"


@pytest.mark.asyncio
async def test_request_user_input_tool_reports_missing_host() -> None:
    class Engine:
        async def request_user_input(self, _payload):
            raise UserInteractionUnavailableError("当前界面不支持结构化交互")

    result = await RequestUserInputTool(Engine()).execute(
        header="确认方案",
        question="请选择",
        options=_options(),
    )

    assert "无法询问用户" in result
    assert "当前界面不支持结构化交互" in result


@pytest.mark.asyncio
async def test_engine_registers_user_interaction_tool_and_callback(tmp_path: Path) -> None:
    engine = AgentEngine(
        AppConfig(memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
    )
    received: list[dict[str, object]] = []

    async def handler(payload: dict[str, object]) -> dict[str, str]:
        received.append(payload)
        return {"kind": "option", "value": "safe", "label": "安全方案"}

    try:
        engine.set_user_interaction_handler(handler)
        tool = engine.tool_registry.get("request_user_input")
        assert tool is not None
        result = json.loads(
            await tool.execute(
                header="确认方案",
                question="请选择",
                options=_options(),
            )
        )
        assert result["value"] == "safe"
        assert received[0]["options"][0]["value"] == "safe"
    finally:
        await engine.shutdown()
