"""Authenticated HTTP contracts over real personal and session databases."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from naumi_agent.api.routes.personal import router
from naumi_agent.config.settings import MemoryConfig
from naumi_agent.memory.session import SessionStore
from naumi_agent.personal.service import PersonalAgentService
from naumi_agent.personal.store import PersonalAgentStore
from naumi_agent.tools.base import Tool, ToolRegistry


class ReadTool(Tool):
    name = "file_read"
    description = "读取文件"
    parameters_schema = {"type": "object"}

    async def execute(self, **kwargs):
        return "文件内容"


@pytest.fixture
async def api(tmp_path):
    config = SimpleNamespace(
        memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")),
        api=SimpleNamespace(api_keys=["personal-test-key"]),
    )
    sessions = SessionStore(config.memory)
    registry = ToolRegistry()
    registry.register(ReadTool())
    engine = SimpleNamespace(
        config=config,
        session_store=sessions,
        tool_registry=registry,
        workspace_root=tmp_path / "workspace",
        router=SimpleNamespace(resolve_model=lambda tier: "test-model"),
    )
    engine.personal_agents = PersonalAgentService(engine, PersonalAgentStore(tmp_path / "personal"))
    app = FastAPI()
    app.include_router(router)
    app.state.engine = engine
    app.state.config = config
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": "personal-test-key"},
    ) as client:
        yield client, app
    await sessions.close()


def payload(**updates):
    return {
        "request_id": "api-create",
        "name": "自定义专员",
        "instructions": "按用户委托读资料",
        "tool_names": ["file_read"],
        **updates,
    }


async def test_current_identity_failure_does_not_become_main_assistant(api, tmp_path):
    client, app = api
    agent = (await client.post("/personal/agents", json=payload())).json()
    opened = (await client.post(f"/personal/agents/{agent['id']}/session")).json()
    engine = app.state.engine
    engine.personal_agents = PersonalAgentService(engine, PersonalAgentStore(tmp_path / "empty"))
    response = await client.get("/personal/current", params={"session_id": opened["session_id"]})
    assert response.status_code == 409
    assert response.json()["code"] == "binding_conflict"


async def test_empty_authentication_and_all_routes_protected(api):
    client, _ = api
    assert (await client.get("/personal/agents")).json() == {"agents": []}
    assert (await client.get("/personal/tools")).json()["tools"][0]["name"] == "file_read"
    for method, url in (
        ("GET", "/personal/agents"),
        ("GET", "/personal/tools"),
        ("GET", "/personal/current?session_id=sample"),
        ("POST", "/personal/agents"),
        ("GET", "/personal/agents/pa_" + "a" * 32),
        ("PATCH", "/personal/agents/pa_" + "a" * 32),
        ("GET", "/personal/agents/pa_" + "a" * 32 + "/history"),
        ("POST", "/personal/agents/pa_" + "a" * 32 + "/session"),
    ):
        response = await client.request(method, url, headers={"X-API-Key": "wrong"})
        assert response.status_code == 401, (url, response.text)
    assert (await client.get("/personal/agents")).json() == {"agents": []}


async def test_lifecycle_history_open_and_current_identity(api):
    client, app = api
    created = await client.post("/personal/agents", json=payload())
    assert created.status_code == 201
    agent = created.json()
    assert (await client.post("/personal/agents", json=payload())).json() == agent
    assert (await client.get(f"/personal/agents/{agent['id']}")).json() == agent
    opened = await client.post(f"/personal/agents/{agent['id']}/session")
    assert opened.status_code == 200
    session_id = opened.json()["session_id"]
    assert await app.state.engine.session_store.load(session_id) is not None
    assert (await client.get("/personal/current", params={"session_id": session_id})).json() == {
        "agent": agent,
    }
    renamed = await client.patch(
        f"/personal/agents/{agent['id']}", json={"expected_version": 1, "name": "新名称"}
    )
    assert renamed.status_code == 200 and renamed.json()["version"] == 2
    stale = await client.patch(
        f"/personal/agents/{agent['id']}", json={"expected_version": 1, "name": "过期修改"}
    )
    assert stale.status_code == 409 and stale.json()["code"] == "version_conflict"
    history = (await client.get(f"/personal/agents/{agent['id']}/history")).json()["revisions"]
    assert [x["version"] for x in history] == [1, 2]
    archived = await client.patch(
        f"/personal/agents/{agent['id']}", json={"expected_version": 2, "status": "archived"}
    )
    assert archived.status_code == 200
    assert (await client.get("/personal/agents")).json() == {"agents": []}
    assert len((await client.get("/personal/agents?include_archived=true")).json()["agents"]) == 1
    assert (await client.post(f"/personal/agents/{agent['id']}/session")).status_code == 409
    assert (await client.get("/personal/current", params={"session_id": session_id})).json()[
        "agent"
    ]["status"] == "archived"


@pytest.mark.parametrize(
    "updates",
    [
        {"name": ""},
        {"name": "x" * 81},
        {"name": 42},
        {"instructions": " "},
        {"instructions": "x" * 12001},
        {"model_tier": "admin"},
        {"unexpected": "value"},
        {"tool_names": "file_read"},
        {"tool_names": ["memory_store"]},
        {"tool_names": ["file_read", "file_read"]},
        {"tool_names": [42]},
        {"member_ids": [None]},
        {"request_id": None},
    ],
)
async def test_invalid_create_is_chinese_422_with_zero_writes(api, updates):
    client, _ = api
    response = await client.post("/personal/agents", json=payload(**updates))
    assert response.status_code == 422
    assert response.json()["detail"] and any(
        "\u4e00" <= c <= "\u9fff" for c in response.json()["detail"]
    )
    assert (await client.get("/personal/agents")).json() == {"agents": []}


async def test_invalid_update_null_and_unknown_fields_preserve_version(api):
    client, _ = api
    agent = (await client.post("/personal/agents", json=payload())).json()
    for fields in (
        {"expected_version": True, "name": "修改"},
        {"expected_version": "1", "name": "修改"},
        {"expected_version": 1, "name": None},
        {"expected_version": 1, "tool_names": None},
        {"expected_version": 1, "unknown": 1},
        {"expected_version": 1},
    ):
        result = await client.patch(f"/personal/agents/{agent['id']}", json=fields)
        assert result.status_code == 422, result.text
    assert (await client.get(f"/personal/agents/{agent['id']}")).json() == agent


async def test_parallel_create_and_open_only_one_agent_and_session(api):
    client, app = api
    responses = await asyncio.gather(
        *(client.post("/personal/agents", json=payload()) for _ in range(6))
    )
    assert all(x.status_code == 201 for x in responses)
    assert len({x.json()["id"] for x in responses}) == 1
    agent_id = responses[0].json()["id"]
    opens = await asyncio.gather(
        *(client.post(f"/personal/agents/{agent_id}/session") for _ in range(6))
    )
    assert all(x.status_code == 200 for x in opens)
    assert len({x.json()["session_id"] for x in opens}) == 1
    assert (await app.state.engine.session_store.list_sessions())[1] == 1


async def test_conflicting_create_team_and_missing_ids(api):
    client, _ = api
    await client.post("/personal/agents", json=payload())
    conflict = await client.post("/personal/agents", json=payload(name="不同请求内容"))
    assert conflict.status_code == 409 and conflict.json()["code"] == "idempotency_conflict"
    team = (
        await client.post("/personal/agents", json=payload(request_id="team", kind="team"))
    ).json()
    opening = await client.post(f"/personal/agents/{team['id']}/session")
    assert opening.status_code == 200 and opening.json()["agent_id"] == team["id"]
    identity = await client.get(
        "/personal/current", params={"session_id": opening.json()["session_id"]}
    )
    assert identity.json()["agent"]["kind"] == "team"
    assert identity.json().get("read_only", False) is False
    assert (await client.get("/personal/agents/pa_" + "0" * 32)).status_code == 404
    assert (await client.get("/personal/agents/bad-id")).status_code == 422
    assert (await client.get("/personal/current")).status_code == 422
    assert (await client.get("/personal/current?session_id=unrelated")).json() == {"agent": None}


async def test_open_rejects_extra_body_fields_and_preserves_empty_sessions(api):
    client, app = api
    agent = (await client.post("/personal/agents", json=payload())).json()
    response = await client.post(f"/personal/agents/{agent['id']}/session", json={"switch": True})
    assert response.status_code == 422 and response.json()["code"] == "invalid_input"
    assert (await app.state.engine.session_store.list_sessions())[1] == 0
