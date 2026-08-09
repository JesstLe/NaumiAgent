from __future__ import annotations

import asyncio
import math
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from naumi_agent.evolution.revalidation_opt_in_observation_window_assessments import (
    EvolutionRevalidationOptInObservationWindowService,
    EvolutionRevalidationOptInObservationWindowStore,
    _read_current_ledger,
)
from naumi_agent.evolution.revalidation_opt_in_observation_windows import (
    EvolutionRevalidationOptInObservationWindowStatus,
)
from naumi_agent.harness.heartbeat import HarnessHeartbeat, HarnessHeartbeatPhase
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.runtime_release_observation import (
    RuntimeReleaseObservationPage,
    build_runtime_release_observation,
)
from naumi_agent.harness.store import HarnessStore
from naumi_agent.release.runtime_identity import inspect_runtime_identity
from tests.unit.test_evolution_revalidation_opt_in_observation_windows import (
    _record_chain,
)
from tests.unit.test_evolution_revalidation_opt_in_runtime_health import (
    _CapturingExecutor,
    _health_service,
    _runtime_backend,
)


def _aware(value: str) -> datetime:
    return datetime.fromisoformat(value)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实执行夹具使用 POSIX shebang")
async def test_durable_window_revalidates_new_failure_and_pointer_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    health_service, health_store, _deployment, completion, slots = (
        await _health_service(
            tmp_path,
            backend=_runtime_backend(valid_health=True),
            executor=_CapturingExecutor(),
        )
    )
    health_view = await health_service.observe(
        completion_id=completion.completion_id
    )
    target = slots.resolve_active_backend()
    identity = inspect_runtime_identity(
        slots,
        environment={
            "NAUMI_ACTIVE_SLOT_ID": target.slot.slot_id,
            "NAUMI_ACTIVE_POINTER_GENERATION": str(target.pointer.generation),
            "NAUMI_INSTALL_ROOT": str(slots.release_root),
        },
        runtime_path=target.backend,
        verified_at=health_view.receipt.completed_at,
    )
    stage = health_view.receipt.deployment.intent.cohort.stage
    timeout = max(3, math.ceil(stage.minimum_observation_seconds / 500))
    minimum_samples = math.ceil(stage.minimum_observation_seconds / timeout) + 1
    paged_samples = max(501, minimum_samples)
    harness_store = HarnessStore(tmp_path / "harness-window-assessment.db")
    binding, samples = await _record_chain(
        store=harness_store,
        workspace_root=tmp_path,
        health=health_view.receipt,
        identity=identity,
        subject_id="new-ui-opt-in-assessment",
        timeout_seconds=timeout,
        operational_samples=paged_samples,
        interval_seconds=timeout,
    )
    now = [_aware(samples[-1].observed_at)]
    window_store = EvolutionRevalidationOptInObservationWindowStore(
        health_store.db_path,
        runtime_health_store=health_store,
        harness_store=harness_store,
    )
    service = EvolutionRevalidationOptInObservationWindowService(
        workspace_root=tmp_path,
        runtime_health_service=health_service,
        harness_store=harness_store,
        store=window_store,
        clock=lambda: now[0],
    )

    views = await asyncio.gather(
        *(
            service.assess(
                completion_id=completion.completion_id,
                subject_id=binding.subject_id,
            )
            for _ in range(6)
        )
    )
    view = views[0]
    assert all(item == view for item in views)
    assert view.receipt.status is (
        EvolutionRevalidationOptInObservationWindowStatus.PASSING
    )
    assert view.receipt.sample_count > 500
    assert view.current_assessment == view.receipt
    assert view.latest_receipt_current
    assert view.observation_ledger_current
    assert view.runtime_liveness_window_authority
    assert not view.completed_run_evidence_authority
    assert not view.opt_in_stage_completion_authority
    assert await window_store.get(view.receipt.window_id) == view.receipt
    assert (
        await window_store.latest(
            completion_id=completion.completion_id,
            subject_id=binding.subject_id,
        )
        == view.receipt
    )

    now[0] += timedelta(seconds=1)
    no_new_evidence = await service.assess(
        completion_id=completion.completion_id,
        subject_id=binding.subject_id,
    )
    assert no_new_evidence.receipt == view.receipt
    assert no_new_evidence.current_assessment is not None
    assert no_new_evidence.current_assessment.assessed_at != view.receipt.assessed_at
    assert no_new_evidence.runtime_liveness_window_authority

    failed_at = _aware(samples[-1].observed_at) + timedelta(seconds=1)
    await harness_store.record_heartbeat(
        workspace_root=tmp_path,
        subject_kind="runtime",
        subject_id=binding.subject_id,
        instance_id=binding.instance_id,
        epoch=binding.epoch,
        sequence=samples[-1].heartbeat_sequence + 1,
        phase=HarnessHeartbeatPhase.FAILED,
        observed_at=failed_at.isoformat(),
        timeout_seconds=timeout,
        detail_code="runtime_failed",
    )
    now[0] = failed_at
    dynamically_breached = await service.inspect(
        completion_id=completion.completion_id,
        subject_id=binding.subject_id,
    )
    assert dynamically_breached.receipt == view.receipt
    assert dynamically_breached.current_assessment is not None
    assert dynamically_breached.current_assessment.status is (
        EvolutionRevalidationOptInObservationWindowStatus.BREACHED
    )
    assert dynamically_breached.current_assessment.breach_reasons == (
        "runtime_failed",
    )
    assert not dynamically_breached.runtime_liveness_window_authority
    assert dynamically_breached.pause_input_authority
    assert dynamically_breached.rollback_input_authority

    persisted_breach = await service.assess(
        completion_id=completion.completion_id,
        subject_id=binding.subject_id,
    )
    assert persisted_breach.receipt.status is (
        EvolutionRevalidationOptInObservationWindowStatus.BREACHED
    )
    assert persisted_breach.pause_input_authority
    old_view = await service.inspect_receipt(
        window_id=view.receipt.window_id,
    )
    assert not old_view.latest_receipt_current
    assert "newer_window_exists" in old_view.invalidation_reasons
    assert not old_view.pause_input_authority

    slots.rollback()
    after_pointer_drift = await service.inspect(
        completion_id=completion.completion_id,
        subject_id=binding.subject_id,
    )
    assert not after_pointer_drift.active_deployment_authority
    assert not after_pointer_drift.runtime_health_authority
    assert not after_pointer_drift.runtime_liveness_window_authority
    assert not after_pointer_drift.pause_input_authority
    assert "active_deployment_stale" in after_pointer_drift.invalidation_reasons

    long_samples = list(samples)
    previous = samples[-1]
    observed_at = _aware(previous.observed_at)
    final_heartbeat = None
    for sequence in range(previous.heartbeat_sequence + 1, 5_503):
        observed_at += timedelta(seconds=1)
        final_heartbeat = HarnessHeartbeat(
            workspace_root=binding.workspace_root,
            subject_kind=HarnessRunKind.RUNTIME,
            subject_id=binding.subject_id,
            instance_id=binding.instance_id,
            epoch=binding.epoch,
            sequence=sequence,
            phase=HarnessHeartbeatPhase.RUNNING,
            observed_at=observed_at.isoformat(),
            timeout_seconds=timeout,
            detail_code="runtime_ready",
        )
        previous = build_runtime_release_observation(
            binding=binding,
            heartbeat=final_heartbeat,
            previous_sample_sha256=previous.sample_sha256,
            chain_origin_sequence=1,
            chain_origin_kind="startup",
        )
        long_samples.append(previous)
    assert final_heartbeat is not None and len(long_samples) > 5_000

    async def fake_binding(**_kwargs):
        return binding

    async def fake_heartbeat(**_kwargs):
        return final_heartbeat

    async def fake_page(*, after_sequence: int, limit: int, **_kwargs):
        candidates = [
            item
            for item in long_samples
            if item.heartbeat_sequence > after_sequence
        ]
        items = tuple(candidates[:limit])
        return RuntimeReleaseObservationPage(
            workspace_root=binding.workspace_root,
            subject_id=binding.subject_id,
            binding_id=binding.binding_id,
            binding_sha256=binding.binding_sha256,
            after_sequence=after_sequence,
            items=items,
            next_sequence=(items[-1].heartbeat_sequence if len(candidates) > limit else 0),
        )

    monkeypatch.setattr(harness_store, "get_runtime_release_binding", fake_binding)
    monkeypatch.setattr(harness_store, "get_heartbeat", fake_heartbeat)
    monkeypatch.setattr(
        harness_store,
        "list_runtime_release_observations",
        fake_page,
    )
    suffix_binding, suffix = await _read_current_ledger(
        harness_store=harness_store,
        workspace_root=tmp_path,
        subject_id=binding.subject_id,
    )
    assert suffix_binding == binding
    assert len(suffix) == 5_000
    assert suffix[-1].heartbeat_sequence == final_heartbeat.sequence
    assert suffix[0].heartbeat_sequence == final_heartbeat.sequence - 4_999
