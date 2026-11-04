"""Real SQLite admission/recovery contracts and isolated Engine integration."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from naumi_agent.api.routes.messages import router as messages_router
from naumi_agent.api.routes.personal import router
from naumi_agent.config.settings import AppConfig
from naumi_agent.daemons.agent_jobs import AgentJobPayload, AgentJobState, AgentJobStore
from naumi_agent.daemons.agent_worker_contract import issue_agent_worker_request
from naumi_agent.memory.session import SessionStore
from naumi_agent.model.router import ModelRouter, StreamChunk, TokenUsage
from naumi_agent.orchestrator.engine import AgentEngine, AgentResult, AgentUsage
from naumi_agent.personal import runtime
from naumi_agent.personal.collaboration import PersonalCollaborationService
from naumi_agent.personal.commands import parse_assistants_command, run_assistants_command
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.tools import PersonalAgentsTool
from naumi_agent.tools.base import ToolCall, ToolExecutionError

pytestmark = pytest.mark.usefixtures("runtime_payload_key")


class ControlledChild:
    """Unit-test executor; real model and tool acceptance is a separate evidence tier."""

    def __init__(self, config, step, records, *, gate=None, status="completed", permission=False):
        self.session_store = SessionStore(config.memory)
        self.step, self.records, self.gate = step, records, gate
        self.status, self.permission = status, permission
        self.chat_run_store = SimpleNamespace(list_runs=self.runs)

    async def runs(self, *args, **kwargs):
        return []

    async def load_session(self, session_id):
        self.session = await self.session_store.load(session_id)
        self._session = self.session
        return self.session is not None

    def set_permission_confirmer(self, callback):
        self.confirm = callback

    def set_user_interaction_handler(self, callback):
        self.ask = callback

    async def run_streaming(self, task, callback, turn_context=""):
        self.records.append(
            {"step": self.step, "task": task, "context": turn_context, "child": self}
        )
        if self.gate:
            await self.gate.wait()
        await self._personal_execution_authorizer()
        if self.permission:
            decision = await self.confirm(
                {
                    "call_id": "child-call",
                    "session_id": self.session.id,
                    "tool_name": "file_write",
                    "arguments": {"path": "result.txt"},
                    "reason": "写入文件需要确认",
                }
            )
            assert decision in {"allow", "deny", "allow_once"}
        self.session.add_message("user", task)
        self.session.add_message("assistant", "结果:" + self.step["id"])
        await self.session_store.save(self.session)
        await callback("tool_start", {"name": "file_read", "call_id": "unit-call"})
        return AgentResult(
            status=self.status,
            response="结果:" + self.step["id"],
            usage=AgentUsage(total_input_tokens=7, total_output_tokens=3, turns=1),
        )

    async def shutdown(self):
        await self.session_store.close()


@pytest.fixture
async def setup(tmp_path):
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
            "max_turns": 3,
            "max_parallel_agents": 3,
            "allowed_dirs": [str(tmp_path)],
        },
    )
    engine = AgentEngine(config)
    try:
        await engine.get_or_create_session()
        engine._session.add_message("user", "MAIN_PRIVATE_DO_NOT_SHARE")
        await engine._save_session()
        records = []
        service = engine.personal_collaborations
        service.start = lambda: None
        service._child_factory = lambda step, job: ControlledChild(config, step, records)
        profiles = [
            engine.personal_agents.create(
                name=name,
                instructions="只读取显式共享资料",
                request_id="profile-" + str(index),
                model_tier="fast",
                tool_names=["file_read"],
            )
            for index, name in enumerate(("资料专员", "复核专员"))
        ]
        yield engine, service, profiles, records
    finally:
        await engine.shutdown()


def plan(engine, profiles, **updates):
    return {
        "source_session_id": engine._session.id,
        "request_id": "collaboration-one",
        "title": "资料复核",
        "context": "EXPLICIT_SHARED_CONTEXT",
        "steps": [
            {
                "id": "read",
                "agent_id": profiles[0].id,
                "agent_version": 1,
                "task": "读取指定资料",
                "depends_on": [],
            },
            {
                "id": "review",
                "agent_id": profiles[1].id,
                "agent_version": 1,
                "task": "复核前置结果",
                "depends_on": ["read"],
            },
        ],
        **updates,
    }


async def finish(service, identity):
    for _ in range(15):
        await service.tick()
        if service._active:
            await asyncio.gather(*tuple(service._active.values()), return_exceptions=True)
        value = await service.get(identity)
        if value["status"] in {"completed", "error", "cancelled", "unknown"}:
            return value
    raise AssertionError("Collaboration did not settle")


async def test_dependency_handoff_real_storage_and_private_session_separation(setup):
    engine, service, profiles, records = setup
    main_id, before = engine._session.id, list(engine._messages)
    daily = await engine.personal_agents.open_session(profiles[0].id)
    first = await service.create(**plan(engine, profiles))
    replay = await service.create(**plan(engine, profiles))
    assert first["id"] == replay["id"]
    assert first["steps"][0]["session_id"] == replay["steps"][0]["session_id"]
    result = await finish(service, first["id"])
    assert result["status"] == "completed"
    assert [record["step"]["id"] for record in records] == ["read", "review"]
    upstream = json.loads(records[1]["context"].split("\n", 1)[1])["upstream"][0]
    assert upstream["response"] == "结果:read"
    assert upstream["result_sha256"] == result["steps"][0]["result_sha256"]
    assert upstream["agent_id"] == profiles[0].id and upstream["agent_version"] == 1
    assert all("MAIN_PRIVATE_DO_NOT_SHARE" not in r["context"] for r in records)
    assert engine._session.id == main_id and engine._messages == before
    assert (await engine.personal_agents.open_session(profiles[0].id)) == daily
    for step in result["steps"]:
        saved = await engine.session_store.load(step["session_id"])
        assert saved.personal_agent_id == step["agent_id"] and len(saved.messages) == 2
        assert engine.personal_agents.current(saved.id).id == step["agent_id"]
        job = await service.jobs.get(step["job_id"])
        assert job.result.total_tokens == 10 and job.state is AgentJobState.COMPLETED
    assert len({s["session_id"] for s in result["steps"]} | {main_id, daily["session_id"]}) == 4
    assert any(e["kind"] == "handoff" for e in result["events"])
    assert any(e["kind"] == "tool_start" for e in result["events"])


async def test_restart_and_two_coordinators_cannot_execute_twice(setup):
    engine, service, profiles, records = setup
    created = await service.create(
        **plan(engine, profiles, steps=plan(engine, profiles)["steps"][:1])
    )
    other = PersonalCollaborationService(engine, child_factory=service._child_factory)
    other.start = lambda: None
    try:
        await asyncio.gather(service.tick(), other.tick())
        await asyncio.gather(*tuple(service._active.values()), *tuple(other._active.values()))
        value = await other.get(created["id"])
        assert value["status"] == "completed" and len(records) == 1
        await other.tick()
        await service.tick()
        assert len(records) == 1
        assert (await other.get(created["id"]))["steps"][0]["response"] == "结果:read"
    finally:
        await other.stop()


async def test_crash_between_journal_prepare_and_job_admission_reuses_original_policy(
    setup, monkeypatch
):
    engine, service, profiles, records = setup
    created = await service.create(
        **plan(engine, profiles, steps=plan(engine, profiles)["steps"][:1])
    )
    original = service.jobs.admit_for_capacity

    async def unavailable(**kwargs):
        raise OSError("controlled admission outage")

    monkeypatch.setattr(service.jobs, "admit_for_capacity", unavailable)
    await service.tick()
    events = service._events(created)
    request = next(e["data"]["request"] for e in events if e["kind"] == "dispatch_prepared")
    assert not records and request["permission_mode"] == "bypass"
    engine.config.safety.permission_mode = "strict"
    monkeypatch.setattr(service.jobs, "admit_for_capacity", original)
    value = await finish(service, created["id"])
    job = await service.jobs.get(value["steps"][0]["job_id"])
    assert value["status"] == "error" and not records
    assert job.request.request_sha256 == request["request_sha256"]
    assert job.request.permission_mode == "bypass"


async def test_cancel_before_dispatch_and_during_live_execution(setup):
    engine, service, profiles, records = setup
    first = await service.create(**plan(engine, profiles))
    value = await service.cancel(first["id"])
    await service.tick()
    assert value["status"] == "cancelled" and not records
    gate = asyncio.Event()
    service._child_factory = lambda step, job: ControlledChild(
        engine.config, step, records, gate=gate
    )
    second = await service.create(**plan(engine, profiles, request_id="second"))
    await service.tick()
    for _ in range(50):
        if records:
            break
        await asyncio.sleep(0.01)
    assert records
    pending = await service.cancel(second["id"])
    assert pending["cancel_requested"]
    value = await finish(service, second["id"])
    assert value["status"] == "cancelled"
    assert len(records) == 1 and value["steps"][0]["status"] == "cancelled"


@pytest.mark.parametrize("change", ["paused", "revision"])
async def test_role_change_prevents_old_dispatch_and_blocks_dependencies(setup, change):
    engine, service, profiles, records = setup
    created = await service.create(**plan(engine, profiles))
    fields = {"status": "paused"} if change == "paused" else {"instructions": "新职责"}
    engine.personal_agents.update(profiles[0].id, 1, **fields)
    replay = await service.create(**plan(engine, profiles))
    assert replay["id"] == created["id"]
    value = await finish(service, created["id"])
    assert value["steps"][0]["status"] == "error"
    assert value["steps"][1]["status"] == "blocked" and not records


async def test_expired_running_keeps_unknown_and_never_replays(setup):
    engine, service, profiles, records = setup
    value = await service.create(
        **plan(engine, profiles, steps=plan(engine, profiles)["steps"][:1])
    )
    step = value["steps"][0]
    now = datetime.now(UTC)
    clock = [now - timedelta(minutes=5)]
    service.jobs = AgentJobStore(service.jobs.db_path, clock=lambda: clock[0])
    context = service._context(value, step, {step["id"]: step})
    request = issue_agent_worker_request(
        task_id=step["task_id"],
        session_id=step["session_id"],
        agent_name=profiles[0].id,
        task=step["task"],
        context=context,
        tool_scope=["file_read"],
        permission_mode="bypass",
        model_tier="fast",
        max_turns=3,
        max_budget_usd=None,
        timeout_seconds=600,
        message_topic=value["id"],
        issued_at=value["created_at"],
    )
    from dataclasses import asdict

    service._event(
        value,
        "prepare:read",
        "dispatch_prepared",
        step_id="read",
        data={"request": {**asdict(request), "tool_scope": list(request.tool_scope)}},
    )
    job = await service.jobs.admit(
        request=request,
        payload=AgentJobPayload(
            task_id=step["task_id"],
            session_id=step["session_id"],
            task=step["task"],
            context=context,
            message_topic=value["id"],
        ),
    )
    claim = await service.jobs.claim(job.job_id, owner_id="dead-owner", lease_seconds=90)
    await service.jobs.mark_running(
        job.job_id, owner_id="dead-owner", claim_epoch=claim.job.claim_epoch
    )
    clock[0] = now
    await service.tick()
    recovered = await service.get(value["id"])
    assert recovered["status"] == "unknown" and not records
    assert "不明" in recovered["steps"][0]["error"]
    await service.tick()
    assert not records


async def test_child_permissions_keep_real_identity_and_ordered_journal(setup):
    engine, service, profiles, records = setup
    decisions = []

    async def confirm(payload):
        decisions.append(payload)
        return "allow_once"

    engine.set_permission_confirmer(confirm)
    service._child_factory = lambda step, job: ControlledChild(
        engine.config,
        step,
        records,
        permission=True,
    )
    created = await service.create(
        **plan(engine, profiles, steps=plan(engine, profiles)["steps"][:1])
    )
    value = await finish(service, created["id"])
    assert decisions[0]["session_id"] == value["steps"][0]["session_id"]
    assert decisions[0]["session_id"] != engine._session.id
    events = [e for e in value["events"] if e["kind"].startswith("permission_")]
    assert [e["kind"] for e in events] == ["permission_requested", "permission_resolved"]
    assert events[0]["data"]["call_id"] == events[1]["data"]["call_id"]


async def test_execution_ownership_does_not_replace_daily_binding_and_is_immutable(setup):
    engine, service, profiles, _ = setup
    created = await service.create(**plan(engine, profiles))
    step = created["steps"][0]
    daily = await engine.personal_agents.open_session(profiles[0].id)
    engine.personal_agents.store.register_execution_session(
        profiles[0].id,
        service.workspace_key,
        step["session_id"],
        session_db_key=engine.personal_agents.session_db_key,
    )
    assert (await engine.personal_agents.open_session(profiles[0].id)) == daily
    with pytest.raises(PersonalAgentError, match="另一位"):
        engine.personal_agents.store.register_execution_session(
            profiles[1].id,
            service.workspace_key,
            step["session_id"],
            session_db_key=engine.personal_agents.session_db_key,
        )
    with pytest.raises(PersonalAgentError, match="执行记录"):
        service.assert_writable(step["session_id"])
    service.assert_writable(step["session_id"], task_id=step["task_id"])


async def test_tool_cli_and_authenticated_api_share_durable_service(setup):
    engine, service, profiles, _ = setup
    tool = PersonalAgentsTool(engine.personal_agents)
    payload = plan(engine, profiles)
    result = json.loads(await tool.execute(operation="collaborate", **payload))
    listed = json.loads(await tool.execute(operation="collaborations"))
    assert listed["collaborations"][0]["id"] == result["id"]
    assert parse_assistants_command("collaboration " + result["id"])["operation"] == "collaboration"
    assert result["id"] in await run_assistants_command(engine, "collaborations")
    with pytest.raises(ToolExecutionError):
        await tool.execute(operation="collaborate", **{**payload, "source_session_id": "other"})
    app = FastAPI()
    app.include_router(router)
    app.include_router(messages_router)
    app.state.engine, app.state.config = engine, engine.config
    engine.config.api.api_keys = ["local-test-token"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/personal/collaborations")).status_code == 401
        client.headers["X-API-Key"] = "local-test-token"
        response = await client.post("/personal/collaborations", json=payload)
        assert response.status_code == 201 and response.json()["id"] == result["id"]
        invalid = await client.post("/personal/collaborations", json={**payload, "extra": True})
        assert invalid.status_code == 422 and invalid.json()["code"] == "invalid_input"
        wrong = await client.post(
            "/personal/collaborations", json={**payload, "context": "changed"}
        )
        assert wrong.status_code == 409
        value = await finish(service, result["id"])
        session_id = value["steps"][0]["session_id"]
        current = (await client.get("/personal/current", params={"session_id": session_id})).json()
        assert current["read_only"] and current["collaboration_id"] == result["id"]
        for method, suffix, body in (
            ("POST", "/messages", {"content": "不要更改记录", "stream": False}),
            ("PATCH", "", {"title": "改写记录"}),
            ("POST", "/archive", {}),
            ("DELETE", "", {}),
        ):
            response = await client.request(method, f"/sessions/{session_id}{suffix}", json=body)
            assert response.status_code == 409, response.text
        assert (await client.get("/personal/collaborations/" + result["id"])).json()[
            "status"
        ] == "completed"


async def test_real_isolated_engine_preserves_parent_globals_and_records_chat_run(
    setup, monkeypatch
):
    engine, service, profiles, _ = setup
    from naumi_agent.tools import analysis, pursuit

    parent_router = analysis._global_router
    parent_manager = analysis._global_subagent_manager
    parent_pursuit = pursuit._global_pursuit_loop

    async def stream(self, **kwargs):
        assert "MAIN_PRIVATE_DO_NOT_SHARE" not in json.dumps(kwargs, ensure_ascii=False)
        yield StreamChunk(token="真实引擎受控模型答复")
        yield StreamChunk(finish_reason="stop", usage=TokenUsage(input_tokens=10, output_tokens=8))

    monkeypatch.setattr(ModelRouter, "stream", stream)
    service._child_factory = service._make_child
    created = await service.create(
        **plan(engine, profiles, steps=plan(engine, profiles)["steps"][:1])
    )
    value = await finish(service, created["id"])
    assert value["status"] == "completed", value
    step = value["steps"][0]
    assert step["run_id"] and "真实引擎" in step["response"]
    record = await engine.chat_run_store.get_run(step["session_id"], step["run_id"])
    assert record is not None and record.status == "completed"
    assert analysis._global_router is parent_router
    assert analysis._global_subagent_manager is parent_manager
    assert pursuit._global_pursuit_loop is parent_pursuit
    assert await engine.load_session(step["session_id"])
    with pytest.raises(PersonalAgentError, match="执行记录"):
        runtime.begin_turn(engine)
    denied = await engine.execute_tool(
        ToolCall(id="unexpected", name="file_read", arguments={"path": "x"})
    )
    assert denied.status == "error" and "执行记录" in denied.content


async def test_parallel_capacity_and_fan_in_are_enforced_across_coordinators(setup):
    engine, service, profiles, records = setup
    gate = asyncio.Event()
    service._child_factory = lambda step, job: ControlledChild(
        engine.config,
        step,
        records,
        gate=gate,
    )
    steps = [
        {
            "id": f"part_{index}",
            "agent_id": profiles[index % 2].id,
            "agent_version": 1,
            "task": f"并行任务{index}",
            "depends_on": [],
        }
        for index in range(4)
    ]
    steps.append(
        {
            "id": "summary",
            "agent_id": profiles[1].id,
            "agent_version": 1,
            "task": "汇总四项结果",
            "depends_on": [s["id"] for s in steps],
        }
    )
    created = await service.create(**plan(engine, profiles, steps=steps))
    other = PersonalCollaborationService(engine, child_factory=service._child_factory)
    other.start = lambda: None
    try:
        await asyncio.gather(service.tick(), other.tick())
        assert len(service._active) + len(other._active) == 3
        gate.set()
        await asyncio.gather(*tuple(service._active.values()), *tuple(other._active.values()))
        value = await finish(service, created["id"])
        assert value["status"] == "completed" and len(records) == 5
        summary = next(r for r in records if r["step"]["id"] == "summary")
        assert len(json.loads(summary["context"].split("\n", 1)[1])["upstream"]) == 4
    finally:
        gate.set()
        await other.stop()


async def test_lost_execution_fence_prevents_more_work_and_does_not_overwrite_unknown(setup):
    engine, service, profiles, records = setup
    gate = asyncio.Event()
    service._child_factory = lambda step, job: ControlledChild(
        engine.config,
        step,
        records,
        gate=gate,
    )
    created = await service.create(
        **plan(engine, profiles, steps=plan(engine, profiles)["steps"][:1])
    )
    await service.tick()
    for _ in range(100):
        if records:
            break
        await asyncio.sleep(0.01)
    step = (await service.get(created["id"]))["steps"][0]
    job = await service.jobs.get(step["job_id"])
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
    with pytest.raises(PersonalAgentError, match="租约"):
        await records[0]["child"]._personal_execution_authorizer()
    gate.set()
    await asyncio.gather(*tuple(service._active.values()), return_exceptions=True)
    value = await service.get(created["id"])
    assert value["status"] == "unknown" and not value["steps"][0]["response"]
    assert not any(e["kind"] == "result" for e in value["events"])


async def test_durable_clarification_validates_exact_question_and_idempotent_answer(setup):
    engine, service, profiles, records = setup
    answers = []

    class AskingChild(ControlledChild):
        async def run_streaming(self, *args, **kwargs):
            answers.append(
                await self.ask(
                    {
                        "header": "选择范围",
                        "question": "这次采用哪个统计口径？",
                        "options": [
                            {"value": "all", "label": "全部资料", "description": "全部月份"},
                            {"value": "recent", "label": "最近资料", "description": "最近一个月"},
                        ],
                        "allow_custom": True,
                    }
                )
            )
            return await super().run_streaming(*args, **kwargs)

    service._child_factory = lambda step, job: AskingChild(engine.config, step, records)
    created = await service.create(
        **plan(engine, profiles, steps=plan(engine, profiles)["steps"][:1])
    )
    await service.tick()
    for _ in range(100):
        questions = [e for e in service._events(created) if e["kind"] == "question_requested"]
        if questions:
            break
        await asyncio.sleep(0.01)
    request_id = questions[0]["data"]["request_id"]
    with pytest.raises(PersonalAgentError, match="不属于"):
        await service.reply(created["id"], request_id, {"kind": "option", "value": "wrong"})
    with pytest.raises(PersonalAgentError, match="不存在"):
        await service.reply(created["id"], "ask-" + "a" * 32, {"kind": "option", "value": "all"})
    response = {"kind": "custom", "custom_text": "以用户提供的两个月份为准"}
    first = await service.reply(created["id"], request_id, response)
    assert await service.reply(created["id"], request_id, response) == first
    with pytest.raises(PersonalAgentError, match="已有回答"):
        await service.reply(created["id"], request_id, {"kind": "option", "value": "all"})
    value = await finish(service, created["id"])
    assert value["status"] == "completed"
    assert answers[0]["custom_text"] == response["custom_text"]
    assert sum(e["kind"] == "question_answered" for e in value["events"]) == 1


@pytest.mark.parametrize("source", ["missing", "other_workspace"])
async def test_collaboration_source_session_is_checked_before_any_dispatch(setup, tmp_path, source):
    engine, service, profiles, records = setup
    if source == "missing":
        source_id = "missing"
    else:
        session = await engine.create_session(title="其他工作区")
        session.workspace_root = str(tmp_path / "other")
        await engine.session_store.save(session)
        source_id = session.id
    with pytest.raises(PersonalAgentError):
        await service.create(**plan(engine, profiles, source_session_id=source_id))
    assert not records and service.store.list(service.workspace_key) == []


async def test_question_timeout_does_not_choose_an_option_for_user(setup):
    engine, service, profiles, records = setup

    class ExpiringChild(ControlledChild):
        async def run_streaming(self, *args, **kwargs):
            await self.ask(
                {
                    "header": "确认口径",
                    "question": "采用哪个月份？",
                    "timeout_seconds": 3,
                    "options": [{"value": "a", "label": "一月"}, {"value": "b", "label": "二月"}],
                }
            )
            raise AssertionError("Expired questions must not continue")

    service._child_factory = lambda step, job: ExpiringChild(engine.config, step, records)
    created = await service.create(
        **plan(engine, profiles, steps=plan(engine, profiles)["steps"][:1])
    )
    value = await finish(service, created["id"])
    assert value["status"] == "error" and not records
    assert any(e["kind"] == "question_expired" for e in value["events"])
    request_id = next(
        e["data"]["request_id"] for e in value["events"] if e["kind"] == "question_requested"
    )
    with pytest.raises(PersonalAgentError, match="已结束"):
        await service.reply(created["id"], request_id, {"kind": "option", "value": "a"})


async def test_oversized_dependency_context_has_visible_pre_admission_failure(setup, monkeypatch):
    engine, service, profiles, records = setup
    created = await service.create(**plan(engine, profiles))
    monkeypatch.setattr(service, "_context", lambda *args: "a" * (16 * 1024**2 + 1))
    value = await finish(service, created["id"])
    assert value["status"] == "error" and not records
    assert all(step["status"] == "blocked" for step in value["steps"])
    assert value["steps"][0]["job_id"] == ""
    assert "超过" in value["steps"][0]["error"]


async def test_zero_waiter_capacity_retains_plan_without_over_admitting_or_deadlocking(setup):
    engine, service, profiles, records = setup
    engine.config.safety.max_parallel_agents = 1
    engine.config.safety.max_queued_agents = 0
    gate = asyncio.Event()
    service._child_factory = lambda step, job: ControlledChild(
        engine.config,
        step,
        records,
        gate=gate,
    )
    steps = [
        {
            "id": f"part{n}",
            "agent_id": profiles[n].id,
            "agent_version": 1,
            "task": "各自读取资料",
            "depends_on": [],
        }
        for n in range(2)
    ]
    created = await service.create(**plan(engine, profiles, steps=steps))
    await service.tick()
    assert len(service._active) == 1
    waiting = await service.get(created["id"])
    assert sum(bool(s["job_id"]) for s in waiting["steps"]) == 1
    assert waiting["steps"][1]["status"] == "waiting"
    gate.set()
    value = await finish(service, created["id"])
    assert value["status"] == "completed" and len(records) == 2


async def test_named_user_created_specialists_are_not_replaced_with_temporary_planner_agents(setup):
    engine, _, profiles, _ = setup
    assert runtime.use_direct_runtime(engine, f"请{profiles[0].name}和{profiles[1].name}接力")
    assert runtime.use_direct_runtime(engine, f"请委派给{profiles[0].id}")
    assert runtime.use_direct_runtime(engine, "请调用personal_agents查看协作")
    assert not runtime.use_direct_runtime(engine, "读取普通项目文件并检查代码")
    paused = engine.personal_agents.update(profiles[0].id, 1, status="paused")
    assert runtime.use_direct_runtime(engine, f"请{paused.name}继续处理")
    archived = engine.personal_agents.update(paused.id, paused.version, status="archived")
    assert runtime.use_direct_runtime(engine, f"请查询{archived.name}的状态")


async def test_missing_runtime_key_has_same_chinese_error_through_tool_and_http(setup, monkeypatch):
    engine, service, profiles, _ = setup
    created = await service.create(
        **plan(engine, profiles, steps=plan(engine, profiles)["steps"][:1])
    )
    await finish(service, created["id"])
    monkeypatch.setattr(service.jobs, "_key_provider", lambda: b"invalid key")
    with pytest.raises(ToolExecutionError) as error:
        await PersonalAgentsTool(engine.personal_agents).execute(
            operation="collaboration",
            collaboration_id=created["id"],
        )
    assert "凭据" in str(error.value)
    app = FastAPI()
    app.state.engine, app.state.config = engine, engine.config
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/personal/collaborations/" + created["id"])
    assert response.status_code == 503 and response.json()["code"] == "storage_error"
    assert "凭据" in response.json()["detail"]
