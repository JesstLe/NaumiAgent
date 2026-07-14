from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.trust import HarnessTrustStore


def _git(workspace: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=workspace, check=True, capture_output=True)


def _service(tmp_path: Path) -> HarnessService:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "tests@example.com")
    _git(workspace, "config", "user.name", "Harness Tests")
    (workspace / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    profile = workspace / ".naumi" / "harness.yaml"
    profile.parent.mkdir()
    argv = json.dumps([sys.executable, "-c", "print('unit ok')"])
    profile.write_text(
        "schema_version: 1\n"
        "completion:\n"
        "  correction_attempts: 1\n"
        "checks:\n"
        "  - id: unit\n"
        f"    argv: {argv}\n"
        "    timeout_seconds: 10\n"
        "    when_changed: ['**/*.py']\n"
        "    required_for: [change]\n",
        encoding="utf-8",
    )
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-qm", "fixture")
    return HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust.db"),
    )


@pytest.mark.asyncio
async def test_run_gate_requires_check_then_verifies_same_tree(tmp_path: Path) -> None:
    service = _service(tmp_path)
    await service.trust(source="user_slash")
    state = await service.begin_completion_run(
        task="修改 source.py",
        run_id="run-gate",
        session_id="session-1",
    )
    assert state is not None
    assert "harness_run_check" in state.context
    assert "unit" in state.context
    (service.workspace_root / "source.py").write_text("VALUE = 2\n", encoding="utf-8")

    first = await service.evaluate_completion_run(state)

    assert first.status == "needs_correction"
    assert "unit" in first.correction_instruction
    assert state.correction_attempt == 1

    check = await service.run_check(check_id="unit", run_id="run-gate")
    final = await service.evaluate_completion_run(state)

    assert check.status.value == "passed"
    assert final.status == "completed_verified"
    assert final.receipt is not None
    assert final.receipt.checks[0].status == "passed"
    assert final.receipt.changed_files == ("source.py",)
    assert state.finalized


@pytest.mark.asyncio
async def test_run_gate_profile_change_cannot_reuse_old_check(tmp_path: Path) -> None:
    service = _service(tmp_path)
    await service.trust(source="user_slash")
    state = await service.begin_completion_run(
        task="修改 source.py",
        run_id="run-profile-change",
        session_id="session-1",
    )
    assert state is not None
    (service.workspace_root / "source.py").write_text("VALUE = 2\n", encoding="utf-8")
    await service.run_check(check_id="unit", run_id="run-profile-change")
    profile = service.workspace_root / ".naumi" / "harness.yaml"
    profile.write_text(profile.read_text() + "\n", encoding="utf-8")

    result = await service.evaluate_completion_run(state)

    assert result.status == "needs_correction"
    assert "Profile digest" in result.correction_instruction


@pytest.mark.asyncio
async def test_missing_or_untrusted_profile_keeps_engine_compatible(tmp_path: Path) -> None:
    service = _service(tmp_path)

    assert await service.begin_completion_run(
        task="只回答问题",
        run_id="run-untrusted",
        session_id="session-1",
    ) is None

    (service.workspace_root / ".naumi" / "harness.yaml").unlink()
    assert await service.begin_completion_run(
        task="只回答问题",
        run_id="run-missing",
        session_id="session-1",
    ) is None


@pytest.mark.asyncio
async def test_trusted_non_git_workspace_blocks_verified_completion(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "plain-workspace"
    profile = workspace / ".naumi" / "harness.yaml"
    profile.parent.mkdir(parents=True)
    profile.write_text("schema_version: 1\n", encoding="utf-8")
    service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "plain-trust.db"),
    )
    await service.trust(source="test")

    state = await service.begin_completion_run(
        task="回答问题",
        run_id="run-non-git",
        session_id="session-1",
    )
    assert state is not None
    first = await service.evaluate_completion_run(state)
    final = await service.evaluate_completion_run(state)

    assert first.status == "needs_correction"
    assert final.status == "blocked"
    assert final.receipt is not None
    assert any("Git" in warning for warning in final.receipt.warnings)


@pytest.mark.asyncio
async def test_unknown_mutation_scope_requires_all_change_checks(tmp_path: Path) -> None:
    service = _service(tmp_path)
    await service.trust(source="test")
    state = await service.begin_completion_run(
        task="修改外部状态",
        run_id="run-unknown-mutation",
        session_id="session-1",
    )
    assert state is not None
    state.mutating_tool_used = True

    result = await service.evaluate_completion_run(state)

    assert result.status == "needs_correction"
    assert state.contract.required_checks == ("unit",)
