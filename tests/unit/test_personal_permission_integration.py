"""Real engine/file tool, SQLite and HTTP approval-view integration.

Only the model step is scripted. Approval remains the real parent callback.
No real account, model request, browser or user data is used.
"""

import asyncio
import hashlib
import json
import shlex
import sqlite3
import sys
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
from tests.unit import test_personal_messages as peer_tests

setup = collaboration_tests.setup
plan = collaboration_tests.plan
finish = collaboration_tests.finish
peer_setup = peer_tests.peer_setup


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
            assert all(permission["data"][key] == value for key, value in requests[0].items())
            assert permission["data"]["actor_id"] == profile.id
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


@pytest.mark.usefixtures("runtime_payload_key")
@pytest.mark.parametrize("body", ["x" * 70000, "中文验收" * 8000], ids=["ascii70k", "utf8-96k"])
@pytest.mark.parametrize("decision", ["allow_once", "deny"])
async def test_nested_peer_full_permission_payload_uses_receiver_identity(
    peer_setup,
    monkeypatch,
    body,
    decision,
):
    from naumi_agent.model.router import ModelRouter, StreamChunk

    engine, service, profiles, _children, directory = peer_setup
    engine.config.safety.permission_mode = "moderate"
    receiver = engine.personal_agents.update(
        profiles[1].id,
        1,
        tool_names=["file_write", "bash_run"],
    )
    destination = directory / "peer-approved-output.txt"
    verification = directory / "test_approved_content.py"
    expected_digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    verification.write_text(
        "import hashlib\nfrom pathlib import Path\n\n"
        "def test_approved_content():\n"
        f"    content = Path({str(destination)!r}).read_text(encoding='utf-8')\n"
        f"    assert hashlib.sha256(content.encode('utf-8')).hexdigest() == {expected_digest!r}\n",
        encoding="utf-8",
    )
    verify_command = (
        f"{shlex.quote(sys.executable)} -m pytest {shlex.quote(str(verification))} "
        f"-q -p no:cacheprovider --basetemp {shlex.quote(str(directory / 'verify-temp'))}"
    )
    prompts, validation_prompts = [], []

    async def confirm(payload):
        if payload["tool_name"] == "bash_run":
            assert decision == "allow_once"
            assert payload["arguments"]["command"] == verify_command
            validation_prompts.append(payload)
            return "allow_once"
        prompts.append(payload)
        assert payload["arguments"]["content"] == body
        message = service.messages.store.lookup_session(
            service.workspace_key, payload["session_id"]
        )
        assert message is not None and message["recipient_agent_id"] == receiver.id
        view = await service.get(message["collaboration_id"])
        event = next(item for item in view["events"] if item["kind"] == "permission_requested")
        assert event["data"]["peer_message_id"] == message["id"]
        assert event["data"]["actor_id"] == receiver.id
        assert event["data"]["session_id"] == message["receiver_session_id"]
        assert event["data"]["session_id"] != view["steps"][0]["session_id"]
        assert event["data"]["arguments"] == payload["arguments"]
        assert not destination.exists()
        return decision

    engine.set_permission_confirmer(confirm)

    async def stream(self, **kwargs):
        tool_results = peer_tests._tools(kwargs)
        if not tool_results:
            yield peer_tests._tool(
                "file_write",
                {"path": str(destination), "content": body},
                "peer-large-write",
            )
        elif decision == "allow_once" and len(tool_results) == 1:
            yield peer_tests._tool(
                "bash_run",
                {"command": verify_command, "timeout": 30},
                "peer-content-verification",
            )
        else:
            if decision == "allow_once":
                assert "1 passed" in tool_results[-1]["content"]
            yield StreamChunk(token="写入已完成" if decision == "allow_once" else "写入未获同意")
            yield peer_tests._usage()

    monkeypatch.setattr(ModelRouter, "stream", stream)
    created = await service.messages.start_manual(
        source_session_id=engine._session.id,
        request_id="peer-large-permission",
        sender_agent_id=profiles[0].id,
        sender_agent_version=1,
        peer_agents=[
            {
                "agent_id": receiver.id,
                "agent_version": receiver.version,
                "tool_names": ["file_write", "bash_run"],
            }
        ],
        **{
            key: value
            for key, value in peer_tests._request(receiver).items()
            if key != "request_id"
        },
    )
    result = await peer_tests._finish(service, created["id"])
    message = (await service.messages.list(created["id"]))[0]
    assert len(prompts) == 1
    assert result["messages"][0]["id"] == message["id"]
    assert result["peer_sessions"][0]["session_id"] == message["receiver_session_id"]
    if decision == "allow_once":
        assert destination.read_text(encoding="utf-8") == body
        terminal = {
            "root_status": result["status"],
            "root_error": result["steps"][0].get("error"),
            "message_outcome": message["outcome"],
            "message_error": message.get("error"),
        }
        assert result["status"] == "completed" and message["outcome"] == "completed", terminal
        assert len(validation_prompts) == 1
        runs = await _children[-1].chat_run_store.list_runs(message["receiver_session_id"], limit=1)
        assert runs[0].receipt.outcome == "completed"
        assert any(
            check.status == "passed" and check.passed == 1 for check in runs[0].receipt.validations
        )
    else:
        assert not destination.exists()
        assert message["outcome"] == "error"
        assert validation_prompts == []
