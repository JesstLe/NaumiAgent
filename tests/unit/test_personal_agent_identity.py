"""Generic Agent identity contracts over real Tool, HTTP and SQLite paths."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from naumi_agent.api.routes.personal import AgentCreate, router
from naumi_agent.config.settings import MemoryConfig
from naumi_agent.memory.session import SessionStore
from naumi_agent.personal import runtime
from naumi_agent.personal.service import PersonalAgentService
from naumi_agent.personal.store import PersonalAgentStore
from naumi_agent.personal.tools import PersonalAgentsTool
from naumi_agent.tools.base import ToolRegistry

ROOT = Path(os.environ.get("IDENTITY_TEST_ROOT", Path(__file__).resolve().parents[2]))
PRODUCT = ROOT / "docs/product/personal-agent"


@pytest.mark.parametrize("surface", ["tool", "http"])
def test_kind_schema_preserves_wire_values_and_explains_independent_agent(surface):
    schema = (
        PersonalAgentsTool(None).parameters_schema
        if surface == "tool"
        else AgentCreate.model_json_schema()
    )
    kind = schema["properties"]["kind"]
    assert kind["enum"] == ["specialist", "team"]
    assert "独立 Agent" in kind.get("description", "")
    assert "兼容" in kind["description"] and "不限定职业" in kind["description"]
    assert "团队" in kind["description"]
    if surface == "http":
        assert kind["default"] == "specialist"


def test_agent_tool_separates_name_from_duties_for_model_callers():
    tool = PersonalAgentsTool(None)
    assert "初始只有主 Agent" in tool.description
    assert "独立 Agent" in tool.description
    assert "用户" in tool.parameters_schema["properties"]["name"]["description"]
    assert "职责" in tool.parameters_schema["properties"]["name"]["description"]
    assert "specialist" not in tool.metadata.search_hint


def test_main_prompt_has_no_preseeded_occupations_and_preserves_user_names():
    engine = SimpleNamespace(_session=None)
    prompt = runtime.role_prompt(engine)
    assert "初始只有主 Agent" in prompt
    assert "仅在用户明确要求时" in prompt
    assert "不要因普通任务自动创建常驻 Agent" in prompt
    assert "不是固定职业" in prompt
    assert "按用户给定名称保存" in prompt
    assert "不默认附加职业化后缀" in prompt
    assert "不因新任务重命名已有Agent" in prompt


def test_product_contract_names_agents_without_predefined_occupations():
    data = json.loads((PRODUCT / "video-capabilities-2026-11-03.json").read_text("utf-8"))
    identity = data["agent_identity"]
    assert identity["entity_name"] == "Agent"
    assert identity["initial_agents"] == ["main"]
    assert identity["creation_policy"] == "explicit_user_request"
    assert identity["fixed_occupations"] is False
    assert identity["name_policy"] == "preserve_user_name"
    assert identity["kind_compatibility"] == {"specialist": "Agent", "team": "团队"}
    assert [c["id"] for c in data["capabilities"]] == [f"VID-{i:02}" for i in range(1, 25)]
    assert all(c["in_scope"] is True for c in data["capabilities"])


def test_machine_and_human_capability_titles_match_without_occupational_types():
    data = json.loads((PRODUCT / "video-capabilities-2026-11-03.json").read_text("utf-8"))
    markdown = (PRODUCT / "video-capabilities-2026-11-03.md").read_text("utf-8")
    titles = dict(re.findall(r"^### (VID-\d{2}) · (.+)$", markdown, re.MULTILINE))
    assert len(titles) == 24
    for capability in data["capabilities"]:
        assert capability["title"] == titles[capability["id"]]
        current = [
            capability["title"],
            capability["requirement"],
            *capability["acceptance"],
            capability.get("implementation_note", ""),
        ]
        assert not any(re.search("专员|研究员|运营员|专业助手", value) for value in current)


@pytest.fixture
async def identity_api(tmp_path):
    config = SimpleNamespace(
        memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")),
        api=SimpleNamespace(api_keys=["identity-local-test-key"]),
    )
    engine = SimpleNamespace(
        config=config,
        session_store=SessionStore(config.memory),
        tool_registry=ToolRegistry(),
        workspace_root=tmp_path / "workspace",
        router=SimpleNamespace(resolve_model=lambda tier: "identity-no-provider"),
        _session=None,
    )
    engine.personal_agents = PersonalAgentService(engine, PersonalAgentStore(tmp_path / "personal"))
    app = FastAPI()
    app.include_router(router)
    app.state.engine, app.state.config = engine, config
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://identity-local",
            headers={"X-API-Key": "identity-local-test-key"},
        ) as client:
            yield client, engine
    finally:
        await engine.session_store.close()


@pytest.mark.parametrize("name", ["Lambda-7", "Mira 🌙", "山海", "用户指定的预算核对员"])
async def test_user_name_identity_history_and_session_survive_duty_change_and_restart(
    identity_api, name
):
    client, engine = identity_api
    assert (await client.get("/personal/agents")).json() == {"agents": []}
    assert runtime.role_prompt(engine).startswith("\n## Agent 管理")
    assert engine.personal_agents.list() == ()
    payload = {
        "request_id": "explicit-user-create",
        "name": name,
        "instructions": "整理用户指定的研究资料。",
        "tool_names": [],
    }
    tool = PersonalAgentsTool(engine.personal_agents)
    created = json.loads(await tool.execute(operation="create", **payload))
    duplicate = await client.post("/personal/agents", json=payload)
    assert duplicate.is_success and duplicate.json() == created
    assert created["name"] == name and created["kind"] == "specialist"
    agent_id = created["id"]
    opened = (await client.post(f"/personal/agents/{agent_id}/session")).json()
    session = await engine.session_store.load(opened["session_id"])
    assert session.personal_agent_id == agent_id
    session.add_message("user", "这条历史属于同一个 Agent。")
    await engine.session_store.save(session)
    updated = await client.patch(
        f"/personal/agents/{agent_id}",
        json={
            "expected_version": created["version"],
            "instructions": "改为整理生活安排，不改变身份。",
        },
    )
    assert updated.is_success
    assert updated.json()["name"] == name
    assert updated.json()["id"] == agent_id and updated.json()["version"] == 2
    await engine.session_store.close()
    engine.session_store = SessionStore(engine.config.memory)
    engine.personal_agents = PersonalAgentService(
        engine, PersonalAgentStore(engine.personal_agents.store.base_dir)
    )
    restored = engine.personal_agents.get(agent_id)
    assert restored.name == name and restored.instructions == updated.json()["instructions"]
    reopened = await engine.personal_agents.open_session(agent_id)
    assert reopened["session_id"] == opened["session_id"]
    history = await engine.session_store.load(reopened["session_id"])
    assert history.messages[-1]["content"] == "这条历史属于同一个 Agent。"
    assert [revision.name for revision in engine.personal_agents.history(agent_id)] == [name, name]
    assert engine.personal_agents.list() == (restored,)
    assert restored.tool_names == ()
