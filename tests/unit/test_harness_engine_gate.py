from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.tools import create_harness_tools
from naumi_agent.harness.trust import HarnessTrustStore
from naumi_agent.model.router import ModelResponse, StreamChunk, TokenUsage
from naumi_agent.orchestrator.engine import AgentEngine


def _git(workspace: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=workspace, check=True, capture_output=True)


def _usage() -> TokenUsage:
    return TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2)


async def _engine(tmp_path: Path) -> AgentEngine:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "tests@example.com")
    _git(workspace, "config", "user.name", "Harness Tests")
    (workspace / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    profile = workspace / ".naumi" / "harness.yaml"
    profile.parent.mkdir()
    argv = json.dumps([sys.executable, "-c", "print('unit ok')"])
    profile.write_text(
        "schema_version: 1\n"
        "completion:\n"
        "  correction_attempts: 1\n"
        "checks:\n"
        "  - id: unit\n"
        f"    argv: {argv}\n"
        "    timeout_seconds: 10\n"
        "    when_changed: ['**/*.py']\n"
        "    required_for: [change]\n",
        encoding="utf-8",
    )
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-qm", "fixture")
    engine = AgentEngine(
        AppConfig(
            workspace_root=str(workspace),
            memory=MemoryConfig(
                session_db_path=str(tmp_path / "sessions.db"),
                vector_db_path=str(tmp_path / "chroma"),
                long_term_enabled=False,
            ),
        )
    )
    engine.harness_service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust.db"),
    )
    for tool in create_harness_tools(engine.harness_service):
        engine.tool_registry.register(tool)
    await engine.harness_service.trust(source="test")
    session = await engine.get_or_create_session()
    engine.task_store.set_session(session.id)
    await engine._begin_harness_completion_run("修改 source.py", run_id="engine-gate")
    return engine


@pytest.mark.asyncio
async def test_engine_hides_premature_completion_until_current_check_passes(
    tmp_path: Path,
) -> None:
    engine = await _engine(tmp_path)
    (engine.workspace_root / "source.py").write_text("VALUE = 2\n", encoding="utf-8")
    responses = [
        ModelResponse(content="未经验证就说完成", usage=_usage(), model="test-model"),
        ModelResponse(
            content="",
            tool_calls=[
                {
                    "id": "check-1",
                    "function": {
                        "name": "harness_run_check",
                        "arguments": json.dumps(
                            {"check_id": "unit", "run_id": "engine-gate"}
                        ),
                    },
                }
            ],
            usage=_usage(),
            model="test-model",
        ),
        ModelResponse(content="已验证完成", usage=_usage(), model="test-model"),
    ]

    try:
        with patch.object(
            engine._router,
            "call",
            new_callable=AsyncMock,
            side_effect=responses,
        ):
            result = await engine._react_loop(engine.tool_registry.get_openai_tools())

        assert result.status == "completed"
        assert result.response == "已验证完成"
        assert result.harness_receipt is not None
        assert result.harness_receipt.status == "completed_verified"
        assert result.harness_receipt.changed_files == ("source.py",)
        assert result.harness_receipt.checks[0].status == "passed"
        assert not any(
            message.get("content") == "未经验证就说完成"
            for message in engine._messages
        )
        assert any(
            "缺少必需检查 unit" in str(message.get("content", ""))
            for message in engine._messages
        )
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_completion_contract_context_is_ephemeral(tmp_path: Path) -> None:
    engine = await _engine(tmp_path)
    try:
        await engine._inject_harness_context_snapshot()

        assert any(
            "<naumi_harness_completion_contract>" in str(message.get("content", ""))
            for message in engine._messages
        )
        assert not any(
            "<naumi_harness_completion_contract>" in str(message.get("content", ""))
            for message in engine._full_history
        )
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_streaming_gate_never_emits_premature_completion(tmp_path: Path) -> None:
    engine = await _engine(tmp_path)
    (engine.workspace_root / "source.py").write_text("VALUE = 2\n", encoding="utf-8")
    events: list[tuple[str, dict[str, object]]] = []
    call_count = 0

    async def stream_response(**_: object):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield StreamChunk(token="绝不能展示的未验证完成")
            yield StreamChunk(finish_reason="stop")
            return
        if call_count == 2:
            yield StreamChunk(
                tool_call={
                    0: {
                        "id": "check-stream",
                        "function": {
                            "name": "harness_run_check",
                            "arguments": json.dumps(
                                {"check_id": "unit", "run_id": "engine-gate"}
                            ),
                        },
                    }
                },
                finish_reason="tool_calls",
            )
            return
        yield StreamChunk(token="流式验证完成")
        yield StreamChunk(finish_reason="stop")

    async def on_event(event: str, data: dict[str, object]) -> None:
        events.append((event, data))

    try:
        with patch.object(engine._router, "stream", new=stream_response):
            result = await engine._react_loop_streaming(
                engine.tool_registry.get_openai_tools(),
                on_event,
            )

        token_text = "".join(
            str(data.get("content", ""))
            for event, data in events
            if event == "token"
        )
        assert token_text == "流式验证完成"
        assert result.harness_receipt is not None
        assert result.harness_receipt.status == "completed_verified"
        assert any(event == "harness_completion_correction" for event, _ in events)
        assert any(event == "harness_completion_receipt" for event, _ in events)
    finally:
        await engine.shutdown()
