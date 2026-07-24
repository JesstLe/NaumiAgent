"""Focused tests for the bounded explicit Doctor provider probe."""

from __future__ import annotations

import asyncio

import pytest

from naumi_agent.config.settings import AppConfig
from naumi_agent.model.router import ModelResponse
from naumi_agent.ui.doctor_probe import (
    doctor_live_probe_payload,
    normalize_doctor_live_probe_timeout,
    run_bounded_doctor_live_probe,
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
async def test_bounded_probe_sends_exactly_one_request_and_returns_receipt(
    tmp_path,
) -> None:
    calls = 0

    async def probe(_config: AppConfig) -> ModelResponse:
        nonlocal calls
        calls += 1
        return ModelResponse(content="OK", model="openai/test-model")

    result = await run_bounded_doctor_live_probe(
        _config(tmp_path),
        workspace_root=tmp_path,
        timeout_ms=2_000,
        live_probe=probe,
        browser_fallback_available=True,
    )
    payload = doctor_live_probe_payload(
        result,
        snapshot_sha256="a" * 64,
    )

    assert calls == 1
    assert result.status == "passed"
    assert payload["request_count"] == 1
    assert payload["request_limit"] == 1
    assert payload["max_output_tokens"] == 8
    assert payload["timeout_ms"] == 2_000


@pytest.mark.asyncio
async def test_bounded_probe_times_out_without_retry(tmp_path) -> None:
    calls = 0

    async def probe(_config: AppConfig) -> ModelResponse:
        nonlocal calls
        calls += 1
        await asyncio.sleep(1)
        return ModelResponse(content="late")

    result = await run_bounded_doctor_live_probe(
        _config(tmp_path),
        workspace_root=tmp_path,
        timeout_ms=1_000,
        live_probe=probe,
        browser_fallback_available=True,
    )

    assert calls == 1
    assert result.status == "failed"
    assert result.diagnostic_code == "provider_timeout"
    assert result.request_count == 1
    assert result.duration_ms >= 900


@pytest.mark.asyncio
async def test_bounded_probe_blocks_invalid_provider_before_network(tmp_path) -> None:
    config = _config(tmp_path)
    config.models.provider = "kimi"
    config.models.default_model = "claude-sonnet-4-6"
    config.models.fast_model = "claude-sonnet-4-6"
    config.models.reasoning_model = "claude-sonnet-4-6"
    config.models.api_base = "https://api.kimi.com/coding/v1"
    calls = 0

    async def probe(_config: AppConfig) -> ModelResponse:
        nonlocal calls
        calls += 1
        return ModelResponse(content="must not run")

    result = await run_bounded_doctor_live_probe(
        config,
        workspace_root=tmp_path,
        live_probe=probe,
        browser_fallback_available=True,
    )

    assert calls == 0
    assert result.status == "blocked"
    assert result.diagnostic_code == "provider_prerequisite_failed"
    assert result.request_count == 0


@pytest.mark.asyncio
async def test_bounded_probe_propagates_cancellation(tmp_path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def probe(_config: AppConfig) -> ModelResponse:
        started.set()
        await release.wait()
        return ModelResponse(content="unreachable")

    task = asyncio.create_task(
        run_bounded_doctor_live_probe(
            _config(tmp_path),
            workspace_root=tmp_path,
            live_probe=probe,
            browser_fallback_available=True,
        )
    )
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_bounded_probe_does_not_expose_provider_exception_body(tmp_path) -> None:
    secret = "sk-secret-should-never-leak"

    async def probe(_config: AppConfig) -> ModelResponse:
        raise RuntimeError(f"provider exploded with {secret}")

    result = await run_bounded_doctor_live_probe(
        _config(tmp_path),
        workspace_root=tmp_path,
        live_probe=probe,
        browser_fallback_available=True,
    )
    encoded = str(
        doctor_live_probe_payload(result, snapshot_sha256="b" * 64)
    )

    assert secret not in encoded
    assert result.diagnostic_code == "provider_request_failed"


@pytest.mark.parametrize("value", [True, 999, 60_001, "invalid"])
def test_probe_timeout_rejects_unsafe_values(value: object) -> None:
    with pytest.raises(ValueError, match="timeout_ms"):
        normalize_doctor_live_probe_timeout(value)
