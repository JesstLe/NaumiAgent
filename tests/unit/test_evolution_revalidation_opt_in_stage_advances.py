from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest

from naumi_agent.evolution.revalidation_opt_in_stage_advances import (
    EvolutionRevalidationOptInStageAdvanceError,
    EvolutionRevalidationOptInStageAdvanceService,
    EvolutionRevalidationOptInStageAdvanceStore,
    _build_receipt,
)
from naumi_agent.evolution.revalidation_opt_in_stage_completions import (
    EvolutionRevalidationOptInStageCompletion,
    EvolutionRevalidationOptInStageCompletionStatus,
    EvolutionRevalidationOptInStageCompletionView,
    _digest,
    _source_set_digest,
)
from naumi_agent.evolution.revalidation_rollout_stage_entries import (
    EvolutionRevalidationRolloutControlActor,
    EvolutionRevalidationRolloutControlService,
)
from naumi_agent.harness.interaction_runtime import DurableInteractionAuthorityClient
from naumi_agent.harness.store import HarnessStore
from naumi_agent.user_interaction import normalize_interaction_request
from tests.unit.test_evolution_revalidation_opt_in_execution_outcome_ledger import (
    _sources,
)
from tests.unit.test_evolution_revalidation_opt_in_stage_completions import (
    _baseline_service,
    _fixed_terminal_run,
    _stage_service,
)


def _passing_projection(receipt) -> EvolutionRevalidationOptInStageCompletion:
    payload = receipt.model_dump(mode="json")
    payload.update(
        baseline_cost_source="live_evidence",
        baseline_mean_cost_microusd=receipt.mean_reported_cost_microusd,
        cost_comparable=True,
        cost_regression_basis_points=0,
        insufficient_reasons=[],
        status="passing",
        opt_in_stage_completion_authority=True,
    )
    payload.pop("evidence_id")
    payload.pop("evidence_sha256")
    digest = _digest(payload)
    return EvolutionRevalidationOptInStageCompletion.model_validate(
        {
            **payload,
            "evidence_id": f"evreoptincomplete_{digest[:24]}",
            "evidence_sha256": digest,
        }
    )


def _next_projection(
    receipt: EvolutionRevalidationOptInStageCompletion,
) -> EvolutionRevalidationOptInStageCompletion:
    payload = receipt.model_dump(mode="json")
    payload.update(
        liveness_window_id="evreoptinwindow_" + "a" * 24,
        liveness_window_sha256="a" * 64,
        assessed_at=(
            datetime.fromisoformat(receipt.assessed_at) + timedelta(microseconds=1)
        ).isoformat(),
    )
    payload["source_set_sha256"] = _source_set_digest(
        plan_id=payload["plan_id"],
        plan_sha256=payload["plan_sha256"],
        baseline_id=payload["baseline_id"],
        baseline_sha256=payload["baseline_sha256"],
        liveness_window_id=payload["liveness_window_id"],
        liveness_window_sha256=payload["liveness_window_sha256"],
        outcome_ids=tuple(payload["outcome_ids"]),
        outcome_sha256=tuple(payload["outcome_sha256"]),
        authoritative_outcome_ids=tuple(payload["authoritative_outcome_ids"]),
        successful_outcome_ids=tuple(payload["successful_outcome_ids"]),
    )
    payload.pop("evidence_id")
    payload.pop("evidence_sha256")
    digest = _digest(payload)
    return EvolutionRevalidationOptInStageCompletion.model_validate(
        {
            **payload,
            "evidence_id": f"evreoptincomplete_{digest[:24]}",
            "evidence_sha256": digest,
        }
    )


async def _replace_completions(db_path: Path, *items) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "DELETE FROM evolution_revalidation_opt_in_stage_completions"
        )
        for item in items:
            await db.execute(
                "INSERT INTO evolution_revalidation_opt_in_stage_completions "
                "(evidence_id, evidence_sha256, completion_id, status, "
                "source_set_sha256, evidence_json, assessed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    item.evidence_id,
                    item.evidence_sha256,
                    item.completion_id,
                    item.status.value,
                    item.source_set_sha256,
                    item.model_dump_json(),
                    item.assessed_at,
                ),
            )
        await db.commit()


def _current_view(receipt) -> EvolutionRevalidationOptInStageCompletionView:
    return EvolutionRevalidationOptInStageCompletionView(
        receipt=receipt,
        evidence_source_current=True,
        latest_assessment=True,
        plan_source_current=True,
        baseline_source_current=True,
        liveness_source_current=True,
        outcome_set_current=True,
        invalidation_reasons=(),
        opt_in_stage_completion_authority=True,
        pause_input_authority=False,
        rollback_input_authority=False,
    )


def _answering_callback(*, authority, answer, now, observed):
    async def callback(payload):
        request = normalize_interaction_request(payload)
        observed.append((str(payload["_interaction_id"]), request))
        record = await authority.create(
            request=request,
            interaction_id=str(payload["_interaction_id"]),
            subject_kind=str(payload["_durable_subject_kind"]),
            subject_id=str(payload["_durable_subject_id"]),
            session_id="opt-in-stage-advance-test",
            agent_name="main",
            now=now.isoformat(),
        )
        record, response = await authority.answer(
            record=record,
            response={"kind": "option", "value": answer},
            now=(now + timedelta(seconds=1)).isoformat(),
        )
        return response

    return callback


async def _fixture(tmp_path: Path):
    completion, binding, samples, chat_store, _outcomes, outcome_service = (
        await _sources(tmp_path)
    )
    plan_service, baseline_service = _baseline_service(tmp_path, outcome_service)
    completion_store, completion_service = _stage_service(
        tmp_path=tmp_path,
        outcome_service=outcome_service,
        plan_service=plan_service,
        baseline_service=baseline_service,
        clock=lambda: datetime.fromisoformat(samples[-1].observed_at),
    )
    plan = (await plan_service.inspect(plan_id=completion.plan_id)).plan
    required = plan.stages[1].minimum_completed_runs
    for index in range(required):
        run = await _fixed_terminal_run(
            store=chat_store,
            binding=binding,
            outcome="completed",
            index=index,
        )
        await outcome_service.record(
            completion_id=completion.completion_id,
            subject_id=binding.subject_id,
            session_id=run.session_id,
            run_id=run.id,
        )
    insufficient = await completion_service.assess(
        completion_id=completion.completion_id,
        subject_id=binding.subject_id,
    )
    assert insufficient.receipt.status is (
        EvolutionRevalidationOptInStageCompletionStatus.INSUFFICIENT
    )
    passing = _passing_projection(insufficient.receipt)
    declined = _next_projection(passing)
    await _replace_completions(completion_store.db_path, passing, declined)
    projections = {
        passing.evidence_id: passing,
        declined.evidence_id: declined,
    }

    async def inspect_projection(*, evidence_id, subject_id):
        item = projections[evidence_id]
        assert subject_id == item.subject_id
        return _current_view(item)

    completion_service.inspect = inspect_projection
    control_store = (
        outcome_service.window_service.runtime_health_service.deployment_service
        .intent_service.candidate_service.stage_advance_service.control_store
    )
    harness_store = HarnessStore(tmp_path / ".naumi" / "harness.db")
    authority = DurableInteractionAuthorityClient(
        store=harness_store,
        workspace_root=tmp_path,
        owner_id="opt-in-stage-advance-test",
    )
    now = datetime.fromisoformat(samples[-1].observed_at) + timedelta(seconds=5)

    def build(answer, observed, offset=0):
        store = EvolutionRevalidationOptInStageAdvanceStore(
            completion_store.db_path,
            completion_service=completion_service,
            plan_service=plan_service,
            control_store=control_store,
            interaction_store=harness_store,
        )
        return EvolutionRevalidationOptInStageAdvanceService(
            workspace_root=tmp_path,
            completion_service=completion_service,
            plan_service=plan_service,
            control_store=control_store,
            interaction_store=harness_store,
            store=store,
            request_user_input=_answering_callback(
                authority=authority,
                answer=answer,
                now=now,
                observed=observed,
            ),
            clock=lambda: now + timedelta(seconds=2, microseconds=offset),
        )

    return passing, declined, plan, control_store, build, now


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实执行夹具使用 POSIX shebang")
async def test_opt_in_stage_advance_fences_manual_decisions_and_control(
    tmp_path: Path,
) -> None:
    passing, declined, plan, control_store, build, now = await _fixture(tmp_path)
    assert plan.stages[1].manual_advance_required
    observed = []
    first = build("advance", observed, 0)
    second = build("advance", observed, 1)

    views = await asyncio.gather(
        *(
            candidate.authorize(
                evidence_id=passing.evidence_id,
                subject_id=passing.subject_id,
            )
            for candidate in (first, second) * 3
        )
    )
    view = views[0]
    assert all(item == view for item in views)
    assert len({item[0] for item in observed}) == 1
    assert view.receipt.decision == "advance"
    assert view.receipt.decision_source == "manual"
    assert view.receipt.percentage_exposure_percent == plan.stages[2].exposure_percent
    assert view.next_stage_entry_authority
    assert view.percentage_stage_entry_authority
    assert not view.receipt.percentage_rollout_authority
    assert not view.receipt.deployment_authority
    assert not view.receipt.stable_rollout_authority
    assert not view.receipt.promotion_authority

    core = view.receipt.model_dump(
        mode="json", exclude={"receipt_id", "receipt_sha256"}
    )
    core["percentage_exposure_percent"] += 1
    forged_digest = _digest(core)
    forged = view.receipt.model_validate_json(
        json.dumps(
            {
                **core,
                "receipt_id": f"evreoptinadvance_{forged_digest[:24]}",
                "receipt_sha256": forged_digest,
            },
            ensure_ascii=False,
        )
    )
    with pytest.raises(EvolutionRevalidationOptInStageAdvanceError) as blocked:
        await first.store.record(forged)
    assert blocked.value.code == "opt_in_stage_advance_source_changed"

    decline_observed = []
    decline_service = build("decline", decline_observed)
    decline = await decline_service.authorize(
        evidence_id=declined.evidence_id,
        subject_id=declined.subject_id,
    )
    assert decline.receipt.decision == "decline"
    assert not decline.next_stage_entry_authority
    assert not decline.percentage_stage_entry_authority

    first.clock = lambda: datetime.fromisoformat(view.receipt.expires_at) + timedelta(
        microseconds=1
    )
    expired = await first.inspect(evidence_id=passing.evidence_id)
    assert expired.expired
    assert not expired.next_stage_entry_authority

    first.clock = lambda: now + timedelta(seconds=3)
    control = EvolutionRevalidationRolloutControlService(
        workspace_root=tmp_path,
        store=control_store,
        control_plane_key_provider=control_store._key_provider,
    )
    await control.pause(
        reason_code="operator_paused_before_percentage_entry",
        actor=EvolutionRevalidationRolloutControlActor.OPERATOR,
        changed_at=(now + timedelta(seconds=4)).isoformat(),
    )
    paused = await first.inspect(evidence_id=passing.evidence_id)
    assert not paused.control_current
    assert "rollout_control_changed" in paused.invalidation_reasons
    assert not paused.next_stage_entry_authority

def test_automatic_receipt_never_fabricates_interaction_or_rollout_authority(
    tmp_path: Path,
) -> None:
    completion = SimpleNamespace(
        subject_id="automatic-opt-in",
        evidence_id="evreoptincomplete_" + "1" * 24,
        evidence_sha256="1" * 64,
        source_set_sha256="2" * 64,
        completion_id="completion-automatic",
        plan_id="evrerolloutplan_" + "3" * 24,
        plan_sha256="3" * 64,
    )
    plan = SimpleNamespace(
        stages=(
            SimpleNamespace(),
            SimpleNamespace(manual_advance_required=False),
            SimpleNamespace(exposure_percent=10),
        )
    )
    receipt = _build_receipt(
        workspace_root=tmp_path,
        completion=completion,
        plan=plan,
        control=None,
        decision="advance",
        decision_source="automatic",
        interaction=None,
        now=datetime(2026, 8, 10, tzinfo=UTC),
        validity_seconds=3600,
    )

    assert receipt.automatic_advance_eligible
    assert receipt.interaction is None
    assert receipt.interaction_request_sha256 == ""
    assert receipt.percentage_stage_entry_authority
    assert not receipt.percentage_rollout_authority
    assert not receipt.deployment_authority
