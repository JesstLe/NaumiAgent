"""Unit tests for workbench API routes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from naumi_agent.api.routes.workbench import get_workbench_snapshot


class _FakeSessionStore:
    def __init__(self, exists: bool) -> None:
        self.exists = exists

    async def load(self, session_id: str):
        if not self.exists:
            return None
        return SimpleNamespace(id=session_id)


class _FakeWorkbenchService:
    async def dashboard_snapshot(self, session_id: str):
        return {
            "session_id": session_id,
            "missions": [],
            "tasks": [],
            "issues": [],
            "failures": [],
            "events": [],
        }


class _FakeEngine:
    def __init__(self, exists: bool) -> None:
        self.session_store = _FakeSessionStore(exists)
        self.workbench_service = _FakeWorkbenchService()
        self.loaded: list[str] = []

    async def load_session(self, session_id: str) -> bool:
        self.loaded.append(session_id)
        return self.session_store.exists


def _fake_request(engine: _FakeEngine):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(engine=engine)))


@pytest.mark.asyncio
async def test_workbench_snapshot_endpoint_requires_existing_session() -> None:
    engine = _FakeEngine(exists=False)

    with pytest.raises(HTTPException) as exc:
        await get_workbench_snapshot("missing", _fake_request(engine), auth="test")

    assert exc.value.status_code == 404
    assert exc.value.detail == "Session not found"


@pytest.mark.asyncio
async def test_workbench_snapshot_endpoint_returns_service_snapshot() -> None:
    engine = _FakeEngine(exists=True)

    response = await get_workbench_snapshot("sess-1", _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert response["session_id"] == "sess-1"
    assert "missions" in response
    assert "events" in response
