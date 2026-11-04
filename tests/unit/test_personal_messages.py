"""Peer integration: real Engines/SQLite/ChatRuns with controlled model transport."""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI

from naumi_agent.api.routes.messages import router as sessions_router
from naumi_agent.api.routes.personal import router as personal_router
from naumi_agent.config.settings import AppConfig
from naumi_agent.daemons.agent_jobs import AgentJobState
from naumi_agent.model.router import ModelRouter, StreamChunk, TokenUsage
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.personal import runtime
from naumi_agent.personal.collaboration import PersonalCollaborationService
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.tools.base import ToolCall

pytestmark = pytest.mark.usefixtures("runtime_payload_key")


@pytest.fixture
async def peer_setup(tmp_path, monkeypatch):
    async def unexpected_nonstreaming_call(self, **kwargs):
        raise AssertionError("Controlled peer tests must never fall back to real model HTTP")

    monkeypatch.setattr(ModelRouter, "call", unexpected_nonstreaming_call)
    config = AppConfig(
        workspace_root=str(tmp_path),
        personal={"data_dir": str(tmp_path / "personal")},
        memory={
            "session_db_path": str(tmp_path / "sessions.db"),
            "vector_db_path": str(tmp_path / "vectors"),
            "long_term_enabled": False,
        },
        safety={
            "permission_mode": "bypass",
            "max_turns": 8,
            "max_parallel_agents": 1,
            "max_queued_agents": 0,
            "allowed_dirs": [str(tmp_path)],
        },
    )
    engine = AgentEngine(config)
    try:
        await engine.get_or_create_session()
        engine._append_message({"role": "user", "content": "MAIN_PRIVATE_NEVER_SHARED"})
        await engine._save_session()
        service = engine.personal_collaborations
        service.start = lambda: None
        profiles = [
            engine.personal_agents.create(
                name="专员" + letter,
                instructions="处理显式资料 PEER_ROLE_" + letter,
                request_id="peer-role-" + letter,
                model_tier="fast",
                tool_names=["personal_message", "file_read", "file_write"],
            )
            for letter in "ABC"
        ]
        daily = await engine.personal_agents.open_session(profiles[1].id)
        private_session = await engine.session_store.load(daily["session_id"])
        private_session.add_message("user", "SPECIALIST_PRIVATE_NEVER_SHARED")
        await engine.session_store.save(private_session)
        children = []

        def factory(step, job):
            child = service._make_child(step, job)
            children.append(child)
            return child

        service._child_factory = factory
        (tmp_path / "budget.csv").write_text("日期,金额\n11-01,500\n11-02,325\n", encoding="utf-8")
        (tmp_path / ".gitignore").write_text(
            "*.db\n*.db-*\npersonal/\npersonal-executions/\nvectors/\n.naumi/\n",
            encoding="utf-8",
        )
        for command in (
            ["init", "--quiet"],
            ["add", "budget.csv", ".gitignore"],
            [
                "-c",
                "user.name=Peer Test",
                "-c",
                "user.email=peer@example.invalid",
                "commit",
                "--quiet",
                "-m",
                "Record peer integration fixture",
            ],
        ):
            subprocess.run(
                ["git", "-C", str(tmp_path), *command], check=True, capture_output=True, text=True
            )
        yield engine, service, profiles, children, tmp_path
    finally:
        await engine.shutdown()


def _tool(name, arguments, call_id="peer-call"):
    return StreamChunk(
        tool_call={
            0: {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            }
        },
        finish_reason="tool_calls",
        usage=TokenUsage(input_tokens=11, output_tokens=7, cost_usd=0.002),
    )


def _usage():
    return StreamChunk(
        finish_reason="stop",
        usage=TokenUsage(input_tokens=11, output_tokens=7, cost_usd=0.002),
    )


def _request(profile, *, content="ASK_B", request_id="question-one"):
    return {
        "recipient_agent_id": profile.id,
        "recipient_agent_version": profile.version,
        "request_id": request_id,
        "content": content,
        "context": "EXPLICIT_BUDGET_CONTEXT",
    }


def _plan(engine, profiles, **updates):
    return {
        "source_session_id": engine._session.id,
        "request_id": "peer-plan-one",
        "title": "预算互询",
        "steps": [
            {
                "id": "ask",
                "agent_id": profiles[0].id,
                "agent_version": 1,
                "task": "ROOT_TASK",
                "depends_on": [],
            }
        ],
        "peer_agents": [{"agent_id": p.id, "agent_version": p.version} for p in profiles],
        **updates,
    }


def _task(kwargs):
    return next(m["content"] for m in reversed(kwargs["messages"]) if m["role"] == "user")


def _tools(kwargs):
    return [m for m in kwargs["messages"] if m["role"] == "tool"]


async def _finish(service, identity):
    for _ in range(12):
        await service.tick()
        if service._active:
            await asyncio.wait_for(
                asyncio.gather(*tuple(service._active.values()), return_exceptions=True), timeout=15
            )
        value = await service.get(identity)
        if value["status"] in {"completed", "error", "cancelled", "unknown"}:
            return value
    raise AssertionError("Peer root did not settle")


async def test_single_capacity_real_read_reply_ack_and_exact_direct_run_accounting(
    peer_setup, monkeypatch
):
    engine, service, profiles, children, directory = peer_setup
    calls = []

    async def stream(self, **kwargs):
        assert "MAIN_PRIVATE_NEVER_SHARED" not in json.dumps(kwargs, ensure_ascii=False)
        assert "SPECIALIST_PRIVATE_NEVER_SHARED" not in json.dumps(kwargs, ensure_ascii=False)
        task, tools = _task(kwargs), _tools(kwargs)
        calls.append((task, len(tools)))
        if task == "ROOT_TASK":
            if not tools:
                yield _tool("personal_message", {"operation": "send", **_request(profiles[1])})
            elif len(tools) == 1:
                result = json.loads(tools[0]["content"])
                assert result["response"] == "预算总额为825元"
                yield _tool(
                    "personal_message",
                    {
                        "operation": "ack",
                        "message_id": result["id"],
                        "expected_response_sha256": result["response_sha256"],
                    },
                    "ack-call",
                )
            else:
                yield StreamChunk(token="已确认对端结果：825元")
                yield _usage()
        elif task == "ASK_B":
            names = {s["function"]["name"] for s in kwargs.get("tools") or []}
            assert "file_read" in names and "file_write" not in names
            if not tools:
                yield _tool("file_read", {"path": str(directory / "budget.csv")}, "read-call")
            else:
                assert "500" in tools[0]["content"] and "325" in tools[0]["content"]
                yield StreamChunk(token="预算总额为825元")
                yield _usage()
        else:
            raise AssertionError(task)

    monkeypatch.setattr(ModelRouter, "stream", stream)
    created = await service.create(**_plan(engine, profiles))
    value = await _finish(service, created["id"])
    assert value["status"] == "completed", json.dumps(value, ensure_ascii=False, indent=2)
    messages = await service.messages.list(created["id"])
    assert len(messages) == 1 and messages[0]["state"] == "acknowledged"
    message = await service.messages.get(messages[0]["id"])
    assert message["acknowledged_by"] == "agent"
    assert "dispatch_token" not in message and "root_owner_id" not in message
    assert message["response"] == "预算总额为825元"
    assert len(children) == 2
    root_run = (await engine.chat_run_store.list_runs(value["steps"][0]["session_id"]))[0]
    peer_run = (await engine.chat_run_store.list_runs(message["receiver_session_id"]))[0]
    assert root_run.status == peer_run.status == "completed"
    assert root_run.id == message["source_run_id"] and peer_run.id == message["receiver_run_id"]
    assert root_run.usage.input_tokens + root_run.usage.output_tokens == 54
    assert peer_run.usage.input_tokens + peer_run.usage.output_tokens == 36
    job = await service.jobs.get(value["steps"][0]["job_id"])
    assert job.result.total_tokens == 90 and job.result.total_cost_microusd == 10000
    assert calls == [
        ("ROOT_TASK", 0),
        ("ASK_B", 0),
        ("ASK_B", 1),
        ("ROOT_TASK", 1),
        ("ROOT_TASK", 2),
    ]
    assert len(value["peer_sessions"]) == 1
    marker = await engine.session_store.load(message["receiver_session_id"])
    assert marker.personal_execution_id == message["receiver_task_id"]
    assert engine._session.id == created["source_session_id"]


async def test_manual_outbox_survives_restart_without_sender_model_and_user_ack(
    peer_setup, monkeypatch
):
    engine, service, profiles, children, _ = peer_setup
    model_calls = []

    async def stream(self, **kwargs):
        model_calls.append(_task(kwargs))
        assert _task(kwargs) == "ASK_B"
        yield StreamChunk(token="持久回答825")
        yield _usage()

    monkeypatch.setattr(ModelRouter, "stream", stream)
    body = dict(
        source_session_id=engine._session.id,
        request_id="manual-one",
        sender_agent_id=profiles[0].id,
        sender_agent_version=1,
        **{k: v for k, v in _request(profiles[1]).items() if k != "request_id"},
    )
    first = await service.messages.start_manual(**body)
    assert first["message_request_id"] == body["request_id"]
    assert first["request_id"].startswith("message-")
    assert first["request_id"] != first["message_request_id"]
    queued = await service.messages.list(first["id"])
    assert len(queued) == 1 and queued[0]["state"] == "queued"
    other = PersonalCollaborationService(engine)
    other.start = lambda: None
    try:
        replay = await other.messages.start_manual(**body)
        assert replay["id"] == first["id"]
        assert replay["message_request_id"] == first["message_request_id"]
        assert replay["request_id"] == first["request_id"]
        await asyncio.gather(service.tick(), other.tick())
        await asyncio.gather(*tuple(service._active.values()), *tuple(other._active.values()))
        value = await _finish(other, first["id"])
        assert value["status"] == "completed", json.dumps(value, ensure_ascii=False, indent=2)
        assert value["message_request_id"] == body["request_id"]
        message = (await other.messages.list(first["id"]))[0]
        assert message["state"] == "responded" and message["origin"] == "manual"
        with pytest.raises(PersonalAgentError):
            await other.messages.acknowledge(message["id"], "0" * 64)
        ack = await other.messages.acknowledge(message["id"], message["response_sha256"])
        assert ack["state"] == "acknowledged" and ack["acknowledged_by"] == "user"
        assert (await other.messages.acknowledge(message["id"], message["response_sha256"])) == ack
        sender_run = (await engine.chat_run_store.list_runs(value["steps"][0]["session_id"]))[0]
        receiver_run = (await engine.chat_run_store.list_runs(message["receiver_session_id"]))[0]
        assert sender_run.status == receiver_run.status == "completed"
        assert sender_run.usage.input_tokens + sender_run.usage.output_tokens == 0
        assert receiver_run.usage.input_tokens + receiver_run.usage.output_tokens == 18
        assert (await other.jobs.get(value["steps"][0]["job_id"])).result.total_tokens == 18
        assert model_calls == ["ASK_B"]
        await other.tick()
        await service.tick()
        assert model_calls == ["ASK_B"]
        await other.messages.start_manual(**body)
        assert model_calls == ["ASK_B"]
    finally:
        await other.stop()


async def test_default_readonly_grant_blocks_real_write_even_in_bypass(peer_setup, monkeypatch):
    engine, service, profiles, _, directory = peer_setup
    attempt = directory / "forbidden.txt"

    async def stream(self, **kwargs):
        if not _tools(kwargs):
            yield _tool("file_write", {"path": str(attempt), "content": "not authorized"})
        else:
            assert "未分配" in _tools(kwargs)[0]["content"]
            yield StreamChunk(token="写入未获授权")
            yield _usage()

    monkeypatch.setattr(ModelRouter, "stream", stream)
    created = await service.messages.start_manual(
        source_session_id=engine._session.id,
        request_id="readonly-default",
        sender_agent_id=profiles[0].id,
        sender_agent_version=1,
        **{k: v for k, v in _request(profiles[1]).items() if k != "request_id"},
    )
    result = await _finish(service, created["id"])
    assert result["status"] == "error", result
    assert not attempt.exists()
    message = (await service.messages.list(created["id"]))[0]
    assert "file_write" not in message["tool_names"]
    assert message["outcome"] == "error"


async def test_nested_peer_engines_share_one_root_and_keep_distinct_receiving_runs(
    peer_setup, monkeypatch
):
    engine, service, profiles, children, _ = peer_setup
    calls = []

    async def stream(self, **kwargs):
        task = _task(kwargs)
        calls.append(task)
        if task == "ROOT_TASK" and not _tools(kwargs):
            yield _tool("personal_message", {"operation": "send", **_request(profiles[1])})
        elif task == "ASK_B" and not _tools(kwargs):
            yield _tool(
                "personal_message",
                {"operation": "send", **_request(profiles[2], content="ASK_C", request_id="ask-c")},
            )
        else:
            yield StreamChunk(token="嵌套结果825")
            yield _usage()

    monkeypatch.setattr(ModelRouter, "stream", stream)
    created = await service.create(**_plan(engine, profiles))
    result = await _finish(service, created["id"])
    assert result["status"] == "completed", result
    messages = await service.messages.list(created["id"])
    assert len(messages) == 2 and len(children) == 3
    assert all(message["outcome"] == "completed" for message in messages), messages
    root = next(m for m in messages if not m["parent_message_id"])
    nested = next(m for m in messages if m["parent_message_id"])
    assert nested["parent_message_id"] == root["id"]
    assert nested["source_session_id"] == root["receiver_session_id"]
    assert nested["source_run_id"] == root["receiver_run_id"]
    assert nested["root_job_id"] == root["root_job_id"] == result["steps"][0]["job_id"]
    assert len({m["receiver_session_id"] for m in messages}) == 2
    assert calls == ["ROOT_TASK", "ASK_B", "ASK_C", "ASK_B", "ROOT_TASK"]
    assert (await service.jobs.get(root["root_job_id"])).result.total_tokens == 90


async def test_message_api_requires_auth_strict_fields_and_keeps_receiver_readonly_after_db_loss(
    peer_setup, monkeypatch
):
    engine, service, profiles, _, _ = peer_setup

    async def stream(self, **kwargs):
        yield StreamChunk(token="API真实执行记录")
        yield _usage()

    monkeypatch.setattr(ModelRouter, "stream", stream)
    app = FastAPI()
    app.include_router(personal_router)
    app.include_router(sessions_router)
    app.state.engine, app.state.config = engine, engine.config
    engine.config.api.api_keys = ["peer-test-token"]
    body = dict(
        source_session_id=engine._session.id,
        request_id="http-manual",
        sender_agent_id=profiles[0].id,
        sender_agent_version=1,
        **{k: v for k, v in _request(profiles[1]).items() if k != "request_id"},
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post("/personal/messages", json=body)).status_code == 401
        client.headers["X-API-Key"] = "peer-test-token"
        for mutation in (
            {"root_owner_id": "forged"},
            {"sender_agent_version": True},
            {"recipient_agent_version": "1"},
            {"content": ""},
        ):
            response = await client.post("/personal/messages", json={**body, **mutation})
            assert response.status_code == 422, response.text
        assert not service.store.list(service.workspace_key)
        accepted = await client.post("/personal/messages", json=body)
        assert accepted.status_code == 201, accepted.text
        assert accepted.json()["message_request_id"] == body["request_id"]
        assert accepted.json()["request_id"] != body["request_id"]
        result = await _finish(service, accepted.json()["id"])
        assert result["status"] == "completed", result
        listed = await client.get(f"/personal/collaborations/{result['id']}/messages")
        message = listed.json()["messages"][0]
        fetched = (await client.get("/personal/messages/" + message["id"])).json()
        assert fetched["response"] == "API真实执行记录" and "dispatch_token" not in fetched
        ack = await client.post(
            "/personal/messages/" + message["id"] + "/ack",
            json={"expected_response_sha256": message["response_sha256"]},
        )
        assert ack.status_code == 200 and ack.json()["acknowledged_by"] == "user"
        receiver_id = message["receiver_session_id"]
        # Losing the auxiliary message log must never turn an execution history
        # into a writable daily specialist conversation.
        service.messages.store.db_path.rename(service.messages.store.db_path.with_suffix(".lost"))
        current = await client.get("/personal/current", params={"session_id": receiver_id})
        assert current.status_code == 200 and current.json()["read_only"] is True
        for method, suffix, content in (
            ("POST", "/messages", {"content": "改写执行历史", "stream": False}),
            ("PATCH", "", {"title": "覆盖"}),
            ("POST", "/archive", {}),
            ("DELETE", "", {}),
        ):
            response = await client.request(
                method, f"/sessions/{receiver_id}{suffix}", json=content
            )
            assert response.status_code == 409, response.text
        assert await engine.load_session(receiver_id)
        with pytest.raises(PersonalAgentError, match="执行记录"):
            runtime.begin_turn(engine)
        denied = await engine.execute_tool(
            ToolCall(id="readonly-after-loss", name="file_read", arguments={"path": "budget.csv"})
        )
        assert denied.status == "error" and "执行记录" in denied.content


async def test_dispatch_started_without_trusted_result_remains_unknown_and_never_restarts(
    peer_setup, monkeypatch
):
    engine, service, profiles, _, _ = peer_setup
    entered, release = asyncio.Event(), asyncio.Event()
    model_calls = []

    async def stream(self, **kwargs):
        model_calls.append(_task(kwargs))
        entered.set()
        await release.wait()
        yield StreamChunk(token="过期执行者结果")
        yield _usage()

    monkeypatch.setattr(ModelRouter, "stream", stream)
    created = await service.messages.start_manual(
        source_session_id=engine._session.id,
        request_id="lost-dispatch",
        sender_agent_id=profiles[0].id,
        sender_agent_version=1,
        **{k: v for k, v in _request(profiles[1]).items() if k != "request_id"},
    )
    await service.tick()
    await asyncio.wait_for(entered.wait(), timeout=10)
    value = await service.get(created["id"])
    job = await service.jobs.get(value["steps"][0]["job_id"])
    service.jobs._clock = lambda: (
        datetime.fromisoformat(job.claim_expires_at) + timedelta(seconds=1)
    )
    await service.jobs.mark_recovery_unknown(
        job.job_id,
        expected_request_sha256=job.request_sha256,
        expected_session_id_sha256=job.request.session_id_sha256,
        expected_claim_epoch=job.claim_epoch,
        expected_latest_receipt_sha256=job.latest_receipt.receipt_sha256,
    )
    message = (await service.messages.list(created["id"]))[0]
    assert message["state"] == "unknown"
    other = PersonalCollaborationService(engine)
    other.start = lambda: None
    try:
        await other.tick()
        assert model_calls == ["ASK_B"]
        assert (await other.jobs.get(job.job_id)).state is AgentJobState.UNKNOWN
    finally:
        release.set()
        await asyncio.gather(*tuple(service._active.values()), return_exceptions=True)
        await other.stop()
    assert (await service.get(created["id"]))["status"] == "unknown"
    assert model_calls == ["ASK_B"]


@pytest.mark.parametrize(
    "body", ["长回答" * 5000, '长回答"\\\n' * 2500], ids=["chinese", "escaped"]
)
async def test_replayed_send_returns_one_receiver_and_long_result_pages_without_truncating_storage(
    peer_setup, monkeypatch, body
):
    engine, service, profiles, children, _ = peer_setup
    received = []

    async def stream(self, **kwargs):
        task, tools = _task(kwargs), _tools(kwargs)
        if task == "ASK_B":
            received.append(task)
            yield StreamChunk(token=body)
            yield _usage()
        elif len(tools) < 2:
            if tools:
                first = json.loads(tools[0]["content"])
                assert first["response_truncated"] is True
                assert 0 < first["next_offset"] < 12000
                assert first["response"] == body[: first["next_offset"]]
                assert len(tools[0]["content"]) < 12000
            yield _tool(
                "personal_message",
                {"operation": "send", **_request(profiles[1])},
                "send-" + str(len(tools)),
            )
        else:
            first, replay = [json.loads(item["content"]) for item in tools[:2]]
            assert first == replay
            page = json.loads(tools[-1]["content"])
            end = page["response_offset"] + len(page["response"])
            assert page["response"] == body[page["response_offset"] : end]
            assert len(tools[-1]["content"]) < 12000
            if page["response_truncated"]:
                assert page["next_offset"] == end
                yield _tool(
                    "personal_message",
                    {
                        "operation": "get",
                        "message_id": page["id"],
                        "response_offset": end,
                        "response_limit": 12000,
                    },
                    "next-page-" + str(len(tools)),
                )
            else:
                assert end == len(body) and page["next_offset"] is None
                yield StreamChunk(token="完整读取结果")
                yield _usage()

    monkeypatch.setattr(ModelRouter, "stream", stream)
    created = await service.create(**_plan(engine, profiles))
    result = await _finish(service, created["id"])
    assert result["status"] == "completed", result
    messages = await service.messages.list(created["id"])
    assert len(messages) == 1 and received == ["ASK_B"] and len(children) == 2
    assert (await service.messages.get(messages[0]["id"]))["response"] == body


async def test_two_peer_calls_in_one_model_batch_are_serialized_with_one_capacity(
    peer_setup, monkeypatch
):
    engine, service, profiles, children, _ = peer_setup
    received = []
    active_models = 0
    maximum_active_models = 0

    async def stream(self, **kwargs):
        nonlocal active_models, maximum_active_models
        task, tools = _task(kwargs), _tools(kwargs)
        if task == "ROOT_TASK" and not tools:
            calls = {}
            for index, profile in enumerate(profiles[1:]):
                chunk = _tool(
                    "personal_message",
                    {
                        "operation": "send",
                        **_request(
                            profile, content="ASK_" + str(index), request_id="batch-" + str(index)
                        ),
                    },
                    "batch-call-" + str(index),
                )
                calls[index] = chunk.tool_call[0]
            yield StreamChunk(tool_call=calls, finish_reason="tool_calls")
        else:
            if task != "ROOT_TASK":
                active_models += 1
                maximum_active_models = max(maximum_active_models, active_models)
                capacity = await service.jobs.capacity_snapshot()
                assert capacity.active_jobs == 1 and capacity.waiting_jobs == 0
                await asyncio.sleep(0.02)
                received.append(task)
                active_models -= 1
            else:
                assert len(tools) == 2
                assert all(json.loads(item["content"])["state"] == "responded" for item in tools)
            yield StreamChunk(token="批次结果825")
            yield _usage()

    monkeypatch.setattr(ModelRouter, "stream", stream)
    created = await service.create(**_plan(engine, profiles))
    result = await _finish(service, created["id"])
    assert result["status"] == "completed", result
    messages = await service.messages.list(created["id"])
    assert len(messages) == 2 and all(m["outcome"] == "completed" for m in messages)
    assert received == ["ASK_0", "ASK_1"] and maximum_active_models == 1
    assert len(children) == 3


async def test_paused_intermediate_ancestor_stops_nested_receiver_before_real_write(
    peer_setup, monkeypatch
):
    engine, service, profiles, _, directory = peer_setup
    forbidden = directory / "after-ancestor-pause.txt"

    async def stream(self, **kwargs):
        task, tools = _task(kwargs), _tools(kwargs)
        if task == "ROOT_TASK" and not tools:
            yield _tool("personal_message", {"operation": "send", **_request(profiles[1])})
        elif task == "ASK_B" and not tools:
            yield _tool(
                "personal_message",
                {"operation": "send", **_request(profiles[2], content="ASK_C", request_id="ask-c")},
            )
        elif task == "ASK_C":
            engine.personal_agents.update(profiles[1].id, 1, status="paused")
            yield _tool("file_write", {"path": str(forbidden), "content": "must not exist"})
        else:
            yield StreamChunk(token="停止旧版本协作")
            yield _usage()

    monkeypatch.setattr(ModelRouter, "stream", stream)
    grants = [
        {"agent_id": p.id, "agent_version": 1, "tool_names": list(p.tool_names)} for p in profiles
    ]
    created = await service.create(**_plan(engine, profiles, peer_agents=grants))
    result = await _finish(service, created["id"])
    assert not forbidden.exists()
    messages = await service.messages.list(created["id"])
    nested = next(item for item in messages if item["parent_message_id"])
    assert nested["outcome"] != "completed"
    nested_run = (await engine.chat_run_store.list_runs(nested["receiver_session_id"]))[0]
    assert nested_run.status != "completed"
    assert result["status"] in {"completed", "error"}


@pytest.mark.parametrize(
    "mutation",
    [
        {"sender_agent_version": True},
        {"recipient_agent_version": True},
        {"peer_agents": []},
        {"source_session_id": "missing"},
    ],
)
async def test_manual_invalid_contract_creates_no_outbox_or_root(peer_setup, mutation):
    engine, service, profiles, _, _ = peer_setup
    body = dict(
        source_session_id=engine._session.id,
        request_id="invalid-manual",
        sender_agent_id=profiles[0].id,
        sender_agent_version=1,
        **{k: v for k, v in _request(profiles[1]).items() if k != "request_id"},
    )
    with pytest.raises(PersonalAgentError):
        await service.messages.start_manual(**{**body, **mutation})
    assert not service.store.list(service.workspace_key)


async def test_missing_peer_scope_denies_schema_and_real_tool_without_execution(peer_setup):
    engine, service, profiles, _, _ = peer_setup
    opened = await engine.personal_agents.open_session(profiles[0].id)
    assert await engine.load_session(opened["session_id"])
    runtime.begin_turn(engine)
    schemas = engine._get_openai_tools_cached()
    assert "personal_message" not in {s["function"]["name"] for s in schemas}
    result = await engine.execute_tool(
        ToolCall(
            id="unbound-peer",
            name="personal_message",
            arguments={"operation": "send", **_request(profiles[1])},
        )
    )
    assert result.status == "error"
    assert not service.store.list(service.workspace_key)


async def test_return_request_to_ancestor_uses_fresh_receiving_session(peer_setup, monkeypatch):
    engine, service, profiles, children, _ = peer_setup

    async def stream(self, **kwargs):
        task, tools = _task(kwargs), _tools(kwargs)
        if task == "ROOT_TASK" and not tools:
            yield _tool("personal_message", {"operation": "send", **_request(profiles[1])})
        elif task == "ASK_B" and not tools:
            yield _tool(
                "personal_message",
                {
                    "operation": "send",
                    **_request(profiles[0], content="FOLLOW_UP_A", request_id="follow-up-a"),
                },
            )
        else:
            yield StreamChunk(token="补充资料825")
            yield _usage()

    monkeypatch.setattr(ModelRouter, "stream", stream)
    created = await service.create(**_plan(engine, profiles))
    result = await _finish(service, created["id"])
    messages = await service.messages.list(created["id"])
    assert result["status"] == "completed" and all(m["outcome"] == "completed" for m in messages)
    assert len(messages) == 2 and len(children) == 3
    returned = next(m for m in messages if m["recipient_agent_id"] == profiles[0].id)
    assert returned["receiver_session_id"] != result["steps"][0]["session_id"]
    assert returned["receiver_session_id"] != engine._session.id


async def test_same_ancestor_question_is_rejected_instead_of_recursive_restart(
    peer_setup, monkeypatch
):
    engine, service, profiles, children, _ = peer_setup
    calls = []

    async def stream(self, **kwargs):
        task, tools = _task(kwargs), _tools(kwargs)
        calls.append(task)
        if task == "ROOT_TASK" and not tools:
            yield _tool("personal_message", {"operation": "send", **_request(profiles[1])})
        elif task == "ASK_B" and not tools:
            yield _tool(
                "personal_message",
                {
                    "operation": "send",
                    **_request(profiles[0], content="FOLLOW_UP_A", request_id="follow-up-a"),
                },
            )
        elif task == "FOLLOW_UP_A" and not tools:
            yield _tool(
                "personal_message",
                {"operation": "send", **_request(profiles[1], request_id="duplicate-ancestor")},
            )
        else:
            yield StreamChunk(token="已停止重复问题")
            yield _usage()

    monkeypatch.setattr(ModelRouter, "stream", stream)
    created = await service.create(**_plan(engine, profiles))
    await _finish(service, created["id"])
    messages = await service.messages.list(created["id"])
    assert len(messages) == 2 and len(children) == 3
    assert calls.count("ASK_B") == 2
    returned = next(m for m in messages if m["recipient_agent_id"] == profiles[0].id)
    saved = await engine.session_store.load(returned["receiver_session_id"])
    failures = [m for m in saved.messages if m["role"] == "tool"]
    assert len(failures) == 1 and "重复互询" in failures[0]["content"]


async def test_receiver_question_binds_real_peer_session_and_user_answer(peer_setup, monkeypatch):
    engine, service, profiles, _, _ = peer_setup
    receiver = engine.personal_agents.update(profiles[1].id, 1, tool_names=["request_user_input"])

    async def stream(self, **kwargs):
        if not _tools(kwargs):
            yield _tool(
                "request_user_input",
                {
                    "header": "统计口径",
                    "question": "选择哪个月份？",
                    "options": [
                        {"value": "all", "label": "全部", "description": "全部月份"},
                        {"value": "recent", "label": "本月", "description": "最近月份"},
                    ],
                },
                "ask-real-user",
            )
        else:
            assert "recent" in _tools(kwargs)[0]["content"]
            yield StreamChunk(token="按最近月份统计")
            yield _usage()

    monkeypatch.setattr(ModelRouter, "stream", stream)
    created = await service.messages.start_manual(
        source_session_id=engine._session.id,
        request_id="peer-question",
        sender_agent_id=profiles[0].id,
        sender_agent_version=1,
        **{k: v for k, v in _request(receiver).items() if k != "request_id"},
    )
    await service.tick()
    for _ in range(1000):
        questions = [e for e in service._events(created) if e["kind"] == "question_requested"]
        if questions:
            break
        await asyncio.sleep(0.02)
    assert len(questions) == 1, await service.get(created["id"])
    event = questions[0]
    message = (await service.messages.list(created["id"]))[0]
    assert message["state"] == "processing"
    assert event["step_id"] == "dispatch"
    assert event["data"]["session_id"] == message["receiver_session_id"]
    assert event["data"]["peer_message_id"] == message["id"]
    assert event["data"]["actor_id"] == receiver.id
    with pytest.raises(PersonalAgentError):
        await service.reply(
            created["id"], event["data"]["request_id"], {"kind": "option", "value": "forged-choice"}
        )
    await service.reply(
        created["id"], event["data"]["request_id"], {"kind": "option", "value": "recent"}
    )
    result = await _finish(service, created["id"])
    assert result["status"] == "completed", result
    assert result["steps"][0]["response"] == "按最近月份统计"


async def test_receiver_sensitive_tool_uses_real_session_in_parent_permission_broker(
    peer_setup, monkeypatch
):
    engine, service, profiles, _, directory = peer_setup
    engine.config.safety.permission_mode = "moderate"
    receiver = engine.personal_agents.update(profiles[1].id, 1, tool_names=["bash_run"])
    prompts = []
    side_effect = directory / "must-not-run.txt"

    async def confirm(payload):
        prompts.append(payload)
        return "deny"

    engine.set_permission_confirmer(confirm)

    async def stream(self, **kwargs):
        if not _tools(kwargs):
            yield _tool("bash_run", {"command": f"echo forbidden > '{side_effect}'"}, "shell-peer")
        else:
            yield StreamChunk(token="执行未获同意")
            yield _usage()

    monkeypatch.setattr(ModelRouter, "stream", stream)
    created = await service.messages.start_manual(
        source_session_id=engine._session.id,
        request_id="peer-permission",
        sender_agent_id=profiles[0].id,
        sender_agent_version=1,
        peer_agents=[
            {"agent_id": receiver.id, "agent_version": receiver.version, "tool_names": ["bash_run"]}
        ],
        **{k: v for k, v in _request(receiver).items() if k != "request_id"},
    )
    result = await _finish(service, created["id"])
    message = (await service.messages.list(created["id"]))[0]
    assert len(prompts) == 1
    assert prompts[0]["session_id"] == message["receiver_session_id"]
    assert prompts[0]["session_id"] != engine._session.id
    events = [e for e in result["events"] if e["kind"] == "permission_requested"]
    assert len(events) == 1 and events[0]["data"]["peer_message_id"] == message["id"]
    assert events[0]["data"]["actor_id"] == receiver.id
    assert not side_effect.exists() and message["outcome"] == "error"


async def test_unclaimed_outbox_key_outage_is_503_through_get_and_list(peer_setup, monkeypatch):
    engine, service, profiles, _, _ = peer_setup
    entered, hold = asyncio.Event(), asyncio.Event()

    async def admitted_not_started(*args):
        entered.set()
        await hold.wait()

    created = await service.messages.start_manual(
        source_session_id=engine._session.id,
        request_id="unclaimed-key-outage",
        sender_agent_id=profiles[0].id,
        sender_agent_version=1,
        **{k: v for k, v in _request(profiles[1]).items() if k != "request_id"},
    )
    message = (await service.messages.list(created["id"]))[0]
    monkeypatch.setattr(service, "_execute", admitted_not_started)
    await service.tick()
    await asyncio.wait_for(entered.wait(), timeout=5)
    raw = service.messages.store.get(message["id"], service.workspace_key)
    assert raw["dispatch_token"] == ""
    monkeypatch.setattr(service.jobs, "_key_provider", lambda: b"invalid key")
    app = FastAPI()
    app.include_router(personal_router)
    app.state.engine, app.state.config = engine, engine.config
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for path in (
            "/personal/messages/" + message["id"],
            f"/personal/collaborations/{created['id']}/messages",
        ):
            response = await client.get(path)
            assert response.status_code == 503, response.text
            assert response.json()["code"] == "storage_error"
    for operation in (
        lambda: service.messages.get(message["id"]),
        lambda: service.messages.list(created["id"]),
    ):
        with pytest.raises(PersonalAgentError) as failure:
            await operation()
        assert failure.value.code == "storage_error"


async def _listing_page(engine, operation, key, **arguments):
    result = await engine.execute_tool(
        ToolCall(
            id="listing-" + operation + "-" + str(arguments.get("list_offset", 0)),
            name="personal_message",
            arguments={"operation": operation, **arguments},
        )
    )
    assert result.status == "success", result.content
    assert len(result.content) < 12000
    messages = [{"role": "tool", "tool_call_id": "listing-check", "content": result.content}]
    preserved, archives = engine._compactor.offload_large_tool_results(messages)
    assert not archives and preserved == messages
    page = json.loads(preserved[0]["content"])
    assert isinstance(page[key], list)
    return page


async def _read_all_listing(engine, operation, key):
    # The no-argument operation remains valid for existing callers. The requested
    # page size is only a ceiling: JSON escaping can make its actual length lower.
    page = await _listing_page(engine, operation, key)
    assert page["offset"] == 0 and 0 < len(page[key]) <= 20
    entries, page_sizes, offsets = [], [], []
    total = page["total"]
    while True:
        assert page["total"] == total
        assert page["offset"] == len(entries)
        offsets.append(page["offset"])
        page_sizes.append(len(page[key]))
        entries.extend(page[key])
        if page["next_offset"] is None:
            break
        assert page["next_offset"] == len(entries) > page["offset"]
        page = await _listing_page(
            engine, operation, key, list_offset=page["next_offset"], list_limit=100
        )
    assert len(entries) == total and len(offsets) > 1
    beyond = await _listing_page(engine, operation, key, list_offset=total + 100, list_limit=100)
    assert beyond[key] == [] and beyond["offset"] == total + 100
    assert beyond["next_offset"] is None and beyond["total"] == total
    return entries, page_sizes


async def test_large_authorized_directory_pages_all_ids_without_compactor_archiving(
    peer_setup, monkeypatch
):
    engine, service, profiles, children, _ = peer_setup
    # Use the actual grant maximum, real persisted profile revisions, and text
    # whose quotes/backslashes/newlines expand inside the JSON envelope.
    for index in range(125):
        profiles.append(
            engine.personal_agents.create(
                name="目录专员-" + str(index),
                description=('说明"\\\n' * 100),
                instructions="只处理用户显式指定资料",
                request_id="large-directory-" + str(index),
                tool_names=["file_read"],
                model_tier="fast",
            )
        )
    seen = []
    sizes = []

    async def stream(self, **kwargs):
        listed, lengths = await _read_all_listing(children[0], "peers", "peers")
        assert all("tool_names" not in item for item in listed)
        seen.extend(item["agent_id"] for item in listed)
        sizes.extend(lengths)
        yield StreamChunk(token="目录分页已核对")
        yield _usage()

    monkeypatch.setattr(ModelRouter, "stream", stream)
    created = await service.create(**_plan(engine, profiles))
    await service.tick()
    await asyncio.wait_for(asyncio.gather(*tuple(service._active.values())), timeout=45)
    result = await service.get(created["id"])
    assert result["status"] == "completed", result
    assert len(seen) == len(set(seen)) == 128
    assert set(seen) == {profile.id for profile in profiles}
    assert sizes[0] < 20


async def test_large_inbox_pages_persisted_message_summaries_without_missing_or_duplicate_ids(
    peer_setup, monkeypatch
):
    engine, service, profiles, children, _ = peer_setup
    created_ids = []
    seen = []

    async def stream(self, **kwargs):
        child = children[0]
        bound = child._personal_peer_context
        for index in range(32):
            message = service.messages.prepare(
                bound.root.plan,
                bound.root.step,
                source_session_id=child._session.id,
                sender_id=profiles[0].id,
                sender_version=profiles[0].version,
                request={
                    **_request(profiles[1], request_id="large-inbox-" + str(index)),
                    "content": '资料"\\\n' * 3200,
                    "context": "显式共享上下文" * 2000,
                },
            )
            created_ids.append(message["id"])
        listed, sizes = await _read_all_listing(child, "inbox", "messages")
        seen.extend(item["id"] for item in listed)
        assert sizes[0] < 20
        assert all(item["content_truncated"] for item in listed)
        assert all(len(item["content"]) == 500 for item in listed)
        assert all(
            "context" not in item and "tool_names" not in item and "run_receipt" not in item
            for item in listed
        )
        yield StreamChunk(token="消息分页已核对")
        yield _usage()

    monkeypatch.setattr(ModelRouter, "stream", stream)
    created = await service.create(**_plan(engine, profiles))
    await service.tick()
    await asyncio.wait_for(asyncio.gather(*tuple(service._active.values())), timeout=45)
    result = await service.get(created["id"])
    assert result["status"] == "completed", result
    assert len(created_ids) == len(seen) == len(set(seen)) == 32
    assert set(seen) == set(created_ids)
