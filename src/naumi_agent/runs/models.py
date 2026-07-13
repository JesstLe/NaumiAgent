"""Typed, bounded completion-receipt value objects."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

ReceiptOutcome = Literal["completed", "partial", "failed", "cancelled"]

_SCHEMA_VERSION = 1
_OUTCOMES = frozenset({"completed", "partial", "failed", "cancelled"})
_MAX_PUBLIC_TEXT = 500
_MAX_SUMMARY_TEXT = 2_000
_MAX_CHANGES = 100
_MAX_VALIDATIONS = 50
_MAX_UNVERIFIED = 50
_MAX_APPROVALS = 50
_MAX_RISKS = 50
_MAX_ACTIONS = 50
_MAX_EVIDENCE_REFS = 100


def _mapping(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object")
    return value


def _text(
    value: Any,
    field_name: str,
    *,
    required: bool = False,
    max_chars: int = _MAX_PUBLIC_TEXT,
) -> str:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise ValueError(f"{field_name} must be a string")
    if required and not text:
        raise ValueError(f"{field_name} must not be blank")
    return text[:max_chars]


def _nonnegative(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _optional_exit_code(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("exit_code must be an integer or null")
    return value


def _bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _sequence(value: Any, field_name: str, limit: int) -> tuple[Any, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field_name} must be an array")
    return tuple(value[:limit])


@dataclass(frozen=True, slots=True)
class ReceiptChange:
    path: str
    status: str
    source_tool: str = ""
    additions: int = 0
    deletions: int = 0

    @classmethod
    def from_dict(cls, value: Any) -> ReceiptChange:
        data = _mapping(value, "change")
        return cls(
            path=_text(data.get("path"), "change.path", required=True),
            status=_text(data.get("status"), "change.status", required=True),
            source_tool=_text(data.get("source_tool"), "change.source_tool"),
            additions=_nonnegative(data.get("additions", 0), "change.additions"),
            deletions=_nonnegative(data.get("deletions", 0), "change.deletions"),
        )


@dataclass(frozen=True, slots=True)
class ReceiptValidation:
    command: str
    scope: str
    status: str
    exit_code: int | None = None
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    log_ref: str = ""

    @classmethod
    def from_dict(cls, value: Any) -> ReceiptValidation:
        data = _mapping(value, "validation")
        return cls(
            command=_text(data.get("command"), "validation.command", required=True),
            scope=_text(data.get("scope"), "validation.scope"),
            status=_text(data.get("status"), "validation.status", required=True),
            exit_code=_optional_exit_code(data.get("exit_code")),
            passed=_nonnegative(data.get("passed", 0), "validation.passed"),
            failed=_nonnegative(data.get("failed", 0), "validation.failed"),
            skipped=_nonnegative(data.get("skipped", 0), "validation.skipped"),
            log_ref=_text(data.get("log_ref"), "validation.log_ref"),
        )


@dataclass(frozen=True, slots=True)
class ReceiptApproval:
    call_id: str
    tool_name: str
    decision: str
    scope: str = ""

    @classmethod
    def from_dict(cls, value: Any) -> ReceiptApproval:
        data = _mapping(value, "approval")
        return cls(
            call_id=_text(data.get("call_id"), "approval.call_id", required=True),
            tool_name=_text(data.get("tool_name"), "approval.tool_name", required=True),
            decision=_text(data.get("decision"), "approval.decision", required=True),
            scope=_text(data.get("scope"), "approval.scope"),
        )


@dataclass(frozen=True, slots=True)
class ReceiptRisk:
    code: str
    level: str
    message: str

    @classmethod
    def from_dict(cls, value: Any) -> ReceiptRisk:
        data = _mapping(value, "risk")
        return cls(
            code=_text(data.get("code"), "risk.code", required=True),
            level=_text(data.get("level"), "risk.level", required=True),
            message=_text(data.get("message"), "risk.message", required=True),
        )


@dataclass(frozen=True, slots=True)
class ReceiptAction:
    id: str
    label: str
    kind: str

    @classmethod
    def from_dict(cls, value: Any) -> ReceiptAction:
        data = _mapping(value, "next_action")
        return cls(
            id=_text(data.get("id"), "next_action.id", required=True),
            label=_text(data.get("label"), "next_action.label", required=True),
            kind=_text(data.get("kind"), "next_action.kind", required=True),
        )


@dataclass(frozen=True, slots=True)
class ReceiptGitState:
    available: bool = False
    branch: str = ""
    dirty: bool = False
    commit: str = ""
    ahead: int = 0
    behind: int = 0

    @classmethod
    def from_dict(cls, value: Any) -> ReceiptGitState:
        data = _mapping(value, "git_state")
        return cls(
            available=_bool(data.get("available", False), "git_state.available"),
            branch=_text(data.get("branch"), "git_state.branch"),
            dirty=_bool(data.get("dirty", False), "git_state.dirty"),
            commit=_text(data.get("commit"), "git_state.commit"),
            ahead=_nonnegative(data.get("ahead", 0), "git_state.ahead"),
            behind=_nonnegative(data.get("behind", 0), "git_state.behind"),
        )


@dataclass(frozen=True, slots=True)
class CompletionReceipt:
    schema_version: int
    receipt_id: str
    run_id: str
    outcome: ReceiptOutcome
    summary: str = ""
    changes: tuple[ReceiptChange, ...] = ()
    validations: tuple[ReceiptValidation, ...] = ()
    unverified: tuple[str, ...] = ()
    approvals: tuple[ReceiptApproval, ...] = ()
    risks: tuple[ReceiptRisk, ...] = ()
    git_state: ReceiptGitState = field(default_factory=ReceiptGitState)
    next_actions: tuple[ReceiptAction, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    started_at: str = ""
    completed_at: str = ""
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Any) -> CompletionReceipt:
        data = _mapping(value, "completion_receipt")
        schema_version = data.get("schema_version")
        if schema_version != _SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version: {schema_version!r}; expected {_SCHEMA_VERSION}"
            )
        outcome = _text(data.get("outcome"), "outcome", required=True)
        if outcome not in _OUTCOMES:
            raise ValueError(f"invalid outcome: {outcome}")

        raw_unverified = _sequence(
            data.get("unverified", ()), "unverified", _MAX_UNVERIFIED
        )
        raw_evidence_refs = _sequence(
            data.get("evidence_refs", ()), "evidence_refs", _MAX_EVIDENCE_REFS
        )
        return cls(
            schema_version=_SCHEMA_VERSION,
            receipt_id=_text(data.get("receipt_id"), "receipt_id", required=True),
            run_id=_text(data.get("run_id"), "run_id", required=True),
            outcome=outcome,  # type: ignore[arg-type]
            summary=_text(
                data.get("summary"), "summary", max_chars=_MAX_SUMMARY_TEXT
            ),
            changes=tuple(
                ReceiptChange.from_dict(item)
                for item in _sequence(data.get("changes", ()), "changes", _MAX_CHANGES)
            ),
            validations=tuple(
                ReceiptValidation.from_dict(item)
                for item in _sequence(
                    data.get("validations", ()), "validations", _MAX_VALIDATIONS
                )
            ),
            unverified=tuple(
                _text(item, "unverified item", required=True)
                for item in raw_unverified
            ),
            approvals=tuple(
                ReceiptApproval.from_dict(item)
                for item in _sequence(
                    data.get("approvals", ()), "approvals", _MAX_APPROVALS
                )
            ),
            risks=tuple(
                ReceiptRisk.from_dict(item)
                for item in _sequence(data.get("risks", ()), "risks", _MAX_RISKS)
            ),
            git_state=ReceiptGitState.from_dict(data.get("git_state", {})),
            next_actions=tuple(
                ReceiptAction.from_dict(item)
                for item in _sequence(
                    data.get("next_actions", ()), "next_actions", _MAX_ACTIONS
                )
            ),
            evidence_refs=tuple(
                _text(item, "evidence_ref", required=True) for item in raw_evidence_refs
            ),
            started_at=_text(data.get("started_at"), "started_at"),
            completed_at=_text(data.get("completed_at"), "completed_at"),
            duration_ms=_nonnegative(data.get("duration_ms", 0), "duration_ms"),
        )


__all__ = [
    "CompletionReceipt",
    "ReceiptAction",
    "ReceiptApproval",
    "ReceiptChange",
    "ReceiptGitState",
    "ReceiptOutcome",
    "ReceiptRisk",
    "ReceiptValidation",
]
