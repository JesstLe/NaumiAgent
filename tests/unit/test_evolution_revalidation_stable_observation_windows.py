from __future__ import annotations

import math
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from naumi_agent.evolution.revalidation_stable_observation_windows import (
    EvolutionRevalidationStableObservationWindow,
    EvolutionRevalidationStableObservationWindowError,
    EvolutionRevalidationStableObservationWindowStatus,
    build_stable_observation_window,
)
from tests.unit.test_evolution_revalidation_stable_runtime_exposures import (
    _exposure_context,
)


async def _all_samples(*, store, workspace_root: Path, subject_id: str):
    samples = []
    after_sequence = 0
    while True:
        page = await store.list_runtime_release_observations(
            workspace_root=workspace_root,
            subject_id=subject_id,
            after_sequence=after_sequence,
            limit=500,
        )
        assert page is not None
        samples.extend(page.items)
        if not page.has_more:
            return tuple(samples)
        assert page.items and page.next_sequence > after_sequence
        after_sequence = page.next_sequence


async def _started_exposure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    subject_id: str,
):
    data = await _exposure_context(tmp_path, monkeypatch)
    lifecycle = data["factory"].create(
        surface="new_ui",
        identity=subject_id,
    )
    lifecycle._producer._auto_pulse = False
    assert await lifecycle.start()
    service, _ = data["build_exposure_service"]()
    exposure = await service.record(
        intent_id=data["intent_id"],
        subject_id=subject_id,
    )
    return data, lifecycle, exposure.receipt


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实 release fixture 使用 POSIX executable")
async def test_stable_window_uses_exact_exposure_chain_and_runtime_duration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject_id = "stable-window-primary"
    data, lifecycle, exposure = await _started_exposure(
        tmp_path,
        monkeypatch,
        subject_id=subject_id,
    )
    samples = await _all_samples(
        store=data["harness_store"],
        workspace_root=tmp_path,
        subject_id=subject_id,
    )

    insufficient = build_stable_observation_window(
        exposure=exposure,
        samples=samples,
        assessed_at=samples[-1].observed_at,
    )
    assert insufficient.status is (
        EvolutionRevalidationStableObservationWindowStatus.INSUFFICIENT
    )
    assert set(insufficient.insufficient_reasons) == {
        "minimum_observation_seconds",
        "minimum_sample_count",
    }
    assert insufficient.installation_exposure_binding_authority
    assert insufficient.sample_scope == "origin"
    assert insufficient.suffix_anchor_sha256 == ""
    assert insufficient.exposure_percent == 100
    assert insufficient.population_snapshot_id == (
        exposure.deployment.preparation.intent.population_snapshot_id
    )
    assert insufficient.installation_member_id == (
        exposure.deployment.preparation.intent.proof.payload.member_id
    )
    assert not insufficient.completed_run_evidence_authority
    assert not insufficient.stable_stage_completion_authority
    with pytest.raises(EvolutionRevalidationStableObservationWindowError) as bounded:
        build_stable_observation_window(
            exposure=exposure,
            samples=samples[:1],
            assessed_at=samples[-1].observed_at,
        )
    assert bounded.value.code == "stable_observation_sample_count_invalid"

    stage = exposure.deployment.preparation.intent.plan.stages[3]
    timeout = samples[0].timeout_seconds
    minimum_samples = math.ceil(stage.minimum_observation_seconds / timeout) + 1
    for _ in range(minimum_samples - 1):
        data["runtime_now"][0] += timedelta(seconds=timeout - 1)
        await lifecycle._producer.pulse_now()

    samples = await _all_samples(
        store=data["harness_store"],
        workspace_root=tmp_path,
        subject_id=subject_id,
    )
    window = build_stable_observation_window(
        exposure=exposure,
        samples=samples,
        assessed_at=samples[-1].observed_at,
    )
    assert window.status is EvolutionRevalidationStableObservationWindowStatus.PASSING
    assert window.stable_runtime_window_authority
    assert window.operational_sample_count == minimum_samples
    assert window.observation_seconds == stage.minimum_observation_seconds
    assert window.required_completed_runs == stage.minimum_completed_runs
    assert window.completed_runs_observed == 0
    assert not window.population_observation_authority
    assert not window.completed_run_evidence_authority
    assert not window.stable_stage_completion_authority
    assert not window.stable_rollout_authority
    assert not window.promotion_authority

    stale = build_stable_observation_window(
        exposure=exposure,
        samples=samples,
        assessed_at=(
            datetime.fromisoformat(samples[-1].observed_at)
            + timedelta(seconds=timeout, microseconds=1)
        ).isoformat(),
    )
    assert stale.status is EvolutionRevalidationStableObservationWindowStatus.BREACHED
    assert stale.breach_reasons == ("heartbeat_stale",)
    assert stale.pause_input_authority and stale.rollback_input_authority

    with pytest.raises(
        EvolutionRevalidationStableObservationWindowError,
        match="sequence/hash chain 不连续",
    ) as broken:
        build_stable_observation_window(
            exposure=exposure,
            samples=(*samples[:10], *samples[11:]),
            assessed_at=samples[-1].observed_at,
        )
    assert broken.value.code == "stable_observation_sample_chain_broken"

    tampered = window.model_dump(mode="json")
    tampered["completed_run_evidence_authority"] = True
    with pytest.raises(ValueError):
        EvolutionRevalidationStableObservationWindow.model_validate(tampered)

    assert await lifecycle.close()
    stopped_samples = await _all_samples(
        store=data["harness_store"],
        workspace_root=tmp_path,
        subject_id=subject_id,
    )
    stopped = build_stable_observation_window(
        exposure=exposure,
        samples=stopped_samples,
        assessed_at=stopped_samples[-1].observed_at,
    )
    assert stopped.status is (
        EvolutionRevalidationStableObservationWindowStatus.INSUFFICIENT
    )
    assert set(stopped.insufficient_reasons) == {
        "minimum_observation_seconds",
        "minimum_sample_count",
        "runtime_not_active",
    }
    assert not stopped.stable_runtime_window_authority


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实 release fixture 使用 POSIX executable")
async def test_stable_window_rejects_other_runtime_and_breaches_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject_id = "stable-window-failed"
    data, lifecycle, exposure = await _started_exposure(
        tmp_path,
        monkeypatch,
        subject_id=subject_id,
    )

    other = data["factory"].create(
        surface="tui",
        identity="stable-window-other",
    )
    other._producer._auto_pulse = False
    assert await other.start()
    other_samples = await _all_samples(
        store=data["harness_store"],
        workspace_root=tmp_path,
        subject_id="stable-window-other",
    )
    with pytest.raises(
        EvolutionRevalidationStableObservationWindowError,
        match="exact Exposure startup pair 或连续 suffix",
    ) as mismatch:
        build_stable_observation_window(
            exposure=exposure,
            samples=other_samples,
            assessed_at=other_samples[-1].observed_at,
        )
    assert mismatch.value.code == "stable_observation_origin_mismatch"
    assert await other.close()

    assert await lifecycle.close(failed=True)
    failed_samples = await _all_samples(
        store=data["harness_store"],
        workspace_root=tmp_path,
        subject_id=subject_id,
    )
    failed = build_stable_observation_window(
        exposure=exposure,
        samples=failed_samples,
        assessed_at=failed_samples[-1].observed_at,
    )
    assert failed.status is EvolutionRevalidationStableObservationWindowStatus.BREACHED
    assert failed.breach_reasons == ("runtime_failed",)
    assert failed.pause_input_authority and failed.rollback_input_authority
