"""Focused persistence contracts for EVO-01.3a Candidate Store Core."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from naumi_agent.evolution.candidate import build_candidate_draft
from naumi_agent.evolution.evidence import EvolutionEvidence, EvolutionEvidenceRef
from naumi_agent.evolution.store import (
    EVOLUTION_STORE_SCHEMA_VERSION,
    EvolutionCandidateStore,
    EvolutionStoreConflictError,
    EvolutionStoreCorruptionError,
    EvolutionStoreError,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _evidence(
    ordinal: int,
    *,
    root: str | None = None,
    scope: str = "src/naumi_agent/sample.py:run",
) -> EvolutionEvidence:
    observed_at = datetime(2026, 7, 18, tzinfo=UTC) + timedelta(seconds=ordinal)
    return EvolutionEvidence(
        evidence_id=f"eve_{_digest(f'observation:{ordinal}')[:24]}",
        source_kind="self_review_static",
        source_uri="artifact://workspace/src/naumi_agent/sample.py",
        observed_at=observed_at.isoformat(),
        finding_code="broad_except",
        scope=scope,
        root_fingerprint=root or _digest("same-mechanical-root"),
        refs=(
            EvolutionEvidenceRef(
                uri="artifact://workspace/src/naumi_agent/sample.py",
                sha256=_digest(f"file-content:{ordinal}"),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_absent_reads_are_lazy_and_first_write_versions_store(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = tmp_path / "state" / "evolution.db"
    store = EvolutionCandidateStore(db_path)
    draft = build_candidate_draft((_evidence(1),))

    assert await store.get_candidate(workspace, draft.candidate_id) is None
    assert await store.list_candidates(workspace) == ()
    assert await store.list_events(workspace, draft.candidate_id) == ()
    assert not db_path.exists()

    stored = await store.upsert_candidate(workspace, draft)

    assert stored.revision == 1
    with sqlite3.connect(db_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert version == EVOLUTION_STORE_SCHEMA_VERSION == 1
    assert {
        "evolution_candidates",
        "evolution_candidate_evidence",
        "evolution_candidate_events",
    } <= tables


@pytest.mark.asyncio
async def test_idempotent_retry_does_not_create_revision_or_event(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = EvolutionCandidateStore(tmp_path / "evolution.db")
    draft = build_candidate_draft((_evidence(1),))

    first = await store.upsert_candidate(workspace, draft)
    retried = await store.upsert_candidate(workspace, draft)

    assert retried == first
    assert len(await store.list_events(workspace, draft.candidate_id)) == 1


@pytest.mark.asyncio
async def test_merge_adds_only_new_evidence_and_chains_audit_digest(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = EvolutionCandidateStore(tmp_path / "evolution.db")
    first_draft = build_candidate_draft((_evidence(1),))
    second_draft = build_candidate_draft((_evidence(2),))

    first = await store.upsert_candidate(workspace, first_draft)
    merged = await store.upsert_candidate(workspace, second_draft)
    stale_retry = await store.upsert_candidate(workspace, first_draft)
    events = await store.list_events(workspace, first_draft.candidate_id)

    assert merged.revision == 2
    assert merged.draft.occurrence_count == 2
    assert stale_retry == merged
    assert [event.event_type for event in events] == ["created", "evidence_merged"]
    assert events[1].previous_sha256 == first.draft_sha256
    assert events[1].current_sha256 == merged.draft_sha256
    assert events[1].added_evidence_ids == (_evidence(2).evidence_id,)


@pytest.mark.asyncio
async def test_concurrent_store_instances_preserve_disjoint_observations(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = tmp_path / "evolution.db"
    drafts = tuple(build_candidate_draft((_evidence(index),)) for index in range(1, 11))
    stores = tuple(EvolutionCandidateStore(db_path) for _ in drafts)

    await asyncio.gather(
        *(store.upsert_candidate(workspace, draft) for store, draft in zip(stores, drafts))
    )

    restored = await stores[0].get_candidate(workspace, drafts[0].candidate_id)
    assert restored is not None
    assert restored.revision == 10
    assert restored.draft.occurrence_count == 10
    assert len(await stores[0].list_events(workspace, drafts[0].candidate_id)) == 10


@pytest.mark.asyncio
async def test_one_hundred_repeated_observations_keep_one_candidate(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = EvolutionCandidateStore(tmp_path / "evolution.db")
    drafts = tuple(
        build_candidate_draft((_evidence(index),)) for index in range(1, 101)
    )

    for draft in drafts:
        await store.upsert_candidate(workspace, draft)

    listed = await store.list_candidates(workspace)
    assert len(listed) == 1
    assert listed[0].draft.candidate_id == drafts[0].candidate_id
    assert listed[0].draft.occurrence_count == 100
    assert listed[0].revision == 100


@pytest.mark.asyncio
async def test_same_candidate_isolated_by_workspace_and_listed_by_recency(
    tmp_path: Path,
) -> None:
    workspace_a = tmp_path / "a"
    workspace_b = tmp_path / "b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    store = EvolutionCandidateStore(tmp_path / "evolution.db")
    older = build_candidate_draft((_evidence(1),))
    other = build_candidate_draft(
        (_evidence(2, root=_digest("other-root"), scope="src/other.py:run"),)
    )

    await store.upsert_candidate(workspace_a, older)
    await store.upsert_candidate(workspace_a, other)
    await store.upsert_candidate(workspace_b, older)

    listed = await store.list_candidates(workspace_a, limit=2)
    assert [item.draft.candidate_id for item in listed] == [
        other.candidate_id,
        older.candidate_id,
    ]
    assert (await store.list_candidates(workspace_b))[0].revision == 1
    with pytest.raises(ValueError, match="1..500"):
        await store.list_candidates(workspace_a, limit=0)


@pytest.mark.asyncio
async def test_rejects_noncanonical_draft_and_conflicting_immutable_evidence(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = EvolutionCandidateStore(tmp_path / "evolution.db")
    original_evidence = _evidence(1)
    draft = build_candidate_draft((original_evidence,))
    await store.upsert_candidate(workspace, draft)

    with pytest.raises(EvolutionStoreConflictError, match="materialization"):
        await store.upsert_candidate(
            workspace,
            draft.model_copy(update={"occurrence_count": 2}),
        )

    conflicting = original_evidence.model_copy(
        update={
            "refs": (
                EvolutionEvidenceRef(
                    uri=original_evidence.source_uri,
                    sha256=_digest("different-file-content"),
                ),
            )
        }
    )
    with pytest.raises(EvolutionStoreConflictError, match="不可变内容冲突"):
        await store.upsert_candidate(workspace, build_candidate_draft((conflicting,)))


@pytest.mark.asyncio
async def test_detects_candidate_and_evidence_tampering(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = tmp_path / "evolution.db"
    store = EvolutionCandidateStore(db_path)
    draft = build_candidate_draft((_evidence(1),))
    await store.upsert_candidate(workspace, draft)

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE evolution_candidates SET occurrence_count = 99"
        )
    with pytest.raises(EvolutionStoreCorruptionError, match="projection"):
        await store.get_candidate(workspace, draft.candidate_id)

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE evolution_candidates SET occurrence_count = 1"
        )
        connection.execute(
            "UPDATE evolution_candidate_evidence SET evidence_sha256 = ?",
            ("0" * 64,),
        )
    with pytest.raises(EvolutionStoreCorruptionError, match="Evidence"):
        await store.get_candidate(workspace, draft.candidate_id)


@pytest.mark.asyncio
async def test_detects_audit_chain_tampering(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = tmp_path / "evolution.db"
    store = EvolutionCandidateStore(db_path)
    draft = build_candidate_draft((_evidence(1),))
    await store.upsert_candidate(workspace, draft)

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE evolution_candidate_events SET current_sha256 = ?",
            ("0" * 64,),
        )

    with pytest.raises(EvolutionStoreCorruptionError, match="materialization"):
        await store.get_candidate(workspace, draft.candidate_id)


@pytest.mark.asyncio
async def test_rejects_future_schema_and_naive_clock(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = tmp_path / "future.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA user_version = 2")
    draft = build_candidate_draft((_evidence(1),))

    with pytest.raises(EvolutionStoreError, match="高于"):
        await EvolutionCandidateStore(db_path).upsert_candidate(workspace, draft)
    with pytest.raises(EvolutionStoreError, match="无法保存"):
        await EvolutionCandidateStore(
            tmp_path / "naive.db",
            clock=lambda: datetime(2026, 7, 18),
        ).upsert_candidate(workspace, draft)


@pytest.mark.skipif(__import__("os").name == "nt", reason="Windows uses ACLs")
@pytest.mark.asyncio
async def test_store_restricts_posix_state_permissions(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    state.mkdir(mode=0o755)
    db_path = state / "evolution.db"

    await EvolutionCandidateStore(db_path).upsert_candidate(
        workspace,
        build_candidate_draft((_evidence(1),)),
    )

    assert state.stat().st_mode & 0o777 == 0o700
    assert db_path.stat().st_mode & 0o777 == 0o600
