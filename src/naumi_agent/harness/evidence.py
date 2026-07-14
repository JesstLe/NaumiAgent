"""Concurrent, digest-only collection of normalized Harness tool evidence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from naumi_agent.harness.completion import HarnessEvidenceRef
from naumi_agent.harness.store import HarnessStore
from naumi_agent.safety.guardrails import OutputGuardrail

_SUCCESS_STATUSES = frozenset({"success", "succeeded", "completed", "passed"})
_SENSITIVE_KEY_PARTS = (
    "token",
    "api_key",
    "apikey",
    "secret",
    "password",
    "credential",
    "bearer",
    "auth",
    "cookie",
    "private_key",
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"\b(api[_-]?key|token|secret|password|passwd|authorization|cookie|credential)"
    r"\s*([:=])\s*([^\s,;]+)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class _ToolStart:
    tool_name: str
    arguments_sha256: str
    arguments_size_bytes: int
    read_only: bool
    destructive: bool
    start_missing: bool = False
    permission_status: str = "not_observed"
    permission_risk_level: str = ""
    permission_required_confirmation: bool = False


class EvidenceCollector:
    """Pair tool events and persist only normalized metadata and digests."""

    def __init__(
        self,
        *,
        store: HarnessStore,
        max_runs: int = 128,
        max_calls_per_run: int = 256,
    ) -> None:
        if not 1 <= max_runs <= 4_096:
            raise ValueError("max_runs 必须在 1 到 4096 之间。")
        if not 1 <= max_calls_per_run <= 16_384:
            raise ValueError("max_calls_per_run 必须在 1 到 16384 之间。")
        self._store = store
        self._max_runs = max_runs
        self._max_calls_per_run = max_calls_per_run
        self._lock = asyncio.Lock()
        self._run_order: OrderedDict[str, None] = OrderedDict()
        self._pending: dict[str, OrderedDict[str, _ToolStart]] = {}
        self._completed: dict[
            str,
            OrderedDict[str, HarnessEvidenceRef],
        ] = {}
        self._inflight: dict[
            tuple[str, str],
            asyncio.Task[HarnessEvidenceRef],
        ] = {}

    async def observe(
        self,
        *,
        run_id: str,
        event: str,
        data: Mapping[str, Any],
    ) -> HarnessEvidenceRef | None:
        """Observe one normalized tool event without retaining raw payloads."""
        normalized_run_id = _bounded_text(run_id, field="run_id", maximum=128)
        if event not in {"tool_start", "tool_end", "permission_bubble"}:
            return None
        call_id = str(data.get("call_id") or data.get("tool_call_id") or "").strip()
        if not call_id:
            return None
        call_key = _sha256_text(call_id)
        if event == "tool_start":
            start = _normalize_start(data)
            async with self._lock:
                if call_key in self._completed.get(normalized_run_id, {}):
                    return None
                self._touch_run(normalized_run_id)
                pending = self._pending.setdefault(normalized_run_id, OrderedDict())
                previous = pending.get(call_key)
                if previous is not None and previous.start_missing:
                    start = replace(
                        start,
                        permission_status=previous.permission_status,
                        permission_risk_level=previous.permission_risk_level,
                        permission_required_confirmation=(
                            previous.permission_required_confirmation
                        ),
                    )
                pending[call_key] = start
                pending.move_to_end(call_key)
                while len(pending) > self._max_calls_per_run:
                    pending.popitem(last=False)
            return None

        if event == "permission_bubble":
            async with self._lock:
                if call_key in self._completed.get(normalized_run_id, {}):
                    return None
                self._touch_run(normalized_run_id)
                pending = self._pending.setdefault(normalized_run_id, OrderedDict())
                start = pending.get(call_key) or _missing_start(data)
                risk_level = _safe_status(data.get("risk_level"))
                if risk_level == "unknown":
                    risk_level = start.permission_risk_level
                pending[call_key] = replace(
                    start,
                    permission_status=_safe_status(data.get("status")),
                    permission_risk_level=risk_level,
                    permission_required_confirmation=(
                        start.permission_required_confirmation
                        or bool(data.get("requires_confirmation"))
                    ),
                )
                pending.move_to_end(call_key)
                while len(pending) > self._max_calls_per_run:
                    pending.popitem(last=False)
            return None

        key = (normalized_run_id, call_key)
        async with self._lock:
            completed = self._completed.get(normalized_run_id, {}).get(call_key)
            if completed is not None:
                return completed
            task = self._inflight.get(key)
            if task is None:
                pending = self._pending.get(normalized_run_id, {}).get(call_key)
                end_snapshot = dict(data)
                task = asyncio.create_task(
                    self._persist_end(
                        run_id=normalized_run_id,
                        call_key=call_key,
                        start=pending,
                        data=end_snapshot,
                    )
                )
                self._inflight[key] = task
        return await asyncio.shield(task)

    async def list_refs(self, run_id: str) -> tuple[HarnessEvidenceRef, ...]:
        normalized_run_id = _bounded_text(run_id, field="run_id", maximum=128)
        async with self._lock:
            refs = self._completed.get(normalized_run_id)
            if refs is None:
                return ()
            self._touch_run(normalized_run_id)
            return tuple(refs.values())

    async def forget_run(self, run_id: str) -> None:
        normalized_run_id = _bounded_text(run_id, field="run_id", maximum=128)
        async with self._lock:
            self._run_order.pop(normalized_run_id, None)
            self._pending.pop(normalized_run_id, None)
            self._completed.pop(normalized_run_id, None)

    async def _persist_end(
        self,
        *,
        run_id: str,
        call_key: str,
        start: _ToolStart | None,
        data: Mapping[str, Any],
    ) -> HarnessEvidenceRef:
        key = (run_id, call_key)
        try:
            tool_name = (
                start.tool_name
                if start is not None
                else _safe_text(data.get("name") or data.get("tool_name") or "tool", 128)
            )
            status = _safe_status(data.get("status"))
            duration_ms = _bounded_duration(data.get("duration_ms"))
            raw_content = str(data.get("content") or "")
            safe_content = _redact_text(raw_content)
            arguments_sha256 = (
                start.arguments_sha256 if start is not None else _sha256_text("{}")
            )
            arguments_size_bytes = start.arguments_size_bytes if start is not None else 0
            summary: dict[str, Any] = {
                "tool_name": tool_name,
                "call_id_sha256": call_key,
                "status": status,
                "duration_ms": duration_ms,
                "arguments_sha256": arguments_sha256,
                "arguments_size_bytes": arguments_size_bytes,
                "result_sha256": _sha256_text(safe_content),
                "result_size_bytes": _bounded_size(
                    data.get("content_length"),
                    fallback=len(raw_content.encode("utf-8", errors="replace")),
                ),
                "read_only": start.read_only if start is not None else bool(data.get("read_only")),
                "destructive": (
                    start.destructive if start is not None else bool(data.get("destructive"))
                ),
                "start_missing": start is None or start.start_missing,
                "permission_status": (
                    start.permission_status if start is not None else "not_observed"
                ),
                "permission_risk_level": (
                    start.permission_risk_level if start is not None else ""
                ),
                "permission_required_confirmation": (
                    start.permission_required_confirmation
                    if start is not None
                    else False
                ),
            }
            event_digest = _sha256_text(_canonical_json(summary))
            evidence_id = f"tool-{_sha256_text(f'{run_id}\0{call_key}')[:32]}"
            outcome = "成功" if status in _SUCCESS_STATUSES else "结束"
            evidence = HarnessEvidenceRef(
                id=evidence_id,
                kind="tool_execution",
                summary=f"工具 {tool_name} 执行{outcome}（{duration_ms}ms）",
            )
            await self._store.record_evidence(
                run_id=run_id,
                evidence=evidence,
                uri=f"chat-run://{quote(run_id, safe='')}/tool/{evidence_id}",
                sha256=event_digest,
                summary=summary,
                producer="harness_evidence_collector",
                created_at=datetime.now(UTC).isoformat(),
            )
        except BaseException:
            async with self._lock:
                self._inflight.pop(key, None)
            raise

        async with self._lock:
            self._touch_run(run_id)
            pending = self._pending.get(run_id)
            if pending is not None:
                pending.pop(call_key, None)
            completed = self._completed.setdefault(run_id, OrderedDict())
            completed[call_key] = evidence
            completed.move_to_end(call_key)
            while len(completed) > self._max_calls_per_run:
                completed.popitem(last=False)
            self._inflight.pop(key, None)
        return evidence

    def _touch_run(self, run_id: str) -> None:
        self._run_order[run_id] = None
        self._run_order.move_to_end(run_id)
        while len(self._run_order) > self._max_runs:
            evicted, _ = self._run_order.popitem(last=False)
            self._pending.pop(evicted, None)
            self._completed.pop(evicted, None)


def _normalize_start(data: Mapping[str, Any]) -> _ToolStart:
    safe_arguments = _safe_arguments(data.get("args"))
    canonical = _canonical_json(safe_arguments)
    return _ToolStart(
        tool_name=_safe_text(data.get("name") or data.get("tool_name") or "tool", 128),
        arguments_sha256=_sha256_text(canonical),
        arguments_size_bytes=len(canonical.encode("utf-8")),
        read_only=bool(data.get("read_only")),
        destructive=bool(data.get("destructive")),
    )


def _missing_start(data: Mapping[str, Any]) -> _ToolStart:
    return _ToolStart(
        tool_name=_safe_text(data.get("tool_name") or data.get("name") or "tool", 128),
        arguments_sha256=_sha256_text("{}"),
        arguments_size_bytes=0,
        read_only=bool(data.get("read_only")),
        destructive=bool(data.get("destructive")),
        start_missing=True,
    )


def _safe_arguments(value: Any) -> Any:
    parsed: Any = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = {"invalid_json": _redact_text(value)}
    return _redact_payload(parsed)


def _redact_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized_key = key.casefold().replace("-", "_")
            if any(part in normalized_key for part in _SENSITIVE_KEY_PARTS):
                redacted[key] = "<redacted>"
            else:
                redacted[key] = _redact_payload(item)
        return redacted
    if isinstance(value, (list, tuple, set)):
        return [_redact_payload(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _safe_text(value, 512)


def _redact_text(value: str) -> str:
    redacted = OutputGuardrail.redact(value)
    return _SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>",
        redacted,
    )


def _safe_text(value: Any, maximum: int) -> str:
    normalized = _redact_text(str(value or "")).strip()
    if not normalized:
        return "tool"
    return normalized[:maximum]


def _safe_status(value: Any) -> str:
    normalized = str(value or "unknown").strip().lower()
    if not normalized:
        return "unknown"
    return normalized[:64]


def _bounded_duration(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return min(max(parsed, 0), 86_400_000)


def _bounded_size(value: Any, *, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return max(fallback, 0)
    return min(max(parsed, 0), 2_147_483_647)


def _bounded_text(value: str, *, field: str, maximum: int) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized:
        raise ValueError(f"{field} 不能为空。")
    if len(normalized) > maximum:
        raise ValueError(f"{field} 长度不能超过 {maximum}。")
    return normalized


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        return json.dumps(
            _safe_text(value, 2_000),
            ensure_ascii=False,
            separators=(",", ":"),
        )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = ["EvidenceCollector"]
