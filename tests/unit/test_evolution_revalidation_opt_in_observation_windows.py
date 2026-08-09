from __future__ import annotations

import math
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from naumi_agent.evolution.revalidation_opt_in_observation_windows import (
    EvolutionRevalidationOptInObservationWindow,
    EvolutionRevalidationOptInObservationWindowError,
    EvolutionRevalidationOptInObservationWindowStatus,
    build_opt_in_observation_window,
)
from naumi_agent.harness.heartbeat import HarnessHeartbeat, HarnessHeartbeatPhase
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.runtime_release_binding import (
    build_runtime_release_binding,
)
from naumi_agent.harness.runtime_release_observation import (
    build_runtime_release_observation,
)
from naumi_agent.harness.store import HarnessStore
from naumi_agent.release.runtime_identity import inspect_runtime_identity
from tests.unit.test_evolution_revalidation_opt_in_runtime_health import (
    _CapturingExecutor,
    _health_service,
    _runtime_backend,
)


def _aware(value: str) -> datetime:
    return datetime.fromisoformat(value)


async def _evidence(tmp_path: Path):
    service, _health_store, _deployment, completion, slots = await _health_service(
        tmp_path,
        backend=_runtime_backend(valid_health=True),
        executor=_CapturingExecutor(),
    )
    health = await service.observe(completion_id=completion.completion_id)
    target = slots.resolve_active_backend()
    identity = inspect_runtime_identity(
        slots,
        environment={
            "NAUMI_ACTIVE_SLOT_ID": target.slot.slot_id,
            "NAUMI_ACTIVE_POINTER_GENERATION": str(target.pointer.generation),
            "NAUMI_INSTALL_ROOT": str(slots.release_root),
        },
        runtime_path=target.backend,
        verified_at=health.receipt.completed_at,
    )
    return health.receipt, identity


async def _record_chain(
    *,
    store: HarnessStore,
    workspace_root: Path,
    health,
    identity,
    subject_id: str,
    timeout_seconds: int,
    operational_samples: int,
    interval_seconds: float,
    final_phase: HarnessHeartbeatPhase = HarnessHeartbeatPhase.RUNNING,
):
    origin = _aware(health.completed_at) + timedelta(seconds=1)
    binding = build_runtime_release_binding(
        workspace_root=workspace_root,
        surface="new_ui",
        subject_id=subject_id,
        instance_id=subject_id,
        epoch=1,
        runtime_identity=identity,
        bound_at=origin.isoformat(),
    )
    await store.record_runtime_release_binding_startup(
        binding=binding,
        observed_at=origin.isoformat(),
        timeout_seconds=timeout_seconds,
        detail_code="runtime_starting",
    )
    for index in range(operational_samples):
        phase = (
            final_phase
            if index == operational_samples - 1
            else HarnessHeartbeatPhase.RUNNING
        )
        await store.record_heartbeat(
            workspace_root=workspace_root,
            subject_kind="runtime",
            subject_id=subject_id,
            instance_id=subject_id,
            epoch=1,
            sequence=index + 2,
            phase=phase,
            observed_at=(
                origin + timedelta(seconds=1 + index * interval_seconds)
            ).isoformat(),
            timeout_seconds=timeout_seconds,
            detail_code=(
                "runtime_ready"
                if phase is HarnessHeartbeatPhase.RUNNING
                else "runtime_terminal"
            ),
        )
    items = []
    after_sequence = 0
    while True:
        page = await store.list_runtime_release_observations(
            workspace_root=workspace_root,
            subject_id=subject_id,
            after_sequence=after_sequence,
            limit=500,
        )
        assert page is not None
        items.extend(page.items)
        if not page.has_more:
            break
        assert page.items and page.next_sequence > after_sequence
        after_sequence = page.next_sequence
    return binding, tuple(items)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实执行夹具使用 POSIX shebang")
async def test_exact_opt_in_runtime_window_passes_without_claiming_completed_runs(
    tmp_path: Path,
) -> None:
    health, identity = await _evidence(tmp_path)
    stage = health.deployment.intent.cohort.stage
    timeout = math.ceil(stage.minimum_observation_seconds / 10)
    minimum_samples = math.ceil(stage.minimum_observation_seconds / timeout) + 1
    store = HarnessStore(tmp_path / "harness-window.db")
    binding, samples = await _record_chain(
        store=store,
        workspace_root=tmp_path,
        health=health,
        identity=identity,
        subject_id="new-ui-opt-in-window",
        timeout_seconds=timeout,
        operational_samples=minimum_samples,
        interval_seconds=timeout,
    )

    window = build_opt_in_observation_window(
        runtime_health=health,
        binding=binding,
        samples=samples,
        assessed_at=samples[-1].observed_at,
    )

    assert window.status is EvolutionRevalidationOptInObservationWindowStatus.PASSING
    assert window.runtime_liveness_window_authority
    assert window.exposure_binding_authority
    assert window.observation_seconds >= stage.minimum_observation_seconds
    assert window.operational_sample_count >= window.minimum_sample_count
    assert window.required_completed_runs == stage.minimum_completed_runs
    assert window.completed_runs_observed == 0
    assert not window.completed_run_evidence_authority
    assert not window.opt_in_stage_completion_authority
    assert not window.percentage_rollout_authority

    stale = build_opt_in_observation_window(
        runtime_health=health,
        binding=binding,
        samples=samples,
        assessed_at=(
            _aware(samples[-1].observed_at) + timedelta(seconds=timeout + 0.1)
        ).isoformat(),
    )
    assert stale.status is EvolutionRevalidationOptInObservationWindowStatus.BREACHED
    assert stale.breach_reasons == ("heartbeat_stale",)

    insufficient = build_opt_in_observation_window(
        runtime_health=health,
        binding=binding,
        samples=samples[:3],
        assessed_at=samples[2].observed_at,
    )
    assert insufficient.status is (
        EvolutionRevalidationOptInObservationWindowStatus.INSUFFICIENT
    )
    assert set(insufficient.insufficient_reasons) == {
        "minimum_observation_seconds",
        "minimum_sample_count",
    }

    invalid = window.model_dump(mode="json")
    invalid["completed_run_evidence_authority"] = True
    with pytest.raises(ValueError):
        EvolutionRevalidationOptInObservationWindow.model_validate(invalid)

    with pytest.raises(
        EvolutionRevalidationOptInObservationWindowError,
        match="sequence/hash chain 不连续",
    ) as broken_chain:
        build_opt_in_observation_window(
            runtime_health=health,
            binding=binding,
            samples=(samples[0], samples[2]),
            assessed_at=samples[2].observed_at,
        )
    assert broken_chain.value.code == "opt_in_observation_sample_chain_broken"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实执行夹具使用 POSIX shebang")
async def test_opt_in_window_distinguishes_gap_failure_terminal_and_legacy(
    tmp_path: Path,
) -> None:
    health, identity = await _evidence(tmp_path)
    stage = health.deployment.intent.cohort.stage
    timeout = math.ceil(stage.minimum_observation_seconds / 10)
    minimum_samples = math.ceil(stage.minimum_observation_seconds / timeout) + 1
    store = HarnessStore(tmp_path / "harness-window.db")

    gap_binding, gap_samples = await _record_chain(
        store=store,
        workspace_root=tmp_path,
        health=health,
        identity=identity,
        subject_id="new-ui-opt-in-gap",
        timeout_seconds=timeout,
        operational_samples=minimum_samples,
        interval_seconds=timeout + 0.1,
    )
    gap = build_opt_in_observation_window(
        runtime_health=health,
        binding=gap_binding,
        samples=gap_samples,
        assessed_at=gap_samples[-1].observed_at,
    )
    assert gap.status is EvolutionRevalidationOptInObservationWindowStatus.BREACHED
    assert gap.breach_reasons == ("heartbeat_gap",)
    assert gap.pause_input_authority and gap.rollback_input_authority

    failed_binding, failed_samples = await _record_chain(
        store=store,
        workspace_root=tmp_path,
        health=health,
        identity=identity,
        subject_id="new-ui-opt-in-failed",
        timeout_seconds=timeout,
        operational_samples=2,
        interval_seconds=1,
        final_phase=HarnessHeartbeatPhase.FAILED,
    )
    failed = build_opt_in_observation_window(
        runtime_health=health,
        binding=failed_binding,
        samples=failed_samples,
        assessed_at=failed_samples[-1].observed_at,
    )
    assert failed.status is EvolutionRevalidationOptInObservationWindowStatus.BREACHED
    assert failed.breach_reasons == ("runtime_failed",)
    with pytest.raises(
        EvolutionRevalidationOptInObservationWindowError,
        match="exact Runtime Release Binding",
    ) as mismatched_binding:
        build_opt_in_observation_window(
            runtime_health=health,
            binding=gap_binding,
            samples=failed_samples,
            assessed_at=failed_samples[-1].observed_at,
        )
    assert mismatched_binding.value.code == (
        "opt_in_observation_sample_binding_mismatch"
    )

    stopped_binding, stopped_samples = await _record_chain(
        store=store,
        workspace_root=tmp_path,
        health=health,
        identity=identity,
        subject_id="new-ui-opt-in-stopped",
        timeout_seconds=timeout,
        operational_samples=minimum_samples + 1,
        interval_seconds=timeout,
        final_phase=HarnessHeartbeatPhase.STOPPED,
    )
    stopped = build_opt_in_observation_window(
        runtime_health=health,
        binding=stopped_binding,
        samples=stopped_samples,
        assessed_at=stopped_samples[-1].observed_at,
    )
    assert stopped.status is (
        EvolutionRevalidationOptInObservationWindowStatus.INSUFFICIENT
    )
    assert set(stopped.insufficient_reasons) == {
        "minimum_observation_seconds",
        "minimum_sample_count",
        "runtime_not_active",
    }

    reset_binding, reset_samples = await _record_chain(
        store=store,
        workspace_root=tmp_path,
        health=health,
        identity=identity,
        subject_id="new-ui-opt-in-reset",
        timeout_seconds=timeout,
        operational_samples=minimum_samples + 1,
        interval_seconds=timeout,
        final_phase=HarnessHeartbeatPhase.DRAINING,
    )
    reset_at = _aware(reset_samples[-1].observed_at)
    for offset in (1, 2):
        await store.record_heartbeat(
            workspace_root=tmp_path,
            subject_kind="runtime",
            subject_id="new-ui-opt-in-reset",
            instance_id="new-ui-opt-in-reset",
            epoch=1,
            sequence=reset_samples[-1].heartbeat_sequence + offset,
            phase=HarnessHeartbeatPhase.RUNNING,
            observed_at=(reset_at + timedelta(seconds=offset)).isoformat(),
            timeout_seconds=timeout,
            detail_code="runtime_ready",
        )
    reset_page = await store.list_runtime_release_observations(
        workspace_root=tmp_path,
        subject_id="new-ui-opt-in-reset",
        limit=500,
    )
    assert reset_page is not None and not reset_page.has_more
    reset = build_opt_in_observation_window(
        runtime_health=health,
        binding=reset_binding,
        samples=reset_page.items,
        assessed_at=reset_page.items[-1].observed_at,
    )
    assert reset.status is (
        EvolutionRevalidationOptInObservationWindowStatus.INSUFFICIENT
    )
    assert reset.operational_sample_count == 2
    assert set(reset.insufficient_reasons) == {
        "minimum_observation_seconds",
        "minimum_sample_count",
    }

    origin = _aware(health.completed_at) + timedelta(seconds=1)
    legacy_binding = build_runtime_release_binding(
        workspace_root=tmp_path,
        surface="tui",
        subject_id="tui-opt-in-legacy",
        instance_id="tui-opt-in-legacy",
        epoch=1,
        runtime_identity=identity,
        bound_at=origin.isoformat(),
    )
    heartbeat = HarnessHeartbeat(
        workspace_root=str(tmp_path.resolve()),
        subject_kind=HarnessRunKind.RUNTIME,
        subject_id="tui-opt-in-legacy",
        instance_id="tui-opt-in-legacy",
        epoch=1,
        sequence=9,
        phase=HarnessHeartbeatPhase.RUNNING,
        observed_at=origin.isoformat(),
        timeout_seconds=timeout,
        detail_code="runtime_alive",
    )
    legacy_sample = build_runtime_release_observation(
        binding=legacy_binding,
        heartbeat=heartbeat,
        previous_sample_sha256="",
        chain_origin_sequence=9,
        chain_origin_kind="legacy_snapshot",
    )
    legacy = build_opt_in_observation_window(
        runtime_health=health,
        binding=legacy_binding,
        samples=(legacy_sample,),
        assessed_at=legacy_sample.observed_at,
    )
    assert legacy.status is (
        EvolutionRevalidationOptInObservationWindowStatus.INSUFFICIENT
    )
    assert "legacy_history_origin" in legacy.insufficient_reasons
