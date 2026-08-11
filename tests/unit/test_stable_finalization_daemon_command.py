from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner

from naumi_agent import main as main_module
from naumi_agent.runtime import composition as composition_module


def test_daemon_command_uses_explicit_foreground_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[str] = []

    async def run(config_path: str) -> None:
        called.append(config_path)

    monkeypatch.setattr(main_module, "_run_stable_finalization_daemon", run)
    result = CliRunner().invoke(
        main_module.app,
        ["stable-finalization-daemon", "--config", "daemon.yaml"],
    )

    assert result.exit_code == 0
    assert called == ["daemon.yaml"]


@pytest.mark.asyncio
async def test_daemon_foreground_releases_engine_when_owner_is_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = SimpleNamespace(
        start_stable_remote_finalization_installation_daemon=AsyncMock(
            return_value=False
        ),
        shutdown=AsyncMock(),
    )
    monkeypatch.setattr(
        main_module.AppConfig,
        "from_yaml",
        lambda _path: SimpleNamespace(log_level="INFO"),
    )
    monkeypatch.setattr(composition_module, "create_agent_engine", lambda _config: engine)
    monkeypatch.setattr("naumi_agent.log_setup.setup_logging", lambda _level: None)

    with pytest.raises(RuntimeError, match="live owner"):
        await main_module._run_stable_finalization_daemon("daemon.yaml")

    engine.shutdown.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_daemon_foreground_exits_after_runtime_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = SimpleNamespace(
        start_stable_remote_finalization_installation_daemon=AsyncMock(
            return_value=True
        ),
        stable_remote_finalization_installation_daemon_snapshot=lambda: (
            SimpleNamespace(lease_epoch=3, endpoint_url="https://localhost:8443")
        ),
        wait_stable_remote_finalization_installation_daemon=AsyncMock(
            return_value=SimpleNamespace(
                state=SimpleNamespace(value="failed"),
                failure_code="stable_installation_lease_lost",
            )
        ),
        shutdown=AsyncMock(),
    )
    monkeypatch.setattr(
        main_module.AppConfig,
        "from_yaml",
        lambda _path: SimpleNamespace(log_level="INFO"),
    )
    monkeypatch.setattr(composition_module, "create_agent_engine", lambda _config: engine)
    monkeypatch.setattr("naumi_agent.log_setup.setup_logging", lambda _level: None)

    with pytest.raises(RuntimeError, match="stable_installation_lease_lost"):
        await main_module._run_stable_finalization_daemon("daemon.yaml")

    engine.wait_stable_remote_finalization_installation_daemon.assert_awaited_once_with()
    engine.shutdown.assert_awaited_once_with()
