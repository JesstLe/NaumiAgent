"""HAR-06.4 workspace-scoped Artifact garbage collection tests."""

from __future__ import annotations

from pathlib import Path

from naumi_agent.harness.artifact_gc import ArtifactGarbageCollector
from naumi_agent.harness.reconciliation import (
    ReconciliationArtifactKind,
    ReconciliationArtifactReference,
)


def _check(value: str) -> ReconciliationArtifactReference:
    return ReconciliationArtifactReference(
        kind=ReconciliationArtifactKind.CHECK_PATH,
        value=value,
    )


def _evidence(value: str) -> ReconciliationArtifactReference:
    return ReconciliationArtifactReference(
        kind=ReconciliationArtifactKind.EVIDENCE_URI,
        value=value,
    )


def test_gc_deduplicates_aliases_and_deletes_regular_artifact(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    artifact = workspace / "artifacts" / "run-1" / "unit.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("ok", encoding="utf-8")
    collector = ArtifactGarbageCollector(workspace)

    result = collector.collect(
        (
            _check("artifacts/run-1/unit.txt"),
            _evidence("artifact://artifacts/run-1/unit.txt"),
        ),
        (),
    )

    assert artifact.exists() is False
    assert result.candidate_count == 1
    assert result.deleted_count == 1
    assert result.shared_count == 0
    assert result.unsafe_reference_count == 0


def test_gc_preserves_shared_artifact_across_reference_aliases(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    artifact = workspace / ".naumi" / "artifacts" / "shared.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("shared", encoding="utf-8")
    collector = ArtifactGarbageCollector(workspace)

    result = collector.collect(
        (_check(str(artifact)),),
        (_evidence("artifact://.naumi/artifacts/shared.json"),),
    )

    assert artifact.read_text(encoding="utf-8") == "shared"
    assert result.deleted_count == 0
    assert result.shared_count == 1


def test_gc_fails_closed_for_unsafe_non_artifact_and_symlink_paths(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    artifacts = workspace / "artifacts"
    artifacts.mkdir(parents=True)
    ordinary = workspace / "README.md"
    ordinary.write_text("keep", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = artifacts / "outside-link"
    try:
        link.symlink_to(outside)
    except OSError:
        link = None
    directory = artifacts / "directory"
    directory.mkdir()
    collector = ArtifactGarbageCollector(workspace)

    references = [
        _check("README.md"),
        _check("artifacts/../../outside.txt"),
        _evidence("artifact://artifacts/%2e%2e/%2e%2e/outside.txt"),
        _check("artifacts/directory"),
    ]
    if link is not None:
        references.append(_check("artifacts/outside-link"))
    result = collector.collect(tuple(references), ())

    assert ordinary.read_text(encoding="utf-8") == "keep"
    assert outside.read_text(encoding="utf-8") == "outside"
    if link is not None:
        assert link.is_symlink()
    assert directory.is_dir()
    assert result.deleted_count == 0
    assert result.unsafe_reference_count == (4 if link is not None else 3)
    assert result.non_file_count == 1


def test_gc_preserves_all_candidates_when_live_reference_is_malformed(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    artifact = workspace / "artifacts" / "candidate.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("keep", encoding="utf-8")
    collector = ArtifactGarbageCollector(workspace)

    result = collector.collect(
        (_check("artifacts/candidate.txt"),),
        (_evidence("artifact://artifacts/%00broken"),),
    )

    assert artifact.exists()
    assert result.blocked_by_unresolved_live_reference is True
    assert result.shared_count == 1


def test_gc_normalizes_windows_separators_without_accepting_foreign_drive(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    artifact = workspace / "artifacts" / "windows" / "unit.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("ok", encoding="utf-8")
    collector = ArtifactGarbageCollector(workspace)

    result = collector.collect(
        (
            _check(r"artifacts\windows\unit.txt"),
            _check(r"C:\foreign\artifact.txt"),
        ),
        (),
    )

    assert artifact.exists() is False
    assert result.deleted_count == 1
    assert result.unsafe_reference_count == 1
