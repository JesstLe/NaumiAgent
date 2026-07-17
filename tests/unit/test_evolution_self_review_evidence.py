from __future__ import annotations

from pathlib import Path

from naumi_agent.evolution.evidence import adapt_self_review_static_evidence
from naumi_agent.evolution.self_review import (
    SelfReviewFindingCode,
    render_self_review_static_scan,
    scan_self_review_files,
)


def _write_source(path: Path, *, leading_lines: int = 0) -> None:
    path.write_text(
        ("\n" * leading_lines)
        + '''
token = "super-secret-value"
cache = []

def public(value):
    try:
        return value
    except:
        return None

def broad(value) -> object:
    try:
        return value
    except Exception:
        return None

def local_secret() -> str:
    password = "function-secret-value"
    return password
'''.lstrip(),
        encoding="utf-8",
    )


def test_structured_self_review_scan_is_ast_based_and_redacted(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    _write_source(source)

    scan = scan_self_review_files([source], workspace_root=tmp_path)
    codes = {finding.code for finding in scan.findings}

    assert scan.files_scanned == 1
    assert scan.errors == ()
    assert SelfReviewFindingCode.HARDCODED_SECRET in codes
    assert SelfReviewFindingCode.MUTABLE_GLOBAL in codes
    assert SelfReviewFindingCode.UNTYPED_PUBLIC_RETURN in codes
    assert SelfReviewFindingCode.BARE_EXCEPT in codes
    assert SelfReviewFindingCode.BROAD_EXCEPT in codes
    assert sum(
        finding.code is SelfReviewFindingCode.HARDCODED_SECRET
        for finding in scan.findings
    ) == 2
    rendered = render_self_review_static_scan(scan)
    assert "值已隐藏" in rendered
    assert "super-secret-value" not in rendered


def test_static_findings_become_digest_only_evolution_evidence(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    _write_source(source)
    first_scan = scan_self_review_files([source], workspace_root=tmp_path)
    first = adapt_self_review_static_evidence(first_scan)

    secret = next(item for item in first if item.finding_code == "hardcoded_secret")
    assert secret.source_kind == "self_review_static"
    assert secret.failure_class is None
    assert secret.scope == "module.py:token"
    assert secret.source_uri == "artifact://workspace/module.py"
    assert len(secret.refs[0].sha256) == 64
    assert "super-secret-value" not in secret.model_dump_json()

    repeated = adapt_self_review_static_evidence(
        scan_self_review_files([source], workspace_root=tmp_path)
    )
    assert repeated == first

    _write_source(source, leading_lines=2)
    shifted = adapt_self_review_static_evidence(
        scan_self_review_files([source], workspace_root=tmp_path)
    )
    shifted_secret = next(
        item for item in shifted if item.finding_code == "hardcoded_secret"
    )
    assert shifted_secret.root_fingerprint == secret.root_fingerprint
    assert shifted_secret.evidence_id != secret.evidence_id


def test_self_review_scan_rejects_files_outside_declared_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("def public():\n    return 1\n", encoding="utf-8")

    scan = scan_self_review_files([outside], workspace_root=workspace)

    assert scan.files_scanned == 0
    assert scan.findings == ()
    assert scan.errors[0].code == "outside_or_missing"
    assert str(tmp_path) not in scan.errors[0].path


def test_scanner_ignores_enum_labels_and_dunder_export_lists(tmp_path: Path) -> None:
    source = tmp_path / "labels.py"
    source.write_text(
        'HARDCODED_SECRET = "hardcoded_secret"\n__all__ = ["HARDCODED_SECRET"]\n',
        encoding="utf-8",
    )

    scan = scan_self_review_files([source], workspace_root=tmp_path)

    assert scan.findings == ()
