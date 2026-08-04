from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.harness.eval_live_suite import (
    MAX_LIVE_SUITE_BYTES,
    HarnessLiveBatchRequest,
    HarnessLiveBatchStatus,
    HarnessLiveSuiteRunner,
    build_live_batch_status,
    load_live_eval_suite,
    resolve_declared_live_eval_suite,
)
from naumi_agent.harness.eval_models import EvalCaseStatus, EvalRunStatus
from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.store import HarnessStore
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
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode

MODEL = "test-provider/live-model"
SUITE_TEXT = """\
schema_version: 1
kind: live
id: live-transport-core
title: Live transport core
cases:
  - id: exact-echo
    runner: live_transport_echo
    prompt_version: naumi_live_echo@1
    expected: {exact_match: true}
    metrics: {primary: live_transport_exact_match}
    budget:
      max_duration_seconds: 5
      max_cost_usd: 0.02
      max_output_tokens: 32
budget:
  max_duration_seconds_per_sample: 5
  max_cost_usd_per_sample: 0.02
comparison_policy:
  min_pass_rate: 1.0
  max_regressions: 0
  max_implementation_failures: 0
  max_pass_rate_drop: 0.0
"""


def _git(workspace: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=workspace, check=True, capture_output=True)


def _workspace(tmp_path: Path, suite_text: str = SUITE_TEXT) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    suite_path = workspace / "evals" / "live.yaml"
    suite_path.parent.mkdir(parents=True)
    suite_path.write_text(suite_text, encoding="utf-8")
    profile = workspace / ".naumi" / "harness.yaml"
    profile.parent.mkdir()
    profile.write_text(
        "schema_version: 1\nevals:\n"
        "  live_suites: [evals/live.yaml]\n"
        "  live_default: false\n"
        "  max_cost_usd: 0.2\n"
        "  max_duration_seconds: 60\n",
        encoding="utf-8",
    )
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "tests@naumi.local")
    _git(workspace, "config", "user.name", "Naumi Tests")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-qm", "fixture")
    return workspace, suite_path


def _capability(*, cost_source: str = "catalog") -> ModelCapabilityContract:
    return ModelCapabilityContract(
        requested_model=MODEL,
        canonical_model=MODEL,
        upstream_model="live-model",
        provider="test-provider",
        api_format="openai_chat",
        max_context=128_000,
        max_output=4_096,
        request_max_tokens=4_096,
        input_cost_per_million=0.1,
        output_cost_per_million=0.2,
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
        },
        status=ModelContractStatus.VERIFIED,
    )


def _reasoning() -> ReasoningEffortStatus:
    return ReasoningEffortStatus(
        model=MODEL,
        effective=ReasoningEffortSetting.MEDIUM,
        source="model",
        supported=(ReasoningEffort.LOW, ReasoningEffort.MEDIUM),
        default=ReasoningEffort.MEDIUM,
    )


class _ModelPort:
    def __init__(
        self,
        *,
        cost_source: str = "catalog",
        response_cost: float = 0.001,
        provider_models: tuple[str, ...] = ("live-model-20260805",),
    ) -> None:
        self.capability = _capability(cost_source=cost_source)
        self.response_cost = response_cost
        self.provider_models = provider_models
        self.calls: list[dict[str, Any]] = []

    def resolve_model(self, _tier: str) -> str:
        return MODEL

    def get_model_capability_contract(
        self,
        _model: str | None = None,
    ) -> ModelCapabilityContract:
        return self.capability

    def get_reasoning_effort_status(
        self,
        _model: str | None = None,
    ) -> ReasoningEffortStatus:
        return _reasoning()

    async def call(self, messages: list[dict[str, Any]], **kwargs: Any) -> ModelResponse:
        self.calls.append({"messages": messages, **kwargs})
        challenge = str(messages[-1]["content"]).removeprefix("Return exactly: ")
        provider_model = self.provider_models[
            min(len(self.calls) - 1, len(self.provider_models) - 1)
        ]
        return ModelResponse(
            content=challenge,
            usage=TokenUsage(
                input_tokens=20,
                output_tokens=8,
                total_tokens=28,
                cost_usd=self.response_cost,
            ),
            model=MODEL,
            provider_model=provider_model,
            finish_reason="stop",
        )


@pytest.mark.asyncio
async def test_live_suite_runs_five_real_transport_samples_with_one_identity(
    tmp_path: Path,
) -> None:
    workspace, suite_path = _workspace(tmp_path)
    loaded = load_live_eval_suite(workspace, suite_path)
    port = _ModelPort()
    runner = HarnessLiveSuiteRunner(port)

    execution = await runner.run(
        workspace_root=workspace,
        loaded=loaded,
        profile_sha256="a" * 64,
        profile_trusted=True,
        model=MODEL,
        repetitions=5,
        batch_id="live-batch-1",
        max_total_duration_seconds=30,
        max_total_cost_usd=0.1,
    )

    assert execution.status == "completed"
    assert len(execution.results) == 5
    assert execution.total_calls == 5
    assert execution.total_tokens == 140
    assert execution.total_cost_usd == pytest.approx(0.005)
    assert execution.provider_model == "live-model-20260805"
    identities = {
        result.baseline_identity.identity_sha256
        for result in execution.results
        if result.baseline_identity is not None
    }
    assert len(identities) == 1
    identity = execution.results[0].baseline_identity
    assert identity is not None and identity.baseline_eligible
    assert identity.configuration.live is True
    assert identity.configuration.repetitions == 5
    assert identity.model is not None
    assert identity.model.provider_model == "live-model-20260805"
    for result in execution.results:
        assert result.status is EvalRunStatus.PASSED
        case = result.cases[0]
        assert case.status is EvalCaseStatus.PASSED
        assert case.live_evidence is not None
        assert case.live_evidence.exact_match is True
        assert (
            case.live_evidence.batch_request_sha256
            == execution.request.request_sha256
        )
        assert case.primary_metric == "live_transport_exact_match"
        assert len(case.metric_observations) == 4
        serialized = json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
        assert "NAUMI_LIVE_OK" not in serialized
        assert "reasoning_content" not in serialized

    status = build_live_batch_status(
        execution,
        persisted_result_sha256=tuple(f"{index + 1:x}" * 64 for index in range(5)),
    )
    assert status.status == "completed"
    assert status.baseline_eligible is True
    assert len(status.receipt_sha256) == 64
    tampered = status.model_dump(mode="json")
    tampered["total_tokens"] += 1
    with pytest.raises(ValidationError, match="receipt_sha256"):
        HarnessLiveBatchStatus.model_validate(tampered)


@pytest.mark.asyncio
async def test_live_suite_stops_without_retry_on_unverified_cost_contract(
    tmp_path: Path,
) -> None:
    workspace, suite_path = _workspace(tmp_path)
    port = _ModelPort(cost_source="fallback")

    execution = await HarnessLiveSuiteRunner(port).run(
        workspace_root=workspace,
        loaded=load_live_eval_suite(workspace, suite_path),
        profile_sha256="a" * 64,
        profile_trusted=True,
        model=MODEL,
        repetitions=5,
        batch_id="live-batch-failed",
        max_total_duration_seconds=30,
        max_total_cost_usd=0.1,
    )

    assert execution.status == "partial"
    assert execution.code == "cost_contract_unverified"
    assert len(execution.results) == 1
    assert execution.results[0].cases[0].status is EvalCaseStatus.EVALUATION_ERROR
    assert execution.results[0].baseline_identity is None
    assert execution.results[0].baseline_identity_code == ("live_batch_provider_model_unavailable")
    assert execution.total_calls == 0
    assert port.calls == []


@pytest.mark.asyncio
async def test_live_suite_rejects_provider_model_drift_for_baseline_identity(
    tmp_path: Path,
) -> None:
    workspace, suite_path = _workspace(tmp_path)
    port = _ModelPort(provider_models=("model-a", "model-b"))

    execution = await HarnessLiveSuiteRunner(port).run(
        workspace_root=workspace,
        loaded=load_live_eval_suite(workspace, suite_path),
        profile_sha256="a" * 64,
        profile_trusted=True,
        model=MODEL,
        repetitions=5,
        batch_id="live-batch-drift",
        max_total_duration_seconds=30,
        max_total_cost_usd=0.1,
    )

    assert execution.status == "completed"
    assert execution.provider_model == ""
    assert all(result.baseline_identity is None for result in execution.results)
    assert {result.baseline_identity_code for result in execution.results} == {
        "live_batch_provider_model_changed"
    }


@pytest.mark.asyncio
async def test_live_suite_reports_provider_actual_overspend_as_partial(
    tmp_path: Path,
) -> None:
    workspace, suite_path = _workspace(tmp_path)
    port = _ModelPort(response_cost=0.03)

    execution = await HarnessLiveSuiteRunner(port).run(
        workspace_root=workspace,
        loaded=load_live_eval_suite(workspace, suite_path),
        profile_sha256="a" * 64,
        profile_trusted=True,
        model=MODEL,
        repetitions=5,
        batch_id="live-batch-overspend",
        max_total_duration_seconds=30,
        max_total_cost_usd=0.02,
    )
    status = build_live_batch_status(
        execution,
        persisted_result_sha256=("1" * 64,),
    )

    assert execution.status == "partial"
    assert execution.code == "actual_cost_exceeded"
    assert execution.total_calls == 1
    assert execution.total_cost_usd == pytest.approx(0.03)
    assert status.status == "partial"
    assert status.code == "actual_cost_exceeded"
    assert status.actual_cost_exceeded is True

    persistence_error = build_live_batch_status(
        execution,
        persisted_result_sha256=("1" * 64,),
        persistence_code="live_batch_persistence_failed",
        persistence_message="持久化失败。",
    )
    assert persistence_error.status == "error"
    assert persistence_error.code == "live_batch_persistence_failed"
    assert persistence_error.actual_cost_exceeded is True


def test_live_batch_request_binds_canonical_creation_time() -> None:
    request = HarnessLiveBatchRequest.create(
        request_id="hlivebatch_" + "a" * 24,
        created_at="2026-08-05T01:02:03Z",
        batch_id="batch",
        suite_id="suite",
        suite_sha256="b" * 64,
        profile_sha256="c" * 64,
        model=MODEL,
        repetitions=5,
        max_total_duration_seconds=30,
        max_total_cost_usd=0.1,
    )

    assert request.created_at == "2026-08-05T01:02:03Z"
    tampered = request.model_dump(mode="json")
    tampered["created_at"] = "2026-08-05T01:02:04Z"
    with pytest.raises(ValidationError, match="request_sha256"):
        HarnessLiveBatchRequest.model_validate(tampered)
    with pytest.raises(ValidationError, match="规范 UTC"):
        HarnessLiveBatchRequest.create(
            request_id="hlivebatch_" + "a" * 24,
            created_at="2026-08-05T01:02:03+00:00",
            batch_id="batch",
            suite_id="suite",
            suite_sha256="b" * 64,
            profile_sha256="c" * 64,
            model=MODEL,
            repetitions=5,
            max_total_duration_seconds=30,
            max_total_cost_usd=0.1,
        )


def test_live_suite_loader_is_declared_bounded_and_strict(tmp_path: Path) -> None:
    workspace, suite_path = _workspace(tmp_path)

    by_path = resolve_declared_live_eval_suite(
        workspace,
        ("evals/live.yaml",),
        "evals/live.yaml",
    )
    by_id = resolve_declared_live_eval_suite(
        workspace,
        ("evals/live.yaml",),
        "live-transport-core",
    )
    assert by_path.sha256 == by_id.sha256
    assert by_path.display_path == "evals/live.yaml"

    with pytest.raises(Exception, match="声明"):
        resolve_declared_live_eval_suite(workspace, (), "live-transport-core")
    with pytest.raises(Exception, match="工作区"):
        load_live_eval_suite(workspace, workspace.parent / "outside.yaml")

    invalid = SUITE_TEXT.replace(
        "max_cost_usd_per_sample: 0.02",
        "max_cost_usd_per_sample: 0.001",
    )
    invalid_path = workspace / "evals" / "invalid.yaml"
    invalid_path.write_text(invalid, encoding="utf-8")
    with pytest.raises(Exception, match="schema version 1"):
        load_live_eval_suite(workspace, invalid_path)

    oversized = workspace / "evals" / "oversized.yaml"
    oversized.write_bytes(b"x" * (MAX_LIVE_SUITE_BYTES + 1))
    with pytest.raises(Exception, match="256 KiB"):
        load_live_eval_suite(workspace, oversized)


@pytest.mark.asyncio
async def test_live_suite_halts_remaining_cases_after_infrastructure_failure(
    tmp_path: Path,
) -> None:
    second_case = """\
  - id: second-echo
    runner: live_transport_echo
    prompt_version: naumi_live_echo@1
    expected: {exact_match: true}
    metrics: {primary: live_transport_exact_match}
    budget:
      max_duration_seconds: 5
      max_cost_usd: 0.02
      max_output_tokens: 32
"""
    suite_text = SUITE_TEXT.replace(
        "budget:\n  max_duration_seconds_per_sample: 5\n  max_cost_usd_per_sample: 0.02",
        second_case
        + "budget:\n  max_duration_seconds_per_sample: 10\n"
        + "  max_cost_usd_per_sample: 0.04",
    )
    workspace, suite_path = _workspace(tmp_path, suite_text)
    port = _ModelPort(cost_source="fallback")

    execution = await HarnessLiveSuiteRunner(port).run(
        workspace_root=workspace,
        loaded=load_live_eval_suite(workspace, suite_path),
        profile_sha256="a" * 64,
        profile_trusted=True,
        model=MODEL,
        repetitions=5,
        batch_id="live-batch-halt",
        max_total_duration_seconds=30,
        max_total_cost_usd=0.1,
    )

    assert execution.total_calls == 0
    assert port.calls == []
    assert [case.code for case in execution.results[0].cases] == [
        "cost_contract_unverified",
        "live_sample_halted",
    ]
    assert {
        case.live_evidence.batch_request_sha256
        for case in execution.results[0].cases
        if case.live_evidence is not None
    } == {execution.request.request_sha256}


def test_live_batch_status_rejects_completed_prefix() -> None:
    raw = {
        "status": "completed",
        "request_id": "hlivebatch_" + "a" * 24,
        "request_sha256": "b" * 64,
        "batch_id": "batch",
        "suite_id": "suite",
        "model": MODEL,
        "requested": 5,
        "completed": 1,
        "persisted": 1,
        "total_calls": 1,
        "total_tokens": 1,
        "total_cost_usd": 0.001,
        "duration_ms": 1,
        "max_total_duration_seconds": 30,
        "max_total_cost_usd": 0.1,
        "sample_result_sha256": ("1" * 64,),
        "receipt_sha256": "0" * 64,
    }
    with pytest.raises(ValidationError):
        HarnessLiveBatchStatus.model_validate(raw)


@pytest.mark.asyncio
async def test_service_tool_and_h5a_share_one_live_batch_authority(
    tmp_path: Path,
) -> None:
    workspace, _suite_path = _workspace(tmp_path)
    store = HarnessStore(tmp_path / "harness.db")
    port = _ModelPort()
    service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust.json"),
        store=store,
        model_port=port,
    )
    await service.trust(source="test")

    status = await service.eval_live_batch(
        "live-transport-core",
        repetitions=5,
        batch_id="service-live-batch",
        model=MODEL,
        max_total_duration_seconds=30,
        max_total_cost_usd=0.1,
    )
    stored = await store.list_eval_results(
        workspace,
        "service-live-batch",
        "live-transport-core",
        limit=10,
    )

    assert status.status == "completed"
    assert status.persisted == 5
    assert status.baseline_eligible is True
    assert [item.sample_index for item in stored] == list(range(5))
    assert len({item.identity_sha256 for item in stored}) == 1
    assert stored[0].result.cases[0].live_evidence is not None

    tool = next(
        item for item in create_harness_tools(service) if item.name == "harness_eval_live_batch"
    )
    assert tool.metadata.requires_confirmation is True
    assert tool.parameters_schema["required"] == ["suite"]
    moderate = PermissionChecker(PermissionMode.MODERATE).check(tool.name, {})
    bypass = PermissionChecker(PermissionMode.BYPASS).check(tool.name, {})
    assert moderate.allowed and moderate.requires_confirmation
    assert bypass.allowed and not bypass.requires_confirmation


@pytest.mark.asyncio
async def test_service_rejects_untrusted_live_profile_before_provider_call(
    tmp_path: Path,
) -> None:
    workspace, _suite_path = _workspace(tmp_path)
    port = _ModelPort()
    service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust.json"),
        store=HarnessStore(tmp_path / "harness.db"),
        model_port=port,
    )

    with pytest.raises(Exception, match="尚未受信任"):
        await service.eval_live_batch(
            "live-transport-core",
            repetitions=5,
            batch_id="untrusted-live-batch",
            model=MODEL,
        )

    assert port.calls == []


@pytest.mark.asyncio
async def test_service_defaults_clamp_profile_budget_to_runner_hard_caps(
    tmp_path: Path,
) -> None:
    workspace, _suite_path = _workspace(tmp_path)
    profile = workspace / ".naumi" / "harness.yaml"
    profile.write_text(
        profile.read_text(encoding="utf-8")
        .replace("max_cost_usd: 0.2", "max_cost_usd: 20")
        .replace("max_duration_seconds: 60", "max_duration_seconds: 7200"),
        encoding="utf-8",
    )
    _git(workspace, "add", ".naumi/harness.yaml")
    _git(workspace, "commit", "-qm", "raise profile caps")
    store = HarnessStore(tmp_path / "harness.db")
    service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust.json"),
        store=store,
        model_port=_ModelPort(),
    )
    await service.trust(source="test")

    status = await service.eval_live_batch(
        "live-transport-core",
        repetitions=5,
        batch_id="service-default-caps",
        model=MODEL,
    )

    assert status.status == "completed"
    assert status.max_total_cost_usd == 10.0
    assert status.max_total_duration_seconds == 3_600.0


@pytest.mark.asyncio
async def test_live_batch_slash_dispatches_bounded_arguments() -> None:
    class _Engine:
        harness_service = object()

        def __init__(self) -> None:
            self.calls: list[Any] = []

        async def execute_tool(self, call: Any) -> Any:
            self.calls.append(call)
            return SimpleNamespace(content="live-batch-dispatched")

    engine = _Engine()
    output = await execute_slash_command(
        engine,
        "/harness eval live live-transport-core --repeat 5 --batch batch-1 "
        "--model test-provider/live-model --timeout 30 --max-cost 0.1",
    )

    assert "live-batch-dispatched" in output
    assert len(engine.calls) == 1
    call = engine.calls[0]
    assert call.name == "harness_eval_live_batch"
    assert json.loads(call.arguments) == {
        "suite": "live-transport-core",
        "repetitions": 5,
        "batch_id": "batch-1",
        "model": MODEL,
        "max_total_duration_seconds": 30.0,
        "max_total_cost_usd": 0.1,
    }

    invalid = await execute_slash_command(
        engine,
        "/harness eval live live-transport-core --repeat 4",
    )
    assert "用法" in invalid
    assert len(engine.calls) == 1
