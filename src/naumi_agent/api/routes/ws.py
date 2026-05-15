"""WebSocket 路由."""

from __future__ import annotations

import asyncio
import json
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

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

    sub_id = f"ws_{session_id}_{uuid.uuid4().hex[:8]}"
    queue = engine.emitter.subscribe(sub_id)

    await websocket.send_json({"type": "connected", "session_id": session_id})

    push_task = asyncio.create_task(_push_events(websocket, queue))

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
                task = asyncio.create_task(engine.run(content))
                try:
                    result = await task
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
    finally:
        engine.emitter.unsubscribe(sub_id)
        push_task.cancel()


@router.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    """快捷聊天 — 自动创建会话."""
    await websocket.accept()
    engine = websocket.app.state.engine

    from naumi_agent.memory.session import Session

    session = Session(title="WebSocket Chat")
    await engine.session_store.save(session)

    sub_id = f"ws_quick_{session.id}"
    queue = engine.emitter.subscribe(sub_id)

    await websocket.send_json({"type": "connected", "session_id": session.id})

    push_task = asyncio.create_task(_push_events(websocket, queue))

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
                    result = await engine.run(content)
                    await websocket.send_json({"type": "message_complete", "status": result.status})
                except Exception as e:
                    await websocket.send_json({"type": "error", "message": str(e)})
            elif data.get("type") == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        pass
    finally:
        engine.emitter.unsubscribe(sub_id)
        push_task.cancel()
        await engine.session_store.save(session)


async def _push_events(websocket: WebSocket, queue: asyncio.Queue):
    while True:
        try:
            event = await asyncio.wait_for(queue.get(), timeout=30.0)
            await websocket.send_text(event.to_ws())
        except TimeoutError:
            try:
                await websocket.send_json({"type": "ping"})
            except Exception:
                break
        except Exception:
            break
