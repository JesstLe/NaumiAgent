"""Deterministic source identity capture for governed Claude Code intake."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SHA256_LENGTH = 64
_MAX_GIT_OUTPUT = 16 * 1024 * 1024
_MAX_EVIDENCE_FILE_BYTES = 4 * 1024 * 1024
_MAX_UNTRACKED_FILE_BYTES = 64 * 1024 * 1024
_MAX_UNTRACKED_FILES = 1_000


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceGitIdentity(_StrictModel):
    remote: str = Field(min_length=1, max_length=2_048)
    commit: str
    branch: str = Field(min_length=1, max_length=256)
    upstream: str = Field(default="", max_length=512)
    ahead: int = Field(ge=0)
    behind: int = Field(ge=0)
    dirty: bool
    worktree_sha256: str | None = None
    dirty_reason: str = Field(default="", max_length=500)

    @field_validator("remote")
    @classmethod
    def _remote_has_no_credentials(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme and (parsed.username is not None or parsed.password is not None):
            raise ValueError("source remote 不得包含内嵌凭据。")
        if any(character in value for character in ("\n", "\r", "\x00")):
            raise ValueError("source remote 含非法控制字符。")
        return value

    @field_validator("commit")
    @classmethod
    def _full_commit(cls, value: str) -> str:
        if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("source commit 必须是完整小写 Git SHA-1。")
        return value

    @model_validator(mode="after")
    def _dirty_state_is_auditable(self) -> SourceGitIdentity:
        if self.dirty:
            _require_sha256(self.worktree_sha256, "dirty source worktree")
            if not self.dirty_reason.strip():
                raise ValueError("dirty source 必须记录理由。")
        elif self.worktree_sha256 is not None or self.dirty_reason:
            raise ValueError("clean source 不得携带 dirty digest 或理由。")
        return self


class SourceLicenseEvidence(_StrictModel):
    path: str = Field(min_length=1, max_length=1_024)
    sha256: str
    claim: str = Field(min_length=1, max_length=1_000)

    @field_validator("sha256")
    @classmethod
    def _digest(cls, value: str) -> str:
        return _require_sha256(value, "license")


class LegacyMappingEvidence(_StrictModel):
    path: str = Field(min_length=1, max_length=1_024)
    sha256: str
    compatibility: Literal["read_only_one_release"] = "read_only_one_release"

    @field_validator("sha256")
    @classmethod
    def _digest(cls, value: str) -> str:
        return _require_sha256(value, "legacy map")


class SourceIdentityManifest(_StrictModel):
    schema_version: Literal[2] = 2
    manifest_kind: Literal["claude_code_source_identity"] = "claude_code_source_identity"
    generated_at: str
    source_name: str = Field(min_length=1, max_length=128)
    checkout_hint: str = Field(min_length=1, max_length=1_024)
    git: SourceGitIdentity
    license: SourceLicenseEvidence
    legacy_mapping: LegacyMappingEvidence

    @field_validator("generated_at")
    @classmethod
    def _aware_timestamp(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("generated_at 必须包含时区。")
        return value


class SourceAuditResult(_StrictModel):
    status: Literal["valid", "stale", "invalid"]
    findings: tuple[str, ...]
    current_commit: str = ""


@dataclass(frozen=True, slots=True)
class _CurrentGitIdentity:
    commit: str
    remote: str
    branch: str
    upstream: str
    ahead: int
    behind: int
    dirty: bool
    worktree_sha256: str | None


def capture_source_identity(
    source_root: str | Path,
    legacy_map_path: str | Path,
    *,
    source_name: str,
    checkout_hint: str,
    license_path: str = "README.md",
    license_claim: str,
    dirty_reason: str = "",
    observed_at: datetime | None = None,
) -> SourceIdentityManifest:
    """Capture source facts without copying source text or Git diffs."""
    root = Path(source_root).expanduser().resolve()
    mapping = Path(legacy_map_path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Claude Code source checkout 不存在。")
    if not mapping.is_file():
        raise ValueError("legacy source map 不存在。")
    license_file = _safe_source_file(root, license_path)
    claim = license_claim.strip()
    if claim not in _read_text_bounded(license_file):
        raise ValueError("许可证声明与 source 文件不一致。")

    status = _git_bytes(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    dirty = bool(status)
    worktree_sha256 = _worktree_digest(root, status) if dirty else None
    if dirty and not dirty_reason.strip():
        raise ValueError("source checkout 有改动，必须提供 dirty_reason。")
    upstream = _optional_git_text(
        root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"
    )
    ahead, behind = _ahead_behind(root, upstream)
    timestamp = observed_at or datetime.now(UTC)
    return SourceIdentityManifest(
        generated_at=timestamp.isoformat(),
        source_name=source_name.strip(),
        checkout_hint=checkout_hint.strip(),
        git=SourceGitIdentity(
            remote=_git_text(root, "remote", "get-url", "origin"),
            commit=_git_text(root, "rev-parse", "HEAD"),
            branch=_git_text(root, "branch", "--show-current"),
            upstream=upstream,
            ahead=ahead,
            behind=behind,
            dirty=dirty,
            worktree_sha256=worktree_sha256,
            dirty_reason=dirty_reason.strip() if dirty else "",
        ),
        license=SourceLicenseEvidence(
            path=license_path,
            sha256=_file_sha256(license_file, max_bytes=_MAX_EVIDENCE_FILE_BYTES),
            claim=claim,
        ),
        legacy_mapping=LegacyMappingEvidence(
            path=_portable_manifest_path(mapping),
            sha256=_file_sha256(mapping, max_bytes=_MAX_EVIDENCE_FILE_BYTES),
        ),
    )


def verify_source_identity(
    manifest: SourceIdentityManifest,
    source_root: str | Path,
    project_root: str | Path,
) -> SourceAuditResult:
    """Compare a captured manifest with current local facts."""
    root = Path(source_root).expanduser().resolve()
    project = Path(project_root).expanduser().resolve()
    if not root.is_dir():
        return SourceAuditResult(status="invalid", findings=("source checkout 不存在。",))
    try:
        current = _read_current_git_identity(root)
    except (OSError, ValueError):
        return SourceAuditResult(
            status="invalid",
            findings=("source Git 身份不可读。",),
        )
    findings = _git_identity_findings(manifest.git, current)
    license_findings, license_invalid = _license_findings(manifest, root)
    mapping_findings, mapping_invalid = _mapping_findings(manifest, project)
    findings.extend(license_findings)
    findings.extend(mapping_findings)
    invalid = license_invalid or mapping_invalid
    status_name: Literal["valid", "stale", "invalid"]
    status_name = "invalid" if invalid else ("stale" if findings else "valid")
    return SourceAuditResult(
        status=status_name,
        findings=tuple(findings),
        current_commit=current.commit,
    )


def _read_current_git_identity(root: Path) -> _CurrentGitIdentity:
    upstream = _optional_git_text(
        root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"
    )
    ahead, behind = _ahead_behind(root, upstream)
    status = _git_bytes(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    dirty = bool(status)
    return _CurrentGitIdentity(
        commit=_git_text(root, "rev-parse", "HEAD"),
        remote=_git_text(root, "remote", "get-url", "origin"),
        branch=_git_text(root, "branch", "--show-current"),
        upstream=upstream,
        ahead=ahead,
        behind=behind,
        dirty=dirty,
        worktree_sha256=_worktree_digest(root, status) if dirty else None,
    )


def _git_identity_findings(
    captured: SourceGitIdentity,
    current: _CurrentGitIdentity,
) -> list[str]:
    findings: list[str] = []
    comparisons = (
        (current.commit, captured.commit, "source commit 已变化，manifest stale。"),
        (current.remote, captured.remote, "source remote 已变化，manifest stale。"),
        (current.branch, captured.branch, "source branch 已变化，manifest stale。"),
        (
            (current.upstream, current.ahead, current.behind),
            (captured.upstream, captured.ahead, captured.behind),
            "source upstream 关系已变化，manifest stale。",
        ),
        (
            (current.dirty, current.worktree_sha256),
            (captured.dirty, captured.worktree_sha256),
            "source worktree 状态已变化，manifest stale。",
        ),
    )
    findings.extend(message for actual, expected, message in comparisons if actual != expected)
    return findings


def _license_findings(
    manifest: SourceIdentityManifest,
    root: Path,
) -> tuple[list[str], bool]:
    try:
        license_file = _safe_source_file(root, manifest.license.path)
        license_text = _read_text_bounded(license_file)
        findings = []
        if _file_sha256(
            license_file,
            max_bytes=_MAX_EVIDENCE_FILE_BYTES,
        ) != manifest.license.sha256:
            findings.append("许可证证据文件已变化，manifest stale。")
        if manifest.license.claim not in license_text:
            findings.append("许可证声明已不存在，source intake 无效。")
            return findings, True
        return findings, False
    except (OSError, UnicodeError, ValueError):
        return ["许可证证据不可读，source intake 无效。"], True


def _mapping_findings(
    manifest: SourceIdentityManifest,
    project: Path,
) -> tuple[list[str], bool]:
    mapping = (project / manifest.legacy_mapping.path).resolve()
    try:
        mapping.relative_to(project)
    except ValueError:
        return ["legacy map 路径越出项目，manifest 无效。"], True
    try:
        current_digest = _file_sha256(
            mapping,
            max_bytes=_MAX_EVIDENCE_FILE_BYTES,
        )
    except (OSError, ValueError):
        return ["legacy source map 不可读，manifest 无效。"], True
    if current_digest != manifest.legacy_mapping.sha256:
        return ["legacy source map 已变化，manifest stale。"], False
    return [], False


def load_source_identity(path: str | Path) -> SourceIdentityManifest:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return SourceIdentityManifest.model_validate(payload)


def write_source_identity(path: str | Path, manifest: SourceIdentityManifest) -> None:
    """Atomically replace a manifest after complete validation."""
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = manifest.model_dump_json(indent=2) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _safe_source_file(root: Path, relative_path: str) -> Path:
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("source evidence 路径越出 checkout。") from exc
    if not candidate.is_file():
        raise ValueError("source evidence 文件不存在。")
    return candidate


def _worktree_digest(root: Path, status: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(b"status\0")
    digest.update(status)
    digest.update(b"diff\0")
    digest.update(_git_bytes(root, "diff", "--no-ext-diff", "--binary", "HEAD"))
    untracked_count = 0
    for entry in status.split(b"\0"):
        if not entry.startswith(b"?? "):
            continue
        untracked_count += 1
        if untracked_count > _MAX_UNTRACKED_FILES:
            raise ValueError("source 未跟踪文件数量超过安全上限。")
        relative = entry[3:].decode("utf-8", errors="surrogateescape")
        candidate = _safe_source_file(root, relative)
        digest.update(b"untracked\0")
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(bytes.fromhex(_file_sha256(
            candidate,
            max_bytes=_MAX_UNTRACKED_FILE_BYTES,
        )))
    return digest.hexdigest()


def _ahead_behind(root: Path, upstream: str) -> tuple[int, int]:
    if not upstream:
        return 0, 0
    values = _git_text(root, "rev-list", "--left-right", "--count", f"HEAD...{upstream}").split()
    if len(values) != 2:
        raise ValueError("无法解析 source ahead/behind。")
    return int(values[0]), int(values[1])


def _git_text(root: Path, *args: str) -> str:
    try:
        return _git_bytes(root, *args).decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("source Git 身份不是有效 UTF-8。") from exc


def _optional_git_text(root: Path, *args: str) -> str:
    try:
        return _git_text(root, *args)
    except ValueError:
        return ""


def _git_bytes(root: Path, *args: str) -> bytes:
    try:
        process = subprocess.run(
            ["git", "-c", "core.quotepath=false", "-C", str(root), *args],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("无法读取 source Git 身份。") from exc
    if process.returncode != 0:
        raise ValueError("无法读取 source Git 身份。")
    if len(process.stdout) > _MAX_GIT_OUTPUT:
        raise ValueError("source Git 状态超过安全上限。")
    return process.stdout


def _file_sha256(path: Path, *, max_bytes: int) -> str:
    if path.stat().st_size > max_bytes:
        raise ValueError("source evidence 文件超过安全上限。")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_text_bounded(path: Path) -> str:
    if path.stat().st_size > _MAX_EVIDENCE_FILE_BYTES:
        raise ValueError("source evidence 文件超过安全上限。")
    return path.read_text(encoding="utf-8")


def _portable_manifest_path(path: Path) -> str:
    marker = ("frontend", "terminal-ui", path.name)
    if path.parts[-3:] == marker:
        return "/".join(marker)
    raise ValueError("legacy map 必须位于 frontend/terminal-ui。")


def _require_sha256(value: str | None, label: str) -> str:
    if value is None or len(value) != _SHA256_LENGTH:
        raise ValueError(f"{label} digest 必须是完整 SHA-256。")
    if any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{label} digest 必须是小写十六进制。")
    return value


def _main() -> int:
    parser = argparse.ArgumentParser(description="验证 Claude Code source identity manifest")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--project-root", default=".")
    args = parser.parse_args()
    try:
        manifest = load_source_identity(args.manifest)
        result = verify_source_identity(manifest, args.source, args.project_root)
    except (OSError, UnicodeError, ValueError):
        result = SourceAuditResult(
            status="invalid",
            findings=("source identity manifest 不可读或格式无效。",),
        )
    print(result.model_dump_json(indent=2))
    return 0 if result.status == "valid" else 1


if __name__ == "__main__":
    raise SystemExit(_main())
