"""Typed, bounded value objects for the authoritative runtime Inspector."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Literal

from naumi_agent.runs.models import (
    ReceiptAction,
    ReceiptChange,
    ReceiptGitState,
    ReceiptValidation,
)

INSPECTOR_SCHEMA_VERSION = 1
INSPECTOR_TAB_NAMES = ("plan", "tools", "context", "changes", "tests")
InspectorState = Literal["ready", "empty", "loading", "stale", "error"]

_STATES = frozenset({"ready", "empty", "loading", "stale", "error"})
_MAX_TEXT = 500
_MAX_ITEMS = 50
_MAX_WARNINGS = 20


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _text(value: Any, name: str, *, required: bool = False) -> str:
    if value is None:
        result = ""
    elif isinstance(value, str):
        result = value.strip()
    else:
        raise ValueError(f"{name} must be a string")
    if required and not result:
        raise ValueError(f"{name} must not be blank")
    return result[:_MAX_TEXT]


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _number(value: Any, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{name} must be a non-negative number")
    return float(value)


def _optional_number(value: Any, name: str) -> float | None:
    if value is None:
        return None
    return _number(value, name)


def _optional_integer(value: Any, name: str) -> int | None:
    if value is None:
        return None
    return _integer(value, name)


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _state(value: Any, name: str) -> InspectorState:
    result = _text(value, name, required=True)
    if result not in _STATES:
        raise ValueError(f"invalid {name}: {result}")
    return result  # type: ignore[return-value]


def _sequence(value: Any, name: str, limit: int = _MAX_ITEMS) -> tuple[Any, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be an array")
    return tuple(value[:limit])


def _warnings(value: Any, name: str) -> tuple[str, ...]:
    return tuple(
        _text(item, f"{name} item", required=True) for item in _sequence(value, name, _MAX_WARNINGS)
    )


@dataclass(frozen=True, slots=True)
class InspectorTodo:
    id: str
    subject: str
    status: str
    active_form: str = ""
    owner: str = ""
    blocked_by: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: Any) -> InspectorTodo:
        data = _mapping(value, "plan.item")
        return cls(
            id=_text(data.get("id"), "plan.item.id", required=True),
            subject=_text(data.get("subject"), "plan.item.subject", required=True),
            status=_text(data.get("status"), "plan.item.status", required=True),
            active_form=_text(data.get("active_form"), "plan.item.active_form"),
            owner=_text(data.get("owner"), "plan.item.owner"),
            blocked_by=tuple(
                _text(item, "plan.item.blocked_by", required=True)
                for item in _sequence(data.get("blocked_by"), "plan.item.blocked_by")
            ),
        )


@dataclass(frozen=True, slots=True)
class InspectorTool:
    call_id: str
    name: str
    status: str
    summary: str = ""
    duration_ms: int = 0
    run_id: str = ""

    @classmethod
    def from_dict(cls, value: Any) -> InspectorTool:
        data = _mapping(value, "tools.item")
        return cls(
            call_id=_text(data.get("call_id"), "tools.item.call_id", required=True),
            name=_text(data.get("name"), "tools.item.name", required=True),
            status=_text(data.get("status"), "tools.item.status", required=True),
            summary=_text(data.get("summary"), "tools.item.summary"),
            duration_ms=_integer(data.get("duration_ms", 0), "tools.item.duration_ms"),
            run_id=_text(data.get("run_id"), "tools.item.run_id"),
        )


@dataclass(frozen=True, slots=True)
class InspectorApproval:
    request_id: str
    tool_name: str
    decision: str
    reason: str = ""
    run_id: str = ""

    @classmethod
    def from_dict(cls, value: Any) -> InspectorApproval:
        data = _mapping(value, "tools.approval")
        return cls(
            request_id=_text(data.get("request_id"), "tools.approval.request_id", required=True),
            tool_name=_text(data.get("tool_name"), "tools.approval.tool_name", required=True),
            decision=_text(data.get("decision"), "tools.approval.decision", required=True),
            reason=_text(data.get("reason"), "tools.approval.reason"),
            run_id=_text(data.get("run_id"), "tools.approval.run_id"),
        )


@dataclass(frozen=True, slots=True)
class InspectorPlan:
    state: InspectorState = "empty"
    items: tuple[InspectorTodo, ...] = ()
    next_actions: tuple[ReceiptAction, ...] = ()
    warnings: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: Any) -> InspectorPlan:
        data = _mapping(value, "plan")
        return cls(
            state=_state(data.get("state"), "plan.state"),
            items=tuple(
                InspectorTodo.from_dict(item) for item in _sequence(data.get("items"), "plan.items")
            ),
            next_actions=tuple(
                ReceiptAction.from_dict(item)
                for item in _sequence(data.get("next_actions"), "plan.next_actions")
            ),
            warnings=_warnings(data.get("warnings"), "plan.warnings"),
        )


@dataclass(frozen=True, slots=True)
class InspectorTools:
    state: InspectorState = "empty"
    items: tuple[InspectorTool, ...] = ()
    approvals: tuple[InspectorApproval, ...] = ()
    warnings: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: Any) -> InspectorTools:
        data = _mapping(value, "tools")
        return cls(
            state=_state(data.get("state"), "tools.state"),
            items=tuple(
                InspectorTool.from_dict(item)
                for item in _sequence(data.get("items"), "tools.items")
            ),
            approvals=tuple(
                InspectorApproval.from_dict(item)
                for item in _sequence(data.get("approvals"), "tools.approvals")
            ),
            warnings=_warnings(data.get("warnings"), "tools.warnings"),
        )


@dataclass(frozen=True, slots=True)
class InspectorContext:
    state: InspectorState = "empty"
    workspace_root: str = ""
    branch: str = ""
    commit: str = ""
    git_available: bool = False
    git_dirty: bool = False
    model: str = ""
    runtime_mode: str = ""
    permission_mode: str = ""
    context_used: int = 0
    context_window: int = 0
    context_percentage: float = 0.0
    budget_enabled: bool = False
    budget_used_usd: float = 0.0
    budget_max_usd: float | None = None
    budget_percentage: float | None = None
    budget_max_input_tokens: int | None = None
    budget_max_output_tokens: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    turns: int = 0
    warnings: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: Any) -> InspectorContext:
        data = _mapping(value, "context")
        return cls(
            state=_state(data.get("state"), "context.state"),
            workspace_root=_text(data.get("workspace_root"), "context.workspace_root"),
            branch=_text(data.get("branch"), "context.branch"),
            commit=_text(data.get("commit"), "context.commit"),
            git_available=_boolean(data.get("git_available", False), "context.git_available"),
            git_dirty=_boolean(data.get("git_dirty", False), "context.git_dirty"),
            model=_text(data.get("model"), "context.model"),
            runtime_mode=_text(data.get("runtime_mode"), "context.runtime_mode"),
            permission_mode=_text(data.get("permission_mode"), "context.permission_mode"),
            context_used=_integer(data.get("context_used", 0), "context.context_used"),
            context_window=_integer(data.get("context_window", 0), "context.context_window"),
            context_percentage=_number(
                data.get("context_percentage", 0), "context.context_percentage"
            ),
            budget_enabled=_boolean(
                data.get("budget_enabled", False), "context.budget_enabled"
            ),
            budget_used_usd=_number(data.get("budget_used_usd", 0), "context.budget_used_usd"),
            budget_max_usd=_optional_number(
                data.get("budget_max_usd"), "context.budget_max_usd"
            ),
            budget_percentage=_optional_number(
                data.get("budget_percentage"), "context.budget_percentage"
            ),
            budget_max_input_tokens=_optional_integer(
                data.get("budget_max_input_tokens"),
                "context.budget_max_input_tokens",
            ),
            budget_max_output_tokens=_optional_integer(
                data.get("budget_max_output_tokens"),
                "context.budget_max_output_tokens",
            ),
            input_tokens=_integer(data.get("input_tokens", 0), "context.input_tokens"),
            output_tokens=_integer(data.get("output_tokens", 0), "context.output_tokens"),
            turns=_integer(data.get("turns", 0), "context.turns"),
            warnings=_warnings(data.get("warnings"), "context.warnings"),
        )


@dataclass(frozen=True, slots=True)
class InspectorChanges:
    state: InspectorState = "empty"
    source_run_id: str = ""
    receipt_id: str = ""
    summary: str = ""
    items: tuple[ReceiptChange, ...] = ()
    git_state: ReceiptGitState = field(default_factory=ReceiptGitState)
    warnings: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: Any) -> InspectorChanges:
        data = _mapping(value, "changes")
        return cls(
            state=_state(data.get("state"), "changes.state"),
            source_run_id=_text(data.get("source_run_id"), "changes.source_run_id"),
            receipt_id=_text(data.get("receipt_id"), "changes.receipt_id"),
            summary=_text(data.get("summary"), "changes.summary"),
            items=tuple(
                ReceiptChange.from_dict(item)
                for item in _sequence(data.get("items"), "changes.items", 100)
            ),
            git_state=ReceiptGitState.from_dict(data.get("git_state", {})),
            warnings=_warnings(data.get("warnings"), "changes.warnings"),
        )


@dataclass(frozen=True, slots=True)
class InspectorTests:
    state: InspectorState = "empty"
    source_run_id: str = ""
    receipt_id: str = ""
    validations: tuple[ReceiptValidation, ...] = ()
    unverified: tuple[str, ...] = ()
    next_actions: tuple[ReceiptAction, ...] = ()
    warnings: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: Any) -> InspectorTests:
        data = _mapping(value, "tests")
        return cls(
            state=_state(data.get("state"), "tests.state"),
            source_run_id=_text(data.get("source_run_id"), "tests.source_run_id"),
            receipt_id=_text(data.get("receipt_id"), "tests.receipt_id"),
            validations=tuple(
                ReceiptValidation.from_dict(item)
                for item in _sequence(data.get("validations"), "tests.validations")
            ),
            unverified=tuple(
                _text(item, "tests.unverified item", required=True)
                for item in _sequence(data.get("unverified"), "tests.unverified")
            ),
            next_actions=tuple(
                ReceiptAction.from_dict(item)
                for item in _sequence(data.get("next_actions"), "tests.next_actions")
            ),
            warnings=_warnings(data.get("warnings"), "tests.warnings"),
        )


@dataclass(frozen=True, slots=True)
class RuntimeInspectorSnapshot:
    schema_version: int
    session_id: str
    revision: int
    generated_at: str
    active_run_id: str = ""
    plan: InspectorPlan = field(default_factory=InspectorPlan)
    tools: InspectorTools = field(default_factory=InspectorTools)
    context: InspectorContext = field(default_factory=InspectorContext)
    changes: InspectorChanges = field(default_factory=InspectorChanges)
    tests: InspectorTests = field(default_factory=InspectorTests)

    @classmethod
    def empty(cls, *, session_id: str = "") -> RuntimeInspectorSnapshot:
        return cls(INSPECTOR_SCHEMA_VERSION, session_id[:_MAX_TEXT], 0, "")

    def with_revision(self, revision: int, generated_at: str) -> RuntimeInspectorSnapshot:
        return replace(self, revision=revision, generated_at=generated_at[:_MAX_TEXT])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Any) -> RuntimeInspectorSnapshot:
        data = _mapping(value, "runtime_inspector")
        if data.get("schema_version") != INSPECTOR_SCHEMA_VERSION:
            raise ValueError(
                "unsupported schema_version: "
                f"{data.get('schema_version')!r}; "
                f"expected {INSPECTOR_SCHEMA_VERSION}"
            )
        return cls(
            schema_version=INSPECTOR_SCHEMA_VERSION,
            session_id=_text(data.get("session_id"), "session_id"),
            revision=_integer(data.get("revision", 0), "revision"),
            generated_at=_text(data.get("generated_at"), "generated_at"),
            active_run_id=_text(data.get("active_run_id"), "active_run_id"),
            plan=InspectorPlan.from_dict(data.get("plan", {})),
            tools=InspectorTools.from_dict(data.get("tools", {})),
            context=InspectorContext.from_dict(data.get("context", {})),
            changes=InspectorChanges.from_dict(data.get("changes", {})),
            tests=InspectorTests.from_dict(data.get("tests", {})),
        )


__all__ = [
    "INSPECTOR_SCHEMA_VERSION",
    "INSPECTOR_TAB_NAMES",
    "InspectorApproval",
    "InspectorChanges",
    "InspectorContext",
    "InspectorPlan",
    "InspectorState",
    "InspectorTests",
    "InspectorTodo",
    "InspectorTool",
    "InspectorTools",
    "RuntimeInspectorSnapshot",
]
