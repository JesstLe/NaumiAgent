"""Permission state panel shared by terminal UI frontends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from naumi_agent.safety.permissions import (
    PREFIX_PERMISSIONS,
    TOOL_PERMISSIONS,
    PermissionMode,
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
        getter = getattr(engine, "get_recent_permission_bubbles", None)
        if callable(getter):
            history = tuple(
                _with_policy(item)
                for item in getter(limit=safe_limit)
                if isinstance(item, dict)
            )
    except Exception as exc:
        warnings.append(f"权限历史读取失败：{type(exc).__name__}: {exc}")

    grants: tuple[dict[str, Any], ...] = ()
    try:
        getter = getattr(engine, "list_permission_grants", None)
        if callable(getter):
            grants = tuple(
                _grant_item(item)
                for item in getter()
            )
    except Exception as exc:
        warnings.append(f"有效授权读取失败：{type(exc).__name__}: {exc}")

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
        "agent_name": payload.get("agent_name") or payload.get("agent") or "main",
        "tool_name": payload.get("tool_name") or payload.get("tool") or "tool",
        "status": payload.get("status") or "needs_confirmation",
        "reason": payload.get("reason") or payload.get("message") or "等待用户确认。",
    }


def _grant_item(grant: Any) -> dict[str, Any]:
    return {
        "grant_id": str(getattr(grant, "grant_id", "")),
        "tool_family": str(getattr(grant, "tool_family", "")),
        "expires_at": getattr(grant, "expires_at", None),
    }


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
    if rule.requires_confirmation or rule.blocked_commands:
        return PermissionRiskLevel.HIGH
    if PermissionMode.STRICT not in rule.allowed_modes:
        return PermissionRiskLevel.HIGH
    if PermissionMode.LOCKDOWN not in rule.allowed_modes:
        return PermissionRiskLevel.MEDIUM
    return PermissionRiskLevel.LOW


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
    return f"  - {request_id} {agent} -> {tool} [{status}] {policy_text} | {reason}"


def _render_grant(grant: dict[str, Any]) -> str:
    grant_id = str(grant.get("grant_id") or "?")
    tool_family = str(grant.get("tool_family") or "tool")
    expires_at = grant.get("expires_at")
    scope = "本会话" if expires_at is None else f"有效至 {expires_at}"
    return f"  - {grant_id} {tool_family} [{scope}]"
