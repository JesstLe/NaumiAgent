"""Workbench routes for the local Mac app."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from naumi_agent.api.deps import AuthDep
from naumi_agent.workbench.models import ParallelMode, RiskLevel

router = APIRouter(tags=["workbench"])


class MissionCreate(BaseModel):
    title: str
    goal: str


class IssueAttach(BaseModel):
    task_id: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    parallel_mode: ParallelMode = ParallelMode.EXCLUSIVE
    risk_level: RiskLevel = RiskLevel.MEDIUM


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
