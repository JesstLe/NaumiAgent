from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.ui.session_list import build_session_list_snapshot


class _Engine:
    def __init__(self, workspace: Path) -> None:
        self.workspace_root = workspace
        self._session = SimpleNamespace(id="session-current")
        self.calls: list[dict[str, object]] = []

    async def list_sessions(self, **kwargs: object) -> tuple[list[object], int]:
        self.calls.append(kwargs)
        return [
            SimpleNamespace(
                id="session-current",
                title="  当前会话  ",
                model="provider/model",
                updated_at=datetime(2026, 7, 22, 8, 0, tzinfo=UTC),
                git_branch="main",
                workspace_root="must-not-leak",
                summary="private summary",
                messages=[
                    {"role": "user", "content": "secret question"},
                    {"role": "assistant", "content": "secret answer"},
                ],
            ),
            SimpleNamespace(
                id="bad id; rm -rf",
                title="异常",
                messages=[],
            ),
            SimpleNamespace(
                id="s" * 129,
                title="过长标识",
                messages=[],
            ),
        ], 3


@pytest.mark.asyncio
async def test_session_list_is_workspace_scoped_bounded_and_public(tmp_path: Path) -> None:
    engine = _Engine(tmp_path / "workspace")

    snapshot = await build_session_list_snapshot(
        engine,
        page=2,
        page_size=50,
        query="current",
    )
    payload = snapshot.to_protocol_dict()

    assert engine.calls == [{
        "page": 2,
        "page_size": 50,
        "query": "current",
        "workspace_root": str((tmp_path / "workspace").resolve()),
    }]
    assert payload["scope"] == "workspace"
    assert payload["total"] == 3
    assert payload["warnings"] == ["已忽略 2 个标识格式异常的会话。"]
    assert payload["items"] == [{
        "session_id": "session-current",
        "title": "当前会话",
        "model": "provider/model",
        "updated_at": "2026-07-22T08:00:00+00:00",
        "message_count": 2,
        "user_message_count": 1,
        "git_branch": "main",
        "is_current": True,
        "resumable": True,
    }]
    rendered = repr(payload)
    assert "secret question" not in rendered
    assert "private summary" not in rendered
    assert "must-not-leak" not in rendered
