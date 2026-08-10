from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from naumi_agent.config.settings import RuntimeHeartbeatRetentionConfig
from naumi_agent.evolution.revalidation_stable_runtime_exposures import (
    EvolutionRevalidationStableRuntimeExposureError,
    EvolutionRevalidationStableRuntimeExposureService,
    EvolutionRevalidationStableRuntimeExposureStore,
)
from naumi_agent.harness.runtime_release_binding import build_runtime_release_binding
from naumi_agent.harness.store import HarnessStore
from naumi_agent.release.runtime_identity import inspect_runtime_identity
from naumi_agent.runtime.terminal_runtime import TerminalRuntimeLifecycleFactory
from tests.unit.test_evolution_revalidation_stable_boot_preparations import _BOOTABLE
from tests.unit.test_evolution_revalidation_stable_deployment_intents import _context
from tests.unit.test_evolution_revalidation_stable_deployments import (
    _deployment_service,
    _prepare,
)
from tests.unit.test_release_launcher import _active_store


async def _exposure_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    context = await _context(
        tmp_path,
        monkeypatch,
        candidate_backend_content=_BOOTABLE,
    )
    runtime_now = [context["issue_at"] + timedelta(seconds=1)]

    def clock():
        return runtime_now[0]

    intent_view, preparation_service, _ = await _prepare(
        context,
        0,
        offset=0,
        clock=clock,
        owner_id="stable-runtime-exposure-boot",
    )
    deployment_service, deployment_store = _deployment_service(
        context,
        preparation_service,
        clock,
    )
    intent_id = intent_view.intent.intent_id
    deployment_view = await deployment_service.deploy(intent_id=intent_id)
    runtime_now[0] += timedelta(seconds=1)

    target = context["slots"].resolve_active_backend()

    def next_now() -> str:
        runtime_now[0] += timedelta(seconds=1)
        return runtime_now[0].isoformat()

    identity = inspect_runtime_identity(
        context["slots"],
        environment={
            "NAUMI_ACTIVE_SLOT_ID": target.slot.slot_id,
            "NAUMI_ACTIVE_POINTER_GENERATION": str(target.pointer.generation),
            "NAUMI_INSTALL_ROOT": str(context["slots"].release_root),
        },
        runtime_path=target.backend,
        verified_at=next_now(),
    )
    harness_store = HarnessStore(tmp_path / "stable-runtime-harness.db")
    factory = TerminalRuntimeLifecycleFactory(
        store=harness_store,
        workspace_root=tmp_path,
        retention_config=RuntimeHeartbeatRetentionConfig(enabled=False),
        heartbeat_interval_seconds=10,
        heartbeat_timeout_seconds=30,
        now_provider=next_now,
        runtime_identity_provider=lambda: identity,
    )

    def build_exposure_service():
        store = EvolutionRevalidationStableRuntimeExposureStore(
            deployment_store.db_path,
            deployment_store=deployment_store,
            harness_store=harness_store,
        )
        service = EvolutionRevalidationStableRuntimeExposureService(
            workspace_root=tmp_path,
            deployment_service=deployment_service,
            harness_store=harness_store,
            store=store,
            clock=clock,
        )
        return service, store

    return {
        "context": context,
        "intent_id": intent_id,
        "deployment_view": deployment_view,
        "deployment_service": deployment_service,
        "deployment_store": deployment_store,
        "harness_store": harness_store,
        "factory": factory,
        "identity": identity,
        "runtime_now": runtime_now,
        "next_now": next_now,
        "build_exposure_service": build_exposure_service,
    }


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实 release fixture 使用 POSIX executable")
async def test_managed_terminal_startup_records_stable_exposure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = await _exposure_context(tmp_path, monkeypatch)
    lifecycle = data["factory"].create(
        surface="new_ui",
        identity="stable-runtime-primary",
    )
    assert await lifecycle.start()
    first, store = data["build_exposure_service"]()
    second, _ = data["build_exposure_service"]()

    views = await asyncio.gather(
        *(
            (first if index % 2 == 0 else second).record(
                intent_id=data["intent_id"],
                subject_id="stable-runtime-primary",
            )
            for index in range(8)
        )
    )
    view = views[0]

    assert all(item == view for item in views)
    assert view.runtime_exposure_fact_authority
    assert view.current_runtime_exposure_authority
    assert view.stable_observation_input_authority
    assert view.receipt.stable_installation_exposure_observed
    assert view.receipt.runtime_process_started
    assert view.receipt.terminal_session_process
    assert not view.receipt.user_request_executed
    assert not view.receipt.completed_run_authority
    assert not view.receipt.percentage_rollout_authority
    assert not view.receipt.stable_rollout_authority
    assert not view.receipt.promotion_authority
    assert view.receipt.startup_observation.heartbeat_sequence == 1
    assert view.receipt.ready_observation.heartbeat_sequence == 2
    assert view.receipt.binding == lifecycle.release_binding()
    assert await store.get_by_intent(data["intent_id"]) == view.receipt

    repeated = await first.record(
        intent_id=data["intent_id"],
        subject_id="stable-runtime-primary",
    )
    assert repeated == view
    with pytest.raises(EvolutionRevalidationStableRuntimeExposureError) as conflict:
        await first.record(
            intent_id=data["intent_id"],
            subject_id="stable-runtime-secondary",
        )
    assert conflict.value.code == "stable_runtime_exposure_already_recorded"

    data["runtime_now"][0] = datetime.fromisoformat(
        view.receipt.deployment.preparation.intent.expires_at
    ) + timedelta(seconds=1)
    await lifecycle._producer.pulse_now()
    after_launch_expiry = await first.inspect(intent_id=data["intent_id"])
    assert not after_launch_expiry.deployment_launch_input_current
    assert after_launch_expiry.active_deployment_current
    assert after_launch_expiry.current_runtime_exposure_authority
    assert after_launch_expiry.stable_observation_input_authority

    assert await lifecycle.close()
    stopped = await first.inspect(intent_id=data["intent_id"])
    assert stopped.runtime_exposure_fact_authority
    assert not stopped.runtime_heartbeat_current
    assert not stopped.current_runtime_exposure_authority
    assert not stopped.stable_observation_input_authority


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实 release fixture 使用 POSIX executable")
async def test_wrong_release_and_incomplete_startup_cannot_create_stable_exposure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = await _exposure_context(tmp_path, monkeypatch)
    external_store = _active_store(tmp_path / "other-stable-release")
    external_target = external_store.resolve_active_backend()
    external_identity = inspect_runtime_identity(
        external_store,
        environment={
            "NAUMI_ACTIVE_SLOT_ID": external_target.slot.slot_id,
            "NAUMI_ACTIVE_POINTER_GENERATION": str(
                external_target.pointer.generation
            ),
            "NAUMI_INSTALL_ROOT": str(external_store.release_root),
        },
        runtime_path=external_target.backend,
        verified_at=data["next_now"](),
    )
    external_factory = TerminalRuntimeLifecycleFactory(
        store=data["harness_store"],
        workspace_root=tmp_path,
        retention_config=RuntimeHeartbeatRetentionConfig(enabled=False),
        heartbeat_interval_seconds=10,
        heartbeat_timeout_seconds=30,
        now_provider=data["next_now"],
        runtime_identity_provider=lambda: external_identity,
    )
    external = external_factory.create(
        surface="tui",
        identity="stable-runtime-wrong-release",
    )
    assert await external.start()
    service, store = data["build_exposure_service"]()

    with pytest.raises(EvolutionRevalidationStableRuntimeExposureError) as wrong:
        await service.record(
            intent_id=data["intent_id"],
            subject_id="stable-runtime-wrong-release",
        )
    assert wrong.value.code == "stable_runtime_exposure_release_mismatch"
    assert await store.get_by_intent(data["intent_id"]) is None

    binding = build_runtime_release_binding(
        workspace_root=tmp_path,
        surface="new_ui",
        subject_id="stable-runtime-starting-only",
        instance_id="stable-runtime-starting-only",
        epoch=1,
        runtime_identity=data["identity"],
        bound_at=data["next_now"](),
    )
    await data["harness_store"].record_runtime_release_binding_startup(
        binding=binding,
        observed_at=data["next_now"](),
        timeout_seconds=30,
        detail_code="runtime_starting",
    )
    with pytest.raises(EvolutionRevalidationStableRuntimeExposureError) as pending:
        await service.record(
            intent_id=data["intent_id"],
            subject_id="stable-runtime-starting-only",
        )
    assert pending.value.code == "stable_runtime_exposure_not_ready"
    assert await store.get_by_intent(data["intent_id"]) is None
    assert await external.close()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实 release fixture 使用 POSIX executable")
async def test_runtime_observation_tamper_revokes_stable_exposure_fact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = await _exposure_context(tmp_path, monkeypatch)
    lifecycle = data["factory"].create(
        surface="tui",
        identity="stable-runtime-tamper",
    )
    assert await lifecycle.start()
    service, _ = data["build_exposure_service"]()
    recorded = await service.record(
        intent_id=data["intent_id"],
        subject_id="stable-runtime-tamper",
    )
    assert recorded.runtime_exposure_fact_authority
    assert await lifecycle.close()

    with sqlite3.connect(data["harness_store"].db_path) as db:
        db.execute(
            "UPDATE harness_runtime_release_observations "
            "SET sample_json = replace(sample_json, ?, ?) "
            "WHERE subject_id = ? AND heartbeat_sequence = 2",
            (
                recorded.receipt.ready_observation.sample_sha256,
                "0" * 64,
                "stable-runtime-tamper",
            ),
        )

    revoked = await service.inspect(intent_id=data["intent_id"])
    assert revoked.release_binding_current
    assert not revoked.startup_observations_current
    assert not revoked.runtime_exposure_fact_authority
    assert not revoked.current_runtime_exposure_authority
    assert "startup_observations_changed" in revoked.invalidation_reasons
