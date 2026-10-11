"""UI-17.2c golden parity for terminal run lifecycle semantics."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.widgets import Input

from naumi_agent.config.settings import AppConfig
from naumi_agent.harness.coordinator import ReconciliationCoordinatorOutcome
from naumi_agent.harness.store import HarnessStore
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
    / "terminal-run-lifecycle-golden.json"
)


def _golden() -> dict[str, object]:
    document = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert set(document) == {
        "schema_version",
        "submission",
        "tool_lifecycle",
        "completion",
        "cancel",
        "capture",
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


def test_engine_events_match_shared_terminal_run_golden() -> None:
    fixture = _golden()
    lifecycle = fixture["tool_lifecycle"]
    completion = fixture["completion"]
    assert isinstance(lifecycle, dict)
    assert isinstance(completion, dict)

    adapter = EngineEventAdapter()
    scenarios = [*lifecycle["events"], completion]
    for scenario in scenarios:
        assert isinstance(scenario, dict)
        message = adapter.adapt(scenario["engine_event"], scenario["data"])
        assert message is not None
        assert _public_message(message) == scenario["expected_message"]


class _RecordingChat:
    def __init__(self) -> None:
        self.prepares: list[str] = []
        self.prepare_ended = 0
        self.started_tools: list[str] = []
        self.ended_tools: list[tuple[str, str, int, str]] = []
        self.mounted: list[object] = []

    def update_tool_prepare(self, text: str) -> None:
        self.prepares.append(text)

    def end_tool_prepare(self) -> None:
        self.prepare_ended += 1

    def start_tool(self, name: str) -> None:
        self.started_tools.append(name)

    def end_tool(
        self,
        name: str,
        status: str,
        duration_ms: int,
        content: str = "",
    ) -> None:
        self.ended_tools.append((name, status, duration_ms, content))

    def mount(self, widget: object) -> None:
        self.mounted.append(widget)


def test_tui_renderer_consumes_same_terminal_run_golden() -> None:
    fixture = _golden()
    lifecycle = fixture["tool_lifecycle"]
    completion = fixture["completion"]
    assert isinstance(lifecycle, dict)
    assert isinstance(completion, dict)

    adapter = EngineEventAdapter()
    renderer = TUIRenderer()
    chat = _RecordingChat()
    status = SimpleNamespace(status_text="")
    scenarios = [*lifecycle["events"], completion]
    for scenario in scenarios:
        assert isinstance(scenario, dict)
        message = adapter.adapt(scenario["engine_event"], scenario["data"])
        assert message is not None
        renderer.render(message, chat, status, None)

    assert chat.prepares == ["准备 bash_run"]
    assert chat.prepare_ended == 1
    assert len(chat.started_tools) == 1
    assert "printf 'ok\\n'" in chat.started_tools[0]
    assert len(chat.ended_tools) == 1
    _, tool_status, duration_ms, output = chat.ended_tools[0]
    assert (tool_status, duration_ms, output) == ("success", 12, "ok\n")
    assert len(chat.mounted) == 1
    assert status.status_text == "完成回执：已完成"


@pytest.mark.asyncio
async def test_startup_recovery_does_not_overwrite_newer_run_status() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    status = SimpleNamespace(status_text="启动恢复中")

    async def start_long_running_services() -> list[SimpleNamespace]:
        entered.set()
        await release.wait()
        return [SimpleNamespace(outcome=ReconciliationCoordinatorOutcome.COMPLETED)]

    engine = SimpleNamespace(
        start_long_running_services=start_long_running_services,
        evolution_patch_recovery_status=lambda: {},
        harness_service=None,
    )
    context = SimpleNamespace(
        engine=engine,
        query_one=lambda _widget: status,
    )

    recover = NaumiApp._recover_session_reconciliations.__wrapped__
    recovery = asyncio.create_task(recover(context))
    await entered.wait()
    status.status_text = "已取消当前运行。"
    release.set()
    await recovery

    assert status.status_text == "已取消当前运行。"


@pytest.mark.asyncio
async def test_tui_ctrl_c_cancels_active_run_from_shared_golden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _golden()
    submission = fixture["submission"]
    assert isinstance(submission, dict)
    started = asyncio.Event()
    cancelled = asyncio.Event()
    calls: list[str] = []

    engine = create_agent_engine(AppConfig())
    engine.workspace_root = tmp_path
    engine.harness_service = SimpleNamespace(
        store=HarnessStore(tmp_path / "harness.db")
    )

    async def run_streaming(task: str, _sink: object) -> None:
        calls.append(task)
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(engine, "run_streaming", run_streaming)
    app = NaumiApp(engine)

    async with app.run_test(size=(100, 30)) as pilot:
        composer = app.query_one("#msg-input", Input)
        composer.value = str(submission["text"])
        composer.focus()
        await pilot.press("enter")
        await asyncio.wait_for(started.wait(), timeout=3)

        await pilot.press("ctrl+c")
        await asyncio.wait_for(cancelled.wait(), timeout=3)
        for _ in range(60):
            if not app._agent_busy:  # noqa: SLF001
                break
            await pilot.pause(0.05)
        else:
            pytest.fail("Ctrl+C 后 TUI 运行未在时限内停止")

        assert calls == [submission["text"]]
        assert app._run_cancel_pending is False  # noqa: SLF001
        assert app.query_one(StatusBar).status_text == "已取消当前运行。"


@pytest.mark.asyncio
async def test_tui_ctrl_c_while_idle_keeps_ui_open(tmp_path: Path) -> None:
    engine = create_agent_engine(AppConfig())
    engine.workspace_root = tmp_path
    engine.harness_service = SimpleNamespace(
        store=HarnessStore(tmp_path / "harness.db")
    )
    app = NaumiApp(engine)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press("ctrl+c")
        assert app.is_running
        assert (
            app.query_one(StatusBar).status_text
            == "当前没有正在运行的任务；使用 Ctrl+Q 退出。"
        )


def test_tui_second_ctrl_c_forces_exit_without_duplicate_cancel() -> None:
    status = SimpleNamespace(status_text="")
    exits: list[bool] = []
    context = SimpleNamespace(
        _agent_busy=True,
        _run_cancel_pending=True,
        query_one=lambda _widget: status,
        exit=lambda: exits.append(True),
    )

    NaumiApp.action_request_run_cancel(context)

    assert exits == [True]
