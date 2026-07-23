"""Deterministic license-scope boundary for governed source intake."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.claude_source.governance import (
    SourceIdentityManifest,
    load_source_identity,
    verify_source_identity,
)
from naumi_agent.claude_source.refresh import source_identity_sha256

IntakeMode = Literal["adapt", "copy", "reference", "reimplement"]
LicenseScopeStatus = Literal["valid", "stale", "invalid"]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ACTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$")
_MAX_MAPPING_BYTES = 4 * 1024 * 1024
_MAX_MAPPING_AREAS = 256
_MAX_PATHS_PER_AREA = 256
_MAX_MAPPED_PATHS = 2_048
_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SECRET_MARKERS = re.compile(
    r"(?i)(?:bearer\s+[a-z0-9._~+/-]{12,}|(?:api[_-]?key|token|password)\s*[:=])"
)
_FINDING_CODES = frozenset({
    "legacy_mapping_invalid",
    "license_claim_binding_mismatch",
    "license_digest_binding_mismatch",
    "license_path_binding_mismatch",
    "mapped_source_empty",
    "mapped_source_excluded",
    "mapped_source_missing",
    "mapped_source_unmatched",
    "scope_evidence_path_invalid",
    "source_commit_binding_mismatch",
    "source_identity_binding_mismatch",
    "source_identity_invalid",
    "source_identity_stale",
    "source_name_binding_mismatch",
})


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LicenseScopeRule(_StrictModel):
    path_prefix: str = Field(min_length=1, max_length=1_024)
    allowed_modes: tuple[IntakeMode, ...] = Field(min_length=1, max_length=4)
    notice_requirement: Literal["unknown", "required", "not_required"]
    rationale: str = Field(min_length=1, max_length=500)

    @field_validator("path_prefix")
    @classmethod
    def _path(cls, value: str) -> str:
        return _normalized_relative_path(value, "scope rule")

    @field_validator("rationale")
    @classmethod
    def _rationale(cls, value: str) -> str:
        return _safe_public_text(value, "scope rationale")

    @model_validator(mode="after")
    def _modes_are_canonical(self) -> LicenseScopeRule:
        if self.allowed_modes != tuple(sorted(set(self.allowed_modes))):
            raise ValueError("allowed_modes 必须排序且不得重复。")
        return self


class LicenseScopeExclusion(_StrictModel):
    path_prefix: str = Field(min_length=1, max_length=1_024)
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("path_prefix")
    @classmethod
    def _path(cls, value: str) -> str:
        return _normalized_relative_path(value, "scope exclusion")

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        return _safe_public_text(value, "scope exclusion reason")


class SourceLicenseScopeManifest(_StrictModel):
    schema_version: Literal[1] = 1
    manifest_kind: Literal["source_license_scope"] = "source_license_scope"
    source_name: str = Field(min_length=1, max_length=128)
    source_commit: str
    source_identity_sha256: str
    license_path: str = Field(min_length=1, max_length=1_024)
    license_sha256: str
    license_claim_sha256: str
    license_expression: str = Field(min_length=1, max_length=128)
    review_status: Literal["approved", "restricted_pending_legal_review"]
    redistribution_status: Literal["allowed", "prohibited", "unreviewed"]
    assessed_by: str
    assessed_at: str
    assessment_reason: str = Field(min_length=1, max_length=1_000)
    legal_review_required: bool
    notice_path: str = Field(default="", max_length=1_024)
    rules: tuple[LicenseScopeRule, ...] = Field(min_length=1, max_length=256)
    exclusions: tuple[LicenseScopeExclusion, ...] = Field(default=(), max_length=256)
    unmatched_policy: Literal["reject"] = "reject"

    @field_validator("source_commit")
    @classmethod
    def _commit(cls, value: str) -> str:
        if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("source_commit 必须是完整小写 Git SHA-1。")
        return value

    @field_validator(
        "source_identity_sha256",
        "license_sha256",
        "license_claim_sha256",
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("license scope digest 必须是完整小写 SHA-256。")
        return value

    @field_validator("license_path")
    @classmethod
    def _license_path(cls, value: str) -> str:
        return _normalized_relative_path(value, "license evidence")

    @field_validator("notice_path")
    @classmethod
    def _notice_path(cls, value: str) -> str:
        return _normalized_relative_path(value, "notice") if value else ""

    @field_validator("assessed_by")
    @classmethod
    def _actor(cls, value: str) -> str:
        if not _ACTOR_RE.fullmatch(value):
            raise ValueError("assessed_by 格式无效。")
        return value

    @field_validator("assessed_at")
    @classmethod
    def _timestamp(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("assessed_at 必须包含时区。")
        return value

    @field_validator("license_expression", "assessment_reason")
    @classmethod
    def _public_text(cls, value: str) -> str:
        return _safe_public_text(value, "license scope text")

    @model_validator(mode="after")
    def _policy_is_fail_closed(self) -> SourceLicenseScopeManifest:
        prefixes = tuple(rule.path_prefix for rule in self.rules)
        if prefixes != tuple(sorted(set(prefixes))):
            raise ValueError("scope rules 必须按 path_prefix 排序且不得重复。")
        exclusions = tuple(item.path_prefix for item in self.exclusions)
        if exclusions != tuple(sorted(set(exclusions))):
            raise ValueError("scope exclusions 必须排序且不得重复。")
        modes = {mode for rule in self.rules for mode in rule.allowed_modes}
        if self.review_status == "restricted_pending_legal_review":
            if modes - {"reference", "reimplement"}:
                raise ValueError("待法律复核 scope 只能允许 reference/reimplement。")
            if self.redistribution_status == "allowed":
                raise ValueError("待法律复核 scope 不得声明允许再分发。")
            if not self.legal_review_required:
                raise ValueError("待法律复核 scope 必须保留 legal_review_required。")
        if modes & {"copy", "adapt"} and self.redistribution_status != "allowed":
            raise ValueError("copy/adapt 必须有明确再分发许可。")
        if any(rule.notice_requirement == "required" for rule in self.rules):
            if not self.notice_path:
                raise ValueError("需要 notice 的 scope 必须提供 notice_path。")
        return self


class LicenseMappedPathDecision(_StrictModel):
    path: str = Field(min_length=1, max_length=1_024)
    status: Literal["covered", "excluded", "missing", "unmatched"]
    rule_prefix: str = Field(default="", max_length=1_024)
    allowed_modes: tuple[IntakeMode, ...] = Field(default=(), max_length=4)

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        return _normalized_relative_path(value, "mapped decision")

    @field_validator("rule_prefix")
    @classmethod
    def _rule_prefix(cls, value: str) -> str:
        return _normalized_relative_path(value, "decision rule") if value else ""

    @model_validator(mode="after")
    def _decision_is_consistent(self) -> LicenseMappedPathDecision:
        if self.allowed_modes != tuple(sorted(set(self.allowed_modes))):
            raise ValueError("decision allowed_modes 必须排序且不得重复。")
        if self.status == "covered":
            if not self.rule_prefix or not self.allowed_modes:
                raise ValueError("covered decision 必须绑定 rule 与 allowed_modes。")
        elif self.allowed_modes:
            raise ValueError("blocked decision 不得声明 allowed_modes。")
        return self


class SourceLicenseScopeAudit(_StrictModel):
    schema_version: Literal[1] = 1
    audit_id: str
    scope_sha256: str
    source_identity_sha256: str
    status: LicenseScopeStatus
    review_status: Literal["approved", "restricted_pending_legal_review"]
    finding_codes: tuple[str, ...] = Field(default=(), max_length=64)
    mapped_path_count: int = Field(ge=0, le=_MAX_MAPPED_PATHS)
    covered_count: int = Field(ge=0, le=_MAX_MAPPED_PATHS)
    blocked_count: int = Field(ge=0, le=_MAX_MAPPED_PATHS)
    common_allowed_modes: tuple[IntakeMode, ...] = Field(default=(), max_length=4)
    decisions: tuple[LicenseMappedPathDecision, ...] = Field(
        default=(),
        max_length=_MAX_MAPPED_PATHS,
    )

    @field_validator("audit_id", "scope_sha256", "source_identity_sha256")
    @classmethod
    def _digest(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("license scope audit digest 无效。")
        return value

    @field_validator("finding_codes")
    @classmethod
    def _finding_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _CODE_RE.fullmatch(item) for item in value):
            raise ValueError("finding code 格式无效。")
        if any(item not in _FINDING_CODES for item in value):
            raise ValueError("finding code 不在已治理集合中。")
        return value

    @model_validator(mode="after")
    def _integrity(self) -> SourceLicenseScopeAudit:
        if self.finding_codes != tuple(sorted(set(self.finding_codes))):
            raise ValueError("finding_codes 必须排序且不得重复。")
        if self.decisions != tuple(sorted(self.decisions, key=lambda item: item.path)):
            raise ValueError("decisions 必须按 path 排序。")
        if len({item.path for item in self.decisions}) != len(self.decisions):
            raise ValueError("decisions path 不得重复。")
        if self.mapped_path_count != len(self.decisions):
            raise ValueError("mapped_path_count 与 decisions 不一致。")
        covered = sum(item.status == "covered" for item in self.decisions)
        if self.covered_count != covered:
            raise ValueError("covered_count 与 decisions 不一致。")
        if self.blocked_count != self.mapped_path_count - covered:
            raise ValueError("blocked_count 与 decisions 不一致。")
        if self.common_allowed_modes != tuple(sorted(set(self.common_allowed_modes))):
            raise ValueError("common_allowed_modes 必须排序且不得重复。")
        if self.status == "valid":
            if self.finding_codes or self.blocked_count:
                raise ValueError("valid audit 不得携带 finding 或 blocked path。")
            if self.mapped_path_count and not self.common_allowed_modes:
                raise ValueError("valid audit 必须保留共同允许模式。")
        elif not self.finding_codes:
            raise ValueError("非 valid audit 必须携带 finding code。")
        if self.status == "stale" and self.decisions:
            raise ValueError("stale audit 不得继续声称 path coverage。")
        if self.audit_id != license_scope_audit_sha256(self):
            raise ValueError("license scope audit digest 不一致。")
        return self


def verify_license_scope(
    scope: SourceLicenseScopeManifest,
    identity: SourceIdentityManifest,
    *,
    source_root: str | Path,
    project_root: str | Path,
) -> SourceLicenseScopeAudit:
    """Verify scope binding and every legacy mapped source path without copying text."""
    root = Path(source_root).expanduser().resolve()
    project = Path(project_root).expanduser().resolve()
    scope_digest = license_scope_sha256(scope)
    identity_digest = source_identity_sha256(identity)
    findings: set[str] = set()

    identity_audit = verify_source_identity(identity, root, project)
    if identity_audit.status == "invalid":
        findings.add("source_identity_invalid")
        return _build_audit(scope, scope_digest, identity_digest, "invalid", findings, ())
    if identity_audit.status == "stale":
        findings.add("source_identity_stale")
        return _build_audit(scope, scope_digest, identity_digest, "stale", findings, ())

    binding_values = (
        (scope.source_name, identity.source_name, "source_name_binding_mismatch"),
        (scope.source_commit, identity.git.commit, "source_commit_binding_mismatch"),
        (
            scope.source_identity_sha256,
            identity_digest,
            "source_identity_binding_mismatch",
        ),
        (scope.license_path, identity.license.path, "license_path_binding_mismatch"),
        (scope.license_sha256, identity.license.sha256, "license_digest_binding_mismatch"),
        (
            scope.license_claim_sha256,
            _sha256_text(identity.license.claim),
            "license_claim_binding_mismatch",
        ),
    )
    findings.update(code for actual, expected, code in binding_values if actual != expected)
    if findings:
        return _build_audit(scope, scope_digest, identity_digest, "stale", findings, ())

    if not _scope_evidence_paths_are_safe(scope, root):
        findings.add("scope_evidence_path_invalid")
        return _build_audit(scope, scope_digest, identity_digest, "invalid", findings, ())

    try:
        mapped_paths = _load_legacy_mapped_paths(identity, project)
    except (OSError, UnicodeError, ValueError):
        findings.add("legacy_mapping_invalid")
        return _build_audit(scope, scope_digest, identity_digest, "invalid", findings, ())
    if not mapped_paths:
        findings.add("mapped_source_empty")
        return _build_audit(scope, scope_digest, identity_digest, "invalid", findings, ())

    decisions = tuple(
        _mapped_path_decision(path, scope=scope, root=root)
        for path in mapped_paths
    )
    statuses = {item.status for item in decisions}
    if "missing" in statuses:
        findings.add("mapped_source_missing")
    if "excluded" in statuses:
        findings.add("mapped_source_excluded")
    if "unmatched" in statuses:
        findings.add("mapped_source_unmatched")
    status: LicenseScopeStatus = "invalid" if findings else "valid"
    return _build_audit(
        scope,
        scope_digest,
        identity_digest,
        status,
        findings,
        decisions,
    )


def load_license_scope(path: str | Path) -> SourceLicenseScopeManifest:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return SourceLicenseScopeManifest.model_validate(payload)


def write_license_scope(path: str | Path, scope: SourceLicenseScopeManifest) -> None:
    """Atomically write a fully validated scope manifest."""
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = scope.model_dump_json(indent=2) + "\n"
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


def license_scope_sha256(scope: SourceLicenseScopeManifest) -> str:
    return _sha256_json(scope.model_dump(mode="json"))


def license_scope_audit_sha256(audit: SourceLicenseScopeAudit) -> str:
    payload = audit.model_dump(mode="json")
    payload.pop("audit_id", None)
    return _sha256_json(payload)


def _build_audit(
    scope: SourceLicenseScopeManifest,
    scope_digest: str,
    identity_digest: str,
    status: LicenseScopeStatus,
    findings: set[str],
    decisions: tuple[LicenseMappedPathDecision, ...],
) -> SourceLicenseScopeAudit:
    ordered_decisions = tuple(sorted(decisions, key=lambda item: item.path))
    covered = [item for item in ordered_decisions if item.status == "covered"]
    common_modes: set[IntakeMode] = set(covered[0].allowed_modes) if covered else set()
    for item in covered[1:]:
        common_modes.intersection_update(item.allowed_modes)
    values = {
        "schema_version": 1,
        "scope_sha256": scope_digest,
        "source_identity_sha256": identity_digest,
        "status": status,
        "review_status": scope.review_status,
        "finding_codes": tuple(sorted(findings)),
        "mapped_path_count": len(ordered_decisions),
        "covered_count": len(covered),
        "blocked_count": len(ordered_decisions) - len(covered),
        "common_allowed_modes": tuple(sorted(common_modes)),
        "decisions": ordered_decisions,
    }
    return SourceLicenseScopeAudit(audit_id=_sha256_json(values), **values)


def _mapped_path_decision(
    path: str,
    *,
    scope: SourceLicenseScopeManifest,
    root: Path,
) -> LicenseMappedPathDecision:
    candidate = _safe_existing_source_path(root, path)
    if candidate is None or not candidate.is_file():
        return LicenseMappedPathDecision(path=path, status="missing")
    exclusions = [
        item for item in scope.exclusions if _path_is_within_prefix(path, item.path_prefix)
    ]
    if exclusions:
        return LicenseMappedPathDecision(
            path=path,
            status="excluded",
            rule_prefix=max(exclusions, key=lambda item: len(item.path_prefix)).path_prefix,
        )
    rules = [rule for rule in scope.rules if _path_is_within_prefix(path, rule.path_prefix)]
    if not rules:
        return LicenseMappedPathDecision(path=path, status="unmatched")
    rule = max(rules, key=lambda item: len(item.path_prefix))
    return LicenseMappedPathDecision(
        path=path,
        status="covered",
        rule_prefix=rule.path_prefix,
        allowed_modes=rule.allowed_modes,
    )


def _scope_evidence_paths_are_safe(scope: SourceLicenseScopeManifest, root: Path) -> bool:
    license_path = _safe_existing_source_path(root, scope.license_path)
    if license_path is None or not license_path.is_file():
        return False
    paths = [rule.path_prefix for rule in scope.rules]
    paths.extend(item.path_prefix for item in scope.exclusions)
    if any(_safe_existing_source_path(root, path) is None for path in paths):
        return False
    if scope.notice_path:
        notice = _safe_existing_source_path(root, scope.notice_path)
        if notice is None or not notice.is_file():
            return False
    return True


def _load_legacy_mapped_paths(
    identity: SourceIdentityManifest,
    project: Path,
) -> tuple[str, ...]:
    mapping = (project / identity.legacy_mapping.path).resolve()
    try:
        mapping.relative_to(project)
    except ValueError as exc:
        raise ValueError("legacy mapping 路径越出项目。") from exc
    if not mapping.is_file() or mapping.stat().st_size > _MAX_MAPPING_BYTES:
        raise ValueError("legacy mapping 不存在或超过安全上限。")
    payload = json.loads(mapping.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != {"source", "mapping"}:
        raise ValueError("legacy mapping 顶层结构无效。")
    areas = payload.get("mapping")
    if not isinstance(areas, list) or len(areas) > _MAX_MAPPING_AREAS:
        raise ValueError("legacy mapping area 数量无效。")
    paths: set[str] = set()
    for area in areas:
        if not isinstance(area, dict):
            raise ValueError("legacy mapping area 必须是对象。")
        source_paths = area.get("claude_code")
        if not isinstance(source_paths, list) or len(source_paths) > _MAX_PATHS_PER_AREA:
            raise ValueError("legacy mapping source paths 无效。")
        for raw_path in source_paths:
            if not isinstance(raw_path, str):
                raise ValueError("legacy mapping source path 必须是字符串。")
            paths.add(_normalized_relative_path(raw_path, "mapped source"))
            if len(paths) > _MAX_MAPPED_PATHS:
                raise ValueError("legacy mapping source path 数量超过安全上限。")
    return tuple(sorted(paths))


def _safe_existing_source_path(root: Path, relative_path: str) -> Path | None:
    try:
        candidate = (root / relative_path).resolve()
        candidate.relative_to(root)
        return candidate if candidate.exists() else None
    except (OSError, RuntimeError, ValueError):
        return None


def _path_is_within_prefix(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _normalized_relative_path(value: str, label: str) -> str:
    if any(character in value for character in ("\\", "\x00", "\n", "\r")):
        raise ValueError(f"{label} 路径含非法字符。")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} 必须是规范化相对路径。")
    normalized = path.as_posix()
    if normalized != value:
        raise ValueError(f"{label} 必须是规范化相对路径。")
    return normalized


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_public_text(value: str, label: str) -> str:
    normalized = value.strip()
    if normalized != value or any(
        character in value for character in ("\x00", "\n", "\r")
    ):
        raise ValueError(f"{label} 含非法空白或控制字符。")
    if _SECRET_MARKERS.search(value):
        raise ValueError(f"{label} 疑似包含 secret。")
    return value


def _sha256_json(payload: object) -> str:
    encoded = json.dumps(
        _jsonable(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _jsonable(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    return value


def _main() -> int:
    parser = argparse.ArgumentParser(description="验证 source 许可证范围合同")
    parser.add_argument("--scope", required=True)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--project-root", default=".")
    args = parser.parse_args()
    try:
        scope = load_license_scope(args.scope)
        identity = load_source_identity(args.identity)
        audit = verify_license_scope(
            scope,
            identity,
            source_root=args.source,
            project_root=args.project_root,
        )
    except (OSError, UnicodeError, ValueError):
        print(json.dumps({
            "schema_version": 1,
            "status": "invalid",
            "finding_codes": ["license_scope_input_invalid"],
        }, ensure_ascii=False, indent=2))
        return 1
    print(audit.model_dump_json(indent=2))
    return 0 if audit.status == "valid" else 1


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = [
    "LicenseMappedPathDecision",
    "LicenseScopeExclusion",
    "LicenseScopeRule",
    "SourceLicenseScopeAudit",
    "SourceLicenseScopeManifest",
    "license_scope_audit_sha256",
    "license_scope_sha256",
    "load_license_scope",
    "verify_license_scope",
    "write_license_scope",
]
