"""Agent-facing trusted feedback intake tool."""

from __future__ import annotations

import platform
from typing import Any

from naumi_agent.harness.feedback import (
    FeedbackIntakeService,
    build_agent_interpreted_feedback,
    render_feedback_result,
)
from naumi_agent.tools.base import Tool, ToolMetadata


class FeedbackIntakeTool(Tool):
    """Record an interpretation only when a durable user turn is active."""

    def __init__(self, engine: Any, service: FeedbackIntakeService) -> None:
        self._engine = engine
        self._service = service

    @property
    def name(self) -> str:
        return "feedback_intake"

    @property
    def description(self) -> str:
        return (
            "把当前真实用户消息中的纠正或缺陷报告记录为不可执行改进候选。"
            "只能在 durable Chat Run 正在处理时使用；偏好、取消和赞扬会被忽略。"
            "本工具记录 Agent interpretation，不会伪装成用户直接提交。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["correction", "defect", "preference", "cancel", "praise"],
                    "description": "反馈类别。只有 correction/defect 形成候选。",
                },
                "scope": {
                    "type": "string",
                    "description": "稳定的相对模块 scope，例如 ui:task_panel。",
                },
                "topic": {
                    "type": "string",
                    "description": "稳定小写主题，例如 subagent_status。",
                },
                "summary": {
                    "type": "string",
                    "description": "本轮反馈摘要；只参与摘要计算，不持久化原文。",
                },
            },
            "required": ["category", "scope", "topic", "summary"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            user_facing_name="反馈候选记录",
            search_hint="feedback correction defect user feedback 纠正 缺陷 反馈",
        )

    async def execute(
        self,
        category: str,
        scope: str,
        topic: str,
        summary: str,
    ) -> str:
        envelope = self._engine.current_feedback_turn()
        if envelope is None:
            return (
                "当前没有可验证的 durable 用户消息，未记录反馈。"
                "请仅在正在处理用户纠正的本轮调用该工具。"
            )
        provider, model = feedback_model_dimensions(self._engine)
        observation = build_agent_interpreted_feedback(
            envelope,
            category=category,
            scope=scope,
            topic=topic,
            summary=summary,
            provider=provider,
            model=model,
            platform=platform.system().lower(),
        )
        result = await self._service.ingest(
            self._engine.workspace_root,
            observation,
        )
        return render_feedback_result(result)


def feedback_model_dimensions(engine: Any) -> tuple[str, str]:
    router = getattr(engine, "router", None)
    model = str(
        getattr(router, "current_model", "")
        or getattr(router, "model", "")
        or ""
    ).strip()
    provider = model.split("/", 1)[0] if "/" in model else ""
    return provider[:128], model[:256]


def create_feedback_tools(
    engine: Any,
    service: FeedbackIntakeService,
) -> list[Tool]:
    return [FeedbackIntakeTool(engine, service)]


__all__ = ["FeedbackIntakeTool", "create_feedback_tools", "feedback_model_dimensions"]
