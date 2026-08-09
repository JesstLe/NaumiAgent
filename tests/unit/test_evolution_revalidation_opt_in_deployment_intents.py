from __future__ import annotations

import asyncio
import base64
from datetime import timedelta
from pathlib import Path

import pytest

from naumi_agent.evolution.revalidation_opt_in_deployment_intents import (
    EvolutionRevalidationOptInDeploymentIntentError,
    EvolutionRevalidationOptInDeploymentIntentService,
    EvolutionRevalidationOptInDeploymentIntentStore,
)
from naumi_agent.evolution.revalidation_rollout_stage_entries import (
    EvolutionRevalidationRolloutControlActor,
    EvolutionRevalidationRolloutControlService,
)
from naumi_agent.harness.interaction_runtime import DurableInteractionAuthorityClient
from naumi_agent.release.build_attestations import (
    ReleaseBuildSigner,
    ReleaseTrustedBuilderKey,
    create_release_build_trust_policy,
)
from tests.unit.test_evolution_revalidation_candidate_bundle_admissions import (
    _admission_service,
)
from tests.unit.test_evolution_revalidation_local_canary_runs import T0
from tests.unit.test_evolution_revalidation_rollout_stage_advances import (
    _answering_callback,
)


async def _intent_service(
    root: Path,
    *,
    answer="enroll",
    after_answer=None,
    candidate_backend_content: bytes | None = None,
):
    (
        candidate_service,
        completion,
        bundle,
        attestation,
        slots,
        original_pointer,
        plan,
        policy_box,
        signer,
    ) = await _admission_service(
        root,
        candidate_backend_content=candidate_backend_content,
    )
    admission = await candidate_service.admit(
        completion_id=completion.completion_id,
        bundle_dir=bundle,
        build_attestation_path=attestation,
    )
    interaction_store = candidate_service.stage_advance_service.interaction_store
    authority = DurableInteractionAuthorityClient(
        store=interaction_store,
        workspace_root=root,
        owner_id="opt-in-deployment-intent-test",
    )
    now_box = [T0 + timedelta(hours=9, minutes=15)]
    observed = []
    store = EvolutionRevalidationOptInDeploymentIntentStore(
        candidate_service.store.db_path,
        interaction_store=interaction_store,
        release_root=candidate_service.release_slot_store.release_root,
    )
    service = EvolutionRevalidationOptInDeploymentIntentService(
        workspace_root=root,
        candidate_service=candidate_service,
        interaction_store=interaction_store,
        store=store,
        request_user_input=_answering_callback(
            authority=authority,
            answer=answer,
            now=now_box[0],
            observed=observed,
            after_answer=after_answer,
        ),
        clock=lambda: now_box[0] + timedelta(seconds=2),
    )
    return (
        service,
        completion,
        admission,
        slots,
        original_pointer,
        plan,
        policy_box,
        signer,
        observed,
        now_box,
    )


@pytest.mark.asyncio
async def test_explicit_opt_in_is_singleflight_durable_and_never_switches_pointer(
    tmp_path: Path,
) -> None:
    (
        service,
        completion,
        admission,
        slots,
        original_pointer,
        plan,
        _policy,
        _signer,
        observed,
        _now,
    ) = await _intent_service(tmp_path)

    views = await asyncio.gather(
        *(service.authorize(completion_id=completion.completion_id) for _ in range(8))
    )
    view = views[0]

    assert all(candidate == view for candidate in views)
    assert len(observed) == 1
    assert observed[0][0].startswith("ask-evredeploy-")
    assert observed[0][1].header == "候选版本本机 Opt-in"
    assert "active runtime" in observed[0][1].question
    assert view.activation_intent_authority
    assert view.intent.admission == admission.admission
    assert view.intent.stage_advance.next_stage == "opt_in"
    assert view.intent.cohort.stage == plan.stages[1]
    assert view.intent.cohort.stage.exposure_percent == 1
    assert view.intent.cohort.local_installation_member
    assert view.intent.cohort.explicit_user_opt_in
    assert not view.intent.cohort.population_assignment_enforced
    assert not view.intent.percentage_rollout_authority
    assert not view.intent.stable_rollout_authority
    assert not view.intent.active_pointer_switched
    assert not view.intent.deployment_receipt_authority
    assert not view.intent.process_started
    assert slots.active() == original_pointer


@pytest.mark.asyncio
async def test_declined_opt_in_is_durable_and_never_creates_intent(tmp_path: Path) -> None:
    (
        service,
        completion,
        admission,
        slots,
        original_pointer,
        _plan,
        _policy,
        _signer,
        observed,
        _now,
    ) = await _intent_service(tmp_path, answer="cancel")

    for _ in range(2):
        with pytest.raises(EvolutionRevalidationOptInDeploymentIntentError) as declined:
            await service.authorize(completion_id=completion.completion_id)
        assert declined.value.code == "deployment_opt_in_declined"

    assert len(observed) == 1
    assert await service.store.get_by_admission(admission.admission.admission_id) is None
    assert slots.active() == original_pointer


@pytest.mark.asyncio
async def test_policy_rotation_pointer_change_and_expiry_dynamically_fence_intent(
    tmp_path: Path,
) -> None:
    (
        service,
        completion,
        admission,
        slots,
        _original_pointer,
        _plan,
        policy_box,
        signer,
        _observed,
        now_box,
    ) = await _intent_service(tmp_path)
    current = await service.authorize(completion_id=completion.completion_id)
    original_policy = policy_box[0]
    rotated_signer = ReleaseBuildSigner.from_private_key_base64(
        builder_id="naumi-test-release",
        key_id="test-2026-q4",
        key_generation=2,
        private_key_base64=base64.b64encode(b"s" * 32).decode("ascii"),
    )
    policy_box[0] = create_release_build_trust_policy(
        (
            original_policy.keys[0],
            ReleaseTrustedBuilderKey(
                identity=rotated_signer.identity,
                state="active",
                valid_from="2026-07-01T00:00:00+00:00",
            ),
        )
    )

    rotated = await service.inspect(completion_id=completion.completion_id)
    assert not rotated.build_trust_current
    assert not rotated.activation_intent_authority

    policy_box[0] = original_policy
    restored = await service.inspect(completion_id=completion.completion_id)
    assert restored.activation_intent_authority

    control = EvolutionRevalidationRolloutControlService(
        workspace_root=tmp_path,
        store=service.candidate_service.stage_advance_service.control_store,
        control_plane_key_provider=(
            service.candidate_service.stage_advance_service.control_store._key_provider
        ),
    )
    await control.pause(
        reason_code="direct_store_revalidation_test",
        actor=EvolutionRevalidationRolloutControlActor.OPERATOR,
        changed_at=(T0 + timedelta(hours=9, minutes=15, seconds=3)).isoformat(),
    )
    with pytest.raises(EvolutionRevalidationOptInDeploymentIntentError) as fenced:
        await service.store.record(current.intent)
    assert fenced.value.code == "deployment_intent_dependency_changed"

    slots.activate(admission.admission.candidate_slot.slot_id)
    switched = await service.inspect(completion_id=completion.completion_id)
    assert not switched.active_pointer_current
    assert not switched.activation_intent_authority

    assert signer.identity == admission.admission.trusted_builder_key.identity
    now_box[0] = T0 + timedelta(days=2)
    expired = await service.inspect(completion_id=completion.completion_id)
    assert expired.expired
    assert not expired.activation_intent_authority
    assert current.intent == expired.intent


@pytest.mark.asyncio
async def test_authority_change_after_user_answer_prevents_intent_commit(
    tmp_path: Path,
) -> None:
    pointer_change = None

    async def after_answer():
        assert pointer_change is not None
        slots, slot_id = pointer_change
        slots.activate(slot_id)

    fixture = await _intent_service(tmp_path, after_answer=after_answer)
    service, completion, admission, slots, original_pointer = fixture[:5]
    pointer_change = (slots, admission.admission.candidate_slot.slot_id)

    with pytest.raises(EvolutionRevalidationOptInDeploymentIntentError) as changed:
        await service.authorize(completion_id=completion.completion_id)

    assert changed.value.code == "deployment_intent_admission_denied"
    assert slots.active() != original_pointer
    assert await service.store.get_by_admission(admission.admission.admission_id) is None
