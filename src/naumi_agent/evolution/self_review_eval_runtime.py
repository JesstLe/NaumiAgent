"""Shared deterministic runtime for Self-Review RED/GREEN eval cohorts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Literal

from naumi_agent.evolution.self_review import (
    SELF_REVIEW_STATIC_RUNNER_VERSION,
    SelfReviewFindingCode,
    SelfReviewStaticFinding,
    scan_self_review_files,
)
from naumi_agent.evolution.validation_cohorts import EvolutionBaselineCohortRequest
from naumi_agent.evolution.validation_metric_bindings import (
    EvolutionMetricRunnerBinding,
    EvolutionMetricRunnerRegistry,
)
from naumi_agent.evolution.validation_plans import EvolutionValidationPlan
from naumi_agent.harness.eval_identity import (
    HarnessEvalBaselineIdentity,
    HarnessEvalConfigurationIdentity,
)
from naumi_agent.harness.eval_models import (
    EvalCaseStatus,
    EvalGuardrailStatus,
    EvalRunStatus,
    HarnessEvalCaseResult,
    HarnessEvalComparisonPolicy,
    HarnessEvalGuardrailResult,
    HarnessEvalMetricObservation,
    HarnessEvalSuiteResult,
)
from naumi_agent.harness.store import HarnessStoredEvalResult

type SelfReviewCohortPhase = Literal["red", "green"]


class SelfReviewEvalRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_self_review_cohort_authority(
    baseline_request: EvolutionBaselineCohortRequest,
    metric_binding: EvolutionMetricRunnerBinding,
    validation_plan: EvolutionValidationPlan,
    *,
    registry: EvolutionMetricRunnerRegistry | None = None,
) -> tuple[
    EvolutionBaselineCohortRequest,
    EvolutionMetricRunnerBinding,
    EvolutionValidationPlan,
]:
    try:
        request = EvolutionBaselineCohortRequest.model_validate(
            baseline_request.model_dump(mode="json")
        )
        binding = EvolutionMetricRunnerBinding.model_validate(
            metric_binding.model_dump(mode="json")
        )
        plan = EvolutionValidationPlan.model_validate(
            validation_plan.model_dump(mode="json")
        )
    except (AttributeError, ValueError, TypeError) as exc:
        raise SelfReviewEvalRuntimeError(
            "self_review_authority_invalid",
            "Self-Review cohort authority 无效或已被篡改。",
        ) from exc
    if not (
        plan.schema_version == 2
        and request.validation_plan_id == plan.validation_plan_id
        and request.validation_plan_sha256 == plan.validation_plan_sha256
        and request.baseline_commit == plan.baseline_commit
        and request.baseline_tree_sha256 == plan.baseline_tree_sha256
        and binding.baseline_request_id == request.request_id
        and binding.baseline_request_sha256 == request.request_sha256
        and binding.validation_plan_id == plan.validation_plan_id
        and binding.validation_plan_sha256 == plan.validation_plan_sha256
        and binding.requested_samples == request.requested_samples
        and binding.binding_status == "ready"
        and binding.metric_binding_complete
    ):
        raise SelfReviewEvalRuntimeError(
            "self_review_authority_mismatch",
            "Self-Review cohort Request、Binding 与 Plan 不一致。",
        )
    if any(item.file_kind != "python" for item in plan.files):
        raise SelfReviewEvalRuntimeError(
            "self_review_python_paths_required",
            "Self-Review 静态 cohort 只接受 Plan 中的 Python 文件。",
        )
    if len(binding.entries) != len(request.metrics):
        raise SelfReviewEvalRuntimeError(
            "metric_binding_set_mismatch",
            "Self-Review metric binding 数量与 Request 不一致。",
        )
    runner_registry = registry or EvolutionMetricRunnerRegistry()
    validation_paths = tuple(item.path for item in plan.files)
    for metric, entry in zip(request.metrics, binding.entries, strict=True):
        expected_resolution = runner_registry.resolve(
            metric,
            validation_paths=validation_paths,
        )
        if not (
            entry.order == metric.order
            and entry.metric_name == metric.metric_name
            and entry.direction == metric.direction
            and entry.target == metric.target
            and entry.procedure_sha256 == metric.procedure_sha256
            and entry.resolution == expected_resolution
            and entry.resolution.status == "ready"
            and entry.resolution.verifier == "self_review_static"
        ):
            raise SelfReviewEvalRuntimeError(
                "self_review_metric_binding_mismatch",
                "Self-Review metric runner authority 不完整或不匹配。",
            )
    return request, binding, plan


def build_self_review_eval_configuration(
    request: EvolutionBaselineCohortRequest,
    binding: EvolutionMetricRunnerBinding,
    plan: EvolutionValidationPlan,
) -> HarnessEvalConfigurationIdentity:
    suite_sha256 = _sha256_payload({
        "request_sha256": request.request_sha256,
        "binding_sha256": binding.binding_sha256,
        "plan_sha256": plan.validation_plan_sha256,
    })
    policy = HarnessEvalComparisonPolicy()
    return HarnessEvalConfigurationIdentity.create(
        suite_id=request.suite_id,
        suite_sha256=suite_sha256,
        profile_sha256=request.profile_sha256,
        policy_sha256=policy.sha256,
        runner_version=SELF_REVIEW_STATIC_RUNNER_VERSION,
        repetitions=request.requested_samples,
        live=False,
    )


async def run_self_review_static_repetitions(
    *,
    files: list[Path],
    scan_root: Path,
    phase: SelfReviewCohortPhase,
    request: EvolutionBaselineCohortRequest,
    binding: EvolutionMetricRunnerBinding,
    plan: EvolutionValidationPlan,
    configuration: HarnessEvalConfigurationIdentity,
    identity: HarnessEvalBaselineIdentity,
) -> tuple[HarnessEvalSuiteResult, ...]:
    timeouts = tuple(
        item.resolution.timeout_seconds_per_sample for item in binding.entries
    )
    if not timeouts or any(item is None for item in timeouts):
        raise SelfReviewEvalRuntimeError(
            "self_review_timeout_unbound",
            "Self-Review metric timeout 未完整绑定。",
        )
    timeout = min(int(item) for item in timeouts if item is not None)
    results: list[HarnessEvalSuiteResult] = []
    for _ in range(request.requested_samples):
        results.append(await run_self_review_static_sample(
            files=files,
            scan_root=scan_root,
            phase=phase,
            request=request,
            binding=binding,
            plan=plan,
            configuration=configuration,
            identity=identity,
            timeout_seconds=timeout,
        ))
    return tuple(results)


async def run_self_review_static_sample(
    *,
    files: list[Path],
    scan_root: Path,
    phase: SelfReviewCohortPhase,
    request: EvolutionBaselineCohortRequest,
    binding: EvolutionMetricRunnerBinding,
    plan: EvolutionValidationPlan,
    configuration: HarnessEvalConfigurationIdentity,
    identity: HarnessEvalBaselineIdentity,
    timeout_seconds: int | None = None,
) -> HarnessEvalSuiteResult:
    """Run one complete typed static metric sample without persisting it."""
    validate_self_review_cohort_authority(request, binding, plan)
    if not (
        configuration.suite_id == request.suite_id
        and configuration.profile_sha256 == request.profile_sha256
        and configuration.repetitions == request.requested_samples
        and not configuration.live
        and identity.configuration == configuration
        and (
            phase == "green"
            or (
                identity.source.commit == request.baseline_commit
                and identity.source.tree_sha256
                == f"sha256:{request.baseline_tree_sha256}"
            )
        )
        and (phase == "green" or not identity.source.dirty)
        and identity.profile_trusted
    ):
        raise SelfReviewEvalRuntimeError(
            "self_review_sample_identity_mismatch",
            "Self-Review metric sample runtime identity 与可信 Request 不一致。",
        )
    bound_timeouts = tuple(
        item.resolution.timeout_seconds_per_sample for item in binding.entries
    )
    if not bound_timeouts or any(item is None for item in bound_timeouts):
        raise SelfReviewEvalRuntimeError(
            "self_review_timeout_unbound",
            "Self-Review metric timeout 未完整绑定。",
        )
    bound_timeout = min(int(item) for item in bound_timeouts if item is not None)
    if timeout_seconds is not None and (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not 1 <= timeout_seconds <= 3_600
    ):
        raise SelfReviewEvalRuntimeError(
            "self_review_timeout_invalid",
            "Self-Review metric sample timeout 格式无效。",
        )
    effective_timeout = bound_timeout if timeout_seconds is None else timeout_seconds
    if effective_timeout != bound_timeout:
        raise SelfReviewEvalRuntimeError(
            "self_review_timeout_mismatch",
            "Self-Review metric sample timeout 与可信 Binding 不一致。",
        )
    started = time.perf_counter()
    try:
        scan = await asyncio.wait_for(
            asyncio.to_thread(
                scan_self_review_files,
                files,
                workspace_root=scan_root,
            ),
            timeout=effective_timeout,
        )
    except TimeoutError as exc:
        raise SelfReviewEvalRuntimeError(
            "self_review_static_timeout",
            f"Self-Review 静态 {phase.upper()} 扫描超时。",
        ) from exc
    if scan.errors or scan.files_scanned != len(files):
        raise SelfReviewEvalRuntimeError(
            "self_review_static_scan_failed",
            f"Self-Review 静态 {phase.upper()} 未完整扫描全部可信文件。",
        )
    return _build_result(
        phase=phase,
        request=request,
        binding=binding,
        plan=plan,
        configuration=configuration,
        identity=identity,
        findings=scan.findings,
        duration_ms=(time.perf_counter() - started) * 1_000,
    )


def require_continuous_eval_prefix(
    records: tuple[HarnessStoredEvalResult, ...],
    requested_samples: int,
    *,
    phase: SelfReviewCohortPhase,
) -> None:
    indexes = [item.sample_index for item in records]
    if indexes != list(range(len(records))) or len(records) > requested_samples:
        raise SelfReviewEvalRuntimeError(
            f"existing_{phase}_cohort_non_continuous",
            f"已有 Self-Review {phase.upper()} cohort sample_index 不连续或越界。",
        )


def _build_result(
    *,
    phase: SelfReviewCohortPhase,
    request: EvolutionBaselineCohortRequest,
    binding: EvolutionMetricRunnerBinding,
    plan: EvolutionValidationPlan,
    configuration: HarnessEvalConfigurationIdentity,
    identity: HarnessEvalBaselineIdentity,
    findings: tuple[SelfReviewStaticFinding, ...],
    duration_ms: float,
) -> HarnessEvalSuiteResult:
    counts = {
        code.value: sum(
            1
            for finding in findings
            if finding.code is code
        )
        for code in SelfReviewFindingCode
    }
    cases: list[HarnessEvalCaseResult] = []
    for entry in binding.entries:
        finding_code = entry.resolution.finding_code
        if finding_code is None or finding_code not in counts:
            raise SelfReviewEvalRuntimeError(
                "self_review_finding_code_invalid",
                "Self-Review metric finding code 无效。",
            )
        observation = HarnessEvalMetricObservation(
            metric=entry.metric_name,
            value=counts[finding_code],
            unit="count",
            direction=entry.direction,
            target=entry.target,
            primary=True,
        )
        status = (
            EvalCaseStatus.PASSED
            if observation.target_met
            else EvalCaseStatus.IMPLEMENTATION_FAILURE
        )
        cases.append(HarnessEvalCaseResult(
            case_id=f"metric-{entry.order:02d}-{finding_code.replace('_', '-')}",
            runner=SELF_REVIEW_STATIC_RUNNER_VERSION,
            status=status,
            primary_metric=entry.metric_name,
            metric_observations=(observation,),
            guardrails=(
                HarnessEvalGuardrailResult(
                    guardrail="no_model",
                    status=EvalGuardrailStatus.PASSED,
                ),
                HarnessEvalGuardrailResult(
                    guardrail="no_side_effect",
                    status=EvalGuardrailStatus.PASSED,
                ),
            ),
            message=(
                f"{phase.upper()} cohort 发现 {counts[finding_code]} 项 "
                f"{finding_code}。"
            ),
            duration_ms=duration_ms,
        ))
    suite_status = (
        EvalRunStatus.PASSED
        if all(item.status is EvalCaseStatus.PASSED for item in cases)
        else EvalRunStatus.FAILED
    )
    title_phase = "RED baseline" if phase == "red" else "GREEN candidate"
    return HarnessEvalSuiteResult(
        suite_id=request.suite_id,
        title=f"Self-Review 静态 {title_phase}",
        suite_path=f"evolution:{plan.validation_plan_id}",
        suite_sha256=configuration.suite_sha256,
        status=suite_status,
        cases=tuple(cases),
        baseline_identity=identity,
        duration_ms=duration_ms,
    )


def _sha256_payload(payload: object) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


__all__ = [
    "SelfReviewEvalRuntimeError",
    "build_self_review_eval_configuration",
    "require_continuous_eval_prefix",
    "run_self_review_static_sample",
    "run_self_review_static_repetitions",
    "validate_self_review_cohort_authority",
]
