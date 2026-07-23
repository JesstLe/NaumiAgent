"""UI-13.5a privacy-bounded Doctor export bundle tests."""

from __future__ import annotations

import json
import os
import zipfile
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest

from naumi_agent.ui.doctor import DoctorCheck, DoctorReport
from naumi_agent.ui.doctor_export import (
    build_doctor_export_plan,
    write_doctor_export,
)
from naumi_agent.ui.doctor_health import build_doctor_health_snapshot


def _snapshot(
    *,
    workspace: Path,
    state_home: Path,
    secret: str = "sk-proj-1234567890abcdefghijklmnopqrstuvwxyzABCDEFGHIJ",
    extra_detail: str = "",
):
    return build_doctor_health_snapshot(
        DoctorReport(checks=(
            DoctorCheck(
                "config 文件",
                "warn",
                (
                    f"workspace={workspace}; state={state_home}; key={secret}; "
                    f"{extra_detail}"
                ),
                f"检查 {Path.home() / '.naumi' / 'config.yaml'}",
            ),
            DoctorCheck("Node.js", "pass", "v22.17.0"),
        )),
        generated_at="2026-07-23T09:00:00+00:00",
    )


def test_doctor_export_is_deterministic_and_excludes_private_content(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "private-user" / "workspace"
    state_home = tmp_path / "private-user" / "state"
    workspace.mkdir(parents=True)
    snapshot = _snapshot(workspace=workspace, state_home=state_home)

    first = build_doctor_export_plan(
        snapshot,
        workspace_root=workspace,
        state_home=state_home,
    )
    second = build_doctor_export_plan(
        snapshot,
        workspace_root=workspace,
        state_home=state_home,
    )

    assert first.archive_bytes == second.archive_bytes
    assert first.preview == second.preview
    assert [item.path for item in first.preview.files] == [
        "health.json",
        "README.txt",
        "manifest.json",
    ]
    archive_text = first.archive_bytes.decode("latin1", errors="ignore")
    assert "sk-proj-1234567890abcdefghijklmnopqrstuvwxyzABCDEFGHIJ" not in archive_text

    archive_path = tmp_path / "inspect.zip"
    archive_path.write_bytes(first.archive_bytes)
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == ["health.json", "README.txt", "manifest.json"]
        combined = b"\n".join(archive.read(name) for name in archive.namelist())
        assert b"sk-proj-1234567890abcdefghijklmnopqrstuvwxyzABCDEFGHIJ" not in combined
        assert str(workspace).encode() not in combined
        assert str(state_home).encode() not in combined
        assert str(Path.home()).encode() not in combined
        assert b"<workspace>" in combined
        assert b"<naumi-state>" in combined
        health = json.loads(archive.read("health.json"))
        assert health["redaction"]["conversation_included"] is False
        assert health["redaction"]["reasoning_included"] is False
        assert health["redaction"]["raw_trace_included"] is False
        manifest = json.loads(archive.read("manifest.json"))
        assert "source_code" in manifest["excludes"]
        assert manifest["source_snapshot_sha256"] == snapshot.snapshot_sha256


def test_doctor_export_writes_atomically_with_private_permissions(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_home = tmp_path / "state"
    plan = build_doctor_export_plan(
        _snapshot(workspace=workspace, state_home=state_home),
        workspace_root=workspace,
        state_home=state_home,
    )
    now = datetime(2026, 7, 23, 10, 11, 12, tzinfo=UTC)

    receipt = write_doctor_export(plan, state_home=state_home, now=now)
    repeated = write_doctor_export(plan, state_home=state_home, now=now)

    output = Path(receipt.output_path)
    assert output.parent == state_home / "diagnostics"
    assert output.name.startswith("naumi-diagnostics-20260723T101112Z-")
    assert output.read_bytes() == plan.archive_bytes
    assert receipt.reused_existing is False
    assert repeated.reused_existing is True
    assert repeated.output_path == receipt.output_path
    if os.name == "posix":
        assert output.stat().st_mode & 0o777 == 0o600
        assert output.parent.stat().st_mode & 0o777 == 0o700


def test_doctor_export_rejects_symlink_and_tampered_plan(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_home = tmp_path / "state"
    plan = build_doctor_export_plan(
        _snapshot(workspace=workspace, state_home=state_home),
        workspace_root=workspace,
        state_home=state_home,
    )
    tampered = type(plan)(
        preview=plan.preview,
        archive_bytes=plan.archive_bytes + b"tampered",
    )
    with pytest.raises(ValueError, match="摘要"):
        write_doctor_export(tampered, state_home=state_home)

    state_home.mkdir()
    target = tmp_path / "outside"
    target.mkdir()
    (state_home / "diagnostics").symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError, match="符号链接"):
        write_doctor_export(plan, state_home=state_home)


def test_doctor_export_redacts_lexical_and_resolved_path_aliases(
    tmp_path: Path,
) -> None:
    real_workspace = tmp_path / "real-workspace"
    real_state = tmp_path / "real-state"
    real_workspace.mkdir()
    real_state.mkdir()
    workspace_alias = tmp_path / "workspace-alias"
    state_alias = tmp_path / "state-alias"
    workspace_alias.symlink_to(real_workspace, target_is_directory=True)
    state_alias.symlink_to(real_state, target_is_directory=True)
    snapshot = _snapshot(
        workspace=workspace_alias,
        state_home=state_alias,
        extra_detail=(
            f"workspace={workspace_alias}; resolved={real_workspace}; "
            f"state={state_alias}; resolved_state={real_state}"
        ),
    )

    plan = build_doctor_export_plan(
        snapshot,
        workspace_root=workspace_alias,
        state_home=state_alias,
    )

    with zipfile.ZipFile(BytesIO(plan.archive_bytes)) as archive:
        content = "\n".join(
            archive.read(name).decode("utf-8")
            for name in archive.namelist()
        )
    for private_path in (
        workspace_alias,
        real_workspace,
        state_alias,
        real_state,
    ):
        assert str(private_path) not in content
    assert "<workspace>" in content
    assert "<naumi-state>" in content
