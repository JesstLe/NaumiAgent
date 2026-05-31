"""WebSocket 路由."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from naumi_agent.api.routes.messages import _engine_event_to_stream_event
from naumi_agent.streaming.events import EventType, StreamEvent

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/sessions/{session_id}")
async def websocket_session(websocket: WebSocket, session_id: str):
    await websocket.accept()
    engine = websocket.app.state.engine

    session = await engine.session_store.load(session_id)
    if not session:
        await websocket.send_json({"type": "error", "message": "Session not found"})
        await websocket.close()
        return

    await websocket.send_json({"type": "connected", "session_id": session_id})

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            msg_type = data.get("type")

            if msg_type == "message":
                content = data.get("content", "")
                if not content:
                    continue
                await websocket.send_json({"type": "message_received"})
                try:
                    result = await _run_streaming_to_websocket(
                        websocket,
                        engine,
                        session_id,
                        content,
                    )
                    await websocket.send_json(
                        {
                            "type": "message_complete",
                            "status": result.status,
                        }
                    )
                except Exception as e:
                    await websocket.send_json({"type": "error", "message": str(e)})

            elif msg_type == "interrupt":
                await websocket.send_json({"type": "interrupted"})

            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        pass


@router.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    """快捷聊天 — 自动创建会话."""
    await websocket.accept()
    engine = websocket.app.state.engine

    from naumi_agent.memory.session import Session

    session = Session(title="WebSocket Chat")
    await engine.session_store.save(session)

    await websocket.send_json({"type": "connected", "session_id": session.id})

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)

            if data.get("type") == "message":
                content = data.get("content", "")
                if not content:
                    continue
                await websocket.send_json({"type": "message_received"})
                try:
                    result = await _run_streaming_to_websocket(
                        websocket,
                        engine,
                        session.id,
                        content,
                    )
                    await websocket.send_json({"type": "message_complete", "status": result.status})
                except Exception as e:
                    await websocket.send_json({"type": "error", "message": str(e)})
            elif data.get("type") == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        pass


async def _run_streaming_to_websocket(
    websocket: WebSocket,
    engine: Any,
    session_id: str,
    content: str,
):
    lock = getattr(websocket.app.state, "engine_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        websocket.app.state.engine_lock = lock

    async def on_event(event: str, data: dict[str, Any]) -> None:
        stream_event = _engine_event_to_stream_event(event, data, session_id=session_id)
        await websocket.send_text(stream_event.to_ws())

    async with lock:
        if not await engine.load_session(session_id):
            await websocket.send_text(
                StreamEvent(
                    type=EventType.AGENT_ERROR,
                    data={"message": "Session not found"},
                    session_id=session_id,
                ).to_ws()
            )
            raise RuntimeError("Session not found")
        return await engine.run_streaming(content, on_event)
