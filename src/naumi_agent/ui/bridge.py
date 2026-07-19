"""JSONL bridge between the Python engine and next-generation terminal UI."""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import subprocess
import sys
import threading
from collections import deque
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO
from uuid import uuid4

from naumi_agent import __version__
from naumi_agent.agent_control import AgentControlSnapshot
from naumi_agent.clipboard import strip_ansi
from naumi_agent.config.paths import DEFAULT_CONFIG_PATH, resolve_config_path
from naumi_agent.config.settings import AppConfig
from naumi_agent.debug_trace import DebugTrace
from naumi_agent.harness.conversation_queue_runtime import (
    ConversationQueueClaim,
    ConversationQueueClaimError,
    DurableConversationQueueAuthority,
)
from naumi_agent.harness.eval_promotion_flow import run_eval_promotion_flow
from naumi_agent.harness.eval_surface import (
    HarnessEvalBaselineStatus,
    HarnessEvalBatchProgress,
    HarnessEvalPromotionFlowStatus,
    eval_batch_terminal_progress,
)
from naumi_agent.harness.explain import HarnessExplainLookup
from naumi_agent.harness.interaction import (
    HarnessInteractionRecord,
)
from naumi_agent.harness.interaction_runtime import (
    DurableInteractionAuthorityClient,
)
from naumi_agent.harness.replay_models import HarnessReplayLookup
from naumi_agent.harness.store import (
    HarnessConversationQueueItem,
    HarnessStore,
    HarnessStoreConflictError,
)
from naumi_agent.inspector import RuntimeInspectorSnapshot
from naumi_agent.log_setup import setup_logging
from naumi_agent.runs.models import CompletionReceipt
from naumi_agent.runtime.terminal_runtime import (
    TerminalRuntimeLifecycle,
    TerminalRuntimeLifecycleFactory,
    TerminalRuntimeState,
)
from naumi_agent.streaming.sinks import CallbackEventSink
from naumi_agent.tasks.models import TaskStatus
from naumi_agent.ui.harness_protocol import (
    harness_eval_baseline_payload,
    harness_eval_batch_payload,
    harness_eval_promotion_payload,
    harness_explain_payload,
    harness_replay_payload,
)
from naumi_agent.ui.messages import EngineEventAdapter, MessageType, SystemNoticeMessage
from naumi_agent.ui.permission_confirmation import summarize_arguments
from naumi_agent.ui.protocol import (
    PROTOCOL_CAPABILITIES,
    ClientEventType,
    ProtocolNegotiationError,
    ServerEventType,
    decode_jsonl_line,
    encode_jsonl,
    make_envelope,
    negotiate_hello,
    normalize_client_record,
    ui_message_payload,
)
from naumi_agent.ui.protocol_registry import load_protocol_event_registry
from naumi_agent.ui.runtime_health import (
    runtime_heartbeat_retention_status_payload,
)
from naumi_agent.user_interaction import (
    UserInteractionRequest,
    UserInteractionUnavailableError,
    normalize_interaction_request,
    normalize_interaction_response,
)
from naumi_agent.workbench.models import ParallelMode, RiskLevel
from naumi_agent.workbench.proposal_governance import (
    ProposalAction,
    ProposalGovernanceConflictError,
)

logger = logging.getLogger(__name__)

_TERMINAL_MISSION_STATUSES = frozenset({
    "completed",
    "cancelled",
    "canceled",
    "closed",
    "archived",
})
_SUPPORTED_PERMISSION_CHOICES = frozenset({"allow_once", "deny", "grant_session"})
_MAX_QUEUED_CONVERSATIONS = 20
_HARNESS_DETAIL_UNAVAILABLE = (
    "Harness 详情暂不可用。请确认当前工作区状态库可读，然后运行 `/harness doctor`。"
)

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


@dataclass(frozen=True)
class QueuedChatSubmission:
    """One Bridge-accepted chat turn waiting for serialized execution."""

    text: str
    request_id: str
    session_id: str = ""
    durable_item: HarnessConversationQueueItem | None = None


@dataclass
class PendingInteraction:
    """One model-initiated question waiting for an exact frontend response."""

    future: asyncio.Future[dict[str, str]]
    request: UserInteractionRequest
    public_payload: dict[str, Any]
    durable_record: HarnessInteractionRecord | None = None
    pursuit_resolve: (
        Callable[[str, dict[str, str]], Awaitable[None]] | None
    ) = None
    replay_only: bool = False
    timeout_task: asyncio.Task[None] | None = None
    owner_renew_task: asyncio.Task[None] | None = None


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


def _fallback_slash_command_registry() -> list[dict[str, Any]]:
    return [
        {"command": "/help", "aliases": ["/h"], "description": "显示帮助"},
        {"command": "/q", "description": "退出"},
        {"command": "/history", "description": "查看历史会话列表"},
        {"command": "/load", "aliases": ["/l"], "description": "加载会话并继续对话"},
        {"command": "/resume", "aliases": ["/r"], "description": "继续最近一次对话"},
        {
            "command": "/tasks",
            "description": "任务面板（筛选、搜索、键盘导航、详情与取消）",
        },
        {"command": "/task", "description": "查看任务运行详情"},
        {
            "command": "/goal",
            "description": "持久目标 — 跨轮次保持方向，可选启动 Pursuit",
        },
        {"command": "/permissions", "description": "显示待确认权限面板"},
        {"command": "/doctor", "description": "运行环境诊断"},
        {
            "command": "/harness",
            "description": "Harness 状态、重复评测、Baseline、运行解释、知识、检查与信任",
        },
        {
            "command": "/queue",
            "description": "审查并处置持久排队消息",
        },
        {
            "command": "/feedback",
            "description": "记录隐私安全的用户纠正或缺陷候选",
        },
        {
            "command": "/evolution",
            "description": "审查 Candidate 或加入 Workbench 队列",
        },
        {
            "command": "/mode",
            "aliases": ["/mode"],
            "description": "切换 runtime 模式 default / plan / bypass",
        },
        {
            "command": "/effort",
            "description": "查看或切换模型思考强度",
        },
        {"command": "/reasoning", "description": "显示/切换思考文本"},
        {"command": "/clear", "aliases": ["/c"], "description": "清空当前会话显示"},
        {"command": "/debug", "description": "显示前端与后端调试路径"},
        {"command": "/pwd", "description": "显示工作区与会话库路径"},
        {"command": "/tools", "description": "列出可用工具"},
        {"command": "/model", "aliases": ["/m"], "description": "查看当前模型配置"},
        {"command": "/models", "description": "列出 provider 可用模型"},
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
        self._client_capabilities = set(PROTOCOL_CAPABILITIES)
        self._writer: TextIO | None = None
        self._writer_lock = asyncio.Lock()
        self._protocol_event_registry = load_protocol_event_registry()
        self._run_task: asyncio.Task[Any] | None = None
        self._harness_eval_batch_tasks: dict[str, asyncio.Task[None]] = {}
        self._harness_eval_promotion_tasks: dict[str, asyncio.Task[None]] = {}
        self._queued_chat_submissions: deque[QueuedChatSubmission] = deque()
        self._queue_owner_id = f"queue-bridge-{uuid4().hex}"
        self._queue_authorities: dict[str, DurableConversationQueueAuthority] = {}
        self._active_queue_claim: ConversationQueueClaim | None = None
        self._active_queue_authority: DurableConversationQueueAuthority | None = None
        self._queue_claim_renew_task: asyncio.Task[None] | None = None
        self._queue_claim_lost = False
        self._deferred_queue_receipt_events: list[tuple[str, dict[str, Any]]] = []
        self._recovered_queue_sessions: set[str] = set()
        self._active_run_context: dict[str, str] = {}
        self._active_completion_receipt: CompletionReceipt | None = None
        self._inspector_subscribed = False
        self._inspector_snapshot: RuntimeInspectorSnapshot | None = None
        self._agents_subscribed = False
        self._agents_snapshot: AgentControlSnapshot | None = None
        self._cli_supported_commands = _load_cli_slash_commands_with_alias()
        self._pending_permissions: dict[str, PendingPermission] = {}
        self._pending_interactions: dict[str, PendingInteraction] = {}
        self._interaction_owner_id = f"bridge-{uuid4().hex}"
        self._interaction_authority_client: (
            DurableInteractionAuthorityClient | None
        ) = None
        self._interaction_authority_store: object | None = None
        self._interaction_replay_task: asyncio.Task[None] | None = None
        runtime_identity = f"terminal-ui-{uuid4().hex}"
        self._runtime_heartbeat_subject_id = runtime_identity
        self._runtime_heartbeat_instance_id = runtime_identity
        self._terminal_runtime_lifecycle: TerminalRuntimeLifecycle | None = None
        self._runtime_heartbeat_notice_emitted = False
        config = getattr(self.engine, "_config", None)
        ui_config = getattr(config, "ui", None)
        self._show_reasoning = bool(getattr(ui_config, "show_reasoning", False))
        self._last_retention_worker_status: dict[str, object] | None = None
        self._last_runtime_heartbeat_retention_status: dict[str, object] | None = None
        self._closed = False

        self.engine.set_permission_confirmer(self.confirm_permission)
        self.engine.set_user_interaction_handler(self.request_user_interaction)

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
        heartbeat_error = await self._start_terminal_runtime_lifecycle()
        payload = self.status_payload()
        retention_status = payload.get("retention_worker")
        if isinstance(retention_status, dict):
            self._last_retention_worker_status = dict(retention_status)
        runtime_retention_status = payload.get("runtime_heartbeat_retention")
        if isinstance(runtime_retention_status, dict):
            self._last_runtime_heartbeat_retention_status = dict(
                runtime_retention_status
            )
        await self.emit(ServerEventType.READY, payload)
        if heartbeat_error:
            await self._emit_runtime_heartbeat_degraded()
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
        await self._replay_durable_interactions()
        current_session_id = str(
            getattr(getattr(self.engine, "_session", None), "id", "") or ""
        )
        if current_session_id:
            await self._recover_durable_conversation_queue(current_session_id)

    def _terminal_runtime_service(self) -> TerminalRuntimeLifecycle | None:
        if self._terminal_runtime_lifecycle is not None:
            return self._terminal_runtime_lifecycle
        factory = getattr(
            self.engine,
            "terminal_runtime_lifecycle_factory",
            None,
        )
        if not isinstance(factory, TerminalRuntimeLifecycleFactory):
            return None
        self._terminal_runtime_lifecycle = factory.create(
            surface="new_ui",
            identity=self._runtime_heartbeat_subject_id,
            on_heartbeat_failure=self._runtime_heartbeat_failed,
        )
        return self._terminal_runtime_lifecycle

    async def _start_terminal_runtime_lifecycle(self) -> str:
        lifecycle = self._terminal_runtime_service()
        if lifecycle is None:
            return ""
        try:
            await lifecycle.start()
        except Exception as exc:
            logger.warning("Runtime heartbeat startup failed (%s)", type(exc).__name__)
            return "heartbeat_start_failed"
        return ""

    async def _runtime_heartbeat_failed(self, _code: str) -> None:
        if self._closed:
            return
        await self._emit_runtime_heartbeat_degraded()

    async def _emit_runtime_heartbeat_degraded(self) -> None:
        if self._runtime_heartbeat_notice_emitted or self._closed:
            return
        self._runtime_heartbeat_notice_emitted = True
        await self.emit(
            ServerEventType.UI_MESSAGE,
            ui_message_payload(
                SystemNoticeMessage(
                    type=MessageType.SYSTEM_NOTICE,
                    title="运行时心跳降级",
                    content=(
                        "持久心跳暂不可用；当前运行仍可继续，但离线诊断可能延迟。"
                        "请运行 /doctor 检查 Harness 状态库。"
                    ),
                    level="warning",
                )
            ),
        )

    def _interaction_authority(
        self,
    ) -> DurableInteractionAuthorityClient | None:
        harness_service = getattr(self.engine, "harness_service", None)
        store = getattr(harness_service, "store", None)
        if store is None:
            self._interaction_authority_client = None
            self._interaction_authority_store = None
            return None
        if (
            self._interaction_authority_client is None
            or self._interaction_authority_store is not store
        ):
            self._interaction_authority_client = DurableInteractionAuthorityClient(
                store=store,
                workspace_root=self.engine.workspace_root,
                owner_id=self._interaction_owner_id,
            )
            self._interaction_authority_store = store
        return self._interaction_authority_client

    def _conversation_queue_authority(
        self,
        session_id: str,
    ) -> DurableConversationQueueAuthority | None:
        """Return the current runtime's durable queue authority when available."""
        normalized_session_id = session_id.strip()
        if not normalized_session_id:
            return None
        harness_service = getattr(self.engine, "harness_service", None)
        store = getattr(harness_service, "store", None)
        if not isinstance(store, HarnessStore):
            return None
        existing = self._queue_authorities.get(normalized_session_id)
        if existing is not None and existing.store is store:
            return existing
        authority = DurableConversationQueueAuthority(
            store=store,
            workspace_root=self.engine.workspace_root,
            session_id=normalized_session_id,
            owner_id=self._queue_owner_id,
        )
        self._queue_authorities[normalized_session_id] = authority
        return authority

    async def _ensure_chat_session_id(self, text: str) -> str:
        session = getattr(self.engine, "_session", None)
        session_id = str(getattr(session, "id", "") or "")
        if session_id:
            return session_id
        get_or_create = getattr(self.engine, "get_or_create_session", None)
        if not callable(get_or_create):
            return ""
        session = await get_or_create(title=_task_title(text))
        return str(getattr(session, "id", "") or "")

    async def _replay_durable_interactions(self) -> None:
        """Take over expired UI owners and replay recoverable pending questions."""
        authority = self._interaction_authority()
        if authority is None:
            return
        now = datetime.now(UTC)
        retry_after_seconds: float | None = None
        try:
            recovery = await authority.recover_pending(
                now=now.isoformat(),
                limit=50,
            )
            retry_after_seconds = recovery.retry_after_seconds
            for record in recovery.claimed:
                if record.interaction_id in self._pending_interactions:
                    continue
                request = record.request()
                future: asyncio.Future[dict[str, str]] = (
                    asyncio.get_running_loop().create_future()
                )
                public_payload = {
                    "request_id": record.interaction_id,
                    "session_id": record.session_id,
                    "run_id": record.subject_id if record.subject_kind == "pursuit" else "",
                    "agent_name": record.agent_name,
                    **request.to_public_dict(),
                    "expires_at": record.expires_at,
                    "status": "needs_input",
                }
                self._pending_interactions[record.interaction_id] = PendingInteraction(
                    future=future,
                    request=request,
                    public_payload=public_payload,
                    durable_record=record,
                    replay_only=True,
                )
                await self.emit(
                    ServerEventType.INTERACTION_REQUEST,
                    public_payload,
                    request_id=record.interaction_id,
                )
                self._schedule_pending_interaction_owner_renewal(
                    record.interaction_id
                )
                self._schedule_pending_interaction_timeout(record.interaction_id)
        except Exception as exc:
            logger.warning(
                "Durable interaction replay failed (%s)", type(exc).__name__,
            )
            retry_after_seconds = min(retry_after_seconds or 0.5, 0.5)
        finally:
            if retry_after_seconds is not None and not self._closed:
                self._schedule_interaction_replay(retry_after_seconds)

    def _schedule_interaction_replay(self, delay_seconds: float) -> None:
        """Recheck a live foreign owner without stealing its valid lease."""
        current = self._interaction_replay_task
        if current is not None and not current.done():
            return
        delay = max(0.05, delay_seconds + 0.05)

        async def replay_after_lease() -> None:
            try:
                await asyncio.sleep(delay)
                if self._interaction_replay_task is asyncio.current_task():
                    self._interaction_replay_task = None
                if not self._closed:
                    await self._replay_durable_interactions()
            except asyncio.CancelledError:
                raise

        self._interaction_replay_task = asyncio.create_task(
            replay_after_lease(),
            name="naumi-interaction-replay",
        )

    def _schedule_pending_interaction_timeout(self, interaction_id: str) -> None:
        pending = self._pending_interactions.get(interaction_id)
        durable = pending.durable_record if pending is not None else None
        if pending is None or durable is None or not durable.expires_at:
            return
        if pending.timeout_task is not None and not pending.timeout_task.done():
            return
        remaining = max(
            0.0,
            (
                datetime.fromisoformat(durable.expires_at) - datetime.now(UTC)
            ).total_seconds(),
        )

        async def expire_at_deadline() -> None:
            try:
                await asyncio.sleep(remaining)
                await self._commit_pending_interaction_expiry(
                    interaction_id,
                    now=datetime.now(UTC).isoformat(),
                )
            except asyncio.CancelledError:
                raise
            finally:
                current = self._pending_interactions.get(interaction_id)
                if (
                    current is not None
                    and current.timeout_task is asyncio.current_task()
                ):
                    current.timeout_task = None

        pending.timeout_task = asyncio.create_task(
            expire_at_deadline(),
            name=f"naumi-interaction-timeout-{interaction_id}",
        )

    def _schedule_pending_interaction_owner_renewal(
        self,
        interaction_id: str,
    ) -> None:
        pending = self._pending_interactions.get(interaction_id)
        authority = self._interaction_authority()
        if pending is None or pending.durable_record is None or authority is None:
            return
        if pending.owner_renew_task is not None and not pending.owner_renew_task.done():
            return

        async def keep_owner_live() -> None:
            failures = 0
            try:
                while not self._closed:
                    await asyncio.sleep(authority.owner_renew_interval_seconds)
                    current = self._pending_interactions.get(interaction_id)
                    if current is not pending or current.future.done():
                        return
                    try:
                        pending.durable_record = await authority.renew(
                            record=pending.durable_record,
                        )
                        failures = 0
                    except Exception:
                        failures += 1
                        try:
                            latest = await authority.store.get_interaction(
                                workspace_root=self.engine.workspace_root,
                                interaction_id=interaction_id,
                            )
                        except Exception:
                            latest = None
                        if (
                            latest is not None
                            and latest.state == "pending"
                            and latest.owner_id == authority.owner_id
                        ):
                            pending.durable_record = latest
                        elif latest is not None or failures >= 3:
                            return
                        await asyncio.sleep(float(min(failures, 3)))
            except asyncio.CancelledError:
                raise
            finally:
                if pending.owner_renew_task is asyncio.current_task():
                    pending.owner_renew_task = None

        pending.owner_renew_task = asyncio.create_task(
            keep_owner_live(),
            name=f"naumi-interaction-owner-{interaction_id}",
        )

    async def _stop_pending_interaction_owner_renewal(
        self,
        pending: PendingInteraction,
    ) -> None:
        owner_task = pending.owner_renew_task
        if owner_task is None or owner_task is asyncio.current_task():
            return
        owner_task.cancel()
        await asyncio.gather(owner_task, return_exceptions=True)
        pending.owner_renew_task = None

    async def _commit_pending_interaction_expiry(
        self,
        interaction_id: str,
        *,
        now: str,
    ) -> None:
        """Commit one live timeout and close the exact pending UI card."""
        pending = self._pending_interactions.get(interaction_id)
        durable = pending.durable_record if pending is not None else None
        if pending is None or durable is None or durable.state != "pending":
            return
        await self._stop_pending_interaction_owner_renewal(pending)
        durable = pending.durable_record
        if durable is None or durable.state != "pending":
            return
        authority = self._interaction_authority()
        if authority is None:
            return
        try:
            expired = await authority.expire(
                record=durable,
                now=now,
            )
        except Exception as exc:
            try:
                current = await authority.store.get_interaction(
                    workspace_root=self.engine.workspace_root,
                    interaction_id=interaction_id,
                )
            except Exception:
                current = None
            if current is not None and current.state != "pending":
                return
            logger.warning(
                "Durable interaction timeout failed (%s)",
                type(exc).__name__,
            )
            return
        pending.durable_record = expired
        timeout_task = pending.timeout_task
        if timeout_task is not None and timeout_task is not asyncio.current_task():
            timeout_task.cancel()
            await asyncio.gather(timeout_task, return_exceptions=True)
            pending.timeout_task = None
        if not pending.future.done():
            if pending.replay_only:
                pending.future.cancel()
            else:
                pending.future.set_exception(
                    UserInteractionUnavailableError("用户交互等待已超时")
                )
        await self.emit(
            ServerEventType.INTERACTION_RESOLVED,
            {
                "request_id": interaction_id,
                "status": "expired",
                "reason": "等待用户回答超时。",
            },
            request_id=interaction_id,
        )
        if self._pending_interactions.get(interaction_id) is pending:
            self._pending_interactions.pop(interaction_id, None)

    def status_payload(self, *, include_slash_commands: bool = True) -> dict[str, Any]:
        """Build the footer/status payload consumed by the terminal UI."""
        usage = self.engine.usage
        try:
            model = self.engine.router.resolve_model("capable")
        except Exception:
            model = ""
        provider = ""
        api_format = ""
        upstream_model = ""
        if model:
            try:
                identity = self.engine.router.get_runtime_identity(model)
                provider = identity.provider
                api_format = identity.api_format
                upstream_model = identity.upstream_model
            except Exception:
                pass
        reasoning_effort = {
            "model": model,
            "effective": "auto",
            "source": "auto",
            "supported": [],
            "default": None,
            "warning": None,
        }
        try:
            reasoning_effort = self.engine.router.get_reasoning_effort_status(
                model or None
            ).to_dict()
        except Exception:
            pass
        model_contract: dict[str, Any] | None = None
        try:
            model_contract = self.engine.router.get_model_capability_contract(
                model or None
            ).to_dict()
        except Exception:
            pass
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
            "version": __version__,
            "protocol_registry": {
                "contract_version": self._protocol_event_registry.contract_version,
                "registry_sha256": self._protocol_event_registry.registry_sha256,
                "client_event_count": len(self._protocol_event_registry.client),
                "server_event_count": len(self._protocol_event_registry.server),
            },
            "mode": str(getattr(self.engine.runtime_mode, "value", self.engine.runtime_mode)),
            "permission_mode": str(
                getattr(self.engine.permission_mode, "value", self.engine.permission_mode)
            ),
            "session_id": str(getattr(getattr(self.engine, "_session", None), "id", "")),
            "model": model,
            "provider": provider,
            "api_format": api_format,
            "upstream_model": upstream_model,
            "reasoning_effort": reasoning_effort,
            "model_contract": model_contract,
            "workspace_root": str(workspace_root),
            "usage": {
                "input_tokens": usage.total_input_tokens,
                "output_tokens": usage.total_output_tokens,
                "turns": usage.turns,
                "total_tokens": usage.total_input_tokens + usage.total_output_tokens,
            },
            "context": context,
            "budget": budget,
            "retention_worker": self._retention_worker_status_payload(),
            "runtime_heartbeat_retention": (
                self._runtime_heartbeat_retention_status_payload()
            ),
            "evolution_patch_recovery": self._evolution_patch_recovery_payload(),
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

    def _evolution_patch_recovery_payload(self) -> dict[str, object]:
        getter = getattr(self.engine, "evolution_patch_recovery_status", None)
        if not callable(getter):
            return {
                "total": 0,
                "completed": 0,
                "rolled_back": 0,
                "already_baseline": 0,
                "orphan_lock_removed": 0,
                "deferred": 0,
                "failed": 0,
                "filesystem_changed": 0,
                "failure_codes": [],
            }
        try:
            return dict(getter())
        except Exception:
            return {
                "total": 0,
                "completed": 0,
                "rolled_back": 0,
                "already_baseline": 0,
                "orphan_lock_removed": 0,
                "deferred": 0,
                "failed": 1,
                "filesystem_changed": 0,
                "failure_codes": ["status_unavailable"],
            }

    def _retention_worker_status_payload(self) -> dict[str, object]:
        try:
            return self.engine.session_retention_worker_status()
        except Exception:
            return {
                "configured_enabled": False,
                "owner_id": "",
                "state": "stopped",
                "lease_held": False,
                "pass_count": 0,
                "completed_session_count": 0,
                "retry_scheduled_count": 0,
                "failure_count": 1,
                "consecutive_empty_passes": 0,
                "next_delay_seconds": 0.0,
                "last_pass_status": "",
                "last_error_code": "status_unavailable",
                "started_at": "",
                "last_pass_at": "",
            }

    def _runtime_heartbeat_retention_status_payload(self) -> dict[str, object]:
        config = getattr(self.engine, "_config", None)
        retention_config = getattr(
            getattr(config, "harness", None),
            "runtime_heartbeat_retention",
            None,
        )
        lifecycle = self._terminal_runtime_lifecycle
        lifecycle_snapshot = lifecycle.snapshot() if lifecycle is not None else None
        retention = (
            lifecycle_snapshot.retention
            if lifecycle_snapshot is not None
            else None
        )
        factory = getattr(
            self.engine,
            "terminal_runtime_lifecycle_factory",
            None,
        )
        configured_enabled = (
            factory.retention_config.enabled
            if isinstance(factory, TerminalRuntimeLifecycleFactory)
            else bool(getattr(retention_config, "enabled", False))
        )
        return runtime_heartbeat_retention_status_payload(
            configured_enabled=configured_enabled,
            available=isinstance(factory, TerminalRuntimeLifecycleFactory),
            snapshot=retention,
        )

    def _task_activity_payload(self) -> dict[str, int]:
        """Return compact task/activity counts for persistent footer rendering."""
        payload = {
            "background_running": 0,
            "background_attention": 0,
            "subagents_active": 0,
            "browser_active": 0,
            "permissions_pending": len(self._pending_permissions),
            "interactions_pending": len(self._pending_interactions),
            "queued_conversations": len(self._queued_chat_submissions),
        }

        try:
            runner = getattr(self.engine, "background_runner", None)
            if runner is not None:
                for task in runner.list_tasks():
                    raw_status = getattr(task, "status", "")
                    status = str(getattr(raw_status, "value", raw_status))
                    if status in {"preparing", "running"}:
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
            try:
                negotiation = negotiate_hello(payload)
            except ProtocolNegotiationError as exc:
                await self.emit_error(
                    str(exc),
                    code=exc.code,
                    request_id=request_id,
                )
                return
            self._client_capabilities = set(negotiation.get("capabilities", ()))
            await self.emit(
                ServerEventType.ACK,
                {"event": event_type, "negotiation": negotiation},
                request_id=request_id,
            )
            await self.emit(
                ServerEventType.STATUS,
                self.status_payload(include_slash_commands=False),
            )
            return

        if event_type == ClientEventType.PING:
            await self.emit(
                ServerEventType.PONG,
                {
                    "ok": True,
                    "active_run": bool(
                        self._run_task is not None and not self._run_task.done()
                    ),
                    "queued_conversations": len(self._queued_chat_submissions),
                },
                request_id=request_id,
            )
            status_changes: dict[str, object] = {}
            retention_status = self._retention_worker_status_payload()
            if retention_status != self._last_retention_worker_status:
                self._last_retention_worker_status = dict(retention_status)
                status_changes["retention_worker"] = retention_status
            runtime_retention_status = (
                self._runtime_heartbeat_retention_status_payload()
            )
            if (
                runtime_retention_status
                != self._last_runtime_heartbeat_retention_status
            ):
                self._last_runtime_heartbeat_retention_status = dict(
                    runtime_retention_status
                )
                status_changes["runtime_heartbeat_retention"] = (
                    runtime_retention_status
                )
            if status_changes:
                await self.emit(
                    ServerEventType.STATUS,
                    status_changes,
                )
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

        if event_type == ClientEventType.INTERACTION_RESPONSE:
            await self.resolve_user_interaction(payload, request_id=request_id)
            return

        if event_type == ClientEventType.INTERACTION_CANCEL:
            await self.cancel_user_interaction(payload, request_id=request_id)
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
        if event_type == ClientEventType.QUEUE_PROMOTE:
            await self.promote_queued_chat(payload, request_id=request_id)
            return
        if event_type == ClientEventType.QUEUE_CANCEL:
            await self.cancel_queued_chat(payload, request_id=request_id)
            return
        if event_type == ClientEventType.RECEIPT_REQUEST:
            await self.resend_completion_receipt(payload, request_id=request_id)
            return
        if event_type == ClientEventType.HARNESS_EXPLAIN_REQUEST:
            await self.query_harness_explain(payload, request_id=request_id)
            return
        if event_type == ClientEventType.HARNESS_REPLAY_REQUEST:
            await self.query_harness_replay(payload, request_id=request_id)
            return
        if event_type == ClientEventType.HARNESS_EVAL_BASELINE_REQUEST:
            await self.query_harness_eval_baseline(payload, request_id=request_id)
            return
        if event_type == ClientEventType.HARNESS_EVAL_BATCH_REQUEST:
            await self.start_harness_eval_batch(payload, request_id=request_id)
            return
        if event_type == ClientEventType.HARNESS_EVAL_PROMOTION_REQUEST:
            await self.start_harness_eval_promotion(payload, request_id=request_id)
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
        if event_type == ClientEventType.WORKBENCH_REQUEST:
            await self.show_workbench(payload, request_id=request_id)
            return
        if event_type == ClientEventType.WORKBENCH_REVIEW_REQUEST:
            await self.show_workbench_review(payload, request_id=request_id)
            return
        if event_type == ClientEventType.WORKBENCH_PROPOSAL_ACTION:
            await self.govern_workbench_proposal(payload, request_id=request_id)
            return
        if event_type == ClientEventType.EVOLUTION_REVIEW_REQUEST:
            await self.show_evolution_review(payload, request_id=request_id)
            return

        if event_type == ClientEventType.RESUME:
            await self.resume_session(payload, request_id=request_id)
            return

        if event_type == ClientEventType.GOAL_PANEL:
            await self.show_goal_panel(payload, request_id=request_id)
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
        session_id = await self._ensure_chat_session_id(text)
        submission = QueuedChatSubmission(
            text=text,
            request_id=request_id,
            session_id=session_id,
        )
        if self._run_task is not None and not self._run_task.done():
            authority = self._conversation_queue_authority(session_id)
            if authority is not None:
                try:
                    durable_item = await authority.enqueue(
                        request_id=request_id,
                        text=text,
                        client_id=self._queue_owner_id,
                    )
                except HarnessStoreConflictError as exc:
                    code = "queue_full" if "20 条上限" in str(exc) else "queue_conflict"
                    await self.emit_error(str(exc), code=code, request_id=request_id)
                    return
                except Exception as exc:
                    logger.warning(
                        "Durable conversation enqueue failed (%s)",
                        type(exc).__name__,
                    )
                    await self.emit_error(
                        "排队消息未能安全保存，请运行 /doctor 后重试。",
                        code="queue_persist_failed",
                        request_id=request_id,
                    )
                    return
                submission = QueuedChatSubmission(
                    text=text,
                    request_id=request_id,
                    session_id=session_id,
                    durable_item=durable_item,
                )
            elif len(self._queued_chat_submissions) >= _MAX_QUEUED_CONVERSATIONS:
                await self.emit_error(
                    f"对话队列已满（最多 {_MAX_QUEUED_CONVERSATIONS} 条），请稍后再发送。",
                    code="queue_full",
                    request_id=request_id,
                )
                return
            self._queued_chat_submissions.append(submission)
            position = len(self._queued_chat_submissions)
            await self.emit(
                ServerEventType.USER_MESSAGE,
                {"content": text},
                request_id=request_id,
            )
            await self.emit(
                ServerEventType.RUN_QUEUED,
                {"task": text, "position": position, "queued": position},
                request_id=request_id,
            )
            await self.emit(ServerEventType.STATUS, self.status_payload())
            return

        await self._start_chat_submission(submission, emit_user_message=True)

    async def _start_chat_submission(
        self,
        submission: QueuedChatSubmission,
        *,
        emit_user_message: bool,
        queue_authority: DurableConversationQueueAuthority | None = None,
        queue_claim: ConversationQueueClaim | None = None,
    ) -> None:
        """Start exactly one chat submission on the shared AgentEngine."""
        text = submission.text
        request_id = submission.request_id
        if emit_user_message:
            await self.emit(ServerEventType.USER_MESSAGE, {"content": text}, request_id=request_id)
        await self.emit(ServerEventType.RUN_STARTED, {"task": text}, request_id=request_id)
        await self.emit(ServerEventType.STATUS, self.status_payload())
        self._active_completion_receipt = None
        self._active_queue_authority = queue_authority
        self._active_queue_claim = queue_claim
        self._queue_claim_lost = False
        self._deferred_queue_receipt_events = []
        if queue_authority is not None and queue_claim is not None:
            self._start_queue_claim_renewal(queue_authority, queue_claim)

        async def run() -> None:
            was_cancelled = False
            terminal_state = "completed"
            terminal_reason = "run_completed"
            queue_commit_ok = True
            deferred_completion_payload: dict[str, Any] | None = None
            try:
                result = await self.engine.run_streaming(
                    text,
                    CallbackEventSink(self.handle_engine_event),
                )
                completion_payload = {
                    "status": result.status,
                    "response": result.response or "",
                    "error": result.error or "",
                    **_receipt_reference(
                        getattr(result, "receipt", None)
                        or self._active_completion_receipt
                    ),
                }
                if queue_claim is None:
                    await self.emit(
                        ServerEventType.RUN_COMPLETED,
                        completion_payload,
                        request_id=request_id,
                    )
                else:
                    deferred_completion_payload = completion_payload
                if result.status not in {"completed", "success"}:
                    terminal_state = "failed"
                    terminal_reason = f"run_{result.status or 'failed'}"
            except asyncio.CancelledError:
                was_cancelled = True
                terminal_state = "cancelled"
                terminal_reason = "run_cancelled"
                raise
            except Exception as exc:
                terminal_state = "failed"
                terminal_reason = "run_failed"
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
                completion_payload = {
                    "status": "failed",
                    "response": "",
                    "error": message,
                }
                if queue_claim is None:
                    await self.emit(
                        ServerEventType.RUN_COMPLETED,
                        completion_payload,
                        request_id=request_id,
                    )
                else:
                    deferred_completion_payload = completion_payload
            finally:
                await self._stop_queue_claim_renewal()
                if queue_authority is not None and queue_claim is not None:
                    if self._queue_claim_lost:
                        queue_commit_ok = False
                    else:
                        try:
                            await queue_authority.finish(
                                queue_claim,
                                state=terminal_state,
                                terminal_reason=terminal_reason,
                            )
                        except Exception as exc:
                            queue_commit_ok = False
                            logger.warning(
                                "Durable conversation terminal commit failed (%s)",
                                type(exc).__name__,
                            )
                            if not self._closed:
                                await self.emit_error(
                                    "排队消息运行结果未通过持久 claim 校验；"
                                    "队列已停止，请恢复会话后核对。",
                                    code="queue_commit_failed",
                                    request_id=request_id,
                                )
                self._active_queue_authority = None
                self._active_queue_claim = None
                if queue_commit_ok and deferred_completion_payload is not None:
                    for event, data in self._deferred_queue_receipt_events:
                        await self._publish_engine_event(event, data)
                    await self.emit(
                        ServerEventType.RUN_COMPLETED,
                        deferred_completion_payload,
                        request_id=request_id,
                    )
                self._deferred_queue_receipt_events = []
                if self._active_run_context.get("request_id") == request_id:
                    self._active_run_context = {}
                if not self._closed and not was_cancelled and queue_commit_ok:
                    await self._start_next_queued_chat()
                    await self.emit(ServerEventType.STATUS, self.status_payload())

        self._active_run_context = {
            "request_id": request_id,
            "intent": "chat",
        }
        self._run_task = asyncio.create_task(run())

    def _start_queue_claim_renewal(
        self,
        authority: DurableConversationQueueAuthority,
        claim: ConversationQueueClaim,
    ) -> None:
        """Keep the current queue dispatch epoch live until terminal commit."""
        if self._queue_claim_renew_task is not None:
            self._queue_claim_renew_task.cancel()

        async def keepalive() -> None:
            current = claim
            try:
                while not self._closed:
                    await asyncio.sleep(max(1.0, authority.lease_seconds / 3))
                    current = await authority.renew(current)
                    self._active_queue_claim = current
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._queue_claim_lost = True
                logger.warning(
                    "Durable conversation claim renewal failed (%s)",
                    type(exc).__name__,
                )
                if not self._closed:
                    await self.emit_error(
                        "排队消息 claim 续租失败，已停止当前运行以避免重复提交。",
                        code="queue_claim_lost",
                        request_id=claim.item.request_id,
                    )
                run_task = self._run_task
                if run_task is not None and not run_task.done():
                    run_task.cancel()

        self._queue_claim_renew_task = asyncio.create_task(
            keepalive(),
            name=f"naumi-queue-claim-{claim.item.request_id}",
        )

    async def _stop_queue_claim_renewal(self) -> None:
        task = self._queue_claim_renew_task
        self._queue_claim_renew_task = None
        if task is None or task is asyncio.current_task():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _start_next_queued_chat(self) -> bool:
        """Advance the FIFO queue if the Bridge is open and no run is active."""
        if self._closed or not self._queued_chat_submissions:
            return False
        current = asyncio.current_task()
        if (
            self._run_task is not None
            and self._run_task is not current
            and not self._run_task.done()
        ):
            return False
        submission = self._queued_chat_submissions[0]
        authority = self._conversation_queue_authority(submission.session_id)
        claim: ConversationQueueClaim | None = None
        if submission.durable_item is not None:
            if authority is None:
                await self.emit_error(
                    "持久队列权威暂不可用，已停止派发。",
                    code="queue_authority_unavailable",
                    request_id=submission.request_id,
                )
                return False
            try:
                claim = await authority.claim(submission.durable_item)
            except ConversationQueueClaimError as exc:
                await self.emit_error(
                    f"{exc} 请恢复会话并核对该消息。",
                    code="queue_recovery_required",
                    request_id=submission.request_id,
                )
                return False
        self._queued_chat_submissions.popleft()
        await self._emit_queued_chat_positions()
        await self._start_chat_submission(
            submission,
            emit_user_message=False,
            queue_authority=authority if claim is not None else None,
            queue_claim=claim,
        )
        return True

    async def _emit_queued_chat_positions(self) -> None:
        """Refresh visible positions after the queue head advances."""
        queued = len(self._queued_chat_submissions)
        for position, submission in enumerate(self._queued_chat_submissions, start=1):
            await self.emit(
                ServerEventType.RUN_QUEUED,
                {"task": submission.text, "position": position, "queued": queued},
                request_id=submission.request_id,
            )

    async def promote_queued_chat(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Promote one queued chat to the next safe run boundary."""
        target_request_id = str(payload.get("target_request_id") or "")
        selected = next(
            (
                submission
                for submission in self._queued_chat_submissions
                if submission.request_id == target_request_id
            ),
            None,
        )
        if selected is None:
            await self.emit_error(
                "未找到可立即发送的排队消息；它可能已经开始、完成或被取消。",
                code="queue_item_not_found",
                request_id=request_id,
            )
            return

        if selected.durable_item is not None:
            authority = self._conversation_queue_authority(selected.session_id)
            if authority is None:
                await self.emit_error(
                    "持久队列权威暂不可用，不能安全重排。",
                    code="queue_authority_unavailable",
                    request_id=request_id,
                )
                return
            try:
                await authority.promote(
                    request_id=target_request_id,
                    active_claim=self._active_queue_claim,
                )
            except (ConversationQueueClaimError, HarnessStoreConflictError) as exc:
                await self.emit_error(
                    str(exc),
                    code="queue_recovery_required",
                    request_id=request_id,
                )
                return
            except Exception as exc:
                logger.warning(
                    "Durable conversation promotion failed (%s)",
                    type(exc).__name__,
                )
                await self.emit_error(
                    "排队消息未能安全重排，请运行 /doctor 后重试。",
                    code="queue_persist_failed",
                    request_id=request_id,
                )
                return
        self._queued_chat_submissions.remove(selected)
        self._queued_chat_submissions.appendleft(selected)
        await self._emit_queued_chat_positions()
        await self.emit(
            ServerEventType.RUN_QUEUE_PROMOTED,
            {
                "target_request_id": target_request_id,
                "position": 1,
                "queued": len(self._queued_chat_submissions),
                "boundary": "after_current_run",
                "message": "已提升，将在当前运行结束后的下一安全边界执行。",
            },
            request_id=request_id,
        )
        await self.emit(ServerEventType.STATUS, self.status_payload())

    async def cancel_queued_chat(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Cancel one queued chat only while it remains before dispatch."""
        target_request_id = str(payload.get("target_request_id") or "")
        selected = next(
            (
                submission
                for submission in self._queued_chat_submissions
                if submission.request_id == target_request_id
            ),
            None,
        )
        if selected is None:
            await self.emit_error(
                "未找到可取消的排队消息；它可能已经开始、完成或被取消。",
                code="queue_item_not_found",
                request_id=request_id,
            )
            return
        if selected.durable_item is not None:
            authority = self._conversation_queue_authority(selected.session_id)
            if authority is None:
                await self.emit_error(
                    "持久队列权威暂不可用，不能安全取消。",
                    code="queue_authority_unavailable",
                    request_id=request_id,
                )
                return
            try:
                await authority.cancel_unclaimed_request(
                    request_id=target_request_id,
                )
            except (ConversationQueueClaimError, HarnessStoreConflictError) as exc:
                await self.emit_error(
                    str(exc),
                    code="queue_cancel_rejected",
                    request_id=request_id,
                )
                return
            except Exception as exc:
                logger.warning(
                    "Durable conversation cancellation failed (%s)",
                    type(exc).__name__,
                )
                await self.emit_error(
                    "排队消息未能安全取消，请运行 /doctor 后重试。",
                    code="queue_persist_failed",
                    request_id=request_id,
                )
                return
        self._queued_chat_submissions.remove(selected)
        await self._emit_queued_chat_positions()
        await self.emit(
            ServerEventType.RUN_QUEUE_CANCELLED,
            {
                "target_request_id": target_request_id,
                "queued": len(self._queued_chat_submissions),
                "reason": "用户在派发前取消了该消息。",
            },
            request_id=request_id,
        )
        await self.emit(ServerEventType.STATUS, self.status_payload())

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
            was_cancelled = False
            try:
                result = await self.engine.run_streaming(
                    text,
                    CallbackEventSink(self.handle_engine_event),
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
                was_cancelled = True
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
                if not self._closed and not was_cancelled:
                    await self._start_next_queued_chat()
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
        if not self._queue_claim_lost:
            await self._start_next_queued_chat()
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

    async def query_harness_explain(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Return one durable, workspace-scoped Harness explanation."""
        run_id = str(payload["run_id"])
        service = getattr(self.engine, "harness_service", None)
        if service is None:
            lookup = HarnessExplainLookup(
                status="unavailable",
                message=_HARNESS_DETAIL_UNAVAILABLE,
            )
        else:
            try:
                lookup = await service.explain_run(run_id)
            except Exception as exc:
                self._trace_harness_lookup_failure("explain", exc)
                lookup = HarnessExplainLookup(
                    status="unavailable",
                    message=_HARNESS_DETAIL_UNAVAILABLE,
                )
        try:
            response = harness_explain_payload(run_id, lookup)
        except Exception as exc:
            self._trace_harness_lookup_failure("explain_payload", exc)
            response = harness_explain_payload(
                run_id,
                HarnessExplainLookup(
                    status="unavailable",
                    message=_HARNESS_DETAIL_UNAVAILABLE,
                ),
            )
        await self.emit(
            ServerEventType.HARNESS_EXPLAIN,
            response,
            request_id=request_id,
        )

    async def query_harness_replay(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Return one deterministic Harness replay without executing the run."""
        run_id = str(payload["run_id"])
        service = getattr(self.engine, "harness_service", None)
        if service is None:
            lookup = HarnessReplayLookup(
                status="unavailable",
                message=_HARNESS_DETAIL_UNAVAILABLE,
            )
        else:
            try:
                lookup = await service.replay_run(run_id)
            except Exception as exc:
                self._trace_harness_lookup_failure("replay", exc)
                lookup = HarnessReplayLookup(
                    status="unavailable",
                    message=_HARNESS_DETAIL_UNAVAILABLE,
                )
        try:
            response = harness_replay_payload(run_id, lookup)
        except Exception as exc:
            self._trace_harness_lookup_failure("replay_payload", exc)
            response = harness_replay_payload(
                run_id,
                HarnessReplayLookup(
                    status="unavailable",
                    message=_HARNESS_DETAIL_UNAVAILABLE,
                ),
            )
        await self.emit(
            ServerEventType.HARNESS_REPLAY,
            response,
            request_id=request_id,
        )

    async def query_harness_eval_baseline(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Return one authoritative, workspace-scoped Eval Baseline snapshot."""
        suite_id = str(payload["suite_id"])
        service = getattr(self.engine, "harness_service", None)
        if service is None:
            status = HarnessEvalBaselineStatus(
                status="unavailable",
                suite_id=suite_id,
                message="Harness 状态库尚未初始化。",
            )
        else:
            try:
                status = await service.eval_baseline_status(suite_id)
            except Exception as exc:
                self._trace_harness_lookup_failure("eval_baseline", exc)
                status = HarnessEvalBaselineStatus(
                    status="unavailable",
                    suite_id=suite_id,
                    message="Harness Eval 状态库损坏、不可读或正忙。",
                )
        try:
            response = harness_eval_baseline_payload(status)
        except Exception as exc:
            self._trace_harness_lookup_failure("eval_baseline_payload", exc)
            response = harness_eval_baseline_payload(
                HarnessEvalBaselineStatus(
                    status="unavailable",
                    suite_id=suite_id,
                    message="Harness Eval 状态暂不可用。",
                )
            )
        await self.emit(
            ServerEventType.HARNESS_EVAL_BASELINE,
            response,
            request_id=request_id,
        )

    async def start_harness_eval_batch(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Start one non-blocking repeated Eval and stream factual progress."""
        if len(self._harness_eval_batch_tasks) >= 4:
            await self.emit_error(
                "并行 Eval Batch 已达上限（4 个），请等待任一 Batch 完成。",
                code="harness_eval_batch_limit",
                request_id=request_id,
            )
            return
        service = getattr(self.engine, "harness_service", None)
        if service is None:
            await self.emit(
                ServerEventType.HARNESS_EVAL_BATCH,
                harness_eval_batch_payload(
                    HarnessEvalBatchProgress(
                        stage="error",
                        batch_id=str(payload.get("batch_id") or "unassigned"),
                        suite_id=str(payload["suite_id"]),
                        requested=int(payload["repetitions"]),
                        completed=0,
                        persisted=0,
                        code="service_unavailable",
                        message="Harness Service 尚未初始化。",
                    )
                ),
                request_id=request_id,
            )
            return

        async def emit_progress(progress: HarnessEvalBatchProgress) -> None:
            await self.emit(
                ServerEventType.HARNESS_EVAL_BATCH,
                harness_eval_batch_payload(progress),
                request_id=request_id,
            )

        async def run() -> None:
            try:
                result = await service.eval_repetition_batch(
                    str(payload["suite_id"]),
                    repetitions=int(payload["repetitions"]),
                    batch_id=str(payload.get("batch_id") or "") or None,
                    on_progress=emit_progress,
                )
                await emit_progress(eval_batch_terminal_progress(result))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._trace_harness_lookup_failure("eval_batch", exc)
                await emit_progress(
                    HarnessEvalBatchProgress(
                        stage="error",
                        batch_id=str(payload.get("batch_id") or "unassigned"),
                        suite_id=str(payload["suite_id"]),
                        requested=int(payload["repetitions"]),
                        completed=0,
                        persisted=0,
                        code="batch_failed",
                        message="Eval Batch 执行失败；请运行 /harness doctor 后重试。",
                    )
                )
            finally:
                self._harness_eval_batch_tasks.pop(request_id, None)

        task = asyncio.create_task(run())
        self._harness_eval_batch_tasks[request_id] = task

    async def start_harness_eval_promotion(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Run one guided promotion without blocking the JSONL control plane."""
        if request_id in self._harness_eval_promotion_tasks:
            await self.emit_error(
                "该 Baseline 晋升请求正在处理中。",
                code="harness_eval_promotion_duplicate",
                request_id=request_id,
            )
            return
        if len(self._harness_eval_promotion_tasks) >= 4:
            await self.emit_error(
                "待处理 Baseline 晋升交互已达上限（4 个）。",
                code="harness_eval_promotion_limit",
                request_id=request_id,
            )
            return
        service = getattr(self.engine, "harness_service", None)

        async def emit_status(status: HarnessEvalPromotionFlowStatus) -> None:
            await self.emit(
                ServerEventType.HARNESS_EVAL_PROMOTION,
                harness_eval_promotion_payload(status),
                request_id=request_id,
            )

        async def run() -> None:
            try:
                if service is None:
                    result = HarnessEvalPromotionFlowStatus(
                        stage="error",
                        suite_id=str(payload["suite_id"]),
                        batch_id=str(payload["batch_id"]),
                        code="service_unavailable",
                        message="Harness Service 尚未初始化。",
                    )
                else:
                    result = await run_eval_promotion_flow(
                        service,
                        suite_id=str(payload["suite_id"]),
                        batch_id=str(payload["batch_id"]),
                        reason=str(payload.get("reason") or ""),
                        interact=self.request_user_interaction,
                        on_progress=emit_status,
                    )
                await emit_status(result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._trace_harness_lookup_failure("eval_promotion", exc)
                await emit_status(
                    HarnessEvalPromotionFlowStatus(
                        stage="error",
                        suite_id=str(payload["suite_id"]),
                        batch_id=str(payload["batch_id"]),
                        code="promotion_failed",
                        message="Baseline 晋升失败；Selector 未改变。",
                    )
                )
            finally:
                self._harness_eval_promotion_tasks.pop(request_id, None)

        task = asyncio.create_task(run())
        self._harness_eval_promotion_tasks[request_id] = task

    def _trace_harness_lookup_failure(self, operation: str, error: Exception) -> None:
        error_type = type(error).__name__
        logger.warning(
            "Harness %s lookup failed (%s)",
            operation,
            error_type,
        )
        if self.debug_trace is not None:
            self.debug_trace.event(
                "harness.detail_lookup_failed",
                {
                    "operation": operation,
                    "error_type": error_type,
                },
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

    async def show_workbench(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Return one read-only, authoritative Workbench snapshot."""
        session = getattr(self.engine, "_session", None)
        if session is None:
            session = await self.engine.get_or_create_session()
        session_id = str(getattr(session, "id", "") or "")
        requested_session_id = str(payload.get("session_id") or "")
        if requested_session_id and requested_session_id != session_id:
            await self.emit_error(
                "Workbench 只能读取当前会话。",
                code="workbench_session_mismatch",
                request_id=request_id,
            )
            return
        service = getattr(self.engine, "workbench_service", None)
        if service is None:
            await self.emit_error(
                "Workbench 服务暂不可用。",
                code="workbench_unavailable",
                request_id=request_id,
            )
            return
        try:
            snapshot = await service.dashboard_snapshot(session_id)
            if (
                str(snapshot.get("session_id") or "") != session_id
                or int(snapshot.get("schema_version") or 0) != 1
                or int(snapshot.get("revision") or 0) < 1
                or not str(snapshot.get("stream_id") or "")
                or snapshot.get("full") is not True
            ):
                raise ValueError("invalid Workbench snapshot contract")
        except Exception as exc:
            error_type = type(exc).__name__
            logger.warning("Workbench snapshot failed (%s)", error_type)
            if self.debug_trace is not None:
                self.debug_trace.event(
                    "workbench.snapshot_failed",
                    {"error_type": error_type},
                )
            await self.emit_error(
                "Workbench 快照加载失败；请稍后重试或运行 `/doctor`。",
                code="workbench_snapshot_failed",
                request_id=request_id,
            )
            return
        await self.emit(
            ServerEventType.WORKBENCH_SNAPSHOT,
            snapshot,
            request_id=request_id,
        )

    async def show_workbench_review(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Return bounded, read-only evidence for one current-session review."""
        session = getattr(self.engine, "_session", None)
        if session is None:
            session = await self.engine.get_or_create_session()
        session_id = str(getattr(session, "id", "") or "")
        requested_session_id = str(payload.get("session_id") or "")
        review_id = str(payload.get("review_id") or "")
        if requested_session_id and requested_session_id != session_id:
            await self.emit_error(
                "Workbench 只能读取当前会话。",
                code="workbench_session_mismatch",
                request_id=request_id,
            )
            return
        service = getattr(self.engine, "workbench_service", None)
        if service is None:
            await self.emit_error(
                "Workbench 服务暂不可用。",
                code="workbench_unavailable",
                request_id=request_id,
            )
            return
        try:
            evidence = await service.get_review_evidence(session_id, review_id)
            if evidence is None:
                await self.emit(
                    ServerEventType.WORKBENCH_REVIEW,
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "review_id": review_id,
                        "status": "unavailable",
                        "code": "review_not_found",
                    },
                    request_id=request_id,
                )
                return
            approval = evidence.get("approval") or {}
            if str(approval.get("id") or "") != review_id:
                raise ValueError("review evidence id mismatch")
        except Exception as exc:
            error_type = type(exc).__name__
            logger.warning("Workbench review evidence failed (%s)", error_type)
            if self.debug_trace is not None:
                self.debug_trace.event(
                    "workbench.review_failed",
                    {"error_type": error_type},
                )
            await self.emit_error(
                "Workbench 审查证据加载失败；请稍后重试。",
                code="workbench_review_failed",
                request_id=request_id,
            )
            return
        await self.emit(
            ServerEventType.WORKBENCH_REVIEW,
            {
                "schema_version": 1,
                "session_id": session_id,
                "review_id": review_id,
                "status": "ready",
                "code": "",
                "evidence": evidence,
            },
            request_id=request_id,
        )

    async def govern_workbench_proposal(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Apply one permission-governed human Proposal decision."""
        session = getattr(self.engine, "_session", None)
        if session is None:
            session = await self.engine.get_or_create_session()
        session_id = str(getattr(session, "id", "") or "")
        requested_session_id = str(payload.get("session_id") or "")
        proposal_id = str(payload.get("proposal_id") or "")
        action = ProposalAction(str(payload.get("action") or ""))
        decision_note = str(payload.get("decision_note") or "")
        confirmed = payload.get("confirmed") is True
        if requested_session_id and requested_session_id != session_id:
            await self.emit_error(
                "Workbench 只能治理当前会话的 Proposal。",
                code="workbench_session_mismatch",
                request_id=request_id,
            )
            return
        service = getattr(self.engine, "workbench_service", None)
        if service is None:
            await self.emit_error(
                "Workbench 服务暂不可用。",
                code="workbench_unavailable",
                request_id=request_id,
            )
            return
        decision = self.engine._permission_checker.check(
            "workbench_govern_proposal",
            {
                "proposal_id": proposal_id,
                "action": action.value,
            },
        )
        if not decision.allowed:
            await self.emit(
                ServerEventType.WORKBENCH_PROPOSAL_ACTION_RESULT,
                {
                    "schema_version": 1,
                    "session_id": session_id,
                    "proposal_id": proposal_id,
                    "action": action.value,
                    "status": "blocked",
                    "message": "当前权限模式不允许治理 Proposal。",
                },
                request_id=request_id,
            )
            return
        if decision.requires_confirmation and not confirmed:
            await self.emit(
                ServerEventType.WORKBENCH_PROPOSAL_ACTION_RESULT,
                {
                    "schema_version": 1,
                    "session_id": session_id,
                    "proposal_id": proposal_id,
                    "action": action.value,
                    "status": "needs_confirmation",
                    "message": "请确认后再次提交该 Proposal 决策。",
                },
                request_id=request_id,
            )
            return
        try:
            proposal = await service.govern_proposal(
                session_id,
                proposal_id,
                action=action,
                reviewer="Human",
                decision_note=decision_note,
            )
            if proposal is None:
                status = "not_found"
                message = "Proposal 不存在或不属于当前会话。"
                snapshot = None
            else:
                status = "completed"
                message = (
                    "Proposal 已批准。"
                    if action is ProposalAction.APPROVE
                    else "Proposal 已拒绝。"
                )
                snapshot = await service.dashboard_snapshot(session_id)
        except ProposalGovernanceConflictError as exc:
            status = "conflict"
            message = str(exc)
            proposal = None
            snapshot = await service.dashboard_snapshot(session_id)
        except (RuntimeError, ValueError) as exc:
            logger.warning("Workbench Proposal action failed (%s)", type(exc).__name__)
            status = "error"
            message = str(exc) if isinstance(exc, ValueError) else "Proposal 决策暂时失败。"
            proposal = None
            snapshot = None
        result = {
            "schema_version": 1,
            "session_id": session_id,
            "proposal_id": proposal_id,
            "action": action.value,
            "status": status,
            "message": message,
            "proposal": proposal,
            "workbench_snapshot": snapshot,
        }
        await self.emit(
            ServerEventType.WORKBENCH_PROPOSAL_ACTION_RESULT,
            result,
            request_id=request_id,
        )
        if snapshot is not None:
            await self.emit(
                ServerEventType.WORKBENCH_SNAPSHOT,
                snapshot,
                request_id=request_id,
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
        await self._resume_harness_receipts(session_id, request_id=request_id)
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
        await self._recover_durable_conversation_queue(session_id)
        await self.emit(ServerEventType.STATUS, self.status_payload())

    async def _recover_durable_conversation_queue(
        self,
        session_id: str,
        *,
        force: bool = False,
    ) -> None:
        """Replay only never-claimed queued messages after an explicit resume."""
        if session_id in self._recovered_queue_sessions and not force:
            return
        authority = self._conversation_queue_authority(session_id)
        if authority is None:
            return
        try:
            recovery = await authority.recover(limit=_MAX_QUEUED_CONVERSATIONS)
        except Exception as exc:
            logger.warning(
                "Durable conversation recovery failed (%s)", type(exc).__name__,
            )
            await self.emit_error(
                "持久队列恢复失败，请运行 /doctor 后重试。",
                code="queue_recovery_failed",
            )
            return
        self._recovered_queue_sessions.add(session_id)
        if force:
            durable_ids = {
                item.request_id for item in (*recovery.ready, *recovery.blocked)
            }
            self._queued_chat_submissions = deque(
                submission
                for submission in self._queued_chat_submissions
                if submission.session_id != session_id
                or submission.request_id in durable_ids
            )
        known = {item.request_id for item in self._queued_chat_submissions}
        recovered = [
            item
            for item in (*recovery.ready, *recovery.blocked)
            if item.request_id not in known
        ]
        for item in recovered:
            self._queued_chat_submissions.append(QueuedChatSubmission(
                text=item.text,
                request_id=item.request_id,
                session_id=session_id,
                durable_item=item,
            ))
            await self.emit(
                ServerEventType.USER_MESSAGE,
                {"content": item.text},
                request_id=item.request_id,
            )
        if recovered:
            await self._emit_queued_chat_positions()
        if recovery.blocked:
            await self.emit_error(
                "检测到上次进程可能已经派发的排队消息；为避免重复副作用，"
                "自动恢复已在该位置停止。",
                code=recovery.blocker_code or "queue_recovery_required",
                request_id=recovery.blocked[0].request_id,
            )
        if recovery.ready and (self._run_task is None or self._run_task.done()):
            await self._start_next_queued_chat()

    async def _resume_harness_receipts(
        self,
        session_id: str,
        *,
        request_id: str,
    ) -> None:
        """Replay durable Harness receipts before their generic completion cards."""
        service = getattr(self.engine, "harness_service", None)
        store = getattr(service, "store", None)
        if store is None:
            return
        try:
            runs = await store.list_session_runs(
                self.engine.workspace_root,
                session_id,
                limit=200,
            )
        except Exception as exc:
            self._trace_harness_lookup_failure("receipt_recovery", exc)
            await self.emit_error(
                "Harness 回执恢复失败；会话与通用完成回执仍会继续恢复。"
                "请运行 `/harness doctor` 检查状态库后重试。",
                code="harness_receipt_recovery_failed",
                request_id=request_id,
            )
            return

        for run in reversed(runs):
            receipt = getattr(run, "receipt", None)
            if receipt is None:
                continue
            await self.emit(
                ServerEventType.HARNESS_RECEIPT,
                {
                    **receipt.model_dump(mode="json"),
                    "schema_version": 1,
                    "revision": 1,
                },
                request_id=request_id,
            )

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
        if raw.lower().startswith("/queue resolve"):
            session = getattr(self.engine, "_session", None)
            session_id = str(getattr(session, "id", "") or "").strip()
            if session_id:
                await self._recover_durable_conversation_queue(
                    session_id,
                    force=True,
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
        from naumi_agent.ui.task_panel import (
            build_task_panel_snapshot,
            render_task_panel_snapshot,
        )

        raw_limit = payload.get("limit", 12)
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            limit = 12
        snapshot = await build_task_panel_snapshot(
            self.engine,
            limit=limit,
            source=str(payload.get("source") or "all"),
            status=str(payload.get("status") or "all"),
            detail_id=str(payload.get("detail_id") or payload.get("detail") or ""),
            history=bool(payload.get("history", False)),
        )
        if "task_snapshot" in self._client_capabilities:
            await self.emit(
                ServerEventType.TASKS_SNAPSHOT,
                snapshot.to_protocol_dict(),
                request_id=request_id,
            )
        else:
            await self.emit(
                ServerEventType.UI_MESSAGE,
                ui_message_payload(
                    SystemNoticeMessage(
                        type=MessageType.SYSTEM_NOTICE,
                        title="tasks",
                        content=render_task_panel_snapshot(snapshot),
                        level="info",
                    )
                ),
                request_id=request_id,
            )
        await self.emit(ServerEventType.STATUS, self.status_payload())

    async def show_goal_panel(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Emit one read-only typed Goal/Pursuit snapshot."""
        from naumi_agent.ui.goal_panel import (
            build_goal_pursuit_snapshot_with_recovery,
            render_goal_pursuit_snapshot,
        )

        harness_service = getattr(self.engine, "harness_service", None)
        snapshot = await build_goal_pursuit_snapshot_with_recovery(
            self.engine.goal_store,
            self.engine.pursuit_store,
            getattr(harness_service, "store", None),
            workspace_root=self.engine.workspace_root,
            limit=int(payload.get("limit", 20)),
            include_finished=bool(payload.get("include_finished", True)),
        )
        if "goal_snapshot" in self._client_capabilities:
            await self.emit(
                ServerEventType.GOALS_SNAPSHOT,
                snapshot.to_protocol_dict(),
                request_id=request_id,
            )
        else:
            await self.emit(
                ServerEventType.UI_MESSAGE,
                ui_message_payload(
                    SystemNoticeMessage(
                        type=MessageType.SYSTEM_NOTICE,
                        title="goal",
                        content=render_goal_pursuit_snapshot(snapshot),
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
        from naumi_agent.ui.permission_panel import (
            build_permission_panel_snapshot,
            permission_panel_payload,
        )

        raw_limit = payload.get("limit", 12)
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            limit = 12
        snapshot = build_permission_panel_snapshot(
            self.engine,
            pending={
                pending_id: pending.public_payload
                for pending_id, pending in self._pending_permissions.items()
            },
            limit=limit,
        )
        await self.emit(
            ServerEventType.PERMISSION_SNAPSHOT,
            permission_panel_payload(snapshot),
            request_id=request_id,
        )
        await self.emit(ServerEventType.STATUS, self.status_payload())

    async def show_evolution_review(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Emit Candidate review state or explicitly enqueue one Proposal."""
        from naumi_agent.evolution.queue import render_queue_result
        from naumi_agent.evolution.review import EvolutionReviewFilter
        from naumi_agent.evolution.store import EvolutionStoreError
        from naumi_agent.ui.evolution_review import evolution_review_payload

        action = str(payload.get("action") or "list")
        try:
            service = self.engine.evolution_review_service
            if action == "enqueue":
                session = getattr(self.engine, "_session", None)
                if session is None:
                    raise ValueError("当前没有活动会话。")
                result = await self.engine.evolution_proposal_queue.enqueue(
                    self.engine.workspace_root,
                    session_id=session.id,
                    mission_id=str(payload.get("mission_id") or ""),
                    task_id=str(payload.get("task_id") or ""),
                    agent_id=str(payload.get("agent_id") or "Human"),
                    candidate_id=str(payload.get("candidate_id") or ""),
                )
                await self._emit_system_notice(
                    "Evolution Proposal",
                    render_queue_result(result),
                    request_id=request_id,
                )
                snapshot = await service.detail_snapshot(
                    self.engine.workspace_root,
                    str(payload.get("candidate_id") or ""),
                )
            elif action == "detail":
                snapshot = await service.detail_snapshot(
                    self.engine.workspace_root,
                    str(payload.get("candidate_id") or ""),
                )
            elif action == "list":
                snapshot = await service.list_snapshot(
                    self.engine.workspace_root,
                    filters=EvolutionReviewFilter(
                        query=str(payload.get("query") or ""),
                        risk=str(payload.get("risk") or ""),
                        source_kind=str(payload.get("source_kind") or ""),
                        limit=int(payload.get("limit") or 50),
                    ),
                )
            else:
                raise ValueError("Evolution action 未注册。")
        except (EvolutionStoreError, OSError, ValueError):
            if action == "enqueue":
                await self.emit_error(
                    "Proposal 未入队：Candidate 未就绪或 mission/task 绑定无效。未执行任何变更。",
                    code="evolution_queue_failed",
                    request_id=request_id,
                )
                return
            await self.emit_error(
                "Evolution Candidate 快照不可用；请运行 /doctor 后重试。",
                code="evolution_review_failed",
                request_id=request_id,
            )
            return
        await self.emit(
            ServerEventType.EVOLUTION_REVIEW,
            evolution_review_payload(snapshot),
            request_id=request_id,
        )
        await self.emit(ServerEventType.STATUS, self.status_payload())

    async def show_doctor_report(self, *, request_id: str) -> None:
        """Render deterministic local diagnostics through the UI protocol."""
        from naumi_agent.ui.doctor import (
            DoctorCheck,
            DoctorReport,
            render_doctor_report,
            run_doctor,
        )
        from naumi_agent.ui.doctor_health import (
            build_doctor_health_snapshot,
            doctor_health_payload,
            pursuit_recovery_health_item,
            runtime_heartbeat_retention_health_item,
        )

        config = getattr(self.engine, "_config", AppConfig())
        additional_items = [
            runtime_heartbeat_retention_health_item(
                self._runtime_heartbeat_retention_status_payload()
            )
        ]
        try:
            recovery = await self._current_pursuit_recovery_snapshot()
            if recovery is not None:
                additional_items.append(pursuit_recovery_health_item(recovery))
        except Exception as exc:
            logger.warning("Pursuit recovery health lookup failed (%s)", type(exc).__name__)
            if self.debug_trace is not None:
                self.debug_trace.exception("ui_bridge.pursuit_recovery", exc)
        try:
            report = await run_doctor(
                config,
                workspace_root=self.engine.workspace_root,
                mcp_manager=getattr(self.engine, "mcp_manager", None),
                model_router=self.engine.router,
            )
            health_snapshot = build_doctor_health_snapshot(
                report,
                additional_items=tuple(additional_items),
            )
        except Exception as exc:
            logger.warning("Local doctor failed (%s)", type(exc).__name__)
            if self.debug_trace is not None:
                self.debug_trace.exception("ui_bridge.doctor", exc)
            report = DoctorReport(
                checks=(
                    DoctorCheck(
                        "Doctor 运行时",
                        "error",
                        "诊断流程自身失败；其余环境状态未知。",
                        "运行 `/debug` 查看脱敏日志路径，然后重启 NaumiAgent 重试。",
                    ),
                )
            )
            health_snapshot = build_doctor_health_snapshot(
                report,
                additional_items=tuple(additional_items),
            )
        await self.emit(
            ServerEventType.DOCTOR_HEALTH,
            doctor_health_payload(health_snapshot),
            request_id=request_id,
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

    async def _current_pursuit_recovery_snapshot(self) -> Any | None:
        """Read the current Goal's recovery facts without mutating runtime state."""
        from naumi_agent.ui.pursuit_recovery import (
            build_pursuit_recovery_snapshot,
        )

        goal_store = getattr(self.engine, "goal_store", None)
        pursuit_store = getattr(self.engine, "pursuit_store", None)
        if goal_store is None or pursuit_store is None:
            return None
        goal = goal_store.current()
        if goal is None or not goal.pursuit_run_id:
            return None
        run = pursuit_store.get_run(goal.pursuit_run_id)
        if run is None:
            return None
        harness_service = getattr(self.engine, "harness_service", None)
        return await build_pursuit_recovery_snapshot(
            run,
            pursuit_store,
            getattr(harness_service, "store", None),
            workspace_root=self.engine.workspace_root,
        )

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

        if (
            self._active_queue_claim is not None
            and event in {"completion_receipt", "harness_completion_receipt"}
        ):
            if event == "completion_receipt":
                self._active_completion_receipt = CompletionReceipt.from_dict(data)
            self._deferred_queue_receipt_events.append((event, dict(data)))
            return
        await self._publish_engine_event(event, data)

    async def _publish_engine_event(self, event: str, data: dict[str, Any]) -> None:
        """Publish one engine event after any durable completion boundary."""

        await self.emit(ServerEventType.ENGINE_EVENT, {"event": event, "data": data})
        if event == "harness_completion_receipt":
            await self.emit(
                ServerEventType.HARNESS_RECEIPT,
                {
                    **data,
                    "schema_version": 1,
                    "revision": 1,
                },
                request_id=self._active_run_context.get("request_id") or None,
            )
        if event == "completion_receipt":
            receipt = CompletionReceipt.from_dict(data)
            self._active_completion_receipt = receipt
            await self.emit(
                ServerEventType.COMPLETION_RECEIPT,
                receipt.to_dict(),
                request_id=self._active_run_context.get("request_id") or None,
            )
        message = self.adapter.adapt(event, data)
        if message is not None and event not in {
            "completion_receipt",
            "harness_completion_receipt",
        }:
            await self.emit(ServerEventType.UI_MESSAGE, ui_message_payload(message))

        if event in {
            "run_started",
            "tool_end",
            "task_snapshot",
            "permission_bubble",
            "context_compacted",
            "harness_completion_correction",
            "harness_completion_receipt",
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
            "harness_completion_correction",
            "harness_completion_receipt",
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
            "harness_completion_correction",
            "harness_completion_receipt",
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

    async def request_user_interaction(self, payload: dict[str, Any]) -> dict[str, str]:
        """Emit one validated interaction and suspend its calling tool."""
        if self._closed:
            raise UserInteractionUnavailableError("界面已关闭，无法继续询问用户")
        request = normalize_interaction_request(payload)
        request_id = str(payload.get("_interaction_id") or "").strip()
        if not re.fullmatch(r"ask-[A-Za-z0-9._:-]{1,128}", request_id):
            request_id = self._next_interaction_request_id()
        authority = self._interaction_authority()
        durable_record: HarnessInteractionRecord | None = None
        if authority is not None:
            subject_kind = str(
                payload.get("_durable_subject_kind") or "runtime"
            )
            subject_id = str(
                payload.get("_durable_subject_id")
                or getattr(getattr(self.engine, "_session", None), "id", "")
                or "runtime-sessionless"
            )
            durable_record = await authority.create(
                request=request,
                subject_kind=subject_kind,
                subject_id=subject_id,
                session_id=str(
                    getattr(getattr(self.engine, "_session", None), "id", "") or ""
                ),
                agent_name=str(payload.get("agent_name") or "main"),
                interaction_id=request_id,
            )
        pursuit_begin = payload.get("_pursuit_begin")
        if callable(pursuit_begin):
            await pursuit_begin(request_id, request.to_public_dict())
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, str]] = loop.create_future()
        public_payload = {
            "request_id": request_id,
            "session_id": str(getattr(getattr(self.engine, "_session", None), "id", "") or ""),
            "run_id": str(self._active_run_context.get("run_id") or ""),
            "agent_name": str(payload.get("agent_name") or "main"),
            **request.to_public_dict(),
            "expires_at": durable_record.expires_at if durable_record else "",
            "status": "needs_input",
        }
        pending = PendingInteraction(
            future=future,
            request=request,
            public_payload=public_payload,
            durable_record=durable_record,
            pursuit_resolve=(
                payload.get("_pursuit_resolve")
                if callable(payload.get("_pursuit_resolve"))
                else None
            ),
        )
        self._pending_interactions[request_id] = pending
        await self.emit(
            ServerEventType.INTERACTION_REQUEST,
            public_payload,
            request_id=request_id,
        )
        self._schedule_pending_interaction_timeout(request_id)
        self._schedule_pending_interaction_owner_renewal(request_id)
        try:
            return await future
        finally:
            if self._pending_interactions.get(request_id) is pending:
                self._pending_interactions.pop(request_id, None)

    def _next_interaction_request_id(self) -> str:
        while True:
            request_id = f"ask-{uuid4().hex}"
            if request_id not in self._pending_interactions:
                return request_id

    async def resolve_user_interaction(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        interaction_id = str(payload.get("request_id") or "").strip()
        pending = self._pending_interactions.get(interaction_id)
        if pending is None or pending.future.done():
            await self.emit_error(
                f"未找到待回答的用户交互: {interaction_id or '-'}",
                code="unknown_interaction_request",
                request_id=request_id,
            )
            return
        try:
            response = normalize_interaction_response(pending.request, payload)
        except ValueError as exc:
            await self.emit_error(
                str(exc),
                code="interaction_response_invalid",
                request_id=request_id,
            )
            return
        await self._stop_pending_interaction_owner_renewal(pending)
        durable = pending.durable_record
        if durable is not None:
            authority = self._interaction_authority()
            if authority is None:
                await self.emit_error(
                    "持久交互 authority 不可用，答案尚未提交。",
                    code="interaction_authority_unavailable",
                    request_id=request_id,
                )
                return
            try:
                durable, response = await authority.answer(
                    record=durable,
                    response=response,
                )
                pending.durable_record = durable
            except Exception as exc:
                logger.warning(
                    "Durable interaction answer failed (%s)",
                    type(exc).__name__,
                )
                await self.emit_error(
                    "答案未能提交到持久交互 authority，请刷新后重试。",
                    code="interaction_answer_not_committed",
                    request_id=request_id,
                )
                return
        if pending.pursuit_resolve is not None:
            try:
                await pending.pursuit_resolve(interaction_id, response)
            except Exception as exc:
                logger.warning(
                    "Pursuit interaction checkpoint resolve failed (%s)",
                    type(exc).__name__,
                )
                await self.emit_error(
                    "答案已持久化，但目标追踪 checkpoint 尚未确认；请使用 `/pursue resume`。",
                    code="interaction_checkpoint_not_resolved",
                    request_id=request_id,
                )
                return
        timeout_task = pending.timeout_task
        if timeout_task is not None:
            timeout_task.cancel()
            await asyncio.gather(timeout_task, return_exceptions=True)
            pending.timeout_task = None
        pending.future.set_result(response)
        await self.emit(
            ServerEventType.INTERACTION_RESOLVED,
            {"request_id": interaction_id, "status": "answered", **response},
            request_id=request_id,
        )
        if pending.replay_only:
            self._pending_interactions.pop(interaction_id, None)

    async def cancel_user_interaction(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Cancel one durable interaction through sequence-fenced authority."""
        interaction_id = str(payload.get("interaction_id") or "")
        authority = self._interaction_authority()
        if authority is None:
            await self.emit_error(
                "持久交互 authority 不可用，取消未提交。",
                code="interaction_authority_unavailable",
                request_id=request_id,
            )
            return
        try:
            record = await authority.store.get_interaction(
                workspace_root=self.engine.workspace_root,
                interaction_id=interaction_id,
            )
        except Exception:
            await self.emit_error(
                "持久交互读取失败，请运行 `/doctor` 后重试。",
                code="interaction_authority_read_failed",
                request_id=request_id,
            )
            return
        if record is None:
            await self.emit_error(
                f"未找到持久用户交互: {interaction_id}",
                code="unknown_interaction_request",
                request_id=request_id,
            )
            return
        if record.state != "pending":
            await self.emit_error(
                f"用户交互已是终态：{record.state}，不能取消。",
                code="interaction_not_pending",
                request_id=request_id,
            )
            return
        try:
            linked_runs = {
                goal.pursuit_run_id
                for goal in self.engine.goal_store.list(
                    include_finished=True,
                    limit=50,
                )
                if goal.pursuit_run_id
            }
        except Exception:
            await self.emit_error(
                "Goal 状态读取失败，请运行 `/doctor` 后重试。",
                code="goal_state_unavailable",
                request_id=request_id,
            )
            return
        if record.subject_kind != "pursuit" or record.subject_id not in linked_runs:
            await self.emit_error(
                "该交互不属于当前 Goal 页面中的 Pursuit，拒绝取消。",
                code="interaction_scope_mismatch",
                request_id=request_id,
            )
            return
        pending = self._pending_interactions.get(interaction_id)
        if pending is not None:
            await self._stop_pending_interaction_owner_renewal(pending)
            if pending.durable_record is not None:
                record = pending.durable_record
        try:
            cancelled = await authority.cancel(record=record)
        except Exception as exc:
            logger.warning("Durable interaction cancel failed (%s)", type(exc).__name__)
            if pending is not None and not pending.future.done():
                self._schedule_pending_interaction_owner_renewal(interaction_id)
            await self.emit_error(
                "用户交互在取消前已发生变化，请刷新 Goal 页面重试。",
                code="interaction_cancel_conflict",
                request_id=request_id,
            )
            return
        if pending is not None:
            pending.durable_record = cancelled
            if pending.timeout_task is not None:
                pending.timeout_task.cancel()
                await asyncio.gather(pending.timeout_task, return_exceptions=True)
                pending.timeout_task = None
            if not pending.future.done():
                if pending.replay_only:
                    pending.future.cancel()
                else:
                    pending.future.set_exception(
                        UserInteractionUnavailableError("用户已取消本次交互")
                    )
            self._pending_interactions.pop(interaction_id, None)
        await self.emit(
            ServerEventType.INTERACTION_RESOLVED,
            {
                "request_id": interaction_id,
                "status": "cancelled",
                "reason": "用户已从 Goal 页面取消本次交互。",
            },
            request_id=request_id,
        )
        await self.emit(ServerEventType.STATUS, self.status_payload())

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
        terminal_runtime = self._terminal_runtime_lifecycle
        if terminal_runtime is not None:
            try:
                await terminal_runtime.begin_draining()
            except Exception as exc:
                logger.warning(
                    "Terminal runtime draining failed (%s)",
                    type(exc).__name__,
                )
        if self._interaction_replay_task is not None:
            self._interaction_replay_task.cancel()
            await asyncio.gather(
                self._interaction_replay_task,
                return_exceptions=True,
            )
            self._interaction_replay_task = None
        queued_submissions = list(self._queued_chat_submissions)
        self._queued_chat_submissions.clear()
        for submission in queued_submissions:
            if submission.durable_item is not None:
                authority = self._conversation_queue_authority(submission.session_id)
                if authority is not None:
                    try:
                        await authority.cancel_unclaimed(
                            submission.durable_item,
                            reason="ui_shutdown",
                        )
                    except Exception as exc:
                        logger.warning(
                            "Durable queued conversation shutdown failed (%s)",
                            type(exc).__name__,
                        )
            await self.emit(
                ServerEventType.RUN_CANCELLED,
                {
                    "status": "cancelled",
                    "target_request_id": submission.request_id,
                    "intent": "chat",
                    "reason": "界面已关闭，排队对话未执行。",
                },
                request_id=submission.request_id,
            )
        for pending in list(self._pending_permissions.values()):
            if not pending.future.done():
                pending.future.set_result("deny")
        self._pending_permissions.clear()
        interaction_timeout_tasks: list[asyncio.Task[None]] = []
        interaction_owner_tasks: list[asyncio.Task[None]] = []
        for pending in list(self._pending_interactions.values()):
            if pending.timeout_task is not None:
                pending.timeout_task.cancel()
                interaction_timeout_tasks.append(pending.timeout_task)
            if pending.owner_renew_task is not None:
                pending.owner_renew_task.cancel()
                interaction_owner_tasks.append(pending.owner_renew_task)
            if not pending.future.done():
                if pending.replay_only:
                    pending.future.cancel()
                else:
                    pending.future.set_exception(
                        UserInteractionUnavailableError("界面已关闭，无法继续询问用户")
                    )
        if interaction_timeout_tasks:
            await asyncio.gather(
                *interaction_timeout_tasks,
                return_exceptions=True,
            )
        if interaction_owner_tasks:
            await asyncio.gather(
                *interaction_owner_tasks,
                return_exceptions=True,
            )
        self._pending_interactions.clear()
        if self._run_task is not None and not self._run_task.done():
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
        batch_tasks = tuple(self._harness_eval_batch_tasks.values())
        for task in batch_tasks:
            task.cancel()
        if batch_tasks:
            await asyncio.gather(*batch_tasks, return_exceptions=True)
        self._harness_eval_batch_tasks.clear()
        promotion_tasks = tuple(self._harness_eval_promotion_tasks.values())
        for task in promotion_tasks:
            task.cancel()
        if promotion_tasks:
            await asyncio.gather(*promotion_tasks, return_exceptions=True)
        self._harness_eval_promotion_tasks.clear()
        try:
            await self.engine.shutdown()
        except Exception:
            if terminal_runtime is not None:
                try:
                    await terminal_runtime.close(failed=True)
                except Exception as exc:
                    logger.warning(
                        "Terminal runtime failure write failed (%s)",
                        type(exc).__name__,
                    )
            raise
        if terminal_runtime is not None:
            try:
                await terminal_runtime.close(
                    failed=(
                        terminal_runtime.snapshot().state
                        is TerminalRuntimeState.FAILED
                    )
                )
            except Exception as exc:
                logger.warning(
                    "Terminal runtime stopped write failed (%s)",
                    type(exc).__name__,
                )
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
    config.bind_runtime_workspace(Path.cwd())
    setup_logging(config.log_level)
    if engine_factory is None:
        from naumi_agent.runtime.composition import create_agent_engine

        engine_factory = create_agent_engine
    engine = engine_factory(config)
    try:
        await engine.start_long_running_services()
    except Exception:
        await engine.shutdown()
        raise
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
    parser.add_argument(
        "--config",
        "-c",
        default=DEFAULT_CONFIG_PATH,
        help="配置文件路径",
    )
    args = parser.parse_args(argv)
    bridge = await create_bridge(config_path=args.config)
    await serve_stdio(bridge)


def main(argv: list[str] | None = None) -> None:
    _configure_stdio_utf8()
    asyncio.run(_amain(argv))


if __name__ == "__main__":
    main()
