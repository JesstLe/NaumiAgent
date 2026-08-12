from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.evolution.capability_sandbox_request import (
    CapabilitySandboxRequestView,
)
from naumi_agent.evolution.capability_scenario_binding import (
    CapabilityScenarioBindingView,
)
from naumi_agent.evolution.proposal import generate_proposal_preview
from naumi_agent.evolution.review import EvolutionReviewService
from naumi_agent.evolution.store import EvolutionCandidateStore
from naumi_agent.harness.feedback import FeedbackIntakeService, build_direct_user_feedback
from naumi_agent.runtime.composition import create_agent_engine
from naumi_agent.tasks.store import TaskStore
from naumi_agent.ui.bridge import JsonlEngineBridge
from naumi_agent.ui.evolution_review import (
    _capability_artifact_payload,
    _capability_scenario_binding_payload,
    evolution_review_payload,
)
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


def test_capability_artifact_public_payload_and_protocol() -> None:
    value = SimpleNamespace(
        model_dump=lambda **_kwargs: {
            "schema_version": 1,
            "artifact": {
                "artifact_id": f"evcia_{'a' * 24}",
                "source_text": "private sealed source",
                "source_sha256": "b" * 64,
            },
        }
    )
    payload = _capability_artifact_payload(
        SimpleNamespace(capability_artifact=value)  # type: ignore[arg-type]
    )
    assert payload is not None
    assert "source_text" not in payload["artifact"]
    capability = normalize_client_record({
        "type": ClientEventType.EVOLUTION_REVIEW_REQUEST,
        "payload": {
            "action": "capability-spec",
            "candidate_id": f"evc_{'a' * 24}",
        },
    })
    assert capability["payload"]["action"] == "capability-spec"
    assert capability["payload"]["candidate_id"] == f"evc_{'a' * 24}"
    governance = normalize_client_record({
        "type": ClientEventType.EVOLUTION_REVIEW_REQUEST,
        "payload": {
            "action": "capability-govern",
            "candidate_id": f"evc_{'b' * 24}",
        },
    })
    assert governance["payload"]["action"] == "capability-govern"
    assert governance["payload"]["candidate_id"] == f"evc_{'b' * 24}"
    artifact = normalize_client_record({
        "type": ClientEventType.EVOLUTION_REVIEW_REQUEST,
        "payload": {
            "action": "capability-artifact",
            "candidate_id": f"evc_{'c' * 24}",
            "source_path": "sandbox/tool.py",
            "class_name": "SandboxTool",
        },
    })
    assert artifact["payload"]["source_path"] == "sandbox/tool.py"
    binding = normalize_client_record({
        "type": ClientEventType.EVOLUTION_REVIEW_REQUEST,
        "payload": {
            "action": "capability-bind",
            "candidate_id": f"evc_{'d' * 24}",
        },
    })
    assert binding["payload"]["action"] == "capability-bind"
    assert binding["payload"]["candidate_id"] == f"evc_{'d' * 24}"
    sandbox = normalize_client_record({
        "type": ClientEventType.EVOLUTION_REVIEW_REQUEST,
        "payload": {
            "action": "capability-sandbox",
            "candidate_id": f"evc_{'e' * 24}",
        },
    })
    assert sandbox["payload"]["action"] == "capability-sandbox"
    assert sandbox["payload"]["candidate_id"] == f"evc_{'e' * 24}"
    run = normalize_client_record({
        "type": ClientEventType.EVOLUTION_REVIEW_REQUEST,
        "payload": {
            "action": "capability-run",
            "candidate_id": f"evc_{'f' * 24}",
        },
    })
    assert run["payload"]["action"] == "capability-run"
    assert run["payload"]["candidate_id"] == f"evc_{'f' * 24}"
    register = normalize_client_record({
        "type": ClientEventType.EVOLUTION_REVIEW_REQUEST,
        "payload": {
            "action": "capability-register",
            "candidate_id": f"evc_{'a' * 24}",
            "duration_seconds": 45,
        },
    })
    assert register["payload"]["action"] == "capability-register"
    assert register["payload"]["duration_seconds"] == 45
    unregister = normalize_client_record({
        "type": ClientEventType.EVOLUTION_REVIEW_REQUEST,
        "payload": {
            "action": "capability-unregister",
            "candidate_id": f"evc_{'b' * 24}",
        },
    })
    assert unregister["payload"]["action"] == "capability-unregister"
    shadow = normalize_client_record({
        "type": ClientEventType.EVOLUTION_REVIEW_REQUEST,
        "payload": {
            "action": "capability-shadow",
            "candidate_id": f"evc_{'c' * 24}",
        },
    })
    assert shadow["payload"]["action"] == "capability-shadow"
    shadow_observation = normalize_client_record({
        "type": ClientEventType.EVOLUTION_REVIEW_REQUEST,
        "payload": {
            "action": "capability-shadow-observation",
            "candidate_id": f"evc_{'d' * 24}",
            "model": "openai/gpt-shadow",
        },
    })
    assert shadow_observation["payload"]["action"] == (
        "capability-shadow-observation"
    )
    assert shadow_observation["payload"]["model"] == "openai/gpt-shadow"
    with pytest.raises(ValueError, match="30"):
        normalize_client_record({
            "type": ClientEventType.EVOLUTION_REVIEW_REQUEST,
            "payload": {
                "action": "capability-register",
                "candidate_id": f"evc_{'a' * 24}",
                "duration_seconds": 901,
            },
        })
    with pytest.raises(ValueError, match="同时提供"):
        normalize_client_record({
            "type": ClientEventType.EVOLUTION_REVIEW_REQUEST,
            "payload": {
                "action": "capability-artifact",
                "candidate_id": f"evc_{'c' * 24}",
                "source_path": "sandbox/tool.py",
            },
        })


def test_capability_scenario_binding_payload_hides_oracle_values() -> None:
    scenario = SimpleNamespace(
        name="真实场景",
        expectation=SimpleNamespace(kind="result"),
        timeout_ms=1500,
    )
    value = SimpleNamespace(
        binding=SimpleNamespace(scenarios=(scenario,)),
        model_dump=lambda **_kwargs: {
            "binding": {
                "scenarios": [{
                    "name": "真实场景",
                    "arguments": {"secret_path": "private"},
                    "expectation": {"kind": "result", "value": {"private": True}},
                    "timeout_ms": 1500,
                }],
            },
        },
    )
    payload = _capability_scenario_binding_payload(
        SimpleNamespace(capability_scenario_binding=value)  # type: ignore[arg-type]
    )
    assert payload is not None
    assert payload["binding"]["scenarios"] == [{
        "name": "真实场景",
        "expectation_kind": "result",
        "timeout_ms": 1500,
    }]


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
async def test_real_bridge_routes_capability_scenario_binding(tmp_path: Path) -> None:
    engine = create_agent_engine(AppConfig(
        workspace_root=str(tmp_path),
        memory=MemoryConfig(
            session_db_path=str(tmp_path / "sessions.db"),
            long_term_enabled=False,
        ),
    ))
    store = EvolutionCandidateStore(tmp_path / "evolution.db")
    candidate_id = await _seed(tmp_path, store)
    engine.evolution_candidate_store = store
    engine.evolution_review_service = EvolutionReviewService(store)
    view = CapabilityScenarioBindingView(
        candidate_id=candidate_id,
        artifact_id=f"evcia_{'a' * 24}",
        binding=None,
        state="missing",
        pending_interaction_id="",
        artifact_current=True,
        binding_current=False,
        sandbox_execution_eligible=False,
    )
    advance = AsyncMock(return_value=view)
    engine.evolution_capability_scenario_binding_service = SimpleNamespace(
        advance=advance,
    )
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)
    try:
        await bridge.handle_client_record({
            "id": "evolution-capability-bind-1",
            "type": ClientEventType.EVOLUTION_REVIEW_REQUEST,
            "payload": {"action": "capability-bind", "candidate_id": candidate_id},
        })
        records = [json.loads(line) for line in writer.getvalue().splitlines()]
        notice = next(record for record in records if record["type"] == "ui/message")
        assert notice["payload"]["title"] == "Capability 可执行场景绑定"
        assert "Sandbox 执行授权：否" in notice["payload"]["content"]
        assert any(record["type"] == "evolution/review" for record in records)
        advance.assert_awaited_once_with(tmp_path, candidate_id=candidate_id)
    finally:
        await bridge.shutdown()


@pytest.mark.asyncio
async def test_bridge_prepares_typed_capability_sandbox_request(
    tmp_path: Path,
) -> None:
    store = EvolutionCandidateStore(tmp_path / "evolution.db")
    candidate_id = await _seed(tmp_path, store)
    engine = create_agent_engine(AppConfig(
        workspace_root=str(tmp_path),
        memory=MemoryConfig(
            session_db_path=str(tmp_path / "sessions.db"),
            long_term_enabled=False,
        ),
    ))
    engine.evolution_candidate_store = store
    engine.evolution_review_service = EvolutionReviewService(store)
    view = CapabilitySandboxRequestView(
        candidate_id=candidate_id,
        request=None,
        state="missing",
        binding_current=False,
        source_current=False,
    )
    prepare = AsyncMock(return_value=view)
    engine.evolution_capability_sandbox_request_service = SimpleNamespace(
        prepare=prepare,
    )
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)
    try:
        await bridge.handle_client_record({
            "id": "evolution-capability-sandbox-1",
            "type": ClientEventType.EVOLUTION_REVIEW_REQUEST,
            "payload": {
                "action": "capability-sandbox",
                "candidate_id": candidate_id,
            },
        })
        records = [json.loads(line) for line in writer.getvalue().splitlines()]
        notice = next(record for record in records if record["type"] == "ui/message")
        assert notice["payload"]["title"] == "Capability Sandbox Execution Request"
        assert "Sandbox 执行授权：否" in notice["payload"]["content"]
        assert any(record["type"] == "evolution/review" for record in records)
        prepare.assert_awaited_once_with(tmp_path, candidate_id=candidate_id)
    finally:
        await bridge.shutdown()


@pytest.mark.asyncio
async def test_bridge_executes_typed_capability_sandbox_action(
    tmp_path: Path,
) -> None:
    store = EvolutionCandidateStore(tmp_path / "evolution.db")
    candidate_id = await _seed(tmp_path, store)
    engine = create_agent_engine(AppConfig(
        workspace_root=str(tmp_path),
        memory=MemoryConfig(
            session_db_path=str(tmp_path / "sessions.db"),
            long_term_enabled=False,
        ),
    ))
    engine.evolution_candidate_store = store
    engine.evolution_review_service = EvolutionReviewService(store)
    execute = AsyncMock(return_value=SimpleNamespace(
        content="# Capability Sandbox Execution\n\n- 状态：`passed`",
    ))
    engine.execute_tool = execute
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)
    try:
        await bridge.handle_client_record({
            "id": "evolution-capability-run-1",
            "type": ClientEventType.EVOLUTION_REVIEW_REQUEST,
            "payload": {
                "action": "capability-run",
                "candidate_id": candidate_id,
            },
        })
        records = [json.loads(line) for line in writer.getvalue().splitlines()]
        notice = next(record for record in records if record["type"] == "ui/message")
        assert notice["payload"]["title"] == "Capability Sandbox Execution"
        assert "passed" in notice["payload"]["content"]
        assert any(record["type"] == "evolution/review" for record in records)
        call = execute.await_args.args[0]
        assert call.name == "evolution_capability_sandbox_execute"
        arguments = json.loads(call.arguments)
        assert arguments["candidate_id"] == candidate_id
        assert arguments["run_id"].startswith("uicaprun-")
    finally:
        await bridge.shutdown()


@pytest.mark.asyncio
async def test_bridge_executes_typed_capability_registry_action(
    tmp_path: Path,
) -> None:
    store = EvolutionCandidateStore(tmp_path / "evolution.db")
    candidate_id = await _seed(tmp_path, store)
    engine = create_agent_engine(AppConfig(
        workspace_root=str(tmp_path),
        memory=MemoryConfig(
            session_db_path=str(tmp_path / "sessions.db"),
            long_term_enabled=False,
        ),
    ))
    engine.evolution_candidate_store = store
    engine.evolution_review_service = EvolutionReviewService(store)
    execute = AsyncMock(return_value=SimpleNamespace(
        content="# Capability Registry Lease\n\n- 状态：`active`",
    ))
    engine.execute_tool = execute
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)
    try:
        await bridge.handle_client_record({
            "id": "evolution-capability-register-1",
            "type": ClientEventType.EVOLUTION_REVIEW_REQUEST,
            "payload": {
                "action": "capability-register",
                "candidate_id": candidate_id,
                "duration_seconds": 45,
            },
        })
        records = [json.loads(line) for line in writer.getvalue().splitlines()]
        notice = next(record for record in records if record["type"] == "ui/message")
        assert notice["payload"]["title"] == "Capability Registry Lease"
        assert "active" in notice["payload"]["content"]
        call = execute.await_args.args[0]
        assert call.name == "evolution_capability_registry_lease"
        arguments = json.loads(call.arguments)
        assert arguments["action"] == "acquire"
        assert arguments["candidate_id"] == candidate_id
        assert arguments["duration_seconds"] == 45
        assert arguments["run_id"].startswith("uicapregistry-")
    finally:
        await bridge.shutdown()


@pytest.mark.asyncio
async def test_bridge_executes_typed_capability_shadow_action(
    tmp_path: Path,
) -> None:
    store = EvolutionCandidateStore(tmp_path / "evolution.db")
    candidate_id = await _seed(tmp_path, store)
    engine = create_agent_engine(AppConfig(
        workspace_root=str(tmp_path),
        memory=MemoryConfig(
            session_db_path=str(tmp_path / "sessions.db"),
            long_term_enabled=False,
        ),
    ))
    engine.evolution_candidate_store = store
    engine.evolution_review_service = EvolutionReviewService(store)
    execute = AsyncMock(return_value=SimpleNamespace(
        content="# Capability Shadow Descriptor\n\n- 状态：`ready`",
    ))
    engine.execute_tool = execute
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)
    try:
        await bridge.handle_client_record({
            "id": "evolution-capability-shadow-1",
            "type": ClientEventType.EVOLUTION_REVIEW_REQUEST,
            "payload": {
                "action": "capability-shadow",
                "candidate_id": candidate_id,
            },
        })
        records = [json.loads(line) for line in writer.getvalue().splitlines()]
        notice = next(record for record in records if record["type"] == "ui/message")
        assert notice["payload"]["title"] == "Capability Shadow Descriptor"
        assert "ready" in notice["payload"]["content"]
        call = execute.await_args.args[0]
        assert call.name == "evolution_capability_shadow_descriptor"
        assert json.loads(call.arguments) == {
            "action": "compile",
            "candidate_id": candidate_id,
        }
    finally:
        await bridge.shutdown()


@pytest.mark.asyncio
async def test_bridge_executes_typed_capability_shadow_observation_action(
    tmp_path: Path,
) -> None:
    store = EvolutionCandidateStore(tmp_path / "evolution.db")
    candidate_id = await _seed(tmp_path, store)
    engine = create_agent_engine(AppConfig(
        workspace_root=str(tmp_path),
        memory=MemoryConfig(
            session_db_path=str(tmp_path / "sessions.db"),
            long_term_enabled=False,
        ),
    ))
    engine.evolution_candidate_store = store
    engine.evolution_review_service = EvolutionReviewService(store)
    execute = AsyncMock(return_value=SimpleNamespace(
        content="# Capability Shadow 观察契约\n\n- 状态：`ready`",
    ))
    engine.execute_tool = execute
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)
    try:
        await bridge.handle_client_record({
            "id": "evolution-capability-shadow-observation-1",
            "type": ClientEventType.EVOLUTION_REVIEW_REQUEST,
            "payload": {
                "action": "capability-shadow-observation",
                "candidate_id": candidate_id,
                "model": "openai/gpt-shadow",
            },
        })
        records = [json.loads(line) for line in writer.getvalue().splitlines()]
        notice = next(record for record in records if record["type"] == "ui/message")
        assert notice["payload"]["title"] == "Capability Shadow 观察契约"
        assert "ready" in notice["payload"]["content"]
        call = execute.await_args.args[0]
        assert call.name == "evolution_capability_shadow_observation_contract"
        assert json.loads(call.arguments) == {
            "action": "compile",
            "candidate_id": candidate_id,
            "model": "openai/gpt-shadow",
        }
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
