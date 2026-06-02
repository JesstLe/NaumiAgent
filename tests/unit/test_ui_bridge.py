from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from naumi_agent.orchestrator.engine import AgentResult, AgentRuntimeMode, AgentUsage
from naumi_agent.safety.permissions import PermissionMode
from naumi_agent.ui.bridge import JsonlEngineBridge, resolve_config_path
from naumi_agent.ui.protocol import (
    ClientEventType,
    ServerEventType,
    decode_jsonl_line,
    encode_jsonl,
    make_envelope,
)


class _FakeRouter:
    def resolve_model(self, tier: str) -> str:
        return f"fake-{tier}"


class _FakeEngine:
    def __init__(self) -> None:
        self.runtime_mode = AgentRuntimeMode.DEFAULT
        self.permission_mode = PermissionMode.MODERATE
        self.workspace_root = Path.cwd()
        self.usage = AgentUsage(total_input_tokens=12, total_output_tokens=3, turns=1)
        self.router = _FakeRouter()
        self.permission_confirmer = None
        self.shutdown_called = False
        self._session = None

    def set_permission_confirmer(self, confirmer: Any) -> None:
        self.permission_confirmer = confirmer

    def set_runtime_mode(self, mode: str) -> AgentRuntimeMode:
        self.runtime_mode = AgentRuntimeMode(mode)
        self.permission_mode = (
            PermissionMode.BYPASS if mode == "bypass" else PermissionMode.MODERATE
        )
        return self.runtime_mode

    def cycle_runtime_mode(self) -> AgentRuntimeMode:
        next_mode = {
            AgentRuntimeMode.DEFAULT: AgentRuntimeMode.PLAN,
            AgentRuntimeMode.PLAN: AgentRuntimeMode.BYPASS,
            AgentRuntimeMode.BYPASS: AgentRuntimeMode.DEFAULT,
        }[self.runtime_mode]
        return self.set_runtime_mode(next_mode.value)

    def get_context_info(self) -> dict[str, Any]:
        return {"used": 12, "window": 256000, "percentage": 0.1}

    def get_budget_info(self) -> dict[str, Any]:
        return {"used_usd": 0.01, "max_usd": 5.0, "percentage": 0.2}

    async def run_streaming(self, task: str, on_event: Any) -> AgentResult:
        await on_event("turn_start", {"turn": 1, "model": "fake-capable"})
        await on_event("response_start", {})
        await on_event("token", {"content": f"收到: {task}"})
        await on_event("response_end", {})
        await on_event(
            "tool_start",
            {
                "name": "file_read",
                "call_id": "call-1",
                "args": '{"file_path": "README.md"}',
            },
        )
        await on_event(
            "tool_end",
            {
                "name": "file_read",
                "call_id": "call-1",
                "status": "success",
                "duration_ms": 7,
                "content": "ok",
            },
        )
        return AgentResult(status="completed", response="完成", usage=self.usage)

    async def shutdown(self) -> None:
        self.shutdown_called = True

    async def list_sessions(self, page: int = 1, page_size: int = 20) -> tuple[list[Any], int]:
        session = SimpleNamespace(
            id="session-1",
            title="历史会话",
            messages=[
                {"role": "user", "content": "旧问题"},
                {"role": "assistant", "content": "旧回答"},
            ],
        )
        return [session], 1

    async def load_session(self, session_id: str) -> bool:
        if session_id != "session-1":
            return False
        self._session = SimpleNamespace(
            id="session-1",
            title="历史会话",
            messages=[
                {"role": "user", "content": "旧问题"},
                {"role": "assistant", "content": "旧回答"},
            ],
        )
        return True


def _records(writer: io.StringIO) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in writer.getvalue().splitlines()
        if line.strip()
    ]


def test_bridge_resolve_config_path_uses_existing_relative_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("log_level: DEBUG\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert resolve_config_path("config.yaml") == "config.yaml"


def test_bridge_resolve_config_path_falls_back_to_repo_config() -> None:
    resolved = Path(resolve_config_path("__missing_naumi_config__.yaml"))

    assert resolved.name == "config.yaml"
    assert resolved.exists()
    assert resolved.parent.name == "NaumiAgent"


def test_protocol_decodes_strict_jsonl() -> None:
    record = make_envelope(ServerEventType.READY, {"ok": True})
    line = encode_jsonl(record)
    decoded = decode_jsonl_line(line)
    assert decoded["type"] == "ready"
    assert decoded["payload"] == {"ok": True}

    with pytest.raises(ValueError, match="缺少 type"):
        decode_jsonl_line('{"payload":{}}\n')


@pytest.mark.asyncio
async def test_bridge_streams_engine_events_as_ui_messages() -> None:
    engine = _FakeEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.submit("你好", request_id="submit-1")
    assert bridge._run_task is not None
    await bridge._run_task

    records = _records(writer)
    event_types = [record["type"] for record in records]
    assert "user/message" in event_types
    assert "run/started" in event_types
    assert "run/completed" in event_types
    assert event_types.count("ui/message") >= 4
    assert any(
        record["type"] == "ui/message"
        and record["payload"].get("type") == "tool_use"
        and record["payload"].get("tool_name") == "file_read"
        and record["payload"].get("tool_call_id") == "call-1"
        for record in records
    )
    assert any(record["type"] == "runtime/status" for record in records)


@pytest.mark.asyncio
async def test_bridge_mode_and_permission_round_trip() -> None:
    engine = _FakeEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record(
        {
            "id": "mode-1",
            "type": ClientEventType.SET_MODE,
            "payload": {"mode": "bypass"},
        }
    )
    assert engine.runtime_mode == AgentRuntimeMode.BYPASS

    permission_task = asyncio.create_task(
        bridge.confirm_permission(
            {
                "call_id": "call-1",
                "tool_name": "bash_run",
                "arguments": {"command": "rm -rf tmp"},
            }
        )
    )
    await asyncio.sleep(0)
    assert any(record["type"] == "permission/request" for record in _records(writer))

    await bridge.handle_client_record(
        {
            "id": "perm-1",
            "type": ClientEventType.PERMISSION_RESPONSE,
            "payload": {"request_id": "call-1", "choice": "allow"},
        }
    )
    assert await permission_task == "allow"
    assert any(record["type"] == "permission/resolved" for record in _records(writer))


@pytest.mark.asyncio
async def test_bridge_resume_replays_session_messages() -> None:
    engine = _FakeEngine()
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.handle_client_record(
        {"id": "resume-1", "type": ClientEventType.RESUME, "payload": {}}
    )

    records = _records(writer)
    assert any(record["type"] == "session/replayed" for record in records)
    replayed = [
        record["payload"]
        for record in records
        if record["type"] == "ui/message"
    ]
    assert any(
        message.get("type") == "user" and message.get("content") == "旧问题"
        for message in replayed
    )
    assert any(
        message.get("type") == "assistant_stream" and message.get("content") == "旧回答"
        for message in replayed
    )
