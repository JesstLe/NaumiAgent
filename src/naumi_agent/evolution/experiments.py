"""Deterministic, non-executable contracts for isolated evolution experiments."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.evolution.proposal import (
    EvolutionProposalPreview,
    ProposalValidationStep,
    parse_proposal_scope_files,
    workbench_validation_plan,
)
from naumi_agent.evolution.review import EvolutionReviewService
from naumi_agent.workbench.service import WorkbenchService

EXPERIMENT_CONTRACT_POLICY = "evolution-experiment-contract-v1"
EXPERIMENT_CONTRACT_AUTHORITY_POLICY = "evolution-experiment-contract-authority-v1"
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
_MAX_CONTRACT_AUTHORITY_BYTES = 512 * 1_024


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

    @model_validator(mode="after")
    def _explicit_multi_file_scope_matches_authority(self) -> ExperimentScope:
        if self.impact_scope.casefold().startswith("files:"):
            parsed = parse_proposal_scope_files(self.impact_scope)
            if parsed != self.allowed_files:
                raise ValueError("multi-file impact_scope 与 allowed_files 不一致。")
        return self


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


class EvolutionExperimentContractAuthority(_StrictModel):
    """Workspace-bound durable authority around an immutable Contract v1."""

    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-experiment-contract-authority-v1"] = (
        EXPERIMENT_CONTRACT_AUTHORITY_POLICY
    )
    authority_id: str = Field(pattern=r"^evxauth_[0-9a-f]{24}$")
    authority_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_root: str = Field(min_length=1, max_length=4_096)
    contract_id: str = Field(pattern=r"^evx_[0-9a-f]{24}$")
    contract_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    approved_at: str = Field(min_length=1, max_length=100)
    contract: EvolutionExperimentContract

    @field_validator("workspace_root")
    @classmethod
    def _canonical_workspace(cls, value: str) -> str:
        return _canonical_workspace_root(value)

    @model_validator(mode="after")
    def _authority_is_exact_and_tamper_evident(
        self,
    ) -> EvolutionExperimentContractAuthority:
        contract = self.contract
        if not (
            self.contract_id == contract.contract_id
            and self.contract_manifest_sha256 == contract.manifest_sha256
            and self.candidate_id == contract.source.candidate_id
            and self.candidate_revision == contract.source.candidate_revision
            and self.approved_at == contract.source.approved_at
        ):
            raise ValueError("Experiment Contract Authority 投影不一致。")
        digest = _manifest_digest(
            self.model_dump(
                mode="json",
                exclude={"authority_id", "authority_sha256"},
            )
        )
        if not hmac.compare_digest(self.authority_sha256, digest):
            raise ValueError("Experiment Contract Authority 摘要不一致。")
        if self.authority_id != f"evxauth_{digest[:24]}":
            raise ValueError("Experiment Contract Authority identity 不一致。")
        return self


class EvolutionExperimentContractStoreError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionExperimentContractStore:
    """Immutable workspace-scoped storage for issued Experiment Contracts."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()

    @property
    def db_path(self) -> Path:
        return self._db_path

    async def record(
        self,
        *,
        workspace_root: str | Path,
        contract: EvolutionExperimentContract,
    ) -> EvolutionExperimentContractAuthority:
        authority = build_experiment_contract_authority(
            workspace_root=workspace_root,
            contract=contract,
        )
        encoded = authority.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_CONTRACT_AUTHORITY_BYTES:
            raise EvolutionExperimentContractStoreError(
                "experiment_contract_authority_oversized",
                "Experiment Contract Authority 超过 512 KiB 上限。",
            )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_contract_store_schema(db)
                await db.commit()
                await db.execute("BEGIN IMMEDIATE")
                proposal_row = await (
                    await db.execute(
                        "SELECT * FROM evolution_experiment_contracts "
                        "WHERE workspace_root = ? AND source_session_id = ? "
                        "AND workbench_proposal_id = ?",
                        (
                            authority.workspace_root,
                            authority.contract.source.session_id,
                            authority.contract.source.workbench_proposal_id,
                        ),
                    )
                ).fetchone()
                if proposal_row is not None:
                    restored = _contract_authority_from_row(proposal_row)
                    if restored != authority:
                        await db.rollback()
                        raise EvolutionExperimentContractStoreError(
                            "experiment_contract_proposal_conflict",
                            "同一 approved Proposal 只能签发一个 Experiment Contract。",
                        )
                    await db.rollback()
                    return restored
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_experiment_contracts "
                        "WHERE workspace_root = ? AND contract_id = ?",
                        (authority.workspace_root, authority.contract_id),
                    )
                ).fetchone()
                if row is not None:
                    restored = _contract_authority_from_row(row)
                    if restored != authority:
                        await db.rollback()
                        raise EvolutionExperimentContractStoreError(
                            "experiment_contract_authority_conflict",
                            "同一工作区和 Contract ID 不可覆盖为不同 authority。",
                        )
                    await db.rollback()
                    return restored
                await db.execute(
                    "INSERT INTO evolution_experiment_contracts "
                    "(workspace_root, contract_id, manifest_sha256, authority_id, "
                    "authority_sha256, authority_json, approved_at, source_session_id, "
                    "workbench_proposal_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        authority.workspace_root,
                        authority.contract_id,
                        authority.contract_manifest_sha256,
                        authority.authority_id,
                        authority.authority_sha256,
                        encoded,
                        authority.approved_at,
                        authority.contract.source.session_id,
                        authority.contract.source.workbench_proposal_id,
                    ),
                )
                await db.commit()
        except EvolutionExperimentContractStoreError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionExperimentContractStoreError(
                "experiment_contract_authority_store_error",
                "Experiment Contract Authority 无法持久化。",
            ) from exc
        restored = await self.get(workspace_root, authority.contract_id)
        assert restored is not None
        return restored

    async def get(
        self,
        workspace_root: str | Path,
        contract_id: str,
    ) -> EvolutionExperimentContractAuthority | None:
        workspace = _canonical_workspace_root(workspace_root)
        if not isinstance(contract_id, str) or _CONTRACT_ID_RE.fullmatch(contract_id) is None:
            raise ValueError("contract_id 格式无效。")
        if not self._db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_contract_store_schema(db)
                await db.commit()
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_experiment_contracts "
                        "WHERE workspace_root = ? AND contract_id = ?",
                        (workspace, contract_id),
                    )
                ).fetchone()
                return _contract_authority_from_row(row) if row is not None else None
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionExperimentContractStoreError(
                "experiment_contract_authority_store_corrupt",
                "Experiment Contract Authority 损坏或无法读取。",
            ) from exc

    async def get_by_proposal(
        self,
        workspace_root: str | Path,
        *,
        session_id: str,
        proposal_id: str,
    ) -> EvolutionExperimentContractAuthority | None:
        workspace = _canonical_workspace_root(workspace_root)
        clean_session = _safe_id(session_id, "session")
        clean_proposal = _safe_id(proposal_id, "Proposal")
        if not self._db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_contract_store_schema(db)
                await db.commit()
                rows = await (
                    await db.execute(
                        "SELECT * FROM evolution_experiment_contracts "
                        "WHERE workspace_root = ? AND source_session_id = ? "
                        "AND workbench_proposal_id = ? LIMIT 2",
                        (workspace, clean_session, clean_proposal),
                    )
                ).fetchall()
                if len(rows) > 1:
                    raise ValueError("approved Proposal 映射到多个 Experiment Contract。")
                return _contract_authority_from_row(rows[0]) if rows else None
        except EvolutionExperimentContractStoreError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionExperimentContractStoreError(
                "experiment_contract_authority_store_corrupt",
                "Experiment Contract Proposal authority 损坏或无法读取。",
            ) from exc


def build_experiment_contract_authority(
    *,
    workspace_root: str | Path,
    contract: EvolutionExperimentContract,
) -> EvolutionExperimentContractAuthority:
    try:
        artifact = EvolutionExperimentContract.model_validate(
            contract.model_dump(mode="json")
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionExperimentContractStoreError(
            "experiment_contract_authority_invalid",
            "Experiment Contract 无效或已被篡改。",
        ) from exc
    payload = {
        "schema_version": 1,
        "policy_version": EXPERIMENT_CONTRACT_AUTHORITY_POLICY,
        "workspace_root": _canonical_workspace_root(workspace_root),
        "contract_id": artifact.contract_id,
        "contract_manifest_sha256": artifact.manifest_sha256,
        "candidate_id": artifact.source.candidate_id,
        "candidate_revision": artifact.source.candidate_revision,
        "approved_at": artifact.source.approved_at,
        "contract": artifact.model_dump(mode="json"),
    }
    digest = _manifest_digest(payload)
    return EvolutionExperimentContractAuthority.model_validate({
        **payload,
        "authority_id": f"evxauth_{digest[:24]}",
        "authority_sha256": digest,
    })


def render_experiment_contract_authority(
    authority: EvolutionExperimentContractAuthority,
) -> str:
    artifact = EvolutionExperimentContractAuthority.model_validate(
        authority.model_dump(mode="json")
    )
    contract = artifact.contract
    checks = ", ".join(f"`{item.metric_name}`" for item in contract.allowed_checks)
    files = ", ".join(f"`{item}`" for item in contract.scope.allowed_files)
    return "\n".join((
        f"# Experiment Contract Authority `{artifact.authority_id}`",
        "",
        "**这是已批准实验约束的不可执行持久 authority，不是执行或推广许可。**",
        "",
        f"- Contract：`{artifact.contract_id}`",
        f"- Candidate：`{artifact.candidate_id}` · revision {artifact.candidate_revision}",
        f"- Reviewer：`{contract.source.reviewer}` · {artifact.approved_at}",
        f"- Scope：{contract.scope.impact_scope}",
        f"- Files：{files}",
        (
            "- Budget："
            f"files {contract.budget.max_changed_files} · "
            f"lines {contract.budget.max_changed_lines} · "
            f"tools {contract.budget.max_tool_calls} · "
            f"seconds {contract.budget.max_duration_seconds} · "
            f"attempts {contract.budget.max_attempts}"
        ),
        f"- Checks：{checks}",
        "- Network / dependency install：禁止 / 禁止",
        f"- Authority SHA-256：`{artifact.authority_sha256}`",
    ))


class ExperimentBaselineReader(Protocol):
    def read(self, workspace_root: str | Path) -> ExperimentBaseline: ...


class ExperimentProposalOutcomeReader(Protocol):
    async def project_session(self, session_id: str) -> Mapping[str, Any]: ...


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
        store: EvolutionExperimentContractStore,
        baseline_reader: ExperimentBaselineReader | None = None,
    ) -> None:
        if not isinstance(store, EvolutionExperimentContractStore):
            raise TypeError("Experiment Contract Issuer 需要 durable Store。")
        self._review_service = review_service
        self._workbench_service = workbench_service
        self._store = store
        self._baseline_reader = baseline_reader or GitExperimentBaselineReader()
        self._proposal_outcome_reader: ExperimentProposalOutcomeReader | None = None

    def bind_proposal_outcome_reader(
        self,
        reader: ExperimentProposalOutcomeReader,
    ) -> None:
        if not callable(getattr(reader, "project_session", None)):
            raise TypeError("Proposal Outcome reader 必须实现 project_session()。")
        self._proposal_outcome_reader = reader

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
        await self._require_contract_issue_open(clean_session, clean_proposal)
        existing = await self._store.get_by_proposal(
            workspace_root,
            session_id=clean_session,
            proposal_id=clean_proposal,
        )
        if existing is not None:
            return existing.contract
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
        contract = EvolutionExperimentContract(
            contract_id=_contract_id(manifest_sha256),
            manifest_sha256=manifest_sha256,
            **plain,
        )
        try:
            authority = await self._store.record(
                workspace_root=workspace_root,
                contract=contract,
            )
        except EvolutionExperimentContractStoreError as exc:
            if exc.code != "experiment_contract_proposal_conflict":
                raise
            authority = await self._store.get_by_proposal(
                workspace_root,
                session_id=clean_session,
                proposal_id=clean_proposal,
            )
            if authority is None:
                raise
        return authority.contract

    async def _require_contract_issue_open(
        self,
        session_id: str,
        proposal_id: str,
    ) -> None:
        reader = self._proposal_outcome_reader
        if reader is None:
            raise EvolutionExperimentContractStoreError(
                "experiment_contract_outcome_source_unavailable",
                "Proposal Outcome source 暂不可用，已安全阻止 Contract 签发。",
            )
        try:
            projections = await reader.project_session(session_id)
            if not isinstance(projections, Mapping) or len(projections) > 100:
                raise TypeError("Proposal Outcome projection 集合无效。")
            projection = projections.get(proposal_id)
            if projection is None:
                return
            if hasattr(projection, "model_dump"):
                payload = projection.model_dump(mode="json")
            elif isinstance(projection, Mapping):
                payload = dict(projection)
            else:
                raise TypeError("Proposal Outcome projection 类型无效。")
            if not (
                payload.get("workbench_session_id") == session_id
                and payload.get("workbench_proposal_id") == proposal_id
                and payload.get("status")
                in {"rolled_back", "rollback_recovery_observed"}
                and payload.get("contract_issue_allowed") is False
            ):
                raise ValueError("Proposal Outcome projection 绑定无效。")
        except EvolutionExperimentContractStoreError:
            raise
        except Exception as exc:
            raise EvolutionExperimentContractStoreError(
                "experiment_contract_outcome_source_unavailable",
                "Proposal Outcome source 暂不可用，已安全阻止 Contract 签发。",
            ) from exc
        raise EvolutionExperimentContractStoreError(
            "experiment_contract_outcome_terminal",
            "该 Proposal 已形成 rollback/recovery Outcome，不能再次签发 Contract。",
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


def _canonical_workspace_root(value: str | Path) -> str:
    raw = str(value)
    if any(char in raw for char in ("\x00", "\r", "\n")):
        raise ValueError("Experiment Contract workspace 含非法字符。")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError("Experiment Contract workspace 必须是绝对路径。")
    return str(path.resolve())


async def _ensure_contract_store_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS evolution_experiment_contracts (
            workspace_root TEXT NOT NULL,
            contract_id TEXT NOT NULL,
            manifest_sha256 TEXT NOT NULL,
            authority_id TEXT NOT NULL UNIQUE,
            authority_sha256 TEXT NOT NULL,
            authority_json TEXT NOT NULL,
            approved_at TEXT NOT NULL,
            source_session_id TEXT,
            workbench_proposal_id TEXT,
            PRIMARY KEY (workspace_root, contract_id)
        )
        """
    )
    columns = {
        str(row[1])
        for row in await (await db.execute(
            "PRAGMA table_info(evolution_experiment_contracts)"
        )).fetchall()
    }
    if "source_session_id" not in columns:
        await db.execute(
            "ALTER TABLE evolution_experiment_contracts "
            "ADD COLUMN source_session_id TEXT"
        )
    if "workbench_proposal_id" not in columns:
        await db.execute(
            "ALTER TABLE evolution_experiment_contracts "
            "ADD COLUMN workbench_proposal_id TEXT"
        )
    await db.execute(
        "UPDATE evolution_experiment_contracts "
        "SET source_session_id = json_extract(authority_json, "
        "'$.contract.source.session_id'), "
        "workbench_proposal_id = json_extract(authority_json, "
        "'$.contract.source.workbench_proposal_id') "
        "WHERE source_session_id IS NULL OR workbench_proposal_id IS NULL"
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "idx_evolution_contract_source_proposal ON evolution_experiment_contracts "
        "(workspace_root, source_session_id, workbench_proposal_id) "
        "WHERE source_session_id IS NOT NULL AND workbench_proposal_id IS NOT NULL"
    )


def _contract_authority_from_row(
    row: aiosqlite.Row,
) -> EvolutionExperimentContractAuthority:
    serialized = row["authority_json"]
    if (
        not isinstance(serialized, str)
        or len(serialized.encode("utf-8")) > _MAX_CONTRACT_AUTHORITY_BYTES
    ):
        raise ValueError("Experiment Contract Authority payload 损坏。")
    authority = EvolutionExperimentContractAuthority.model_validate_json(serialized)
    if not (
        row["workspace_root"] == authority.workspace_root
        and row["contract_id"] == authority.contract_id
        and row["manifest_sha256"] == authority.contract_manifest_sha256
        and row["authority_id"] == authority.authority_id
        and row["authority_sha256"] == authority.authority_sha256
        and row["approved_at"] == authority.approved_at
        and row["source_session_id"] == authority.contract.source.session_id
        and row["workbench_proposal_id"]
        == authority.contract.source.workbench_proposal_id
    ):
        raise ValueError("Experiment Contract Authority row 与 payload 不一致。")
    return authority


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


def default_experiment_seed(proposal_id: str) -> int:
    clean = _safe_id(proposal_id, "Proposal")
    digest = hashlib.sha256(
        f"{EXPERIMENT_CONTRACT_POLICY}:{clean}".encode()
    ).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFF_FFFF_FFFF_FFFF


__all__ = [
    "EXPERIMENT_CONTRACT_AUTHORITY_POLICY",
    "EvolutionExperimentContract",
    "EvolutionExperimentContractAuthority",
    "EvolutionExperimentContractIssuer",
    "EvolutionExperimentContractStore",
    "EvolutionExperimentContractStoreError",
    "ExperimentBaseline",
    "ExperimentBudget",
    "ExperimentCheck",
    "ExperimentScope",
    "ExperimentSource",
    "GitExperimentBaselineReader",
    "build_experiment_contract_authority",
    "default_experiment_budget",
    "default_experiment_seed",
    "render_experiment_contract_authority",
]
