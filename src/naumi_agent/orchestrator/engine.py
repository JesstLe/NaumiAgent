"""Agent 核心引擎 — ReAct 主循环."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from naumi_agent.agent_control import AgentControlService
from naumi_agent.background import BackgroundRunner, BackgroundTaskStore, create_background_tools
from naumi_agent.config.settings import AppConfig
from naumi_agent.evolution.experiment_leases import (
    EvolutionExperimentLeaseManager,
    EvolutionExperimentLeaseStore,
)
from naumi_agent.evolution.experiment_snapshots import (
    EvolutionExperimentSourceSnapshotBuilder,
)
from naumi_agent.evolution.experiments import EvolutionExperimentContractIssuer
from naumi_agent.evolution.mutation_generation import (
    EvolutionMutationGenerationService,
    EvolutionMutationGenerationTraceStore,
)
from naumi_agent.evolution.mutation_plans import EvolutionMutationPlanner
from naumi_agent.evolution.mutation_receipts import (
    EvolutionMutationReceiptService,
    EvolutionMutationReceiptStore,
)
from naumi_agent.evolution.mutation_turns import EvolutionMutationTurnRunner
from naumi_agent.evolution.patch_journals import EvolutionPatchJournalStore
from naumi_agent.evolution.patch_recovery import (
    EvolutionPatchRecoveryCoordinator,
    EvolutionPatchRecoveryResult,
    EvolutionPatchSetRecoveryCoordinator,
    EvolutionPatchSetRecoveryResult,
)
from naumi_agent.evolution.patch_set_writers import EvolutionPatchSetWriter
from naumi_agent.evolution.patch_sets import EvolutionPatchSetStore
from naumi_agent.evolution.patch_writers import EvolutionPatchWriter
from naumi_agent.evolution.queue import EvolutionProposalQueueAdapter
from naumi_agent.evolution.review import EvolutionReviewService
from naumi_agent.evolution.static_guards import EvolutionStaticGuard
from naumi_agent.harness.completion import (
    CompletionGateResult,
    HarnessCompletionReceipt,
    HarnessRunState,
)
from naumi_agent.harness.coordinator import (
    ReconciliationCoordinatorOutcome,
    ReconciliationCoordinatorResult,
    SessionReconciliationCoordinator,
)
from naumi_agent.harness.feedback import (
    FeedbackIntakeService,
    FeedbackSourceEnvelope,
)
from naumi_agent.harness.retention import LifecycleActor
from naumi_agent.harness.retention_executor import (
    SessionRetentionExecutor,
    SessionRetentionPassResult,
)
from naumi_agent.harness.retention_periodic import (
    RetentionPeriodicPolicy,
    RetentionWorkerSnapshot,
    SessionRetentionPeriodicService,
    retention_worker_status_payload,
)
from naumi_agent.harness.retention_planner import (
    SessionRetentionPolicy,
    SessionRetentionPreview,
    plan_session_retention,
)
from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.tools import create_harness_tools
from naumi_agent.hooks import HookContext, HookManager, HookPoint
from naumi_agent.inspector import RuntimeInspectorEventSink, RuntimeInspectorService
from naumi_agent.mcp.client import MCPClientManager, MCPServerConfig, setup_mcp_servers
from naumi_agent.memory.auto_extract import extract_memory_candidates
from naumi_agent.memory.compactor import ContextCompactor
from naumi_agent.memory.lifecycle import SessionDeletePreview
from naumi_agent.memory.long_term import LongTermMemory, MemoryEntry
from naumi_agent.memory.session import Session
from naumi_agent.model.router import ModelTier, TokenUsage
from naumi_agent.orchestrator.context_assembly import (
    HARNESS_CONTEXT_MARKER,
    HarnessContextAssembler,
    HarnessContextInput,
    is_harness_context_message,
)
from naumi_agent.orchestrator.planner import AdaptivePlanner, ExecutionMode, Plan
from naumi_agent.orchestrator.system_prompt import (
    PromptAssemblyInput,
    build_system_prompt,
    is_generated_system_prompt,
)
from naumi_agent.orchestrator.tool_batches import (
    ScheduledToolCall,
    ToolBatch,
    build_tool_batches,
    execute_tool_batch,
)
from naumi_agent.runs.models import CompletionReceipt
from naumi_agent.runs.recorder import ChatRunRecorder, ChatRunRecorderEventSink
from naumi_agent.runtime.dependencies import RuntimePortOverrides, RuntimePorts
from naumi_agent.runtime.paths import RuntimePaths
from naumi_agent.runtime.ports.events import (
    EventSink,
    LegacyEventCallback,
    RuntimeEvent,
    RuntimeEventType,
)
from naumi_agent.runtime.ports.model import ModelPort
from naumi_agent.runtime.ports.permission import PermissionPort
from naumi_agent.runtime.ports.session import SessionPort
from naumi_agent.runtime.ports.tool_execution import ToolExecutionPort
from naumi_agent.runtime.resources import RuntimeResources
from naumi_agent.safety.budget import BudgetTracker, TokenBudget
from naumi_agent.safety.guardrails import OutputGuardrail
from naumi_agent.safety.permission_grants import PermissionGrant, PermissionGrantStore
from naumi_agent.safety.permissions import PermissionMode, PermissionOutcome
from naumi_agent.scheduler import SchedulerRunner, SchedulerStore, create_scheduler_tools
from naumi_agent.skills.loader import SkillLoader
from naumi_agent.skills.tool import create_skill_tools
from naumi_agent.streaming.event_bus import EventEmitter
from naumi_agent.streaming.publisher import RuntimeEventPublisher
from naumi_agent.streaming.sinks import (
    CallbackEventSink,
    CompositeEventSink,
    coerce_event_sink,
)
from naumi_agent.tasks.models import TaskStatus
from naumi_agent.tasks.reconciliation import (
    TodoReconciliationAction,
    reconcile_todos,
)
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.browser.runtime.browser_runtime import BrowserRuntime
from naumi_agent.tools.browser.tools import create_browser_tools
from naumi_agent.tools.browser_daemon import BrowserDaemonClient, create_browser_daemon_tools
from naumi_agent.tools.builtin import create_builtin_tools
from naumi_agent.tools.memory import create_memory_tools
from naumi_agent.tools.sandbox import create_sandbox_tools
from naumi_agent.tools.web import create_web_tools
from naumi_agent.workbench.review_evidence import ReviewEvidenceCollector
from naumi_agent.workbench.service import WorkbenchService
from naumi_agent.workbench.tools import create_workbench_tools
from naumi_agent.workbench.validation import ValidationRunner
from naumi_agent.worktree import WorktreeManager, create_worktree_tools

PermissionConfirmationCallback = Callable[[dict[str, Any]], Awaitable[str | bool]]
UserInteractionCallback = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class _ObservedRuntimeEventPublisher:
    """Publish through one run publisher, then update local derived metrics."""

    def __init__(
        self,
        publisher: RuntimeEventPublisher,
        observer: Callable[
            [RuntimeEventType, Mapping[str, object], int],
            Awaitable[None],
        ],
    ) -> None:
        self._publisher = publisher
        self._observer = observer

    async def publish(
        self,
        event_type: RuntimeEventType,
        data: Mapping[str, object],
        *,
        turn: int = 0,
    ) -> RuntimeEvent:
        event = await self._publisher.publish(event_type, data, turn=turn)
        await self._observer(event_type, data, turn)
        return event

    def legacy_callback(self) -> LegacyEventCallback:
        return self._publisher.legacy_callback()


type EngineEventPublisher = RuntimeEventPublisher | _ObservedRuntimeEventPublisher

logger = logging.getLogger(__name__)

_OUTPUT_TRUNCATED_FINISH_REASONS = {
    "length",
    "max_tokens",
    "max_output_tokens",
    "content_filter_length",
}
_MAX_OUTPUT_CONTINUATIONS = 2
_TOOL_TEXT_GUARD_CHARS = 24
_OUTPUT_CONTINUATION_PROMPT = (
    "你的上一条回答因为输出上限被截断。请从截断处直接继续，"
    "不要重写已经说过的内容，不要添加开场白。"
)
_REPEATED_TOOL_CALL_MESSAGE = (
    "同一个工具调用已经连续重复，系统已跳过本次重复执行。"
    "请基于已有工具结果继续判断；如需要继续操作，请选择有明确差异的下一步。"
)
_DEFAULT_COMPACTION_RESERVED_TOKENS = 20_000


def _budget_percentage(
    used: int | float,
    limit: int | float | None,
) -> float | None:
    if limit is None:
        return None
    if limit == 0:
        return 100.0
    return round(float(used) / float(limit) * 100, 1)

_TASK_EVENT_TOOLS = {
    "delegate_task",
    "todo_write",
    "task_create",
    "task_update",
    "task_list",
    "task_delete",
    "todo_reconciliation",
}

_TOOL_PREPARE_MIN_INTERVAL = 0.25
_TOOL_PREPARE_MIN_ARG_DELTA = 4096


class AgentRuntimeMode(StrEnum):
    """User-facing runtime modes controlled by Shift+Tab."""

    DEFAULT = "default"
    PLAN = "plan"
    BYPASS = "bypass"


_RUNTIME_MODE_CYCLE = (
    AgentRuntimeMode.DEFAULT,
    AgentRuntimeMode.PLAN,
    AgentRuntimeMode.BYPASS,
)

_PLAN_MODE_READ_ONLY_TOOLS = {
    "file_read",
    "yaml_micro_verify",
    "yaml_validate",
    "web_search",
    "web_fetch",
    "memory_recall",
    "task_list",
    "runtime_status",
    "tool_search",
    "list_agents",
    "read_agent",
    "team_status",
    "blackboard_read",
    "pursuit_status",
    "pursuit_list",
    "background_status",
    "background_list",
    "background_read_output",
    "schedule_list",
    "worktree_status",
}


def _notification_preview(content: str, *, max_chars: int = 240) -> str:
    """Build a compact one-line preview for runtime notification events."""
    lines = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or (line.startswith("<") and line.endswith(">")):
            continue
        lines.append(line)
        if len(lines) >= 4:
            break
    preview = "；".join(lines) if lines else "已收到运行时通知。"
    if len(preview) <= max_chars:
        return preview
    return preview[: max_chars - 1] + "…"


def _decode_partial_json_string(raw: str) -> str:
    """Decode a best-effort JSON string fragment without failing on truncation."""
    if not raw:
        return ""
    # A streamed JSON argument can end halfway through an escape sequence. Trim
    # the dangling slash so the preview remains stable while more bytes arrive.
    if raw.endswith("\\"):
        raw = raw[:-1]
    try:
        return json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        return (
            raw.replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace('\\"', '"')
            .replace("\\\\", "\\")
        )


def _extract_tool_arg_field(arguments: str, keys: tuple[str, ...]) -> str:
    """Extract a complete or partial string field from streamed tool arguments."""
    if not arguments:
        return ""
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        for key in keys:
            value = parsed.get(key)
            if isinstance(value, str):
                return value
            if value is not None:
                return str(value)

    for key in keys:
        marker = f'"{key}"'
        start = arguments.find(marker)
        if start < 0:
            continue
        colon = arguments.find(":", start + len(marker))
        if colon < 0:
            continue
        value_start = colon + 1
        while value_start < len(arguments) and arguments[value_start].isspace():
            value_start += 1
        if value_start >= len(arguments):
            continue
        if arguments[value_start] != '"':
            value_end = value_start
            while value_end < len(arguments) and arguments[value_end] not in ",}":
                value_end += 1
            return arguments[value_start:value_end].strip()

        value_start += 1
        escaped = False
        chars: list[str] = []
        for ch in arguments[value_start:]:
            if escaped:
                chars.append("\\" + ch)
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == '"':
                break
            chars.append(ch)
        return _decode_partial_json_string("".join(chars))
    return ""


def _extract_todo_prepare_preview(tool_name: str, arguments: str) -> dict[str, Any]:
    """Extract compact todo progress from streamed todo_write arguments."""
    if tool_name != "todo_write" or '"todos"' not in arguments:
        return {}

    raw_items: list[dict[str, Any]] = []
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict) and isinstance(parsed.get("todos"), list):
        raw_items = [item for item in parsed["todos"] if isinstance(item, dict)]
    else:
        raw_items = _extract_partial_todo_items(arguments)

    if not raw_items:
        return {}

    items = [_normalize_todo_prepare_item(item) for item in raw_items]
    items = [item for item in items if item["subject"]]
    if not items:
        return {}

    open_items = [item for item in items if item["status"] != "completed"]
    return {
        "todo_total": len(items),
        "todo_completed": len(items) - len(open_items),
        "todo_open": len(open_items),
        "todo_items": open_items,
    }


def _extract_partial_todo_items(arguments: str) -> list[dict[str, Any]]:
    """Extract already completed object fragments from a streamed todos array."""
    items: list[dict[str, Any]] = []
    for match in re.finditer(
        r"\{[^{}]*(?:\"content\"|\"subject\"|\"status\"|\"id\")[^{}]*\}",
        arguments,
    ):
        fragment = match.group(0)
        item: dict[str, Any] = {}
        for key in ("id", "content", "subject", "active_form", "status"):
            value = _extract_tool_arg_field(fragment, (key,))
            if value:
                item[key] = value
        if item:
            items.append(item)
    return items


def _normalize_todo_prepare_item(item: dict[str, Any]) -> dict[str, str]:
    subject = (
        item.get("active_form")
        or item.get("subject")
        or item.get("content")
        or item.get("title")
        or ""
    )
    status = str(item.get("status") or "pending").strip().lower()
    if status in {"done", "complete"}:
        status = "completed"
    if status in {"running", "active"}:
        status = "in_progress"
    if status not in {"pending", "in_progress", "completed", "blocked"}:
        status = "pending"
    return {
        "id": str(item.get("id") or "..."),
        "status": status,
        "subject": str(subject).strip(),
    }


def _summarize_tool_prepare_snapshot(
    snapshot: Any,
    *,
    started_at: float,
    now: float,
) -> dict[str, Any]:
    """Build a compact, user-safe progress payload from streamed tool args."""
    entries: list[dict[str, Any]] = []
    if isinstance(snapshot, dict):
        entries = [call for _, call in sorted(snapshot.items(), key=lambda item: str(item[0]))]
    elif isinstance(snapshot, list):
        entries = [call for call in snapshot if isinstance(call, dict)]

    call = entries[0] if entries else {}
    function = call.get("function") if isinstance(call, dict) else {}
    if not isinstance(function, dict):
        function = {}
    name = str(function.get("name") or "tool")
    arguments = str(function.get("arguments") or "")
    path = _extract_tool_arg_field(
        arguments,
        ("file_path", "path", "target_path", "filename", "url"),
    )
    content = _extract_tool_arg_field(arguments, ("content", "text", "code", "patch"))
    data: dict[str, Any] = {
        "name": name,
        "tool_call_id": str(call.get("id") or "") if isinstance(call, dict) else "",
        "argument_chars": len(arguments),
        "elapsed_ms": int(max(0.0, now - started_at) * 1000),
    }
    if path:
        data["path"] = path
    if content:
        data["content_chars"] = len(content)
        data["content_lines"] = content.count("\n") + 1
    data.update(_extract_todo_prepare_preview(name, arguments))
    return data


def _tool_prepare_signature(data: dict[str, Any]) -> str:
    """Return the fields that should trigger visible progress updates."""
    return "|".join(
        str(data.get(key, ""))
        for key in (
            "name",
            "path",
            "argument_chars",
            "content_chars",
            "content_lines",
            "todo_total",
            "todo_completed",
            "todo_open",
        )
    )


@dataclass
class AgentUsage:
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    turns: int = 0
    cache_tokens: int = 0


@dataclass
class AgentResult:
    status: str  # "completed" | "max_turns" | "error"
    response: str = ""
    usage: AgentUsage = field(default_factory=AgentUsage)
    error: str | None = None
    task_summary: str | None = None
    receipt: CompletionReceipt | None = None
    harness_receipt: HarnessCompletionReceipt | None = None


class AgentEngine:
    """Agent 主引擎 — 管理 LLM 循环和工具调用."""

    def __init__(
        self,
        config: AppConfig,
        *,
        ports: RuntimePorts[Session] | None = None,
        paths: RuntimePaths | None = None,
        resources: RuntimeResources | None = None,
        session_port: SessionPort[Session] | None = None,
        permission_port: PermissionPort | None = None,
        model_port: ModelPort | None = None,
        tool_execution_port: ToolExecutionPort | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        self._config = config
        legacy_ports = (
            session_port,
            permission_port,
            model_port,
            tool_execution_port,
            event_sink,
        )
        if ports is not None and any(value is not None for value in legacy_ports):
            raise TypeError("ports 与单独 Port 参数不能同时提供")
        if ports is not None and not isinstance(ports, RuntimePorts):
            raise TypeError("ports 必须是完整的 RuntimePorts")
        if paths is not None and not isinstance(paths, RuntimePaths):
            raise TypeError("paths 必须是完整的 RuntimePaths")
        if resources is not None and not isinstance(resources, RuntimeResources):
            raise TypeError("resources 必须是完整的 RuntimeResources")
        if ports is None or paths is None or resources is None:
            from naumi_agent.runtime.composition import (
                build_runtime_paths,
                build_runtime_ports,
                build_runtime_resources,
            )

            if paths is None:
                paths = build_runtime_paths(config)

            if ports is None:
                ports = build_runtime_ports(
                    config,
                    paths=paths,
                    overrides=RuntimePortOverrides(
                        session_port=session_port,
                        permission_port=permission_port,
                        model_port=model_port,
                        tool_execution_port=tool_execution_port,
                        event_sink=event_sink,
                    ),
                )

            if resources is None:
                resources = build_runtime_resources(paths)

        self._event_sink = ports.event_sink
        self._session_port = ports.session_port
        self._permission_port = ports.permission_port
        self._model_port = ports.model_port
        self._tool_execution_port = ports.tool_execution_port
        self._paths = paths
        self._resources = resources
        self.workspace_root = paths.workspace_root
        self._runtime_data_dir = paths.runtime_data_dir
        self._worktree_storage_dir = paths.worktree_storage_dir
        self._harness_store = resources.harness_store
        self.harness_service = HarnessService(
            workspace_root=self.workspace_root,
            trust_store=resources.harness_trust_store,
            store=self._harness_store,
        )
        self.evolution_candidate_store = resources.evolution_candidate_store
        self.feedback_intake_service = FeedbackIntakeService(
            self.evolution_candidate_store
        )
        self.evolution_review_service = EvolutionReviewService(
            self.evolution_candidate_store
        )
        self._session_reconciliation_coordinator = SessionReconciliationCoordinator(
            session_port=self._session_port,
            harness_store=self._harness_store,
            fallback_workspace=self.workspace_root,
        )
        retention_config = config.memory.session_retention
        self._retention_periodic_service = SessionRetentionPeriodicService(
            lease_port=self._harness_store,
            run_pass=self._run_periodic_session_retention,
            policy=RetentionPeriodicPolicy(
                interval_seconds=retention_config.interval_seconds,
                max_empty_backoff_seconds=(
                    retention_config.max_empty_backoff_seconds
                ),
                lease_seconds=retention_config.worker_lease_seconds,
                standby_retry_seconds=retention_config.standby_retry_seconds,
                jitter_ratio=retention_config.jitter_ratio,
            ),
        )
        self._session_reconciliation_worker_id = f"engine-{uuid.uuid4().hex}"
        self.chat_run_store = resources.chat_run_store
        self._tool_registry = ToolRegistry()
        self._messages: list[dict[str, Any]] = []
        self._full_history: list[dict[str, Any]] = []  # untruncated display history
        self._usage = AgentUsage()
        self._budget_tracker = BudgetTracker(
            TokenBudget(
                max_input_tokens=config.safety.max_input_tokens,
                max_output_tokens=config.safety.max_output_tokens,
                max_usd=config.safety.max_budget_usd,
            )
        )
        self._output_guardrail = OutputGuardrail()
        self._permission_grant_store = PermissionGrantStore()
        self._default_permission_mode = self._permission_port.mode
        self._runtime_mode = (
            AgentRuntimeMode.BYPASS
            if self._default_permission_mode == PermissionMode.BYPASS
            else AgentRuntimeMode.DEFAULT
        )
        self.long_term_memory = LongTermMemory(config.memory)
        self._compactor = ContextCompactor(
            config.memory,
            self._model_port,
            threshold=config.memory.compaction_threshold,
            long_term_memory=(
                self.long_term_memory
                if config.memory.long_term_enabled
                else None
            ),
        )

        self.emitter = EventEmitter()
        self.hooks = HookManager()
        self._session: Session | None = None
        self._active_feedback_turn: FeedbackSourceEnvelope | None = None
        self._session_authorization_generation = 0
        self._session_transition_epochs: dict[str, set[int]] = {}
        self._session_transition_tokens: dict[int, str] = {}
        self._next_session_transition_epoch = 0
        self._session_transition_lock = asyncio.Lock()
        self._openai_tools_cache_key: tuple[tuple[str, int], ...] = ()
        self._openai_tools_cache: list[dict[str, Any]] | None = None
        self._browser_session = BrowserRuntime(
            paths.browser_data_dir,
            replay_recording_enabled=(
                config.browser.replay_recording_enabled
            ),
        )
        self.browser_daemon = BrowserDaemonClient(
            config.browser_daemon,
            log_dir=paths.browser_daemon_log_dir,
        )
        self._planner = AdaptivePlanner(
            self._model_port,
            usage_callback=self._track_model_usage,
        )
        self._harness_context = HarnessContextAssembler()
        self._active_harness_run: HarnessRunState | None = None
        self._permission_bubble_history: list[dict[str, Any]] = []
        self._permission_confirmer: PermissionConfirmationCallback | None = None
        self._user_interaction_handler: UserInteractionCallback | None = None

        self.task_store = resources.task_store
        self.workbench_store = resources.workbench_store
        self.worktree_manager = WorktreeManager(
            repo_root=self.workspace_root,
            storage_dir=self._worktree_storage_dir,
            task_store=self.task_store,
        )
        self.validation_runner = ValidationRunner(
            store=self.workbench_store,
            allowed_commands=[
                ["python3", "-m", "pytest"],
                ["pytest"],
                ["python3", "-m", "ruff"],
                ["ruff"],
                ["swift", "test"],
                ["swift", "build"],
            ],
            timeout_seconds=120,
        )
        self.review_evidence_collector = ReviewEvidenceCollector(
            store=self.workbench_store,
            task_store=self.task_store,
            worktree_storage_dir=self._worktree_storage_dir,
        )
        self.workbench_service = WorkbenchService(
            task_store=self.task_store,
            workbench_store=self.workbench_store,
            validation_runner=self.validation_runner,
            workspace_root=str(self.workspace_root),
            review_evidence_collector=self.review_evidence_collector,
            worktree_manager=self.worktree_manager,
        )
        self.evolution_review_service.bind_governance_reader(self.workbench_service)
        self.evolution_proposal_queue = EvolutionProposalQueueAdapter(
            review_service=self.evolution_review_service,
            workbench_service=self.workbench_service,
        )
        self.evolution_experiment_contract_issuer = EvolutionExperimentContractIssuer(
            review_service=self.evolution_review_service,
            workbench_service=self.workbench_service,
        )
        self.evolution_experiment_lease_store = EvolutionExperimentLeaseStore(
            config.memory.session_db_path
        )
        self.evolution_experiment_lease_manager = EvolutionExperimentLeaseManager(
            store=self.evolution_experiment_lease_store,
            worktree_manager=self.worktree_manager,
        )
        self.evolution_experiment_source_snapshot_builder = (
            EvolutionExperimentSourceSnapshotBuilder(
                self._tool_registry,
                worktree_storage_dir=self._worktree_storage_dir,
            )
        )
        self.evolution_mutation_planner = EvolutionMutationPlanner(
            review_service=self.evolution_review_service,
            snapshot_builder=self.evolution_experiment_source_snapshot_builder,
        )
        self.evolution_static_guard = EvolutionStaticGuard(
            snapshot_builder=self.evolution_experiment_source_snapshot_builder,
        )
        self.evolution_patch_journal_store = EvolutionPatchJournalStore(
            config.memory.session_db_path
        )
        self.evolution_patch_set_store = EvolutionPatchSetStore(
            config.memory.session_db_path
        )
        self.evolution_mutation_receipt_store = EvolutionMutationReceiptStore(
            config.memory.session_db_path
        )
        self.evolution_mutation_generation_trace_store = (
            EvolutionMutationGenerationTraceStore(config.memory.session_db_path)
        )
        self.evolution_mutation_generation_service = EvolutionMutationGenerationService(
            trace_store=self.evolution_mutation_generation_trace_store,
        )
        self.evolution_mutation_turn_runner = EvolutionMutationTurnRunner(
            model_port=self._model_port,
            generation_service=self.evolution_mutation_generation_service,
        )
        self.evolution_patch_recovery = EvolutionPatchRecoveryCoordinator(
            journal_store=self.evolution_patch_journal_store,
            patch_set_store=self.evolution_patch_set_store,
            worktree_storage_dir=self._worktree_storage_dir,
        )
        self.evolution_patch_set_recovery = EvolutionPatchSetRecoveryCoordinator(
            patch_set_store=self.evolution_patch_set_store,
            journal_store=self.evolution_patch_journal_store,
        )
        self._last_evolution_patch_recovery: tuple[
            EvolutionPatchRecoveryResult, ...
        ] = ()
        self._last_evolution_patch_set_recovery: tuple[
            EvolutionPatchSetRecoveryResult, ...
        ] = ()
        self.evolution_patch_writer = EvolutionPatchWriter(
            static_guard=self.evolution_static_guard,
            journal_store=self.evolution_patch_journal_store,
            patch_set_store=self.evolution_patch_set_store,
        )
        self.evolution_patch_set_writer = EvolutionPatchSetWriter(
            static_guard=self.evolution_static_guard,
            patch_set_store=self.evolution_patch_set_store,
            journal_store=self.evolution_patch_journal_store,
        )
        self.evolution_mutation_receipt_service = EvolutionMutationReceiptService(
            journal_store=self.evolution_patch_journal_store,
            patch_set_store=self.evolution_patch_set_store,
            receipt_store=self.evolution_mutation_receipt_store,
        )
        self.background_runner = BackgroundRunner(
            BackgroundTaskStore(self._runtime_data_dir / "background")
        )
        self.scheduler_runner = SchedulerRunner(
            SchedulerStore(self._runtime_data_dir / "scheduler")
        )
        self.goal_store = resources.goal_store
        self.pursuit_store = resources.pursuit_store
        self.runtime_inspector = RuntimeInspectorService(self)

        self._mcp_manager: MCPClientManager | None = None

        self._task_runner: Any | None = None
        self._security_auditor: Any | None = None

        self.skill_loader = SkillLoader()

        self._register_builtin_tools()
        self._register_subagent_manager()
        self._register_shell_hooks()
        self._register_skills()

    def _register_builtin_tools(self) -> None:
        for tool in create_harness_tools(self.harness_service):
            self._tool_registry.register(tool)
        for tool in create_builtin_tools(
            self.workspace_root,
            shell_output_dir=self.workspace_root / ".naumi" / "shell-output",
        ):
            self._tool_registry.register(tool)
        for tool in create_browser_tools(self._browser_session):
            self._tool_registry.register(tool)
        for tool in create_browser_daemon_tools(self.browser_daemon):
            self._tool_registry.register(tool)
        for tool in create_sandbox_tools():
            self._tool_registry.register(tool)
        try:
            for tool in create_web_tools(
                self._browser_session,
                search_config=self._config.search,
            ):
                self._tool_registry.register(tool)
        except Exception:
            pass  # web tools optional (may need API keys)

        # 分析模式工具（chaos/scale/state/vibe）
        from naumi_agent.tools.analysis import (
            create_analysis_tools,
            set_analysis_router,
        )

        set_analysis_router(self._model_port)
        for tool in create_analysis_tools():
            self._tool_registry.register(tool)

        try:
            for tool in create_memory_tools(self.long_term_memory):
                self._tool_registry.register(tool)
        except Exception:
            pass  # memory tools optional (chromadb may not be installed)

        # Task management tools
        from naumi_agent.tasks.tools import create_task_tools

        for tool in create_task_tools(self.task_store):
            self._tool_registry.register(tool)

        # Background task tools
        for tool in create_background_tools(self.background_runner):
            self._tool_registry.register(tool)

        # Scheduler / reminder tools
        for tool in create_scheduler_tools(self.scheduler_runner):
            self._tool_registry.register(tool)

        # Worktree isolation tools
        for tool in create_worktree_tools(self.worktree_manager):
            self._tool_registry.register(tool)

        # Workbench read/proposal tools
        for tool in create_workbench_tools(self.workbench_service):
            self._tool_registry.register(tool)

        # Runtime status tools
        from naumi_agent.tools.doctor import DoctorDiagnosticsTool
        from naumi_agent.tools.evolution_review import create_evolution_review_tools
        from naumi_agent.tools.feedback import create_feedback_tools
        from naumi_agent.tools.runtime import create_runtime_tools
        from naumi_agent.tools.search import create_tool_search_tools
        from naumi_agent.tools.session import create_session_tools
        from naumi_agent.tools.user_interaction import RequestUserInputTool

        self._tool_registry.register(DoctorDiagnosticsTool(self))
        self._tool_registry.register(RequestUserInputTool(self))
        for tool in create_feedback_tools(self, self.feedback_intake_service):
            self._tool_registry.register(tool)
        for tool in create_evolution_review_tools(self, self.evolution_review_service):
            self._tool_registry.register(tool)
        for tool in create_session_tools(self):
            self._tool_registry.register(tool)
        for tool in create_runtime_tools(self):
            self._tool_registry.register(tool)
        for tool in create_tool_search_tools(self._tool_registry):
            self._tool_registry.register(tool)

        # Hot-reload tool
        from naumi_agent.tools.hotreload import HotReloadTool

        self._tool_registry.register(HotReloadTool())

        # Self-modification tool
        from naumi_agent.tools.self_modify import SelfModifyTool

        self._tool_registry.register(SelfModifyTool())

        # Self-evolution tool
        from naumi_agent.tools.self_evolve import SelfEvolveTool

        self._tool_registry.register(SelfEvolveTool())

        # Tool forge
        from naumi_agent.tools.forge import ForgeTool, load_all_generated_tools

        self._tool_registry.register(ForgeTool())

        # Load previously generated tools
        for tool in load_all_generated_tools():
            self._tool_registry.register(tool)
            logger.info("Loaded generated tool: %s", tool.name)

    def _register_subagent_manager(self) -> None:
        from naumi_agent.orchestrator.subagent_manager import SubAgentManager
        from naumi_agent.tools.analysis import set_analysis_subagent_manager
        from naumi_agent.tools.pursuit import set_pursuit_dependencies
        from naumi_agent.tools.subagent import create_subagent_tools

        self.subagent_manager = SubAgentManager(self)
        self.agent_control = AgentControlService(
            self,
            session_id_getter=lambda: self._session.id if self._session else "",
        )
        set_analysis_subagent_manager(self.subagent_manager)
        for tool in create_subagent_tools(self.subagent_manager):
            self._tool_registry.register(tool)

        # Goal pursuit tool
        set_pursuit_dependencies(
            router=self._model_port,
            tool_registry=self._tool_registry,
            subagent_manager=self.subagent_manager,
            store=self.pursuit_store,
            execute_tool_call=self.execute_tool,
            lease_port=self._harness_store,
            workspace_root=self.workspace_root,
            background_reconcile_source=self.background_runner,
            interaction_port=self._harness_store,
        )
        from naumi_agent.tools.pursuit import create_pursuit_tool
        for tool in create_pursuit_tool():
            self._tool_registry.register(tool)

        from naumi_agent.tools.goal import create_goal_tools

        for tool in create_goal_tools(
            self.goal_store,
            self.pursuit_store,
            session_id_getter=lambda: self._session.id if self._session else "",
            pursuit_tool_getter=lambda: self._tool_registry.get("pursue_goal"),
            recovery_authority=self._harness_store,
            workspace_root=self.workspace_root,
        ):
            self._tool_registry.register(tool)

        self._reaper_started = False

    def _get_openai_tools_cached(self) -> list[dict[str, Any]] | None:
        """Return cached OpenAI tool schemas while the registry is unchanged."""
        if len(self._tool_registry) <= 0:
            return None
        tools = self._tool_registry.all()
        cache_key = tuple((tool.name, id(tool)) for tool in tools)
        if self._openai_tools_cache is None or cache_key != self._openai_tools_cache_key:
            self._openai_tools_cache_key = cache_key
            self._openai_tools_cache = [tool.to_openai_tool() for tool in tools]
        return self._openai_tools_cache

    @staticmethod
    def _should_preplan_streaming(task: str) -> bool:
        """Keep streaming responsive unless the user explicitly asks for orchestration."""
        text = task.lower()
        explicit_markers = (
            "多智能体",
            "子 agent",
            "子agent",
            "subagent",
            "sub-agent",
            "orchestrator",
            "orchestrate",
            "并行执行",
            "并行规划",
            "任务分解",
            "制定计划",
            "先规划",
            "执行计划",
        )
        return any(marker in text for marker in explicit_markers)

    def _register_shell_hooks(self) -> None:
        """从 config.yaml 的 hooks 段注册 shell 命令 hook."""
        from naumi_agent.hooks.shell_hook import ShellHookConfig, create_shell_hook_runner

        hooks_cfg = self._config.hooks
        registered = 0
        for point_name in HookPoint:
            entries = getattr(hooks_cfg, point_name.value, None)
            if not entries:
                continue
            for entry in entries:
                if not isinstance(entry, dict) or "command" not in entry:
                    logger.warning("Invalid shell hook config for %s: %s", point_name.value, entry)
                    continue
                shell_cfg = ShellHookConfig.from_dict(entry)
                runner = create_shell_hook_runner(shell_cfg)
                self.hooks.register(point_name, runner)
                registered += 1
        if registered:
            logger.info("Registered %d shell hooks from config", registered)

    def _register_skills(self) -> None:
        """从配置的搜索路径加载 Skill 并注册为 Tool."""
        search_paths = self._config.skills.search_paths

        # 默认搜索路径：项目 .naumi/skills/ 和用户 ~/.naumi/skills/
        default_paths = [
            str(Path.cwd() / ".naumi" / "skills"),
            str(Path.home() / ".naumi" / "skills"),
        ]
        all_paths = default_paths + search_paths

        self.skill_loader = SkillLoader(search_paths=all_paths)
        skills = self.skill_loader.load_all()

        if not skills:
            return

        for tool in create_skill_tools(skills):
            self._tool_registry.register(tool)

        logger.info("Registered %d skills from %d search paths", len(skills), len(all_paths))

    async def setup_mcp_tools(self) -> None:
        """从配置连接 MCP Server 并注册工具（需在异步上下文中调用）."""
        server_configs = self._config.mcp.servers
        if not server_configs:
            return

        manager, tools = await setup_mcp_servers(server_configs)
        self._mcp_manager = manager
        for tool in tools:
            self._tool_registry.register(tool)

    async def connect_mcp_server(
        self,
        *,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> list[str]:
        """Connect an MCP server at runtime and register discovered tools."""
        if self._mcp_manager is None:
            self._mcp_manager = MCPClientManager()
        config = MCPServerConfig(command=command, args=args or [], env=env)
        tools = await self._mcp_manager.connect(name, config)
        for tool in tools:
            self._tool_registry.register(tool)
        return [tool.name for tool in tools]

    @property
    def tool_registry(self) -> ToolRegistry:
        return self._tool_registry

    def current_feedback_turn(self) -> FeedbackSourceEnvelope | None:
        """Return the runtime-minted durable user turn active in this engine."""
        return self._active_feedback_turn

    @property
    def config(self) -> AppConfig:
        return self._config

    @property
    def router(self) -> ModelPort:
        return self._model_port

    @property
    def _router(self) -> ModelPort:
        """Return the legacy read-only alias for the injected model port."""
        return self._model_port

    @property
    def tool_executor(self) -> ToolExecutionPort:
        """Return the authorized tool invocation port."""
        return self._tool_execution_port

    @property
    def event_sink(self) -> EventSink:
        """Return the injected Runtime event-output port."""
        return self._event_sink

    @property
    def session_store(self) -> SessionPort[Session]:
        """Return the legacy read-only alias for the injected session port."""
        return self._session_port

    @property
    def _permission_checker(self) -> PermissionPort:
        """Return the legacy read-only alias for the injected permission port."""
        return self._permission_port

    @property
    def usage(self) -> AgentUsage:
        return self._usage

    @property
    def permission_mode(self) -> PermissionMode:
        """Return the active permission mode."""
        return self._permission_port.mode

    @property
    def runtime_mode(self) -> AgentRuntimeMode:
        """Return the user-facing runtime mode."""
        return self._runtime_mode

    def set_runtime_mode(self, mode: AgentRuntimeMode | str) -> AgentRuntimeMode:
        """Apply a user-facing runtime mode to the underlying permission layer."""
        runtime_mode = AgentRuntimeMode(mode)
        self._runtime_mode = runtime_mode
        if runtime_mode == AgentRuntimeMode.DEFAULT:
            permission_mode = self._default_permission_mode
        elif runtime_mode == AgentRuntimeMode.PLAN:
            permission_mode = PermissionMode.STRICT
        else:
            permission_mode = PermissionMode.BYPASS
        self._permission_port.set_mode(permission_mode)
        self._config.safety.permission_mode = permission_mode.value
        return self._runtime_mode

    def cycle_runtime_mode(self) -> AgentRuntimeMode:
        """Cycle default → plan → bypass → default for Shift+Tab."""
        idx = _RUNTIME_MODE_CYCLE.index(self._runtime_mode)
        next_mode = _RUNTIME_MODE_CYCLE[(idx + 1) % len(_RUNTIME_MODE_CYCLE)]
        return self.set_runtime_mode(next_mode)

    def set_permission_confirmer(
        self,
        confirmer: PermissionConfirmationCallback | None,
    ) -> None:
        """Register a UI callback used when a tool needs user confirmation."""
        self._permission_confirmer = confirmer

    def set_user_interaction_handler(
        self,
        handler: UserInteractionCallback | None,
    ) -> None:
        """Register the active UI callback for structured user decisions."""
        self._user_interaction_handler = handler

    async def request_user_input(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Pause one tool call until the active UI returns a validated response."""
        if self._user_interaction_handler is None:
            from naumi_agent.user_interaction import UserInteractionUnavailableError

            raise UserInteractionUnavailableError("当前界面不支持结构化交互")
        from naumi_agent.orchestrator.pursuit import (
            current_pursuit_interaction_context,
        )

        interaction_id = f"ask-{uuid.uuid4().hex}"
        context = current_pursuit_interaction_context()
        session_id = self._session.id if self._session else ""
        enriched: dict[str, Any] = {
            **payload,
            "_interaction_id": interaction_id,
            "_durable_subject_kind": "pursuit" if context is not None else "runtime",
            "_durable_subject_id": (
                context.run_id
                if context is not None
                else session_id or "runtime-sessionless"
            ),
        }
        if context is not None:
            enriched["_pursuit_begin"] = context.begin
            enriched["_pursuit_resolve"] = context.resolve
        return await self._user_interaction_handler(enriched)

    def list_permission_grants(self) -> tuple[PermissionGrant, ...]:
        """Return active grants for the current session only."""
        if self._session is None:
            return ()
        return self._permission_grant_store.list_session(self._session.id)

    def revoke_permission_grant(self, grant_id: str) -> bool:
        """Revoke one grant only when it belongs to the current session."""
        if self._session is None:
            return False
        grant = next(
            (
                candidate
                for candidate in self._permission_grant_store.list_session(
                    self._session.id
                )
                if candidate.grant_id == grant_id
            ),
            None,
        )
        if grant is None:
            return False
        revoked = self._permission_grant_store.revoke(grant_id, self._session.id)
        if revoked:
            self._record_permission_grant_revocation(
                grant,
                reason="用户主动撤销了本会话权限授权。",
                source="explicit_revoke",
            )
        return revoked

    def revoke_all_permission_grants(self) -> int:
        """Revoke every active grant for the current session."""
        if self._session is None:
            return 0
        return self._revoke_permission_grants_for_session(
            self._session.id,
            reason="用户撤销了本会话的全部权限授权。",
            source="revoke_all",
        )

    def _revoke_permission_grants_for_session(
        self,
        session_id: str,
        *,
        reason: str,
        source: str,
    ) -> int:
        """Revoke and audit every active grant scoped to one session."""
        grants = self._permission_grant_store.list_session(session_id)
        revoked_count = 0
        for grant in grants:
            if self._permission_grant_store.revoke(grant.grant_id, session_id):
                self._record_permission_grant_revocation(
                    grant,
                    reason=reason,
                    source=source,
                )
                revoked_count += 1
        return revoked_count

    def _clear_permission_grants(self, *, reason: str, source: str) -> None:
        """Clear and audit all active grants held by this engine."""
        grants = self._permission_grant_store.list_all()
        self._permission_grant_store.clear()
        for grant in grants:
            self._record_permission_grant_revocation(
                grant,
                reason=reason,
                source=source,
            )

    def _record_permission_grant_revocation(
        self,
        grant: PermissionGrant,
        *,
        reason: str,
        source: str,
    ) -> None:
        """Append one audit record after an in-memory grant is removed."""
        self._append_permission_bubble({
            "agent_name": "main",
            "tool_name": "permission_grant",
            "call_id": grant.source_request_id,
            "status": "grant_revoked",
            "reason": reason,
            "risk_level": "",
            "requires_confirmation": False,
            "session_id": grant.session_id,
            "timestamp": time.time(),
            "grant_id": grant.grant_id,
            "tool_family": grant.tool_family,
            "source": source,
            "source_request_id": grant.source_request_id,
        })

    @property
    def task_runner(self) -> Any:
        if self._task_runner is None:
            from naumi_agent.tools.browser.orchestrator.task_runner import (
                TaskRunner,
            )

            browser_dir = str(
                Path(self._config.memory.session_db_path).parent / "browser"
            )
            self._task_runner = TaskRunner(
                base_dir=browser_dir,
                options={
                    "runtime": self._browser_session,
                    "model_router": self._model_port,
                    "max_concurrent_runs": (
                        self._config.browser.max_concurrent_runs
                    ),
                    "run_history_limit": self._config.browser.run_history_limit,
                },
            )
        return self._task_runner

    @property
    def security_auditor(self) -> Any:
        if self._security_auditor is None:
            from naumi_agent.tools.browser.security import SecurityAuditor

            self._security_auditor = SecurityAuditor(
                self._browser_session
            )
        return self._security_auditor

    def reset(self) -> None:
        self._advance_session_authorization_generation()
        self._messages.clear()
        self._full_history.clear()
        self._usage = AgentUsage()
        self._budget_tracker.reset()
        self._clear_permission_grants(
            reason="引擎重置时撤销了权限授权。",
            source="reset",
        )
        self._session = None
        self.task_store.set_session("")
        self._permission_port.reset_counts()

    async def _shutdown_component(
        self,
        name: str,
        operation: Callable[[], Awaitable[Any]],
    ) -> None:
        try:
            await operation()
        except Exception as exc:
            logger.warning(
                "Engine shutdown component failed [%s]: %s: %s",
                name,
                type(exc).__name__,
                exc,
            )

    async def shutdown(self) -> None:
        """释放资源（关闭数据库连接、浏览器、MCP 连接等）."""
        self._advance_session_authorization_generation()
        self._clear_permission_grants(
            reason="引擎关闭时撤销了权限授权。",
            source="shutdown",
        )
        await self._shutdown_component(
            "session_retention_worker",
            self._retention_periodic_service.stop,
        )
        if hasattr(self, "subagent_manager"):
            await self._shutdown_component(
                "subagent_reaper",
                self.subagent_manager.stop_reaper,
            )
            try:
                self.subagent_manager.destroy_all_dynamic()
            except Exception as exc:
                logger.warning(
                    "Engine shutdown component failed [subagents]: %s: %s",
                    type(exc).__name__,
                    exc,
                )
            try:
                from naumi_agent.tools.analysis import clear_analysis_subagent_manager

                clear_analysis_subagent_manager(self.subagent_manager)
            except Exception as e:
                logger.debug("Failed to clear analysis subagent manager: %s", e)
        if self._task_runner is not None:
            for run in self._task_runner.runs:
                if run.get("status") in ("running", "queued"):
                    self._task_runner.abort_run(
                        run["id"], reason="Engine shutdown"
                    )
        if hasattr(self, "background_runner"):
            await self._shutdown_component(
                "background_runner",
                self.background_runner.shutdown,
            )
        if hasattr(self, "scheduler_runner"):
            await self._shutdown_component(
                "scheduler_runner",
                self.scheduler_runner.shutdown,
            )
        await self._shutdown_component(
            "browser",
            self._browser_session.stop,
        )
        if self._mcp_manager:
            await self._shutdown_component(
                "mcp",
                self._mcp_manager.disconnect_all,
            )
        if hasattr(self, "task_store"):
            try:
                self.task_store.set_session("")
            except Exception as exc:
                logger.warning(
                    "Engine shutdown component failed [task_store]: %s: %s",
                    type(exc).__name__,
                    exc,
                )
        await self._shutdown_component(
            "session_store",
            self._session_port.close,
        )

    async def reload_tools(self, domain: str = "tools") -> dict[str, Any]:
        """热重载指定域的模块并重新注册工具.

        Args:
            domain: "tools", "memory", "skills", "all"

        Returns:
            重载结果统计
        """
        from naumi_agent.tools.hotreload import reload_domain

        results = reload_domain(domain)

        reloaded = sum(1 for r in results if r["status"] == "reloaded")
        errors = sum(1 for r in results if r["status"] == "error")

        # If tools were reloaded, re-register analysis tools (most common case)
        if domain in ("tools", "all") and reloaded > 0:
            try:
                from naumi_agent.tools.analysis import (
                    create_analysis_tools,
                    set_analysis_router,
                )

                set_analysis_router(self._model_port)
                for tool in create_analysis_tools():
                    self._tool_registry.register(tool)
            except Exception as e:
                logger.warning("Failed to re-register analysis tools: %s", e)

        # If skills were reloaded, re-register
        if domain in ("skills", "all") and reloaded > 0:
            self._register_skills()

        logger.info(
            "Hot-reload complete: %d reloaded, %d errors", reloaded, errors,
        )

        return {
            "reloaded": reloaded,
            "errors": errors,
            "details": results,
        }

    def set_system_prompt(self, prompt: str) -> None:
        """设置/更新系统提示词."""
        # 移除旧的 system message
        self._messages = [m for m in self._messages if m.get("role") != "system"]
        self._full_history = [m for m in self._full_history if m.get("role") != "system"]
        msg = {"role": "system", "content": prompt}
        self._messages.insert(0, msg)
        self._full_history.insert(0, msg)

    # --- 会话持久化 ---

    def _advance_session_authorization_generation(self) -> None:
        """Invalidate authorization captured before a real session transition."""
        self._session_authorization_generation += 1

    def _begin_active_session_transition(self, session_id: str) -> int | None:
        """Fence one operation without letting another operation clear it."""
        if not session_id or self._session is None or self._session.id != session_id:
            return None
        self._next_session_transition_epoch += 1
        epoch = self._next_session_transition_epoch
        self._session_transition_epochs.setdefault(session_id, set()).add(epoch)
        self._session_transition_tokens[epoch] = session_id
        return epoch

    def _move_session_transition(self, epoch: int | None, session_id: str) -> None:
        """Move a queued operation's fence to the session it will now leave."""
        if epoch is None:
            return
        previous_session_id = self._session_transition_tokens.get(epoch)
        if previous_session_id == session_id:
            return
        if previous_session_id:
            epochs = self._session_transition_epochs.get(previous_session_id)
            if epochs is not None:
                epochs.discard(epoch)
                if not epochs:
                    del self._session_transition_epochs[previous_session_id]
        if session_id:
            self._session_transition_epochs.setdefault(session_id, set()).add(epoch)
            self._session_transition_tokens[epoch] = session_id
        else:
            self._session_transition_tokens.pop(epoch, None)

    def _finish_session_transition(self, session_id: str, epoch: int | None) -> None:
        """Clear only the transition barrier established by this operation."""
        if epoch is None:
            return
        owned_session_id = self._session_transition_tokens.pop(epoch, None)
        if not owned_session_id:
            return
        epochs = self._session_transition_epochs.get(owned_session_id)
        if epochs is not None:
            epochs.discard(epoch)
            if not epochs:
                del self._session_transition_epochs[owned_session_id]

    def _is_session_transitioning(self, session_id: str) -> bool:
        return bool(session_id) and bool(self._session_transition_epochs.get(session_id))

    def _is_tool_call_session_active(
        self,
        session_id: str,
        authorization_generation: int,
    ) -> bool:
        """Return whether a tool call still belongs to an executable session."""
        if authorization_generation != self._session_authorization_generation:
            return False
        if not session_id:
            return self._session is None
        return (
            self._session is not None
            and self._session.id == session_id
            and not self._is_session_transitioning(session_id)
        )

    async def _block_transitioning_tool_call(
        self,
        *,
        tool_call: ToolCall,
        session_id: str,
        events: EngineEventPublisher | None,
        agent_name: str | None,
    ) -> ToolResult | None:
        """Fail closed before permission handling while a session is changing."""
        if not self._is_session_transitioning(session_id):
            return None
        reason = "当前会话正在切换，已停止执行该工具。请等待切换完成后重试。"
        await self._emit_permission_bubble(
            events,
            agent_name=agent_name,
            tool_name=tool_call.name,
            call_id=tool_call.id,
            status="blocked_by_session_transition",
            reason=reason,
            requires_confirmation=False,
            session_id=session_id,
        )
        return ToolResult(
            call_id=tool_call.id,
            status="error",
            content=f"权限拒绝：{reason}",
        )

    async def get_or_create_session(self, title: str | None = None) -> Session:
        """获取当前会话，不存在则创建."""
        if self._session is not None:
            return self._session
        default_prompt = self._build_system_prompt()
        self._session = await self._session_port.create_session(
            title=title,
            model=self._model_port.resolve_model(ModelTier.CAPABLE),
            system_prompt=next(
                (m["content"] for m in self._messages if m.get("role") == "system"),
                default_prompt,
            ),
        )
        self._advance_session_authorization_generation()
        return self._session

    async def load_session(self, session_id: str) -> bool:
        """加载已有会话，恢复上下文.

        清理不完整的工具调用序列，避免 LLM API 拒绝续接。
        """
        initial_session_id = self._session.id if self._session is not None else ""
        transition_epoch = (
            self._begin_active_session_transition(initial_session_id)
            if initial_session_id and initial_session_id != session_id
            else None
        )
        try:
            async with self._session_transition_lock:
                previous_session_id = self._session.id if self._session is not None else ""
                if previous_session_id and previous_session_id != session_id:
                    if transition_epoch is None:
                        transition_epoch = self._begin_active_session_transition(
                            previous_session_id
                        )
                    else:
                        self._move_session_transition(
                            transition_epoch,
                            previous_session_id,
                        )
                elif transition_epoch is not None:
                    self._finish_session_transition("", transition_epoch)
                    transition_epoch = None

                resume = getattr(self._session_port, "resume", None)
                session = (
                    await resume(session_id)
                    if callable(resume)
                    else await self._session_port.load(session_id)
                )
                if session is None:
                    return False
                if previous_session_id != session.id:
                    if previous_session_id:
                        self._revoke_permission_grants_for_session(
                            previous_session_id,
                            reason="切换到其他会话时撤销了权限授权。",
                            source="session_load",
                        )
                    self._advance_session_authorization_generation()
                self._session = session
                cleaned_messages = self._sanitize_messages(session.messages)
                self._messages = cleaned_messages
                self._full_history = list(cleaned_messages)
                self._usage = AgentUsage(
                    total_input_tokens=session.total_tokens,
                    total_cost_usd=session.total_cost_usd,
                )
                return True
        finally:
            self._finish_session_transition("", transition_epoch)

    @staticmethod
    def _sanitize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """修复消息序列中的不完整工具调用对.

        保留所有 tool_calls 和已有的 tool 结果（包括报错信息），
        对缺失的 tool 结果补一条占位消息，确保 LLM API 不会拒绝。
        """
        cleaned: list[dict[str, Any]] = []
        i = 0
        while i < len(messages):
            msg = messages[i]
            role = msg.get("role", "")

            if role == "assistant" and msg.get("tool_calls"):
                tool_call_ids = []
                for tc in msg["tool_calls"]:
                    tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                    if tc_id:
                        tool_call_ids.append(tc_id)

                # Collect matching tool results into a dict keyed by tool_call_id
                tool_results: dict[str, dict[str, Any]] = {}
                j = i + 1
                while j < len(messages) and messages[j].get("role") == "tool":
                    r = messages[j]
                    tc_id = r.get("tool_call_id")
                    if tc_id:
                        tool_results[tc_id] = r
                    j += 1

                # Always keep the assistant message with tool_calls
                cleaned.append(msg)

                # Emit results: existing ones preserved, missing ones get a placeholder
                for tc_id in tool_call_ids:
                    if tc_id in tool_results:
                        cleaned.append(tool_results[tc_id])
                    else:
                        cleaned.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": "[工具调用结果缺失 — 会话恢复时未能找到对应结果]",
                        })

                i = j
            elif role == "tool":
                # Orphan tool result without preceding assistant tool_calls — skip
                i += 1
            else:
                cleaned.append(msg)
                i += 1

        return cleaned

    async def list_sessions(
        self,
        page: int = 1,
        page_size: int = 20,
        query: str = "",
    ) -> tuple[list[Session], int]:
        """列出历史会话."""
        return await self._session_port.list_sessions(
            page=page,
            page_size=page_size,
            query=query,
        )

    async def delete_session(self, session_id: str) -> bool:
        """Delete one session, returning true only after full reconciliation."""
        result = await self.delete_session_detailed(session_id)
        return result.outcome is ReconciliationCoordinatorOutcome.COMPLETED

    async def delete_session_detailed(
        self,
        session_id: str,
    ) -> ReconciliationCoordinatorResult:
        """Delete through the durable coordinator and preserve partial outcomes."""
        transition_epoch = self._begin_active_session_transition(session_id)
        try:
            async with self._session_transition_lock:
                active_session_id = self._session.id if self._session is not None else ""
                if active_session_id == session_id:
                    if transition_epoch is None:
                        transition_epoch = self._begin_active_session_transition(
                            session_id
                        )
                    else:
                        self._move_session_transition(transition_epoch, session_id)
                elif transition_epoch is not None:
                    self._finish_session_transition("", transition_epoch)
                    transition_epoch = None

                completion_task = asyncio.create_task(
                    self._session_reconciliation_coordinator.delete_session(session_id)
                )
                try:
                    result = await self._await_delete_completion(completion_task)
                except asyncio.CancelledError:
                    if await self._session_absent_after_cancellation(session_id):
                        self._reconcile_deleted_session_runtime(session_id)
                    raise
                if (
                    result.outcome is not ReconciliationCoordinatorOutcome.NOT_FOUND
                    and await self._session_port.load(session_id) is None
                ):
                    self._reconcile_deleted_session_runtime(session_id)
                return result
        finally:
            self._finish_session_transition("", transition_epoch)

    async def _session_absent_after_cancellation(self, session_id: str) -> bool:
        """Finish authoritative persistence check despite repeated caller cancellation."""
        load_task = asyncio.create_task(self._session_port.load(session_id))
        while not load_task.done():
            try:
                session = await asyncio.shield(load_task)
            except asyncio.CancelledError:
                continue
            else:
                return session is None
        if load_task.cancelled():
            return False
        return load_task.result() is None

    async def recover_session_reconciliations(
        self,
        *,
        now: str | None = None,
        lease_seconds: int = 60,
        limit: int = 20,
    ) -> tuple[ReconciliationCoordinatorResult, ...]:
        """Run one bounded startup/background reconciliation recovery pass."""
        results = await self._session_reconciliation_coordinator.recover_due(
            worker_id=self._session_reconciliation_worker_id,
            now=now or datetime.now(UTC).isoformat(),
            lease_seconds=lease_seconds,
            limit=limit,
        )
        for result in results:
            if (
                result.outcome is ReconciliationCoordinatorOutcome.COMPLETED
                and await self._session_port.load(result.session_id) is None
            ):
                self._reconcile_deleted_session_runtime(result.session_id)
        return results

    async def preview_session_delete(
        self,
        session_id: str,
    ) -> SessionDeletePreview | None:
        """Preview workspace-scoped persistence impact without mutating state."""
        normalized_session_id = (session_id or "").strip()
        if not normalized_session_id:
            raise ValueError("session_id 不能为空。")
        session = await self._session_port.load(normalized_session_id)
        if session is None:
            return None
        saved_workspace = str(getattr(session, "workspace_root", "") or "").strip()
        workspace = Path(saved_workspace or self.workspace_root).expanduser().resolve()
        impact = await self.harness_service.preview_session_delete(
            session.id,
            workspace_root=workspace,
        )
        return SessionDeletePreview(
            session_id=session.id,
            title=session.title or "新会话",
            workspace_root=str(workspace),
            message_count=len(session.messages),
            is_active=self._session is not None and self._session.id == session.id,
            harness_run_count=impact.run_count,
            criterion_count=impact.criterion_count,
            check_count=impact.check_count,
            evidence_count=impact.evidence_count,
            replay_baseline_count=impact.replay_baseline_count,
            check_artifact_reference_count=impact.check_artifact_reference_count,
            evidence_artifact_reference_count=(
                impact.evidence_artifact_reference_count
            ),
        )

    async def preview_session_retention(self) -> SessionRetentionPreview:
        """Build a bounded, read-only retention plan for archived Sessions."""
        scan_candidates = getattr(
            self._session_port,
            "scan_retention_candidates",
            None,
        )
        if not callable(scan_candidates):
            raise RuntimeError("当前 Session 存储不支持保留策略预览。")
        configured = self._config.memory.session_retention
        policy = SessionRetentionPolicy(
            delete_archived_after_days=configured.delete_archived_after_days,
            max_archived_session_bytes=configured.max_archived_session_bytes,
            max_sessions_per_pass=configured.max_sessions_per_pass,
            max_bytes_per_pass=configured.max_bytes_per_pass,
            scan_limit=configured.scan_limit,
        )
        scan = await scan_candidates(limit=policy.scan_limit)
        return plan_session_retention(
            scan.candidates,
            total_archived_count=scan.total_archived_count,
            total_archived_bytes=scan.total_archived_bytes,
            policy=policy,
            now=datetime.now(),
            current_session_id=(self._session.id if self._session is not None else ""),
        )

    async def run_session_retention_once(
        self,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> SessionRetentionPassResult:
        """Execute one explicit bounded pass through retention-worker authority."""
        preview = await self.preview_session_retention()

        async def delete_archived(
            session_id: str,
        ) -> ReconciliationCoordinatorResult:
            return await self._session_reconciliation_coordinator.delete_session(
                session_id,
                actor=LifecycleActor.RETENTION_WORKER,
            )

        executor = SessionRetentionExecutor(delete_archived)
        return await executor.execute(
            preview,
            max_runtime_seconds=(
                self._config.memory.session_retention.max_runtime_seconds
            ),
            cancel_event=cancel_event,
        )

    async def _run_periodic_session_retention(
        self,
        cancel_event: asyncio.Event,
    ) -> SessionRetentionPassResult:
        return await self.run_session_retention_once(cancel_event=cancel_event)

    async def start_long_running_services(
        self,
    ) -> tuple[ReconciliationCoordinatorResult, ...]:
        """Recover durable Patch/Session work before enabling background services."""
        self._last_evolution_patch_set_recovery = (
            await self.evolution_patch_set_recovery.recover_pending()
        )
        self._last_evolution_patch_recovery = (
            await self.evolution_patch_recovery.recover_pending()
        )
        recovered = await self.recover_session_reconciliations()
        self.start_session_retention_worker()
        return recovered

    def evolution_patch_recovery_status(self) -> dict[str, object]:
        """Return a content-free startup recovery summary for CLI/UI surfaces."""
        single_outcomes = self._last_evolution_patch_recovery
        multi_outcomes = self._last_evolution_patch_set_recovery
        outcomes = (*multi_outcomes, *single_outcomes)
        failure_codes = sorted({item.failure_code for item in outcomes if item.failure_code})
        return {
            "total": len(outcomes),
            "single_file_total": len(single_outcomes),
            "multi_file_total": len(multi_outcomes),
            "completed": sum(item.recovery_complete for item in outcomes),
            "rolled_back": sum(item.status == "rolled_back" for item in outcomes),
            "already_baseline": sum(item.status == "already_baseline" for item in outcomes),
            "orphan_lock_removed": sum(
                item.status == "orphan_lock_removed" for item in outcomes
            ),
            "deferred": sum(item.status == "deferred" for item in outcomes),
            "failed": sum(item.status == "failed" for item in outcomes),
            "filesystem_changed": sum(item.filesystem_changed for item in outcomes),
            "failure_codes": failure_codes,
        }

    def start_session_retention_worker(self) -> bool:
        """Start only when periodic retention is explicitly enabled in config."""
        if not self._config.memory.session_retention.periodic_enabled:
            return False
        return self._retention_periodic_service.start()

    async def stop_session_retention_worker(self) -> bool:
        return await self._retention_periodic_service.stop()

    def wake_session_retention_worker(self) -> bool:
        return self._retention_periodic_service.wake()

    def session_retention_worker_snapshot(self) -> RetentionWorkerSnapshot:
        return self._retention_periodic_service.snapshot()

    def session_retention_worker_status(self) -> dict[str, object]:
        return retention_worker_status_payload(
            self.session_retention_worker_snapshot(),
            configured_enabled=(
                self._config.memory.session_retention.periodic_enabled
            ),
        )

    async def _await_delete_completion(
        self,
        completion_task: asyncio.Task[Any],
    ) -> Any:
        """Wait for Engine-owned reconciliation despite repeated caller cancellation."""
        caller_cancelled = False
        cancellation_forwarded = False
        while not completion_task.done():
            try:
                result = await asyncio.shield(completion_task)
            except asyncio.CancelledError:
                caller_cancelled = True
                if not completion_task.done() and not cancellation_forwarded:
                    completion_task.cancel()
                    cancellation_forwarded = True
            else:
                if caller_cancelled:
                    raise asyncio.CancelledError
                return result

        if completion_task.cancelled():
            raise asyncio.CancelledError
        result = completion_task.result()
        if caller_cancelled:
            raise asyncio.CancelledError
        return result

    def _reconcile_deleted_session_runtime(self, session_id: str) -> None:
        """Invalidate in-memory authority after Session deletion is authoritative."""
        self._revoke_permission_grants_for_session(
            session_id,
            reason="删除会话时撤销了权限授权。",
            source="session_deletion",
        )
        if self._session is not None and self._session.id == session_id:
            self._advance_session_authorization_generation()
            self._messages.clear()
            self._full_history.clear()
            self._usage = AgentUsage()
            self._budget_tracker.reset()
            self._session = None
            self.task_store.set_session("")
            self._permission_port.reset_counts()

    async def archive_session(self, session_id: str) -> bool:
        """归档指定会话."""
        archived = await self._session_port.archive(session_id)
        if archived and self._session is not None and self._session.id == session_id:
            # Keep the live conversation usable, but mirror persistence authority so
            # a later save cannot silently reactivate the archived row.
            self._session.status = "archived"
            self._session.archived_at = datetime.now()
        return archived

    async def _save_session(self) -> None:
        """将完整历史写入持久化存储（不丢失压缩前的消息）."""
        session = await self.get_or_create_session()
        session.messages = list(self._full_history) if self._full_history else list(self._messages)
        session.total_tokens = self._usage.total_input_tokens + self._usage.total_output_tokens
        session.total_cost_usd = self._usage.total_cost_usd
        session.workspace_root = str(self.workspace_root)
        session.git_branch = self._current_git_branch()
        session.summary = self._build_session_summary(session.messages)

        # 自动标题：从第一条用户消息中提取
        if not session.title or session.title == "新会话":
            for m in self._messages:
                if m.get("role") == "user":
                    session.title = m.get("content", "")[:50].split("\n")[0]
                    break

        await self._session_port.save(session)

    def _current_git_branch(self) -> str:
        """Return current git branch for session history metadata."""
        import subprocess

        try:
            return subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(self.workspace_root),
                stdin=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2,
            ).strip()
        except Exception:
            return ""

    @staticmethod
    def _build_session_summary(messages: list[dict[str, Any]], max_chars: int = 180) -> str:
        """Build a deterministic session preview from user/assistant messages."""
        parts: list[str] = []
        for message in messages:
            if message.get("role") not in {"user", "assistant"}:
                continue
            content = str(message.get("content", "") or "").strip().replace("\n", " ")
            if content:
                parts.append(content)
            if len(" ".join(parts)) >= max_chars:
                break
        summary = " / ".join(parts).strip()
        if len(summary) > max_chars:
            return summary[: max_chars - 1].rstrip() + "…"
        return summary

    # --- 记忆注入 ---

    async def _inject_relevant_memories(self, user_message: str) -> None:
        """自动召回与用户消息相关的长期记忆，注入到上下文中."""
        if not self._config.memory.long_term_enabled:
            return
        session_id = self._session.id if self._session else ""
        if not session_id:
            return
        try:
            results = await self.long_term_memory.recall_for_session(
                user_message,
                session_id=session_id,
                top_k=3,
                min_relevance=0.4,
            )
        except Exception as e:
            logger.debug("Memory recall for injection failed: %s", e)
            return

        if not results:
            return

        lines = ["## 相关记忆"]
        for r in results:
            lines.append(f"- [{r.entry.category}] {r.entry.content}")
        memory_block = "\n".join(lines)

        # Remove any previous memory injection to avoid accumulation
        self._messages = [
            m for m in self._messages
            if not (
                m.get("role") == "system"
                and "## 相关记忆" in m.get("content", "")
            )
        ]

        self._messages.append({"role": "system", "content": memory_block})
        logger.info("Injected %d relevant memories into context", len(results))

    async def _auto_extract_memories(self, task: str, result: AgentResult) -> None:
        """Store high-confidence facts/preferences/decisions from a completed turn."""
        if not self._config.memory.long_term_enabled:
            return
        if result.status != "completed":
            return
        candidates = extract_memory_candidates(task, result.response or "")
        if not candidates:
            return

        session_id = self._session.id if self._session else ""
        for candidate in candidates:
            scope = "global" if candidate.category == "preference" else "session"
            entry = MemoryEntry(
                id="",
                content=candidate.content,
                category=candidate.category,
                metadata={
                    "source": "auto_extract",
                    "reason": candidate.reason,
                    "session_id": session_id,
                    "scope": scope,
                },
            )
            try:
                await self.long_term_memory.store(entry)
            except Exception as e:
                logger.debug("Auto memory extraction failed: %s", e)

    # --- 上下文压缩 ---

    def _append_message(self, msg: dict[str, Any]) -> None:
        """Append to both _messages and _full_history."""
        sanitized_messages, visual_replacements = self._compactor.sanitize_visual_payloads(
            [msg]
        )
        safe_msg = sanitized_messages[0] if sanitized_messages else msg
        if visual_replacements:
            logger.info(
                "Sanitized %d inline visual payloads before appending history",
                visual_replacements,
            )
        self._messages.append(safe_msg)
        self._full_history.append(safe_msg)

    def _coerce_engine_events(
        self,
        candidate: EngineEventPublisher | LegacyEventCallback | None,
    ) -> EngineEventPublisher | None:
        """Normalize temporary direct callback callers at one Engine boundary."""
        if candidate is None:
            return None
        if isinstance(
            candidate,
            (RuntimeEventPublisher, _ObservedRuntimeEventPublisher),
        ):
            return candidate
        if not callable(candidate):
            raise TypeError("Engine 事件消费者必须是 Publisher 或异步事件回调")
        session_id = self._session.id if self._session else ""
        run_id = (
            self._active_harness_run.contract.run_id
            if self._active_harness_run is not None
            else ""
        )
        return RuntimeEventPublisher(
            CallbackEventSink(candidate),
            session_id=session_id,
            run_id=run_id,
        )

    async def _inject_background_notifications(
        self,
        events: EngineEventPublisher | LegacyEventCallback | None = None,
    ) -> None:
        """Inject newly completed background task notifications into context."""
        events = self._coerce_engine_events(events)
        notifications = self.background_runner.collect_notifications()
        if not notifications:
            return
        content = "\n\n".join(notifications)
        self._append_message({
            "role": "user",
            "content": content,
        })
        await self._emit_runtime_notification(
            events,
            source="background",
            title="后台任务通知",
            notifications=notifications,
            content=content,
        )

    async def _inject_scheduler_notifications(
        self,
        events: EngineEventPublisher | LegacyEventCallback | None = None,
    ) -> None:
        """Inject due schedule notifications into context."""
        events = self._coerce_engine_events(events)
        notifications = self.scheduler_runner.collect_notifications()
        if not notifications:
            return
        content = "\n\n".join(notifications)
        self._append_message({
            "role": "user",
            "content": content,
        })
        await self._emit_runtime_notification(
            events,
            source="schedule",
            title="调度提醒",
            notifications=notifications,
            content=content,
        )

    async def _emit_runtime_notification(
        self,
        events: EngineEventPublisher | None,
        *,
        source: str,
        title: str,
        notifications: list[str],
        content: str,
    ) -> None:
        """Make runtime-delivered notifications visible in streaming UIs."""
        if events is None:
            return
        await events.publish(
            RuntimeEventType.RUNTIME_NOTIFICATION,
            {
                "source": source,
                "title": title,
                "count": len(notifications),
                "preview": _notification_preview(content),
                "content": content,
            },
        )

    def _ensure_system_prompt(self) -> None:
        """Ensure the system prompt is present in active and persisted history."""
        prompt = self._build_system_prompt()
        for index, message in enumerate(self._messages):
            if message.get("role") != "system":
                continue
            content = str(message.get("content", ""))
            if is_generated_system_prompt(content):
                refreshed = {**message, "content": prompt}
                self._messages[index] = refreshed
                self._replace_generated_system_prompt_in_full_history(prompt)
            return
        self._append_message({"role": "system", "content": prompt})

    def _build_system_prompt(self) -> str:
        """Build the default prompt from named sections plus safe runtime facts."""
        return build_system_prompt(
            PromptAssemblyInput(
                workspace_root=str(self.workspace_root),
                permission_mode=self._config.safety.permission_mode,
                tool_names=tuple(sorted(self._tool_registry.names)),
                skill_names=tuple(sorted(skill.name for skill in self.skill_loader.all())),
            )
        )

    def _replace_generated_system_prompt_in_full_history(self, prompt: str) -> None:
        for index, message in enumerate(self._full_history):
            if message.get("role") != "system":
                continue
            if is_generated_system_prompt(str(message.get("content", ""))):
                self._full_history[index] = {**message, "content": prompt}
                return

    async def _maybe_compact(
        self,
        events: EngineEventPublisher | LegacyEventCallback | None = None,
    ) -> None:
        """检查并执行上下文压缩."""
        events = self._coerce_engine_events(events)
        model = self._model_port.resolve_model(ModelTier.CAPABLE)
        context_budget, reserve_tokens = self._compute_context_budget(model)
        if context_budget <= 0:
            logger.warning(
                "Context budget invalid (model=%s, reserve=%d)", model, reserve_tokens
            )
            return

        self._messages, visual_replacements = self._compactor.sanitize_visual_payloads(
            self._messages
        )
        if visual_replacements:
            logger.info(
                "Sanitized %d inline visual payloads from context",
                visual_replacements,
            )

        self._messages, archived_tool_results = (
            self._compactor.offload_large_tool_results(self._messages)
        )

        if (
            not archived_tool_results
            and not self._compactor.should_compact(self._messages, context_budget)
        ):
            return

        before = len(self._messages)
        runtime_snapshot, preserved_sections, warnings = (
            await self._build_compaction_runtime_snapshot()
        )
        self._messages = await self._compactor.compact(
            self._messages,
            context_budget,
            runtime_snapshot=runtime_snapshot,
        )
        after = len(self._messages)

        if after < before or archived_tool_results:
            logger.info(
                "Context compacted: %d → %d messages (usable=%d, reserve=%d, archived=%d)",
                before,
                after,
                context_budget,
                reserve_tokens,
                len(archived_tool_results),
            )
            if events is not None:
                await events.publish(
                    RuntimeEventType.CONTEXT_COMPACTED,
                    {
                        "before": before,
                        "after": after,
                        "archived_tool_results": len(archived_tool_results),
                        "preserved_sections": preserved_sections,
                        "warnings": warnings,
                    },
                )

    def _sanitize_messages_for_model(
        self,
        messages: list[dict[str, Any]],
        *,
        update_engine_context: bool = False,
    ) -> list[dict[str, Any]]:
        """Return model-ready messages with inline visual payloads summarized."""
        sanitized, replacements = self._compactor.sanitize_visual_payloads(messages)
        if replacements:
            logger.info(
                "Sanitized %d inline visual payloads before model call",
                replacements,
            )
            if update_engine_context:
                self._messages = sanitized
        return sanitized

    async def _build_compaction_runtime_snapshot(self) -> tuple[str, list[str], list[str]]:
        """Build deterministic state that must survive history compaction."""
        sections: list[str] = []
        preserved: list[str] = []
        warnings: list[str] = []

        task_section, task_warnings = await self._compaction_task_section()
        if task_section:
            sections.append(task_section)
            preserved.append("todo")
            warnings.extend(task_warnings)

        team_section, team_warnings = await self._compaction_team_section()
        if team_section:
            sections.append(team_section)
            preserved.append("team_protocol")
            warnings.extend(team_warnings)

        subagent_section = self._compaction_subagent_section()
        if subagent_section:
            sections.append(subagent_section)
            preserved.append("subagent_events")

        constraint_section = self._compaction_user_constraint_section()
        if constraint_section:
            sections.append(constraint_section)
            preserved.append("recent_user_constraints")

        hook_section = self._compaction_hook_section()
        if hook_section:
            sections.append(hook_section)
            preserved.append("hook_trace")

        if not sections:
            return "当前没有需要额外保留的运行时状态。", [], []
        return "\n\n".join(sections), preserved, warnings

    async def _compaction_task_section(self) -> tuple[str, list[str]]:
        try:
            tasks = await self.task_store.list_tasks()
        except Exception as e:
            logger.debug("Compaction task snapshot failed: %s", e)
            return "### Todo 状态\n- 任务状态读取失败。", ["任务状态读取失败"]
        if not tasks:
            return "", []

        warnings: list[str] = []
        active = [
            task for task in tasks
            if task.status in {TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED}
        ]
        if active:
            warnings.append(f"有 {len(active)} 个未完成/阻塞 todo")

        counts = {
            status: sum(1 for task in tasks if task.status == status)
            for status in TaskStatus
        }
        lines = [
            "### Todo 状态",
            "- 汇总："
            f"{counts[TaskStatus.COMPLETED]} 完成，"
            f"{counts[TaskStatus.IN_PROGRESS]} 进行中，"
            f"{counts[TaskStatus.BLOCKED]} 阻塞，"
            f"{counts[TaskStatus.PENDING]} 待处理",
        ]
        for task in tasks[:8]:
            active_form = f" | 当前：{task.active_form}" if task.active_form else ""
            blocked_by = f" | blocked_by={','.join(task.blocked_by)}" if task.blocked_by else ""
            lines.append(
                f"- #{task.id} [{task.status.value}] {task.subject}"
                f"{active_form}{blocked_by}"
            )
        if len(tasks) > 8:
            lines.append(f"- ... 还有 {len(tasks) - 8} 个")
        return "\n".join(lines), warnings

    async def _compaction_team_section(self) -> tuple[str, list[str]]:
        bus = self.subagent_manager.message_bus
        warnings: list[str] = []
        history = [
            msg for msg in bus.get_history(limit=12)
            if msg.topic.startswith("team.")
            or "team_event_type" in msg.metadata
        ]
        try:
            blackboard = await bus.blackboard_get_all()
        except Exception as e:
            logger.debug("Compaction team blackboard snapshot failed: %s", e)
            blackboard = {}
            warnings.append("团队黑板读取失败")

        team_entries = [
            (key, entry) for key, entry in sorted(blackboard.items())
            if key.startswith("team/")
        ]
        if not history and not team_entries:
            return "", warnings

        blockers = [
            msg for msg in history
            if msg.metadata.get("team_event_type") == "blocker"
            or msg.priority.value == "critical"
        ]
        if blockers:
            warnings.append(f"有 {len(blockers)} 条团队阻塞/高危事件")

        lines = ["### Team Protocol"]
        if history:
            lines.append("- 最近团队消息：")
            for msg in history[-8:]:
                event = msg.metadata.get("team_event_type", msg.topic)
                target = msg.recipient or "广播"
                lines.append(
                    f"  - {event}: {msg.sender} → {target} "
                    f"[{msg.priority.value}] {msg.content[:140]}"
                )
        if team_entries:
            lines.append("- 团队黑板：")
            for key, entry in team_entries[-8:]:
                value = entry.value
                content = (
                    value.get("content", value)
                    if isinstance(value, dict)
                    else value
                )
                lines.append(
                    f"  - {key} (v{entry.version}, {entry.author}): "
                    f"{str(content)[:140]}"
                )
        return "\n".join(lines), warnings

    def _compaction_subagent_section(self) -> str:
        events = self.subagent_manager.get_recent_events(limit=8)
        if not events:
            return ""
        lines = ["### Subagent 事件"]
        for event in events:
            agent = str(event.get("agent_name") or "未匹配")
            status = str(event.get("status") or "?")
            task_id = str(event.get("task_id") or "?")
            message = str(event.get("message") or "")
            lines.append(f"- {status}: {agent} / {task_id} {message[:140]}")
        return "\n".join(lines)

    def _compaction_user_constraint_section(self) -> str:
        keywords = ("不要", "先不", "最后", "注意", "必须", "禁止", "可以", "不要全量")
        constraints: list[str] = []
        for message in self._messages[-24:]:
            if message.get("role") != "user":
                continue
            content = str(message.get("content", "")).strip()
            if not content:
                continue
            if any(keyword in content for keyword in keywords):
                constraints.append(content[:180])
        if not constraints:
            return ""
        lines = ["### 最近用户约束"]
        for item in constraints[-6:]:
            lines.append(f"- {item}")
        return "\n".join(lines)

    def _compaction_hook_section(self) -> str:
        trace = self.hooks.get_trace()[-8:]
        interesting = [
            entry for entry in trace
            if entry.aborted or entry.error
        ]
        if not interesting:
            return ""
        lines = ["### Hook 风险"]
        for entry in interesting:
            status = "aborted" if entry.aborted else "error"
            detail = entry.error or ""
            lines.append(f"- {entry.point}:{entry.callback} {status} {detail[:120]}")
        return "\n".join(lines)

    async def _fire_hook(
        self,
        ctx: HookContext,
        events: EngineEventPublisher | LegacyEventCallback | None = None,
    ) -> HookContext:
        """Fire hooks and optionally emit user-visible trace events."""
        events = self._coerce_engine_events(events)
        trace = self.hooks.get_trace()
        last_sequence = trace[-1].sequence if trace else 0
        result = await self.hooks.fire(ctx)
        if events is not None:
            for entry in self.hooks.get_trace():
                if entry.sequence <= last_sequence:
                    continue
                await events.publish(RuntimeEventType.HOOK_TRACE, {
                    "point": entry.point,
                    "callback": entry.callback,
                    "duration_ms": entry.duration_ms,
                    "aborted": entry.aborted,
                    "error": entry.error,
                })
        return result

    async def _call_model_with_recovery(
        self,
        *,
        messages: list[dict[str, Any]],
        tier: ModelTier,
        tools: list[dict[str, Any]] | None,
        events: EngineEventPublisher | None = None,
        streaming: bool = False,
        cause: Exception | None = None,
    ) -> Any:
        """Call the model and recover once from prompt-too-long failures."""
        try:
            if cause is not None and _is_prompt_too_long_error(cause):
                raise cause
            safe_messages = self._sanitize_messages_for_model(
                messages,
                update_engine_context=messages is self._messages,
            )
            return await self._model_port.call(messages=safe_messages, tier=tier, tools=tools)
        except Exception as e:
            if not _is_prompt_too_long_error(e):
                raise
            recovered = await self._reactive_compact_for_prompt_too_long(
                events=events,
                streaming=streaming,
                error=e,
            )
            if not recovered:
                raise
            safe_messages = self._sanitize_messages_for_model(
                self._messages,
                update_engine_context=True,
            )
            return await self._model_port.call(
                messages=safe_messages,
                tier=tier,
                tools=tools,
            )

    async def _continue_truncated_final_response(
        self,
        *,
        partial_content: str,
        tier: ModelTier,
        events: EngineEventPublisher | None = None,
        streaming: bool = False,
    ) -> str:
        """Continue a final answer cut off by the model output limit."""
        if not partial_content:
            return partial_content

        combined = partial_content
        for attempt in range(1, _MAX_OUTPUT_CONTINUATIONS + 1):
            if events is not None:
                await events.publish(RuntimeEventType.RECOVERY_EVENT, {
                    "reason": "output_truncated",
                    "action": "continue_output",
                    "phase": "started",
                    "attempt": attempt,
                    "before": len(combined),
                    "unit": "chars",
                    "streaming": streaming,
                })

            continuation_messages = [
                *self._messages,
                {"role": "assistant", "content": combined},
                {"role": "user", "content": _OUTPUT_CONTINUATION_PROMPT},
            ]
            response = await self._call_model_with_recovery(
                messages=continuation_messages,
                tier=tier,
                tools=None,
                events=events,
                streaming=streaming,
            )
            self._track_model_usage(response.usage, response.model)

            combined = _join_continued_output(combined, response.content)
            still_truncated = _is_output_truncated(response.finish_reason)
            if events is not None:
                await events.publish(RuntimeEventType.RECOVERY_EVENT, {
                    "reason": "output_truncated",
                    "action": "continue_output",
                    "phase": "continued" if still_truncated else "completed",
                    "attempt": attempt,
                    "before": len(partial_content),
                    "after": len(combined),
                    "unit": "chars",
                    "streaming": streaming,
                })

            if not still_truncated:
                break

        return combined

    async def _reactive_compact_for_prompt_too_long(
        self,
        *,
        events: EngineEventPublisher | None = None,
        streaming: bool = False,
        error: Exception,
    ) -> bool:
        """Aggressively make room after a prompt-too-long model error."""
        before = len(self._messages)
        self._messages = [
            message for message in self._messages
            if not is_harness_context_message(message)
        ]
        base_count = len(self._messages)
        runtime_snapshot, preserved_sections, warnings = (
            await self._build_compaction_runtime_snapshot()
        )
        if events is not None:
            await events.publish(RuntimeEventType.RECOVERY_EVENT, {
                "reason": "prompt_too_long",
                "action": "reactive_compact_retry",
                "phase": "started",
                "before": before,
                "streaming": streaming,
                "error": str(error)[:240],
            })

        self._messages = await self._compactor.compact(
            self._messages,
            max_tokens=1,
            runtime_snapshot=runtime_snapshot,
        )

        if len(self._messages) >= base_count:
            self._messages = _fallback_reactive_compact_messages(
                self._messages,
                runtime_snapshot=runtime_snapshot,
            )

        await self._inject_harness_context_snapshot(events)
        after = len(self._messages)
        recovered = after < before
        if events is not None:
            await events.publish(RuntimeEventType.RECOVERY_EVENT, {
                "reason": "prompt_too_long",
                "action": "reactive_compact_retry",
                "phase": "completed" if recovered else "failed",
                "before": before,
                "after": after,
                "streaming": streaming,
                "preserved_sections": preserved_sections,
                "warnings": warnings,
            })
        logger.warning(
            "Reactive compact for prompt_too_long: %d → %d messages (recovered=%s)",
            before,
            after,
            recovered,
        )
        return recovered

    async def _inject_harness_context_snapshot(
        self,
        events: EngineEventPublisher | None = None,
    ) -> None:
        """Inject one ephemeral system snapshot of current harness state."""
        self._messages = [
            message for message in self._messages
            if not is_harness_context_message(message)
        ]
        session_id = self._session.id if self._session else ""
        start_ctx = await self._fire_hook(HookContext(
            point=HookPoint.CONTEXT_ASSEMBLE_START,
            data={"message_count": len(self._messages)},
            session_id=session_id,
        ), events)
        if start_ctx.should_abort:
            return
        snapshot = await self._harness_context.assemble(
            HarnessContextInput(
                tool_registry=self._tool_registry,
                skill_loader=self.skill_loader,
                task_store=self.task_store,
                background_runner=self.background_runner,
                scheduler_runner=self.scheduler_runner,
                worktree_manager=self.worktree_manager,
                goal_store=self.goal_store,
                pursuit_store=self.pursuit_store,
                mcp_manager=self._mcp_manager,
                context_info=self.get_context_info(),
                budget_info=self.get_budget_info(),
            )
        )
        extra_sections: list[str] = []
        if self._active_harness_run is not None:
            extra_sections.append(self._active_harness_run.context)
        knowledge_result = None
        try:
            current_task = self._latest_user_task()
            if current_task:
                model = self._model_port.resolve_model(ModelTier.CAPABLE)
                model_window = self._model_port.get_context_window(model)
                knowledge_result = await self.harness_service.knowledge_context(
                    current_task,
                    model_window=model_window,
                )
                if knowledge_result.bundle is not None:
                    extra_sections.append(knowledge_result.bundle.rendered)
        except Exception as exc:
            logger.warning("Harness knowledge context unavailable: %s", exc)
        if events is not None and knowledge_result is not None:
            await events.publish(RuntimeEventType.HARNESS_KNOWLEDGE, {
                "status": knowledge_result.code.value,
                "cache_hit": knowledge_result.cache_hit,
                "elapsed_ms": knowledge_result.elapsed_ms,
                "tokens": (
                    knowledge_result.bundle.total_tokens
                    if knowledge_result.bundle is not None
                    else 0
                ),
                "sources": (
                    list(knowledge_result.bundle.source_paths)
                    if knowledge_result.bundle is not None
                    else []
                ),
            })
        end_ctx = await self._fire_hook(HookContext(
            point=HookPoint.CONTEXT_ASSEMBLE_END,
            data={"snapshot": snapshot, "extra_sections": extra_sections},
            session_id=session_id,
        ), events)
        snapshot = str(end_ctx.data.get("snapshot", snapshot))
        extra_sections = end_ctx.data.get("extra_sections", [])
        if isinstance(extra_sections, str):
            extra_sections = [extra_sections]
        if isinstance(extra_sections, list) and extra_sections:
            snapshot = _append_harness_context_sections(
                snapshot,
                [str(section) for section in extra_sections if str(section).strip()],
            )
        self._messages.append({"role": "system", "content": snapshot})

    def _latest_user_task(self) -> str:
        """Return the latest user text without consulting persistent history."""
        for message in reversed(self._messages):
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
        return ""

    async def _begin_harness_completion_run(
        self,
        task: str,
        *,
        run_id: str,
    ) -> None:
        session_id = self._session.id if self._session else ""
        self._active_harness_run = await self.harness_service.begin_completion_run(
            task=task,
            run_id=run_id,
            session_id=session_id,
        )

    async def _observe_harness_tool_event(
        self,
        event: str,
        data: dict[str, Any],
    ) -> None:
        state = self._active_harness_run
        if state is None or state.finalized:
            return
        try:
            await self.harness_service.observe_tool_event(
                run_id=state.contract.run_id,
                event=event,
                data=data,
            )
        except Exception as exc:
            logger.warning("Harness EvidenceCollector event failed: %s", exc)

    async def _evaluate_harness_completion(
        self,
        events: EngineEventPublisher | None = None,
        *,
        force_final: bool = False,
    ) -> CompletionGateResult | None:
        state = self._active_harness_run
        if state is None:
            return None
        if force_final:
            state.correction_attempt = state.contract.correction_attempts
        try:
            tasks = await self.task_store.list_tasks()
            pending_todo_ids = tuple(
                task.id for task in tasks if task.status is not TaskStatus.COMPLETED
            )
        except Exception:
            pending_todo_ids = ()
        result = await self.harness_service.evaluate_completion_run(
            state,
            pending_todo_ids=pending_todo_ids,
        )
        if result.status == "needs_correction":
            self._append_message(
                {"role": "system", "content": result.correction_instruction}
            )
            if events is not None:
                await events.publish(
                    RuntimeEventType.HARNESS_COMPLETION_CORRECTION,
                    {
                        "message": result.correction_instruction,
                        "run_id": state.contract.run_id,
                        "attempt": state.correction_attempt,
                    },
                )
        elif result.receipt is not None and events is not None:
            await events.publish(
                RuntimeEventType.HARNESS_COMPLETION_RECEIPT,
                result.receipt.model_dump(mode="json"),
            )
        return result

    async def _fire_user_prompt_submit(
        self,
        task: str,
        *,
        streaming: bool = False,
        events: EngineEventPublisher | None = None,
    ) -> str | None:
        """Fire user prompt hook and return the possibly rewritten prompt."""
        session_id = self._session.id if self._session else ""
        ctx = await self._fire_hook(HookContext(
            point=HookPoint.USER_PROMPT_SUBMIT,
            data={"prompt": task, "streaming": streaming},
            session_id=session_id,
        ), events)
        if ctx.should_abort:
            return None
        return str(ctx.data.get("prompt", task))

    async def _fire_agent_stop(
        self,
        *,
        status: str,
        response: str,
        reason: str = "",
        streaming: bool = False,
        events: EngineEventPublisher | None = None,
    ) -> None:
        """Fire agent stop hook with final status metadata."""
        session_id = self._session.id if self._session else ""
        await self._fire_hook(HookContext(
            point=HookPoint.AGENT_STOP,
            data={
                "status": status,
                "response_length": len(response or ""),
                "reason": reason,
                "streaming": streaming,
            },
            session_id=session_id,
        ), events)

    async def _emit_task_snapshot(
        self,
        events: EngineEventPublisher | None,
        *,
        source: str,
    ) -> None:
        """Emit a user-visible task list after task tools mutate state."""
        if events is None or source not in _TASK_EVENT_TOOLS:
            return
        try:
            from naumi_agent.tasks.store import format_task_list

            tasks = await self.task_store.list_tasks()
            open_tasks = [task for task in tasks if task.status != TaskStatus.COMPLETED]
            completed_count = len(tasks) - len(open_tasks)
            await events.publish(RuntimeEventType.TASK_SNAPSHOT, {
                "source": source,
                "count": len(tasks),
                "open_count": len(open_tasks),
                "completed_count": completed_count,
                "items": [
                    {
                        "id": task.id,
                        "status": task.status.value,
                        "subject": (
                            task.active_form
                            if task.status in {TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED}
                            and task.active_form
                            else task.subject
                        ),
                    }
                    for task in open_tasks
                ],
                "summary": format_task_list(tasks),
            })
        except Exception as e:
            logger.debug("Task snapshot event failed: %s", e)

    async def _block_unreconciled_todos(
        self,
        events: EngineEventPublisher | None = None,
    ) -> None:
        """Prevent terminal engine exits from leaving active Todo state behind."""
        reconciliation = await reconcile_todos(self.task_store, attempted=True)
        if reconciliation.warning:
            logger.warning("%s", reconciliation.warning)
            if events is not None:
                await events.publish(
                    RuntimeEventType.TASK_RECONCILIATION_WARNING,
                    {"message": reconciliation.warning},
                )
        if reconciliation.action == TodoReconciliationAction.BLOCKED:
            await self._emit_task_snapshot(events, source="todo_reconciliation")

    def _check_budget(self) -> AgentResult | None:
        if not self._budget_tracker.is_exceeded():
            return None

        summary = self._budget_tracker.get_summary()
        budget = self._budget_tracker.budget
        reasons: list[str] = []
        if budget.max_input_tokens is not None and (
            budget.max_input_tokens == 0
            or summary.total_input_tokens > budget.max_input_tokens
        ):
            reasons.append(
                f"输入 token {summary.total_input_tokens:,}/{budget.max_input_tokens:,}"
            )
        if budget.max_output_tokens is not None and (
            budget.max_output_tokens == 0
            or summary.total_output_tokens > budget.max_output_tokens
        ):
            reasons.append(
                f"输出 token {summary.total_output_tokens:,}/{budget.max_output_tokens:,}"
            )
        if budget.max_usd is not None and (
            budget.max_usd == 0 or summary.total_cost_usd > budget.max_usd
        ):
            reasons.append(f"费用 ${summary.total_cost_usd:.4f}/${budget.max_usd:.2f}")

        detail = "，".join(reasons) if reasons else "已超过配置上限"
        message = (
            "预算已耗尽，已停止继续执行，避免产生额外消耗。\n\n"
            f"超限项：{detail}"
        )
        logger.warning("Budget exceeded: %s", detail)
        return AgentResult(
            status="budget_exceeded",
            response=message,
            usage=self._usage,
        )

    async def run(self, task: str, turn_context: str = "") -> AgentResult:
        """执行任务 — 自适应规划 + ReAct 主循环."""
        self._ensure_system_prompt()

        session = await self.get_or_create_session()
        self.task_store.set_session(session.id)

        hooked_task = await self._fire_user_prompt_submit(task)
        if hooked_task is None:
            await self._fire_agent_stop(
                status="error",
                response="用户输入已被 hook 拦截。",
                reason="user_prompt_submit_aborted",
            )
            return AgentResult(
                status="error",
                error="用户输入已被 hook 拦截。",
            )
        task = hooked_task
        self._remove_turn_context_messages()
        self._append_message({"role": "user", "content": task})
        if turn_context:
            self._append_message({"role": "system", "content": turn_context})
        await self._begin_harness_completion_run(
            task,
            run_id=f"agent:{uuid.uuid4().hex}",
        )
        await self._inject_relevant_memories(task)
        tools = self._get_openai_tools_cached()

        session_id = self._session.id if self._session else ""
        await self._fire_hook(HookContext(
            point=HookPoint.ENGINE_RUN_START,
            data={"task": task},
            session_id=session_id,
        ))

        try:
            plan = await self._planner.plan(task)
            exceeded = self._check_budget()
            if exceeded:
                result = exceeded
            elif plan.mode == ExecutionMode.ORCHESTRATOR and hasattr(self, "subagent_manager"):
                result = await self._run_orchestrated(plan, tools)
            else:
                result = await self._react_loop(tools, plan=plan)
        except Exception as e:
            logger.exception("Agent loop failed")
            result = AgentResult(status="error", error=self._format_error(e))

        if (
            result.status == "completed"
            and self._active_harness_run is not None
            and not self._active_harness_run.finalized
        ):
            gate = await self._evaluate_harness_completion(force_final=True)
            if gate is not None:
                result.harness_receipt = gate.receipt
                if gate.status in {"completed_unverified", "blocked"}:
                    result.status = gate.status

        await self._fire_hook(HookContext(
            point=HookPoint.ENGINE_RUN_END,
            data={"status": result.status, "task": task},
            session_id=session_id,
        ))

        await self._auto_extract_memories(task, result)
        await self._save_session()

        # Attach task summary if tasks exist
        tasks = await self.task_store.list_tasks()
        if tasks:
            from naumi_agent.tasks.store import format_task_list
            result.task_summary = format_task_list(tasks)

        return result

    async def run_streaming(
        self,
        task: str,
        on_event: EventSink | LegacyEventCallback,
        turn_context: str = "",
    ) -> AgentResult:
        """Execute and durably record one streamed Agent run."""
        session = await self.get_or_create_session()
        self.task_store.set_session(session.id)
        recorder = await ChatRunRecorder.start(
            store=self.chat_run_store,
            workspace_root=self.workspace_root,
            session_id=session.id,
            task=task,
        )
        caller_sink = coerce_event_sink(on_event)
        publisher = RuntimeEventPublisher(
            CompositeEventSink((
                RuntimeInspectorEventSink(self.runtime_inspector.tracker),
                ChatRunRecorderEventSink(recorder),
                self._event_sink,
                caller_sink,
            )),
            session_id=session.id,
            run_id=recorder.run_id,
        )

        previous_feedback_turn = self._active_feedback_turn
        self._active_feedback_turn = FeedbackSourceEnvelope(
            run_id=recorder.run_id,
            user_message_id=recorder.record.user_message_id,
            content_sha256=hashlib.sha256(task.encode("utf-8")).hexdigest(),
            observed_at=recorder.record.started_at,
        )
        try:
            try:
                result = await self._run_streaming_core(
                    task,
                    publisher,
                    turn_context=turn_context,
                    harness_run_id=recorder.run_id,
                )
            finally:
                self._active_feedback_turn = previous_feedback_turn
        except asyncio.CancelledError as exc:
            await self._finish_streaming_run(
                recorder=recorder,
                publisher=publisher,
                status="cancelled",
                summary="运行已由用户取消。",
                original_error=exc,
            )
            raise
        except Exception as exc:
            await self._finish_streaming_run(
                recorder=recorder,
                publisher=publisher,
                status="failed",
                summary=self._format_error(exc),
                original_error=exc,
            )
            raise

        summary = result.response or result.error or "本轮运行已结束。"
        receipt = await self._finish_streaming_run(
            recorder=recorder,
            publisher=publisher,
            status=result.status,
            summary=summary,
        )
        result.receipt = receipt
        return result

    async def _finish_streaming_run(
        self,
        *,
        recorder: ChatRunRecorder,
        publisher: RuntimeEventPublisher,
        status: str,
        summary: str,
        original_error: BaseException | None = None,
    ) -> CompletionReceipt:
        """Persist one terminal receipt before attempting terminal delivery."""
        receipt = await recorder.finish(status, summary)
        try:
            await publisher.publish(
                RuntimeEventType.COMPLETION_RECEIPT,
                receipt.to_dict(),
            )
        except Exception as delivery_error:
            if original_error is None:
                raise
            original_error.add_note(
                "完成回执已持久化，但终端事件发送失败："
                f"{type(delivery_error).__name__}: {delivery_error}"
            )
        return receipt

    async def _run_streaming_core(
        self,
        task: str,
        events: RuntimeEventPublisher,
        turn_context: str = "",
        harness_run_id: str | None = None,
    ) -> AgentResult:
        """执行任务 — 流式 ReAct 主循环，通过 Publisher 推送类型化事件."""
        perf_start = time.perf_counter()
        latency_start = perf_start
        first_progress_recorded = False
        first_model_chunk_recorded = False
        first_token_recorded = False
        progress_events = {
            "perf_phase",
            "thinking_start",
            "thinking_delta",
            "response_start",
            "token",
            "tool_prepare_start",
            "tool_prepare_snapshot",
            "tool_start",
            "error",
        }

        async def emit_latency_metric(
            metric: str,
            label: str,
            now: float,
            *,
            turn: int = 0,
        ) -> None:
            await events.publish(
                RuntimeEventType.LATENCY_METRIC,
                {
                    "metric": metric,
                    "label": label,
                    "duration_ms": int((now - latency_start) * 1000),
                    "turn": turn,
                },
                turn=turn,
            )

        async def observe_event(
            event: RuntimeEventType,
            data: Mapping[str, object],
            turn: int,
        ) -> None:
            nonlocal first_progress_recorded
            nonlocal first_model_chunk_recorded
            nonlocal first_token_recorded

            if event is RuntimeEventType.LATENCY_METRIC:
                return

            now = time.perf_counter()
            event_name = event.value
            if not first_progress_recorded and event_name in progress_events:
                first_progress_recorded = True
                await emit_latency_metric("first_progress", "首反馈", now, turn=turn)

            if (
                not first_model_chunk_recorded
                and event is RuntimeEventType.PERF_PHASE
                and data.get("phase") == "llm_first_chunk"
            ):
                first_model_chunk_recorded = True
                await emit_latency_metric("first_model_chunk", "模型首包", now, turn=turn)

            if (
                not first_token_recorded
                and event is RuntimeEventType.TOKEN
                and data.get("content")
            ):
                first_token_recorded = True
                await emit_latency_metric("first_token", "端到端首字", now, turn=turn)

        observed_events = _ObservedRuntimeEventPublisher(events, observe_event)

        async def emit_perf_phase(
            phase: str,
            label: str,
            start: float,
            **extra: Any,
        ) -> None:
            await observed_events.publish(
                RuntimeEventType.PERF_PHASE,
                {
                    "phase": phase,
                    "label": label,
                    "duration_ms": int((time.perf_counter() - start) * 1000),
                    **extra,
                },
            )

        await observed_events.publish(RuntimeEventType.RUN_STARTED, {"task": task})
        self._ensure_system_prompt()

        phase_start = time.perf_counter()
        session = await self.get_or_create_session()
        self.task_store.set_session(session.id)
        await emit_perf_phase("session_prepare", "会话准备", phase_start)

        phase_start = time.perf_counter()
        hooked_task = await self._fire_user_prompt_submit(
            task,
            streaming=True,
            events=observed_events,
        )
        await emit_perf_phase("prompt_hooks", "输入 Hook", phase_start)
        if hooked_task is None:
            message = "用户输入已被 hook 拦截。"
            await observed_events.publish(RuntimeEventType.ERROR, {"message": message})
            await self._fire_agent_stop(
                status="error",
                response=message,
                reason="user_prompt_submit_aborted",
                streaming=True,
                events=observed_events,
            )
            return AgentResult(status="error", error=message)
        task = hooked_task
        self._remove_turn_context_messages()
        self._append_message({"role": "user", "content": task})
        if turn_context:
            self._append_message({"role": "system", "content": turn_context})
        await self._begin_harness_completion_run(
            task,
            run_id=harness_run_id or f"agent:{uuid.uuid4().hex}",
        )
        phase_start = time.perf_counter()
        await self._inject_relevant_memories(task)
        await emit_perf_phase("memory_recall", "记忆召回", phase_start)
        phase_start = time.perf_counter()
        tools = self._get_openai_tools_cached()
        await emit_perf_phase(
            "tool_schema",
            "工具 Schema",
            phase_start,
            tool_count=len(tools or []),
        )

        session_id = self._session.id if self._session else ""
        phase_start = time.perf_counter()
        await self._fire_hook(HookContext(
            point=HookPoint.ENGINE_RUN_START,
            data={"task": task, "streaming": True},
            session_id=session_id,
        ), observed_events)
        await emit_perf_phase("engine_start_hooks", "启动 Hook", phase_start)

        try:
            phase_start = time.perf_counter()
            if self._should_preplan_streaming(task):
                plan = await self._planner.plan(task)
                planning_mode = str(plan.mode)
            else:
                plan = None
                planning_mode = "skipped_for_streaming"
            await emit_perf_phase(
                "planning",
                "规划",
                phase_start,
                mode=planning_mode,
            )
            exceeded = self._check_budget()
            if exceeded:
                result = exceeded
            elif (
                plan is not None
                and plan.mode == ExecutionMode.ORCHESTRATOR
                and hasattr(self, "subagent_manager")
            ):
                result = await self._run_orchestrated(plan, tools)
            else:
                result = await self._react_loop_streaming(
                    tools,
                    observed_events,
                    plan=plan,
                )
        except Exception as e:
            logger.exception("Agent streaming loop failed")
            error_msg = self._format_error(e)
            await observed_events.publish(
                RuntimeEventType.ERROR,
                {"message": error_msg},
            )
            result = AgentResult(status="error", error=error_msg)

        if (
            result.status == "completed"
            and self._active_harness_run is not None
            and not self._active_harness_run.finalized
        ):
            gate = await self._evaluate_harness_completion(
                observed_events,
                force_final=True,
            )
            if gate is not None:
                result.harness_receipt = gate.receipt
                if gate.status in {"completed_unverified", "blocked"}:
                    result.status = gate.status

        phase_start = time.perf_counter()
        await self._fire_hook(HookContext(
            point=HookPoint.ENGINE_RUN_END,
            data={"status": result.status, "task": task, "streaming": True},
            session_id=session_id,
        ), observed_events)
        await emit_perf_phase("engine_end_hooks", "结束 Hook", phase_start)

        phase_start = time.perf_counter()
        await self._auto_extract_memories(task, result)
        await self._save_session()
        await emit_perf_phase("persistence", "保存会话", phase_start)
        await emit_perf_phase("run_total", "总耗时", perf_start, status=result.status)

        # Attach task summary if tasks exist
        tasks = await self.task_store.list_tasks()
        if tasks:
            from naumi_agent.tasks.store import format_task_list
            result.task_summary = format_task_list(tasks)

        return result

    def _remove_turn_context_messages(self) -> None:
        marker = "<naumi_turn_context>"
        self._messages = [
            message
            for message in self._messages
            if marker not in str(message.get("content", ""))
        ]
        self._full_history = [
            message
            for message in self._full_history
            if marker not in str(message.get("content", ""))
        ]

    async def _run_orchestrated(
        self, plan: Any, tools: list[dict[str, Any]] | None
    ) -> AgentResult:
        """执行编排模式：按 DAG 依赖关系委派子任务给专用 Agent."""
        from naumi_agent.orchestrator.subagent_manager import SubTask

        tasks = [
            SubTask(
                id=step.id,
                description=step.description,
                agent_name=None,
                depends_on=step.depends_on,
            )
            for step in plan.steps
        ]

        results = await self.subagent_manager.execute_dag(tasks)

        combined_parts = []
        total_tokens = 0
        total_cost = 0.0
        failures: list[str] = []
        for step in plan.steps:
            r = results.get(step.id)
            if r and r.status == "completed":
                combined_parts.append(f"## {step.description}\n{r.response[:2000]}")
            elif r:
                failure = f"{step.description}: {r.status}"
                if r.error:
                    failure += f" - {r.error}"
                failures.append(failure)
                combined_parts.append(
                    f"## {step.description}\n⚠️ {r.status}: {r.error or ''}"
                )
            else:
                failures.append(f"{step.description}: missing_result")
                combined_parts.append(
                    f"## {step.description}\n⚠️ missing_result: 子任务没有返回结果"
                )

            if r:
                total_tokens += r.total_tokens
                total_cost += r.total_cost_usd

        self._track_model_usage(
            TokenUsage(
                input_tokens=0,
                output_tokens=total_tokens,
                total_tokens=total_tokens,
                cost_usd=total_cost,
            ),
            "subagent-orchestrator",
        )
        budget_result = self._check_budget()
        if budget_result is not None:
            return budget_result

        response = "\n\n".join(combined_parts)
        self._append_message({"role": "assistant", "content": response})
        return AgentResult(
            status="error" if failures else "completed",
            response=response,
            usage=self._usage,
            error="\n".join(failures) if failures else None,
        )

    def _is_repeated_tool_call(
        self, tool_name: str, args: str, history: list[str]
    ) -> bool:
        """Detect if the same tool+args has been called 3+ consecutive times."""
        sig = f"{tool_name}:{args}"
        if len(history) < 2:
            return False
        return history[-1] == sig and history[-2] == sig

    async def _execute_tool_calls(
        self,
        raw_calls: list[dict[str, Any]],
        *,
        tool_call_history: list[str],
        session_id: str,
        turn: int,
        events: EngineEventPublisher | None = None,
    ) -> list[str]:
        """Execute one model tool-call response with deterministic ordering."""
        scheduled: list[ScheduledToolCall] = []
        outcomes: dict[int, ToolResult] = {}
        parsed_calls: dict[int, ToolCall] = {}
        observed_tool_indices: set[int] = set()
        signatures: list[str] = []
        skip_remaining_reason = ""

        for index, raw_call in enumerate(raw_calls):
            call = self._parse_tool_call(raw_call)
            if call is None:
                call_id = self._extract_tool_call_id(raw_call)
                if call_id:
                    outcomes[index] = ToolResult(
                        call_id=call_id,
                        status="error",
                        content="工具调用格式无效，无法解析函数名称或参数。",
                    )
                continue

            parsed_calls[index] = call
            signatures.append(f"{call.name}:{call.arguments}")
            if skip_remaining_reason:
                outcomes[index] = ToolResult(
                    call_id=call.id,
                    status="skipped",
                    content=skip_remaining_reason,
                )
                continue
            if self._is_repeated_tool_call(
                call.name,
                call.arguments,
                tool_call_history,
            ):
                logger.warning(
                    "Repeated tool call detected: %s, injecting stop",
                    call.name,
                )
                skip_remaining_reason = _REPEATED_TOOL_CALL_MESSAGE
                outcomes[index] = ToolResult(
                    call_id=call.id,
                    status="skipped",
                    content=skip_remaining_reason,
                )
                continue
            scheduled.append(ScheduledToolCall(index=index, call=call))

        batches = build_tool_batches(
            scheduled,
            self._tool_registry,
            max_parallel_tools=self._config.safety.max_parallel_tools,
        )
        for batch_number, batch in enumerate(batches, start=1):
            batch_id = f"turn-{turn}-batch-{batch_number}"
            batch_started_at = time.perf_counter()
            metadata = {
                "batch_id": batch_id,
                "batch_size": len(batch.calls),
                "parallel": batch.parallel,
            }
            logger.debug(
                "Tool batch started: id=%s size=%d parallel=%s",
                batch_id,
                len(batch.calls),
                batch.parallel,
            )
            ready: list[ScheduledToolCall] = []
            for item in batch.calls:
                observed_tool_indices.add(item.index)
                registered_tool = self._tool_registry.get(item.call.name)
                start_payload = {
                    "name": item.call.name,
                    "call_id": item.call.id,
                    "args": item.call.arguments,
                    "read_only": bool(
                        registered_tool is not None and registered_tool.is_read_only
                    ),
                    "destructive": bool(
                        registered_tool is not None and registered_tool.is_destructive
                    ),
                    **metadata,
                }
                await self._observe_harness_tool_event("tool_start", start_payload)
                if events is not None:
                    await events.publish(
                        RuntimeEventType.TOOL_START,
                        start_payload,
                        turn=turn,
                    )
                hook_ctx = await self._fire_hook(
                    HookContext(
                        point=HookPoint.TOOL_EXECUTE_START,
                        data={
                            "tool_name": item.call.name,
                            "arguments": item.call.arguments,
                        },
                        session_id=session_id,
                    ),
                    events,
                )
                if hook_ctx.should_abort:
                    reason = hook_ctx.data.get("abort_reason", "未提供原因")
                    outcomes[item.index] = ToolResult(
                        call_id=item.call.id,
                        status="aborted",
                        content=f"被 Hook 中止：{reason}",
                    )
                    continue
                ready.append(item)

            if ready:
                executable_batch = ToolBatch(
                    calls=tuple(ready),
                    parallel=batch.parallel and len(ready) > 1,
                )
                executed = await execute_tool_batch(
                    executable_batch,
                    lambda call: self.execute_tool(call, _events=events),
                )
                for item in executed:
                    if item.result is not None:
                        outcomes[item.index] = item.result
                    else:
                        assert item.exception is not None
                        logger.error(
                            "Parallel tool execution failed: %s",
                            item.call.name,
                            exc_info=(
                                type(item.exception),
                                item.exception,
                                item.exception.__traceback__,
                            ),
                        )
                        outcomes[item.index] = ToolResult(
                            call_id=item.call.id,
                            status="error",
                            content=(
                                "工具执行失败："
                                f"{type(item.exception).__name__}"
                            ),
                        )

            for item in batch.calls:
                result = outcomes[item.index]
                registered_tool = self._tool_registry.get(item.call.name)
                end_payload = {
                    "name": item.call.name,
                    "call_id": item.call.id,
                    "status": result.status,
                    "duration_ms": result.duration_ms,
                    "content": result.content[:2000],
                    "content_length": len(
                        result.content.encode("utf-8", errors="replace")
                    ),
                    "read_only": bool(
                        registered_tool is not None and registered_tool.is_read_only
                    ),
                    "destructive": bool(
                        registered_tool is not None and registered_tool.is_destructive
                    ),
                    **metadata,
                }
                await self._observe_harness_tool_event("tool_end", end_payload)
                if events is not None:
                    await events.publish(
                        RuntimeEventType.TOOL_END,
                        end_payload,
                        turn=turn,
                    )
                await self._fire_hook(
                    HookContext(
                        point=HookPoint.TOOL_EXECUTE_END,
                        data={
                            "tool_name": item.call.name,
                            "status": result.status,
                            "duration_ms": result.duration_ms,
                            "content_length": len(result.content),
                        },
                        session_id=session_id,
                    ),
                    events,
                )
            logger.debug(
                "Tool batch completed: id=%s elapsed_ms=%d",
                batch_id,
                int((time.perf_counter() - batch_started_at) * 1000),
            )

        for index, result in outcomes.items():
            if index in observed_tool_indices:
                continue
            call = parsed_calls.get(index)
            raw_call = raw_calls[index]
            function = raw_call.get("function")
            function_data = function if isinstance(function, dict) else {}
            tool_name = (
                call.name
                if call is not None
                else str(
                    function_data.get("name")
                    or raw_call.get("name")
                    or "invalid_tool_call"
                )
            )
            raw_arguments = (
                call.arguments
                if call is not None
                else function_data.get("arguments", raw_call.get("arguments", "{}"))
            )
            arguments = (
                raw_arguments
                if isinstance(raw_arguments, str)
                else json.dumps(raw_arguments, ensure_ascii=False, default=str)
            )
            registered_tool = self._tool_registry.get(tool_name)
            metadata = {
                "batch_id": f"turn-{turn}-preflight",
                "batch_size": 1,
                "parallel": False,
            }
            start_payload = {
                "name": tool_name,
                "call_id": result.call_id,
                "args": arguments,
                "read_only": bool(
                    registered_tool is not None and registered_tool.is_read_only
                ),
                "destructive": bool(
                    registered_tool is not None and registered_tool.is_destructive
                ),
                **metadata,
            }
            end_payload = {
                "name": tool_name,
                "call_id": result.call_id,
                "status": result.status,
                "duration_ms": result.duration_ms,
                "content": result.content[:2000],
                "content_length": len(
                    result.content.encode("utf-8", errors="replace")
                ),
                "read_only": start_payload["read_only"],
                "destructive": start_payload["destructive"],
                **metadata,
            }
            await self._observe_harness_tool_event("tool_start", start_payload)
            await self._observe_harness_tool_event("tool_end", end_payload)
            if events is not None:
                await events.publish(
                    RuntimeEventType.TOOL_START,
                    start_payload,
                    turn=turn,
                )
                await events.publish(
                    RuntimeEventType.TOOL_END,
                    end_payload,
                    turn=turn,
                )

        for index, raw_call in enumerate(raw_calls):
            result = outcomes.get(index)
            if result is None:
                continue
            self._append_message(
                {
                    "role": "tool",
                    "tool_call_id": result.call_id,
                    "content": result.content,
                }
            )

        return signatures

    def _format_plan_as_guidance(self, plan: Plan) -> str | None:
        """Format a plan as system message guidance for decision commitment."""
        if plan.approach == "直接执行" or len(plan.steps) <= 1:
            return None
        lines = ["## 执行计划指导"]
        lines.append(f"策略：{plan.approach}")
        lines.append(f"任务理解：{plan.understanding}")
        lines.append("执行步骤：")
        for i, step in enumerate(plan.steps, 1):
            tool_hint = f"（工具：{step.tool}）" if step.tool else ""
            lines.append(f"  {i}. {step.description} {tool_hint}")
        if plan.potential_issues:
            lines.append(f"注意事项：{'、'.join(plan.potential_issues)}")
        lines.append("")
        lines.append(
            "请优先按此计划推进；如果工具结果证明某一步不可行，"
            "可以调整下一步，但要基于已有证据简短说明原因。"
        )
        return "\n".join(lines)

    async def _react_loop(
        self,
        tools: list[dict[str, Any]] | None,
        plan: Plan | None = None,
    ) -> AgentResult:
        """ReAct 循环：推理 → 行动 → 观察."""
        max_turns = self._config.safety.max_turns
        tool_call_history: list[str] = []
        todo_reconciliation_attempted = False

        # Inject plan as guidance to prevent approach oscillation
        if plan:
            plan_guidance = self._format_plan_as_guidance(plan)
            if plan_guidance:
                self._append_message({"role": "system", "content": plan_guidance})

        for turn in range(max_turns):
            self._usage.turns = turn + 1
            await self._inject_background_notifications()
            await self._inject_scheduler_notifications()

            exceeded = self._check_budget()
            if exceeded:
                await self._block_unreconciled_todos()
                await self._fire_agent_stop(
                    status=exceeded.status,
                    response=exceeded.response,
                    reason="budget_exceeded",
                )
                return exceeded

            await self._maybe_compact()
            await self._inject_harness_context_snapshot()

            # --- 推理：调用 LLM ---
            session_id = self._session.id if self._session else ""
            await self._fire_hook(HookContext(
                point=HookPoint.LLM_CALL_START,
                data={"turn": turn + 1, "message_count": len(self._messages)},
                session_id=session_id,
            ))
            response = await self._call_model_with_recovery(
                messages=self._messages,
                tier=ModelTier.CAPABLE,
                tools=tools,
            )
            self._track_model_usage(response.usage, response.model)
            await self._fire_hook(HookContext(
                point=HookPoint.LLM_CALL_END,
                data={
                    "turn": turn + 1,
                    "model": response.model,
                    "total_tokens": response.usage.total_tokens,
                    "cost_usd": response.usage.cost_usd,
                    "has_tool_calls": bool(response.tool_calls),
                },
                session_id=session_id,
            ))

            exceeded = self._check_budget()
            if exceeded:
                await self._block_unreconciled_todos()
                await self._fire_agent_stop(
                    status=exceeded.status,
                    response=exceeded.response,
                    reason="budget_exceeded",
                )
                return exceeded

            # --- 行动：处理工具调用 ---
            if response.tool_calls:
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": response.content or None,
                    "tool_calls": response.tool_calls,
                }
                if response.reasoning_content:
                    assistant_msg["reasoning_content"] = response.reasoning_content
                self._append_message(assistant_msg)

                cur_calls = await self._execute_tool_calls(
                    response.tool_calls,
                    tool_call_history=tool_call_history,
                    session_id=session_id,
                    turn=turn + 1,
                )
                tool_call_history.extend(cur_calls)

                exceeded = self._check_budget()
                if exceeded:
                    await self._block_unreconciled_todos()
                    await self._fire_agent_stop(
                        status=exceeded.status,
                        response=exceeded.response,
                        reason="budget_exceeded",
                    )
                    return exceeded

                continue

            # --- 无工具调用：最终回答 ---
            tool_call_history.clear()
            reconciliation = await reconcile_todos(
                self.task_store,
                attempted=todo_reconciliation_attempted,
            )
            if reconciliation.warning:
                logger.warning("%s", reconciliation.warning)
            if reconciliation.action == TodoReconciliationAction.RETRY:
                todo_reconciliation_attempted = True
                self._append_message(
                    {"role": "system", "content": reconciliation.instruction}
                )
                continue
            harness_gate = await self._evaluate_harness_completion()
            if harness_gate is not None and harness_gate.status == "needs_correction":
                continue
            final_content = response.content
            if _is_output_truncated(response.finish_reason):
                final_content = await self._continue_truncated_final_response(
                    partial_content=response.content,
                    tier=ModelTier.CAPABLE,
                )
            safe_content = self._output_guardrail.redact(final_content)
            self._append_message({"role": "assistant", "content": final_content})
            final_status = (
                harness_gate.status
                if harness_gate is not None
                and harness_gate.status in {"completed_unverified", "blocked"}
                else "completed"
            )
            await self._fire_agent_stop(
                status=final_status,
                response=safe_content,
                reason="final_response",
            )
            return AgentResult(
                status=final_status,
                response=safe_content,
                usage=self._usage,
                harness_receipt=(
                    harness_gate.receipt if harness_gate is not None else None
                ),
            )

        await self._block_unreconciled_todos()
        await self._fire_agent_stop(
            status="max_turns",
            response="已达到最大轮次限制，任务未完成。",
            reason="max_turns",
        )
        return AgentResult(
            status="max_turns",
            response="已达到最大轮次限制，任务未完成。",
            usage=self._usage,
        )

    async def _react_loop_streaming(
        self,
        tools: list[dict[str, Any]] | None,
        event_source: EngineEventPublisher | LegacyEventCallback,
        plan: Plan | None = None,
    ) -> AgentResult:
        """流式 ReAct 循环：通过 router.stream() 逐 token 输出."""
        events = self._coerce_engine_events(event_source)
        if events is None:
            raise TypeError("流式 ReAct 循环需要事件 Publisher")
        max_turns = self._config.safety.max_turns
        model_str = self._model_port.resolve_model(ModelTier.CAPABLE)
        session_id = self._session.id if self._session else ""
        tool_call_history: list[str] = []
        todo_reconciliation_attempted = False

        # Inject plan as guidance to prevent approach oscillation
        if plan:
            plan_guidance = self._format_plan_as_guidance(plan)
            if plan_guidance:
                self._append_message({"role": "system", "content": plan_guidance})

        for turn in range(max_turns):
            self._usage.turns = turn + 1
            await self._inject_background_notifications(events)
            await self._inject_scheduler_notifications(events)

            exceeded = self._check_budget()
            if exceeded:
                await self._block_unreconciled_todos(events)
                await self._fire_agent_stop(
                    status=exceeded.status,
                    response=exceeded.response,
                    reason="budget_exceeded",
                    streaming=True,
                    events=events,
                )
                return exceeded

            phase_start = time.perf_counter()
            await self._maybe_compact(events)
            await self._inject_harness_context_snapshot(events)
            await events.publish(
                RuntimeEventType.PERF_PHASE,
                {
                    "phase": "context_prepare",
                    "label": "上下文准备",
                    "duration_ms": int((time.perf_counter() - phase_start) * 1000),
                    "turn": turn + 1,
                },
                turn=turn + 1,
            )
            await events.publish(
                RuntimeEventType.TURN_START,
                {"turn": turn + 1, "model": model_str},
                turn=turn + 1,
            )

            text_parts: list[str] = []
            pending_text_parts: list[str] = []
            thinking_parts: list[str] = []
            collected_tool_calls: dict[int, dict[str, Any]] = {}
            got_response = False
            got_thinking = False
            stream_tokens = 0
            finish_reason: str | None = None
            try:
                guard_todo_final = any(
                    task.status == TaskStatus.IN_PROGRESS
                    for task in await self.task_store.list_tasks()
                )
            except Exception:
                guard_todo_final = False
            guard_harness_final = self._active_harness_run is not None
            should_guard_text = bool(tools) or guard_todo_final or guard_harness_final
            tool_call_started = False
            tool_prepare_started = False
            tool_prepare_start = 0.0
            last_tool_prepare_emit = 0.0
            last_tool_prepare_arg_chars = 0
            last_tool_prepare_signature = ""
            llm_start = 0.0
            first_chunk_seen = False

            async def flush_pending_text() -> None:
                nonlocal got_response
                if not pending_text_parts:
                    return
                if not got_response:
                    got_response = True
                    await events.publish(
                        RuntimeEventType.RESPONSE_START,
                        {},
                        turn=turn + 1,
                    )
                await events.publish(
                    RuntimeEventType.TOKEN,
                    {"content": "".join(pending_text_parts)},
                    turn=turn + 1,
                )
                pending_text_parts.clear()

            await self._fire_hook(HookContext(
                point=HookPoint.LLM_CALL_START,
                data={"turn": turn + 1, "streaming": True, "message_count": len(self._messages)},
                session_id=session_id,
            ), events)

            try:
                llm_start = time.perf_counter()
                stream_messages = self._sanitize_messages_for_model(
                    self._messages,
                    update_engine_context=True,
                )
                async for chunk in self._model_port.stream(
                    messages=stream_messages,
                    tier=ModelTier.CAPABLE,
                    tools=tools,
                ):
                    if not first_chunk_seen:
                        first_chunk_seen = True
                        await events.publish(
                            RuntimeEventType.PERF_PHASE,
                            {
                                "phase": "llm_first_chunk",
                                "label": "模型首包",
                                "duration_ms": int(
                                    (time.perf_counter() - llm_start) * 1000
                                ),
                                "turn": turn + 1,
                            },
                            turn=turn + 1,
                        )
                    if chunk.usage:
                        self._track_model_usage(chunk.usage, model_str)
                        stream_tokens = chunk.usage.total_tokens

                    if chunk.finish_reason and chunk.finish_reason != "stop":
                        finish_reason = chunk.finish_reason

                    if chunk.thinking:
                        if not got_thinking:
                            got_thinking = True
                            await events.publish(
                                RuntimeEventType.THINKING_START,
                                {},
                                turn=turn + 1,
                            )
                        thinking_parts.append(chunk.thinking)
                        await events.publish(
                            RuntimeEventType.THINKING_DELTA,
                            {"content": chunk.thinking},
                            turn=turn + 1,
                        )

                    if chunk.tool_call_started:
                        tool_call_started = True
                        pending_text_parts.clear()
                        if not tool_prepare_started:
                            tool_prepare_started = True
                            tool_prepare_start = time.perf_counter()
                            last_tool_prepare_emit = tool_prepare_start
                            data = _summarize_tool_prepare_snapshot(
                                chunk.tool_call_snapshot,
                                started_at=tool_prepare_start,
                                now=tool_prepare_start,
                            )
                            last_tool_prepare_arg_chars = int(
                                data.get("argument_chars", 0) or 0
                            )
                            last_tool_prepare_signature = _tool_prepare_signature(data)
                            await events.publish(
                                RuntimeEventType.TOOL_PREPARE_START,
                                data,
                                turn=turn + 1,
                            )

                    if chunk.tool_call_snapshot:
                        if not tool_prepare_started:
                            tool_prepare_started = True
                            tool_prepare_start = time.perf_counter()
                            last_tool_prepare_emit = tool_prepare_start
                        now = time.perf_counter()
                        data = _summarize_tool_prepare_snapshot(
                            chunk.tool_call_snapshot,
                            started_at=tool_prepare_start,
                            now=now,
                        )
                        signature = _tool_prepare_signature(data)
                        arg_chars = int(data.get("argument_chars", 0) or 0)
                        enough_time = (
                            now - last_tool_prepare_emit
                            >= _TOOL_PREPARE_MIN_INTERVAL
                        )
                        enough_growth = (
                            arg_chars - last_tool_prepare_arg_chars
                            >= _TOOL_PREPARE_MIN_ARG_DELTA
                        )
                        if (
                            signature != last_tool_prepare_signature
                            and (enough_time or enough_growth)
                        ):
                            last_tool_prepare_emit = now
                            last_tool_prepare_arg_chars = arg_chars
                            last_tool_prepare_signature = signature
                            await events.publish(
                                RuntimeEventType.TOOL_PREPARE_SNAPSHOT,
                                data,
                                turn=turn + 1,
                            )

                    if chunk.token:
                        text_parts.append(chunk.token)
                        if tool_call_started:
                            continue
                        if (guard_todo_final or guard_harness_final) and not got_response:
                            pending_text_parts.append(chunk.token)
                            continue
                        if should_guard_text and not got_response:
                            # Tool-capable streaming can emit a few text fragments before
                            # the first tool-call delta arrives. Keep only a small guard
                            # buffer, then release normal answers early instead of waiting
                            # for the whole completion to finish.
                            pending_text_parts.append(chunk.token)
                            if len("".join(pending_text_parts)) >= _TOOL_TEXT_GUARD_CHARS:
                                await flush_pending_text()
                            continue
                        if pending_text_parts:
                            await flush_pending_text()
                        elif not got_response:
                            got_response = True
                            await events.publish(
                                RuntimeEventType.RESPONSE_START,
                                {},
                                turn=turn + 1,
                            )
                        await events.publish(
                            RuntimeEventType.TOKEN,
                            {"content": chunk.token},
                            turn=turn + 1,
                        )

                    if chunk.tool_call and isinstance(chunk.tool_call, dict):
                        collected_tool_calls.update(chunk.tool_call)
                if llm_start:
                    await events.publish(
                        RuntimeEventType.PERF_PHASE,
                        {
                            "phase": "llm_stream",
                            "label": "模型流式",
                            "duration_ms": int(
                                (time.perf_counter() - llm_start) * 1000
                            ),
                            "turn": turn + 1,
                        },
                        turn=turn + 1,
                    )
            except Exception as e:
                logger.warning("Streaming failed, fallback to non-streaming: %s", e)
                response = await self._call_model_with_recovery(
                    messages=self._messages,
                    tier=ModelTier.CAPABLE,
                    tools=tools,
                    events=events,
                    streaming=True,
                    cause=e,
                )
                self._track_model_usage(response.usage, model_str)
                stream_tokens = response.usage.total_tokens
                if response.content:
                    text_parts.append(response.content)
                if response.reasoning_content:
                    got_thinking = True
                    thinking_parts.append(response.reasoning_content)
                if response.tool_calls:
                    collected_tool_calls = {i: tc for i, tc in enumerate(response.tool_calls)}
                finish_reason = response.finish_reason

            await self._fire_hook(HookContext(
                point=HookPoint.LLM_CALL_END,
                data={
                    "turn": turn + 1,
                    "model": model_str,
                    "total_tokens": stream_tokens,
                    "has_tool_calls": bool(collected_tool_calls),
                    "streaming": True,
                },
                session_id=session_id,
            ), events)

            if got_thinking:
                await events.publish(
                    RuntimeEventType.THINKING_END,
                    {"content": "".join(thinking_parts)},
                    turn=turn + 1,
                )

            text_content = "".join(text_parts)
            thinking_content = "".join(thinking_parts)

            exceeded = self._check_budget()
            if exceeded:
                await events.publish(
                    RuntimeEventType.ERROR,
                    {"message": exceeded.response},
                    turn=turn + 1,
                )
                await self._block_unreconciled_todos(events)
                await self._fire_agent_stop(
                    status=exceeded.status,
                    response=exceeded.response,
                    reason="budget_exceeded",
                    streaming=True,
                    events=events,
                )
                return exceeded

            # --- 工具调用 ---
            if collected_tool_calls:
                pending_text_parts.clear()
                if tool_prepare_started:
                    first_snapshot = _summarize_tool_prepare_snapshot(
                        collected_tool_calls,
                        started_at=tool_prepare_start or time.perf_counter(),
                        now=time.perf_counter(),
                    )
                    await events.publish(
                        RuntimeEventType.TOOL_PREPARE_END,
                        first_snapshot,
                        turn=turn + 1,
                    )
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": list(collected_tool_calls.values()),
                }
                if thinking_content:
                    assistant_msg["reasoning_content"] = thinking_content
                self._append_message(assistant_msg)

                cur_calls = await self._execute_tool_calls(
                    list(collected_tool_calls.values()),
                    tool_call_history=tool_call_history,
                    session_id=session_id,
                    turn=turn + 1,
                    events=events,
                )
                tool_call_history.extend(cur_calls)

                exceeded = self._check_budget()
                if exceeded:
                    await self._block_unreconciled_todos(events)
                    return exceeded
                continue

            if tool_prepare_started:
                await events.publish(
                    RuntimeEventType.TOOL_PREPARE_END,
                    {
                        "name": "tool",
                        "argument_chars": 0,
                        "elapsed_ms": int(
                            max(0.0, time.perf_counter() - tool_prepare_start) * 1000
                        ),
                    },
                )

            # --- 最终回答 ---
            tool_call_history.clear()
            reconciliation = await reconcile_todos(
                self.task_store,
                attempted=todo_reconciliation_attempted,
            )
            if reconciliation.warning:
                await events.publish(
                    RuntimeEventType.TASK_RECONCILIATION_WARNING,
                    {"message": reconciliation.warning},
                )
            if reconciliation.action == TodoReconciliationAction.RETRY:
                pending_text_parts.clear()
                todo_reconciliation_attempted = True
                self._append_message(
                    {"role": "system", "content": reconciliation.instruction}
                )
                continue
            if reconciliation.action == TodoReconciliationAction.BLOCKED:
                await self._emit_task_snapshot(
                    events,
                    source="todo_reconciliation",
                )
            harness_gate = await self._evaluate_harness_completion(events)
            if harness_gate is not None and harness_gate.status == "needs_correction":
                pending_text_parts.clear()
                continue
            if pending_text_parts:
                await flush_pending_text()
            elif text_content and not got_response:
                got_response = True
                await events.publish(
                    RuntimeEventType.RESPONSE_START,
                    {},
                    turn=turn + 1,
                )
                await events.publish(
                    RuntimeEventType.TOKEN,
                    {"content": text_content},
                    turn=turn + 1,
                )

            if _is_output_truncated(finish_reason):
                continued_content = await self._continue_truncated_final_response(
                    partial_content=text_content,
                    tier=ModelTier.CAPABLE,
                    events=events,
                    streaming=True,
                )
                continuation_suffix = continued_content[len(text_content):]
                if continuation_suffix:
                    if not got_response:
                        got_response = True
                        await events.publish(
                            RuntimeEventType.RESPONSE_START,
                            {},
                            turn=turn + 1,
                        )
                    await events.publish(
                        RuntimeEventType.TOKEN,
                        {"content": continuation_suffix},
                        turn=turn + 1,
                    )
                text_content = continued_content

            if got_response:
                await events.publish(
                    RuntimeEventType.RESPONSE_END,
                    {},
                    turn=turn + 1,
                )
            self._append_message({"role": "assistant", "content": text_content})
            safe_content = self._output_guardrail.redact(text_content)
            final_status = (
                harness_gate.status
                if harness_gate is not None
                and harness_gate.status in {"completed_unverified", "blocked"}
                else "completed"
            )
            await self._fire_agent_stop(
                status=final_status,
                response=safe_content,
                reason="final_response",
                streaming=True,
                events=events,
            )
            return AgentResult(
                status=final_status,
                response=safe_content,
                usage=self._usage,
                harness_receipt=(
                    harness_gate.receipt if harness_gate is not None else None
                ),
            )

        await self._block_unreconciled_todos(events)
        await self._fire_agent_stop(
            status="max_turns",
            response="已达到最大轮次限制，任务未完成。",
            reason="max_turns",
            streaming=True,
            events=events,
        )
        return AgentResult(
            status="max_turns",
            response="已达到最大轮次限制，任务未完成。",
            usage=self._usage,
        )

    async def execute_tool(
        self,
        tool_call: ToolCall,
        *,
        on_event: LegacyEventCallback | None = None,
        agent_name: str | None = None,
        _events: EngineEventPublisher | None = None,
    ) -> ToolResult:
        """Execute one tool call through the authoritative runtime pipeline."""
        return await self._execute_tool(
            tool_call,
            on_event=on_event,
            agent_name=agent_name,
            events=_events,
        )

    async def _execute_tool(
        self,
        tc: ToolCall,
        on_event: LegacyEventCallback | None = None,
        agent_name: str | None = None,
        *,
        events: EngineEventPublisher | None = None,
    ) -> ToolResult:
        """执行单个工具调用（含权限检查）."""
        if events is not None and on_event is not None:
            raise TypeError("不能同时传入 events 和旧 on_event 回调")
        if events is None:
            events = self._coerce_engine_events(on_event)
        tool = self._tool_registry.get(tc.name)
        if tool is None:
            return ToolResult(
                call_id=tc.id,
                status="error",
                content=f"未知工具：{tc.name}",
            )

        try:
            args = tool.parse_arguments(tc.arguments)
        except ValueError as e:
            return ToolResult(call_id=tc.id, status="error", content=str(e))

        session_id = self._session.id if self._session else ""
        authorization_generation = self._session_authorization_generation
        transition_block = await self._block_transitioning_tool_call(
            tool_call=tc,
            session_id=session_id,
            events=events,
            agent_name=agent_name,
        )
        if transition_block is not None:
            return transition_block
        if (
            self._runtime_mode == AgentRuntimeMode.PLAN
            and not self._tool_allowed_in_plan_mode(tc.name, tool)
        ):
            reason = (
                "Plan 模式只允许只读工具。按 Shift+Tab 可切换到 default 或 bypass 后重试。"
            )
            await self._emit_permission_bubble(
                events,
                agent_name=agent_name,
                tool_name=tc.name,
                status="blocked_by_plan_mode",
                reason=reason,
                requires_confirmation=False,
                session_id=session_id,
            )
            return ToolResult(
                call_id=tc.id,
                status="error",
                content=f"权限拒绝：{reason}",
            )

        before_ctx = await self._fire_hook(HookContext(
            point=HookPoint.TOOL_PERMISSION_CHECK,
            data={
                "phase": "before",
                "tool_name": tc.name,
                "arguments": args,
                "agent_name": agent_name or "",
                "permission_bubble": True,
            },
            agent_name=agent_name,
            session_id=session_id,
        ), events)
        if before_ctx.should_abort:
            reason = before_ctx.data.get("abort_reason", "hook policy")
            await self._emit_permission_bubble(
                events,
                agent_name=agent_name,
                tool_name=tc.name,
                status="blocked_by_hook",
                reason=str(reason),
                requires_confirmation=False,
                session_id=session_id,
            )
            return ToolResult(
                call_id=tc.id,
                status="error",
                content=f"权限被 Hook 拒绝：{reason}",
            )

        transition_block = await self._block_transitioning_tool_call(
            tool_call=tc,
            session_id=session_id,
            events=events,
            agent_name=agent_name,
        )
        if transition_block is not None:
            return transition_block
        decision = self._permission_port.check(tc.name, args, tool=tool)
        after_ctx = await self._fire_hook(HookContext(
            point=HookPoint.TOOL_PERMISSION_CHECK,
            data={
                "phase": "after",
                "tool_name": tc.name,
                "arguments": args,
                "allowed": decision.allowed,
                "requires_confirmation": decision.requires_confirmation,
                "reason": decision.reason,
                "agent_name": agent_name or "",
                "permission_bubble": True,
            },
            agent_name=agent_name,
            session_id=session_id,
        ), events)
        if after_ctx.should_abort:
            reason = after_ctx.data.get("abort_reason", "hook policy")
            await self._emit_permission_bubble(
                events,
                agent_name=agent_name,
                tool_name=tc.name,
                status="blocked_by_hook",
                reason=str(reason),
                requires_confirmation=decision.requires_confirmation,
                session_id=session_id,
            )
            return ToolResult(
                call_id=tc.id,
                status="error",
                content=f"权限被 Hook 拒绝：{reason}",
            )
        transition_block = await self._block_transitioning_tool_call(
            tool_call=tc,
            session_id=session_id,
            events=events,
            agent_name=agent_name,
        )
        if transition_block is not None:
            return transition_block
        if decision.outcome is PermissionOutcome.BLOCK:
            logger.warning("Tool %s blocked: %s", tc.name, decision.reason)
            await self._emit_permission_bubble(
                events,
                agent_name=agent_name,
                tool_name=tc.name,
                status="blocked",
                reason=decision.reason,
                requires_confirmation=decision.requires_confirmation,
                session_id=session_id,
            )
            return ToolResult(
                call_id=tc.id,
                status="error",
                content=f"权限拒绝：{decision.reason}",
            )
        session_grant_applies = (
            decision.outcome is PermissionOutcome.CONFIRM
            and decision.allow_session_grant
            and bool(session_id)
            and self._is_tool_call_session_active(
                session_id,
                authorization_generation,
            )
            and self._permission_grant_store.allows(
                session_id,
                decision.tool_family,
            )
        )
        if decision.outcome is PermissionOutcome.CONFIRM and not session_grant_applies:
            logger.info("Tool %s requires confirmation", tc.name)
            confirmation = await self._confirm_tool_execution(
                events,
                agent_name=agent_name,
                tool_call=tc,
                arguments=args,
                decision=decision,
                session_id=session_id,
                authorization_generation=authorization_generation,
            )
            if confirmation != "allow_once":
                if confirmation == "unavailable":
                    content = (
                        "权限拒绝：该工具需要用户确认，但当前界面未注册确认入口。"
                        "请使用支持确认的 CLI/TUI 后重试。"
                    )
                elif confirmation == "error":
                    content = "权限拒绝：权限确认流程异常，已停止执行该工具。"
                elif confirmation == "grant_rejected_high_risk":
                    content = "权限拒绝：当前高风险操作不支持本会话授权。"
                elif confirmation == "grant_rejected_no_session":
                    content = "权限拒绝：当前工具调用没有活动会话，无法创建本会话授权。"
                elif confirmation == "grant_rejected_session_changed":
                    content = "权限拒绝：请求确认期间会话已切换，无法创建本会话授权。"
                elif confirmation == "grant_rejected":
                    content = "权限拒绝：当前工具不支持本会话授权。"
                else:
                    content = "权限拒绝：用户已拒绝执行该工具。"
                return ToolResult(call_id=tc.id, status="error", content=content)
            if not self._is_tool_call_session_active(
                session_id,
                authorization_generation,
            ):
                await self._emit_permission_bubble(
                    events,
                    agent_name=agent_name,
                    tool_name=tc.name,
                    call_id=tc.id,
                    status="stale_confirmation_rejected",
                    reason="请求确认期间会话已切换，已停止执行该工具。",
                    risk_level=getattr(
                        decision.risk_level,
                        "value",
                        str(decision.risk_level),
                    ),
                    requires_confirmation=True,
                    session_id=session_id,
                )
                return ToolResult(
                    call_id=tc.id,
                    status="error",
                    content="权限拒绝：请求确认期间会话已切换，已停止执行该工具。",
                )

        transition_block = await self._block_transitioning_tool_call(
            tool_call=tc,
            session_id=session_id,
            events=events,
            agent_name=agent_name,
        )
        if transition_block is not None:
            return transition_block
        if not self._is_tool_call_session_active(
            session_id,
            authorization_generation,
        ):
            await self._emit_permission_bubble(
                events,
                agent_name=agent_name,
                tool_name=tc.name,
                call_id=tc.id,
                status="stale_confirmation_rejected",
                reason="工具执行前会话已切换，已停止执行该工具。",
                risk_level=getattr(
                    decision.risk_level,
                    "value",
                    str(decision.risk_level),
                ),
                requires_confirmation=decision.requires_confirmation,
                session_id=session_id,
            )
            return ToolResult(
                call_id=tc.id,
                status="error",
                content="权限拒绝：工具执行前会话已切换，已停止执行该工具。",
            )

        try:
            outcome = await self._tool_execution_port.invoke(
                tool,
                args,
                event_callback=(
                    events.legacy_callback() if events is not None else None
                ),
            )
            output = outcome.content
            duration = outcome.duration_ms

            logger.info("Tool %s executed in %dms", tc.name, duration)
            if not tool.metadata.read_only:
                if (
                    self._active_harness_run is not None
                    and tc.name != "harness_run_check"
                ):
                    self._active_harness_run.mutating_tool_used = True
                try:
                    await self.harness_service.invalidate_knowledge_cache()
                    if events is not None:
                        await events.publish(
                            RuntimeEventType.HARNESS_KNOWLEDGE_INVALIDATED,
                            {
                                "source": tc.name,
                                "reason": "mutating_tool_succeeded",
                            },
                        )
                except Exception as exc:
                    logger.warning(
                        "Harness knowledge invalidation failed after %s: %s",
                        tc.name,
                        exc,
                    )
            await self._emit_task_snapshot(events, source=tc.name)
            return ToolResult(
                call_id=tc.id,
                status="success",
                content=output,
                duration_ms=duration,
            )
        except Exception as e:
            logger.warning("Tool %s failed: %s", tc.name, e)
            return ToolResult(
                call_id=tc.id,
                status="error",
                content=f"Tool error: {type(e).__name__}: {e}",
            )

    async def _confirm_tool_execution(
        self,
        events: EngineEventPublisher | None,
        *,
        agent_name: str | None,
        tool_call: ToolCall,
        arguments: dict[str, Any],
        decision: Any,
        session_id: str,
        authorization_generation: int,
    ) -> str:
        """Ask the UI to confirm a tool execution that policy marked as sensitive."""
        reason = decision.reason or "该工具需要用户确认。"
        risk_level = getattr(decision.risk_level, "value", str(decision.risk_level))
        await self._emit_permission_bubble(
            events,
            agent_name=agent_name,
            tool_name=tool_call.name,
            call_id=tool_call.id,
            status="needs_confirmation",
            reason=reason,
            risk_level=risk_level,
            requires_confirmation=True,
            session_id=session_id,
        )
        if self._permission_confirmer is None:
            return "unavailable"

        allow_session_grant = decision.allow_session_grant and bool(session_id)
        choices = ["allow_once", "deny"]
        if allow_session_grant:
            choices.append("grant_session")
        payload = {
            "agent_name": agent_name or "main",
            "tool_name": tool_call.name,
            "call_id": tool_call.id,
            "arguments": arguments,
            "reason": reason,
            "risk_level": risk_level,
            "requires_confirmation": True,
            "permission_mode": self._permission_port.mode.value,
            "session_id": session_id,
            "tool_family": decision.tool_family,
            "choices": choices,
            "scope": "session" if allow_session_grant else "call",
            "expires_at": None,
            "requires_double_confirm": decision.requires_double_confirm,
        }
        try:
            raw_choice = await self._permission_confirmer(payload)
        except Exception as e:
            logger.warning("Permission confirmation callback failed: %s", e)
            await self._emit_permission_bubble(
                events,
                agent_name=agent_name,
                tool_name=tool_call.name,
                call_id=tool_call.id,
                status="confirmation_error",
                reason=str(e),
                risk_level=risk_level,
                requires_confirmation=True,
                session_id=session_id,
            )
            return "error"

        choice = self._normalize_permission_confirmation(raw_choice)
        if choice == "bypass":
            self.set_runtime_mode(AgentRuntimeMode.BYPASS)
            await self._emit_permission_bubble(
                events,
                agent_name=agent_name,
                tool_name=tool_call.name,
                call_id=tool_call.id,
                status="bypass_enabled",
                reason="用户已切换到 bypass，全权限模式已放行当前及后续工具。",
                risk_level=risk_level,
                requires_confirmation=False,
                session_id=session_id,
            )
            return "allow_once"
        if choice == "grant_session":
            if not decision.allow_session_grant:
                await self._emit_permission_bubble(
                    events,
                    agent_name=agent_name,
                    tool_name=tool_call.name,
                    call_id=tool_call.id,
                    status="grant_rejected",
                    reason="当前操作不支持本会话授权。",
                    risk_level=risk_level,
                    requires_confirmation=True,
                    session_id=session_id,
                )
                return "grant_rejected"
            if not session_id.strip():
                await self._emit_permission_bubble(
                    events,
                    agent_name=agent_name,
                    tool_name=tool_call.name,
                    call_id=tool_call.id,
                    status="grant_rejected",
                    reason="当前工具调用没有活动会话，无法创建本会话授权。",
                    risk_level=risk_level,
                    requires_confirmation=True,
                    session_id=session_id,
                )
                return "grant_rejected_no_session"
            if not self._is_tool_call_session_active(
                session_id,
                authorization_generation,
            ):
                await self._emit_permission_bubble(
                    events,
                    agent_name=agent_name,
                    tool_name=tool_call.name,
                    call_id=tool_call.id,
                    status="grant_rejected",
                    reason="请求确认期间会话已切换，无法创建本会话授权。",
                    risk_level=risk_level,
                    requires_confirmation=True,
                    session_id=session_id,
                )
                return "grant_rejected_session_changed"
            try:
                self._permission_grant_store.create(
                    session_id,
                    decision.tool_family,
                    tool_call.id,
                )
            except ValueError:
                await self._emit_permission_bubble(
                    events,
                    agent_name=agent_name,
                    tool_name=tool_call.name,
                    call_id=tool_call.id,
                    status="grant_rejected",
                    reason="当前工具调用没有活动会话，无法创建本会话授权。",
                    risk_level=risk_level,
                    requires_confirmation=True,
                    session_id=session_id,
                )
                return "grant_rejected_no_session"
            await self._emit_permission_bubble(
                events,
                agent_name=agent_name,
                tool_name=tool_call.name,
                call_id=tool_call.id,
                status="session_granted",
                reason=f"用户已允许本会话使用工具族 `{decision.tool_family}`。",
                risk_level=risk_level,
                requires_confirmation=False,
                session_id=session_id,
            )
            return "allow_once"
        if choice == "allow_once":
            await self._emit_permission_bubble(
                events,
                agent_name=agent_name,
                tool_name=tool_call.name,
                call_id=tool_call.id,
                status="confirmed",
                reason="用户已允许本次工具执行。",
                risk_level=risk_level,
                requires_confirmation=False,
                session_id=session_id,
            )
            return "allow_once"

        await self._emit_permission_bubble(
            events,
            agent_name=agent_name,
            tool_name=tool_call.name,
            call_id=tool_call.id,
            status="denied",
            reason="用户拒绝执行该工具。",
            risk_level=risk_level,
            requires_confirmation=True,
            session_id=session_id,
        )
        return "deny"

    @staticmethod
    def _normalize_permission_confirmation(choice: str | bool) -> str:
        if choice is True:
            return "allow_once"
        if choice is False:
            return "deny"
        normalized = str(choice or "").strip().lower()
        if normalized in {"allow", "allowed", "yes", "y", "allow_once"}:
            return "allow_once"
        if normalized in {"bypass", "b"}:
            return "bypass"
        if normalized == "grant_session":
            return "grant_session"
        return "deny"

    def _tool_allowed_in_plan_mode(self, tool_name: str, tool: Any) -> bool:
        """Return whether a tool is safe for read-only planning mode."""
        metadata = getattr(tool, "metadata", None)
        if getattr(metadata, "destructive", False):
            return False
        if getattr(metadata, "read_only", False):
            return True
        if tool_name in _PLAN_MODE_READ_ONLY_TOOLS:
            return True
        if tool_name.startswith("browser_daemon_"):
            readonly_suffixes = (
                "health",
                "status",
                "list",
                "get",
                "read",
                "observe",
                "screenshot",
            )
            return tool_name.removeprefix("browser_daemon_").startswith(readonly_suffixes)
        return False

    async def _emit_permission_bubble(
        self,
        events: EngineEventPublisher | None,
        *,
        agent_name: str | None,
        tool_name: str,
        call_id: str = "",
        status: str,
        reason: str,
        risk_level: str = "",
        requires_confirmation: bool,
        session_id: str | None = None,
    ) -> None:
        """Emit permission decisions visible to parent or top-level UI."""
        payload = {
            "agent_name": agent_name or "main",
            "tool_name": tool_name,
            "call_id": call_id,
            "status": status,
            "reason": reason,
            "risk_level": risk_level,
            "requires_confirmation": requires_confirmation,
            "session_id": (
                self._session.id if session_id is None and self._session else session_id or ""
            ),
            "timestamp": time.time(),
        }
        self._append_permission_bubble(payload)
        await self._observe_harness_tool_event("permission_bubble", payload)
        if events is not None:
            await events.publish(RuntimeEventType.PERMISSION_BUBBLE, payload)

    def _append_permission_bubble(self, payload: dict[str, Any]) -> None:
        """Append one permission audit record while retaining the latest 100."""
        self._permission_bubble_history.append(payload)
        if len(self._permission_bubble_history) > 100:
            self._permission_bubble_history = self._permission_bubble_history[-100:]

    def get_recent_permission_bubbles(self, limit: int = 8) -> list[dict[str, Any]]:
        """Return recent subagent permission decisions that bubbled to parent."""
        safe_limit = max(1, min(limit, 50))
        return list(self._permission_bubble_history[-safe_limit:])

    def _parse_tool_call(self, raw: dict[str, Any]) -> ToolCall | None:
        """从 LLM 响应中提取 ToolCall."""
        try:
            func = raw.get("function", {})
            return ToolCall(
                id=raw.get("id", ""),
                name=func.get("name", ""),
                arguments=func.get("arguments", "{}"),
            )
        except Exception:
            logger.warning("Failed to parse tool call: %s", raw)
            return None

    @staticmethod
    def _extract_tool_call_id(raw: dict[str, Any]) -> str:
        """Best-effort extraction of tool_call id for protocol-complete error replies."""
        try:
            return str(raw.get("id") or "")
        except Exception:
            return ""

    def _accumulate_usage(self, usage: TokenUsage) -> None:
        """累加 token 用量."""
        self._usage.total_input_tokens += usage.input_tokens
        self._usage.total_output_tokens += usage.output_tokens
        self._usage.total_cost_usd += usage.cost_usd
        self._usage.cache_tokens += usage.cache_tokens

    def _track_model_usage(self, usage: TokenUsage, model: str) -> None:
        """Record model usage in both user-facing stats and budget enforcement."""
        self._accumulate_usage(usage)
        self._budget_tracker.track(usage, model)

    def get_context_info(self) -> dict[str, Any]:
        """Return context window usage estimate."""
        model = self._model_port.resolve_model(ModelTier.CAPABLE)
        window = self._compute_context_budget(model)[0]
        sanitized, _ = self._compactor.sanitize_visual_payloads(self._messages)
        used = self._compactor._estimate_tokens(sanitized)
        return {
            "model": model,
            "window": window,
            "used": used,
            "percentage": min(100, round(used / window * 100, 1)) if window > 0 else 0,
        }

    def _compute_context_budget(self, model: str) -> tuple[int, int]:
        """计算可用于会话构建的上下文预算（保留输出缓冲区）。"""
        context_window = self._model_port.get_context_window(model)
        cumulative_limit = self._config.safety.max_input_tokens
        hard_cap = (
            context_window
            if cumulative_limit is None
            else min(context_window, cumulative_limit)
        )
        if hard_cap <= 0:
            return 0, 0

        output_cap = self._model_port.get_max_output(model)
        reserve = self._config.memory.compaction_reserved_tokens
        if reserve <= 0:
            reserve = _DEFAULT_COMPACTION_RESERVED_TOKENS

        if output_cap > 0:
            reserve = min(reserve, output_cap)

        budget = max(0, hard_cap - reserve)
        if budget <= 0:
            return hard_cap, 0
        return budget, reserve

    def get_budget_info(self) -> dict[str, Any]:
        """Return budget consumption info."""
        summary = self._budget_tracker.get_summary()
        budget = self._budget_tracker.budget
        cost_percentage = _budget_percentage(summary.total_cost_usd, budget.max_usd)
        input_percentage = _budget_percentage(
            summary.total_input_tokens,
            budget.max_input_tokens,
        )
        output_percentage = _budget_percentage(
            summary.total_output_tokens,
            budget.max_output_tokens,
        )
        active_percentages = [
            percentage
            for percentage in (cost_percentage, input_percentage, output_percentage)
            if percentage is not None
        ]
        return {
            "enabled": budget.enabled,
            "used_usd": summary.total_cost_usd,
            "max_usd": budget.max_usd,
            "remaining_usd": summary.remaining_usd,
            "cost_percentage": cost_percentage,
            "input_tokens": summary.total_input_tokens,
            "max_input_tokens": budget.max_input_tokens,
            "input_percentage": input_percentage,
            "output_tokens": summary.total_output_tokens,
            "max_output_tokens": budget.max_output_tokens,
            "output_percentage": output_percentage,
            "percentage": max(active_percentages) if active_percentages else None,
        }

    @staticmethod
    def _format_error(e: Exception) -> str:
        """将异常转为用户友好的错误信息."""
        error_type = type(e).__name__
        msg = str(e)
        if "AuthenticationError" in error_type or "api_key" in msg.lower():
            return (
                "API Key 未设置或无效。请通过环境变量设置:\n"
                "  export NAUMI_MODELS__API_KEY=your-key\n"
                "或重新运行首次引导保存到系统凭据库"
            )
        if "RateLimitError" in error_type:
            return "API 调用频率超限，请稍后重试。"
        return f"{error_type}: {msg}"


def _append_harness_context_sections(snapshot: str, sections: list[str]) -> str:
    """Append hook-provided sections before the closing harness marker."""
    if not sections:
        return snapshot
    extra = "\n\n".join(sections)
    closing = f"\n{HARNESS_CONTEXT_MARKER}"
    if snapshot.endswith(closing):
        return snapshot[: -len(closing)] + f"\n\n{extra}{closing}"
    return f"{snapshot}\n\n{extra}"


def _is_prompt_too_long_error(error: Exception) -> bool:
    """Detect provider errors that mean the input context is too large."""
    text = f"{type(error).__name__}: {error}".lower()
    markers = (
        "prompt_too_long",
        "context_length",
        "context length",
        "maximum context",
        "max context",
        "input too long",
        "too many tokens",
        "token limit",
        "request too large",
        "413",
    )
    return any(marker in text for marker in markers)


def _is_output_truncated(finish_reason: str | None) -> bool:
    """Detect model finish reasons that indicate an incomplete final answer."""
    reason = (finish_reason or "").lower().strip()
    return reason in _OUTPUT_TRUNCATED_FINISH_REASONS


def _join_continued_output(base: str, addition: str) -> str:
    """Append continuation text while removing simple boundary overlap."""
    if not addition:
        return base
    if addition.startswith(base):
        return addition

    max_overlap = min(len(base), len(addition), 240)
    for size in range(max_overlap, 0, -1):
        if base[-size:] == addition[:size]:
            return base + addition[size:]
    return base + addition


def _fallback_reactive_compact_messages(
    messages: list[dict[str, Any]],
    *,
    runtime_snapshot: str,
) -> list[dict[str, Any]]:
    """Deterministic fallback when LLM compaction cannot reduce the prompt."""
    system_messages = [
        message for message in messages
        if message.get("role") == "system"
        and not is_harness_context_message(message)
    ]
    base_system = system_messages[:1]
    non_system = [message for message in messages if message.get("role") != "system"]
    recent = non_system[-5:]
    summary = {
        "role": "system",
        "content": (
            "## Reactive compact fallback\n\n"
            "模型报告上下文超限。旧对话已被确定性裁剪，只保留最近消息和运行时状态。"
            "\n\n## 压缩时保留的运行时状态\n\n"
            f"{runtime_snapshot.strip() or '无额外运行时状态。'}"
        ),
    }
    return [*base_system, summary, *recent]
