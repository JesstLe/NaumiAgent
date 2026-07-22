from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from naumi_agent.claude_source.baseline import (
    SourceBaselineObservation,
    observe_source_baseline,
)
from naumi_agent.claude_source.governance import (
    SourceIdentityManifest,
    capture_source_identity,
    load_source_identity,
    write_source_identity,
)
from naumi_agent.claude_source.refresh import SourceRefreshStore

CLAIM = "本项目内容可自由复用、参考和学习"
NOW = datetime(2026, 7, 23, 10, 0, tzinfo=UTC)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    project = tmp_path / "project"
    source = tmp_path / "claude-code"
    mapping = project / "frontend" / "terminal-ui" / "cc-source-map.json"
    manifest_path = mapping.with_name("cc-source-map.v2.json")
    mapping.parent.mkdir(parents=True)
    mapping.write_text('{"source": {}, "mapping": []}\n', encoding="utf-8")
    source.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.email", "source@example.invalid")
    _git(source, "config", "user.name", "Source Fixture")
    _git(source, "remote", "add", "origin", "https://example.invalid/source.git")
    (source / "README.md").write_text(f"# source\n\n{CLAIM}\n", encoding="utf-8")
    (source / "src").mkdir()
    (source / "src" / "main.tsx").write_text(
        "export const first = true;\n",
        encoding="utf-8",
    )
    _git(source, "add", ".")
    _git(source, "commit", "-m", "initial")
    manifest = _capture(source, mapping, observed_at=NOW)
    write_source_identity(manifest_path, manifest)
    return project, source, mapping, manifest_path


def _capture(
    source: Path,
    mapping: Path,
    *,
    observed_at: datetime,
) -> SourceIdentityManifest:
    return capture_source_identity(
        source,
        mapping,
        source_name="local-claude-code",
        checkout_hint="../claude-code",
        license_claim=CLAIM,
        observed_at=observed_at,
    )


def _approved_store(
    tmp_path: Path,
    manifest: SourceIdentityManifest,
    source: Path,
    project: Path,
) -> SourceRefreshStore:
    store = SourceRefreshStore(tmp_path / "state" / "claude-source.db")
    store.bootstrap(
        manifest,
        source_root=source,
        project_root=project,
        reviewed_by="maintainer",
        review_reason="导入已批准 source identity 基线。",
        reviewed_at=NOW,
    )
    return store


def test_current_observation_is_deterministic_and_read_only(tmp_path: Path) -> None:
    project, source, _mapping, manifest_path = _fixture(tmp_path)
    manifest = load_source_identity(manifest_path)
    store = _approved_store(tmp_path, manifest, source, project)
    before_mtime = store.db_path.stat().st_mtime_ns

    first = observe_source_baseline(
        store,
        manifest_path=manifest_path,
        source_root=source,
        project_root=project,
    )
    second = observe_source_baseline(
        store,
        manifest_path=manifest_path,
        source_root=source,
        project_root=project,
    )

    assert first == second
    assert first.status == "current"
    assert first.audit.status == "valid"
    assert first.baseline_revision == 1
    assert first.approved_git.commit == manifest.git.commit
    assert first.license.sha256 == manifest.license.sha256
    assert first.mapping.sha256 == manifest.legacy_mapping.sha256
    assert first.mapping_format == "legacy_unversioned_v1"
    assert first.mapping_schema_version is None
    assert store.db_path.stat().st_mtime_ns == before_mtime


def test_live_commit_change_updates_observation_without_advancing_history(
    tmp_path: Path,
) -> None:
    project, source, _mapping, manifest_path = _fixture(tmp_path)
    manifest = load_source_identity(manifest_path)
    store = _approved_store(tmp_path, manifest, source, project)
    current = observe_source_baseline(
        store,
        manifest_path=manifest_path,
        source_root=source,
        project_root=project,
    )
    (source / "src" / "second.tsx").write_text(
        "export const second = true;\n",
        encoding="utf-8",
    )
    _git(source, "add", ".")
    _git(source, "commit", "-m", "new upstream commit")

    changed = observe_source_baseline(
        store,
        manifest_path=manifest_path,
        source_root=source,
        project_root=project,
    )

    assert changed.status == "change_detected"
    assert changed.audit.status == "stale"
    assert changed.approved_git.commit == manifest.git.commit
    assert changed.audit.current_commit == _git(source, "rev-parse", "HEAD")
    assert changed.observation_id != current.observation_id
    assert [item.revision for item in store.list_history(manifest.source_name)] == [1]
    command = subprocess.run(
        [
            "python3",
            "-m",
            "naumi_agent.claude_source.baseline",
            "--manifest",
            str(manifest_path),
            "--source",
            str(source),
            "--project-root",
            str(project),
            "--store",
            str(store.db_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert command.returncode == 1
    assert json.loads(command.stdout)["status"] == "change_detected"


def test_manifest_must_match_approved_history(tmp_path: Path) -> None:
    project, source, mapping, manifest_path = _fixture(tmp_path)
    approved = load_source_identity(manifest_path)
    store = _approved_store(tmp_path, approved, source, project)
    (source / "src" / "next.tsx").write_text("next\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "unapproved source")
    write_source_identity(
        manifest_path,
        _capture(source, mapping, observed_at=NOW + timedelta(minutes=1)),
    )

    with pytest.raises(ValueError, match="manifest 与已批准"):
        observe_source_baseline(
            store,
            manifest_path=manifest_path,
            source_root=source,
            project_root=project,
        )


def test_absent_history_fails_without_creating_store(tmp_path: Path) -> None:
    project, source, _mapping, manifest_path = _fixture(tmp_path)
    store = SourceRefreshStore(tmp_path / "absent" / "claude-source.db")

    with pytest.raises(ValueError, match="尚无已批准"):
        observe_source_baseline(
            store,
            manifest_path=manifest_path,
            source_root=source,
            project_root=project,
        )

    assert not store.db_path.exists()


def test_invalid_live_source_is_reported_without_losing_approved_baseline(
    tmp_path: Path,
) -> None:
    project, source, _mapping, manifest_path = _fixture(tmp_path)
    manifest = load_source_identity(manifest_path)
    store = _approved_store(tmp_path, manifest, source, project)

    observation = observe_source_baseline(
        store,
        manifest_path=manifest_path,
        source_root=tmp_path / "missing-source",
        project_root=project,
    )

    assert observation.status == "invalid"
    assert observation.audit.status == "invalid"
    assert observation.approved_git.commit == manifest.git.commit


def test_observation_digest_and_cli_contract(tmp_path: Path) -> None:
    project, source, _mapping, manifest_path = _fixture(tmp_path)
    manifest = load_source_identity(manifest_path)
    store = _approved_store(tmp_path, manifest, source, project)
    observation = observe_source_baseline(
        store,
        manifest_path=manifest_path,
        source_root=source,
        project_root=project,
    )
    payload = observation.model_dump(mode="json")
    payload["baseline_revision"] = 2
    with pytest.raises(ValueError, match="observation digest"):
        SourceBaselineObservation.model_validate(payload)

    command = subprocess.run(
        [
            "python3",
            "-m",
            "naumi_agent.claude_source.baseline",
            "--manifest",
            str(manifest_path),
            "--source",
            str(source),
            "--project-root",
            str(project),
            "--store",
            str(store.db_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    rendered = json.loads(command.stdout)
    assert rendered["observation_id"] == observation.observation_id
    assert rendered["status"] == "current"
