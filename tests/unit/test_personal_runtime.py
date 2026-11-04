"""Persistent identity enforcement through the real Engine and file executor."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock

import pytest

from naumi_agent.config.settings import AppConfig
from naumi_agent.model.router import ModelResponse, ModelTier, TokenUsage
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.personal import runtime
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.tools.base import ToolCall

pytestmark = pytest.mark.usefixtures("runtime_payload_key")


@pytest.fixture
async def engine(tmp_path):
    config = AppConfig(
        workspace_root=str(tmp_path),
        personal={"data_dir": str(tmp_path / "personal")},
        memory={
            "session_db_path": str(tmp_path / "sessions.db"),
            "vector_db_path": str(tmp_path / "vectors"),
            "long_term_enabled": False,
        },
        safety={"permission_mode": "bypass", "max_turns": 3},
    )
    result = AgentEngine(config)
    yield result
    await result.shutdown()


async def specialist(engine, name="资料专员", tools=("file_read",)):
    profile = engine.personal_agents.create(
        name=name,
        instructions=f"只处理{name}的资料",
        request_id=uuid.uuid4().hex,
        tool_names=list(tools),
        model_tier="fast",
    )
    opened = await engine.personal_agents.open_session(profile.id)
    assert await engine.load_session(opened["session_id"])
    runtime.begin_turn(engine)
    return profile, opened


async def test_no_presets_and_main_tool_registration(engine):
    assert engine.personal_agents.list() == ()
    assert "personal_agents" in engine.tool_registry.names
    assert "初始只有主 Agent" in engine._build_system_prompt()
    result = await engine.execute_tool(
        ToolCall(
            id="create",
            name="personal_agents",
            arguments={
                "operation": "create",
                "name": "园艺助手",
                "instructions": "根据用户提供的植物记录整理养护建议",
                "request_id": "user-garden",
            },
        )
    )
    assert result.status == "success"
    assert json.loads(result.content)["name"] == "园艺助手"
    assert len(engine.personal_agents.list()) == 1


async def test_real_file_read_and_nonassigned_write_rejected_even_bypass(engine, tmp_path):
    await specialist(engine)
    (tmp_path / "notes.txt").write_text("真实资料内容", encoding="utf-8")
    result = await engine.execute_tool(
        ToolCall(
            id="read",
            name="file_read",
            arguments={"path": str(tmp_path / "notes.txt")},
        )
    )
    assert result.status == "success" and "真实资料内容" in result.content
    forbidden = await engine.execute_tool(
        ToolCall(
            id="write",
            name="file_write",
            arguments={"path": str(tmp_path / "escape.txt"), "content": "must not write"},
        )
    )
    assert forbidden.status == "error"
    assert not (tmp_path / "escape.txt").exists()
    assert [t["function"]["name"] for t in engine._get_openai_tools_cached()] == ["file_read"]


async def test_empty_toolset_and_catalog_admin_are_enforced(engine):
    await specialist(engine, tools=())
    assert engine._get_openai_tools_cached() is None
    result = await engine.execute_tool(
        ToolCall(
            id="admin",
            name="personal_agents",
            arguments={"operation": "list"},
        )
    )
    assert result.status == "error"


async def test_pause_and_revision_change_stop_current_turn(engine):
    profile, _ = await specialist(engine)
    changed = engine.personal_agents.update(profile.id, profile.version, instructions="新职责")
    assert "配置已变更" in runtime.tool_denial(engine, "file_read")
    runtime.begin_turn(engine)
    assert "新职责" in engine._build_system_prompt()
    engine.personal_agents.update(profile.id, changed.version, status="paused")
    with pytest.raises(PersonalAgentError, match="暂停"):
        runtime.begin_turn(engine)


async def test_permission_wait_revalidates_profile(engine, tmp_path):
    profile, _ = await specialist(engine, tools=("file_write",))
    from naumi_agent.safety.permissions import PermissionMode

    engine._permission_port.set_mode(PermissionMode.MODERATE)

    async def confirm(payload):
        engine.personal_agents.update(profile.id, profile.version, status="paused")
        return "allow_once"

    engine.set_permission_confirmer(confirm)
    result = await engine.execute_tool(
        ToolCall(
            id="paused-while-confirming",
            name="file_write",
            arguments={"path": str(tmp_path / "late.txt"), "content": "no write"},
        )
    )
    assert result.status == "error"
    assert not (tmp_path / "late.txt").exists()


async def test_private_conversations_do_not_recall_or_extract_global_memory(engine):
    await specialist(engine)
    engine.config.memory.long_term_enabled = True
    engine.long_term_memory.recall_for_session = AsyncMock()
    engine.long_term_memory.store = AsyncMock()
    runtime.begin_turn(engine)
    await engine._inject_relevant_memories("工作资料")
    from naumi_agent.orchestrator.engine import AgentResult

    await engine._auto_extract_memories("我喜欢私人资料", AgentResult(status="completed"))
    engine.long_term_memory.recall_for_session.assert_not_called()
    engine.long_term_memory.store.assert_not_called()
    assert engine._compactor._long_term_memory is engine.long_term_memory
    assert engine._compactor._long_term_memory is not engine._legacy_long_term_memory


async def test_independent_sessions_and_return_to_main(engine):
    _, first = await specialist(engine, "第一位")
    engine._append_message({"role": "user", "content": "只属于第一位的内容"})
    await engine._save_session()
    _, second = await specialist(engine, "第二位")
    assert first["session_id"] != second["session_id"]
    assert "只属于第一位" not in str(engine._messages)
    assert runtime.model_tier(engine) == ModelTier.FAST
    engine.reset()
    await engine.get_or_create_session()
    runtime.begin_turn(engine)
    assert runtime.current_agent(engine) is None
    assert len(engine._get_openai_tools_cached()) > 1


async def test_model_receives_current_role_and_only_assigned_tools(engine):
    await specialist(engine)
    engine.router.call = AsyncMock(
        return_value=ModelResponse(
            content="已读取配置",
            tool_calls=[],
            model="fixture",
            usage=TokenUsage(),
        )
    )
    engine._ensure_system_prompt()
    await engine._call_model_with_recovery(
        messages=engine._messages,
        tier=ModelTier.CAPABLE,
        tools=[t.to_openai_tool() for t in engine.tool_registry.all()],
    )
    arguments = engine.router.call.call_args.kwargs
    assert arguments["tier"] == ModelTier.FAST
    assert [t["function"]["name"] for t in arguments["tools"]] == ["file_read"]
    assert "只处理资料专员的资料" in str(arguments["messages"])


async def test_tool_errors_are_not_successful_actions(engine):
    result = await engine.execute_tool(
        ToolCall(
            id="bad",
            name="personal_agents",
            arguments={"operation": "create", "name": ""},
        )
    )
    assert result.status == "error"


async def test_role_change_rehydrates_after_new_engine_and_old_binding(engine, tmp_path):
    profile, opened = await specialist(engine)
    await engine._save_session()
    from naumi_agent.personal.service import PersonalAgentService
    from naumi_agent.personal.store import PersonalAgentStore

    service = PersonalAgentService(engine, PersonalAgentStore(engine.config.personal.data_dir))
    assert service.current(opened["session_id"]).id == profile.id
    archived = engine.personal_agents.update(profile.id, profile.version, status="archived")
    assert service.current(opened["session_id"]).status == "archived"
    with pytest.raises(PersonalAgentError):
        runtime.begin_turn(engine)
    engine.personal_agents.update(profile.id, archived.version, status="active")
    runtime.begin_turn(engine)
    assert runtime.current_agent(engine).id == profile.id


async def test_missing_catalog_never_escalates_a_specialist_to_main(engine, tmp_path):
    profile, opened = await specialist(engine, tools=())
    saved = await engine.session_store.load(opened["session_id"])
    assert saved.personal_agent_id == profile.id
    from naumi_agent.personal.service import PersonalAgentService
    from naumi_agent.personal.store import PersonalAgentStore

    engine.personal_agents = PersonalAgentService(engine, PersonalAgentStore(tmp_path / "empty"))
    with pytest.raises(PersonalAgentError, match="目录不可用"):
        runtime.begin_turn(engine)
    result = await engine.execute_tool(
        ToolCall(
            id="lost-catalog",
            name="file_write",
            arguments={"path": str(tmp_path / "escape.txt"), "content": "no"},
        )
    )
    assert result.status == "error" and not (tmp_path / "escape.txt").exists()
    engine.personal_agents.list()
    with pytest.raises(PersonalAgentError, match="归属不一致"):
        runtime.begin_turn(engine)


async def test_compaction_does_not_copy_main_team_history(engine):
    from naumi_agent.agents.message_bus import AgentMessage

    await engine.subagent_manager.message_bus.publish(
        AgentMessage(
            sender="main",
            topic="team.request",
            content="MAIN_PRIVATE_SENTINEL",
        )
    )
    await specialist(engine)
    snapshot, sections, _ = await engine._build_compaction_runtime_snapshot()
    assert "MAIN_PRIVATE_SENTINEL" not in snapshot
    assert sections == ["personal_agent"]


async def test_model_retry_checks_revocation_again(engine):
    profile, _ = await specialist(engine)
    engine.router.call = AsyncMock(
        side_effect=[
            RuntimeError("maximum context length exceeded"),
            ModelResponse(content="must not call"),
        ]
    )

    async def compact(**kwargs):
        engine.personal_agents.update(profile.id, profile.version, status="paused")
        return True

    engine._reactive_compact_for_prompt_too_long = compact
    with pytest.raises(PersonalAgentError, match="暂停"):
        await engine._call_model_with_recovery(messages=[], tier=ModelTier.CAPABLE, tools=None)
    assert engine.router.call.call_count == 1


async def test_explicit_user_command_can_resume_paused_agent_without_model_escalation(engine):
    profile, _ = await specialist(engine, tools=())
    paused = engine.personal_agents.update(profile.id, profile.version, status="paused")
    args = {
        "operation": "update",
        "agent_id": profile.id,
        "expected_version": paused.version,
        "status": "active",
    }
    denied = await engine.execute_tool(ToolCall(id="model", name="personal_agents", arguments=args))
    assert denied.status == "error"
    allowed = await engine.execute_tool(
        ToolCall(id="human", name="personal_agents", arguments=args), agent_name="cli"
    )
    assert allowed.status == "success"
    assert engine.personal_agents.get(profile.id).status == "active"
