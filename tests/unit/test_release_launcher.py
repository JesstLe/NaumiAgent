from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from naumi_agent.release.artifact import assemble_release_artifact
from naumi_agent.release.launcher import _resolve_and_record, main, resolve_launch
from naumi_agent.release.slots import ReleaseSlotError, ReleaseSlotStore, host_release_target


def _executable(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def _active_store(tmp_path: Path) -> ReleaseSlotStore:
    target = host_release_target()
    if target.startswith("windows-"):
        pytest.skip("此真实执行夹具使用 POSIX 脚本。")
    backend = tmp_path / "backend"
    _executable(
        backend / "naumi-runtime",
        "#!/bin/sh\necho 'naumi 2.0.0'\n",
    )
    launcher = _executable(tmp_path / "launcher" / "naumi", "#!/bin/sh\nexit 0\n")
    ui = _executable(tmp_path / "naumi-ui", "#!/bin/sh\nexit 0\n")
    config = tmp_path / "config.yaml.example"
    config.write_text("models: {}\n", encoding="utf-8")
    artifact = assemble_release_artifact(
        backend_dir=backend,
        launcher_dir=launcher.parent,
        ui_binary=ui,
        config_example=config,
        output_dir=tmp_path / "release",
        version="2.0.0",
        target=target,
        archive_format="tar.gz",
    )
    store = ReleaseSlotStore(tmp_path / "installed")
    slot = store.install(artifact.bundle_dir)
    store.verify_bootable(slot.slot_id)
    store.activate(slot.slot_id)
    return store


def test_status_resolution_has_no_process_start_authority(tmp_path: Path) -> None:
    store = _active_store(tmp_path)

    resolution = resolve_launch(
        store,
        argument_count=0,
        process_start_requested=False,
        resolved_at="2026-08-06T03:00:00+00:00",
    )
    store.record_launch_resolution(resolution)

    assert not resolution.process_start_requested
    assert not resolution.process_start_authority
    with sqlite3.connect(store.db_path) as db:
        stored = json.loads(
            db.execute("SELECT resolution_json FROM release_launch_resolutions").fetchone()[0]
        )
    assert stored["arguments_persisted"] is False
    assert "arguments" not in stored


def test_resolution_uses_exact_boot_receipt_bound_by_active_pointer(
    tmp_path: Path,
) -> None:
    store = _active_store(tmp_path)
    pointer = store.active()
    assert pointer is not None
    later = store.verify_bootable(
        pointer.current_slot_id,
        checked_at="2026-08-06T04:00:00+00:00",
    )
    assert later.receipt_id != pointer.boot_receipt_id

    resolution = resolve_launch(store, argument_count=0)

    assert resolution.boot_receipt_id == pointer.boot_receipt_id
    assert resolution.boot_receipt_id != later.receipt_id


def test_resolution_retries_bounded_pointer_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _active_store(tmp_path)
    original = store.record_launch_resolution
    attempts = 0

    def race_once(resolution) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ReleaseSlotError(
                "release_launch_pointer_stale",
                "fixture pointer race",
            )
        original(resolution)

    monkeypatch.setattr(store, "record_launch_resolution", race_once)

    resolution = _resolve_and_record(
        store,
        argument_count=1,
        process_start_requested=True,
    )

    assert attempts == 2
    assert resolution.process_start_authority


def test_launcher_status_resolves_active_slot_without_starting_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _active_store(tmp_path)
    monkeypatch.setenv("NAUMI_INSTALL_ROOT", str(store.release_root))

    assert main(["--launcher-status"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["slot_id"] == store.active().current_slot_id
    assert payload["process_start_requested"] is False
    assert payload["process_start_authority"] is False


def test_launcher_fails_closed_when_active_runtime_is_tampered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _active_store(tmp_path)
    active = store.resolve_active_backend()
    active.backend.chmod(0o755)
    active.backend.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    monkeypatch.setenv("NAUMI_INSTALL_ROOT", str(store.release_root))

    assert main(["--launcher-status"]) == 78

    error = capsys.readouterr().err
    assert "Naumi 启动器错误" in error
    assert "release_bundle_file_mismatch" in error


def test_launcher_reports_exec_failure_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _active_store(tmp_path)
    monkeypatch.setenv("NAUMI_INSTALL_ROOT", str(store.release_root))

    def fail_execve(*_args, **_kwargs):
        raise OSError("fixture exec failure")

    monkeypatch.setattr(os, "execve", fail_execve)

    assert main(["hello"]) == 78

    error = capsys.readouterr().err
    assert "Naumi 启动器无法启动 active runtime" in error
    assert "Traceback" not in error
