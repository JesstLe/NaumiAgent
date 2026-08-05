"""Doctor diagnostics tests."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from naumi_agent.config.settings import AppConfig, ModelMeta
from naumi_agent.model.router import ModelResponse, ModelRouter
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.ui.doctor import (
    _check_search_readiness,
    _check_state_store_catalog,
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
async def test_run_doctor_checks_local_environment(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NAUMI_STATE_HOME", str(tmp_path / "user-state"))
    config = _config(tmp_path)

    report = await run_doctor(
        config,
        workspace_root=tmp_path,
        browser_fallback_available=True,
    )
    rendered = render_doctor_report(report)

    names = {check.name: check for check in report.checks}
    assert names["Python 环境"].status == "pass"
    assert names["Node.js"].status in {"pass", "warn"}
    assert names["API key"].status == "pass"
    assert names["网络搜索"].status == "pass"
    assert "零配置" in names["网络搜索"].detail
    assert names["workspace 权限"].status == "pass"
    assert names["browser daemon"].status == "warn"
    assert names["debug log 写入权限"].status == "pass"
    assert names["状态存储目录"].status == "pass"
    assert "已登记 18 个" in names["状态存储目录"].detail
    assert "环境诊断" in rendered
    assert "可直接复制" in rendered


def test_state_store_doctor_names_unversioned_and_corrupt_physical_store(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    monkeypatch.setenv("NAUMI_STATE_HOME", str(tmp_path / "user-state"))
    config = _config(tmp_path)
    with sqlite3.connect(config.memory.session_db_path) as connection:
        connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")

    warning = _check_state_store_catalog(config)

    assert warning.status == "warn"
    assert "runtime.core" in warning.detail
    assert "未版本化" in warning.suggestion

    Path(config.memory.session_db_path).write_bytes(b"broken sqlite")
    error = _check_state_store_catalog(config)
    assert error.status == "error"
    assert "runtime.core" in error.detail
    assert "不要手工覆盖" in error.suggestion


@pytest.mark.asyncio
async def test_doctor_summarizes_unique_model_capability_contract(tmp_path) -> None:
    config = _config(tmp_path)
    config.models.model_info["openai/test-model"] = ModelMeta(
        max_context=128000,
        max_output=8192,
        input_cost_per_million=1,
        output_cost_per_million=4,
        supports_tools=True,
        supports_streaming=True,
        supports_parallel_tools=True,
        supports_structured_output=True,
        supports_reasoning=False,
        supports_vision=False,
        input_modalities=("text",),
        output_modalities=("text",),
    )
    router = ModelRouter(config.models)

    report = await run_doctor(
        config,
        workspace_root=tmp_path,
        browser_fallback_available=True,
        model_router=router,
    )

    contract_checks = [item for item in report.checks if item.name.startswith("模型契约")]
    assert len(contract_checks) == 1
    assert contract_checks[0].status == "pass"
    assert "context 128000" in contract_checks[0].detail


@pytest.mark.asyncio
async def test_doctor_reports_catalog_load_error_without_crashing(tmp_path) -> None:
    report = await run_doctor(
        _config(tmp_path),
        workspace_root=tmp_path,
        browser_fallback_available=True,
        model_router_error="provider.vendor.models.bad.limit.context 必须是正整数",
    )

    check = next(item for item in report.checks if item.name == "模型契约")
    assert check.status == "error"
    assert "limit.context" in check.detail
    assert "修复 catalog" in check.suggestion


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


@pytest.mark.asyncio
async def test_doctor_uses_custom_brave_environment_reference(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    monkeypatch.setenv("NAUMI_CUSTOM_BRAVE_KEY", "custom-search-secret")
    config = _config(tmp_path)
    config.search.brave.api_key_ref = "{env:NAUMI_CUSTOM_BRAVE_KEY}"

    report = await run_doctor(
        config,
        workspace_root=tmp_path,
        browser_fallback_available=True,
    )

    check = next(item for item in report.checks if item.name == "网络搜索")
    assert check.status == "pass"
    assert "已增强" in check.detail
    assert "custom-search-secret" not in render_doctor_report(report)


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
    check = next(check for check in report.checks if check.name == "API key")
    assert check.status == "error"
    assert check.diagnostic_code == "provider_credentials_missing"
    assert "`provider_credentials_missing`" in render_doctor_report(report)


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
    assert check.diagnostic_code == "provider_config_invalid"
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
async def test_doctor_reports_stable_code_when_default_model_is_missing(tmp_path) -> None:
    config = _config(tmp_path)
    config.models.default_model = ""

    report = await run_doctor(config, workspace_root=tmp_path)

    check = next(item for item in report.checks if item.name == "model provider")
    assert check.status == "error"
    assert check.diagnostic_code == "provider_model_missing"


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
    assert check.diagnostic_code == "provider_temperature_invalid"
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
    assert check.diagnostic_code == "provider_prerequisite_failed"


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
async def test_doctor_live_probe_tool_is_registered_with_exact_budget(
    tmp_path,
) -> None:
    engine = AgentEngine(_config(tmp_path))
    try:
        tool = engine.tool_registry.get("doctor_live_probe")
        assert tool is not None
        assert tool.metadata.read_only is False
        assert tool.metadata.requires_confirmation is False
        assert tool.parameters_schema["properties"]["timeout_ms"] == {
            "type": "integer",
            "minimum": 1000,
            "maximum": 60000,
            "default": 15000,
            "description": "单次模型请求超时，范围 1000..60000 毫秒。",
        }
        assert "1 个请求" in tool.description
        assert "8 个输出 token" in tool.description
        assert "不会自动重试" in tool.description
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_doctor_export_tool_previews_then_writes_platform_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_home = tmp_path / "state"
    monkeypatch.setenv("NAUMI_STATE_HOME", str(state_home))
    engine = AgentEngine(_config(tmp_path))
    try:
        tool = engine.tool_registry.get("doctor_export_diagnostics")
        assert tool is not None
        assert tool.metadata.read_only is False
        assert tool.metadata.requires_confirmation is False

        with pytest.raises(ValueError, match="缺少本进程内"):
            await tool.execute(
                action="write",
                expected_snapshot_sha256="a" * 64,
            )

        preview = await tool.execute(action="preview")
        digest = re.search(r"来源快照：`([0-9a-f]{64})`", preview)
        preview_bundle = re.search(r"Bundle：`([0-9a-f]{64})`", preview)
        assert digest is not None
        assert preview_bundle is not None
        assert "脱敏诊断包预览" in preview
        assert not (state_home / "diagnostics").exists()

        written = await tool.execute(
            action="write",
            expected_snapshot_sha256=digest.group(1),
        )
        assert "诊断包导出完成" in written
        assert f"Bundle：`{preview_bundle.group(1)}`" in written
        assert len(list((state_home / "diagnostics").glob("*.zip"))) == 1
        with pytest.raises(ValueError, match="缺少本进程内"):
            await tool.execute(
                action="write",
                expected_snapshot_sha256=digest.group(1),
            )
    finally:
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
    ("message", "detail", "diagnostic_code"),
    [
        (
            "401 invalid authentication secret-value",
            "认证失败（401）",
            "provider_auth_failed",
        ),
        (
            "404 resource not found secret-value",
            "模型或 API 地址不存在（404）",
            "provider_resource_not_found",
        ),
        ("429 rate limit secret-value", "服务限流（429）", "provider_rate_limited"),
        ("503 unavailable secret-value", "模型服务暂时不可用（503）", "provider_server_error"),
        ("request timeout secret-value", "连接超时", "provider_timeout"),
    ],
)
async def test_live_doctor_classifies_errors_without_leaking_raw_message(
    tmp_path,
    message: str,
    detail: str,
    diagnostic_code: str,
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
    assert check.diagnostic_code == diagnostic_code
    assert "secret-value" not in check.detail


@pytest.mark.asyncio
async def test_live_doctor_prefers_structured_http_status_over_message_text(
    tmp_path,
) -> None:
    class ProviderFailureError(RuntimeError):
        status_code = 429

    async def failed_probe(_config: AppConfig) -> ModelResponse:
        raise ProviderFailureError("mentions 401 but structured status is authoritative")

    report = await run_doctor(
        _config(tmp_path),
        workspace_root=tmp_path,
        live=True,
        live_probe=failed_probe,
    )

    check = next(item for item in report.checks if item.name == "模型实时连接")
    assert check.status == "warn"
    assert check.detail == "服务限流（429）"
    assert check.diagnostic_code == "provider_rate_limited"

    ProviderFailureError.status_code = 400
    report = await run_doctor(
        _config(tmp_path),
        workspace_root=tmp_path,
        live=True,
        live_probe=failed_probe,
    )
    check = next(item for item in report.checks if item.name == "模型实时连接")
    assert check.status == "error"
    assert check.diagnostic_code == "provider_request_failed"
