"""Deterministic, non-executable contracts for isolated evolution experiments."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.evolution.proposal import (
    EvolutionProposalPreview,
    ProposalValidationStep,
    workbench_validation_plan,
)
from naumi_agent.evolution.review import EvolutionReviewService
from naumi_agent.workbench.service import WorkbenchService

EXPERIMENT_CONTRACT_POLICY = "evolution-experiment-contract-v1"
EXPERIMENT_SCOPE_POLICY = "evolution-experiment-scope-v1"
EXPERIMENT_BUDGET_POLICY = "evolution-experiment-budget-v1"
_CONTRACT_ID_RE = re.compile(r"^evx_[0-9a-f]{24}$")
_PROPOSAL_ID_RE = re.compile(r"^evp_[0-9a-f]{24}$")
_CANDIDATE_ID_RE = re.compile(r"^evc_[0-9a-f]{24}$")
_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_METRIC_RE = re.compile(r"^[a-z][a-z0-9_.]{0,127}$")
_SAFE_ID_RE = re.compile(r"^[^\x00\r\n]{1,128}$")
_ABSOLUTE_PATH_RE = re.compile(r"^(?:/|[A-Za-z]:[\\/])")
_ALLOWED_TOOLS = ("file_read", "glob", "grep", "file_edit", "file_write")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class ExperimentBudget(_StrictModel):
    policy_version: Literal["evolution-experiment-budget-v1"] = EXPERIMENT_BUDGET_POLICY
    max_changed_files: int = Field(ge=1, le=16)
    max_changed_lines: int = Field(ge=1, le=2_000)
    max_tool_calls: int = Field(ge=1, le=200)
    max_duration_seconds: int = Field(ge=60, le=3_600)
    max_attempts: int = Field(ge=1, le=3)


class ExperimentBaseline(_StrictModel):
    commit: str
    workspace_dirty_at_issue: bool

    @field_validator("commit")
    @classmethod
    def _valid_commit(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _COMMIT_RE.fullmatch(normalized):
            raise ValueError("baseline commit 必须是完整 Git object ID。")
        return normalized


class ExperimentScope(_StrictModel):
    policy_version: Literal["evolution-experiment-scope-v1"] = EXPERIMENT_SCOPE_POLICY
    impact_scope: str = Field(min_length=1, max_length=1_024)
    allowed_files: tuple[str, ...] = Field(min_length=1, max_length=16)

    @field_validator("impact_scope")
    @classmethod
    def _safe_scope(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        parts = re.split(r"[/:]", normalized)
        if (
            not normalized
            or _ABSOLUTE_PATH_RE.match(normalized)
            or ".." in parts
            or any(char in normalized for char in ("\x00", "\r", "\n"))
        ):
            raise ValueError("experiment impact_scope 格式无效。")
        return normalized

    @field_validator("allowed_files")
    @classmethod
    def _safe_files(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip().replace("\\", "/") for value in values)
        if len(set(normalized)) != len(normalized):
            raise ValueError("experiment allowed_files 不得重复。")
        for value in normalized:
            parts = value.split("/")
            if (
                not value
                or len(value) > 1_024
                or _ABSOLUTE_PATH_RE.match(value)
                or ".." in parts
                or any(char in value for char in ("\x00", "\r", "\n"))
            ):
                raise ValueError("experiment allowed_files 必须是安全相对路径。")
        return normalized


class ExperimentCheck(_StrictModel):
    metric_name: str = Field(min_length=1, max_length=128)
    direction: Literal["decrease", "increase"]
    target: float
    verifier: Literal[
        "harness_replay",
        "self_review_static",
        "feedback_recurrence",
    ]
    procedure: str = Field(min_length=1, max_length=1_000)

    @field_validator("metric_name")
    @classmethod
    def _valid_metric(cls, value: str) -> str:
        normalized = value.strip()
        if not _METRIC_RE.fullmatch(normalized):
            raise ValueError("experiment metric_name 格式无效。")
        return normalized

    @field_validator("procedure")
    @classmethod
    def _safe_procedure(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(char in normalized for char in ("\x00", "\r")):
            raise ValueError("experiment procedure 格式无效。")
        return normalized


class ExperimentSource(_StrictModel):
    session_id: str
    mission_id: str
    task_id: str
    workbench_proposal_id: str
    proposal_id: str
    candidate_id: str
    candidate_revision: int = Field(ge=1)
    candidate_sha256: str
    proposal_kind: Literal["knowledge", "profile", "prompt", "tool", "test", "code"]
    generator_version: Literal["evolution-proposal-v1"]
    governance_policy_version: Literal["proposal-governance-v1"]
    reviewer: str
    approved_at: str = Field(min_length=1, max_length=100)

    @field_validator(
        "session_id",
        "mission_id",
        "task_id",
        "workbench_proposal_id",
        "reviewer",
    )
    @classmethod
    def _safe_binding(cls, value: str) -> str:
        normalized = value.strip()
        if not _SAFE_ID_RE.fullmatch(normalized):
            raise ValueError("experiment source binding 格式无效。")
        return normalized

    @field_validator("candidate_sha256")
    @classmethod
    def _valid_digest(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise ValueError("candidate_sha256 必须是 SHA-256。")
        return normalized

    @field_validator("proposal_id")
    @classmethod
    def _valid_proposal_id(cls, value: str) -> str:
        if not _PROPOSAL_ID_RE.fullmatch(value):
            raise ValueError("experiment proposal_id 格式无效。")
        return value

    @field_validator("candidate_id")
    @classmethod
    def _valid_candidate_id(cls, value: str) -> str:
        if not _CANDIDATE_ID_RE.fullmatch(value):
            raise ValueError("experiment candidate_id 格式无效。")
        return value

    @field_validator("approved_at")
    @classmethod
    def _valid_approved_at(cls, value: str) -> str:
        normalized = value.strip()
        try:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("approved_at 必须是 ISO-8601 时间。") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("approved_at 必须包含时区。")
        return normalized


class EvolutionExperimentContract(_StrictModel):
    schema_version: Literal[1] = 1
    contract_id: str
    manifest_sha256: str
    policy_version: Literal["evolution-experiment-contract-v1"] = (
        EXPERIMENT_CONTRACT_POLICY
    )
    source: ExperimentSource
    baseline: ExperimentBaseline
    scope: ExperimentScope
    budget: ExperimentBudget
    allowed_tools: tuple[
        Literal["file_read", "glob", "grep", "file_edit", "file_write"], ...
    ] = _ALLOWED_TOOLS
    allowed_checks: tuple[ExperimentCheck, ...] = Field(min_length=1, max_length=8)
    seed: int = Field(ge=0, le=9_223_372_036_854_775_807)
    network_access: Literal[False] = False
    dependency_installation: Literal[False] = False
    requires_worktree_lease: Literal[True] = True
    requires_source_snapshot: Literal[True] = True
    requires_static_guard: Literal[True] = True
    execution_ready: Literal[False] = False
    state: Literal["contract"] = "contract"

    @model_validator(mode="after")
    def _contract_is_deterministic_and_bounded(self) -> EvolutionExperimentContract:
        if tuple(self.allowed_tools) != _ALLOWED_TOOLS:
            raise ValueError("allowed_tools 必须使用 contract v1 固定最小集合。")
        if self.budget.max_changed_files < len(self.scope.allowed_files):
            raise ValueError("max_changed_files 小于已批准文件数量。")
        if len({check.metric_name for check in self.allowed_checks}) != len(
            self.allowed_checks
        ):
            raise ValueError("allowed_checks metric 不得重复。")
        if not _CONTRACT_ID_RE.fullmatch(self.contract_id):
            raise ValueError("contract_id 格式无效。")
        payload = self.model_dump(exclude={"contract_id", "manifest_sha256"})
        digest = _manifest_digest(payload)
        if self.manifest_sha256 != digest:
            raise ValueError("manifest_sha256 与 Experiment manifest 不一致。")
        if self.contract_id != _contract_id(digest):
            raise ValueError("contract_id 与 Experiment manifest 不一致。")
        return self

    @field_validator("manifest_sha256")
    @classmethod
    def _valid_manifest_digest(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise ValueError("manifest_sha256 必须是 SHA-256。")
        return normalized


class ExperimentBaselineReader(Protocol):
    def read(self, workspace_root: str | Path) -> ExperimentBaseline: ...


class GitExperimentBaselineReader:
    """Read one bounded, non-mutating baseline from the exact repository root."""

    def __init__(
        self,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._runner = runner

    def read(self, workspace_root: str | Path) -> ExperimentBaseline:
        root = Path(workspace_root).expanduser().resolve()
        if not root.is_dir():
            raise ValueError("Experiment workspace 不存在或不是目录。")
        env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
        top = self._git(root, "rev-parse", "--show-toplevel", env=env).strip()
        if Path(top).expanduser().resolve() != root:
            raise ValueError("Experiment workspace 必须是精确 Git 仓库根目录。")
        commit = self._git(root, "rev-parse", "HEAD", env=env).strip().lower()
        dirty = bool(
            self._git(
                root,
                "status",
                "--porcelain=v1",
                "--untracked-files=normal",
                env=env,
            )
        )
        return ExperimentBaseline(commit=commit, workspace_dirty_at_issue=dirty)

    def _git(self, root: Path, *args: str, env: dict[str, str]) -> str:
        try:
            completed = self._runner(
                ["git", "-C", str(root), *args],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
                env=env,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("无法读取 Experiment Git baseline。") from exc
        if completed.returncode != 0:
            raise ValueError("Experiment workspace 不是可用的 Git 仓库。")
        if len(completed.stdout) > 1_000_000:
            raise RuntimeError("Experiment Git baseline 输出超过安全上限。")
        return completed.stdout


class EvolutionExperimentContractIssuer:
    """Issue a non-executable contract from one current, approved Proposal."""

    def __init__(
        self,
        *,
        review_service: EvolutionReviewService,
        workbench_service: WorkbenchService,
        baseline_reader: ExperimentBaselineReader | None = None,
    ) -> None:
        self._review_service = review_service
        self._workbench_service = workbench_service
        self._baseline_reader = baseline_reader or GitExperimentBaselineReader()

    async def issue(
        self,
        workspace_root: str | Path,
        *,
        session_id: str,
        proposal_id: str,
        seed: int,
        budget: ExperimentBudget | None = None,
    ) -> EvolutionExperimentContract:
        clean_session = _safe_id(session_id, "session")
        clean_proposal = _safe_id(proposal_id, "Proposal")
        proposal = await self._workbench_service.get_proposal(
            clean_session,
            clean_proposal,
        )
        if proposal is None:
            raise ValueError("Workbench Proposal 不存在。")
        _require_approved_evolution_proposal(proposal)
        snapshot = await self._review_service.detail_snapshot(
            workspace_root,
            str(proposal["source_id"]),
        )
        preview = snapshot.selected.proposal if snapshot.selected is not None else None
        if preview is None:
            raise ValueError("Proposal 对应的 Candidate Preview 当前不可验证。")
        _verify_proposal_binding(proposal, preview)
        baseline = await asyncio.to_thread(self._baseline_reader.read, workspace_root)
        effective_budget = budget or default_experiment_budget(
            str(preview.risk_level),
            file_count=len(preview.intended_files),
        )
        _validate_budget_cap(
            effective_budget,
            risk_level=str(preview.risk_level),
            file_count=len(preview.intended_files),
        )
        payload: dict[str, Any] = {
            "schema_version": 1,
            "policy_version": EXPERIMENT_CONTRACT_POLICY,
            "source": ExperimentSource(
                session_id=clean_session,
                mission_id=str(proposal["mission_id"]),
                task_id=str(proposal["task_id"]),
                workbench_proposal_id=clean_proposal,
                proposal_id=preview.proposal_id,
                candidate_id=preview.source.candidate_id,
                candidate_revision=preview.source.candidate_revision,
                candidate_sha256=preview.source.candidate_sha256,
                proposal_kind=preview.proposal_kind,
                generator_version=preview.generator_version,
                governance_policy_version=str(proposal["governance_policy_version"]),
                reviewer=str(proposal["reviewer"]),
                approved_at=str(proposal["decision_at"]),
            ),
            "baseline": baseline,
            "scope": ExperimentScope(
                impact_scope=preview.impact_scope,
                allowed_files=preview.intended_files,
            ),
            "budget": effective_budget,
            "allowed_tools": _ALLOWED_TOOLS,
            "allowed_checks": tuple(_experiment_check(step) for step in preview.validation_plan),
            "seed": seed,
            "network_access": False,
            "dependency_installation": False,
            "requires_worktree_lease": True,
            "requires_source_snapshot": True,
            "requires_static_guard": True,
            "execution_ready": False,
            "state": "contract",
        }
        plain = _jsonable(payload)
        manifest_sha256 = _manifest_digest(plain)
        return EvolutionExperimentContract(
            contract_id=_contract_id(manifest_sha256),
            manifest_sha256=manifest_sha256,
            **plain,
        )


_BUDGET_CAPS: dict[str, ExperimentBudget] = {
    "low": ExperimentBudget(
        max_changed_files=8,
        max_changed_lines=800,
        max_tool_calls=80,
        max_duration_seconds=1_800,
        max_attempts=3,
    ),
    "medium": ExperimentBudget(
        max_changed_files=6,
        max_changed_lines=500,
        max_tool_calls=60,
        max_duration_seconds=1_200,
        max_attempts=2,
    ),
    "high": ExperimentBudget(
        max_changed_files=4,
        max_changed_lines=300,
        max_tool_calls=40,
        max_duration_seconds=900,
        max_attempts=2,
    ),
    "critical": ExperimentBudget(
        max_changed_files=2,
        max_changed_lines=150,
        max_tool_calls=25,
        max_duration_seconds=600,
        max_attempts=1,
    ),
}


def default_experiment_budget(risk_level: str, *, file_count: int) -> ExperimentBudget:
    cap = _BUDGET_CAPS.get(str(risk_level).strip().lower())
    if cap is None:
        raise ValueError("Experiment risk level 未知。")
    if file_count < 1 or file_count > cap.max_changed_files:
        raise ValueError("Proposal 文件数量超过该风险等级的 Experiment 上限。")
    return cap


def _validate_budget_cap(
    budget: ExperimentBudget,
    *,
    risk_level: str,
    file_count: int,
) -> None:
    if not isinstance(budget, ExperimentBudget):
        raise TypeError("budget 必须是 ExperimentBudget。")
    cap = default_experiment_budget(risk_level, file_count=file_count)
    for field in (
        "max_changed_files",
        "max_changed_lines",
        "max_tool_calls",
        "max_duration_seconds",
        "max_attempts",
    ):
        if getattr(budget, field) > getattr(cap, field):
            raise ValueError(f"Experiment budget {field} 超过风险策略上限。")
    if budget.max_changed_files < file_count:
        raise ValueError("Experiment budget 无法覆盖已批准文件。")


def _require_approved_evolution_proposal(proposal: Mapping[str, Any]) -> None:
    if str(proposal.get("state") or "") != "approved":
        raise ValueError("只有 approved Proposal 可以签发 Experiment Contract。")
    if str(proposal.get("source_kind") or "") != "evolution_candidate":
        raise ValueError("只有可信 Evolution Candidate Proposal 可以签发实验契约。")
    required = (
        "mission_id",
        "task_id",
        "source_id",
        "source_sha256",
        "source_proposal_id",
        "generator_version",
        "proposal_kind",
        "reviewer",
        "decision_at",
        "governance_policy_version",
    )
    if any(not str(proposal.get(field) or "").strip() for field in required):
        raise ValueError("approved Proposal 缺少可信治理或来源字段。")


def _verify_proposal_binding(
    proposal: Mapping[str, Any],
    preview: EvolutionProposalPreview,
) -> None:
    expected = {
        "source_id": preview.source.candidate_id,
        "source_revision": preview.source.candidate_revision,
        "source_sha256": preview.source.candidate_sha256,
        "source_proposal_id": preview.proposal_id,
        "generator_version": preview.generator_version,
        "proposal_kind": preview.proposal_kind,
        "impact_scope": preview.impact_scope,
        "intended_files": list(preview.intended_files),
        "validation_plan": list(workbench_validation_plan(preview)),
        "risk_level": preview.risk_level,
    }
    for field, value in expected.items():
        if proposal.get(field) != value:
            raise ValueError(f"approved Proposal 的 {field} 与当前可信 Preview 不一致。")


def _experiment_check(step: ProposalValidationStep) -> ExperimentCheck:
    return ExperimentCheck(
        metric_name=step.metric_name,
        direction=step.direction,
        target=step.target,
        verifier=step.verifier,
        procedure=step.procedure,
    )


def _safe_id(value: str, label: str) -> str:
    normalized = str(value).strip()
    if not _SAFE_ID_RE.fullmatch(normalized):
        raise ValueError(f"{label} ID 必须为 1..128 个无控制字符的文本。")
    return normalized


def _jsonable(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, default=_json_default))


def _json_default(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    raise TypeError(f"无法序列化 Experiment Contract 字段: {type(value).__name__}")


def _manifest_digest(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _contract_id(manifest_sha256: str) -> str:
    return f"evx_{manifest_sha256[:24]}"


__all__ = [
    "EvolutionExperimentContract",
    "EvolutionExperimentContractIssuer",
    "ExperimentBaseline",
    "ExperimentBudget",
    "ExperimentCheck",
    "ExperimentScope",
    "ExperimentSource",
    "GitExperimentBaselineReader",
    "default_experiment_budget",
]
