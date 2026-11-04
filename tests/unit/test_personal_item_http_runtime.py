"""Real Engine and authenticated HTTP integration without provider-side effects."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from naumi_agent.api.app import create_app
from naumi_agent.config.settings import AppConfig
from naumi_agent.personal.item_execution import PersonalItemExecutionService
from naumi_agent.runtime.composition import create_agent_engine
from naumi_agent.tools.base import ToolCall

pytestmark = pytest.mark.usefixtures("runtime_payload_key")


@pytest.fixture
async def system(tmp_path, monkeypatch):
    monkeypatch.setenv("NAUMI_STATE_HOME", str(tmp_path / "state-home"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = AppConfig(
        workspace_root=str(workspace),
        personal={"data_dir": str(tmp_path / "personal")},
        memory={
            "session_db_path": str(tmp_path / "runtime" / "sessions.db"),
            "long_term_enabled": False,
        },
        api={"api_keys": ["item-http-fixture"]},
        safety={"permission_mode": "bypass"},
    )
    engine = create_agent_engine(config)
    engine.router.call = AsyncMock(side_effect=AssertionError("No provider calls in fixture"))
    app = create_app()
    app.state.engine, app.state.config = engine, config
    app.state.chat_run_store = engine.chat_run_store
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://item-test/api/v1",
        headers={"X-API-Key": "item-http-fixture"},
    ) as client:
        yield engine, client, app
    engine.router.call.assert_not_awaited()
    await engine.shutdown()


def payload(**updates):
    return {
        "request_id": "http-original",
        "assignee_id": "main",
        "assignee_version": 0,
        "title": "真实事项",
        "instructions": "整理所选资料",
        "schedule": {
            "mode": "once",
            "local_time": "2035-01-01T09:00:00",
            "timezone": "Asia/Shanghai",
        },
        **updates,
    }


async def test_authenticated_real_http_create_cas_controls_and_no_read_side_effects(system):
    engine, client, _ = system
    created = await client.post("/personal/items", json=payload())
    assert created.status_code == 201, created.text
    item = created.json()
    assert item["definition"]["assignee"]["id"] == "main"
    retry = await client.post("/personal/items", json=payload())
    assert retry.json() == item
    assert len((await client.get("/personal/items")).json()["items"]) == 1
    paused = await client.post(
        f"/personal/items/{item['item_id']}/pause",
        json={
            "request_id": "pause-http",
            "expected_revision": 1,
        },
    )
    assert paused.status_code == 200 and paused.json()["state"] == "paused"
    stale = await client.patch(
        f"/personal/items/{item['item_id']}",
        json={
            **payload(request_id="stale-update"),
            "expected_revision": 1,
        },
    )
    assert stale.status_code == 409
    assert (await client.get("/personal/item-results?unread=true")).json()["results"] == []
    assert await engine.personal_items._intents() == []
    assert (await client.get("/personal/items", headers={"X-API-Key": "wrong"})).status_code == 401


async def test_manual_private_material_dialogue_can_create_actor_scoped_item(system):
    engine, _, _ = system
    session = await engine.get_or_create_session()
    artifact = await engine.personal_artifacts.publish(
        actor_id="main",
        request_id="selected-note",
        title="所选笔记",
        kind="material",
        filename="note.txt",
        content=b"exact-private-material",
    )
    ref = engine.personal_artifacts.reference(artifact)
    await engine.personal_artifacts.bind_refs(session.id, "main", [ref])
    engine._personal_artifact_refs = [ref]
    result = await engine.execute_tool(
        ToolCall(
            id="create-private-item",
            name="personal_items",
            arguments={
                "operation": "create",
                **payload(),
                "input_refs": [ref],
            },
        ),
        agent_name="cli",
    )
    assert result.status == "success", result.content
    item = json.loads(result.content)
    assert item["definition"]["inputs"]["artifact_refs"] == [ref]
    assert item["definition"]["origin"]["session_id"] == session.id
    assert item["occurrence"] is None


async def test_item_run_messages_http_uses_original_grant_gate(system):
    engine, client, _ = system
    item = (await client.post("/personal/items", json=payload())).json()
    raw = await engine.personal_items._item(item["item_id"])
    intent = engine.personal_items.executor.prepare(
        raw,
        occurrence_id="pio_" + "a" * 32,
        execution_session_id="pie_" + "a" * 32,
        task=raw["definition"]["instructions"],
        context="",
        input_refs=[],
        input_manifest=[],
        schedule_id="sch_pi_" + "c" * 64,
        event_id="event-exact",
    )
    await engine.personal_items._call(
        "prepare",
        item_id=item["item_id"],
        expected_definition_sha256=item["definition_sha256"],
        intent=intent,
    )
    gate = AsyncMock(
        side_effect=__import__(
            "naumi_agent.personal.models",
            fromlist=["PersonalAgentError"],
        ).PersonalAgentError("artifact_access_revoked", "原授权已撤销")
    )
    engine.personal_artifacts.authorize_refs = gate
    for path in ("messages", "runs", "runs/exact-run"):
        response = await client.get(f"/sessions/{intent['execution_session_id']}/{path}")
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "artifact_access_revoked"
    assert gate.await_count == 3


async def test_started_old_cancel_does_not_change_new_once_header(system):
    engine, client, _ = system
    item = (await client.post("/personal/items", json=payload())).json()
    raw = await engine.personal_items._item(item["item_id"])
    executor: PersonalItemExecutionService = engine.personal_items.executor
    intent = executor.prepare(
        raw,
        occurrence_id="pio_" + "b" * 32,
        execution_session_id="pie_" + "b" * 32,
        task=raw["definition"]["instructions"],
        context="",
        input_refs=[],
        input_manifest=[],
        schedule_id="sch_pi_" + "d" * 64,
        event_id="event-original-cancel",
    )
    intent = await engine.personal_items._call(
        "prepare",
        item_id=item["item_id"],
        expected_definition_sha256=item["definition_sha256"],
        intent=intent,
    )
    original_cancel = {"request_id": "original-cancel", "expected_revision": 1}
    cancelled = await engine.personal_items.control(item["item_id"], "cancel", original_cancel)
    restarted = await engine.personal_items.control(
        item["item_id"],
        "restart",
        {
            "request_id": "explicit-restart",
            "expected_revision": cancelled["revision"],
        },
    )
    await engine.personal_items.control(item["item_id"], "cancel", original_cancel)
    latest = await engine.personal_items.get(item["item_id"])
    assert latest["state"] == "active"
    assert latest["once_key"] == restarted["once_key"] != intent["once_key"]
