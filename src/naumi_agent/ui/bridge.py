"""JSONL bridge between the Python engine and next-generation terminal UI."""

from __future__ import annotations

import argparse
import asyncio
import logging
import subprocess
import sys
import threading
from collections.abc import Callable, Collection, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO
from uuid import uuid4

from naumi_agent.agent_control import AgentControlSnapshot
from naumi_agent.clipboard import strip_ansi
from naumi_agent.config.settings import AppConfig
from naumi_agent.debug_trace import DebugTrace
from naumi_agent.inspector import RuntimeInspectorSnapshot
from naumi_agent.log_setup import setup_logging
from naumi_agent.runs.models import CompletionReceipt
from naumi_agent.tasks.models import TaskStatus
from naumi_agent.ui.messages import EngineEventAdapter, MessageType, SystemNoticeMessage
from naumi_agent.ui.permission_confirmation import summarize_arguments
from naumi_agent.ui.protocol import (
    ClientEventType,
    ServerEventType,
    decode_jsonl_line,
    encode_jsonl,
    make_envelope,
    normalize_client_record,
    ui_message_payload,
)
from naumi_agent.workbench.models import ParallelMode, RiskLevel

logger = logging.getLogger(__name__)

_TERMINAL_MISSION_STATUSES = frozenset({
    "completed",
    "cancelled",
    "canceled",
    "closed",
    "archived",
})
_SUPPORTED_PERMISSION_CHOICES = frozenset({"allow_once", "deny", "grant_session"})

if TYPE_CHECKING:
    from naumi_agent.orchestrator.engine import AgentEngine

EngineFactory = Callable[[AppConfig], "AgentEngine"]


@dataclass
class PendingPermission:
    """One independently resolvable terminal permission confirmation."""

    future: asyncio.Future[str]
    public_payload: dict[str, Any]
    choices: tuple[str, ...]
    session_id: str
    call_id: str


def _backend_choices_error_message(kind: str) -> str:
    if kind == "missing":
        return "后端权限选择缺失，系统已拒绝本次操作。"
    if kind == "invalid":
        return "后端权限选择格式或内容无效，系统已拒绝本次操作。"
    if kind == "medium_risk_unusable":
        return "后端权限选择无法同时提供批准与拒绝，系统已拒绝本次操作。"
    return "后端权限选择为空或无效，系统已拒绝本次操作。"


def _normalize_backend_choices(raw_choices: Any) -> tuple[str, ...] | None:
    if (
        not isinstance(raw_choices, Collection)
        or isinstance(raw_choices, (str, bytes, bytearray, Mapping))
    ):
        return None

    values = (
        sorted(raw_choices, key=lambda choice: str(choice))
        if isinstance(raw_choices, (set, frozenset))
        else raw_choices
    )
    choices: list[str] = []
    for value in values:
        if not isinstance(value, str):
            return None
        choice = value.strip().lower()
        if not choice or choice not in _SUPPORTED_PERMISSION_CHOICES:
            return None
        if choice not in choices:
            choices.append(choice)
    return tuple(choices)


_SLASH_ALIAS_MAP: dict[str, str] = {
    "/h": "/help",
    "/r": "/resume",
    "/l": "/load",
    "/t": "/tools",
    "/c": "/clear",
    "/m": "/model",
    "/u": "/usage",
    "/v": "/version",
}


def _configure_stdio_utf8(
    *,
    streams: tuple[TextIO, TextIO, TextIO] | None = None,
) -> None:
    """Keep the Node/Python JSONL protocol UTF-8 on Windows code pages."""
    stdin, stdout, stderr = streams or (sys.stdin, sys.stdout, sys.stderr)
    for stream, errors in ((stdin, "strict"), (stdout, "strict"), (stderr, "replace")):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors=errors)


def _start_stdin_line_reader(
    stream: TextIO,
    loop: asyncio.AbstractEventLoop,
) -> asyncio.Queue[str]:
    """Read blocking Windows stdin without occupying asyncio's worker pool."""
    queue: asyncio.Queue[str] = asyncio.Queue()

    def pump() -> None:
        while True:
            line = stream.readline()
            try:
                loop.call_soon_threadsafe(queue.put_nowait, line)
            except RuntimeError:
                return
            if line == "":
                return

    threading.Thread(
        target=pump,
        name="naumi-ui-stdin",
        daemon=True,
    ).start()
    return queue
_EXIT_COMMANDS = {"/q", "/quit", "/exit", "exit"}


def _task_title(text: str) -> str:
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "新任务")
    return first_line[:80]


def _public_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    return {}


def _receipt_reference(receipt: CompletionReceipt | None) -> dict[str, str]:
    if receipt is None:
        return {}
    return {
        "receipt_id": receipt.receipt_id,
        "run_id": receipt.run_id,
    }


def _task_turn_context(
    *,
    task_id: str,
    mission_id: str,
    title: str,
    payload: dict[str, Any],
) -> str:
    criteria = list(payload.get("acceptance_criteria") or [])
    criteria_text = "；".join(criteria) if criteria else "未单独指定"
    return "\n".join([
        "[Workbench task context - trusted runtime fact]",
        f"task_id: {task_id}",
        f"mission_id: {mission_id}",
        f"title: {title}",
        f"parallel_mode: {payload.get('parallel_mode') or 'exclusive'}",
        f"risk_level: {payload.get('risk_level') or 'medium'}",
        f"acceptance_criteria: {criteria_text}",
    ])


def _present_run_error(exc: Exception) -> tuple[str, str]:
    """Map provider failures to actionable UI copy without leaking raw responses."""
    evidence = f"{type(exc).__name__} {exc}".lower()
    auth_markers = ("401", "authentication", "unauthorized", "invalid api key")
    if any(marker in evidence for marker in auth_markers):
        return (
            "模型服务认证失败。请运行 `naumi configure` 更新安全凭据，"
            "然后执行 `naumi doctor --live` 验证。",
            "model_auth_failed",
        )
    if any(marker in evidence for marker in ("404", "notfound", "not found", "resource_not_found")):
        return (
            "模型或 API Base 不匹配，服务端未找到请求资源。请运行 `naumi doctor --live` 检查配置。",
            "model_not_found",
        )
    if any(marker in evidence for marker in ("429", "rate limit", "ratelimit")):
        return (
            "模型服务当前请求过多。请稍后重试；若持续出现，请检查供应商配额。",
            "model_rate_limited",
        )
    if any(marker in evidence for marker in ("timeout", "timed out")):
        return (
            "模型服务响应超时。请检查网络后重试，并可运行 `naumi doctor --live` 验证连接。",
            "model_timeout",
        )
    return (
        "执行失败，详细信息已写入调试日志。请运行 `/debug` 查看诊断路径。",
        "run_failed",
    )


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
        example_path = directory / "config.yaml.example"
        if example_path.exists():
            return example_path
    return None


def _fallback_slash_command_registry() -> list[dict[str, Any]]:
    return [
        {"command": "/help", "aliases": ["/h"], "description": "显示帮助"},
        {"command": "/q", "description": "退出"},
        {"command": "/history", "description": "查看历史会话列表"},
        {"command": "/load", "aliases": ["/l"], "description": "加载会话并继续对话"},
        {"command": "/resume", "aliases": ["/r"], "description": "继续最近一次对话"},
        {
            "command": "/tasks",
            "description": "显示/更新任务面板（支持 list/open/cancel/refresh）",
        },
        {"command": "/task", "description": "查看任务运行详情"},
        {"command": "/permissions", "description": "显示待确认权限面板"},
        {"command": "/doctor", "description": "运行环境诊断"},
        {
            "command": "/mode",
            "aliases": ["/mode"],
            "description": "切换 runtime 模式 default / plan / bypass",
        },
        {"command": "/reasoning", "description": "显示/切换思考过程输出"},
        {"command": "/clear", "aliases": ["/c"], "description": "清空当前会话显示"},
        {"command": "/debug", "description": "显示前端与后端调试路径"},
        {"command": "/pwd", "description": "显示工作区与会话库路径"},
        {"command": "/tools", "description": "列出可用工具"},
        {"command": "/model", "aliases": ["/m"], "description": "查看当前模型配置"},
        {"command": "/usage", "aliases": ["/u"], "description": "查看 Token 与费用"},
        {"command": "/version", "aliases": ["/v"], "description": "查看当前版本"},
        {"command": "/glob", "description": "按 glob 规则搜索工作区文件路径"},
        {"command": "/grep", "description": "搜索文件内容（可配置过滤）"},
        {"command": "/read", "description": "读取文件内容"},
        {"command": "/file_read", "aliases": ["/read"], "description": "读取文件内容（别名）"},
        {"command": "/write", "description": "写入文件（覆盖）"},
        {"command": "/file_write", "aliases": ["/write"], "description": "写入文件（覆盖）"},
        {"command": "/edit", "description": "按文本替换更新文件"},
        {"command": "/file_edit", "aliases": ["/edit"], "description": "按文本替换更新文件"},
    ]


def _load_cli_slash_commands() -> list[dict[str, Any]]:
    try:
        from naumi_agent.cli.completer import COMMANDS
    except Exception:
        return []

    commands = []
    for item in COMMANDS:
        if not item or len(item) < 2:
            continue
        command = str(item[0]).strip()
        if not command.startswith("/"):
            continue
        description = str(item[1]).strip()
        commands.append({"command": command, "description": description})
    return commands


def _load_cli_slash_commands_with_alias() -> list[str]:
    """Load command names from CLI completer and normalize to lower-case set."""
    commands = set[str]()
    try:
        from naumi_agent.cli.completer import COMMANDS
    except Exception:
        for item in _fallback_slash_command_registry():
            commands.add(str(item.get("command", "")).strip())
            for alias in item.get("aliases", []):
                if alias:
                    commands.add(str(alias))
        commands.update(_SLASH_ALIAS_MAP)
        return sorted(commands)

    for item in COMMANDS:
        if not item or len(item) < 1:
            continue
        command = str(item[0]).strip().lower()
        if command.startswith("/"):
            commands.add(command)
    for alias in _SLASH_ALIAS_MAP:
        commands.add(alias)
    return sorted(commands)


def _normalize_slash_commands(commands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    alias_map: dict[str, list[str]] = {}
    for alias, canonical in _SLASH_ALIAS_MAP.items():
        alias_map.setdefault(canonical, []).append(alias)
    canonical: dict[str, dict[str, Any]] = {}
    for item in commands:
        if not item or not isinstance(item, dict):
            continue
        command = str(item.get("command", "")).strip()
        if not command.startswith("/"):
            continue
        canonical_name = command
        entry = canonical.setdefault(
            canonical_name,
            {
                "command": canonical_name,
                "description": str(item.get("description", "") or ""),
                "aliases": list(alias_map.get(command, [])),
            },
        )
        existing_aliases = set(entry.get("aliases") or [])
        for alias in item.get("aliases") if isinstance(item.get("aliases"), list) else []:
            if alias:
                existing_aliases.add(str(alias))
        entry["aliases"] = sorted(existing_aliases)
        if not entry["description"] and item.get("description"):
            entry["description"] = str(item.get("description") or "")
    if not canonical:
        return _fallback_slash_command_registry()
    return sorted(canonical.values(), key=lambda item: item["command"])


def _slash_command_payload() -> list[dict[str, Any]]:
    cli_commands = _load_cli_slash_commands()
    return _normalize_slash_commands(
        cli_commands if cli_commands else _fallback_slash_command_registry()
    )


def _is_exit_command(text: str) -> bool:
    """Return whether user input should close the JSONL bridge."""
    return text.strip().lower() in _EXIT_COMMANDS


def _git_snapshot(cwd: Path) -> dict[str, Any]:
    """Return current git branch and dirty bit for status rendering."""
    result: dict[str, Any] = {"branch": "", "dirty": False}
    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).decode().strip()
        result["branch"] = branch
        result["dirty"] = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"],
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
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
        self._active_run_context: dict[str, str] = {}
        self._active_completion_receipt: CompletionReceipt | None = None
        self._inspector_subscribed = False
        self._inspector_snapshot: RuntimeInspectorSnapshot | None = None
        self._agents_subscribed = False
        self._agents_snapshot: AgentControlSnapshot | None = None
        self._cli_supported_commands = _load_cli_slash_commands_with_alias()
        self._pending_permissions: dict[str, PendingPermission] = {}
        config = getattr(self.engine, "_config", None)
        ui_config = getattr(config, "ui", None)
        self._show_reasoning = bool(getattr(ui_config, "show_reasoning", False))
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

    def status_payload(self, *, include_slash_commands: bool = True) -> dict[str, Any]:
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
        payload = {
            "mode": str(getattr(self.engine.runtime_mode, "value", self.engine.runtime_mode)),
            "permission_mode": str(
                getattr(self.engine.permission_mode, "value", self.engine.permission_mode)
            ),
            "session_id": str(getattr(getattr(self.engine, "_session", None), "id", "")),
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
            "tasks": self._task_activity_payload(),
            "ui": {
                "show_reasoning": self._show_reasoning,
            },
            "git": _git_snapshot(workspace_root),
            "config_path": self.config_path,
        }
        if include_slash_commands:
            payload["slash_commands"] = _slash_command_payload()
        return payload

    def _task_activity_payload(self) -> dict[str, int]:
        """Return compact task/activity counts for persistent footer rendering."""
        payload = {
            "background_running": 0,
            "background_attention": 0,
            "subagents_active": 0,
            "browser_active": 0,
            "permissions_pending": len(self._pending_permissions),
        }

        try:
            runner = getattr(self.engine, "background_runner", None)
            if runner is not None:
                for task in runner.list_tasks():
                    raw_status = getattr(task, "status", "")
                    status = str(getattr(raw_status, "value", raw_status))
                    if status == "running":
                        payload["background_running"] += 1
                    elif (
                        status in {"failed", "timed_out"}
                        and not bool(getattr(task, "notified", False))
                    ):
                        payload["background_attention"] += 1
        except Exception:
            payload["background_attention"] += 1

        try:
            manager = getattr(self.engine, "subagent_manager", None)
            if manager is not None:
                for agent in manager.list_agents():
                    state = str(agent.get("state") or "")
                    if state in {"spawned", "running"}:
                        payload["subagents_active"] += 1
        except Exception:
            payload["subagents_active"] += 1

        try:
            task_runner = getattr(self.engine, "task_runner", None)
            if task_runner is not None:
                for run in task_runner.list_runs(limit=20):
                    status = str(run.get("status") or "")
                    if status not in {"completed", "failed", "cancelled", "timeout", "timed_out"}:
                        payload["browser_active"] += 1
        except Exception:
            payload["browser_active"] += 1

        return payload

    async def handle_client_record(self, record: dict[str, Any]) -> None:
        """Dispatch one client protocol record."""
        if not record:
            return
        try:
            record = normalize_client_record(record)
        except ValueError as exc:
            bad_request_id = str(record.get("id") or record.get("request_id") or "")
            await self.emit_error(str(exc), code="bad_request", request_id=bad_request_id)
            return
        event_type = str(record.get("type", ""))
        payload = record.get("payload", {})
        request_id = str(record.get("id") or record.get("request_id") or "")

        if self.debug_trace is not None:
            self.debug_trace.input("ui_bridge.stdin", encode_jsonl(record))

        if event_type == ClientEventType.HELLO:
            await self.emit(ServerEventType.ACK, {"event": event_type}, request_id=request_id)
            await self.emit(
                ServerEventType.STATUS,
                self.status_payload(include_slash_commands=False),
            )
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

        if event_type == ClientEventType.SET_REASONING:
            await self.set_reasoning(bool(payload.get("enabled")), request_id=request_id)
            return

        if event_type == ClientEventType.PERMISSION_RESPONSE:
            await self.resolve_permission(payload, request_id=request_id)
            return

        if event_type == ClientEventType.PERMISSION_REVOKE:
            await self.revoke_permission_grant(payload, request_id=request_id)
            return

        if event_type == ClientEventType.SUBMIT:
            await self.submit(str(payload.get("text", "")), request_id=request_id)
            return

        if event_type == ClientEventType.TASK_SUBMIT:
            await self.submit_task(payload, request_id=request_id)
            return

        if event_type == ClientEventType.RUN_CANCEL:
            await self.cancel_run(payload, request_id=request_id)
            return
        if event_type == ClientEventType.RECEIPT_REQUEST:
            await self.resend_completion_receipt(payload, request_id=request_id)
            return
        if event_type == ClientEventType.INSPECTOR_REQUEST:
            await self.show_inspector(payload, request_id=request_id)
            return
        if event_type == ClientEventType.AGENTS_REQUEST:
            await self.show_agents(payload, request_id=request_id)
            return
        if event_type == ClientEventType.AGENTS_STOP:
            await self.stop_agent_execution(payload, request_id=request_id)
            return

        if event_type == ClientEventType.RESUME:
            await self.resume_session(payload, request_id=request_id)
            return

        if event_type == ClientEventType.TASK_PANEL:
            await self.show_task_panel(payload, request_id=request_id)
            return

        if event_type == ClientEventType.TASK_CANCEL:
            await self.cancel_task(payload, request_id=request_id)
            return

        if event_type == ClientEventType.PERMISSIONS_PANEL:
            await self.show_permissions_panel(payload, request_id=request_id)
            return

        if event_type == ClientEventType.DOCTOR:
            await self.show_doctor_report(request_id=request_id)
            return

        if event_type == ClientEventType.SHUTDOWN:
            await self.shutdown()
            return

        await self.emit_error(f"未知客户端事件: {event_type}", request_id=request_id)

    async def set_reasoning(self, enabled: bool, *, request_id: str) -> None:
        self._show_reasoning = enabled
        await self.emit(
            ServerEventType.STATUS,
            self.status_payload(),
            request_id=request_id,
        )

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
        normalized_text = text.strip()
        if _is_exit_command(normalized_text):
            await self.shutdown()
            return
        if normalized_text.startswith("/"):
            await self._run_cli_slash_command(normalized_text, request_id=request_id)
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
        self._active_completion_receipt = None

        async def run() -> None:
            try:
                result = await self.engine.run_streaming(text, self.handle_engine_event)
                await self.emit(
                    ServerEventType.RUN_COMPLETED,
                    {
                        "status": result.status,
                        "response": result.response or "",
                        "error": result.error or "",
                        **_receipt_reference(
                            getattr(result, "receipt", None)
                            or self._active_completion_receipt
                        ),
                    },
                    request_id=request_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.debug_trace is not None:
                    self.debug_trace.exception("ui_bridge.run", exc)
                logger.debug("UI bridge agent run failed: %s", type(exc).__name__)
                message, code = _present_run_error(exc)
                await self.emit_error(
                    message,
                    code=code,
                    request_id=request_id,
                    details=_receipt_reference(self._active_completion_receipt),
                )
                await self.emit(
                    ServerEventType.RUN_COMPLETED,
                    {
                        "status": "failed",
                        "response": "",
                        "error": message,
                    },
                    request_id=request_id,
                )
            finally:
                if self._active_run_context.get("request_id") == request_id:
                    self._active_run_context = {}
                await self.emit(ServerEventType.STATUS, self.status_payload())

        self._active_run_context = {
            "request_id": request_id,
            "intent": "chat",
        }
        self._run_task = asyncio.create_task(run())

    async def submit_task(self, payload: dict[str, Any], *, request_id: str) -> None:
        """Create one Workbench issue and execute it in the active conversation."""
        if self._run_task is not None and not self._run_task.done():
            await self.emit_error(
                "当前任务仍在执行，请等待完成后再创建任务。",
                code="run_in_progress",
                request_id=request_id,
            )
            return

        text = str(payload.get("text") or "").strip("\n")
        title = str(payload.get("title") or "").strip() or _task_title(text)
        session = await self.engine.get_or_create_session(title=title)
        session_id = str(session.id)
        task_store = self.engine.task_store.scoped(session_id)
        task_store.set_session(session_id)
        service = getattr(self.engine, "workbench_service", None)
        if service is None:
            await self.emit_error(
                "Workbench 服务暂不可用。",
                code="workbench_unavailable",
                request_id=request_id,
            )
            return

        mission = await self._resolve_task_mission(
            service,
            session_id=session_id,
            mission_id=str(payload.get("mission_id") or ""),
            title=title,
            goal=text,
            request_id=request_id,
        )
        if mission is None:
            return
        mission_data = _public_mapping(mission)
        mission_id = str(mission_data.get("id") or "")
        try:
            issue = await service.create_issue(
                session_id=session_id,
                mission_id=mission_id,
                title=title,
                description=text,
                blocked_by=list(payload.get("blocked_by") or []),
                acceptance_criteria=list(payload.get("acceptance_criteria") or []),
                parallel_mode=ParallelMode(str(payload.get("parallel_mode") or "exclusive")),
                risk_level=RiskLevel(str(payload.get("risk_level") or "medium")),
            )
        except (RuntimeError, ValueError) as exc:
            await self.emit_error(str(exc), code="task_create_failed", request_id=request_id)
            return

        task_data = dict(issue.get("task") or {})
        task_id = str(task_data.get("id") or issue.get("task_id") or "")
        await task_store.update_task(task_id, status=TaskStatus.IN_PROGRESS)
        task_data["status"] = TaskStatus.IN_PROGRESS.value
        snapshot = await service.dashboard_snapshot(session_id)
        context = _task_turn_context(
            task_id=task_id,
            mission_id=mission_id,
            title=title,
            payload=payload,
        )
        await self.emit(
            ServerEventType.USER_MESSAGE,
            {"content": text, "intent": "task", "task_id": task_id},
            request_id=request_id,
        )
        await self.emit(
            ServerEventType.TASK_CREATED,
            {
                "mission": mission_data,
                "issue": issue,
                "task": task_data,
                "workbench_snapshot": snapshot,
            },
            request_id=request_id,
        )
        await self.emit(ServerEventType.WORKBENCH_SNAPSHOT, snapshot, request_id=request_id)
        await self.emit(
            ServerEventType.RUN_STARTED,
            {"task": text, "task_id": task_id, "mission_id": mission_id, "intent": "task"},
            request_id=request_id,
        )
        await self.emit(ServerEventType.STATUS, self.status_payload())
        self._active_completion_receipt = None

        async def run() -> None:
            try:
                result = await self.engine.run_streaming(
                    text,
                    self.handle_engine_event,
                    turn_context=context,
                )
                final_status = (
                    TaskStatus.COMPLETED
                    if result.status == "completed"
                    else TaskStatus.BLOCKED
                )
                await task_store.update_task(task_id, status=final_status)
                final_snapshot = await service.dashboard_snapshot(session_id)
                await self.emit(
                    ServerEventType.WORKBENCH_SNAPSHOT,
                    final_snapshot,
                    request_id=request_id,
                )
                await self.emit(
                    ServerEventType.RUN_COMPLETED,
                    {
                        "status": result.status,
                        "response": result.response or "",
                        "error": result.error or "",
                        "task_id": task_id,
                        "mission_id": mission_id,
                        "intent": "task",
                        **_receipt_reference(
                            getattr(result, "receipt", None)
                            or self._active_completion_receipt
                        ),
                    },
                    request_id=request_id,
                )
            except asyncio.CancelledError:
                await task_store.update_task(task_id, status=TaskStatus.BLOCKED)
                cancelled_snapshot = await service.dashboard_snapshot(session_id)
                await self.emit(
                    ServerEventType.WORKBENCH_SNAPSHOT,
                    cancelled_snapshot,
                    request_id=request_id,
                )
                raise
            except Exception as exc:
                await task_store.update_task(task_id, status=TaskStatus.BLOCKED)
                failed_snapshot = await service.dashboard_snapshot(session_id)
                await self.emit(
                    ServerEventType.WORKBENCH_SNAPSHOT,
                    failed_snapshot,
                    request_id=request_id,
                )
                message, code = _present_run_error(exc)
                await self.emit_error(
                    message,
                    code=code,
                    request_id=request_id,
                    details={
                        "task_id": task_id,
                        "mission_id": mission_id,
                        "intent": "task",
                        "task_status": TaskStatus.BLOCKED.value,
                        **_receipt_reference(self._active_completion_receipt),
                    },
                )
            finally:
                if self._active_run_context.get("request_id") == request_id:
                    self._active_run_context = {}
                await self.emit(ServerEventType.STATUS, self.status_payload())

        self._active_run_context = {
            "request_id": request_id,
            "intent": "task",
            "task_id": task_id,
            "mission_id": mission_id,
        }
        self._run_task = asyncio.create_task(run())

    async def cancel_run(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Cancel the active Agent run without shutting down the Bridge."""
        run_task = self._run_task
        if run_task is None or run_task.done():
            await self.emit_error(
                "当前没有正在运行的任务。",
                code="no_active_run",
                request_id=request_id,
            )
            return

        context = dict(self._active_run_context)
        target_request_id = context.get("request_id", "")
        reason = str(payload.get("reason") or "").strip() or "用户取消了当前运行。"
        await self.emit(
            ServerEventType.ACK,
            {
                "event": ClientEventType.RUN_CANCEL,
                "status": "accepted",
                "target_request_id": target_request_id,
            },
            request_id=request_id,
        )
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

        cancelled = {
            "status": "cancelled",
            "target_request_id": target_request_id,
            "intent": context.get("intent", "chat"),
            "reason": reason,
            **_receipt_reference(self._active_completion_receipt),
        }
        if context.get("task_id"):
            cancelled.update({
                "task_id": context["task_id"],
                "mission_id": context.get("mission_id", ""),
                "task_status": TaskStatus.BLOCKED.value,
            })
        await self.emit(
            ServerEventType.RUN_CANCELLED,
            cancelled,
            request_id=request_id,
        )
        await self.emit(ServerEventType.STATUS, self.status_payload())

    async def resend_completion_receipt(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Resend one durable receipt without allowing cross-session lookup."""
        receipt_id = str(payload.get("receipt_id") or "")
        run_id = str(payload.get("run_id") or "")
        session_id = str(payload.get("session_id") or "")
        current_session_id = str(
            getattr(getattr(self.engine, "_session", None), "id", "")
        )
        if not session_id:
            session_id = current_session_id

        receipt = self._active_completion_receipt
        if session_id and current_session_id and session_id != current_session_id:
            receipt = None
        if receipt is not None and (
            (receipt_id and receipt.receipt_id != receipt_id)
            or (run_id and receipt.run_id != run_id)
        ):
            receipt = None

        store = getattr(self.engine, "chat_run_store", None)
        if receipt is None and store is not None and session_id:
            if receipt_id:
                receipt = await store.get_receipt(session_id, receipt_id)
            elif run_id:
                run = await store.get_run(session_id, run_id)
                receipt = run.receipt if run is not None else None
        if receipt is None or (run_id and receipt.run_id != run_id):
            await self.emit_error(
                "未找到可补发的完成回执。",
                code="receipt_not_found",
                request_id=request_id,
            )
            return
        await self.emit(
            ServerEventType.COMPLETION_RECEIPT,
            receipt.to_dict(),
            request_id=request_id,
        )

    async def show_inspector(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Open, refresh, or close the current session Inspector subscription."""
        if not bool(payload.get("open", True)):
            self._inspector_subscribed = False
            revision = (
                self._inspector_snapshot.revision
                if self._inspector_snapshot is not None
                else 0
            )
            self._inspector_snapshot = None
            await self.emit(
                ServerEventType.ACK,
                {
                    "event": str(ClientEventType.INSPECTOR_REQUEST),
                    "open": False,
                    "revision": revision,
                },
                request_id=request_id,
            )
            return

        session = getattr(self.engine, "_session", None)
        if session is None:
            session = await self.engine.get_or_create_session()
        session_id = str(getattr(session, "id", "") or "")
        requested_session_id = str(payload.get("session_id") or "")
        if requested_session_id and requested_session_id != session_id:
            await self.emit_error(
                "Inspector 只能读取当前会话。",
                code="inspector_session_mismatch",
                request_id=request_id,
            )
            return

        snapshot = await self.engine.runtime_inspector.snapshot()
        if snapshot.session_id != session_id:
            await self.emit_error(
                "Inspector 快照会话与当前会话不一致。",
                code="inspector_session_mismatch",
                request_id=request_id,
            )
            return
        self._inspector_subscribed = True
        self._inspector_snapshot = snapshot
        await self.emit(
            ServerEventType.INSPECTOR_SNAPSHOT,
            snapshot.to_dict(),
            request_id=request_id,
        )

    async def _emit_inspector_update(self) -> None:
        if not self._inspector_subscribed:
            return
        try:
            current = await self.engine.runtime_inspector.snapshot()
            previous = self._inspector_snapshot
            if previous is None or previous.session_id != current.session_id:
                self._inspector_snapshot = current
                await self.emit(ServerEventType.INSPECTOR_SNAPSHOT, current.to_dict())
                return
            changed_tabs = self.engine.runtime_inspector.changed_tabs(previous, current)
            if not changed_tabs and current.revision == previous.revision:
                return
            self._inspector_snapshot = current
            if not changed_tabs or current.revision != previous.revision + 1:
                await self.emit(ServerEventType.INSPECTOR_SNAPSHOT, current.to_dict())
                return
            payload = current.to_dict()
            await self.emit(
                ServerEventType.INSPECTOR_UPDATE,
                {
                    "schema_version": payload["schema_version"],
                    "session_id": payload["session_id"],
                    "revision": payload["revision"],
                    "generated_at": payload["generated_at"],
                    "active_run_id": payload["active_run_id"],
                    "changed_tabs": {
                        name: payload[name]
                        for name in changed_tabs
                    },
                },
            )
        except Exception:
            logger.exception("Runtime Inspector refresh failed")
            await self.emit_error(
                "Inspector 刷新失败，已保留上一次快照。",
                code="inspector_refresh_failed",
            )

    async def show_agents(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Open, refresh, or close the current session Agent subscription."""
        was_subscribed = self._agents_subscribed
        if not bool(payload.get("open", True)):
            self._agents_subscribed = False
            revision = self._agents_snapshot.revision if self._agents_snapshot else 0
            self._agents_snapshot = None
            await self.emit(
                ServerEventType.ACK,
                {
                    "event": str(ClientEventType.AGENTS_REQUEST),
                    "open": False,
                    "revision": revision,
                },
                request_id=request_id,
            )
            return

        session = getattr(self.engine, "_session", None)
        if session is None:
            session = await self.engine.get_or_create_session()
        session_id = str(getattr(session, "id", "") or "")
        requested_session_id = str(payload.get("session_id") or "")
        if requested_session_id and requested_session_id != session_id:
            await self.emit_error(
                "Agent 页面只能读取当前会话。",
                code="agents_session_mismatch",
                request_id=request_id,
            )
            return

        try:
            snapshot = await self.engine.agent_control.snapshot()
        except Exception:
            logger.exception("Agent Control initial snapshot failed")
            await self.emit_error(
                "Agent 页面暂时无法加载，请稍后重试。",
                code="agents_snapshot_failed",
                request_id=request_id,
            )
            return
        if snapshot.session_id != session_id:
            await self.emit_error(
                "Agent 快照会话与当前会话不一致。",
                code="agents_session_mismatch",
                request_id=request_id,
            )
            return
        self._agents_subscribed = True
        self._agents_snapshot = snapshot
        if was_subscribed and int(payload.get("known_revision", 0)) == snapshot.revision:
            await self.emit(
                ServerEventType.ACK,
                {
                    "event": str(ClientEventType.AGENTS_REQUEST),
                    "open": True,
                    "revision": snapshot.revision,
                },
                request_id=request_id,
            )
            return
        await self.emit(
            ServerEventType.AGENTS_SNAPSHOT,
            snapshot.to_dict(),
            request_id=request_id,
        )

    async def stop_agent_execution(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Request cancellation of one concrete Agent execution."""
        session = getattr(self.engine, "_session", None)
        session_id = str(getattr(session, "id", "") or "")
        requested_session_id = str(payload.get("session_id") or "")
        if requested_session_id and requested_session_id != session_id:
            await self.emit_error(
                "Agent 停止请求不属于当前会话。",
                code="agents_session_mismatch",
                request_id=request_id,
            )
            return
        task_id = str(payload.get("task_id") or "")
        target = next(
            (
                item
                for item in self.engine.subagent_manager.list_executions(limit=100)
                if item.task_id == task_id
            ),
            None,
        )
        if target is not None and target.session_id != session_id:
            await self.emit_error(
                "Agent 停止目标不属于当前会话。",
                code="agents_session_mismatch",
                request_id=request_id,
            )
            return
        result = await self.engine.subagent_manager.stop_execution(
            task_id,
            str(payload.get("reason") or "用户请求停止子 Agent。"),
        )
        await self.emit(
            ServerEventType.AGENTS_ACTION,
            asdict(result),
            request_id=request_id,
        )
        await self._emit_agents_update()

    async def _emit_agents_update(self) -> None:
        if not self._agents_subscribed:
            return
        try:
            current = await self.engine.agent_control.snapshot()
            previous = self._agents_snapshot
            if previous is None or previous.session_id != current.session_id:
                self._agents_snapshot = current
                await self.emit(ServerEventType.AGENTS_SNAPSHOT, current.to_dict())
                return
            changed_sections = self.engine.agent_control.changed_sections(previous, current)
            if not changed_sections and current.revision == previous.revision:
                return
            self._agents_snapshot = current
            if not changed_sections or current.revision != previous.revision + 1:
                await self.emit(ServerEventType.AGENTS_SNAPSHOT, current.to_dict())
                return
            public = current.to_dict()
            await self.emit(
                ServerEventType.AGENTS_UPDATE,
                {
                    "schema_version": public["schema_version"],
                    "session_id": public["session_id"],
                    "revision": public["revision"],
                    "generated_at": public["generated_at"],
                    "changed_sections": {
                        name: public[name]
                        for name in changed_sections
                    },
                },
            )
        except Exception:
            logger.exception("Agent Control refresh failed")
            await self.emit_error(
                "Agent 页面刷新失败，已保留上一次快照。",
                code="agents_refresh_failed",
            )

    async def _resolve_task_mission(
        self,
        service: Any,
        *,
        session_id: str,
        mission_id: str,
        title: str,
        goal: str,
        request_id: str,
    ) -> Any | None:
        response = await service.list_missions(session_id)
        missions = list(response.get("missions") or [])
        if mission_id:
            match = next(
                (
                    mission
                    for mission in missions
                    if str(_public_mapping(mission).get("id")) == mission_id
                ),
                None,
            )
            if match is None:
                await self.emit_error(
                    f"Mission 不存在或不属于当前会话: {mission_id}",
                    code="mission_not_found",
                    request_id=request_id,
                )
                return None
            status = str(_public_mapping(match).get("status") or "").strip().lower()
            if status in _TERMINAL_MISSION_STATUSES:
                await self.emit_error(
                    f"Mission 已结束，不能创建新任务: {mission_id}",
                    code="mission_closed",
                    request_id=request_id,
                )
                return None
            return match
        open_missions = [
            mission
            for mission in missions
            if str(_public_mapping(mission).get("status") or "").strip().lower()
            not in _TERMINAL_MISSION_STATUSES
        ]
        if len(open_missions) == 1:
            return open_missions[0]
        if not open_missions:
            return await service.create_mission(
                session_id=session_id,
                title=title[:80],
                goal=goal,
            )
        candidates = "、".join(
            f"{data.get('id')}({data.get('title') or '未命名'})"
            for data in (_public_mapping(mission) for mission in open_missions[:8])
        )
        await self.emit_error(
            f"当前会话有多个 Mission，请指定 mission_id。可选: {candidates}",
            code="mission_required",
            request_id=request_id,
        )
        return None

    async def resume_session(self, payload: dict[str, Any], *, request_id: str) -> None:
        """Load a persisted session and replay it as typed UI messages."""
        from naumi_agent.ui.messages.replay import replay_messages

        if self._run_task is not None and not self._run_task.done():
            await self.emit_error(
                "当前任务仍在执行，请等待完成后再恢复会话。",
                code="run_in_progress",
                request_id=request_id,
            )
            return

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

        self._inspector_subscribed = False
        self._inspector_snapshot = None

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
        run_store = getattr(self.engine, "chat_run_store", None)
        if run_store is not None:
            runs = await run_store.list_runs(session_id, limit=200)
            for run in reversed(runs):
                if run.receipt is not None:
                    await self.emit(
                        ServerEventType.COMPLETION_RECEIPT,
                        run.receipt.to_dict(),
                        request_id=request_id,
                    )
        await self.emit(ServerEventType.STATUS, self.status_payload())

    async def _run_cli_slash_command(self, cmd: str, *, request_id: str) -> None:
        """Execute a slash command through the legacy CLI command handlers."""
        from naumi_agent.cli.slash_router import execute_slash_command

        parse_reasoning_toggle = None
        try:
            from naumi_agent.main import _parse_reasoning_toggle as parse_reasoning_toggle
        except Exception:
            parse_reasoning_toggle = None

        try:
            output = await execute_slash_command(self.engine, cmd)
        except Exception as exc:
            logger.exception("UI bridge slash command execution failed")
            if self.debug_trace is not None:
                self.debug_trace.exception("ui_bridge.slash", exc)
            await self.emit_error(
                f"执行命令失败: {cmd}",
                code="slash_failed",
                request_id=request_id,
            )
            return

        plain_output = strip_ansi(output).strip()
        if plain_output.startswith("未知命令:"):
            command = str(cmd).split(maxsplit=1)[0]
            await self.emit_error(
                f"未知命令: {command}",
                code="unknown_command",
                request_id=request_id,
            )
            return

        raw = str(cmd).strip()
        if raw.lower().startswith("/reasoning"):
            parts = raw.split(maxsplit=1)
            arg = parts[1] if len(parts) > 1 else ""
            try:
                if parse_reasoning_toggle is None:
                    raise ValueError
                enabled, _ = parse_reasoning_toggle(arg, self._show_reasoning)
            except TypeError:
                enabled = None
            except ValueError:
                enabled = self._show_reasoning
            if enabled is not None:
                self._show_reasoning = enabled

        text = plain_output
        if text:
            command_name = raw.split(maxsplit=1)[0].lower()
            notice_title = "help" if command_name in {"/help", "/h"} else "command"
            await self._emit_system_notice(
                notice_title,
                text,
                "info",
                request_id=request_id,
            )
        else:
            await self._emit_system_notice(
                "command",
                f"命令已执行: {cmd}",
                "info",
                request_id=request_id,
            )
        await self.emit(ServerEventType.STATUS, self.status_payload())

    async def _load_session_command(self, arg: str, *, request_id: str) -> None:
        if not arg:
            sessions, _ = await self.engine.list_sessions(page=1, page_size=10)
            if not sessions:
                await self._emit_system_notice(
                    "load",
                    "暂无可恢复会话。",
                    "warning",
                    request_id=request_id,
                )
                return
            lines = ["可恢复会话（输入 /load <编号> 或 /load <id>）："]
            for index, session in enumerate(sessions, 1):
                message_count = len(getattr(session, "messages", []) or [])
                title = getattr(session, "title", "新会话") or "新会话"
                if len(title) > 28:
                    title = f"{title[:25]}…"
                lines.append(f"{index}. {session.id} · {title} · {message_count}条消息")
            await self._emit_system_notice("load", "\n".join(lines), "info", request_id=request_id)
            return

        if arg.isdigit():
            sessions, _ = await self.engine.list_sessions(page=1, page_size=20)
            index = int(arg) - 1
            if 0 <= index < len(sessions):
                await self.resume_session(
                    {"session_id": str(sessions[index].id)},
                    request_id=request_id,
                )
                return
            await self._emit_system_notice(
                "load",
                f"编号无效: {arg}",
                "warning",
                request_id=request_id,
            )
            return

        loaded = await self.engine.load_session(arg)
        if not loaded:
            await self._emit_system_notice(
                "load",
                f"会话不存在: {arg}",
                "warning",
                request_id=request_id,
            )
            return
        await self.resume_session({"session_id": arg}, request_id=request_id)

    def _git_snapshot_branch(self) -> str:
        return _git_snapshot(getattr(self.engine, "workspace_root", Path.cwd())).get("branch", "")

    async def _emit_system_notice(
        self,
        title: str,
        content: str,
        level: str = "info",
        *,
        request_id: str,
    ) -> None:
        await self.emit(
            ServerEventType.UI_MESSAGE,
            ui_message_payload(
                SystemNoticeMessage(
                    type=MessageType.SYSTEM_NOTICE,
                    title=title,
                    content=content,
                    level=level,
                )
            ),
            request_id=request_id,
        )

    async def show_task_panel(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Render the read-only task panel through the UI protocol."""
        from naumi_agent.ui.task_panel import render_task_panel

        raw_limit = payload.get("limit", 12)
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            limit = 12
        content = await render_task_panel(
            self.engine,
            limit=limit,
            source=str(payload.get("source") or "all"),
            status=str(payload.get("status") or "all"),
            detail_id=str(payload.get("detail_id") or payload.get("detail") or ""),
            history=bool(payload.get("history", False)),
        )
        await self.emit(
            ServerEventType.UI_MESSAGE,
            ui_message_payload(
                SystemNoticeMessage(
                    type=MessageType.SYSTEM_NOTICE,
                    title="tasks",
                    content=content,
                    level="info",
                )
            ),
            request_id=request_id,
        )
        await self.emit(ServerEventType.STATUS, self.status_payload())

    async def cancel_task(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Cancel a concrete task owned by a backend runner."""
        task_id = str(payload.get("task_id") or "").strip()
        source = str(payload.get("source") or "all").strip().lower().replace("-", "_")
        reason = str(payload.get("reason") or "用户从任务面板取消。").strip()
        if not task_id:
            await self.emit_error(
                "任务取消缺少 task_id。",
                code="task_cancel_missing_id",
                request_id=request_id,
            )
            return

        message = ""
        level = "info"

        if source in {"all", "background"}:
            runner = getattr(self.engine, "background_runner", None)
            if runner is not None:
                task = None
                getter = getattr(runner, "get", None)
                if callable(getter):
                    task = getter(task_id)
                if task is not None:
                    cancelled = await runner.cancel(task_id)
                    status = getattr(getattr(cancelled, "status", ""), "value", "")
                    message = f"已请求取消后台任务 {task_id}。当前状态: {status or '-'}"
                elif source == "background":
                    message = f"未找到后台任务 {task_id}。"
                    level = "warning"

        if not message and source in {"all", "browser"}:
            task_runner = getattr(self.engine, "task_runner", None)
            if task_runner is not None:
                try:
                    run = task_runner.abort_run(
                        task_id,
                        reason=reason or "用户从任务面板取消。",
                    )
                except ValueError as exc:
                    if source == "browser":
                        message = f"浏览器任务取消失败: {exc}"
                        level = "warning"
                else:
                    status = str(run.get("status") or "-")
                    message = f"已请求取消浏览器任务 {task_id}。当前状态: {status}"

        if not message:
            message = (
                f"任务 {task_id} 当前来源不支持直接取消。"
                "支持来源: background / browser。"
            )
            level = "warning"

        await self.emit(
            ServerEventType.UI_MESSAGE,
            ui_message_payload(
                SystemNoticeMessage(
                    type=MessageType.SYSTEM_NOTICE,
                    title="tasks",
                    content=message,
                    level=level,
                )
            ),
            request_id=request_id,
        )
        await self.emit(ServerEventType.STATUS, self.status_payload())

    async def show_permissions_panel(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Render the read-only permission panel through the UI protocol."""
        from naumi_agent.ui.permission_panel import render_permission_panel

        raw_limit = payload.get("limit", 12)
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            limit = 12
        content = render_permission_panel(
            self.engine,
            pending={
                pending_id: pending.public_payload
                for pending_id, pending in self._pending_permissions.items()
            },
            limit=limit,
        )
        await self.emit(
            ServerEventType.UI_MESSAGE,
            ui_message_payload(
                SystemNoticeMessage(
                    type=MessageType.SYSTEM_NOTICE,
                    title="permissions",
                    content=content,
                    level="info",
                )
            ),
            request_id=request_id,
        )
        await self.emit(ServerEventType.STATUS, self.status_payload())

    async def show_doctor_report(self, *, request_id: str) -> None:
        """Render deterministic local diagnostics through the UI protocol."""
        from naumi_agent.ui.doctor import render_doctor_report, run_doctor

        config = getattr(self.engine, "_config", AppConfig())
        report = await run_doctor(
            config,
            workspace_root=self.engine.workspace_root,
            mcp_manager=getattr(self.engine, "mcp_manager", None),
        )
        await self.emit(
            ServerEventType.UI_MESSAGE,
            ui_message_payload(
                SystemNoticeMessage(
                    type=MessageType.SYSTEM_NOTICE,
                    title="doctor",
                    content=render_doctor_report(report),
                    level=report.status,
                )
            ),
            request_id=request_id,
        )
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
        if event == "completion_receipt":
            receipt = CompletionReceipt.from_dict(data)
            self._active_completion_receipt = receipt
            await self.emit(
                ServerEventType.COMPLETION_RECEIPT,
                receipt.to_dict(),
                request_id=self._active_run_context.get("request_id") or None,
            )
        message = self.adapter.adapt(event, data)
        if message is not None and event != "completion_receipt":
            await self.emit(ServerEventType.UI_MESSAGE, ui_message_payload(message))

        if event in {
            "run_started",
            "tool_end",
            "task_snapshot",
            "permission_bubble",
            "context_compacted",
            "error",
        }:
            await self.emit(
                ServerEventType.STATUS,
                self.status_payload(include_slash_commands=False),
            )
        if event in {
            "run_started",
            "turn_start",
            "tool_start",
            "tool_end",
            "tool_error",
            "task_snapshot",
            "permission_bubble",
            "context_compacted",
            "response_end",
            "completion_receipt",
            "error",
        }:
            await self._emit_inspector_update()
        if event in {
            "subagent_event",
            "team_event",
            "tool_prepare_start",
            "tool_prepare_snapshot",
            "tool_prepare_end",
            "tool_start",
            "tool_use",
            "tool_result",
            "tool_end",
            "tool_error",
            "permission_bubble",
            "completion_receipt",
            "error",
        }:
            await self._emit_agents_update()

    async def confirm_permission(self, payload: dict[str, Any]) -> str:
        if self._closed:
            return "deny"

        call_id = str(payload.get("call_id") or "").strip()
        request_id = call_id
        if not request_id or request_id in self._pending_permissions:
            request_id = self._next_permission_request_id()
        if "choices" not in payload:
            await self.emit_error(
                _backend_choices_error_message("missing"),
                code="permission_choices_missing",
                request_id=request_id,
            )
            return "deny"
        choices = _normalize_backend_choices(payload["choices"])
        if choices is None:
            await self.emit_error(
                _backend_choices_error_message("invalid"),
                code="permission_choices_invalid",
                request_id=request_id,
            )
            return "deny"
        if not choices:
            await self.emit_error(
                _backend_choices_error_message("empty"),
                code="permission_choices_empty",
                request_id=request_id,
            )
            return "deny"
        if not {"allow_once", "deny"}.issubset(choices):
            await self.emit_error(
                _backend_choices_error_message("medium_risk_unusable"),
                code="permission_choices_medium_risk_unusable",
                request_id=request_id,
            )
            return "deny"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        public_payload = {
            "request_id": request_id,
            "call_id": call_id,
            "session_id": str(payload.get("session_id") or ""),
            "run_id": str(payload.get("run_id") or ""),
            "agent_name": str(payload.get("agent_name") or payload.get("agent") or "main"),
            "tool_name": str(payload.get("tool_name") or payload.get("tool") or "tool"),
            "tool_family": str(payload.get("tool_family") or ""),
            "arguments_summary": summarize_arguments(payload.get("arguments", {})),
            "reason": str(payload.get("reason") or "等待用户确认。"),
            "risk_level": str(payload.get("risk_level") or "medium"),
            "choices": list(choices),
            "scope": "session" if "grant_session" in choices else "call",
            "expires_at": payload.get("expires_at"),
            "requires_double_confirm": False,
            "status": "needs_confirmation",
        }
        pending = PendingPermission(
            future=future,
            public_payload=public_payload,
            choices=choices,
            session_id=public_payload["session_id"],
            call_id=call_id,
        )
        self._pending_permissions[request_id] = pending
        await self.emit(ServerEventType.PERMISSION_REQUEST, public_payload, request_id=request_id)
        try:
            return await future
        finally:
            if self._pending_permissions.get(request_id) is pending:
                self._pending_permissions.pop(request_id, None)

    def _next_permission_request_id(self) -> str:
        while True:
            request_id = f"perm-{uuid4().hex}"
            if request_id not in self._pending_permissions:
                return request_id

    async def resolve_permission(self, payload: dict[str, Any], *, request_id: str) -> None:
        permission_id = str(payload.get("request_id") or request_id)
        choice = str(payload.get("choice", "deny")).strip().lower()
        pending = self._pending_permissions.get(permission_id)
        if pending is None or pending.future.done():
            await self.emit_error(
                f"未找到待确认权限请求: {permission_id}",
                code="unknown_permission_request",
                request_id=request_id,
            )
            return
        if choice == "allow":
            choice = "allow_once"
        elif choice == "bypass":
            runtime_mode = self.engine.set_runtime_mode("bypass")
            await self.emit(
                ServerEventType.MODE_CHANGED,
                {"mode": runtime_mode.value, "status": self.status_payload()},
                request_id=request_id,
            )
            await self.emit(ServerEventType.STATUS, self.status_payload())
            await self._resolve_pending_permission(
                permission_id,
                pending,
                "allow_once",
                response_request_id=request_id,
                public_choice="bypass",
            )
            return
        if choice not in pending.choices:
            await self.emit_error(
                "当前权限请求不支持该选择。",
                code="permission_choice_unavailable",
                request_id=request_id,
            )
            return
        await self._resolve_pending_permission(
            permission_id,
            pending,
            choice,
            response_request_id=request_id,
        )

    async def _resolve_pending_permission(
        self,
        permission_id: str,
        pending: PendingPermission,
        choice: str,
        *,
        response_request_id: str,
        public_choice: str | None = None,
    ) -> None:
        pending.future.set_result(choice)
        resolved_choice = public_choice or choice
        status = {
            "allow_once": "allowed",
            "deny": "denied",
            "grant_session": "granted",
            "bypass": "bypass_enabled",
        }[resolved_choice]
        await self.emit(
            ServerEventType.PERMISSION_RESOLVED,
            {"request_id": permission_id, "choice": resolved_choice, "status": status},
            request_id=response_request_id,
        )

    async def revoke_permission_grant(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Revoke one or all current-session grants through the engine API."""
        if payload.get("scope") == "all":
            revoked = int(self.engine.revoke_all_permission_grants())
        else:
            revoked = int(bool(self.engine.revoke_permission_grant(str(payload["grant_id"]))))
        grants = [_public_mapping(grant) for grant in self.engine.list_permission_grants()]
        await self.emit(
            ServerEventType.PERMISSION_GRANTS_CHANGED,
            {"revoked": revoked, "grants": grants},
            request_id=request_id,
        )

    async def emit_error(
        self,
        message: str,
        *,
        code: str = "error",
        request_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        payload = {"message": message, "code": code}
        if details:
            payload.update(details)
        await self.emit(
            ServerEventType.ERROR,
            payload,
            request_id=request_id,
        )

    async def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        for pending in list(self._pending_permissions.values()):
            if not pending.future.done():
                pending.future.set_result("deny")
        self._pending_permissions.clear()
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
    lines = _start_stdin_line_reader(sys.stdin, loop)
    while not bridge._closed:
        line = await lines.get()
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
    engine_factory: EngineFactory | None = None,
) -> JsonlEngineBridge:
    resolved = resolve_config_path(config_path)
    config = AppConfig.from_yaml(resolved)
    setup_logging(config.log_level)
    if engine_factory is None:
        from naumi_agent.orchestrator.engine import AgentEngine

        engine_factory = AgentEngine
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
    _configure_stdio_utf8()
    asyncio.run(_amain(argv))


if __name__ == "__main__":
    main()
