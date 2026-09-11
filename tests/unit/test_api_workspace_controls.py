"""Exercise workspace controls with real session and task databases."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from naumi_agent.api.routes.workspace_controls import router
from naumi_agent.config.settings import MemoryConfig
from naumi_agent.memory.session import SessionStore
from naumi_agent.orchestrator.goal_store import GoalStore
from naumi_agent.orchestrator.pursuit_store import PursuitStore
from naumi_agent.tasks.store import TaskStore


@pytest.fixture
async def controls(tmp_path):
    sessions = SessionStore(MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
    first = await sessions.create_session(title="真实待办测试")
    second = await sessions.create_session(title="隔离会话")
    app = FastAPI()
    app.include_router(router)
    app.state.engine = SimpleNamespace(
        session_store=sessions, task_store=TaskStore(str(tmp_path / "tasks.db"))
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
