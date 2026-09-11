"""Web controls over the existing durable runtime stores and tools."""

from __future__ import annotations

import os
import subprocess
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from naumi_agent.api.deps import AuthDep
from naumi_agent.api.routes.messages import _engine_lock
from naumi_agent.orchestrator.goal_store import GoalStatus, GoalStoreError
from naumi_agent.scheduler.models import ScheduleJob
from naumi_agent.tasks.models import TaskStatus
from naumi_agent.tasks.tools import TaskCreateTool, TaskUpdateTool
from naumi_agent.tools.goal import GoalCreateTool, GoalUpdateTool
from naumi_agent.ui.goal_panel import build_goal_pursuit_snapshot

router = APIRouter(tags=["workspace-controls"])

_TREE_IGNORED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".naumi",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
}
_TREE_MAX_DEPTH = 10
_TREE_MAX_ITEMS = 2500


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


class ScheduleCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    kind: str = Field(pattern="^(once|cron)$")
    expression: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=8000)


class GitBranchSwitch(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    branch: str = Field(min_length=1, max_length=255)


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


def _schedule_payload(job: ScheduleJob) -> dict[str, object]:
    return {
        "id": job.id,
        "kind": job.kind.value,
        "expression": job.expression,
        "prompt": job.prompt,
        "target": job.target.value,
        "status": job.status.value,
        "next_fire_at": job.next_fire_at,
        "created_at": job.created_at,
        "last_fired_at": job.last_fired_at,
        "fired_count": job.fired_count,
    }


@router.get("/schedules")
async def list_schedules(request: Request, auth: str = AuthDep):
    runner = request.app.state.engine.scheduler_runner
    return {"schedules": [_schedule_payload(job) for job in runner.list_jobs()]}


@router.post("/schedules", status_code=201)
async def create_schedule(body: ScheduleCreate, request: Request, auth: str = AuthDep):
    try:
        job = request.app.state.engine.scheduler_runner.create(
            kind=body.kind,
            expression=body.expression,
            prompt=body.prompt,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    request.app.state.engine.scheduler_runner.start()
    return _schedule_payload(job)


@router.post("/schedules/{schedule_id}/{action}")
async def control_schedule(
    schedule_id: str,
    action: str,
    request: Request,
    auth: str = AuthDep,
):
    runner = request.app.state.engine.scheduler_runner
    operations = {
        "pause": runner.pause,
        "resume": runner.resume,
        "cancel": runner.cancel,
    }
    operation = operations.get(action)
    if operation is None:
        raise HTTPException(404, "不支持的定时任务操作")
    job = operation(schedule_id)
    if job is None:
        raise HTTPException(404, "定时任务不存在")
    if action == "resume":
        runner.start()
    return _schedule_payload(job)


@router.get("/extensions/skills")
async def list_skill_extensions(request: Request, auth: str = AuthDep):
    snapshot = request.app.state.engine.skill_loader.discovery_snapshot
    return {
        "summary": {
            "selected": snapshot.selected_count,
            "shadowed": snapshot.shadowed_count,
            "invalid": snapshot.invalid_count,
        },
        "sources": [
            {
                "scope": source.scope,
                "path": str(source.path),
                "priority": source.priority,
                "available": source.available,
                "requires_trust_gate": source.requires_trust_gate,
            }
            for source in snapshot.sources
        ],
        "skills": [
            {
                "name": item.name,
                "manifest_path": str(item.manifest_path),
                "source_scope": item.source_scope,
                "source_priority": item.source_priority,
                "state": item.state,
                "reason_code": item.reason_code,
                "selected_manifest_path": (
                    str(item.selected_manifest_path) if item.selected_manifest_path else ""
                ),
            }
            for item in snapshot.candidates
        ],
    }


def _git(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(503, f"Git 操作不可用：{exc}") from exc


def _workspace_tree(engine) -> dict[str, object]:
    workspace = Path(engine.workspace_root).resolve()
    if not workspace.is_dir():
        raise HTTPException(409, "当前工作目录不存在或不可读取")

    items: dict[str, dict[str, object]] = {
        ".": {
            "id": ".",
            "name": workspace.name or str(workspace),
            "path": ".",
            "kind": "directory",
            "extension": "",
            "children": [],
        }
    }
    truncated = False

    def scan(directory: Path, parent_id: str, depth: int) -> None:
        nonlocal truncated
        if truncated or depth > _TREE_MAX_DEPTH:
            truncated = True
            return
        try:
            entries = list(os.scandir(directory))
        except OSError:
            items[parent_id]["unreadable"] = True
            return

        safe_entries: list[os.DirEntry[str]] = []
        for entry in entries:
            if entry.name in _TREE_IGNORED_DIRECTORIES:
                continue
            try:
                if entry.is_symlink():
                    continue
                target = Path(entry.path).resolve()
                target.relative_to(workspace)
            except (OSError, ValueError):
                continue
            safe_entries.append(entry)
        safe_entries.sort(
            key=lambda entry: (
                not entry.is_dir(follow_symlinks=False),
                entry.name.casefold(),
            )
        )

        children: list[str] = []
        for entry in safe_entries:
            if len(items) >= _TREE_MAX_ITEMS:
                truncated = True
                break
            path = Path(entry.path)
            relative = path.relative_to(workspace).as_posix()
            is_directory = entry.is_dir(follow_symlinks=False)
            items[relative] = {
                "id": relative,
                "name": entry.name,
                "path": relative,
                "kind": "directory" if is_directory else "file",
                "extension": "" if is_directory else path.suffix.removeprefix(".").lower(),
                "children": [],
            }
            children.append(relative)
            if is_directory:
                scan(path, relative, depth + 1)
        items[parent_id]["children"] = children

    scan(workspace, ".", 1)
    return {
        "workspace_root": str(workspace),
        "root_id": ".",
        "items": items,
        "truncated": truncated,
        "max_items": _TREE_MAX_ITEMS,
    }


@router.get("/workspace/tree")
async def workspace_tree(request: Request, auth: str = AuthDep):
    return _workspace_tree(request.app.state.engine)


def _git_branches(engine) -> dict[str, object]:
    workspace = Path(engine.workspace_root).resolve()
    current = _git(workspace, "branch", "--show-current")
    if current.returncode != 0:
        return {
            "available": False,
            "workspace_root": str(workspace),
            "current": "",
            "branches": [],
            "dirty": False,
            "error": current.stderr.strip() or "当前工作目录不是 Git 仓库",
        }
    refs = _git(
        workspace,
        "for-each-ref",
        "--format=%(refname:short)",
        "refs/heads",
    )
    branches = []
    for branch in refs.stdout.splitlines():
        name = branch.strip()
        if name and name not in branches:
            branches.append(name)
    dirty = bool(_git(workspace, "status", "--porcelain").stdout.strip())
    return {
        "available": True,
        "workspace_root": str(workspace),
        "current": current.stdout.strip(),
        "branches": branches,
        "dirty": dirty,
        "error": "",
    }


@router.get("/workspace/git/branches")
async def list_git_branches(request: Request, auth: str = AuthDep):
    return _git_branches(request.app.state.engine)


@router.post("/workspace/git/branch")
async def switch_git_branch(
    body: GitBranchSwitch,
    request: Request,
    auth: str = AuthDep,
):
    async with _idle_lock(request):
        engine = request.app.state.engine
        state = _git_branches(engine)
        if not state["available"]:
            raise HTTPException(409, str(state["error"]))
        if state["dirty"]:
            raise HTTPException(409, "工作区存在未提交修改，请处理后再切换分支")
        branches = state["branches"]
        if body.branch not in branches:
            raise HTTPException(404, "分支不存在，请刷新分支列表")
        workspace = Path(engine.workspace_root).resolve()
        result = _git(workspace, "switch", body.branch)
        if result.returncode != 0:
            raise HTTPException(409, result.stderr.strip() or "Git 分支切换失败")
        return _git_branches(engine)
