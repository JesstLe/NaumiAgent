from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from naumi_agent.claude_source.governance import (
    SourceIdentityManifest,
    capture_source_identity,
    load_source_identity,
    verify_source_identity,
    write_source_identity,
)

CLAIM = "本项目内容可自由复用、参考和学习"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    project = tmp_path / "project"
    source = tmp_path / "claude-code"
    mapping = project / "frontend" / "terminal-ui" / "cc-source-map.json"
    mapping.parent.mkdir(parents=True)
    mapping.write_text('{"mapping": []}\n', encoding="utf-8")
    source.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.email", "source@example.invalid")
    _git(source, "config", "user.name", "Source Fixture")
    _git(source, "remote", "add", "origin", "https://example.invalid/claude-code.git")
    (source / "README.md").write_text(f"# source\n\n{CLAIM}\n", encoding="utf-8")
    (source / "src").mkdir()
    (source / "src" / "main.tsx").write_text("export {};\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "fixture")
    return project, source, mapping


def _capture(source: Path, mapping: Path, *, dirty_reason: str = "") -> SourceIdentityManifest:
    return capture_source_identity(
        source,
        mapping,
        source_name="local-claude-code",
        checkout_hint="../claude-code",
        license_claim=CLAIM,
        dirty_reason=dirty_reason,
        observed_at=datetime(2026, 7, 18, tzinfo=UTC),
    )


def test_capture_write_load_and_verify_clean_source(tmp_path: Path) -> None:
    project, source, mapping = _fixture(tmp_path)
    manifest = _capture(source, mapping)

    assert manifest.git.commit == _git(source, "rev-parse", "HEAD")
    assert manifest.git.remote == "https://example.invalid/claude-code.git"
    assert manifest.git.dirty is False
    assert manifest.git.worktree_sha256 is None
    assert manifest.license.claim == CLAIM
    assert manifest.legacy_mapping.path == "frontend/terminal-ui/cc-source-map.json"

    target = project / "frontend" / "terminal-ui" / "cc-source-map.v2.json"
    write_source_identity(target, manifest)
    loaded = load_source_identity(target)
    assert loaded == manifest
    assert verify_source_identity(loaded, source, project).status == "valid"


def test_changed_commit_and_mapping_are_stale(tmp_path: Path) -> None:
    project, source, mapping = _fixture(tmp_path)
    manifest = _capture(source, mapping)
    (source / "src" / "second.tsx").write_text("export const second = true;\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "source changed")
    mapping.write_text('{"mapping": [{"area": "changed"}]}\n', encoding="utf-8")

    result = verify_source_identity(manifest, source, project)

    assert result.status == "stale"
    assert "source commit 已变化" in "\n".join(result.findings)
    assert "legacy source map 已变化" in "\n".join(result.findings)


def test_dirty_source_requires_reason_and_digest_changes(tmp_path: Path) -> None:
    project, source, mapping = _fixture(tmp_path)
    del project
    (source / "src" / "main.tsx").write_text("export const dirty = true;\n", encoding="utf-8")
    with pytest.raises(ValueError, match="dirty_reason"):
        _capture(source, mapping)

    first = _capture(source, mapping, dirty_reason="本地实验分支，仅用于行为审计。")
    assert first.git.dirty is True
    assert first.git.worktree_sha256
    serialized = json.dumps(first.model_dump(mode="json"), ensure_ascii=False)
    assert "export const dirty" not in serialized

    (source / "untracked.txt").write_text("first", encoding="utf-8")
    second = _capture(source, mapping, dirty_reason="本地实验分支，仅用于行为审计。")
    (source / "untracked.txt").write_text("second", encoding="utf-8")
    third = _capture(source, mapping, dirty_reason="本地实验分支，仅用于行为审计。")
    digests = {
        first.git.worktree_sha256,
        second.git.worktree_sha256,
        third.git.worktree_sha256,
    }
    assert len(digests) == 3


def test_missing_license_claim_is_rejected(tmp_path: Path) -> None:
    _project, source, mapping = _fixture(tmp_path)
    (source / "README.md").write_text("# claim removed\n", encoding="utf-8")

    with pytest.raises(ValueError, match="许可证声明"):
        _capture(source, mapping, dirty_reason="许可证审计中的本地变更。")


def test_manifest_rejects_unknown_or_inconsistent_fields(tmp_path: Path) -> None:
    _project, source, mapping = _fixture(tmp_path)
    payload = _capture(source, mapping).model_dump(mode="json")
    payload["private_source_text"] = "not allowed"
    with pytest.raises(ValueError):
        SourceIdentityManifest.model_validate(payload)

    payload.pop("private_source_text")
    payload["git"]["dirty"] = True
    with pytest.raises(ValueError, match="dirty source"):
        SourceIdentityManifest.model_validate(payload)

    payload["git"]["dirty"] = False
    payload["git"]["remote"] = "https://embedded-token@example.invalid/source.git"
    with pytest.raises(ValueError, match="内嵌凭据"):
        SourceIdentityManifest.model_validate(payload)


def test_non_git_directory_is_invalid_instead_of_crashing(tmp_path: Path) -> None:
    project, source, mapping = _fixture(tmp_path)
    manifest = _capture(source, mapping)
    not_git = tmp_path / "not-git"
    not_git.mkdir()

    result = verify_source_identity(manifest, not_git, project)

    assert result.status == "invalid"
    assert result.findings == ("source Git 身份不可读。",)
