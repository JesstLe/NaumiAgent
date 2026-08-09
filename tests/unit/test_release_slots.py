from __future__ import annotations

import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from naumi_agent.release.artifact import assemble_release_artifact
from naumi_agent.release.slots import (
    ReleaseSlotError,
    ReleaseSlotStore,
    host_release_target,
)

SOURCE_COMMIT = "a" * 40
SOURCE_TREE_SHA256 = "b" * 64


def _binary(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o755)
    return path


def _bundle(
    root: Path,
    *,
    version: str,
    output_name: str,
    reported=None,
    source_commit: str = SOURCE_COMMIT,
    source_tree_sha256: str = SOURCE_TREE_SHA256,
    build_signer=None,
    build_context=None,
) -> Path:
    target = host_release_target()
    windows = target.startswith("windows-")
    backend_name = "naumi-runtime.exe" if windows else "naumi-runtime"
    launcher_name = "naumi.exe" if windows else "naumi"
    ui_name = "naumi-ui.exe" if windows else "naumi-ui"
    reported = version if reported is None else reported
    backend = root / f"backend-{output_name}"
    if windows:
        backend_content = b"not-a-real-windows-binary"
        ui_content = b"not-a-real-windows-ui"
    else:
        backend_content = f"#!/bin/sh\necho 'naumi {reported}'\n".encode()
        ui_content = b"#!/bin/sh\nexit 0\n"
    _binary(backend / backend_name, backend_content)
    launcher = _binary(
        root / f"launcher-{output_name}" / launcher_name,
        b"not-a-real-windows-launcher" if windows else b"#!/bin/sh\nexit 0\n",
    )
    ui = _binary(root / f"ui-{output_name}" / ui_name, ui_content)
    config = root / f"config-{output_name}.yaml.example"
    config.write_text("models: {}\n", encoding="utf-8")
    return assemble_release_artifact(
        backend_dir=backend,
        launcher_dir=launcher.parent,
        ui_binary=ui,
        config_example=config,
        output_dir=root / output_name,
        version=version,
        target=target,
        source_commit=source_commit,
        source_tree_sha256=source_tree_sha256,
        archive_format="zip" if windows else "tar.gz",
        build_signer=build_signer,
        build_context=build_context,
    ).bundle_dir


@pytest.mark.skipif(os.name == "nt", reason="fixture is a POSIX executable")
def test_install_boot_activate_upgrade_and_atomic_rollback(tmp_path: Path) -> None:
    store = ReleaseSlotStore(tmp_path / "installed")
    v1 = store.install(
        _bundle(tmp_path, version="1.0.0", output_name="release-v1"),
        installed_at="2026-08-06T01:00:00+00:00",
    )
    boot1 = store.verify_bootable(v1.slot_id, checked_at="2026-08-06T01:01:00+00:00")
    active1 = store.activate(v1.slot_id, activated_at="2026-08-06T01:02:00+00:00")
    v2 = store.install(
        _bundle(tmp_path, version="1.1.0", output_name="release-v2"),
        installed_at="2026-08-06T02:00:00+00:00",
    )
    store.verify_bootable(v2.slot_id, checked_at="2026-08-06T02:01:00+00:00")
    active2 = store.activate(v2.slot_id, activated_at="2026-08-06T02:02:00+00:00")
    rolled_back = store.rollback(activated_at="2026-08-06T02:03:00+00:00")

    assert boot1.bootable and boot1.version_matched
    assert v1.source_commit == SOURCE_COMMIT
    assert v1.source_tree_sha256 == SOURCE_TREE_SHA256
    assert boot1.arguments == ("--version",)
    assert boot1.version_output == "naumi 1.0.0"
    assert active1.generation == 1 and active1.previous_slot_id is None
    assert active2.generation == 2 and active2.previous_slot_id == v1.slot_id
    assert rolled_back.generation == 3
    assert rolled_back.action == "rollback"
    assert rolled_back.current_slot_id == v1.slot_id
    assert rolled_back.previous_slot_id == v2.slot_id
    assert store.active() == rolled_back
    assert store.get_activation_event(1) == active1
    assert store.get_activation_event(2) == active2
    assert store.get_activation_event(3) == rolled_back
    assert store.get_activation_event(4) is None
    assert Path(v1.bundle_dir).is_dir() and Path(v2.bundle_dir).is_dir()
    assert not (Path(v1.bundle_dir) / "naumi-runtime").stat().st_mode & 0o200


def test_install_is_cross_thread_idempotent(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, version="1.0.0", output_name="release")
    store = ReleaseSlotStore(tmp_path / "installed")

    with ThreadPoolExecutor(max_workers=8) as pool:
        slots = tuple(pool.map(lambda _: store.install(bundle), range(8)))

    assert all(item.slot_id == slots[0].slot_id for item in slots)
    assert len(tuple(store.slots_dir.glob("relslot_*"))) == 1


def test_install_rejects_bundle_outside_signed_manifest_precondition(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path, version="1.0.0", output_name="release")
    store = ReleaseSlotStore(tmp_path / "installed")

    with pytest.raises(ReleaseSlotError) as blocked:
        store.install(bundle, expected_manifest_sha256="0" * 64)

    assert blocked.value.code == "release_manifest_digest_mismatch"
    assert not store.slots_dir.exists()


@pytest.mark.skipif(os.name == "nt", reason="fixture is a POSIX executable")
def test_activation_requires_current_boot_receipt_and_detects_tamper(
    tmp_path: Path,
) -> None:
    store = ReleaseSlotStore(tmp_path / "installed")
    slot = store.install(_bundle(tmp_path, version="1.0.0", output_name="release"))

    with pytest.raises(ReleaseSlotError) as missing:
        store.activate(slot.slot_id)
    assert missing.value.code == "release_slot_boot_receipt_missing"

    store.verify_bootable(slot.slot_id)
    binary = Path(slot.bundle_dir) / slot.backend_path
    binary.chmod(0o755)
    binary.write_text("#!/bin/sh\necho 'naumi 9.9.9'\n", encoding="utf-8")
    with pytest.raises(ReleaseSlotError) as tampered:
        store.activate(slot.slot_id)
    assert tampered.value.code == "release_bundle_file_mismatch"
    assert store.active() is None


@pytest.mark.skipif(os.name == "nt", reason="fixture is a POSIX executable")
def test_boot_probe_rejects_wrong_reported_version(tmp_path: Path) -> None:
    store = ReleaseSlotStore(tmp_path / "installed")
    slot = store.install(
        _bundle(
            tmp_path,
            version="1.0.0",
            output_name="release",
            reported="11.0.0",
        )
    )

    with pytest.raises(ReleaseSlotError) as blocked:
        store.verify_bootable(slot.slot_id)

    assert blocked.value.code == "release_slot_not_bootable"


@pytest.mark.skipif(os.name == "nt", reason="fixture is a POSIX executable")
def test_upgrade_refuses_to_abandon_tampered_active_slot(tmp_path: Path) -> None:
    store = ReleaseSlotStore(tmp_path / "installed")
    v1 = store.install(_bundle(tmp_path, version="1.0.0", output_name="release-v1"))
    store.verify_bootable(v1.slot_id)
    active = store.activate(v1.slot_id)
    v2 = store.install(_bundle(tmp_path, version="1.1.0", output_name="release-v2"))
    store.verify_bootable(v2.slot_id)
    old_binary = Path(v1.bundle_dir) / v1.backend_path
    old_binary.chmod(0o755)
    old_binary.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")

    with pytest.raises(ReleaseSlotError) as blocked:
        store.activate(v2.slot_id)

    assert blocked.value.code == "release_bundle_file_mismatch"
    assert store.active() == active


def test_slot_verifier_rejects_manifested_naumi_source(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, version="1.0.0", output_name="release")
    source = bundle / "_internal" / "naumi_agent" / "secret.py"
    source.parent.mkdir(parents=True)
    source.write_text("SECRET = True\n", encoding="utf-8")
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    import hashlib

    content = source.read_bytes()
    manifest["files"].append(
        {
            "path": "_internal/naumi_agent/secret.py",
            "kind": "file",
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    )
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ReleaseSlotError) as blocked:
        ReleaseSlotStore(tmp_path / "installed").install(bundle)

    assert blocked.value.code == "release_bundle_source_exposure"


@pytest.mark.skipif(os.name == "nt", reason="fixture is a POSIX executable")
def test_active_pointer_rejects_deleted_intermediate_generation(tmp_path: Path) -> None:
    store = ReleaseSlotStore(tmp_path / "installed")
    v1 = store.install(_bundle(tmp_path, version="1.0.0", output_name="release-v1"))
    store.verify_bootable(v1.slot_id)
    store.activate(v1.slot_id)
    v2 = store.install(_bundle(tmp_path, version="1.1.0", output_name="release-v2"))
    store.verify_bootable(v2.slot_id)
    store.activate(v2.slot_id)
    with sqlite3.connect(store.db_path) as db:
        db.execute("DELETE FROM release_active_events WHERE generation = 1")

    with pytest.raises(ReleaseSlotError) as blocked:
        store.get_activation_event(2)

    assert blocked.value.code == "release_active_chain_broken"
