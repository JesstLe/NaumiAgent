"""Deterministic fixed-viewport golden captures for Naumi terminal surfaces."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from rich.console import Console
from textual.app import App, ComposeResult
from textual.containers import Container
from textual.widgets import Footer, Header

from naumi_agent.clipboard import strip_ansi
from naumi_agent.tui.app import (
    ActivityPanel,
    BrowserPanel,
    ChatPanel,
    HistoryPanel,
    InputBar,
    NaumiApp,
    Spinner,
    StatusBar,
    TodoBar,
)
from naumi_agent.tui.renderers.registry import TUIRenderer
from naumi_agent.ui.messages.adapter import EngineEventAdapter
from naumi_agent.ui.terminal_width import display_width
from naumi_agent.ui.theme import build_ui_style_config

_MANIFEST_SCHEMA = "naumi.terminal-golden-capture.v1"
_FRAME_SCHEMA = "naumi.terminal-golden-frame.v1"
_MAX_FIXTURE_BYTES = 2_000_000
_MIN_WIDTH = 40
_MAX_WIDTH = 400
_MIN_HEIGHT = 12
_MAX_HEIGHT = 200


class TerminalCaptureError(RuntimeError):
    """Raised when a terminal capture cannot provide trustworthy evidence."""


@dataclass(frozen=True)
class TerminalGoldenFrame:
    """One renderer frame with both styled and plain terminal evidence."""

    surface: str
    renderer: str
    width: int
    height: int
    ansi: str
    text: str
    ansi_sha256: str
    text_sha256: str
    line_count: int
    max_visible_width: int
    required_anchors: tuple[str, ...]
    missing_anchors: tuple[str, ...]

    def manifest_entry(self) -> dict[str, Any]:
        return {
            "surface": self.surface,
            "renderer": self.renderer,
            "width": self.width,
            "height": self.height,
            "ansi_sha256": self.ansi_sha256,
            "text_sha256": self.text_sha256,
            "line_count": self.line_count,
            "max_visible_width": self.max_visible_width,
            "required_anchors": list(self.required_anchors),
            "missing_anchors": list(self.missing_anchors),
        }


class _GoldenCaptureTUI(App[None]):
    """Production Textual components hosted without constructing an AgentEngine."""

    TITLE = "NaumiAgent TUI"
    SUB_TITLE = "Golden Capture"
    CSS = NaumiApp.CSS + "\n" + build_ui_style_config().tui_css()
    BINDINGS = NaumiApp.BINDINGS

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="main-area"):
            yield ChatPanel()
            yield HistoryPanel()
            yield BrowserPanel()
            yield ActivityPanel()
        yield TodoBar()
        yield InputBar()
        yield Spinner()
        yield StatusBar()
        yield Footer()


async def capture_terminal_goldens(
    fixture_path: Path,
    output_dir: Path,
    *,
    width: int | None = None,
    height: int | None = None,
    node_binary: str = "node",
) -> dict[str, Any]:
    """Capture New UI and TUI from one fixture and atomically publish evidence."""
    fixture_path = fixture_path.expanduser().resolve(strict=True)
    fixture_bytes = fixture_path.read_bytes()
    if len(fixture_bytes) > _MAX_FIXTURE_BYTES:
        raise TerminalCaptureError(
            f"golden fixture 超过 {_MAX_FIXTURE_BYTES} bytes 上限"
        )
    try:
        fixture = json.loads(fixture_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TerminalCaptureError("golden fixture 不是有效 UTF-8 JSON") from exc
    capture = _validate_fixture(fixture)
    viewport_width = _dimension(
        capture["width"] if width is None else width,
        "width",
        _MIN_WIDTH,
        _MAX_WIDTH,
    )
    viewport_height = _dimension(
        capture["height"] if height is None else height,
        "height",
        _MIN_HEIGHT,
        _MAX_HEIGHT,
    )
    fixture_sha256 = hashlib.sha256(fixture_bytes).hexdigest()

    new_ui_frame = _capture_new_ui_frame(
        fixture_path,
        fixture_sha256=fixture_sha256,
        width=viewport_width,
        height=viewport_height,
        required_anchors=tuple(capture["required_anchors"]),
        node_binary=node_binary,
    )
    tui_frame = await _capture_tui_frame(
        fixture,
        width=viewport_width,
        height=viewport_height,
        required_anchors=tuple(capture["required_anchors"]),
    )
    frames = (new_ui_frame, tui_frame)
    missing = {
        frame.surface: list(frame.missing_anchors)
        for frame in frames
        if frame.missing_anchors
    }
    if missing:
        raise TerminalCaptureError(
            "双端 capture 缺少语义锚点: "
            + json.dumps(missing, ensure_ascii=False, sort_keys=True)
        )

    manifest = {
        "schema": _MANIFEST_SCHEMA,
        "fixture": {
            "name": fixture_path.name,
            "sha256": fixture_sha256,
            "schema_version": int(fixture["schema_version"]),
        },
        "viewport": {"width": viewport_width, "height": viewport_height},
        "semantic_parity": {
            "required_anchors": list(capture["required_anchors"]),
            "passed": True,
        },
        "captures": {
            "new_ui": {
                **new_ui_frame.manifest_entry(),
                "ansi_file": "new-ui.ansi",
                "text_file": "new-ui.txt",
            },
            "tui": {
                **tui_frame.manifest_entry(),
                "ansi_file": "tui.ansi",
                "text_file": "tui.txt",
            },
        },
    }
    output_dir = output_dir.expanduser()
    _publish_capture_files(
        output_dir,
        {
            "new-ui.ansi": new_ui_frame.ansi,
            "new-ui.txt": new_ui_frame.text,
            "tui.ansi": tui_frame.ansi,
            "tui.txt": tui_frame.text,
            "manifest.json": json.dumps(
                manifest,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
        },
    )
    return manifest


async def _capture_tui_frame(
    fixture: dict[str, Any],
    *,
    width: int,
    height: int,
    required_anchors: tuple[str, ...],
) -> TerminalGoldenFrame:
    app = _GoldenCaptureTUI()
    async with app.run_test(size=(width, height)) as pilot:
        chat = app.query_one(ChatPanel)
        status = app.query_one(StatusBar)
        todo = app.query_one(TodoBar)
        chat.add_user_message(str(fixture["submission"]["text"]))
        adapter = EngineEventAdapter()
        renderer = TUIRenderer()
        for event in fixture["tool_lifecycle"]["events"]:
            message = adapter.adapt(str(event["engine_event"]), dict(event.get("data") or {}))
            if message is None:
                raise TerminalCaptureError(
                    f"TUI adapter 无法识别事件: {event['engine_event']}"
                )
            renderer.render(message, chat, status, todo)
        completion = fixture["completion"]
        message = adapter.adapt(
            str(completion["engine_event"]),
            dict(completion.get("data") or {}),
        )
        if message is None:
            raise TerminalCaptureError("TUI adapter 无法识别 completion receipt")
        renderer.render(message, chat, status, todo)
        await pilot.pause()
        ansi, text = _render_textual_screen(app, width=width, height=height)

    lines = text.splitlines()
    max_width = max((display_width(line) for line in lines), default=0)
    if strip_ansi(ansi) != text:
        raise TerminalCaptureError("TUI ANSI frame 与纯文本 frame 不一致")
    if len(lines) != height or max_width > width:
        raise TerminalCaptureError(
            "TUI capture 超出固定终端视口: "
            f"lines={len(lines)}/{height}, width={max_width}/{width}"
        )
    missing = tuple(anchor for anchor in required_anchors if anchor not in text)
    return TerminalGoldenFrame(
        surface="tui",
        renderer="textual-compositor",
        width=width,
        height=height,
        ansi=ansi,
        text=text,
        ansi_sha256=_sha256_text(ansi),
        text_sha256=_sha256_text(text),
        line_count=len(lines),
        max_visible_width=max_width,
        required_anchors=required_anchors,
        missing_anchors=missing,
    )


def _render_textual_screen(app: App[Any], *, width: int, height: int) -> tuple[str, str]:
    compositor = getattr(app.screen, "_compositor", None)
    background_screens = getattr(app, "_background_screens", None)
    if compositor is None or background_screens is None:
        raise TerminalCaptureError(
            "当前 Textual 版本不提供稳定 compositor capture；请更新适配器"
        )
    console = Console(
        width=width,
        height=height,
        file=io.StringIO(),
        force_terminal=True,
        color_system="truecolor",
        record=True,
        legacy_windows=False,
        safe_box=False,
    )
    update = compositor.render_update(
        full=True,
        screen_stack=background_screens,
        simplify=True,
    )
    console.print(update, end="")
    ansi = console.export_text(styles=True, clear=False)
    text = console.export_text(styles=False)
    if not ansi.endswith("\n"):
        ansi += "\n"
    if not text.endswith("\n"):
        text += "\n"
    return ansi, text


def _capture_new_ui_frame(
    fixture_path: Path,
    *,
    fixture_sha256: str,
    width: int,
    height: int,
    required_anchors: tuple[str, ...],
    node_binary: str,
) -> TerminalGoldenFrame:
    module_path = _find_new_ui_capture_module()
    try:
        completed = subprocess.run(
            [
                node_binary,
                str(module_path),
                "--fixture",
                str(fixture_path),
                "--width",
                str(width),
                "--height",
                str(height),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except FileNotFoundError as exc:
        raise TerminalCaptureError(f"找不到 Node.js 可执行文件: {node_binary}") from exc
    except subprocess.TimeoutExpired as exc:
        raise TerminalCaptureError("New UI capture 超过 20 秒，已终止") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip()[:1_000] or f"exit {completed.returncode}"
        raise TerminalCaptureError(f"New UI capture 失败: {detail}")
    try:
        payload = json.loads(completed.stdout)
        frame = payload["frame"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise TerminalCaptureError("New UI capture 返回了无效 JSON") from exc
    if payload.get("fixture_sha256") != fixture_sha256:
        raise TerminalCaptureError("New UI capture 使用的 fixture digest 不一致")
    parsed = _frame_from_mapping(frame)
    if (
        parsed.surface != "new_ui"
        or parsed.width != width
        or parsed.height != height
        or parsed.line_count != height
        or parsed.max_visible_width > width
        or parsed.required_anchors != required_anchors
    ):
        raise TerminalCaptureError("New UI capture 元数据与请求视口不一致")
    return parsed


def _frame_from_mapping(value: Any) -> TerminalGoldenFrame:
    if not isinstance(value, dict) or value.get("schema") != _FRAME_SCHEMA:
        raise TerminalCaptureError("终端 frame schema 不受支持")
    try:
        frame = TerminalGoldenFrame(
            surface=str(value["surface"]),
            renderer=str(value["renderer"]),
            width=int(value["width"]),
            height=int(value["height"]),
            ansi=str(value["ansi"]),
            text=str(value["text"]),
            ansi_sha256=str(value["ansi_sha256"]),
            text_sha256=str(value["text_sha256"]),
            line_count=int(value["line_count"]),
            max_visible_width=int(value["max_visible_width"]),
            required_anchors=tuple(str(item) for item in value["required_anchors"]),
            missing_anchors=tuple(str(item) for item in value["missing_anchors"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise TerminalCaptureError("终端 frame 字段不完整") from exc
    if (
        frame.ansi_sha256 != _sha256_text(frame.ansi)
        or frame.text_sha256 != _sha256_text(frame.text)
    ):
        raise TerminalCaptureError("终端 frame digest 校验失败")
    lines = frame.text.splitlines()
    if (
        strip_ansi(frame.ansi) != frame.text
        or len(lines) != frame.line_count
        or max((display_width(line) for line in lines), default=0)
        != frame.max_visible_width
    ):
        raise TerminalCaptureError("终端 frame 内容与元数据不一致")
    actual_missing = tuple(
        anchor for anchor in frame.required_anchors if anchor not in frame.text
    )
    if actual_missing != frame.missing_anchors:
        raise TerminalCaptureError("终端 frame 语义锚点元数据不一致")
    return frame


def _validate_fixture(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or isinstance(value.get("schema_version"), bool)
        or value.get("schema_version") != 1
    ):
        raise TerminalCaptureError("golden fixture schema_version 必须为 1")
    capture = value.get("capture")
    if (
        not isinstance(capture, dict)
        or isinstance(capture.get("schema_version"), bool)
        or capture.get("schema_version") != 1
    ):
        raise TerminalCaptureError("golden fixture 缺少 capture schema v1")
    submission = value.get("submission")
    if not isinstance(submission, dict) or not submission.get("text") or not submission.get(
        "request_id"
    ):
        raise TerminalCaptureError("golden fixture 缺少稳定 submission identity")
    lifecycle = value.get("tool_lifecycle")
    events = lifecycle.get("events") if isinstance(lifecycle, dict) else None
    if not isinstance(events, list) or not events:
        raise TerminalCaptureError("golden fixture 缺少 tool lifecycle events")
    for event in events:
        if not isinstance(event, dict) or not event.get("engine_event"):
            raise TerminalCaptureError("golden fixture tool event 不完整")
    completion = value.get("completion")
    if (
        not isinstance(completion, dict)
        or not completion.get("engine_event")
        or not isinstance(completion.get("data"), dict)
    ):
        raise TerminalCaptureError("golden fixture 缺少 completion receipt")
    anchors = capture.get("required_anchors")
    if not isinstance(anchors, list) or not 1 <= len(anchors) <= 20:
        raise TerminalCaptureError("capture.required_anchors 必须包含 1-20 项")
    required_anchors = [str(item).strip() for item in anchors]
    if any(not item or len(item) > 200 for item in required_anchors):
        raise TerminalCaptureError("capture.required_anchors 含空值或超长内容")
    return {
        "width": _dimension(capture.get("width"), "width", _MIN_WIDTH, _MAX_WIDTH),
        "height": _dimension(capture.get("height"), "height", _MIN_HEIGHT, _MAX_HEIGHT),
        "required_anchors": required_anchors,
    }


def _dimension(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise TerminalCaptureError(f"{name} 必须是 {minimum}-{maximum} 的整数")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise TerminalCaptureError(f"{name} 必须是 {minimum}-{maximum} 的整数") from exc
    if parsed != value or parsed < minimum or parsed > maximum:
        raise TerminalCaptureError(f"{name} 必须是 {minimum}-{maximum} 的整数")
    return parsed


def _find_new_ui_capture_module() -> Path:
    package_root = Path(__file__).resolve().parents[1]
    candidates = (
        Path(__file__).resolve().parents[3]
        / "frontend"
        / "terminal-ui"
        / "src"
        / "golden-capture.js",
        package_root / "frontend" / "terminal-ui" / "src" / "golden-capture.js",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise TerminalCaptureError(
        "找不到 New UI golden-capture.js；请使用完整开发树或重新安装 NaumiAgent"
    )


def _publish_capture_files(output_dir: Path, files: dict[str, str]) -> None:
    if output_dir.exists() and not output_dir.is_dir():
        raise TerminalCaptureError(f"输出路径不是目录: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        target = output_dir / name
        temporary = output_dir / f".{name}.{os.getpid()}.{uuid4().hex}.tmp"
        try:
            with temporary.open("x", encoding="utf-8", newline="") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从同一 fixture 捕获 Naumi New UI 与 TUI 固定终端帧。"
    )
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--node", default="node")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    try:
        manifest = asyncio.run(
            capture_terminal_goldens(
                args.fixture,
                args.output,
                width=args.width,
                height=args.height,
                node_binary=args.node,
            )
        )
    except (OSError, TerminalCaptureError) as exc:
        raise SystemExit(f"终端 golden capture 失败: {exc}") from exc
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
