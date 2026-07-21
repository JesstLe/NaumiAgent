from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import naumi_agent.api.app as api_app


def test_product_sources_do_not_construct_agent_engine_directly() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "naumi_agent"
    main_source = (root / "main.py").read_text(encoding="utf-8")
    api_source = (root / "api" / "app.py").read_text(encoding="utf-8")

    assert "AgentEngine(config)" not in main_source
    assert "AgentEngine(config)" not in api_source
    assert main_source.count("create_agent_engine(config)") == 3
    assert api_source.count("create_agent_engine(config)") == 1


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
