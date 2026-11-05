"""Isolated personal executions use the authoritative, identity-preserving composition root."""

from __future__ import annotations

import ast
import hashlib
import inspect
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from naumi_agent.config.settings import AppConfig, ModelConfig
from naumi_agent.model.router import ModelRouter
from naumi_agent.personal import engine_execution
from naumi_agent.runtime import composition
from naumi_agent.runtime.paths import RuntimePaths
from naumi_agent.safety.permissions import PermissionMode
from naumi_agent.tools import analysis, pursuit

pytestmark = pytest.mark.usefixtures("runtime_payload_key")
SHARED_PATHS = frozenset({
    "workspace_root", "session_db_path", "runtime_data_dir", "chat_run_db_path",
})
KEY = "a" * 32
NESTED_KEY = "b" * 32
SIBLING_KEY = "c" * 32


def _factory():
    factory = getattr(composition, "create_isolated_agent_engine", None)
    assert callable(factory), "Isolated Engines need the explicit composition-root factory"
    return factory


def _config(tmp_path: Path) -> AppConfig:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return AppConfig(
        workspace_root=str(workspace),
        personal={"data_dir": str(tmp_path / "personal")},
        memory={
            "session_db_path": str(tmp_path / "runtime" / "sessions.db"),
            "vector_db_path": str(tmp_path / "vectors"), "long_term_enabled": False,
        },
        models=ModelConfig(
            default_model="openai/gpt-4o-mini", fast_model="openai/gpt-4o-mini",
            reasoning_model="openai/gpt-4o-mini", api_key="isolated-composition-only",
            api_base="http://127.0.0.1:1/v1",
        ),
        safety={"permission_mode": "strict", "allowed_dirs": [str(workspace)]},
        skills={"search_paths": []},
    )


def _tree(root: Path) -> dict[str, tuple[bytes, int]]:
    return {path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in root.rglob("*") if path.is_file()}


def _assert_paths(engine, base: RuntimePaths, key: str, nested: str | None = None) -> Path:
    directory = base.runtime_data_dir / "personal-executions" / key
    if nested is not None:
        directory = directory / "p" / nested
    assert engine._isolated_child is True
    assert engine._execution_data_dir == directory
    for field in fields(RuntimePaths):
        actual = getattr(engine._paths, field.name)
        if field.name in SHARED_PATHS:
            assert actual == getattr(base, field.name), field.name
        else:
            field_key = hashlib.sha256(field.name.encode()).hexdigest()[:12]
            expected = directory / field_key / getattr(base, field.name).name
            assert actual == expected, field.name
            assert actual.is_relative_to(directory), field.name
            assert actual != getattr(base, field.name), field.name
    assert engine.background_runner.store.base_dir == directory / "background"
    assert engine.scheduler_runner.store.base_dir == directory / "scheduler"
    return directory


def _assert_bundles(engine) -> None:
    resources, paths, services = engine._resources, engine._paths, engine._services
    pairs = {
        "chat_run_store": "chat_run_db_path", "terminal_event_store": "terminal_event_db_path",
        "worker_registry_store": "worker_registry_db_path",
        "execution_grant_store": "execution_grant_db_path",
        "run_delegation_grant_store": "run_delegation_grant_db_path",
        "permission_decision_store": "permission_decision_db_path",
        "tool_job_store": "tool_job_db_path", "agent_job_store": "agent_job_db_path",
        "evolution_candidate_store": "evolution_db_path",
        "tool_catalog_miss_store": "evolution_db_path", "harness_store": "harness_db_path",
        "task_store": "session_db_path", "workbench_store": "session_db_path",
    }
    for resource, path in pairs.items():
        assert getattr(resources, resource).db_path == getattr(paths, path), resource
    assert resources.harness_trust_store._db_path == paths.harness_trust_db_path
    assert resources.goal_store.base_dir == paths.goal_storage_dir
    assert resources.pursuit_store.base_dir == paths.pursuit_storage_dir
    assert Path(engine._session_port._db_path).resolve() == paths.session_db_path
    assert engine.router is engine._model_port
    assert services.agent_execution_heartbeat_factory.store is resources.harness_store
    assert services.browser_execution_heartbeat_factory.store is resources.harness_store
    assert services.terminal_runtime_lifecycle_factory.store is resources.harness_store
    worker = services.agent_worker_process_factory
    assert worker.worker_registry is resources.worker_registry_store
    assert worker.agent_job_store is resources.agent_job_store
    assert worker.heartbeat_store is resources.harness_store
    assert worker.runtime_dir == paths.agent_worker_runtime_dir
    assert engine.subagent_manager._agent_job_store is resources.agent_job_store
    assert engine.subagent_manager._agent_worker_process_factory is worker
    assert engine.scheduler_runner._loop_task is None
    assert engine.subagent_manager._reaper_task is None
    assert engine._task_runner is None
    assert engine.background_runner._processes == {}
    assert engine.background_runner._watchers == {}


@pytest.fixture
async def parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NAUMI_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    for module, name in ((analysis, "_global_router"), (analysis, "_global_subagent_manager"),
                         (pursuit, "_global_pursuit_loop")):
        monkeypatch.setattr(module, name, getattr(module, name))

    async def no_provider(*_args: Any, **_kwargs: Any):
        raise AssertionError("No provider call belongs to composition tests")

    async def no_stream(*_args: Any, **_kwargs: Any):
        raise AssertionError("No provider stream belongs to composition tests")
        yield  # pragma: no cover

    monkeypatch.setattr(ModelRouter, "call", no_provider)
    monkeypatch.setattr(ModelRouter, "stream", no_stream)
    engine = composition.create_agent_engine(_config(tmp_path))
    try:
        yield engine
    finally:
        await engine.shutdown()


def test_personal_execution_constructor_delegates_to_the_composition_root() -> None:
    source = inspect.getsource(engine_execution.make_execution_child)
    tree = ast.parse(source)
    names = {node.func.id for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert "AgentEngine" not in names
    assert "create_isolated_agent_engine" in names


@pytest.mark.parametrize("field,value", [
    ("execution_key", value) for value in (
        None, False, 7, b"a" * 32, "", "a" * 31, "a" * 33, "A" * 32,
        "../" + "a" * 32, "a" * 31 + "/", "g" * 32, "a" * 31 + "\x00",
    )
] + [
    ("nested_execution_key", value) for value in (
        False, 7, b"b" * 32, "", "b" * 31, "b" * 33, "B" * 32,
        "../" + "b" * 32, "b" * 31 + "\\", "h" * 32, "b" * 31 + "\n",
    )
])
def test_invalid_identity_keys_fail_before_any_builder_or_filesystem_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: Any,
) -> None:
    factory = _factory()
    config = _config(tmp_path)
    before, called = _tree(tmp_path), []

    def forbidden(*_args: Any, **_kwargs: Any):
        called.append("builder")
        raise AssertionError("Invalid identity reached a runtime builder")

    for name in ("build_runtime_paths", "build_runtime_ports", "build_runtime_resources",
                 "build_runtime_services"):
        monkeypatch.setattr(composition, name, forbidden)
    arguments = {"execution_key": KEY, field: value}
    with pytest.raises((TypeError, ValueError)):
        factory(config, **arguments)
    assert called == []
    assert _tree(tmp_path) == before


@pytest.mark.parametrize("nested", [None, NESTED_KEY])
async def test_real_factory_binds_every_isolated_path_and_complete_dependency_bundle(
    parent, nested,
):
    base = parent._paths
    config = parent.config.model_copy(deep=True)
    before = config.model_dump()
    child = _factory()(config, execution_key=KEY, nested_execution_key=nested)
    try:
        _assert_paths(child, base, KEY, nested)
        _assert_bundles(child)
        assert config.model_dump() == before
        assert child._session_port is not parent._session_port
        assert child.router is not parent.router
        assert child._resources is not parent._resources
        assert child._services is not parent._services
        assert child._permission_port.mode is PermissionMode.STRICT
        assert str(child._paths.worktree_storage_dir) in child._permission_port._allowed_dirs
        original_paths = child._paths
        goal = child.goal_store.create("同一执行键的原记录", session_id="same-key-proof")
    finally:
        await child.shutdown()
    reopened = _factory()(config, execution_key=KEY, nested_execution_key=nested)
    try:
        assert reopened._paths == original_paths
        assert reopened.goal_store.get(goal.id).objective == "同一执行键的原记录"
        _assert_bundles(reopened)
    finally:
        await reopened.shutdown()


@pytest.mark.parametrize("cost", [None, 0, 25000])
@pytest.mark.parametrize("team", [None, {"max_input_tokens": 1234, "max_output_tokens": 567}])
def test_make_child_preserves_original_identity_config_budgets_and_memory_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cost: int | None, team: dict | None,
) -> None:
    _factory()
    config = _config(tmp_path)
    config.memory.long_term_enabled = True
    parent = SimpleNamespace(config=config, personal_memory_runtime=SimpleNamespace(enabled=True))
    job = SimpleNamespace(job_id="original-personal-job", request=SimpleNamespace(
        permission_mode="moderate", max_turns=7, max_cost_microusd=cost,
    ))
    captured = {}
    child = SimpleNamespace()

    def factory(child_config, **keys):
        captured.update(config=child_config, keys=keys)
        return child

    monkeypatch.setattr(composition, "create_isolated_agent_engine", factory)
    before = config.model_dump()
    result = engine_execution.make_execution_child(
        parent, job, team_execution=team, peer_message_id="pm_" + NESTED_KEY,
    )
    assert result is child
    assert captured["keys"] == {
        "execution_key": hashlib.sha256(job.job_id.encode()).hexdigest()[:32],
        "nested_execution_key": NESTED_KEY,
    }
    isolated = captured["config"]
    assert isolated is not config and isolated.models is not config.models
    assert isolated.memory.long_term_enabled is False
    assert isolated.safety.permission_mode == "moderate"
    assert isolated.safety.max_turns == 7
    assert isolated.safety.max_budget_usd == (None if cost is None else cost / 1_000_000)
    if team is not None:
        assert isolated.safety.max_input_tokens == team["max_input_tokens"]
        assert isolated.safety.max_output_tokens == team["max_output_tokens"]
    else:
        assert isolated.safety.max_input_tokens == config.safety.max_input_tokens
        assert isolated.safety.max_output_tokens == config.safety.max_output_tokens
    assert child._personal_memory_parent_enabled is True
    assert child._personal_memory_policy_parent is parent
    assert config.model_dump() == before


async def test_child_shutdown_preserves_parent_global_bindings_shared_history_and_sibling(parent):
    factory = _factory()
    parent_session = await parent.get_or_create_session(title="原主 Agent 会话")
    globals_before = (analysis._global_router, analysis._global_subagent_manager,
                      pursuit._global_pursuit_loop)
    assert globals_before[0] is parent.router
    assert globals_before[1] is parent.subagent_manager
    child = sibling = None
    try:
        child = factory(parent.config.model_copy(deep=True), execution_key=KEY)
        sibling = factory(parent.config.model_copy(deep=True), execution_key=SIBLING_KEY)
        child_directory = _assert_paths(child, parent._paths, KEY)
        sibling_directory = _assert_paths(sibling, parent._paths, SIBLING_KEY)
        assert child_directory != sibling_directory
        assert (analysis._global_router, analysis._global_subagent_manager,
                pursuit._global_pursuit_loop) == globals_before
        child_session = await child.get_or_create_session(title="原子执行会话")
        assert child_session.id != parent_session.id
        reopened = await parent.session_store.load(child_session.id)
        assert reopened is not None and reopened.id == child_session.id
        goal = child.goal_store.create("仅属于目标子执行", session_id=child_session.id)
        assert child.goal_store.get(goal.id).objective == "仅属于目标子执行"
        assert parent.goal_store.get(goal.id) is None
        assert sibling.goal_store.get(goal.id) is None
        assert parent._session.id == parent_session.id
        await child.shutdown()
        child = None
        assert (analysis._global_router, analysis._global_subagent_manager,
                pursuit._global_pursuit_loop) == globals_before
        assert (await parent.session_store.load(parent_session.id)).id == parent_session.id
        sibling_session = await sibling.get_or_create_session(title="兄弟子执行继续可用")
        assert sibling_session.id not in {child_session.id, parent_session.id}
        assert (await parent.session_store.load(sibling_session.id)).id == sibling_session.id
        _assert_bundles(sibling)
    finally:
        if child is not None:
            await child.shutdown()
        if sibling is not None:
            await sibling.shutdown()
