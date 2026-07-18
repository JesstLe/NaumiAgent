"""Tamper-evident source snapshots for isolated evolution experiments."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent import __version__
from naumi_agent.evolution.experiment_leases import (
    ExperimentLeaseState,
    ExperimentWorktreeLease,
)
from naumi_agent.evolution.experiments import EvolutionExperimentContract
from naumi_agent.harness.models import HarnessProfileStatus
from naumi_agent.harness.profile import load_harness_profile
from naumi_agent.tools.base import Tool, ToolRegistry

EXPERIMENT_SOURCE_SNAPSHOT_POLICY = "evolution-source-snapshot-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_GIT_OBJECT_RE = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
_CONTRACT_ID_PATTERN = r"^evx_[0-9a-f]{24}$"
_LEASE_ID_PATTERN = r"^evl_[0-9a-f]{24}$"
_SNAPSHOT_ID_RE = re.compile(r"^evs_[0-9a-f]{24}$")
_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_MAX_GIT_OUTPUT = 64 * 1024 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class ExperimentToolIdentity(_StrictModel):
    """Safe identity of one Contract-approved registered tool."""

    name: str = Field(min_length=1, max_length=128)
    implementation: str = Field(min_length=1, max_length=512)
    naumi_version: str = Field(min_length=1, max_length=64)
    schema_sha256: str = Field(pattern=_SHA256_RE)
    metadata_sha256: str = Field(pattern=_SHA256_RE)
    identity_sha256: str = Field(pattern=_SHA256_RE)

    @field_validator("name")
    @classmethod
    def _valid_tool_name(cls, value: str) -> str:
        normalized = value.strip()
        if not _TOOL_NAME_RE.fullmatch(normalized):
            raise ValueError("Snapshot tool name 格式无效。")
        return normalized

    @model_validator(mode="after")
    def _identity_matches(self) -> Self:
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"identity_sha256"})
        )
        if not hmac.compare_digest(self.identity_sha256, expected):
            raise ValueError("Tool identity_sha256 与工具事实不一致。")
        return self


class EvolutionExperimentSourceSnapshot(_StrictModel):
    """Immutable source/config/tool identity captured before mutation planning."""

    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-source-snapshot-v1"] = (
        EXPERIMENT_SOURCE_SNAPSHOT_POLICY
    )
    snapshot_id: str
    snapshot_sha256: str = Field(pattern=_SHA256_RE)
    contract_id: str = Field(pattern=_CONTRACT_ID_PATTERN)
    contract_manifest_sha256: str = Field(pattern=_SHA256_RE)
    lease_id: str = Field(pattern=_LEASE_ID_PATTERN)
    baseline_commit: str = Field(pattern=_GIT_OBJECT_RE)
    baseline_tree: str = Field(pattern=_GIT_OBJECT_RE)
    baseline_tree_sha256: str = Field(pattern=_SHA256_RE)
    profile_status: Literal["valid", "missing"]
    profile_sha256: str = Field(pattern=_SHA256_RE)
    experiment_config_sha256: str = Field(pattern=_SHA256_RE)
    tools: tuple[ExperimentToolIdentity, ...] = Field(min_length=1, max_length=16)
    toolset_sha256: str = Field(pattern=_SHA256_RE)
    worktree_clean: Literal[True] = True
    source_ready: Literal[True] = True
    execution_ready: Literal[False] = False

    @field_validator("snapshot_id")
    @classmethod
    def _valid_snapshot_id(cls, value: str) -> str:
        if not _SNAPSHOT_ID_RE.fullmatch(value):
            raise ValueError("snapshot_id 格式无效。")
        return value

    @model_validator(mode="after")
    def _snapshot_matches_payload(self) -> Self:
        if tuple(tool.name for tool in self.tools) != tuple(
            sorted(tool.name for tool in self.tools)
        ):
            raise ValueError("Snapshot tools 必须按名称稳定排序。")
        if len({tool.name for tool in self.tools}) != len(self.tools):
            raise ValueError("Snapshot tools 不得重复。")
        expected_toolset = _sha256_payload(
            [tool.model_dump(mode="json") for tool in self.tools]
        )
        if not hmac.compare_digest(self.toolset_sha256, expected_toolset):
            raise ValueError("toolset_sha256 与工具集合不一致。")
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"snapshot_id", "snapshot_sha256"})
        )
        if not hmac.compare_digest(self.snapshot_sha256, expected):
            raise ValueError("snapshot_sha256 与 Source Snapshot 不一致。")
        if self.snapshot_id != f"evs_{expected[:24]}":
            raise ValueError("snapshot_id 与 Source Snapshot 不一致。")
        return self


class EvolutionExperimentSourceSnapshotBuilder:
    """Capture exact pre-mutation facts from an active Contract-bound worktree."""

    def __init__(
        self,
        tool_registry: ToolRegistry,
        *,
        worktree_storage_dir: str | Path,
    ) -> None:
        if not isinstance(tool_registry, ToolRegistry):
            raise TypeError("Source Snapshot 必须使用 ToolRegistry。")
        self._tool_registry = tool_registry
        self._worktree_storage_dir = Path(worktree_storage_dir).expanduser().resolve()

    def capture(
        self,
        contract: EvolutionExperimentContract,
        lease: ExperimentWorktreeLease,
    ) -> EvolutionExperimentSourceSnapshot:
        _require_binding(contract, lease)
        try:
            root = Path(lease.worktree_path).resolve(strict=True)
        except OSError as exc:
            raise ValueError("Experiment worktree 不存在或无法读取。") from exc
        if not root.is_dir():
            raise ValueError("Experiment worktree 不存在或不是目录。")
        if root.parent != self._worktree_storage_dir or root.name != lease.worktree_name:
            raise ValueError("Experiment worktree 路径不属于受管 Lease 存储目录。")

        top = self._git_text(root, "rev-parse", "--show-toplevel")
        if Path(top).resolve() != root:
            raise ValueError("Experiment worktree 必须是精确 Git 仓库根目录。")
        head = self._git_text(root, "rev-parse", "HEAD").lower()
        if head != contract.baseline.commit:
            raise ValueError("Experiment worktree HEAD 已偏离 Contract baseline。")
        branch = self._git_text(root, "rev-parse", "--abbrev-ref", "HEAD")
        if branch != lease.branch:
            raise ValueError("Experiment worktree branch 与 Lease binding 不一致。")
        status = self._git_bytes(
            root,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        )
        if status:
            raise ValueError("Experiment worktree 已存在变更，不能生成 Source Snapshot。")

        tree = self._git_text(root, "rev-parse", f"{head}^{{tree}}").lower()
        tree_listing = self._git_bytes(
            root,
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            head,
        )
        profile_status, profile_sha256 = _profile_identity(root)
        tools = tuple(
            sorted(
                (_tool_identity(self._require_tool(name)) for name in contract.allowed_tools),
                key=lambda item: item.name,
            )
        )
        toolset_sha256 = _sha256_payload(
            [tool.model_dump(mode="json") for tool in tools]
        )
        config_sha256 = _experiment_config_digest(contract)
        payload = {
            "schema_version": 1,
            "policy_version": EXPERIMENT_SOURCE_SNAPSHOT_POLICY,
            "contract_id": contract.contract_id,
            "contract_manifest_sha256": contract.manifest_sha256,
            "lease_id": lease.lease_id,
            "baseline_commit": head,
            "baseline_tree": tree,
            "baseline_tree_sha256": hashlib.sha256(tree_listing).hexdigest(),
            "profile_status": profile_status,
            "profile_sha256": profile_sha256,
            "experiment_config_sha256": config_sha256,
            "tools": [tool.model_dump(mode="json") for tool in tools],
            "toolset_sha256": toolset_sha256,
            "worktree_clean": True,
            "source_ready": True,
            "execution_ready": False,
        }
        digest = _sha256_payload(payload)
        return EvolutionExperimentSourceSnapshot.model_validate(
            {
                **payload,
                "snapshot_id": f"evs_{digest[:24]}",
                "snapshot_sha256": digest,
            }
        )

    def _require_tool(self, name: str) -> Tool:
        tool = self._tool_registry.get(name)
        if tool is None or tool.name != name:
            raise ValueError(f"Contract 允许工具未注册：{name}")
        return tool

    def _git_text(self, root: Path, *args: str) -> str:
        try:
            return self._git_bytes(root, *args).decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise RuntimeError("Experiment Git identity 输出不是 ASCII。") from exc

    def _git_bytes(self, root: Path, *args: str) -> bytes:
        env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
        try:
            completed = subprocess.run(
                ["git", "-C", str(root), *args],
                check=False,
                capture_output=True,
                timeout=15,
                env=env,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("无法读取 Experiment Source Snapshot Git 事实。") from exc
        if completed.returncode != 0:
            raise ValueError("Experiment worktree Git identity 不可验证。")
        if len(completed.stdout) > _MAX_GIT_OUTPUT:
            raise RuntimeError("Experiment Git identity 输出超过安全上限。")
        return completed.stdout


def _require_binding(
    contract: EvolutionExperimentContract,
    lease: ExperimentWorktreeLease,
) -> None:
    if not isinstance(contract, EvolutionExperimentContract):
        raise TypeError("Source Snapshot 只能绑定 EvolutionExperimentContract。")
    if not isinstance(lease, ExperimentWorktreeLease):
        raise TypeError("Source Snapshot 只能绑定 ExperimentWorktreeLease。")
    if lease.state is not ExperimentLeaseState.ACTIVE or not lease.worktree_ready:
        raise ValueError("只有 active Experiment Lease 可以生成 Source Snapshot。")
    if (
        lease.contract_id != contract.contract_id
        or lease.manifest_sha256 != contract.manifest_sha256
        or lease.baseline_commit != contract.baseline.commit
        or lease.session_id != contract.source.session_id
        or lease.mission_id != contract.source.mission_id
        or lease.task_id != contract.source.task_id
    ):
        raise ValueError("Experiment Lease 与 Contract binding 不一致。")
    if contract.execution_ready or not contract.requires_source_snapshot:
        raise ValueError("Experiment Contract Source Snapshot 前置不完整。")


def _profile_identity(root: Path) -> tuple[Literal["valid", "missing"], str]:
    snapshot = load_harness_profile(root)
    if snapshot.status is HarnessProfileStatus.VALID and snapshot.digest is not None:
        return "valid", snapshot.digest
    if snapshot.status is HarnessProfileStatus.MISSING:
        return "missing", _sha256_payload({"status": "missing", "schema_version": 1})
    raise ValueError("Harness Profile 无效，不能生成 Experiment Source Snapshot。")


def _tool_identity(tool: Tool) -> ExperimentToolIdentity:
    implementation = f"{tool.__class__.__module__}.{tool.__class__.__qualname__}"
    payload = {
        "name": tool.name,
        "implementation": implementation,
        "naumi_version": __version__,
        "schema_sha256": _sha256_payload(tool.to_openai_tool()),
        "metadata_sha256": _sha256_payload(asdict(tool.metadata)),
    }
    return ExperimentToolIdentity.model_validate(
        {**payload, "identity_sha256": _sha256_payload(payload)}
    )


def _experiment_config_digest(contract: EvolutionExperimentContract) -> str:
    return _sha256_payload(
        {
            "policy_version": contract.policy_version,
            "scope": contract.scope.model_dump(mode="json"),
            "budget": contract.budget.model_dump(mode="json"),
            "allowed_tools": list(contract.allowed_tools),
            "allowed_checks": [
                check.model_dump(mode="json") for check in contract.allowed_checks
            ],
            "seed": contract.seed,
            "network_access": contract.network_access,
            "dependency_installation": contract.dependency_installation,
            "requires_static_guard": contract.requires_static_guard,
        }
    )


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
    "EvolutionExperimentSourceSnapshot",
    "EvolutionExperimentSourceSnapshotBuilder",
    "ExperimentToolIdentity",
]
