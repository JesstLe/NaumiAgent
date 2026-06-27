"""Workbench routes for the local Mac app."""

from __future__ import annotations

import os
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from naumi_agent import __version__
from naumi_agent.api.deps import AuthDep
from naumi_agent.workbench.market import TaskMarket
from naumi_agent.workbench.models import ApprovalState, DecisionKind, ParallelMode, RiskLevel

router = APIRouter(tags=["workbench"])


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


class MissionCreate(BaseModel):
    title: str
    goal: str


class IssueAttach(BaseModel):
    task_id: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    parallel_mode: ParallelMode = ParallelMode.EXCLUSIVE
    risk_level: RiskLevel = RiskLevel.MEDIUM


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


class MissionsResponse(BaseModel):
    missions: list[dict[str, Any]]
    status: str | None
    limit: int


def _get_task_market(engine) -> TaskMarket:
    market = getattr(engine, "workbench_market", None)
    if market is not None:
        return market
    return TaskMarket(
        task_store=engine.task_store,
        workbench_store=engine.workbench_store,
    )


async def _count_workspaces(engine) -> int:
    session_store = getattr(engine, "session_store", None)
    if session_store is None:
        return 0
    try:
        _, total = await session_store.list_sessions(page=1, page_size=1)
        return int(total)
    except (AttributeError, TypeError, ValueError):
        return 0


@router.get("/workbench/daemon/status", response_model=DaemonStatusResponse)
async def get_daemon_status(request: Request, auth: str = AuthDep):
    engine = request.app.state.engine
    started_at = getattr(request.app.state, "started_at", None)
    if started_at is None:
        started_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    return DaemonStatusResponse(
        status="running",
        version=__version__,
        pid=os.getpid(),
        host=request.url.hostname or "127.0.0.1",
        port=request.url.port or 8765,
        started_at=started_at,
        workspace_count=await _count_workspaces(engine),
    )


@router.get("/workbench/capabilities", response_model=WorkbenchCapabilitiesResponse)
async def get_workbench_capabilities(request: Request, auth: str = AuthDep):
    return WorkbenchCapabilitiesResponse(
        supports_daemon_management=False,
        supports_workspace_registry=False,
        supports_validation_runner=True,
        supports_cloud_sync=False,
        supported_locales=["zh-CN", "en-US"],
        protocol_version=1,
    )


@router.get("/workbench/sessions/{session_id}/snapshot")
async def get_workbench_snapshot(session_id: str, request: Request, auth: str = AuthDep):
    engine = request.app.state.engine
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return await engine.workbench_service.dashboard_snapshot(session_id)


@router.get(
    "/workbench/sessions/{session_id}/events",
    response_model=WorkbenchEventsResponse,
)
async def get_workbench_events(
    session_id: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    events = await engine.workbench_service.list_events(session_id, limit=limit)
    return WorkbenchEventsResponse(events=events, limit=limit)


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
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    runs = await engine.workbench_service.list_validation_runs(
        session_id, task_id=task_id, limit=limit
    )
    return ValidationRunsResponse(validation_runs=runs, task_id=task_id, limit=limit)


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
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    snapshots = await engine.workbench_service.list_context_snapshots(
        session_id, task_id=task_id, agent_id=agent_id, limit=limit
    )
    return ContextSnapshotsResponse(
        context_snapshots=snapshots, task_id=task_id, agent_id=agent_id, limit=limit
    )


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
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    failures = await engine.workbench_service.list_failures(
        session_id, task_id=task_id, status=status, limit=limit
    )
    return FailuresResponse(
        failures=failures, task_id=task_id, status=status, limit=limit
    )


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
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    missions = await engine.workbench_service.list_missions(
        session_id, status=status, limit=limit
    )
    return MissionsResponse(
        missions=missions["missions"], status=status, limit=limit
    )


@router.post("/workbench/sessions/{session_id}/missions", status_code=201)
async def create_workbench_mission(
    session_id: str,
    body: MissionCreate,
    request: Request,
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    mission = await engine.workbench_service.create_mission(
        session_id=session_id,
        title=body.title,
        goal=body.goal,
    )
    return asdict(mission)


@router.post("/workbench/sessions/{session_id}/missions/{mission_id}/issues")
async def attach_workbench_issue(
    session_id: str,
    mission_id: str,
    body: IssueAttach,
    request: Request,
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    issue = await engine.workbench_service.attach_issue(
        session_id=session_id,
        mission_id=mission_id,
        task_id=body.task_id,
        acceptance_criteria=body.acceptance_criteria,
        parallel_mode=body.parallel_mode,
        risk_level=body.risk_level,
    )
    return issue


@router.post(
    "/workbench/sessions/{session_id}/missions/{mission_id}/intent-locks",
    status_code=201,
)
async def create_intent_lock(
    session_id: str,
    mission_id: str,
    body: IntentLockCreate,
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
    return lock


@router.post(
    "/workbench/sessions/{session_id}/missions/{mission_id}/decisions",
    status_code=201,
)
async def create_decision(
    session_id: str,
    mission_id: str,
    body: DecisionCreate,
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
    return decision


@router.post("/workbench/sessions/{session_id}/issues/{task_id}/claim", status_code=201)
async def claim_workbench_issue(
    session_id: str,
    task_id: str,
    body: ClaimIssue,
    request: Request,
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
    return asdict(lease)


@router.post("/workbench/sessions/{session_id}/leases/{lease_id}/release")
async def release_workbench_lease(
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
    market = _get_task_market(engine)
    lease = await market.release(lease_id)
    if lease is None:
        raise HTTPException(status_code=404, detail="租约不存在")
    return asdict(lease)


@router.post("/workbench/sessions/{session_id}/leases/expire")
async def expire_workbench_leases(
    session_id: str,
    request: Request,
    auth: str = AuthDep,
):
    engine = request.app.state.engine
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    market = _get_task_market(engine)
    expired = await market.expire_overdue_leases()
    return {"expired": [asdict(lease) for lease in expired]}


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
    session = await engine.session_store.load(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await engine.load_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    issues = await engine.workbench_service.list_issues(
        session_id, mission_id=mission_id, risk_level=risk_level, limit=limit
    )
    return IssuesResponse(
        issues=issues["issues"],
        mission_id=mission_id,
        risk_level=risk_level,
        limit=limit,
    )


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
    leases = await engine.workbench_service.list_leases(
        session_id, state=state, task_id=task_id, agent_id=agent_id, limit=limit
    )
    return LeasesResponse(
        leases=leases["leases"],
        state=state,
        task_id=task_id,
        agent_id=agent_id,
        limit=limit,
    )


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
    approvals = await engine.workbench_service.list_approvals(
        session_id, state=state, limit=limit
    )
    return ApprovalsResponse(
        approvals=approvals,
        state=state.value if state is not None else None,
        limit=limit,
    )


@router.post("/workbench/sessions/{session_id}/approvals/{approval_id}/resolve")
async def resolve_approval(
    session_id: str,
    approval_id: str,
    body: ApprovalResolve,
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
        approval = await engine.workbench_service.resolve_approval(
            session_id=session_id,
            approval_id=approval_id,
            actor=body.actor,
            state=body.state,
            decision_note=body.decision_note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if approval is None:
        raise HTTPException(status_code=404, detail="审批请求不存在")
    return approval


@router.post(
    "/workbench/sessions/{session_id}/validation-runs",
    response_model=ValidationRunResultResponse,
    status_code=201,
)
async def create_validation_run(
    session_id: str,
    body: ValidationRunCreate,
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
    return ValidationRunResultResponse(
        id=result["id"],
        status=result["status"],
        exit_code=result["exit_code"],
        output=result["output"],
    )
