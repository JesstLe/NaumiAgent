from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.api.routes.messages import create_session
from naumi_agent.api.schemas import SessionCreate


class _RecordingStore:
    def __init__(self) -> None:
        self.created: dict[str, object] = {}

    async def create_session(self, **kwargs):
        self.created = kwargs
        now = datetime(2026, 9, 11, 12, 0, 0)
        return SimpleNamespace(
            id="bound-session",
            title=kwargs.get("title") or "新会话",
            model=kwargs.get("model") or "test-model",
            messages=[],
            created_at=now,
            updated_at=now,
            total_tokens=0,
            total_cost_usd=0.0,
            status="active",
            pinned_at=None,
            workspace_root=kwargs.get("workspace_root") or "",
            git_branch=kwargs.get("git_branch") or "",
            summary="",
        )


@pytest.mark.asyncio
async def test_api_created_session_uses_the_daemon_workspace_authority(tmp_path: Path) -> None:
    store = _RecordingStore()
    async def create_bound_session(**kwargs):
        return await store.create_session(
            **kwargs,
            workspace_root=str(tmp_path),
            git_branch="codex/project-workspaces",
        )

    engine = SimpleNamespace(
        workspace_root=tmp_path,
        session_store=store,
        current_git_branch=lambda: "codex/project-workspaces",
        create_session=create_bound_session,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(engine=engine)))

    response = await create_session(
        SessionCreate(title="指定目录会话", model="test-model"),
        request,
        auth="test",
    )

    assert store.created["workspace_root"] == str(tmp_path)
    assert store.created["git_branch"] == "codex/project-workspaces"
    assert response.workspace_root == str(tmp_path)
    assert response.git_branch == "codex/project-workspaces"
