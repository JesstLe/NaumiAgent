from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from naumi_agent.claude_source.behavior_inventory import (
    BehaviorInventoryManifest,
    behavior_inventory_sha256,
    load_behavior_inventory,
    verify_behavior_inventory,
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
]:
    project = tmp_path / "project"
    source = tmp_path / "source"
    mapping = project / "frontend" / "terminal-ui" / "cc-source-map.json"
    mapping.parent.mkdir(parents=True)
    mapping.write_text(json.dumps({
        "source": {"name": "fixture"},
        "mapping": [{
            "area": "core",
            "claude_code": [
                "src/Doctor.tsx",
                "src/Permission.tsx",
                "src/Task.tsx",
            ],
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
    for name, symbol in (
        ("Doctor.tsx", "Doctor"),
        ("Permission.tsx", "Permission"),
        ("Task.tsx", "Task"),
    ):
        (source / "src" / name).write_text(
            f"export function {symbol}() {{ return null; }}\n",
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
        observed_at=datetime(2026, 7, 24, tzinfo=UTC),
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
        "assessed_at": "2026-07-24T00:00:00+00:00",
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
        "\n".join([
            "def doctor_target():",
            "    return None",
            "def permission_target():",
            "    return None",
            "def task_target():",
            "    return None",
            "",
        ]),
        encoding="utf-8",
    )
    (project / "tests").mkdir()
    (project / "tests" / "test_target.py").write_text(
        "\n".join([
            "def test_doctor_target():",
            "    pass",
            "def test_permission_target():",
            "    pass",
            "def test_task_target():",
            "    pass",
            "",
        ]),
        encoding="utf-8",
    )
    behaviors = []
    for area, source_symbol in (
        ("doctor", "Doctor"),
        ("permission", "Permission"),
        ("task", "Task"),
    ):
        behaviors.append({
            "behavior_id": f"{area}.core",
            "area": area,
            "category": "presentation",
            "decision": "aligned",
            "source_evidence": [{
                "path": f"src/{source_symbol}.tsx",
                "symbol": source_symbol,
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
        })
    inventory = BehaviorInventoryManifest.model_validate({
        "source_name": identity.source_name,
        "source_commit": identity.git.commit,
        "source_identity_sha256": source_identity_sha256(identity),
        "license_scope_sha256": license_scope_sha256(scope),
        "behaviors": behaviors,
    })
    return project, source, identity, scope, inventory


def test_behavior_inventory_verifies_real_symbols_and_tests_deterministically(
    tmp_path: Path,
) -> None:
    project, source, identity, scope, inventory = _fixture(tmp_path)

    first = verify_behavior_inventory(
        inventory,
        identity,
        scope,
        source_root=source,
        project_root=project,
    )
    second = verify_behavior_inventory(
        inventory,
        identity,
        scope,
        source_root=source,
        project_root=project,
    )

    assert first == second
    assert first.status == "valid"
    assert first.behavior_count == 3
    assert first.aligned_count == 3
    assert first.verified_source_anchor_count == 3
    assert first.verified_target_anchor_count == 3
    assert first.verified_target_test_count == 3
    assert first.inventory_sha256 == behavior_inventory_sha256(inventory)


def test_missing_source_target_and_test_anchors_fail_closed(tmp_path: Path) -> None:
    project, source, identity, scope, inventory = _fixture(tmp_path)
    (source / "src" / "Doctor.tsx").write_text(
        "export function ChangedDoctor() { return null; }\n",
        encoding="utf-8",
    )
    _git(source, "add", ".")
    _git(source, "commit", "-m", "change source")

    stale = verify_behavior_inventory(
        inventory,
        identity,
        scope,
        source_root=source,
        project_root=project,
    )
    assert stale.status == "stale"
    assert stale.finding_codes == ("source_identity_stale",)

    _git(source, "reset", "--hard", identity.git.commit)
    (project / "src" / "target.py").write_text("def other():\n    pass\n", encoding="utf-8")
    (project / "tests" / "test_target.py").write_text(
        "def test_other():\n    pass\n",
        encoding="utf-8",
    )
    invalid = verify_behavior_inventory(
        inventory,
        identity,
        scope,
        source_root=source,
        project_root=project,
    )
    assert invalid.status == "invalid"
    assert invalid.finding_codes == ("target_anchor_missing", "target_test_missing")


def test_missing_source_anchor_and_disallowed_intake_mode_fail_closed(
    tmp_path: Path,
) -> None:
    project, source, identity, scope, inventory = _fixture(tmp_path)
    payload = inventory.model_dump(mode="json")
    payload["behaviors"][0]["source_evidence"][0]["symbol"] = "MissingDoctor"
    missing = verify_behavior_inventory(
        BehaviorInventoryManifest.model_validate(payload),
        identity,
        scope,
        source_root=source,
        project_root=project,
    )
    assert missing.status == "invalid"
    assert missing.finding_codes == ("source_anchor_missing", "source_evidence_missing")

    scope_payload = scope.model_dump(mode="json")
    scope_payload["rules"][0]["allowed_modes"] = ["reference"]
    reference_only = SourceLicenseScopeManifest.model_validate(scope_payload)
    payload = inventory.model_dump(mode="json")
    payload["license_scope_sha256"] = license_scope_sha256(reference_only)
    blocked = verify_behavior_inventory(
        BehaviorInventoryManifest.model_validate(payload),
        identity,
        reference_only,
        source_root=source,
        project_root=project,
    )
    assert blocked.status == "invalid"
    assert blocked.finding_codes == ("license_mode_disallowed", "source_evidence_missing")
    assert blocked.verified_source_anchor_count == 0


def test_inventory_shape_rejects_untraceable_alignment(tmp_path: Path) -> None:
    _project, _source, _identity, _scope, inventory = _fixture(tmp_path)
    payload = inventory.model_dump(mode="json")
    payload["behaviors"][0]["target_tests"] = []

    with pytest.raises(ValueError, match="target test"):
        BehaviorInventoryManifest.model_validate(payload)

    payload = inventory.model_dump(mode="json")
    payload["behaviors"][0]["source_evidence"][0]["path"] = "../escape.tsx"
    with pytest.raises(ValueError, match="安全相对路径"):
        BehaviorInventoryManifest.model_validate(payload)


def test_cli_verifies_fixture_without_writing_source(tmp_path: Path) -> None:
    project, source, identity, scope, inventory = _fixture(tmp_path)
    identity_path = project / "identity.json"
    scope_path = project / "scope.json"
    inventory_path = project / "inventory.json"
    write_source_identity(identity_path, identity)
    write_license_scope(scope_path, scope)
    inventory_path.write_text(inventory.model_dump_json(indent=2) + "\n", encoding="utf-8")
    before = _git(source, "status", "--short")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "naumi_agent.claude_source.behavior_inventory",
            "--inventory",
            str(inventory_path),
            "--identity",
            str(identity_path),
            "--license-scope",
            str(scope_path),
            "--source-root",
            str(source),
            "--project-root",
            str(project),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "valid"
    assert _git(source, "status", "--short") == before == ""


def test_committed_core_inventory_matches_current_source_when_available() -> None:
    if not (LOCAL_CLAUDE_SOURCE / ".git").exists():
        pytest.skip("本机未提供受治理 Claude Code checkout。")

    audit = verify_behavior_inventory(
        load_behavior_inventory(
            PROJECT_ROOT / "frontend/terminal-ui/cc-behavior-inventory.v1.json"
        ),
        SourceIdentityManifest.model_validate_json(
            (PROJECT_ROOT / "frontend/terminal-ui/cc-source-map.v2.json").read_text()
        ),
        SourceLicenseScopeManifest.model_validate_json(
            (PROJECT_ROOT / "frontend/terminal-ui/cc-license-scope.v1.json").read_text()
        ),
        source_root=LOCAL_CLAUDE_SOURCE,
        project_root=PROJECT_ROOT,
    )

    assert audit.status == "valid"
    assert audit.behavior_count == 6
    assert audit.aligned_count == 3
    assert audit.extension_count == 3
