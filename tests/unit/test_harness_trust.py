from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from naumi_agent.harness.trust import (
    HarnessTrustStore,
    resolve_harness_trust_db_path,
)


def test_default_trust_database_uses_user_state_not_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_home = tmp_path / "user-state"
    monkeypatch.setenv("NAUMI_STATE_HOME", str(state_home))

    path = resolve_harness_trust_db_path()

    assert path == (state_home / "harness-trust.db").resolve()
    assert not path.is_relative_to(workspace)


@pytest.mark.asyncio
async def test_reading_uninitialized_store_does_not_create_database(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state" / "harness-trust.db"
    store = HarnessTrustStore(db_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    assert await store.get(workspace) is None
    assert not await store.is_trusted(workspace, "a" * 64)
    assert not await store.untrust(workspace)
    assert not db_path.exists()


@pytest.mark.asyncio
async def test_trust_persists_across_store_instances(tmp_path: Path) -> None:
    db_path = tmp_path / "harness-trust.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = HarnessTrustStore(db_path)

    record = await first.trust(workspace, "a" * 64, source="user_slash")

    second = HarnessTrustStore(db_path)
    assert record.workspace_root == str(workspace.resolve())
    assert record.profile_digest == "a" * 64
    assert record.source == "user_slash"
    assert await second.is_trusted(workspace, "a" * 64)


@pytest.mark.asyncio
async def test_digest_change_invalidates_and_new_trust_replaces_record(
    tmp_path: Path,
) -> None:
    store = HarnessTrustStore(tmp_path / "trust.db")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    await store.trust(workspace, "a" * 64, source="user_slash")

    assert not await store.is_trusted(workspace, "b" * 64)

    replacement = await store.trust(workspace, "b" * 64, source="user_slash")
    current = await store.get(workspace)
    assert replacement.profile_digest == "b" * 64
    assert current == replacement
    assert not await store.is_trusted(workspace, "a" * 64)


@pytest.mark.asyncio
async def test_trust_is_scoped_to_canonical_workspace(tmp_path: Path) -> None:
    store = HarnessTrustStore(tmp_path / "trust.db")
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    await store.trust(first, "a" * 64, source="user_slash")

    assert await store.is_trusted(first / ".", "a" * 64)
    assert not await store.is_trusted(second, "a" * 64)


@pytest.mark.asyncio
async def test_untrust_is_idempotent(tmp_path: Path) -> None:
    store = HarnessTrustStore(tmp_path / "trust.db")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    await store.trust(workspace, "a" * 64, source="user_slash")

    assert await store.untrust(workspace)
    assert not await store.untrust(workspace)
    assert await store.get(workspace) is None


@pytest.mark.asyncio
async def test_repeated_same_trust_is_idempotent(tmp_path: Path) -> None:
    store = HarnessTrustStore(tmp_path / "trust.db")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    first = await store.trust(workspace, "a" * 64, source="user_slash")
    second = await store.trust(workspace, "a" * 64, source="user_slash")

    assert second == first


@pytest.mark.asyncio
async def test_concurrent_trust_writes_leave_one_complete_record(tmp_path: Path) -> None:
    store = HarnessTrustStore(tmp_path / "trust.db")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    digests = [f"{index:064x}" for index in range(20)]

    await asyncio.gather(
        *(store.trust(workspace, digest, source="user_slash") for digest in digests)
    )

    current = await store.get(workspace)
    assert current is not None
    assert current.profile_digest in digests
    assert await store.is_trusted(workspace, current.profile_digest)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("workspace", "digest", "source"),
    [
        ("", "a" * 64, "user_slash"),
        (".", "short", "user_slash"),
        (".", "z" * 64, "user_slash"),
        (".", "a" * 64, ""),
    ],
)
async def test_invalid_trust_inputs_are_rejected(
    tmp_path: Path,
    workspace: str,
    digest: str,
    source: str,
) -> None:
    store = HarnessTrustStore(tmp_path / "trust.db")

    with pytest.raises(ValueError):
        await store.trust(workspace, digest, source=source)
