from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from naumi_agent.evolution.queue import (
    EvolutionProposalQueueAdapter,
    render_queue_result,
)
from naumi_agent.evolution.review import EvolutionReviewService
from naumi_agent.evolution.store import EvolutionCandidateStore
from naumi_agent.harness.feedback import FeedbackIntakeService, build_direct_user_feedback
from naumi_agent.tasks.store import TaskStore
from naumi_agent.ui.bridge import JsonlEngineBridge
from naumi_agent.ui.protocol import ServerEventType
from naumi_agent.workbench.service import WorkbenchService
from naumi_agent.workbench.store import WorkbenchStore

NOW = datetime(2026, 7, 18, 21, 0, tzinfo=UTC)


async def _fixture(tmp_path: Path, *, repeats: int = 2):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    evolution_store = EvolutionCandidateStore(tmp_path / "evolution.db")
    intake = FeedbackIntakeService(evolution_store)
    result = None
    for offset in range(repeats):
        result = await intake.ingest(
            workspace,
            build_direct_user_feedback(
                session_id="queue-test",
                category="defect",
                scope="src/naumi_agent/ui/footer.py:render_footer",
                topic="footer_truncation",
                summary=f"底栏再次截断 {offset}",
                now=NOW + timedelta(minutes=offset),
            ),
        )
    assert result is not None

    db_path = str(tmp_path / "runtime.db")
    workbench_store = WorkbenchStore(db_path)
    service = WorkbenchService(
        task_store=TaskStore(db_path),
        workbench_store=workbench_store,
        workspace_root=str(workspace),
    )
    mission = await service.create_mission(
        session_id="session-1",
        title="治理自进化",
        goal="人工审阅候选后再实验",
    )
    issue = await service.create_issue(
        session_id="session-1",
        mission_id=mission.id,
        title="审阅 footer Candidate",
    )
    adapter = EvolutionProposalQueueAdapter(
        review_service=EvolutionReviewService(evolution_store),
        workbench_service=service,
    )
    return (
        workspace,
        evolution_store,
        workbench_store,
        adapter,
        mission.id,
        issue["task"]["id"],
        result.candidate_id,
    )


@pytest.mark.asyncio
async def test_explicit_enqueue_persists_verified_provenance_and_audit(
    tmp_path: Path,
) -> None:
    workspace, _evolution, store, adapter, mission_id, task_id, candidate_id = await _fixture(
        tmp_path
    )

    result = await adapter.enqueue(
        workspace,
        session_id="session-1",
        mission_id=mission_id,
        task_id=task_id,
        agent_id="Evolution-Agent",
        candidate_id=candidate_id,
    )

    assert result.created is True
    assert result.proposal["source_kind"] == "evolution_candidate"
    assert result.proposal["source_id"] == candidate_id
    assert result.proposal["source_revision"] == 2
    assert result.proposal["source_proposal_id"].startswith("evp_")
    assert (
        result.proposal["idempotency_key"] == f"evolution:{result.proposal['source_proposal_id']}"
    )
    assert result.proposal["state"] == "open"
    events = await store.list_events("session-1", event_type="proposal.created")
    assert len(events) == 1
    assert events[0].payload["source_id"] == candidate_id
    rendered = render_queue_result(result)
    assert "仍需人工决定，不可执行" in rendered
    assert result.proposal["source_proposal_id"] in rendered


@pytest.mark.asyncio
async def test_concurrent_enqueue_is_idempotent_and_audited_once(tmp_path: Path) -> None:
    workspace, _evolution, store, adapter, mission_id, task_id, candidate_id = await _fixture(
        tmp_path
    )

    results = await asyncio.gather(
        *(
            adapter.enqueue(
                workspace,
                session_id="session-1",
                mission_id=mission_id,
                task_id=task_id,
                agent_id="Evolution-Agent",
                candidate_id=candidate_id,
            )
            for _ in range(8)
        )
    )

    assert sum(result.created for result in results) == 1
    assert len({result.proposal["id"] for result in results}) == 1
    proposals = await store.list_proposals("session-1")
    assert len(proposals) == 1
    events = await store.list_events("session-1", event_type="proposal.created")
    assert len(events) == 1


@pytest.mark.asyncio
async def test_enqueue_requires_review_ready_candidate_and_matching_issue(
    tmp_path: Path,
) -> None:
    workspace, _evolution, _store, adapter, mission_id, task_id, candidate_id = await _fixture(
        tmp_path, repeats=1
    )

    with pytest.raises(ValueError, match="尚未达到"):
        await adapter.enqueue(
            workspace,
            session_id="session-1",
            mission_id=mission_id,
            task_id=task_id,
            agent_id="Evolution-Agent",
            candidate_id=candidate_id,
        )
    with pytest.raises(ValueError, match="Candidate 不存在"):
        await adapter.enqueue(
            workspace,
            session_id="session-1",
            mission_id=mission_id,
            task_id=task_id,
            agent_id="Evolution-Agent",
            candidate_id="evc_000000000000000000000000",
        )


@pytest.mark.asyncio
async def test_enqueue_rejects_untracked_task_or_missing_mission(tmp_path: Path) -> None:
    workspace, _evolution, store, adapter, mission_id, task_id, candidate_id = await _fixture(
        tmp_path
    )

    with pytest.raises(ValueError, match="任务 #missing 不存在"):
        await adapter.enqueue(
            workspace,
            session_id="session-1",
            mission_id=mission_id,
            task_id="missing",
            agent_id="Human",
            candidate_id=candidate_id,
        )
    other_mission = await store.create_mission(
        "session-1", "另一治理任务", "不得借用其他 mission 的 issue"
    )
    with pytest.raises(ValueError, match="已跟踪 issue"):
        await adapter.enqueue(
            workspace,
            session_id="session-1",
            mission_id=other_mission.id,
            task_id=task_id,
            agent_id="Human",
            candidate_id=candidate_id,
        )
    with pytest.raises(ValueError, match="mission 不存在"):
        await adapter.enqueue(
            workspace,
            session_id="session-1",
            mission_id="missing",
            task_id="missing",
            agent_id="Human",
            candidate_id=candidate_id,
        )


@pytest.mark.asyncio
async def test_new_ui_bridge_enqueues_then_returns_current_detail(tmp_path: Path) -> None:
    workspace, evolution, _store, adapter, mission_id, task_id, candidate_id = await _fixture(
        tmp_path
    )
    engine = SimpleNamespace(
        workspace_root=workspace,
        evolution_review_service=EvolutionReviewService(evolution),
        evolution_proposal_queue=adapter,
        _session=SimpleNamespace(id="session-1"),
    )
    bridge = SimpleNamespace(
        engine=engine,
        emit=AsyncMock(),
        emit_error=AsyncMock(),
        _emit_system_notice=AsyncMock(),
        status_payload=lambda: {"session_id": "session-1"},
    )

    await JsonlEngineBridge.show_evolution_review(
        bridge,
        {
            "action": "enqueue",
            "candidate_id": candidate_id,
            "mission_id": mission_id,
            "task_id": task_id,
            "agent_id": "Human",
        },
        request_id="request-1",
    )

    bridge.emit_error.assert_not_awaited()
    bridge._emit_system_notice.assert_awaited_once()
    emitted_types = [call.args[0] for call in bridge.emit.await_args_list]
    assert emitted_types == [ServerEventType.EVOLUTION_REVIEW, ServerEventType.STATUS]
    detail_payload = bridge.emit.await_args_list[0].args[1]
    assert detail_payload["mode"] == "detail"
    assert detail_payload["selected"]["candidate_id"] == candidate_id
