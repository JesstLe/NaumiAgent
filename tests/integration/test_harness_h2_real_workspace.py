"""Real NaumiAgent H2 knowledge selection without model or network access."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from naumi_agent.harness.service import (
    HarnessKnowledgeStatusCode,
    HarnessService,
)
from naumi_agent.harness.trust import HarnessTrustStore


@pytest.mark.asyncio
async def test_real_workspace_selects_distinct_bounded_knowledge(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).resolve().parents[2]
    service = HarnessService(
        workspace_root=repository,
        trust_store=HarnessTrustStore(tmp_path / "real-trust.db"),
    )
    await service.trust(source="h2_real_workspace_test")

    engine = await service.knowledge_context(
        "修改 AgentEngine 的 Harness 上下文注入",
        model_window=124_000,
    )
    terminal = await service.knowledge_context(
        "优化 frontend/terminal-ui 的状态栏和语义颜色",
        model_window=124_000,
    )
    workbench = await service.knowledge_context(
        "调整 Mac Workbench issue 运行控制",
        model_window=124_000,
    )
    warmed = await service.knowledge_context(
        "调整 Mac Workbench issue 运行控制",
        model_window=124_000,
    )

    assert all(
        result.code is HarnessKnowledgeStatusCode.READY
        for result in (engine, terminal, workbench, warmed)
    )
    assert all(
        result.bundle is not None
        for result in (engine, terminal, workbench, warmed)
    )
    assert engine.bundle is not None
    assert terminal.bundle is not None
    assert workbench.bundle is not None
    assert any(
        path in engine.bundle.source_paths
        for path in (
            "src/naumi_agent/orchestrator/engine.py",
            "src/naumi_agent/orchestrator/context_assembly.py",
        )
    )
    assert any(
        path.startswith("frontend/terminal-ui/")
        for path in terminal.bundle.source_paths
    )
    assert any(
        path.startswith("apps/macos/NaumiAgentWorkbench/")
        or path.startswith("src/naumi_agent/workbench/")
        for path in workbench.bundle.source_paths
    )
    assert (
        set(engine.bundle.source_paths)
        != set(terminal.bundle.source_paths)
        != set(workbench.bundle.source_paths)
    )
    for result in (engine, terminal, workbench):
        assert result.bundle is not None
        assert "AGENTS.md" in result.bundle.source_paths
        assert result.bundle.l0.estimated_tokens <= 1_000
        assert result.bundle.l1.estimated_tokens <= 8_000
        assert result.bundle.total_tokens <= 12_000
        assert result.bundle.total_tokens <= int(124_000 * 0.15)
    assert warmed.cache_hit
    assert warmed.index_fingerprint == workbench.index_fingerprint


@pytest.mark.asyncio
async def test_copied_real_profile_edit_revokes_exact_digest_trust(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).resolve().parents[2]
    workspace = tmp_path / "workspace-copy"
    (workspace / ".naumi").mkdir(parents=True)
    shutil.copy2(repository / ".naumi/harness.yaml", workspace / ".naumi/harness.yaml")
    shutil.copy2(repository / "AGENTS.md", workspace / "AGENTS.md")
    shutil.copy2(repository / "README.md", workspace / "README.md")
    service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "copy-trust.db"),
    )
    await service.trust(source="h2_copy_test")
    before = await service.knowledge_context("读取规则", model_window=124_000)

    profile_path = workspace / ".naumi/harness.yaml"
    profile_path.write_text(
        profile_path.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    after = await service.knowledge_context("读取规则", model_window=124_000)

    assert before.code is HarnessKnowledgeStatusCode.READY
    assert after.code is HarnessKnowledgeStatusCode.UNTRUSTED
    assert after.bundle is None
