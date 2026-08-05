from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from naumi_agent.claude_source.behavior_inventory import (
    BehaviorInventoryManifest,
    behavior_inventory_sha256,
    load_behavior_inventory,
)
from naumi_agent.claude_source.behavior_matrix import (
    MATRIX_AREAS,
    MATRIX_DIMENSIONS,
    BehaviorMatrixManifest,
    behavior_matrix_sha256,
    load_behavior_matrix,
    verify_behavior_matrix,
)
from naumi_agent.claude_source.governance import (
    SourceIdentityManifest,
    capture_source_identity,
    write_source_identity,
)
from naumi_agent.claude_source.license_scope import (
    SourceLicenseScopeManifest,
    license_scope_sha256,
    write_license_scope,
)
from naumi_agent.claude_source.refresh import source_identity_sha256

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOCAL_CLAUDE_SOURCE = Path("/Users/lv/Workspace/claude-code")
CLAIM = "Fixture source may be referenced and studied"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _fixture(
    tmp_path: Path,
) -> tuple[
    Path,
    Path,
    SourceIdentityManifest,
    SourceLicenseScopeManifest,
    BehaviorInventoryManifest,
    BehaviorMatrixManifest,
]:
    project = tmp_path / "project"
    source = tmp_path / "source"
    mapping = project / "frontend" / "terminal-ui" / "cc-source-map.json"
    mapping.parent.mkdir(parents=True)
    mapping.write_text(json.dumps({
        "source": {"name": "fixture"},
        "mapping": [{
            "area": "core",
            "claude_code": [f"src/{area}.tsx" for area in MATRIX_AREAS],
            "naumi_agent": ["src/target.py"],
        }],
    }) + "\n", encoding="utf-8")
    source.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.email", "source@example.invalid")
    _git(source, "config", "user.name", "Source Fixture")
    _git(source, "remote", "add", "origin", "https://example.invalid/source.git")
    (source / "README.md").write_text(f"# Source\n\n{CLAIM}\n", encoding="utf-8")
    (source / "src").mkdir()
    for area in MATRIX_AREAS:
        (source / "src" / f"{area}.tsx").write_text(
            f"export function {area}_source() {{ return null; }}\n",
            encoding="utf-8",
        )
    _git(source, "add", ".")
    _git(source, "commit", "-m", "fixture")
    identity = capture_source_identity(
        source,
        mapping,
        source_name="fixture-source",
        checkout_hint="../source",
        license_claim=CLAIM,
        observed_at=datetime(2026, 8, 5, tzinfo=UTC),
    )
    scope = SourceLicenseScopeManifest.model_validate({
        "source_name": identity.source_name,
        "source_commit": identity.git.commit,
        "source_identity_sha256": source_identity_sha256(identity),
        "license_path": identity.license.path,
        "license_sha256": identity.license.sha256,
        "license_claim_sha256": hashlib.sha256(CLAIM.encode()).hexdigest(),
        "license_expression": "LicenseRef-Fixture",
        "review_status": "restricted_pending_legal_review",
        "redistribution_status": "unreviewed",
        "assessed_by": "codex-local-audit",
        "assessed_at": "2026-08-05T00:00:00+00:00",
        "assessment_reason": "Independent semantic reimplementation only.",
        "legal_review_required": True,
        "rules": [{
            "path_prefix": "src",
            "allowed_modes": ["reference", "reimplement"],
            "notice_requirement": "unknown",
            "rationale": "Fixture semantic research boundary.",
        }],
    })
    (project / "src").mkdir()
    (project / "src" / "target.py").write_text(
        "".join(f"def {area}_target():\n    return None\n" for area in MATRIX_AREAS),
        encoding="utf-8",
    )
    (project / "tests").mkdir()
    (project / "tests" / "test_target.py").write_text(
        "".join(f"def test_{area}_target():\n    pass\n" for area in MATRIX_AREAS),
        encoding="utf-8",
    )
    core = BehaviorInventoryManifest.model_validate({
        "source_name": identity.source_name,
        "source_commit": identity.git.commit,
        "source_identity_sha256": source_identity_sha256(identity),
        "license_scope_sha256": license_scope_sha256(scope),
        "behaviors": [{
            "behavior_id": f"{area}.core",
            "area": area,
            "category": "presentation",
            "decision": "aligned",
            "source_evidence": [{
                "path": f"src/{area}.tsx",
                "symbol": f"{area}_source",
            }],
            "target_evidence": [{
                "path": "src/target.py",
                "symbol": f"{area}_target",
            }],
            "target_tests": [{
                "path": "tests/test_target.py",
                "test_name": f"test_{area}_target",
            }],
            "divergence": "",
        } for area in MATRIX_AREAS],
    })
    cells = []
    for area in MATRIX_AREAS:
        for dimension in MATRIX_DIMENSIONS:
            aligned = dimension == "presentation"
            cells.append({
                "cell_id": f"{area}.{dimension}",
                "area": area,
                "dimension": dimension,
                "disposition": "aligned" if aligned else "not_applicable",
                "behavior": f"{area} {dimension} governed behavior",
                "source_evidence": ([{
                    "path": f"src/{area}.tsx",
                    "symbol": f"{area}_source",
                }] if aligned else []),
                "target_evidence": ([{
                    "path": "src/target.py",
                    "symbol": f"{area}_target",
                }] if aligned else []),
                "target_tests": ([{
                    "path": "tests/test_target.py",
                    "test_name": f"test_{area}_target",
                }] if aligned else []),
                "rationale": "" if aligned else "Fixture product boundary.",
            })
    matrix = BehaviorMatrixManifest.model_validate({
        "source_name": identity.source_name,
        "source_commit": identity.git.commit,
        "source_identity_sha256": source_identity_sha256(identity),
        "license_scope_sha256": license_scope_sha256(scope),
        "core_inventory_sha256": behavior_inventory_sha256(core),
        "cells": cells,
    })
    return project, source, identity, scope, core, matrix


def test_complete_matrix_verifies_deterministically(tmp_path: Path) -> None:
    project, source, identity, scope, core, matrix = _fixture(tmp_path)

    first = verify_behavior_matrix(
        matrix, core, identity, scope, source_root=source, project_root=project
    )
    second = verify_behavior_matrix(
        matrix, core, identity, scope, source_root=source, project_root=project
    )

    assert first == second
    assert first.status == "valid"
    assert first.cell_count == 24
    assert first.aligned_count == 3
    assert first.not_applicable_count == 21
    assert first.verified_source_anchor_count == 3
    assert first.verified_target_anchor_count == 3
    assert first.verified_target_test_count == 3
    assert first.matrix_sha256 == behavior_matrix_sha256(matrix)


def test_matrix_requires_exact_sorted_cartesian_coverage(tmp_path: Path) -> None:
    *_, matrix = _fixture(tmp_path)
    payload = matrix.model_dump(mode="json")
    payload["cells"] = payload["cells"][:-1]
    with pytest.raises(ValidationError, match="at least 24 items"):
        BehaviorMatrixManifest.model_validate(payload)

    payload = matrix.model_dump(mode="json")
    payload["cells"][-1] = payload["cells"][-2]
    with pytest.raises(ValidationError, match="24 个单元格"):
        BehaviorMatrixManifest.model_validate(payload)

    payload = matrix.model_dump(mode="json")
    payload["cells"][0], payload["cells"][1] = payload["cells"][1], payload["cells"][0]
    with pytest.raises(ValidationError, match="cell_id 排序"):
        BehaviorMatrixManifest.model_validate(payload)


def test_matrix_rejects_disguised_evidence_shapes(tmp_path: Path) -> None:
    *_, matrix = _fixture(tmp_path)
    payload = matrix.model_dump(mode="json")
    payload["cells"][0]["source_evidence"] = [{
        "path": "src/doctor.tsx",
        "symbol": "doctor_source",
    }]
    with pytest.raises(ValidationError, match="不得绑定伪证据"):
        BehaviorMatrixManifest.model_validate(payload)

    payload = matrix.model_dump(mode="json")
    aligned = next(item for item in payload["cells"] if item["disposition"] == "aligned")
    aligned["target_tests"] = []
    with pytest.raises(ValidationError, match="必须绑定 source、target 与 target test"):
        BehaviorMatrixManifest.model_validate(payload)


def test_core_binding_and_missing_target_fail_closed(tmp_path: Path) -> None:
    project, source, identity, scope, core, matrix = _fixture(tmp_path)
    payload = matrix.model_dump(mode="json")
    payload["core_inventory_sha256"] = "0" * 64
    stale = verify_behavior_matrix(
        BehaviorMatrixManifest.model_validate(payload),
        core,
        identity,
        scope,
        source_root=source,
        project_root=project,
    )
    assert stale.status == "stale"
    assert stale.finding_codes == ("core_inventory_binding_mismatch",)

    (project / "src" / "target.py").write_text("def other():\n    pass\n", encoding="utf-8")
    invalid = verify_behavior_matrix(
        matrix, core, identity, scope, source_root=source, project_root=project
    )
    assert invalid.status == "invalid"
    assert invalid.finding_codes == ("core_inventory_invalid", "target_anchor_missing")


def test_matrix_cli_is_read_only(tmp_path: Path) -> None:
    project, source, identity, scope, core, matrix = _fixture(tmp_path)
    manifest_dir = project / "manifests"
    manifest_dir.mkdir()
    matrix_path = manifest_dir / "matrix.json"
    core_path = manifest_dir / "core.json"
    identity_path = manifest_dir / "identity.json"
    scope_path = manifest_dir / "scope.json"
    matrix_path.write_text(matrix.model_dump_json(indent=2) + "\n", encoding="utf-8")
    core_path.write_text(core.model_dump_json(indent=2) + "\n", encoding="utf-8")
    write_source_identity(identity_path, identity)
    write_license_scope(scope_path, scope)
    before_source = _git(source, "status", "--porcelain=v1")
    before_target = (project / "src" / "target.py").read_bytes()

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "naumi_agent.claude_source.behavior_matrix",
            "--matrix",
            str(matrix_path),
            "--core-inventory",
            str(core_path),
            "--identity",
            str(identity_path),
            "--license-scope",
            str(scope_path),
            "--source-root",
            str(source),
            "--project-root",
            str(project),
        ],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "valid"
    assert _git(source, "status", "--porcelain=v1") == before_source
    assert (project / "src" / "target.py").read_bytes() == before_target


def test_committed_matrix_validates_governed_local_checkout() -> None:
    if not LOCAL_CLAUDE_SOURCE.exists():
        pytest.skip("governed local Claude source checkout is unavailable")
    matrix = load_behavior_matrix(
        PROJECT_ROOT / "frontend" / "terminal-ui" / "cc-behavior-matrix.v1.json"
    )
    core = load_behavior_inventory(
        PROJECT_ROOT / "frontend" / "terminal-ui" / "cc-behavior-inventory.v1.json"
    )
    from naumi_agent.claude_source.governance import load_source_identity
    from naumi_agent.claude_source.license_scope import load_license_scope

    audit = verify_behavior_matrix(
        matrix,
        core,
        load_source_identity(PROJECT_ROOT / "frontend" / "terminal-ui" / "cc-source-map.v2.json"),
        load_license_scope(PROJECT_ROOT / "frontend" / "terminal-ui" / "cc-license-scope.v1.json"),
        source_root=LOCAL_CLAUDE_SOURCE,
        project_root=PROJECT_ROOT,
    )

    assert audit.status == "valid"
    assert audit.cell_count == 24
    assert audit.finding_codes == ()
