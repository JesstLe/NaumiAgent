from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import naumi_agent.api.app as api_app


def _called_names(source: str) -> set[str]:
    return {
        node.func.id
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def test_product_sources_do_not_construct_agent_engine_directly() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "naumi_agent"
    main_source = (root / "main.py").read_text(encoding="utf-8")
    api_source = (root / "api" / "app.py").read_text(encoding="utf-8")

    main_calls = _called_names(main_source)
    api_calls = _called_names(api_source)

    assert "AgentEngine" not in main_calls
    assert "AgentEngine" not in api_calls
    assert "create_agent_engine" in main_calls
    assert "create_agent_engine" in api_calls


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
