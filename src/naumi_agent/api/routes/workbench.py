"""Workbench routes for the local Mac app."""

from __future__ import annotations

import logging
import os
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from naumi_agent import __version__
from naumi_agent.api.deps import AuthDep, extract_api_key_from_connection
from naumi_agent.api.schemas import SessionListResponse
from naumi_agent.workbench.market import TaskMarket
from naumi_agent.workbench.models import ApprovalState, DecisionKind, ParallelMode, RiskLevel

router = APIRouter(tags=["workbench"])
LOCAL_DAEMON_BIND_HOST = "127.0.0.1"
logger = logging.getLogger(__name__)


class DaemonStatusResponse(BaseModel):
    status: str
    version: str
    pid: int
    host: str
    port: int
    started_at: str
    workspace_count: int


class WorkbenchCapabilitiesResponse(BaseModel):
    supports_daemon_management: bool
    supports_workspace_registry: bool
    supports_validation_runner: bool
    supports_cloud_sync: bool
    supported_locales: list[str]
    protocol_version: int


class WorkbenchBootstrapResponse(BaseModel):
    daemon_status: DaemonStatusResponse
    capabilities: WorkbenchCapabilitiesResponse
    sessions: list[dict[str, Any]]
    total_sessions: int
    selected_session_id: str | None
    snapshot: dict[str, Any] | None


class WorkbenchSessionCreate(BaseModel):
    title: str = "Mac 工作台"
    model: str | None = None
    system_prompt: str | None = None


class MissionCreate(BaseModel):
    title: str
    goal: str


class IssueAttach(BaseModel):
    task_id: str | None = None
    title: str | None = None
    description: str = ""
    blocked_by: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    parallel_mode: ParallelMode = ParallelMode.EXCLUSIVE
    risk_level: RiskLevel = RiskLevel.MEDIUM


class AgentProfileUpsert(BaseModel):
    name: str
    role: str
    capabilities: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    max_parallel_tasks: int = Field(default=1, ge=1)
    status: str = "idle"
    actor: str = "Human"


class IntentLockCreate(BaseModel):
    actor: str = "Human"
    rule: str
    blocked_paths: list[str] = Field(default_factory=list)
    allowed_paths: list[str] = Field(default_factory=list)
    require_proposal_for_risk: RiskLevel = RiskLevel.HIGH


class DecisionCreate(BaseModel):
    actor: str = "Human"
    kind: DecisionKind = DecisionKind.ARCHITECTURE
    title: str
    content: str


class ApprovalResolve(BaseModel):
    actor: str = "Human"
    state: ApprovalState
    decision_note: str = ""


class ClaimIssue(BaseModel):
    agent_id: str
    duration_minutes: int = Field(default=45, ge=1)
    worktree_name: str = ""


class WorktreeKeep(BaseModel):
    actor: str = "Human"
    reason: str = ""


class ValidationRunCreate(BaseModel):
    task_id: str
    actor: str = "Human"
    argv: list[str] = Field(min_length=1)
    cwd: str | None = None


class ValidationRunResultResponse(BaseModel):
    id: str
    status: str
    exit_code: int
    output: str


class WorkbenchEventsResponse(BaseModel):
    events: list[dict[str, Any]]
    event_type: str | None
    subject_id: str | None
    actor: str | None
    since: str | None
    limit: int


class ValidationRunsResponse(BaseModel):
    validation_runs: list[dict[str, Any]]
    task_id: str | None
    limit: int


class ContextSnapshotsResponse(BaseModel):
    context_snapshots: list[dict[str, Any]]
    task_id: str | None
    agent_id: str | None
    limit: int


class ContextHealthRecord(BaseModel):
    agent_id: str
    minutes_since_sync: int = Field(ge=0)
    token_load_ratio: float = Field(ge=0)
    policy_conflict: bool = False
    actor: str = "Human"


class ApprovalsResponse(BaseModel):
    approvals: list[dict[str, Any]]
    state: str | None
    limit: int


class FailuresResponse(BaseModel):
    failures: list[dict[str, Any]]
    task_id: str | None
    status: str | None
    limit: int


class IssuesResponse(BaseModel):
    issues: list[dict[str, Any]]
    mission_id: str | None
    risk_level: str | None
    limit: int


class LeasesResponse(BaseModel):
    leases: list[dict[str, Any]]
    state: str | None
    task_id: str | None
    agent_id: str | None
    limit: int


class WorktreesResponse(BaseModel):
    worktrees: list[dict[str, Any]]
    task_id: str | None
    status: str | None
    limit: int


class WorktreeRemovalResponse(BaseModel):
    name: str
    discard_changes: bool
    message: str


class WorktreeRemovalSnapshotResponse(BaseModel):
    removal: WorktreeRemovalResponse
    snapshot: dict[str, Any]


class MissionsResponse(BaseModel):
    missions: list[dict[str, Any]]
    status: str | None
    limit: int


class AgentProfilesResponse(BaseModel):
    agent_profiles: list[dict[str, Any]]
    status: str | None
    limit: int


class IntentLocksResponse(BaseModel):
    intent_locks: list[dict[str, Any]]
    mission_id: str


class DecisionsResponse(BaseModel):
    decisions: list[dict[str, Any]]
    mission_id: str


def _get_task_market(engine) -> TaskMarket:
    market = getattr(engine, "workbench_market", None)
    if market is not None:
        return market
    return TaskMarket(
        task_store=engine.task_store,
        workbench_store=engine.workbench_store,
    )


def _worktree_to_dict(record) -> dict[str, Any]:
    data = asdict(record)
    status = data.get("status")
    if hasattr(status, "value"):
        data["status"] = status.value
    data["removable"] = record.removable
    return data


async def _build_workbench_snapshot(engine, session_id: str) -> dict[str, Any]:
    snapshot = await engine.workbench_service.dashboard_snapshot(session_id)
    manager = getattr(engine, "worktree_manager", None)
    if manager is None:
        return {**snapshot, "worktrees": []}

    records = await manager.status()
    if not isinstance(records, list):
        records = [records]
    return {
        **snapshot,
        "worktrees": [_worktree_to_dict(record) for record in records],
    }


async def _count_workspaces(engine) -> int:
    session_store = getattr(engine, "session_store", None)
    if session_store is None:
        return 0
    try:
        _, total = await session_store.list_sessions(page=1, page_size=1)
        return int(total)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return 0


def _session_to_bootstrap_dict(session) -> dict[str, Any]:
    created_at = getattr(session, "created_at", "")
    updated_at = getattr(session, "updated_at", "")
    if hasattr(created_at, "isoformat"):
        created_at = created_at.isoformat()
    if hasattr(updated_at, "isoformat"):
        updated_at = updated_at.isoformat()

    messages = getattr(session, "messages", [])
    return {
        "id": getattr(session, "id", ""),
        "title": getattr(session, "title", None),
        "model": getattr(session, "model", ""),
        "created_at": created_at,
        "updated_at": updated_at,
        "message_count": len(messages),
        "total_tokens": getattr(session, "total_tokens", 0),
        "total_cost_usd": getattr(session, "total_cost_usd", 0.0),
        "status": getattr(session, "status", "active"),
    }


async def _build_daemon_status(request: Request) -> DaemonStatusResponse:
    engine = request.app.state.engine
    started_at = getattr(request.app.state, "started_at", None)
    if started_at is None:
        started_at = datetime.now(UTC).replace(microsecond=0).isoformat()
        request.app.state.started_at = started_at
    return DaemonStatusResponse(
        status="running",
        version=__version__,
        pid=os.getpid(),
        host=LOCAL_DAEMON_BIND_HOST,
        port=request.url.port or 8765,
        started_at=started_at,
        workspace_count=await _count_workspaces(engine),
    )


def _build_capabilities() -> WorkbenchCapabilitiesResponse:
    return WorkbenchCapabilitiesResponse(
        supports_daemon_management=False,
        supports_workspace_registry=False,
        supports_validation_runner=True,
        supports_cloud_sync=False,
        supported_locales=["zh-CN", "en-US"],
        protocol_version=1,
    )


def _is_workbench_websocket_api_key_valid(websocket: WebSocket) -> bool:
    config = getattr(websocket.app.state, "config", None)
    api_keys = getattr(getattr(config, "api", None), "api_keys", [])
    if not api_keys:
        return True

    api_key = extract_api_key_from_connection(websocket)
    return bool(api_key and api_key in api_keys)


@router.get("/workbench/daemon/status", response_model=DaemonStatusResponse)
async def get_daemon_status(request: Request, auth: str = AuthDep):
    return await _build_daemon_status(request)


@router.get("/workbench/capabilities", response_model=WorkbenchCapabilitiesResponse)
async def get_workbench_capabilities(request: Request, auth: str = AuthDep):
    return _build_capabilities()


@router.get("/workbench/sessions", response_model=SessionListResponse)
async def list_workbench_sessions(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    try:
        sessions, total = await engine.session_store.list_sessions(
            page=page,
            page_size=page_size,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return SessionListResponse(
        sessions=[_session_to_bootstrap_dict(session) for session in sessions],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post(
    "/workbench/sessions",
    response_model=WorkbenchBootstrapResponse,
    status_code=201,
)
async def create_workbench_session(
    body: WorkbenchSessionCreate,
    request: Request,
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    title = body.title.strip() or "Mac 工作台"
    try:
        session = await engine.session_store.create_session(
            title=title,
            model=body.model,
            system_prompt=body.system_prompt,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    session_id = getattr(session, "id", "")
    if not session_id or not await engine.load_session(session_id):
        raise HTTPException(status_code=503, detail="会话创建后无法加载")

    try:
        snapshot = await _build_workbench_snapshot(engine, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return WorkbenchBootstrapResponse(
        daemon_status=await _build_daemon_status(request),
        capabilities=_build_capabilities(),
        sessions=[_session_to_bootstrap_dict(session)],
        total_sessions=await _count_workspaces(engine),
        selected_session_id=session_id,
        snapshot=snapshot,
    )


@router.get("/workbench/bootstrap", response_model=WorkbenchBootstrapResponse)
async def get_workbench_bootstrap(
    request: Request,
    page_size: Annotated[int, Query(ge=1, le=20)] = 1,
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    try:
        sessions, total = await engine.session_store.list_sessions(
            page=1,
            page_size=page_size,
        )
    except RuntimeError:
        logger.exception("Workbench bootstrap could not read session registry")
        sessions = []
        total = 0
    session_dicts = [_session_to_bootstrap_dict(session) for session in sessions]
    selected_session_id = None
    snapshot = None

    for session_dict in session_dicts:
        candidate_session_id = session_dict["id"]
        if not await engine.load_session(candidate_session_id):
            continue
        try:
            snapshot = await _build_workbench_snapshot(engine, candidate_session_id)
        except (RuntimeError, ValueError):
            logger.exception(
                "Workbench bootstrap snapshot failed for session %s",
                candidate_session_id,
            )
        else:
            selected_session_id = candidate_session_id
            break

    return WorkbenchBootstrapResponse(
        daemon_status=await _build_daemon_status(request),
        capabilities=_build_capabilities(),
        sessions=session_dicts,
        total_sessions=total,
        selected_session_id=selected_session_id,
        snapshot=snapshot,
    )


@router.get("/workbench/sessions/{session_id}/snapshot")
async def get_workbench_snapshot(session_id: str, request: Request, auth: str = AuthDep):
    engine = request.app.state.engine
    try:
        session = await engine.session_store.load(session_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        return await _build_workbench_snapshot(engine, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get(
    "/workbench/sessions/{session_id}/events",
    response_model=WorkbenchEventsResponse,
)
async def get_workbench_events(
    session_id: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    event_type: Annotated[str | None, Query(alias="type")] = None,
    subject_id: Annotated[str | None, Query()] = None,
    actor: Annotated[str | None, Query()] = None,
    since: Annotated[str | None, Query()] = None,
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    try:
        session = await engine.session_store.load(session_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        result = await engine.workbench_service.list_events(
            session_id,
            event_type=event_type,
            subject_id=subject_id,
            actor=actor,
            since=since,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return WorkbenchEventsResponse(
        events=result["events"],
        event_type=result["event_type"],
        subject_id=result["subject_id"],
        actor=result["actor"],
        since=result["since"],
        limit=result["limit"],
    )


@router.get("/workbench/sessions/{session_id}/events/{event_id}")
async def get_workbench_event(
    session_id: str,
    event_id: str,
    request: Request,
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    try:
        session = await engine.session_store.load(session_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        event = await engine.workbench_service.get_event(session_id, event_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if event is None:
        raise HTTPException(status_code=404, detail="审计事件不存在")
    return event


@router.websocket("/workbench/sessions/{session_id}/events/stream")
async def websocket_workbench_events(websocket: WebSocket, session_id: str):
    await websocket.accept()

    if not _is_workbench_websocket_api_key_valid(websocket):
        await websocket.send_json({"type": "error", "message": "Invalid API key"})
        await websocket.close()
        return

    engine = websocket.app.state.engine

    try:
        session = await engine.session_store.load(session_id)
    except RuntimeError as exc:
        await websocket.send_json({"type": "error", "message": str(exc)})
        await websocket.close()
        return
    if not session:
        await websocket.send_json({"type": "error", "message": "Session not found"})
        await websocket.close()
        return
    try:
        session_loaded = await engine.load_session(session_id)
    except RuntimeError as exc:
        await websocket.send_json({"type": "error", "message": str(exc)})
        await websocket.close()
        return
    if not session_loaded:
        await websocket.send_json({"type": "error", "message": "Session not found"})
        await websocket.close()
        return

    await websocket.send_json({"type": "connected", "session_id": session_id})
    if _truthy_query_param(websocket, "include_snapshot"):
        await _send_workbench_snapshot(websocket, engine, session_id)
    await _send_workbench_event_refresh(
        websocket,
        engine,
        session_id,
        event_type=None,
        subject_id=None,
        actor=None,
        since=None,
        limit=50,
    )

    try:
        while True:
            data = await websocket.receive_json()
            message_type = data.get("type")
            if message_type == "ping":
                await websocket.send_json({"type": "pong"})
                continue
            if message_type != "refresh":
                await websocket.send_json(
                    {
                        "type": "error",
                        "message": f"Unsupported workbench event stream message: {message_type}",
                    }
                )
                continue

            limit = _bounded_event_stream_limit(data.get("limit", 50))
            await _send_workbench_event_refresh(
                websocket,
                engine,
                session_id,
                event_type=data.get("event_type"),
                subject_id=data.get("subject_id"),
                actor=data.get("actor"),
                since=data.get("since"),
                limit=limit,
            )
    except WebSocketDisconnect:
        pass


def _truthy_query_param(websocket: WebSocket, key: str) -> bool:
    value = websocket.query_params.get(key)
    if value is None:
        return False
    return value.lower() in {"1", "true", "yes", "on"}


async def _send_workbench_snapshot(
    websocket: WebSocket,
    engine: Any,
    session_id: str,
) -> None:
    try:
        snapshot = await _build_workbench_snapshot(engine, session_id)
    except (RuntimeError, ValueError) as exc:
        logger.warning(
            "Workbench event stream snapshot failed for session %s",
            session_id,
            exc_info=True,
        )
        await websocket.send_json({"type": "error", "message": str(exc)})
        return

    await websocket.send_json(
        {
            "type": "workbench/snapshot",
            "version": 1,
            "payload": snapshot,
        }
    )


async def _send_workbench_event_refresh(
    websocket: WebSocket,
    engine: Any,
    session_id: str,
    *,
    event_type: str | None,
    subject_id: str | None,
    actor: str | None,
    since: str | None,
    limit: int,
) -> None:
    try:
        result = await engine.workbench_service.list_events(
            session_id,
            event_type=event_type,
            subject_id=subject_id,
            actor=actor,
            since=since,
            limit=limit,
        )
    except (RuntimeError, ValueError) as exc:
        logger.warning(
            "Workbench event stream refresh failed for session %s",
            session_id,
            exc_info=True,
        )
        await websocket.send_json({"type": "error", "message": str(exc)})
        return

    events = result["events"]
    for event in events:
        await websocket.send_json({"type": "workbench.event", "event": event})
    await websocket.send_json({"type": "refresh_complete", "count": len(events)})


def _bounded_event_stream_limit(value: Any) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return 50
    return max(1, min(limit, 200))


@router.get(
    "/workbench/sessions/{session_id}/validation-runs",
    response_model=ValidationRunsResponse,
)
async def get_validation_runs(
    session_id: str,
    request: Request,
    task_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    try:
        session = await engine.session_store.load(session_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        runs = await engine.workbench_service.list_validation_runs(
            session_id, task_id=task_id, limit=limit
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return ValidationRunsResponse(validation_runs=runs, task_id=task_id, limit=limit)


@router.get("/workbench/sessions/{session_id}/validation-runs/{run_id}")
async def get_validation_run(
    session_id: str,
    run_id: str,
    request: Request,
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    try:
        session = await engine.session_store.load(session_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        run = await engine.workbench_service.get_validation_run(session_id, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if run is None:
        raise HTTPException(status_code=404, detail="验证运行不存在")
    return run


@router.get(
    "/workbench/sessions/{session_id}/context-snapshots",
    response_model=ContextSnapshotsResponse,
)
async def get_context_snapshots(
    session_id: str,
    request: Request,
    task_id: str | None = Query(default=None),
    agent_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    try:
        session = await engine.session_store.load(session_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        snapshots = await engine.workbench_service.list_context_snapshots(
            session_id, task_id=task_id, agent_id=agent_id, limit=limit
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return ContextSnapshotsResponse(
        context_snapshots=snapshots, task_id=task_id, agent_id=agent_id, limit=limit
    )


@router.get("/workbench/sessions/{session_id}/context-snapshots/{snapshot_id}")
async def get_context_snapshot(
    session_id: str,
    snapshot_id: str,
    request: Request,
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    try:
        session = await engine.session_store.load(session_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        snapshot = await engine.workbench_service.get_context_snapshot(
            session_id,
            snapshot_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if snapshot is None:
        raise HTTPException(status_code=404, detail="上下文快照不存在")
    return snapshot


@router.post(
    "/workbench/sessions/{session_id}/issues/{task_id}/context-health",
    status_code=201,
)
async def create_context_health_snapshot(
    session_id: str,
    task_id: str,
    body: ContextHealthRecord,
    request: Request,
    include_snapshot: Annotated[bool, Query()] = False,
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    try:
        session = await engine.session_store.load(session_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        context_snapshot = await engine.workbench_service.record_context_health(
            session_id=session_id,
            task_id=task_id,
            agent_id=body.agent_id,
            minutes_since_sync=body.minutes_since_sync,
            token_load_ratio=body.token_load_ratio,
            policy_conflict=body.policy_conflict,
            actor=body.actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not include_snapshot:
        return context_snapshot

    try:
        snapshot = await _build_workbench_snapshot(engine, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"context_snapshot": context_snapshot, "snapshot": snapshot}


@router.get(
    "/workbench/sessions/{session_id}/failures",
    response_model=FailuresResponse,
)
async def get_failures(
    session_id: str,
    request: Request,
    task_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    try:
        session = await engine.session_store.load(session_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        failures = await engine.workbench_service.list_failures(
            session_id, task_id=task_id, status=status, limit=limit
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return FailuresResponse(
        failures=failures, task_id=task_id, status=status, limit=limit
    )


@router.get("/workbench/sessions/{session_id}/failures/{failure_id}")
async def get_failure(
    session_id: str,
    failure_id: str,
    request: Request,
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    try:
        session = await engine.session_store.load(session_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        failure = await engine.workbench_service.get_failure(session_id, failure_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if failure is None:
        raise HTTPException(status_code=404, detail="失败卡片不存在")
    return failure


@router.get(
    "/workbench/sessions/{session_id}/missions",
    response_model=MissionsResponse,
)
async def get_missions(
    session_id: str,
    request: Request,
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    try:
        session = await engine.session_store.load(session_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        missions = await engine.workbench_service.list_missions(
            session_id, status=status, limit=limit
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return MissionsResponse(
        missions=missions["missions"], status=status, limit=limit
    )


@router.get("/workbench/sessions/{session_id}/missions/{mission_id}")
async def get_mission(
    session_id: str,
    mission_id: str,
    request: Request,
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        mission = await engine.workbench_service.get_mission(session_id, mission_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if mission is None:
        raise HTTPException(status_code=404, detail="mission 不存在")
    return mission


@router.post("/workbench/sessions/{session_id}/missions", status_code=201)
async def create_workbench_mission(
    session_id: str,
    body: MissionCreate,
    request: Request,
    include_snapshot: Annotated[bool, Query()] = False,
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        mission = await engine.workbench_service.create_mission(
            session_id=session_id,
            title=body.title,
            goal=body.goal,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    mission_payload = asdict(mission)
    if not include_snapshot:
        return mission_payload

    try:
        snapshot = await _build_workbench_snapshot(engine, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"mission": mission_payload, "snapshot": snapshot}


@router.post(
    "/workbench/sessions/{session_id}/missions/{mission_id}/issues",
    status_code=201,
)
async def attach_workbench_issue(
    session_id: str,
    mission_id: str,
    body: IssueAttach,
    request: Request,
    include_snapshot: Annotated[bool, Query()] = False,
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        if body.task_id:
            issue = await engine.workbench_service.attach_issue(
                session_id=session_id,
                mission_id=mission_id,
                task_id=body.task_id,
                acceptance_criteria=body.acceptance_criteria,
                parallel_mode=body.parallel_mode,
                risk_level=body.risk_level,
            )
        else:
            issue = await engine.workbench_service.create_issue(
                session_id=session_id,
                mission_id=mission_id,
                title=body.title or "",
                description=body.description,
                blocked_by=body.blocked_by,
                acceptance_criteria=body.acceptance_criteria,
                parallel_mode=body.parallel_mode,
                risk_level=body.risk_level,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not include_snapshot:
        return issue

    try:
        snapshot = await _build_workbench_snapshot(engine, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"issue": issue, "snapshot": snapshot}


@router.post(
    "/workbench/sessions/{session_id}/missions/{mission_id}/intent-locks",
    status_code=201,
)
async def create_intent_lock(
    session_id: str,
    mission_id: str,
    body: IntentLockCreate,
    request: Request,
    include_snapshot: Annotated[bool, Query()] = False,
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        lock = await engine.workbench_service.create_intent_lock(
            session_id=session_id,
            mission_id=mission_id,
            actor=body.actor,
            rule=body.rule,
            blocked_paths=body.blocked_paths,
            allowed_paths=body.allowed_paths,
            require_proposal_for_risk=body.require_proposal_for_risk,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not include_snapshot:
        return lock

    try:
        snapshot = await _build_workbench_snapshot(engine, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"intent_lock": lock, "snapshot": snapshot}


@router.post(
    "/workbench/sessions/{session_id}/missions/{mission_id}/decisions",
    status_code=201,
)
async def create_decision(
    session_id: str,
    mission_id: str,
    body: DecisionCreate,
    request: Request,
    include_snapshot: Annotated[bool, Query()] = False,
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        decision = await engine.workbench_service.create_decision(
            session_id=session_id,
            mission_id=mission_id,
            actor=body.actor,
            kind=body.kind,
            title=body.title,
            content=body.content,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not include_snapshot:
        return decision

    try:
        snapshot = await _build_workbench_snapshot(engine, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"decision": decision, "snapshot": snapshot}


@router.get(
    "/workbench/sessions/{session_id}/missions/{mission_id}/intent-locks",
    response_model=IntentLocksResponse,
)
async def get_intent_locks(
    session_id: str,
    mission_id: str,
    request: Request,
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        locks = await engine.workbench_service.list_intent_locks(session_id, mission_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return IntentLocksResponse(intent_locks=locks, mission_id=mission_id)


@router.get("/workbench/sessions/{session_id}/missions/{mission_id}/intent-locks/{lock_id}")
async def get_intent_lock(
    session_id: str,
    mission_id: str,
    lock_id: str,
    request: Request,
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        lock = await engine.workbench_service.get_intent_lock(
            session_id, mission_id, lock_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if lock is None:
        raise HTTPException(status_code=404, detail="意图锁不存在")
    return lock


@router.get(
    "/workbench/sessions/{session_id}/missions/{mission_id}/decisions",
    response_model=DecisionsResponse,
)
async def get_decisions(
    session_id: str,
    mission_id: str,
    request: Request,
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        decisions = await engine.workbench_service.list_decisions(session_id, mission_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return DecisionsResponse(decisions=decisions, mission_id=mission_id)


@router.get("/workbench/sessions/{session_id}/missions/{mission_id}/decisions/{decision_id}")
async def get_decision(
    session_id: str,
    mission_id: str,
    decision_id: str,
    request: Request,
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        decision = await engine.workbench_service.get_decision(
            session_id, mission_id, decision_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if decision is None:
        raise HTTPException(status_code=404, detail="决策不存在")
    return decision


@router.post("/workbench/sessions/{session_id}/issues/{task_id}/claim", status_code=201)
async def claim_workbench_issue(
    session_id: str,
    task_id: str,
    body: ClaimIssue,
    request: Request,
    include_snapshot: Annotated[bool, Query()] = False,
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    market = _get_task_market(engine)
    try:
        lease = await market.claim(
            task_id=task_id,
            agent_id=body.agent_id,
            duration_minutes=body.duration_minutes,
            worktree_name=body.worktree_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    lease_payload = asdict(lease)
    if not include_snapshot:
        return lease_payload

    try:
        snapshot = await _build_workbench_snapshot(engine, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"lease": lease_payload, "snapshot": snapshot}


@router.post("/workbench/sessions/{session_id}/leases/{lease_id}/release")
async def release_workbench_lease(
    session_id: str,
    lease_id: str,
    request: Request,
    include_snapshot: Annotated[bool, Query()] = False,
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    market = _get_task_market(engine)
    try:
        lease = await market.release(session_id, lease_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if lease is None:
        raise HTTPException(status_code=404, detail="租约不存在")
    lease_payload = asdict(lease)
    if not include_snapshot:
        return lease_payload

    try:
        snapshot = await _build_workbench_snapshot(engine, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"lease": lease_payload, "snapshot": snapshot}


@router.post("/workbench/sessions/{session_id}/leases/expire")
async def expire_workbench_leases(
    session_id: str,
    request: Request,
    include_snapshot: Annotated[bool, Query()] = False,
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    market = _get_task_market(engine)
    try:
        expired = await market.expire_overdue_leases(session_id=session_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    response = {"expired": [asdict(lease) for lease in expired]}
    if not include_snapshot:
        return response

    try:
        snapshot = await _build_workbench_snapshot(engine, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {**response, "snapshot": snapshot}


@router.get(
    "/workbench/sessions/{session_id}/issues",
    response_model=IssuesResponse,
)
async def get_issues(
    session_id: str,
    request: Request,
    mission_id: str | None = Query(default=None),
    risk_level: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    try:
        session = await engine.session_store.load(session_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        issues = await engine.workbench_service.list_issues(
            session_id, mission_id=mission_id, risk_level=risk_level, limit=limit
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return IssuesResponse(
        issues=issues["issues"],
        mission_id=mission_id,
        risk_level=risk_level,
        limit=limit,
    )


@router.get("/workbench/sessions/{session_id}/issues/{task_id}")
async def get_issue(
    session_id: str,
    task_id: str,
    request: Request,
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    try:
        session = await engine.session_store.load(session_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        issue = await engine.workbench_service.get_issue(session_id, task_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if issue is None:
        raise HTTPException(status_code=404, detail="issue 不存在")
    return issue


@router.get(
    "/workbench/sessions/{session_id}/agents",
    response_model=AgentProfilesResponse,
)
async def get_agent_profiles(
    session_id: str,
    request: Request,
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        result = await engine.workbench_service.list_agent_profiles(
            session_id, status=status, limit=limit
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return AgentProfilesResponse(
        agent_profiles=result["agent_profiles"],
        status=result["status"],
        limit=result["limit"],
    )


@router.get("/workbench/sessions/{session_id}/agents/{agent_id}")
async def get_agent_profile(
    session_id: str,
    agent_id: str,
    request: Request,
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        profile = await engine.workbench_service.get_agent_profile(session_id, agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if profile is None:
        raise HTTPException(status_code=404, detail="智能体不存在")
    return profile


@router.post("/workbench/sessions/{session_id}/agents/{agent_id}", status_code=201)
async def upsert_agent_profile(
    session_id: str,
    agent_id: str,
    body: AgentProfileUpsert,
    request: Request,
    include_snapshot: Annotated[bool, Query()] = False,
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        profile = await engine.workbench_service.register_agent_profile(
            session_id=session_id,
            agent_id=agent_id,
            name=body.name,
            role=body.role,
            capabilities=body.capabilities,
            permissions=body.permissions,
            max_parallel_tasks=body.max_parallel_tasks,
            status=body.status,
            actor=body.actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not include_snapshot:
        return profile

    try:
        snapshot = await _build_workbench_snapshot(engine, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"agent_profile": profile, "snapshot": snapshot}


@router.get(
    "/workbench/sessions/{session_id}/worktrees",
    response_model=WorktreesResponse,
)
async def get_worktrees(
    session_id: str,
    request: Request,
    task_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")

    manager = getattr(engine, "worktree_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="worktree 管理器未初始化")

    records = await manager.status()
    if not isinstance(records, list):
        records = [records]
    worktrees = [_worktree_to_dict(record) for record in records]
    if task_id is not None:
        worktrees = [record for record in worktrees if record["task_id"] == task_id]
    if status is not None:
        worktrees = [record for record in worktrees if record["status"] == status]

    return WorktreesResponse(
        worktrees=worktrees[:limit],
        task_id=task_id,
        status=status,
        limit=limit,
    )


@router.post("/workbench/sessions/{session_id}/worktrees/{name:path}/keep")
async def keep_worktree(
    session_id: str,
    name: str,
    body: WorktreeKeep,
    request: Request,
    include_snapshot: Annotated[bool, Query()] = False,
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")

    manager = getattr(engine, "worktree_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="worktree 管理器未初始化")

    reason = body.reason.strip()
    try:
        await manager.keep(name, reason=reason)
        record = await manager.status(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="worktree 不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(record, list):
        raise HTTPException(status_code=404, detail="worktree 不存在")

    store = getattr(engine, "workbench_store", None)
    if store is not None:
        await store.append_event(
            session_id=session_id,
            type="worktree.kept",
            actor=body.actor.strip() or "Human",
            subject_id=name,
            payload={"reason": reason},
        )
    worktree = _worktree_to_dict(record)
    if not include_snapshot:
        return worktree

    try:
        snapshot = await _build_workbench_snapshot(engine, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"worktree": worktree, "snapshot": snapshot}


@router.get("/workbench/sessions/{session_id}/worktrees/{name:path}")
async def get_worktree(
    session_id: str,
    name: str,
    request: Request,
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")

    manager = getattr(engine, "worktree_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="worktree 管理器未初始化")

    try:
        record = await manager.status(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="worktree 不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(record, list):
        raise HTTPException(status_code=404, detail="worktree 不存在")
    return _worktree_to_dict(record)


@router.delete(
    "/workbench/sessions/{session_id}/worktrees/{name:path}",
    response_model=WorktreeRemovalResponse | WorktreeRemovalSnapshotResponse,
)
async def delete_worktree(
    session_id: str,
    name: str,
    request: Request,
    discard_changes: bool = Query(default=False),
    include_snapshot: Annotated[bool, Query()] = False,
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")

    manager = getattr(engine, "worktree_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="worktree 管理器未初始化")

    try:
        message = await manager.remove(name, discard_changes=discard_changes)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="worktree 不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if message.startswith("拒绝删除："):
        raise HTTPException(status_code=409, detail=message)

    store = getattr(engine, "workbench_store", None)
    if store is not None:
        await store.append_event(
            session_id=session_id,
            type="worktree.removed",
            actor="Human",
            subject_id=name,
            payload={"discard_changes": discard_changes},
        )
    removal = WorktreeRemovalResponse(
        name=name,
        discard_changes=discard_changes,
        message=message,
    )
    if not include_snapshot:
        return removal

    try:
        snapshot = await _build_workbench_snapshot(engine, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return WorktreeRemovalSnapshotResponse(removal=removal, snapshot=snapshot)


@router.get(
    "/workbench/sessions/{session_id}/leases",
    response_model=LeasesResponse,
)
async def get_leases(
    session_id: str,
    request: Request,
    state: str | None = Query(default=None),
    task_id: str | None = Query(default=None),
    agent_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        leases = await engine.workbench_service.list_leases(
            session_id, state=state, task_id=task_id, agent_id=agent_id, limit=limit
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return LeasesResponse(
        leases=leases["leases"],
        state=state,
        task_id=task_id,
        agent_id=agent_id,
        limit=limit,
    )


@router.get("/workbench/sessions/{session_id}/leases/{lease_id}")
async def get_lease(
    session_id: str,
    lease_id: str,
    request: Request,
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        lease = await engine.workbench_service.get_lease(session_id, lease_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if lease is None:
        raise HTTPException(status_code=404, detail="租约不存在")
    return lease


@router.get(
    "/workbench/sessions/{session_id}/approvals",
    response_model=ApprovalsResponse,
)
async def get_approvals(
    session_id: str,
    request: Request,
    state: ApprovalState | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        approvals = await engine.workbench_service.list_approvals(
            session_id, state=state, limit=limit
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return ApprovalsResponse(
        approvals=approvals,
        state=state.value if state is not None else None,
        limit=limit,
    )


@router.get("/workbench/sessions/{session_id}/approvals/{approval_id}")
async def get_approval(
    session_id: str,
    approval_id: str,
    request: Request,
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        approval = await engine.workbench_service.get_approval(session_id, approval_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if approval is None:
        raise HTTPException(status_code=404, detail="审批请求不存在")
    return approval


@router.post("/workbench/sessions/{session_id}/approvals/{approval_id}/resolve")
async def resolve_approval(
    session_id: str,
    approval_id: str,
    body: ApprovalResolve,
    request: Request,
    include_snapshot: Annotated[bool, Query()] = False,
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        approval = await engine.workbench_service.resolve_approval(
            session_id=session_id,
            approval_id=approval_id,
            actor=body.actor,
            state=body.state,
            decision_note=body.decision_note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if approval is None:
        raise HTTPException(status_code=404, detail="审批请求不存在")
    if not include_snapshot:
        return approval

    try:
        snapshot = await _build_workbench_snapshot(engine, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"approval": approval, "snapshot": snapshot}


@router.post(
    "/workbench/sessions/{session_id}/validation-runs",
    status_code=201,
)
async def create_validation_run(
    session_id: str,
    body: ValidationRunCreate,
    request: Request,
    include_snapshot: Annotated[bool, Query()] = False,
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        result = await engine.workbench_service.run_validation(
            session_id=session_id,
            task_id=body.task_id,
            actor=body.actor,
            argv=body.argv,
            cwd=body.cwd,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    validation_run = ValidationRunResultResponse(
        id=result["id"],
        status=result["status"],
        exit_code=result["exit_code"],
        output=result["output"],
    )
    if not include_snapshot:
        return validation_run

    try:
        snapshot = await _build_workbench_snapshot(engine, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"validation_run": validation_run.model_dump(), "snapshot": snapshot}
