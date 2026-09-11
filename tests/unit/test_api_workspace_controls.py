"""Exercise workspace controls with real session and task databases."""

import asyncio
import subprocess
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from naumi_agent.api.routes.workspace_controls import router
from naumi_agent.config.settings import MemoryConfig
from naumi_agent.memory.session import SessionStore
from naumi_agent.orchestrator.goal_store import GoalStore
from naumi_agent.orchestrator.pursuit_store import PursuitStore
from naumi_agent.scheduler.runner import SchedulerRunner
from naumi_agent.scheduler.store import SchedulerStore
from naumi_agent.skills.loader import SkillLoader, build_skill_sources
from naumi_agent.tasks.store import TaskStore


@pytest.fixture
async def controls(tmp_path):
    sessions = SessionStore(MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
    first = await sessions.create_session(title="真实待办测试")
    second = await sessions.create_session(title="隔离会话")
    app = FastAPI()
    app.include_router(router)
    skill_root = tmp_path / ".naumi" / "skills" / "demo"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        "---\nname: demo\ndescription: 真实扩展\n---\n# Demo\n",
        encoding="utf-8",
    )
    skill_loader = SkillLoader(
        sources=build_skill_sources(workspace_root=tmp_path, configured_paths=[], home=tmp_path)
    )
    skill_loader.load_all()
    app.state.engine = SimpleNamespace(
        session_store=sessions,
        task_store=TaskStore(str(tmp_path / "tasks.db")),
        scheduler_runner=SchedulerRunner(SchedulerStore(tmp_path / "scheduler.json")),
        skill_loader=skill_loader,
        workspace_root=tmp_path,
    )
    app.state.engine_lock = asyncio.Lock()
    app.state.engine.goal_store = GoalStore(tmp_path / "goals")
    app.state.engine.pursuit_store = PursuitStore(tmp_path / "pursuit")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, app, first.id, second.id


async def test_todo_lifecycle_dependencies_and_isolation(controls):
    client, app, first, second = controls
    endpoint = f"/sessions/{first}/todos"
    assert (await client.get(endpoint)).json()["todos"] == []
    assert (await client.post(endpoint, json={"subject": " "})).status_code == 422
    assert (
        await client.post(endpoint, json={"subject": "缺失依赖", "blocked_by": ["99"]})
    ).status_code == 409
    response = await client.post(endpoint, json={"subject": "读取配置"})
    assert response.status_code == 201
    first_task = response.json()["todos"][0]["id"]
    response = await client.post(endpoint, json={"subject": "核对配置", "blocked_by": [first_task]})
    dependent = response.json()["todos"][-1]["id"]
    assert (
        await client.patch(f"{endpoint}/{dependent}", json={"status": "completed"})
    ).status_code == 409
    assert (
        await client.patch(f"{endpoint}/{first_task}", json={"status": "completed"})
    ).status_code == 200
    assert (
        await client.patch(f"{endpoint}/{first_task}", json={"status": "pending"})
    ).status_code == 409
    assert (
        await client.patch(f"{endpoint}/{dependent}", json={"status": "in_progress"})
    ).status_code == 200
    assert (await client.get(f"/sessions/{second}/todos")).json()["todos"] == []
    assert (await client.get("/sessions/missing/todos")).status_code == 404
    reopened = TaskStore(str(app.state.engine.task_store.db_path)).scoped(first)
    assert [task.subject for task in await reopened.list_tasks()] == ["读取配置", "核对配置"]


async def test_todo_concurrent_writes_and_busy_rejection(controls):
    client, app, first, _ = controls
    endpoint = f"/sessions/{first}/todos"
    responses = await asyncio.gather(
        *(client.post(endpoint, json={"subject": f"并发 {index}"}) for index in range(5))
    )
    assert all(response.status_code in {201, 409} for response in responses)
    rows = (await client.get(endpoint)).json()["todos"]
    assert len(rows) == len({row["id"] for row in rows})
    assert len(rows) == sum(response.status_code == 201 for response in responses)
    async with app.state.engine_lock:
        assert (await client.post(endpoint, json={"subject": "执行中修改"})).status_code == 409


async def test_goal_lifecycle_conflict_and_pursuit_guard(controls):
    client, app, first, _ = controls
    assert (await client.get("/goals")).json()["goals"] == []
    endpoint = f"/sessions/{first}/goals"
    assert (await client.post(endpoint, json={"objective": " "})).status_code == 422
    result = await client.post(endpoint, json={"objective": "核对当前目标"})
    assert result.status_code == 201
    goal_id = result.json()["current_goal_id"]
    assert (await client.post(endpoint, json={"objective": "重复未完成目标"})).status_code == 409
    assert (await client.patch(f"/goals/{goal_id}", json={"status": "paused"})).status_code == 200
    assert (await client.patch(f"/goals/{goal_id}", json={"status": "active"})).status_code == 200
    app.state.engine.goal_store.attach_pursuit(goal_id, "pursuit-existing")
    assert (
        await client.patch(f"/goals/{goal_id}", json={"status": "completed"})
    ).status_code == 409
    assert (await client.get("/goals")).json()["goals"][0]["pursuit_link_status"] == "missing"


async def test_goal_completion_persists_and_cannot_reopen(controls):
    client, app, first, _ = controls
    response = await client.post(f"/sessions/{first}/goals", json={"objective": "可验收目标"})
    goal_id = response.json()["current_goal_id"]
    result = await client.patch(
        f"/goals/{goal_id}", json={"status": "completed", "note": "已核对结果"}
    )
    assert result.status_code == 200
    assert result.json()["current_goal_id"] == ""
    assert (await client.patch(f"/goals/{goal_id}", json={"status": "active"})).status_code == 409
    restored = GoalStore(app.state.engine.goal_store.base_dir).get(goal_id)
    assert restored.status == "completed" and restored.note == "已核对结果"


async def test_schedule_and_extension_controls_use_real_runtime_stores(controls):
    client, _, _, _ = controls
    assert (await client.get("/schedules")).json()["schedules"] == []
    created = await client.post(
        "/schedules",
        json={
            "kind": "cron",
            "expression": "*/15 * * * *",
            "prompt": "检查构建状态",
        },
    )
    assert created.status_code == 201
    schedule_id = created.json()["id"]
    assert (await client.post(f"/schedules/{schedule_id}/pause")).json()["status"] == "paused"
    assert (await client.post(f"/schedules/{schedule_id}/resume")).json()["status"] == "active"
    assert (await client.post(f"/schedules/{schedule_id}/cancel")).json()["status"] == "cancelled"

    extensions = (await client.get("/extensions/skills")).json()
    assert extensions["summary"]["selected"] == 1
    assert extensions["skills"][0]["name"] == "demo"
    assert extensions["skills"][0]["state"] == "selected"


async def test_git_branch_control_refuses_dirty_workspace(controls, tmp_path):
    client, app, _, _ = controls
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "tracked.txt").write_text("one", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "branch", "feature"], cwd=repo, check=True)
    app.state.engine.workspace_root = repo

    state = (await client.get("/workspace/git/branches")).json()
    assert state["available"] is True
    assert "feature" in state["branches"]
    switched = await client.post("/workspace/git/branch", json={"branch": "feature"})
    assert switched.status_code == 200
    assert switched.json()["current"] == "feature"

    (repo / "tracked.txt").write_text("dirty", encoding="utf-8")
    refused = await client.post("/workspace/git/branch", json={"branch": "master"})
    assert refused.status_code == 409
    assert "未提交修改" in refused.json()["detail"]
