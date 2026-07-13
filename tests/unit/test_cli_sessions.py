"""CLI session command tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import pytest

from naumi_agent.main import _has_user_conversation, _replay_session_to_cli, _resume_latest


@dataclass
class _FakeSession:
    id: str
    messages: list[dict[str, Any]]
    title: str = ""
    model: str = ""
    total_tokens: int = 0
    total_cost_usd: float = 0.0


class _ReplayCLI:
    def __init__(self) -> None:
        self.output: list[str] = ["old transcript\n"]
        self.status = ""
        self.reset_count = 0

    def reset_output(self) -> None:
        self.output.clear()
        self.reset_count += 1

    def append_output(self, text: str) -> None:
        self.output.append(text)

    def set_status(self, text: str) -> None:
        self.status = text

    def transcript(self) -> str:
        return "".join(self.output)


class _ReplayRouter:
    def resolve_model(self, _tier: str) -> str:
        return "test-model"


class _ReplayUsage:
    total_input_tokens = 10
    total_output_tokens = 5


class _ReplayEngine:
    router = _ReplayRouter()
    usage = _ReplayUsage()
    workspace_root = "/tmp/workspace"

    def get_context_info(self) -> dict[str, int]:
        return {"used": 1000, "window": 2000, "percentage": 50}

    def get_budget_info(self) -> dict[str, object]:
        return {"enabled": False, "used_usd": 0.1, "max_usd": None}


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

    def test_replay_session_replaces_screen_without_inline_status(self) -> None:
        cli = _ReplayCLI()
        session = _FakeSession(
            "s1",
            [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "已完成"},
            ],
            title="旧会话",
            model="old-model",
            total_tokens=999,
            total_cost_usd=1.23,
        )

        _replay_session_to_cli(cli, session, engine=_ReplayEngine())

        transcript = cli.transcript()
        assert cli.reset_count == 1
        assert "old transcript" not in transcript
        assert "恢复会话: 旧会话" in transcript
        assert "❯" in transcript
        assert "你好" in transcript
        assert "已完成" in transcript
        assert "会话已恢复" in transcript
        assert "Token: 999" not in transcript
        assert "上下文:" not in transcript
        assert "test-model" in cli.status
        assert "预算: 不限 · 已用 $0.1000" in cli.status

    def test_replay_session_uses_engine_full_history_when_available(self) -> None:
        cli = _ReplayCLI()
        session = _FakeSession(
            "s1",
            [
                {"role": "user", "content": "读文件"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {"name": "file_read", "arguments": "{}"},
                        }
                    ],
                },
            ],
            title="缺失工具结果",
        )
        engine = _ReplayEngine()
        engine._full_history = [
            *session.messages,
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": "[工具调用结果缺失 — 会话恢复时未能找到对应结果]",
            },
        ]

        _replay_session_to_cli(cli, session, engine=engine)

        transcript = cli.transcript()
        assert "工具调用结果缺失" in transcript
        assert "file_read" in transcript
