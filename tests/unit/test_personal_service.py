"""Personal-agent service and tool contracts with a real SQLite session store."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.config.settings import MemoryConfig
from naumi_agent.memory.session import Session, SessionStore
from naumi_agent.model.router import ModelTier
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.service import PersonalAgentService
from naumi_agent.personal.store import PersonalAgentStore
from naumi_agent.personal.tools import PersonalAgentsTool
from naumi_agent.tools.base import Tool, ToolExecutionError, ToolMetadata, ToolRegistry


class NamedTool(Tool):
    def __init__(self, name: str, *, delegates: tuple[str, ...] = ()):
        self._name = name
        self._delegates = delegates

    @property
    def name(self):
        return self._name

    @property
    def description(self):
        return "真实已注册测试能力"

    @property
    def parameters_schema(self):
        return {"type": "object", "additionalProperties": False}

    @property
    def metadata(self):
        return ToolMetadata(delegated_tool_names=self._delegates)

    async def execute(self, **kwargs):
        return "执行结果"


@pytest.fixture
async def setup_service(tmp_path):
    config = SimpleNamespace(memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
    sessions = SessionStore(config.memory)
    registry = ToolRegistry()
    for name in (
        "file_read",
        "web_search",
        "personal_agents",
        "memory_recall",
        "delegate_task",
        "run_skill",
        "forge_tool",
        "tool_search",
        "session_list",
        "mcp__unreviewed",
    ):
        registry.register(NamedTool(name))

    async def forbidden_create(*args, **kwargs):
        raise AssertionError("Personal sessions must not switch the engine's active state")

    engine = SimpleNamespace(
        config=config,
        session_store=sessions,
        tool_registry=registry,
        router=SimpleNamespace(
            resolve_model=lambda tier: {
                ModelTier.FAST: "model-fast",
                ModelTier.CAPABLE: "model-capable",
                ModelTier.REASONING: "model-reasoning",
            }[tier]
        ),
        workspace_root=tmp_path / "workspace",
        _session=object(),
        _messages=[{"role": "user", "content": "主助理上下文"}],
        create_session=forbidden_create,
    )
    service = PersonalAgentService(engine, PersonalAgentStore(tmp_path / "personal"))
    yield service, engine
    await sessions.close()


def profile(service, request="one", **fields):
    return service.create(
        **{
            "name": "用户创建的专员",
            "instructions": "读取指定材料并给出依据",
            "request_id": request,
            **fields,
        }
    )


def error(code):
    return pytest.raises(PersonalAgentError, match="") if code is None else _Error(code)


class _Error:
    def __init__(self, code):
        self.code = code
        self.context = pytest.raises(PersonalAgentError)

    def __enter__(self):
        return self.context.__enter__()

    def __exit__(self, kind, value, traceback):
        result = self.context.__exit__(kind, value, traceback)
        assert value.code == self.code
        return result


async def test_empty_catalog_actual_tools_and_model_session_creation(setup_service):
    service, engine = setup_service
    assert service.list() == ()
    assert [x["name"] for x in service.assignable_tools()] == ["file_read", "web_search"]
    before = engine._session, engine._messages.copy()
    agent = profile(service, model_tier="reasoning", tool_names=["file_read"])
    opened = await service.open_session(agent.id)
    session = await engine.session_store.load(opened["session_id"])
    assert opened == {"agent_id": agent.id, "session_id": session.id, "version": 1}
    assert session.engine == "naumi" and session.model == "model-reasoning"
    assert session.workspace_root == str(Path(engine.workspace_root).resolve())
    assert session.title == agent.name and session.messages == []
    assert (engine._session, engine._messages) == before
    assert service.current(session.id) == agent
    assert service.current("unrelated-session") is None


@pytest.mark.parametrize(
    "tools",
    [
        ["missing"],
        ["personal_agents"],
        ["memory_recall"],
        ["delegate_task"],
        ["run_skill"],
        ["forge_tool"],
        ["tool_search"],
        ["session_list"],
        ["mcp__unreviewed"],
        ["default.file_read"],
        ["default__file_read"],
    ],
)
async def test_meta_and_unknown_tools_never_granted(setup_service, tools):
    service, _ = setup_service
    with error("tool_not_assignable"):
        profile(service, tool_names=tools)
    assert service.list() == ()


async def test_wrapped_leaf_tool_is_not_assignable(setup_service):
    service, engine = setup_service
    engine.tool_registry.register(NamedTool("file_read", delegates=("memory_recall",)))
    assert [x["name"] for x in service.assignable_tools()] == ["web_search"]
    with error("tool_not_assignable"):
        profile(service, tool_names=["file_read"])


@pytest.mark.parametrize("tools", ["file_read", [None], ["file_read", "file_read"], ["x"] * 129])
async def test_malformed_tool_collections_are_rejected(setup_service, tools):
    service, _ = setup_service
    with error("invalid_input"):
        profile(service, tool_names=tools)


async def test_empty_tool_scope_and_crud_history_use_same_store(setup_service):
    service, _ = setup_service
    first = profile(service)
    assert first.tool_names == ()
    renamed = service.update(first.id, first.version, name="新专员", tool_names=["web_search"])
    assert service.get(first.id) == renamed
    assert service.history(first.id) == (first, renamed)
    with error("version_conflict"):
        service.update(first.id, 1, name="冲突")
    archived = service.update(first.id, 2, status="archived")
    assert service.list() == ()
    assert service.list(include_archived=True) == (archived,)
    assert profile(service) == archived


async def test_paused_archived_and_team_are_not_executable(setup_service):
    service, _ = setup_service
    agent = profile(service)
    paused = service.update(agent.id, 1, status="paused")
    with error("agent_inactive"):
        await service.open_session(agent.id)
    service.update(agent.id, paused.version, status="archived")
    with error("agent_inactive"):
        await service.open_session(agent.id)
    team = profile(service, "team", kind="team")
    with error("team_execution_pending"):
        await service.open_session(team.id)


async def test_open_reuses_active_session_and_survives_service_restart(setup_service):
    service, engine = setup_service
    agent = profile(service)
    original = await service.open_session(agent.id)
    session = await engine.session_store.load(original["session_id"])
    session.add_message("user", "持久历史")
    await engine.session_store.save(session)
    restarted = PersonalAgentService(engine, PersonalAgentStore(service.store.base_dir))
    assert await restarted.open_session(agent.id) == original
    assert (await engine.session_store.load(session.id)).messages[-1]["content"] == "持久历史"
    assert (await engine.session_store.list_sessions())[1] == 1


@pytest.mark.parametrize("removed", [False, True])
async def test_deleted_or_archived_conversation_is_recovered_by_cas(setup_service, removed):
    service, engine = setup_service
    agent = profile(service)
    initial = await service.open_session(agent.id)
    if removed:
        await engine.session_store.delete(initial["session_id"])
    else:
        await engine.session_store.archive(initial["session_id"])
    recovered = await service.open_session(agent.id)
    assert recovered["session_id"] != initial["session_id"]
    assert service.current(initial["session_id"]) == agent
    assert service.current(recovered["session_id"]) == agent
    assert await service.open_session(agent.id) == recovered
    old = await engine.session_store.load(initial["session_id"])
    assert old is None if removed else old.status == "archived"


async def test_different_workspaces_and_session_databases_have_distinct_bindings(
    setup_service, tmp_path
):
    service, engine = setup_service
    agent = profile(service)
    one = await service.open_session(agent.id)
    other_engine = SimpleNamespace(**vars(engine))
    other_engine.workspace_root = tmp_path / "other-workspace"
    other = PersonalAgentService(other_engine, service.store)
    assert other.workspace_key != service.workspace_key
    assert other.session_db_key == service.session_db_key
    two = await other.open_session(agent.id)
    assert two["session_id"] != one["session_id"]
    assert other.current(one["session_id"]) is None
    second_memory = MemoryConfig(session_db_path=str(tmp_path / "second.db"))
    third_engine = SimpleNamespace(**vars(engine))
    third_engine.config = SimpleNamespace(memory=second_memory)
    third_engine.session_store = SessionStore(second_memory)
    try:
        third = PersonalAgentService(third_engine, service.store)
        assert third.workspace_key != service.workspace_key
        assert third.session_db_key != service.session_db_key
        assert third.current(one["session_id"]) is None
        three = await third.open_session(agent.id)
        assert three["session_id"] not in {one["session_id"], two["session_id"]}
    finally:
        await third_engine.session_store.close()


@pytest.mark.parametrize("engine_name,workspace", [("pi", "same"), ("naumi", "foreign")])
async def test_binding_does_not_adopt_wrong_workspace_or_engine(
    setup_service, engine_name, workspace
):
    service, engine = setup_service
    agent = profile(service)
    session = Session(
        engine=engine_name,
        workspace_root=(
            service.workspace_root if workspace == "same" else service.workspace_root + "-foreign"
        ),
    )
    await engine.session_store.save(session)
    service.store.bind_session(
        agent.id, service.workspace_key, session.id, session_db_key=service.session_db_key
    )
    with error("session_scope_mismatch"):
        await service.open_session(agent.id)
    assert (await engine.session_store.list_sessions())[1] == 1


async def test_removed_tools_revalidated_before_existing_session_reuse(setup_service):
    service, engine = setup_service
    agent = profile(service, tool_names=["file_read"])
    await service.open_session(agent.id)
    engine.tool_registry = ToolRegistry()
    with error("tool_not_assignable"):
        await service.open_session(agent.id)


async def test_model_resolution_failure_is_actionable_and_creates_no_session(setup_service):
    service, engine = setup_service
    agent = profile(service)

    def broken_model(tier):
        raise RuntimeError("provider detail must remain private")

    engine.router.resolve_model = broken_model
    with error("model_unavailable") as failure:
        await service.open_session(agent.id)
    assert "provider" not in str(failure.value)
    assert (await engine.session_store.list_sessions())[1] == 0


async def test_session_storage_failure_is_actionable(setup_service, monkeypatch):
    service, engine = setup_service
    agent = profile(service)

    async def broken_save(session):
        raise OSError("private storage details")

    monkeypatch.setattr(engine.session_store, "save", broken_save)
    with error("storage_error") as failure:
        await service.open_session(agent.id)
    assert "private" not in str(failure.value)
    assert service.store.get_binding(agent.id, service.workspace_key) is None


async def test_concurrent_openers_keep_one_winner_and_cleanup_orphans(setup_service):
    service, engine = setup_service
    agent = profile(service)
    # Initialize the shared SessionStore before exercising service-level races.
    await engine.session_store.list_sessions()
    services = [PersonalAgentService(engine, service.store) for _ in range(10)]
    opened = await asyncio.gather(*(s.open_session(agent.id) for s in services))
    assert len({x["session_id"] for x in opened}) == 1
    assert (await engine.session_store.list_sessions())[1] == 1


async def test_cancellation_after_save_removes_only_own_unbound_session(setup_service, monkeypatch):
    service, engine = setup_service
    ordinary = await engine.session_store.create_session(title="保留主会话")
    agent = profile(service)
    original_save = engine.session_store.save

    async def cancelled_save(session):
        await original_save(session)
        raise asyncio.CancelledError()

    monkeypatch.setattr(engine.session_store, "save", cancelled_save)
    with pytest.raises(asyncio.CancelledError):
        await service.open_session(agent.id)
    assert (await engine.session_store.list_sessions())[1] == 1
    assert await engine.session_store.load(ordinary.id) is not None
    assert service.store.get_binding(agent.id, service.workspace_key) is None


async def test_tool_same_service_crud_and_strict_parameters(setup_service):
    service, _ = setup_service
    tool = PersonalAgentsTool(service)
    assert tool.metadata.requires_confirmation is True
    assert json.loads(await tool.execute(operation="list")) == {"agents": []}
    created = json.loads(
        await tool.execute(
            operation="create",
            name="专员",
            instructions="整理资料",
            request_id="tool-create",
            tool_names=["file_read"],
        )
    )
    assert service.get(created["id"]).name == "专员"
    for bad in (
        {"operation": "create", "name": "missing"},
        {"operation": "list", "instructions": "无关字段"},
        {"operation": "get", "agent_id": created["id"], "extra": True},
        {"operation": "list", "include_archived": "true"},
    ):
        with pytest.raises(ToolExecutionError) as failure:
            await tool.execute(**bad)
        assert failure.value.code == "invalid_input"
    opened = json.loads(await tool.execute(operation="open", agent_id=created["id"]))
    assert service.current(opened["session_id"]).id == created["id"]
    changed = json.loads(
        await tool.execute(
            operation="update", agent_id=created["id"], expected_version=1, name="重命名"
        )
    )
    assert changed["version"] == 2
    revisions = json.loads(await tool.execute(operation="history", agent_id=created["id"]))
    assert len(revisions["revisions"]) == 2
