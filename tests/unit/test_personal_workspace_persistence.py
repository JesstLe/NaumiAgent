"""Explicit federation enrollment, owner/workspace pins and cold reopen without an Engine."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.store import PersonalAgentStore
from naumi_agent.personal.workspace_persistence import WorkspacePersistence
from naumi_agent.personal.workspace_store import WorkspaceStore


def engine(root, *, catalog=None, workspace="source", session="runtime/sessions.db"):
    """Use the real catalog/journal; no execution Engine, Router or worker is constructed."""
    catalog = catalog or PersonalAgentStore(root / "personal")
    session_db = root / session
    workspace_root = root / workspace
    database_key = os.path.normcase(str(session_db.resolve()))
    workspace_key = hashlib.sha256(
        (os.path.normcase(str(workspace_root.resolve())) + "\0" + database_key).encode()
    ).hexdigest()
    owner_key = hashlib.sha256(
        os.path.normcase(str(catalog.db_path.resolve())).encode()
    ).hexdigest()
    return SimpleNamespace(
        personal_agents=SimpleNamespace(store=catalog, workspace_key=workspace_key),
        personal_items=SimpleNamespace(owner_key=owner_key),
        config=SimpleNamespace(memory=SimpleNamespace(session_db_path=str(session_db))),
    )


@pytest.fixture
def system(tmp_path):
    value = engine(tmp_path)
    return value, WorkspacePersistence(value)


async def assert_code(code, operation):
    with pytest.raises(PersonalAgentError) as caught:
        await operation
    assert caught.value.code == code


def grant(service, key="wg_" + "1" * 32, *, workspace=None):
    return {
        "id": key,
        "workspace_key": workspace or service.workspace_key,
        "status": "active",
        "opaque": "no body or token",
    }


def counts(service):
    return service.store.read(
        lambda tx: {kind: len(tx.list(kind)) for kind in sorted(service.store.spec.kinds)}
    )


async def test_constructor_and_fresh_reads_create_no_directory_catalog_or_pin(system, tmp_path):
    value, service = system
    assert not list(tmp_path.iterdir())
    assert service.personal is value.personal_agents
    assert service.owner_key == value.personal_items.owner_key
    assert service.workspace_key == value.personal_agents.workspace_key
    assert service.enrolled() is False
    assert await service._read(lambda tx: tx is None) is True
    assert await service._record("grant", "missing", missing=True) is None
    await assert_code("not_found", service._record("grant", "missing"))
    assert not list(tmp_path.iterdir())
    assert not hasattr(service, "start") and not hasattr(service, "stop")


async def test_explicit_ensure_enrolls_only_metadata_and_exact_external_identity(system):
    value, service = system
    identity = await service.ensure()
    assert len(identity) == 32 and service.enrolled() is True
    assert value.personal_agents.store.db_path.is_file()
    assert value.personal_agents.store.list() == ()
    assert service.store.identity() == identity
    assert counts(service) == {"connection": 0, "grant": 0, "read": 0, "source_read": 0}
    pin = json.loads(service.boundary.read_text("utf-8"))
    assert pin == {
        "version": 1,
        "owner_key": service.owner_key,
        "workspace_key": service.workspace_key,
        "instance_id": identity,
    }
    assert service.boundary.name == f".personal-workspaces-{service.workspace_key}-v1"
    assert await service.ensure() == identity
    assert not Path(value.config.memory.session_db_path).exists(), (
        "Directory identity is not a session-DB instance claim"
    )


async def test_write_before_enrollment_is_rejected_without_implicit_initialization(
    system, tmp_path
):
    _, service = system
    await assert_code(
        "workspace_not_enrolled",
        service._write(lambda tx: tx.put("grant", grant(service), expected_revision=0)),
    )
    assert not list(tmp_path.iterdir())


async def test_scope_record_cas_history_and_detachment_survive_cold_reopen(system):
    value, service = system
    identity = await service.ensure()
    saved = await service._write(lambda tx: tx.put("grant", grant(service), expected_revision=0))
    changed = await service._write(
        lambda tx: tx.put("grant", {**saved, "status": "revoked"}, expected_revision=1)
    )
    cold = WorkspacePersistence(value)
    assert cold.enrolled() is True and cold.store.expected_instance_id == identity
    assert await cold._record("grant", saved["id"], revision=1) == saved
    assert await cold._record("grant", saved["id"]) == changed
    changed["opaque"] = "caller mutation"
    assert (await cold._record("grant", saved["id"]))["opaque"] != "caller mutation"


async def test_second_workspace_read_does_not_auto_enroll_or_publish_external_pin(system, tmp_path):
    value, first = system
    identity = await first.ensure()
    document = await first._write(lambda tx: tx.put("grant", grant(first), expected_revision=0))
    second_engine = engine(
        tmp_path,
        catalog=value.personal_agents.store,
        workspace="other",
        session="second-runtime/sessions.db",
    )
    second = WorkspacePersistence(second_engine)
    assert second.workspace_key != first.workspace_key and second.owner_key == first.owner_key
    assert second.enrolled() is False
    assert await second._read(lambda tx: tx.get("grant", document["id"])) == document
    assert not second.boundary.exists() and not second.boundary.parent.exists()
    await assert_code("not_found", second._record("grant", document["id"]))
    await assert_code("workspace_not_enrolled", second._write(lambda tx: None))
    assert await second.ensure() == identity
    assert second.enrolled() is True
    assert counts(first)["grant"] == 1
    await assert_code("not_found", second._record("grant", document["id"]))


async def test_concurrent_enrollment_has_one_instance_without_business_record(system):
    value, service = system
    peers = [service, *(WorkspacePersistence(value) for _ in range(3))]
    identities = await asyncio.gather(*(peer.ensure() for peer in peers))
    assert identities == [identities[0]] * 4
    assert all(peer.enrolled() for peer in peers)
    assert counts(service) == {"connection": 0, "grant": 0, "read": 0, "source_read": 0}


@pytest.mark.parametrize("kind", ["grant", "connection", "read", "source_read"])
async def test_record_workspace_scope_applies_to_all_journal_domains(system, kind):
    _, service = system
    await service.ensure()
    document = grant(service, workspace="f" * 64)
    await service._write(lambda tx: tx.put(kind, document, expected_revision=0))
    await assert_code("not_found", service._record(kind, document["id"]))
    await assert_code("not_found", service._record(kind, document["id"], revision=1))


async def test_catalog_loss_after_enrollment_is_not_recreated_by_read_write_or_ensure(system):
    value, service = system
    await service.ensure()
    value.personal_agents.store.db_path.unlink()
    for operation in (
        service._read(lambda tx: None),
        service._write(lambda tx: None),
        service.ensure(),
    ):
        await assert_code("storage_error", operation)
    assert not value.personal_agents.store.db_path.exists()


async def test_pinned_db_plus_local_witness_loss_fails_on_real_cold_reopen(system):
    value, service = system
    await service.ensure()
    service.store.db_path.unlink()
    service.store.witness_path.unlink()
    cold = WorkspacePersistence(value)
    for operation in (cold._read(lambda tx: None), cold._write(lambda tx: None), cold.ensure()):
        await assert_code("storage_error", operation)
    assert not service.store.db_path.exists() and not service.store.witness_path.exists()


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "wrong_owner",
        "wrong_workspace",
        "wrong_instance",
        "bool_version",
        "unknown",
        "duplicate",
        "oversize",
    ],
)
async def test_external_pin_loss_corruption_or_replacement_never_gets_repaired(system, change):
    _, service = system
    await service.ensure()
    value = json.loads(service.boundary.read_text("utf-8"))
    if change == "missing":
        service.boundary.unlink()
    elif change == "duplicate":
        service.boundary.write_text('{"version":1,"version":1}', encoding="utf-8")
    elif change == "oversize":
        service.boundary.write_bytes(b"x" * 1025)
    else:
        key, replacement = {
            "wrong_owner": ("owner_key", "f" * 64),
            "wrong_workspace": ("workspace_key", "f" * 64),
            "wrong_instance": ("instance_id", "f" * 32),
            "bool_version": ("version", True),
            "unknown": ("extra", True),
        }[change]
        value[key] = replacement
        service.boundary.write_text(json.dumps(value), encoding="utf-8")
    before = service.boundary.read_bytes() if service.boundary.exists() else None
    await assert_code("storage_error", service._read(lambda tx: None))
    await assert_code("storage_error", service.ensure())
    assert (service.boundary.read_bytes() if service.boundary.exists() else None) == before


@pytest.mark.parametrize("path", ["pin", "catalog"])
async def test_hardlinked_pin_or_catalog_is_not_an_alternative_identity(system, tmp_path, path):
    value, service = system
    await service.ensure()
    original = service.boundary if path == "pin" else value.personal_agents.store.db_path
    os.link(original, tmp_path / f"alias-{path}")
    await assert_code("storage_error", service._read(lambda tx: None))


async def test_wrong_owner_or_external_instance_override_is_rejected_before_write(system, tmp_path):
    value, service = system
    await service.ensure()
    candidates = [
        WorkspaceStore(tmp_path / "wrong-owner", owner_key="f" * 64),
        WorkspaceStore(
            tmp_path / "wrong-instance", owner_key=service.owner_key, expected_instance_id="f" * 32
        ),
    ]
    for store in candidates:
        with pytest.raises(PersonalAgentError) as caught:
            WorkspacePersistence(value, store=store)
        assert caught.value.code == "storage_error"
        assert not store.base_dir.exists()


async def test_foreign_or_boolean_revision_cannot_be_smuggled_through_fresh_record_read(system):
    _, service = system
    await assert_code("storage_error", service._record("unknown", "x", missing=True))
    await assert_code("invalid_input", service._record("grant", "x", revision=True, missing=True))
    await assert_code("invalid_input", service._record("grant", "x", missing="yes"))


async def test_callback_failure_rolls_back_business_facts_without_undoing_enrollment(system):
    _, service = system
    identity = await service.ensure()

    def fail(tx):
        tx.put("grant", grant(service), expected_revision=0)
        raise PersonalAgentError("fixture_abort", "明确中止测试写入")

    await assert_code("fixture_abort", service._write(fail))
    assert service.enrolled() and service.store.identity() == identity
    assert counts(service)["grant"] == 0


async def test_pin_file_replacement_during_read_is_not_trusted_from_old_open_handle(
    system, tmp_path, monkeypatch
):
    _, service = system
    await service.ensure()
    original = service.boundary.read_bytes()
    replacement = tmp_path / "replacement-pin"
    replacement.write_bytes(original)
    opened, replaced = Path.open, []

    def replace_after_open(path, *args, **kwargs):
        stream = opened(path, *args, **kwargs)
        if path == service.boundary and args and args[0] == "rb" and not replaced:
            try:
                os.replace(replacement, service.boundary)
                replaced.append(True)
            except PermissionError:
                stream.close()
                pytest.skip("This filesystem does not allow replacing an open read handle")
        return stream

    monkeypatch.setattr(Path, "open", replace_after_open)
    await assert_code("storage_error", service._read(lambda tx: None))
    assert replaced
