"""Focused cross-platform tests for the shared terminal capability contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from naumi_agent.ui.doctor import _check_terminal
from naumi_agent.ui.terminal_capabilities import (
    TERMINAL_CAPABILITY_CONTRACT,
    detect_terminal_capabilities,
    load_terminal_capability_contract,
)


def test_python_profile_matches_shared_wire_contract_for_iterm() -> None:
    profile = detect_terminal_capabilities(
        platform="darwin",
        env={
            "TERM": "xterm-256color",
            "TERM_PROGRAM": "iTerm.app",
            "COLORTERM": "truecolor",
            "HOME": "/Users/naumi",
        },
        stdin_is_tty=True,
        stdout_is_tty=True,
    )

    assert profile.to_wire() == {
        "interactive": True,
        "fullScreen": True,
        "ansiControl": True,
        "colorLevel": "truecolor",
        "colors": True,
        "unicode": True,
        "mouseProtocol": "sgr",
        "alternateScreen": True,
        "bracketedPaste": True,
        "cursorControl": True,
        "synchronizedOutput": True,
        "enhancedKeyboard": False,
        "animate": True,
        "signalMode": "posix",
        "home": "/Users/naumi",
        "terminal": "xterm-256color",
        "terminalProgram": "iTerm.app",
    }
    assert set(profile.to_wire()) == set(
        TERMINAL_CAPABILITY_CONTRACT["profile_fields"]
    )


def test_windows_terminal_uses_unicode_windows_signals_and_userprofile() -> None:
    profile = detect_terminal_capabilities(
        platform="win32",
        env={
            "WT_SESSION": "session",
            "USERPROFILE": r"C:\Users\naumi",
        },
        stdin_is_tty=True,
        stdout_is_tty=True,
    )

    assert profile.interactive is True
    assert profile.full_screen is True
    assert profile.color_level == "ansi16"
    assert profile.unicode is True
    assert profile.mouse_protocol == "sgr"
    assert profile.signal_mode == "windows"
    assert profile.home == r"C:\Users\naumi"


def test_unknown_terminal_cannot_force_ansi_subcapabilities() -> None:
    profile = detect_terminal_capabilities(
        platform="linux",
        env={
            "TERM": "unknown-terminal",
            "NAUMI_ALT_SCREEN": "1",
            "NAUMI_BRACKETED_PASTE": "1",
            "NAUMI_SYNCHRONIZED_OUTPUT": "1",
        },
        stdin_is_tty=True,
        stdout_is_tty=True,
    )

    assert profile.interactive is True
    assert profile.ansi_control is False
    assert profile.full_screen is False
    assert profile.color_level == "none"
    assert profile.alternate_screen is False
    assert profile.bracketed_paste is False
    assert profile.synchronized_output is False


def test_color_and_motion_environment_flags_are_independent() -> None:
    no_color = detect_terminal_capabilities(
        platform="linux",
        env={"TERM": "xterm-256color", "NO_COLOR": "1"},
        stdin_is_tty=True,
        stdout_is_tty=True,
    )
    forced = detect_terminal_capabilities(
        platform="linux",
        env={"TERM": "xterm-256color", "NO_COLOR": "1", "FORCE_COLOR": "3"},
        stdin_is_tty=True,
        stdout_is_tty=True,
    )
    reduced = detect_terminal_capabilities(
        platform="linux",
        env={"TERM": "xterm-256color", "NAUMI_REDUCE_MOTION": "1"},
        stdin_is_tty=True,
        stdout_is_tty=True,
    )

    assert no_color.color_level == "none"
    assert forced.color_level == "truecolor"
    assert reduced.animate is False
    assert reduced.full_screen is True


def test_contract_loader_rejects_malformed_and_unknown_matchers(tmp_path: Path) -> None:
    invalid_json = tmp_path / "invalid.json"
    invalid_json.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="有效 JSON"):
        load_terminal_capability_contract(invalid_json)

    unknown_matcher = tmp_path / "unknown.json"
    value = json.loads(
        Path(
            "frontend/terminal-ui/terminal-capability-contract.json"
        ).read_text(encoding="utf-8")
    )
    value["matchers"]["ansi_control"]["unreviewed_probe"] = ["unsafe"]
    unknown_matcher.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="unreviewed_probe 未知"):
        load_terminal_capability_contract(unknown_matcher)


def test_doctor_reports_profile_and_safe_new_ui_fallback() -> None:
    supported = detect_terminal_capabilities(
        platform="linux",
        env={"TERM": "xterm-256color"},
        stdin_is_tty=True,
        stdout_is_tty=True,
    )
    unsupported = detect_terminal_capabilities(
        platform="linux",
        env={"TERM": "unknown-terminal"},
        stdin_is_tty=True,
        stdout_is_tty=True,
    )

    passing = _check_terminal(supported, width=100)
    warning = _check_terminal(unsupported, width=100)

    assert passing.status == "pass"
    assert "color=ansi256" in passing.detail
    assert "fullscreen=yes" in passing.detail
    assert "mouse=sgr" in passing.detail
    assert warning.status == "warn"
    assert "fullscreen=no" in warning.detail
    assert "naumi --tui" in warning.suggestion


def test_doctor_keeps_narrow_terminal_warning_after_capability_pass() -> None:
    supported = detect_terminal_capabilities(
        platform="linux",
        env={"TERM": "xterm-256color"},
        stdin_is_tty=True,
        stdout_is_tty=True,
    )

    warning = _check_terminal(supported, width=59)

    assert warning.status == "warn"
    assert "width=59" in warning.detail
    assert "至少 80 列" in warning.suggestion
