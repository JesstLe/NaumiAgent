from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.evolution.post_rollback_long_term_outcomes import (
    EvolutionPostRollbackLongTermOutcomeError,
    EvolutionPostRollbackLongTermOutcomeService,
    EvolutionPostRollbackLongTermOutcomeStore,
    render_post_rollback_long_term_outcome,
)
from naumi_agent.evolution.revalidation_rollback_outcomes import (
    EVOLUTION_REVALIDATION_ROLLBACK_OUTCOME_POLICY,
    EvolutionRevalidationRollbackOutcome,
    EvolutionRevalidationRollbackOutcomeStore,
)
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.evolution_review import EvolutionPostRollbackLongTermOutcomeTool
from tests.unit.test_post_rollback_long_term_observation_assessment import (
    _fixture as _assessment_fixture,
)
from tests.unit.test_post_rollback_long_term_observation_assessment import (
    _heartbeat,
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _rollback_outcome(tmp_path: Path) -> EvolutionRevalidationRollbackOutcome:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir(exist_ok=True)
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_ROLLBACK_OUTCOME_POLICY,
        "workspace_root": str(workspace),
        "status": "rolled_back",
        "rollback_receipt_id": "evrerollbackexec_" + "1" * 24,
        "rollback_receipt_sha256": "1" * 64,
        "request_id": "evrerollbackreq_" + "2" * 24,
        "request_sha256": "2" * 64,
        "plan_id": "evrerolloutplan_" + "3" * 24,
        "plan_sha256": "3" * 64,
        "promotion_input_id": "evrevalpromoin_" + "4" * 24,
        "promotion_input_sha256": "4" * 64,
        "runtime_contract_id": "evrevalruntime_" + "5" * 24,
        "runtime_contract_sha256": "5" * 64,
        "prior_input_id": "evpromoin_" + "6" * 24,
        "prior_input_sha256": "6" * 64,
        "experiment_contract_id": "evx_" + "7" * 24,
        "experiment_contract_sha256": "7" * 64,
        "experiment_authority_id": "evxauth_" + "8" * 24,
        "experiment_authority_sha256": "8" * 64,
        "workbench_session_id": "session-long-term-outcome",
        "workbench_proposal_id": "proposal-long-term-outcome",
        "proposal_id": "evp_" + "9" * 24,
        "proposal_kind": "code",
        "candidate_id": "evc_" + "a" * 24,
        "candidate_revision": 3,
        "candidate_sha256": "a" * 64,
        "candidate_slot_id": "candidate-slot",
        "candidate_slot_sha256": "b" * 64,
        "baseline_slot_id": "baseline-slot",
        "baseline_slot_sha256": "c" * 64,
        "breach_reasons": ["runtime_guardrail_breach"],
        "rollback_fact_verified": True,
        "proposal_binding_verified": True,
        "outcome_recorded": True,
        "promoted": False,
        "superseded": False,
        "long_term_metrics_recorded": False,
        "learning_authority": False,
        "promotion_authority": False,
        "recorded_at": "2026-08-10T09:00:00+00:00",
    }
    digest = _digest(core)
    return EvolutionRevalidationRollbackOutcome.model_validate(
        {
            **core,
            "outcome_id": f"evrerollbackout_{digest[:24]}",
            "outcome_sha256": digest,
        }
    )


def _seed_root(db_path: Path, outcome: EvolutionRevalidationRollbackOutcome) -> None:
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS evolution_revalidation_rollback_outcomes ("
            "outcome_id TEXT PRIMARY KEY, outcome_sha256 TEXT NOT NULL UNIQUE, "
            "request_id TEXT NOT NULL UNIQUE, rollback_receipt_id TEXT NOT NULL UNIQUE, "
            "workbench_session_id TEXT NOT NULL, workbench_proposal_id TEXT NOT NULL, "
            "status TEXT NOT NULL CHECK(status = 'rolled_back'), "
            "outcome_json TEXT NOT NULL, recorded_at TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO evolution_revalidation_rollback_outcomes VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                outcome.outcome_id,
                outcome.outcome_sha256,
                outcome.request_id,
                outcome.rollback_receipt_id,
                outcome.workbench_session_id,
                outcome.workbench_proposal_id,
                outcome.status,
                outcome.model_dump_json(),
                outcome.recorded_at,
            ),
        )


class _RollbackOutcomeService:
    def __init__(self, outcome: EvolutionRevalidationRollbackOutcome) -> None:
        self.outcome = outcome
        self.authority = True

    async def inspect(self, *, request_id: str):
        assert request_id == self.outcome.request_id
        return SimpleNamespace(
            outcome=self.outcome,
            outcome_authority=self.authority,
        )


async def _fixture(tmp_path: Path):
    root = _rollback_outcome(tmp_path)
    (
        assessment_service,
        assessment_store,
        admission,
        harness_store,
        binding,
        origin,
        now,
    ) = await _assessment_fixture(tmp_path, rollback_outcome=root)
    _seed_root(assessment_store.db_path, root)
    root_store = EvolutionRevalidationRollbackOutcomeStore(assessment_store.db_path)
    root_service = _RollbackOutcomeService(root)
    store = EvolutionPostRollbackLongTermOutcomeStore(
        assessment_store.db_path,
        rollback_outcome_store=root_store,
        contract_store=assessment_service.contract_store,
        assessment_store=assessment_store,
    )
    service = EvolutionPostRollbackLongTermOutcomeService(
        workspace_root=assessment_service.workspace_root,
        rollback_outcome_store=root_store,
        rollback_outcome_service=root_service,  # type: ignore[arg-type]
        contract_store=assessment_service.contract_store,
        contract_service=assessment_service.contract_service,
        assessment_store=assessment_store,
        assessment_service=assessment_service,
        store=store,
    )
    return (
        service,
        store,
        root,
        root_store,
        root_service,
        assessment_service,
        admission,
        harness_store,
        binding,
        origin,
        now,
    )


async def _pass_window(harness_store, binding, origin, now):
    first = origin + timedelta(seconds=1)
    for offset in range(13):
        latest = first + timedelta(seconds=300 * offset)
        await _heartbeat(
            harness_store,
            binding,
            sequence=offset + 2,
            observed_at=latest,
        )
    now[0] = latest
    return latest


@pytest.mark.asyncio
async def test_passing_assessment_appends_outcome_and_supersede_event(
    tmp_path: Path,
) -> None:
    (
        service,
        store,
        root,
        root_store,
        _root_service,
        _assessment_service,
        admission,
        harness,
        binding,
        origin,
        now,
    ) = await _fixture(tmp_path)
    await _pass_window(harness, binding, origin, now)

    view = await service.record(
        request_id=root.request_id,
        subject_id=admission.subject_id,
    )

    assert view.outcome_authority
    assert view.outcome.status == "rollback_recovery_observed"
    assert view.outcome.revision_sequence == 1
    assert view.outcome.prior_outcome_kind == "rollback_outcome"
    assert view.outcome.prior_outcome_id == root.outcome_id
    assert view.outcome.rollback_fact_preserved
    assert view.outcome.long_term_metrics_recorded
    assert view.outcome.baseline_sustained_health_verified
    assert not view.outcome.candidate_promoted
    assert not view.outcome.promoted
    assert not view.learning_authority
    assert not view.promotion_authority
    assert not view.execution_authority
    assert view.supersede_event.sequence == 1
    assert view.supersede_event.previous_event_id == ""
    assert not view.supersede_event.rollback_fact_deleted
    assert await store.head(root.request_id) == view.outcome
    assert await root_store.get_by_request(root.request_id) == root
    rendered = render_post_rollback_long_term_outcome(view)
    assert "原 rolled_back fact：保留" in rendered
    assert "false / false / false" in rendered


@pytest.mark.asyncio
async def test_non_passing_assessment_cannot_issue_outcome(tmp_path: Path) -> None:
    (
        service,
        _store,
        root,
        _root_store,
        _root_service,
        _assessment_service,
        admission,
        _harness,
        _binding,
        _origin,
        _now,
    ) = await _fixture(tmp_path)

    with pytest.raises(EvolutionPostRollbackLongTermOutcomeError) as blocked:
        await service.record(
            request_id=root.request_id,
            subject_id=admission.subject_id,
        )
    assert blocked.value.code == "post_rollback_long_term_health_not_passing"
    assert "insufficient" in str(blocked.value)


@pytest.mark.asyncio
async def test_concurrent_recording_converges_and_new_assessment_appends_revision(
    tmp_path: Path,
) -> None:
    (
        service,
        store,
        root,
        root_store,
        root_service,
        assessment_service,
        admission,
        harness,
        binding,
        origin,
        now,
    ) = await _fixture(tmp_path)
    latest = await _pass_window(harness, binding, origin, now)
    peer_store = EvolutionPostRollbackLongTermOutcomeStore(
        store.db_path,
        rollback_outcome_store=root_store,
        contract_store=assessment_service.contract_store,
        assessment_store=assessment_service.store,
    )
    peer = EvolutionPostRollbackLongTermOutcomeService(
        workspace_root=assessment_service.workspace_root,
        rollback_outcome_store=root_store,
        rollback_outcome_service=root_service,  # type: ignore[arg-type]
        contract_store=assessment_service.contract_store,
        contract_service=assessment_service.contract_service,
        assessment_store=assessment_service.store,
        assessment_service=assessment_service,
        store=peer_store,
    )

    left, right = await asyncio.gather(
        service.record(request_id=root.request_id, subject_id=admission.subject_id),
        peer.record(request_id=root.request_id, subject_id=admission.subject_id),
    )
    assert left == right

    next_sample = latest + timedelta(seconds=300)
    await _heartbeat(
        harness,
        binding,
        sequence=15,
        observed_at=next_sample,
    )
    now[0] = next_sample
    second = await service.record(
        request_id=root.request_id,
        subject_id=admission.subject_id,
    )
    assert second.outcome.revision_sequence == 2
    assert second.outcome.prior_outcome_kind == "long_term_outcome"
    assert second.outcome.prior_outcome_id == left.outcome.outcome_id
    assert second.supersede_event.previous_event_id == left.supersede_event.event_id
    assert second.outcome_authority

    old = await service.inspect(
        outcome=left.outcome,
        event=left.supersede_event,
    )
    assert not old.projection_head_authority
    assert not old.outcome_authority
    assert await root_store.get_by_request(root.request_id) == root

    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "DELETE FROM evolution_post_rollback_outcome_supersede_events "
            "WHERE event_id = ?",
            (left.supersede_event.event_id,),
        )
    broken_chain = await service.inspect(
        outcome=second.outcome,
        event=second.supersede_event,
    )
    assert not broken_chain.durable_pair_valid
    assert not broken_chain.outcome_authority
    event = left.supersede_event
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "INSERT INTO evolution_post_rollback_outcome_supersede_events "
            "(event_id, event_sha256, request_id, sequence, previous_event_id, "
            "prior_outcome_id, successor_outcome_id, event_json, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.event_id,
                event.event_sha256,
                event.request_id,
                event.sequence,
                event.previous_event_id,
                event.prior_outcome_id,
                event.successor_outcome_id,
                event.model_dump_json(),
                event.recorded_at,
            ),
        )

    now[0] = next_sample + timedelta(seconds=301)
    stale = await service.inspect(
        outcome=second.outcome,
        event=second.supersede_event,
    )
    assert not stale.current_long_term_health_authority
    assert not stale.outcome_authority


@pytest.mark.asyncio
async def test_tampered_supersede_row_fails_closed(tmp_path: Path) -> None:
    (
        service,
        store,
        root,
        _root_store,
        _root_service,
        _assessment_service,
        admission,
        harness,
        binding,
        origin,
        now,
    ) = await _fixture(tmp_path)
    await _pass_window(harness, binding, origin, now)
    view = await service.record(
        request_id=root.request_id,
        subject_id=admission.subject_id,
    )
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE evolution_post_rollback_outcome_supersede_events "
            "SET event_sha256 = ? WHERE event_id = ?",
            ("f" * 64, view.supersede_event.event_id),
        )

    stale = await service.inspect(
        outcome=view.outcome,
        event=view.supersede_event,
    )
    assert not stale.durable_pair_valid
    assert not stale.outcome_authority


@pytest.mark.asyncio
async def test_store_rejects_content_addressed_but_misbound_lineage(
    tmp_path: Path,
) -> None:
    (
        service,
        store,
        root,
        _root_store,
        _root_service,
        assessment_service,
        admission,
        harness,
        binding,
        origin,
        now,
    ) = await _fixture(tmp_path)
    await _pass_window(harness, binding, origin, now)
    view = await service.record(
        request_id=root.request_id,
        subject_id=admission.subject_id,
    )
    assessment = await assessment_service.store.get(view.outcome.assessment_id)
    contract = await assessment_service.contract_store.get_by_outcome(root.outcome_id)
    assert assessment is not None and contract is not None

    outcome_core = view.outcome.model_dump(
        mode="json",
        exclude={"outcome_id", "outcome_sha256"},
    )
    outcome_core["baseline_version"] = "forged-version"
    outcome_digest = _digest(outcome_core)
    forged_outcome = type(view.outcome).model_validate(
        {
            **outcome_core,
            "outcome_id": f"evpostlongout_{outcome_digest[:24]}",
            "outcome_sha256": outcome_digest,
        }
    )
    event_core = view.supersede_event.model_dump(
        mode="json",
        exclude={"event_id", "event_sha256"},
    )
    event_core["successor_outcome_id"] = forged_outcome.outcome_id
    event_core["successor_outcome_sha256"] = forged_outcome.outcome_sha256
    event_digest = _digest(event_core)
    forged_event = type(view.supersede_event).model_validate(
        {
            **event_core,
            "event_id": f"evpostoutsup_{event_digest[:24]}",
            "event_sha256": event_digest,
        }
    )

    with pytest.raises(EvolutionPostRollbackLongTermOutcomeError) as rejected:
        await store.record(
            outcome=forged_outcome,
            event=forged_event,
            root=root,
            contract=contract,
            assessment=assessment,
        )
    assert rejected.value.code == "post_rollback_long_term_outcome_pair_invalid"


@pytest.mark.asyncio
async def test_long_term_outcome_tool_and_slash_share_service(tmp_path: Path) -> None:
    (
        service,
        _store,
        root,
        _root_store,
        _root_service,
        _assessment_service,
        admission,
        harness,
        binding,
        origin,
        now,
    ) = await _fixture(tmp_path)
    await _pass_window(harness, binding, origin, now)
    tool = EvolutionPostRollbackLongTermOutcomeTool(
        SimpleNamespace(evolution_post_rollback_long_term_outcome_service=service)
    )
    arguments = {
        "request_id": root.request_id,
        "subject_id": admission.subject_id,
    }
    for mode in (PermissionMode.MODERATE, PermissionMode.BYPASS):
        decision = PermissionChecker(mode).check(tool.name, arguments, tool=tool)
        assert decision.allowed
        assert not decision.requires_confirmation
        assert not decision.requires_double_confirm

    rendered = await tool.execute(**arguments)
    assert "Post-Rollback Long-Term Outcome" in rendered
    assert "原 rolled_back fact：保留" in rendered

    registry = ToolRegistry()
    registry.register(tool)

    class _SlashEngine:
        tool_registry = registry

        async def execute_tool(self, call: ToolCall, *, agent_name=None):
            registered = self.tool_registry.get(call.name)
            assert registered is not None and agent_name == "cli"
            parsed = registered.parse_arguments(call.arguments)
            return ToolResult(
                call_id=call.id,
                status="success",
                content=await registered.execute(**parsed),
            )

    slash = await execute_slash_command(
        _SlashEngine(),
        f"/evolution outcome-record-long-term {root.request_id} "
        f"{admission.subject_id}",
    )
    assert "Post-Rollback Long-Term Outcome" in slash
    assert root.outcome_id in slash
