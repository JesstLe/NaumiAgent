from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from naumi_agent.claude_source.governance import (
    SourceIdentityManifest,
    capture_source_identity,
    load_source_identity,
    write_source_identity,
)
from naumi_agent.claude_source.license_scope import (
    LicenseScopeRule,
    SourceLicenseScopeAudit,
    SourceLicenseScopeManifest,
    license_scope_sha256,
    load_license_scope,
    verify_license_scope,
    write_license_scope,
)
from naumi_agent.claude_source.refresh import source_identity_sha256

CLAIM = "Fixture source may be referenced and studied"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOCAL_CLAUDE_SOURCE = Path("/Users/lv/Workspace/claude-code")


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _fixture(
    tmp_path: Path,
    *,
    mapped_paths: tuple[str, ...] = ("src/main.tsx", "src/view.tsx"),
) -> tuple[Path, Path, Path, SourceIdentityManifest]:
    project = tmp_path / "project"
    source = tmp_path / "source"
    mapping = project / "frontend" / "terminal-ui" / "cc-source-map.json"
    mapping.parent.mkdir(parents=True)
    mapping.write_text(json.dumps({
        "source": {"name": "fixture"},
        "mapping": [{
            "area": "ui",
            "claude_code": list(mapped_paths),
            "naumi_agent": ["frontend/terminal-ui/src/index.js"],
        }],
    }) + "\n", encoding="utf-8")
    source.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.email", "source@example.invalid")
    _git(source, "config", "user.name", "Source Fixture")
    _git(source, "remote", "add", "origin", "https://example.invalid/source.git")
    (source / "README.md").write_text(f"# Source\n\n{CLAIM}\n", encoding="utf-8")
    (source / "src").mkdir()
    for relative in {path for path in mapped_paths if path != "src/missing.tsx"}:
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("export {};\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "fixture")
    identity = capture_source_identity(
        source,
        mapping,
        source_name="fixture-source",
        checkout_hint="../source",
        license_claim=CLAIM,
        observed_at=datetime(2026, 7, 23, tzinfo=UTC),
    )
    return project, source, mapping, identity


def _scope(
    identity: SourceIdentityManifest,
    *,
    path_prefix: str = "src",
    allowed_modes: tuple[str, ...] = ("reference", "reimplement"),
) -> SourceLicenseScopeManifest:
    return SourceLicenseScopeManifest.model_validate({
        "schema_version": 1,
        "manifest_kind": "source_license_scope",
        "source_name": identity.source_name,
        "source_commit": identity.git.commit,
        "source_identity_sha256": source_identity_sha256(identity),
        "license_path": identity.license.path,
        "license_sha256": identity.license.sha256,
        "license_claim_sha256": hashlib.sha256(
            identity.license.claim.encode("utf-8")
        ).hexdigest(),
        "license_expression": "LicenseRef-Fixture-README",
        "review_status": "restricted_pending_legal_review",
        "redistribution_status": "unreviewed",
        "assessed_by": "codex-local-audit",
        "assessed_at": "2026-07-23T00:00:00+00:00",
        "assessment_reason": "No standalone license file; fail closed pending review.",
        "legal_review_required": True,
        "notice_path": "",
        "rules": [{
            "path_prefix": path_prefix,
            "allowed_modes": list(allowed_modes),
            "notice_requirement": "unknown",
            "rationale": "Only semantic reference and independent reimplementation are allowed.",
        }],
        "exclusions": [],
        "unmatched_policy": "reject",
    })


def test_restricted_scope_covers_every_mapped_path_deterministically(
    tmp_path: Path,
) -> None:
    project, source, _mapping, identity = _fixture(tmp_path)
    scope = _scope(identity)

    first = verify_license_scope(
        scope,
        identity,
        source_root=source,
        project_root=project,
    )
    second = verify_license_scope(
        scope,
        identity,
        source_root=source,
        project_root=project,
    )

    assert first == second
    assert first.status == "valid"
    assert first.mapped_path_count == 2
    assert first.covered_count == 2
    assert first.blocked_count == 0
    assert first.common_allowed_modes == ("reference", "reimplement")
    assert first.scope_sha256 == license_scope_sha256(scope)
    assert {item.status for item in first.decisions} == {"covered"}


def test_pending_legal_review_cannot_allow_copy_or_adapt(
    tmp_path: Path,
) -> None:
    _project, _source, _mapping, identity = _fixture(tmp_path)

    with pytest.raises(ValueError, match="只能允许 reference/reimplement"):
        _scope(identity, allowed_modes=("copy", "reference"))


def test_source_change_or_scope_binding_change_is_stale(tmp_path: Path) -> None:
    project, source, _mapping, identity = _fixture(tmp_path)
    scope = _scope(identity)
    (source / "src" / "new.tsx").write_text("export const value = 1;\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "source changed")

    changed = verify_license_scope(
        scope,
        identity,
        source_root=source,
        project_root=project,
    )
    assert changed.status == "stale"
    assert changed.finding_codes == ("source_identity_stale",)

    payload = scope.model_dump(mode="json")
    payload["source_commit"] = "a" * 40
    rebound = SourceLicenseScopeManifest.model_validate(payload)
    _git(source, "reset", "--hard", identity.git.commit)
    binding = verify_license_scope(
        rebound,
        identity,
        source_root=source,
        project_root=project,
    )
    assert binding.status == "stale"
    assert "source_commit_binding_mismatch" in binding.finding_codes


@pytest.mark.parametrize(
    ("mapped_paths", "scope_prefix", "finding"),
    [
        (("src/main.tsx", "src/other/view.tsx"), "src/main.tsx", "mapped_source_unmatched"),
        (("src/main.tsx", "src/missing.tsx"), "src", "mapped_source_missing"),
    ],
)
def test_uncovered_or_missing_mapped_paths_fail_closed(
    tmp_path: Path,
    mapped_paths: tuple[str, ...],
    scope_prefix: str,
    finding: str,
) -> None:
    project, source, _mapping, identity = _fixture(
        tmp_path,
        mapped_paths=mapped_paths,
    )
    scope = _scope(identity, path_prefix=scope_prefix)

    audit = verify_license_scope(
        scope,
        identity,
        source_root=source,
        project_root=project,
    )

    assert audit.status == "invalid"
    assert finding in audit.finding_codes
    assert audit.blocked_count == 1


def test_scope_path_symlink_escape_is_invalid(tmp_path: Path) -> None:
    project, source, mapping, _identity = _fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (source / "escape").symlink_to(outside, target_is_directory=True)
    _git(source, "add", ".")
    _git(source, "commit", "-m", "add escaped symlink fixture")
    identity = capture_source_identity(
        source,
        mapping,
        source_name="fixture-source",
        checkout_hint="../source",
        license_claim=CLAIM,
        observed_at=datetime(2026, 7, 23, tzinfo=UTC),
    )
    scope = _scope(identity, path_prefix="escape")

    audit = verify_license_scope(
        scope,
        identity,
        source_root=source,
        project_root=project,
    )

    assert audit.status == "invalid"
    assert audit.finding_codes == ("scope_evidence_path_invalid",)


def test_scope_and_audit_models_reject_tampering(tmp_path: Path) -> None:
    project, source, _mapping, identity = _fixture(tmp_path)
    scope = _scope(identity)
    target = tmp_path / "scope.json"
    write_license_scope(target, scope)
    assert load_license_scope(target) == scope

    scope_payload = scope.model_dump(mode="json")
    scope_payload["private_legal_notes"] = "must not persist"
    with pytest.raises(ValueError):
        SourceLicenseScopeManifest.model_validate(scope_payload)

    audit = verify_license_scope(
        scope,
        identity,
        source_root=source,
        project_root=project,
    )
    audit_payload = audit.model_dump(mode="json")
    audit_payload["covered_count"] = 1
    with pytest.raises(ValueError, match="covered_count"):
        SourceLicenseScopeAudit.model_validate(audit_payload)

    audit_payload = audit.model_dump(mode="json")
    audit_payload["finding_codes"] = ["BAD-CODE"]
    audit_payload["status"] = "invalid"
    with pytest.raises(ValueError, match="finding code"):
        SourceLicenseScopeAudit.model_validate(audit_payload)

    audit_payload = audit.model_dump(mode="json")
    audit_payload["finding_codes"] = ["invented_finding"]
    audit_payload["status"] = "invalid"
    with pytest.raises(ValueError, match="已治理集合"):
        SourceLicenseScopeAudit.model_validate(audit_payload)

    secret_payload = scope.model_dump(mode="json")
    secret_payload["assessment_reason"] = "token=super-secret-value"
    with pytest.raises(ValueError, match="secret"):
        SourceLicenseScopeManifest.model_validate(secret_payload)


def test_cli_verifies_scope_without_modifying_source(tmp_path: Path) -> None:
    project, source, _mapping, identity = _fixture(tmp_path)
    scope = _scope(identity)
    identity_path = project / "identity.json"
    scope_path = project / "scope.json"
    write_source_identity(identity_path, identity)
    write_license_scope(scope_path, scope)
    before = _git(source, "status", "--porcelain=v1")

    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "naumi_agent.claude_source.license_scope",
            "--scope",
            str(scope_path),
            "--identity",
            str(identity_path),
            "--source",
            str(source),
            "--project-root",
            str(project),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert process.returncode == 0
    assert json.loads(process.stdout)["status"] == "valid"
    assert _git(source, "status", "--porcelain=v1") == before == ""


def test_rule_paths_and_order_are_strict() -> None:
    with pytest.raises(ValueError, match="规范化相对路径"):
        LicenseScopeRule(
            path_prefix="../src",
            allowed_modes=("reference",),
            notice_requirement="unknown",
            rationale="invalid",
        )
    with pytest.raises(ValueError, match="排序"):
        LicenseScopeRule(
            path_prefix="src",
            allowed_modes=("reimplement", "reference"),
            notice_requirement="unknown",
            rationale="invalid order",
        )


def test_empty_mapping_fails_closed(tmp_path: Path) -> None:
    project, source, _mapping, identity = _fixture(tmp_path, mapped_paths=())
    scope = _scope(identity)

    audit = verify_license_scope(
        scope,
        identity,
        source_root=source,
        project_root=project,
    )

    assert audit.status == "invalid"
    assert audit.finding_codes == ("mapped_source_empty",)
    assert audit.mapped_path_count == 0


def test_committed_scope_is_restricted_and_matches_current_source_when_available() -> None:
    scope = load_license_scope(
        PROJECT_ROOT / "frontend" / "terminal-ui" / "cc-license-scope.v1.json"
    )
    identity = load_source_identity(
        PROJECT_ROOT / "frontend" / "terminal-ui" / "cc-source-map.v2.json"
    )
    assert scope.review_status == "restricted_pending_legal_review"
    assert scope.redistribution_status == "unreviewed"
    assert {
        mode for rule in scope.rules for mode in rule.allowed_modes
    } == {"reference", "reimplement"}
    if not LOCAL_CLAUDE_SOURCE.is_dir():
        pytest.skip("local Claude source checkout unavailable")

    audit = verify_license_scope(
        scope,
        identity,
        source_root=LOCAL_CLAUDE_SOURCE,
        project_root=PROJECT_ROOT,
    )
    assert audit.status == "valid"
    assert audit.mapped_path_count == audit.covered_count == 28
    assert audit.blocked_count == 0
