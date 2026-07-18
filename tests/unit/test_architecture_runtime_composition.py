from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = PROJECT_ROOT / "src" / "naumi_agent"
TEST_ROOT = PROJECT_ROOT / "tests"
ENGINE_PATH = SOURCE_ROOT / "orchestrator" / "engine.py"
DEPENDENCIES_PATH = SOURCE_ROOT / "runtime" / "dependencies.py"
COMPOSITION_PATH = SOURCE_ROOT / "runtime" / "composition.py"
PATHS_PATH = SOURCE_ROOT / "runtime" / "paths.py"
_BANNED_ENGINE_NAMES = {
    "ChatRunStore",
    "EvolutionCandidateStore",
    "HarnessStore",
    "HarnessTrustStore",
    "LocalToolExecutor",
    "ModelRouter",
    "NullEventSink",
    "PermissionChecker",
    "SessionStore",
    "TaskStore",
    "WorkbenchStore",
    "load_provider_catalog",
}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_names(path: Path) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            imported.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.update(alias.asname or alias.name for alias in node.names)
    return imported


def _agent_engine_calls(root: Path) -> list[tuple[Path, ast.Call]]:
    calls: list[tuple[Path, ast.Call]] = []
    for path in root.rglob("*.py"):
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "AgentEngine"
            ):
                calls.append((path, node))
    return calls


def _constructor_paths(root: Path, names: set[str]) -> dict[str, list[Path]]:
    calls = {name: [] for name in names}
    for path in root.rglob("*.py"):
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in names
            ):
                calls[node.func.id].append(path)
    return calls


def test_engine_imports_and_constructs_no_default_port_adapter() -> None:
    assert _imported_names(ENGINE_PATH).isdisjoint(_BANNED_ENGINE_NAMES)
    called = {
        node.func.id
        for node in ast.walk(_tree(ENGINE_PATH))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert called.isdisjoint(_BANNED_ENGINE_NAMES)


def test_dependency_bundle_has_protocol_only_imports() -> None:
    allowed = {
        "__future__",
        "dataclasses",
        "naumi_agent.runtime.ports.events",
        "naumi_agent.runtime.ports.model",
        "naumi_agent.runtime.ports.permission",
        "naumi_agent.runtime.ports.session",
        "naumi_agent.runtime.ports.tool_execution",
    }
    modules = {
        node.module
        for node in ast.walk(_tree(DEPENDENCIES_PATH))
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert modules <= allowed


def test_only_composition_root_constructs_agent_engine_in_product_source() -> None:
    calls = _agent_engine_calls(SOURCE_ROOT)

    assert [path for path, _ in calls] == [COMPOSITION_PATH]
    assert all(
        {keyword.arg for keyword in call.keywords} >= {"ports", "paths", "resources"}
        for _, call in calls
    )


def test_only_composition_root_constructs_harness_resources() -> None:
    calls = _constructor_paths(SOURCE_ROOT, {"HarnessStore", "HarnessTrustStore"})

    assert calls == {
        "HarnessStore": [COMPOSITION_PATH],
        "HarnessTrustStore": [COMPOSITION_PATH],
    }


def test_only_composition_root_constructs_evolution_candidate_store() -> None:
    calls = _constructor_paths(SOURCE_ROOT, {"EvolutionCandidateStore"})

    assert calls == {"EvolutionCandidateStore": [COMPOSITION_PATH]}


def test_only_composition_root_constructs_chat_run_store() -> None:
    calls = _constructor_paths(SOURCE_ROOT, {"ChatRunStore"})

    assert calls == {"ChatRunStore": [COMPOSITION_PATH]}


def test_task_and_workbench_default_resources_have_owned_constructors() -> None:
    calls = _constructor_paths(SOURCE_ROOT, {"TaskStore", "WorkbenchStore"})

    assert calls["WorkbenchStore"] == [COMPOSITION_PATH]
    assert set(calls["TaskStore"]) == {
        COMPOSITION_PATH,
        SOURCE_ROOT / "tasks" / "store.py",
    }
    assert len(calls["TaskStore"]) == 2


def test_runtime_paths_contract_has_no_adapter_or_config_imports() -> None:
    allowed = {"__future__", "dataclasses", "pathlib"}
    modules = {
        node.module
        for node in ast.walk(_tree(PATHS_PATH))
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert modules <= allowed


def test_bridge_default_factory_does_not_assign_agent_engine() -> None:
    bridge = _tree(SOURCE_ROOT / "ui" / "bridge.py")
    assignments = [
        node
        for node in ast.walk(bridge)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and isinstance(node.value, ast.Name)
        and node.value.id == "AgentEngine"
    ]
    assert assignments == []


def test_legacy_test_constructor_budget_never_exceeds_arc_01_4a_baseline() -> None:
    legacy = [
        (path, call)
        for path, call in _agent_engine_calls(TEST_ROOT)
        if not any(keyword.arg == "ports" for keyword in call.keywords)
    ]

    assert len(legacy) <= 171
