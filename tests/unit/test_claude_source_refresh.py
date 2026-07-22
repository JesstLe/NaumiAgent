from __future__ import annotations

import json
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from naumi_agent.claude_source.governance import (
    SourceIdentityManifest,
    capture_source_identity,
    load_source_identity,
    verify_source_identity,
    write_source_identity,
)
from naumi_agent.claude_source.refresh import (
    CLAUDE_SOURCE_STORE_SCHEMA_VERSION,
    SourceIdentityHistoryEntry,
    SourceRefreshProposal,
    SourceRefreshStore,
    approve_source_refresh,
    assess_source_refresh,
    manifest_sha256,
)

CLAIM = "本项目内容可自由复用、参考和学习"
NOW = datetime(2026, 7, 23, 8, 0, tzinfo=UTC)


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
    mapping.write_text('{"mapping": []}\n', encoding="utf-8")
    source.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.email", "source@example.invalid")
    _git(source, "config", "user.name", "Source Fixture")
    _git(source, "remote", "add", "origin", "https://example.invalid/claude-code.git")
    (source / "README.md").write_text(f"# source\n\n{CLAIM}\n", encoding="utf-8")
    (source / "src").mkdir()
    (source / "src" / "main.tsx").write_text("export const first = true;\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "initial")
    current = _capture(source, mapping, observed_at=NOW)
    write_source_identity(manifest_path, current)
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


def _bootstrap(
    store: SourceRefreshStore,
    manifest: SourceIdentityManifest,
    source: Path,
    project: Path,
) -> SourceIdentityHistoryEntry:
    return store.bootstrap(
        manifest,
        source_root=source,
        project_root=project,
        reviewed_by="maintainer@example.invalid",
        review_reason="导入已经完成审核的 source identity 基线。",
        reviewed_at=NOW,
    )


def _commit_source_change(source: Path, *, text: str = "export const second = true;\n") -> None:
    (source / "src" / "second.tsx").write_text(text, encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "source changed")


def test_bootstrap_is_valid_idempotent_and_tamper_evident(tmp_path: Path) -> None:
    project, source, _mapping, manifest_path = _fixture(tmp_path)
    manifest = load_source_identity(manifest_path)
    store = SourceRefreshStore(tmp_path / "state" / "source.db")

    first = _bootstrap(store, manifest, source, project)
    second = _bootstrap(store, manifest, source, project)

    assert first == second
    assert first.revision == 1
    assert first.previous_entry_id == ""
    assert first.manifest_sha256 == manifest_sha256(manifest)
    assert store.latest(manifest.source_name) == first
    assert SourceRefreshStore(store.db_path).list_history(manifest.source_name) == (first,)
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == (
            CLAUDE_SOURCE_STORE_SCHEMA_VERSION
        )

    payload = first.model_dump(mode="json")
    payload["review_reason"] = "tampered"
    with pytest.raises(ValueError, match="entry digest"):
        SourceIdentityHistoryEntry.model_validate(payload)


def test_store_rejects_unknown_schema_and_malformed_v1_without_repair(
    tmp_path: Path,
) -> None:
    future_path = tmp_path / "future.db"
    with sqlite3.connect(future_path) as db:
        db.execute("PRAGMA user_version = 2")

    with pytest.raises(ValueError, match="版本不受支持"):
        SourceRefreshStore(future_path).list_history("local-claude-code")

    malformed_path = tmp_path / "malformed.db"
    with sqlite3.connect(malformed_path) as db:
        db.execute("CREATE TABLE source_identity_history (entry_id TEXT PRIMARY KEY)")
        db.execute("PRAGMA user_version = 1")

    with pytest.raises(ValueError, match="表结构无效"):
        SourceRefreshStore(malformed_path).list_history("local-claude-code")
    with sqlite3.connect(malformed_path) as db:
        columns = db.execute("PRAGMA table_info(source_identity_history)").fetchall()
        assert [row[1] for row in columns] == ["entry_id"]


def test_history_rejects_projection_tampering_and_deleted_chain_entry(
    tmp_path: Path,
) -> None:
    project, source, _mapping, manifest_path = _fixture(tmp_path)
    current = load_source_identity(manifest_path)
    projection_store = SourceRefreshStore(tmp_path / "projection.db")
    baseline = _bootstrap(projection_store, current, source, project)
    with sqlite3.connect(projection_store.db_path) as db:
        db.execute(
            "UPDATE source_identity_history SET reviewed_at = ? WHERE entry_id = ?",
            ("2026-07-23T09:00:00+00:00", baseline.entry_id),
        )
    with pytest.raises(ValueError, match="投影列"):
        projection_store.latest(current.source_name)

    chain_store = SourceRefreshStore(tmp_path / "chain.db")
    _bootstrap(chain_store, current, source, project)
    _commit_source_change(source)
    assessment = assess_source_refresh(
        chain_store,
        current,
        source_root=source,
        project_root=project,
        observed_at=NOW + timedelta(minutes=1),
    )
    assert assessment.proposal is not None
    approved = chain_store.approve(
        assessment.proposal.proposal_id,
        source_root=source,
        project_root=project,
        reviewed_by="maintainer",
        review_reason="构造两段审批历史以验证断链检测。",
    )[1]
    with sqlite3.connect(chain_store.db_path) as db:
        db.execute("DELETE FROM source_identity_history WHERE revision = 1")
    with pytest.raises(ValueError, match="链断裂"):
        chain_store.list_history(approved.source_name)


def test_unchanged_source_is_idempotent_and_creates_no_proposal(tmp_path: Path) -> None:
    project, source, _mapping, manifest_path = _fixture(tmp_path)
    current = load_source_identity(manifest_path)
    store = SourceRefreshStore(tmp_path / "source.db")
    _bootstrap(store, current, source, project)

    assessment = assess_source_refresh(
        store,
        current,
        source_root=source,
        project_root=project,
        observed_at=NOW + timedelta(minutes=1),
    )

    assert assessment.status == "unchanged"
    assert assessment.audit.status == "valid"
    assert assessment.proposal is None
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT count(*) FROM source_refresh_proposals").fetchone()[0] == 0


def test_changed_commit_creates_one_stable_pending_proposal(tmp_path: Path) -> None:
    project, source, _mapping, manifest_path = _fixture(tmp_path)
    current = load_source_identity(manifest_path)
    store = SourceRefreshStore(tmp_path / "source.db")
    baseline = _bootstrap(store, current, source, project)
    _commit_source_change(source)

    first = assess_source_refresh(
        store,
        current,
        source_root=source,
        project_root=project,
        observed_at=NOW + timedelta(minutes=1),
    )
    second = assess_source_refresh(
        store,
        current,
        source_root=source,
        project_root=project,
        observed_at=NOW + timedelta(minutes=2),
    )

    assert first.status == second.status == "change_detected"
    assert first.proposal is not None
    assert second.proposal == first.proposal
    assert first.proposal.base_entry_id == baseline.entry_id
    assert first.proposal.changes == ("git.commit",)
    assert first.proposal.status == "pending"
    serialized = first.proposal.model_dump_json()
    assert "export const second" not in serialized
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT count(*) FROM source_refresh_proposals").fetchone()[0] == 1


def test_approval_appends_history_and_materializes_manifest_once(tmp_path: Path) -> None:
    project, source, _mapping, manifest_path = _fixture(tmp_path)
    current = load_source_identity(manifest_path)
    store = SourceRefreshStore(tmp_path / "source.db")
    baseline = _bootstrap(store, current, source, project)
    _commit_source_change(source)
    assessment = assess_source_refresh(
        store,
        current,
        source_root=source,
        project_root=project,
        observed_at=NOW + timedelta(minutes=1),
    )
    assert assessment.proposal is not None

    approved = approve_source_refresh(
        store,
        assessment.proposal.proposal_id,
        manifest_path=manifest_path,
        source_root=source,
        project_root=project,
        reviewed_by="maintainer@example.invalid",
        review_reason="已审查 source commit 差异，批准更新身份基线。",
        decided_at=NOW + timedelta(minutes=2),
    )
    repeated = approve_source_refresh(
        store,
        assessment.proposal.proposal_id,
        manifest_path=manifest_path,
        source_root=source,
        project_root=project,
        reviewed_by="maintainer@example.invalid",
        review_reason="已审查 source commit 差异，批准更新身份基线。",
        decided_at=NOW + timedelta(minutes=3),
    )

    assert approved == repeated
    assert approved.revision == 2
    assert approved.previous_entry_id == baseline.entry_id
    assert load_source_identity(manifest_path) == assessment.proposal.candidate
    assert verify_source_identity(approved.manifest, source, project).status == "valid"
    history = store.list_history(current.source_name)
    assert [item.revision for item in history] == [2, 1]
    assert store.get_proposal(assessment.proposal.proposal_id).status == "approved"


def test_live_candidate_change_blocks_approval_without_mutating_manifest(tmp_path: Path) -> None:
    project, source, _mapping, manifest_path = _fixture(tmp_path)
    current = load_source_identity(manifest_path)
    store = SourceRefreshStore(tmp_path / "source.db")
    _bootstrap(store, current, source, project)
    _commit_source_change(source)
    assessment = assess_source_refresh(
        store,
        current,
        source_root=source,
        project_root=project,
        observed_at=NOW + timedelta(minutes=1),
    )
    assert assessment.proposal is not None
    (source / "src" / "second.tsx").write_text("unreviewed dirty change\n", encoding="utf-8")

    with pytest.raises(ValueError, match="candidate 已变化"):
        approve_source_refresh(
            store,
            assessment.proposal.proposal_id,
            manifest_path=manifest_path,
            source_root=source,
            project_root=project,
            reviewed_by="maintainer@example.invalid",
            review_reason="不应成功。",
        )

    assert load_source_identity(manifest_path) == current
    assert store.latest(current.source_name).revision == 1
    assert store.get_proposal(assessment.proposal.proposal_id).status == "pending"


def test_license_change_requires_explicit_acknowledgement(tmp_path: Path) -> None:
    project, source, _mapping, manifest_path = _fixture(tmp_path)
    current = load_source_identity(manifest_path)
    store = SourceRefreshStore(tmp_path / "source.db")
    _bootstrap(store, current, source, project)
    (source / "README.md").write_text(
        f"# source\n\nUpdated evidence.\n\n{CLAIM}\n",
        encoding="utf-8",
    )
    _git(source, "add", ".")
    _git(source, "commit", "-m", "license evidence changed")
    assessment = assess_source_refresh(
        store,
        current,
        source_root=source,
        project_root=project,
        observed_at=NOW + timedelta(minutes=1),
    )
    assert assessment.proposal is not None
    assert assessment.proposal.requires_license_review is True
    candidate = assessment.proposal.candidate
    with pytest.raises(ValueError, match="真实 identity 差异"):
        store.propose(
            current,
            candidate,
            source_root=source,
            project_root=project,
            changes=("git.commit",),
        )

    with pytest.raises(ValueError, match="显式确认 license review"):
        approve_source_refresh(
            store,
            assessment.proposal.proposal_id,
            manifest_path=manifest_path,
            source_root=source,
            project_root=project,
            reviewed_by="legal-reviewer",
            review_reason="已读取新证据，但尚未确认适用范围。",
        )
    approved = approve_source_refresh(
        store,
        assessment.proposal.proposal_id,
        manifest_path=manifest_path,
        source_root=source,
        project_root=project,
        reviewed_by="legal-reviewer",
        review_reason="已人工确认许可证声明与适用范围没有收窄。",
        allow_license_change=True,
    )
    assert approved.license_change_acknowledged is True


def test_mapping_change_requires_explicit_acknowledgement(tmp_path: Path) -> None:
    project, source, mapping, manifest_path = _fixture(tmp_path)
    current = load_source_identity(manifest_path)
    store = SourceRefreshStore(tmp_path / "source.db")
    _bootstrap(store, current, source, project)
    mapping.write_text('{"mapping": [{"area": "prompt"}]}\n', encoding="utf-8")
    assessment = assess_source_refresh(
        store,
        current,
        source_root=source,
        project_root=project,
        observed_at=NOW + timedelta(minutes=1),
    )
    assert assessment.proposal is not None
    assert assessment.proposal.changes == ("mapping.sha256",)
    assert assessment.proposal.requires_mapping_review is True

    with pytest.raises(ValueError, match="显式确认 mapping review"):
        approve_source_refresh(
            store,
            assessment.proposal.proposal_id,
            manifest_path=manifest_path,
            source_root=source,
            project_root=project,
            reviewed_by="mapping-reviewer",
            review_reason="已发现映射证据变化，但尚未完成逐项复核。",
        )
    approved = approve_source_refresh(
        store,
        assessment.proposal.proposal_id,
        manifest_path=manifest_path,
        source_root=source,
        project_root=project,
        reviewed_by="mapping-reviewer",
        review_reason="已人工复核新映射摘要并确认基线可以更新。",
        allow_mapping_change=True,
    )
    assert approved.mapping_change_acknowledged is True


def test_approved_proposal_remains_idempotent_after_source_moves_again(
    tmp_path: Path,
) -> None:
    project, source, _mapping, manifest_path = _fixture(tmp_path)
    current = load_source_identity(manifest_path)
    store = SourceRefreshStore(tmp_path / "source.db")
    _bootstrap(store, current, source, project)
    _commit_source_change(source)
    assessment = assess_source_refresh(
        store,
        current,
        source_root=source,
        project_root=project,
        observed_at=NOW + timedelta(minutes=1),
    )
    assert assessment.proposal is not None
    first = approve_source_refresh(
        store,
        assessment.proposal.proposal_id,
        manifest_path=manifest_path,
        source_root=source,
        project_root=project,
        reviewed_by="maintainer",
        review_reason="批准已经复核的 source commit。",
    )
    (source / "src" / "later.tsx").write_text(
        "export const later = true;\n",
        encoding="utf-8",
    )

    repeated = approve_source_refresh(
        store,
        assessment.proposal.proposal_id,
        manifest_path=manifest_path,
        source_root=source,
        project_root=project,
        reviewed_by="different-repeat-actor",
        review_reason="重复请求必须返回原审批事实。",
    )

    assert repeated == first
    assert store.list_history(current.source_name)[0] == first


def test_concurrent_propose_and_approve_converge_without_duplicate_history(
    tmp_path: Path,
) -> None:
    project, source, mapping, manifest_path = _fixture(tmp_path)
    current = load_source_identity(manifest_path)
    store = SourceRefreshStore(tmp_path / "source.db")
    _bootstrap(store, current, source, project)
    _commit_source_change(source)
    candidate = _capture(source, mapping, observed_at=NOW + timedelta(minutes=1))

    with ThreadPoolExecutor(max_workers=8) as pool:
        proposals = list(pool.map(
            lambda _index: store.propose(
                current,
                candidate,
                source_root=source,
                project_root=project,
                changes=("git.commit",),
                created_at=NOW + timedelta(minutes=1),
            ),
            range(16),
        ))
    assert len({item.proposal_id for item in proposals}) == 1
    proposal = proposals[0]
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(
            lambda _index: store.approve(
                proposal.proposal_id,
                source_root=source,
                project_root=project,
                reviewed_by="maintainer",
                review_reason="并发审批必须收敛到同一不可变记录。",
                decided_at=NOW + timedelta(minutes=2),
            )[1],
            range(16),
        ))
    assert len({item.entry_id for item in outcomes}) == 1
    assert [item.revision for item in store.list_history(current.source_name)] == [2, 1]


def test_secret_like_review_reason_and_tampered_proposal_are_rejected(tmp_path: Path) -> None:
    project, source, _mapping, manifest_path = _fixture(tmp_path)
    current = load_source_identity(manifest_path)
    store = SourceRefreshStore(tmp_path / "source.db")
    with pytest.raises(ValueError, match="secret"):
        store.bootstrap(
            current,
            source_root=source,
            project_root=project,
            reviewed_by="maintainer",
            review_reason="api_key=should-not-be-stored",
        )

    _bootstrap(store, current, source, project)
    _commit_source_change(source)
    assessment = assess_source_refresh(
        store,
        current,
        source_root=source,
        project_root=project,
        observed_at=NOW + timedelta(minutes=1),
    )
    assert assessment.proposal is not None
    payload = assessment.proposal.model_dump(mode="json")
    payload["requires_license_review"] = True
    with pytest.raises(ValueError, match="license review"):
        SourceRefreshProposal.model_validate(payload)

    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE source_refresh_proposals SET status = 'approved' WHERE proposal_id = ?",
            (assessment.proposal.proposal_id,),
        )
    with pytest.raises(ValueError, match="proposal 投影列"):
        store.get_proposal(assessment.proposal.proposal_id)


def test_cli_bootstrap_and_history_use_explicit_local_paths(tmp_path: Path) -> None:
    project, source, _mapping, manifest_path = _fixture(tmp_path)
    store_path = tmp_path / "state" / "source.db"
    common = [
        "--manifest",
        str(manifest_path),
        "--source",
        str(source),
        "--project-root",
        str(project),
        "--store",
        str(store_path),
    ]
    bootstrapped = subprocess.run(
        [
            "python3",
            "-m",
            "naumi_agent.claude_source.refresh",
            "bootstrap",
            *common,
            "--reviewed-by",
            "maintainer",
            "--reason",
            "导入现有人工审核基线。",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    history = subprocess.run(
        [
            "python3",
            "-m",
            "naumi_agent.claude_source.refresh",
            "history",
            *common,
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(bootstrapped.stdout)["revision"] == 1
    assert len(json.loads(history.stdout)) == 1
    assert store_path.is_file()
