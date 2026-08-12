"""Fail-closed Capability Proposals derived from ranked Evolution opportunities."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.eligibility import CandidateEligibilityAssessment
from naumi_agent.evolution.prioritization import CandidatePriority, OpportunityPortfolio
from naumi_agent.evolution.store import EvolutionStoredCandidate

_GENERATOR_VERSION = "evolution-capability-proposal-v1"
_PROPOSAL_ID_RE = re.compile(r"^evcp_[0-9a-f]{24}$")
_CANDIDATE_ID_RE = re.compile(r"^evc_[0-9a-f]{24}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TOOL_SCOPE_RE = re.compile(r"^capability:tool:([a-z][a-z0-9_.:-]{0,127})$")
_GOAL_SCOPE_RE = re.compile(r"^capability:need:([0-9a-f]{16})$")
_PERMISSION_FAMILIES = (
    "workspace_read",
    "workspace_write",
    "process",
    "network",
    "browser",
    "secrets",
)
_PROHIBITED_CONTENT = (
    "secret_values",
    "raw_user_conversation",
    "absolute_workspace_paths",
    "unbounded_stdout",
)
_REQUIRED_CHECKS = (
    "schema_contract",
    "permission_denial_paths",
    "empty_and_extreme_inputs",
    "concurrent_idempotency",
    "real_scenario_e2e",
    "slash_agent_parity",
)
_RETIREMENT_CRITERIA = (
    "authority_revoked",
    "catalog_need_satisfied",
    "slo_breach",
    "security_regression",
    "superseded_capability",
    "low_verified_value",
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class CapabilityProposalSource(_StrictModel):
    candidate_id: str
    candidate_revision: int = Field(ge=1)
    candidate_sha256: str
    source_kinds: tuple[str, ...] = Field(min_length=1, max_length=16)
    priority_policy: Literal["evolution-priority-v1"]
    portfolio_anchor_at: str = Field(min_length=20, max_length=64)
    priority_rank: int = Field(ge=1, le=500)
    priority_score_basis_points: int = Field(ge=1, le=10_000)
    authority_valid: Literal[True] = True
    governance_valid: Literal[True] = True

    @model_validator(mode="after")
    def _identity_is_valid(self) -> CapabilityProposalSource:
        if not _CANDIDATE_ID_RE.fullmatch(self.candidate_id):
            raise ValueError("Capability Proposal candidate_id 格式无效。")
        if not _SHA256_RE.fullmatch(self.candidate_sha256):
            raise ValueError("Capability Proposal Candidate 摘要无效。")
        return self


class CapabilityInterfaceContract(_StrictModel):
    invocation_kind: Literal["tool"] = "tool"
    requested_name: str | None = Field(default=None, max_length=128)
    name_status: Literal["known", "unresolved"]
    parameters_schema_status: Literal["unresolved"] = "unresolved"
    result_schema_status: Literal["unresolved"] = "unresolved"
    error_contract_status: Literal["unresolved"] = "unresolved"
    versioning_status: Literal["unresolved"] = "unresolved"

    @model_validator(mode="after")
    def _name_matches_status(self) -> CapabilityInterfaceContract:
        if self.name_status == "known":
            if self.requested_name is None or not re.fullmatch(
                r"[a-z][a-z0-9_.:-]{0,127}", self.requested_name
            ):
                raise ValueError("已知 Tool 名必须是安全的精确标识符。")
        elif self.requested_name is not None:
            raise ValueError("未解析的 Tool 名不得携带猜测值。")
        return self


class CapabilityPermissionContract(_StrictModel):
    policy_status: Literal["unresolved"] = "unresolved"
    candidate_families: tuple[
        Literal["workspace_read", "workspace_write", "process", "network", "browser", "secrets"],
        ...,
    ] = _PERMISSION_FAMILIES
    granted_families: tuple[str, ...] = ()
    bypass_grants_registration: Literal[False] = False
    bypass_grants_execution: Literal[False] = False

    @model_validator(mode="after")
    def _permission_catalog_is_complete(self) -> CapabilityPermissionContract:
        if self.candidate_families != _PERMISSION_FAMILIES or self.granted_families:
            raise ValueError("Capability Proposal 权限目录必须完整且不得预授予权限。")
        return self


class CapabilityDataContract(_StrictModel):
    input_classes_status: Literal["unresolved"] = "unresolved"
    output_classes_status: Literal["unresolved"] = "unresolved"
    retention_status: Literal["unresolved"] = "unresolved"
    prohibited_content: tuple[str, ...] = _PROHIBITED_CONTENT

    @model_validator(mode="after")
    def _privacy_floor_is_fixed(self) -> CapabilityDataContract:
        if self.prohibited_content != _PROHIBITED_CONTENT:
            raise ValueError("Capability Proposal 隐私禁区不得删改。")
        return self


class CapabilityVerificationContract(_StrictModel):
    source_metrics: tuple[str, ...] = Field(min_length=1, max_length=8)
    required_checks: tuple[str, ...] = _REQUIRED_CHECKS
    real_scenario_status: Literal["unresolved"] = "unresolved"

    @model_validator(mode="after")
    def _verification_floor_is_fixed(self) -> CapabilityVerificationContract:
        if self.required_checks != _REQUIRED_CHECKS or len(set(self.source_metrics)) != len(
            self.source_metrics
        ):
            raise ValueError("Capability Proposal 验证下限不完整或指标重复。")
        return self


class CapabilityOperationsContract(_StrictModel):
    owner_status: Literal["unassigned"] = "unassigned"
    slo_status: Literal["unresolved"] = "unresolved"
    maintenance_status: Literal["unresolved"] = "unresolved"
    retirement_criteria: tuple[str, ...] = _RETIREMENT_CRITERIA

    @model_validator(mode="after")
    def _retirement_floor_is_fixed(self) -> CapabilityOperationsContract:
        if self.retirement_criteria != _RETIREMENT_CRITERIA:
            raise ValueError("Capability Proposal 退休条件不得删改。")
        return self


class CapabilityLifecycleContract(_StrictModel):
    state: Literal["proposal"] = "proposal"
    next_stage: Literal["sandbox_registration"] = "sandbox_registration"
    sandbox_eligible: Literal[False] = False
    shadow_eligible: Literal[False] = False
    limited_activation_eligible: Literal[False] = False
    executable: Literal[False] = False
    registry_mutation_allowed: Literal[False] = False
    builtin_override_allowed: Literal[False] = False


class EvolutionCapabilityProposal(_StrictModel):
    schema_version: Literal[1] = 1
    proposal_id: str
    generator_version: Literal["evolution-capability-proposal-v1"] = _GENERATOR_VERSION
    status: Literal["needs_specification"] = "needs_specification"
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=1_000)
    impact_scope: str = Field(min_length=1, max_length=1_024)
    source: CapabilityProposalSource
    interface: CapabilityInterfaceContract
    permissions: CapabilityPermissionContract
    data: CapabilityDataContract
    verification: CapabilityVerificationContract
    operations: CapabilityOperationsContract
    lifecycle: CapabilityLifecycleContract
    unresolved_requirements: tuple[str, ...] = Field(min_length=1, max_length=32)
    requires_human_review: Literal[True] = True

    @model_validator(mode="after")
    def _identity_and_gates_are_consistent(self) -> EvolutionCapabilityProposal:
        if not _PROPOSAL_ID_RE.fullmatch(self.proposal_id):
            raise ValueError("Capability Proposal ID 格式无效。")
        if self.proposal_id != _proposal_id(
            source=self.source,
            interface=self.interface,
            unresolved_requirements=self.unresolved_requirements,
        ):
            raise ValueError("Capability Proposal ID 与来源快照不一致。")
        if len(set(self.unresolved_requirements)) != len(self.unresolved_requirements):
            raise ValueError("Capability Proposal 未决项不得重复。")
        expected_unresolved = _unresolved_requirements(
            tool_name_known=self.interface.name_status == "known"
        )
        if self.unresolved_requirements != expected_unresolved:
            raise ValueError("Capability Proposal 未决项必须完整且顺序稳定。")
        tool_match = _TOOL_SCOPE_RE.fullmatch(self.impact_scope)
        goal_match = _GOAL_SCOPE_RE.fullmatch(self.impact_scope)
        if self.interface.name_status == "known":
            if (
                tool_match is None
                or tool_match.group(1) != self.interface.requested_name
                or "tool_catalog_miss" not in self.source.source_kinds
            ):
                raise ValueError("Capability Proposal Tool scope 与来源不一致。")
        elif goal_match is None or "goal_need" not in self.source.source_kinds:
            raise ValueError("Capability Proposal Goal scope 与来源不一致。")
        return self


def generate_capability_proposal(
    stored: EvolutionStoredCandidate,
    *,
    eligibility: CandidateEligibilityAssessment,
    priority: CandidatePriority | None,
    portfolio: OpportunityPortfolio,
) -> EvolutionCapabilityProposal | None:
    """Build a non-executable capability contract from one current ranked source."""
    if not isinstance(stored, EvolutionStoredCandidate):
        raise TypeError("Capability Proposal 只能处理 EvolutionStoredCandidate。")
    if not isinstance(eligibility, CandidateEligibilityAssessment):
        raise TypeError("Capability Proposal 必须使用当前 Eligibility 结论。")
    if not isinstance(portfolio, OpportunityPortfolio):
        raise TypeError("Capability Proposal 必须绑定当前 Opportunity Portfolio。")
    candidate = stored.draft
    if stored.draft_sha256 != _candidate_sha256(candidate.model_dump(mode="json")):
        raise ValueError("Capability Proposal Candidate 摘要不可信。")
    if candidate.kind != "capability" or not eligibility.review_ready:
        return None
    if (
        priority is None
        or priority.candidate_id != candidate.candidate_id
        or not priority.rankable
        or priority.rank is None
        or priority.domain != "capability"
        or priority.policy_version != portfolio.policy_version
        or not portfolio.anchor_at
    ):
        return None
    checks = {check.code: check.passed for check in eligibility.checks}
    if not checks.get("source_authority") or not checks.get("cooldown_gate"):
        return None

    requested_name: str | None = None
    name_status: Literal["known", "unresolved"] = "unresolved"
    match = _TOOL_SCOPE_RE.fullmatch(candidate.scope)
    if match is not None and "tool_catalog_miss" in candidate.source_kinds:
        requested_name = match.group(1)
        name_status = "known"
    elif _GOAL_SCOPE_RE.fullmatch(candidate.scope) is None:
        return None

    interface = CapabilityInterfaceContract(
        requested_name=requested_name,
        name_status=name_status,
    )
    unresolved = _unresolved_requirements(tool_name_known=requested_name is not None)
    source = CapabilityProposalSource(
        candidate_id=candidate.candidate_id,
        candidate_revision=stored.revision,
        candidate_sha256=stored.draft_sha256,
        source_kinds=candidate.source_kinds,
        priority_policy=priority.policy_version,
        portfolio_anchor_at=portfolio.anchor_at,
        priority_rank=priority.rank,
        priority_score_basis_points=priority.score_basis_points,
    )
    metrics = tuple(
        f"{metric.name}:{metric.direction}:{metric.target:g}:{metric.verifier}"
        for metric in candidate.expected_metrics
    )
    proposal_id = _proposal_id(
        source=source,
        interface=interface,
        unresolved_requirements=unresolved,
    )
    label = requested_name or candidate.candidate_id
    return EvolutionCapabilityProposal(
        proposal_id=proposal_id,
        title=f"能力提案：{label}",
        summary=(
            "已从当前有效且进入全局排序的能力机会建立治理契约；"
            "未知 API、权限、数据和运维字段保持显式未决，禁止进入 Sandbox。"
        ),
        impact_scope=candidate.scope,
        source=source,
        interface=interface,
        permissions=CapabilityPermissionContract(),
        data=CapabilityDataContract(),
        verification=CapabilityVerificationContract(source_metrics=metrics),
        operations=CapabilityOperationsContract(),
        lifecycle=CapabilityLifecycleContract(),
        unresolved_requirements=unresolved,
    )


def _unresolved_requirements(*, tool_name_known: bool) -> tuple[str, ...]:
    common = (
        "api.parameters_schema",
        "api.result_schema",
        "api.error_contract",
        "api.versioning",
        "permissions.required_families",
        "data.input_output_retention",
        "verification.real_scenario",
        "operations.owner",
        "operations.slo",
        "operations.maintenance",
    )
    return common if tool_name_known else ("api.tool_name", *common)


def _candidate_sha256(value: dict[str, object]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _proposal_id(
    *,
    source: CapabilityProposalSource,
    interface: CapabilityInterfaceContract,
    unresolved_requirements: tuple[str, ...],
) -> str:
    payload = json.dumps(
        {
            "generator_version": _GENERATOR_VERSION,
            "source": source.model_dump(mode="json"),
            "interface": interface.model_dump(mode="json"),
            "unresolved_requirements": unresolved_requirements,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"evcp_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]}"


__all__ = [
    "CapabilityDataContract",
    "CapabilityInterfaceContract",
    "CapabilityLifecycleContract",
    "CapabilityOperationsContract",
    "CapabilityPermissionContract",
    "CapabilityProposalSource",
    "CapabilityVerificationContract",
    "EvolutionCapabilityProposal",
    "generate_capability_proposal",
]
