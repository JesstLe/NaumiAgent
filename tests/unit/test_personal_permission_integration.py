"""Real engine/file tool, SQLite and HTTP approval-view integration.

Only the model step is scripted. Approval remains the real parent callback.
No real account, model request, browser or user data is used.
"""

import asyncio
import json
import sqlite3
from pathlib import Path
from types import MethodType

import httpx
import pytest
from fastapi import FastAPI

from naumi_agent.api.routes.personal import router
from naumi_agent.daemons.permission_decisions import permission_arguments_sha256
from naumi_agent.orchestrator.engine import AgentResult, AgentUsage
from naumi_agent.personal import runtime
from naumi_agent.tools.base import ToolCall
from tests.unit import test_personal_collaboration as collaboration_tests

setup = collaboration_tests.setup
plan = collaboration_tests.plan
finish = collaboration_tests.finish


@pytest.fixture
def runtime_payload_key(monkeypatch):
    monkeypatch.setenv("NAUMI_RUNTIME_PAYLOAD_KEY", "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=")


@pytest.mark.usefixtures("runtime_payload_key")
@pytest.mark.parametrize("body", ["x" * 70000, "中文验收" * 8000], ids=["ascii70k", "utf8-96k"])
@pytest.mark.parametrize("decision", ["allow_once", "deny"])
async def test_full_large_approval_arguments_reach_ui_and_execution_without_truncation(
    setup, body, decision
):
    engine, service, profiles, _records = setup
    engine.config.safety.permission_mode = "moderate"
    profile = engine.personal_agents.update(profiles[0].id, 1, tool_names=["file_write"])
    entered, release = asyncio.Event(), asyncio.Event()
    requests, outcomes, destinations, receipts = [], [], [], []

    async def confirmer(payload):
        requests.append(payload)
        entered.set()
        await release.wait()
        return decision

    engine.set_permission_confirmer(confirmer)

    def child_factory(step, job):
        child = service._make_child(step, job)

        async def scripted_run(self, task, callback, turn_context=""):
            runtime.begin_turn(self)
            destination = Path(self.workspace_root) / "approved-output.txt"
            destinations.append(destination)
            outcomes.append(
                await self._execute_tool(
                    ToolCall(
                        id="large-permission-call",
                        name="file_write",
                        arguments={"path": str(destination), "content": body},
                    )
                )
            )
            receipts.extend(
                self._permission_decision_store.list_session(self._session.id, limit=50)
            )
            return AgentResult(
                status="completed", response="controlled terminal result", usage=AgentUsage()
            )

        child.run_streaming = MethodType(scripted_run, child)
        return child

    service._child_factory = child_factory
    payload = plan(engine, profiles, steps=plan(engine, profiles)["steps"][:1])
    payload["steps"][0]["agent_version"] = profile.version
    created = await service.create(**payload)
    await service.tick()
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        assert len(requests) == 1 and requests[0]["arguments"]["content"] == body
        assert not destinations[0].exists()
        app = FastAPI()
        app.include_router(router)
        app.state.engine, app.state.config = engine, engine.config
        engine.config.api.api_keys = ["isolated-local-test"]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (
                await client.get("/personal/collaborations/" + created["id"])
            ).status_code == 401
            client.headers["X-API-Key"] = "isolated-local-test"
            response = await client.get("/personal/collaborations/" + created["id"])
            assert response.status_code == 200
            permission = next(
                e for e in response.json()["events"] if e["kind"] == "permission_requested"
            )
            assert permission["data"] == requests[0]
            assert permission["data"]["arguments"]["content"] == body
        with sqlite3.connect(service.store.db_path) as db:
            raw = db.execute(
                "SELECT data_json FROM collaboration_events WHERE kind='permission_requested'"
            ).fetchone()[0]
            assert len(raw.encode("utf-8")) < 64 * 1024
            assert body not in raw
            envelope = db.execute(
                "SELECT envelope_json FROM collaboration_permission_payloads"
            ).fetchone()[0]
            assert body not in envelope
    finally:
        release.set()
    settled = await finish(service, created["id"])
    assert len(outcomes) == 1
    events = [e for e in settled["events"] if e["kind"].startswith("permission_")]
    assert [e["kind"] for e in events] == ["permission_requested", "permission_resolved"]
    assert events[0]["data"]["arguments"] == requests[0]["arguments"]
    if decision == "allow_once":
        assert outcomes[0].status == "success"
        assert destinations[0].read_text(encoding="utf-8") == body
        matches = [r for r in receipts if r.call_id == "large-permission-call"]
        assert matches and any(
            r.arguments_sha256 == permission_arguments_sha256(requests[0]["arguments"])
            for r in matches
        )
    else:
        assert outcomes[0].status == "error"
        assert not destinations[0].exists()


@pytest.mark.usefixtures("runtime_payload_key")
async def test_approval_argument_limit_stays_256k_before_callback(setup):
    engine, service, profiles, _records = setup
    engine.config.safety.permission_mode = "moderate"
    profile = engine.personal_agents.update(profiles[0].id, 1, tool_names=["file_write"])
    arguments = {
        "path": str(Path(engine.workspace_root) / "too-large.txt"),
        "content": "x" * (256 * 1024),
    }
    with pytest.raises(ValueError):
        permission_arguments_sha256(arguments)
    assert len(json.dumps(arguments).encode()) > 256 * 1024
    requests, outcomes = [], []

    async def confirmer(payload):
        requests.append(payload)
        return "allow_once"

    engine.set_permission_confirmer(confirmer)

    def child_factory(step, job):
        child = service._make_child(step, job)

        async def scripted_run(self, task, callback, turn_context=""):
            runtime.begin_turn(self)
            outcomes.append(
                await self._execute_tool(
                    ToolCall(
                        id="over-limit-call",
                        name="file_write",
                        arguments=arguments,
                    )
                )
            )
            return AgentResult(
                status="completed", response="controlled terminal result", usage=AgentUsage()
            )

        child.run_streaming = MethodType(scripted_run, child)
        return child

    service._child_factory = child_factory
    value = plan(engine, profiles, steps=plan(engine, profiles)["steps"][:1])
    value["steps"][0]["agent_version"] = profile.version
    created = await service.create(**value)
    await finish(service, created["id"])
    assert len(outcomes) == 1 and outcomes[0].status == "error"
    assert requests == []
    assert not Path(arguments["path"]).exists()
