"""Workbench routes for the local Mac app."""

from __future__ import annotations

import os
from dataclasses import asdict
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from naumi_agent import __version__
from naumi_agent.api.deps import AuthDep
from naumi_agent.workbench.market import TaskMarket
from naumi_agent.workbench.models import ParallelMode, RiskLevel

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


class ClaimIssue(BaseModel):
    agent_id: str
    duration_minutes: int = Field(default=45, ge=1)
    worktree_name: str = ""


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
