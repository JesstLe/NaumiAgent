from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.store import HarnessStore, resolve_harness_db_path
from naumi_agent.harness.tools import create_harness_tools
from naumi_agent.harness.trust import HarnessTrustStore
from naumi_agent.model.router import ModelResponse, StreamChunk, TokenUsage
from naumi_agent.orchestrator.engine import AgentEngine, AgentRuntimeMode
from naumi_agent.tools.base import ToolResult


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


async def _untrusted_engine(tmp_path: Path, task: str) -> AgentEngine:
    engine = await _engine(tmp_path)
    await engine.harness_service.untrust()
    await engine._begin_harness_completion_run(task, run_id="core-action-gate")
    assert engine._active_harness_run is None
    assert engine._active_action_evidence is not None
    return engine


@pytest.mark.asyncio
async def test_engine_wires_user_state_harness_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace-default-store"
    workspace.mkdir()
    state_home = tmp_path / "user-state"
    monkeypatch.setenv("NAUMI_STATE_HOME", str(state_home))
    engine = AgentEngine(
        AppConfig(
            workspace_root=str(workspace),
            memory=MemoryConfig(
                session_db_path=str(tmp_path / "sessions-default-store.db"),
                vector_db_path=str(tmp_path / "chroma-default-store"),
                long_term_enabled=False,
            ),
        )
    )
    try:
        assert isinstance(engine.harness_service.store, HarnessStore)
        assert engine.harness_service.store.db_path == resolve_harness_db_path()
        assert not engine.harness_service.store.db_path.is_relative_to(workspace)
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_engine_hides_premature_completion_until_current_check_passes(
    tmp_path: Path,
) -> None:
    engine = await _engine(tmp_path)
    (engine.workspace_root / "source.py").write_text("VALUE = 2\n", encoding="utf-8")
    engine._record_action_evidence_tool_success(
        tool_name="file_write",
        arguments={"path": "source.py"},
        read_only=False,
    )
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
    engine._record_action_evidence_tool_success(
        tool_name="file_write",
        arguments={"path": "source.py"},
        read_only=False,
    )
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


@pytest.mark.asyncio
async def test_streaming_change_request_retries_plan_only_then_requires_file_change(
    tmp_path: Path,
) -> None:
    engine = await _engine(tmp_path)
    events: list[tuple[str, dict[str, object]]] = []
    call_count = 0
    stream_kwargs: list[dict[str, object]] = []

    async def stream_response(**kwargs: object):
        nonlocal call_count
        call_count += 1
        stream_kwargs.append(kwargs)
        if call_count == 1:
            yield StreamChunk(token="只描述页面方案，不执行")
            yield StreamChunk(finish_reason="stop")
            return
        if call_count == 2:
            yield StreamChunk(
                tool_call={
                    0: {
                        "id": "write-page",
                        "function": {
                            "name": "file_write",
                            "arguments": json.dumps(
                                {"path": "gallery.html", "content": "<main>完成</main>"}
                            ),
                        },
                    }
                },
                finish_reason="tool_calls",
            )
            return
        yield StreamChunk(token="页面已创建并验证。")
        yield StreamChunk(finish_reason="stop")

    async def execute_file(tool_call, **_: object):
        (engine.workspace_root / "gallery.html").write_text(
            "<main>完成</main>",
            encoding="utf-8",
        )
        engine._record_action_evidence_tool_success(
            tool_name="file_write",
            arguments={"path": "gallery.html"},
            read_only=False,
        )
        return ToolResult(
            call_id=tool_call.id,
            status="success",
            content="已创建 gallery.html",
        )

    async def on_event(event: str, data: dict[str, object]) -> None:
        events.append((event, data))

    try:
        with (
            patch.object(engine._router, "stream", new=stream_response),
            patch.object(engine, "_execute_tool", new=execute_file),
        ):
            result = await engine._react_loop_streaming(
                engine.tool_registry.get_openai_tools(),
                on_event,
            )

        token_text = "".join(
            str(data.get("content", ""))
            for event, data in events
            if event == "token"
        )
        assert token_text == "页面已创建并验证。"
        assert call_count == 3
        assert stream_kwargs[1]["tool_choice"] == "required"
        forced_names = {
            str(schema["function"]["name"])
            for schema in stream_kwargs[1]["tools"]
        }
        assert forced_names <= {
            "file_write",
            "file_edit",
            "bash_run",
            "self_modify",
            "output_publish",
        }
        assert (engine.workspace_root / "gallery.html").is_file()
        assert any(
            event == "phase_summary" and "尚未执行工作区修改" in str(data)
            for event, data in events
        )
        assert result.status == "completed"
        assert result.harness_receipt is not None
        assert result.harness_receipt.changed_files == ("gallery.html",)
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_core_action_gate_blocks_three_plan_only_responses_without_harness(
    tmp_path: Path,
) -> None:
    engine = await _untrusted_engine(tmp_path, "创建一个高级 HTML 页面")
    engine.set_runtime_mode(AgentRuntimeMode.BYPASS)
    responses = [
        ModelResponse(content="我先规划页面结构。", usage=_usage(), model="test-model"),
        ModelResponse(content="页面已经设计完成。", usage=_usage(), model="test-model"),
        ModelResponse(content="页面方案已经准备好。", usage=_usage(), model="test-model"),
    ]

    try:
        with patch.object(
            engine._router,
            "call",
            new_callable=AsyncMock,
            side_effect=responses,
        ):
            result = await engine._react_loop(engine.tool_registry.get_openai_tools())

        assert result.status == "failed"
        assert result.error == "模型没有执行用户要求的工作区修改，本次任务未完成，请重试。"
        assert not any(
            message.get("role") == "assistant"
            and message.get("content") in {
                "我先规划页面结构。",
                "页面已经设计完成。",
                "页面方案已经准备好。",
            }
            for message in engine._messages
        )
        assert any(
            "请立即调用 file_write" in str(message.get("content", ""))
            for message in engine._messages
        )
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_streaming_action_recovery_survives_empty_forced_tool_turn(
    tmp_path: Path,
) -> None:
    engine = await _untrusted_engine(tmp_path, "创建一个高级 HTML 页面")
    engine.set_runtime_mode(AgentRuntimeMode.BYPASS)
    events: list[tuple[str, dict[str, object]]] = []
    call_count = 0
    stream_kwargs: list[dict[str, object]] = []

    async def stream_response(**kwargs: object):
        nonlocal call_count
        call_count += 1
        stream_kwargs.append(kwargs)
        if call_count == 1:
            yield StreamChunk(token="我先整理页面方案。")
            yield StreamChunk(finish_reason="stop")
            return
        if call_count == 2:
            yield StreamChunk(finish_reason="stop")
            return
        if call_count == 3:
            yield StreamChunk(token="接下来准备写入页面。")
            yield StreamChunk(finish_reason="stop")
            return
        if call_count == 4:
            yield StreamChunk(
                tool_call={
                    0: {
                        "id": "write-after-empty",
                        "function": {
                            "name": "file_write",
                            "arguments": json.dumps(
                                {"path": "recovered.html", "content": "<main>恢复成功</main>"}
                            ),
                        },
                    }
                },
                finish_reason="tool_calls",
            )
            return
        yield StreamChunk(token="recovered.html 已创建。")
        yield StreamChunk(finish_reason="stop")

    async def on_event(event: str, data: dict[str, object]) -> None:
        events.append((event, data))

    try:
        with patch.object(engine._router, "stream", new=stream_response):
            result = await engine._react_loop_streaming(
                engine.tool_registry.get_openai_tools(),
                on_event,
            )

        assert result.status == "completed"
        assert result.response == "recovered.html 已创建。"
        assert call_count == 5
        assert all(kwargs.get("tool_choice") == "required" for kwargs in stream_kwargs[1:4])
        assert (engine.workspace_root / "recovered.html").read_text(encoding="utf-8") == (
            "<main>恢复成功</main>"
        )
        recovery_events = [
            data for event, data in events
            if event == "phase_summary" and data.get("phase_kind") == "recovery"
        ]
        assert len(recovery_events) == 3
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_streaming_core_action_gate_hides_plan_and_emits_correction(
    tmp_path: Path,
) -> None:
    engine = await _untrusted_engine(tmp_path, "写一个交互式 HTML 文件")
    events: list[tuple[str, dict[str, object]]] = []
    call_count = 0
    stream_kwargs: list[dict[str, object]] = []

    async def stream_response(**kwargs: object):
        nonlocal call_count
        call_count += 1
        stream_kwargs.append(kwargs)
        text = "我先给出页面方案。" if call_count == 1 else "页面已经完成。"
        yield StreamChunk(token=text)
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
        assert token_text == ""
        assert result.status == "failed"
        assert stream_kwargs[1]["tool_choice"] == "required"
        assert any(
            event == "phase_summary"
            and "尚未执行工作区修改" in str(data)
            for event, data in events
        )
        assert any(
            event == "error"
            and "本次任务未完成" in str(data.get("message", ""))
            for event, data in events
        )
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_core_action_gate_accepts_real_file_write_without_harness(
    tmp_path: Path,
) -> None:
    engine = await _untrusted_engine(tmp_path, "创建 gallery.html 页面")
    engine.set_runtime_mode(AgentRuntimeMode.BYPASS)
    responses = [
        ModelResponse(content="先说明方案。", usage=_usage(), model="test-model"),
        ModelResponse(
            content="",
            tool_calls=[
                {
                    "id": "write-gallery",
                    "function": {
                        "name": "file_write",
                        "arguments": json.dumps(
                            {
                                "path": "gallery.html",
                                "content": "<button>纸上天文台</button>",
                            }
                        ),
                    },
                }
            ],
            usage=_usage(),
            model="test-model",
        ),
        ModelResponse(content="gallery.html 已创建。", usage=_usage(), model="test-model"),
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
        assert result.response == "gallery.html 已创建。"
        assert (engine.workspace_root / "gallery.html").read_text(encoding="utf-8") == (
            "<button>纸上天文台</button>"
        )
        assert engine._active_action_evidence is not None
        assert engine._active_action_evidence.successful_tools == ["file_write"]
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_core_action_gate_rejects_noop_write_and_todo_evidence(
    tmp_path: Path,
) -> None:
    engine = await _untrusted_engine(tmp_path, "修改 source.py 文件")
    engine.set_runtime_mode(AgentRuntimeMode.BYPASS)
    responses = [
        ModelResponse(
            content="",
            tool_calls=[
                {
                    "id": "noop-write",
                    "function": {
                        "name": "file_write",
                        "arguments": json.dumps(
                            {"path": "source.py", "content": "VALUE = 1\n"}
                        ),
                    },
                }
            ],
            usage=_usage(),
            model="test-model",
        ),
        ModelResponse(content="已经修改。", usage=_usage(), model="test-model"),
        ModelResponse(content="确认完成。", usage=_usage(), model="test-model"),
        ModelResponse(content="再次确认完成。", usage=_usage(), model="test-model"),
    ]

    try:
        with patch.object(
            engine._router,
            "call",
            new_callable=AsyncMock,
            side_effect=responses,
        ):
            result = await engine._react_loop(engine.tool_registry.get_openai_tools())

        assert result.status == "failed"
        assert engine._active_action_evidence is not None
        assert engine._active_action_evidence.successful_tools == ["file_write"]
        engine._record_action_evidence_tool_success(
            tool_name="todo_write",
            arguments={"path": "source.py"},
            read_only=False,
        )
        assert engine._active_action_evidence.successful_tools == ["file_write"]
    finally:
        await engine.shutdown()
