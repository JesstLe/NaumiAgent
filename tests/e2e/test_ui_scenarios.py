"""Replay terminal UI scenarios from YAML fixtures.

The scenarios exercise the same adapter and renderer path used by the CLI/TUI:
engine event -> UIMessage -> CLI transcript/TUI side effect.  They also cover
resume replay, structured diff rendering, and terminal-width fitting so UI
regressions are reproducible from compact fixture files.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml
from prompt_toolkit.utils import get_cwidth

from naumi_agent.cli.history import VirtualizedCLIHistory
from naumi_agent.cli.layout import _fit_text_to_width
from naumi_agent.cli.renderers.registry import CLIRenderer
from naumi_agent.tui.renderers.registry import TUIRenderer
from naumi_agent.ui.diff_viewer import DiffSnapshot, parse_unified_diff, render_diff_snapshot
from naumi_agent.ui.messages import EngineEventAdapter
from naumi_agent.ui.messages.replay import replay_messages

SCENARIO_DIR = Path(__file__).parent / "ui_scenarios"
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


@dataclass
class FakeChatPanel:
    """Capture TUI renderer effects without launching a real terminal app."""

    events: list[str] = field(default_factory=list)
    mounted: list[Any] = field(default_factory=list)

    def start_thinking(self) -> None:
        self.events.append("thinking:start")

    def add_thinking_chunk(self, content: str) -> None:
        self.events.append(f"thinking:{content}")

    def end_thinking(self) -> None:
        self.events.append("thinking:end")

    def start_response(self) -> None:
        self.events.append("response:start")

    def add_response_token(self, content: str) -> None:
        self.events.append(f"response:{content}")

    def start_tool(self, name: str) -> None:
        self.events.append(f"tool:start:{name}")

    def update_tool_prepare(self, text: str) -> None:
        self.events.append(f"tool:prepare:{text}")

    def end_tool_prepare(self) -> None:
        self.events.append("tool:prepare:end")

    def end_tool(
        self,
        label: str,
        status: str,
        duration_ms: int,
        content_preview: str,
    ) -> None:
        self.events.append(f"tool:end:{label}:{status}:{duration_ms}:{content_preview}")

    def mount(self, widget: Any) -> None:
        self.mounted.append(widget)
        self.events.append(_render_widget_text(widget))

    def show_model(self, model: str) -> None:
        self.events.append(f"model:{model}")


@dataclass
class FakeStatusBar:
    history: list[str] = field(default_factory=list)
    _status_text: str = ""

    @property
    def status_text(self) -> str:
        return self._status_text

    @status_text.setter
    def status_text(self, value: str) -> None:
        self._status_text = value
        self.history.append(value)


@dataclass
class FakeTodoBar:
    todo_text: str = ""


@pytest.mark.parametrize("scenario_path", sorted(SCENARIO_DIR.glob("*.yaml")), ids=lambda p: p.stem)
def test_terminal_ui_scenario_replays_without_layout_regression(
    scenario_path: Path,
) -> None:
    scenario = _load_scenario(scenario_path)

    cli_plain, tui_plain, history = _replay_engine_events(scenario)

    if messages := scenario.get("messages"):
        cli_plain += "\n" + _render_replayed_messages(messages, history)

    if diff_text := scenario.get("diff_text"):
        cli_plain += "\n" + _render_diff_fixture(diff_text, history)

    _assert_contains(cli_plain, scenario.get("assert_cli_contains", []), "CLI transcript")
    _assert_contains(tui_plain, scenario.get("assert_tui_contains", []), "TUI transcript")
    _assert_not_contains(
        cli_plain,
        scenario.get("assert_cli_not_contains", []),
        "CLI transcript",
    )

    if viewport := scenario.get("viewport"):
        visible = _strip_ansi(
            history.visible_text(
                width=int(viewport.get("width", 80)),
                height=int(viewport.get("height", 12)),
            )
        )
        _assert_contains(visible, viewport.get("assert_contains", []), "CLI viewport")

    if resize := scenario.get("resize"):
        text = str(resize["status_text"])
        for width in resize["widths"]:
            fitted = _fit_text_to_width(text, int(width))
            assert get_cwidth(fitted) == int(width), (
                f"{scenario_path.name}: 状态栏宽度不稳定，"
                f"目标 {width} cells，实际 {get_cwidth(fitted)} cells"
            )
            assert fitted.endswith("…") or get_cwidth(text) <= int(width)

    if min_lines := scenario.get("min_history_lines"):
        assert history.line_count() >= int(min_lines)


def _load_scenario(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path.name}: scenario 必须是 YAML 对象"
    assert data.get("name") == path.stem, f"{path.name}: name 必须等于文件名"
    return data


def _replay_engine_events(
    scenario: dict[str, Any],
) -> tuple[str, str, VirtualizedCLIHistory]:
    adapter = EngineEventAdapter()
    cli_renderer = CLIRenderer()
    tui_renderer = TUIRenderer()
    history = VirtualizedCLIHistory()
    chat = FakeChatPanel()
    status = FakeStatusBar()
    todo = FakeTodoBar()

    for item in scenario.get("events", []):
        msg = adapter.adapt(str(item["event"]), item.get("data") or {})
        assert msg is not None, f"{scenario['name']}: 未识别 engine event {item['event']}"

        cli_text = cli_renderer.render(msg)
        if cli_text:
            history.append_output(cli_text)
        tui_renderer.render(msg, chat, status, todo)

    tui_plain = "\n".join([*chat.events, *status.history, todo.todo_text])
    return _strip_ansi(history.transcript()), tui_plain, history


def _render_replayed_messages(
    messages: list[dict[str, Any]],
    history: VirtualizedCLIHistory,
) -> str:
    renderer = CLIRenderer()
    rendered: list[str] = []
    for msg in replay_messages(messages):
        text = renderer.render(msg)
        if text:
            history.append_output(text)
            rendered.append(text)
    return _strip_ansi("".join(rendered))


def _render_diff_fixture(diff_text: str, history: VirtualizedCLIHistory) -> str:
    snapshot = DiffSnapshot(
        cwd=Path("/tmp/naumi-ui-e2e"),
        scope="all",
        files=parse_unified_diff(diff_text),
        raw_diff=diff_text,
    )
    rendered = render_diff_snapshot(snapshot)
    history.append_output(rendered)
    return _strip_ansi(rendered)


def _assert_contains(text: str, needles: list[str], label: str) -> None:
    for needle in needles:
        assert needle in text, f"{label} 缺少预期文本: {needle!r}\n{text}"


def _assert_not_contains(text: str, needles: list[str], label: str) -> None:
    for needle in needles:
        assert needle not in text, f"{label} 出现了不应展示的文本: {needle!r}\n{text}"


def _strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def _render_widget_text(widget: Any) -> str:
    content = getattr(widget, "content", None)
    if hasattr(content, "plain"):
        return str(content.plain)
    if content is not None:
        return str(content)
    renderable = getattr(widget, "renderable", None)
    if hasattr(renderable, "plain"):
        return str(renderable.plain)
    if renderable is not None:
        return str(renderable)
    return str(widget)
