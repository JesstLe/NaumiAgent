"""Bounded runtime-event evidence for Inspector tool and approval views."""

from __future__ import annotations

import json
import re
from collections import OrderedDict, deque
from dataclasses import asdict
from typing import Any

from naumi_agent.inspector.models import InspectorApproval, InspectorTool
from naumi_agent.safety.guardrails import OutputGuardrail

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|authorization|cookie)"
    r"(\s*[:=]\s*)(?:bearer\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s,}]+)"
)
_SUCCESS = frozenset({"success", "succeeded", "completed"})
_FAILURE = frozenset({"error", "failed", "aborted", "blocked", "denied"})


class RuntimeInspectorTracker:
    """Correlate a bounded public view of recent engine lifecycle events."""

    def __init__(self, *, max_tools: int = 20, max_approvals: int = 20) -> None:
        self._max_tools = max(1, min(int(max_tools), 100))
        self._max_approvals = max(1, min(int(max_approvals), 100))
        self._tools: OrderedDict[str, InspectorTool] = OrderedDict()
        self._approvals: deque[InspectorApproval] = deque(maxlen=self._max_approvals)
        self._seen_event_ids: deque[str] = deque(maxlen=500)
        self._seen_event_id_set: set[str] = set()
        self.session_id = ""
        self.active_run_id = ""
        self.latest_receipt_id = ""

    @property
    def tools(self) -> tuple[InspectorTool, ...]:
        return tuple(self._tools.values())

    @property
    def approvals(self) -> tuple[InspectorApproval, ...]:
        return tuple(self._approvals)

    def observe(self, event: str, data: dict[str, Any]) -> bool:
        """Observe one event, returning whether public evidence changed."""
        session_changed = self.bind_session(_public(data.get("session_id"), 500))
        event_id = _public(data.get("event_id"), 500)
        if event_id and event_id in self._seen_event_id_set:
            return False
        if event_id:
            self._remember_event_id(event_id)

        run_id = _public(data.get("run_id"), 500)
        if event == "run_started":
            changed = self.active_run_id != run_id
            self.active_run_id = run_id
            return changed or session_changed
        if event == "completion_receipt":
            receipt_id = _public(data.get("receipt_id"), 500)
            changed = bool(self.active_run_id) or self.latest_receipt_id != receipt_id
            self.active_run_id = ""
            self.latest_receipt_id = receipt_id
            return changed or session_changed
        if event in {"run_cancelled", "run_completed"}:
            changed = bool(self.active_run_id)
            self.active_run_id = ""
            return changed or session_changed
        if event in {"tool_start", "tool_end", "tool_error"}:
            return self._observe_tool(event, data, run_id) or session_changed
        if event == "permission_bubble":
            return self._observe_approval(data, run_id) or session_changed
        return session_changed

    def bind_session(self, session_id: str) -> bool:
        """Bind evidence to one session and clear it on a real session switch."""
        if not session_id or session_id == self.session_id:
            return False
        changed = bool(
            self.session_id
            or self._tools
            or self._approvals
            or self.active_run_id
            or self.latest_receipt_id
        )
        self.session_id = session_id
        self._tools.clear()
        self._approvals.clear()
        self._seen_event_ids.clear()
        self._seen_event_id_set.clear()
        self.active_run_id = ""
        self.latest_receipt_id = ""
        return changed

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "active_run_id": self.active_run_id,
            "latest_receipt_id": self.latest_receipt_id,
            "tools": [asdict(item) for item in self.tools],
            "approvals": [asdict(item) for item in self.approvals],
        }

    def _observe_tool(self, event: str, data: dict[str, Any], run_id: str) -> bool:
        name = _public(data.get("name") or data.get("tool_name") or "tool", 500)
        call_id = _public(
            data.get("call_id") or data.get("tool_call_id") or f"{name}:{len(self._tools)}",
            500,
        )
        existing = self._tools.get(call_id)
        status = "running"
        if event == "tool_end":
            raw_status = _public(data.get("status") or "success", 100).lower()
            status = "success" if raw_status in _SUCCESS else "failed"
        elif event == "tool_error":
            status = "failed"
        item = InspectorTool(
            call_id=call_id,
            name=name or (existing.name if existing is not None else "tool"),
            status=status,
            summary=existing.summary if existing is not None else _tool_summary(data),
            duration_ms=_nonnegative(data.get("duration_ms")),
            run_id=run_id or (existing.run_id if existing is not None else ""),
        )
        if existing == item:
            return False
        self._tools[call_id] = item
        self._tools.move_to_end(call_id)
        while len(self._tools) > self._max_tools:
            self._tools.popitem(last=False)
        return True

    def _observe_approval(self, data: dict[str, Any], run_id: str) -> bool:
        tool_name = _public(data.get("tool_name") or data.get("name") or "tool", 500)
        request_id = _public(
            data.get("request_id") or data.get("call_id") or f"permission:{tool_name}",
            500,
        )
        item = InspectorApproval(
            request_id=request_id,
            tool_name=tool_name,
            decision=_approval_decision(data.get("status")),
            reason=_public(data.get("reason"), 500),
            run_id=run_id,
        )
        if self._approvals and self._approvals[-1] == item:
            return False
        self._approvals.append(item)
        return True

    def _remember_event_id(self, event_id: str) -> None:
        if len(self._seen_event_ids) == self._seen_event_ids.maxlen:
            expired = self._seen_event_ids.popleft()
            self._seen_event_id_set.discard(expired)
        self._seen_event_ids.append(event_id)
        self._seen_event_id_set.add(event_id)


def _tool_summary(data: dict[str, Any]) -> str:
    raw = data.get("args")
    if isinstance(raw, dict):
        parsed = raw
    elif isinstance(raw, str):
        try:
            candidate = json.loads(raw)
        except json.JSONDecodeError:
            candidate = None
        parsed = candidate if isinstance(candidate, dict) else {}
    else:
        parsed = {}
    for key in ("path", "file_path", "command", "query", "url", "instruction"):
        if parsed.get(key):
            return _public(parsed[key], 500)
    return _public(raw, 500)


def _approval_decision(value: Any) -> str:
    status = str(value or "").strip().lower()
    if status in {"confirmed", "allow", "allowed", "allowed_once"}:
        return "allowed_once"
    if status in _FAILURE:
        return "denied"
    if status in {"bypass", "bypass_enabled"}:
        return "bypass"
    if status == "needs_confirmation":
        return "pending"
    return status or "unknown"


def _public(value: Any, maximum: int) -> str:
    text = OutputGuardrail.redact(str(value or ""))
    text = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}{match.group(2)}<redacted>", text)
    return text.strip()[:maximum]


def _nonnegative(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


__all__ = ["RuntimeInspectorTracker"]
