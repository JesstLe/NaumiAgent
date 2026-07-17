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


def _install_engine(
    monkeypatch: pytest.MonkeyPatch,
    engine: object,
) -> dict[str, object]:
    captured: dict[str, object] = {}
    module = ModuleType("naumi_agent.runtime.composition")

    def create_agent_engine(config: object) -> object:
        captured["config"] = config
        return engine

    module.create_agent_engine = create_agent_engine  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(main_module, "_resolve_config_path", lambda path: path)
    parsed = SimpleNamespace(log_level="INFO")
    monkeypatch.setattr(
        main_module.AppConfig,
        "from_yaml",
        lambda _path: parsed,
    )
    monkeypatch.setattr(main_module, "_check_api_key", lambda _config: None)
    captured["parsed"] = parsed
    return captured


@pytest.mark.asyncio
async def test_long_running_engine_start_failure_closes_engine() -> None:
    engine = SimpleNamespace(
        start_long_running_services=AsyncMock(
            side_effect=RuntimeError("recovery failed")
        ),
        shutdown=AsyncMock(),
    )

    with pytest.raises(RuntimeError, match="recovery failed"):
        await main_module._start_long_running_engine(engine)

    engine.shutdown.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_run_task_shuts_down_engine_after_success(monkeypatch) -> None:
    usage = SimpleNamespace(
        total_input_tokens=1,
        total_output_tokens=1,
        total_cost_usd=0.0,
        turns=1,
    )
    engine = SimpleNamespace(
        recover_session_reconciliations=AsyncMock(return_value=()),
        start_long_running_services=AsyncMock(return_value=()),
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
    captured = _install_engine(monkeypatch, engine)

    await main_module._run_task("hello", "config.yaml")

    assert captured["config"] is captured["parsed"]
    engine.recover_session_reconciliations.assert_awaited_once_with()
    engine.start_long_running_services.assert_not_awaited()
    engine.run.assert_awaited_once_with("hello")
    engine.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_task_shuts_down_engine_after_failure(monkeypatch) -> None:
    engine = SimpleNamespace(
        recover_session_reconciliations=AsyncMock(return_value=()),
        start_long_running_services=AsyncMock(return_value=()),
        run=AsyncMock(side_effect=RuntimeError("boom")),
        shutdown=AsyncMock(),
    )
    captured = _install_engine(monkeypatch, engine)

    await main_module._run_task("hello", "config.yaml")

    assert captured["config"] is captured["parsed"]
    engine.start_long_running_services.assert_not_awaited()
    engine.shutdown.assert_awaited_once()
