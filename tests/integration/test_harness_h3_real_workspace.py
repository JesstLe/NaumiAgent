from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.trust import HarnessTrustStore


def _git(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.asyncio
async def test_real_worktree_completion_gate_rejects_stale_evidence(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).resolve().parents[2]
    worktree = tmp_path / "h3-worktree"
    _git(repository, "worktree", "add", "--detach", str(worktree), "HEAD")
    try:
        profile = worktree / ".naumi" / "harness.yaml"
        check_argv = json.dumps(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "tests/unit/test_harness_completion.py",
            ]
        )
        profile.write_text(
            "schema_version: 1\n"
            "completion:\n"
            "  correction_attempts: 1\n"
            "checks:\n"
            "  - id: h3_smoke\n"
            f"    argv: {check_argv}\n"
            "    timeout_seconds: 60\n"
            "    when_changed: ['src/naumi_agent/harness/**/*.py']\n"
            "    required_for: [change]\n",
            encoding="utf-8",
        )
        _git(worktree, "add", ".naumi/harness.yaml")
        service = HarnessService(
            workspace_root=worktree,
            trust_store=HarnessTrustStore(tmp_path / "trust.db"),
        )
        await service.trust(source="integration_test")
        source = worktree / "src" / "naumi_agent" / "harness" / "completion.py"

        verified_run = await service.begin_completion_run(
            task="修改 Harness completion",
            run_id="h3-real-verified",
            session_id="integration",
        )
        assert verified_run is not None
        source.write_text(
            source.read_text(encoding="utf-8") + "\n# h3 verified scenario\n",
            encoding="utf-8",
        )
        correction = await service.evaluate_completion_run(verified_run)
        assert correction.status == "needs_correction"
        assert "h3_smoke" in correction.correction_instruction

        passed = await service.run_check(
            check_id="h3_smoke",
            run_id=verified_run.contract.run_id,
        )
        verified = await service.evaluate_completion_run(verified_run)
        assert passed.status.value == "passed"
        assert verified.status == "completed_verified"
        assert verified.receipt is not None
        assert verified.receipt.changed_files == (
            "src/naumi_agent/harness/completion.py",
        )

        stale_run = await service.begin_completion_run(
            task="继续修改 Harness completion",
            run_id="h3-real-stale",
            session_id="integration",
        )
        assert stale_run is not None
        source.write_text(
            source.read_text(encoding="utf-8") + "# before check\n",
            encoding="utf-8",
        )
        assert (await service.evaluate_completion_run(stale_run)).status == (
            "needs_correction"
        )
        assert (
            await service.run_check(
                check_id="h3_smoke",
                run_id=stale_run.contract.run_id,
            )
        ).status.value == "passed"
        source.write_text(
            source.read_text(encoding="utf-8") + "# after check\n",
            encoding="utf-8",
        )
        stale = await service.evaluate_completion_run(stale_run)
        assert stale.status == "completed_unverified"
        assert stale.receipt is not None
        assert stale.receipt.checks[0].status == "stale"

        profile_run = await service.begin_completion_run(
            task="Profile 变化时禁止执行",
            run_id="h3-real-profile-change",
            session_id="integration",
        )
        assert profile_run is not None
        profile.write_text(profile.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        blocked = await service.run_check(
            check_id="h3_smoke",
            run_id=profile_run.contract.run_id,
        )
        assert blocked.status.value == "blocked_by_policy"
        assert blocked.duration_ms == 0
        assert "未受信任" in blocked.message
    finally:
        _git(repository, "worktree", "remove", "--force", str(worktree))
