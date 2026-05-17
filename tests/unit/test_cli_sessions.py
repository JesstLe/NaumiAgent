"""CLI session command tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import pytest

from naumi_agent.main import _has_user_conversation, _resume_latest


@dataclass
class _FakeSession:
    id: str
    messages: list[dict[str, Any]]


class _FakeEngine:
    def __init__(self, sessions: list[_FakeSession]) -> None:
        self.sessions = sessions

    async def list_sessions(
        self,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[_FakeSession], int]:
        start = (page - 1) * page_size
        end = start + page_size
        return self.sessions[start:end], len(self.sessions)


class TestResumeLatest:
    def test_has_user_conversation_skips_system_only_sessions(self) -> None:
        assert _has_user_conversation(
            _FakeSession("empty", [{"role": "system", "content": "prompt"}])
        ) is False
        assert _has_user_conversation(
            _FakeSession("real", [{"role": "user", "content": "你好"}])
        ) is True

    @pytest.mark.asyncio
    async def test_resume_latest_skips_empty_recent_sessions(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        load_session = AsyncMock()
        monkeypatch.setattr("naumi_agent.main._load_session", load_session)

        engine = _FakeEngine([
            _FakeSession("empty-latest", [{"role": "system", "content": "prompt"}]),
            _FakeSession("real-latest", [{"role": "user", "content": "继续之前的话题"}]),
        ])

        await _resume_latest(engine)

        load_session.assert_awaited_once_with(engine, "real-latest")
