from __future__ import annotations

from threading import Thread

import pytest
from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

import naumi_agent.ui.selection as selection
from naumi_agent.ui.selection import TerminalChoice, select_terminal_choice

CHOICES = (
    TerminalChoice("kimi", "Kimi Coding API"),
    TerminalChoice("openai", "OpenAI"),
    TerminalChoice("anthropic", "Anthropic"),
)


def _run_interactive(keys: str) -> str:
    with create_pipe_input() as pipe_input:
        sender = Thread(target=lambda: pipe_input.send_text(keys), daemon=True)
        sender.start()
        with create_app_session(input=pipe_input, output=DummyOutput()):
            result = select_terminal_choice(
                "选择模型提供商",
                CHOICES,
                default="kimi",
                interactive=True,
            )
        sender.join(timeout=1)
        return result


def test_terminal_choice_moves_with_arrow_and_accepts_enter() -> None:
    assert _run_interactive("\x1b[B\r") == "openai"


def test_terminal_choice_supports_number_shortcuts() -> None:
    assert _run_interactive("3\r") == "anthropic"


def test_terminal_choice_preserves_keyboard_interrupt() -> None:
    with pytest.raises(KeyboardInterrupt):
        _run_interactive("\x03")


def test_terminal_choice_uses_explicit_fallback_outside_a_tty() -> None:
    calls: list[tuple[tuple[TerminalChoice, ...], str]] = []

    selected = select_terminal_choice(
        "选择模型提供商",
        CHOICES,
        default="kimi",
        interactive=False,
        fallback=lambda options, default: calls.append((options, default)) or "openai",
    )

    assert selected == "openai"
    assert calls == [(CHOICES, "kimi")]


def test_terminal_choice_falls_back_when_interactive_input_reaches_eof(
    monkeypatch,
) -> None:
    monkeypatch.setattr(selection, "choice", lambda **_kwargs: (_ for _ in ()).throw(EOFError))

    assert select_terminal_choice(
        "选择模型提供商",
        CHOICES,
        default="kimi",
        interactive=True,
        fallback=lambda _options, _default: "openai",
    ) == "openai"


@pytest.mark.parametrize(
    ("choices", "default", "error"),
    [
        ((), "kimi", "至少提供一个选项"),
        ((TerminalChoice("same", "A"), TerminalChoice("same", "B")), "same", "选项值不能重复"),
        (CHOICES, "missing", "默认值必须属于选项"),
    ],
)
def test_terminal_choice_rejects_invalid_definitions(
    choices: tuple[TerminalChoice, ...],
    default: str,
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        select_terminal_choice(
            "选择模型提供商",
            choices,
            default=default,
            interactive=False,
            fallback=lambda _options, selected: selected,
        )
