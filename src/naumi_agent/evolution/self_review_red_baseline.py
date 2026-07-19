"""Read-only Self-Review RED cohorts backed by exact Git objects and H5a."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.evolution.self_review import (
    SelfReviewFindingCode,
)
from naumi_agent.evolution.self_review_eval_runtime import (
    SelfReviewEvalRuntimeError,
    build_self_review_eval_configuration,
    require_continuous_eval_prefix,
    run_self_review_static_repetitions,
    validate_self_review_cohort_authority,
)
from naumi_agent.evolution.validation_cohorts import (
    EvolutionBaselineCohortRequest,
)
from naumi_agent.evolution.validation_metric_bindings import (
    EvolutionMetricRunnerBinding,
)
from naumi_agent.evolution.validation_plans import EvolutionValidationPlan
from naumi_agent.harness.eval_identity import (
    HarnessEvalBaselineIdentity,
    HarnessEvalConfigurationIdentity,
    HarnessEvalSourceIdentity,
    build_eval_baseline_identity,
    capture_eval_platform_identity,
)
from naumi_agent.harness.eval_models import HarnessEvalSuiteResult
from naumi_agent.harness.store import (
    HarnessStore,
    HarnessStoreConflictError,
    HarnessStoredEvalResult,
)
from naumi_agent.harness.trust import HarnessTrustStore

SELF_REVIEW_RED_BASELINE_POLICY = "evolution-self-review-red-baseline-v1"
_MAX_SOURCE_BYTES = 2_000_000
_GIT_TIMEOUT_SECONDS = 10
_SHA256_RE = r"^[0-9a-f]{64}$"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class SelfReviewRedMetricSummary(_StrictModel):
    metric_name: str = Field(pattern=r"^self_review\.[a-z][a-z0-9_]*\.count$")
    finding_code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    direction: Literal["decrease", "increase"]
    target: float
    sample_values: tuple[int, ...] = Field(min_length=5, max_length=100)

    @model_validator(mode="after")
    def _count_contract_is_consistent(self) -> Self:
        expected_metric = f"self_review.{self.finding_code}.count"
        if (
            self.metric_name != expected_metric
            or self.finding_code not in {item.value for item in SelfReviewFindingCode}
            or self.direction != "decrease"
            or self.target < 0
            or not float(self.target).is_integer()
            or any(isinstance(value, bool) or value < 0 for value in self.sample_values)
        ):
            raise ValueError("Self-Review RED metric summary 合同不一致。")
        return self


class EvolutionSelfReviewRedCohortReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-self-review-red-baseline-v1"] = (
        SELF_REVIEW_RED_BASELINE_POLICY
    )
    receipt_id: str = Field(pattern=r"^evvredrun_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    baseline_request_id: str = Field(pattern=r"^evvred_[0-9a-f]{24}$")
    baseline_request_sha256: str = Field(pattern=_SHA256_RE)
    metric_binding_id: str = Field(pattern=r"^evvmetric_[0-9a-f]{24}$")
    metric_binding_sha256: str = Field(pattern=_SHA256_RE)
    validation_plan_id: str = Field(pattern=r"^evvplan_[0-9a-f]{24}$")
    validation_plan_sha256: str = Field(pattern=_SHA256_RE)
    phase: Literal["red"] = "red"
    suite_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    batch_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    baseline_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    baseline_tree_sha256: str = Field(pattern=_SHA256_RE)
    requested_samples: int = Field(ge=5, le=100)
    persisted_samples: int = Field(ge=5, le=100)
    sample_result_sha256: tuple[str, ...] = Field(min_length=5, max_length=100)
    metrics: tuple[SelfReviewRedMetricSummary, ...] = Field(min_length=1, max_length=8)
    source_access: Literal["git_object_database"] = "git_object_database"
    profile_trust_revalidated: Literal[True] = True
    model_access: Literal[False] = False
    network_access: Literal[False] = False
    project_code_executed: Literal[False] = False
    arc04_worker_used: Literal[False] = False
    cohort_complete: Literal[True] = True
    completed_at: str

    @field_validator("sample_result_sha256")
    @classmethod
    def _valid_result_digests(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(re.fullmatch(_SHA256_RE, value) is None for value in values):
            raise ValueError("Self-Review RED sample digest 格式无效。")
        return values

    @field_validator("completed_at")
    @classmethod
    def _aware_completed_at(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("Self-Review RED completed_at 格式无效。") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("Self-Review RED completed_at 必须包含时区。")
        return parsed.isoformat()

    @model_validator(mode="after")
    def _receipt_is_complete_and_tamper_evident(self) -> Self:
        if not (
            self.persisted_samples == self.requested_samples
            == len(self.sample_result_sha256)
        ):
            raise ValueError("Self-Review RED cohort 样本汇总不完整。")
        if any(
            len(item.sample_values) != self.requested_samples
            for item in self.metrics
        ):
            raise ValueError("Self-Review RED metric 样本数量不完整。")
        metric_names = tuple(item.metric_name for item in self.metrics)
        if len(metric_names) != len(set(metric_names)):
            raise ValueError("Self-Review RED metric summary 不得重复。")
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        )
        if not hmac.compare_digest(self.receipt_sha256, expected):
            raise ValueError("Self-Review RED receipt 摘要不一致。")
        if self.receipt_id != f"evvredrun_{expected[:24]}":
            raise ValueError("Self-Review RED receipt identity 不一致。")
        return self


class EvolutionSelfReviewRedBaselineError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionSelfReviewRedBaselineExecutor:
    """Persist one trustworthy static RED cohort without executing project code."""

    def __init__(
        self,
        *,
        store: HarnessStore,
        trust_store: HarnessTrustStore,
    ) -> None:
        if not isinstance(store, HarnessStore):
            raise TypeError("Self-Review RED executor 需要 HarnessStore。")
        if not isinstance(trust_store, HarnessTrustStore):
            raise TypeError("Self-Review RED executor 需要 HarnessTrustStore。")
        self._store = store
        self._trust_store = trust_store

    async def execute(
        self,
        *,
        workspace_root: str | Path,
        baseline_request: EvolutionBaselineCohortRequest,
        metric_binding: EvolutionMetricRunnerBinding,
        validation_plan: EvolutionValidationPlan,
    ) -> EvolutionSelfReviewRedCohortReceipt:
        root = _canonical_git_root(workspace_root)
        try:
            request, binding, plan = validate_self_review_cohort_authority(
                baseline_request,
                metric_binding,
                validation_plan,
            )
        except SelfReviewEvalRuntimeError as exc:
            raise EvolutionSelfReviewRedBaselineError(exc.code, str(exc)) from exc
        if not await self._trust_store.is_trusted(root, request.profile_sha256):
            raise EvolutionSelfReviewRedBaselineError(
                "profile_trust_revalidation_failed",
                "Harness Profile 信任已失效，不能执行 Self-Review RED baseline。",
            )
        blobs = _load_exact_git_blobs(root, request, plan)
        configuration, identity = _build_identity(root, request, binding, plan)
        existing = await self._store.list_eval_results(
            root,
            request.batch_id,
            request.suite_id,
            limit=request.requested_samples + 1,
        )
        _require_red_prefix(existing, request.requested_samples)
        results = await _run_repetitions(
            blobs=blobs,
            request=request,
            binding=binding,
            plan=plan,
            configuration=configuration,
            identity=identity,
        )
        for stored, expected in zip(existing, results, strict=False):
            if stored.result.canonical_payload() != expected.canonical_payload():
                raise EvolutionSelfReviewRedBaselineError(
                    "existing_cohort_conflict",
                    "已有 Self-Review RED sample 与当前可信输入不一致。",
                )
        if len(existing) == request.requested_samples:
            return _build_receipt(request, binding, plan, existing)

        created_at = datetime.now(UTC).isoformat()
        for sample_index in range(len(existing), request.requested_samples):
            try:
                await self._store.record_eval_result(
                    workspace_root=root,
                    batch_id=request.batch_id,
                    sample_index=sample_index,
                    result=results[sample_index],
                    created_at=created_at,
                )
            except HarnessStoreConflictError as exc:
                raced = await self._store.get_eval_result(
                    root,
                    request.batch_id,
                    request.suite_id,
                    sample_index,
                )
                if (
                    raced is None
                    or raced.result.canonical_payload()
                    != results[sample_index].canonical_payload()
                ):
                    raise EvolutionSelfReviewRedBaselineError(
                        "cohort_persistence_conflict",
                        "Self-Review RED cohort 并发写入发生冲突。",
                    ) from exc

        persisted = await self._store.list_eval_results(
            root,
            request.batch_id,
            request.suite_id,
            limit=request.requested_samples + 1,
        )
        _require_red_prefix(persisted, request.requested_samples)
        if len(persisted) != request.requested_samples:
            raise EvolutionSelfReviewRedBaselineError(
                "cohort_persistence_incomplete",
                "Self-Review RED cohort 未完整写入 H5a。",
            )
        return _build_receipt(request, binding, plan, persisted)


async def _run_repetitions(
    *,
    blobs: tuple[tuple[str, bytes], ...],
    request: EvolutionBaselineCohortRequest,
    binding: EvolutionMetricRunnerBinding,
    plan: EvolutionValidationPlan,
    configuration: HarnessEvalConfigurationIdentity,
    identity: HarnessEvalBaselineIdentity,
) -> tuple[HarnessEvalSuiteResult, ...]:
    with tempfile.TemporaryDirectory(prefix="naumi-evo-red-") as temporary:
        root = Path(temporary).resolve()
        files: list[Path] = []
        for relative, content in blobs:
            destination = root.joinpath(*PurePosixPath(relative).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            files.append(destination)
        try:
            return await run_self_review_static_repetitions(
                files=files,
                scan_root=root,
                phase="red",
                request=request,
                binding=binding,
                plan=plan,
                configuration=configuration,
                identity=identity,
            )
        except SelfReviewEvalRuntimeError as exc:
            raise EvolutionSelfReviewRedBaselineError(exc.code, str(exc)) from exc


def _require_red_prefix(
    records: tuple[HarnessStoredEvalResult, ...],
    requested_samples: int,
) -> None:
    try:
        require_continuous_eval_prefix(records, requested_samples, phase="red")
    except SelfReviewEvalRuntimeError as exc:
        raise EvolutionSelfReviewRedBaselineError(exc.code, str(exc)) from exc


def _build_identity(
    root: Path,
    request: EvolutionBaselineCohortRequest,
    binding: EvolutionMetricRunnerBinding,
    plan: EvolutionValidationPlan,
):
    configuration = build_self_review_eval_configuration(request, binding, plan)
    identity = build_eval_baseline_identity(
        root,
        configuration=configuration,
        platform_identity=capture_eval_platform_identity(),
        profile_trusted=True,
        source_identity=HarnessEvalSourceIdentity(
            commit=request.baseline_commit,
            tree_sha256=f"sha256:{request.baseline_tree_sha256}",
            dirty=False,
        ),
    )
    return configuration, identity


def _load_exact_git_blobs(
    root: Path,
    request: EvolutionBaselineCohortRequest,
    plan: EvolutionValidationPlan,
) -> tuple[tuple[str, bytes], ...]:
    resolved_commit = _git(root, "rev-parse", "--verify", f"{request.baseline_commit}^{{commit}}")
    if resolved_commit.decode("ascii").strip().lower() != request.baseline_commit:
        raise EvolutionSelfReviewRedBaselineError(
            "baseline_commit_mismatch",
            "Git baseline commit 无法精确解析。",
        )
    tree_listing = _git(
        root,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        request.baseline_commit,
    )
    if hashlib.sha256(tree_listing).hexdigest() != request.baseline_tree_sha256:
        raise EvolutionSelfReviewRedBaselineError(
            "baseline_tree_mismatch",
            "Git baseline tree 与 Validation Plan 不一致。",
        )
    blobs: list[tuple[str, bytes]] = []
    for file in plan.files:
        entry = _git(
            root,
            "ls-tree",
            "-z",
            "--full-tree",
            request.baseline_commit,
            "--",
            file.path,
        )
        if file.operation == "create":
            if entry:
                raise EvolutionSelfReviewRedBaselineError(
                    "created_path_exists_at_baseline",
                    "Create Validation file 已存在于 Git baseline。",
                )
            blobs.append((file.path, b""))
            continue
        if file.operation != "modify" or file.baseline_sha256 is None:
            raise EvolutionSelfReviewRedBaselineError(
                "validation_file_operation_unbound",
                "Validation Plan 未绑定可信文件 operation。",
            )
        parts = entry.rstrip(b"\0").split(b"\t", maxsplit=1)
        if len(parts) != 2 or parts[1] != file.path.encode("utf-8"):
            raise EvolutionSelfReviewRedBaselineError(
                "baseline_path_missing",
                "Validation Plan 文件在 Git baseline 中不存在。",
            )
        header = parts[0].decode("ascii").split()
        if len(header) != 3 or header[0] not in {"100644", "100755"} or header[1] != "blob":
            raise EvolutionSelfReviewRedBaselineError(
                "baseline_path_type_unsafe",
                "Validation Plan 文件不是普通 Git blob。",
            )
        blob = header[2]
        size_raw = _git(root, "cat-file", "-s", blob)
        try:
            size = int(size_raw.decode("ascii").strip())
        except ValueError as exc:
            raise EvolutionSelfReviewRedBaselineError(
                "baseline_blob_invalid",
                "Git baseline blob 大小无效。",
            ) from exc
        if not 0 <= size <= _MAX_SOURCE_BYTES:
            raise EvolutionSelfReviewRedBaselineError(
                "baseline_blob_too_large",
                "Self-Review baseline 单文件不能超过 2 MiB。",
            )
        content = _git(root, "cat-file", "blob", blob)
        if len(content) != size:
            raise EvolutionSelfReviewRedBaselineError(
                "baseline_blob_size_mismatch",
                "Git baseline blob 读取不完整。",
            )
        if hashlib.sha256(content).hexdigest() != file.baseline_sha256:
            raise EvolutionSelfReviewRedBaselineError(
                "baseline_blob_digest_mismatch",
                "Git baseline blob 与 Mutation Receipt before digest 不一致。",
            )
        blobs.append((file.path, content))
    return tuple(blobs)


def load_exact_validation_blobs(
    workspace_root: str | Path,
    request: EvolutionBaselineCohortRequest,
    plan: EvolutionValidationPlan,
) -> tuple[tuple[str, bytes], ...]:
    """Load validation files from the exact bound Git tree without checkout."""
    return _load_exact_git_blobs(
        _canonical_git_root(workspace_root),
        request,
        plan,
    )


def _canonical_git_root(workspace_root: str | Path) -> Path:
    try:
        requested = Path(workspace_root).expanduser().resolve(strict=True)
    except (OSError, TypeError) as exc:
        raise EvolutionSelfReviewRedBaselineError(
            "workspace_unavailable",
            "Self-Review RED workspace 不存在或无法读取。",
        ) from exc
    top = _git(requested, "rev-parse", "--show-toplevel")
    root = Path(top.decode("utf-8").strip()).resolve()
    if requested != root:
        raise EvolutionSelfReviewRedBaselineError(
            "workspace_root_required",
            "Self-Review RED workspace 必须是精确 Git 仓库根目录。",
        )
    return root


def _git(root: Path, *args: str) -> bytes:
    command = [
        "git",
        "--no-replace-objects",
        "--literal-pathspecs",
        "-C",
        str(root),
        *args,
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EvolutionSelfReviewRedBaselineError(
            "git_read_failed",
            "无法只读访问 Git baseline。",
        ) from exc
    if completed.returncode != 0:
        raise EvolutionSelfReviewRedBaselineError(
            "git_read_failed",
            "无法只读访问 Git baseline。",
        )
    return completed.stdout


def _build_receipt(
    request: EvolutionBaselineCohortRequest,
    binding: EvolutionMetricRunnerBinding,
    plan: EvolutionValidationPlan,
    records: tuple[HarnessStoredEvalResult, ...],
) -> EvolutionSelfReviewRedCohortReceipt:
    metrics: list[SelfReviewRedMetricSummary] = []
    for entry in binding.entries:
        values: list[int] = []
        for record in records:
            observation = next(
                item
                for case in record.result.cases
                for item in case.metric_observations
                if item.metric == entry.metric_name
            )
            values.append(int(observation.value))
        metrics.append(SelfReviewRedMetricSummary(
            metric_name=entry.metric_name,
            finding_code=entry.resolution.finding_code or "invalid",
            direction=entry.direction,
            target=entry.target,
            sample_values=tuple(values),
        ))
    payload = {
        "schema_version": 1,
        "policy_version": SELF_REVIEW_RED_BASELINE_POLICY,
        "baseline_request_id": request.request_id,
        "baseline_request_sha256": request.request_sha256,
        "metric_binding_id": binding.binding_id,
        "metric_binding_sha256": binding.binding_sha256,
        "validation_plan_id": plan.validation_plan_id,
        "validation_plan_sha256": plan.validation_plan_sha256,
        "phase": "red",
        "suite_id": request.suite_id,
        "batch_id": request.batch_id,
        "baseline_commit": request.baseline_commit,
        "baseline_tree_sha256": request.baseline_tree_sha256,
        "requested_samples": request.requested_samples,
        "persisted_samples": len(records),
        "sample_result_sha256": [item.result_sha256 for item in records],
        "metrics": [item.model_dump(mode="json") for item in metrics],
        "source_access": "git_object_database",
        "profile_trust_revalidated": True,
        "model_access": False,
        "network_access": False,
        "project_code_executed": False,
        "arc04_worker_used": False,
        "cohort_complete": True,
        "completed_at": max(item.created_at for item in records),
    }
    digest = _sha256_payload(payload)
    return EvolutionSelfReviewRedCohortReceipt.model_validate({
        **payload,
        "receipt_id": f"evvredrun_{digest[:24]}",
        "receipt_sha256": digest,
    })


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
    "EvolutionSelfReviewRedBaselineError",
    "EvolutionSelfReviewRedBaselineExecutor",
    "EvolutionSelfReviewRedCohortReceipt",
    "SelfReviewRedMetricSummary",
    "load_exact_validation_blobs",
]
