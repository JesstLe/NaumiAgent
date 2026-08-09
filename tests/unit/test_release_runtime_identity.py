from __future__ import annotations

from pathlib import Path

import pytest

from naumi_agent.release.runtime_identity import (
    ReleaseRuntimeIdentityError,
    discover_runtime_identity,
    inspect_runtime_identity,
)
from tests.unit.test_release_launcher import _active_store


def _environment(store, target) -> dict[str, str]:
    return {
        "NAUMI_ACTIVE_SLOT_ID": target.slot.slot_id,
        "NAUMI_ACTIVE_POINTER_GENERATION": str(target.pointer.generation),
        "NAUMI_INSTALL_ROOT": str(store.release_root),
    }


def test_terminal_runtime_identity_binds_exact_active_process(tmp_path: Path) -> None:
    store = _active_store(tmp_path)
    target = store.resolve_active_backend()
    environment = _environment(store, target)

    identity = inspect_runtime_identity(
        store,
        environment=environment,
        runtime_path=target.backend,
        verified_at="2026-08-10T10:00:00+00:00",
    )
    discovered = discover_runtime_identity(
        environment=environment,
        runtime_path=target.backend,
        verified_at="2026-08-10T10:00:00+00:00",
    )

    assert discovered == identity
    assert identity.pointer_sha256 == target.pointer.pointer_sha256
    assert identity.slot_sha256 == target.slot.slot_sha256
    assert identity.binary_sha256 == target.boot_receipt.binary_sha256
    assert identity.runtime_process_started
    assert identity.terminal_session_process
    assert not identity.health_probe_process
    assert identity.invocation_kind == "terminal_session"


def test_runtime_identity_discovery_is_unmanaged_or_fails_closed(
    tmp_path: Path,
) -> None:
    assert discover_runtime_identity(
        environment={},
        runtime_path=tmp_path / "dev-python",
    ) is None

    with pytest.raises(ReleaseRuntimeIdentityError) as partial:
        discover_runtime_identity(
            environment={"NAUMI_ACTIVE_SLOT_ID": "relslot_" + "1" * 24},
            runtime_path=tmp_path / "runtime",
        )
    assert partial.value.code == "release_runtime_identity_environment_incomplete"

    store = _active_store(tmp_path)
    target = store.resolve_active_backend()
    environment = _environment(store, target)
    with pytest.raises(ReleaseRuntimeIdentityError) as wrong_binary:
        discover_runtime_identity(
            environment=environment,
            runtime_path=tmp_path / "other-runtime",
        )
    assert wrong_binary.value.code == "release_runtime_identity_binary_mismatch"
