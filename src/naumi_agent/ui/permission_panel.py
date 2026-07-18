"""Permission state panel shared by terminal UI frontends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from naumi_agent.safety.permissions import (
    PREFIX_PERMISSIONS,
    TOOL_PERMISSIONS,
    PermissionRiskLevel,
    PermissionRule,
)


@dataclass(frozen=True)
class PermissionPanelSnapshot:
    """Read-only permission state collected from the bridge and engine."""

    runtime_mode: str = ""
    permission_mode: str = ""
    pending: tuple[dict[str, Any], ...] = ()
    grants: tuple[dict[str, Any], ...] = ()
    history: tuple[dict[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()


PERMISSION_PANEL_SCHEMA_VERSION = 1


def build_permission_panel_snapshot(
    engine: Any,
    *,
    pending: dict[str, dict[str, Any]] | None = None,
    limit: int = 12,
) -> PermissionPanelSnapshot:
    """Collect permission state without mutating the engine."""
    safe_limit = max(1, min(limit, 50))
    warnings: list[str] = []
    pending_items = tuple(
        _with_policy(_pending_item(request_id, payload))
        for request_id, payload in (pending or {}).items()
    )

    history: tuple[dict[str, Any], ...] = ()
    try:
        durable_getter = getattr(engine, "list_permission_decision_receipts", None)
        if callable(durable_getter):
            history = tuple(
                _with_policy(_decision_receipt_item(item))
                for item in durable_getter(limit=safe_limit)
            )
        else:
            getter = getattr(engine, "get_recent_permission_bubbles", None)
            if not callable(getter):
                getter = None
        if not callable(durable_getter) and getter is not None:
            history = tuple(
                _with_policy(item)
                for item in getter(limit=safe_limit)
                if isinstance(item, dict)
            )
    except Exception:
        warnings.append("权限历史暂时无法读取，请稍后刷新。")

    grants: tuple[dict[str, Any], ...] = ()
    try:
        getter = getattr(engine, "list_permission_grants", None)
        if callable(getter):
            grants = tuple(
                _grant_item(item)
                for item in getter()
            )
    except Exception:
        warnings.append("有效授权暂时无法读取，请稍后刷新。")

    runtime_mode = getattr(engine, "runtime_mode", "")
    permission_mode = getattr(engine, "permission_mode", "")
    return PermissionPanelSnapshot(
        runtime_mode=str(getattr(runtime_mode, "value", runtime_mode)),
        permission_mode=str(getattr(permission_mode, "value", permission_mode)),
        pending=pending_items[-safe_limit:],
        grants=grants[-safe_limit:],
        history=history[-safe_limit:],
        warnings=tuple(warnings),
    )


def permission_panel_payload(snapshot: PermissionPanelSnapshot) -> dict[str, Any]:
    """Serialize one bounded permission snapshot without private request fields."""
    return {
        "schema_version": PERMISSION_PANEL_SCHEMA_VERSION,
        "runtime_mode": _text(snapshot.runtime_mode),
        "permission_mode": _text(snapshot.permission_mode),
        "pending": [_permission_payload(item) for item in snapshot.pending[:50]],
        "grants": [_grant_payload(item) for item in snapshot.grants[:50]],
        "history": [_permission_payload(item) for item in snapshot.history[:50]],
        "warnings": [_text(item) for item in snapshot.warnings[:20]],
    }


def render_permission_panel_snapshot(snapshot: PermissionPanelSnapshot) -> str:
    """Render permission state as ANSI-friendly text."""
    lines: list[str] = ["\033[1m权限面板\033[0m"]
    lines.append(
        f"mode: {snapshot.runtime_mode or '-'} | "
        f"permission: {snapshot.permission_mode or '-'}"
    )

    lines.extend(["", "\033[1mPending\033[0m"])
    if snapshot.pending:
        for item in snapshot.pending:
            lines.append(_render_permission_item(item))
    else:
        lines.append("\033[2m  暂无待确认权限\033[0m")

    lines.extend(["", "\033[1m有效授权\033[0m"])
    if snapshot.grants:
        for grant in snapshot.grants:
            lines.append(_render_grant(grant))
    else:
        lines.append("\033[2m  暂无本会话有效授权\033[0m")

    lines.extend(["", "\033[1mHistory\033[0m"])
    if snapshot.history:
        for item in snapshot.history[-10:]:
            lines.append(_render_permission_item(item))
    else:
        lines.append("\033[2m  暂无权限历史\033[0m")

    if snapshot.warnings:
        lines.extend(["", "\033[33m面板警告\033[0m"])
        lines.extend(f"  - {warning}" for warning in snapshot.warnings)

    return "\n".join(lines).rstrip() + "\n"


def render_permission_panel(
    engine: Any,
    *,
    pending: dict[str, dict[str, Any]] | None = None,
    limit: int = 12,
) -> str:
    """Build and render the permission panel."""
    return render_permission_panel_snapshot(
        build_permission_panel_snapshot(engine, pending=pending, limit=limit)
    )


def _pending_item(request_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "call_id": payload.get("call_id") or "",
        "session_id": payload.get("session_id") or "",
        "run_id": payload.get("run_id") or "",
        "agent_name": payload.get("agent_name") or payload.get("agent") or "main",
        "tool_name": payload.get("tool_name") or payload.get("tool") or "tool",
        "tool_family": payload.get("tool_family") or "",
        "arguments_summary": payload.get("arguments_summary") or "",
        "status": payload.get("status") or "needs_confirmation",
        "reason": payload.get("reason") or payload.get("message") or "等待用户确认。",
        "risk_level": payload.get("risk_level") or "high",
        "choices": payload.get("choices") if isinstance(payload.get("choices"), list) else [],
        "scope": payload.get("scope") or "call",
        "expires_at": payload.get("expires_at") or "",
    }


def _grant_item(grant: Any) -> dict[str, Any]:
    return {
        "grant_id": str(getattr(grant, "grant_id", "")),
        "tool_family": str(getattr(grant, "tool_family", "")),
        "created_at": getattr(grant, "created_at", ""),
        "expires_at": getattr(grant, "expires_at", None),
        "source_request_id": str(getattr(grant, "source_request_id", "")),
    }


def _decision_receipt_item(receipt: Any) -> dict[str, Any]:
    outcome = getattr(receipt, "outcome", "")
    source = getattr(receipt, "source", "")
    actor = getattr(receipt, "actor", "")
    return {
        "request_id": str(getattr(receipt, "request_id", "")),
        "call_id": str(getattr(receipt, "call_id", "")),
        "session_id": str(getattr(receipt, "session_id", "")),
        "run_id": str(getattr(receipt, "run_id", "")),
        "agent_name": str(getattr(receipt, "agent_name", "main")),
        "tool_name": str(getattr(receipt, "tool_name", "tool")),
        "tool_family": str(getattr(receipt, "tool_family", "")),
        "status": str(getattr(outcome, "value", outcome)),
        "reason": _decision_reason(str(getattr(outcome, "value", outcome))),
        "risk_level": str(getattr(receipt, "risk_level", "")),
        "scope": "session" if str(getattr(source, "value", source)) == "session_grant" else "call",
        "decided_at": str(getattr(receipt, "decided_at", "")),
        "actor": str(getattr(actor, "value", actor)),
        "source": str(getattr(source, "value", source)),
        "receipt_id": str(getattr(receipt, "receipt_id", "")),
    }


def _decision_reason(outcome: str) -> str:
    return {
        "allow_once": "用户已允许本次工具执行。",
        "session_granted": "用户已授予本会话工具族权限。",
        "bypass_enabled": "用户已启用 bypass 全权限模式。",
        "denied": "用户拒绝执行该工具。",
    }.get(outcome, "已记录终态权限决定。")


def _permission_payload(item: dict[str, Any]) -> dict[str, Any]:
    policy = item.get("policy") if isinstance(item.get("policy"), dict) else {}
    choices = item.get("choices") if isinstance(item.get("choices"), list) else []
    return {
        "request_id": _text(item.get("request_id") or item.get("call_id")),
        "call_id": _text(item.get("call_id")),
        "session_id": _text(item.get("session_id")),
        "run_id": _text(item.get("run_id")),
        "agent_name": _text(item.get("agent_name") or "main"),
        "tool_name": _text(item.get("tool_name") or "tool"),
        "tool_family": _text(item.get("tool_family")),
        "arguments_summary": _text(item.get("arguments_summary")),
        "status": _text(item.get("status")),
        "reason": _text(item.get("reason")),
        "risk_level": _text(item.get("risk_level") or policy.get("risk")),
        "choices": [_text(choice) for choice in choices[:10]],
        "scope": _text(item.get("scope")),
        "expires_at": _text(item.get("expires_at")),
        "receipt_id": _text(item.get("receipt_id")),
        "actor": _text(item.get("actor")),
        "source": _text(item.get("source")),
        "decided_at": _text(item.get("decided_at")),
        "policy": {
            "source": _text(policy.get("source")),
            "risk": _text(policy.get("risk")),
            "modes": _text(policy.get("modes")),
            "confirmation": _text(policy.get("confirmation")),
            "bypass": _text(policy.get("bypass")),
        },
    }


def _grant_payload(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "grant_id": _text(item.get("grant_id")),
        "tool_family": _text(item.get("tool_family")),
        "created_at": _text(item.get("created_at")),
        "expires_at": _text(item.get("expires_at")),
        "source_request_id": _text(item.get("source_request_id")),
    }


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())[:500]


def _with_policy(item: dict[str, Any]) -> dict[str, Any]:
    policy = _policy_for_tool(str(item.get("tool_name") or ""))
    return {**item, "policy": policy}


def _policy_for_tool(tool_name: str) -> dict[str, str]:
    source, rule = _resolve_permission_rule(tool_name)
    if rule is None:
        if tool_name.startswith("mcp__"):
            return {
                "source": "dynamic:mcp",
                "risk": PermissionRiskLevel.HIGH.value,
                "modes": "bypass/permissive/moderate",
                "confirmation": "按动态 MCP 策略",
                "bypass": "bypass 允许；仍受工具实现和危险命令硬限制",
            }
        return {
            "source": "unknown_tool",
            "risk": PermissionRiskLevel.HIGH.value,
            "modes": "-",
            "confirmation": "其他模式未知工具会被拒绝",
            "bypass": "bypass 全权限放行",
        }

    return {
        "source": source,
        "risk": _risk_for_rule(rule).value,
        "modes": "/".join(mode.value for mode in rule.allowed_modes),
        "confirmation": "需要确认" if rule.requires_confirmation else "无需确认",
        "bypass": _bypass_scope(rule),
    }


def _resolve_permission_rule(tool_name: str) -> tuple[str, PermissionRule | None]:
    candidates = [tool_name]
    if "." in tool_name:
        candidates.append(tool_name.split(".")[-1])
    if "__" in tool_name:
        candidates.append(tool_name.split("__")[-1])

    for candidate in candidates:
        if candidate in TOOL_PERMISSIONS:
            return f"TOOL_PERMISSIONS:{candidate}", TOOL_PERMISSIONS[candidate]

    for candidate in candidates:
        for prefix, rule in PREFIX_PERMISSIONS.items():
            if candidate.startswith(prefix):
                return f"PREFIX_PERMISSIONS:{prefix}", rule

    return "unknown_tool", None


def _risk_for_rule(rule: PermissionRule) -> PermissionRiskLevel:
    return rule.risk_level


def _bypass_scope(_rule: PermissionRule) -> str:
    return "bypass 全权限放行；不执行确认、路径、命令与次数检查"


def _render_permission_item(item: dict[str, Any]) -> str:
    request_id = str(item.get("request_id") or item.get("call_id") or "?")
    agent = str(item.get("agent_name") or "main")
    tool = str(item.get("tool_name") or "tool")
    status = str(item.get("status") or "?")
    reason = str(item.get("reason") or "")
    if len(reason) > 120:
        reason = reason[:117] + "..."
    policy = item.get("policy") if isinstance(item.get("policy"), dict) else {}
    policy_text = (
        f"风险:{policy.get('risk', '-')}"
        f" · 来源:{policy.get('source', '-')}"
        f" · 模式:{policy.get('modes', '-')}"
        f" · 确认:{policy.get('confirmation', '-')}"
        f" · {policy.get('bypass', '-')}"
    )
    audit = ""
    if item.get("receipt_id"):
        audit = (
            f" · 操作者:{item.get('actor') or '-'}"
            f" · 决策源:{item.get('source') or '-'}"
            f" · 时间:{item.get('decided_at') or '-'}"
        )
    return (
        f"  - {request_id} {agent} -> {tool} [{status}] "
        f"{policy_text}{audit} | {reason}"
    )


def _render_grant(grant: dict[str, Any]) -> str:
    grant_id = str(grant.get("grant_id") or "?")
    tool_family = str(grant.get("tool_family") or "tool")
    expires_at = grant.get("expires_at")
    scope = "本会话" if expires_at is None else f"有效至 {expires_at}"
    return f"  - {grant_id} {tool_family} [{scope}]"
