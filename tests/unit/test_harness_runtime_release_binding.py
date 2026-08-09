from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from naumi_agent.config.settings import RuntimeHeartbeatRetentionConfig
from naumi_agent.harness.heartbeat import HarnessHeartbeat, HarnessHeartbeatPhase
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.runtime_release_binding import (
    build_runtime_release_binding,
)
from naumi_agent.harness.runtime_release_observation import (
    build_runtime_release_observation,
)
from naumi_agent.harness.store import HarnessStore, HarnessStoreConflictError
from naumi_agent.release.runtime_identity import (
    ReleaseRuntimeIdentityError,
    inspect_runtime_identity,
)
from naumi_agent.runtime.terminal_runtime import TerminalRuntimeLifecycleFactory
from tests.unit.test_release_launcher import _active_store


class _Clock:
    def __init__(self) -> None:
        self._value = datetime(2026, 8, 10, 10, 0, tzinfo=UTC)

    def now(self) -> str:
        self._value += timedelta(seconds=1)
        return self._value.isoformat()


def _managed_identity(tmp_path: Path):
    release_store = _active_store(tmp_path / "release-fixture")
    target = release_store.resolve_active_backend()
    environment = {
        "NAUMI_ACTIVE_SLOT_ID": target.slot.slot_id,
        "NAUMI_ACTIVE_POINTER_GENERATION": str(target.pointer.generation),
        "NAUMI_INSTALL_ROOT": str(release_store.release_root),
    }
    return inspect_runtime_identity(
        release_store,
        environment=environment,
        runtime_path=target.backend,
        verified_at="2026-08-10T10:00:00+00:00",
    )


def _factory(
    tmp_path: Path,
    *,
    identity_provider,
    clock: _Clock | None = None,
) -> TerminalRuntimeLifecycleFactory:
    return TerminalRuntimeLifecycleFactory(
        store=HarnessStore(tmp_path / "harness.db"),
        workspace_root=tmp_path,
        retention_config=RuntimeHeartbeatRetentionConfig(enabled=False),
        heartbeat_interval_seconds=1,
        heartbeat_timeout_seconds=3,
        now_provider=(clock or _Clock()).now,
        runtime_identity_provider=identity_provider,
    )


@pytest.mark.asyncio
async def test_new_ui_and_tui_bind_the_same_exact_managed_release(tmp_path) -> None:
    identity = _managed_identity(tmp_path)
    factory = _factory(tmp_path, identity_provider=lambda: identity)
    new_ui = factory.create(surface="new_ui", identity="new-ui-managed")
    tui = factory.create(surface="tui", identity="tui-managed")

    assert await new_ui.start()
    assert await tui.start()

    new_snapshot = new_ui.snapshot()
    tui_snapshot = tui.snapshot()
    assert new_snapshot.managed_release
    assert tui_snapshot.managed_release
    assert new_snapshot.release_identity_id == identity.identity_id
    assert tui_snapshot.release_identity_id == identity.identity_id
    assert new_snapshot.release_binding_id != tui_snapshot.release_binding_id
    assert not new_snapshot.release_binding_error_code

    new_binding = await factory.store.get_runtime_release_binding(
        workspace_root=tmp_path,
        subject_id="new-ui-managed",
    )
    tui_binding = await factory.store.get_runtime_release_binding(
        workspace_root=tmp_path,
        subject_id="tui-managed",
    )
    assert new_binding is not None and new_binding.surface == "new_ui"
    assert tui_binding is not None and tui_binding.surface == "tui"
    assert new_binding.runtime_identity == tui_binding.runtime_identity == identity
    assert new_binding.release_identity_authority
    assert not new_binding.heartbeat_liveness_authority
    assert not new_binding.rollout_observation_authority

    assert await new_ui.close()
    assert await tui.close()


@pytest.mark.asyncio
async def test_identity_failure_keeps_ui_heartbeat_without_release_authority(
    tmp_path,
) -> None:
    def fail_identity():
        raise ReleaseRuntimeIdentityError(
            "release_runtime_identity_binary_mismatch",
            "private runtime path",
        )

    factory = _factory(tmp_path, identity_provider=fail_identity)
    lifecycle = factory.create(surface="new_ui", identity="new-ui-unbound")

    assert await lifecycle.start()
    snapshot = lifecycle.snapshot()
    assert not snapshot.managed_release
    assert not snapshot.release_binding_id
    assert snapshot.release_binding_error_code == (
        "release_runtime_identity_binary_mismatch"
    )
    assert await factory.store.get_runtime_release_binding(
        workspace_root=tmp_path,
        subject_id="new-ui-unbound",
    ) is None
    heartbeat = await factory.store.get_heartbeat(
        workspace_root=tmp_path,
        subject_kind="runtime",
        subject_id="new-ui-unbound",
    )
    assert heartbeat is not None
    assert heartbeat.phase is HarnessHeartbeatPhase.RUNNING
    assert await lifecycle.close()


@pytest.mark.asyncio
async def test_binding_startup_is_idempotent_conflict_safe_and_pruned(
    tmp_path,
) -> None:
    identity = _managed_identity(tmp_path)
    store = HarnessStore(tmp_path / "harness.db")
    binding = build_runtime_release_binding(
        workspace_root=tmp_path,
        surface="tui",
        subject_id="tui-atomic-binding",
        instance_id="tui-atomic-binding",
        epoch=1,
        runtime_identity=identity,
        bound_at="2026-08-10T10:00:01+00:00",
    )
    with pytest.raises(ValueError, match="不能早于 release binding"):
        build_runtime_release_observation(
            binding=binding,
            heartbeat=HarnessHeartbeat(
                workspace_root=str(tmp_path.resolve()),
                subject_kind=HarnessRunKind.RUNTIME,
                subject_id="tui-atomic-binding",
                instance_id="tui-atomic-binding",
                epoch=1,
                sequence=1,
                phase=HarnessHeartbeatPhase.STARTING,
                observed_at="2026-08-10T10:00:00+00:00",
                timeout_seconds=3,
                detail_code="runtime_starting",
            ),
            previous_sample_sha256="",
            chain_origin_sequence=1,
            chain_origin_kind="startup",
        )
    request = {
        "binding": binding,
        "observed_at": "2026-08-10T10:00:02+00:00",
        "timeout_seconds": 3,
        "detail_code": "runtime_starting",
    }

    first = await store.record_runtime_release_binding_startup(**request)
    repeated = await store.record_runtime_release_binding_startup(**request)
    assert repeated == first

    conflicting = build_runtime_release_binding(
        workspace_root=tmp_path,
        surface="tui",
        subject_id="tui-atomic-binding",
        instance_id="tui-atomic-binding",
        epoch=1,
        runtime_identity=identity,
        bound_at="2026-08-10T10:00:01.500000+00:00",
    )
    with pytest.raises(HarnessStoreConflictError, match="不同 release identity"):
        await store.record_runtime_release_binding_startup(
            **{**request, "binding": conflicting}
        )

    orphan_subject = "tui-existing-heartbeat"
    await store.record_heartbeat(
        workspace_root=tmp_path,
        subject_kind="runtime",
        subject_id=orphan_subject,
        instance_id=orphan_subject,
        epoch=1,
        sequence=1,
        phase="starting",
        observed_at="2026-08-10T10:00:02+00:00",
        timeout_seconds=3,
        detail_code="runtime_starting",
    )
    orphan_binding = build_runtime_release_binding(
        workspace_root=tmp_path,
        surface="tui",
        subject_id=orphan_subject,
        instance_id=orphan_subject,
        epoch=1,
        runtime_identity=identity,
        bound_at="2026-08-10T10:00:01+00:00",
    )
    with pytest.raises(HarnessStoreConflictError):
        await store.record_runtime_release_binding_startup(
            binding=orphan_binding,
            observed_at="2026-08-10T10:00:02+00:00",
            timeout_seconds=3,
            detail_code="runtime_starting",
        )
    assert await store.get_runtime_release_binding(
        workspace_root=tmp_path,
        subject_id=orphan_subject,
    ) is None

    await store.record_heartbeat(
        workspace_root=tmp_path,
        subject_kind="runtime",
        subject_id="tui-atomic-binding",
        instance_id="tui-atomic-binding",
        epoch=1,
        sequence=2,
        phase="stopped",
        observed_at="2026-08-10T10:00:03+00:00",
        timeout_seconds=3,
        detail_code="runtime_stopped",
    )
    receipt = await store.prune_runtime_heartbeats(
        workspace_root=tmp_path,
        observed_before="2026-08-10T10:00:04+00:00",
        assessed_at="2026-08-10T10:00:10+00:00",
    )
    assert receipt.deleted_subject_ids == ("tui-atomic-binding",)
    assert await store.get_runtime_release_binding(
        workspace_root=tmp_path,
        subject_id="tui-atomic-binding",
    ) is None
    assert await store.get_heartbeat(
        workspace_root=tmp_path,
        subject_kind="runtime",
        subject_id="tui-atomic-binding",
    ) is None
    with sqlite3.connect(store.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM harness_runtime_release_observations "
            "WHERE subject_id = ?",
            ("tui-atomic-binding",),
        ).fetchone()[0] == 0
