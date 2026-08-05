"""CC-03.2a semantic mapping for authoritative terminal snapshot payloads."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.claude_source.behavior_inventory import TestEvidence, _declares_test, _safe_file
from naumi_agent.claude_source.behavior_matrix import (
    BehaviorMatrixManifest,
    behavior_matrix_sha256,
    load_behavior_matrix,
)
from naumi_agent.safety.permissions import PermissionRiskLevel
from naumi_agent.ui.doctor_health import (
    DoctorHealthDomain,
    DoctorHealthItem,
    DoctorHealthResponsibility,
    DoctorHealthSeverity,
    DoctorHealthSnapshot,
    _severity,
    doctor_health_payload,
)
from naumi_agent.ui.permission_panel import (
    PermissionPanelSnapshot,
    permission_panel_payload,
)
from naumi_agent.ui.task_panel import (
    TaskPanelFilter,
    TaskPanelSnapshot,
    TaskViewItem,
    _canonical_status,
)

MappingArea = Literal["doctor", "permission", "task"]
ResponsibilityOwner = Literal["bridge_snapshot", "not_applicable", "ui_local"]
FieldKind = Literal["array", "boolean", "enum", "integer", "literal", "object", "string"]
MappingStatus = Literal["invalid", "stale", "valid"]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CELL_ID_RE = re.compile(
    r"^(?:doctor|permission|task)\."
    r"(?:cancel|detail|empty|error|focus|keyboard|loading|presentation)$"
)
_FIELD_PATH_RE = re.compile(r"^payload(?:\.[a-z][a-z0-9_]*(?:\[\])?)+$")
_STATE_ID_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_FINDING_CODES = frozenset({
    "event_registry_missing",
    "field_domain_mismatch",
    "field_kind_mismatch",
    "field_missing",
    "field_value_mapping_mismatch",
    "mapping_binding_mismatch",
    "matrix_responsibility_mismatch",
    "producer_failure",
    "protocol_contract_stale",
    "server_event_missing",
    "target_path_missing",
    "target_test_missing",
})


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _visible_line(value: str, *, label: str) -> str:
    if value != value.strip() or any(ord(char) < 32 for char in value):
        raise ValueError(f"{label} 必须是单行可见文本。")
    return value


class CellResponsibility(_StrictModel):
    cell_id: str
    owner: ResponsibilityOwner
    reason: str = Field(min_length=4, max_length=400)

    @field_validator("cell_id")
    @classmethod
    def _cell_id(cls, value: str) -> str:
        if not _CELL_ID_RE.fullmatch(value):
            raise ValueError("responsibility cell_id 格式无效。")
        return value

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        return _visible_line(value, label="responsibility reason")


class SourceValueMapping(_StrictModel):
    source_value: str = Field(min_length=1, max_length=120)
    target_value: str = Field(min_length=1, max_length=120)
    meaning: str = Field(min_length=4, max_length=300)

    @field_validator("source_value", "target_value", "meaning")
    @classmethod
    def _line(cls, value: str) -> str:
        return _visible_line(value, label="source value mapping")


class ProtocolFieldMapping(_StrictModel):
    field_path: str
    value_kind: FieldKind
    required: bool = True
    semantic: str = Field(min_length=4, max_length=400)
    allowed_values: tuple[str, ...] = Field(default=(), max_length=32)
    source_values: tuple[SourceValueMapping, ...] = Field(default=(), max_length=32)

    @field_validator("field_path")
    @classmethod
    def _field_path(cls, value: str) -> str:
        if not _FIELD_PATH_RE.fullmatch(value):
            raise ValueError("protocol field_path 格式无效。")
        return value

    @field_validator("semantic")
    @classmethod
    def _semantic(cls, value: str) -> str:
        return _visible_line(value, label="field semantic")

    @model_validator(mode="after")
    def _shape(self) -> ProtocolFieldMapping:
        if self.allowed_values != tuple(sorted(set(self.allowed_values))):
            raise ValueError("allowed_values 必须排序且不得重复。")
        if self.value_kind in {"enum", "literal"} and not self.allowed_values:
            raise ValueError("enum/literal field 必须声明 allowed_values。")
        if self.value_kind not in {"enum", "literal"} and self.allowed_values:
            raise ValueError("非 enum/literal field 不得伪造 allowed_values。")
        if self.source_values != tuple(
            sorted(self.source_values, key=lambda item: item.source_value)
        ):
            raise ValueError("source_values 必须按 source_value 排序。")
        if len({item.source_value for item in self.source_values}) != len(self.source_values):
            raise ValueError("source_value 不得重复。")
        if self.allowed_values and any(
            item.target_value not in self.allowed_values for item in self.source_values
        ):
            raise ValueError("source value 的 target 必须位于 allowed_values。")
        return self


class SnapshotProjection(_StrictModel):
    area: MappingArea
    producer: Literal[
        "doctor_health_snapshot_v1",
        "permission_panel_snapshot_v1",
        "task_panel_snapshot_v1",
    ]
    server_event: Literal["doctor/health", "permissions/snapshot", "tasks/snapshot"]
    matrix_cells: tuple[str, ...] = Field(min_length=1, max_length=8)
    fields: tuple[ProtocolFieldMapping, ...] = Field(min_length=1, max_length=64)
    target_tests: tuple[TestEvidence, ...] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def _shape(self) -> SnapshotProjection:
        expected = {
            "doctor": ("doctor_health_snapshot_v1", "doctor/health"),
            "permission": ("permission_panel_snapshot_v1", "permissions/snapshot"),
            "task": ("task_panel_snapshot_v1", "tasks/snapshot"),
        }[self.area]
        if (self.producer, self.server_event) != expected:
            raise ValueError("area、producer 与 server_event 必须使用固定权威组合。")
        if self.matrix_cells != tuple(sorted(set(self.matrix_cells))):
            raise ValueError("projection matrix_cells 必须排序且不得重复。")
        if any(not item.startswith(f"{self.area}.") for item in self.matrix_cells):
            raise ValueError("projection matrix cell 必须与 area 一致。")
        if self.fields != tuple(sorted(self.fields, key=lambda item: item.field_path)):
            raise ValueError("projection fields 必须按 field_path 排序。")
        if len({item.field_path for item in self.fields}) != len(self.fields):
            raise ValueError("projection field_path 不得重复。")
        if self.target_tests != tuple(
            sorted(set(self.target_tests), key=lambda item: (item.path, item.test_name))
        ):
            raise ValueError("projection target_tests 必须排序且不得重复。")
        return self


class SemanticMappingManifest(_StrictModel):
    schema_version: Literal[1] = 1
    manifest_kind: Literal["claude_code_snapshot_semantic_mapping"] = (
        "claude_code_snapshot_semantic_mapping"
    )
    behavior_matrix_sha256: str
    protocol_contract_sha256: str
    scope: Literal["cc_03_2a_snapshot_semantics"] = "cc_03_2a_snapshot_semantics"
    responsibilities: tuple[CellResponsibility, ...] = Field(min_length=24, max_length=24)
    projections: tuple[SnapshotProjection, ...] = Field(min_length=3, max_length=3)

    @field_validator("behavior_matrix_sha256", "protocol_contract_sha256")
    @classmethod
    def _digest(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("semantic mapping digest 必须是完整小写 SHA-256。")
        return value

    @model_validator(mode="after")
    def _coverage(self) -> SemanticMappingManifest:
        if self.responsibilities != tuple(
            sorted(self.responsibilities, key=lambda item: item.cell_id)
        ):
            raise ValueError("responsibilities 必须按 cell_id 排序。")
        if len({item.cell_id for item in self.responsibilities}) != 24:
            raise ValueError("responsibilities 必须精确包含 24 个唯一单元格。")
        if self.projections != tuple(sorted(self.projections, key=lambda item: item.area)):
            raise ValueError("projections 必须按 area 排序。")
        if {item.area for item in self.projections} != {"doctor", "permission", "task"}:
            raise ValueError("projections 必须精确覆盖三个 area。")
        projected = [cell for projection in self.projections for cell in projection.matrix_cells]
        owned = [
            item.cell_id
            for item in self.responsibilities
            if item.owner == "bridge_snapshot"
        ]
        if sorted(projected) != sorted(owned) or len(projected) != len(set(projected)):
            raise ValueError("bridge_snapshot responsibility 必须被 projection 精确覆盖一次。")
        return self


class SemanticMappingAudit(_StrictModel):
    schema_version: Literal[1] = 1
    audit_id: str
    mapping_sha256: str
    behavior_matrix_sha256: str
    protocol_contract_sha256: str
    status: MappingStatus
    finding_codes: tuple[str, ...] = Field(default=(), max_length=32)
    responsibility_count: int = Field(ge=0, le=24)
    projection_count: int = Field(ge=0, le=3)
    field_count: int = Field(ge=0, le=192)
    verified_field_count: int = Field(ge=0, le=192)
    verified_target_test_count: int = Field(ge=0, le=36)

    @field_validator(
        "audit_id",
        "mapping_sha256",
        "behavior_matrix_sha256",
        "protocol_contract_sha256",
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("semantic mapping audit digest 无效。")
        return value

    @field_validator("finding_codes")
    @classmethod
    def _findings(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("finding_codes 必须排序且不得重复。")
        if any(item not in _FINDING_CODES for item in value):
            raise ValueError("finding code 不在治理集合中。")
        return value

    @model_validator(mode="after")
    def _integrity(self) -> SemanticMappingAudit:
        if self.status == "valid" and self.finding_codes:
            raise ValueError("valid semantic audit 不得携带 finding。")
        if self.status != "valid" and not self.finding_codes:
            raise ValueError("非 valid semantic audit 必须携带 finding。")
        if self.verified_field_count > self.field_count:
            raise ValueError("verified field count 不得超过声明数。")
        if self.audit_id != semantic_mapping_audit_sha256(self):
            raise ValueError("semantic mapping audit digest 不一致。")
        return self


@dataclass(frozen=True)
class _ProducerContract:
    event: str
    payload: dict[str, Any]
    domains: dict[str, tuple[str, ...]]
    source_value_targets: dict[str, dict[str, str]]


def load_semantic_mapping(path: str | Path) -> SemanticMappingManifest:
    return SemanticMappingManifest.model_validate_json(Path(path).read_text(encoding="utf-8"))


def semantic_mapping_sha256(mapping: SemanticMappingManifest) -> str:
    return _sha256_json(mapping.model_dump(mode="json"))


def protocol_contract_sha256(contract: dict[str, Any]) -> str:
    return _sha256_json(contract)


def semantic_mapping_audit_sha256(audit: SemanticMappingAudit) -> str:
    payload = audit.model_dump(mode="json")
    payload.pop("audit_id", None)
    return _sha256_json(payload)


def verify_semantic_mapping(
    mapping: SemanticMappingManifest,
    matrix: BehaviorMatrixManifest,
    protocol_contract: dict[str, Any],
    *,
    project_root: str | Path,
) -> SemanticMappingAudit:
    """Verify matrix ownership, event registration and real serializer payload fields."""
    project = Path(project_root).expanduser().resolve()
    findings: set[str] = set()
    verified_fields = 0
    verified_tests = 0

    matrix_digest = behavior_matrix_sha256(matrix)
    contract_digest = protocol_contract_sha256(protocol_contract)
    if mapping.behavior_matrix_sha256 != matrix_digest:
        findings.add("mapping_binding_mismatch")
    if mapping.protocol_contract_sha256 != contract_digest:
        findings.add("protocol_contract_stale")

    matrix_by_id = {item.cell_id: item for item in matrix.cells}
    responsibility_by_id = {item.cell_id: item for item in mapping.responsibilities}
    if set(matrix_by_id) != set(responsibility_by_id):
        findings.add("matrix_responsibility_mismatch")
    else:
        for cell_id, cell in matrix_by_id.items():
            owner = responsibility_by_id[cell_id].owner
            if (cell.disposition == "not_applicable") != (owner == "not_applicable"):
                findings.add("matrix_responsibility_mismatch")
                break

    server_events = set(protocol_contract.get("server_events") or [])
    registry = protocol_contract.get("event_registry") or {}
    server_registry = registry.get("server") if isinstance(registry, dict) else {}
    if not isinstance(server_registry, dict):
        server_registry = {}

    for projection in mapping.projections:
        if projection.server_event not in server_events:
            findings.add("server_event_missing")
        if projection.server_event not in server_registry:
            findings.add("event_registry_missing")
        try:
            producer = _producer_contract(projection.producer)
        except Exception:
            findings.add("producer_failure")
            continue
        if producer.event != projection.server_event:
            findings.add("producer_failure")
            continue
        for field in projection.fields:
            values = _resolve_field_path(producer.payload, field.field_path)
            if not values:
                if field.required:
                    findings.add("field_missing")
                continue
            if not all(_matches_kind(value, field.value_kind) for value in values):
                findings.add("field_kind_mismatch")
                continue
            expected_domain = producer.domains.get(field.field_path, ())
            if expected_domain != field.allowed_values:
                findings.add("field_domain_mismatch")
                continue
            if field.allowed_values and any(
                str(value) not in field.allowed_values for value in values
            ):
                findings.add("field_domain_mismatch")
                continue
            declared_value_mapping = {
                item.source_value: item.target_value for item in field.source_values
            }
            actual_value_mapping = producer.source_value_targets.get(
                field.field_path,
                {},
            )
            if declared_value_mapping != actual_value_mapping:
                findings.add("field_value_mapping_mismatch")
                continue
            verified_fields += 1
        for evidence in projection.target_tests:
            path = _safe_file(project, evidence.path)
            if path is None:
                findings.add("target_path_missing")
            elif not _declares_test(path, evidence.test_name):
                findings.add("target_test_missing")
            else:
                verified_tests += 1

    status: MappingStatus
    if findings & {"mapping_binding_mismatch", "protocol_contract_stale"}:
        status = "stale"
    else:
        status = "invalid" if findings else "valid"
    field_count = sum(len(item.fields) for item in mapping.projections)
    values = {
        "schema_version": 1,
        "mapping_sha256": semantic_mapping_sha256(mapping),
        "behavior_matrix_sha256": matrix_digest,
        "protocol_contract_sha256": contract_digest,
        "status": status,
        "finding_codes": tuple(sorted(findings)),
        "responsibility_count": len(mapping.responsibilities),
        "projection_count": len(mapping.projections),
        "field_count": field_count,
        "verified_field_count": verified_fields,
        "verified_target_test_count": verified_tests,
    }
    return SemanticMappingAudit(audit_id=_sha256_json(values), **values)


def _producer_contract(producer: str) -> _ProducerContract:
    if producer == "task_panel_snapshot_v1":
        item = TaskViewItem(
            view_id="todo:task-1",
            source="todo",
            task_id="task-1",
            status="running",
            raw_status="in_progress",
            title="Fixture task",
            owner="main",
            dependency_ids=("task-0",),
        )
        payload = TaskPanelSnapshot(
            view_items=(item,),
            filters=TaskPanelFilter(detail_id="task-1"),
            warnings=("fixture warning",),
        ).to_protocol_dict()
        statuses = tuple(sorted({
            _canonical_status(value)
            for value in (
                "pending", "running", "completed", "failed", "cancelled", "blocked"
            )
        }))
        return _ProducerContract(
            event="tasks/snapshot",
            payload=payload,
            domains={
                "payload.schema_version": ("1",),
                "payload.items[].status": statuses,
            },
            source_value_targets={
                "payload.items[].status": {
                    value: _canonical_status(value)
                    for value in ("completed", "in_progress", "pending")
                },
            },
        )
    if producer == "permission_panel_snapshot_v1":
        permission = {
            "request_id": "perm-1",
            "agent_name": "main",
            "tool_name": "bash_run",
            "status": "needs_confirmation",
            "reason": "Fixture confirmation",
            "policy": {
                "source": "TOOL_PERMISSIONS:bash_run",
                "risk": "high",
                "modes": "bypass/moderate",
                "confirmation": "需要确认",
                "bypass": "bypass 全权限放行",
            },
        }
        payload = permission_panel_payload(PermissionPanelSnapshot(
            runtime_mode="bypass",
            permission_mode="bypass",
            pending=(permission,),
            grants=({"grant_id": "grant-1", "tool_family": "shell"},),
            history=({**permission, "status": "allow_once"},),
            warnings=("fixture warning",),
        ))
        return _ProducerContract(
            event="permissions/snapshot",
            payload=payload,
            domains={
                "payload.schema_version": ("1",),
                "payload.pending[].policy.risk": tuple(
                    sorted(item.value for item in PermissionRiskLevel)
                ),
            },
            source_value_targets={
                "payload.pending[].policy.risk": {
                    item.value: item.value for item in PermissionRiskLevel
                },
            },
        )
    if producer == "doctor_health_snapshot_v1":
        item = DoctorHealthItem(
            id="runtime-fixture",
            domain="runtime",
            label="Fixture check",
            severity="error",
            responsibility="product_runtime",
            detail="Fixture detail",
            diagnostic_code="fixture_failure",
        )
        payload = doctor_health_payload(DoctorHealthSnapshot(
            status="error",
            generated_at="2026-08-05T00:00:00+00:00",
            snapshot_sha256="0" * 64,
            items=(item,),
        ))
        return _ProducerContract(
            event="doctor/health",
            payload=payload,
            domains={
                "payload.schema_version": ("1",),
                "payload.status": tuple(sorted(get_args(DoctorHealthSeverity))),
                "payload.items[].severity": tuple(sorted(get_args(DoctorHealthSeverity))),
                "payload.items[].domain": tuple(sorted(get_args(DoctorHealthDomain))),
                "payload.items[].responsibility": tuple(
                    sorted(get_args(DoctorHealthResponsibility))
                ),
            },
            source_value_targets={
                "payload.items[].severity": {
                    value: _severity(value) for value in ("error", "pass", "warn")
                },
            },
        )
    raise ValueError("unknown producer")


def _resolve_field_path(payload: dict[str, Any], field_path: str) -> list[Any]:
    current: list[Any] = [{"payload": payload}]
    for token in field_path.split("."):
        is_array = token.endswith("[]")
        key = token[:-2] if is_array else token
        next_values: list[Any] = []
        for value in current:
            if not isinstance(value, dict) or key not in value:
                continue
            child = value[key]
            if is_array:
                if isinstance(child, (list, tuple)):
                    next_values.extend(child)
            else:
                next_values.append(child)
        current = next_values
    return current


def _matches_kind(value: Any, kind: FieldKind) -> bool:
    if kind in {"enum", "literal", "string"}:
        return isinstance(value, str) if kind != "literal" else isinstance(value, (str, int, bool))
    if kind == "array":
        return isinstance(value, (list, tuple))
    if kind == "object":
        return isinstance(value, dict)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return False


def _sha256_json(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验 CC-03.2a Snapshot 语义映射。")
    parser.add_argument("--mapping", required=True)
    parser.add_argument("--matrix", required=True)
    parser.add_argument("--protocol-contract", required=True)
    parser.add_argument("--project-root", default=".")
    arguments = parser.parse_args(argv)
    contract = json.loads(Path(arguments.protocol_contract).read_text(encoding="utf-8"))
    audit = verify_semantic_mapping(
        load_semantic_mapping(arguments.mapping),
        load_behavior_matrix(arguments.matrix),
        contract,
        project_root=arguments.project_root,
    )
    print(audit.model_dump_json(indent=2))
    return 0 if audit.status == "valid" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CellResponsibility",
    "ProtocolFieldMapping",
    "SemanticMappingAudit",
    "SemanticMappingManifest",
    "SnapshotProjection",
    "SourceValueMapping",
    "load_semantic_mapping",
    "protocol_contract_sha256",
    "semantic_mapping_audit_sha256",
    "semantic_mapping_sha256",
    "verify_semantic_mapping",
]
