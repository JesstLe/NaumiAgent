"""会话与消息路由."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime

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

    if body.stream:
        return StreamingResponse(
            _stream_response(engine, session_id, body.content, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    result = await engine.run(body.content)
    return MessageResponse(
        id=uuid.uuid4().hex[:12],
        role="assistant",
        content=result.response,
        timestamp=datetime.now().isoformat(),
        metadata={"turns": result.usage.turns, "cost_usd": result.usage.total_cost_usd},
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

    sub_id = f"sse_{session_id}_{uuid.uuid4().hex[:8]}"
    queue = engine.emitter.subscribe(sub_id)

    agent_task = asyncio.create_task(engine.run(content))

    try:
        while not agent_task.done():
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
                yield event.to_sse()
            except TimeoutError:
                yield ": keepalive\n\n"

        while not queue.empty():
            event = queue.get_nowait()
            yield event.to_sse()
    finally:
        engine.emitter.unsubscribe(sub_id)
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
