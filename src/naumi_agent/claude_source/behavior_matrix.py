"""Complete, machine-verifiable CC-03.1b behavior matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.claude_source.behavior_inventory import (
    BehaviorInventoryItem,
    BehaviorInventoryManifest,
    SymbolEvidence,
    TestEvidence,
    behavior_inventory_sha256,
    load_behavior_inventory,
    verify_behavior_inventory,
)
from naumi_agent.claude_source.governance import (
    SourceIdentityManifest,
    load_source_identity,
)
from naumi_agent.claude_source.license_scope import (
    SourceLicenseScopeManifest,
    license_scope_sha256,
    load_license_scope,
)
from naumi_agent.claude_source.refresh import source_identity_sha256

MatrixArea = Literal["doctor", "permission", "task"]
MatrixDimension = Literal[
    "cancel",
    "detail",
    "empty",
    "error",
    "focus",
    "keyboard",
    "loading",
    "presentation",
]
MatrixDisposition = Literal[
    "aligned",
    "deferred",
    "naumi_extension",
    "not_applicable",
]
MatrixStatus = Literal["invalid", "stale", "valid"]

MATRIX_AREAS = ("doctor", "permission", "task")
MATRIX_DIMENSIONS = (
    "cancel",
    "detail",
    "empty",
    "error",
    "focus",
    "keyboard",
    "loading",
    "presentation",
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CELL_ID_RE = re.compile(
    r"^(?:doctor|permission|task)\."
    r"(?:cancel|detail|empty|error|focus|keyboard|loading|presentation)$"
)
_FINDING_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_BASE_FINDING_CODES = frozenset({
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
_FINDING_CODES = _BASE_FINDING_CODES | frozenset({
    "core_inventory_binding_mismatch",
    "core_inventory_invalid",
    "core_inventory_stale",
    "matrix_binding_mismatch",
})


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _visible_line(value: str, *, label: str) -> str:
    if value != value.strip() or any(ord(char) < 32 for char in value):
        raise ValueError(f"{label} 必须是单行可见文本。")
    return value


class BehaviorMatrixCell(_StrictModel):
    cell_id: str
    area: MatrixArea
    dimension: MatrixDimension
    disposition: MatrixDisposition
    behavior: str = Field(min_length=4, max_length=300)
    source_evidence: tuple[SymbolEvidence, ...] = Field(default=(), max_length=12)
    target_evidence: tuple[SymbolEvidence, ...] = Field(default=(), max_length=12)
    target_tests: tuple[TestEvidence, ...] = Field(default=(), max_length=12)
    rationale: str = Field(default="", max_length=500)

    @field_validator("cell_id")
    @classmethod
    def _cell_id(cls, value: str) -> str:
        if not _CELL_ID_RE.fullmatch(value):
            raise ValueError("matrix cell_id 格式无效。")
        return value

    @field_validator("behavior")
    @classmethod
    def _behavior(cls, value: str) -> str:
        return _visible_line(value, label="behavior")

    @field_validator("rationale")
    @classmethod
    def _rationale(cls, value: str) -> str:
        return _visible_line(value, label="rationale")

    @model_validator(mode="after")
    def _shape(self) -> BehaviorMatrixCell:
        if self.cell_id != f"{self.area}.{self.dimension}":
            raise ValueError("cell_id 必须精确等于 area.dimension。")
        for values in (self.source_evidence, self.target_evidence, self.target_tests):
            if values != tuple(sorted(
                set(values),
                key=lambda item: (
                    item.path,
                    getattr(item, "symbol", getattr(item, "test_name", "")),
                ),
            )):
                raise ValueError("matrix evidence 必须排序且不得重复。")
        if self.disposition == "aligned":
            if not self.source_evidence or not self.target_evidence or not self.target_tests:
                raise ValueError("aligned cell 必须绑定 source、target 与 target test。")
            if self.rationale:
                raise ValueError("aligned cell 不得携带差异说明。")
        elif self.disposition == "naumi_extension":
            if self.source_evidence or not self.target_evidence or not self.target_tests:
                raise ValueError("naumi_extension cell 只能绑定 target 与 target test。")
            if not self.rationale:
                raise ValueError("naumi_extension cell 必须说明差异。")
        elif self.disposition == "deferred":
            if not self.source_evidence or self.target_evidence or self.target_tests:
                raise ValueError("deferred cell 只能绑定 source evidence。")
            if not self.rationale:
                raise ValueError("deferred cell 必须说明延期边界。")
        else:
            if self.source_evidence or self.target_evidence or self.target_tests:
                raise ValueError("not_applicable cell 不得绑定伪证据。")
            if not self.rationale:
                raise ValueError("not_applicable cell 必须说明产品边界。")
        return self


class BehaviorMatrixManifest(_StrictModel):
    schema_version: Literal[1] = 1
    manifest_kind: Literal["claude_code_behavior_matrix"] = (
        "claude_code_behavior_matrix"
    )
    source_name: str = Field(min_length=1, max_length=128)
    source_commit: str
    source_identity_sha256: str
    license_scope_sha256: str
    core_inventory_sha256: str
    intake_mode: Literal["reimplement"] = "reimplement"
    scope: Literal["cc_03_1b_complete_matrix"] = "cc_03_1b_complete_matrix"
    cells: tuple[BehaviorMatrixCell, ...] = Field(min_length=24, max_length=24)

    @field_validator("source_commit")
    @classmethod
    def _commit(cls, value: str) -> str:
        if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("source_commit 必须是完整小写 Git SHA-1。")
        return value

    @field_validator(
        "source_identity_sha256",
        "license_scope_sha256",
        "core_inventory_sha256",
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("matrix digest 必须是完整小写 SHA-256。")
        return value

    @model_validator(mode="after")
    def _coverage(self) -> BehaviorMatrixManifest:
        if self.cells != tuple(sorted(self.cells, key=lambda item: item.cell_id)):
            raise ValueError("matrix cells 必须按 cell_id 排序。")
        expected = {
            f"{area}.{dimension}"
            for area in MATRIX_AREAS
            for dimension in MATRIX_DIMENSIONS
        }
        actual = {item.cell_id for item in self.cells}
        if len(actual) != len(self.cells) or actual != expected:
            raise ValueError("matrix 必须精确覆盖 3×8 的全部 24 个单元格。")
        aligned_areas = {
            item.area for item in self.cells if item.disposition == "aligned"
        }
        if aligned_areas != set(MATRIX_AREAS):
            raise ValueError("每个 area 至少需要一个 source→target aligned cell。")
        return self


class BehaviorMatrixAudit(_StrictModel):
    schema_version: Literal[1] = 1
    audit_id: str
    matrix_sha256: str
    core_inventory_sha256: str
    source_identity_sha256: str
    license_scope_sha256: str
    status: MatrixStatus
    finding_codes: tuple[str, ...] = Field(default=(), max_length=32)
    cell_count: int = Field(ge=0, le=24)
    aligned_count: int = Field(ge=0, le=24)
    extension_count: int = Field(ge=0, le=24)
    deferred_count: int = Field(ge=0, le=24)
    not_applicable_count: int = Field(ge=0, le=24)
    verified_source_anchor_count: int = Field(ge=0, le=288)
    verified_target_anchor_count: int = Field(ge=0, le=288)
    verified_target_test_count: int = Field(ge=0, le=288)

    @field_validator(
        "audit_id",
        "matrix_sha256",
        "core_inventory_sha256",
        "source_identity_sha256",
        "license_scope_sha256",
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("matrix audit digest 无效。")
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
    def _integrity(self) -> BehaviorMatrixAudit:
        total = (
            self.aligned_count
            + self.extension_count
            + self.deferred_count
            + self.not_applicable_count
        )
        if total != self.cell_count:
            raise ValueError("matrix disposition counts 不一致。")
        if self.status == "valid" and self.finding_codes:
            raise ValueError("valid matrix audit 不得携带 finding。")
        if self.status != "valid" and not self.finding_codes:
            raise ValueError("非 valid matrix audit 必须携带 finding。")
        if self.audit_id != behavior_matrix_audit_sha256(self):
            raise ValueError("matrix audit digest 不一致。")
        return self


def load_behavior_matrix(path: str | Path) -> BehaviorMatrixManifest:
    return BehaviorMatrixManifest.model_validate_json(Path(path).read_text(encoding="utf-8"))


def behavior_matrix_sha256(matrix: BehaviorMatrixManifest) -> str:
    return _sha256_json(matrix.model_dump(mode="json"))


def behavior_matrix_audit_sha256(audit: BehaviorMatrixAudit) -> str:
    payload = audit.model_dump(mode="json")
    payload.pop("audit_id", None)
    return _sha256_json(payload)


def verify_behavior_matrix(
    matrix: BehaviorMatrixManifest,
    core_inventory: BehaviorInventoryManifest,
    identity: SourceIdentityManifest,
    license_scope: SourceLicenseScopeManifest,
    *,
    source_root: str | Path,
    project_root: str | Path,
) -> BehaviorMatrixAudit:
    """Verify exact matrix coverage and all governed evidence without source copying."""
    findings: set[str] = set()
    source_count = 0
    target_count = 0
    test_count = 0

    core_audit = verify_behavior_inventory(
        core_inventory,
        identity,
        license_scope,
        source_root=source_root,
        project_root=project_root,
    )
    if core_audit.status != "valid":
        findings.add(
            "core_inventory_stale"
            if core_audit.status == "stale"
            else "core_inventory_invalid"
        )
    actual_core_digest = behavior_inventory_sha256(core_inventory)
    if matrix.core_inventory_sha256 != actual_core_digest:
        findings.add("core_inventory_binding_mismatch")

    matrix_binding = (
        matrix.source_name == core_inventory.source_name == identity.source_name
        and matrix.source_commit == core_inventory.source_commit == identity.git.commit
        and matrix.source_identity_sha256
        == core_inventory.source_identity_sha256
        == source_identity_sha256(identity)
        and matrix.license_scope_sha256
        == core_inventory.license_scope_sha256
        == license_scope_sha256(license_scope)
    )
    if not matrix_binding:
        findings.add("matrix_binding_mismatch")

    evidence_cells = tuple(
        BehaviorInventoryItem(
            behavior_id=item.cell_id,
            area=item.area,
            category=item.dimension,
            decision=item.disposition,
            source_evidence=item.source_evidence,
            target_evidence=item.target_evidence,
            target_tests=item.target_tests,
            divergence=item.rationale,
        )
        for item in matrix.cells
        if item.disposition != "not_applicable"
    )
    evidence_inventory = BehaviorInventoryManifest(
        source_name=matrix.source_name,
        source_commit=matrix.source_commit,
        source_identity_sha256=matrix.source_identity_sha256,
        license_scope_sha256=matrix.license_scope_sha256,
        behaviors=evidence_cells,
    )
    evidence_audit = verify_behavior_inventory(
        evidence_inventory,
        identity,
        license_scope,
        source_root=source_root,
        project_root=project_root,
    )
    findings.update(evidence_audit.finding_codes)
    source_count = evidence_audit.verified_source_anchor_count
    target_count = evidence_audit.verified_target_anchor_count
    test_count = evidence_audit.verified_target_test_count

    stale_findings = {
        "core_inventory_binding_mismatch",
        "core_inventory_stale",
        "inventory_binding_mismatch",
        "license_scope_stale",
        "matrix_binding_mismatch",
        "source_identity_stale",
    }
    status: MatrixStatus = (
        "stale" if findings & stale_findings else "invalid" if findings else "valid"
    )
    dispositions = [item.disposition for item in matrix.cells]
    values = {
        "schema_version": 1,
        "matrix_sha256": behavior_matrix_sha256(matrix),
        "core_inventory_sha256": actual_core_digest,
        "source_identity_sha256": source_identity_sha256(identity),
        "license_scope_sha256": license_scope_sha256(license_scope),
        "status": status,
        "finding_codes": tuple(sorted(findings)),
        "cell_count": len(matrix.cells),
        "aligned_count": dispositions.count("aligned"),
        "extension_count": dispositions.count("naumi_extension"),
        "deferred_count": dispositions.count("deferred"),
        "not_applicable_count": dispositions.count("not_applicable"),
        "verified_source_anchor_count": source_count,
        "verified_target_anchor_count": target_count,
        "verified_target_test_count": test_count,
    }
    return BehaviorMatrixAudit(audit_id=_sha256_json(values), **values)


def _sha256_json(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验 CC-03.1b 完整行为矩阵。")
    parser.add_argument("--matrix", required=True)
    parser.add_argument("--core-inventory", required=True)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--license-scope", required=True)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--project-root", default=".")
    arguments = parser.parse_args(argv)
    audit = verify_behavior_matrix(
        load_behavior_matrix(arguments.matrix),
        load_behavior_inventory(arguments.core_inventory),
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
    "BehaviorMatrixAudit",
    "BehaviorMatrixCell",
    "BehaviorMatrixManifest",
    "MATRIX_AREAS",
    "MATRIX_DIMENSIONS",
    "behavior_matrix_audit_sha256",
    "behavior_matrix_sha256",
    "load_behavior_matrix",
    "verify_behavior_matrix",
]
