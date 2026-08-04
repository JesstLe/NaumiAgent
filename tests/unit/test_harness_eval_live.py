from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.harness.eval_live import (
    HarnessLiveEvalError,
    HarnessLiveEvalReceipt,
    HarnessLiveEvalRequest,
    HarnessLiveEvalRunner,
    HarnessLiveEvalStatus,
)
from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.tools import create_harness_tools
from naumi_agent.harness.trust import HarnessTrustStore
from naumi_agent.model.reasoning import (
    ReasoningEffort,
    ReasoningEffortSetting,
    ReasoningEffortStatus,
)
from naumi_agent.model.router import (
    ModelCapabilityContract,
    ModelContractStatus,
    ModelResponse,
    TokenUsage,
)
from naumi_agent.safety.permissions import (
    PermissionChecker,
    PermissionMode,
    PermissionRiskLevel,
)

REQUEST_ID = "hlive_" + "a" * 24
NOW = datetime(2026, 8, 5, 9, 0, tzinfo=UTC)
MODEL = "test-provider/test-model"


def _capability(
    *,
    status: ModelContractStatus = ModelContractStatus.VERIFIED,
    provider: str = "test-provider",
    input_cost: float = 0.1,
    output_cost: float = 0.2,
    cost_source: str = "catalog",
) -> ModelCapabilityContract:
    return ModelCapabilityContract(
        requested_model=MODEL,
        canonical_model=MODEL,
        upstream_model="test-model",
        provider=provider,
        api_format="openai_chat",
        max_context=128_000,
        max_output=4_096,
        request_max_tokens=4_096,
        input_cost_per_million=input_cost,
        output_cost_per_million=output_cost,
        supports_tools=True,
        supports_streaming=True,
        supports_parallel_tools=True,
        supports_structured_output=True,
        supports_reasoning=True,
        supports_vision=False,
        input_modalities=("text",),
        output_modalities=("text",),
        field_sources={
            "max_context": "catalog",
            "max_output": "catalog",
            "input_cost_per_million": cost_source,
            "output_cost_per_million": cost_source,
            "supports_tools": "catalog",
            "supports_streaming": "catalog",
        },
        status=status,
    )


def _reasoning() -> ReasoningEffortStatus:
    return ReasoningEffortStatus(
        model=MODEL,
        effective=ReasoningEffortSetting.MEDIUM,
        source="model",
        supported=(ReasoningEffort.LOW, ReasoningEffort.MEDIUM),
        default=ReasoningEffort.MEDIUM,
    )


class _LiveModelPort:
    def __init__(
        self,
        *,
        capability: ModelCapabilityContract | None = None,
        response: ModelResponse | None = None,
        failure: Exception | None = None,
        capability_after: ModelCapabilityContract | None = None,
    ) -> None:
        self.capability = capability or _capability()
        self.capability_after = capability_after
        self.response = response
        self.failure = failure
        self.calls: list[dict[str, Any]] = []
        self.capability_reads = 0

    def resolve_model(self, _tier: str) -> str:
        return MODEL

    def get_model_capability_contract(
        self,
        _model: str | None = None,
    ) -> ModelCapabilityContract:
        self.capability_reads += 1
        if self.capability_after is not None and self.capability_reads > 1:
            return self.capability_after
        return self.capability

    def get_reasoning_effort_status(
        self,
        _model: str | None = None,
    ) -> ReasoningEffortStatus:
        return _reasoning()

    async def call(self, messages: list[dict[str, Any]], **kwargs: Any) -> ModelResponse:
        self.calls.append({"messages": messages, **kwargs})
        if self.failure is not None:
            raise self.failure
        if self.response is not None:
            return self.response
        challenge = str(messages[-1]["content"]).removeprefix("Return exactly: ")
        return ModelResponse(
            content=challenge,
            usage=TokenUsage(
                input_tokens=24,
                output_tokens=12,
                total_tokens=36,
                cost_usd=0.000005,
            ),
            model=MODEL,
            provider_model="test-model-20260805",
            finish_reason="stop",
        )


def _request(**changes: Any) -> HarnessLiveEvalRequest:
    values: dict[str, Any] = {
        "live": True,
        "model": MODEL,
        "max_duration_seconds": 5,
        "max_cost_usd": 0.05,
        "max_output_tokens": 32,
    }
    values.update(changes)
    return HarnessLiveEvalRequest(**values)


def _runner(port: _LiveModelPort) -> HarnessLiveEvalRunner:
    return HarnessLiveEvalRunner(
        port,
        request_id_factory=lambda: REQUEST_ID,
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_live_runner_verifies_fixed_transport_without_retaining_output() -> None:
    port = _LiveModelPort()

    receipt = await _runner(port).run(_request())

    assert receipt.status is HarnessLiveEvalStatus.PASSED
    assert receipt.code == "live_transport_verified"
    assert receipt.provider_model == "test-model-20260805"
    assert receipt.input_tokens == 24
    assert receipt.output_tokens == 12
    assert receipt.total_tokens == 36
    assert receipt.exact_match is True
    assert receipt.provider_call_attempted is True
    assert receipt.persisted is False
    assert receipt.baseline_eligible is False
    assert receipt.model is not None
    assert receipt.model.capability_status == "verified"
    assert receipt.model.reasoning_effort == "medium"
    assert len(receipt.response_sha256) == 64
    assert len(receipt.receipt_sha256) == 64
    assert len(port.calls) == 1
    call = port.calls[0]
    assert call["tools"] is None
    assert call["temperature"] == 0.0
    assert call["max_tokens"] == 32
    assert call["model"] == MODEL
    serialized = json.dumps(receipt.model_dump(mode="json"), ensure_ascii=False)
    challenge = str(call["messages"][-1]["content"]).removeprefix("Return exactly: ")
    assert challenge not in serialized
    assert "reasoning_content" not in serialized

    payload = receipt.model_dump(mode="json")
    payload["cost_usd"] = 9.0
    with pytest.raises(ValidationError, match="receipt_sha256"):
        HarnessLiveEvalReceipt.model_validate(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("port", "live_request", "code"),
    [
        (
            _LiveModelPort(capability=_capability(status=ModelContractStatus.INCOMPATIBLE)),
            _request(),
            "model_incompatible",
        ),
        (
            _LiveModelPort(capability=_capability(cost_source="fallback")),
            _request(),
            "cost_contract_unverified",
        ),
        (
            _LiveModelPort(capability=_capability(input_cost=1_000, output_cost=1_000)),
            _request(max_cost_usd=0.01),
            "preflight_cost_exceeded",
        ),
    ],
)
async def test_live_preflight_fails_closed_without_provider_call(
    port: _LiveModelPort,
    live_request: HarnessLiveEvalRequest,
    code: str,
) -> None:
    receipt = await _runner(port).run(live_request)

    assert receipt.status is HarnessLiveEvalStatus.EVALUATION_ERROR
    assert receipt.code == code
    assert receipt.provider_call_attempted is False
    assert port.calls == []


@pytest.mark.asyncio
async def test_live_runner_distinguishes_timeout_usage_and_challenge_failure() -> None:
    timed_out = _LiveModelPort(failure=TimeoutError("private timeout"))
    timeout_receipt = await _runner(timed_out).run(_request())
    assert timeout_receipt.status is HarnessLiveEvalStatus.PARTIAL
    assert timeout_receipt.code == "deadline_exceeded"
    assert "private timeout" not in timeout_receipt.message

    inconsistent = _LiveModelPort(
        response=ModelResponse(
            content=f"NAUMI_LIVE_OK_{'A' * 24}",
            usage=TokenUsage(
                input_tokens=3,
                output_tokens=2,
                total_tokens=99,
                cost_usd=0.001,
            ),
            provider_model="test-model",
            finish_reason="stop",
        )
    )
    usage_receipt = await _runner(inconsistent).run(_request())
    assert usage_receipt.status is HarnessLiveEvalStatus.PARTIAL
    assert usage_receipt.code == "usage_inconsistent"

    fractional = _LiveModelPort(
        response=ModelResponse(
            content=f"NAUMI_LIVE_OK_{'A' * 24}",
            usage=TokenUsage(
                input_tokens=3.5,  # type: ignore[arg-type]
                output_tokens=2,
                total_tokens=5.5,  # type: ignore[arg-type]
                cost_usd=0.001,
            ),
            provider_model="test-model",
            finish_reason="stop",
        )
    )
    fractional_receipt = await _runner(fractional).run(_request())
    assert fractional_receipt.status is HarnessLiveEvalStatus.PARTIAL
    assert fractional_receipt.code == "usage_invalid"
    assert fractional_receipt.input_tokens == 0
    assert fractional_receipt.total_tokens == 2

    invalid_content = _LiveModelPort(
        response=ModelResponse(
            content=None,  # type: ignore[arg-type]
            usage=TokenUsage(
                input_tokens=3,
                output_tokens=2,
                total_tokens=5,
                cost_usd=0.001,
            ),
            provider_model="test-model",
            finish_reason="stop",
        )
    )
    content_receipt = await _runner(invalid_content).run(_request())
    assert content_receipt.status is HarnessLiveEvalStatus.PARTIAL
    assert content_receipt.code == "response_content_invalid"

    unsafe_provider_model = _LiveModelPort(
        response=ModelResponse(
            content=f"NAUMI_LIVE_OK_{'A' * 24}",
            usage=TokenUsage(
                input_tokens=3,
                output_tokens=2,
                total_tokens=5,
                cost_usd=0.001,
            ),
            provider_model="test model [untrusted]",
            finish_reason="stop",
        )
    )
    provider_receipt = await _runner(unsafe_provider_model).run(_request())
    assert provider_receipt.status is HarnessLiveEvalStatus.PARTIAL
    assert provider_receipt.code == "provider_identity_missing"
    assert provider_receipt.provider_model == ""

    mismatch = _LiveModelPort(
        response=ModelResponse(
            content="not-the-challenge",
            usage=TokenUsage(
                input_tokens=3,
                output_tokens=2,
                total_tokens=5,
                cost_usd=0.001,
            ),
            provider_model="test-model",
            finish_reason="stop",
        )
    )
    mismatch_receipt = await _runner(mismatch).run(_request())
    assert mismatch_receipt.status is HarnessLiveEvalStatus.IMPLEMENTATION_FAILURE
    assert mismatch_receipt.code == "challenge_mismatch"


@pytest.mark.asyncio
async def test_live_runner_marks_contract_drift_and_actual_overspend_partial() -> None:
    drifted = _LiveModelPort(
        capability_after=_capability(provider="changed-provider"),
    )
    drift_receipt = await _runner(drifted).run(_request())
    assert drift_receipt.status is HarnessLiveEvalStatus.PARTIAL
    assert drift_receipt.code == "model_contract_drift"

    overspend = _LiveModelPort(
        response=ModelResponse(
            content=f"NAUMI_LIVE_OK_{'A' * 24}",
            usage=TokenUsage(
                input_tokens=3,
                output_tokens=2,
                total_tokens=5,
                cost_usd=0.06,
            ),
            provider_model="test-model",
            finish_reason="stop",
        )
    )
    overspend_receipt = await _runner(overspend).run(_request())
    assert overspend_receipt.status is HarnessLiveEvalStatus.PARTIAL
    assert overspend_receipt.code == "actual_cost_exceeded"


def test_live_request_requires_explicit_mode_and_safe_budgets() -> None:
    with pytest.raises(ValidationError):
        HarnessLiveEvalRequest(live=False, model=MODEL)
    with pytest.raises(ValidationError):
        _request(max_duration_seconds=True)
    with pytest.raises(ValidationError):
        _request(max_cost_usd=float("nan"))
    with pytest.raises(ValidationError):
        _request(max_output_tokens=65)


@pytest.mark.asyncio
async def test_service_tool_and_permission_share_one_live_authority(tmp_path: Path) -> None:
    port = _LiveModelPort()
    service = HarnessService(
        workspace_root=tmp_path,
        trust_store=HarnessTrustStore(tmp_path / "trust.json"),
        model_port=port,
    )
    service._live_eval_runner = _runner(port)
    tool = next(item for item in create_harness_tools(service) if item.name == "harness_eval_live")

    rendered = await tool.execute(
        model=MODEL,
        max_duration_seconds=5,
        max_cost_usd=0.05,
        max_output_tokens=16,
    )

    assert "# Harness Live Eval" in rendered
    assert "live_transport_verified" in rendered
    assert tool.metadata.read_only is False
    assert tool.metadata.requires_confirmation is True
    moderate = PermissionChecker(PermissionMode.MODERATE).check(tool.name, {})
    bypass = PermissionChecker(PermissionMode.BYPASS).check(tool.name, {})
    assert moderate.allowed and moderate.requires_confirmation
    assert moderate.risk_level is PermissionRiskLevel.MEDIUM
    assert bypass.allowed and not bypass.requires_confirmation

    unavailable = HarnessService(
        workspace_root=tmp_path,
        trust_store=HarnessTrustStore(tmp_path / "other-trust.json"),
    )
    with pytest.raises(HarnessLiveEvalError, match="模型调用端口"):
        await unavailable.eval_live()


@pytest.mark.asyncio
async def test_slash_live_parser_dispatches_only_bounded_arguments() -> None:
    class _Engine:
        harness_service = object()

        def __init__(self) -> None:
            self.calls: list[Any] = []

        async def execute_tool(self, call: Any) -> Any:
            self.calls.append(call)
            return SimpleNamespace(content="live-dispatched")

    engine = _Engine()
    output = await execute_slash_command(
        engine,
        (
            "/harness eval live --model test-provider/test-model "
            "--timeout 12 --max-cost 0.02 --max-output 24"
        ),
    )

    assert "live-dispatched" in output
    assert len(engine.calls) == 1
    call = engine.calls[0]
    assert call.name == "harness_eval_live"
    assert json.loads(call.arguments) == {
        "model": MODEL,
        "max_duration_seconds": 12.0,
        "max_cost_usd": 0.02,
        "max_output_tokens": 24,
    }

    invalid = await execute_slash_command(
        engine,
        "/harness eval live --timeout 0",
    )
    assert "用法" in invalid
    assert len(engine.calls) == 1
