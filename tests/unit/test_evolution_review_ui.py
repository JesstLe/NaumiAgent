from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.evolution.proposal import generate_proposal_preview
from naumi_agent.evolution.review import EvolutionReviewService
from naumi_agent.evolution.store import EvolutionCandidateStore
from naumi_agent.harness.feedback import FeedbackIntakeService, build_direct_user_feedback
from naumi_agent.runtime.composition import create_agent_engine
from naumi_agent.tasks.store import TaskStore
from naumi_agent.ui.bridge import JsonlEngineBridge
from naumi_agent.ui.evolution_review import evolution_review_payload
from naumi_agent.ui.protocol import ClientEventType, normalize_client_record
from naumi_agent.workbench.models import ProposalSourceKind, RiskLevel
from naumi_agent.workbench.proposal_governance import ProposalAction
from naumi_agent.workbench.service import WorkbenchService
from naumi_agent.workbench.store import WorkbenchStore

NOW = datetime(2026, 7, 18, 18, 0, tzinfo=UTC)


async def _seed(root: Path, store: EvolutionCandidateStore) -> str:
    intake = FeedbackIntakeService(store)
    result = None
    for offset in range(2):
        result = await intake.ingest(
            root,
            build_direct_user_feedback(
                session_id="typed-ui",
                category="defect",
                scope="ui:footer",
                topic="truncation",
                summary=f"底栏截断 {offset} token=never-render",
                now=NOW + timedelta(minutes=offset),
            ),
        )
    assert result is not None
    return result.candidate_id


@pytest.mark.asyncio
async def test_typed_payload_is_bounded_private_and_contains_policy(tmp_path: Path) -> None:
    store = EvolutionCandidateStore(tmp_path / "evolution.db")
    candidate_id = await _seed(tmp_path, store)
    snapshot = await EvolutionReviewService(store).detail_snapshot(tmp_path, candidate_id)

    payload = evolution_review_payload(snapshot)

    assert payload["schema_version"] == 1
    assert payload["mode"] == "detail"
    assert payload["read_only"] is True
    assert payload["selected"]["decision"] == "review_ready"  # type: ignore[index]
    assert payload["selected"]["experiment_eligible"] is False  # type: ignore[index]
    assert payload["selected"]["aggregation"]["policy_version"] == "candidate-aggregation-v1"  # type: ignore[index]
    assert payload["selected"]["aggregation"]["total_count"] == 2  # type: ignore[index]
    assert payload["selected"]["proposal"]["proposal_kind"] == "code"  # type: ignore[index]
    assert payload["selected"]["proposal"]["executable"] is False  # type: ignore[index]
    assert payload["selected"]["proposal"]["state"] == "preview"  # type: ignore[index]
    assert len(payload["events"]) == 2
    assert "never-render" not in json.dumps(payload, ensure_ascii=False)


@pytest.mark.asyncio
async def test_typed_detail_reflects_durable_cooldown_and_significant_evidence(
    tmp_path: Path,
) -> None:
    evolution_store = EvolutionCandidateStore(tmp_path / "evolution.db")
    candidate_id = await _seed(tmp_path, evolution_store)
    stored = await evolution_store.get_candidate(tmp_path, candidate_id)
    assert stored is not None
    preview = generate_proposal_preview(stored)
    assert preview is not None

    database = str(tmp_path / "workbench.db")
    workbench_store = WorkbenchStore(database)
    workbench = WorkbenchService(
        task_store=TaskStore(database),
        workbench_store=workbench_store,
    )
    proposal = await workbench_store.create_proposal(
        session_id="typed-ui-session",
        mission_id="mission-1",
        task_id="task-1",
        agent_id="Evolution-Agent",
        title=preview.title,
        impact_scope=preview.impact_scope,
        risk_level=RiskLevel(preview.risk_level),
        source_kind=ProposalSourceKind.EVOLUTION_CANDIDATE,
        source_id=candidate_id,
        source_revision=preview.source.candidate_revision,
        source_occurrence_count=preview.source.occurrence_count,
        source_sha256=preview.source.candidate_sha256,
        source_proposal_id=preview.proposal_id,
        generator_version=preview.generator_version,
        proposal_kind=preview.proposal_kind,
        idempotency_key=f"evolution:{preview.proposal_id}",
    )
    await workbench.govern_proposal(
        "typed-ui-session",
        proposal.id,
        action=ProposalAction.REJECT,
        reviewer="Human",
        decision_note="当前证据不足",
        now=NOW,
    )
    review = EvolutionReviewService(
        evolution_store,
        governance_reader=workbench,
    )

    blocked = evolution_review_payload(
        await review.detail_snapshot(tmp_path, candidate_id)
    )
    assert blocked["selected"]["decision"] == "needs_evidence"  # type: ignore[index]
    assert blocked["selected"]["proposal"] is None  # type: ignore[index]
    assert blocked["selected"]["governance"]["reason"] == "cooldown_active"  # type: ignore[index]
    assert blocked["selected"]["governance"]["allowed"] is False  # type: ignore[index]

    intake = FeedbackIntakeService(evolution_store)
    for offset in range(2, 4):
        await intake.ingest(
            tmp_path,
            build_direct_user_feedback(
                session_id="typed-ui",
                category="defect",
                scope="ui:footer",
                topic="truncation",
                summary=f"底栏截断新增证据 {offset}",
                now=NOW + timedelta(minutes=offset),
            ),
        )
    significant = evolution_review_payload(
        await review.detail_snapshot(tmp_path, candidate_id)
    )
    assert significant["selected"]["decision"] == "review_ready"  # type: ignore[index]
    assert significant["selected"]["proposal"] is not None  # type: ignore[index]
    assert significant["selected"]["governance"]["reason"] == "significant_new_evidence"  # type: ignore[index]
    assert significant["selected"]["governance"]["allowed"] is True  # type: ignore[index]


def test_protocol_normalizes_and_rejects_evolution_review_requests() -> None:
    record = normalize_client_record({
        "type": ClientEventType.EVOLUTION_REVIEW_REQUEST,
        "payload": {
            "action": "LIST",
            "query": " footer ",
            "risk": "MEDIUM",
            "source_kind": "user_feedback",
            "limit": 25,
        },
    })
    assert record["payload"] == {
        "action": "list",
        "candidate_id": "",
        "query": "footer",
        "risk": "medium",
        "source_kind": "user_feedback",
        "limit": 25,
    }
    dropped = normalize_client_record({
        "type": ClientEventType.EVOLUTION_REVIEW_REQUEST,
        "payload": {"action": "list", "candidate_id": "private-value"},
    })
    assert dropped["payload"]["candidate_id"] == ""
    with pytest.raises(ValueError, match="candidate_id"):
        normalize_client_record({
            "type": ClientEventType.EVOLUTION_REVIEW_REQUEST,
            "payload": {"action": "detail", "candidate_id": "../other"},
        })


@pytest.mark.asyncio
async def test_real_bridge_emits_typed_read_only_detail(tmp_path: Path) -> None:
    engine = create_agent_engine(AppConfig(
        workspace_root=str(tmp_path),
        memory=MemoryConfig(
            session_db_path=str(tmp_path / "sessions.db"),
            long_term_enabled=False,
        ),
    ))
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)
    store = EvolutionCandidateStore(tmp_path / "evolution.db")
    candidate_id = await _seed(tmp_path, store)
    engine.evolution_candidate_store = store
    engine.evolution_review_service = EvolutionReviewService(store)
    before = await store.list_events(tmp_path, candidate_id)
    try:
        await bridge.handle_client_record({
            "id": "evolution-detail-1",
            "type": ClientEventType.EVOLUTION_REVIEW_REQUEST,
            "payload": {"action": "detail", "candidate_id": candidate_id},
        })
        records = [json.loads(line) for line in writer.getvalue().splitlines()]
        payload = next(
            record["payload"]
            for record in records
            if record["type"] == "evolution/review"
        )
        assert payload["selected"]["candidate_id"] == candidate_id
        assert payload["selected"]["decision"] == "review_ready"
        assert payload["selected"]["proposal"]["proposal_id"].startswith("evp_")
        assert payload["read_only"] is True
        assert await store.list_events(tmp_path, candidate_id) == before
    finally:
        await bridge.shutdown()


@pytest.mark.asyncio
async def test_bridge_evolution_failure_is_fixed_and_private(tmp_path: Path) -> None:
    class BrokenReview:
        async def list_snapshot(self, *_args: object, **_kwargs: object) -> object:
            raise OSError("token=must-not-render")

    engine = create_agent_engine(AppConfig(
        workspace_root=str(tmp_path),
        memory=MemoryConfig(
            session_db_path=str(tmp_path / "sessions.db"),
            long_term_enabled=False,
        ),
    ))
    engine.evolution_review_service = BrokenReview()  # type: ignore[assignment]
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)
    try:
        await bridge.handle_client_record({
            "id": "evolution-list-failure",
            "type": ClientEventType.EVOLUTION_REVIEW_REQUEST,
            "payload": {"action": "list"},
        })
        records = [json.loads(line) for line in writer.getvalue().splitlines()]
        error = next(record for record in records if record["type"] == "error")
        assert error["payload"]["code"] == "evolution_review_failed"
        assert "must-not-render" not in writer.getvalue()
    finally:
        await bridge.shutdown()
