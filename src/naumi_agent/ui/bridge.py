"""JSONL bridge between the Python engine and next-generation terminal UI."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import re
import subprocess
import sys
import threading
from collections import deque
from collections.abc import Awaitable, Callable
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
from naumi_agent.evolution.evaluation_lane_receipts import (
    EvolutionEvaluationLaneReceiptError,
)
from naumi_agent.evolution.experiments import (
    EvolutionExperimentContractAuthority,
    EvolutionExperimentContractStoreError,
    default_experiment_seed,
)
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
    InteractionClaimError,
)
from naumi_agent.harness.replay_models import HarnessReplayLookup
from naumi_agent.harness.sandbox_retry_recovery import (
    SANDBOX_RETRY_RECOVERY_LIMIT,
    HarnessSandboxRetryRecoverySnapshot,
    unavailable_sandbox_retry_recovery_snapshot,
)
from naumi_agent.harness.store import (
    HarnessConversationQueueItem,
    HarnessStore,
    HarnessStoreConflictError,
)
from naumi_agent.inspector import RuntimeInspectorSnapshot
from naumi_agent.log_setup import setup_logging
from naumi_agent.orchestrator.pursuit_recovery_attempt import (
    pursuit_recovery_attempt_id,
)
from naumi_agent.runs.models import CompletionReceipt
from naumi_agent.runtime.terminal_events import (
    REPLAY_SAFE_TERMINAL_EVENTS,
    TerminalEventJournalError,
    TerminalEventJournalStore,
    TerminalEventRecord,
)
from naumi_agent.runtime.terminal_runtime import (
    TerminalRuntimeLifecycle,
    TerminalRuntimeLifecycleFactory,
    TerminalRuntimeState,
    terminal_run_release_context,
)
from naumi_agent.streaming.sinks import CallbackEventSink
from naumi_agent.tasks.models import TaskStatus
from naumi_agent.ui.command_index import build_terminal_command_index
from naumi_agent.ui.doctor_export import (
    DoctorExportPlan,
    build_doctor_export_plan,
    doctor_export_preview_payload,
    doctor_export_receipt_payload,
    write_doctor_export,
)
from naumi_agent.ui.doctor_health import DoctorHealthSnapshot
from naumi_agent.ui.doctor_probe import (
    DOCTOR_LIVE_PROBE_DEFAULT_TIMEOUT_MS,
    cancelled_doctor_live_probe_payload,
    doctor_live_probe_payload,
    run_bounded_doctor_live_probe,
)
from naumi_agent.ui.evaluation_lane_receipt import evaluation_lane_receipt_payload
from naumi_agent.ui.harness_protocol import (
    harness_eval_baseline_payload,
    harness_eval_batch_payload,
    harness_eval_promotion_payload,
    harness_explain_payload,
    harness_replay_payload,
    harness_sandbox_cancel_receipt_payload,
    harness_sandbox_retry_result_payload,
)
from naumi_agent.ui.messages import EngineEventAdapter, MessageType, SystemNoticeMessage
from naumi_agent.ui.page_index import build_terminal_page_index
from naumi_agent.ui.permission_confirmation import (
    normalize_backend_permission_choices,
    public_permission_request_payload,
)
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
from naumi_agent.ui.workspace_file_index import workspace_file_search_payload
from naumi_agent.user_interaction import (
    UserInteractionRequest,
    UserInteractionUnavailableError,
    normalize_interaction_request,
    normalize_interaction_response,
    public_interaction_request_payload,
)
from naumi_agent.workbench.models import ApprovalState, ParallelMode, RiskLevel
from naumi_agent.workbench.proposal_governance import (
    ProposalAction,
    ProposalGovernanceConflictError,
)
from naumi_agent.workbench.store import ApprovalResolutionConflictError

logger = logging.getLogger(__name__)

_TERMINAL_MISSION_STATUSES = frozenset({
    "completed",
    "cancelled",
    "canceled",
    "closed",
    "archived",
})
_MAX_QUEUED_CONVERSATIONS = 20
_MAX_RECOVERED_INTERACTION_CARDS = 50
_SANDBOX_RETRY_RECOVERY_TIMEOUT_SECONDS = 2.0
_WORKBENCH_TIMELINE_POLL_SECONDS = 0.5
_WORKBENCH_TIMELINE_REPLAY_LIMIT = 100
_HARNESS_DETAIL_UNAVAILABLE = (
    "Harness 详情暂不可用。请确认当前工作区状态库可读，然后运行 `/harness doctor`。"
)


def _terminal_event_idempotency_key(
    event_type: str,
    payload: dict[str, Any],
) -> str:
    """Derive a stable semantic identity without transport request metadata."""
    if event_type == str(ServerEventType.COMPLETION_RECEIPT):
        receipt_id = str(payload.get("receipt_id") or "").strip()
        if not receipt_id:
            raise TerminalEventJournalError("完成回执缺少 receipt_id。")
        return f"completion:{receipt_id}"
    if event_type == str(ServerEventType.HARNESS_RECEIPT):
        run_id = str(payload.get("run_id") or "").strip()
        revision = payload.get("revision")
        if (
            not run_id
            or not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 1
        ):
            raise TerminalEventJournalError(
                "Harness 回执缺少有效的 run_id 或 revision。"
            )
        return f"harness:{run_id}:{revision}"
    raise TerminalEventJournalError(f"事件不支持持久化: {event_type}")


def _experiment_contract_public_payload(
    authority: EvolutionExperimentContractAuthority,
) -> dict[str, Any]:
    item = EvolutionExperimentContractAuthority.model_validate(
        authority.model_dump(mode="json")
    )
    contract = item.contract
    return {
        "schema_version": 1,
        "authority_id": item.authority_id,
        "authority_sha256": item.authority_sha256,
        "contract_id": item.contract_id,
        "manifest_sha256": item.contract_manifest_sha256,
        "proposal_id": contract.source.workbench_proposal_id,
        "candidate_id": item.candidate_id,
        "candidate_revision": item.candidate_revision,
        "impact_scope": contract.scope.impact_scope,
        "allowed_files": list(contract.scope.allowed_files),
        "budget": contract.budget.model_dump(mode="json"),
        "execution_ready": False,
        "promotion_ready": False,
    }

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


@dataclass(slots=True)
class _BridgeHarnessSandboxFrontend:
    """Bind one shared Slash progress stream to its originating UI request."""

    bridge: Any
    request_id: str

    async def update_harness_sandbox_eval(
        self,
        progress: dict[str, object],
    ) -> None:
        await self.bridge.emit(
            ServerEventType.HARNESS_EVAL_BATCH,
            dict(progress),
            request_id=self.request_id,
        )


def _backend_choices_error_message(kind: str) -> str:
    if kind == "missing":
        return "后端权限选择缺失，系统已拒绝本次操作。"
    if kind == "invalid":
        return "后端权限选择格式或内容无效，系统已拒绝本次操作。"
    if kind == "medium_risk_unusable":
        return "后端权限选择无法同时提供批准与拒绝，系统已拒绝本次操作。"
    return "后端权限选择为空或无效，系统已拒绝本次操作。"


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
            "command": "/extensions",
            "description": "查看扩展来源、优先级、冲突与无效清单",
        },
        {
            "command": "/harness",
            "description": "Harness 状态、重复评测、Baseline、运行解释、证据、知识、检查与信任",
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
            "description": "审查 Candidate、执行受控回滚并记录 Proposal Outcome",
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
        return [
            item.to_public_dict()
            for item in build_terminal_command_index("new_ui")
        ]
    except Exception:
        return []


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
    if cli_commands and all(item.get("schema_version") == 1 for item in cli_commands):
        return cli_commands
    return _normalize_slash_commands(
        cli_commands if cli_commands else _fallback_slash_command_registry()
    )


def _navigation_page_payload() -> list[dict[str, Any]]:
    """Return fail-closed static navigation metadata for the New UI."""
    try:
        return [
            item.to_public_dict()
            for item in build_terminal_page_index("new_ui")
        ]
    except Exception:
        logger.warning("Unable to build terminal navigation page index", exc_info=True)
        return []


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


def _bounded_action_message(value: object) -> str:
    """Return one terminal-safe action summary without raw control sequences."""
    text = strip_ansi(str(value or ""))
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", text).strip()
    return text[:4_000] or "恢复动作没有返回可展示结果。"


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
        self._protocol_negotiated = False
        self._writer: TextIO | None = None
        self._writer_lock = asyncio.Lock()
        self._protocol_event_registry = load_protocol_event_registry()
        terminal_event_store = getattr(self.engine, "terminal_event_store", None)
        if terminal_event_store is not None and not isinstance(
            terminal_event_store,
            TerminalEventJournalStore,
        ):
            raise TypeError(
                "engine.terminal_event_store 必须是 TerminalEventJournalStore。"
            )
        self._terminal_event_store = terminal_event_store
        if self._terminal_event_store is not None:
            self._validate_terminal_event_policies()
        self._run_task: asyncio.Task[Any] | None = None
        self._harness_eval_batch_tasks: dict[str, asyncio.Task[None]] = {}
        self._harness_eval_retry_tasks: dict[str, asyncio.Task[None]] = {}
        self._harness_eval_promotion_tasks: dict[str, asyncio.Task[None]] = {}
        self._goal_lifecycle_tasks: dict[str, asyncio.Task[None]] = {}
        self._pursuit_recovery_tasks: dict[str, asyncio.Task[None]] = {}
        self._pursuit_terminal_outbox_tasks: dict[str, asyncio.Task[None]] = {}
        self._workspace_file_search_task: asyncio.Task[None] | None = None
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
        self._workbench_subscribed = False
        self._workbench_session_id = ""
        self._workbench_timeline_stream_id = ""
        self._workbench_timeline_cursor = 0
        self._workbench_refresh_task: asyncio.Task[None] | None = None
        self._workbench_refresh_error_emitted = False
        self._doctor_health_snapshot: DoctorHealthSnapshot | None = None
        self._doctor_export_plan: DoctorExportPlan | None = None
        self._doctor_probe_task: asyncio.Task[None] | None = None
        self._doctor_probe_request_id = ""
        self._doctor_probe_timeout_ms = DOCTOR_LIVE_PROBE_DEFAULT_TIMEOUT_MS
        self._doctor_probe_request_started = False
        self._cli_supported_commands = _load_cli_slash_commands_with_alias()
        self._pending_permissions: dict[str, PendingPermission] = {}
        self._pending_interactions: dict[str, PendingInteraction] = {}
        self._interaction_owner_id = f"bridge-{uuid4().hex}"
        self._interaction_authority_client: (
            DurableInteractionAuthorityClient | None
        ) = None
        self._interaction_authority_store: object | None = None
        self._interaction_replay_task: asyncio.Task[None] | None = None
        self._interaction_recovery_cursor = ""
        self._interaction_recovery_retry_after: float | None = None
        self._interaction_recovery_rescan_pending = False
        self._interaction_claim_lock = asyncio.Lock()
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
        self._sandbox_retry_recovery_snapshot = (
            unavailable_sandbox_retry_recovery_snapshot(
                self.engine.workspace_root,
                error_code="not_scanned",
            )
        )
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
        journal: bool = True,
        event_id: str | None = None,
        stream_id: str | None = None,
        cursor: int | None = None,
    ) -> None:
        """Emit one JSONL record to the frontend."""
        if self._writer is None:
            raise RuntimeError("bridge writer is not bound")
        async with self._writer_lock:
            event_type = str(event)
            policy = self._protocol_event_registry.policy("server", event_type)
            explicit_durable_fields = (event_id, stream_id, cursor)
            if any(value is not None for value in explicit_durable_fields) and not all(
                value is not None for value in explicit_durable_fields
            ):
                raise ValueError("event_id、stream_id 与 cursor 必须同时提供。")
            durable_fields: dict[str, str | int] = (
                {
                    "event_id": str(event_id),
                    "stream_id": str(stream_id),
                    "cursor": int(cursor),
                }
                if event_id is not None and stream_id is not None and cursor is not None
                else {}
            )
            if (
                journal
                and self._terminal_event_store is not None
                and event_type in REPLAY_SAFE_TERMINAL_EVENTS
            ):
                if durable_fields:
                    raise ValueError("持久终端事件不能注入外部 durable identity。")
                session_id = str(
                    getattr(getattr(self.engine, "_session", None), "id", "") or ""
                ).strip()
                if not session_id:
                    raise TerminalEventJournalError(
                        "回执缺少会话边界，无法分配持久事件游标。"
                    )
                stored = await self._terminal_event_store.append(
                    session_id=session_id,
                    event_type=event_type,
                    criticality=policy.criticality,
                    idempotency_key=_terminal_event_idempotency_key(
                        event_type,
                        payload or {},
                    ),
                    payload=payload or {},
                )
                durable_fields = stored.envelope_fields()
            next_sequence = self._sequence + 1
            record = make_envelope(
                event,
                payload or {},
                request_id=request_id,
                sequence=next_sequence,
                criticality=policy.criticality,
                **durable_fields,
            )
            text = encode_jsonl(record)
            self._writer.write(text)
            self._writer.flush()
            self._sequence = next_sequence
        if self.debug_trace is not None:
            self.debug_trace.output("ui_bridge.stdout", text)

    async def _emit_replayed_terminal_event(
        self,
        record: TerminalEventRecord,
        *,
        request_id: str,
    ) -> None:
        """Publish one already-verified durable record without reallocating identity."""
        if self._writer is None:
            raise RuntimeError("bridge writer is not bound")
        async with self._writer_lock:
            policy = self._protocol_event_registry.policy("server", record.event_type)
            if policy.criticality != record.criticality:
                raise TerminalEventJournalError(
                    "终端事件恢复记录与当前协议策略不一致。"
                )
            next_sequence = self._sequence + 1
            envelope = make_envelope(
                record.event_type,
                record.payload,
                request_id=request_id,
                sequence=next_sequence,
                criticality=policy.criticality,
                **record.envelope_fields(),
            )
            text = encode_jsonl(envelope)
            self._writer.write(text)
            self._writer.flush()
            self._sequence = next_sequence
        if self.debug_trace is not None:
            self.debug_trace.output("ui_bridge.stdout", text)

    def _validate_terminal_event_policies(self) -> None:
        """Fail closed if the shared registry makes a journaled event unsafe."""
        for event_type in sorted(REPLAY_SAFE_TERMINAL_EVENTS):
            policy = self._protocol_event_registry.policy("server", event_type)
            if (
                policy.criticality != "terminal"
                or policy.persistence != "audit"
                or policy.sensitive_fields
                or policy.redaction != "none"
            ):
                raise TerminalEventJournalError(
                    f"终端事件 {event_type} 的协议策略不再允许原文持久化。"
                )

    async def emit_ready(self) -> None:
        heartbeat_error = await self._start_terminal_runtime_lifecycle()
        await self._refresh_sandbox_retry_recovery_snapshot()
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

    async def _refresh_sandbox_retry_recovery_snapshot(self) -> None:
        """Discover restart work without acquiring any retry execution fence."""
        service = getattr(self.engine, "harness_service", None)
        snapshot_builder = getattr(
            service,
            "sandbox_retry_recovery_snapshot",
            None,
        )
        if not callable(snapshot_builder):
            self._sandbox_retry_recovery_snapshot = (
                unavailable_sandbox_retry_recovery_snapshot(
                    self.engine.workspace_root,
                    error_code="service_unavailable",
                )
            )
            return
        try:
            snapshot = await asyncio.wait_for(
                snapshot_builder(limit=SANDBOX_RETRY_RECOVERY_LIMIT),
                timeout=_SANDBOX_RETRY_RECOVERY_TIMEOUT_SECONDS,
            )
            self._sandbox_retry_recovery_snapshot = (
                HarnessSandboxRetryRecoverySnapshot.model_validate(
                    snapshot.model_dump(mode="json")
                )
            )
        except Exception as exc:
            error_code = (
                "startup_scan_timeout"
                if isinstance(exc, TimeoutError)
                else "startup_scan_failed"
            )
            self._sandbox_retry_recovery_snapshot = (
                unavailable_sandbox_retry_recovery_snapshot(
                    self.engine.workspace_root,
                    error_code=error_code,
                )
            )
            logger.warning(
                "Sandbox retry startup discovery failed (%s)",
                type(exc).__name__,
            )

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
        capacity = _MAX_RECOVERED_INTERACTION_CARDS - len(
            self._pending_interactions
        )
        if capacity <= 0:
            return
        if not self._interaction_recovery_cursor:
            self._interaction_recovery_rescan_pending = False
        now = datetime.now(UTC)
        cursor_before = self._interaction_recovery_cursor
        try:
            async with self._interaction_claim_lock:
                recovery = await authority.recover_pending(
                    now=now.isoformat(),
                    limit=capacity,
                    cursor=self._interaction_recovery_cursor,
                )
                if recovery.retry_after_seconds is not None:
                    current_retry = self._interaction_recovery_retry_after
                    self._interaction_recovery_retry_after = min(
                        current_retry or recovery.retry_after_seconds,
                        recovery.retry_after_seconds,
                    )
                for record in recovery.claimed:
                    await self._bind_replayed_interaction(record)
                self._interaction_recovery_cursor = recovery.next_cursor
        except Exception as exc:
            logger.warning(
                "Durable interaction replay failed (%s)", type(exc).__name__,
            )
            current_retry = self._interaction_recovery_retry_after
            self._interaction_recovery_retry_after = min(
                current_retry or 0.5,
                0.5,
            )
            if not self._interaction_recovery_cursor:
                self._interaction_recovery_rescan_pending = True
            if not self._closed:
                self._schedule_interaction_replay(0.5)
            return
        if self._closed:
            return
        if (
            self._interaction_recovery_cursor == cursor_before
            and self._interaction_recovery_cursor
            and recovery.retry_after_seconds is not None
        ):
            self._schedule_interaction_replay(recovery.retry_after_seconds)
            return
        if (
            self._interaction_recovery_cursor
            and len(self._pending_interactions) < _MAX_RECOVERED_INTERACTION_CARDS
        ):
            self._schedule_interaction_replay(0.0, immediate=True)
            return
        if not self._interaction_recovery_cursor:
            retry_after_seconds = self._interaction_recovery_retry_after
            self._interaction_recovery_retry_after = None
            if retry_after_seconds is not None:
                self._interaction_recovery_rescan_pending = True
                self._schedule_interaction_replay(retry_after_seconds)

    def _schedule_interaction_recovery_fill(self) -> None:
        """Fill one released replay-card slot from the current snapshot."""
        if (
            (self._interaction_recovery_cursor or self._interaction_recovery_rescan_pending)
            and len(self._pending_interactions) < _MAX_RECOVERED_INTERACTION_CARDS
            and not self._closed
        ):
            self._schedule_interaction_replay(0.0, immediate=True)

    async def _bind_replayed_interaction(
        self,
        record: HarnessInteractionRecord,
    ) -> bool:
        """Bind one authority-owned record to this Bridge Future and UI card."""
        if record.interaction_id in self._pending_interactions:
            return False
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
        pending = self._pending_interactions[record.interaction_id]
        try:
            await self.emit(
                ServerEventType.INTERACTION_REQUEST,
                public_payload,
                request_id=record.interaction_id,
            )
            self._schedule_pending_interaction_owner_renewal(record.interaction_id)
            self._schedule_pending_interaction_timeout(record.interaction_id)
        except Exception:
            if self._pending_interactions.get(record.interaction_id) is pending:
                self._pending_interactions.pop(record.interaction_id, None)
            tasks = tuple(
                task
                for task in (pending.owner_renew_task, pending.timeout_task)
                if task is not None and task is not asyncio.current_task()
            )
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if not future.done():
                future.cancel()
            raise
        return True

    def _schedule_interaction_replay(
        self,
        delay_seconds: float,
        *,
        immediate: bool = False,
    ) -> None:
        """Recheck a live foreign owner without stealing its valid lease."""
        current = self._interaction_replay_task
        if current is not None and not current.done():
            return
        delay = (
            max(0.0, delay_seconds)
            if immediate else max(0.05, delay_seconds + 0.05)
        )

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
            self._schedule_interaction_recovery_fill()

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
                "compatible_registry_sha256": list(
                    self._protocol_event_registry.compatible_registry_sha256
                ),
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
            "sandbox_retry_recovery": (
                self._sandbox_retry_recovery_snapshot.model_dump(mode="json")
            ),
            "tasks": self._task_activity_payload(),
            "ui": {
                "show_reasoning": self._show_reasoning,
            },
            "git": _git_snapshot(workspace_root),
            "config_path": self.config_path,
        }
        if include_slash_commands:
            payload["slash_commands"] = _slash_command_payload()
            payload["navigation_pages"] = _navigation_page_payload()
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
            self._protocol_negotiated = True
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

        required_capability = self._protocol_event_registry.required_capability(
            "client",
            event_type,
        )
        if (
            required_capability is not None
            and (
                not self._protocol_negotiated
                or required_capability not in self._client_capabilities
            )
        ):
            await self.emit_error(
                "当前终端 UI 未协商此类型化能力；请使用兼容命令通道或升级终端 UI。",
                code="protocol_capability_not_negotiated",
                request_id=request_id,
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

        if event_type == ClientEventType.INTERACTION_TAKEOVER:
            await self.takeover_user_interaction(payload, request_id=request_id)
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
        if event_type == ClientEventType.WORKSPACE_FILES_REQUEST:
            await self.search_workspace_files(payload, request_id=request_id)
            return
        if event_type == ClientEventType.WORKSPACE_FILES_CANCEL:
            await self.cancel_workspace_file_index(request_id=request_id)
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
        if event_type == ClientEventType.HARNESS_EVAL_SANDBOX_CANCEL:
            await self.cancel_harness_eval_sandbox(payload, request_id=request_id)
            return
        if event_type == ClientEventType.HARNESS_EVAL_SANDBOX_RETRY:
            await self.start_harness_eval_sandbox_retry(
                payload,
                request_id=request_id,
            )
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
        if event_type == ClientEventType.AGENTS_RECOVERY_RESOLVE_UNKNOWN:
            await self.resolve_agent_recovery_unknown(
                payload,
                request_id=request_id,
            )
            return
        if event_type == ClientEventType.AGENTS_RESULT_ACKNOWLEDGE:
            await self.acknowledge_agent_result(
                payload,
                request_id=request_id,
            )
            return
        if event_type == ClientEventType.WORKBENCH_REQUEST:
            await self.show_workbench(payload, request_id=request_id)
            return
        if event_type == ClientEventType.WORKBENCH_REVIEW_REQUEST:
            await self.show_workbench_review(payload, request_id=request_id)
            return
        if event_type == ClientEventType.WORKBENCH_APPROVAL_ACTION:
            await self.resolve_workbench_approval(payload, request_id=request_id)
            return
        if event_type == ClientEventType.WORKBENCH_PROPOSAL_ACTION:
            await self.govern_workbench_proposal(payload, request_id=request_id)
            return
        if event_type == ClientEventType.GOAL_LIFECYCLE_UPDATE:
            await self.start_goal_lifecycle_update(payload, request_id=request_id)
            return
        if event_type == ClientEventType.PURSUIT_RECOVERY_RESUME:
            await self.start_pursuit_recovery(payload, request_id=request_id)
            return
        if event_type == ClientEventType.PURSUIT_TERMINAL_OUTBOX_RUN_NOW:
            await self.start_pursuit_terminal_outbox_run_now(request_id=request_id)
            return
        if event_type == ClientEventType.PURSUIT_TERMINAL_DEAD_LETTER_REQUEUE:
            await self.start_pursuit_terminal_dead_letter_requeue(
                payload,
                request_id=request_id,
            )
            return
        if event_type == ClientEventType.PURSUIT_TERMINAL_DEAD_LETTER_ABANDON:
            await self.start_pursuit_terminal_dead_letter_abandon(
                payload,
                request_id=request_id,
            )
            return
        if event_type == ClientEventType.EVOLUTION_REVIEW_REQUEST:
            await self.show_evolution_review(payload, request_id=request_id)
            return
        if event_type == ClientEventType.EVOLUTION_EVALUATION_LANE_REQUEST:
            await self.show_evolution_evaluation_lane(payload, request_id=request_id)
            return

        if event_type == ClientEventType.RESUME:
            await self.resume_session(payload, request_id=request_id)
            return
        if event_type == ClientEventType.TERMINAL_EVENTS_ACK:
            await self.acknowledge_terminal_events(payload, request_id=request_id)
            return

        if event_type == ClientEventType.SESSIONS_LIST_REQUEST:
            await self.list_sessions(payload, request_id=request_id)
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
        if event_type == ClientEventType.DOCTOR_TRACE:
            await self.show_doctor_trace(payload, request_id=request_id)
            return
        if event_type == ClientEventType.DOCTOR_EXPORT:
            await self.export_doctor_report(payload, request_id=request_id)
            return
        if event_type == ClientEventType.DOCTOR_PROBE:
            await self.start_doctor_live_probe(payload, request_id=request_id)
            return
        if event_type == ClientEventType.DOCTOR_PROBE_CANCEL:
            await self.cancel_doctor_live_probe(payload, request_id=request_id)
            return

        if event_type == ClientEventType.SHUTDOWN:
            try:
                await self.shutdown(request_id=request_id)
            except Exception:
                await self.emit(
                    ServerEventType.SHUTDOWN,
                    {
                        "ok": False,
                        "code": "runtime_shutdown_failed",
                    },
                    request_id=request_id,
                )
                raise
            return

        await self.emit_error(f"未知客户端事件: {event_type}", request_id=request_id)

    async def acknowledge_terminal_events(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Persist one client cursor only inside the current session authority."""
        store = self._terminal_event_store
        if store is None:
            await self.emit_error(
                "终端事件 ACK 权威暂不可用。",
                code="terminal_event_ack_unavailable",
                request_id=request_id,
            )
            return
        current_session_id = str(
            getattr(getattr(self.engine, "_session", None), "id", "") or ""
        ).strip()
        if not current_session_id or payload["session_id"] != current_session_id:
            await self.emit_error(
                "终端事件 ACK 只能确认当前会话。",
                code="terminal_event_ack_session_mismatch",
                request_id=request_id,
            )
            return
        try:
            ack = await store.acknowledge(
                client_id=payload["client_id"],
                session_id=current_session_id,
                stream_id=payload["stream_id"],
                cursor=payload["cursor"],
            )
        except Exception as exc:
            logger.warning("Terminal event ACK failed (%s)", type(exc).__name__)
            await self.emit_error(
                "终端事件 ACK 未被权威日志接受；下次重连将重新核对回执。",
                code="terminal_event_ack_rejected",
                request_id=request_id,
            )
            return
        await self.emit(
            ServerEventType.ACK,
            {
                "event": str(ClientEventType.TERMINAL_EVENTS_ACK),
                "session_id": ack.session_id,
                "stream_id": ack.stream_id,
                "cursor": ack.cursor,
            },
            request_id=request_id,
        )

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
            await self.shutdown(request_id=request_id)
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
                with terminal_run_release_context(
                    self._terminal_runtime_service()
                ):
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
                with terminal_run_release_context(
                    self._terminal_runtime_service()
                ):
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

    async def search_workspace_files(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Search the Engine-owned file index without blocking the control plane."""
        previous = self._workspace_file_search_task
        if previous is not None and not previous.done():
            previous.cancel()
            await asyncio.gather(previous, return_exceptions=True)
        index = getattr(self.engine, "workspace_file_index", None)
        if index is None:
            await self.emit_error(
                "Workspace 文件索引尚未初始化。",
                code="workspace_file_index_unavailable",
                request_id=request_id,
            )
            return

        async def run() -> None:
            try:
                result = await index.search(
                    str(payload.get("query") or ""),
                    limit=int(payload.get("limit", 200)),
                    refresh=bool(payload.get("refresh", False)),
                )
                await self.emit(
                    ServerEventType.WORKSPACE_FILES,
                    workspace_file_search_payload(result),
                    request_id=request_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Workspace file index search failed (%s)",
                    type(exc).__name__,
                )
                await self.emit_error(
                    "Workspace 文件索引读取失败，请检查目录权限后重试。",
                    code="workspace_file_index_failed",
                    request_id=request_id,
                )
            finally:
                if self._workspace_file_search_task is asyncio.current_task():
                    self._workspace_file_search_task = None

        self._workspace_file_search_task = asyncio.create_task(
            run(),
            name=f"workspace-file-search-{request_id}",
        )

    async def cancel_workspace_file_index(self, *, request_id: str) -> None:
        """Cancel the current query and any in-flight background index build."""
        task = self._workspace_file_search_task
        self._workspace_file_search_task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        index = getattr(self.engine, "workspace_file_index", None)
        if index is None:
            await self.emit_error(
                "Workspace 文件索引尚未初始化。",
                code="workspace_file_index_unavailable",
                request_id=request_id,
            )
            return
        result = await index.cancel()
        await self.emit(
            ServerEventType.WORKSPACE_FILES,
            workspace_file_search_payload(result),
            request_id=request_id,
        )

    async def cancel_harness_eval_sandbox(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Apply one durable exact-ticket cancellation and emit its audit receipt."""
        authority = getattr(self.engine, "harness_sandbox_batch_admission", None)
        if authority is None:
            await self.emit_error(
                "Sandbox Batch 取消权威尚未初始化。",
                code="sandbox_batch_cancel_authority_unavailable",
                request_id=request_id,
            )
            return
        try:
            receipt, ticket = await authority.cancel(
                action_id=str(payload["action_id"]),
                ticket_id=str(payload["ticket_id"]),
                authority_key=str(payload["authority_key"]),
                epoch=int(payload["epoch"]),
                expected_state=str(payload["expected_state"]),
                actor_id="new-ui",
                reason=str(payload.get("reason") or "用户请求取消 Sandbox Batch"),
            )
        except Exception as exc:
            self._trace_harness_lookup_failure("eval_sandbox_cancel", exc)
            await self.emit_error(
                "Sandbox Batch 取消请求未能安全裁决，请刷新状态后重试。",
                code=getattr(exc, "code", "sandbox_batch_cancel_unavailable"),
                request_id=request_id,
            )
            return
        await self.emit(
            ServerEventType.HARNESS_EVAL_SANDBOX_CANCEL_RESULT,
            harness_sandbox_cancel_receipt_payload(receipt, ticket),
            request_id=request_id,
        )

    async def start_harness_eval_sandbox_retry(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Run one retry through the authoritative Tool pipeline."""
        if request_id in self._harness_eval_retry_tasks:
            await self.emit_error(
                "该 Sandbox retry 请求正在处理中。",
                code="sandbox_eval_retry_duplicate",
                request_id=request_id,
            )
            return
        if len(self._harness_eval_retry_tasks) >= 4:
            await self.emit_error(
                "并行 Sandbox retry 已达上限（4 个），请等待任一请求完成。",
                code="sandbox_eval_retry_limit",
                request_id=request_id,
            )
            return
        service = getattr(self.engine, "harness_service", None)
        store = getattr(service, "store", None)
        if service is None or store is None:
            await self.emit_error(
                "Sandbox retry 状态权威尚未初始化。",
                code="sandbox_eval_retry_authority_unavailable",
                request_id=request_id,
            )
            return

        async def publish_tool_event(
            event: str,
            data: dict[str, object],
        ) -> None:
            if event == "harness_sandbox_eval_progress":
                await self.emit(
                    ServerEventType.ENGINE_EVENT,
                    {"event": event, "data": data},
                    request_id=request_id,
                )
                await self.emit(
                    ServerEventType.HARNESS_EVAL_BATCH,
                    data,
                    request_id=request_id,
                )
                return
            await self.handle_engine_event(event, dict(data))

        async def emit_result(message: str) -> bool:
            retry = await store.get_sandbox_admission_retry(
                workspace_root=self.engine.workspace_root,
                action_id=str(payload["action_id"]),
            )
            if retry is None:
                return False
            dispatch = await store.get_sandbox_retry_dispatch(
                workspace_root=self.engine.workspace_root,
                retry_action_id=retry.action_id,
            )
            batch_id = ""
            requested = 0
            persisted = 0
            if retry.eval_request_sha256:
                stored = await store.get_sandbox_eval_request(
                    self.engine.workspace_root,
                    retry.eval_request_sha256,
                )
                if stored is not None:
                    batch_id = stored.request.batch_id
                    requested = stored.request.requested_samples
                    records = await store.list_eval_results(
                        self.engine.workspace_root,
                        stored.request.batch_id,
                        stored.request.suite_id,
                        limit=requested + 1,
                    )
                    persisted = len(records)
            await self.emit(
                ServerEventType.HARNESS_EVAL_SANDBOX_RETRY_RESULT,
                harness_sandbox_retry_result_payload(
                    retry,
                    dispatch,
                    batch_id=batch_id,
                    requested_samples=requested,
                    persisted_samples=persisted,
                    message=message,
                ),
                request_id=request_id,
            )
            return True

        async def run() -> None:
            from naumi_agent.tools.base import ToolCall

            try:
                session = await self.engine.get_or_create_session()
                run_id = f"manual:{session.id}"
                result = await self.engine.execute_tool(
                    ToolCall(
                        id=f"new-ui-harness-retry-{uuid4().hex}",
                        name="harness_eval_sandbox_retry",
                        arguments=json.dumps(
                            {
                                "retry_action_id": payload["action_id"],
                                "cancel_receipt_id": payload["cancel_receipt_id"],
                                "cancel_receipt_sha256": payload[
                                    "cancel_receipt_sha256"
                                ],
                                "reason": payload["reason"],
                                "run_id": run_id,
                            },
                            ensure_ascii=False,
                        ),
                    ),
                    on_event=publish_tool_event,
                    agent_name="new-ui",
                )
                if not await emit_result(result.content):
                    await self.emit_error(
                        "Sandbox retry 未产生可审计 authority；请检查权限状态后重试。",
                        code="sandbox_eval_retry_not_authorized",
                        request_id=request_id,
                    )
            except asyncio.CancelledError:
                if self._closed:
                    raise
                if not await emit_result("Sandbox retry 已由用户取消。"):
                    raise
            except Exception as exc:
                self._trace_harness_lookup_failure("eval_sandbox_retry", exc)
                await self.emit_error(
                    "Sandbox retry 未能安全完成，请刷新状态后重试。",
                    code=getattr(exc, "code", "sandbox_eval_retry_unavailable"),
                    request_id=request_id,
                )
            finally:
                self._harness_eval_retry_tasks.pop(request_id, None)

        task = asyncio.create_task(
            run(),
            name=f"harness-eval-sandbox-retry-{request_id}",
        )
        self._harness_eval_retry_tasks[request_id] = task

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
        """Open, recover, refresh, or close the Workbench Timeline subscription."""
        if not bool(payload.get("open", True)):
            await self._stop_workbench_subscription()
            await self.emit(
                ServerEventType.ACK,
                {
                    "event": str(ClientEventType.WORKBENCH_REQUEST),
                    "open": False,
                    "subscribed": False,
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
        subscribe = bool(payload.get("subscribe", False))
        known_timeline_stream_id = str(
            payload.get("known_timeline_stream_id") or ""
        )
        known_timeline_cursor = int(payload.get("known_timeline_cursor") or 0)
        try:
            snapshot = await service.dashboard_snapshot(session_id)
            timeline_stream_id, timeline_cursor = self._validate_workbench_snapshot(
                snapshot,
                session_id=session_id,
            )
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
        same_dashboard = (
            bool(payload.get("known_stream_id"))
            and str(payload.get("known_stream_id")) == str(snapshot.get("stream_id"))
            and int(payload.get("known_revision") or 0)
            == int(snapshot.get("revision") or 0)
        )
        recovery: dict[str, Any] | None = None
        if known_timeline_stream_id or known_timeline_cursor > 0:
            replay_builder = getattr(service, "timeline_replay_window", None)
            if not callable(replay_builder):
                await self.emit_error(
                    "Workbench Timeline 恢复暂不可用，已拒绝猜测缺失事件。",
                    code="workbench_timeline_recovery_unavailable",
                    request_id=request_id,
                )
                return
            try:
                recovery = await replay_builder(
                    session_id,
                    after_cursor=known_timeline_cursor,
                    expected_stream_id=known_timeline_stream_id,
                    limit=_WORKBENCH_TIMELINE_REPLAY_LIMIT,
                )
                self._validate_workbench_recovery(
                    recovery,
                    session_id=session_id,
                    requested_cursor=known_timeline_cursor,
                )
            except Exception as exc:
                logger.warning(
                    "Workbench Timeline recovery failed (%s)",
                    type(exc).__name__,
                )
                await self.emit_error(
                    "Workbench Timeline 恢复窗口无法核验，已改用完整快照。",
                    code="workbench_timeline_recovery_failed",
                    request_id=request_id,
                )
                return

        if recovery is not None and not recovery.get("gap") and same_dashboard:
            for event in list(recovery.get("events") or []):
                await self._emit_workbench_timeline_event(
                    event,
                    stream_id=str(recovery.get("stream_id") or ""),
                    request_id=request_id,
                )
            replayed_events = list(recovery.get("events") or [])
            replay_cursor = (
                int(replayed_events[-1].get("cursor") or 0)
                if replayed_events
                else known_timeline_cursor
            )
            await self.emit(
                ServerEventType.ACK,
                {
                    "event": str(ClientEventType.WORKBENCH_REQUEST),
                    "open": True,
                    "subscribed": subscribe,
                    "revision": int(snapshot.get("revision") or 0),
                    "timeline_recovery": {
                        "mode": "cursor_replay",
                        "stream_id": str(recovery.get("stream_id") or ""),
                        "requested_cursor": known_timeline_cursor,
                        "latest_cursor": int(recovery.get("latest_cursor") or 0),
                        "replayed_count": len(replayed_events),
                    },
                },
                request_id=request_id,
            )
            self._set_workbench_subscription_state(
                subscribed=subscribe,
                session_id=session_id,
                stream_id=str(recovery.get("stream_id") or ""),
                cursor=replay_cursor,
            )
        else:
            if recovery is not None and recovery.get("gap"):
                snapshot["timeline_recovery"] = {
                    "mode": "gap_snapshot",
                    "gap_reason": str(recovery.get("gap_reason") or "unknown"),
                    "requested_cursor": known_timeline_cursor,
                    "earliest_cursor": int(recovery.get("earliest_cursor") or 0),
                    "latest_cursor": int(recovery.get("latest_cursor") or 0),
                }
            else:
                snapshot["timeline_recovery"] = {"mode": "full_snapshot"}
            await self.emit(
                ServerEventType.WORKBENCH_SNAPSHOT,
                snapshot,
                request_id=request_id,
            )
            self._set_workbench_subscription_state(
                subscribed=subscribe,
                session_id=session_id,
                stream_id=timeline_stream_id,
                cursor=timeline_cursor,
            )
        if subscribe:
            self._ensure_workbench_refresh_task()

    def _set_workbench_subscription_state(
        self,
        *,
        subscribed: bool,
        session_id: str,
        stream_id: str,
        cursor: int,
    ) -> None:
        self._workbench_subscribed = subscribed
        self._workbench_session_id = session_id if subscribed else ""
        self._workbench_timeline_stream_id = stream_id if subscribed else ""
        self._workbench_timeline_cursor = cursor if subscribed else 0
        self._workbench_refresh_error_emitted = False

    @staticmethod
    def _validate_workbench_snapshot(
        snapshot: dict[str, Any],
        *,
        session_id: str,
    ) -> tuple[str, int]:
        if (
            not isinstance(snapshot, dict)
            or str(snapshot.get("session_id") or "") != session_id
            or int(snapshot.get("schema_version") or 0) != 1
            or int(snapshot.get("revision") or 0) < 1
            or not str(snapshot.get("stream_id") or "")
            or snapshot.get("full") is not True
        ):
            raise ValueError("invalid Workbench snapshot contract")
        timeline_stream_id = str(snapshot.get("timeline_stream_id") or "")
        timeline_cursor = snapshot.get("timeline_cursor", 0)
        earliest_cursor = snapshot.get("timeline_earliest_cursor", 0)
        if (
            isinstance(timeline_cursor, bool)
            or not isinstance(timeline_cursor, int)
            or timeline_cursor < 0
            or timeline_cursor > 9_007_199_254_740_991
            or isinstance(earliest_cursor, bool)
            or not isinstance(earliest_cursor, int)
            or earliest_cursor < 0
            or earliest_cursor > 9_007_199_254_740_991
            or (timeline_cursor > 0 and not timeline_stream_id)
            or len(timeline_stream_id) > 128
            or any(ord(char) < 32 or ord(char) == 127 for char in timeline_stream_id)
        ):
            raise ValueError("invalid Workbench Timeline snapshot contract")
        return timeline_stream_id, timeline_cursor

    @staticmethod
    def _validate_workbench_recovery(
        recovery: object,
        *,
        session_id: str,
        requested_cursor: int,
    ) -> None:
        if not isinstance(recovery, dict):
            raise ValueError("invalid Workbench Timeline recovery contract")
        stream_id = recovery.get("stream_id")
        events = recovery.get("events")
        cursor_fields = (
            recovery.get("requested_cursor"),
            recovery.get("earliest_cursor"),
            recovery.get("latest_cursor"),
        )
        if (
            recovery.get("schema_version") != 1
            or recovery.get("session_id") != session_id
            or recovery.get("requested_cursor") != requested_cursor
            or not isinstance(stream_id, str)
            or len(stream_id) > 128
            or any(ord(char) < 32 or ord(char) == 127 for char in stream_id)
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or value > 9_007_199_254_740_991
                for value in cursor_fields
            )
            or not isinstance(events, list)
            or len(events) > _WORKBENCH_TIMELINE_REPLAY_LIMIT
            or not isinstance(recovery.get("gap"), bool)
        ):
            raise ValueError("invalid Workbench Timeline recovery contract")
        gap_reason = recovery.get("gap_reason")
        allowed_gap_reasons = {
            "stream_changed",
            "stream_identity_required",
            "cursor_ahead",
            "cursor_before_retention",
            "stream_unavailable",
        }
        if recovery["gap"]:
            if events:
                raise ValueError("Workbench Timeline gap 不得携带增量事件")
            if gap_reason not in allowed_gap_reasons:
                raise ValueError("Workbench Timeline gap_reason 无效")
            return
        if gap_reason != "":
            raise ValueError("Workbench Timeline 连续窗口不得携带 gap_reason")
        if recovery["latest_cursor"] < requested_cursor:
            raise ValueError("Workbench Timeline latest_cursor 落后于请求游标")
        expected_cursor = requested_cursor + 1
        for event in events:
            if (
                not isinstance(event, dict)
                or not str(event.get("id") or "")
                or event.get("session_id") != session_id
                or event.get("cursor") != expected_cursor
            ):
                raise ValueError("Workbench Timeline 增量事件不连续")
            expected_cursor += 1
        if events and events[-1]["cursor"] > recovery["latest_cursor"]:
            raise ValueError("Workbench Timeline 增量事件超出最新游标")

    def _ensure_workbench_refresh_task(self) -> None:
        if self._workbench_refresh_task is not None and not self._workbench_refresh_task.done():
            return
        self._workbench_refresh_task = asyncio.create_task(
            self._workbench_refresh_loop(),
            name="workbench-timeline-refresh",
        )

    async def _stop_workbench_subscription(self) -> None:
        self._workbench_subscribed = False
        self._workbench_session_id = ""
        self._workbench_timeline_stream_id = ""
        self._workbench_timeline_cursor = 0
        task = self._workbench_refresh_task
        self._workbench_refresh_task = None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _workbench_refresh_loop(self) -> None:
        try:
            while self._workbench_subscribed and not self._closed:
                await asyncio.sleep(_WORKBENCH_TIMELINE_POLL_SECONDS)
                await self._refresh_workbench_timeline()
        except asyncio.CancelledError:
            raise
        finally:
            if self._workbench_refresh_task is asyncio.current_task():
                self._workbench_refresh_task = None

    async def _refresh_workbench_timeline(self) -> None:
        if not self._workbench_subscribed or not self._workbench_session_id:
            return
        service = getattr(self.engine, "workbench_service", None)
        replay_builder = getattr(service, "timeline_replay_window", None)
        if not callable(replay_builder):
            return
        try:
            recovery = await replay_builder(
                self._workbench_session_id,
                after_cursor=self._workbench_timeline_cursor,
                expected_stream_id=self._workbench_timeline_stream_id,
                limit=_WORKBENCH_TIMELINE_REPLAY_LIMIT,
            )
            self._validate_workbench_recovery(
                recovery,
                session_id=self._workbench_session_id,
                requested_cursor=self._workbench_timeline_cursor,
            )
            if recovery.get("gap"):
                snapshot = await service.dashboard_snapshot(self._workbench_session_id)
                self._validate_workbench_snapshot(
                    snapshot,
                    session_id=self._workbench_session_id,
                )
                snapshot["timeline_recovery"] = {
                    "mode": "gap_snapshot",
                    "gap_reason": str(recovery.get("gap_reason") or "unknown"),
                    "requested_cursor": self._workbench_timeline_cursor,
                    "earliest_cursor": int(recovery.get("earliest_cursor") or 0),
                    "latest_cursor": int(recovery.get("latest_cursor") or 0),
                }
                await self.emit(ServerEventType.WORKBENCH_SNAPSHOT, snapshot)
                self._workbench_timeline_stream_id = str(
                    snapshot.get("timeline_stream_id") or ""
                )
                self._workbench_timeline_cursor = int(
                    snapshot.get("timeline_cursor") or 0
                )
                return
            for event in list(recovery.get("events") or []):
                await self._emit_workbench_timeline_event(
                    event,
                    stream_id=str(recovery.get("stream_id") or ""),
                )
                self._workbench_timeline_stream_id = str(
                    recovery.get("stream_id") or ""
                )
                self._workbench_timeline_cursor = int(event.get("cursor") or 0)
            self._workbench_refresh_error_emitted = False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Workbench Timeline background refresh failed (%s)",
                type(exc).__name__,
            )
            if not self._workbench_refresh_error_emitted:
                self._workbench_refresh_error_emitted = True
                await self.emit_error(
                    "Workbench Timeline 增量刷新失败；已保留当前快照。",
                    code="workbench_timeline_refresh_failed",
                )

    async def _emit_workbench_timeline_event(
        self,
        event: dict[str, Any],
        *,
        stream_id: str,
        request_id: str | None = None,
    ) -> None:
        event_id = str(event.get("id") or "")
        event_cursor = event.get("cursor")
        session_id = str(event.get("session_id") or "")
        active_session_id = str(
            getattr(getattr(self.engine, "_session", None), "id", "") or ""
        )
        expected_session_id = self._workbench_session_id or active_session_id
        if (
            not event_id
            or not stream_id
            or isinstance(event_cursor, bool)
            or not isinstance(event_cursor, int)
            or event_cursor < 1
            or not expected_session_id
            or session_id != expected_session_id
        ):
            raise ValueError("invalid Workbench Timeline event contract")
        payload = {**event, "stream_id": stream_id, "cursor": event_cursor}
        await self.emit(
            ServerEventType.WORKBENCH_EVENT,
            payload,
            request_id=request_id,
            journal=False,
            event_id=event_id,
            stream_id=stream_id,
            cursor=event_cursor,
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

    async def resolve_workbench_approval(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Apply one permission-governed waiting Approval decision."""
        session = getattr(self.engine, "_session", None)
        if session is None:
            session = await self.engine.get_or_create_session()
        session_id = str(getattr(session, "id", "") or "")
        requested_session_id = str(payload.get("session_id") or "")
        approval_id = str(payload.get("approval_id") or "")
        action = str(payload.get("action") or "")
        decision_note = str(payload.get("decision_note") or "")
        confirmed = payload.get("confirmed") is True
        if requested_session_id and requested_session_id != session_id:
            await self.emit_error(
                "Workbench 只能审批当前会话的 Approval。",
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
            "workbench_resolve_approval",
            {"approval_id": approval_id, "action": action},
        )
        if not decision.allowed:
            await self._emit_workbench_approval_result(
                request_id=request_id,
                session_id=session_id,
                approval_id=approval_id,
                action=action,
                status="blocked",
                message="当前权限模式不允许决策 Approval。",
            )
            return
        if decision.requires_confirmation and not confirmed:
            await self._emit_workbench_approval_result(
                request_id=request_id,
                session_id=session_id,
                approval_id=approval_id,
                action=action,
                status="needs_confirmation",
                message="请确认后再次提交该 Approval 决策。",
            )
            return
        state = (
            ApprovalState.APPROVED
            if action == "approve"
            else ApprovalState.REJECTED
        )
        approval: dict[str, Any] | None = None
        snapshot: dict[str, Any] | None = None
        try:
            approval = await service.resolve_approval(
                session_id=session_id,
                approval_id=approval_id,
                actor="Human",
                state=state,
                decision_note=decision_note,
            )
            if approval is None:
                status = "not_found"
                message = "Approval 不存在或不属于当前会话。"
            else:
                status = "completed"
                message = "Approval 已批准。" if action == "approve" else "Approval 已拒绝。"
                snapshot = await service.dashboard_snapshot(session_id)
        except ApprovalResolutionConflictError as exc:
            status = "conflict"
            message = str(exc)
            snapshot = await service.dashboard_snapshot(session_id)
        except Exception as exc:
            logger.warning("Workbench Approval action failed (%s)", type(exc).__name__)
            status = "error"
            message = str(exc) if isinstance(exc, ValueError) else "Approval 决策暂时失败。"
        await self._emit_workbench_approval_result(
            request_id=request_id,
            session_id=session_id,
            approval_id=approval_id,
            action=action,
            status=status,
            message=message,
            approval=approval,
            snapshot=snapshot,
        )
        if snapshot is not None:
            await self.emit(
                ServerEventType.WORKBENCH_SNAPSHOT,
                snapshot,
                request_id=request_id,
            )

    async def _emit_workbench_approval_result(
        self,
        *,
        request_id: str,
        session_id: str,
        approval_id: str,
        action: str,
        status: str,
        message: str,
        approval: dict[str, Any] | None = None,
        snapshot: dict[str, Any] | None = None,
    ) -> None:
        await self.emit(
            ServerEventType.WORKBENCH_APPROVAL_ACTION_RESULT,
            {
                "schema_version": 1,
                "session_id": session_id,
                "approval_id": approval_id,
                "action": action,
                "status": status,
                "message": message,
                "approval": approval,
                "workbench_snapshot": snapshot,
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
        action_name = str(payload.get("action") or "")
        if action_name == "issue_contract":
            await self.issue_workbench_proposal_contract(
                payload,
                request_id=request_id,
                session_id=session_id,
            )
            return
        action = ProposalAction(action_name)
        decision_note = str(payload.get("decision_note") or "")
        defer_days = payload.get("defer_days", 0)
        merge_into_id = str(payload.get("merge_into_id") or "")
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
                "defer_days": defer_days,
                "merge_into_id": merge_into_id,
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
            defer_until = ""
            if action is ProposalAction.DEFER:
                from naumi_agent.workbench.proposal_governance import (
                    proposal_defer_until_for_preset,
                )

                defer_until = proposal_defer_until_for_preset(defer_days)
            governance_kwargs: dict[str, Any] = {
                "action": action,
                "reviewer": "Human",
                "decision_note": decision_note,
            }
            if defer_until:
                governance_kwargs["defer_until"] = defer_until
            if merge_into_id:
                governance_kwargs["merge_into_id"] = merge_into_id
            proposal = await service.govern_proposal(
                session_id,
                proposal_id,
                **governance_kwargs,
            )
            if proposal is None:
                status = "not_found"
                message = "Proposal 不存在或不属于当前会话。"
                snapshot = None
            else:
                status = "completed"
                if action is ProposalAction.APPROVE:
                    message = "Proposal 已批准。"
                elif action is ProposalAction.REJECT:
                    message = "Proposal 已拒绝。"
                elif action is ProposalAction.DEFER:
                    message = f"Proposal 已延后至 {proposal.get('cooldown_until', '-')}。"
                else:
                    message = f"Proposal 已合并到 {proposal.get('merged_into_id', '-')}。"
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

    async def issue_workbench_proposal_contract(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
        session_id: str,
    ) -> None:
        """Issue or reopen one durable, non-executable Experiment Contract."""
        requested_session_id = str(payload.get("session_id") or "")
        proposal_id = str(payload.get("proposal_id") or "")
        confirmed = payload.get("confirmed") is True
        if requested_session_id and requested_session_id != session_id:
            await self.emit_error(
                "Workbench 只能转换当前会话的 approved Proposal。",
                code="workbench_session_mismatch",
                request_id=request_id,
            )
            return
        issuer = getattr(self.engine, "evolution_experiment_contract_issuer", None)
        store = getattr(self.engine, "evolution_experiment_contract_store", None)
        service = getattr(self.engine, "workbench_service", None)
        if issuer is None or store is None or service is None:
            await self.emit_error(
                "Experiment Contract 签发服务暂不可用。",
                code="experiment_contract_issuer_unavailable",
                request_id=request_id,
            )
            return
        decision = self.engine._permission_checker.check(
            "evolution_issue_experiment_contract",
            {"proposal_id": proposal_id},
        )
        if not decision.allowed:
            await self._emit_experiment_contract_action_result(
                request_id=request_id,
                session_id=session_id,
                proposal_id=proposal_id,
                status="blocked",
                message="当前权限模式不允许签发 Experiment Contract。",
            )
            return
        if decision.requires_confirmation and not confirmed:
            await self._emit_experiment_contract_action_result(
                request_id=request_id,
                session_id=session_id,
                proposal_id=proposal_id,
                status="needs_confirmation",
                message="请确认签发不可执行的 Experiment Contract。",
            )
            return
        try:
            contract = await issuer.issue(
                self.engine.workspace_root,
                session_id=session_id,
                proposal_id=proposal_id,
                seed=default_experiment_seed(proposal_id),
            )
            authority = await store.get(
                self.engine.workspace_root,
                contract.contract_id,
            )
            if authority is None:
                raise RuntimeError("Experiment Contract authority 未持久化。")
            proposal = await service.get_proposal(session_id, proposal_id)
            snapshot = await service.dashboard_snapshot(session_id)
        except EvolutionExperimentContractStoreError as exc:
            logger.warning("Experiment Contract store failed (%s)", exc.code)
            await self._emit_experiment_contract_action_result(
                request_id=request_id,
                session_id=session_id,
                proposal_id=proposal_id,
                status="error",
                message="Experiment Contract 状态库损坏或暂不可用。",
            )
            return
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning(
                "Experiment Contract issue failed (%s)",
                type(exc).__name__,
            )
            message = str(exc) if isinstance(exc, ValueError) else (
                "Experiment Contract 签发暂时失败。"
            )
            await self._emit_experiment_contract_action_result(
                request_id=request_id,
                session_id=session_id,
                proposal_id=proposal_id,
                status="error",
                message=message,
            )
            return
        await self._emit_experiment_contract_action_result(
            request_id=request_id,
            session_id=session_id,
            proposal_id=proposal_id,
            status="completed",
            message=(
                f"Experiment Contract {contract.contract_id} 已持久化；"
                "尚未执行代码或授予发布权限。"
            ),
            proposal=proposal,
            snapshot=snapshot,
            authority=authority,
        )
        await self.emit(
            ServerEventType.WORKBENCH_SNAPSHOT,
            snapshot,
            request_id=request_id,
        )

    async def _emit_experiment_contract_action_result(
        self,
        *,
        request_id: str,
        session_id: str,
        proposal_id: str,
        status: str,
        message: str,
        proposal: dict[str, Any] | None = None,
        snapshot: dict[str, Any] | None = None,
        authority: EvolutionExperimentContractAuthority | None = None,
    ) -> None:
        await self.emit(
            ServerEventType.WORKBENCH_PROPOSAL_ACTION_RESULT,
            {
                "schema_version": 1,
                "session_id": session_id,
                "proposal_id": proposal_id,
                "action": "issue_contract",
                "status": status,
                "message": message,
                "proposal": proposal,
                "workbench_snapshot": snapshot,
                "experiment_contract": (
                    _experiment_contract_public_payload(authority)
                    if authority is not None
                    else None
                ),
            },
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
        subscribe = bool(payload.get("subscribe", True))
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
        if subscribe:
            self._agents_subscribed = True
            self._agents_snapshot = snapshot
        if (
            subscribe
            and was_subscribed
            and int(payload.get("known_revision", 0)) == snapshot.revision
        ):
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
            self._public_agent_snapshot(snapshot),
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

    async def resolve_agent_recovery_unknown(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Apply one exact current-session running-to-unknown recovery fence."""
        session = getattr(self.engine, "_session", None)
        session_id = str(getattr(session, "id", "") or "")
        requested_session_id = str(payload.get("session_id") or "")
        if not session_id or requested_session_id != session_id:
            await self.emit_error(
                "Agent 恢复请求不属于当前会话。",
                code="agents_session_mismatch",
                request_id=request_id,
            )
            return
        result = await self.engine.subagent_manager.resolve_recovery_unknown(
            session_id=session_id,
            job_id=str(payload.get("job_id") or ""),
            expected_request_sha256=str(
                payload.get("request_sha256") or ""
            ),
            expected_claim_epoch=int(payload.get("claim_epoch") or 0),
            expected_latest_receipt_sha256=str(
                payload.get("receipt_sha256") or ""
            ),
        )
        await self.emit(
            ServerEventType.AGENTS_RECOVERY_ACTION_RESULT,
            asdict(result),
            request_id=request_id,
        )
        await self._emit_agents_update()

    async def acknowledge_agent_result(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Acknowledge one exact current-session durable result fence."""
        session = getattr(self.engine, "_session", None)
        session_id = str(getattr(session, "id", "") or "")
        requested_session_id = str(payload.get("session_id") or "")
        if not session_id or requested_session_id != session_id:
            await self.emit_error(
                "Agent 结果确认不属于当前会话。",
                code="agents_session_mismatch",
                request_id=request_id,
            )
            return
        try:
            result = (
                await self.engine.subagent_manager.acknowledge_result_inbox(
                    session_id=session_id,
                    delivery_id=str(payload.get("delivery_id") or ""),
                    expected_delivery_sha256=str(
                        payload.get("delivery_sha256") or ""
                    ),
                )
            )
        except Exception as exc:
            logger.warning(
                "Agent result acknowledgement failed (%s)",
                type(exc).__name__,
            )
            await self.emit_error(
                "当前无法确认结果已读；持久结果和已读状态均未被改写。",
                code="agents_result_ack_unavailable",
                request_id=request_id,
            )
            return
        await self.emit(
            ServerEventType.AGENTS_RESULT_ACKNOWLEDGEMENT,
            asdict(result),
            request_id=request_id,
        )
        await self._emit_agents_update()

    def _public_agent_snapshot(self, snapshot: Any) -> dict[str, Any]:
        """Downgrade result acknowledgement fields for older clients."""
        public = snapshot.to_dict()
        if "agent_result_acknowledgement" in self._client_capabilities:
            return public
        public["schema_version"] = 6
        public["summary"].pop("durable_unread_results", None)
        for result in public["results"]:
            result.pop("acknowledged", None)
            result.pop("acknowledged_at", None)
            result.pop("acknowledgement_receipt_sha256", None)
        return public

    async def _emit_agents_update(self) -> None:
        if not self._agents_subscribed:
            return
        try:
            current = await self.engine.agent_control.snapshot()
            previous = self._agents_snapshot
            if previous is None or previous.session_id != current.session_id:
                self._agents_snapshot = current
                await self.emit(
                    ServerEventType.AGENTS_SNAPSHOT,
                    self._public_agent_snapshot(current),
                )
                return
            changed_sections = self.engine.agent_control.changed_sections(previous, current)
            if not changed_sections and current.revision == previous.revision:
                return
            self._agents_snapshot = current
            if not changed_sections or current.revision != previous.revision + 1:
                await self.emit(
                    ServerEventType.AGENTS_SNAPSHOT,
                    self._public_agent_snapshot(current),
                )
                return
            public = self._public_agent_snapshot(current)
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

        recovery_window = None
        recovery_mode = "legacy_snapshot"
        if "resume_after_cursor" in payload:
            if self._terminal_event_store is None:
                await self.emit_error(
                    "终端事件恢复权威暂不可用，已拒绝猜测缺失回执。",
                    code="terminal_event_recovery_unavailable",
                    request_id=request_id,
                )
                return
            try:
                recovery_window = await self._terminal_event_store.replay_window(
                    client_id=payload["terminal_event_client_id"],
                    session_id=session_id,
                    cursor=payload["resume_after_cursor"],
                    expected_stream_id=payload["terminal_event_stream_id"],
                )
            except Exception as exc:
                logger.warning(
                    "Terminal event recovery planning failed (%s)",
                    type(exc).__name__,
                )
                await self.emit_error(
                    "终端事件恢复窗口无法核验，已拒绝静默跳过回执。",
                    code="terminal_event_recovery_failed",
                    request_id=request_id,
                )
                return
            recovery_mode = (
                "gap_snapshot" if recovery_window.gap else "cursor_replay"
            )

        loaded = await self.engine.load_session(session_id)
        if not loaded:
            await self.emit_error(
                f"会话不存在: {session_id}",
                code="session_not_found",
                request_id=request_id,
            )
            return

        await self._stop_workbench_subscription()
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
                "terminal_event_recovery": (
                    {
                        "mode": recovery_mode,
                        "stream_id": recovery_window.stream_id,
                        "requested_cursor": recovery_window.requested_cursor,
                        "earliest_cursor": recovery_window.earliest_cursor,
                        "latest_cursor": recovery_window.latest_cursor,
                        "gap_reason": recovery_window.gap_reason,
                    }
                    if recovery_window is not None
                    else {"mode": recovery_mode}
                ),
            },
            request_id=request_id,
        )
        for message in replay_messages(raw_messages):
            await self.emit(ServerEventType.UI_MESSAGE, ui_message_payload(message))
        if recovery_mode == "cursor_replay":
            for record in recovery_window.records:
                await self._emit_replayed_terminal_event(
                    record,
                    request_id=request_id,
                )
        else:
            snapshot_without_journal = recovery_mode == "gap_snapshot"
            harness_snapshot_complete = await self._resume_harness_receipts(
                session_id,
                request_id=request_id,
                journal=not snapshot_without_journal,
                required=snapshot_without_journal,
            )
            if snapshot_without_journal and not harness_snapshot_complete:
                return
            run_store = getattr(self.engine, "chat_run_store", None)
            if run_store is None and snapshot_without_journal:
                await self.emit_error(
                    "通用完成回执权威暂不可用，未建立新的恢复游标基线。",
                    code="terminal_event_snapshot_authority_unavailable",
                    request_id=request_id,
                )
                return
            if run_store is not None:
                try:
                    runs = await run_store.list_runs(session_id, limit=200)
                except Exception as exc:
                    logger.warning(
                        "Completion receipt snapshot failed (%s)",
                        type(exc).__name__,
                    )
                    await self.emit_error(
                        "通用完成回执快照读取失败，未建立新的恢复游标基线。",
                        code="terminal_event_snapshot_failed",
                        request_id=request_id,
                    )
                    return
                for run in reversed(runs):
                    if run.receipt is not None:
                        await self.emit(
                            ServerEventType.COMPLETION_RECEIPT,
                            run.receipt.to_dict(),
                            request_id=request_id,
                            journal=not snapshot_without_journal,
                        )
        if recovery_window is not None:
            await self.emit(
                ServerEventType.TERMINAL_EVENTS_RECOVERY,
                {
                    "schema_version": 1,
                    "session_id": session_id,
                    "mode": (
                        "snapshot_complete"
                        if recovery_mode == "gap_snapshot"
                        else "replay_complete"
                    ),
                    "stream_id": recovery_window.stream_id,
                    "requested_cursor": recovery_window.requested_cursor,
                    "earliest_cursor": recovery_window.earliest_cursor,
                    "latest_cursor": recovery_window.latest_cursor,
                    "gap_reason": recovery_window.gap_reason,
                    "replayed_count": len(recovery_window.records),
                },
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
        journal: bool = True,
        required: bool = False,
    ) -> bool:
        """Replay durable Harness receipts before their generic completion cards."""
        service = getattr(self.engine, "harness_service", None)
        store = getattr(service, "store", None)
        if store is None:
            if required:
                await self.emit_error(
                    "Harness 回执权威暂不可用，未建立新的恢复游标基线。",
                    code="terminal_event_snapshot_authority_unavailable",
                    request_id=request_id,
                )
                return False
            return True
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
            return False

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
                journal=journal,
            )
        return True

    async def _run_cli_slash_command(self, cmd: str, *, request_id: str) -> None:
        """Execute a slash command through the legacy CLI command handlers."""
        from naumi_agent.cli.slash_router import execute_slash_command

        parse_reasoning_toggle = None
        try:
            from naumi_agent.main import _parse_reasoning_toggle as parse_reasoning_toggle
        except Exception:
            parse_reasoning_toggle = None

        try:
            frontend = (
                _BridgeHarnessSandboxFrontend(self, request_id)
                if re.match(
                    r"^/harness\s+eval\s+sandbox(?:\s|$)",
                    cmd,
                    re.IGNORECASE,
                )
                else None
            )
            output = await execute_slash_command(
                self.engine,
                cmd,
                frontend=frontend,
            )
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

    async def list_sessions(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Emit one workspace-scoped, bounded, read-only session snapshot."""
        from naumi_agent.ui.session_list import build_session_list_snapshot

        try:
            snapshot = await build_session_list_snapshot(
                self.engine,
                page=int(payload["page"]),
                page_size=int(payload["page_size"]),
                query=str(payload["query"]),
            )
        except Exception:
            logger.exception("Session list projection failed")
            await self.emit_error(
                "暂时无法读取当前工作区的会话列表，请稍后重试。",
                code="session_list_failed",
                request_id=request_id,
            )
            return
        await self.emit(
            ServerEventType.SESSIONS_LIST,
            snapshot.to_protocol_dict(),
            request_id=request_id,
        )

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

        disposed_cursor = str(
            payload.get("terminal_outbox_disposed_cursor") or ""
        )
        if (
            disposed_cursor
            and "goal_terminal_outbox_disposed_cursor"
            not in self._client_capabilities
        ):
            await self.emit_error(
                "当前终端版本不支持终态历史翻页，请升级后重试。",
                code="goal_disposed_cursor_unsupported",
                request_id=request_id,
            )
            return
        harness_service = getattr(self.engine, "harness_service", None)
        snapshot = await build_goal_pursuit_snapshot_with_recovery(
            self.engine.goal_store,
            self.engine.pursuit_store,
            getattr(harness_service, "store", None),
            workspace_root=self.engine.workspace_root,
            limit=int(payload.get("limit", 20)),
            include_finished=bool(payload.get("include_finished", True)),
            selected_goal_id=str(payload.get("selected_goal_id") or ""),
            interaction_limit=int(payload.get("interaction_limit", 10)),
            interaction_filter=str(payload.get("interaction_filter") or "all"),
            interaction_cursor=str(payload.get("interaction_cursor") or ""),
            selected_interaction_id=str(
                payload.get("selected_interaction_id") or ""
            ),
            terminal_outbox_disposed_cursor=disposed_cursor,
            terminal_outbox_enabled=(
                bool(getattr(self.engine, "pursuit_terminal_outbox_enabled", False))
            ),
            terminal_outbox_worker_snapshot=(
                getattr(
                    self.engine,
                    "pursuit_terminal_outbox_worker_snapshot",
                    None,
                )
            ),
        )
        if "goal_snapshot" in self._client_capabilities:
            public_snapshot = snapshot.to_protocol_dict()
            if (
                "goal_terminal_outbox_disposed_cursor"
                not in self._client_capabilities
            ):
                terminal_outbox = public_snapshot.get("terminal_outbox")
                if isinstance(terminal_outbox, dict):
                    terminal_outbox["schema_version"] = 4
                    for field in (
                        "disposed_cursor",
                        "disposed_next_cursor",
                        "disposed_has_more",
                        "disposed_warning",
                    ):
                        terminal_outbox.pop(field, None)
            await self.emit(
                ServerEventType.GOALS_SNAPSHOT,
                public_snapshot,
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

    async def start_goal_lifecycle_update(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Run one reversible Goal transition through the shared ToolExecution path."""
        from naumi_agent.orchestrator.goal_store import GoalStatus

        goal_id = str(payload.get("goal_id") or "")
        action = str(payload.get("action") or "")
        if action not in {"pause", "resume"}:
            await self.emit_error(
                "Goal 操作无效，请刷新页面后重试。",
                code="bad_request",
                request_id=request_id,
            )
            return
        target = GoalStatus.PAUSED if action == "pause" else GoalStatus.ACTIVE
        required_source = GoalStatus.ACTIVE if action == "pause" else GoalStatus.PAUSED
        if request_id in self._goal_lifecycle_tasks:
            await self._emit_goal_lifecycle_action_result(
                goal_id=goal_id,
                action=action,
                request_id=request_id,
                status="blocked",
                code="duplicate_request",
                message="该 Goal 操作正在处理中，请等待当前结果。",
            )
            return
        if len(self._goal_lifecycle_tasks) >= 4:
            await self._emit_goal_lifecycle_action_result(
                goal_id=goal_id,
                action=action,
                request_id=request_id,
                status="blocked",
                code="action_capacity_reached",
                message="当前 Goal 控制通道已满，请稍后重试。",
            )
            return
        goal_store = getattr(self.engine, "goal_store", None)
        try:
            goal = goal_store.get(goal_id) if goal_store is not None else None
        except Exception:
            await self._emit_goal_lifecycle_action_result(
                goal_id=goal_id,
                action=action,
                request_id=request_id,
                status="error",
                code="goal_store_unavailable",
                message="Goal 状态库暂不可用，请运行 `/doctor` 后重试。",
            )
            return
        if goal is None:
            await self._emit_goal_lifecycle_action_result(
                goal_id=goal_id,
                action=action,
                request_id=request_id,
                status="not_found",
                code="goal_not_found",
                message="未找到该 Goal，未执行状态变更。",
            )
            return
        if goal.status is not required_source:
            message = (
                f"当前 Goal 为 {goal.status.value}，"
                f"仅允许从 {required_source.value} 执行 {action}。"
            )
            await self._emit_goal_lifecycle_action_result(
                goal_id=goal_id,
                action=action,
                request_id=request_id,
                status="conflict",
                code="state_conflict",
                message=message,
                goal_status=goal.status.value,
            )
            return

        async def publish_tool_event(event: str, data: dict[str, object]) -> None:
            await self.handle_engine_event(event, dict(data))

        async def publish_authoritative_result(
            *,
            fallback_status: str,
            fallback_code: str,
            fallback_message: str,
        ) -> None:
            try:
                refreshed = goal_store.get(goal_id)
            except Exception:
                refreshed = None
                fallback_status = "error"
                fallback_code = "goal_store_unavailable"
                fallback_message = "Goal 状态库暂不可用，请运行 `/doctor` 后重试。"
            if refreshed is not None and refreshed.status is target:
                await self._emit_goal_lifecycle_action_result(
                    goal_id=goal_id,
                    action=action,
                    request_id=request_id,
                    status="completed",
                    code="goal_paused" if action == "pause" else "goal_resumed",
                    message=(
                        "Goal 已暂停，可随时恢复。"
                        if action == "pause"
                        else "Goal 已恢复为进行中。"
                    ),
                    goal_status=refreshed.status.value,
                )
            else:
                await self._emit_goal_lifecycle_action_result(
                    goal_id=goal_id,
                    action=action,
                    request_id=request_id,
                    status=fallback_status,
                    code=fallback_code,
                    message=fallback_message,
                    goal_status=(refreshed.status.value if refreshed is not None else ""),
                )

        async def run() -> None:
            from naumi_agent.tools.base import ToolCall

            try:
                await self.engine.get_or_create_session()
                result = await self.engine.execute_tool(
                    ToolCall(
                        id=f"new-ui-goal-{action}-{uuid4().hex}",
                        name="goal_update",
                        arguments=json.dumps(
                            {
                                "goal_id": goal_id,
                                "status": target.value,
                                "note": goal.note,
                            },
                            ensure_ascii=False,
                        ),
                    ),
                    on_event=publish_tool_event,
                    agent_name="new-ui",
                )
                await publish_authoritative_result(
                    fallback_status=("blocked" if result.status == "error" else "conflict"),
                    fallback_code=(
                        "tool_execution_rejected"
                        if result.status == "error"
                        else "state_not_applied"
                    ),
                    fallback_message=("Goal 状态未变更；请检查权限回执后刷新页面。"),
                )
                try:
                    await self.show_goal_panel(
                        {"selected_goal_id": goal_id},
                        request_id=request_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "Goal lifecycle snapshot refresh failed (%s)",
                        type(exc).__name__,
                    )
                    await self.emit_error(
                        "Goal 状态已处理，但页面刷新失败；请按 `r` 重新读取。",
                        code="goal_snapshot_refresh_failed",
                        request_id=request_id,
                    )
            except asyncio.CancelledError:
                if self._closed:
                    raise
                await publish_authoritative_result(
                    fallback_status="error",
                    fallback_code="cancelled",
                    fallback_message="Goal 操作已取消，请刷新页面确认持久状态。",
                )
            except Exception as exc:
                logger.warning("Goal lifecycle UI action failed (%s)", type(exc).__name__)
                await publish_authoritative_result(
                    fallback_status="error",
                    fallback_code="internal_error",
                    fallback_message="Goal 操作未能安全完成，请刷新状态或运行 `/doctor`。",
                )
            finally:
                self._goal_lifecycle_tasks.pop(request_id, None)

        task = asyncio.create_task(run(), name=f"goal-lifecycle-{request_id}")
        self._goal_lifecycle_tasks[request_id] = task

    async def _emit_goal_lifecycle_action_result(
        self,
        *,
        goal_id: str,
        action: str,
        request_id: str,
        status: str,
        code: str,
        message: str,
        goal_status: str = "",
    ) -> None:
        await self.emit(
            ServerEventType.GOAL_LIFECYCLE_ACTION_RESULT,
            {
                "schema_version": 1,
                "goal_id": goal_id,
                "action": action,
                "status": status,
                "code": code,
                "message": _bounded_action_message(message),
                "goal_status": goal_status,
            },
            request_id=request_id,
        )

    async def start_pursuit_recovery(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Run one controlled Pursuit resume without blocking UI confirmations."""
        run_id = str(payload.get("run_id") or "")
        if request_id in self._pursuit_recovery_tasks:
            await self._emit_pursuit_recovery_action_result(
                run_id=run_id,
                request_id=request_id,
                status="blocked",
                code="duplicate_request",
                message="该恢复请求正在处理中，请等待当前结果。",
            )
            return
        if len(self._pursuit_recovery_tasks) >= 4:
            await self._emit_pursuit_recovery_action_result(
                run_id=run_id,
                request_id=request_id,
                status="blocked",
                code="recovery_capacity_reached",
                message="当前恢复控制通道已满，请稍后重试。",
            )
            return
        recovery = await self._pursuit_recovery_snapshot_for_run(run_id)
        if recovery is None:
            await self._emit_pursuit_recovery_action_result(
                run_id=run_id,
                request_id=request_id,
                status="blocked",
                code="run_not_found",
                message="未找到该 Pursuit，未发起恢复。",
            )
            return
        if recovery.resume_action.state != "available":
            await self._emit_pursuit_recovery_action_result(
                run_id=run_id,
                request_id=request_id,
                status="blocked",
                code=recovery.resume_action.code,
                message=recovery.resume_action.reason,
                recovery=recovery,
            )
            return

        async def publish_tool_event(
            event: str,
            data: dict[str, object],
        ) -> None:
            await self.handle_engine_event(event, dict(data))

        async def run() -> None:
            from naumi_agent.tools.base import ToolCall

            tool_call_id = f"new-ui-pursuit-resume-{uuid4().hex}"
            attempt_id = pursuit_recovery_attempt_id(
                run_id=run_id,
                source_request_id=tool_call_id,
            )
            try:
                await self.engine.get_or_create_session()
                result = await self.engine.execute_tool(
                    ToolCall(
                        id=tool_call_id,
                        name="pursuit_resume",
                        arguments=json.dumps(
                            {"run_id": run_id},
                            ensure_ascii=False,
                        ),
                    ),
                    on_event=publish_tool_event,
                    agent_name="new-ui",
                )
                refreshed = await self._pursuit_recovery_snapshot_for_run(run_id)
                attempt = next(
                    (
                        item
                        for item in (refreshed.attempts if refreshed is not None else ())
                        if item.attempt_id == attempt_id
                    ),
                    None,
                )
                if attempt is None:
                    status = "blocked" if result.status == "error" else "error"
                    code = (
                        "tool_execution_rejected"
                        if result.status == "error"
                        else "attempt_authority_missing"
                    )
                else:
                    status = attempt.state
                    code = attempt.result_code or attempt.state
                await self._emit_pursuit_recovery_action_result(
                    run_id=run_id,
                    request_id=request_id,
                    status=status,
                    code=code,
                    message=_bounded_action_message(result.content),
                    attempt=(
                        attempt.model_dump(mode="json")
                        if attempt is not None
                        else None
                    ),
                    recovery=refreshed,
                )
                await self.show_goal_panel({}, request_id=request_id)
            except asyncio.CancelledError:
                if self._closed:
                    raise
                refreshed, attempt = await self._pursuit_recovery_attempt_for_run(
                    run_id,
                    attempt_id,
                )
                await self._emit_pursuit_recovery_action_result(
                    run_id=run_id,
                    request_id=request_id,
                    status=attempt.state if attempt is not None else "error",
                    code=(
                        attempt.result_code or attempt.state
                        if attempt is not None
                        else "cancelled"
                    ),
                    message="恢复请求已取消，请刷新 Goal 页面确认持久账本。",
                    attempt=(
                        attempt.model_dump(mode="json")
                        if attempt is not None
                        else None
                    ),
                    recovery=refreshed,
                )
            except Exception as exc:
                logger.warning(
                    "Pursuit recovery UI action failed (%s)",
                    type(exc).__name__,
                )
                refreshed, attempt = await self._pursuit_recovery_attempt_for_run(
                    run_id,
                    attempt_id,
                )
                await self._emit_pursuit_recovery_action_result(
                    run_id=run_id,
                    request_id=request_id,
                    status=attempt.state if attempt is not None else "error",
                    code=(
                        attempt.result_code or attempt.state
                        if attempt is not None
                        else "internal_error"
                    ),
                    message="恢复动作未能安全完成，请刷新状态或运行 `/doctor`。",
                    attempt=(
                        attempt.model_dump(mode="json")
                        if attempt is not None
                        else None
                    ),
                    recovery=refreshed,
                )
            finally:
                self._pursuit_recovery_tasks.pop(request_id, None)

        task = asyncio.create_task(
            run(),
            name=f"pursuit-recovery-{request_id}",
        )
        self._pursuit_recovery_tasks[request_id] = task

    async def _pursuit_recovery_snapshot_for_run(self, run_id: str) -> Any | None:
        from naumi_agent.ui.pursuit_recovery import (
            build_pursuit_recovery_snapshot,
        )

        pursuit_store = getattr(self.engine, "pursuit_store", None)
        if pursuit_store is None:
            return None
        run = pursuit_store.get_run(run_id)
        if run is None:
            return None
        harness_service = getattr(self.engine, "harness_service", None)
        return await build_pursuit_recovery_snapshot(
            run,
            pursuit_store,
            getattr(harness_service, "store", None),
            workspace_root=self.engine.workspace_root,
        )

    async def _pursuit_recovery_attempt_for_run(
        self,
        run_id: str,
        attempt_id: str,
    ) -> tuple[Any | None, Any | None]:
        """Best-effort public attempt lookup for exceptional action exits."""
        try:
            recovery = await self._pursuit_recovery_snapshot_for_run(run_id)
        except Exception:
            return None, None
        attempt = next(
            (
                item
                for item in (recovery.attempts if recovery is not None else ())
                if item.attempt_id == attempt_id
            ),
            None,
        )
        return recovery, attempt

    async def _emit_pursuit_recovery_action_result(
        self,
        *,
        run_id: str,
        request_id: str,
        status: str,
        code: str,
        message: str,
        attempt: dict[str, Any] | None = None,
        recovery: Any | None = None,
    ) -> None:
        await self.emit(
            ServerEventType.PURSUIT_RECOVERY_ACTION_RESULT,
            {
                "schema_version": 1,
                "run_id": run_id,
                "status": status,
                "code": code,
                "message": _bounded_action_message(message),
                "attempt": attempt,
                "resume_action": (
                    recovery.resume_action.model_dump(mode="json")
                    if recovery is not None
                    else None
                ),
            },
            request_id=request_id,
        )

    async def start_pursuit_terminal_outbox_run_now(
        self,
        *,
        request_id: str,
    ) -> None:
        """Execute one explicit outbox pass through ToolExecution authority."""
        if request_id in self._pursuit_terminal_outbox_tasks:
            return
        if self._pursuit_terminal_outbox_tasks:
            await self._emit_pursuit_terminal_outbox_action_result(
                request_id=request_id,
                status="blocked",
                code="operation_busy",
                message="已有一轮终态队列恢复正在执行，请等待其回执。",
            )
            return
        if not bool(getattr(self.engine, "pursuit_terminal_outbox_enabled", False)):
            await self._emit_pursuit_terminal_outbox_action_result(
                request_id=request_id,
                status="blocked",
                code="terminal_outbox_disabled",
                message="Pursuit 终态 outbox worker 当前未启用。",
            )
            return

        async def publish_tool_event(
            event: str,
            data: dict[str, object],
        ) -> None:
            await self.handle_engine_event(event, dict(data))

        async def run() -> None:
            from naumi_agent.tools.base import ToolCall

            tool_call_id = (
                "new-ui-terminal-outbox-"
                + hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:32]
            )
            try:
                await self.engine.get_or_create_session()
                result = await self.engine.execute_tool(
                    ToolCall(
                        id=tool_call_id,
                        name="pursuit_terminal_outbox_run_now",
                        arguments="{}",
                    ),
                    on_event=publish_tool_event,
                    agent_name="new-ui",
                )
                request_sha256 = hashlib.sha256(
                    tool_call_id.encode("utf-8")
                ).hexdigest()
                receipt = self.engine.pursuit_store.get_terminal_outbox_run_receipt(
                    request_sha256
                )
                if receipt is None:
                    status = "blocked" if result.status == "error" else "error"
                    code = (
                        "tool_execution_rejected"
                        if result.status == "error"
                        else "receipt_authority_missing"
                    )
                else:
                    status = receipt.status.value
                    code = receipt.status.value
                await self._emit_pursuit_terminal_outbox_action_result(
                    request_id=request_id,
                    status=status,
                    code=code,
                    message=_bounded_action_message(result.content),
                    receipt=(
                        receipt.model_dump(
                            mode="json",
                            exclude={"source_request_sha256"},
                        )
                        if receipt is not None
                        else None
                    ),
                )
                await self.show_goal_panel({}, request_id=request_id)
            except asyncio.CancelledError:
                if self._closed:
                    raise
                await self._emit_pursuit_terminal_outbox_action_result(
                    request_id=request_id,
                    status="error",
                    code="cancelled",
                    message="终态队列恢复请求已取消，请刷新 Goal 页面确认权威状态。",
                )
            except Exception as exc:
                logger.warning(
                    "Terminal outbox UI action failed (%s)",
                    type(exc).__name__,
                )
                await self._emit_pursuit_terminal_outbox_action_result(
                    request_id=request_id,
                    status="error",
                    code="internal_error",
                    message="终态队列恢复未能安全完成，请刷新状态或运行 `/doctor`。",
                )
            finally:
                self._pursuit_terminal_outbox_tasks.pop(request_id, None)

        task = asyncio.create_task(
            run(),
            name=f"pursuit-terminal-outbox-{request_id}",
        )
        self._pursuit_terminal_outbox_tasks[request_id] = task

    async def _emit_pursuit_terminal_outbox_action_result(
        self,
        *,
        request_id: str,
        status: str,
        code: str,
        message: str,
        receipt: dict[str, Any] | None = None,
    ) -> None:
        await self.emit(
            ServerEventType.PURSUIT_TERMINAL_OUTBOX_ACTION_RESULT,
            {
                "schema_version": 1,
                "status": status,
                "code": code,
                "message": _bounded_action_message(message),
                "receipt": receipt,
            },
            request_id=request_id,
        )

    async def start_pursuit_terminal_dead_letter_requeue(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Requeue one selected dead letter through ToolExecution authority."""
        dead_letter_id = str(payload.get("dead_letter_id") or "").strip()
        if not re.fullmatch(r"ptfail_[0-9a-f]{24}", dead_letter_id):
            await self._emit_pursuit_terminal_dead_letter_requeue_result(
                request_id=request_id,
                dead_letter_id=dead_letter_id,
                status="blocked",
                code="invalid_dead_letter_id",
                message="死信重入队目标无效，请刷新 Goal 页面后重新选择。",
            )
            return
        if request_id in self._pursuit_terminal_outbox_tasks:
            return
        if self._pursuit_terminal_outbox_tasks:
            await self._emit_pursuit_terminal_dead_letter_requeue_result(
                request_id=request_id,
                dead_letter_id=dead_letter_id,
                status="blocked",
                code="operation_busy",
                message="已有终态队列控制动作正在执行，请等待其回执。",
            )
            return

        async def publish_tool_event(
            event: str,
            data: dict[str, object],
        ) -> None:
            await self.handle_engine_event(event, dict(data))

        async def run() -> None:
            from naumi_agent.tools.base import ToolCall

            tool_call_id = (
                "new-ui-terminal-requeue-"
                + hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:32]
            )
            try:
                await self.engine.get_or_create_session()
                result = await self.engine.execute_tool(
                    ToolCall(
                        id=tool_call_id,
                        name="pursuit_terminal_dead_letter_requeue",
                        arguments=json.dumps(
                            {"dead_letter_id": dead_letter_id},
                            ensure_ascii=False,
                        ),
                    ),
                    on_event=publish_tool_event,
                    agent_name="new-ui",
                )
                receipt = self.engine.pursuit_store.get_terminal_dead_letter_requeue(
                    dead_letter_id
                )
                if receipt is None:
                    status = "blocked" if result.status == "error" else "error"
                    code = (
                        "tool_execution_rejected"
                        if result.status == "error"
                        else "receipt_authority_missing"
                    )
                else:
                    status = "requeued"
                    code = "requeued"
                await self._emit_pursuit_terminal_dead_letter_requeue_result(
                    request_id=request_id,
                    dead_letter_id=dead_letter_id,
                    status=status,
                    code=code,
                    message=_bounded_action_message(result.content),
                    receipt=(
                        receipt.model_dump(
                            mode="json",
                            exclude={
                                "source_request_sha256",
                                "prior_failure_sha256",
                                "dispatch_before_sha256",
                                "dispatch_after_sha256",
                            },
                        )
                        if receipt is not None
                        else None
                    ),
                )
                await self.show_goal_panel({}, request_id=request_id)
            except asyncio.CancelledError:
                if self._closed:
                    raise
                await self._emit_pursuit_terminal_dead_letter_requeue_result(
                    request_id=request_id,
                    dead_letter_id=dead_letter_id,
                    status="error",
                    code="cancelled",
                    message="死信重入队请求已取消，请刷新 Goal 页面确认权威状态。",
                )
            except Exception as exc:
                logger.warning(
                    "Terminal dead-letter requeue UI action failed (%s)",
                    type(exc).__name__,
                )
                await self._emit_pursuit_terminal_dead_letter_requeue_result(
                    request_id=request_id,
                    dead_letter_id=dead_letter_id,
                    status="error",
                    code="internal_error",
                    message="死信未能安全重入队，请刷新状态或运行 `/doctor`。",
                )
            finally:
                self._pursuit_terminal_outbox_tasks.pop(request_id, None)

        task = asyncio.create_task(
            run(),
            name=f"pursuit-terminal-requeue-{request_id}",
        )
        self._pursuit_terminal_outbox_tasks[request_id] = task

    async def _emit_pursuit_terminal_dead_letter_requeue_result(
        self,
        *,
        request_id: str,
        dead_letter_id: str,
        status: str,
        code: str,
        message: str,
        receipt: dict[str, Any] | None = None,
    ) -> None:
        await self.emit(
            ServerEventType.PURSUIT_TERMINAL_DEAD_LETTER_REQUEUE_RESULT,
            {
                "schema_version": 1,
                "dead_letter_id": dead_letter_id,
                "status": status,
                "code": code,
                "message": _bounded_action_message(message),
                "receipt": receipt,
            },
            request_id=request_id,
        )

    async def start_pursuit_terminal_dead_letter_abandon(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Permanently stop one selected dead letter through ToolExecution."""
        dead_letter_id = str(payload.get("dead_letter_id") or "").strip()
        reason = str(payload.get("reason") or "").strip()
        reasons = {
            "no_longer_required",
            "superseded",
            "external_resolution",
            "invalid_target",
        }
        if (
            not re.fullmatch(r"ptfail_[0-9a-f]{24}", dead_letter_id)
            or reason not in reasons
        ):
            await self._emit_pursuit_terminal_dead_letter_abandon_result(
                request_id=request_id,
                dead_letter_id=dead_letter_id,
                status="blocked",
                code="invalid_abandon_target",
                message="死信放弃目标或原因无效，请刷新 Goal 页面后重新选择。",
            )
            return
        if request_id in self._pursuit_terminal_outbox_tasks:
            return
        if self._pursuit_terminal_outbox_tasks:
            await self._emit_pursuit_terminal_dead_letter_abandon_result(
                request_id=request_id,
                dead_letter_id=dead_letter_id,
                status="blocked",
                code="operation_busy",
                message="已有终态队列控制动作正在执行，请等待其回执。",
            )
            return

        async def publish_tool_event(
            event: str,
            data: dict[str, object],
        ) -> None:
            await self.handle_engine_event(event, dict(data))

        async def run() -> None:
            from naumi_agent.tools.base import ToolCall

            tool_call_id = (
                "new-ui-terminal-abandon-"
                + hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:32]
            )
            try:
                await self.engine.get_or_create_session()
                result = await self.engine.execute_tool(
                    ToolCall(
                        id=tool_call_id,
                        name="pursuit_terminal_dead_letter_abandon",
                        arguments=json.dumps(
                            {"dead_letter_id": dead_letter_id, "reason": reason},
                            ensure_ascii=False,
                        ),
                    ),
                    on_event=publish_tool_event,
                    agent_name="new-ui",
                )
                receipt = self.engine.pursuit_store.get_terminal_dead_letter_abandon(
                    dead_letter_id
                )
                if receipt is None:
                    status = "blocked" if result.status == "error" else "error"
                    code = (
                        "tool_execution_rejected"
                        if result.status == "error"
                        else "receipt_authority_missing"
                    )
                else:
                    status = "abandoned"
                    code = "abandoned"
                await self._emit_pursuit_terminal_dead_letter_abandon_result(
                    request_id=request_id,
                    dead_letter_id=dead_letter_id,
                    status=status,
                    code=code,
                    message=_bounded_action_message(result.content),
                    receipt=(
                        receipt.model_dump(
                            mode="json",
                            exclude={
                                "source_request_sha256",
                                "prior_failure_sha256",
                                "dispatch_sha256",
                            },
                        )
                        if receipt is not None
                        else None
                    ),
                )
                await self.show_goal_panel({}, request_id=request_id)
            except asyncio.CancelledError:
                if self._closed:
                    raise
                await self._emit_pursuit_terminal_dead_letter_abandon_result(
                    request_id=request_id,
                    dead_letter_id=dead_letter_id,
                    status="error",
                    code="cancelled",
                    message="死信放弃请求已取消，请刷新 Goal 页面确认权威状态。",
                )
            except Exception as exc:
                logger.warning(
                    "Terminal dead-letter abandon UI action failed (%s)",
                    type(exc).__name__,
                )
                await self._emit_pursuit_terminal_dead_letter_abandon_result(
                    request_id=request_id,
                    dead_letter_id=dead_letter_id,
                    status="error",
                    code="internal_error",
                    message="死信未能安全放弃，请刷新状态或运行 `/doctor`。",
                )
            finally:
                self._pursuit_terminal_outbox_tasks.pop(request_id, None)

        task = asyncio.create_task(
            run(),
            name=f"pursuit-terminal-abandon-{request_id}",
        )
        self._pursuit_terminal_outbox_tasks[request_id] = task

    async def _emit_pursuit_terminal_dead_letter_abandon_result(
        self,
        *,
        request_id: str,
        dead_letter_id: str,
        status: str,
        code: str,
        message: str,
        receipt: dict[str, Any] | None = None,
    ) -> None:
        await self.emit(
            ServerEventType.PURSUIT_TERMINAL_DEAD_LETTER_ABANDON_RESULT,
            {
                "schema_version": 1,
                "dead_letter_id": dead_letter_id,
                "status": status,
                "code": code,
                "message": _bounded_action_message(message),
                "receipt": receipt,
            },
            request_id=request_id,
        )

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
        from naumi_agent.evolution.capability_artifact import (
            CapabilityArtifactError,
            render_capability_artifact,
        )
        from naumi_agent.evolution.capability_governance import (
            CapabilityGovernanceError,
            render_capability_governance,
        )
        from naumi_agent.evolution.capability_sandbox_request import (
            CapabilitySandboxRequestError,
            render_capability_sandbox_request,
        )
        from naumi_agent.evolution.capability_scenario_binding import (
            CapabilityScenarioBindingError,
            render_capability_scenario_binding,
        )
        from naumi_agent.evolution.capability_specification import (
            CapabilitySpecificationStoreError,
            render_capability_specification,
        )
        from naumi_agent.evolution.queue import render_queue_result
        from naumi_agent.evolution.review import EvolutionReviewFilter
        from naumi_agent.evolution.store import EvolutionStoreError
        from naumi_agent.ui.evolution_review import evolution_review_payload

        action = str(payload.get("action") or "list")
        try:
            service = self.engine.evolution_review_service
            if action == "capability-spec":
                session = getattr(self.engine, "_session", None)
                view = (
                    await self.engine.evolution_capability_specification_service.advance(
                        self.engine.workspace_root,
                        candidate_id=str(payload.get("candidate_id") or ""),
                        session_id=str(getattr(session, "id", "")),
                        agent_name="Human",
                    )
                )
                await self._emit_system_notice(
                    "Capability Specification",
                    render_capability_specification(view),
                    request_id=request_id,
                )
                snapshot = await service.detail_snapshot(
                    self.engine.workspace_root,
                    str(payload.get("candidate_id") or ""),
                )
            elif action == "capability-govern":
                view = await self.engine.evolution_capability_governance_service.decide(
                    self.engine.workspace_root,
                    candidate_id=str(payload.get("candidate_id") or ""),
                )
                await self._emit_system_notice(
                    "Capability Governance",
                    render_capability_governance(view),
                    request_id=request_id,
                )
                snapshot = await service.detail_snapshot(
                    self.engine.workspace_root,
                    str(payload.get("candidate_id") or ""),
                )
            elif action == "capability-artifact":
                source_path = str(payload.get("source_path") or "")
                class_name = str(payload.get("class_name") or "")
                artifact_service = self.engine.evolution_capability_artifact_service
                if source_path and class_name:
                    view = await artifact_service.create(
                        self.engine.workspace_root,
                        candidate_id=str(payload.get("candidate_id") or ""),
                        source_path=source_path,
                        class_name=class_name,
                    )
                else:
                    view = await artifact_service.inspect(
                        self.engine.workspace_root,
                        str(payload.get("candidate_id") or ""),
                    )
                await self._emit_system_notice(
                    "Capability Sandbox 准入预检",
                    render_capability_artifact(view),
                    request_id=request_id,
                )
                snapshot = await service.detail_snapshot(
                    self.engine.workspace_root,
                    str(payload.get("candidate_id") or ""),
                )
            elif action == "capability-bind":
                view = (
                    await self.engine.evolution_capability_scenario_binding_service.advance(
                        self.engine.workspace_root,
                        candidate_id=str(payload.get("candidate_id") or ""),
                    )
                )
                await self._emit_system_notice(
                    "Capability 可执行场景绑定",
                    render_capability_scenario_binding(view),
                    request_id=request_id,
                )
                snapshot = await service.detail_snapshot(
                    self.engine.workspace_root,
                    str(payload.get("candidate_id") or ""),
                )
            elif action == "capability-sandbox":
                view = (
                    await self.engine.evolution_capability_sandbox_request_service.prepare(
                        self.engine.workspace_root,
                        candidate_id=str(payload.get("candidate_id") or ""),
                    )
                )
                await self._emit_system_notice(
                    "Capability Sandbox Execution Request",
                    render_capability_sandbox_request(view),
                    request_id=request_id,
                )
                snapshot = await service.detail_snapshot(
                    self.engine.workspace_root,
                    str(payload.get("candidate_id") or ""),
                )
            elif action == "capability-run":
                from naumi_agent.tools.base import ToolCall

                candidate_id = str(payload.get("candidate_id") or "")
                run_id = f"uicaprun-{uuid4().hex}"
                result = await self.engine.execute_tool(
                    ToolCall(
                        id=f"ui-capability-sandbox-{uuid4()}",
                        name="evolution_capability_sandbox_execute",
                        arguments=json.dumps(
                            {"candidate_id": candidate_id, "run_id": run_id},
                            ensure_ascii=False,
                        ),
                    ),
                    agent_name="new-ui",
                )
                await self._emit_system_notice(
                    "Capability Sandbox Execution",
                    result.content,
                    request_id=request_id,
                )
                snapshot = await service.detail_snapshot(
                    self.engine.workspace_root,
                    candidate_id,
                )
            elif action == "enqueue":
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
            elif action in {"list", "priorities"}:
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
        except (
            CapabilitySpecificationStoreError,
            CapabilityGovernanceError,
            CapabilityArtifactError,
            CapabilityScenarioBindingError,
            CapabilitySandboxRequestError,
            EvolutionStoreError,
            OSError,
            ValueError,
        ):
            if action in {
                "capability-spec", "capability-govern", "capability-artifact",
                "capability-bind",
                "capability-sandbox",
                "capability-run",
            }:
                await self.emit_error(
                    (
                        "Capability 实现制品未就绪；源码、规格或治理来源已失效。"
                        if action == "capability-artifact"
                        else (
                            "Capability 场景未绑定；Artifact、人工答案或 JSON Schema 已失效。"
                            if action == "capability-bind"
                            else (
                                "Capability Sandbox Execution 未完成；"
                                "Request、Run Grant 或 ARC-04 Worker 已失效。"
                                if action == "capability-run"
                                else (
                                    "Capability Sandbox Request 未形成；"
                                    "Binding 或 Git source 已失效。"
                                    if action == "capability-sandbox"
                                    else (
                                        "Capability Specification/Governance 未推进；"
                                        "来源失效、答案无效或交互仍待处理。"
                                    )
                                )
                            )
                        )
                    ),
                    code=(
                        "evolution_capability_artifact_failed"
                        if action == "capability-artifact"
                        else (
                            "evolution_capability_scenario_binding_failed"
                            if action == "capability-bind"
                            else (
                                "evolution_capability_sandbox_execution_failed"
                                if action == "capability-run"
                                else (
                                    "evolution_capability_sandbox_request_failed"
                                    if action == "capability-sandbox"
                                    else (
                                        "evolution_capability_governance_failed"
                                        if action == "capability-govern"
                                        else "evolution_capability_specification_failed"
                                    )
                                )
                            )
                        )
                    ),
                    request_id=request_id,
                )
                return
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

    async def show_evolution_evaluation_lane(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Build and return one workspace-scoped, non-final lane receipt."""
        executor = getattr(
            self.engine,
            "evolution_evaluation_lane_receipt_executor",
            None,
        )
        if executor is None:
            await self.emit_error(
                "Evaluation Lane authority 尚未初始化；请运行 /doctor 后重试。",
                code="evolution_evaluation_lane_failed",
                request_id=request_id,
            )
            return
        try:
            receipt = await executor.execute_by_id(
                workspace_root=self.engine.workspace_root,
                comparison_id=str(payload["comparison_id"]),
            )
            response = evaluation_lane_receipt_payload(receipt)
        except (EvolutionEvaluationLaneReceiptError, OSError, ValueError):
            await self.emit_error(
                "Evaluation Lane Receipt 不可用；请确认 Comparison 属于当前工作区且证据完整。",
                code="evolution_evaluation_lane_failed",
                request_id=request_id,
            )
            return
        await self.emit(
            ServerEventType.EVOLUTION_EVALUATION_LANE,
            response,
            request_id=request_id,
        )

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
        )

        config = getattr(self.engine, "_config", AppConfig())
        additional_items = await self._doctor_additional_health_items()
        try:
            report = await run_doctor(
                config,
                workspace_root=self.engine.workspace_root,
                mcp_manager=getattr(self.engine, "_mcp_manager", None),
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
        self._doctor_health_snapshot = health_snapshot
        self._doctor_export_plan = None
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

    async def show_doctor_trace(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Emit a bounded body-folded index for this exact Bridge run."""
        from naumi_agent.ui.doctor_trace import (
            DOCTOR_TRACE_DEFAULT_LIMIT,
            DoctorTraceIndexError,
            build_doctor_trace_index,
            doctor_trace_payload,
        )

        if self.debug_trace is None or not self.debug_trace.enabled:
            await self.emit_error(
                "当前 Bridge 的结构化调试日志未启用，无法建立 Trace 索引。",
                code="trace_disabled",
                request_id=request_id,
            )
            return
        try:
            index = build_doctor_trace_index(
                self.debug_trace.run_dir.parent,
                query=str(payload.get("query") or ""),
                limit=payload.get("limit", DOCTOR_TRACE_DEFAULT_LIMIT),
                preferred_run_id=self.debug_trace.run_id,
            )
        except DoctorTraceIndexError as exc:
            await self.emit_error(
                str(exc),
                code=exc.code,
                request_id=request_id,
            )
            return
        await self.emit(
            ServerEventType.DOCTOR_TRACE_RESULT,
            doctor_trace_payload(index),
            request_id=request_id,
        )

    async def start_doctor_live_probe(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Start one explicit bounded provider request without blocking control input."""
        active = self._doctor_probe_task
        if active is not None and not active.done():
            await self.emit_error(
                "已有在线探测正在运行；可先取消当前探测。",
                code="doctor_probe_busy",
                request_id=request_id,
                details={"target_request_id": self._doctor_probe_request_id},
            )
            return
        timeout_ms = int(payload["timeout_ms"])
        self._doctor_probe_request_id = request_id
        self._doctor_probe_timeout_ms = timeout_ms
        self._doctor_probe_request_started = False
        self._doctor_probe_task = asyncio.create_task(
            self._run_doctor_live_probe(
                request_id=request_id,
                timeout_ms=timeout_ms,
            ),
            name=f"doctor-live-probe:{request_id}",
        )

    async def cancel_doctor_live_probe(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Cancel only the caller-selected live probe and acknowledge the control."""
        target_request_id = str(payload["target_request_id"])
        task = self._doctor_probe_task
        if (
            task is None
            or task.done()
            or target_request_id != self._doctor_probe_request_id
        ):
            await self.emit_error(
                "目标在线探测不存在或已结束。",
                code="doctor_probe_not_running",
                request_id=request_id,
            )
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await self.emit(
            ServerEventType.ACK,
            {
                "event": str(ClientEventType.DOCTOR_PROBE_CANCEL),
                "target_request_id": target_request_id,
            },
            request_id=request_id,
        )

    async def _run_doctor_live_probe(
        self,
        *,
        request_id: str,
        timeout_ms: int,
    ) -> None:
        from naumi_agent.ui.doctor_health import (
            build_doctor_health_snapshot,
            doctor_health_payload,
        )

        current_task = asyncio.current_task()
        try:
            result = await run_bounded_doctor_live_probe(
                getattr(self.engine, "_config", AppConfig()),
                workspace_root=self.engine.workspace_root,
                timeout_ms=timeout_ms,
                mcp_manager=getattr(self.engine, "mcp_manager", None),
                model_router=self.engine.router,
                on_request_start=self._mark_doctor_probe_request_started,
            )
            snapshot = build_doctor_health_snapshot(
                result.report,
                live_probe=True,
                additional_items=tuple(
                    await self._doctor_additional_health_items()
                ),
            )
            self._doctor_health_snapshot = snapshot
            self._doctor_export_plan = None
            await self.emit(
                ServerEventType.DOCTOR_HEALTH,
                doctor_health_payload(snapshot),
                request_id=request_id,
            )
            await self.emit(
                ServerEventType.DOCTOR_PROBE_RESULT,
                doctor_live_probe_payload(
                    result,
                    snapshot_sha256=snapshot.snapshot_sha256,
                ),
                request_id=request_id,
            )
        except asyncio.CancelledError:
            if not self._closed:
                await self.emit(
                    ServerEventType.DOCTOR_PROBE_RESULT,
                    cancelled_doctor_live_probe_payload(
                        timeout_ms=timeout_ms,
                        request_count=int(self._doctor_probe_request_started),
                    ),
                    request_id=request_id,
                )
            raise
        except Exception as exc:
            logger.warning("Doctor live probe failed (%s)", type(exc).__name__)
            if self.debug_trace is not None:
                self.debug_trace.exception("ui_bridge.doctor_probe", exc)
            if not self._closed:
                await self.emit_error(
                    "在线探测运行时失败；不会自动重试。请查看 /debug。",
                    code="doctor_probe_failed",
                    request_id=request_id,
                )
        finally:
            if self._doctor_probe_task is current_task:
                self._doctor_probe_task = None
                self._doctor_probe_request_id = ""
                self._doctor_probe_request_started = False

    def _mark_doctor_probe_request_started(self) -> None:
        self._doctor_probe_request_started = True

    async def _doctor_additional_health_items(self) -> list[Any]:
        """Collect shared runtime-only Doctor items without provider traffic."""
        from naumi_agent.ui.doctor_health import (
            pursuit_recovery_health_item,
            runtime_heartbeat_retention_health_item,
        )

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
        return additional_items

    async def export_doctor_report(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Preview or atomically write the last typed Doctor snapshot."""
        snapshot = self._doctor_health_snapshot
        if snapshot is None:
            await self.emit_error(
                "请先打开 `/doctor` 生成本轮 typed Health 快照，再预览导出包。",
                code="doctor_export_snapshot_missing",
                request_id=request_id,
            )
            return
        action = str(payload.get("action") or "preview")
        try:
            if action == "preview":
                plan = build_doctor_export_plan(
                    snapshot,
                    workspace_root=self.engine.workspace_root,
                )
                self._doctor_export_plan = plan
                response = doctor_export_preview_payload(plan.preview)
            else:
                expected = str(payload.get("expected_snapshot_sha256") or "")
                plan = self._doctor_export_plan
                if (
                    plan is None
                    or expected != snapshot.snapshot_sha256
                    or plan.preview.source_snapshot_sha256 != expected
                ):
                    self._doctor_export_plan = None
                    await self.emit_error(
                        "诊断事实已变化或预览已失效；请重新按 `e` 预览后再导出。",
                        code="doctor_export_preview_stale",
                        request_id=request_id,
                    )
                    return
                receipt = write_doctor_export(plan)
                response = doctor_export_receipt_payload(plan.preview, receipt)
                self._doctor_export_plan = None
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("Doctor export failed (%s)", type(exc).__name__)
            if self.debug_trace is not None:
                self.debug_trace.exception("ui_bridge.doctor_export", exc)
            await self.emit_error(
                "诊断包导出失败；未写入不完整文件。请检查 Naumi 状态目录权限。",
                code="doctor_export_failed",
                request_id=request_id,
            )
            return
        await self.emit(
            ServerEventType.DOCTOR_EXPORT_RESULT,
            response,
            request_id=request_id,
        )

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
        if event in {
            "harness_live_eval_progress",
            "harness_sandbox_eval_progress",
        }:
            await self.emit(
                ServerEventType.HARNESS_EVAL_BATCH,
                data,
                request_id=self._active_run_context.get("request_id") or None,
            )
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
        choices = normalize_backend_permission_choices(payload["choices"])
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
        public_payload = public_permission_request_payload(
            payload,
            request_id=request_id,
            choices=choices,
        )
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
        public_payload = public_interaction_request_payload(
            request,
            request_id=request_id,
            session_id=str(
                getattr(getattr(self.engine, "_session", None), "id", "") or ""
            ),
            run_id=str(self._active_run_context.get("run_id") or ""),
            agent_name=str(payload.get("agent_name") or "main"),
            expires_at=durable_record.expires_at if durable_record else "",
        )
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
                self._schedule_interaction_recovery_fill()

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
            self._schedule_interaction_recovery_fill()

    async def _read_goal_linked_interaction(
        self,
        interaction_id: str,
        *,
        request_id: str,
    ) -> tuple[
        DurableInteractionAuthorityClient | None,
        HarnessInteractionRecord | None,
    ]:
        """Read one Goal-linked interaction or emit a bounded public error."""
        authority = self._interaction_authority()
        if authority is None:
            await self.emit_error(
                "持久交互 authority 不可用。",
                code="interaction_authority_unavailable",
                request_id=request_id,
            )
            return None, None
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
            return None, None
        if record is None:
            await self.emit_error(
                f"未找到持久用户交互: {interaction_id}",
                code="unknown_interaction_request",
                request_id=request_id,
            )
            return None, None
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
            return None, None
        if record.subject_kind != "pursuit" or record.subject_id not in linked_runs:
            await self.emit_error(
                "该交互不属于当前 Goal 页面中的 Pursuit。",
                code="interaction_scope_mismatch",
                request_id=request_id,
            )
            return None, None
        return authority, record

    async def takeover_user_interaction(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Claim exactly one interaction and bind it to this Bridge UI host."""
        interaction_id = str(payload.get("interaction_id") or "")
        async with self._interaction_claim_lock:
            authority, record = await self._read_goal_linked_interaction(
                interaction_id,
                request_id=request_id,
            )
            if authority is None or record is None:
                return
            if interaction_id in self._pending_interactions:
                await self.emit_error(
                    "该用户交互已在当前界面中展示。",
                    code="interaction_already_active",
                    request_id=request_id,
                )
                return
            try:
                claimed = await authority.claim(interaction_id=interaction_id)
            except InteractionClaimError as exc:
                if exc.code == "expired":
                    await self.emit(
                        ServerEventType.INTERACTION_RESOLVED,
                        {
                            "request_id": interaction_id,
                            "status": "expired",
                            "reason": str(exc),
                        },
                        request_id=request_id,
                    )
                    await self.emit(ServerEventType.STATUS, self.status_payload())
                    return
                await self.emit_error(
                    str(exc),
                    code=f"interaction_takeover_{exc.code}",
                    request_id=request_id,
                )
                return
            except Exception as exc:
                logger.warning(
                    "Durable interaction takeover failed (%s)",
                    type(exc).__name__,
                )
                await self.emit_error(
                    "用户交互在接管前已发生变化，请刷新 Goal 页面重试。",
                    code="interaction_takeover_conflict",
                    request_id=request_id,
                )
                return
            try:
                bound = await self._bind_replayed_interaction(claimed)
            except Exception as exc:
                logger.warning(
                    "Durable interaction host binding failed (%s)",
                    type(exc).__name__,
                )
                await self.emit_error(
                    "交互已取得临时租约，但当前界面未能展示；"
                    "租约到期后可刷新 Goal 页面重试。",
                    code="interaction_takeover_bind_failed",
                    request_id=request_id,
                )
                return
            if not bound:
                await self.emit_error(
                    "该用户交互已在当前界面中展示。",
                    code="interaction_already_active",
                    request_id=request_id,
                )
                return
        await self.emit(ServerEventType.STATUS, self.status_payload())

    async def cancel_user_interaction(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        """Cancel one durable interaction through sequence-fenced authority."""
        interaction_id = str(payload.get("interaction_id") or "")
        authority, record = await self._read_goal_linked_interaction(
            interaction_id,
            request_id=request_id,
        )
        if authority is None or record is None:
            return
        if record.state != "pending":
            await self.emit_error(
                f"用户交互已是终态：{record.state}，不能取消。",
                code="interaction_not_pending",
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
            self._schedule_interaction_recovery_fill()
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

    async def shutdown(self, *, request_id: str | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        await self._stop_workbench_subscription()
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
        doctor_probe_task = self._doctor_probe_task
        self._doctor_probe_task = None
        self._doctor_probe_request_id = ""
        self._doctor_probe_request_started = False
        if doctor_probe_task is not None and not doctor_probe_task.done():
            doctor_probe_task.cancel()
            await asyncio.gather(doctor_probe_task, return_exceptions=True)
        batch_tasks = tuple(self._harness_eval_batch_tasks.values())
        for task in batch_tasks:
            task.cancel()
        if batch_tasks:
            await asyncio.gather(*batch_tasks, return_exceptions=True)
        self._harness_eval_batch_tasks.clear()
        retry_tasks = tuple(self._harness_eval_retry_tasks.values())
        for task in retry_tasks:
            task.cancel()
        if retry_tasks:
            await asyncio.gather(*retry_tasks, return_exceptions=True)
        self._harness_eval_retry_tasks.clear()
        promotion_tasks = tuple(self._harness_eval_promotion_tasks.values())
        for task in promotion_tasks:
            task.cancel()
        if promotion_tasks:
            await asyncio.gather(*promotion_tasks, return_exceptions=True)
        self._harness_eval_promotion_tasks.clear()
        goal_lifecycle_tasks = tuple(self._goal_lifecycle_tasks.values())
        for task in goal_lifecycle_tasks:
            task.cancel()
        if goal_lifecycle_tasks:
            await asyncio.gather(*goal_lifecycle_tasks, return_exceptions=True)
        self._goal_lifecycle_tasks.clear()
        pursuit_recovery_tasks = tuple(self._pursuit_recovery_tasks.values())
        for task in pursuit_recovery_tasks:
            task.cancel()
        if pursuit_recovery_tasks:
            await asyncio.gather(*pursuit_recovery_tasks, return_exceptions=True)
        self._pursuit_recovery_tasks.clear()
        terminal_outbox_tasks = tuple(self._pursuit_terminal_outbox_tasks.values())
        for task in terminal_outbox_tasks:
            task.cancel()
        if terminal_outbox_tasks:
            await asyncio.gather(*terminal_outbox_tasks, return_exceptions=True)
        self._pursuit_terminal_outbox_tasks.clear()
        workspace_file_task = self._workspace_file_search_task
        self._workspace_file_search_task = None
        if workspace_file_task is not None and not workspace_file_task.done():
            workspace_file_task.cancel()
            await asyncio.gather(workspace_file_task, return_exceptions=True)
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
        await self.emit(
            ServerEventType.SHUTDOWN,
            {"ok": True},
            request_id=request_id,
        )
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
