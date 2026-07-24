"""Machine-verifiable behavior inventory for governed Claude source research."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.claude_source.governance import (
    SourceIdentityManifest,
    load_source_identity,
    verify_source_identity,
)
from naumi_agent.claude_source.license_scope import (
    SourceLicenseScopeManifest,
    license_scope_sha256,
    load_license_scope,
    verify_license_scope,
)
from naumi_agent.claude_source.refresh import source_identity_sha256

BehaviorArea = Literal["doctor", "permission", "task"]
BehaviorCategory = Literal[
    "cancel",
    "detail",
    "empty",
    "error",
    "focus",
    "keyboard",
    "loading",
    "presentation",
]
AlignmentDecision = Literal["aligned", "deferred", "naumi_extension"]
InventoryStatus = Literal["invalid", "stale", "valid"]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BEHAVIOR_ID_RE = re.compile(r"^(?:doctor|permission|task)\.[a-z][a-z0-9_]{0,63}$")
_SYMBOL_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]{0,127}$")
_FINDING_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_EVIDENCE_BYTES = 2 * 1024 * 1024
_FINDING_CODES = frozenset({
    "inventory_binding_mismatch",
    "license_mode_disallowed",
    "license_scope_invalid",
    "license_scope_stale",
    "source_anchor_missing",
    "source_evidence_missing",
    "source_identity_invalid",
    "source_identity_stale",
    "source_path_missing",
    "target_anchor_missing",
    "target_path_missing",
    "target_test_missing",
})


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SymbolEvidence(_StrictModel):
    path: str = Field(min_length=1, max_length=1_024)
    symbol: str = Field(min_length=1, max_length=128)

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        return _relative_path(value)

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, value: str) -> str:
        if not _SYMBOL_RE.fullmatch(value):
            raise ValueError("evidence symbol 格式无效。")
        return value


class TestEvidence(_StrictModel):
    path: str = Field(min_length=1, max_length=1_024)
    test_name: str = Field(min_length=1, max_length=300)

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        return _relative_path(value)

    @field_validator("test_name")
    @classmethod
    def _test_name(cls, value: str) -> str:
        if value != value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("test_name 必须是单行可见文本。")
        return value


class BehaviorInventoryItem(_StrictModel):
    behavior_id: str
    area: BehaviorArea
    category: BehaviorCategory
    decision: AlignmentDecision
    source_evidence: tuple[SymbolEvidence, ...] = Field(default=(), max_length=12)
    target_evidence: tuple[SymbolEvidence, ...] = Field(default=(), max_length=12)
    target_tests: tuple[TestEvidence, ...] = Field(default=(), max_length=12)
    divergence: str = Field(default="", max_length=500)

    @field_validator("behavior_id")
    @classmethod
    def _behavior_id(cls, value: str) -> str:
        if not _BEHAVIOR_ID_RE.fullmatch(value):
            raise ValueError("behavior_id 格式无效。")
        return value

    @field_validator("divergence")
    @classmethod
    def _divergence(cls, value: str) -> str:
        if value != value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("divergence 必须是单行可见文本。")
        return value

    @model_validator(mode="after")
    def _shape(self) -> BehaviorInventoryItem:
        if not self.behavior_id.startswith(f"{self.area}."):
            raise ValueError("behavior_id 必须与 area 一致。")
        for values in (self.source_evidence, self.target_evidence, self.target_tests):
            if values != tuple(sorted(
                set(values),
                key=lambda item: (
                    item.path,
                    getattr(item, "symbol", getattr(item, "test_name", "")),
                ),
            )):
                raise ValueError("evidence 必须排序且不得重复。")
        if self.decision == "aligned":
            if not self.source_evidence or not self.target_evidence or not self.target_tests:
                raise ValueError("aligned behavior 必须同时绑定 source、target 与 target test。")
        elif self.decision == "naumi_extension":
            if self.source_evidence or not self.target_evidence or not self.target_tests:
                raise ValueError("naumi_extension 只能绑定 target 与 target test。")
            if not self.divergence:
                raise ValueError("naumi_extension 必须说明差异。")
        else:
            if not self.source_evidence or self.target_evidence or self.target_tests:
                raise ValueError("deferred behavior 只能绑定 source evidence。")
            if not self.divergence:
                raise ValueError("deferred behavior 必须说明延期原因。")
        return self


class BehaviorInventoryManifest(_StrictModel):
    schema_version: Literal[1] = 1
    manifest_kind: Literal["claude_code_behavior_inventory"] = (
        "claude_code_behavior_inventory"
    )
    source_name: str = Field(min_length=1, max_length=128)
    source_commit: str
    source_identity_sha256: str
    license_scope_sha256: str
    intake_mode: Literal["reimplement"] = "reimplement"
    scope: Literal["cc_03_1a_core_flows"] = "cc_03_1a_core_flows"
    behaviors: tuple[BehaviorInventoryItem, ...] = Field(min_length=3, max_length=256)

    @field_validator("source_commit")
    @classmethod
    def _commit(cls, value: str) -> str:
        if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("source_commit 必须是完整小写 Git SHA-1。")
        return value

    @field_validator("source_identity_sha256", "license_scope_sha256")
    @classmethod
    def _digest(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("inventory digest 必须是完整小写 SHA-256。")
        return value

    @model_validator(mode="after")
    def _coverage(self) -> BehaviorInventoryManifest:
        if self.behaviors != tuple(sorted(self.behaviors, key=lambda item: item.behavior_id)):
            raise ValueError("behaviors 必须按 behavior_id 排序。")
        if len({item.behavior_id for item in self.behaviors}) != len(self.behaviors):
            raise ValueError("behavior_id 不得重复。")
        areas = {item.area for item in self.behaviors}
        if areas != {"doctor", "permission", "task"}:
            raise ValueError("核心行为清单必须覆盖 task、permission、doctor。")
        aligned_areas = {
            item.area for item in self.behaviors if item.decision == "aligned"
        }
        if aligned_areas != areas:
            raise ValueError("每个核心 area 至少需要一条 aligned 行为。")
        return self


class BehaviorInventoryAudit(_StrictModel):
    schema_version: Literal[1] = 1
    audit_id: str
    inventory_sha256: str
    source_identity_sha256: str
    license_scope_sha256: str
    status: InventoryStatus
    finding_codes: tuple[str, ...] = Field(default=(), max_length=32)
    behavior_count: int = Field(ge=0, le=256)
    aligned_count: int = Field(ge=0, le=256)
    extension_count: int = Field(ge=0, le=256)
    deferred_count: int = Field(ge=0, le=256)
    verified_source_anchor_count: int = Field(ge=0, le=3_072)
    verified_target_anchor_count: int = Field(ge=0, le=3_072)
    verified_target_test_count: int = Field(ge=0, le=3_072)

    @field_validator(
        "audit_id",
        "inventory_sha256",
        "source_identity_sha256",
        "license_scope_sha256",
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("behavior audit digest 无效。")
        return value

    @field_validator("finding_codes")
    @classmethod
    def _findings(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("finding_codes 必须排序且不得重复。")
        if any(not _FINDING_CODE_RE.fullmatch(item) for item in value):
            raise ValueError("finding code 格式无效。")
        if any(item not in _FINDING_CODES for item in value):
            raise ValueError("finding code 不在治理集合中。")
        return value

    @model_validator(mode="after")
    def _integrity(self) -> BehaviorInventoryAudit:
        if self.aligned_count + self.extension_count + self.deferred_count != self.behavior_count:
            raise ValueError("behavior decision counts 不一致。")
        if self.status == "valid" and self.finding_codes:
            raise ValueError("valid audit 不得携带 finding。")
        if self.status != "valid" and not self.finding_codes:
            raise ValueError("非 valid audit 必须携带 finding。")
        if self.audit_id != behavior_inventory_audit_sha256(self):
            raise ValueError("behavior audit digest 不一致。")
        return self


def load_behavior_inventory(path: str | Path) -> BehaviorInventoryManifest:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return BehaviorInventoryManifest.model_validate(payload)


def behavior_inventory_sha256(inventory: BehaviorInventoryManifest) -> str:
    return _sha256_json(inventory.model_dump(mode="json"))


def behavior_inventory_audit_sha256(audit: BehaviorInventoryAudit) -> str:
    payload = audit.model_dump(mode="json")
    payload.pop("audit_id", None)
    return _sha256_json(payload)


def verify_behavior_inventory(
    inventory: BehaviorInventoryManifest,
    identity: SourceIdentityManifest,
    license_scope: SourceLicenseScopeManifest,
    *,
    source_root: str | Path,
    project_root: str | Path,
) -> BehaviorInventoryAudit:
    """Verify governed source symbols and independently implemented target evidence."""
    source = Path(source_root).expanduser().resolve()
    project = Path(project_root).expanduser().resolve()
    findings: set[str] = set()
    source_count = 0
    target_count = 0
    test_count = 0

    identity_audit = verify_source_identity(identity, source, project)
    if identity_audit.status != "valid":
        findings.add(
            "source_identity_stale"
            if identity_audit.status == "stale"
            else "source_identity_invalid"
        )
        return _audit(inventory, identity, license_scope, findings, 0, 0, 0)

    scope_audit = verify_license_scope(
        license_scope,
        identity,
        source_root=source,
        project_root=project,
    )
    if scope_audit.status != "valid":
        findings.add(
            "license_scope_stale"
            if scope_audit.status == "stale"
            else "license_scope_invalid"
        )
        return _audit(inventory, identity, license_scope, findings, 0, 0, 0)

    expected = (
        inventory.source_name == identity.source_name
        and inventory.source_commit == identity.git.commit
        and inventory.source_identity_sha256 == source_identity_sha256(identity)
        and inventory.license_scope_sha256 == license_scope_sha256(license_scope)
    )
    if not expected:
        findings.add("inventory_binding_mismatch")
        return _audit(inventory, identity, license_scope, findings, 0, 0, 0)

    for behavior in inventory.behaviors:
        for evidence in behavior.source_evidence:
            path = _safe_file(source, evidence.path)
            if path is None:
                findings.add("source_path_missing")
                continue
            if not _mode_allowed(license_scope, evidence.path, inventory.intake_mode):
                findings.add("license_mode_disallowed")
                continue
            if not _declares_symbol(path, evidence.symbol):
                findings.add("source_anchor_missing")
                continue
            source_count += 1
        for evidence in behavior.target_evidence:
            path = _safe_file(project, evidence.path)
            if path is None:
                findings.add("target_path_missing")
                continue
            if not _declares_symbol(path, evidence.symbol):
                findings.add("target_anchor_missing")
                continue
            target_count += 1
        for evidence in behavior.target_tests:
            path = _safe_file(project, evidence.path)
            if path is None:
                findings.add("target_path_missing")
                continue
            if not _declares_test(path, evidence.test_name):
                findings.add("target_test_missing")
                continue
            test_count += 1

    expected_source = sum(len(item.source_evidence) for item in inventory.behaviors)
    if source_count != expected_source and "source_path_missing" not in findings:
        findings.add("source_evidence_missing")
    return _audit(
        inventory,
        identity,
        license_scope,
        findings,
        source_count,
        target_count,
        test_count,
    )


def _audit(
    inventory: BehaviorInventoryManifest,
    identity: SourceIdentityManifest,
    license_scope: SourceLicenseScopeManifest,
    findings: set[str],
    source_count: int,
    target_count: int,
    test_count: int,
) -> BehaviorInventoryAudit:
    decisions = [item.decision for item in inventory.behaviors]
    status: InventoryStatus
    if findings & {"inventory_binding_mismatch", "license_scope_stale", "source_identity_stale"}:
        status = "stale"
    else:
        status = "invalid" if findings else "valid"
    values = {
        "schema_version": 1,
        "inventory_sha256": behavior_inventory_sha256(inventory),
        "source_identity_sha256": source_identity_sha256(identity),
        "license_scope_sha256": license_scope_sha256(license_scope),
        "status": status,
        "finding_codes": tuple(sorted(findings)),
        "behavior_count": len(inventory.behaviors),
        "aligned_count": decisions.count("aligned"),
        "extension_count": decisions.count("naumi_extension"),
        "deferred_count": decisions.count("deferred"),
        "verified_source_anchor_count": source_count,
        "verified_target_anchor_count": target_count,
        "verified_target_test_count": test_count,
    }
    return BehaviorInventoryAudit(audit_id=_sha256_json(values), **values)


def _mode_allowed(
    scope: SourceLicenseScopeManifest,
    path: str,
    mode: str,
) -> bool:
    if any(_within(path, item.path_prefix) for item in scope.exclusions):
        return False
    matches = [rule for rule in scope.rules if _within(path, rule.path_prefix)]
    if not matches:
        return False
    rule = max(matches, key=lambda item: len(PurePosixPath(item.path_prefix).parts))
    return mode in rule.allowed_modes


def _within(path: str, prefix: str) -> bool:
    value = PurePosixPath(path).parts
    boundary = PurePosixPath(prefix).parts
    return value[: len(boundary)] == boundary


def _safe_file(root: Path, relative: str) -> Path | None:
    candidate = root.joinpath(*PurePosixPath(relative).parts)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    if not resolved.is_file() or resolved.stat().st_size > _MAX_EVIDENCE_BYTES:
        return None
    return resolved


def _declares_symbol(path: Path, symbol: str) -> bool:
    text = path.read_text(encoding="utf-8")
    escaped = re.escape(symbol)
    patterns = (
        rf"\b(?:export\s+)?(?:async\s+)?(?:function|class|const|let|var)\s+{escaped}\b",
        rf"(?m)^\s*(?:async\s+def|def|class)\s+{escaped}\b",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def _declares_test(path: Path, test_name: str) -> bool:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".py":
        return bool(re.search(rf"(?m)^\s*(?:async\s+def|def)\s+{re.escape(test_name)}\b", text))
    quoted = re.escape(test_name)
    return bool(re.search(rf"\b(?:test|it)\(\s*([\"']){quoted}\1\s*,", text))


def _relative_path(value: str) -> str:
    if "\\" in value:
        raise ValueError("evidence path 必须使用 POSIX 分隔符。")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("evidence path 必须是安全相对路径。")
    return path.as_posix()


def _sha256_json(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验 CC-03.1a 行为清单。")
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--license-scope", required=True)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--project-root", default=".")
    arguments = parser.parse_args(argv)
    audit = verify_behavior_inventory(
        load_behavior_inventory(arguments.inventory),
        load_source_identity(arguments.identity),
        load_license_scope(arguments.license_scope),
        source_root=arguments.source_root,
        project_root=arguments.project_root,
    )
    print(audit.model_dump_json(indent=2))
    return 0 if audit.status == "valid" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BehaviorInventoryAudit",
    "BehaviorInventoryItem",
    "BehaviorInventoryManifest",
    "SymbolEvidence",
    "TestEvidence",
    "behavior_inventory_audit_sha256",
    "behavior_inventory_sha256",
    "load_behavior_inventory",
    "verify_behavior_inventory",
]
