from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from naumi_agent.claude_source.behavior_matrix import load_behavior_matrix
from naumi_agent.claude_source.semantic_mapping import (
    SemanticMappingManifest,
    load_semantic_mapping,
    protocol_contract_sha256,
    semantic_mapping_sha256,
    verify_semantic_mapping,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAPPING_PATH = PROJECT_ROOT / "frontend" / "terminal-ui" / "cc-semantic-mapping.v1.json"
MATRIX_PATH = PROJECT_ROOT / "frontend" / "terminal-ui" / "cc-behavior-matrix.v1.json"
CONTRACT_PATH = PROJECT_ROOT / "frontend" / "terminal-ui" / "protocol-contract.json"


def _inputs() -> tuple[SemanticMappingManifest, object, dict[str, object]]:
    return (
        load_semantic_mapping(MAPPING_PATH),
        load_behavior_matrix(MATRIX_PATH),
        json.loads(CONTRACT_PATH.read_text(encoding="utf-8")),
    )


def test_semantic_mapping_verifies_real_snapshot_producers_deterministically() -> None:
    mapping, matrix, contract = _inputs()

    first = verify_semantic_mapping(
        mapping, matrix, contract, project_root=PROJECT_ROOT
    )
    second = verify_semantic_mapping(
        mapping, matrix, contract, project_root=PROJECT_ROOT
    )

    assert first == second
    assert first.status == "valid"
    assert first.responsibility_count == 24
    assert first.projection_count == 3
    assert first.field_count == 27
    assert first.verified_field_count == 27
    assert first.verified_target_test_count == 3
    assert first.mapping_sha256 == semantic_mapping_sha256(mapping)


def test_semantic_mapping_requires_exact_responsibility_and_projection_ownership() -> None:
    mapping, matrix, contract = _inputs()
    payload = mapping.model_dump(mode="json")
    payload["responsibilities"] = payload["responsibilities"][:-1]
    with pytest.raises(ValidationError, match="at least 24 items"):
        SemanticMappingManifest.model_validate(payload)

    payload = mapping.model_dump(mode="json")
    task_empty = next(
        item for item in payload["responsibilities"] if item["cell_id"] == "task.empty"
    )
    task_empty["owner"] = "ui_local"
    with pytest.raises(ValidationError, match="精确覆盖一次"):
        SemanticMappingManifest.model_validate(payload)

    payload = mapping.model_dump(mode="json")
    permission_cancel = next(
        item
        for item in payload["responsibilities"]
        if item["cell_id"] == "permission.cancel"
    )
    permission_cancel["owner"] = "ui_local"
    audit = verify_semantic_mapping(
        SemanticMappingManifest.model_validate(payload),
        matrix,
        contract,
        project_root=PROJECT_ROOT,
    )
    assert audit.status == "invalid"
    assert audit.finding_codes == ("matrix_responsibility_mismatch",)


def test_semantic_mapping_binding_change_is_stale() -> None:
    mapping, matrix, contract = _inputs()
    payload = mapping.model_dump(mode="json")
    payload["behavior_matrix_sha256"] = "0" * 64

    audit = verify_semantic_mapping(
        SemanticMappingManifest.model_validate(payload),
        matrix,
        contract,
        project_root=PROJECT_ROOT,
    )

    assert audit.status == "stale"
    assert audit.finding_codes == ("mapping_binding_mismatch",)


def test_semantic_mapping_missing_field_and_wrong_domain_fail_closed() -> None:
    mapping, matrix, contract = _inputs()
    payload = mapping.model_dump(mode="json")
    task = next(item for item in payload["projections"] if item["area"] == "task")
    status = next(
        item for item in task["fields"] if item["field_path"] == "payload.items[].status"
    )
    status["allowed_values"].append("unknown")
    wrong_domain = verify_semantic_mapping(
        SemanticMappingManifest.model_validate(payload),
        matrix,
        contract,
        project_root=PROJECT_ROOT,
    )
    assert wrong_domain.status == "invalid"
    assert wrong_domain.finding_codes == ("field_domain_mismatch",)

    payload = mapping.model_dump(mode="json")
    task = next(item for item in payload["projections"] if item["area"] == "task")
    title = next(
        item for item in task["fields"] if item["field_path"] == "payload.items[].title"
    )
    title["field_path"] = "payload.items[].unknown_title"
    task["fields"].sort(key=lambda item: item["field_path"])
    missing = verify_semantic_mapping(
        SemanticMappingManifest.model_validate(payload),
        matrix,
        contract,
        project_root=PROJECT_ROOT,
    )
    assert missing.status == "invalid"
    assert missing.finding_codes == ("field_missing",)


def test_semantic_mapping_executes_declared_source_value_normalization() -> None:
    mapping, matrix, contract = _inputs()
    payload = mapping.model_dump(mode="json")
    task = next(item for item in payload["projections"] if item["area"] == "task")
    status = next(
        item for item in task["fields"] if item["field_path"] == "payload.items[].status"
    )
    in_progress = next(
        item for item in status["source_values"] if item["source_value"] == "in_progress"
    )
    in_progress["target_value"] = "pending"

    audit = verify_semantic_mapping(
        SemanticMappingManifest.model_validate(payload),
        matrix,
        contract,
        project_root=PROJECT_ROOT,
    )

    assert audit.status == "invalid"
    assert audit.finding_codes == ("field_value_mapping_mismatch",)


def test_semantic_mapping_checks_event_registry_and_target_tests() -> None:
    mapping, matrix, contract = _inputs()
    contract["server_events"].remove("tasks/snapshot")
    payload = mapping.model_dump(mode="json")
    payload["protocol_contract_sha256"] = protocol_contract_sha256(contract)
    missing_event = verify_semantic_mapping(
        SemanticMappingManifest.model_validate(payload),
        matrix,
        contract,
        project_root=PROJECT_ROOT,
    )
    assert missing_event.status == "invalid"
    assert missing_event.finding_codes == ("server_event_missing",)

    mapping_payload = mapping.model_dump(mode="json")
    mapping_payload["projections"][0]["target_tests"][0]["test_name"] = (
        "test_missing_semantic_contract"
    )
    missing_test = verify_semantic_mapping(
        SemanticMappingManifest.model_validate(mapping_payload),
        matrix,
        json.loads(CONTRACT_PATH.read_text(encoding="utf-8")),
        project_root=PROJECT_ROOT,
    )
    assert missing_test.status == "invalid"
    assert missing_test.finding_codes == ("target_test_missing",)


def test_semantic_mapping_cli_is_read_only(tmp_path: Path) -> None:
    mapping_before = MAPPING_PATH.read_bytes()
    matrix_before = MATRIX_PATH.read_bytes()
    contract_before = CONTRACT_PATH.read_bytes()

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "naumi_agent.claude_source.semantic_mapping",
            "--mapping",
            str(MAPPING_PATH),
            "--matrix",
            str(MATRIX_PATH),
            "--protocol-contract",
            str(CONTRACT_PATH),
            "--project-root",
            str(PROJECT_ROOT),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "valid"
    assert MAPPING_PATH.read_bytes() == mapping_before
    assert MATRIX_PATH.read_bytes() == matrix_before
    assert CONTRACT_PATH.read_bytes() == contract_before
