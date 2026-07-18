"""Agent-facing read-only Evolution Candidate review tool."""

from __future__ import annotations

from typing import Any

from naumi_agent.evolution.review import (
    EvolutionReviewFilter,
    EvolutionReviewService,
    render_evolution_review,
)
from naumi_agent.evolution.store import EvolutionStoreError
from naumi_agent.tools.base import Tool, ToolMetadata


class EvolutionCandidatesTool(Tool):
    def __init__(self, engine: Any, service: EvolutionReviewService) -> None:
        self._engine = engine
        self._service = service

    @property
    def name(self) -> str:
        return "evolution_candidates"

    @property
    def description(self) -> str:
        return (
            "只读列出或查看当前工作区的 Evolution Candidate。"
            "显示来源、风险、频次、机械指标和审计链，不批准实验或修改代码。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "detail"], "default": "list"},
                "candidate_id": {"type": "string"},
                "query": {"type": "string"},
                "risk": {"type": "string", "enum": ["", "low", "medium", "high", "critical"]},
                "source_kind": {
                    "type": "string",
                    "enum": [
                        "",
                        "harness_failure",
                        "self_review_static",
                        "user_feedback",
                        "agent_interpreted_feedback",
                    ],
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
            },
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            user_facing_name="Evolution 候选审查",
            search_hint="evolution candidates review evidence risk 自进化 候选 审查",
        )

    async def execute(
        self,
        action: str = "list",
        candidate_id: str = "",
        query: str = "",
        risk: str = "",
        source_kind: str = "",
        limit: int = 50,
    ) -> str:
        try:
            if action == "detail":
                if not candidate_id.strip():
                    return "用法：evolution_candidates(action='detail', candidate_id='<id>')"
                snapshot = await self._service.detail_snapshot(
                    self._engine.workspace_root,
                    candidate_id.strip(),
                )
            elif action == "list":
                snapshot = await self._service.list_snapshot(
                    self._engine.workspace_root,
                    filters=EvolutionReviewFilter(
                        query=query.strip(),
                        risk=risk.strip(),
                        source_kind=source_kind.strip(),
                        limit=limit,
                    ),
                )
            else:
                return "action 仅支持 list 或 detail。"
        except (EvolutionStoreError, OSError, ValueError):
            return "Evolution Candidate 状态库不可读，或过滤条件无效。请运行 /doctor。"
        return render_evolution_review(snapshot)


def create_evolution_review_tools(
    engine: Any,
    service: EvolutionReviewService,
) -> list[Tool]:
    return [EvolutionCandidatesTool(engine, service)]


__all__ = ["EvolutionCandidatesTool", "create_evolution_review_tools"]
