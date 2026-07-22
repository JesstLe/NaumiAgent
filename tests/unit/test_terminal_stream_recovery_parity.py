"""UI-17.2e golden parity for streaming and terminal error semantics."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.widgets import Input

from naumi_agent.config.settings import AppConfig
from naumi_agent.harness.store import HarnessStore
from naumi_agent.orchestrator.engine import AgentResult, AgentUsage
from naumi_agent.runtime.composition import create_agent_engine
from naumi_agent.tui.app import NaumiApp, StatusBar
from naumi_agent.tui.renderers.registry import TUIRenderer
from naumi_agent.ui.messages.adapter import EngineEventAdapter

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_PATH = (
    PROJECT_ROOT
    / "tests"
    / "fixtures"
    / "ui17"
    / "terminal-stream-recovery-golden.json"
)


def _golden() -> dict[str, object]:
    document = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert set(document) == {
        "schema_version",
        "stream",
        "interrupted",
        "delivery_retry",
    }
    assert document["schema_version"] == 1
    return document


def _public_message(message: object) -> dict[str, object]:
    public = asdict(message)  # type: ignore[arg-type]
    public.pop("message_id", None)
    public.pop("raw_event", None)
    public.pop("raw_data", None)
    public["type"] = public["type"].value
    return json.loads(json.dumps(public, ensure_ascii=False))


def test_stream_and_error_events_match_shared_golden() -> None:
    fixture = _golden()
    stream = fixture["stream"]
    interrupted = fixture["interrupted"]
    assert isinstance(stream, dict)
    assert isinstance(interrupted, dict)

    adapter = EngineEventAdapter()
    scenarios = [*stream["events"], *interrupted["partial_events"]]
    for scenario in scenarios:
        assert isinstance(scenario, dict)
        message = adapter.adapt(scenario["engine_event"], scenario["data"])
        assert message is not None
        assert _public_message(message) == scenario["expected_message"]

    error_record = interrupted["error_record"]
    assert isinstance(error_record, dict)
    error_payload = error_record["payload"]
    assert isinstance(error_payload, dict)
    error_message = adapter.adapt("error", error_payload)
    assert error_message is not None
    assert _public_message(error_message) == interrupted["expected_error_message"]


class _RecordingChat:
    def __init__(self) -> None:
        self.response_starts = 0
        self.tokens: list[str] = []

    def start_response(self) -> None:
        self.response_starts += 1

    def add_response_token(self, token: str) -> None:
        self.tokens.append(token)


def test_tui_renderer_consumes_shared_stream_and_error_golden() -> None:
    fixture = _golden()
    interrupted = fixture["interrupted"]
    assert isinstance(interrupted, dict)

    adapter = EngineEventAdapter()
    renderer = TUIRenderer()
    chat = _RecordingChat()
    status = SimpleNamespace(status_text="")

    for scenario in interrupted["partial_events"]:
        message = adapter.adapt(scenario["engine_event"], scenario["data"])
        assert message is not None
        renderer.render(message, chat, status, None)

    error_record = interrupted["error_record"]
    error_message = adapter.adapt("error", error_record["payload"])
    assert error_message is not None
    renderer.render(error_message, chat, status, None)

    assert chat.response_starts == 2
    assert chat.tokens == [
        interrupted["expected_content"],
        f"**错误**: {error_record['payload']['message']}",
    ]
    assert status.status_text == "执行失败"


@pytest.mark.asyncio
async def test_tui_failed_result_never_renders_success_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _golden()["interrupted"]
    assert isinstance(fixture, dict)
    config = AppConfig(
        workspace_root=str(tmp_path),
        memory={
            "session_db_path": str(tmp_path / "sessions.db"),
            "vector_db_path": str(tmp_path / "chroma"),
            "long_term_enabled": False,
        },
    )
    engine = create_agent_engine(config)
    engine.harness_service = SimpleNamespace(store=HarnessStore(tmp_path / "harness.db"))

    async def run_streaming(task: str, _sink: object) -> AgentResult:
        assert task == fixture["submission_text"]
        return AgentResult(
            status="error",
            error=fixture["error_record"]["payload"]["message"],
            usage=AgentUsage(turns=1),
        )

    monkeypatch.setattr(engine, "run_streaming", run_streaming)
    app = NaumiApp(engine)

    async with app.run_test(size=(100, 30)) as pilot:
        composer = app.query_one("#msg-input", Input)
        composer.value = str(fixture["submission_text"])
        composer.focus()
        await pilot.press("enter")
        for _ in range(80):
            if not app._agent_busy:  # noqa: SLF001
                break
            await pilot.pause(0.05)
        else:
            pytest.fail("TUI 失败结果未在时限内结束")

        status_text = app.query_one(StatusBar).status_text
        assert status_text.startswith("❌ ")
        assert not status_text.startswith("✅ ")
