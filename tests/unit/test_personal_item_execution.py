"""Real Job/Run/Library/inbox item execution with controlled, zero-paid transport."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from naumi_agent.config.settings import AppConfig
from naumi_agent.daemons.agent_jobs import AgentJobPayload, AgentJobState, agent_job_id_for_request
from naumi_agent.model.router import ModelRouter, StreamChunk, TokenUsage
from naumi_agent.personal import runtime
from naumi_agent.personal.item_execution import PersonalItemExecutionService, _request
from naumi_agent.personal.item_store import PersonalItemStore
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.runtime.composition import create_agent_engine

pytestmark = pytest.mark.usefixtures("runtime_payload_key")
ANSWER = "指定资料的整理结果。没有支付凭证，支付状态保持未知。"


@pytest.fixture
async def execution(tmp_path, monkeypatch, request):
    monkeypatch.setenv("NAUMI_STATE_HOME", str(tmp_path / "state"))
    calls = []

    async def no_provider(self, **kwargs):
        raise AssertionError("No paid/non-controlled model call belongs to this test")

    async def stream(self, **kwargs):
        calls.append(kwargs)
        yield StreamChunk(
            token=ANSWER,
            finish_reason="stop",
            usage=TokenUsage(input_tokens=12, output_tokens=5, cost_usd=0.001),
        )

    monkeypatch.setattr(ModelRouter, "call", no_provider)
    monkeypatch.setattr(ModelRouter, "stream", stream)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    if getattr(request, "param", "git") != "without_git":
        (workspace / "source.txt").write_text("Fixture source", encoding="utf-8")
        for argv in (
            ["init", "-q"],
            ["add", "source.txt"],
            [
                "-c",
                "user.name=Item Fixture",
                "-c",
                "user.email=item@example.invalid",
                "commit",
                "-qm",
                "Record fixture input",
            ],
        ):
            subprocess.run(["git", "-C", str(workspace), *argv], check=True, capture_output=True)
    engine = create_agent_engine(
        AppConfig(
            workspace_root=str(workspace),
            personal={"data_dir": str(tmp_path / "personal")},
            memory={
                "session_db_path": str(tmp_path / "sessions.db"),
                "vector_db_path": str(tmp_path / "vectors"),
                "long_term_enabled": False,
            },
            safety={
                "permission_mode": "bypass",
                "max_turns": 3,
                "max_parallel_agents": 1,
                "max_queued_agents": 0,
            },
        )
    )
    owner = "a" * 64
    store = PersonalItemStore(tmp_path / "items", owner_key=owner)

    async def validate(intent):
        item = store.get(owner_key=owner, item_id=intent["item_id"])
        if item["state"] == "paused":
            raise PersonalAgentError("occurrence_paused", "事项已暂停")
        if item["state"] != "active":
            raise PersonalAgentError("cancelled", "事项已取消或关闭")
        assert item["definition_sha256"] == intent["definition_sha256"]
        assert item["once_key"] == intent["once_key"]

    service = PersonalItemExecutionService(engine, store, owner, validate_intent=validate)
    try:
        yield engine, store, owner, service, calls, validate
    finally:
        await service.stop()
        await engine.shutdown()


def _definition(engine, actor="main", version=0, tools=None):
    return {
        "title": "资料事项",
        "instructions": "整理指定资料并输出报告。",
        "tags": ["工作"],
        "assignee": {
            "id": actor,
            "version": version,
            "name": "Mira" if version else "主 Agent",
            "kind": "specialist" if version else "main",
        },
        "origin": {"actor_id": "main", "session_id": ""},
        "route": {
            "workspace_key": engine.personal_agents.workspace_key,
            "workspace_root": str(engine.workspace_root),
            "session_db_key": engine.personal_agents.session_db_key,
        },
        "schedule": {
            "mode": "once",
            "local_time": "2030-11-05T09:00:00",
            "timezone": "Asia/Shanghai",
            "fold": None,
            "due_at": "2030-11-05T01:00:00+00:00",
            "misfire_policy": "run_once",
            "grace_seconds": 86400,
        },
        "inputs": {"artifact_refs": [], "directories": []},
        "policy": {
            "tool_names": tools if tools is not None else ["personal_artifacts"],
            "artifact_operations": ["get", "read", "publish"],
            "model_tier": "fast" if version else "capable",
            "max_turns": 3,
            "max_cost_microusd": 1000000,
            "timeout_seconds": 30,
        },
        "output": {"destination": "personal_library", "format": "report"},
    }


def _prepare(execution, definition=None):
    engine, store, owner, service, *_ = execution
    item = store.create(
        owner_key=owner, request_id=uuid.uuid4().hex, definition=definition or _definition(engine)
    )
    raw = service.prepare(
        item,
        occurrence_id="pio_" + uuid.uuid4().hex,
        execution_session_id="pie_" + uuid.uuid4().hex,
        task=item["definition"]["instructions"],
        context="明确授权的事项输入。",
        input_refs=[],
        input_manifest=[],
        schedule_id="schedule-" + item["once_key"],
        event_id="event-" + item["once_key"],
    )
    return store.prepare(
        owner_key=owner,
        item_id=item["item_id"],
        expected_definition_sha256=item["definition_sha256"],
        intent=raw,
    )


async def _settle(service, intent):
    if service._active:
        results = await asyncio.wait_for(
            asyncio.gather(*list(service._active.values()), return_exceptions=True), 40
        )
        assert not [r for r in results if isinstance(r, BaseException)], results
    return await service.reconcile(intent)


async def test_main_only_real_run_fallback_report_exact_receipt_inbox_and_human_read(execution):
    engine, store, owner, service, calls, _ = execution
    assert engine.personal_agents.list() == ()
    intent = _prepare(execution)
    assert await service.jobs.get(agent_job_id_for_request(_request(intent))) is None
    await service.submit(intent)
    result = await _settle(service, intent)
    assert result["status"] == "completed", result
    assert len(calls) == 1 and result["usage_known"] is True
    assert result["receipt"]["outcome"] == "completed"
    assert result["receipt"]["run_id"] == result["run_id"]
    assert result["delivery_id"] and result["read_receipt"] is None
    assert engine.personal_agents.list() == () and engine._session is None
    ref = result["artifacts"][0]
    value, content = await engine.personal_artifacts.read(
        actor_id="main", artifact_id=ref["artifact_id"], version=ref["version"]
    )
    assert content.decode() == ANSWER and value["evidence"]["run_id"] == result["run_id"]
    saved = await engine.session_store.load(intent["execution_session_id"])
    assert saved.personal_agent_id == "" and saved.personal_execution_id == intent["task_id"]
    assert saved.messages[-1]["metadata"]["artifact_refs"] == result["artifacts"]
    assert saved.messages[-1]["metadata"]["run_id"] == result["run_id"]
    acknowledged = await service.ack(
        intent,
        delivery_id=result["delivery_id"],
        expected_delivery_sha256=result["delivery_sha256"],
        trusted_channel="management_web",
    )
    assert acknowledged["read_receipt"]["kind"] == "human_result_read"
    await service.submit(intent)
    assert len(calls) == 1
    assert (
        store.get_intent(owner_key=owner, occurrence_id=intent["occurrence_id"])["references"][
            "run_id"
        ]
        == result["run_id"]
    )


async def test_existing_agent_keeps_real_identity_and_daily_pointer(execution):
    engine, _, _, service, calls, _ = execution
    agent = engine.personal_agents.create(
        name="Mira",
        instructions="整理我的资料",
        tool_names=["personal_artifacts"],
        model_tier="fast",
        request_id="mira",
    )
    daily = await engine.personal_agents.open_session(agent.id)
    intent = _prepare(execution, _definition(engine, agent.id, agent.version))
    await service.submit(intent)
    result = await _settle(service, intent)
    assert result["status"] == "completed", result
    assert len(calls) == 1
    assert engine.personal_agents.current(intent["execution_session_id"]).id == agent.id
    assert (await engine.personal_agents.open_session(agent.id))["session_id"] == daily[
        "session_id"
    ]


async def test_two_coordinators_submit_same_intent_without_double_model(execution):
    engine, store, owner, service, calls, validate = execution
    other = PersonalItemExecutionService(engine, store, owner, validate_intent=validate)
    intent = _prepare(execution)
    try:
        await asyncio.gather(*(s.submit(intent) for s in (service, other, service, other)))
        for s in (service, other):
            if s._active:
                await asyncio.wait_for(asyncio.gather(*list(s._active.values())), 40)
        assert (await service.reconcile(intent))["status"] == "completed"
        assert len(calls) == 1
    finally:
        await other.stop()


async def test_cancel_before_admission_does_not_create_job_model_or_delivery(execution):
    _, _, _, service, calls, _ = execution
    intent = _prepare(execution)
    value = await service.cancel(intent)
    assert value["status"] == "cancelled" and value["delivery_id"] == ""
    assert value["usage_known"] is True and calls == []
    assert await service.jobs.get(agent_job_id_for_request(_request(intent))) is None


async def test_view_is_pure_read_and_tampered_prepared_is_rejected(execution):
    _, _, _, service, calls, _ = execution
    intent = _prepare(execution)
    assert (await service.view(intent))["status"] == "prepared"
    assert service._active == {} and calls == []
    changed = {**intent, "event_id": "different"}
    with pytest.raises(PersonalAgentError):
        await service.submit(changed)


async def test_started_expired_job_becomes_unknown_without_model_or_fake_delivery(execution):
    _, _, _, service, calls, _ = execution
    intent = _prepare(execution)
    old_clock = service.jobs._clock
    service.jobs._clock = lambda: datetime.now(UTC) - timedelta(hours=1)
    job = await service.jobs.admit(
        request=_request(intent), payload=AgentJobPayload(**intent["payload"])
    )
    claim = await service.jobs.claim(job.job_id, owner_id="dead-owner", lease_seconds=90)
    await service.jobs.mark_running(
        job.job_id, owner_id="dead-owner", claim_epoch=claim.job.claim_epoch
    )
    service.jobs._clock = old_clock
    value = await service.reconcile(intent)
    assert value["status"] == "unknown" and value["usage_known"] is False
    assert value["delivery_id"] == "" and calls == []
    assert await service.jobs.get_job_publication(job.job_id) is None
    assert (await service.submit(intent))["status"] == "unknown" and calls == []


async def test_pause_after_claim_race_cancels_without_start_and_frees_capacity(execution):
    _, store, owner, service, calls, _ = execution
    intent = _prepare(execution)
    real_validate = service._validate
    entered, release = asyncio.Event(), asyncio.Event()
    count = 0

    async def gated(value):
        nonlocal count
        count += 1
        if count == 2:
            entered.set()
            await asyncio.wait_for(release.wait(), 15)
        await real_validate(value)

    service._validate = gated
    try:
        await service.submit(intent)
        await asyncio.wait_for(entered.wait(), 15)
        item = store.get(owner_key=owner, item_id=intent["item_id"])
        store.control(
            owner_key=owner,
            item_id=item["item_id"],
            expected_revision=item["revision"],
            request_id="race-pause",
            action="pause",
        )
        release.set()
        value = await _settle(service, intent)
        assert value["status"] == "cancelled" and value["delivery_id"] == ""
        assert calls == []
        job = await service.jobs.get(value["job_id"])
        assert job.result is None and job.state is AgentJobState.CANCELLED
        assert await service.jobs.get_job_publication(job.job_id) is None
    finally:
        release.set()


async def test_main_execution_enforces_scope_tier_direct_and_isolated_context(
    execution, monkeypatch
):
    engine, _, _, service, _, _ = execution
    intent = _prepare(execution)
    checked = []
    real = service._child_factory

    def inspect_child(parent, job):
        child = real(parent, job)
        original = child.run_streaming

        async def run(*args, **kwargs):
            context = child._personal_item_context
            assert context.force_private and list(context.process_workspace.iterdir()) == []
            assert runtime.use_direct_runtime(child, "普通事项")
            assert runtime.model_tier(child).value == intent["policy"]["model_tier"]
            assert runtime.tool_denial(child, "file_write")
            with pytest.raises(PersonalAgentError):
                await context.authorize_tool("personal_artifacts", {"operation": "share"})
            with pytest.raises(PersonalAgentError):
                await context.authorize_tool("file_read", {"path": "C:/private.txt"})
            with pytest.raises(PersonalAgentError):
                await context.authorize_tool(
                    "personal_artifacts",
                    {"operation": "read", "artifact_id": "art_" + "a" * 32, "version": 1},
                )
            checked.append(True)
            return await original(*args, **kwargs)

        monkeypatch.setattr(child, "run_streaming", run)
        return child

    service._child_factory = inspect_child
    await service.submit(intent)
    assert (await _settle(service, intent))["status"] == "completed"
    assert checked == [True] and engine._session is None


async def test_publication_recovers_after_close_without_new_model(execution, monkeypatch):
    engine, store, owner, service, calls, validate = execution
    intent = _prepare(execution)

    async def fail_delivery(*args):
        raise OSError("delivery interruption")

    monkeypatch.setattr(service, "_deliver", fail_delivery)
    await service.submit(intent)
    if service._active:
        await asyncio.wait_for(
            asyncio.gather(*list(service._active.values()), return_exceptions=True), 40
        )
    item = store.get(owner_key=owner, item_id=intent["item_id"])
    store.control(
        owner_key=owner,
        item_id=item["item_id"],
        expected_revision=item["revision"],
        request_id="close-known",
        action="close",
    )
    restarted = PersonalItemExecutionService(engine, store, owner, validate_intent=validate)
    try:
        result = await restarted.reconcile(intent)
        assert result["status"] == "completed" and result["delivery_id"]
        assert len(calls) == 1
    finally:
        await restarted.stop()


@pytest.mark.parametrize("execution", ["without_git"], indirect=True)
async def test_partial_real_run_receipt_never_becomes_completed_item(execution):
    _, _, _, service, calls, _ = execution
    intent = _prepare(execution)
    await service.submit(intent)
    result = await _settle(service, intent)
    assert result["status"] == "error"
    assert result["receipt"]["outcome"] != "completed"
    assert result["artifacts"] == [] and len(calls) == 1


async def test_missing_provider_usage_stays_unknown_with_no_zero_result_or_delivery(
    execution, monkeypatch
):
    _, _, _, service, calls, _ = execution

    async def no_usage(self, **kwargs):
        calls.append(kwargs)
        yield StreamChunk(token="有文本但没有真实用量。", finish_reason="stop")

    monkeypatch.setattr(ModelRouter, "stream", no_usage)
    intent = _prepare(execution)
    await service.submit(intent)
    result = await _settle(service, intent)
    assert result["status"] == "unknown" and result["usage_known"] is False
    assert result["usage"] is None and not result["delivery_id"]
    job = await service.jobs.get(result["job_id"])
    assert job.result is None and await service.jobs.get_job_publication(job.job_id) is None
    await service.reconcile(intent)
    assert len(calls) == 1


async def test_real_zero_usage_receipt_remains_known_not_unknown(execution, monkeypatch):
    _, _, _, service, calls, _ = execution

    async def zero_usage(self, **kwargs):
        calls.append(kwargs)
        yield StreamChunk(token=ANSWER, finish_reason="stop", usage=TokenUsage())

    monkeypatch.setattr(ModelRouter, "stream", zero_usage)
    intent = _prepare(execution)
    await service.submit(intent)
    result = await _settle(service, intent)
    assert result["status"] == "completed" and result["usage_known"] is True
    assert result["usage"]["input_tokens"] == result["usage"]["output_tokens"] == 0


async def test_missing_run_usage_receipt_is_unknown_even_if_model_reported_usage(
    execution, monkeypatch
):
    _, _, _, service, calls, _ = execution
    real = service._child_factory

    def factory(parent, job):
        child = real(parent, job)
        finish = child._finish_streaming_run

        async def missing_usage(**kwargs):
            kwargs["usage_before"] = None
            return await finish(**kwargs)

        monkeypatch.setattr(child, "_finish_streaming_run", missing_usage)
        return child

    service._child_factory = factory
    intent = _prepare(execution)
    await service.submit(intent)
    result = await _settle(service, intent)
    assert result["status"] == "unknown" and result["usage"] is None
    job = await service.jobs.get(result["job_id"])
    assert job.result is None and len(calls) == 1 and not result["delivery_id"]


async def test_bound_job_catalog_loss_or_replacement_does_not_readmit(execution):
    _, _, _, service, calls, _ = execution
    intent = _prepare(execution)
    request = _request(intent)
    job = await service.jobs.admit(request=request, payload=AgentJobPayload(**intent["payload"]))
    service._bind(intent, job_id=job.job_id, namespace=service.namespace)
    original = service.jobs.db_path
    moved = original.with_name("original-missing.db")
    original.replace(moved)
    try:
        with pytest.raises(PersonalAgentError, match="原件缺失"):
            await service.submit(intent)
        assert not original.exists() and calls == [] and service._active == {}
    finally:
        moved.replace(original)


async def test_replayed_old_cancel_never_cancels_restarted_once(execution):
    _, store, owner, service, calls, _ = execution
    intent = _prepare(execution)
    await service.cancel(intent)
    cancelled = store.get(owner_key=owner, item_id=intent["item_id"])
    restarted = store.restart(
        owner_key=owner,
        item_id=cancelled["item_id"],
        expected_revision=cancelled["revision"],
        request_id="explicit-restart",
        definition=cancelled["definition"],
    )
    assert restarted["once_key"] != intent["once_key"]
    result = await service.cancel(intent)
    assert result["status"] == "cancelled" and calls == []
    current = store.get(owner_key=owner, item_id=intent["item_id"])
    assert current["state"] == "active" and current["once_key"] == restarted["once_key"]


async def test_missing_original_run_after_completion_is_not_reported_as_success(execution):
    engine, _, _, service, _, _ = execution
    intent = _prepare(execution)
    await service.submit(intent)
    result = await _settle(service, intent)
    with sqlite3.connect(engine.chat_run_store.db_path) as db:
        db.execute("DELETE FROM chat_runs WHERE id=?", (result["run_id"],))
    with pytest.raises(PersonalAgentError, match="原始运行"):
        await service.view(intent)


async def test_revoked_original_input_redacts_run_steps_receipt_answer_and_error(execution):
    engine, store, owner, service, _, _ = execution
    agent = engine.personal_agents.create(
        name="Mira",
        instructions="整理资料",
        tool_names=["personal_artifacts"],
        model_tier="fast",
        request_id="private-mira",
    )
    source = await engine.personal_artifacts.publish(
        actor_id="main",
        request_id="private-source",
        title="私有来源",
        kind="material",
        filename="secret.md",
        content=b"PRIVATE_ONLY_SENTINEL",
    )
    grant = await engine.personal_artifacts.share(
        actor_id="main",
        artifact_id=source["artifact_id"],
        version=1,
        recipient_id=agent.id,
        request_id="share-private",
    )
    snapshot = await engine.personal_artifacts.get(
        actor_id=agent.id, artifact_id=source["artifact_id"], version=1
    )
    ref = engine.personal_artifacts.reference(snapshot)
    definition = _definition(engine, agent.id, agent.version)
    definition["inputs"]["artifact_refs"] = [ref]
    item = store.create(owner_key=owner, request_id="private-item", definition=definition)
    intent = service.prepare(
        item,
        occurrence_id="pio_" + uuid.uuid4().hex,
        execution_session_id="pie_" + uuid.uuid4().hex,
        task=definition["instructions"],
        context="PRIVATE_ONLY_SENTINEL",
        input_refs=[ref],
        input_manifest=[
            {
                "source": "artifact",
                "artifact_ref": ref,
                "filename": "secret.md",
                "bytes": 21,
                "sha256": ref["sha256"],
            }
        ],
        schedule_id="private-schedule",
        event_id="private-event",
    )
    intent = store.prepare(
        owner_key=owner,
        item_id=item["item_id"],
        expected_definition_sha256=item["definition_sha256"],
        intent=intent,
    )
    await service.submit(intent)
    result = await _settle(service, intent)
    assert result["status"] == "completed", result
    await engine.chat_run_store.append_step(
        result["run_id"],
        sequence=999,
        stage="analysis",
        status="completed",
        summary="PRIVATE_ONLY_SENTINEL",
        detail="PRIVATE_ONLY_SENTINEL",
        metadata={"private": "PRIVATE_ONLY_SENTINEL"},
    )
    await engine.personal_artifacts.revoke(
        actor_id="main",
        grant_id=grant["grant_id"],
        expected_revision=ref["grant_revision"],
        request_id="revoke-private",
    )
    redacted = await service.view(intent)
    assert "PRIVATE_ONLY_SENTINEL" not in json.dumps(redacted, ensure_ascii=False)
    assert redacted["answer"] == "" and redacted["artifacts"] == []
    assert redacted["receipt"] is None and redacted["steps"] == []
    assert redacted["status"] == "completed" and redacted["delivery_id"]
