from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path

import pytest

from naumi_agent.harness.checks import HarnessCheckResult, HarnessCheckStatus
from naumi_agent.harness.completion import (
    HarnessCompletionReceipt,
    HarnessEvidenceRef,
    HarnessReceiptCheck,
    HarnessReceiptCriterion,
)
from naumi_agent.harness.models import (
    HarnessAcceptanceCriterion,
    HarnessCompletionContract,
    HarnessTaskKind,
)
from naumi_agent.harness.store import (
    HARNESS_STORE_SCHEMA_VERSION,
    HarnessStore,
    HarnessStoreConflictError,
    HarnessStoreError,
    resolve_harness_db_path,
)

_PROFILE_DIGEST = "a" * 64
_TREE_BEFORE = "b" * 64
_TREE_AFTER = "c" * 64
_NOW = "2026-07-14T10:00:00+08:00"
_LATER = "2026-07-14T10:00:02+08:00"


def _contract(
    *,
    run_id: str = "run-store",
    session_id: str = "session-1",
) -> HarnessCompletionContract:
    return HarnessCompletionContract(
        run_id=run_id,
        session_id=session_id,
        task_id="task-1",
        issue_id="issue-1",
        profile_digest=_PROFILE_DIGEST,
        task_kind=HarnessTaskKind.CHANGE,
        objective="修改 Harness Store 并验证持久化",
        acceptance_criteria=(
            HarnessAcceptanceCriterion(
                id="store_persists",
                description="Harness 运行可以跨进程恢复",
            ),
        ),
        required_checks=("unit",),
        required_evidence=("check_output",),
        source_refs=("user_request",),
    )


def _check_result(*, run_id: str = "run-store") -> HarnessCheckResult:
    return HarnessCheckResult(
        check_id="unit",
        run_id=run_id,
        status=HarnessCheckStatus.PASSED,
        tree_fingerprint=_TREE_AFTER,
        profile_digest=_PROFILE_DIGEST,
        message="检查通过",
        output="THIS_RAW_OUTPUT_MUST_NOT_BE_STORED",
        exit_code=0,
        duration_ms=42,
    )


def _receipt(*, run_id: str = "run-store") -> HarnessCompletionReceipt:
    return HarnessCompletionReceipt(
        run_id=run_id,
        status="completed_verified",
        task_kind=HarnessTaskKind.CHANGE,
        changed_files=("src/naumi_agent/harness/store.py",),
        checks=(
            HarnessReceiptCheck(
                id="unit",
                status="passed",
                tree_fingerprint=_TREE_AFTER,
            ),
        ),
        criteria=(
            HarnessReceiptCriterion(
                id="store_persists",
                status="satisfied",
                evidence_ids=("evidence-1",),
            ),
        ),
        warnings=(),
        tree_fingerprint=_TREE_AFTER,
    )


def test_default_database_uses_user_state_not_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_home = tmp_path / "user-state"
    monkeypatch.setenv("NAUMI_STATE_HOME", str(state_home))

    path = resolve_harness_db_path()

    assert path == (state_home / "harness.db").resolve()


@pytest.mark.asyncio
async def test_schema_migration_is_idempotent_and_adds_current_tables(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state" / "harness.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = HarnessStore(db_path)

    await first.record_profile(
        workspace_root=workspace,
        profile_digest=_PROFILE_DIGEST,
        schema_version=1,
        loaded_at=_NOW,
        trusted_at=_NOW,
        trust_source="user_slash",
        status="trusted",
    )
    await HarnessStore(db_path).record_profile(
        workspace_root=workspace,
        profile_digest=_PROFILE_DIGEST,
        schema_version=1,
        loaded_at=_LATER,
        trusted_at=_NOW,
        trust_source="user_slash",
        status="trusted",
    )

    with sqlite3.connect(db_path) as db:
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        version = db.execute("PRAGMA user_version").fetchone()[0]
        rows = db.execute("SELECT COUNT(*) FROM harness_profiles").fetchone()[0]

    assert tables == {
        "harness_profiles",
        "harness_runs",
        "harness_contract_criteria",
        "harness_checks",
        "harness_evidence",
        "harness_replay_baselines",
        "harness_session_reconciliations",
        "harness_session_artifact_gc",
        "harness_session_reconciliation_tombstones",
        "harness_session_reconciliation_failure_events",
        "harness_session_reconciliation_terminals",
        "harness_retention_worker_leases",
        "harness_eval_results",
        "harness_eval_baselines",
        "harness_eval_baseline_selectors",
        "harness_eval_baseline_events",
        "harness_eval_comparison_receipts",
        "harness_run_leases",
        "harness_run_fence_events",
        "harness_heartbeats",
        "harness_interactions",
        "harness_interaction_events",
        "harness_conversation_queue",
        "harness_conversation_queue_resolutions",
    }
    assert version == HARNESS_STORE_SCHEMA_VERSION == 15
    assert rows == 1


@pytest.mark.asyncio
async def test_full_run_lifecycle_survives_a_new_store_instance(tmp_path: Path) -> None:
    db_path = tmp_path / "harness.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(db_path)
    contract = _contract()

    await store.record_profile(
        workspace_root=workspace,
        profile_digest=_PROFILE_DIGEST,
        schema_version=1,
        loaded_at=_NOW,
        trusted_at=_NOW,
        trust_source="user_slash",
        status="trusted",
    )
    started = await store.start_run(
        workspace_root=workspace,
        contract=contract,
        tree_fingerprint_before=_TREE_BEFORE,
        started_at=_NOW,
    )
    await store.record_check(
        result=_check_result(),
        argv=("python3", "-m", "pytest", "tests/unit/test_harness_store.py"),
        cwd=workspace,
        started_at=_NOW,
        completed_at=_LATER,
        artifact_path="artifacts/run-store/unit.txt",
    )
    await store.record_evidence(
        run_id=contract.run_id,
        evidence=HarnessEvidenceRef(
            id="evidence-1",
            kind="check_output",
            summary="Harness Store 定向测试通过",
            criterion_ids=("store_persists",),
        ),
        uri="artifact://run-store/unit.txt",
        sha256="d" * 64,
        summary={"exit_code": 0, "passed": 7},
        producer="harness_check",
        created_at=_LATER,
    )
    completed = await store.finish_run(
        run_id=contract.run_id,
        receipt=_receipt(),
        completed_at=_LATER,
    )

    restored = await HarnessStore(db_path).get_run(contract.run_id)

    assert started.status == "running"
    assert completed.status == "completed_verified"
    assert restored == completed
    assert restored is not None
    assert restored.workspace_root == str(workspace.resolve())
    assert restored.receipt == _receipt()
    assert restored.criteria[0].status == "satisfied"
    assert restored.criteria[0].evidence_ids == ("evidence-1",)
    assert restored.checks[0].argv[0] == "python3"
    assert restored.checks[0].duration_ms == 42
    assert restored.evidence[0].summary == {"exit_code": 0, "passed": 7}
    assert b"THIS_RAW_OUTPUT_MUST_NOT_BE_STORED" not in db_path.read_bytes()


@pytest.mark.asyncio
async def test_start_and_finish_are_idempotent_but_reject_conflicts(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(tmp_path / "harness.db")
    contract = _contract()

    first = await store.start_run(
        workspace_root=workspace,
        contract=contract,
        tree_fingerprint_before=_TREE_BEFORE,
        started_at=_NOW,
    )
    repeated = await store.start_run(
        workspace_root=workspace,
        contract=contract,
        tree_fingerprint_before=_TREE_BEFORE,
        started_at=_NOW,
    )

    assert repeated == first

    conflicting_contract = contract.model_copy(update={"objective": "另一个任务"})
    with pytest.raises(HarnessStoreConflictError, match="run-store"):
        await store.start_run(
            workspace_root=workspace,
            contract=conflicting_contract,
            tree_fingerprint_before=_TREE_BEFORE,
            started_at=_NOW,
        )

    completed = await store.finish_run(
        run_id=contract.run_id,
        receipt=_receipt(),
        completed_at=_LATER,
    )
    assert await store.finish_run(
        run_id=contract.run_id,
        receipt=_receipt(),
        completed_at=_LATER,
    ) == completed

    conflicting_receipt = _receipt().model_copy(update={"status": "blocked"})
    with pytest.raises(HarnessStoreConflictError, match="已完成"):
        await store.finish_run(
            run_id=contract.run_id,
            receipt=conflicting_receipt,
            completed_at=_LATER,
        )


@pytest.mark.asyncio
async def test_workspace_queries_are_isolated_and_bounded(tmp_path: Path) -> None:
    store = HarnessStore(tmp_path / "harness.db")
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    first_workspace.mkdir()
    second_workspace.mkdir()

    await store.start_run(
        workspace_root=first_workspace,
        contract=_contract(run_id="run-first"),
        tree_fingerprint_before=_TREE_BEFORE,
        started_at=_NOW,
    )
    await store.start_run(
        workspace_root=second_workspace,
        contract=_contract(run_id="run-second", session_id="session-2"),
        tree_fingerprint_before=_TREE_BEFORE,
        started_at=_LATER,
    )

    first_runs = await store.list_runs(first_workspace, limit=1)

    assert [run.id for run in first_runs] == ["run-first"]
    with pytest.raises(ValueError, match="limit"):
        await store.list_runs(first_workspace, limit=0)


@pytest.mark.asyncio
async def test_session_run_queries_require_workspace_and_session_and_keep_order(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "harness.db"
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    first_workspace.mkdir()
    second_workspace.mkdir()
    store = HarnessStore(db_path)

    for run_id, workspace, session_id, started_at in (
        ("run-old", first_workspace, "shared-session", _NOW),
        ("run-new", first_workspace, "shared-session", _LATER),
        ("run-other-session", first_workspace, "other-session", _LATER),
        ("run-other-workspace", second_workspace, "shared-session", _LATER),
    ):
        await store.start_run(
            workspace_root=workspace,
            contract=_contract(run_id=run_id, session_id=session_id),
            tree_fingerprint_before=_TREE_BEFORE,
            started_at=started_at,
        )

    matching = await HarnessStore(db_path).list_session_runs(
        first_workspace,
        "shared-session",
        limit=10,
    )
    bounded = await store.list_session_runs(
        first_workspace,
        "shared-session",
        limit=1,
    )

    assert [run.id for run in matching] == ["run-new", "run-old"]
    assert [run.id for run in bounded] == ["run-new"]
    assert await HarnessStore(tmp_path / "missing.db").list_session_runs(
        first_workspace,
        "shared-session",
    ) == ()
    with pytest.raises(ValueError, match="limit"):
        await store.list_session_runs(first_workspace, "shared-session", limit=0)


@pytest.mark.asyncio
async def test_session_run_query_rejects_corrupt_persisted_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "harness.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(db_path)
    await store.start_run(
        workspace_root=workspace,
        contract=_contract(),
        tree_fingerprint_before=_TREE_BEFORE,
        started_at=_NOW,
    )
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE harness_runs SET contract_json = ? WHERE id = ?",
            ("{", "run-store"),
        )
        db.commit()

    with pytest.raises(HarnessStoreError, match="损坏"):
        await store.list_session_runs(workspace, "session-1")


@pytest.mark.asyncio
async def test_session_reconciliation_cascades_children(tmp_path: Path) -> None:
    db_path = tmp_path / "harness.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(db_path)
    contract = _contract()
    await store.start_run(
        workspace_root=workspace,
        contract=contract,
        tree_fingerprint_before=_TREE_BEFORE,
        started_at=_NOW,
    )
    await store.record_check(
        result=_check_result(),
        argv=("python3", "-V"),
        cwd=workspace,
        started_at=_NOW,
        completed_at=_LATER,
    )
    await store.record_evidence(
        run_id=contract.run_id,
        evidence=HarnessEvidenceRef(
            id="evidence-1",
            kind="check_output",
            summary="检查输出摘要",
            criterion_ids=("store_persists",),
        ),
        uri="artifact://run-store/unit.txt",
        sha256="d" * 64,
        summary={"exit_code": 0},
        producer="harness_check",
        created_at=_LATER,
    )

    assert await store.delete_session_records(workspace, contract.session_id) == 1
    assert await store.delete_session_records(workspace, contract.session_id) == 0
    assert await store.get_run(contract.run_id) is None

    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM harness_contract_criteria").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM harness_checks").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM harness_evidence").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_session_delete_preview_is_workspace_scoped_and_counts_derived_rows(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "harness.db"
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    store = HarnessStore(db_path)
    target = _contract(run_id="run-a", session_id="shared-session")
    other = _contract(run_id="run-b", session_id="shared-session")
    await store.start_run(
        workspace_root=workspace_a,
        contract=target,
        tree_fingerprint_before=_TREE_BEFORE,
        started_at=_NOW,
    )
    await store.start_run(
        workspace_root=workspace_b,
        contract=other,
        tree_fingerprint_before=_TREE_BEFORE,
        started_at=_NOW,
    )
    await store.record_check(
        result=_check_result(run_id="run-a"),
        argv=("python3", "-V"),
        cwd=workspace_a,
        started_at=_NOW,
        completed_at=_LATER,
        artifact_path="artifacts/run-a/unit.txt",
    )
    await store.record_evidence(
        run_id="run-a",
        evidence=HarnessEvidenceRef(
            id="evidence-a",
            kind="check_output",
            summary="目标工作区证据",
            criterion_ids=("store_persists",),
        ),
        uri="artifact://run-a/unit.txt",
        sha256="d" * 64,
        summary={"exit_code": 0},
        producer="harness_check",
        created_at=_LATER,
    )

    preview = await store.preview_session_delete(workspace_a, "shared-session")

    assert preview.workspace_root == str(workspace_a.resolve())
    assert preview.run_count == 1
    assert preview.criterion_count == 1
    assert preview.check_count == 1
    assert preview.evidence_count == 1
    assert preview.replay_baseline_count == 0
    assert preview.check_artifact_reference_count == 1
    assert preview.evidence_artifact_reference_count == 1
    assert preview.artifact_reference_count == 2

    assert await store.delete_session_records(workspace_a, "shared-session") == 1
    remaining = await store.preview_session_delete(workspace_b, "shared-session")
    assert remaining.run_count == 1


@pytest.mark.asyncio
async def test_session_delete_preview_does_not_create_missing_database(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "missing" / "harness.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(db_path)

    preview = await store.preview_session_delete(workspace, "missing-session")

    assert preview.run_count == 0
    assert not db_path.exists()


@pytest.mark.asyncio
async def test_session_delete_preview_reports_corrupt_database(tmp_path: Path) -> None:
    db_path = tmp_path / "harness.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path.write_bytes(b"not a sqlite database")
    store = HarnessStore(db_path)

    with pytest.raises(HarnessStoreError, match="可能损坏或正忙"):
        await store.preview_session_delete(workspace, "broken-session")


@pytest.mark.asyncio
async def test_concurrent_evidence_writes_do_not_lose_records(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(tmp_path / "harness.db")
    contract = _contract()
    await store.start_run(
        workspace_root=workspace,
        contract=contract,
        tree_fingerprint_before=_TREE_BEFORE,
        started_at=_NOW,
    )

    async def write(index: int) -> None:
        await store.record_evidence(
            run_id=contract.run_id,
            evidence=HarnessEvidenceRef(
                id=f"evidence-{index}",
                kind="check_output",
                summary=f"证据 {index}",
            ),
            uri=f"artifact://run-store/{index}.json",
            sha256=f"{index:064x}",
            summary={"index": index},
            producer="concurrency_test",
            created_at=_LATER,
        )

    await asyncio.gather(*(write(index) for index in range(20)))

    restored = await store.get_run(contract.run_id)
    assert restored is not None
    assert len(restored.evidence) == 20


@pytest.mark.asyncio
async def test_evidence_rejects_sensitive_summary_keys_and_unknown_criteria(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(tmp_path / "harness.db")
    contract = _contract()
    await store.start_run(
        workspace_root=workspace,
        contract=contract,
        tree_fingerprint_before=_TREE_BEFORE,
        started_at=_NOW,
    )

    base = HarnessEvidenceRef(
        id="evidence-secret",
        kind="check_output",
        summary="安全摘要",
    )
    with pytest.raises(ValueError, match="敏感字段"):
        await store.record_evidence(
            run_id=contract.run_id,
            evidence=base,
            uri="artifact://run-store/secret.json",
            sha256="d" * 64,
            summary={"api_key": "must-not-persist"},
            producer="test",
            created_at=_LATER,
        )

    with pytest.raises(HarnessStoreConflictError, match="验收条件"):
        await store.record_evidence(
            run_id=contract.run_id,
            evidence=base.model_copy(update={"criterion_ids": ("unknown",)}),
            uri="artifact://run-store/unknown.json",
            sha256="d" * 64,
            summary={"safe": True},
            producer="test",
            created_at=_LATER,
        )


@pytest.mark.asyncio
async def test_check_argv_is_redacted_before_persistence(tmp_path: Path) -> None:
    db_path = tmp_path / "harness.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(db_path)
    await store.start_run(
        workspace_root=workspace,
        contract=_contract(),
        tree_fingerprint_before=_TREE_BEFORE,
        started_at=_NOW,
    )

    await store.record_check(
        result=_check_result(),
        argv=(
            "curl",
            "--authorization",
            "Bearer private-credential",
            "--api-key=brave-private-key",
            "BRAVE_SEARCH_API_KEY=brave-env-key",
        ),
        cwd=workspace,
        started_at=_NOW,
        completed_at=_LATER,
    )

    restored = await store.get_run("run-store")
    assert restored is not None
    assert restored.checks[0].argv == (
        "curl",
        "--authorization",
        "<redacted>",
        "--api-key=<redacted>",
        "BRAVE_SEARCH_API_KEY=<redacted>",
    )
    raw_database = db_path.read_bytes()
    assert b"private-credential" not in raw_database
    assert b"brave-private-key" not in raw_database
    assert b"brave-env-key" not in raw_database


@pytest.mark.asyncio
async def test_contract_receipt_and_evidence_text_are_redacted_before_persistence(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "harness.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(db_path)
    contract = _contract().model_copy(
        update={"objective": "验证 api_key=objective-private-credential"}
    )
    await store.start_run(
        workspace_root=workspace,
        contract=contract,
        tree_fingerprint_before=_TREE_BEFORE,
        started_at=_NOW,
    )
    await store.record_evidence(
        run_id=contract.run_id,
        evidence=HarnessEvidenceRef(
            id="safe-evidence",
            kind="note",
            summary="password=evidence-private-credential",
        ),
        uri="artifact://run-store/safe.json",
        sha256="d" * 64,
        summary={"note": "sk-123456789012345678901234567890"},
        producer="test",
        created_at=_LATER,
    )
    receipt = _receipt().model_copy(
        update={"warnings": ("token=warning-private-credential",)}
    )
    await store.finish_run(
        run_id=contract.run_id,
        receipt=receipt,
        completed_at=_LATER,
    )

    restored = await store.get_run(contract.run_id)
    assert restored is not None
    assert "objective-private-credential" not in restored.objective
    assert "evidence-private-credential" not in restored.evidence[0].description
    assert "123456789012345678901234567890" not in restored.evidence[0].summary["note"]
    assert restored.receipt is not None
    assert "warning-private-credential" not in restored.receipt.warnings[0]
    raw_database = db_path.read_bytes()
    for secret in (
        b"objective-private-credential",
        b"evidence-private-credential",
        b"123456789012345678901234567890",
        b"warning-private-credential",
    ):
        assert secret not in raw_database


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="Windows ACL 不使用 POSIX mode")
async def test_store_restricts_unix_permissions(tmp_path: Path) -> None:
    db_path = tmp_path / "private" / "harness.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(db_path)

    await store.start_run(
        workspace_root=workspace,
        contract=_contract(),
        tree_fingerprint_before=_TREE_BEFORE,
        started_at=_NOW,
    )

    assert db_path.parent.stat().st_mode & 0o777 == 0o700
    assert db_path.stat().st_mode & 0o777 == 0o600
