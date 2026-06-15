"""Shared slash command execution helpers for CLI/TUI/New UI."""

from __future__ import annotations

import contextlib
import importlib
import shlex
from typing import Any

from rich.console import Console

_SLASH_ALIAS_MAP: dict[str, str] = {
    "/h": "/help",
    "/histroy": "/history",
    "/r": "/resume",
    "/l": "/load",
    "/task": "/task",
    "/t": "/tools",
    "/c": "/clear",
    "/m": "/model",
    "/u": "/usage",
    "/v": "/version",
    "/q": "/q",
    "/quit": "/quit",
    "/exit": "/exit",
    "/n": "/new",
}


def _normalize_slash_alias(raw: str) -> str:
    lowered = raw.lower()
    return _SLASH_ALIAS_MAP.get(lowered, lowered)


def _split_command_batch(raw: str) -> list[str]:
    """Split a slash input into multiple slash command segments.

    Supports separators:
    - newline
    - semicolon ';'
    - logical AND token '&&'

    Separators inside single/double quotes are preserved as plain text.
    """
    normalized = raw.strip()
    if not normalized:
        return []

    segments: list[str] = []
    current: list[str] = []
    quote: str | None = None
    i = 0

    while i < len(normalized):
        char = normalized[i]

        if char in {"\"", "'"}:
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
            current.append(char)
            i += 1
            continue

        if quote is None:
            if char == "\\" and i + 1 < len(normalized):
                current.append(char)
                i += 1
                current.append(normalized[i])
                i += 1
                continue

            if char == "\n" or char == ";":
                segment = "".join(current).strip()
                if segment:
                    segments.append(segment)
                current = []
                i += 1
                continue

            if char == "&" and normalized.startswith("&&", i):
                segment = "".join(current).strip()
                if segment:
                    segments.append(segment)
                current = []
                i += 2
                continue

        current.append(char)
        i += 1

    segment = "".join(current).strip()
    if segment:
        segments.append(segment)
    return segments


def _normalize_command(raw_command: str) -> str:
    """Normalize alias and empty fallback for one slash command."""
    normalized = raw_command.strip()
    if not normalized:
        return "/help"

    try:
        parts = shlex.split(normalized)
    except ValueError:
        parts = normalized.split()

    if not parts:
        return "/help"

    normalized_command = _normalize_slash_alias(parts[0])
    if len(parts) > 1:
        normalized_command = f"{normalized_command} {shlex.join(parts[1:])}"
    if normalized_command == "/":
        normalized_command = "/help"
    return normalized_command


@contextlib.contextmanager
def _with_frontend(engine_main: Any, frontend: Any | None) -> Any:
    """Temporarily bind frontend object as the active CLI adapter."""
    previous_frontend = getattr(engine_main, "_active_cli", None)
    if frontend is not None:
        engine_main._active_cli = frontend
    try:
        yield engine_main
    finally:
        engine_main._active_cli = previous_frontend


async def execute_slash_command(
    engine: Any,
    command: str,
    *,
    frontend: Any | None = None,
) -> str:
    """Execute one slash command through the shared `_handle_command` backend."""
    engine_main = importlib.import_module("naumi_agent.main")
    handle_command = getattr(engine_main, "_handle_command", None)
    if not callable(handle_command):
        raise RuntimeError("未绑定命令处理器")

    commands = _split_command_batch(command)
    if not commands:
        commands = ["/help"]

    normalized_commands = [_normalize_command(cmd) for cmd in commands]

    outputs: list[str] = []

    for normalized_command in normalized_commands:
        output = await _run_single_slash_command(
            handle_command=handle_command,
            engine=engine,
            engine_main=engine_main,
            command=normalized_command,
            frontend=frontend,
        )
        if output.strip():
            outputs.append(output.strip())

    if len(outputs) <= 1:
        return outputs[0] if outputs else ""
    return "\n\n".join(outputs)


async def _run_single_slash_command(
    *,
    handle_command: Any,
    engine: Any,
    engine_main: Any,
    command: str,
    frontend: Any | None = None,
) -> str:
    """Run one normalized slash command through `_handle_command`."""
    capture_async = getattr(engine_main, "_capture_async", None)
    if callable(capture_async):
        async def _run() -> None:
            with _with_frontend(engine_main, frontend):
                await handle_command(engine, command)

        return await capture_async(_run)

    import io
    import shutil
    from io import StringIO

    # Fallback capture, compatible with minimal test doubles that do not provide
    # an existing capture helper.
    buffer = StringIO()
    width = shutil.get_terminal_size().columns
    capturing_console = Console(
        file=buffer,
        force_terminal=True,
        color_system="standard",
        legacy_windows=False,
        width=width,
    )

    previous_console = getattr(engine_main, "console", None)
    try:
        with _with_frontend(engine_main, frontend):
            if previous_console is None:
                # If console is unavailable, try capturing stdout directly.
                fallback_buffer = io.StringIO()
                with (
                    contextlib.redirect_stdout(fallback_buffer),
                    contextlib.redirect_stderr(fallback_buffer),
                ):
                    await handle_command(engine, command)
                return fallback_buffer.getvalue()

            engine_main.console = capturing_console
            await handle_command(engine, command)
    finally:
        if previous_console is not None:
            engine_main.console = previous_console

    return buffer.getvalue()
