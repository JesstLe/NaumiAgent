from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import naumi_agent.main as main_module


class _ReconfigurableStream:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def reconfigure(self, **kwargs: str) -> None:
        self.calls.append(kwargs)


def test_configure_windows_utf8_reconfigures_console_streams() -> None:
    stdout = _ReconfigurableStream()
    stderr = _ReconfigurableStream()

    main_module._configure_windows_utf8(
        platform="win32",
        streams=(stdout, stderr),
    )

    assert stdout.calls == [{"encoding": "utf-8", "errors": "replace"}]
    assert stderr.calls == [{"encoding": "utf-8", "errors": "replace"}]


def _install_engine(monkeypatch: pytest.MonkeyPatch, engine: object) -> None:
    module = ModuleType("naumi_agent.orchestrator.engine")
    module.AgentEngine = lambda _config: engine  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(main_module, "_resolve_config_path", lambda path: path)
    monkeypatch.setattr(
        main_module.AppConfig,
        "from_yaml",
        lambda _path: SimpleNamespace(log_level="INFO"),
    )
    monkeypatch.setattr(main_module, "_check_api_key", lambda _config: None)


@pytest.mark.asyncio
async def test_run_task_shuts_down_engine_after_success(monkeypatch) -> None:
    usage = SimpleNamespace(
        total_input_tokens=1,
        total_output_tokens=1,
        total_cost_usd=0.0,
        turns=1,
    )
    engine = SimpleNamespace(
        run=AsyncMock(
            return_value=SimpleNamespace(
                response="ok",
                usage=usage,
                status="completed",
            )
        ),
        shutdown=AsyncMock(),
        router=SimpleNamespace(resolve_model=lambda _tier: "test-model"),
    )
    _install_engine(monkeypatch, engine)

    await main_module._run_task("hello", "config.yaml")

    engine.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_task_shuts_down_engine_after_failure(monkeypatch) -> None:
    engine = SimpleNamespace(
        run=AsyncMock(side_effect=RuntimeError("boom")),
        shutdown=AsyncMock(),
    )
    _install_engine(monkeypatch, engine)

    await main_module._run_task("hello", "config.yaml")

    engine.shutdown.assert_awaited_once()
