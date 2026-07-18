"""Tamper-evident, non-executable validation plans for evolved mutations."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.evolution.experiment_leases import (
    ExperimentLeaseState,
    ExperimentWorktreeLease,
)
from naumi_agent.evolution.experiment_snapshots import EvolutionExperimentSourceSnapshot
from naumi_agent.evolution.experiments import EvolutionExperimentContract
from naumi_agent.evolution.mutation_receipts import EvolutionMutationReceipt
from naumi_agent.harness.checks import select_required_check_ids
from naumi_agent.harness.models import (
    HarnessCheckSpec,
    HarnessProfileStatus,
)
from naumi_agent.harness.profile import load_harness_profile
from naumi_agent.harness.trust import HarnessTrustStore

VALIDATION_PLAN_POLICY = "evolution-validation-plan-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_SAFE_CODE_RE = r"^[a-z][a-z0-9_-]{0,63}$"

type ValidationCheckKind = Literal["lint", "compile", "unit", "contract", "smoke"]
type ValidationFileKind = Literal[
    "python",
    "javascript",
    "typescript",
    "swift",
    "rust",
    "go",
    "markdown",
    "yaml",
    "json",
    "toml",
    "other",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class ValidationMetricPair(_StrictModel):
    order: int = Field(ge=1, le=8)
    metric_name: str = Field(min_length=1, max_length=128)
    direction: Literal["decrease", "increase"]
    target: float
    verifier: Literal[
        "harness_replay",
        "self_review_static",
        "feedback_recurrence",
    ]
    procedure: str = Field(min_length=1, max_length=1_000)
    baseline_phase: Literal["red"] = "red"
    candidate_phase: Literal["green"] = "green"
    same_fixture_required: Literal[True] = True
    same_seed_required: Literal[True] = True


class ValidationFileRequirement(_StrictModel):
    path: str = Field(min_length=1, max_length=1_024)
    file_kind: ValidationFileKind
    required_checks: tuple[ValidationCheckKind, ...] = Field(min_length=1, max_length=5)

    @field_validator("path")
    @classmethod
    def _safe_path(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        path = Path(normalized)
        if (
            not normalized
            or path.is_absolute()
            or ".." in path.parts
            or any(char in normalized for char in ("\x00", "\r", "\n"))
        ):
            raise ValueError("Validation file path 必须是安全相对路径。")
        return normalized

    @field_validator("required_checks")
    @classmethod
    def _unique_checks(
        cls,
        values: tuple[ValidationCheckKind, ...],
    ) -> tuple[ValidationCheckKind, ...]:
        if len(values) != len(set(values)):
            raise ValueError("Validation file checks 不得重复。")
        return values


class EvolutionValidationPlan(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-validation-plan-v1"] = VALIDATION_PLAN_POLICY
    validation_plan_id: str = Field(pattern=r"^evvplan_[0-9a-f]{24}$")
    validation_plan_sha256: str = Field(pattern=_SHA256_RE)
    contract_id: str = Field(pattern=r"^evx_[0-9a-f]{24}$")
    contract_manifest_sha256: str = Field(pattern=_SHA256_RE)
    lease_id: str = Field(pattern=r"^evl_[0-9a-f]{24}$")
    source_snapshot_id: str = Field(pattern=r"^evs_[0-9a-f]{24}$")
    source_snapshot_sha256: str = Field(pattern=_SHA256_RE)
    mutation_receipt_id: str = Field(pattern=r"^evmr_[0-9a-f]{24}$")
    mutation_receipt_sha256: str = Field(pattern=_SHA256_RE)
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    seed: int = Field(ge=0, le=9_223_372_036_854_775_807)
    baseline_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    baseline_tree_sha256: str = Field(pattern=_SHA256_RE)
    profile_sha256: str = Field(pattern=_SHA256_RE)
    experiment_config_sha256: str = Field(pattern=_SHA256_RE)
    toolset_sha256: str = Field(pattern=_SHA256_RE)
    candidate_files_sha256: str = Field(pattern=_SHA256_RE)
    files: tuple[ValidationFileRequirement, ...] = Field(min_length=1, max_length=16)
    metrics: tuple[ValidationMetricPair, ...] = Field(min_length=1, max_length=8)
    required_check_kinds: tuple[ValidationCheckKind, ...] = Field(
        min_length=1,
        max_length=5,
    )
    baseline_first: Literal[True] = True
    identical_environment_required: Literal[True] = True
    har08_comparison_receipt_required: Literal[True] = True
    validation_ready: Literal[True] = True
    runner_binding_status: Literal["required"] = "required"
    execution_ready: Literal[False] = False
    promotion_ready: Literal[False] = False

    @model_validator(mode="after")
    def _plan_is_ordered_and_tamper_evident(self) -> Self:
        if tuple(item.order for item in self.metrics) != tuple(
            range(1, len(self.metrics) + 1)
        ):
            raise ValueError("Validation metrics 必须按连续顺序排列。")
        paths = tuple(item.path for item in self.files)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("Validation files 必须排序且不得重复。")
        derived = tuple(sorted({kind for item in self.files for kind in item.required_checks}))
        if self.required_check_kinds != derived:
            raise ValueError("required_check_kinds 与文件要求不一致。")
        expected = _sha256_payload(
            self.model_dump(
                mode="json",
                exclude={"validation_plan_id", "validation_plan_sha256"},
            )
        )
        if not hmac.compare_digest(self.validation_plan_sha256, expected):
            raise ValueError("Validation Plan 摘要不一致。")
        if self.validation_plan_id != f"evvplan_{expected[:24]}":
            raise ValueError("Validation Plan identity 不一致。")
        return self


class ValidationProfileCheckBinding(_StrictModel):
    check_id: str = Field(pattern=_SAFE_CODE_RE)
    spec_sha256: str = Field(pattern=_SHA256_RE)
    argv_sha256: str = Field(pattern=_SHA256_RE)
    timeout_seconds: int = Field(ge=1, le=3_600)
    provides: tuple[ValidationCheckKind, ...] = Field(min_length=1, max_length=5)

    @field_validator("provides")
    @classmethod
    def _ordered_provides(
        cls,
        values: tuple[ValidationCheckKind, ...],
    ) -> tuple[ValidationCheckKind, ...]:
        if values != tuple(sorted(set(values))):
            raise ValueError("Validation check provides 必须排序且不得重复。")
        return values


class ValidationCheckCoverage(_StrictModel):
    path: str = Field(min_length=1, max_length=1_024)
    check_kind: ValidationCheckKind
    check_id: str = Field(pattern=_SAFE_CODE_RE)

    @field_validator("path")
    @classmethod
    def _safe_path(cls, value: str) -> str:
        return ValidationFileRequirement._safe_path(value)


class EvolutionValidationProfileBinding(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-validation-profile-binding-v1"] = (
        "evolution-validation-profile-binding-v1"
    )
    binding_id: str = Field(pattern=r"^evvbind_[0-9a-f]{24}$")
    binding_sha256: str = Field(pattern=_SHA256_RE)
    validation_plan_id: str = Field(pattern=r"^evvplan_[0-9a-f]{24}$")
    validation_plan_sha256: str = Field(pattern=_SHA256_RE)
    profile_sha256: str = Field(pattern=_SHA256_RE)
    profile_path: str = Field(min_length=1, max_length=1_024)
    trusted_at: str = Field(min_length=1, max_length=100)
    trust_source: str = Field(min_length=1, max_length=64)
    required_check_kinds: tuple[ValidationCheckKind, ...] = Field(
        min_length=1,
        max_length=5,
    )
    plan_requirements_sha256: str = Field(pattern=_SHA256_RE)
    checks: tuple[ValidationProfileCheckBinding, ...] = Field(
        min_length=1,
        max_length=80,
    )
    coverage: tuple[ValidationCheckCoverage, ...] = Field(
        min_length=1,
        max_length=80,
    )
    profile_trusted: Literal[True] = True
    profile_trust_must_be_revalidated: Literal[True] = True
    binding_ready: Literal[True] = True
    arc04_worker_required: Literal[True] = True
    execution_ready: Literal[False] = False

    @field_validator("profile_path")
    @classmethod
    def _safe_profile_path(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        path = Path(normalized)
        if not normalized or path.is_absolute() or ".." in path.parts:
            raise ValueError("Validation Profile path 必须是工作区内相对路径。")
        return normalized

    @field_validator("trusted_at")
    @classmethod
    def _aware_trusted_at(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("Validation Profile trusted_at 必须是 ISO-8601。") from exc
        if parsed.utcoffset() is None:
            raise ValueError("Validation Profile trusted_at 必须包含 UTC offset。")
        return value

    @field_validator("trust_source")
    @classmethod
    def _safe_trust_source(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(char in normalized for char in ("\x00", "\r", "\n")):
            raise ValueError("Validation Profile trust_source 格式无效。")
        return normalized

    @model_validator(mode="after")
    def _binding_is_complete_and_tamper_evident(self) -> Self:
        check_ids = tuple(item.check_id for item in self.checks)
        if check_ids != tuple(sorted(set(check_ids))):
            raise ValueError("Validation bound checks 必须排序且不得重复。")
        coverage_keys = tuple((item.path, item.check_kind) for item in self.coverage)
        if coverage_keys != tuple(sorted(set(coverage_keys))):
            raise ValueError("Validation coverage 必须排序且不得重复。")
        coverage_kinds = tuple(sorted({item.check_kind for item in self.coverage}))
        if coverage_kinds != self.required_check_kinds:
            raise ValueError("Validation coverage 未完整覆盖 required check kinds。")
        expected_requirements = _sha256_payload([
            {"path": item.path, "check_kind": item.check_kind}
            for item in self.coverage
        ])
        if not hmac.compare_digest(self.plan_requirements_sha256, expected_requirements):
            raise ValueError("Validation coverage 与 Plan requirements 不一致。")
        by_id = {item.check_id: item for item in self.checks}
        if any(
            item.check_id not in by_id
            or item.check_kind not in by_id[item.check_id].provides
            for item in self.coverage
        ):
            raise ValueError("Validation coverage 与 bound check capability 不一致。")
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"binding_id", "binding_sha256"})
        )
        if not hmac.compare_digest(self.binding_sha256, expected):
            raise ValueError("Validation Profile Binding 摘要不一致。")
        if self.binding_id != f"evvbind_{expected[:24]}":
            raise ValueError("Validation Profile Binding identity 不一致。")
        return self


class EvolutionValidationBindingError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionValidationProfileBinder:
    """Bind abstract validation requirements to one currently trusted Profile."""

    def __init__(self, trust_store: HarnessTrustStore) -> None:
        if not isinstance(trust_store, HarnessTrustStore):
            raise TypeError("Validation Profile Binder 需要 HarnessTrustStore。")
        self._trust_store = trust_store

    async def bind(
        self,
        plan: EvolutionValidationPlan,
        *,
        workspace_root: str | Path,
    ) -> EvolutionValidationProfileBinding:
        if not isinstance(plan, EvolutionValidationPlan):
            raise TypeError("Validation Profile Binder 需要 EvolutionValidationPlan。")
        plan = EvolutionValidationPlan.model_validate(plan.model_dump(mode="json"))
        workspace = Path(workspace_root).expanduser().resolve()
        snapshot = load_harness_profile(workspace)
        if (
            snapshot.status is not HarnessProfileStatus.VALID
            or snapshot.profile is None
            or snapshot.digest is None
        ):
            raise EvolutionValidationBindingError(
                "validation_profile_invalid",
                "当前 Harness Profile 无效，无法绑定 Validation checks。",
            )
        if snapshot.digest != plan.profile_sha256:
            raise EvolutionValidationBindingError(
                "validation_profile_drifted",
                "Harness Profile 已偏离 Mutation baseline，必须重新开始实验。",
            )
        trust = await self._trust_store.get(workspace)
        if trust is None or trust.profile_digest != snapshot.digest:
            raise EvolutionValidationBindingError(
                "validation_profile_untrusted",
                "当前 Harness Profile 尚未由用户信任，未绑定 Validation checks。",
            )
        coverage: list[ValidationCheckCoverage] = []
        used_ids: set[str] = set()
        for file in plan.files:
            selected_ids = select_required_check_ids(
                snapshot.profile.checks,
                task_kind="change",
                changed_paths=(file.path,),
            )
            selected = tuple(
                check for check in snapshot.profile.checks if check.id in selected_ids
            )
            for kind in file.required_checks:
                candidates = tuple(check for check in selected if kind in check.provides)
                if not candidates:
                    raise EvolutionValidationBindingError(
                        "validation_check_missing",
                        f"可信 Harness Profile 未覆盖 {file.path} 的 check kind：{kind}。",
                    )
                if len(candidates) != 1:
                    raise EvolutionValidationBindingError(
                        "validation_check_ambiguous",
                        f"{file.path} 的 check kind {kind} 匹配多个 Profile checks。",
                    )
                check = candidates[0]
                used_ids.add(check.id)
                coverage.append(ValidationCheckCoverage(
                    path=file.path,
                    check_kind=kind,
                    check_id=check.id,
                ))
        checks = tuple(
            _profile_check_binding(check)
            for check in sorted(snapshot.profile.checks, key=lambda item: item.id)
            if check.id in used_ids
        )
        ordered_coverage = tuple(
            sorted(coverage, key=lambda item: (item.path, item.check_kind))
        )
        requirements_sha256 = _sha256_payload([
            {"path": item.path, "check_kind": item.check_kind}
            for item in ordered_coverage
        ])
        relative_profile = snapshot.profile_path.relative_to(workspace).as_posix()
        payload = {
            "schema_version": 1,
            "policy_version": "evolution-validation-profile-binding-v1",
            "validation_plan_id": plan.validation_plan_id,
            "validation_plan_sha256": plan.validation_plan_sha256,
            "profile_sha256": snapshot.digest,
            "profile_path": relative_profile,
            "trusted_at": trust.trusted_at,
            "trust_source": trust.source,
            "required_check_kinds": list(plan.required_check_kinds),
            "plan_requirements_sha256": requirements_sha256,
            "checks": [item.model_dump(mode="json") for item in checks],
            "coverage": [
                item.model_dump(mode="json")
                for item in ordered_coverage
            ],
            "profile_trusted": True,
            "profile_trust_must_be_revalidated": True,
            "binding_ready": True,
            "arc04_worker_required": True,
            "execution_ready": False,
        }
        digest = _sha256_payload(payload)
        return EvolutionValidationProfileBinding.model_validate({
            **payload,
            "binding_id": f"evvbind_{digest[:24]}",
            "binding_sha256": digest,
        })


class EvolutionValidationPlanner:
    """Compile existing evolution authority into a non-executable RED/GREEN plan."""

    def plan(
        self,
        *,
        contract: EvolutionExperimentContract,
        lease: ExperimentWorktreeLease,
        source_snapshot: EvolutionExperimentSourceSnapshot,
        mutation_receipt: EvolutionMutationReceipt,
    ) -> EvolutionValidationPlan:
        _require_bindings(contract, lease, source_snapshot, mutation_receipt)
        contract = EvolutionExperimentContract.model_validate(
            contract.model_dump(mode="json")
        )
        lease = ExperimentWorktreeLease.model_validate(lease.model_dump(mode="json"))
        source_snapshot = EvolutionExperimentSourceSnapshot.model_validate(
            source_snapshot.model_dump(mode="json")
        )
        mutation_receipt = EvolutionMutationReceipt.model_validate(
            mutation_receipt.model_dump(mode="json")
        )
        metrics = tuple(
            ValidationMetricPair(
                order=index,
                metric_name=check.metric_name,
                direction=check.direction,
                target=check.target,
                verifier=check.verifier,
                procedure=check.procedure,
            )
            for index, check in enumerate(contract.allowed_checks, start=1)
        )
        files = tuple(
            validation_requirements_for_path(item.path)
            for item in mutation_receipt.files
        )
        payload = {
            "schema_version": 1,
            "policy_version": VALIDATION_PLAN_POLICY,
            "contract_id": contract.contract_id,
            "contract_manifest_sha256": contract.manifest_sha256,
            "lease_id": lease.lease_id,
            "source_snapshot_id": source_snapshot.snapshot_id,
            "source_snapshot_sha256": source_snapshot.snapshot_sha256,
            "mutation_receipt_id": mutation_receipt.mutation_receipt_id,
            "mutation_receipt_sha256": mutation_receipt.receipt_sha256,
            "candidate_id": mutation_receipt.candidate_id,
            "candidate_revision": mutation_receipt.candidate_revision,
            "seed": contract.seed,
            "baseline_commit": contract.baseline.commit,
            "baseline_tree_sha256": source_snapshot.baseline_tree_sha256,
            "profile_sha256": source_snapshot.profile_sha256,
            "experiment_config_sha256": source_snapshot.experiment_config_sha256,
            "toolset_sha256": source_snapshot.toolset_sha256,
            "candidate_files_sha256": mutation_receipt.files_sha256,
            "files": [item.model_dump(mode="json") for item in files],
            "metrics": [item.model_dump(mode="json") for item in metrics],
            "required_check_kinds": sorted(
                {kind for item in files for kind in item.required_checks}
            ),
            "baseline_first": True,
            "identical_environment_required": True,
            "har08_comparison_receipt_required": True,
            "validation_ready": True,
            "runner_binding_status": "required",
            "execution_ready": False,
            "promotion_ready": False,
        }
        digest = _sha256_payload(payload)
        return EvolutionValidationPlan.model_validate({
            **payload,
            "validation_plan_id": f"evvplan_{digest[:24]}",
            "validation_plan_sha256": digest,
        })


def _require_bindings(
    contract: EvolutionExperimentContract,
    lease: ExperimentWorktreeLease,
    snapshot: EvolutionExperimentSourceSnapshot,
    receipt: EvolutionMutationReceipt,
) -> None:
    if not isinstance(contract, EvolutionExperimentContract):
        raise TypeError("Validation Plan 需要 EvolutionExperimentContract。")
    if not isinstance(lease, ExperimentWorktreeLease):
        raise TypeError("Validation Plan 需要 ExperimentWorktreeLease。")
    if not isinstance(snapshot, EvolutionExperimentSourceSnapshot):
        raise TypeError("Validation Plan 需要 EvolutionExperimentSourceSnapshot。")
    if not isinstance(receipt, EvolutionMutationReceipt):
        raise TypeError("Validation Plan 需要 EvolutionMutationReceipt。")
    if lease.state is not ExperimentLeaseState.ACTIVE or not lease.worktree_ready:
        raise ValueError("Validation Plan 需要 active Experiment Lease。")
    if receipt.schema_version != 2 or not receipt.validation_ready:
        raise ValueError("Validation Plan 只接受 validation-ready Mutation Receipt v2。")
    if snapshot.profile_status != "valid":
        raise ValueError("Validation Plan 需要有效 Harness Profile identity。")
    if not (
        contract.contract_id == lease.contract_id == snapshot.contract_id == receipt.contract_id
        and contract.manifest_sha256
        == lease.manifest_sha256
        == snapshot.contract_manifest_sha256
        == receipt.contract_manifest_sha256
        and lease.lease_id == snapshot.lease_id == receipt.lease_id
        and snapshot.snapshot_id == receipt.source_snapshot_id
        and snapshot.snapshot_sha256 == receipt.source_snapshot_sha256
        and contract.source.candidate_id == receipt.candidate_id
        and contract.source.candidate_revision == receipt.candidate_revision
        and contract.source.candidate_sha256 == receipt.candidate_sha256
        and contract.baseline.commit == snapshot.baseline_commit == lease.baseline_commit
    ):
        raise ValueError("Validation Plan authority binding 不一致。")
    expected_paths = tuple(sorted(contract.scope.allowed_files))
    if tuple(item.path for item in receipt.files) != expected_paths:
        raise ValueError("Validation Plan 文件 scope 与 Contract 不一致。")
    expected_metrics = tuple(check.metric_name for check in contract.allowed_checks)
    if receipt.required_metrics != expected_metrics:
        raise ValueError("Validation Plan metrics 与 Contract 不一致。")


def _file_kind(path: str) -> ValidationFileKind:
    suffix = Path(path).suffix.casefold()
    kinds: dict[str, ValidationFileKind] = {
        ".py": "python",
        ".js": "javascript",
        ".mjs": "javascript",
        ".cjs": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".swift": "swift",
        ".rs": "rust",
        ".go": "go",
        ".md": "markdown",
        ".markdown": "markdown",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".json": "json",
        ".toml": "toml",
    }
    return kinds.get(suffix, "other")


def validation_requirements_for_path(path: str) -> ValidationFileRequirement:
    """Return deterministic language-aware checks without inventing commands."""
    kind = _file_kind(path)
    return ValidationFileRequirement(
        path=path,
        file_kind=kind,
        required_checks=_required_checks(kind),
    )


def _profile_check_binding(check: HarnessCheckSpec) -> ValidationProfileCheckBinding:
    payload = check.model_dump(mode="json")
    return ValidationProfileCheckBinding(
        check_id=check.id,
        spec_sha256=_sha256_payload(payload),
        argv_sha256=_sha256_payload(list(check.argv)),
        timeout_seconds=check.timeout_seconds,
        provides=check.provides,
    )


def _required_checks(kind: ValidationFileKind) -> tuple[ValidationCheckKind, ...]:
    return {
        "python": ("lint", "compile", "unit", "contract"),
        "javascript": ("lint", "unit", "contract"),
        "typescript": ("lint", "compile", "unit", "contract"),
        "swift": ("compile", "unit", "contract"),
        "rust": ("compile", "unit", "contract"),
        "go": ("compile", "unit", "contract"),
        "markdown": ("lint", "contract"),
        "yaml": ("lint", "contract"),
        "json": ("lint", "contract"),
        "toml": ("lint", "contract"),
        "other": ("contract", "smoke"),
    }[kind]


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
    "EvolutionValidationBindingError",
    "EvolutionValidationPlan",
    "EvolutionValidationPlanner",
    "EvolutionValidationProfileBinder",
    "EvolutionValidationProfileBinding",
    "ValidationCheckCoverage",
    "ValidationFileRequirement",
    "ValidationMetricPair",
    "ValidationProfileCheckBinding",
    "validation_requirements_for_path",
]
