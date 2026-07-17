"""Fail-closed workspace Artifact garbage collection for Session deletion."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

from naumi_agent.harness.reconciliation import (
    ReconciliationArtifactKind,
    ReconciliationArtifactReference,
)

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[/\\]")
_ALLOWED_ROOTS = (("artifacts",), (".naumi", "artifacts"))


class ArtifactGarbageCollectionError(RuntimeError):
    """Raised when an eligible Artifact cannot be safely inspected or removed."""


@dataclass(frozen=True, slots=True)
class ArtifactGcResult:
    candidate_count: int
    deleted_count: int
    missing_count: int
    shared_count: int
    unsafe_reference_count: int
    non_file_count: int
    blocked_by_unresolved_live_reference: bool


@dataclass(slots=True)
class ArtifactGcPlan:
    candidates: dict[str, Path]
    shared_keys: set[str] = field(default_factory=set)
    unsafe_reference_count: int = 0
    unresolved_live_reference: bool = False


class ArtifactGarbageCollector:
    """Normalize aliases, preserve shared files, and unlink eligible Artifacts."""

    def __init__(self, workspace_root: str | Path) -> None:
        if isinstance(workspace_root, str) and not workspace_root.strip():
            raise ValueError("workspace_root 不能为空。")
        self.workspace_root = Path(workspace_root).expanduser().resolve()

    def build_plan(
        self,
        references: tuple[ReconciliationArtifactReference, ...],
    ) -> ArtifactGcPlan:
        plan = ArtifactGcPlan(candidates={})
        for reference in references:
            candidate = self._candidate(reference)
            if candidate is None:
                plan.unsafe_reference_count += 1
                continue
            key, path = candidate
            plan.candidates.setdefault(key, path)
        return plan

    def observe_surviving_references(
        self,
        plan: ArtifactGcPlan,
        references: tuple[ReconciliationArtifactReference, ...],
    ) -> None:
        for reference in references:
            candidate = self._candidate(reference)
            if candidate is None:
                plan.unresolved_live_reference = True
                continue
            key, _ = candidate
            if key in plan.candidates:
                plan.shared_keys.add(key)

    def execute(self, plan: ArtifactGcPlan) -> ArtifactGcResult:
        shared_keys = set(plan.shared_keys)
        if plan.unresolved_live_reference:
            shared_keys.update(plan.candidates)
        deleted = 0
        missing = 0
        non_file = 0
        unsafe = plan.unsafe_reference_count
        failures: list[str] = []
        for key, path in sorted(plan.candidates.items()):
            if key in shared_keys:
                continue
            try:
                disposition = self._unlink_candidate(path)
            except OSError as exc:
                failures.append(type(exc).__name__)
                continue
            if disposition == "deleted":
                deleted += 1
            elif disposition == "missing":
                missing += 1
            elif disposition == "non_file":
                non_file += 1
            else:
                unsafe += 1
        if failures:
            kinds = ", ".join(sorted(set(failures)))
            raise ArtifactGarbageCollectionError(
                f"{len(failures)} 个 Artifact 无法安全删除（{kinds}）。"
            )
        return ArtifactGcResult(
            candidate_count=len(plan.candidates),
            deleted_count=deleted,
            missing_count=missing,
            shared_count=len(shared_keys),
            unsafe_reference_count=unsafe,
            non_file_count=non_file,
            blocked_by_unresolved_live_reference=plan.unresolved_live_reference,
        )

    def collect(
        self,
        references: tuple[ReconciliationArtifactReference, ...],
        surviving_references: tuple[ReconciliationArtifactReference, ...],
    ) -> ArtifactGcResult:
        plan = self.build_plan(references)
        self.observe_surviving_references(plan, surviving_references)
        return self.execute(plan)

    def _candidate(
        self,
        reference: ReconciliationArtifactReference,
    ) -> tuple[str, Path] | None:
        try:
            value = self._reference_path(reference)
        except (TypeError, ValueError):
            return None
        if (
            not value
            or "\x00" in value
            or (os.name != "nt" and _WINDOWS_DRIVE_RE.match(value))
        ):
            return None
        normalized = value.replace("\\", "/")
        raw_path = Path(normalized).expanduser()
        lexical = raw_path if raw_path.is_absolute() else self.workspace_root / raw_path
        try:
            relative_lexical = lexical.relative_to(self.workspace_root)
        except ValueError:
            return None
        if not self._is_allowed_relative(relative_lexical):
            return None
        if self._contains_filesystem_indirection(relative_lexical):
            return None
        resolved = lexical.resolve(strict=False)
        try:
            relative = resolved.relative_to(self.workspace_root)
        except ValueError:
            return None
        if not self._is_allowed_relative(relative):
            return None
        key = os.path.normcase(str(resolved))
        return key, resolved

    @staticmethod
    def _reference_path(reference: ReconciliationArtifactReference) -> str:
        if reference.kind is ReconciliationArtifactKind.CHECK_PATH:
            return reference.value.strip()
        if reference.kind is not ReconciliationArtifactKind.EVIDENCE_URI:
            raise ValueError("未知 Artifact 引用类型。")
        parsed = urlsplit(reference.value.strip())
        if (
            parsed.scheme != "artifact"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Artifact URI 格式无效。")
        decoded = unquote(f"{parsed.netloc}{parsed.path}")
        if not decoded or "\x00" in decoded:
            raise ValueError("Artifact URI 路径无效。")
        return decoded.lstrip("/")

    @staticmethod
    def _is_allowed_relative(relative: Path) -> bool:
        parts = relative.parts
        return any(parts[: len(root)] == root for root in _ALLOWED_ROOTS)

    def _contains_filesystem_indirection(self, relative: Path) -> bool:
        current = self.workspace_root
        for part in relative.parts:
            current = current / part
            try:
                is_junction = getattr(current, "is_junction", lambda: False)
                if current.is_symlink() or is_junction() or current.is_mount():
                    return True
            except OSError:
                return True
        return False

    def _unlink_candidate(self, path: Path) -> str:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return "missing"
        if stat.S_ISLNK(metadata.st_mode):
            return "unsafe"
        if not stat.S_ISREG(metadata.st_mode):
            return "non_file"
        if os.name == "posix" and os.unlink in os.supports_dir_fd:
            self._unlink_posix(path)
        else:
            resolved = path.resolve(strict=True)
            if os.path.normcase(str(resolved)) != os.path.normcase(str(path)):
                return "unsafe"
            path.unlink()
        return "deleted"

    def _unlink_posix(self, path: Path) -> None:
        relative = path.relative_to(self.workspace_root)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        descriptors: list[int] = []
        try:
            current_fd = os.open(self.workspace_root, directory_flags)
            descriptors.append(current_fd)
            for part in relative.parts[:-1]:
                current_fd = os.open(
                    part,
                    directory_flags | no_follow,
                    dir_fd=current_fd,
                )
                descriptors.append(current_fd)
            leaf = relative.parts[-1]
            metadata = os.stat(leaf, dir_fd=current_fd, follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError("Artifact 在删除前不再是普通文件。")
            os.unlink(leaf, dir_fd=current_fd)
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
