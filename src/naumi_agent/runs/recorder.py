"""Engine-neutral lifecycle recorder for durable streamed runs."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

from naumi_agent.runs.models import CompletionReceipt
from naumi_agent.runs.receipt_builder import RunReceiptBuilder
from naumi_agent.runs.store import ChatRunRecord, ChatRunStore
from naumi_agent.runtime.ports.events import RuntimeEvent
from naumi_agent.safety.guardrails import OutputGuardrail
from naumi_agent.streaming.sinks import CallbackEventSink

_SUCCESS_STATUSES = frozenset({"success", "succeeded", "completed"})
_FAILURE_STATUSES = frozenset({"error", "failed", "aborted", "denied"})


class ChatRunRecorder:
    """Persist one engine run and its authoritative completion receipt."""

    def __init__(
        self,
        *,
        store: ChatRunStore,
        record: ChatRunRecord,
        builder: RunReceiptBuilder,
    ) -> None:
        self.store = store
        self.record = record
        self.builder = builder
        self._step_sequences: dict[str, int] = {}
        self._finish_lock = asyncio.Lock()
        self._receipt: CompletionReceipt | None = None
        self._guardrail = OutputGuardrail()

    @property
    def run_id(self) -> str:
        return self.record.id

    @classmethod
    async def start(
        cls,
        *,
        store: ChatRunStore,
        workspace_root: str | Path,
        session_id: str,
        task: str,
        run_id: str | None = None,
    ) -> ChatRunRecorder:
        record = await store.start_run(
            session_id=session_id,
            user_message_id=f"msg-{uuid.uuid4().hex[:12]}",
            run_id=run_id,
        )
        builder = await RunReceiptBuilder.start(
            workspace_root=workspace_root,
            run_id=record.id,
            started_at=record.started_at,
        )
        recorder = cls(store=store, record=record, builder=builder)
        await recorder._append_step(
            "request",
            stage="request",
            status="completed",
            summary=_compact(task, 160),
            event_id=f"{record.id}:request",
        )
        return recorder

    async def observe(self, event: str, data: dict[str, Any]) -> None:
        """Persist a raw engine event and feed it into deterministic evidence parsing."""
        self.builder.observe(event, data)
        fields = _step_fields(event, data)
        if fields is not None:
            step_key, stage, status, summary, detail = fields
            await self._append_step(
                step_key,
                stage=stage,
                status=status,
                summary=summary,
                detail=detail,
                event_id=str(data.get("event_id") or f"{self.run_id}:{step_key}"),
            )

        name = str(data.get("name") or data.get("tool_name") or "")
        if event == "tool_end" and name == "delegate_task":
            content = self._public_text(data.get("content"), 420)
            if content:
                call_id = str(data.get("call_id") or uuid.uuid4().hex[:12])
                await self.store.append_artifact(
                    self.run_id,
                    artifact_id=f"subagent-{call_id}",
                    kind="subagent",
                    title="delegate_task",
                    summary={"text": content},
                    status=str(data.get("status") or "success"),
                )

    async def finish(self, status: str, summary: str) -> CompletionReceipt:
        """Build and durably store the receipt exactly once."""
        async with self._finish_lock:
            if self._receipt is not None:
                return self._receipt
            receipt = await self.builder.finish(status, summary)
            await self.store.finish_run(
                self.run_id,
                status=_stored_status(status),
                receipt=receipt,
            )
            self._receipt = receipt
            return receipt

    async def _append_step(
        self,
        step_key: str,
        *,
        stage: str,
        status: str,
        summary: str,
        detail: str = "",
        event_id: str = "",
    ) -> None:
        sequence = self._step_sequences.setdefault(
            step_key,
            len(self._step_sequences) + 1,
        )
        await self.store.append_step(
            self.run_id,
            sequence=sequence,
            stage=stage,
            status=status,
            summary=self._public_text(summary, 500),
            detail=self._public_text(detail, 2_000),
            event_id=event_id[:500],
        )

    def _public_text(self, value: Any, maximum: int) -> str:
        return _compact(self._guardrail.redact(str(value or "")), maximum)


class ChatRunRecorderEventSink:
    """Deliver immutable Runtime events to one durable run recorder."""

    def __init__(self, recorder: ChatRunRecorder) -> None:
        if not isinstance(recorder, ChatRunRecorder):
            raise TypeError("ChatRunRecorderEventSink 需要 ChatRunRecorder")
        self._sink = CallbackEventSink(recorder.observe)

    async def emit(self, event: RuntimeEvent) -> None:
        await self._sink.emit(event)


def _step_fields(
    event: str,
    data: dict[str, Any],
) -> tuple[str, str, str, str, str] | None:
    if event in {"turn_start", "thinking_start", "thinking_delta", "thinking_end"}:
        return "analysis", "analysis", "running", "分析请求", ""
    if event in {"tool_start", "tool_end", "tool_error", "permission_bubble"}:
        name = str(data.get("tool_name") or data.get("name") or "tool")
        call_id = str(data.get("call_id") or data.get("request_id") or name)
        if event == "permission_bubble":
            permission_status = str(data.get("status") or "")
            if permission_status == "needs_confirmation":
                return (
                    f"tool:{call_id}",
                    "approval",
                    "awaiting_approval",
                    name,
                    str(data.get("reason") or ""),
                )
            if permission_status in {"denied", "confirmation_error", "blocked"}:
                return f"tool:{call_id}", "approval", "failed", name, ""
            return f"tool:{call_id}", "tool", "running", name, ""
        if event == "tool_end":
            raw_status = str(data.get("status") or "").lower()
            status = "completed" if raw_status in _SUCCESS_STATUSES else "failed"
            detail = str(data.get("content") or "") if name == "delegate_task" else ""
            return f"tool:{call_id}", "tool", status, name, detail
        if event == "tool_error":
            return f"tool:{call_id}", "tool", "failed", name, ""
        return f"tool:{call_id}", "tool", "running", name, ""
    if event in {"response_start", "token", "response_end"}:
        status = "completed" if event == "response_end" else "running"
        return "response", "response", status, "生成答复", ""
    if event == "error":
        return (
            "response",
            "response",
            "failed",
            "生成答复",
            str(data.get("message") or ""),
        )
    return None


def _stored_status(status: str) -> str:
    normalized = status.strip().lower()
    if normalized == "cancelled":
        return "cancelled"
    if normalized in _SUCCESS_STATUSES:
        return "completed"
    if normalized in _FAILURE_STATUSES or normalized:
        return "failed"
    return "failed"


def _compact(value: str, maximum: int) -> str:
    normalized = value.strip()
    if len(normalized) <= maximum:
        return normalized
    return f"{normalized[: maximum - 3]}..."


__all__ = ["ChatRunRecorder", "ChatRunRecorderEventSink"]
