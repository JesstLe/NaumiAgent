"""Tool that pauses an agent run for validated user input."""

from __future__ import annotations

import json
from typing import Any

from naumi_agent.tools.base import Tool, ToolMetadata
from naumi_agent.user_interaction import (
    UserInteractionUnavailableError,
    normalize_interaction_request,
    normalize_interaction_response,
)


class RequestUserInputTool(Tool):
    """Ask only for decisions that materially affect the requested outcome."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "request_user_input"

    @property
    def description(self) -> str:
        return (
            "当一个无法从上下文安全推断的用户决定会实质改变结果时，暂停当前运行并询问用户。"
            "提供 2 到 3 个互斥选项及简短影响说明，可允许用户输入其他答案。"
            "不要用它询问可自行判断的小问题，也不要用于工具权限确认。"
        )

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            user_facing_name="询问用户",
            search_hint="ask user input choice clarification decision options custom response",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "header": {"type": "string", "minLength": 1, "maxLength": 40},
                "question": {"type": "string", "minLength": 1, "maxLength": 2000},
                "options": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 3,
                    "items": {
                        "type": "object",
                        "properties": {
                            "value": {"type": "string", "minLength": 1, "maxLength": 80},
                            "label": {"type": "string", "minLength": 1, "maxLength": 80},
                            "description": {"type": "string", "maxLength": 300},
                        },
                        "required": ["value", "label", "description"],
                        "additionalProperties": False,
                    },
                },
                "allow_custom": {"type": "boolean", "default": True},
                "custom_label": {"type": "string", "minLength": 1, "maxLength": 80},
            },
            "required": ["header", "question", "options"],
            "additionalProperties": False,
        }

    async def execute(
        self,
        *,
        header: str,
        question: str,
        options: list[dict[str, str]],
        allow_custom: bool = True,
        custom_label: str = "其他",
        **kwargs: Any,
    ) -> str:
        try:
            request = normalize_interaction_request(
                {
                    "header": header,
                    "question": question,
                    "options": options,
                    "allow_custom": allow_custom,
                    "custom_label": custom_label,
                }
            )
            raw_response = await self._engine.request_user_input(request.to_public_dict())
            response = normalize_interaction_response(request, raw_response)
        except UserInteractionUnavailableError as exc:
            return f"⚠️ 无法询问用户：{exc}"
        except ValueError as exc:
            return f"⚠️ 用户交互输入或响应无效：{exc}"
        return json.dumps(response, ensure_ascii=False)
