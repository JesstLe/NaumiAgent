"""Web controls over the existing durable runtime stores and tools."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from naumi_agent.api.deps import AuthDep
from naumi_agent.api.routes.messages import _engine_lock
from naumi_agent.orchestrator.goal_store import GoalStatus, GoalStoreError
from naumi_agent.tasks.models import TaskStatus
from naumi_agent.tasks.tools import TaskCreateTool, TaskUpdateTool
from naumi_agent.tools.goal import GoalCreateTool, GoalUpdateTool
from naumi_agent.ui.goal_panel import build_goal_pursuit_snapshot

router = APIRouter(tags=["workspace-controls"])


class GoalCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    objective: str = Field(min_length=1, max_length=8000)


class GoalUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    status: GoalStatus
    note: str = Field(default="", max_length=4000)


def _goals(engine):
    return build_goal_pursuit_snapshot(engine.goal_store, engine.pursuit_store).to_protocol_dict()


@router.get("/goals")
async def list_goals(request: Request, auth: str = AuthDep):
    return _goals(request.app.state.engine)


@router.post("/sessions/{session_id}/goals", status_code=201)
async def create_goal(
    session_id: str,
    body: GoalCreate,
    request: Request,
    auth: str = AuthDep,
):
    async with _idle_lock(request):
        engine = await _session_engine(request, session_id)
        result = await GoalCreateTool(engine.goal_store, lambda: session_id).execute(
            objective=body.objective,
        )
        if result.startswith("⚠"):
            raise HTTPException(409, result)
        return _goals(engine)


@router.patch("/goals/{goal_id}")
async def update_goal(
    goal_id: str,
    body: GoalUpdate,
    request: Request,
    auth: str = AuthDep,
):
    async with _idle_lock(request):
        engine = request.app.state.engine
        try:
            goal = engine.goal_store.get(goal_id)
        except GoalStoreError as exc:
            raise HTTPException(422, str(exc)) from exc
        if goal is None:
            raise HTTPException(404, "目标不存在，请刷新列表")
        if goal.pursuit_run_id:
            raise HTTPException(
                409, "目标已关联追踪运行，请通过追踪控制流程处理，避免只改变目标标签"
            )
        result = await GoalUpdateTool(engine.goal_store).execute(
            goal_id=goal_id,
            **body.model_dump(),
        )
        if result.startswith("⚠"):
            raise HTTPException(409, result)
        return _goals(engine)


class TodoCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    subject: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=8000)
    blocked_by: list[str] = Field(default_factory=list, max_length=100)


class TodoUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: TaskStatus


async def _session_engine(request: Request, session_id: str):
    engine = request.app.state.engine
    if not await engine.session_store.load(session_id):
        raise HTTPException(404, "会话不存在，请刷新会话列表")
    return engine


def _idle_lock(request: Request):
    lock = _engine_lock(request)
    if lock.locked():
        raise HTTPException(409, "Agent 正在执行，请等待本次操作结束后再修改")
    return lock


@router.get("/sessions/{session_id}/todos")
async def list_todos(session_id: str, request: Request, auth: str = AuthDep):
    engine = await _session_engine(request, session_id)
    tasks = await engine.task_store.scoped(session_id).list_tasks()
    return {"session_id": session_id, "todos": [asdict(task) for task in tasks]}


@router.post("/sessions/{session_id}/todos", status_code=201)
async def create_todo(
    session_id: str,
    body: TodoCreate,
    request: Request,
    auth: str = AuthDep,
):
    async with _idle_lock(request):
        engine = await _session_engine(request, session_id)
        store = engine.task_store.scoped(session_id)
        result = await TaskCreateTool(store).execute(**body.model_dump())
        if result.startswith("错误："):
            raise HTTPException(409, result.removeprefix("错误："))
        return {"message": result, "todos": [asdict(task) for task in await store.list_tasks()]}


@router.patch("/sessions/{session_id}/todos/{task_id}")
async def update_todo(
    session_id: str,
    task_id: str,
    body: TodoUpdate,
    request: Request,
    auth: str = AuthDep,
):
    async with _idle_lock(request):
        engine = await _session_engine(request, session_id)
        store = engine.task_store.scoped(session_id)
        task = await store.get_task(task_id)
        if task is None:
            raise HTTPException(404, "待办不存在，请刷新列表")
        if body.status in {TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED}:
            if task.is_blocked(await store.list_tasks()):
                raise HTTPException(409, "请先完成依赖的待办，再开始或完成此项")
        result = await TaskUpdateTool(store).execute(task_id=task_id, status=body.status)
        if result.startswith("错误："):
            raise HTTPException(409, result.removeprefix("错误："))
        return {"message": result, "todos": [asdict(item) for item in await store.list_tasks()]}
