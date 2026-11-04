"""Regression coverage for streaming redaction and committed-output recovery."""

from __future__ import annotations

import gc
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.config.settings import AppConfig
from naumi_agent.model.router import ModelResponse, StreamChunk
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.safety import guardrails


def scanner():
    cls = getattr(guardrails, "IncrementalRedactor", None)
    assert cls is not None, "IncrementalRedactor has not been implemented"
    return cls()


CASES = [
    ('before api_key="' + "A" * 30 + '" after', "A" * 30),
    ("before api-key='" + "A" * 30 + "' after", "A" * 30),
    ("before password=abcdefgh after", "abcdefgh"),
    ("before token=" + "T" * 30 + " after", "T" * 30),
    ("before secret=" + "S" * 30 + " after", "S" * 30),
    ("before sk-" + "K" * 30 + " after", "K" * 30),
    ("before ghp_" + "G" * 48 + " after", "G" * 48),
    ("before gho_" + "O" * 48 + " after", "O" * 48),
    ("token=password=abcdefgh after", "abcdefgh"),
]


@pytest.mark.parametrize(("source", "secret"), CASES)
def test_incremental_every_split_matches_whole_redaction(source, secret):
    expected = guardrails.OutputGuardrail.redact(source)
    for cut in range(len(source) + 1):
        redactor = scanner()
        result = redactor.feed(source[:cut]) + redactor.feed(source[cut:]) + redactor.finish()
        assert secret not in result
        assert result == expected
    redactor = scanner()
    result = "".join(redactor.feed(c) for c in source) + redactor.finish()
    assert result == expected


def test_incremental_safe_first_chunk_is_not_globally_buffered():
    redactor = scanner()
    text = "abcdefghijklmnopqrstuvwxyz普通回答"
    assert redactor.feed(text) == text
    assert redactor.finish() == ""


@pytest.mark.parametrize("prefix", ["sk-", "ghp_", "gho_", "api_key=", "password="])
def test_incremental_large_secret_is_consumed_with_bounded_state(prefix):
    redactor = scanner()
    result = redactor.feed(prefix)
    for _ in range(4096):
        result += redactor.feed("Z" * 128)
        assert redactor.buffered_chars <= 64
    result += redactor.feed(" safe text") + redactor.finish()
    assert "Z" not in result
    assert result.endswith(" safe text")
    assert len(result) < 100


def test_incremental_long_separator_does_not_hold_entire_output():
    redactor = scanner()
    result = redactor.feed("api_key=")
    for _ in range(256):
        result += redactor.feed(" " * 128)
        assert redactor.buffered_chars <= 64
    result += redactor.feed("A" * 30 + "\n") + redactor.finish()
    assert "A" * 30 not in result
    assert result.startswith("api_key=" + " " * 32768)


@pytest.mark.parametrize(
    "text",
    [
        "normal response",
        "tokenization in a passwordless world",
        "sk-short",
        "password=small",
        "api_key=too-short",
        "ghp_tiny",
        "a secret?",
    ],
)
def test_incremental_near_misses_preserve_text(text):
    redactor = scanner()
    assert "".join(redactor.feed(c) for c in text) + redactor.finish() == text


@pytest.fixture
async def isolated_engine(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("NAUMI_RUNTIME_PAYLOAD_KEY", "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=")
    engine = AgentEngine(
        AppConfig(
            _env_file=None,
            workspace_root=str(tmp_path),
            memory={
                "session_db_path": str(tmp_path / "sessions.db"),
                "vector_db_path": str(tmp_path / "vectors"),
                "long_term_enabled": False,
            },
            personal={"data_dir": str(tmp_path / "personal")},
            safety={"max_turns": 2, "allowed_dirs": [str(tmp_path)]},
        )
    )
    await engine.create_session(title="isolated stream regression")
    engine._messages = [{"role": "user", "content": "回答合成测试问题"}]
    engine._full_history = list(engine._messages)
    try:
        yield engine
    finally:
        await engine.shutdown()
        gc.collect()


@pytest.mark.parametrize("with_tools", [False, True])
async def test_stream_redacts_tokens_return_and_persisted_history(isolated_engine, with_tools):
    engine = isolated_engine
    source = "normal prefix api_key=" + "A" * 30 + " done"
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    async def stream(**kwargs):
        for char in source:
            yield StreamChunk(token=char)
        yield StreamChunk(finish_reason="stop")

    tools = (
        [
            {
                "type": "function",
                "function": {
                    "name": "file_read",
                    "description": "read",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        if with_tools
        else None
    )
    with patch.object(engine._router, "stream", new=stream):
        result = await engine._react_loop_streaming(tools, emit)
    await engine._save_session()
    saved = await engine.session_store.load(engine._session.id)
    tokens = "".join(d.get("content", "") for k, d in events if k == "token")
    assert result.status == "completed"
    assert "A" * 30 not in tokens
    assert tokens == result.response == saved.messages[-1]["content"]


async def test_non_streaming_history_uses_safe_final(isolated_engine):
    engine = isolated_engine
    response = ModelResponse(content="api_key=" + "A" * 30, model="test")
    with patch.object(engine._router, "call", new=AsyncMock(return_value=response)):
        result = await engine._react_loop(None)
    await engine._save_session()
    saved = await engine.session_store.load(engine._session.id)
    assert "A" * 30 not in result.response
    assert saved.messages[-1]["content"] == result.response


async def test_unpublished_partial_is_discarded_before_fallback(isolated_engine):
    engine = isolated_engine
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    async def stream(**kwargs):
        yield StreamChunk(token="api_key=")
        raise ConnectionError("synthetic pre-visible failure")

    recovery = AsyncMock(return_value=ModelResponse(content="recovered answer", model="test"))
    # The tool guard keeps this short text unpublished even if the redactor can
    # safely release the nonsecret field label itself.
    tools = [
        {
            "type": "function",
            "function": {
                "name": "file_read",
                "description": "read",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    with (
        patch.object(engine._router, "stream", new=stream),
        patch.object(engine._router, "call", new=recovery),
    ):
        result = await engine._react_loop_streaming(tools, emit)
    tokens = "".join(d.get("content", "") for k, d in events if k == "token")
    recovery.assert_awaited_once()
    assert result.status == "completed"
    assert result.response == tokens == "recovered answer"
    assert engine._full_history[-1]["content"] == "recovered answer"


async def test_visible_partial_fails_without_second_model_call(isolated_engine):
    engine = isolated_engine
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    async def stream(**kwargs):
        yield StreamChunk(token="already visible safe prefix. ")
        raise ConnectionError("synthetic post-visible failure")

    recovery = AsyncMock(return_value=ModelResponse(content="must not be used", model="test"))
    with (
        patch.object(engine._router, "stream", new=stream),
        patch.object(engine._router, "call", new=recovery),
    ):
        result = await engine._react_loop_streaming(None, emit)
    tokens = "".join(d.get("content", "") for k, d in events if k == "token")
    recovery.assert_not_awaited()
    assert result.status == "failed"
    assert result.response == tokens == "already visible safe prefix. "
    assert result.error
    assert any(k == "error" for k, d in events)
    assert "must not be used" not in repr(engine._full_history)


async def test_fallback_drops_failed_stream_completed_tool_snapshot(isolated_engine):
    engine = isolated_engine
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    async def stream(**kwargs):
        yield StreamChunk(
            tool_call={
                0: {
                    "id": "stale-call",
                    "type": "function",
                    "function": {"name": "file_read", "arguments": '{"path":"stale"}'},
                }
            },
            finish_reason="tool_calls",
        )
        raise ConnectionError("synthetic broken usage tail")

    recovery = AsyncMock(return_value=ModelResponse(content="recovered answer", model="test"))
    execution = AsyncMock(return_value=[])
    with (
        patch.object(engine._router, "stream", new=stream),
        patch.object(engine._router, "call", new=recovery),
        patch.object(engine, "_execute_tool_calls", new=execution),
    ):
        result = await engine._react_loop_streaming(None, emit)
    execution.assert_not_awaited()
    assert result.status == "completed"
    assert result.response == "recovered answer"


async def test_reasoning_history_is_separate_from_model_private_context(isolated_engine):
    engine = isolated_engine
    secret = "sk-" + "A" * 30
    args = '{"content":"' + secret + '"}'
    engine._append_message(
        {
            "role": "assistant",
            "content": secret,
            "reasoning_content": secret,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "file_write", "arguments": args},
                }
            ],
        }
    )
    assert engine._messages[-1]["reasoning_content"] == secret
    assert engine._messages[-1]["tool_calls"][0]["function"]["arguments"] == args
    assert secret not in engine._full_history[-1]["content"]
    assert secret not in engine._full_history[-1]["reasoning_content"]
    assert engine._full_history[-1] is not engine._messages[-1]
    await engine._save_session()
    saved = await engine.session_store.load(engine._session.id)
    assert secret not in saved.messages[-1]["reasoning_content"]


async def test_thinking_events_are_incrementally_redacted(isolated_engine):
    engine = isolated_engine
    secret = "sk-" + "A" * 30
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    async def stream(**kwargs):
        for char in "safe thought " + secret + " done":
            yield StreamChunk(thinking=char)
        yield StreamChunk(token="normal answer", finish_reason="stop")

    with patch.object(engine._router, "stream", new=stream):
        result = await engine._react_loop_streaming(None, emit)
    deltas = "".join(d.get("content", "") for k, d in events if k == "thinking_delta")
    ending = [d["content"] for k, d in events if k == "thinking_end"]
    assert secret not in deltas
    assert ending == [deltas]
    assert deltas == guardrails.OutputGuardrail.redact("safe thought " + secret + " done")
    assert result.response == "normal answer"


async def test_secret_spanning_output_continuation_stays_redacted(isolated_engine):
    engine = isolated_engine
    secret = "sk-" + "A" * 40
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    async def stream(**kwargs):
        yield StreamChunk(token=secret[:13], finish_reason="length")

    recovery = AsyncMock(return_value=ModelResponse(content=secret + " done", model="test"))
    with (
        patch.object(engine._router, "stream", new=stream),
        patch.object(engine._router, "call", new=recovery),
    ):
        result = await engine._react_loop_streaming(None, emit)
    tokens = "".join(d.get("content", "") for k, d in events if k == "token")
    assert "A" not in tokens.replace("REDACTED_API_KEY", "")
    assert tokens == result.response == guardrails.OutputGuardrail.redact(secret + " done")


async def test_partial_failure_marks_history_and_drops_undecided_secret(isolated_engine):
    engine = isolated_engine
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    async def stream(**kwargs):
        yield StreamChunk(token="visible prefix. ")
        yield StreamChunk(token="sk-" + "A" * 10)
        raise ConnectionError("synthetic interrupted secret")

    with patch.object(engine._router, "stream", new=stream):
        result = await engine._react_loop_streaming(None, emit)
    await engine._save_session()
    saved = await engine.session_store.load(engine._session.id)
    assert result.status == "failed"
    assert result.response == "visible prefix. "
    assert saved.messages[-1]["content"] == result.response
    assert saved.messages[-1]["metadata"]["interrupted"] is True
    assert "metadata" not in engine._messages[-1]
    assert [k for k, d in events].count("response_end") == 1


async def test_failed_fallback_does_not_persist_discarded_text(isolated_engine):
    engine = isolated_engine

    async def emit(kind, data):
        pass

    async def stream(**kwargs):
        yield StreamChunk(token="sk-")
        raise ConnectionError("synthetic original failure")

    recovery = AsyncMock(side_effect=ConnectionError("synthetic fallback failure"))
    with (
        patch.object(engine._router, "stream", new=stream),
        patch.object(engine._router, "call", new=recovery),
    ):
        with pytest.raises(ConnectionError, match="fallback failure"):
            await engine._react_loop_streaming(None, emit)
    assert not any(m["role"] == "assistant" for m in engine._full_history)


async def test_public_stream_interruption_records_failed_run(isolated_engine):
    engine = isolated_engine
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    async def stream(**kwargs):
        yield StreamChunk(token="已收到的安全答复内容已经超过首次输出保护窗口。" * 2)
        raise ConnectionError("synthetic interruption after visible content")

    recovery = AsyncMock(return_value=ModelResponse(content="must not be used", model="test"))
    with (
        patch.object(engine._router, "stream", new=stream),
        patch.object(engine._router, "call", new=recovery),
    ):
        result = await engine.run_streaming("直接回答这个问题", emit)
    recovery.assert_not_awaited()
    assert result.status == "failed"
    assert result.response
    runs = await engine.chat_run_store.list_runs(engine._session.id)
    assert runs[0].status == "failed"
    saved = await engine.session_store.load(engine._session.id)
    assert saved.messages[-1]["metadata"]["interrupted"] is True
    assert saved.messages[-1]["content"] == result.response
    assert any(kind == "completion_receipt" for kind, _ in events)


def test_incremental_finish_is_idempotent_and_terminal():
    redactor = scanner()
    assert redactor.feed("sk-") == ""
    assert redactor.finish() == "sk-"
    assert redactor.finish() == ""
    with pytest.raises(ValueError, match="脱敏流已结束"):
        redactor.feed("after EOF")
