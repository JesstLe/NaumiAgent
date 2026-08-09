from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import naumi_agent.main as main_module
from naumi_agent.release.runtime_health import (
    ReleaseRuntimeHealthError,
    inspect_runtime_health,
    parse_runtime_health_report,
)
from tests.unit.test_release_launcher import _active_store


def _environment(store, target) -> dict[str, str]:
    return {
        "NAUMI_ACTIVE_SLOT_ID": target.slot.slot_id,
        "NAUMI_ACTIVE_POINTER_GENERATION": str(target.pointer.generation),
        "NAUMI_INSTALL_ROOT": str(store.release_root),
    }


def test_runtime_health_binds_active_chain_binary_and_environment(tmp_path: Path) -> None:
    store = _active_store(tmp_path)
    target = store.resolve_active_backend()

    report = inspect_runtime_health(
        store,
        environment=_environment(store, target),
        runtime_path=target.backend,
        checked_at="2026-08-10T09:00:00+00:00",
    )
    restored = parse_runtime_health_report(report.model_dump_json())

    assert restored == report
    assert report.status == "healthy"
    assert report.pointer_sha256 == target.pointer.pointer_sha256
    assert report.slot_sha256 == target.slot.slot_sha256
    assert report.boot_receipt_sha256 == target.boot_receipt.receipt_sha256
    assert report.binary_sha256 == target.boot_receipt.binary_sha256
    assert report.process_started
    assert not report.user_session_started


def test_runtime_health_rejects_environment_binary_and_output_drift(
    tmp_path: Path,
) -> None:
    store = _active_store(tmp_path)
    target = store.resolve_active_backend()
    environment = _environment(store, target)

    with pytest.raises(ReleaseRuntimeHealthError) as wrong_generation:
        inspect_runtime_health(
            store,
            environment={**environment, "NAUMI_ACTIVE_POINTER_GENERATION": "999"},
            runtime_path=target.backend,
        )
    assert wrong_generation.value.code == "release_runtime_health_environment_mismatch"

    with pytest.raises(ReleaseRuntimeHealthError) as wrong_binary:
        inspect_runtime_health(
            store,
            environment=environment,
            runtime_path=tmp_path / "other-runtime",
        )
    assert wrong_binary.value.code == "release_runtime_health_binary_mismatch"

    report = inspect_runtime_health(
        store,
        environment=environment,
        runtime_path=target.backend,
    )
    with pytest.raises(ReleaseRuntimeHealthError) as extra_output:
        parse_runtime_health_report(report.model_dump_json() + "\nwarning")
    assert extra_output.value.code == "release_runtime_health_output_invalid"


def test_hidden_runtime_health_command_bypasses_onboarding_and_emits_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _active_store(tmp_path)
    target = store.resolve_active_backend()
    for name, value in _environment(store, target).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(main_module.sys, "executable", str(target.backend))

    result = CliRunner().invoke(main_module.app, ["--runtime-health-check"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    report = parse_runtime_health_report(json.dumps(payload))
    assert report.runtime_path == str(target.backend.resolve())
    assert report.pointer_sha256 == target.pointer.pointer_sha256

    monkeypatch.delenv("NAUMI_INSTALL_ROOT")
    missing_root = CliRunner().invoke(main_module.app, ["--runtime-health-check"])
    assert missing_root.exit_code == 78
    assert "release_runtime_health_environment_missing" in missing_root.stderr
