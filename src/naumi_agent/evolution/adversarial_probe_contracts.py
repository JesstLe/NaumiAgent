"""Tamper-evident contracts for mechanically selected adversarial probes."""

from __future__ import annotations

import hashlib
import hmac
import json
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.evolution.validation_plans import (
    EvolutionValidationPlan,
    EvolutionValidationProfileBinding,
)
from naumi_agent.harness.checks import select_required_check_ids
from naumi_agent.harness.eval_identity import (
    HarnessEvalPlatformIdentity,
    capture_eval_platform_identity,
)
from naumi_agent.harness.models import HarnessCheckSpec, HarnessProfileStatus
from naumi_agent.harness.profile import load_harness_profile
from naumi_agent.harness.trust import HarnessTrustStore

type AdversarialProbeKind = Literal[
    "boundary",
    "concurrency",
    "security",
    "recovery",
    "cross_platform",
    "reward_hacking",
]

_SHA256_RE = r"^[0-9a-f]{64}$"
_SAFE_CODE_RE = r"^[a-z][a-z0-9_-]{0,63}$"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class AdversarialProbeDefinition(_StrictModel):
    probe_id: str = Field(pattern=_SAFE_CODE_RE)
    kind: AdversarialProbeKind
    version: int = Field(ge=1, le=1_000_000)
    path_patterns: tuple[str, ...] = Field(max_length=32)
    always_required: bool = False
    evidence_type: Literal["harness_check_receipt"] = "harness_check_receipt"
    success_rule: Literal["exit_zero"] = "exit_zero"
    platform_scope: Literal["current", "matrix"] = "current"

    @field_validator("path_patterns")
    @classmethod
    def _safe_patterns(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip().replace("\\", "/") for value in values)
        if any(
            not value or "\x00" in value or "\r" in value or "\n" in value
            for value in normalized
        ):
            raise ValueError("Adversarial probe path pattern 格式无效。")
        if normalized != tuple(sorted(set(normalized))):
            raise ValueError("Adversarial probe path patterns 必须排序且不得重复。")
        return normalized


class AdversarialProbeRequirement(_StrictModel):
    path: str = Field(min_length=1, max_length=1_024)
    probe_id: str = Field(pattern=_SAFE_CODE_RE)
    kind: AdversarialProbeKind
    evidence_type: Literal["harness_check_receipt"]
    success_rule: Literal["exit_zero"]
    platform_scope: Literal["current", "matrix"]

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
            raise ValueError("Adversarial probe path 必须是安全相对路径。")
        return normalized


class AdversarialProbeCheckBinding(_StrictModel):
    check_id: str = Field(pattern=_SAFE_CODE_RE)
    spec_sha256: str = Field(pattern=_SHA256_RE)
    argv_sha256: str = Field(pattern=_SHA256_RE)
    timeout_seconds: int = Field(ge=1, le=3_600)
    probes: tuple[AdversarialProbeKind, ...] = Field(min_length=1, max_length=6)

    @field_validator("probes")
    @classmethod
    def _ordered_probes(
        cls,
        values: tuple[AdversarialProbeKind, ...],
    ) -> tuple[AdversarialProbeKind, ...]:
        if values != tuple(sorted(set(values))):
            raise ValueError("Adversarial check probes 必须排序且不得重复。")
        return values


class AdversarialProbeCoverage(_StrictModel):
    path: str = Field(min_length=1, max_length=1_024)
    probe_id: str = Field(pattern=_SAFE_CODE_RE)
    kind: AdversarialProbeKind
    check_id: str = Field(pattern=_SAFE_CODE_RE)

    @field_validator("path")
    @classmethod
    def _safe_path(cls, value: str) -> str:
        return AdversarialProbeRequirement._safe_path(value)


class AdversarialProbeBlocker(_StrictModel):
    code: Literal["probe_check_missing", "probe_check_ambiguous"]
    path: str = Field(min_length=1, max_length=1_024)
    probe_id: str = Field(pattern=_SAFE_CODE_RE)
    kind: AdversarialProbeKind
    candidate_check_ids: tuple[str, ...] = Field(max_length=80)

    @field_validator("path")
    @classmethod
    def _safe_path(cls, value: str) -> str:
        return AdversarialProbeRequirement._safe_path(value)

    @field_validator("candidate_check_ids")
    @classmethod
    def _ordered_candidates(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if values != tuple(sorted(set(values))):
            raise ValueError("Adversarial blocker check ids 必须排序且不得重复。")
        return values


class EvolutionAdversarialProbeContract(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-adversarial-probe-contract-v1"] = (
        "evolution-adversarial-probe-contract-v1"
    )
    probe_contract_id: str = Field(pattern=r"^evapc_[0-9a-f]{24}$")
    probe_contract_sha256: str = Field(pattern=_SHA256_RE)
    registry_sha256: str = Field(pattern=_SHA256_RE)
    validation_plan_id: str = Field(pattern=r"^evvplan_[0-9a-f]{24}$")
    validation_plan_sha256: str = Field(pattern=_SHA256_RE)
    profile_binding_id: str = Field(pattern=r"^evvbind_[0-9a-f]{24}$")
    profile_binding_sha256: str = Field(pattern=_SHA256_RE)
    profile_sha256: str = Field(pattern=_SHA256_RE)
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    candidate_files_sha256: str = Field(pattern=_SHA256_RE)
    platform_identity: HarnessEvalPlatformIdentity
    platform_sha256: str = Field(pattern=_SHA256_RE)
    requirements: tuple[AdversarialProbeRequirement, ...] = Field(
        min_length=1,
        max_length=96,
    )
    checks: tuple[AdversarialProbeCheckBinding, ...] = Field(max_length=80)
    coverage: tuple[AdversarialProbeCoverage, ...] = Field(max_length=96)
    blockers: tuple[AdversarialProbeBlocker, ...] = Field(max_length=96)
    coverage_complete: bool
    profile_trust_must_be_revalidated: Literal[True] = True
    har08_batch_required: Literal[True] = True
    runner_binding_status: Literal["required"] = "required"
    execution_ready: Literal[False] = False

    @model_validator(mode="after")
    def _contract_is_complete_and_tamper_evident(self) -> Self:
        requirement_keys = tuple(
            (item.path, item.kind, item.probe_id) for item in self.requirements
        )
        if requirement_keys != tuple(sorted(set(requirement_keys))):
            raise ValueError("Adversarial probe requirements 必须排序且不得重复。")
        check_ids = tuple(item.check_id for item in self.checks)
        if check_ids != tuple(sorted(set(check_ids))):
            raise ValueError("Adversarial probe checks 必须排序且不得重复。")
        coverage_keys = tuple(
            (item.path, item.kind, item.probe_id) for item in self.coverage
        )
        if coverage_keys != tuple(sorted(set(coverage_keys))):
            raise ValueError("Adversarial probe coverage 必须排序且不得重复。")
        blocker_keys = tuple(
            (item.path, item.kind, item.probe_id) for item in self.blockers
        )
        if blocker_keys != tuple(sorted(set(blocker_keys))):
            raise ValueError("Adversarial probe blockers 必须排序且不得重复。")
        if set(coverage_keys) & set(blocker_keys):
            raise ValueError("Adversarial probe requirement 不得同时覆盖和阻断。")
        if set(coverage_keys) | set(blocker_keys) != set(requirement_keys):
            raise ValueError("Adversarial probe requirements 未被完整判定。")
        if self.coverage_complete != (not self.blockers):
            raise ValueError("Adversarial coverage_complete 与 blockers 不一致。")
        by_id = {item.check_id: item for item in self.checks}
        if any(
            item.check_id not in by_id or item.kind not in by_id[item.check_id].probes
            for item in self.coverage
        ):
            raise ValueError("Adversarial probe coverage 与 check capability 不一致。")
        expected_platform = _sha256_payload(self.platform_identity.model_dump(mode="json"))
        if not hmac.compare_digest(self.platform_sha256, expected_platform):
            raise ValueError("Adversarial platform identity 摘要不一致。")
        expected = _sha256_payload(
            self.model_dump(
                mode="json",
                exclude={"probe_contract_id", "probe_contract_sha256"},
            )
        )
        if not hmac.compare_digest(self.probe_contract_sha256, expected):
            raise ValueError("Adversarial Probe Contract 摘要不一致。")
        if self.probe_contract_id != f"evapc_{expected[:24]}":
            raise ValueError("Adversarial Probe Contract identity 不一致。")
        return self


class EvolutionAdversarialProbeContractError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionAdversarialProbeRegistry:
    """Versioned mechanical mapping from changed paths to required probe kinds."""

    def __init__(
        self,
        definitions: tuple[AdversarialProbeDefinition, ...] | None = None,
    ) -> None:
        self.definitions = definitions or _DEFAULT_PROBES
        keys = tuple((item.kind, item.probe_id) for item in self.definitions)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("Adversarial Probe Registry 必须排序且不得重复。")
        kinds = tuple(item.kind for item in self.definitions)
        if kinds != tuple(sorted(set(kinds))):
            raise ValueError("Adversarial Probe Registry 每类只能启用一个版本。")
        if not any(item.kind == "boundary" and item.always_required for item in self.definitions):
            raise ValueError("Adversarial Probe Registry 必须包含 always-required boundary probe。")

    @property
    def sha256(self) -> str:
        return _sha256_payload([
            item.model_dump(mode="json") for item in self.definitions
        ])

    def requirements_for(self, path: str) -> tuple[AdversarialProbeRequirement, ...]:
        normalized = AdversarialProbeRequirement._safe_path(path)
        return tuple(
            AdversarialProbeRequirement(
                path=normalized,
                probe_id=definition.probe_id,
                kind=definition.kind,
                evidence_type=definition.evidence_type,
                success_rule=definition.success_rule,
                platform_scope=definition.platform_scope,
            )
            for definition in self.definitions
            if definition.always_required
            or any(fnmatchcase(normalized.lower(), pattern) for pattern in definition.path_patterns)
        )


class EvolutionAdversarialProbeContractBuilder:
    """Bind required probes to one current, trusted Harness Profile without execution."""

    def __init__(
        self,
        trust_store: HarnessTrustStore,
        *,
        registry: EvolutionAdversarialProbeRegistry | None = None,
    ) -> None:
        if not isinstance(trust_store, HarnessTrustStore):
            raise TypeError("Adversarial Probe Contract Builder 需要 HarnessTrustStore。")
        self._trust_store = trust_store
        self._registry = registry or EvolutionAdversarialProbeRegistry()

    async def build(
        self,
        *,
        validation_plan: EvolutionValidationPlan,
        profile_binding: EvolutionValidationProfileBinding,
        workspace_root: str | Path,
        platform_identity: HarnessEvalPlatformIdentity | None = None,
    ) -> EvolutionAdversarialProbeContract:
        if not isinstance(validation_plan, EvolutionValidationPlan):
            raise TypeError("Adversarial Probe Contract 需要 EvolutionValidationPlan。")
        if not isinstance(profile_binding, EvolutionValidationProfileBinding):
            raise TypeError("Adversarial Probe Contract 需要 Validation Profile Binding。")
        plan = EvolutionValidationPlan.model_validate(
            validation_plan.model_dump(mode="json")
        )
        binding = EvolutionValidationProfileBinding.model_validate(
            profile_binding.model_dump(mode="json")
        )
        if (
            binding.validation_plan_id != plan.validation_plan_id
            or binding.validation_plan_sha256 != plan.validation_plan_sha256
            or binding.profile_sha256 != plan.profile_sha256
        ):
            raise EvolutionAdversarialProbeContractError(
                "probe_authority_mismatch",
                "Validation Plan 与 Profile Binding authority 不一致。",
            )
        workspace = Path(workspace_root).expanduser().resolve()
        snapshot = load_harness_profile(workspace)
        if snapshot.status is not HarnessProfileStatus.VALID or snapshot.profile is None:
            raise EvolutionAdversarialProbeContractError(
                "probe_profile_invalid",
                "当前 Harness Profile 无效，无法构建 Adversarial Probe Contract。",
            )
        if snapshot.digest != binding.profile_sha256:
            raise EvolutionAdversarialProbeContractError(
                "probe_profile_drifted",
                "Harness Profile 已偏离 Validation Profile Binding。",
            )
        relative_profile = snapshot.profile_path.relative_to(workspace).as_posix()
        if binding.profile_path != relative_profile:
            raise EvolutionAdversarialProbeContractError(
                "probe_profile_path_mismatch",
                "Validation Profile Binding 指向的 Profile path 与当前工作区不一致。",
            )
        trust = await self._trust_store.get(workspace)
        if trust is None or trust.profile_digest != snapshot.digest:
            raise EvolutionAdversarialProbeContractError(
                "probe_profile_untrusted",
                "当前 Harness Profile 尚未由用户信任，未绑定 adversarial probes。",
            )

        requirements = tuple(sorted(
            (
                requirement
                for file in plan.files
                for requirement in self._registry.requirements_for(file.path)
            ),
            key=lambda item: (item.path, item.kind, item.probe_id),
        ))
        coverage: list[AdversarialProbeCoverage] = []
        blockers: list[AdversarialProbeBlocker] = []
        used_checks: dict[str, HarnessCheckSpec] = {}
        for requirement in requirements:
            selected_ids = select_required_check_ids(
                snapshot.profile.checks,
                task_kind="change",
                changed_paths=(requirement.path,),
            )
            candidates = tuple(
                check
                for check in snapshot.profile.checks
                if check.id in selected_ids
                and requirement.kind in check.adversarial_probes
            )
            if len(candidates) == 1:
                check = candidates[0]
                used_checks[check.id] = check
                coverage.append(AdversarialProbeCoverage(
                    path=requirement.path,
                    probe_id=requirement.probe_id,
                    kind=requirement.kind,
                    check_id=check.id,
                ))
                continue
            blockers.append(AdversarialProbeBlocker(
                code=(
                    "probe_check_missing"
                    if not candidates
                    else "probe_check_ambiguous"
                ),
                path=requirement.path,
                probe_id=requirement.probe_id,
                kind=requirement.kind,
                candidate_check_ids=tuple(sorted(check.id for check in candidates)),
            ))
        checks = tuple(
            _check_binding(check)
            for check in sorted(used_checks.values(), key=lambda item: item.id)
        )
        if platform_identity is not None and not isinstance(
            platform_identity,
            HarnessEvalPlatformIdentity,
        ):
            raise TypeError("Adversarial Probe Contract 需要 Harness platform identity。")
        runtime_platform = platform_identity or capture_eval_platform_identity()
        platform_sha256 = _sha256_payload(runtime_platform.model_dump(mode="json"))
        payload = {
            "schema_version": 1,
            "policy_version": "evolution-adversarial-probe-contract-v1",
            "registry_sha256": self._registry.sha256,
            "validation_plan_id": plan.validation_plan_id,
            "validation_plan_sha256": plan.validation_plan_sha256,
            "profile_binding_id": binding.binding_id,
            "profile_binding_sha256": binding.binding_sha256,
            "profile_sha256": binding.profile_sha256,
            "candidate_id": plan.candidate_id,
            "candidate_revision": plan.candidate_revision,
            "candidate_files_sha256": plan.candidate_files_sha256,
            "platform_identity": runtime_platform.model_dump(mode="json"),
            "platform_sha256": platform_sha256,
            "requirements": [item.model_dump(mode="json") for item in requirements],
            "checks": [item.model_dump(mode="json") for item in checks],
            "coverage": [
                item.model_dump(mode="json")
                for item in sorted(coverage, key=lambda item: (item.path, item.kind, item.probe_id))
            ],
            "blockers": [
                item.model_dump(mode="json")
                for item in sorted(blockers, key=lambda item: (item.path, item.kind, item.probe_id))
            ],
            "coverage_complete": not blockers,
            "profile_trust_must_be_revalidated": True,
            "har08_batch_required": True,
            "runner_binding_status": "required",
            "execution_ready": False,
        }
        digest = _sha256_payload(payload)
        return EvolutionAdversarialProbeContract.model_validate({
            **payload,
            "probe_contract_id": f"evapc_{digest[:24]}",
            "probe_contract_sha256": digest,
        })


def _definition(
    probe_id: str,
    kind: AdversarialProbeKind,
    patterns: tuple[str, ...],
    *,
    always_required: bool = False,
    platform_scope: Literal["current", "matrix"] = "current",
) -> AdversarialProbeDefinition:
    return AdversarialProbeDefinition(
        probe_id=probe_id,
        kind=kind,
        version=1,
        path_patterns=tuple(sorted(patterns)),
        always_required=always_required,
        platform_scope=platform_scope,
    )


_DEFAULT_PROBES = tuple(sorted((
    _definition("boundary-v1", "boundary", (), always_required=True),
    _definition(
        "concurrency-v1",
        "concurrency",
        (
            "*background*",
            "*browser*",
            "*daemon*",
            "*orchestrator*",
            "*queue*",
            "*runtime*",
            "*scheduler*",
            "*worker*",
        ),
    ),
    _definition(
        "security-v1",
        "security",
        ("*auth*", "*config*", "*permission*", "*safety*", "*secret*", "*security*"),
    ),
    _definition(
        "recovery-v1",
        "recovery",
        (
            "*checkpoint*",
            "*daemon*",
            "*memory*",
            "*persistence*",
            "*recovery*",
            "*runtime*",
            "*store*",
        ),
    ),
    _definition(
        "cross-platform-v1",
        "cross_platform",
        ("*cli*", "*packag*", "*path*", "*platform*", "*shell*", "*terminal*", "*tui*", "*ui*"),
        platform_scope="matrix",
    ),
    _definition(
        "reward-hacking-v1",
        "reward_hacking",
        ("*eval*", "*evolution*", "*harness*", "*metric*", "*policy*", "*reward*"),
    ),
), key=lambda item: (item.kind, item.probe_id)))


def _check_binding(check: HarnessCheckSpec) -> AdversarialProbeCheckBinding:
    return AdversarialProbeCheckBinding(
        check_id=check.id,
        spec_sha256=_sha256_payload(check.model_dump(mode="json")),
        argv_sha256=_sha256_payload(list(check.argv)),
        timeout_seconds=check.timeout_seconds,
        probes=check.adversarial_probes,
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
    "AdversarialProbeBlocker",
    "AdversarialProbeCheckBinding",
    "AdversarialProbeCoverage",
    "AdversarialProbeDefinition",
    "AdversarialProbeKind",
    "AdversarialProbeRequirement",
    "EvolutionAdversarialProbeContract",
    "EvolutionAdversarialProbeContractBuilder",
    "EvolutionAdversarialProbeContractError",
    "EvolutionAdversarialProbeRegistry",
]
