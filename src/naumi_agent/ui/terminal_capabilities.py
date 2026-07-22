"""Cross-platform terminal capability detection shared with the Node UI."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, cast

ColorLevel = Literal["none", "ansi16", "ansi256", "truecolor"]
MouseProtocol = Literal["none", "sgr"]
SignalMode = Literal["none", "posix", "windows"]

_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_TRUECOLOR_VALUES = frozenset({"24bit", "truecolor"})
_CONTRACT_RELATIVE_PATH = Path(
    "frontend/terminal-ui/terminal-capability-contract.json"
)


@dataclass(frozen=True, slots=True)
class TerminalCapabilities:
    """Validated terminal capabilities with a stable cross-language wire shape."""

    interactive: bool
    full_screen: bool
    ansi_control: bool
    color_level: ColorLevel
    colors: bool
    unicode: bool
    mouse_protocol: MouseProtocol
    alternate_screen: bool
    bracketed_paste: bool
    cursor_control: bool
    synchronized_output: bool
    enhanced_keyboard: bool
    animate: bool
    signal_mode: SignalMode
    home: str
    terminal: str
    terminal_program: str

    def to_wire(self) -> dict[str, bool | str]:
        """Return the exact camelCase profile consumed by the Node frontend."""
        return {
            "interactive": self.interactive,
            "fullScreen": self.full_screen,
            "ansiControl": self.ansi_control,
            "colorLevel": self.color_level,
            "colors": self.colors,
            "unicode": self.unicode,
            "mouseProtocol": self.mouse_protocol,
            "alternateScreen": self.alternate_screen,
            "bracketedPaste": self.bracketed_paste,
            "cursorControl": self.cursor_control,
            "synchronizedOutput": self.synchronized_output,
            "enhancedKeyboard": self.enhanced_keyboard,
            "animate": self.animate,
            "signalMode": self.signal_mode,
            "home": self.home,
            "terminal": self.terminal,
            "terminalProgram": self.terminal_program,
        }


def load_terminal_capability_contract(
    path: str | Path | None = None,
) -> Mapping[str, Any]:
    """Load and strictly validate the versioned terminal capability contract."""
    if path is not None:
        raw = Path(path).read_text(encoding="utf-8")
    else:
        source_path = Path(__file__).resolve().parents[3] / _CONTRACT_RELATIVE_PATH
        if source_path.is_file():
            raw = source_path.read_text(encoding="utf-8")
        else:
            resource = resources.files("naumi_agent").joinpath(
                *_CONTRACT_RELATIVE_PATH.parts
            )
            raw = resource.read_text(encoding="utf-8")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("terminal capability contract 不是有效 JSON") from exc
    _validate_contract(value)
    return cast(Mapping[str, Any], _freeze(value))


def detect_terminal_capabilities(
    *,
    platform: str = sys.platform,
    env: Mapping[str, str] = os.environ,
    stdin_is_tty: bool | None = None,
    stdout_is_tty: bool | None = None,
    contract: Mapping[str, Any] | None = None,
) -> TerminalCapabilities:
    """Detect capabilities conservatively without writing terminal sequences."""
    if contract is None:
        contract = TERMINAL_CAPABILITY_CONTRACT
    if stdin_is_tty is None:
        stdin_is_tty = _stream_is_tty(sys.stdin)
    if stdout_is_tty is None:
        stdout_is_tty = _stream_is_tty(sys.stdout)

    terminal = str(env.get("TERM", "")).strip()
    terminal_program = str(env.get("TERM_PROGRAM", "")).strip()
    dumb = terminal.lower() == "dumb"
    allow_non_tty = _environment_flag(env.get("NAUMI_TERMINAL_UI_ALLOW_NON_TTY"))
    interactive = allow_non_tty or (stdin_is_tty and stdout_is_tty and not dumb)
    match_context = (terminal, terminal_program, env, contract)
    ansi_control = interactive and not dumb and _matches_rule(
        "ansi_control", *match_context
    )
    alternate_screen = ansi_control and _negotiated_boolean(
        env.get("NAUMI_ALT_SCREEN"), True
    )
    bracketed_paste = ansi_control and _negotiated_boolean(
        env.get("NAUMI_BRACKETED_PASTE"), True
    )
    cursor_control = ansi_control
    full_screen = ansi_control and alternate_screen and cursor_control
    windows_unicode = platform != "win32" or _matches_rule(
        "windows_unicode", *match_context
    )
    unicode = interactive and not dumb and windows_unicode
    color_level = _resolve_color_level(
        ansi_control=ansi_control,
        terminal=terminal,
        env=env,
    )
    synchronized_output = ansi_control and _negotiated_boolean(
        env.get("NAUMI_SYNCHRONIZED_OUTPUT"),
        _matches_rule("synchronized_output", *match_context),
    )
    enhanced_keyboard = ansi_control and _matches_rule(
        "enhanced_keyboard", *match_context
    )
    mouse_protocol: MouseProtocol = (
        "sgr" if ansi_control and _matches_rule("mouse_sgr", *match_context) else "none"
    )
    animate = (
        full_screen
        and unicode
        and not _environment_flag(env.get("CI"))
        and not _environment_flag(env.get("NAUMI_REDUCE_MOTION"))
    )
    signal_mode: SignalMode = (
        "none" if not interactive else "windows" if platform == "win32" else "posix"
    )
    profile = TerminalCapabilities(
        interactive=interactive,
        full_screen=full_screen,
        ansi_control=ansi_control,
        color_level=color_level,
        colors=color_level != "none",
        unicode=unicode,
        mouse_protocol=mouse_protocol,
        alternate_screen=alternate_screen,
        bracketed_paste=bracketed_paste,
        cursor_control=cursor_control,
        synchronized_output=synchronized_output,
        enhanced_keyboard=enhanced_keyboard,
        animate=animate,
        signal_mode=signal_mode,
        home=_resolve_terminal_home(env),
        terminal=terminal,
        terminal_program=terminal_program,
    )
    _validate_profile(profile, contract)
    return profile


def _resolve_terminal_home(env: Mapping[str, str]) -> str:
    if str(env.get("HOME", "")).strip():
        return str(env["HOME"]).strip()
    if str(env.get("USERPROFILE", "")).strip():
        return str(env["USERPROFILE"]).strip()
    drive = str(env.get("HOMEDRIVE", "")).strip()
    path = str(env.get("HOMEPATH", "")).strip()
    return f"{drive}{path}" if drive and path else ""


def _resolve_color_level(
    *,
    ansi_control: bool,
    terminal: str,
    env: Mapping[str, str],
) -> ColorLevel:
    if not ansi_control:
        return "none"
    forced = _forced_color_level(env.get("FORCE_COLOR"))
    if forced is not None:
        return forced
    if "NO_COLOR" in env:
        return "none"
    if str(env.get("COLORTERM", "")).strip().lower() in _TRUECOLOR_VALUES:
        return "truecolor"
    return "ansi256" if "256color" in terminal.lower() else "ansi16"


def _forced_color_level(value: str | None) -> ColorLevel | None:
    if value is None or not str(value).strip():
        return None
    normalized = str(value).strip().lower()
    if normalized in _FALSE_VALUES:
        return "none"
    if normalized == "3":
        return "truecolor"
    if normalized == "2":
        return "ansi256"
    return "ansi16"


def _negotiated_boolean(value: str | None, fallback: bool) -> bool:
    explicit = _optional_environment_flag(value)
    return fallback if explicit is None else explicit


def _matches_rule(
    name: str,
    terminal: str,
    terminal_program: str,
    env: Mapping[str, str],
    contract: Mapping[str, Any],
) -> bool:
    rule = contract["matchers"].get(name)
    if not isinstance(rule, Mapping):
        return False
    term = terminal.lower()
    program = terminal_program.lower()
    if term in rule.get("term_exact", ()):
        return True
    if any(term.startswith(prefix) for prefix in rule.get("term_prefixes", ())):
        return True
    if any(part in program for part in rule.get("program_contains", ())):
        return True
    presence_keys = (*rule.get("presence_env", ()), *rule.get("windows_env", ()))
    return any(_environment_flag(env.get(key)) for key in presence_keys)


def _validate_profile(
    profile: TerminalCapabilities,
    contract: Mapping[str, Any],
) -> None:
    if set(profile.to_wire()) != set(contract["profile_fields"]):
        raise ValueError("terminal capability profile 字段与 contract 不一致")
    if profile.color_level not in contract["color_levels"]:
        raise ValueError("terminal capability colorLevel 无效")
    if profile.mouse_protocol not in contract["mouse_protocols"]:
        raise ValueError("terminal capability mouseProtocol 无效")
    if profile.signal_mode not in contract["signal_modes"]:
        raise ValueError("terminal capability signalMode 无效")
    if not profile.ansi_control and any(
        (
            profile.alternate_screen,
            profile.bracketed_paste,
            profile.cursor_control,
            profile.synchronized_output,
            profile.enhanced_keyboard,
            profile.color_level != "none",
        )
    ):
        raise ValueError("terminal capability ANSI 子能力不能脱离 ansiControl")
    expected_full_screen = (
        profile.ansi_control and profile.alternate_screen and profile.cursor_control
    )
    if profile.full_screen != expected_full_screen:
        raise ValueError("terminal capability fullScreen 派生值无效")
    if profile.animate and (not profile.full_screen or not profile.unicode):
        raise ValueError("terminal capability animate 派生值无效")


def _validate_contract(value: Any) -> None:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("terminal capability contract schema 无效")
    matchers = value.get("matchers")
    if not isinstance(matchers, dict):
        raise ValueError("terminal capability contract matchers 无效")
    for key in (
        "profile_fields",
        "color_levels",
        "mouse_protocols",
        "signal_modes",
        "required_matchers",
        "matcher_fields",
    ):
        entries = value.get(key)
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"terminal capability contract {key} 无效")
        if any(not isinstance(entry, str) or not entry for entry in entries):
            raise ValueError(f"terminal capability contract {key} 包含无效值")
        if len(entries) != len(set(entries)):
            raise ValueError(f"terminal capability contract {key} 存在重复值")
    if set(matchers) != set(value["required_matchers"]):
        raise ValueError("terminal capability contract matcher 集合无效")
    allowed_matcher_fields = set(value["matcher_fields"])
    for name, rule in matchers.items():
        if not isinstance(name, str) or not isinstance(rule, dict):
            raise ValueError("terminal capability contract matcher 无效")
        for matcher_key, entries in rule.items():
            if matcher_key not in allowed_matcher_fields:
                raise ValueError(f"terminal capability matcher {matcher_key} 未知")
            if not isinstance(entries, list) or any(
                not isinstance(entry, str) or not entry for entry in entries
            ):
                raise ValueError(f"terminal capability matcher {matcher_key} 无效")


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _optional_environment_flag(value: str | None) -> bool | None:
    if value is None or not str(value).strip():
        return None
    return _environment_flag(value)


def _environment_flag(value: str | None) -> bool:
    normalized = str(value or "").strip().lower()
    return bool(normalized) and normalized not in _FALSE_VALUES


def _stream_is_tty(stream: Any) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError, ValueError):
        return False


TERMINAL_CAPABILITY_CONTRACT = load_terminal_capability_contract()
