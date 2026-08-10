from __future__ import annotations

import asyncio
import math
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from naumi_agent.evolution.revalidation_stable_observation_window_assessments import (
    EvolutionRevalidationStableObservationAssessmentError,
    EvolutionRevalidationStableObservationWindowService,
    EvolutionRevalidationStableObservationWindowStore,
    _read_current_ledger,
)
from naumi_agent.evolution.revalidation_stable_observation_windows import (
    EvolutionRevalidationStableObservationWindowStatus,
    build_stable_observation_window,
)
from naumi_agent.harness.heartbeat import HarnessHeartbeat, HarnessHeartbeatPhase
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.runtime_release_observation import (
    RuntimeReleaseObservationPage,
    build_runtime_release_observation,
)
from tests.unit.test_evolution_revalidation_stable_observation_windows import (
    _all_samples,
    _started_exposure,
)


def _aware(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _assessment_service(data, *, exposure_service, exposure_store):
    store = EvolutionRevalidationStableObservationWindowStore(
        exposure_store.db_path,
        exposure_store=exposure_store,
        harness_store=data["harness_store"],
    )
    service = EvolutionRevalidationStableObservationWindowService(
        workspace_root=exposure_service.workspace_root,
        exposure_service=exposure_service,
        harness_store=data["harness_store"],
        store=store,
        clock=lambda: data["runtime_now"][0],
    )
    return service, store


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实 release fixture 使用 POSIX executable")
async def test_durable_stable_window_revalidates_failure_and_pointer_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject_id = "stable-assessment-primary"
    data, lifecycle, exposure = await _started_exposure(
        tmp_path,
        monkeypatch,
        subject_id=subject_id,
    )
    stage = exposure.deployment.preparation.intent.plan.stages[3]
    timeout = exposure.ready_observation.timeout_seconds
    minimum_samples = math.ceil(stage.minimum_observation_seconds / timeout) + 1
    for _ in range(minimum_samples - 1):
        data["runtime_now"][0] += timedelta(seconds=timeout - 1)
        await lifecycle._producer.pulse_now()

    exposure_service_a, exposure_store_a = data["build_exposure_service"]()
    exposure_service_b, exposure_store_b = data["build_exposure_service"]()
    service_a, store_a = _assessment_service(
        data,
        exposure_service=exposure_service_a,
        exposure_store=exposure_store_a,
    )
    service_b, _store_b = _assessment_service(
        data,
        exposure_service=exposure_service_b,
        exposure_store=exposure_store_b,
    )
    intent_id = data["intent_id"]
    views = await asyncio.gather(
        *(
            (service_a if index % 2 == 0 else service_b).assess(
                intent_id=intent_id,
                subject_id=subject_id,
            )
            for index in range(6)
        )
    )
    view = views[0]
    assert all(item == view for item in views)
    assert view.receipt.status is (
        EvolutionRevalidationStableObservationWindowStatus.PASSING
    )
    assert view.current_assessment == view.receipt
    assert view.window_source_current and view.latest_receipt_current
    assert view.exposure_source_current and view.exposure_fact_authority
    assert view.active_deployment_authority
    assert not view.deployment_launch_input_authority
    assert view.observation_ledger_current
    assert view.stable_runtime_window_authority
    assert not view.completed_run_evidence_authority
    assert not view.population_observation_authority
    assert not view.stable_stage_completion_authority
    assert await store_a.get(view.receipt.window_id) == view.receipt
    assert (
        await store_a.latest(
            intent_id=intent_id,
            subject_id=subject_id,
        )
        == view.receipt
    )

    data["runtime_now"][0] += timedelta(seconds=1)
    unchanged = await service_a.assess(
        intent_id=intent_id,
        subject_id=subject_id,
    )
    assert unchanged.receipt == view.receipt
    assert unchanged.current_assessment is not None
    assert unchanged.current_assessment.assessed_at != view.receipt.assessed_at
    assert unchanged.stable_runtime_window_authority

    healthy_now = data["runtime_now"][0]
    data["runtime_now"][0] += timedelta(seconds=timeout + 1)
    stale = await service_a.inspect(
        intent_id=intent_id,
        subject_id=subject_id,
    )
    assert stale.current_assessment is not None
    assert stale.current_assessment.breach_reasons == ("heartbeat_stale",)
    assert not stale.stable_runtime_window_authority
    assert stale.pause_input_authority and stale.rollback_input_authority
    data["runtime_now"][0] = healthy_now

    assert await lifecycle.close(failed=True)
    breached = await service_a.inspect(
        intent_id=intent_id,
        subject_id=subject_id,
    )
    assert breached.receipt == view.receipt
    assert breached.current_assessment is not None
    assert breached.current_assessment.status is (
        EvolutionRevalidationStableObservationWindowStatus.BREACHED
    )
    assert breached.current_assessment.breach_reasons == ("runtime_failed",)
    assert not breached.current_runtime_exposure_authority
    assert not breached.stable_runtime_window_authority
    assert breached.pause_input_authority and breached.rollback_input_authority

    persisted_breach = await service_a.assess(
        intent_id=intent_id,
        subject_id=subject_id,
    )
    assert persisted_breach.receipt.status is (
        EvolutionRevalidationStableObservationWindowStatus.BREACHED
    )
    old = await service_a.inspect_receipt(window_id=view.receipt.window_id)
    assert not old.latest_receipt_current
    assert "newer_window_exists" in old.invalidation_reasons
    assert not old.pause_input_authority

    data["context"]["slots"].rollback()
    drifted = await service_a.inspect(
        intent_id=intent_id,
        subject_id=subject_id,
    )
    assert not drifted.deployment_launch_input_authority
    assert not drifted.active_deployment_authority
    assert not drifted.stable_runtime_window_authority
    assert not drifted.pause_input_authority
    assert "active_deployment_stale" in drifted.invalidation_reasons

    with sqlite3.connect(store_a.db_path) as db:
        db.execute(
            "UPDATE evolution_revalidation_stable_observation_windows "
            "SET window_json = replace(window_json, ?, ?) WHERE window_id = ?",
            (
                persisted_breach.receipt.window_sha256,
                "0" * 64,
                persisted_breach.receipt.window_id,
            ),
        )
    with pytest.raises(
        EvolutionRevalidationStableObservationAssessmentError,
    ) as tampered:
        await store_a.get(persisted_breach.receipt.window_id)
    assert tampered.value.code == "stable_observation_window_source_invalid"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实 release fixture 使用 POSIX executable")
async def test_stable_assessment_reads_verified_bounded_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject_id = "stable-assessment-suffix"
    data, lifecycle, exposure = await _started_exposure(
        tmp_path,
        monkeypatch,
        subject_id=subject_id,
    )
    initial = await _all_samples(
        store=data["harness_store"],
        workspace_root=tmp_path,
        subject_id=subject_id,
    )
    binding = exposure.binding
    samples = list(initial)
    previous = samples[-1]
    observed_at = _aware(previous.observed_at)
    final_heartbeat = None
    for sequence in range(previous.heartbeat_sequence + 1, 5_503):
        observed_at += timedelta(seconds=3)
        final_heartbeat = HarnessHeartbeat(
            workspace_root=binding.workspace_root,
            subject_kind=HarnessRunKind.RUNTIME,
            subject_id=binding.subject_id,
            instance_id=binding.instance_id,
            epoch=binding.epoch,
            sequence=sequence,
            phase=HarnessHeartbeatPhase.RUNNING,
            observed_at=observed_at.isoformat(),
            timeout_seconds=initial[0].timeout_seconds,
            detail_code="runtime_alive",
        )
        previous = build_runtime_release_observation(
            binding=binding,
            heartbeat=final_heartbeat,
            previous_sample_sha256=previous.sample_sha256,
            chain_origin_sequence=1,
            chain_origin_kind="startup",
        )
        samples.append(previous)
    assert final_heartbeat is not None and len(samples) > 5_000

    async def fake_binding(**_kwargs):
        return binding

    async def fake_heartbeat(**_kwargs):
        return final_heartbeat

    async def fake_page(*, after_sequence: int, limit: int, **_kwargs):
        candidates = [
            item for item in samples if item.heartbeat_sequence > after_sequence
        ]
        items = tuple(candidates[:limit])
        return RuntimeReleaseObservationPage(
            workspace_root=binding.workspace_root,
            subject_id=binding.subject_id,
            binding_id=binding.binding_id,
            binding_sha256=binding.binding_sha256,
            after_sequence=after_sequence,
            items=items,
            next_sequence=(
                items[-1].heartbeat_sequence if len(candidates) > limit else 0
            ),
        )

    monkeypatch.setattr(
        data["harness_store"],
        "get_runtime_release_binding",
        fake_binding,
    )
    monkeypatch.setattr(data["harness_store"], "get_heartbeat", fake_heartbeat)
    monkeypatch.setattr(
        data["harness_store"],
        "list_runtime_release_observations",
        fake_page,
    )
    current_binding, suffix = await _read_current_ledger(
        harness_store=data["harness_store"],
        workspace_root=tmp_path,
        subject_id=subject_id,
    )
    assert current_binding == binding
    assert len(suffix) == 5_000
    assert suffix[0].heartbeat_sequence == final_heartbeat.sequence - 4_999
    assert suffix[-1].heartbeat_sequence == final_heartbeat.sequence

    window = build_stable_observation_window(
        exposure=exposure,
        samples=suffix,
        assessed_at=suffix[-1].observed_at,
    )
    assert window.sample_scope == "suffix"
    assert window.suffix_anchor_sha256 == suffix[0].previous_sample_sha256
    assert window.status is EvolutionRevalidationStableObservationWindowStatus.PASSING
    assert window.stable_runtime_window_authority
    assert len(window.model_dump_json().encode()) < 8 * 1024 * 1024
    assert await lifecycle.close()
