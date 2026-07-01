"""会话与消息路由."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from naumi_agent.api.deps import AuthDep
from naumi_agent.api.schemas import (
    MessageCreate,
    MessageListResponse,
    MessageResponse,
    SessionCreate,
    SessionListResponse,
    SessionResponse,
)
from naumi_agent.streaming.events import EventType, StreamEvent

router = APIRouter(tags=["sessions", "messages"])


# --- Sessions ---


@router.post("/sessions", response_model=SessionResponse, status_code=201)
async def create_session(body: SessionCreate, request: Request, auth: str = AuthDep):
    engine = request.app.state.engine
    session = await engine.session_store.create_session(
        title=body.title,
        model=body.model,
        system_prompt=body.system_prompt,
    )
    return _session_to_response(session)


@router.get("/sessions", response_model=SessionListResponse)
async def list_sessions(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    sessions, total = await engine.session_store.list_sessions(page=page, page_size=page_size)
    return SessionListResponse(
        sessions=[_session_to_response(s) for s in sessions],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session(session_id: str, request: Request, auth: str = AuthDep):
    engine = request.app.state.engine
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return _session_to_response(session)


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(session_id: str, request: Request, auth: str = AuthDep):
    engine = request.app.state.engine
    deleted = await engine.session_store.delete(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")


# --- Messages ---


@router.post("/sessions/{session_id}/messages")
async def send_message(
    session_id: str,
    body: MessageCreate,
    request: Request,
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if body.stream and body.workbench_issue is not None:
        raise HTTPException(status_code=400, detail="流式对话暂不支持同步创建 Issue")

    if body.stream:
        return StreamingResponse(
            _stream_response(engine, session_id, body.content, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async with _engine_lock(request):
        if not await engine.load_session(session_id):
            raise HTTPException(status_code=404, detail="Session not found")
        result = await engine.run(body.content)
        workbench_metadata = await _create_workbench_issue_from_message(
            engine, session_id, body
        )
    metadata = {"turns": result.usage.turns, "cost_usd": result.usage.total_cost_usd}
    metadata.update(workbench_metadata)
    return MessageResponse(
        id=uuid.uuid4().hex[:12],
        role="assistant",
        content=result.response,
        timestamp=datetime.now().isoformat(),
        metadata=metadata,
    )


@router.get("/sessions/{session_id}/messages", response_model=MessageListResponse)
async def list_messages(
    session_id: str,
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    start = (page - 1) * page_size
    end = start + page_size
    msgs = session.messages[start:end]

    return MessageListResponse(
        messages=[
            MessageResponse(
                id=uuid.uuid4().hex[:8],
                role=m.get("role", "unknown"),
                content=m.get("content", ""),
                timestamp=m.get("timestamp", ""),
            )
            for m in msgs
        ],
        total=len(session.messages),
    )


# --- SSE Stream ---


async def _stream_response(engine, session_id: str, content: str, request: Request):
    queue: asyncio.Queue[StreamEvent | None] = asyncio.Queue()

    async def on_event(event: str, data: dict[str, Any]) -> None:
        await queue.put(_engine_event_to_stream_event(event, data, session_id=session_id))

    async def run_agent() -> None:
        async with _engine_lock(request):
            if not await engine.load_session(session_id):
                await queue.put(
                    StreamEvent(
                        type=EventType.AGENT_ERROR,
                        data={"message": "Session not found"},
                        session_id=session_id,
                    )
                )
                return
            result = await engine.run_streaming(content, on_event)
            await queue.put(
                StreamEvent(
                    type=EventType.AGENT_END,
                    data={
                        "status": result.status,
                        "turns": result.usage.turns,
                        "cost_usd": result.usage.total_cost_usd,
                    },
                    session_id=session_id,
                )
            )

    agent_task = asyncio.create_task(run_agent())
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
                if event is None:
                    break
                yield event.to_sse()
            except TimeoutError:
                yield ": keepalive\n\n"
            if agent_task.done() and queue.empty():
                break
    finally:
        try:
            await agent_task
        except Exception:
            pass


# --- Helpers ---


def _session_to_response(session) -> SessionResponse:
    return SessionResponse(
        id=session.id,
        title=session.title,
        model=session.model,
        created_at=session.created_at.isoformat(),
        updated_at=session.updated_at.isoformat(),
        message_count=len(session.messages),
        total_tokens=session.total_tokens,
        total_cost_usd=session.total_cost_usd,
        status=session.status,
    )


def _engine_lock(request: Request):
    lock = getattr(request.app.state, "engine_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        request.app.state.engine_lock = lock
    return lock


async def _create_workbench_issue_from_message(
    engine,
    session_id: str,
    body: MessageCreate,
) -> dict[str, Any]:
    if body.workbench_issue is None:
        return {}
    if body.stream:
        raise HTTPException(status_code=400, detail="流式对话暂不支持同步创建 Issue")
    workbench_service = getattr(engine, "workbench_service", None)
    if workbench_service is None:
        raise HTTPException(status_code=503, detail="Workbench 服务暂不可用")

    issue_request = body.workbench_issue
    try:
        issue = await workbench_service.create_issue(
            session_id=session_id,
            mission_id=issue_request.mission_id,
            title=issue_request.title,
            description=issue_request.description,
            blocked_by=issue_request.blocked_by,
            acceptance_criteria=issue_request.acceptance_criteria,
            parallel_mode=issue_request.parallel_mode,
            risk_level=issue_request.risk_level,
        )
        snapshot = await workbench_service.dashboard_snapshot(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return {
        "workbench_issue": issue,
        "workbench_snapshot": snapshot,
    }


def _engine_event_to_stream_event(
    event: str,
    data: dict[str, Any],
    *,
    session_id: str,
) -> StreamEvent:
    event_type = {
        "turn_start": EventType.TURN_START,
        "thinking_start": EventType.THINKING_START,
        "thinking_delta": EventType.THINKING_DELTA,
        "thinking_end": EventType.THINKING_END,
        "tool_start": EventType.TOOL_CALL_START,
        "tool_end": (
            EventType.TOOL_CALL_END
            if data.get("status") in (None, "success")
            else EventType.TOOL_CALL_ERROR
        ),
        "token": EventType.TOKEN_DELTA,
        "context_compacted": EventType.CONTEXT_COMPACTED,
        "error": EventType.AGENT_ERROR,
        "response_start": EventType.AGENT_START,
        "response_end": EventType.AGENT_END,
    }.get(event, EventType.TURN_END)

    payload = dict(data)
    if event == "token" and "content" in payload:
        payload["token"] = payload.pop("content")
    return StreamEvent(type=event_type, data=payload, session_id=session_id)
