"""Mechanical preflight guard for proposed evolution source mutations."""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import hmac
import json
import math
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.experiment_leases import (
    ExperimentLeaseState,
    ExperimentWorktreeLease,
)
from naumi_agent.evolution.experiment_snapshots import (
    EvolutionExperimentSourceSnapshot,
    EvolutionExperimentSourceSnapshotBuilder,
)
from naumi_agent.evolution.experiments import EvolutionExperimentContract
from naumi_agent.evolution.mutation_plans import EvolutionMutationPlan

STATIC_GUARD_POLICY = "evolution-static-guard-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_PROPOSED_FILE_BYTES = 2 * 1024 * 1024
_MAX_SECRET_SCAN_BYTES = 2 * 1024 * 1024
_MAX_PROPOSED_FILES = 16
_MAX_EXACT_DIFF_LINES = 10_000
_MAX_COUNTED_LINES = 4_194_304

_PROTECTED_PREFIXES = (
    ".github/workflows/",
    "src/naumi_agent/safety/",
    "src/naumi_agent/config/credentials.py",
    "src/naumi_agent/persistence/migrations",
    "src/naumi_agent/release",
    "src/naumi_agent/update",
    "src/naumi_agent/orchestrator/engine.py",
    "src/naumi_agent/runtime/ports/permission.py",
    "src/naumi_agent/tools/base.py",
    "src/naumi_agent/evolution/experiments.py",
    "src/naumi_agent/evolution/experiment_leases.py",
    "src/naumi_agent/evolution/experiment_snapshots.py",
    "src/naumi_agent/evolution/mutation_plans.py",
    "src/naumi_agent/evolution/static_guards.py",
    "scripts/build",
    "scripts/package",
    "scripts/release",
)
_DEPENDENCY_PATHS = frozenset(
    {
        "pyproject.toml",
        "uv.lock",
        "requirements.txt",
        "requirements-dev.txt",
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "cargo.toml",
        "cargo.lock",
        "go.mod",
        "go.sum",
    }
)
_GENERATED_PATH_PARTS = frozenset({"build", "dist", "generated", "node_modules", ".venv"})
_GENERATED_NAME_MARKERS = (".generated.", "_generated.", ".min.js", ".min.css")
_GENERATED_CONTENT_PATTERNS = (
    re.compile(rb"@generated\b", re.IGNORECASE),
    re.compile(rb"code generated .* do not edit", re.IGNORECASE),
    re.compile(rb"(?:auto|automatically)[ -]?generated.*do not edit", re.IGNORECASE),
    re.compile(rb"this file (?:is|was) generated", re.IGNORECASE),
)
_KNOWN_SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(rb"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}\b", re.IGNORECASE),
)
_SECRET_ASSIGNMENT_RE = re.compile(
    rb"(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|secret|credential)"
    rb"[\s\"']*(?::|=)[\s\"']*([A-Za-z0-9._~+/=-]{16,})",
    re.IGNORECASE,
)
_SAFE_PLACEHOLDER_MARKERS = (b"example", b"placeholder", b"redacted", b"dummy", b"test", b"fake")

GuardViolationCode = Literal[
    "source_drift",
    "scope_expansion",
    "protected_path",
    "dependency_change",
    "path_escape",
    "symlink",
    "generated_file",
    "binary_content",
    "invalid_encoding",
    "file_too_large",
    "hardcoded_secret",
    "baseline_mismatch",
    "operation_mismatch",
    "no_changes",
    "file_budget_exceeded",
    "line_budget_exceeded",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class StaticGuardViolation(_StrictModel):
    code: GuardViolationCode
    path: str = Field(default="", max_length=1_024)
    detail: str = Field(min_length=1, max_length=500)


class StaticGuardChangeFact(_StrictModel):
    path: str = Field(min_length=1, max_length=1_024)
    operation: Literal["modify", "create", "invalid"]
    before_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    after_sha256: str = Field(pattern=_SHA256_RE)
    size_bytes: int = Field(ge=0, le=_MAX_PROPOSED_FILE_BYTES + 1)
    added_lines: int = Field(ge=0, le=_MAX_COUNTED_LINES)
    deleted_lines: int = Field(ge=0, le=_MAX_COUNTED_LINES)
    changed_lines: int = Field(ge=0, le=_MAX_COUNTED_LINES * 2)

    @model_validator(mode="after")
    def _line_count_matches(self) -> Self:
        if self.changed_lines != self.added_lines + self.deleted_lines:
            raise ValueError("Static Guard changed_lines 与增删行数不一致。")
        return self


class EvolutionStaticGuardReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-static-guard-v1"] = STATIC_GUARD_POLICY
    guard_id: str = Field(pattern=r"^evg_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    policy_sha256: str = Field(pattern=_SHA256_RE)
    contract_id: str = Field(pattern=r"^evx_[0-9a-f]{24}$")
    lease_id: str = Field(pattern=r"^evl_[0-9a-f]{24}$")
    source_snapshot_id: str = Field(pattern=r"^evs_[0-9a-f]{24}$")
    mutation_plan_id: str = Field(pattern=r"^evpplan_[0-9a-f]{24}$")
    mutation_plan_sha256: str = Field(pattern=_SHA256_RE)
    changes: tuple[StaticGuardChangeFact, ...] = Field(max_length=16)
    changes_sha256: str = Field(pattern=_SHA256_RE)
    violations: tuple[StaticGuardViolation, ...] = Field(max_length=64)
    total_changed_files: int = Field(ge=0, le=16)
    total_changed_lines: int = Field(ge=0, le=_MAX_COUNTED_LINES * _MAX_PROPOSED_FILES * 2)
    preflight_passed: bool
    bypass_can_override: Literal[False] = False
    write_authorized: Literal[False] = False
    execution_ready: Literal[False] = False

    @model_validator(mode="after")
    def _receipt_is_consistent_and_tamper_evident(self) -> Self:
        if self.preflight_passed != (not self.violations):
            raise ValueError("Static Guard passed 与 violations 不一致。")
        if self.total_changed_files != len(self.changes):
            raise ValueError("Static Guard changed file count 不一致。")
        if self.total_changed_lines != sum(item.changed_lines for item in self.changes):
            raise ValueError("Static Guard total changed lines 不一致。")
        expected_changes = _sha256_payload([item.model_dump(mode="json") for item in self.changes])
        if not hmac.compare_digest(self.changes_sha256, expected_changes):
            raise ValueError("changes_sha256 与 Guard change facts 不一致。")
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"guard_id", "receipt_sha256"})
        )
        if not hmac.compare_digest(self.receipt_sha256, expected):
            raise ValueError("receipt_sha256 与 Static Guard Receipt 不一致。")
        if self.guard_id != f"evg_{expected[:24]}":
            raise ValueError("guard_id 与 Static Guard Receipt 不一致。")
        return self


class EvolutionStaticGuardPolicy:
    """Versioned path/content policy independent from interactive permission mode."""

    @property
    def digest(self) -> str:
        return _sha256_payload(
            {
                "policy_version": STATIC_GUARD_POLICY,
                "protected_prefixes": list(_PROTECTED_PREFIXES),
                "dependency_paths": sorted(_DEPENDENCY_PATHS),
                "generated_path_parts": sorted(_GENERATED_PATH_PARTS),
                "generated_name_markers": list(_GENERATED_NAME_MARKERS),
                "max_proposed_file_bytes": _MAX_PROPOSED_FILE_BYTES,
                "max_proposed_files": _MAX_PROPOSED_FILES,
                "max_exact_diff_lines": _MAX_EXACT_DIFF_LINES,
                "secret_policy": "known-prefix+assignment+entropy-v1",
            }
        )

    def inspect_path(self, root: Path, relative: str) -> tuple[StaticGuardViolation, ...]:
        normalized = _safe_relative_path(relative)
        folded = normalized.casefold()
        violations: list[StaticGuardViolation] = []
        if any(folded.startswith(prefix) for prefix in _PROTECTED_PREFIXES):
            violations.append(_violation("protected_path", normalized, "目标命中受保护模块策略。"))
        if folded in _DEPENDENCY_PATHS:
            violations.append(
                _violation("dependency_change", normalized, "实验禁止修改依赖或锁文件。")
            )
        parts = tuple(part.casefold() for part in Path(normalized).parts)
        name = Path(normalized).name.casefold()
        if any(part in _GENERATED_PATH_PARTS for part in parts) or any(
            marker in name for marker in _GENERATED_NAME_MARKERS
        ):
            violations.append(_violation("generated_file", normalized, "目标路径属于生成产物。"))

        target = root / normalized
        try:
            resolved = target.resolve(strict=False)
        except OSError:
            violations.append(_violation("path_escape", normalized, "目标路径无法安全解析。"))
            return tuple(violations)
        if not resolved.is_relative_to(root):
            violations.append(_violation("path_escape", normalized, "目标路径越过实验 worktree。"))
        if _has_symlink_component(root, target):
            violations.append(_violation("symlink", normalized, "目标或父目录包含符号链接。"))
        if target.is_file() and not target.is_symlink():
            try:
                with target.open("rb") as stream:
                    prefix = stream.read(16_384)
            except OSError:
                prefix = b""
            if any(pattern.search(prefix) for pattern in _GENERATED_CONTENT_PATTERNS):
                violations.append(
                    _violation("generated_file", normalized, "现有目标声明为自动生成文件。")
                )
        return tuple(violations)

    def inspect_content(
        self,
        relative: str,
        content: bytes,
    ) -> tuple[StaticGuardViolation, ...]:
        violations: list[StaticGuardViolation] = []
        if len(content) > _MAX_PROPOSED_FILE_BYTES:
            violations.append(_violation("file_too_large", relative, "提议内容超过 2 MiB 上限。"))
            return tuple(violations)
        if b"\x00" in content:
            violations.append(
                _violation("binary_content", relative, "提议内容包含 NUL，疑似二进制。")
            )
            return tuple(violations)
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            violations.append(
                _violation("invalid_encoding", relative, "提议内容必须是 UTF-8 文本。")
            )
            return tuple(violations)
        prefix = content[: min(len(content), _MAX_SECRET_SCAN_BYTES)]
        if any(pattern.search(prefix[:16_384]) for pattern in _GENERATED_CONTENT_PATTERNS):
            violations.append(_violation("generated_file", relative, "内容声明为自动生成文件。"))
        if _contains_secret(prefix):
            violations.append(_violation("hardcoded_secret", relative, "检测到疑似硬编码凭据。"))
        return tuple(violations)


class EvolutionStaticGuard:
    """Evaluate exact proposed contents without writing them to disk."""

    def __init__(
        self,
        *,
        snapshot_builder: EvolutionExperimentSourceSnapshotBuilder,
        policy: EvolutionStaticGuardPolicy | None = None,
    ) -> None:
        self._snapshot_builder = snapshot_builder
        self._policy = policy or EvolutionStaticGuardPolicy()

    async def preflight(
        self,
        *,
        contract: EvolutionExperimentContract,
        lease: ExperimentWorktreeLease,
        source_snapshot: EvolutionExperimentSourceSnapshot,
        mutation_plan: EvolutionMutationPlan,
        proposed_contents: Mapping[str, str | bytes],
    ) -> EvolutionStaticGuardReceipt:
        _require_bindings(contract, lease, source_snapshot, mutation_plan)
        root = Path(lease.worktree_path).resolve(strict=True)
        violations: list[StaticGuardViolation] = []
        if not await self._snapshot_matches(contract, lease, source_snapshot):
            violations.append(_violation("source_drift", "", "Source Snapshot 已漂移。"))

        normalized_contents: dict[str, bytes] = {}
        if len(proposed_contents) > _MAX_PROPOSED_FILES:
            raise ValueError("Static Guard 一次最多审查 16 个文件。")
        for raw_path, raw_content in proposed_contents.items():
            if not isinstance(raw_path, str):
                raise TypeError("Static Guard proposed path 必须是 str。")
            try:
                path = _safe_relative_path(raw_path)
            except ValueError:
                path = _invalid_path_label(raw_path)
                violations.append(
                    _violation("path_escape", path, "目标不是安全的 worktree 相对路径。")
                )
            if path in normalized_contents:
                raise ValueError("Static Guard proposed path 规范化后重复。")
            if isinstance(raw_content, str):
                content = raw_content.encode("utf-8")
            elif isinstance(raw_content, bytes):
                content = bytes(raw_content)
            else:
                raise TypeError("Static Guard proposed content 必须是 str 或 bytes。")
            normalized_contents[path] = content

        planned = {item.path: item for item in mutation_plan.planned_files}
        if not normalized_contents:
            violations.append(_violation("no_changes", "", "没有可审查的提议变更。"))
        changes: list[StaticGuardChangeFact] = []
        for path in sorted(normalized_contents):
            content = normalized_contents[path]
            fact = planned.get(path)
            if fact is None:
                violations.append(
                    _violation("scope_expansion", path, "目标不在 approved Mutation Plan。")
                )
            violations.extend(self._policy.inspect_path(root, path))
            violations.extend(self._policy.inspect_content(path, content))
            change, change_violations = _change_fact(root, path, content, fact)
            changes.append(change)
            violations.extend(change_violations)

        total_lines = sum(item.changed_lines for item in changes)
        if len(changes) > mutation_plan.max_changed_files:
            violations.append(
                _violation("file_budget_exceeded", "", "提议文件数超过 Mutation Plan。")
            )
        if total_lines > mutation_plan.max_changed_lines:
            violations.append(
                _violation("line_budget_exceeded", "", "提议变更行数超过 Mutation Plan。")
            )
        if not await self._snapshot_matches(contract, lease, source_snapshot):
            violations.append(
                _violation("source_drift", "", "Guard 扫描期间 Source Snapshot 漂移。")
            )

        ordered_violations = _deduplicate_violations(violations)
        changes_tuple = tuple(changes)
        changes_sha256 = _sha256_payload([item.model_dump(mode="json") for item in changes_tuple])
        payload = {
            "schema_version": 1,
            "policy_version": STATIC_GUARD_POLICY,
            "policy_sha256": self._policy.digest,
            "contract_id": contract.contract_id,
            "lease_id": lease.lease_id,
            "source_snapshot_id": source_snapshot.snapshot_id,
            "mutation_plan_id": mutation_plan.plan_id,
            "mutation_plan_sha256": mutation_plan.plan_sha256,
            "changes": [item.model_dump(mode="json") for item in changes_tuple],
            "changes_sha256": changes_sha256,
            "violations": [item.model_dump(mode="json") for item in ordered_violations],
            "total_changed_files": len(changes_tuple),
            "total_changed_lines": total_lines,
            "preflight_passed": not ordered_violations,
            "bypass_can_override": False,
            "write_authorized": False,
            "execution_ready": False,
        }
        digest = _sha256_payload(payload)
        return EvolutionStaticGuardReceipt.model_validate(
            {
                **payload,
                "guard_id": f"evg_{digest[:24]}",
                "receipt_sha256": digest,
            }
        )

    async def _snapshot_matches(
        self,
        contract: EvolutionExperimentContract,
        lease: ExperimentWorktreeLease,
        source_snapshot: EvolutionExperimentSourceSnapshot,
    ) -> bool:
        try:
            current = await asyncio.to_thread(
                self._snapshot_builder.capture,
                contract,
                lease,
            )
        except (OSError, RuntimeError, ValueError):
            return False
        return current == source_snapshot


def _require_bindings(
    contract: EvolutionExperimentContract,
    lease: ExperimentWorktreeLease,
    snapshot: EvolutionExperimentSourceSnapshot,
    plan: EvolutionMutationPlan,
) -> None:
    if lease.state is not ExperimentLeaseState.ACTIVE or not lease.worktree_ready:
        raise ValueError("Static Guard 需要 active Experiment Lease。")
    if (
        lease.contract_id != contract.contract_id
        or snapshot.contract_id != contract.contract_id
        or snapshot.lease_id != lease.lease_id
        or plan.contract_id != contract.contract_id
        or plan.lease_id != lease.lease_id
        or plan.source_snapshot_id != snapshot.snapshot_id
        or plan.source_snapshot_sha256 != snapshot.snapshot_sha256
        or plan.contract_manifest_sha256 != contract.manifest_sha256
    ):
        raise ValueError("Static Guard Contract/Lease/Snapshot/Plan binding 不一致。")
    if plan.execution_ready or not plan.static_guard_required:
        raise ValueError("Mutation Plan Static Guard 前置不完整。")


def _change_fact(
    root: Path,
    path: str,
    content: bytes,
    planned: object | None,
) -> tuple[StaticGuardChangeFact, tuple[StaticGuardViolation, ...]]:
    violations: list[StaticGuardViolation] = []
    target = root / path
    before: bytes | None
    try:
        before = target.read_bytes() if target.is_file() and not target.is_symlink() else None
    except OSError:
        before = None
    expected_exists = bool(getattr(planned, "exists", False))
    expected_sha = str(getattr(planned, "content_sha256", ""))
    if expected_exists:
        if before is None or hashlib.sha256(before).hexdigest() != expected_sha:
            violations.append(
                _violation("baseline_mismatch", path, "当前文件与 Mutation Plan baseline 不一致。")
            )
    elif before is not None:
        violations.append(_violation("operation_mismatch", path, "计划创建的目标当前已经存在。"))

    operation: Literal["modify", "create", "invalid"]
    if planned is None:
        operation = "invalid"
    else:
        operation = "modify" if expected_exists else "create"
    if len(content) > _MAX_PROPOSED_FILE_BYTES:
        return (
            StaticGuardChangeFact(
                path=path,
                operation=operation,
                before_sha256=(
                    hashlib.sha256(before).hexdigest() if before is not None else None
                ),
                after_sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=_MAX_PROPOSED_FILE_BYTES + 1,
                added_lines=0,
                deleted_lines=0,
                changed_lines=0,
            ),
            tuple(violations),
        )
    before_text = _decode_lines(before)
    after_text = _decode_lines(content)
    added, deleted = _line_delta(before_text, after_text)
    if before is not None and before == content:
        violations.append(_violation("no_changes", path, "提议内容与 baseline 完全相同。"))
    return (
        StaticGuardChangeFact(
            path=path,
            operation=operation,
            before_sha256=(hashlib.sha256(before).hexdigest() if before is not None else None),
            after_sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=min(len(content), _MAX_PROPOSED_FILE_BYTES + 1),
            added_lines=added,
            deleted_lines=deleted,
            changed_lines=added + deleted,
        ),
        tuple(violations),
    )


def _decode_lines(content: bytes | None) -> tuple[str, ...]:
    if content is None:
        return ()
    try:
        return tuple(content.decode("utf-8").splitlines())
    except UnicodeDecodeError:
        return ()


def _line_delta(before: tuple[str, ...], after: tuple[str, ...]) -> tuple[int, int]:
    if len(before) + len(after) > _MAX_EXACT_DIFF_LINES:
        return min(len(after), _MAX_COUNTED_LINES), min(len(before), _MAX_COUNTED_LINES)
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    added = 0
    deleted = 0
    for tag, left_start, left_end, right_start, right_end in matcher.get_opcodes():
        if tag in {"replace", "delete"}:
            deleted += left_end - left_start
        if tag in {"replace", "insert"}:
            added += right_end - right_start
    return added, deleted


def _contains_secret(content: bytes) -> bool:
    if any(pattern.search(content) for pattern in _KNOWN_SECRET_PATTERNS):
        return True
    for match in _SECRET_ASSIGNMENT_RE.finditer(content):
        value = match.group(1)
        folded = value.lower()
        if any(marker in folded for marker in _SAFE_PLACEHOLDER_MARKERS):
            continue
        if _entropy(value) >= 3.2:
            return True
    return False


def _entropy(value: bytes) -> float:
    if not value:
        return 0.0
    counts: dict[int, int] = {}
    for item in value:
        counts[item] = counts.get(item, 0) + 1
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def _has_symlink_component(root: Path, target: Path) -> bool:
    try:
        relative = target.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current = current / part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                return True
        except FileNotFoundError:
            continue
        except OSError:
            return True
    return False


def _safe_relative_path(value: str) -> str:
    normalized = str(value).strip().replace("\\", "/")
    path = Path(normalized)
    if (
        not normalized
        or normalized == "."
        or path.is_absolute()
        or re.match(r"^[A-Za-z]:/", normalized)
        or ".." in path.parts
        or any(ord(char) < 32 or ord(char) == 127 for char in normalized)
    ):
        raise ValueError("Static Guard path 必须是安全相对路径。")
    return normalized


def _invalid_path_label(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()
    return f"<invalid-path:{digest[:16]}>"


def _violation(code: GuardViolationCode, path: str, detail: str) -> StaticGuardViolation:
    return StaticGuardViolation(code=code, path=path, detail=detail)


def _deduplicate_violations(
    violations: list[StaticGuardViolation],
) -> tuple[StaticGuardViolation, ...]:
    unique = {(item.code, item.path, item.detail): item for item in violations}
    return tuple(unique[key] for key in sorted(unique))


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
    "EvolutionStaticGuard",
    "EvolutionStaticGuardPolicy",
    "EvolutionStaticGuardReceipt",
    "StaticGuardChangeFact",
    "StaticGuardViolation",
]
