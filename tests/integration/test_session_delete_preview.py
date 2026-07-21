"""Real Session Store + Harness Store delete-preview integration."""

from __future__ import annotations

from pathlib import Path

import pytest

from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.harness.models import HarnessCompletionContract, HarnessTaskKind
from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.store import HarnessStore
from naumi_agent.harness.trust import HarnessTrustStore
from naumi_agent.runtime.composition import create_agent_engine


@pytest.mark.asyncio
async def test_engine_previews_real_session_and_workspace_scoped_harness_rows(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = AppConfig(
        memory=MemoryConfig(session_db_path=str(tmp_path / "runtime" / "sessions.db")),
        workspace_root=str(workspace),
    )
    engine = create_agent_engine(config)
    harness_store = HarnessStore(tmp_path / "state" / "harness.db")
    engine.harness_service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "state" / "trust.db"),
        store=harness_store,
    )
    try:
        session = await engine.session_store.create_session(title="真实预览")
        session.workspace_root = str(workspace)
        session.add_message("user", "保留这条消息")
        await engine.session_store.save(session)
        await harness_store.start_run(
            workspace_root=workspace,
            contract=HarnessCompletionContract(
                run_id="preview-run",
                session_id=session.id,
                task_kind=HarnessTaskKind.ANALYSIS,
                objective="验证删除预览",
            ),
            tree_fingerprint_before="a" * 64,
            started_at="2026-07-17T12:00:00+08:00",
        )

        preview = await engine.preview_session_delete(session.id)

        assert preview is not None
        assert preview.title == "真实预览"
        assert preview.message_count == 1
        assert preview.workspace_root == str(workspace.resolve())
        assert preview.harness_run_count == 1
        assert await engine.session_store.load(session.id) is not None
        assert await harness_store.get_run("preview-run") is not None
    finally:
        await engine.shutdown()
