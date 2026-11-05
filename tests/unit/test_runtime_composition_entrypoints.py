from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import naumi_agent.api.app as api_app


def _factory_routes(source: str) -> dict[str, int]:
    tree = ast.parse(source)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    assert not any(
        (isinstance(call.func, ast.Name) and call.func.id == "AgentEngine")
        or (isinstance(call.func, ast.Attribute) and call.func.attr == "AgentEngine")
        for call in calls
    ), "Entry points must use the composition root, not AgentEngine directly"
    routes = {}
    for function in tree.body:
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            count = sum(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "create_agent_engine"
                for node in ast.walk(function)
            )
            if count:
                routes[function.name] = count
    return routes


def test_product_sources_do_not_construct_agent_engine_directly() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "naumi_agent"
    main_source = (root / "main.py").read_text(encoding="utf-8")
    api_source = (root / "api" / "app.py").read_text(encoding="utf-8")

    assert _factory_routes(main_source) == {
        "_create_tui_engine": 1,
        "_chat": 1,
        "_create_single_task_engine": 1,
        "_run_stable_finalization_daemon": 1,
    }
    assert _factory_routes(api_source) == {"lifespan": 1}


def test_factory_routes_ignore_comments_and_call_formatting() -> None:
    assert _factory_routes('''
# AgentEngine(config); create_agent_engine(config)
async def entry(config):
    """AgentEngine(config) is documentation, not a constructor call."""
    return create_agent_engine(
        config=config,
    )
''') == {"entry": 1}


@pytest.mark.parametrize("constructor", ["AgentEngine", "runtime.AgentEngine"])
def test_factory_routes_reject_direct_construction(constructor: str) -> None:
    with pytest.raises(AssertionError, match="composition root"):
        _factory_routes(f"def entry(config):\n    return {constructor}(config=config)\n")


@pytest.mark.asyncio
async def test_api_lifespan_uses_root_and_shuts_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = SimpleNamespace(
        chat_run_store=object(),
        set_permission_confirmer=lambda _callback: None,
        start_long_running_services=AsyncMock(return_value=("recovered",)),
        shutdown=AsyncMock(),
    )
    config = SimpleNamespace()
    monkeypatch.setattr(api_app.AppConfig, "from_yaml", lambda _path: config)
    monkeypatch.setattr(api_app, "create_agent_engine", lambda value: engine)
    app = SimpleNamespace(state=SimpleNamespace())

    async with api_app.lifespan(app):
        assert app.state.engine is engine
        assert app.state.config is config
        assert app.state.session_reconciliation_recovery == ("recovered",)

    engine.start_long_running_services.assert_awaited_once()
    engine.shutdown.assert_awaited_once()
