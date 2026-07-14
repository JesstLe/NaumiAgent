from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from naumi_agent.harness.service import HarnessService, HarnessStatusCode
from naumi_agent.harness.trust import HarnessTrustStore


@pytest.mark.asyncio
async def test_real_repository_profile_diagnoses_and_invalidates_trust(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    source_profile = repository_root / ".naumi" / "harness.yaml"
    assert source_profile.is_file(), "真实仓库必须提供 .naumi/harness.yaml"

    workspace = tmp_path / "real-git-workspace"
    workspace.mkdir()
    subprocess.run(
        ["git", "init", "-q", str(workspace)],
        check=True,
        capture_output=True,
        text=True,
    )
    (workspace / ".naumi").mkdir()
    profile_path = workspace / ".naumi" / "harness.yaml"
    shutil.copy2(source_profile, profile_path)
    for relative in (
        "AGENTS.md",
        "README.md",
        "docs/superpowers/specs/2026-07-14-harness-engineering-design.md",
    ):
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"fixture for {relative}\n", encoding="utf-8")

    def fail_spawn(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"H1 不得执行 Profile 命令: {args!r} {kwargs!r}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)
    service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "harness-trust.db"),
    )

    started = time.perf_counter()
    report = await service.doctor()
    elapsed = time.perf_counter() - started

    assert report.status.code is HarnessStatusCode.UNTRUSTED
    assert report.command_summaries
    assert elapsed < 2.0
    trusted = await service.trust(source="integration_test")
    assert (await service.status()).trusted

    profile_path.write_bytes(profile_path.read_bytes() + b"\n")

    changed = await service.status()
    assert changed.code is HarnessStatusCode.UNTRUSTED
    assert changed.profile_digest != trusted.profile_digest
