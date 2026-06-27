"""Tests for the `serve` CLI default network boundary."""

from __future__ import annotations

from pathlib import Path

import pytest
import uvicorn
from typer.testing import CliRunner

from naumi_agent.main import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _patch_uvicorn_run(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def fake_run(*_args: object, **kwargs: object) -> None:
        calls.append({"kwargs": kwargs})

    monkeypatch.setattr(uvicorn, "run", fake_run)
    return calls


def test_serve_default_host_is_localhost(monkeypatch, runner: CliRunner) -> None:
    calls = _patch_uvicorn_run(monkeypatch)
    example_config = str(Path(__file__).resolve().parents[2] / "config.yaml.example")

    result = runner.invoke(app, ["serve", "--config", example_config])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]["kwargs"]["host"] == "127.0.0.1"


def test_serve_explicit_host_override_still_works(monkeypatch, runner: CliRunner) -> None:
    calls = _patch_uvicorn_run(monkeypatch)
    example_config = str(Path(__file__).resolve().parents[2] / "config.yaml.example")

    result = runner.invoke(
        app,
        ["serve", "--config", example_config, "--host", "0.0.0.0"],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]["kwargs"]["host"] == "0.0.0.0"


def test_serve_default_port_is_mac_workbench_daemon_port(monkeypatch, runner: CliRunner) -> None:
    calls = _patch_uvicorn_run(monkeypatch)
    example_config = str(Path(__file__).resolve().parents[2] / "config.yaml.example")

    result = runner.invoke(app, ["serve", "--config", example_config])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]["kwargs"]["port"] == 8765


def test_serve_explicit_port_override_still_works(monkeypatch, runner: CliRunner) -> None:
    calls = _patch_uvicorn_run(monkeypatch)
    example_config = str(Path(__file__).resolve().parents[2] / "config.yaml.example")

    result = runner.invoke(
        app,
        ["serve", "--config", example_config, "--port", "8080"],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]["kwargs"]["port"] == 8080
