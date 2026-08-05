from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from naumi_agent.evolution.revalidation_rollout_stage_advances import (
    EvolutionRevalidationRolloutStageAdvanceError,
    EvolutionRevalidationRolloutStageAdvanceService,
    EvolutionRevalidationRolloutStageAdvanceStore,
)
from naumi_agent.evolution.revalidation_rollout_stage_completions import (
    EvolutionRevalidationRolloutStageCompletion,
)
from naumi_agent.evolution.revalidation_rollout_stage_entries import (
    EvolutionRevalidationRolloutControlActor,
)
from naumi_agent.harness.interaction_runtime import DurableInteractionAuthorityClient
from naumi_agent.harness.store import HarnessStore
from naumi_agent.user_interaction import normalize_interaction_request
from tests.unit.test_evolution_revalidation_local_canary_runs import T0
from tests.unit.test_evolution_revalidation_rollout_stage_completions import _passing


def _answering_callback(*, authority, answer, now, observed, after_answer=None):
    async def callback(payload):
        request = normalize_interaction_request(payload)
        observed.append((str(payload["_interaction_id"]), request))
        record = await authority.create(
            request=request,
            interaction_id=str(payload["_interaction_id"]),
            subject_kind=str(payload["_durable_subject_kind"]),
            subject_id=str(payload["_durable_subject_id"]),
            session_id="rollout-stage-advance-test",
            agent_name="main",
            now=now.isoformat(),
        )
        record, response = await authority.answer(
            record=record,
            response={"kind": "option", "value": answer},
            now=(now + timedelta(seconds=1)).isoformat(),
        )
        if after_answer is not None:
            await after_answer()
        return response

    return callback


async def _service(root: Path, *, answer="advance", after_answer=None):
    completion_service, observation, _executor, _entry, _parent, control, _plan = (
        await _passing(root)
    )
    completion = await completion_service.complete(
        observation_id=observation.observation_id
    )
    harness_store = HarnessStore(root / ".naumi" / "harness.db")
    authority = DurableInteractionAuthorityClient(
        store=harness_store,
        workspace_root=root,
        owner_id="rollout-stage-advance-test",
    )
    now = T0 + timedelta(hours=9, minutes=5)
    observed = []
    store = EvolutionRevalidationRolloutStageAdvanceStore(
        completion_service.store.db_path,
        interaction_store=harness_store,
    )
    service = EvolutionRevalidationRolloutStageAdvanceService(
        workspace_root=root,
        completion_service=completion_service,
        completion_store=completion_service.store,
        control_store=control.store,
        interaction_store=harness_store,
        store=store,
        request_user_input=_answering_callback(
            authority=authority,
            answer=answer,
            now=now,
            observed=observed,
            after_answer=after_answer,
        ),
        clock=lambda: now + timedelta(seconds=2),
    )
    return service, completion, control, observed


@pytest.mark.asyncio
async def test_manual_advance_is_singleflight_and_only_grants_stage_entry(
    tmp_path: Path,
) -> None:
    service, completion, _control, observed = await _service(tmp_path)

    views = await asyncio.gather(
        *(service.authorize(completion_id=completion.completion_id) for _ in range(8))
    )
    view = views[0]

    assert all(candidate == view for candidate in views)
    assert len(observed) == 1
    assert observed[0][0].startswith("ask-evrerolloutadvance-")
    assert observed[0][1].header == "自进化 Rollout 阶段推进"
    assert view.receipt.decision == "advance"
    assert view.receipt.decision_source == "manual"
    assert view.receipt.interaction is not None
    assert view.next_stage_entry_authority
    assert not view.receipt.deployment_authority
    assert not view.receipt.promotion_authority
    assert not view.receipt.git_write_executed
    assert not view.receipt.publish_executed


@pytest.mark.asyncio
async def test_manual_decline_is_durable_and_never_grants_authority(
    tmp_path: Path,
) -> None:
    service, completion, _control, observed = await _service(
        tmp_path, answer="decline"
    )

    first = await service.authorize(completion_id=completion.completion_id)
    second = await service.authorize(completion_id=completion.completion_id)

    assert first == second
    assert len(observed) == 1
    assert first.receipt.decision == "decline"
    assert not first.receipt.advance_authorized
    assert not first.receipt.next_stage_entry_authority
    assert not first.next_stage_entry_authority


@pytest.mark.asyncio
async def test_control_pause_after_answer_fences_receipt_creation(tmp_path: Path) -> None:
    control_holder = {}

    async def pause_after_answer():
        await control_holder["control"].pause(
            reason_code="operator_paused_before_stage_advance",
            actor=EvolutionRevalidationRolloutControlActor.OPERATOR,
            changed_at=(T0 + timedelta(hours=9, minutes=5, seconds=2)).isoformat(),
        )

    service, completion, control, _observed = await _service(
        tmp_path,
        after_answer=pause_after_answer,
    )
    control_holder["control"] = control

    with pytest.raises(EvolutionRevalidationRolloutStageAdvanceError) as blocked:
        await service.authorize(completion_id=completion.completion_id)

    assert blocked.value.code == "rollout_stage_advance_completion_stale"
    assert await service.store.get_by_completion(completion.completion_id) is None


@pytest.mark.asyncio
async def test_expired_receipt_loses_projected_stage_entry_authority(
    tmp_path: Path,
) -> None:
    service, completion, _control, _observed = await _service(tmp_path)
    issued = await service.authorize(completion_id=completion.completion_id)
    service.clock = lambda: datetime.fromisoformat(issued.receipt.expires_at) + timedelta(
        seconds=1
    )

    expired = await service.inspect(completion_id=completion.completion_id)

    assert expired.expired
    assert expired.receipt.next_stage_entry_authority
    assert not expired.next_stage_entry_authority


@pytest.mark.asyncio
async def test_explicit_automatic_eligibility_uses_same_receipt_without_interaction(
    tmp_path: Path,
) -> None:
    real_service, completion, _control, _observed = await _service(tmp_path)
    core = completion.model_dump(
        mode="json", exclude={"completion_id", "completion_sha256"}
    )
    core.update(
        manual_advance_required=False,
        automatic_advance_eligible=True,
        manual_interaction_required=False,
    )
    digest = hashlib.sha256(
        json.dumps(
            core,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    automatic = EvolutionRevalidationRolloutStageCompletion.model_validate(
        {
            **core,
            "completion_id": f"evrerolloutcomplete_{digest[:24]}",
            "completion_sha256": digest,
        }
    )

    class CompletionStore:
        async def get(self, completion_id):
            return automatic if completion_id == automatic.completion_id else None

    class CompletionService:
        async def complete(self, *, observation_id):
            assert observation_id == automatic.observation_id
            return automatic

    class ControlStore:
        async def latest(self, _workspace_root):
            return None

    class ReceiptStore:
        item = None

        async def get_by_completion(self, _completion_id):
            return self.item

        async def record(self, receipt):
            self.item = receipt
            return receipt

    async def must_not_ask(_payload):
        raise AssertionError("automatic stage advance 不应创建用户交互")

    service = EvolutionRevalidationRolloutStageAdvanceService(
        workspace_root=tmp_path,
        completion_service=CompletionService(),
        completion_store=CompletionStore(),
        control_store=ControlStore(),
        interaction_store=real_service.interaction_store,
        store=ReceiptStore(),
        request_user_input=must_not_ask,
        clock=lambda: T0 + timedelta(hours=9, minutes=10),
    )

    view = await service.authorize(completion_id=automatic.completion_id)

    assert view.receipt.decision_source == "automatic"
    assert view.receipt.interaction is None
    assert view.next_stage_entry_authority
    assert not view.receipt.deployment_authority


@pytest.mark.asyncio
async def test_store_rejects_receipt_with_forged_completion_projection(
    tmp_path: Path,
) -> None:
    service, completion, _control, _observed = await _service(tmp_path)
    issued = (await service.authorize(completion_id=completion.completion_id)).receipt
    core = issued.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
    core["plan_id"] = "evrerolloutplan_" + ("0" * 24)
    digest = hashlib.sha256(
        json.dumps(
            core,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    forged = issued.model_validate_json(
        json.dumps(
            {
                **core,
                "receipt_id": f"evrerolloutadvance_{digest[:24]}",
                "receipt_sha256": digest,
            },
            ensure_ascii=False,
        )
    )

    with pytest.raises(EvolutionRevalidationRolloutStageAdvanceError) as blocked:
        await service.store.record(forged)

    assert blocked.value.code == "rollout_stage_advance_dependency_changed"
