from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest

from naumi_agent.evolution.revalidation_percentage_stage_advances import (
    EvolutionRevalidationPercentageStageAdvanceError,
    EvolutionRevalidationPercentageStageAdvanceService,
    EvolutionRevalidationPercentageStageAdvanceStore,
    _digest,
)
from naumi_agent.evolution.revalidation_percentage_stage_completions import (
    EvolutionRevalidationPercentageStageCompletion,
    EvolutionRevalidationPercentageStageCompletionView,
    _source_set_digest,
)
from naumi_agent.evolution.revalidation_rollout_stage_entries import (
    EvolutionRevalidationRolloutControlActor,
    EvolutionRevalidationRolloutControlService,
)
from naumi_agent.harness.interaction_runtime import DurableInteractionAuthorityClient
from naumi_agent.harness.store import HarnessStore
from naumi_agent.user_interaction import normalize_interaction_request
from tests.unit.test_evolution_revalidation_percentage_execution_outcome_ledger import (
    _sources,
    _terminal_run,
)
from tests.unit.test_evolution_revalidation_percentage_stage_completions import (
    _services,
)


def _passing_projection(receipt) -> EvolutionRevalidationPercentageStageCompletion:
    payload = receipt.model_dump(mode="json")
    metrics = payload["metrics"]
    metrics.update(
        baseline_cost_source="live_evidence",
        baseline_mean_cost_microusd=metrics["mean_reported_cost_microusd"],
        cost_comparable=True,
        cost_regression_basis_points=0,
        insufficient_reasons=[],
        status="passing",
    )
    payload["percentage_stage_completion_authority"] = True
    payload.pop("evidence_id")
    payload.pop("evidence_sha256")
    digest = _digest(payload)
    return EvolutionRevalidationPercentageStageCompletion.model_validate(
        {
            **payload,
            "evidence_id": f"evrepercentcomplete_{digest[:24]}",
            "evidence_sha256": digest,
        }
    )


def _next_projection(receipt):
    payload = receipt.model_dump(mode="json")
    payload.update(
        liveness_window_id="evrepercentwindow_" + "a" * 24,
        liveness_window_sha256="a" * 64,
        assessed_at=(
            datetime.fromisoformat(receipt.assessed_at) + timedelta(microseconds=1)
        ).isoformat(),
    )
    payload["source_set_sha256"] = _source_set_digest(SimpleNamespace(**payload))
    payload.pop("evidence_id")
    payload.pop("evidence_sha256")
    digest = _digest(payload)
    return EvolutionRevalidationPercentageStageCompletion.model_validate(
        {
            **payload,
            "evidence_id": f"evrepercentcomplete_{digest[:24]}",
            "evidence_sha256": digest,
        }
    )


async def _replace_completions(db_path: Path, *items) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "DELETE FROM evolution_revalidation_percentage_stage_completions"
        )
        for item in items:
            await db.execute(
                "INSERT INTO evolution_revalidation_percentage_stage_completions "
                "(evidence_id, evidence_sha256, assignment_id, status, "
                "source_set_sha256, evidence_json, assessed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    item.evidence_id,
                    item.evidence_sha256,
                    item.assignment_id,
                    item.metrics.status.value,
                    item.source_set_sha256,
                    item.model_dump_json(),
                    item.assessed_at,
                ),
            )
        await db.commit()


def _current_view(receipt) -> EvolutionRevalidationPercentageStageCompletionView:
    return EvolutionRevalidationPercentageStageCompletionView(
        receipt=receipt,
        evidence_source_current=True,
        latest_assessment=True,
        plan_source_current=True,
        baseline_source_current=True,
        liveness_source_current=True,
        outcome_set_current=True,
        invalidation_reasons=(),
        percentage_stage_completion_authority=True,
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
            session_id="percentage-stage-advance-test",
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


async def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data, lifecycle, exposure, chat_store, _outcome_store, outcome_service = (
        await _sources(tmp_path)
    )
    monkeypatch.setattr(
        "naumi_agent.runs.store._now_iso",
        lambda: (data["runtime_now"][0] - timedelta(seconds=1)).isoformat(),
    )
    plan_service, completion_store, completion_service = _services(
        tmp_path,
        outcome_service,
        lambda: data["runtime_now"][0],
    )
    plan = (
        await plan_service.inspect(
            plan_id=exposure.deployment.preparation.intent.plan.plan_id
        )
    ).plan
    required = plan.stages[2].minimum_completed_runs
    for index in range(required):
        run = await _terminal_run(
            store=chat_store,
            binding=exposure.binding,
            outcome="completed",
            index=index,
        )
        await outcome_service.record(
            assignment_id=data["assignment_id"],
            subject_id=exposure.binding.subject_id,
            session_id=run.session_id,
            run_id=run.id,
        )
    insufficient = await completion_service.assess(
        assignment_id=data["assignment_id"],
        subject_id=exposure.binding.subject_id,
    )
    passing = _passing_projection(insufficient.receipt)
    declined = _next_projection(passing)
    await _replace_completions(completion_store.db_path, passing, declined)
    projections = {passing.evidence_id: passing, declined.evidence_id: declined}

    async def inspect_projection(*, evidence_id, subject_id):
        item = projections[evidence_id]
        assert subject_id == item.subject_id
        return _current_view(item)

    completion_service.inspect = inspect_projection
    assignment_service = (
        outcome_service.window_service.exposure_service.deployment_service
        .intent_service.assignment_service
    )
    control_store = assignment_service.advance_service.control_store
    harness_store = HarnessStore(tmp_path / ".naumi" / "harness.db")
    authority = DurableInteractionAuthorityClient(
        store=harness_store,
        workspace_root=tmp_path,
        owner_id="percentage-stage-advance-test",
    )
    now = data["runtime_now"][0] + timedelta(seconds=5)

    def build(answer, observed, offset=0):
        store = EvolutionRevalidationPercentageStageAdvanceStore(
            completion_store.db_path,
            completion_service=completion_service,
            plan_service=plan_service,
            control_store=control_store,
            interaction_store=harness_store,
        )
        return EvolutionRevalidationPercentageStageAdvanceService(
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

    return lifecycle, passing, declined, plan, control_store, build, now


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实执行夹具使用 POSIX shebang")
async def test_percentage_advance_requires_durable_user_decision_and_fencing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle, passing, declined, plan, control_store, build, now = await _fixture(
        tmp_path,
        monkeypatch,
    )
    assert plan.stages[3].manual_advance_required
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
    assert view.receipt.stable_exposure_percent == 100
    assert view.receipt.interaction.answered_by == "user"
    assert not view.receipt.interaction.allow_custom
    assert view.next_stage_entry_authority
    assert view.stable_stage_entry_authority
    assert not view.receipt.stable_rollout_authority
    assert not view.receipt.deployment_authority
    assert not view.receipt.promotion_authority

    core = view.receipt.model_dump(
        mode="json",
        exclude={"receipt_id", "receipt_sha256"},
    )
    core["plan_sha256"] = "0" * 64
    forged_digest = _digest(core)
    forged = view.receipt.model_validate_json(
        json.dumps(
            {
                **core,
                "receipt_id": f"evrepercentadvance_{forged_digest[:24]}",
                "receipt_sha256": forged_digest,
            },
            ensure_ascii=False,
        )
    )
    with pytest.raises(EvolutionRevalidationPercentageStageAdvanceError) as blocked:
        await first.store.record(forged)
    assert blocked.value.code == "percentage_stage_advance_source_changed"

    decline_observed = []
    decline = await build("decline", decline_observed).authorize(
        evidence_id=declined.evidence_id,
        subject_id=declined.subject_id,
    )
    assert decline.receipt.decision == "decline"
    assert not decline.next_stage_entry_authority
    assert not decline.stable_stage_entry_authority

    first.clock = lambda: datetime.fromisoformat(view.receipt.expires_at) + timedelta(
        microseconds=1
    )
    expired = await first.inspect(evidence_id=passing.evidence_id)
    assert expired.expired
    assert not expired.stable_stage_entry_authority

    first.clock = lambda: now + timedelta(seconds=3)
    control = EvolutionRevalidationRolloutControlService(
        workspace_root=tmp_path,
        store=control_store,
        control_plane_key_provider=control_store._key_provider,
    )
    await control.pause(
        reason_code="operator_paused_before_stable_entry",
        actor=EvolutionRevalidationRolloutControlActor.OPERATOR,
        changed_at=(now + timedelta(seconds=4)).isoformat(),
    )
    paused = await first.inspect(evidence_id=passing.evidence_id)
    assert not paused.control_current
    assert "rollout_control_changed" in paused.invalidation_reasons
    assert not paused.stable_stage_entry_authority
    with pytest.raises(ValueError, match="evidence_id"):
        await first.inspect(evidence_id="' OR 1=1 --")
    assert await lifecycle.close()
