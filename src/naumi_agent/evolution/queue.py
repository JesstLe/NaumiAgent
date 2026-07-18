"""Explicit, idempotent adapter from Evolution Preview to Workbench review."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from naumi_agent.evolution.review import EvolutionReviewService
from naumi_agent.workbench.models import ProposalSourceKind, RiskLevel
from naumi_agent.workbench.service import WorkbenchService

_BINDING_ID_RE = re.compile(r"^[^\x00\r\n]{1,128}$")


@dataclass(frozen=True, slots=True)
class EvolutionProposalQueueResult:
    proposal: dict[str, Any]
    created: bool


class EvolutionProposalQueueAdapter:
    """Queue a server-verified Preview without granting execution authority."""

    def __init__(
        self,
        *,
        review_service: EvolutionReviewService,
        workbench_service: WorkbenchService,
    ) -> None:
        self._review_service = review_service
        self._workbench_service = workbench_service

    async def enqueue(
        self,
        workspace_root: str | Path,
        *,
        session_id: str,
        mission_id: str,
        task_id: str,
        agent_id: str,
        candidate_id: str,
    ) -> EvolutionProposalQueueResult:
        """Explicitly enqueue one current review-ready Candidate revision."""
        clean_session_id = _binding_id(session_id, "session")
        clean_mission_id = _binding_id(mission_id, "mission")
        clean_task_id = _binding_id(task_id, "task")
        clean_agent_id = _binding_id(agent_id, "Proposal 提交者")
        snapshot = await self._review_service.detail_snapshot(
            workspace_root,
            candidate_id.strip(),
        )
        selected = snapshot.selected
        preview = selected.proposal if selected is not None else None
        if selected is None:
            raise ValueError("Evolution Candidate 不存在。")
        if preview is None:
            raise ValueError("Candidate 尚未达到人工审阅入队条件。")

        await self._workbench_service.require_proposal_binding(
            session_id=clean_session_id,
            mission_id=clean_mission_id,
            task_id=clean_task_id,
        )
        validation_plan = [
            (
                f"{step.metric_name} {step.direction} {step.target:g} · "
                f"{step.verifier} · {step.procedure}"
            )
            for step in preview.validation_plan
        ]
        proposal, created = await self._workbench_service.create_or_get_proposal(
            session_id=clean_session_id,
            mission_id=clean_mission_id,
            task_id=clean_task_id,
            agent_id=clean_agent_id,
            title=preview.title,
            impact_scope=preview.impact_scope,
            intended_files=list(preview.intended_files),
            validation_plan=validation_plan,
            risk_level=RiskLevel(preview.risk_level),
            questions=[],
            source_kind=ProposalSourceKind.EVOLUTION_CANDIDATE,
            source_id=preview.source.candidate_id,
            source_revision=preview.source.candidate_revision,
            source_sha256=preview.source.candidate_sha256,
            source_proposal_id=preview.proposal_id,
            generator_version=preview.generator_version,
            proposal_kind=preview.proposal_kind,
            idempotency_key=f"evolution:{preview.proposal_id}",
        )
        return EvolutionProposalQueueResult(proposal=proposal, created=created)


def _binding_id(value: str, label: str) -> str:
    normalized = str(value).strip()
    if not _BINDING_ID_RE.fullmatch(normalized):
        raise ValueError(f"{label} ID 必须为 1..128 个无控制字符的文本。")
    return normalized


def render_queue_result(result: EvolutionProposalQueueResult) -> str:
    proposal = result.proposal
    status = "已加入" if result.created else "已在"
    return "\n".join(
        [
            f"# Evolution Proposal {status} Workbench 审阅队列",
            "",
            f"- Workbench ID：`{proposal['id']}`",
            f"- Preview ID：`{proposal['source_proposal_id']}`",
            f"- Candidate：`{proposal['source_id']}` · revision {proposal['source_revision']}",
            f"- 类型：`{proposal['proposal_kind']}`",
            f"- 状态：`{proposal['state']}`（仍需人工决定，不可执行）",
        ]
    )


__all__ = [
    "EvolutionProposalQueueAdapter",
    "EvolutionProposalQueueResult",
    "render_queue_result",
]
