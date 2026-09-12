from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from naumi_agent.claude_source.governance import (
    SourceIdentityManifest,
    capture_source_identity,
    write_source_identity,
)
from naumi_agent.claude_source.refresh import SourceRefreshStore
from naumi_agent.claude_source.structural_diff import (
    SourceStructuralDiff,
    build_source_structural_diff,
)

CLAIM = "Structural diff fixture license claim"
NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _fixture(
    tmp_path: Path,
) -> tuple[Path, Path, Path, Path, SourceRefreshStore, SourceIdentityManifest]:
    project = tmp_path / "project"
    source = tmp_path / "claude-code"
    mapping = project / "frontend" / "terminal-ui" / "cc-source-map.json"
    manifest_path = mapping.with_name("cc-source-map.v2.json")
    mapping.parent.mkdir(parents=True)
    mapping.write_text(
        json.dumps(
            {
                "source": {"name": "local-claude-code"},
                "mapping": [
                    {
                        "area": "entrypoint",
                        "claude_code": ["src/main.tsx"],
                        "naumi_agent": ["frontend/terminal-ui/src/index.js"],
                    },
                    {
                        "area": "legacy-component",
                        "claude_code": ["src/old.tsx"],
                        "naumi_agent": ["frontend/terminal-ui/src/components/core.js"],
                    },
                ],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    source.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.email", "source@example.invalid")
    _git(source, "config", "user.name", "Source Fixture")
    _git(source, "remote", "add", "origin", "https://example.invalid/source.git")
    (source / "README.md").write_text(f"# source\n\n{CLAIM}\n", encoding="utf-8")
    (source / "package.json").write_text('{"dependencies":{}}\n', encoding="utf-8")
    (source / "src").mkdir()
    (source / "src" / "main.tsx").write_text("export const main = true;\n", encoding="utf-8")
    (source / "src" / "old.tsx").write_text("export const old = true;\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "approved baseline")
    manifest = capture_source_identity(
        source,
        mapping,
        source_name="local-claude-code",
        checkout_hint="../claude-code",
        license_path="README.md",
        license_claim=CLAIM,
        observed_at=NOW,
    )
    write_source_identity(manifest_path, manifest)
    store = SourceRefreshStore(tmp_path / "state" / "claude-source.db")
    store.bootstrap(
        manifest,
        source_root=source,
        project_root=project,
        reviewed_by="maintainer",
        review_reason="Approve structural diff baseline.",
        reviewed_at=NOW,
    )
    return project, source, mapping, manifest_path, store, manifest


def test_unchanged_structural_diff_is_deterministic_and_read_only(tmp_path: Path) -> None:
    project, source, _mapping, manifest_path, store, manifest = _fixture(tmp_path)
    before_mtime = store.db_path.stat().st_mtime_ns

    first = build_source_structural_diff(
        store,
        manifest_path=manifest_path,
        source_root=source,
        project_root=project,
    )
    second = build_source_structural_diff(
        store,
        manifest_path=manifest_path,
        source_root=source,
        project_root=project,
    )

    assert first == second
    assert first.status == "unchanged"
    assert first.baseline_commit == manifest.git.commit
    assert first.current_commit == manifest.git.commit
    assert first.changes == ()
    assert first.risk_flags == ()
    assert first.mapping_status == "current"
    assert store.db_path.stat().st_mtime_ns == before_mtime


def test_committed_tree_diff_detects_rename_delete_dependency_and_mapped_impact(
    tmp_path: Path,
) -> None:
    project, source, _mapping, manifest_path, store, _manifest = _fixture(tmp_path)
    (source / "src" / "main.tsx").rename(source / "src" / "app.tsx")
    (source / "src" / "old.tsx").unlink()
    (source / "src" / "new.tsx").write_text("export const fresh = true;\n", encoding="utf-8")
    (source / "package.json").write_text('{"dependencies":{"ink":"1.0.0"}}\n', encoding="utf-8")
    _git(source, "add", "-A")
    _git(source, "commit", "-m", "upstream structure changes")

    receipt = build_source_structural_diff(
        store,
        manifest_path=manifest_path,
        source_root=source,
        project_root=project,
    )

    assert receipt.status == "change_detected"
    assert receipt.includes_worktree is False
    assert {item.kind for item in receipt.changes} == {
        "added",
        "deleted",
        "modified",
        "renamed",
    }
    renamed = next(item for item in receipt.changes if item.kind == "renamed")
    assert (renamed.old_path, renamed.new_path, renamed.similarity) == (
        "src/main.tsx",
        "src/app.tsx",
        100,
    )
    assert receipt.counts == {"added": 1, "deleted": 1, "modified": 1, "renamed": 1}
    assert {item.area for item in receipt.mapped_impacts} == {
        "entrypoint",
        "legacy-component",
    }
    assert "dependency_manifest_changed" in receipt.risk_flags
    assert "mapped_path_deleted" in receipt.risk_flags


def test_dirty_worktree_and_mapping_change_are_visible_without_writes(tmp_path: Path) -> None:
    project, source, mapping, manifest_path, store, _manifest = _fixture(tmp_path)
    (source / "src" / "main.tsx").write_text("export const main = false;\n", encoding="utf-8")
    (source / "src" / "untracked.tsx").write_text("untracked\n", encoding="utf-8")
    mapping.write_text('{"mapping":[]}\n', encoding="utf-8")

    receipt = build_source_structural_diff(
        store,
        manifest_path=manifest_path,
        source_root=source,
        project_root=project,
    )

    assert receipt.status == "change_detected"
    assert receipt.includes_worktree is True
    assert {(item.kind, item.new_path) for item in receipt.changes} == {
        ("added", "src/untracked.tsx"),
        ("modified", "src/main.tsx"),
    }
    assert receipt.mapping_status == "stale"
    assert receipt.mapped_impacts == ()
    assert set(receipt.risk_flags) == {"mapping_changed", "uncommitted_source"}


def test_license_change_and_invalid_source_are_factual(tmp_path: Path) -> None:
    project, source, _mapping, manifest_path, store, _manifest = _fixture(tmp_path)
    (source / "README.md").write_text(f"# changed\n\n{CLAIM}\n", encoding="utf-8")
    _git(source, "add", "README.md")
    _git(source, "commit", "-m", "license evidence changed")

    changed = build_source_structural_diff(
        store,
        manifest_path=manifest_path,
        source_root=source,
        project_root=project,
    )
    invalid = build_source_structural_diff(
        store,
        manifest_path=manifest_path,
        source_root=tmp_path / "missing-source",
        project_root=project,
    )

    assert changed.status == "change_detected"
    assert "license_changed" in changed.risk_flags
    assert invalid.status == "invalid"
    assert invalid.errors == ("source checkout 不存在。",)
    assert invalid.changes == ()


def test_receipt_integrity_and_real_cli(tmp_path: Path) -> None:
    project, source, _mapping, manifest_path, store, _manifest = _fixture(tmp_path)
    receipt = build_source_structural_diff(
        store,
        manifest_path=manifest_path,
        source_root=source,
        project_root=project,
    )
    payload = receipt.model_dump(mode="json")
    payload["mapping_status"] = "stale"
    with pytest.raises(ValueError, match="digest 不一致"):
        SourceStructuralDiff.model_validate(payload)

    command = subprocess.run(
        [
            sys.executable,
            "-m",
            "naumi_agent.claude_source.structural_diff",
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
    assert rendered["diff_id"] == receipt.diff_id
    assert rendered["status"] == "unchanged"


def test_dirty_approved_baseline_fails_closed(tmp_path: Path) -> None:
    project, source, mapping, manifest_path, _store, _manifest = _fixture(tmp_path)
    dirty_store = SourceRefreshStore(tmp_path / "dirty-state" / "claude-source.db")
    (source / "src" / "main.tsx").write_text("approved but dirty\n", encoding="utf-8")
    dirty_manifest = capture_source_identity(
        source,
        mapping,
        source_name="local-claude-code",
        checkout_hint="../claude-code",
        license_path="README.md",
        license_claim=CLAIM,
        dirty_reason="Explicit historical research baseline.",
        observed_at=NOW,
    )
    write_source_identity(manifest_path, dirty_manifest)
    dirty_store.bootstrap(
        dirty_manifest,
        source_root=source,
        project_root=project,
        reviewed_by="maintainer",
        review_reason="Approve explicit dirty fixture.",
        reviewed_at=NOW,
    )

    receipt = build_source_structural_diff(
        dirty_store,
        manifest_path=manifest_path,
        source_root=source,
        project_root=project,
    )

    assert receipt.status == "invalid"
    assert receipt.includes_worktree is False
    assert "无法重建" in receipt.errors[0]
    assert receipt.changes == ()


def test_missing_approved_git_object_returns_typed_invalid_receipt(tmp_path: Path) -> None:
    project, _source, _mapping, manifest_path, store, _manifest = _fixture(tmp_path)
    unrelated = tmp_path / "unrelated-checkout"
    unrelated.mkdir()
    _git(unrelated, "init", "-b", "main")
    _git(unrelated, "config", "user.email", "source@example.invalid")
    _git(unrelated, "config", "user.name", "Source Fixture")
    _git(unrelated, "remote", "add", "origin", "https://example.invalid/source.git")
    (unrelated / "README.md").write_text(f"# unrelated\n\n{CLAIM}\n", encoding="utf-8")
    (unrelated / "src").mkdir()
    (unrelated / "src" / "main.tsx").write_text("unrelated history\n", encoding="utf-8")
    _git(unrelated, "add", ".")
    _git(unrelated, "commit", "-m", "unrelated root")

    receipt = build_source_structural_diff(
        store,
        manifest_path=manifest_path,
        source_root=unrelated,
        project_root=project,
    )

    assert receipt.status == "invalid"
    assert receipt.mapping_status == "unavailable"
    assert receipt.errors == ("Git structural diff 无法读取已批准 commit。",)
