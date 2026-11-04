"""Real Engine, API and execution-boundary wiring with a controlled model port."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from naumi_agent.api.app import create_app
from naumi_agent.config.settings import AppConfig
from naumi_agent.model.router import ModelResponse, ModelRouter, ModelTier, StreamChunk, TokenUsage
from naumi_agent.personal import runtime
from naumi_agent.personal.artifact_boundary import guarded_tool, is_protected
from naumi_agent.personal.engine_execution import bind_execution_memory, make_execution_child
from naumi_agent.personal.memory_adapter import ScopedMemoryAdapter
from naumi_agent.personal.memory_models import canonical
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.runtime.composition import create_agent_engine
from naumi_agent.tools.base import ToolCall

pytestmark = pytest.mark.usefixtures("runtime_payload_key")


class UnavailableSemantic:
    def status(self):
        return {"status": "unavailable", "error": "受控测试未启用语义通道"}

    def query(self, *args, **kwargs):
        raise PersonalAgentError("memory_index_unavailable", "受控测试未启用语义通道")

    def upsert(self, *args, **kwargs):
        raise PersonalAgentError("memory_index_unavailable", "受控测试未启用语义通道")

    def delete(self, *args, **kwargs):
        return None

    def ensure_current(self, records):
        if records:
            raise PersonalAgentError("memory_index_unavailable", "受控测试未启用语义通道")
        return 0

    def reconcile(self, records):
        return {"repaired": self.ensure_current(records), "removed": 0}


@pytest.fixture
async def memory_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("NAUMI_STATE_HOME", str(tmp_path / "state"))

    async def forbidden(self, **kwargs):
        raise AssertionError("No external provider call belongs to this controlled test")

    monkeypatch.setattr(ModelRouter, "call", forbidden)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    engine = create_agent_engine(
        AppConfig(
            workspace_root=str(workspace),
            personal={"data_dir": str(tmp_path / "personal")},
            memory={
                "session_db_path": str(tmp_path / "sessions.db"),
                "vector_db_path": str(tmp_path / "legacy"),
                "long_term_enabled": True,
            },
            safety={"permission_mode": "bypass", "max_turns": 3},
            skills={"search_paths": []},
        )
    )
    engine.personal_memory.index = UnavailableSemantic()
    await engine.get_or_create_session(title="记忆运行时受控验收")
    try:
        yield engine
    finally:
        await engine.shutdown()


async def save(engine, *, content="MEMORY_ONLY_LILAC_520", actor="main", scope="personal"):
    return await engine.personal_memory.dispatch(
        "create",
        {
            "request_id": "manual-" + actor + "-" + scope,
            "topic": "回答格式",
            "category": "preference",
            "content": content,
            "scope": scope,
        },
        actor_id=actor,
        trusted_channel="manual_cli",
    )


async def test_tool_registration_and_no_global_chroma_writes(memory_engine):
    engine = memory_engine
    assert {"personal_memories", "memory_store", "memory_recall"} <= set(engine.tool_registry.names)
    assert isinstance(engine.long_term_memory, ScopedMemoryAdapter)
    assert engine.long_term_memory is not engine._legacy_long_term_memory
    for name in ("personal_memories", "memory_store", "memory_recall"):
        engine.personal_agents.validate_tools([name])
    assert engine._legacy_long_term_memory._client is None
    runtime.begin_turn(engine)
    with runtime.run_context(engine, engine._session.id, "controlled-run", None):
        result = await engine.execute_tool(
            ToolCall(
                id="candidate",
                name="memory_store",
                arguments={"content": "助手建议使用浅色布局"},
            )
        )
    assert result.status == "success"
    value = json.loads(result.content)
    assert value["state"] == "proposed" and value["refs"] == []
    assert engine._legacy_long_term_memory._client is None
    candidate = await engine.personal_memory.get(value["candidate_id"])
    assert candidate["source"]["authority"] == "assistant"
    assert candidate["confirmation"] is None


async def test_actual_call_uses_fresh_copy_and_revalidates_before_second_call(
    memory_engine, monkeypatch
):
    engine = memory_engine
    note = await save(engine)
    runtime.begin_turn(engine)
    await engine.personal_memory_runtime.prime("请回答当前格式")
    raw = [
        {"role": "system", "content": "BASE_SYSTEM"},
        {"role": "system", "content": "## 相关记忆\nSTALE_GLOBAL_TEXT"},
        {"role": "user", "content": "请回答当前格式"},
    ]
    calls = []

    async def controlled(**kwargs):
        calls.append(kwargs)
        return ModelResponse(content="受控回答", model="controlled")

    monkeypatch.setattr(engine.router, "call", controlled)
    await engine._call_model_with_recovery(messages=raw, tier=ModelTier.FAST, tools=None)
    assert "MEMORY_ONLY_LILAC_520" in canonical(calls[0]["messages"])
    assert "STALE_GLOBAL_TEXT" not in canonical(calls[0]["messages"])
    assert "MEMORY_ONLY_LILAC_520" not in canonical(raw)
    await engine.personal_memory.dispatch(
        "delete",
        {
            "request_id": "delete-before-second-call",
            "memory_id": note["memory_id"],
            "expected_revision": note["revision"],
        },
        trusted_channel="manual_cli",
    )
    with pytest.raises(PersonalAgentError, match="纠正、删除或撤权"):
        await engine._call_model_with_recovery(messages=raw, tier=ModelTier.FAST, tools=None)
    assert len(calls) == 1
    runtime.begin_turn(engine)
    raw.append(
        {
            "role": "tool",
            "tool_call_id": "prior",
            "content": canonical({"type": "personal_memory_refs", "refs": [note["ref"]]}),
        }
    )
    await engine._call_model_with_recovery(messages=raw, tier=ModelTier.FAST, tools=None)
    assert "MEMORY_ONLY_LILAC_520" not in canonical(calls[1]["messages"])


async def test_final_send_consumes_metadata_before_removing_provider_extras(memory_engine):
    engine = memory_engine
    note = await save(engine)
    raw = [
        {
            "role": "assistant",
            "content": "STALE_METADATA_ONLY_MEMORY_BODY",
            "metadata": {"personal_memory_refs": [note["ref"]]},
        }
    ]
    current = await engine._prepare_messages_for_model(raw)
    assert "MEMORY_ONLY_LILAC_520" in canonical(current)
    assert "STALE_METADATA_ONLY_MEMORY_BODY" not in canonical(current)
    assert "metadata" not in current[-1]
    engine.config.memory.long_term_enabled = False
    disabled = await engine._prepare_messages_for_model(raw)
    assert "STALE_METADATA_ONLY_MEMORY_BODY" not in canonical(disabled)
    assert "MEMORY_ONLY_LILAC_520" not in canonical(disabled)
    assert all("metadata" not in value for value in disabled)
    assert raw[0]["content"] == "STALE_METADATA_ONLY_MEMORY_BODY"


@pytest.mark.parametrize("phase", ["extract", "compact"])
async def test_compactor_internal_model_calls_revalidate_current_refs(
    memory_engine,
    monkeypatch,
    phase,
):
    engine = memory_engine
    note = await save(engine)
    runtime.begin_turn(engine)
    await engine._prepare_messages_for_model([])
    await engine.personal_memory.dispatch(
        "delete",
        {
            "request_id": "delete-before-compaction",
            "memory_id": note["memory_id"],
            "expected_revision": note["revision"],
        },
        trusted_channel="manual_cli",
    )
    calls, checks = [], []

    async def transport(**kwargs):
        calls.append(kwargs)
        raise AssertionError("Revoked context must not reach the compaction model")

    async def authorize():
        checks.append(phase)
        await runtime.authorize_execution(engine)

    monkeypatch.setattr(engine.router, "call", transport)
    engine._compactor._execution_authorizer = authorize
    history = [
        {"role": "user" if i % 2 else "assistant", "content": "PRIVATE_OLD_HISTORY_" * 30}
        for i in range(12)
    ]
    if phase == "extract":
        await engine._compactor._extract_memories(history)
        assert checks == ["extract"]
    else:
        assert await engine._compactor.compact(history, max_tokens=1) == history
        assert checks == ["compact", "compact"]
    assert calls == []


async def test_streaming_send_resolves_actor_memory_without_persisting_block(
    memory_engine, monkeypatch
):
    engine = memory_engine
    agent = engine.personal_agents.create(
        name="Echo",
        instructions="回答用户的当前问题",
        request_id="explicit-create-echo",
        tool_names=[],
        model_tier="fast",
    )
    opened = await engine.personal_agents.open_session(agent.id)
    await engine.load_session(opened["session_id"])
    await save(engine, actor=agent.id)
    calls = []

    async def controlled(**kwargs):
        calls.append(kwargs)
        yield StreamChunk(
            token="已按当前格式回答。",
            finish_reason="stop",
            usage=TokenUsage(input_tokens=15, output_tokens=6, cost_usd=0.001),
        )

    monkeypatch.setattr(engine.router, "stream", controlled)

    async def observe(*args):
        pass

    result = await engine.run_streaming("只回答这一个问题", observe)
    assert result.status == "completed"
    assert calls and "MEMORY_ONLY_LILAC_520" in canonical(calls[0]["messages"])
    stored = await engine.session_store.load(opened["session_id"])
    assert "MEMORY_ONLY_LILAC_520" not in canonical(stored.messages)
    assert "## 已确认记忆与来源" not in canonical(stored.messages)
    assert engine._legacy_long_term_memory._client is None


async def test_api_routes_use_real_memory_authority_and_opaque_discard(memory_engine):
    engine = memory_engine
    app = create_app()
    app.state.engine = engine
    app.state.config = engine.config
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/personal/memory-requests",
            json={
                "request_id": "opaque-engine-request",
                "actor_id": "main",
                "operation": "create",
                "payload": {
                    "request_id": "opaque-engine-request",
                    "topic": "回答格式",
                    "category": "preference",
                    "content": "PRIVATE_PREPARED_PAYLOAD",
                    "scope": "personal",
                },
            },
        )
        assert response.status_code == 201, response.text
        opaque = response.json()
        assert "PRIVATE_PREPARED_PAYLOAD" not in response.text
        path = "/api/v1/personal/memory-requests/opaque-engine-request"
        discarded = await client.post(
            path + "/discard?actor_id=main",
            json={
                "expected_fingerprint": opaque["fingerprint"],
            },
        )
        assert discarded.status_code == 200, discarded.text
        assert discarded.json()["state"] == "erased"
        replay = await client.post(
            path + "/apply?actor_id=main",
            json={
                "expected_fingerprint": opaque["fingerprint"],
            },
        )
        assert replay.status_code >= 400
        assert (await client.get("/api/v1/personal/memories?state=active")).json()["memories"] == []


async def test_private_memory_roots_and_source_refs_join_actual_tool_guard(memory_engine, tmp_path):
    engine = memory_engine
    await save(engine)
    assert is_protected(engine, engine.personal_memory.store.db_path)
    assert is_protected(engine, engine.personal_memory.runtime_boundary_marker)
    assert is_protected(engine, engine.personal_memory.owner_boundary_marker)
    assert is_protected(engine, tmp_path / "legacy" / "chroma.sqlite3")
    runtime.begin_turn(engine)
    result = await engine.execute_tool(
        ToolCall(
            id="read-authority",
            name="file_read",
            arguments={"path": str(engine.personal_memory.store.db_path)},
        )
    )
    assert result.status == "error"
    tool = engine.tool_registry.get("web_fetch")
    assert guarded_tool(engine, tool, "source-bound") is not tool


async def test_memory_source_artifacts_remain_pinned_after_retrieval_and_restart(memory_engine):
    engine = memory_engine
    source = await engine.personal_artifacts.publish(
        actor_id="main",
        request_id="source-pin-input",
        title="原始资料",
        kind="material",
        filename="private.txt",
        content=b"SOURCE_PRIVATE_BODY_TURN_ONE",
    )
    ref = engine.personal_artifacts.reference(source)
    candidate = await engine.personal_memory.dispatch(
        "propose",
        {
            "request_id": "source-pin-note",
            "topic": "原始资料事实",
            "category": "fact",
            "content": "SOURCE_PRIVATE_BODY_TURN_ONE",
            "scope": "workspace",
            "source_artifact_ref": ref,
        },
        actor_id="main",
        trusted_channel="manual_cli",
    )
    await engine.personal_memory.dispatch(
        "confirm",
        {
            "request_id": "source-pin-confirm",
            "memory_id": candidate["memory_id"],
            "expected_revision": candidate["revision"],
            "expected_content_sha256": candidate["content_sha256"],
            "expected_active_ref": None,
        },
        trusted_channel="manual_cli",
    )
    runtime.begin_turn(engine)
    await engine.personal_memory_runtime.prime("SOURCE_PRIVATE_BODY_TURN_ONE")
    prepared = await engine._prepare_messages_for_model([])
    assert "SOURCE_PRIVATE_BODY_TURN_ONE" in canonical(prepared)
    engine._append_message({"role": "assistant", "content": "SOURCE_PRIVATE_BODY_TURN_ONE"})
    await engine._save_session()
    session_id = engine._session.id
    engine.config.memory.long_term_enabled = False
    runtime.begin_turn(engine)
    assert engine._personal_memory_artifact_refs == []
    assert engine._personal_artifact_refs == [ref]
    fresh = create_agent_engine(engine.config.model_copy(deep=True))
    try:
        assert await fresh.load_session(session_id)
        runtime.begin_turn(fresh)
        assert fresh._personal_artifact_refs == [ref]
        result = await fresh.execute_tool(
            ToolCall(
                id="no-private-network-after-restart",
                name="web_fetch",
                arguments={"url": "https://example.com/never-requested"},
            )
        )
        assert result.status == "error" and "私有资料" in result.content
    finally:
        await fresh.shutdown()


async def test_child_binding_preserves_legacy_off_and_has_no_implicit_identity(memory_engine):
    parent = memory_engine
    job = SimpleNamespace(
        job_id="controlled-child-job",
        request=SimpleNamespace(
            permission_mode="bypass",
            max_turns=3,
            max_cost_microusd=10000,
        ),
    )
    child = make_execution_child(parent, job)
    try:
        assert child.config.memory.long_term_enabled is False
        assert child.personal_memory_runtime.enabled is False
        await child.get_or_create_session(title="受控执行")
        await bind_execution_memory(child, actor_id="main", origin_actor_id="main", parent=parent)
        assert child.personal_memory_runtime.enabled is True
        assert child._personal_memory_execution_context["origin_actor_ids"] == ["main"]
        parent.config.memory.long_term_enabled = False
        assert child.personal_memory_runtime.enabled is False
        with pytest.raises(PersonalAgentError):
            await child.personal_memory._eligible("main")
    finally:
        await child.shutdown()
