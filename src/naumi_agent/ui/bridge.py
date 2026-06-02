"""JSONL bridge between the Python engine and next-generation terminal UI."""

from __future__ import annotations

import argparse
import asyncio
import logging
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from naumi_agent.config.settings import AppConfig
from naumi_agent.debug_trace import DebugTrace
from naumi_agent.log_setup import setup_logging
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.ui.messages import EngineEventAdapter
from naumi_agent.ui.protocol import (
    ClientEventType,
    ServerEventType,
    decode_jsonl_line,
    encode_jsonl,
    make_envelope,
    ui_message_payload,
)

logger = logging.getLogger(__name__)

EngineFactory = Callable[[AppConfig], AgentEngine]


def resolve_config_path(path: str) -> str:
    """Resolve a CLI config path with the same fallback as the legacy CLI."""
    candidate = Path(path)
    if candidate.exists():
        return str(candidate)
    fallback = _find_default_config_path(Path(__file__).resolve())
    return str(fallback) if fallback is not None else path


def _find_default_config_path(start_path: Path) -> Path | None:
    """Find the source-tree config.yaml from a module path, regardless of depth."""
    start_dir = start_path if start_path.is_dir() else start_path.parent
    for directory in (start_dir, *start_dir.parents):
        config_path = directory / "config.yaml"
        if config_path.exists():
            return config_path
    return None


def _git_snapshot(cwd: Path) -> dict[str, Any]:
    """Return current git branch and dirty bit for status rendering."""
    result: dict[str, Any] = {"branch": "", "dirty": False}
    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(cwd),
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        result["branch"] = branch
        result["dirty"] = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"],
                cwd=str(cwd),
                stderr=subprocess.DEVNULL,
            ).decode().strip()
        )
    except Exception:
        pass
    return result


class JsonlEngineBridge:
    """Owns one AgentEngine and exposes it over a small JSONL control plane."""

    def __init__(
        self,
        engine: AgentEngine,
        *,
        config_path: str,
        debug_trace: DebugTrace | None = None,
    ) -> None:
        self.engine = engine
        self.config_path = config_path
        self.debug_trace = debug_trace
        self.adapter = EngineEventAdapter()
        self._sequence = 0
        self._writer: TextIO | None = None
        self._writer_lock = asyncio.Lock()
        self._run_task: asyncio.Task[Any] | None = None
        self._pending_permissions: dict[str, asyncio.Future[str]] = {}
        self._closed = False

        self.engine.set_permission_confirmer(self.confirm_permission)

    def bind_writer(self, writer: TextIO) -> None:
        self._writer = writer

    async def emit(
        self,
        event: ServerEventType | str,
        payload: dict[str, Any] | None = None,
        *,
        request_id: str | None = None,
    ) -> None:
        """Emit one JSONL record to the frontend."""
        if self._writer is None:
            raise RuntimeError("bridge writer is not bound")
        self._sequence += 1
        record = make_envelope(
            event,
            payload or {},
            request_id=request_id,
            sequence=self._sequence,
        )
        text = encode_jsonl(record)
        async with self._writer_lock:
            self._writer.write(text)
            self._writer.flush()
        if self.debug_trace is not None:
            self.debug_trace.output("ui_bridge.stdout", text)

    async def emit_ready(self) -> None:
        await self.emit(ServerEventType.READY, self.status_payload())
        if self.debug_trace is not None:
            await self.emit(
                ServerEventType.DEBUG_TRACE,
                {
                    "run_id": self.debug_trace.run_id,
                    "run_dir": str(self.debug_trace.run_dir),
                    "events_path": str(self.debug_trace.events_path),
                    "transcript_path": str(self.debug_trace.transcript_path),
                },
            )

    def status_payload(self) -> dict[str, Any]:
        """Build the footer/status payload consumed by the terminal UI."""
        usage = self.engine.usage
        try:
            model = self.engine.router.resolve_model("capable")
        except Exception:
            model = ""
        try:
            context = self.engine.get_context_info()
        except Exception:
            context = {}
        try:
            budget = self.engine.get_budget_info()
        except Exception:
            budget = {}
        workspace_root = Path(getattr(self.engine, "workspace_root", Path.cwd()))
        return {
            "mode": str(getattr(self.engine.runtime_mode, "value", self.engine.runtime_mode)),
            "permission_mode": str(
                getattr(self.engine.permission_mode, "value", self.engine.permission_mode)
            ),
            "model": model,
            "workspace_root": str(workspace_root),
            "usage": {
                "input_tokens": usage.total_input_tokens,
                "output_tokens": usage.total_output_tokens,
                "turns": usage.turns,
                "total_tokens": usage.total_input_tokens + usage.total_output_tokens,
            },
            "context": context,
            "budget": budget,
            "git": _git_snapshot(workspace_root),
            "config_path": self.config_path,
        }

    async def handle_client_record(self, record: dict[str, Any]) -> None:
        """Dispatch one client protocol record."""
        if not record:
            return
        event_type = str(record.get("type", ""))
        payload = record.get("payload", {})
        if not isinstance(payload, dict):
            await self.emit_error("payload 必须是对象。", request_id=str(record.get("id", "")))
            return
        request_id = str(record.get("id") or record.get("request_id") or "")

        if self.debug_trace is not None:
            self.debug_trace.input("ui_bridge.stdin", encode_jsonl(record))

        if event_type == ClientEventType.HELLO:
            await self.emit(ServerEventType.ACK, {"event": event_type}, request_id=request_id)
            await self.emit(ServerEventType.STATUS, self.status_payload())
            return

        if event_type == ClientEventType.PING:
            await self.emit(ServerEventType.PONG, {"ok": True}, request_id=request_id)
            return

        if event_type == ClientEventType.SET_MODE:
            await self.set_mode(str(payload.get("mode", "")), request_id=request_id)
            return

        if event_type == ClientEventType.CYCLE_MODE:
            mode = self.engine.cycle_runtime_mode()
            await self.emit(
                ServerEventType.MODE_CHANGED,
                {"mode": mode.value, "status": self.status_payload()},
                request_id=request_id,
            )
            await self.emit(ServerEventType.STATUS, self.status_payload())
            return

        if event_type == ClientEventType.PERMISSION_RESPONSE:
            await self.resolve_permission(payload, request_id=request_id)
            return

        if event_type == ClientEventType.SUBMIT:
            await self.submit(str(payload.get("text", "")), request_id=request_id)
            return

        if event_type == ClientEventType.RESUME:
            await self.resume_session(payload, request_id=request_id)
            return

        if event_type == ClientEventType.SHUTDOWN:
            await self.shutdown()
            return

        await self.emit_error(f"未知客户端事件: {event_type}", request_id=request_id)

    async def set_mode(self, mode: str, *, request_id: str) -> None:
        try:
            runtime_mode = self.engine.set_runtime_mode(mode)
        except ValueError:
            await self.emit_error(
                "模式无效，可用值: default / plan / bypass。",
                code="invalid_mode",
                request_id=request_id,
            )
            return
        await self.emit(
            ServerEventType.MODE_CHANGED,
            {"mode": runtime_mode.value, "status": self.status_payload()},
            request_id=request_id,
        )
        await self.emit(ServerEventType.STATUS, self.status_payload())

    async def submit(self, text: str, *, request_id: str) -> None:
        text = text.strip("\n")
        if not text.strip():
            await self.emit_error("输入不能为空。", code="empty_input", request_id=request_id)
            return
        if self._run_task is not None and not self._run_task.done():
            await self.emit_error(
                "当前任务仍在执行，请等待完成后再发送。",
                code="run_in_progress",
                request_id=request_id,
            )
            return

        await self.emit(ServerEventType.USER_MESSAGE, {"content": text}, request_id=request_id)
        await self.emit(ServerEventType.RUN_STARTED, {"task": text}, request_id=request_id)
        await self.emit(ServerEventType.STATUS, self.status_payload())

        async def run() -> None:
            try:
                result = await self.engine.run_streaming(text, self.handle_engine_event)
                await self.emit(
                    ServerEventType.RUN_COMPLETED,
                    {
                        "status": result.status,
                        "response": result.response or "",
                        "error": result.error or "",
                    },
                    request_id=request_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("UI bridge agent run failed")
                if self.debug_trace is not None:
                    self.debug_trace.exception("ui_bridge.run", exc)
                await self.emit_error(
                    f"执行失败: {type(exc).__name__}: {exc}",
                    code="run_failed",
                    request_id=request_id,
                )
            finally:
                await self.emit(ServerEventType.STATUS, self.status_payload())

        self._run_task = asyncio.create_task(run())

    async def resume_session(self, payload: dict[str, Any], *, request_id: str) -> None:
        """Load a persisted session and replay it as typed UI messages."""
        from naumi_agent.ui.messages.replay import replay_messages

        session_id = str(payload.get("session_id") or "").strip()
        if not session_id:
            session_id = await self._find_latest_resumable_session_id()
        if not session_id:
            await self.emit_error(
                "暂无可恢复的历史会话。",
                code="no_session",
                request_id=request_id,
            )
            return

        loaded = await self.engine.load_session(session_id)
        if not loaded:
            await self.emit_error(
                f"会话不存在: {session_id}",
                code="session_not_found",
                request_id=request_id,
            )
            return

        session = getattr(self.engine, "_session", None)
        raw_messages = list(getattr(session, "messages", []) or [])
        await self.emit(
            ServerEventType.SESSION_REPLAYED,
            {
                "session_id": session_id,
                "title": getattr(session, "title", "") or session_id,
                "message_count": len(raw_messages),
                "clear": bool(payload.get("clear", True)),
            },
            request_id=request_id,
        )
        for message in replay_messages(raw_messages):
            await self.emit(ServerEventType.UI_MESSAGE, ui_message_payload(message))
        await self.emit(ServerEventType.STATUS, self.status_payload())

    async def _find_latest_resumable_session_id(self) -> str:
        page = 1
        page_size = 20
        checked = 0
        while True:
            sessions, total = await self.engine.list_sessions(page=page, page_size=page_size)
            if not sessions:
                return ""
            for session in sessions:
                messages = getattr(session, "messages", []) or []
                if any(message.get("role") == "user" for message in messages):
                    return str(session.id)
            checked += len(sessions)
            if checked >= total:
                return ""
            page += 1

    async def handle_engine_event(self, event: str, data: dict[str, Any]) -> None:
        if self.debug_trace is not None:
            self.debug_trace.event("engine.stream_event", {"event": event, "data": data})

        await self.emit(ServerEventType.ENGINE_EVENT, {"event": event, "data": data})
        message = self.adapter.adapt(event, data)
        if message is not None:
            await self.emit(ServerEventType.UI_MESSAGE, ui_message_payload(message))

        if event in {
            "run_started",
            "turn_start",
            "tool_end",
            "task_snapshot",
            "permission_bubble",
            "context_compacted",
            "error",
        }:
            await self.emit(ServerEventType.STATUS, self.status_payload())

    async def confirm_permission(self, payload: dict[str, Any]) -> str:
        request_id = (
            str(payload.get("call_id") or "")
            or f"perm-{len(self._pending_permissions) + 1}"
        )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._pending_permissions[request_id] = future
        await self.emit(ServerEventType.PERMISSION_REQUEST, payload, request_id=request_id)
        try:
            return await future
        finally:
            self._pending_permissions.pop(request_id, None)

    async def resolve_permission(self, payload: dict[str, Any], *, request_id: str) -> None:
        permission_id = str(payload.get("request_id") or request_id)
        choice = str(payload.get("choice", "deny")).strip().lower()
        if choice not in {"allow", "deny", "bypass"}:
            await self.emit_error(
                "权限选择无效，可用值: allow / deny / bypass。",
                code="invalid_permission_choice",
                request_id=request_id,
            )
            return
        future = self._pending_permissions.get(permission_id)
        if future is None or future.done():
            await self.emit_error(
                f"未找到待确认权限请求: {permission_id}",
                code="unknown_permission_request",
                request_id=request_id,
            )
            return
        future.set_result(choice)
        await self.emit(
            ServerEventType.PERMISSION_RESOLVED,
            {"request_id": permission_id, "choice": choice},
            request_id=request_id,
        )

    async def emit_error(
        self,
        message: str,
        *,
        code: str = "error",
        request_id: str | None = None,
    ) -> None:
        await self.emit(
            ServerEventType.ERROR,
            {"message": message, "code": code},
            request_id=request_id,
        )

    async def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        for future in list(self._pending_permissions.values()):
            if not future.done():
                future.set_result("deny")
        if self._run_task is not None and not self._run_task.done():
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
        await self.engine.shutdown()
        await self.emit(ServerEventType.SHUTDOWN, {"ok": True})
        if self.debug_trace is not None:
            self.debug_trace.close()


async def serve_stdio(bridge: JsonlEngineBridge) -> None:
    """Serve JSONL from stdin to stdout."""
    bridge.bind_writer(sys.stdout)
    await bridge.emit_ready()

    loop = asyncio.get_running_loop()
    while not bridge._closed:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if line == "":
            await bridge.shutdown()
            return
        try:
            record = decode_jsonl_line(line)
            await bridge.handle_client_record(record)
        except Exception as exc:
            if bridge.debug_trace is not None:
                bridge.debug_trace.exception("ui_bridge.decode_or_dispatch", exc)
            await bridge.emit_error(str(exc), code="bad_request")


async def create_bridge(
    *,
    config_path: str,
    engine_factory: EngineFactory = AgentEngine,
) -> JsonlEngineBridge:
    resolved = resolve_config_path(config_path)
    config = AppConfig.from_yaml(resolved)
    setup_logging(config.log_level)
    engine = engine_factory(config)
    debug_trace = DebugTrace.create(
        interface="terminal-ui-bridge",
        base_dir=Path(config.memory.session_db_path).parent / "debug-runs",
        metadata={
            "config_path": str(Path(resolved).resolve()),
            "cwd": str(Path.cwd()),
            "workspace_root": str(engine.workspace_root),
            "session_db_path": str(Path(config.memory.session_db_path).resolve()),
            "vector_db_path": str(Path(config.memory.vector_db_path).resolve()),
            "model": engine.router.resolve_model("capable"),
        },
    )
    return JsonlEngineBridge(engine, config_path=resolved, debug_trace=debug_trace)


async def _amain(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="NaumiAgent terminal UI JSONL bridge")
    parser.add_argument("--config", "-c", default="config.yaml", help="配置文件路径")
    args = parser.parse_args(argv)
    bridge = await create_bridge(config_path=args.config)
    await serve_stdio(bridge)


def main(argv: list[str] | None = None) -> None:
    asyncio.run(_amain(argv))


if __name__ == "__main__":
    main()
