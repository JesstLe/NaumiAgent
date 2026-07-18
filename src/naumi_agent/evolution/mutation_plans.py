"""Deterministic, non-executable mutation plans for evolution experiments."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.evolution.experiment_leases import (
    ExperimentLeaseState,
    ExperimentWorktreeLease,
)
from naumi_agent.evolution.experiment_snapshots import (
    EvolutionExperimentSourceSnapshot,
    EvolutionExperimentSourceSnapshotBuilder,
)
from naumi_agent.evolution.experiments import EvolutionExperimentContract
from naumi_agent.evolution.review import EvolutionReviewService

MUTATION_PLAN_POLICY = "evolution-mutation-plan-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_PLAN_ID_RE = r"^evpplan_[0-9a-f]{24}$"
_GIT_OBJECT_RE = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
_MAX_SOURCE_BYTES = 2 * 1024 * 1024
_MAX_GIT_OUTPUT = 2 * 1024 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class MutationObjective(_StrictModel):
    finding_code: str = Field(min_length=1, max_length=128)
    candidate_kind: str = Field(min_length=1, max_length=64)
    proposal_kind: str = Field(min_length=1, max_length=64)
    scope: str = Field(min_length=1, max_length=1_024)
    hypothesis: str = Field(min_length=1, max_length=4_000)

    @field_validator("finding_code", "candidate_kind", "proposal_kind", "scope", "hypothesis")
    @classmethod
    def _safe_objective_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(char in normalized for char in ("\x00", "\r")):
            raise ValueError("Mutation objective 含非法控制字符。")
        return normalized


class MutationFileFact(_StrictModel):
    path: str = Field(min_length=1, max_length=1_024)
    exists: bool
    file_kind: Literal[
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
        "text",
        "other",
    ]
    size_bytes: int = Field(ge=0, le=_MAX_SOURCE_BYTES)
    content_sha256: str = Field(pattern=_SHA256_RE)
    baseline_blob: str | None = Field(default=None, pattern=_GIT_OBJECT_RE)
    change_mode: Literal["modify", "create"]

    @field_validator("path")
    @classmethod
    def _safe_relative_path(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        path = Path(normalized)
        if (
            not normalized
            or path.is_absolute()
            or ".." in path.parts
            or any(char in normalized for char in ("\x00", "\r", "\n"))
        ):
            raise ValueError("Mutation file path 必须是安全相对路径。")
        return normalized

    @model_validator(mode="after")
    def _existence_matches_mode(self) -> Self:
        if self.exists != (self.change_mode == "modify"):
            raise ValueError("Mutation file existence 与 change_mode 不一致。")
        if self.exists != (self.baseline_blob is not None):
            raise ValueError("Mutation file existence 与 baseline blob 不一致。")
        return self


class MutationPlanStage(_StrictModel):
    order: int = Field(ge=1, le=6)
    phase: Literal[
        "inspect",
        "baseline_check",
        "mutation",
        "static_guard",
        "candidate_check",
        "receipt",
    ]
    purpose: str = Field(min_length=1, max_length=500)
    allowed_tools: tuple[str, ...] = Field(default=(), max_length=8)
    target_files: tuple[str, ...] = Field(default=(), max_length=16)
    metric_names: tuple[str, ...] = Field(default=(), max_length=8)

    @field_validator("allowed_tools", "target_files", "metric_names")
    @classmethod
    def _unique_values(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("Mutation Plan stage 列表不得重复。")
        return values


class EvolutionMutationPlan(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-mutation-plan-v1"] = MUTATION_PLAN_POLICY
    plan_id: str = Field(pattern=_PLAN_ID_RE)
    plan_sha256: str = Field(pattern=_SHA256_RE)
    contract_id: str = Field(pattern=r"^evx_[0-9a-f]{24}$")
    contract_manifest_sha256: str = Field(pattern=_SHA256_RE)
    lease_id: str = Field(pattern=r"^evl_[0-9a-f]{24}$")
    source_snapshot_id: str = Field(pattern=r"^evs_[0-9a-f]{24}$")
    source_snapshot_sha256: str = Field(pattern=_SHA256_RE)
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    candidate_sha256: str = Field(pattern=_SHA256_RE)
    objective: MutationObjective
    authorized_files: tuple[str, ...] = Field(min_length=1, max_length=16)
    planned_files: tuple[MutationFileFact, ...] = Field(min_length=1, max_length=16)
    stages: tuple[MutationPlanStage, ...] = Field(min_length=6, max_length=6)
    max_changed_files: int = Field(ge=1, le=16)
    max_changed_lines: int = Field(ge=1, le=2_000)
    max_tool_calls: int = Field(ge=1, le=200)
    max_attempts: int = Field(ge=1, le=3)
    baseline_check_required: Literal[True] = True
    static_guard_required: Literal[True] = True
    unrelated_refactor_allowed: Literal[False] = False
    scope_expansion_allowed: Literal[False] = False
    network_access: Literal[False] = False
    dependency_installation: Literal[False] = False
    plan_ready: Literal[True] = True
    execution_ready: Literal[False] = False

    @field_validator("authorized_files")
    @classmethod
    def _safe_authorized_files(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(MutationFileFact._safe_relative_path(value) for value in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError("Mutation authorized_files 不得重复。")
        return normalized

    @model_validator(mode="after")
    def _plan_is_ordered_bounded_and_tamper_evident(self) -> Self:
        if tuple(stage.order for stage in self.stages) != tuple(range(1, 7)):
            raise ValueError("Mutation Plan stages 必须是连续 1..6。")
        expected_phases = (
            "inspect",
            "baseline_check",
            "mutation",
            "static_guard",
            "candidate_check",
            "receipt",
        )
        if tuple(stage.phase for stage in self.stages) != expected_phases:
            raise ValueError("Mutation Plan 必须遵循 inspect→RED→mutation→guard→GREEN→receipt。")
        planned_paths = tuple(item.path for item in self.planned_files)
        if planned_paths != self.authorized_files:
            raise ValueError("Mutation Plan 不得删除、重排或扩大 approved file scope。")
        if len(planned_paths) > self.max_changed_files:
            raise ValueError("Mutation Plan 文件数超过 Contract budget。")
        if self.contract_id != f"evx_{self.contract_manifest_sha256[:24]}":
            raise ValueError("Mutation Plan Contract identity 不一致。")
        expected_lease = hashlib.sha256(
            f"{self.contract_id}:{self.contract_manifest_sha256}".encode()
        ).hexdigest()
        if self.lease_id != f"evl_{expected_lease[:24]}":
            raise ValueError("Mutation Plan Lease identity 不一致。")
        if self.source_snapshot_id != f"evs_{self.source_snapshot_sha256[:24]}":
            raise ValueError("Mutation Plan Source Snapshot identity 不一致。")
        if self.stages[0].allowed_tools != ("file_read", "glob", "grep"):
            raise ValueError("Inspect stage 必须使用固定只读工具集合。")
        mutation = self.stages[2]
        if mutation.target_files != planned_paths:
            raise ValueError("Mutation stage target_files 与计划文件不一致。")
        if any(tool not in {"file_edit", "file_write"} for tool in mutation.allowed_tools):
            raise ValueError("Mutation stage 包含未授权写工具。")
        if mutation.allowed_tools != ("file_edit", "file_write"):
            raise ValueError("Mutation stage 必须使用固定最小写工具集合。")
        if any(self.stages[index].allowed_tools for index in (1, 3, 4, 5)):
            raise ValueError("非 inspect/mutation 阶段不得声明执行工具。")
        for index in (0, 2, 3, 5):
            if self.stages[index].target_files != planned_paths:
                raise ValueError("Mutation Plan 文件阶段必须绑定全部 approved targets。")
        if self.stages[1].target_files or self.stages[4].target_files:
            raise ValueError("RED/GREEN 阶段只绑定机械指标。")
        if not self.stages[1].metric_names:
            raise ValueError("Mutation Plan 至少需要一个机械验证指标。")
        if self.stages[1].metric_names != self.stages[4].metric_names:
            raise ValueError("RED/GREEN 必须使用相同机械指标。")
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"plan_id", "plan_sha256"})
        )
        if not hmac.compare_digest(self.plan_sha256, expected):
            raise ValueError("plan_sha256 与 Mutation Plan 不一致。")
        if self.plan_id != f"evpplan_{expected[:24]}":
            raise ValueError("plan_id 与 Mutation Plan 不一致。")
        return self


class EvolutionMutationPlanner:
    """Build one verified plan without writing files or executing checks."""

    def __init__(
        self,
        *,
        review_service: EvolutionReviewService,
        snapshot_builder: EvolutionExperimentSourceSnapshotBuilder,
    ) -> None:
        self._review_service = review_service
        self._snapshot_builder = snapshot_builder

    async def plan(
        self,
        workspace_root: str | Path,
        *,
        contract: EvolutionExperimentContract,
        lease: ExperimentWorktreeLease,
        source_snapshot: EvolutionExperimentSourceSnapshot,
    ) -> EvolutionMutationPlan:
        _require_bindings(contract, lease, source_snapshot)
        current_snapshot = await asyncio.to_thread(
            self._snapshot_builder.capture,
            contract,
            lease,
        )
        if current_snapshot != source_snapshot:
            raise ValueError("Experiment Source Snapshot 已漂移，必须重新签发计划。")
        review = await self._review_service.detail_snapshot(
            workspace_root,
            contract.source.candidate_id,
        )
        item = review.selected
        proposal = item.proposal if item is not None else None
        if item is None or proposal is None:
            raise ValueError("Mutation Plan 的 Candidate 当前不可验证。")
        if (
            item.candidate_id != contract.source.candidate_id
            or item.revision != contract.source.candidate_revision
            or proposal.source.candidate_sha256 != contract.source.candidate_sha256
            or proposal.proposal_id != contract.source.proposal_id
            or proposal.proposal_kind != contract.source.proposal_kind
        ):
            raise ValueError("Mutation Plan Candidate/Proposal 已偏离 approved Contract。")

        root = Path(lease.worktree_path).resolve(strict=True)
        file_facts = tuple(
            await asyncio.gather(*(
                asyncio.to_thread(
                    _scan_file,
                    root,
                    contract.baseline.commit,
                    path,
                )
                for path in contract.scope.allowed_files
            ))
        )
        final_snapshot = await asyncio.to_thread(
            self._snapshot_builder.capture,
            contract,
            lease,
        )
        if final_snapshot != source_snapshot:
            raise ValueError("文件扫描期间 Source Snapshot 发生漂移，已停止规划。")
        metrics = tuple(check.metric_name for check in contract.allowed_checks)
        files = tuple(fact.path for fact in file_facts)
        max_changed_lines = min(
            contract.budget.max_changed_lines,
            sum(_line_budget(fact.file_kind) for fact in file_facts),
        )
        max_tool_calls = min(
            contract.budget.max_tool_calls,
            6 + (5 * len(file_facts)) + (2 * len(metrics)),
        )
        read_tools = tuple(
            tool for tool in contract.allowed_tools if tool in {"file_read", "glob", "grep"}
        )
        write_tools = tuple(
            tool for tool in contract.allowed_tools if tool in {"file_edit", "file_write"}
        )
        stages = (
            MutationPlanStage(
                order=1,
                phase="inspect",
                purpose="读取 approved scope 的基线事实，只定位与 Finding 直接相关的最小修改点。",
                allowed_tools=read_tools,
                target_files=files,
            ),
            MutationPlanStage(
                order=2,
                phase="baseline_check",
                purpose="在任何写入前采集全部机械指标的 RED/baseline 证据。",
                metric_names=metrics,
            ),
            MutationPlanStage(
                order=3,
                phase="mutation",
                purpose="仅修改 approved files；禁止顺手重构、扩展 scope 或安装依赖。",
                allowed_tools=write_tools,
                target_files=files,
            ),
            MutationPlanStage(
                order=4,
                phase="static_guard",
                purpose=(
                    "写入后必须先通过 EVO-02.6 路径、protected、secret、binary "
                    "与预算机械门禁。"
                ),
                target_files=files,
            ),
            MutationPlanStage(
                order=5,
                phase="candidate_check",
                purpose="使用与 baseline 完全相同的指标采集 GREEN/candidate 证据。",
                metric_names=metrics,
            ),
            MutationPlanStage(
                order=6,
                phase="receipt",
                purpose="生成 diff、工具证据和检查引用；不得自行宣称提升或推广。",
                target_files=files,
            ),
        )
        payload = {
            "schema_version": 1,
            "policy_version": MUTATION_PLAN_POLICY,
            "contract_id": contract.contract_id,
            "contract_manifest_sha256": contract.manifest_sha256,
            "lease_id": lease.lease_id,
            "source_snapshot_id": source_snapshot.snapshot_id,
            "source_snapshot_sha256": source_snapshot.snapshot_sha256,
            "candidate_id": item.candidate_id,
            "candidate_revision": item.revision,
            "candidate_sha256": proposal.source.candidate_sha256,
            "objective": MutationObjective(
                finding_code=item.finding_code,
                candidate_kind=item.kind,
                proposal_kind=proposal.proposal_kind,
                scope=item.scope,
                hypothesis=item.hypothesis,
            ).model_dump(mode="json"),
            "authorized_files": list(contract.scope.allowed_files),
            "planned_files": [fact.model_dump(mode="json") for fact in file_facts],
            "stages": [stage.model_dump(mode="json") for stage in stages],
            "max_changed_files": len(file_facts),
            "max_changed_lines": max_changed_lines,
            "max_tool_calls": max_tool_calls,
            "max_attempts": contract.budget.max_attempts,
            "baseline_check_required": True,
            "static_guard_required": True,
            "unrelated_refactor_allowed": False,
            "scope_expansion_allowed": False,
            "network_access": False,
            "dependency_installation": False,
            "plan_ready": True,
            "execution_ready": False,
        }
        digest = _sha256_payload(payload)
        return EvolutionMutationPlan.model_validate(
            {
                **payload,
                "plan_id": f"evpplan_{digest[:24]}",
                "plan_sha256": digest,
            }
        )


def _require_bindings(
    contract: EvolutionExperimentContract,
    lease: ExperimentWorktreeLease,
    snapshot: EvolutionExperimentSourceSnapshot,
) -> None:
    if lease.state is not ExperimentLeaseState.ACTIVE or not lease.worktree_ready:
        raise ValueError("Mutation Plan 需要 active Experiment Lease。")
    if not snapshot.source_ready or snapshot.execution_ready:
        raise ValueError("Mutation Plan 需要不可执行的 ready Source Snapshot。")
    if (
        lease.contract_id != contract.contract_id
        or lease.manifest_sha256 != contract.manifest_sha256
        or snapshot.contract_id != contract.contract_id
        or snapshot.contract_manifest_sha256 != contract.manifest_sha256
        or snapshot.lease_id != lease.lease_id
        or snapshot.baseline_commit != contract.baseline.commit
    ):
        raise ValueError("Mutation Plan Contract/Lease/Snapshot binding 不一致。")


def _scan_file(root: Path, baseline_commit: str, relative: str) -> MutationFileFact:
    target = root / relative
    resolved = target.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise ValueError(f"Mutation target 越过 worktree 边界：{relative}")
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return MutationFileFact(
            path=relative,
            exists=False,
            file_kind=_file_kind(relative),
            size_bytes=0,
            content_sha256=_sha256_payload({"missing": relative}),
            baseline_blob=None,
            change_mode="create",
        )
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"Mutation target 是符号链接，必须等待 Static Guard 复核：{relative}")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"Mutation target 不是普通文件：{relative}")
    if metadata.st_size > _MAX_SOURCE_BYTES:
        raise ValueError(f"Mutation target 超过 2 MiB 规划上限：{relative}")
    content = target.read_bytes()
    if b"\x00" in content:
        raise ValueError(f"Mutation target 疑似二进制文件：{relative}")
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Mutation target 必须是 UTF-8 文本：{relative}") from exc
    blob = _baseline_blob(root, baseline_commit, relative)
    if blob is None:
        raise ValueError(f"Mutation target 存在但不属于 Contract baseline：{relative}")
    baseline_content = _baseline_content(root, blob)
    if content != baseline_content:
        raise ValueError(f"Mutation target 内容已偏离 Contract baseline：{relative}")
    return MutationFileFact(
        path=relative,
        exists=True,
        file_kind=_file_kind(relative),
        size_bytes=len(content),
        content_sha256=hashlib.sha256(content).hexdigest(),
        baseline_blob=blob,
        change_mode="modify",
    )


def _baseline_blob(root: Path, commit: str, relative: str) -> str | None:
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", f"{commit}:{relative}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("无法读取 Mutation target baseline blob。") from exc
    if completed.returncode != 0:
        return None
    if len(completed.stdout) > _MAX_GIT_OUTPUT:
        raise RuntimeError("Mutation baseline blob 输出超过安全上限。")
    blob = completed.stdout.strip().lower()
    if not (len(blob) in {40, 64} and all(char in "0123456789abcdef" for char in blob)):
        raise RuntimeError("Mutation baseline blob identity 格式无效。")
    return blob


def _baseline_content(root: Path, blob: str) -> bytes:
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "cat-file", "blob", blob],
            check=False,
            capture_output=True,
            timeout=10,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("无法读取 Mutation target baseline 内容。") from exc
    if completed.returncode != 0:
        raise ValueError("Mutation target baseline blob 不可读取。")
    if len(completed.stdout) > _MAX_SOURCE_BYTES:
        raise ValueError("Mutation target baseline 内容超过 2 MiB 规划上限。")
    return completed.stdout


def _file_kind(path: str) -> str:
    suffix = Path(path).suffix.lower()
    return {
        ".py": "python",
        ".js": "javascript",
        ".jsx": "javascript",
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
        ".txt": "text",
    }.get(suffix, "other")


def _line_budget(file_kind: str) -> int:
    if file_kind in {"yaml", "json", "toml"}:
        return 120
    if file_kind in {"markdown", "text"}:
        return 160
    if file_kind in {"python", "javascript", "typescript", "swift", "rust", "go"}:
        return 200
    return 80


def _sha256_payload(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "EvolutionMutationPlan",
    "EvolutionMutationPlanner",
    "MutationFileFact",
    "MutationObjective",
    "MutationPlanStage",
]
