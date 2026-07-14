from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from naumi_agent.harness.checks import HarnessCheckStatus
from naumi_agent.harness.fingerprint import compute_tree_fingerprint
from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.trust import HarnessTrustStore


def _git(workspace: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )


def _workspace(tmp_path: Path, *, command_code: str = "print('check ok')") -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "tests@example.com")
    _git(workspace, "config", "user.name", "Harness Tests")
    source = workspace / "source.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    profile = workspace / ".naumi" / "harness.yaml"
    profile.parent.mkdir()
    profile.write_text(
        "schema_version: 1\n"
        "checks:\n"
        "  - id: unit\n"
        f"    argv: {json.dumps([sys.executable, '-c', command_code])}\n"
        "    timeout_seconds: 10\n"
        "    when_changed: ['**/*.py']\n"
        "    required_for: [change]\n",
        encoding="utf-8",
    )
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-qm", "fixture")
    return workspace, profile


def _service(
    tmp_path: Path,
    *,
    command_code: str = "print('check ok')",
) -> tuple[HarnessService, Path]:
    workspace, profile = _workspace(tmp_path, command_code=command_code)
    return (
        HarnessService(
            workspace_root=workspace,
            trust_store=HarnessTrustStore(tmp_path / "trust.db"),
        ),
        profile,
    )


def test_tree_fingerprint_changes_for_worktree_index_and_untracked_bytes(
    tmp_path: Path,
) -> None:
    workspace, _ = _workspace(tmp_path)
    clean = compute_tree_fingerprint(workspace)

    source = workspace / "source.py"
    source.write_text("VALUE = 2\n", encoding="utf-8")
    modified = compute_tree_fingerprint(workspace)
    _git(workspace, "add", "source.py")
    staged = compute_tree_fingerprint(workspace)
    mode_changed = None
    if os.name != "nt":
        source.chmod(0o755)
        mode_changed = compute_tree_fingerprint(workspace)
    untracked = workspace / "new.py"
    untracked.write_text("NEW = 1\n", encoding="utf-8")
    first_untracked = compute_tree_fingerprint(workspace)
    untracked.write_text("NEW = 2\n", encoding="utf-8")
    second_untracked = compute_tree_fingerprint(workspace)

    digests = {
        clean.digest,
        modified.digest,
        staged.digest,
        first_untracked.digest,
        second_untracked.digest,
    }
    if mode_changed is not None:
        digests.add(mode_changed.digest)
    assert len(digests) == (6 if mode_changed is not None else 5)
    assert modified.dirty_paths == ("source.py",)
    assert "new.py" in second_untracked.dirty_paths


def test_tree_fingerprint_handles_git_rename_records(tmp_path: Path) -> None:
    workspace, _ = _workspace(tmp_path)
    _git(workspace, "mv", "source.py", "renamed source.py")

    fingerprint = compute_tree_fingerprint(workspace)

    assert fingerprint.dirty_paths == ("renamed source.py", "source.py")


@pytest.mark.asyncio
async def test_untrusted_profile_never_starts_check(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-run"
    service, _ = _service(
        tmp_path,
        command_code=f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
    )

    result = await service.run_check(check_id="unit", run_id="run-1")

    assert result.status is HarnessCheckStatus.BLOCKED_BY_POLICY
    assert "未受信任" in result.message
    assert not marker.exists()


@pytest.mark.asyncio
async def test_trusted_check_passes_and_is_reused_only_for_same_run_and_tree(
    tmp_path: Path,
) -> None:
    counter = tmp_path / "counter"
    code = (
        "from pathlib import Path; "
        f"p=Path({str(counter)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x'); "
        "print('verified')"
    )
    service, _ = _service(tmp_path, command_code=code)
    await service.trust(source="user_slash")

    first = await service.run_check(check_id="unit", run_id="run-1")
    cached = await service.run_check(check_id="unit", run_id="run-1")
    other_run = await service.run_check(check_id="unit", run_id="run-2")

    assert first.status is HarnessCheckStatus.PASSED
    assert not first.cached
    assert cached.status is HarnessCheckStatus.PASSED
    assert cached.cached
    assert other_run.status is HarnessCheckStatus.PASSED
    assert not other_run.cached
    assert counter.read_text() == "xx"


@pytest.mark.asyncio
async def test_concurrent_same_check_is_single_flight_and_waiter_cancel_isolated(
    tmp_path: Path,
) -> None:
    counter = tmp_path / "counter"
    code = (
        "import time; from pathlib import Path; "
        f"p=Path({str(counter)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x'); "
        "time.sleep(0.4); print('done')"
    )
    service, _ = _service(tmp_path, command_code=code)
    await service.trust(source="user_slash")

    cancelled_waiter = asyncio.create_task(
        service.run_check(check_id="unit", run_id="same-run")
    )
    surviving_waiter = asyncio.create_task(
        service.run_check(check_id="unit", run_id="same-run")
    )
    await asyncio.sleep(0.1)
    cancelled_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter
    result = await surviving_waiter

    assert result.status is HarnessCheckStatus.PASSED
    assert counter.read_text() == "x"


@pytest.mark.asyncio
async def test_profile_or_tree_change_invalidates_check_result(tmp_path: Path) -> None:
    service, profile = _service(
        tmp_path,
        command_code="import time; time.sleep(0.3); print('ok')",
    )
    await service.trust(source="user_slash")

    pending = asyncio.create_task(
        service.run_check(check_id="unit", run_id="profile-change")
    )
    await asyncio.sleep(0.1)
    profile.write_text(profile.read_text() + "\n", encoding="utf-8")
    changed_profile = await pending

    assert changed_profile.status is HarnessCheckStatus.BLOCKED_BY_POLICY
    assert "执行期间发生变化" in changed_profile.message

    await service.trust(source="user_slash")
    passed = await service.run_check(check_id="unit", run_id="tree-change")
    (service.workspace_root / "source.py").write_text("VALUE = 3\n", encoding="utf-8")
    rerun = await service.run_check(check_id="unit", run_id="tree-change")

    assert passed.status is HarnessCheckStatus.PASSED
    assert rerun.status is HarnessCheckStatus.PASSED
    assert not rerun.cached
    assert passed.tree_fingerprint != rerun.tree_fingerprint


@pytest.mark.asyncio
async def test_required_checks_follow_task_kind_and_changed_patterns(
    tmp_path: Path,
) -> None:
    service, _ = _service(tmp_path)
    await service.trust(source="user_slash")

    assert await service.required_check_ids(
        task_kind="change",
        changed_paths=("src/example.py",),
    ) == ("unit",)
    assert await service.required_check_ids(
        task_kind="change",
        changed_paths=("README.md",),
    ) == ()
    assert await service.required_check_ids(
        task_kind="analysis",
        changed_paths=("src/example.py",),
    ) == ()
