"""会话与消息路由."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from naumi_agent.api.chat_environment import ChatEnvironmentCollector
from naumi_agent.api.chat_runs import ChatRunRecord, ChatRunStore, SourceReferenceRecord
from naumi_agent.api.deps import AuthDep
from naumi_agent.api.schemas import (
    ChatArtifactResponse,
    ChatBackgroundProcessResponse,
    ChatEnvironmentResponse,
    ChatGitEnvironmentResponse,
    ChatRunCancelResponse,
    ChatRunListResponse,
    ChatRunResponse,
    ChatRunStepResponse,
    ChatSourceCreate,
    ChatSourceReferenceResponse,
    MessageCreate,
    MessageListResponse,
    MessageResponse,
    PermissionResolutionCreate,
    PermissionResolutionResponse,
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
    deleted = await engine.delete_session(session_id)
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
    if body.source_ids or body.linked_issue_id:
        turn_context, source_records, linked_issue = await _build_turn_context(
            engine=engine,
            store=_chat_run_store(request),
            session_id=session_id,
            source_ids=body.source_ids,
            linked_issue_id=body.linked_issue_id,
        )
    else:
        turn_context, source_records, linked_issue = "", [], None

    has_issue_draft = body.workbench_issue is not None
    explicit_stream = "stream" in body.model_fields_set
    if body.stream and has_issue_draft and explicit_stream:
        raise HTTPException(status_code=400, detail="流式对话暂不支持同步创建 Issue")
    if body.stream and has_issue_draft:
        body.stream = False

    if body.stream:
        return StreamingResponse(
            _stream_response(
                engine,
                session_id,
                body.content,
                request,
                turn_context=turn_context,
                source_records=source_records,
                linked_issue=linked_issue,
                runtime_mode=body.runtime_mode,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async with _engine_lock(request):
        if not await engine.load_session(session_id):
            raise HTTPException(status_code=404, detail="Session not found")
        previous_runtime_mode = engine.runtime_mode
        engine.set_runtime_mode(body.runtime_mode)
        try:
            result = await engine.run(body.content, turn_context=turn_context)
            workbench_metadata = await _create_workbench_issue_from_message(
                engine, session_id, body
            )
        finally:
            engine.set_runtime_mode(previous_runtime_mode)
    metadata = {"turns": result.usage.turns, "cost_usd": result.usage.total_cost_usd}
    if linked_issue is not None:
        metadata["linked_issue"] = {"task_id": linked_issue.task_id}
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
                id=str(m.get("id") or f"msg-{start + index + 1}"),
                role=m.get("role", "unknown"),
                content=str(m.get("content") or ""),
                timestamp=m.get("timestamp", ""),
                metadata=m.get("metadata", {}),
            )
            for index, m in enumerate(msgs)
        ],
        total=len(session.messages),
    )


@router.get(
    "/sessions/{session_id}/runs",
    response_model=ChatRunListResponse,
)
async def list_chat_runs(
    session_id: str,
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    auth: str = AuthDep,
):
    store = _chat_run_store(request)
    runs = await store.list_runs(session_id, limit=limit)
    return ChatRunListResponse(
        runs=[_chat_run_to_response(run) for run in runs],
        total=len(runs),
    )


@router.get(
    "/sessions/{session_id}/runs/{run_id}",
    response_model=ChatRunResponse,
)
async def get_chat_run(
    session_id: str,
    run_id: str,
    request: Request,
    auth: str = AuthDep,
):
    run = await _chat_run_store(request).get_run(session_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Chat run not found")
    return _chat_run_to_response(run)


@router.get(
    "/sessions/{session_id}/environment",
    response_model=ChatEnvironmentResponse,
)
async def get_chat_environment(
    session_id: str,
    request: Request,
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    if not await engine.session_store.load(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    snapshot = await ChatEnvironmentCollector(
        workspace_root=engine.workspace_root,
        background_store=engine.background_runner.store,
        chat_run_store=_chat_run_store(request),
    ).collect(session_id=session_id)
    return ChatEnvironmentResponse(
        session_id=snapshot.session_id,
        workspace_root=snapshot.workspace_root,
        workspace_name=snapshot.workspace_name,
        git=ChatGitEnvironmentResponse(**asdict(snapshot.git)),
        processes=[
            ChatBackgroundProcessResponse(**asdict(process))
            for process in snapshot.processes
        ],
        sources=[
            ChatSourceReferenceResponse(**asdict(source)) for source in snapshot.sources
        ],
    )


@router.post(
    "/sessions/{session_id}/sources",
    response_model=ChatSourceReferenceResponse,
    status_code=201,
)
async def add_chat_source(
    session_id: str,
    body: ChatSourceCreate,
    request: Request,
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    if not await engine.session_store.load(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    workspace_root = Path(engine.workspace_root).expanduser().resolve()
    requested = Path(body.path).expanduser()
    resolved = (
        requested.resolve()
        if requested.is_absolute()
        else (workspace_root / requested).resolve()
    )
    try:
        relative_path = str(resolved.relative_to(workspace_root))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="来源必须位于当前工作区内") from exc
    if not resolved.is_file():
        raise HTTPException(status_code=400, detail="来源文件不存在或不是普通文件")
    source = await _chat_run_store(request).add_source(
        session_id=session_id,
        kind=body.kind,
        title=body.title.strip() or resolved.name,
        path=relative_path,
    )
    return ChatSourceReferenceResponse(
        id=source.id,
        kind=source.kind,
        title=source.title,
        path=source.path,
        created_at=source.created_at,
    )


@router.post(
    "/sessions/{session_id}/runs/{run_id}/cancel",
    response_model=ChatRunCancelResponse,
)
async def cancel_chat_run(
    session_id: str,
    run_id: str,
    request: Request,
    auth: str = AuthDep,
):
    active = _active_chat_run_tasks(request)
    entry = active.get(run_id)
    if entry is not None:
        active_session_id, task = entry
        if active_session_id != session_id:
            raise HTTPException(status_code=404, detail="Chat run not found")
        task.cancel()
        return ChatRunCancelResponse(status="cancellation_requested")

    run = await _chat_run_store(request).get_run(session_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Chat run not found")
    return ChatRunCancelResponse(status="already_finished")


@router.post(
    "/sessions/{session_id}/permissions/{call_id}/resolve",
    response_model=PermissionResolutionResponse,
)
async def resolve_permission(
    session_id: str,
    call_id: str,
    body: PermissionResolutionCreate,
    request: Request,
    auth: str = AuthDep,
):
    broker = getattr(request.app.state, "permission_broker", None)
    if broker is None:
        raise HTTPException(status_code=404, detail="未找到待确认的权限请求")
    resolved = await broker.resolve(session_id, call_id, body.decision)
    if not resolved:
        raise HTTPException(status_code=404, detail="未找到待确认的权限请求")
    return PermissionResolutionResponse(status="resolved")


# --- SSE Stream ---


async def _stream_response(
    engine,
    session_id: str,
    content: str,
    request: Request,
    *,
    turn_context: str = "",
    source_records: list[SourceReferenceRecord] | None = None,
    linked_issue=None,
    runtime_mode: str = "default",
):
    queue: asyncio.Queue[StreamEvent | None] = asyncio.Queue()
    store = getattr(request.app.state, "chat_run_store", None)
    run = (
        await store.start_run(
            session_id=session_id,
            user_message_id=f"msg-{uuid.uuid4().hex[:12]}",
        )
        if store is not None
        else None
    )
    step_sequences: dict[str, int] = {}
    if run is not None:
        for source in source_records or []:
            await store.append_artifact(
                run.id,
                kind="source",
                title=source.title,
                summary={"path": source.path},
                status="ready",
                artifact_id=f"{run.id}-{source.id}",
                metadata={"source_id": source.id},
            )
        if linked_issue is not None:
            await store.append_artifact(
                run.id,
                kind="task",
                title=linked_issue.task_id,
                summary={
                    "task_id": linked_issue.task_id,
                    "mission_id": linked_issue.mission_id,
                    "risk_level": linked_issue.risk_level.value
                    if hasattr(linked_issue.risk_level, "value")
                    else str(linked_issue.risk_level),
                },
                status="linked",
                artifact_id=f"{run.id}-issue-{linked_issue.task_id}",
            )

    async def on_event(event: str, data: dict[str, Any]) -> None:
        stream_event = _engine_event_to_stream_event(event, data, session_id=session_id)
        if run is not None:
            stream_event = _stream_event_with_run_id(stream_event, run.id)
            await _persist_stream_event(store, run.id, stream_event, step_sequences)
        await queue.put(stream_event)

    async def run_agent() -> None:
        try:
            async with _engine_lock(request):
                if not await engine.load_session(session_id):
                    raise RuntimeError("Session not found")
                previous_runtime_mode = engine.runtime_mode
                engine.set_runtime_mode(runtime_mode)
                try:
                    result = await engine.run_streaming(
                        content,
                        on_event,
                        turn_context=turn_context,
                    )
                finally:
                    engine.set_runtime_mode(previous_runtime_mode)
                terminal_event = StreamEvent(
                    type=EventType.AGENT_END,
                    data={
                        "status": result.status,
                        "turns": result.usage.turns,
                        "cost_usd": result.usage.total_cost_usd,
                    },
                    session_id=session_id,
                )
                if run is not None:
                    terminal_event = _stream_event_with_run_id(terminal_event, run.id)
                    await _persist_stream_event(
                        store, run.id, terminal_event, step_sequences
                    )
                    await store.finish_run(run.id, status=result.status)
                await queue.put(terminal_event)
        except asyncio.CancelledError:
            if run is not None:
                await store.finish_run(run.id, status="cancelled")
            raise
        except Exception as exc:
            error_event = StreamEvent(
                type=EventType.AGENT_ERROR,
                data={"message": str(exc) or "本次对话未能完成。"},
                session_id=session_id,
            )
            if run is not None:
                error_event = _stream_event_with_run_id(error_event, run.id)
                await _persist_stream_event(store, run.id, error_event, step_sequences)
                await store.finish_run(run.id, status="failed")
            await queue.put(error_event)
        finally:
            await queue.put(None)

    agent_task = asyncio.create_task(run_agent())
    if run is not None:
        _active_chat_run_tasks(request)[run.id] = (session_id, agent_task)
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
        if not agent_task.done():
            agent_task.cancel()
        try:
            await agent_task
        except (Exception, asyncio.CancelledError):
            pass
        if run is not None:
            _active_chat_run_tasks(request).pop(run.id, None)


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


def _chat_run_store(request: Request) -> ChatRunStore:
    store = getattr(request.app.state, "chat_run_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Chat run store unavailable")
    return store


def _active_chat_run_tasks(
    request: Request,
) -> dict[str, tuple[str, asyncio.Task[None]]]:
    active = getattr(request.app.state, "active_chat_run_tasks", None)
    if active is None:
        active = {}
        request.app.state.active_chat_run_tasks = active
    return active


async def _build_turn_context(
    *,
    engine,
    store: ChatRunStore,
    session_id: str,
    source_ids: list[str],
    linked_issue_id: str | None = None,
) -> tuple[str, list[SourceReferenceRecord], object | None]:
    by_id = {source.id: source for source in await store.list_sources(session_id)}
    sources: list[SourceReferenceRecord] = []
    sections = ["<naumi_turn_context>"]
    workspace_root = Path(engine.workspace_root).expanduser().resolve()
    if source_ids:
        sections.append("## 用户选择的本轮来源")
    for source_id in source_ids:
        source = by_id.get(source_id)
        if source is None:
            raise HTTPException(status_code=400, detail="来源不存在或不属于当前会话")
        path = (workspace_root / source.path).resolve()
        try:
            path.relative_to(workspace_root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="来源路径越界") from exc
        if not path.is_file():
            raise HTTPException(status_code=400, detail="来源文件已不存在")
        content = path.read_text(encoding="utf-8", errors="replace")[:20000]
        sections.extend(
            [
                f"### {source.title}",
                f"路径：{source.path}",
                "```text",
                content,
                "```",
            ]
        )
        sources.append(source)
    linked_issue = None
    if linked_issue_id:
        linked_issue = await engine.workbench_store.get_issue(
            session_id, linked_issue_id
        )
        if linked_issue is None:
            raise HTTPException(status_code=400, detail="关联 Issue 不存在")
        sections.extend(
            [
                "## 用户关联的现有 Issue",
                f"- task_id: {linked_issue.task_id}",
                f"- mission_id: {linked_issue.mission_id}",
                f"- risk_level: {linked_issue.risk_level}",
                f"- acceptance_criteria: {linked_issue.acceptance_criteria}",
            ]
        )
    sections.append("<naumi_turn_context>")
    return "\n".join(sections), sources, linked_issue


def _chat_run_to_response(run: ChatRunRecord) -> ChatRunResponse:
    return ChatRunResponse(
        id=run.id,
        session_id=run.session_id,
        user_message_id=run.user_message_id,
        assistant_message_id=run.assistant_message_id,
        status=run.status,
        started_at=run.started_at,
        updated_at=run.updated_at,
        completed_at=run.completed_at,
        steps=[
            ChatRunStepResponse(
                sequence=step.sequence,
                stage=step.stage,
                status=step.status,
                summary=step.summary,
                detail=step.detail,
                event_id=step.event_id,
                started_at=step.started_at,
                completed_at=step.completed_at,
                metadata=step.metadata,
            )
            for step in run.steps
        ],
        artifacts=[
            ChatArtifactResponse(
                id=artifact.id,
                kind=artifact.kind,
                title=artifact.title,
                summary=artifact.summary,
                status=artifact.status,
                created_at=artifact.created_at,
                metadata=artifact.metadata,
            )
            for artifact in run.artifacts
        ],
    )


def _stream_event_with_run_id(event: StreamEvent, run_id: str) -> StreamEvent:
    return StreamEvent(
        id=event.id,
        type=event.type,
        data={**event.data, "run_id": run_id},
        timestamp=event.timestamp,
        session_id=event.session_id,
        turn=event.turn,
    )


async def _persist_stream_event(
    store: ChatRunStore,
    run_id: str,
    event: StreamEvent,
    step_sequences: dict[str, int],
) -> None:
    step_key, stage, status, summary, detail = _stream_step_fields(event)
    if step_key is not None:
        sequence = step_sequences.setdefault(step_key, len(step_sequences) + 1)
        await store.append_step(
            run_id,
            sequence=sequence,
            stage=stage,
            status=status,
            summary=summary,
            detail=detail,
            event_id=event.id,
        )

    if event.type == EventType.TOOL_CALL_END and event.data.get("name") == "delegate_task":
        content = str(event.data.get("content") or "")
        if content:
            call_id = str(event.data.get("call_id") or event.id)
            await store.append_artifact(
                run_id,
                artifact_id=f"subagent-{call_id}",
                kind="subagent",
                title="delegate_task",
                summary={"text": _compact_public_text(content)},
                status=str(event.data.get("status") or "success"),
            )


def _stream_step_fields(
    event: StreamEvent,
) -> tuple[str | None, str, str, str, str]:
    if event.type in {
        EventType.TURN_START,
        EventType.THINKING_START,
        EventType.THINKING_DELTA,
        EventType.THINKING_END,
    }:
        return "analysis", "analysis", "running", "分析请求", ""
    if event.type in {
        EventType.TOOL_CALL_START,
        EventType.TOOL_CALL_END,
        EventType.TOOL_CALL_ERROR,
        EventType.PERMISSION_REQUEST,
    }:
        name = str(event.data.get("tool_name") or event.data.get("name") or "tool")
        call_id = str(event.data.get("call_id") or event.id)
        if event.type == EventType.PERMISSION_REQUEST:
            permission_status = str(event.data.get("status") or "")
            if permission_status == "needs_confirmation":
                return (
                    f"tool:{call_id}",
                    "approval",
                    "awaiting_approval",
                    name,
                    str(event.data.get("reason") or ""),
                )
            if permission_status in {"denied", "confirmation_error"}:
                return f"tool:{call_id}", "approval", "failed", name, ""
            return f"tool:{call_id}", "tool", "running", name, ""
        if event.type == EventType.TOOL_CALL_END:
            detail = (
                _compact_public_text(str(event.data.get("content") or ""))
                if name == "delegate_task"
                else ""
            )
            return f"tool:{call_id}", "tool", "completed", name, detail
        if event.type == EventType.TOOL_CALL_ERROR:
            return f"tool:{call_id}", "tool", "failed", name, ""
        return f"tool:{call_id}", "tool", "running", name, ""
    if event.type in {EventType.AGENT_START, EventType.TOKEN_DELTA}:
        return "response", "response", "running", "生成答复", ""
    if event.type == EventType.AGENT_END:
        status = str(event.data.get("status") or "completed")
        return "response", "response", status, "生成答复", ""
    if event.type == EventType.AGENT_ERROR:
        return (
            "response",
            "response",
            "failed",
            "生成答复",
            str(event.data.get("message") or ""),
        )
    return None, "", "", "", ""


def _compact_public_text(content: str, maximum_length: int = 420) -> str:
    normalized = content.strip()
    if len(normalized) <= maximum_length:
        return normalized
    return f"{normalized[:maximum_length]}..."


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
    if event == "thinking_delta":
        return StreamEvent(
            type=EventType.THINKING_DELTA,
            data={},
            session_id=session_id,
        )
    if event == "thinking_end":
        return StreamEvent(
            type=EventType.THINKING_END,
            data={},
            session_id=session_id,
        )
    if event == "permission_bubble":
        safe_fields = (
            "agent_name",
            "tool_name",
            "call_id",
            "status",
            "reason",
            "risk_level",
            "requires_confirmation",
        )
        return StreamEvent(
            type=EventType.PERMISSION_REQUEST,
            data={field: data[field] for field in safe_fields if field in data},
            session_id=session_id,
        )
    if event == "tool_start":
        call_id = data.get("call_id") or data.get("tool_call_id")
        safe_data = {"name": str(data.get("name") or "tool")}
        if call_id:
            safe_data["call_id"] = str(call_id)
        return StreamEvent(
            type=EventType.TOOL_CALL_START,
            data=safe_data,
            session_id=session_id,
        )

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
