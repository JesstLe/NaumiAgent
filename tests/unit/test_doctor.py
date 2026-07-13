"""Doctor diagnostics tests."""

from __future__ import annotations

import pytest

from naumi_agent.config.settings import AppConfig
from naumi_agent.model.router import ModelResponse
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.ui.doctor import (
    _check_search_readiness,
    render_doctor_report,
    run_doctor,
)


def _config(tmp_path) -> AppConfig:
    config = AppConfig()
    config.models.provider = "custom"
    config.models.default_model = "openai/test-model"
    config.models.fast_model = "openai/test-model"
    config.models.reasoning_model = "openai/test-model"
    config.models.api_base = "https://example.test/v1"
    config.models.api_key = "test-key"
    config.browser_daemon.enabled = False
    config.memory.session_db_path = str(tmp_path / "sessions.db")
    config.memory.vector_db_path = str(tmp_path / "chroma")
    config.workspace_root = str(tmp_path)
    return config


@pytest.mark.asyncio
async def test_run_doctor_checks_local_environment(tmp_path) -> None:
    config = _config(tmp_path)

    report = await run_doctor(
        config,
        workspace_root=tmp_path,
        browser_fallback_available=True,
    )
    rendered = render_doctor_report(report)

    names = {check.name: check for check in report.checks}
    assert names["Python 环境"].status == "pass"
    assert names["API key"].status == "pass"
    assert names["网络搜索"].status == "pass"
    assert "零配置" in names["网络搜索"].detail
    assert names["workspace 权限"].status == "pass"
    assert names["browser daemon"].status == "warn"
    assert names["debug log 写入权限"].status == "pass"
    assert "环境诊断" in rendered
    assert "可直接复制" in rendered


@pytest.mark.asyncio
async def test_doctor_reports_enhanced_search_when_brave_key_exists(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "configured-secret")

    report = await run_doctor(
        _config(tmp_path),
        workspace_root=tmp_path,
        browser_fallback_available=True,
    )

    check = next(item for item in report.checks if item.name == "网络搜索")
    assert check.status == "pass"
    assert "已增强" in check.detail
    assert "configured-secret" not in check.detail


def test_search_readiness_reports_restricted_without_any_route(monkeypatch) -> None:
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)

    check = _check_search_readiness(
        direct_search_available=False,
        browser_fallback_available=False,
    )

    assert check.status == "warn"
    assert "受限" in check.detail


def test_search_readiness_warns_when_browser_runtime_is_missing(monkeypatch) -> None:
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)

    check = _check_search_readiness(
        direct_search_available=True,
        browser_fallback_available=False,
    )

    assert check.status == "warn"
    assert "浏览器回退不可用" in check.detail
    assert "playwright install chromium" in check.suggestion


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
@pytest.mark.parametrize("provider", ["kimi", None])
async def test_doctor_rejects_claude_model_with_kimi_api_base(
    tmp_path,
    provider: str | None,
) -> None:
    config = _config(tmp_path)
    config.models.provider = provider
    config.models.default_model = "claude-sonnet-4-6"
    config.models.fast_model = "claude-haiku-4-5"
    config.models.reasoning_model = "claude-opus-4-7"
    config.models.api_base = "https://api.kimi.com/coding/v1"

    report = await run_doctor(config, workspace_root=tmp_path)

    check = next(item for item in report.checks if item.name == "model provider")
    assert check.status == "error"
    assert "kimi" in check.detail
    assert "naumi configure" in check.suggestion


@pytest.mark.asyncio
async def test_doctor_accepts_consistent_kimi_configuration(tmp_path) -> None:
    config = _config(tmp_path)
    config.models.provider = "kimi"
    config.models.default_model = "openai/kimi-for-coding"
    config.models.fast_model = "openai/kimi-for-coding"
    config.models.reasoning_model = "openai/kimi-for-coding"
    config.models.api_base = "https://api.kimi.com/coding/v1"

    report = await run_doctor(config, workspace_root=tmp_path)

    check = next(item for item in report.checks if item.name == "model provider")
    assert check.status == "pass"
    assert "kimi" in check.detail


@pytest.mark.asyncio
async def test_doctor_rejects_invalid_kimi_temperature_override(tmp_path) -> None:
    config = _config(tmp_path)
    config.models.provider = "kimi"
    config.models.default_model = "openai/kimi-for-coding"
    config.models.fast_model = "openai/kimi-for-coding"
    config.models.reasoning_model = "openai/kimi-for-coding"
    config.models.api_base = "https://api.kimi.com/coding/v1"
    config.models.temperature = 0.7

    report = await run_doctor(config, workspace_root=tmp_path)

    check = next(item for item in report.checks if item.name == "model provider")
    assert check.status == "error"
    assert "temperature" in check.detail
    assert "NAUMI_MODELS__TEMPERATURE" in check.suggestion


@pytest.mark.asyncio
async def test_live_doctor_skips_network_when_provider_configuration_is_invalid(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    config.models.provider = "kimi"
    config.models.default_model = "claude-sonnet-4-6"
    config.models.api_base = "https://api.kimi.com/coding/v1"

    async def fail_probe(_config: AppConfig) -> ModelResponse:
        pytest.fail("invalid local configuration must not reach the network")

    report = await run_doctor(
        config,
        workspace_root=tmp_path,
        live=True,
        live_probe=fail_probe,
    )

    check = next(item for item in report.checks if item.name == "模型实时连接")
    assert check.status == "error"
    assert "已跳过" in check.detail


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


@pytest.mark.asyncio
async def test_doctor_does_not_probe_model_without_live_flag(tmp_path) -> None:
    async def fail_probe(_config: AppConfig) -> ModelResponse:
        pytest.fail("local doctor must not call the model")

    report = await run_doctor(
        _config(tmp_path),
        workspace_root=tmp_path,
        live_probe=fail_probe,
    )

    assert all(check.name != "模型实时连接" for check in report.checks)


@pytest.mark.asyncio
async def test_live_doctor_reports_model_and_latency_without_response_body(tmp_path) -> None:
    async def successful_probe(_config: AppConfig) -> ModelResponse:
        return ModelResponse(content="private response", model="openai/test-model")

    report = await run_doctor(
        _config(tmp_path),
        workspace_root=tmp_path,
        live=True,
        live_probe=successful_probe,
    )

    check = next(item for item in report.checks if item.name == "模型实时连接")
    assert check.status == "pass"
    assert "openai/test-model" in check.detail
    assert "ms" in check.detail
    assert "private response" not in check.detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "detail"),
    [
        ("401 invalid authentication secret-value", "认证失败（401）"),
        ("404 resource not found secret-value", "模型或 API 地址不存在（404）"),
        ("429 rate limit secret-value", "服务限流（429）"),
        ("request timeout secret-value", "连接超时"),
    ],
)
async def test_live_doctor_classifies_errors_without_leaking_raw_message(
    tmp_path,
    message: str,
    detail: str,
) -> None:
    async def failed_probe(_config: AppConfig) -> ModelResponse:
        raise RuntimeError(message)

    report = await run_doctor(
        _config(tmp_path),
        workspace_root=tmp_path,
        live=True,
        live_probe=failed_probe,
    )

    check = next(item for item in report.checks if item.name == "模型实时连接")
    assert detail in check.detail
    assert "secret-value" not in check.detail
