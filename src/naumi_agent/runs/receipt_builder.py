"""Deterministic aggregation of runtime evidence into completion receipts."""

from __future__ import annotations

import json
import re
import shlex
import time
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from naumi_agent.runs.git_probe import GitWorkspaceProbe, GitWorkspaceSnapshot, diff_run_changes
from naumi_agent.runs.models import (
    CompletionReceipt,
    ReceiptAction,
    ReceiptApproval,
    ReceiptChange,
    ReceiptRisk,
    ReceiptValidation,
)
from naumi_agent.safety.guardrails import OutputGuardrail

_FILE_MUTATION_TOOLS = frozenset(
    {
        "edit",
        "file_edit",
        "file_write",
        "write",
        "file_delete",
        "delete",
    }
)
_TERMINAL_APPROVALS = {
    "confirmed": "allowed_once",
    "session_granted": "allowed_session",
    "bypass_enabled": "bypass",
    "denied": "denied",
    "confirmation_error": "error",
}
_SECRET_NAME = re.compile(
    r"(?:token|secret|password|passwd|api[_-]?key|authorization|cookie)",
    re.IGNORECASE,
)
_EXIT_CODE = re.compile(r"\[exit code:\s*(-?\d+)\]", re.IGNORECASE)
_PYTEST_COUNT = {
    "passed": re.compile(r"\b(\d+)\s+passed\b", re.IGNORECASE),
    "failed": re.compile(r"\b(\d+)\s+failed\b", re.IGNORECASE),
    "skipped": re.compile(r"\b(\d+)\s+skipped\b", re.IGNORECASE),
}
_TAP_COUNT = {
    "passed": re.compile(r"^#\s*pass\s+(\d+)\s*$", re.IGNORECASE | re.MULTILINE),
    "failed": re.compile(r"^#\s*fail\s+(\d+)\s*$", re.IGNORECASE | re.MULTILINE),
    "skipped": re.compile(r"^#\s*skipped\s+(\d+)\s*$", re.IGNORECASE | re.MULTILINE),
}
_SWIFT_COUNT = re.compile(
    r"Executed\s+(\d+)\s+tests?,\s+with\s+(\d+)\s+failures?",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class _ToolObservation:
    name: str
    arguments: dict[str, Any]
    command: str = ""


class RunReceiptBuilder:
    """Collect real events and workspace facts for a single streamed run."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        run_id: str,
        started_at: str,
        started_monotonic: float,
        before_git: GitWorkspaceSnapshot,
    ) -> None:
        self.workspace_root = workspace_root
        self.run_id = run_id
        self.started_at = started_at
        self._started_monotonic = started_monotonic
        self._before_git = before_git
        self._probe = GitWorkspaceProbe(workspace_root)
        self._tools: dict[str, _ToolObservation] = {}
        self._path_tools: dict[str, str] = {}
        self._validations: list[ReceiptValidation] = []
        self._approvals: dict[str, ReceiptApproval] = {}
        self._failed_tools: list[tuple[str, str]] = []
        self._evidence_refs: list[str] = []

    @classmethod
    async def start(
        cls,
        *,
        workspace_root: str | Path,
        run_id: str,
        started_at: str = "",
    ) -> RunReceiptBuilder:
        root = Path(workspace_root).expanduser().resolve()
        return cls(
            workspace_root=root,
            run_id=run_id,
            started_at=started_at or _now_iso(),
            started_monotonic=time.perf_counter(),
            before_git=await GitWorkspaceProbe(root).capture(),
        )

    def observe(self, event: str, data: dict[str, Any]) -> None:
        """Observe one raw engine event without trusting model prose."""
        if event == "tool_start":
            self._observe_tool_start(data)
        elif event == "tool_end":
            self._observe_tool_end(data)
        elif event == "permission_bubble":
            self._observe_permission(data)

    async def finish(self, requested_status: str, summary: str) -> CompletionReceipt:
        after_git = await self._probe.capture()
        delta = diff_run_changes(self._before_git, after_git)
        changes = tuple(self._attribute_change(change) for change in delta.changes)
        validations = tuple(self._validations)
        approvals = tuple(self._approvals.values())
        unverified = list(delta.warnings)
        risks: list[ReceiptRisk] = []

        failed_validations = [item for item in validations if item.status == "failed"]
        denied_approvals = [item for item in approvals if item.decision in {"denied", "error"}]
        if changes and not validations:
            unverified.append("检测到文件改动，但本轮未运行验证命令。")
            risks.append(
                ReceiptRisk(
                    code="changes_unverified",
                    level="medium",
                    message="文件改动尚未经过真实验证。",
                )
            )
        if failed_validations:
            risks.append(
                ReceiptRisk(
                    code="validation_failed",
                    level="high",
                    message=f"{len(failed_validations)} 项验证失败。",
                )
            )
        if denied_approvals:
            risks.append(
                ReceiptRisk(
                    code="approval_not_granted",
                    level="high",
                    message=f"{len(denied_approvals)} 项审批未获允许。",
                )
            )
        if self._failed_tools:
            risks.append(
                ReceiptRisk(
                    code="tool_failed",
                    level="high",
                    message=f"{len(self._failed_tools)} 个工具执行失败。",
                )
            )
        if not after_git.available:
            risks.append(
                ReceiptRisk(
                    code="git_unavailable",
                    level="medium",
                    message="Git 状态不可用，无法核查提交和工作区事实。",
                )
            )

        unverified_tuple = _unique(unverified)
        outcome = _derive_outcome(
            requested_status,
            changes=changes,
            validations=validations,
            unverified=unverified_tuple,
            risks=tuple(risks),
        )
        actions = _next_actions(
            outcome=outcome,
            changes=changes,
            validations=validations,
            approvals=approvals,
            git_dirty=delta.git_state.dirty,
        )
        completed_at = _now_iso()
        return CompletionReceipt.from_dict(
            {
                "schema_version": 1,
                "receipt_id": f"receipt-{uuid.uuid4().hex[:16]}",
                "run_id": self.run_id,
                "outcome": outcome,
                "summary": _public_summary(summary, outcome),
                "changes": [asdict(change) for change in changes],
                "validations": [asdict(validation) for validation in validations],
                "unverified": list(unverified_tuple),
                "approvals": [asdict(approval) for approval in approvals],
                "risks": [asdict(risk) for risk in risks],
                "git_state": asdict(delta.git_state),
                "next_actions": [asdict(action) for action in actions],
                "evidence_refs": list(_unique(self._evidence_refs)),
                "started_at": self.started_at,
                "completed_at": completed_at,
                "duration_ms": max(
                    int((time.perf_counter() - self._started_monotonic) * 1000),
                    0,
                ),
            }
        )

    def _observe_tool_start(self, data: dict[str, Any]) -> None:
        call_id = str(data.get("call_id") or data.get("tool_call_id") or "").strip()
        if not call_id:
            return
        name = str(data.get("name") or data.get("tool_name") or "tool").strip()
        arguments = _parse_arguments(data.get("args"))
        command = str(arguments.get("command") or "").strip()
        self._tools[call_id] = _ToolObservation(
            name=name,
            arguments=arguments,
            command=command,
        )
        if name in _FILE_MUTATION_TOOLS:
            raw_path = arguments.get("path") or arguments.get("file_path")
            relative_path = _relative_workspace_path(self.workspace_root, raw_path)
            if relative_path:
                self._path_tools[relative_path] = name

    def _observe_tool_end(self, data: dict[str, Any]) -> None:
        call_id = str(data.get("call_id") or data.get("tool_call_id") or "").strip()
        observed = self._tools.get(call_id)
        if observed is None:
            observed = _ToolObservation(
                name=str(data.get("name") or data.get("tool_name") or "tool"),
                arguments={},
            )
        status = str(data.get("status") or "unknown").strip().lower()
        content = str(data.get("content") or "")[:50_000]
        evidence_ref = f"run:{self.run_id}:tool:{call_id or observed.name}"
        if observed.command and _is_validation_command(observed.command):
            self._validations.append(
                _validation_from_tool(
                    command=observed.command,
                    tool_status=status,
                    content=content,
                    evidence_ref=evidence_ref,
                )
            )
            self._evidence_refs.append(evidence_ref)
        elif status not in {"success", "succeeded", "completed"}:
            self._failed_tools.append((call_id, observed.name))
            self._evidence_refs.append(evidence_ref)

    def _observe_permission(self, data: dict[str, Any]) -> None:
        status = str(data.get("status") or "").strip().lower()
        decision = _TERMINAL_APPROVALS.get(status)
        if decision is None:
            return
        call_id = str(data.get("call_id") or data.get("request_id") or "").strip()
        if not call_id:
            return
        approval = ReceiptApproval(
            call_id=call_id[:500],
            tool_name=str(data.get("tool_name") or "tool")[:500],
            decision=decision,
            scope=str(data.get("scope") or _approval_scope(decision))[:500],
        )
        self._approvals[call_id] = approval
        self._evidence_refs.append(f"run:{self.run_id}:approval:{call_id}")

    def _attribute_change(self, change: ReceiptChange) -> ReceiptChange:
        source_tool = self._path_tools.get(change.path, "")
        return replace(change, source_tool=source_tool)


def _derive_outcome(
    requested_status: str,
    *,
    changes: tuple[ReceiptChange, ...],
    validations: tuple[ReceiptValidation, ...],
    unverified: tuple[str, ...],
    risks: tuple[ReceiptRisk, ...],
) -> str:
    normalized = requested_status.strip().lower()
    if normalized == "cancelled":
        return "cancelled"
    if normalized in {"error", "failed", "max_turns", "budget_exceeded"}:
        return "failed"
    if any(item.status == "failed" for item in validations):
        return "partial"
    if changes and not validations:
        return "partial"
    if unverified or any(item.level in {"high", "critical"} for item in risks):
        return "partial"
    return "completed"


def _next_actions(
    *,
    outcome: str,
    changes: tuple[ReceiptChange, ...],
    validations: tuple[ReceiptValidation, ...],
    approvals: tuple[ReceiptApproval, ...],
    git_dirty: bool,
) -> tuple[ReceiptAction, ...]:
    actions: list[ReceiptAction] = []
    if any(item.status == "failed" for item in validations):
        actions.append(
            ReceiptAction(
                id="retry-validation",
                label="重试失败验证",
                kind="retry_validation",
            )
        )
    if any(item.decision in {"denied", "error"} for item in approvals):
        actions.append(
            ReceiptAction(
                id="request-approval",
                label="重新请求审批",
                kind="request_approval",
            )
        )
    if changes and not validations:
        actions.append(
            ReceiptAction(
                id="run-validation",
                label="运行相关验证",
                kind="run_validation",
            )
        )
    if changes:
        actions.append(
            ReceiptAction(
                id="review-changes",
                label="审查本轮改动",
                kind="review_changes",
            )
        )
    if outcome == "cancelled":
        actions.append(
            ReceiptAction(
                id="continue-run",
                label="继续未完成运行",
                kind="continue_run",
            )
        )
    if changes and git_dirty:
        actions.append(
            ReceiptAction(
                id="commit-changes",
                label="验证后提交改动",
                kind="commit_changes",
            )
        )
    return tuple(actions)


def _validation_from_tool(
    *,
    command: str,
    tool_status: str,
    content: str,
    evidence_ref: str,
) -> ReceiptValidation:
    exit_match = _EXIT_CODE.search(content)
    exit_code = int(exit_match.group(1)) if exit_match else (
        0 if tool_status in {"success", "succeeded", "completed"} else 1
    )
    counts = _validation_counts(content)
    return ReceiptValidation(
        command=_redact_command(command),
        scope=_validation_scope(command),
        status="passed" if exit_code == 0 else "failed",
        exit_code=exit_code,
        passed=counts[0],
        failed=counts[1],
        skipped=counts[2],
        log_ref=evidence_ref,
    )


def _validation_counts(content: str) -> tuple[int, int, int]:
    counts = {
        name: _first_count(pattern, content)
        for name, pattern in _PYTEST_COUNT.items()
    }
    if not any(counts.values()):
        counts = {
            name: _first_count(pattern, content)
            for name, pattern in _TAP_COUNT.items()
        }
    swift_match = _SWIFT_COUNT.search(content)
    if swift_match and not any(counts.values()):
        total = int(swift_match.group(1))
        failed = int(swift_match.group(2))
        counts["passed"] = max(total - failed, 0)
        counts["failed"] = failed
    return counts["passed"], counts["failed"], counts["skipped"]


def _first_count(pattern: re.Pattern[str], content: str) -> int:
    match = pattern.search(content)
    return int(match.group(1)) if match else 0


def _is_validation_command(command: str) -> bool:
    tokens = _command_tokens(command)
    lowered = [token.lower() for token in tokens]
    joined = " ".join(lowered)
    return any(
        marker in joined
        for marker in (
            " pytest",
            "pytest ",
            "ruff check",
            "node --test",
            "npm test",
            "npm run test",
            "pnpm test",
            "swift test",
            "swift build",
            "mypy ",
            "pyright ",
        )
    ) or joined in {"pytest", "ruff check", "npm test", "swift test", "swift build"}


def _validation_scope(command: str) -> str:
    tokens = _command_tokens(command)
    lowered = [token.lower() for token in tokens]
    anchors = ("pytest", "--test", "check", "test", "build", "mypy", "pyright")
    anchor_index = next(
        (index for index, token in enumerate(lowered) if token in anchors),
        -1,
    )
    if anchor_index < 0:
        return ""
    candidates = [
        token
        for token in tokens[anchor_index + 1 :]
        if token and not token.startswith("-") and "=" not in token
    ]
    return " ".join(candidates[:5])[:500]


def _redact_command(command: str) -> str:
    tokens = _command_tokens(command)
    redacted: list[str] = []
    redact_next = False
    for token in tokens:
        if redact_next:
            redacted.append("<redacted>")
            redact_next = False
            continue
        if token.startswith("-") and _SECRET_NAME.search(token):
            if "=" in token:
                name = token.split("=", 1)[0]
                redacted.append(f"{name}=<redacted>")
            else:
                redacted.append(token)
                redact_next = True
            continue
        if "=" in token:
            name, _value = token.split("=", 1)
            if _SECRET_NAME.search(name):
                redacted.append(f"{name}=<redacted>")
                continue
        redacted.append(token)
    return OutputGuardrail.redact(shlex.join(redacted))[:500]


def _command_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def _parse_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str) or not value:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return dict(decoded) if isinstance(decoded, dict) else {}


def _relative_workspace_path(workspace_root: Path, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    requested = Path(value).expanduser()
    candidate = requested if requested.is_absolute() else workspace_root / requested
    try:
        return candidate.resolve().relative_to(workspace_root).as_posix()
    except (OSError, ValueError):
        return ""


def _public_summary(summary: str, outcome: str) -> str:
    first_line = next((line.strip() for line in str(summary).splitlines() if line.strip()), "")
    if not first_line:
        first_line = {
            "completed": "运行已完成。",
            "partial": "运行部分完成，仍有待处理事项。",
            "failed": "运行失败，证据已保留。",
            "cancelled": "运行已取消，已完成证据仍被保留。",
        }[outcome]
    return OutputGuardrail.redact(first_line)[:500]


def _approval_scope(decision: str) -> str:
    return {
        "allowed_once": "本次调用",
        "allowed_session": "当前会话",
        "bypass": "bypass 模式",
        "denied": "本次调用",
        "error": "本次调用",
    }[decision]


def _unique(items: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for item in items if item))


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


__all__ = ["RunReceiptBuilder"]
