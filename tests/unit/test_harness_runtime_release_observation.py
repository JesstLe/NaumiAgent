from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from naumi_agent.config.settings import RuntimeHeartbeatRetentionConfig
from naumi_agent.harness.runtime_release_observation import (
    HarnessRuntimeReleaseObservation,
)
from naumi_agent.harness.store import HarnessStore, HarnessStoreError
from naumi_agent.release.runtime_identity import inspect_runtime_identity
from naumi_agent.runtime.terminal_runtime import TerminalRuntimeLifecycleFactory
from tests.unit.test_release_launcher import _active_store


class _Clock:
    def __init__(self) -> None:
        self._value = datetime(2026, 8, 10, 11, 0, tzinfo=UTC)

    def now(self) -> str:
        self._value += timedelta(seconds=1)
        return self._value.isoformat()


def _factory(tmp_path: Path) -> TerminalRuntimeLifecycleFactory:
    release_store = _active_store(tmp_path / "release-fixture")
    target = release_store.resolve_active_backend()
    identity = inspect_runtime_identity(
        release_store,
        environment={
            "NAUMI_ACTIVE_SLOT_ID": target.slot.slot_id,
            "NAUMI_ACTIVE_POINTER_GENERATION": str(target.pointer.generation),
            "NAUMI_INSTALL_ROOT": str(release_store.release_root),
        },
        runtime_path=target.backend,
        verified_at="2026-08-10T11:00:00+00:00",
    )
    return TerminalRuntimeLifecycleFactory(
        store=HarnessStore(tmp_path / "harness.db"),
        workspace_root=tmp_path,
        retention_config=RuntimeHeartbeatRetentionConfig(enabled=False),
        heartbeat_interval_seconds=1,
        heartbeat_timeout_seconds=3,
        now_provider=_Clock().now,
        runtime_identity_provider=lambda: identity,
    )


@pytest.mark.asyncio
async def test_managed_lifecycle_appends_exact_paginated_observation_chain(
    tmp_path,
) -> None:
    factory = _factory(tmp_path)
    lifecycle = factory.create(surface="new_ui", identity="new-ui-observed")

    assert await lifecycle.start()
    await lifecycle._producer.pulse_now()
    await lifecycle._producer.enter_waiting(detail_code="waiting_for_user")
    await lifecycle._producer.pulse_now()
    await lifecycle._producer.resume_running(detail_code="user_replied")
    assert await lifecycle.begin_draining()
    assert await lifecycle.close()

    pages = []
    after_sequence = 0
    while True:
        page = await HarnessStore(factory.store.db_path).list_runtime_release_observations(
            workspace_root=tmp_path,
            subject_id="new-ui-observed",
            after_sequence=after_sequence,
            limit=3,
        )
        assert page is not None
        pages.append(page)
        if not page.has_more:
            break
        after_sequence = page.next_sequence
    items = tuple(item for page in pages for item in page.items)

    assert tuple(item.heartbeat_sequence for item in items) == tuple(range(1, 9))
    assert tuple(item.phase.value for item in items) == (
        "starting",
        "running",
        "running",
        "waiting",
        "waiting",
        "running",
        "draining",
        "stopped",
    )
    assert items[0].previous_sample_sha256 == ""
    assert items[0].chain_origin_sequence == 1
    assert items[0].chain_origin_kind == "startup"
    assert all(
        current.previous_sample_sha256 == previous.sample_sha256
        for previous, current in zip(items[:-1], items[1:], strict=True)
    )
    assert all(item.heartbeat_observation_authority for item in items)
    assert not any(item.current_liveness_authority for item in items)
    assert not any(item.rollout_observation_window_authority for item in items)
    assert len({item.binding_id for item in items}) == 1
    assert len({item.runtime_identity_id for item in items}) == 1

    with pytest.raises(ValueError, match="1 到 500"):
        await factory.store.list_runtime_release_observations(
            workspace_root=tmp_path,
            subject_id="new-ui-observed",
            limit=501,
        )
    with pytest.raises(HarnessStoreError, match="cursor"):
        await factory.store.list_runtime_release_observations(
            workspace_root=tmp_path,
            subject_id="new-ui-observed",
            after_sequence=99,
        )


@pytest.mark.asyncio
async def test_observation_failure_rolls_back_heartbeat_and_local_sequence(
    tmp_path,
) -> None:
    factory = _factory(tmp_path)
    lifecycle = factory.create(surface="tui", identity="tui-atomic-observed")
    assert await lifecycle.start()

    with sqlite3.connect(factory.store.db_path) as db:
        db.execute(
            """
            CREATE TRIGGER reject_third_runtime_observation
            BEFORE INSERT ON harness_runtime_release_observations
            WHEN NEW.heartbeat_sequence = 3
            BEGIN
                SELECT RAISE(ABORT, 'simulated observation outage');
            END
            """
        )

    with pytest.raises(HarnessStoreError, match="无法保存 Harness 心跳"):
        await lifecycle._producer.pulse_now()
    heartbeat = await factory.store.get_heartbeat(
        workspace_root=tmp_path,
        subject_kind="runtime",
        subject_id="tui-atomic-observed",
    )
    page = await factory.store.list_runtime_release_observations(
        workspace_root=tmp_path,
        subject_id="tui-atomic-observed",
    )
    assert heartbeat is not None and heartbeat.sequence == 2
    assert lifecycle._producer.sequence == 2
    assert page is not None
    assert tuple(item.heartbeat_sequence for item in page.items) == (1, 2)

    with sqlite3.connect(factory.store.db_path) as db:
        db.execute("DROP TRIGGER reject_third_runtime_observation")
    retried = await lifecycle._producer.pulse_now()
    assert retried.sequence == 3
    assert await lifecycle.close()


@pytest.mark.asyncio
async def test_observation_read_fails_closed_on_durable_tampering(tmp_path) -> None:
    factory = _factory(tmp_path)
    lifecycle = factory.create(surface="tui", identity="tui-tamper-observed")
    assert await lifecycle.start()
    assert await lifecycle.close()

    with sqlite3.connect(factory.store.db_path) as db:
        payload = db.execute(
            "SELECT sample_json FROM harness_runtime_release_observations "
            "WHERE heartbeat_sequence = 2"
        ).fetchone()[0]
        db.execute(
            "UPDATE harness_runtime_release_observations SET sample_json = ? "
            "WHERE heartbeat_sequence = 2",
            (payload.replace("runtime_ready", "runtime_alive"),),
        )

    with pytest.raises(HarnessStoreError, match="无法读取"):
        await HarnessStore(factory.store.db_path).list_runtime_release_observations(
            workspace_root=tmp_path,
            subject_id="tui-tamper-observed",
        )


@pytest.mark.asyncio
async def test_legacy_binding_freezes_only_provable_latest_baseline(tmp_path) -> None:
    factory = _factory(tmp_path)
    lifecycle = factory.create(surface="tui", identity="tui-legacy-observed")
    assert await lifecycle.start()

    with sqlite3.connect(factory.store.db_path) as db:
        db.execute("DROP TABLE harness_runtime_release_observations")
        db.execute("PRAGMA user_version = 25")

    current = await factory.store.get_heartbeat(
        workspace_root=tmp_path,
        subject_kind="runtime",
        subject_id="tui-legacy-observed",
    )
    assert current is not None and current.sequence == 2
    upgraded = HarnessStore(factory.store.db_path)
    await upgraded.record_heartbeat(
        workspace_root=current.workspace_root,
        subject_kind=current.subject_kind,
        subject_id=current.subject_id,
        instance_id=current.instance_id,
        epoch=current.epoch,
        sequence=current.sequence,
        phase=current.phase,
        observed_at=current.observed_at,
        timeout_seconds=current.timeout_seconds,
        detail_code=current.detail_code,
    )
    with sqlite3.connect(factory.store.db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 26

    pulse = await lifecycle._producer.pulse_now()
    assert pulse.sequence == 3
    page = await factory.store.list_runtime_release_observations(
        workspace_root=tmp_path,
        subject_id="tui-legacy-observed",
    )
    assert page is not None
    assert tuple(item.heartbeat_sequence for item in page.items) == (2, 3)
    assert all(item.chain_origin_sequence == 2 for item in page.items)
    assert all(item.chain_origin_kind == "legacy_snapshot" for item in page.items)
    assert page.items[0].previous_sample_sha256 == ""
    assert await lifecycle.close()


def test_observation_artifact_cannot_claim_window_authority(tmp_path) -> None:
    invalid = {
        "schema_version": 1,
        "policy_version": "harness-runtime-release-observation-v1",
        "sample_id": "hrreleaseobservation_" + "1" * 24,
        "sample_sha256": "1" * 64,
        "previous_sample_sha256": "",
        "workspace_root": str(tmp_path.resolve()),
        "binding_id": "hrreleasebinding_" + "1" * 24,
        "binding_sha256": "1" * 64,
        "runtime_identity_id": "relruntimeidentity_" + "1" * 24,
        "runtime_identity_sha256": "1" * 64,
        "surface": "tui",
        "subject_kind": "runtime",
        "subject_id": "runtime-model-boundary",
        "instance_id": "runtime-model-boundary",
        "epoch": 1,
        "chain_origin_sequence": 1,
        "chain_origin_kind": "startup",
        "heartbeat_sequence": 1,
        "phase": "running",
        "observed_at": "2026-08-10T11:00:00+00:00",
        "timeout_seconds": 3,
        "detail_code": "runtime_ready",
        "heartbeat_observation_authority": True,
        "current_liveness_authority": False,
        "rollout_observation_window_authority": True,
    }
    with pytest.raises(ValueError):
        HarnessRuntimeReleaseObservation.model_validate(invalid)
