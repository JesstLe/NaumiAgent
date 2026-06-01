"""Doctor diagnostics tests."""

from __future__ import annotations

import pytest

from naumi_agent.config.settings import AppConfig
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.ui.doctor import render_doctor_report, run_doctor


def _config(tmp_path) -> AppConfig:
    config = AppConfig()
    config.models.api_key = "test-key"
    config.browser_daemon.enabled = False
    config.memory.session_db_path = str(tmp_path / "sessions.db")
    config.memory.vector_db_path = str(tmp_path / "chroma")
    config.workspace_root = str(tmp_path)
    return config


@pytest.mark.asyncio
async def test_run_doctor_checks_local_environment(tmp_path) -> None:
    config = _config(tmp_path)

    report = await run_doctor(config, workspace_root=tmp_path)
    rendered = render_doctor_report(report)

    names = {check.name: check for check in report.checks}
    assert names["Python 环境"].status == "pass"
    assert names["API key"].status == "pass"
    assert names["workspace 权限"].status == "pass"
    assert names["browser daemon"].status == "warn"
    assert names["debug log 写入权限"].status == "pass"
    assert "环境诊断" in rendered
    assert "可直接复制" in rendered


@pytest.mark.asyncio
async def test_run_doctor_reports_missing_api_key(tmp_path, monkeypatch) -> None:
    config = _config(tmp_path)
    config.models.api_key = None
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    report = await run_doctor(config, workspace_root=tmp_path)

    assert report.status == "error"
    assert next(check for check in report.checks if check.name == "API key").status == "error"


@pytest.mark.asyncio
async def test_doctor_tool_is_registered_and_uses_shared_report(tmp_path) -> None:
    config = _config(tmp_path)
    engine = AgentEngine(config)

    tool = engine.tool_registry.get("doctor_diagnostics")
    assert tool is not None

    output = await tool.execute()

    assert "## 环境诊断" in output
    assert "Python 环境" in output
    await engine.shutdown()
