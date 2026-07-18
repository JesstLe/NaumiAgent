from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from naumi_agent.harness.eval_models import (
    EvalCaseStatus,
    EvalRunStatus,
    HarnessEvalCaseResult,
    HarnessEvalSuiteResult,
)
from naumi_agent.harness.store import (
    HARNESS_STORE_SCHEMA_VERSION,
    HarnessStore,
    HarnessStoreConflictError,
    HarnessStoreError,
)

_NOW = "2026-07-18T10:00:00+08:00"
_LATER = "2026-07-18T10:01:00+08:00"


def _result(
    *,
    duration_ms: float = 10.0,
    message: str = "",
) -> HarnessEvalSuiteResult:
    return HarnessEvalSuiteResult(
        suite_id="protocol-store",
        title="持久化协议评测",
        suite_path="evals/protocol-store.yaml",
        suite_sha256="a" * 64,
        status=EvalRunStatus.PASSED,
        cases=(
            HarnessEvalCaseResult(
                case_id="hello",
                runner="protocol_hello",
                status=EvalCaseStatus.PASSED,
                message=message,
            ),
        ),
        duration_ms=duration_ms,
    )


@pytest.mark.asyncio
async def test_eval_result_is_idempotent_immutable_and_survives_restart(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "harness.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(db_path)
    result = _result()

    first = await store.record_eval_result(
        workspace_root=workspace,
        batch_id="baseline:protocol-store:001",
        sample_index=0,
        result=result,
        created_at=_NOW,
    )
    retry = await store.record_eval_result(
        workspace_root=workspace,
        batch_id="baseline:protocol-store:001",
        sample_index=0,
        result=result,
        created_at=_LATER,
    )
    restored = await HarnessStore(db_path).get_eval_result(
        workspace,
        "baseline:protocol-store:001",
        "protocol-store",
        0,
    )

    assert retry == first
    assert retry.created_at == _NOW
    assert restored == first
    assert restored is not None
    assert restored.result == result
    assert restored.result_sha256
    with pytest.raises(HarnessStoreConflictError, match="不可覆盖"):
        await store.record_eval_result(
            workspace_root=workspace,
            batch_id="baseline:protocol-store:001",
            sample_index=0,
            result=_result(duration_ms=99),
            created_at=_LATER,
        )


@pytest.mark.asyncio
async def test_eval_result_cohort_is_ordered_bounded_and_workspace_isolated(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "harness.db"
    workspace = tmp_path / "workspace"
    other = tmp_path / "other"
    workspace.mkdir()
    other.mkdir()
    store = HarnessStore(db_path)
    for index in (2, 0, 1):
        await store.record_eval_result(
            workspace_root=workspace,
            batch_id="candidate-001",
            sample_index=index,
            result=_result(duration_ms=10 + index),
            created_at=_NOW,
        )

    records = await HarnessStore(db_path).list_eval_results(
        workspace,
        "candidate-001",
        "protocol-store",
        limit=2,
    )

    assert [item.sample_index for item in records] == [0, 1]
    assert await store.list_eval_results(
        other,
        "candidate-001",
        "protocol-store",
    ) == ()
    assert await store.get_eval_result(
        other,
        "candidate-001",
        "protocol-store",
        0,
    ) is None


@pytest.mark.asyncio
async def test_two_store_instances_concurrently_retry_one_sample(tmp_path: Path) -> None:
    db_path = tmp_path / "harness.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    first, second = await asyncio.gather(
        HarnessStore(db_path).record_eval_result(
            workspace_root=workspace,
            batch_id="concurrent-001",
            sample_index=0,
            result=_result(),
            created_at=_NOW,
        ),
        HarnessStore(db_path).record_eval_result(
            workspace_root=workspace,
            batch_id="concurrent-001",
            sample_index=0,
            result=_result(),
            created_at=_LATER,
        ),
    )

    assert first == second
    assert first.created_at in {_NOW, _LATER}


@pytest.mark.asyncio
async def test_schema_v8_migration_is_idempotent_and_tamper_evident(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "harness.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA user_version = 7")
        db.commit()
    store = HarnessStore(db_path)
    await store.record_eval_result(
        workspace_root=workspace,
        batch_id="tamper-001",
        sample_index=0,
        result=_result(),
        created_at=_NOW,
    )
    await HarnessStore(db_path).list_eval_results(
        workspace,
        "tamper-001",
        "protocol-store",
    )
    with sqlite3.connect(db_path) as db:
        version = int(db.execute("PRAGMA user_version").fetchone()[0])
        table = db.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name = 'harness_eval_results'"
        ).fetchone()[0]
        db.execute(
            "UPDATE harness_eval_results SET result_json = ?",
            ('{"suite_id":"forged"}',),
        )
        db.commit()

    assert version == HARNESS_STORE_SCHEMA_VERSION == 16
    assert table == 1
    with pytest.raises(HarnessStoreError, match="损坏"):
        await HarnessStore(db_path).get_eval_result(
            workspace,
            "tamper-001",
            "protocol-store",
            0,
        )


@pytest.mark.asyncio
async def test_v8_eval_results_survive_migration_to_latest_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "harness.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(db_path)
    await store.record_eval_result(
        workspace_root=workspace,
        batch_id="migration-v8",
        sample_index=0,
        result=_result(),
        created_at=_NOW,
    )
    with sqlite3.connect(db_path) as db:
        db.execute("DROP TABLE harness_eval_baseline_events")
        db.execute("DROP TABLE harness_eval_baseline_selectors")
        db.execute("DROP TABLE harness_eval_baselines")
        db.execute("PRAGMA user_version = 8")
        db.commit()

    migrated = HarnessStore(db_path)
    await migrated.record_eval_result(
        workspace_root=workspace,
        batch_id="migration-v8",
        sample_index=1,
        result=_result(duration_ms=11),
        created_at=_LATER,
    )
    with sqlite3.connect(db_path) as db:
        version = int(db.execute("PRAGMA user_version").fetchone()[0])
        rows = int(db.execute("SELECT COUNT(*) FROM harness_eval_results").fetchone()[0])
        baseline_tables = int(
            db.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
                "AND name LIKE 'harness_eval_baseline%'"
            ).fetchone()[0]
        )

    assert version == HARNESS_STORE_SCHEMA_VERSION == 16
    assert rows == 2
    assert baseline_tables == 3


@pytest.mark.asyncio
async def test_eval_result_rejects_unsafe_keys_and_limits(tmp_path: Path) -> None:
    store = HarnessStore(tmp_path / "harness.db")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(ValueError, match="batch_id"):
        await store.record_eval_result(
            workspace_root=workspace,
            batch_id="bad/id",
            sample_index=0,
            result=_result(),
            created_at=_NOW,
        )
    with pytest.raises(ValueError, match="sample_index"):
        await store.record_eval_result(
            workspace_root=workspace,
            batch_id="valid",
            sample_index=True,
            result=_result(),
            created_at=_NOW,
        )
    with pytest.raises(ValueError, match="limit"):
        await store.list_eval_results(
            workspace,
            "valid",
            "protocol-store",
            limit=0,
        )

    redacted = await store.record_eval_result(
        workspace_root=workspace,
        batch_id="redacted",
        sample_index=0,
        result=_result(message="api_key=super-secret-value"),
        created_at=_NOW,
    )
    assert "super-secret-value" not in redacted.result.cases[0].message
    assert "<redacted>" in redacted.result.cases[0].message


@pytest.mark.asyncio
async def test_baseline_promotion_rejects_missing_identity_and_sample_gaps(
    tmp_path: Path,
) -> None:
    store = HarnessStore(tmp_path / "harness.db")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for index in range(5):
        await store.record_eval_result(
            workspace_root=workspace,
            batch_id="no-identity",
            sample_index=index,
            result=_result(),
            created_at=_NOW,
        )
    with pytest.raises(ValueError, match="Identity"):
        await store.promote_eval_baseline(
            workspace_root=workspace,
            batch_id="no-identity",
            suite_id="protocol-store",
            promoted_by="Harness-Test",
            promotion_reason="必须拒绝无身份 cohort",
            created_at=_LATER,
        )

    for index in (0, 2):
        await store.record_eval_result(
            workspace_root=workspace,
            batch_id="sample-gap",
            sample_index=index,
            result=_result(),
            created_at=_NOW,
        )
    with pytest.raises(ValueError, match="连续递增"):
        await store.promote_eval_baseline(
            workspace_root=workspace,
            batch_id="sample-gap",
            suite_id="protocol-store",
            promoted_by="Harness-Test",
            promotion_reason="必须拒绝缺失 sample",
            created_at=_LATER,
        )
