from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from naumi_agent.orchestrator.pursuit_store import (
    PursuitStore,
    PursuitStoreConflictError,
    PursuitStoreError,
)
from naumi_agent.orchestrator.pursuit_terminal_dead_letter_abandon import (
    PursuitTerminalDeadLetterAbandonReason,
)
from naumi_agent.orchestrator.pursuit_terminal_retention import (
    PursuitTerminalOutboxRetentionPreview,
    build_terminal_outbox_retention_preview,
    render_terminal_outbox_retention_preview,
)
from naumi_agent.orchestrator.pursuit_terminal_retention_admission import (
    PursuitTerminalOutboxRetentionAdmission,
    new_retention_candidate_plan,
    render_terminal_outbox_retention_admission,
)
from naumi_agent.safety.permissions import TOOL_PERMISSIONS, PermissionMode
from naumi_agent.tools.pursuit import (
    create_pursuit_tool,
    parse_terminal_outbox_retention_admission_args,
    parse_terminal_outbox_retention_preview_args,
)
from tests.unit.test_pursuit_terminal_outbox_dead_letter import _pending_store


def _abandoned_store(tmp_path):
    store, outbox_id, due_at = _pending_store(tmp_path)
    claim = store.claim_next_terminal_outbox(
        owner_id="retention-preview-worker",
        now=due_at,
        lease_seconds=30,
    )
    assert claim is not None
    failure, _ = store.record_terminal_outbox_failure(
        outbox_id,
        owner_id="retention-preview-worker",
        claim_epoch=claim.dispatch.claim_epoch,
        now=due_at + 1,
        retry_delay_seconds=5,
        failure_code="lease_missing",
        max_failures=8,
        permanent=True,
    )
    receipt, _ = store.abandon_terminal_outbox_dead_letter(
        failure.event_id,
        source_request_id="permission-call-retention-preview",
        reason=PursuitTerminalDeadLetterAbandonReason.NO_LONGER_REQUIRED,
        now=due_at + 2,
    )
    return store, outbox_id, failure, receipt


def _preview_for_admission(store, receipt, tmp_path):
    return build_terminal_outbox_retention_preview(
        store.preview_terminal_outbox_retention(
            assessed_at=receipt.abandoned_at + 31 * 86_400,
            retention_days=30,
            limit=1,
            scan_limit=1,
        ),
        workspace_root=str(tmp_path.resolve()),
    )


def _admit(store, preview, receipt, tmp_path, *, source="admission-call"):
    return store.admit_terminal_outbox_retention(
        preview_id=preview.preview_id,
        preview_sha256=preview.preview_sha256,
        workspace_root=str(tmp_path.resolve()),
        assessed_at=receipt.abandoned_at + 31 * 86_400,
        retention_days=30,
        limit=1,
        scan_limit=1,
        source_request_id=source,
        now=receipt.abandoned_at + 31 * 86_400 + 1,
    )


def test_terminal_outbox_retention_preview_is_read_only_and_tamper_evident(
    tmp_path,
) -> None:
    store, _outbox_id, failure, receipt = _abandoned_store(tmp_path)
    before = store.db_path.read_bytes()

    page = PursuitStore(store.base_dir).preview_terminal_outbox_retention(
        assessed_at=receipt.abandoned_at + 31 * 86_400,
        retention_days=30,
        limit=1,
        scan_limit=1,
    )
    preview = build_terminal_outbox_retention_preview(
        page,
        workspace_root=str(tmp_path.resolve()),
    )

    assert store.db_path.read_bytes() == before
    assert preview.total_outbox_count == 1
    assert preview.pending_count == preview.delivered_count == 0
    assert preview.abandoned_count == preview.eligible_count == 1
    assert preview.scanned_count == preview.selected_count == 1
    assert not preview.scan_truncated
    assert not preview.selection_truncated
    candidate = preview.candidates[0]
    assert candidate.dead_letter_id == failure.event_id
    assert candidate.abandon_receipt_id == receipt.receipt_id
    assert candidate.eligibility_reason == "abandoned_before_cutoff"
    assert candidate.protection_reason == "protected_pending_apply_authority"
    assert candidate.age_seconds == 31 * 86_400
    kinds = {item.kind for item in candidate.protection_refs}
    assert {
        "recovery_attempt",
        "recovery_attempt_event",
        "outbox_snapshot",
        "outbox_event",
        "dispatch_snapshot",
        "dispatch_event",
        "failure_head",
        "failure_event",
        "abandon_receipt",
        "boundary_decision",
        "checkpoint_pointer",
    } <= kinds
    payload = preview.model_dump_json()
    assert str(tmp_path.resolve()) not in payload
    assert "outbox_id" not in payload
    assert "attempt_id" not in payload
    assert "run_id" not in payload
    rendered = render_terminal_outbox_retention_preview(preview)
    assert "只读候选预演，不是删除授权" in rendered
    assert "保护引用" in rendered
    assert "永远不能直接执行物理删除" in rendered

    tampered = preview.model_dump(mode="json")
    tampered["candidates"][0]["protection_refs"][0]["fact_sha256"] = "f" * 64
    with pytest.raises(ValidationError, match="protection refs 摘要"):
        PursuitTerminalOutboxRetentionPreview.model_validate(tampered)


def test_terminal_outbox_retention_admission_persists_recoverable_plan(
    tmp_path,
) -> None:
    store, outbox_id, _failure, receipt = _abandoned_store(tmp_path)
    preview = _preview_for_admission(store, receipt, tmp_path)

    admission = _admit(store, preview, receipt, tmp_path)

    assert admission.status == "admitted"
    assert admission.durable is True
    assert admission.execution_authority is False
    assert admission.physical_prune_authority is False
    assert admission.candidate_count == 1
    candidate = admission.candidates[0]
    assert candidate.candidate_id == preview.candidates[0].candidate_id
    assert candidate.protection_refs_sha256 == (
        preview.candidates[0].protection_refs_sha256
    )
    assert [step.operation for step in candidate.steps] == [
        "delete_abandon_receipt",
        "delete_failure_head",
        "delete_failure_events",
        "delete_dispatch_events",
        "delete_dispatch_snapshot",
        "delete_outbox_events",
        "delete_outbox_snapshot",
    ]
    assert all(step.before_killpoint != step.after_killpoint for step in candidate.steps)
    assert store.get_terminal_outbox_effective_state(outbox_id).state.value == (
        "abandoned"
    )
    assert store.get_terminal_outbox_retention_admission(
        admission.admission_id
    ) == admission
    public = admission.model_dump_json()
    rendered = render_terminal_outbox_retention_admission(admission)
    assert outbox_id not in public
    assert outbox_id not in rendered
    assert "execution_authority=false" in rendered
    assert "本次没有删除" in rendered
    with sqlite3.connect(store.db_path) as conn:
        private = conn.execute(
            "SELECT recovery_json FROM "
            "pursuit_terminal_outbox_retention_admissions"
        ).fetchone()[0]
    assert outbox_id in private


def test_terminal_outbox_retention_admission_is_idempotent_and_strict(
    tmp_path,
) -> None:
    store, _outbox_id, _failure, receipt = _abandoned_store(tmp_path)
    preview = _preview_for_admission(store, receipt, tmp_path)

    first = _admit(store, preview, receipt, tmp_path, source="same-admission")
    second = _admit(store, preview, receipt, tmp_path, source="same-admission")

    assert second == first
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM pursuit_terminal_outbox_retention_admissions"
        ).fetchone()[0] == 1
    tampered = first.model_dump(mode="json")
    tampered["candidates"][0]["steps"][0]["record_count"] += 1
    with pytest.raises(ValidationError, match="plan step 摘要"):
        PursuitTerminalOutboxRetentionAdmission.model_validate(tampered)

    later = build_terminal_outbox_retention_preview(
        store.preview_terminal_outbox_retention(
            assessed_at=receipt.abandoned_at + 32 * 86_400,
            retention_days=30,
            limit=1,
            scan_limit=1,
        ),
        workspace_root=str(tmp_path.resolve()),
    )
    with pytest.raises(PursuitStoreConflictError, match="绑定其他 preview"):
        store.admit_terminal_outbox_retention(
            preview_id=later.preview_id,
            preview_sha256=later.preview_sha256,
            workspace_root=str(tmp_path.resolve()),
            assessed_at=receipt.abandoned_at + 32 * 86_400,
            retention_days=30,
            limit=1,
            scan_limit=1,
            source_request_id="same-admission",
            now=receipt.abandoned_at + 32 * 86_400 + 1,
        )


def test_terminal_outbox_retention_admission_serializes_concurrent_writers(
    tmp_path,
) -> None:
    store, _outbox_id, _failure, receipt = _abandoned_store(tmp_path)
    preview = _preview_for_admission(store, receipt, tmp_path)

    def admit(index: int):
        return _admit(
            PursuitStore(store.base_dir),
            preview,
            receipt,
            tmp_path,
            source=f"concurrent-admission-{index}",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = tuple(pool.map(admit, range(2)))

    assert first == second
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM pursuit_terminal_outbox_retention_admissions"
        ).fetchone()[0] == 1


def test_terminal_outbox_retention_killpoints_are_candidate_scoped() -> None:
    first = new_retention_candidate_plan(
        candidate_id="ptorp_" + "a" * 24,
        candidate_sha256="a" * 64,
        protection_refs_sha256="b" * 64,
        recovery_snapshot_sha256="c" * 64,
        operation_counts=(("delete_outbox_snapshot", 1),),
    )
    second = new_retention_candidate_plan(
        candidate_id="ptorp_" + "d" * 24,
        candidate_sha256="d" * 64,
        protection_refs_sha256="e" * 64,
        recovery_snapshot_sha256="f" * 64,
        operation_counts=(("delete_outbox_snapshot", 1),),
    )

    assert first.steps[0].before_killpoint != second.steps[0].before_killpoint
    assert first.steps[0].after_killpoint != second.steps[0].after_killpoint


def test_terminal_outbox_retention_admission_rejects_changed_or_empty_preview(
    tmp_path,
) -> None:
    store, _outbox_id, _failure, receipt = _abandoned_store(tmp_path)
    changed_sha = "f" * 64

    changed = store.admit_terminal_outbox_retention(
        preview_id=f"ptorpv_{changed_sha[:24]}",
        preview_sha256=changed_sha,
        workspace_root=str(tmp_path.resolve()),
        assessed_at=receipt.abandoned_at + 31 * 86_400,
        retention_days=30,
        limit=1,
        scan_limit=1,
        source_request_id="changed-preview",
        now=receipt.abandoned_at + 31 * 86_400 + 1,
    )
    assert changed.status == "rejected"
    assert changed.rejection_reasons == ("preview_authority_changed",)
    assert not changed.durable

    empty = build_terminal_outbox_retention_preview(
        store.preview_terminal_outbox_retention(
            assessed_at=receipt.abandoned_at + 10 * 86_400,
            retention_days=30,
            limit=1,
            scan_limit=1,
        ),
        workspace_root=str(tmp_path.resolve()),
    )
    rejected = store.admit_terminal_outbox_retention(
        preview_id=empty.preview_id,
        preview_sha256=empty.preview_sha256,
        workspace_root=str(tmp_path.resolve()),
        assessed_at=receipt.abandoned_at + 10 * 86_400,
        retention_days=30,
        limit=1,
        scan_limit=1,
        source_request_id="empty-preview",
        now=receipt.abandoned_at + 10 * 86_400 + 1,
    )
    assert rejected.status == "rejected"
    assert rejected.rejection_reasons == ("no_candidates",)
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM pursuit_terminal_outbox_retention_admissions"
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "killpoint",
    ["before_admission_insert", "after_admission_insert"],
)
def test_terminal_outbox_retention_admission_rolls_back_each_write_boundary(
    tmp_path,
    monkeypatch,
    killpoint,
) -> None:
    store, outbox_id, _failure, receipt = _abandoned_store(tmp_path)
    preview = _preview_for_admission(store, receipt, tmp_path)

    def fail_at(name: str) -> None:
        if name == killpoint:
            raise RuntimeError("simulated-process-kill")

    monkeypatch.setattr(store, "_retention_admission_fault_point", fail_at)
    with pytest.raises(RuntimeError, match="simulated-process-kill"):
        _admit(store, preview, receipt, tmp_path)

    reopened = PursuitStore(store.base_dir)
    assert reopened.get_terminal_outbox_effective_state(outbox_id).state.value == (
        "abandoned"
    )
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM pursuit_terminal_outbox_retention_admissions"
        ).fetchone()[0] == 0


def test_terminal_outbox_retention_admission_detects_persisted_tampering(
    tmp_path,
) -> None:
    store, _outbox_id, _failure, receipt = _abandoned_store(tmp_path)
    preview = _preview_for_admission(store, receipt, tmp_path)
    admission = _admit(store, preview, receipt, tmp_path)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE pursuit_terminal_outbox_retention_admissions "
            "SET recovery_json = replace(recovery_json, 'delete_outbox_snapshot', "
            "'delete_outbox_events')"
        )

    with pytest.raises(PursuitStoreError, match="持久化摘要不匹配"):
        PursuitStore(store.base_dir).get_terminal_outbox_retention_admission(
            admission.admission_id
        )


def test_terminal_outbox_retention_admission_authenticates_index_columns(
    tmp_path,
) -> None:
    store, _outbox_id, _failure, receipt = _abandoned_store(tmp_path)
    preview = _preview_for_admission(store, receipt, tmp_path)
    admission = _admit(store, preview, receipt, tmp_path)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE pursuit_terminal_outbox_retention_admissions "
            "SET source_request_sha256 = ?",
            ("f" * 64,),
        )

    with pytest.raises(PursuitStoreError, match="持久化摘要不匹配"):
        PursuitStore(store.base_dir).get_terminal_outbox_retention_admission(
            admission.admission_id
        )


def test_terminal_outbox_retention_admission_read_is_legacy_safe(tmp_path) -> None:
    base_dir = tmp_path / "legacy-pursuit"
    base_dir.mkdir()
    with sqlite3.connect(base_dir / "pursuit.db") as conn:
        conn.execute("CREATE TABLE legacy_marker (value TEXT NOT NULL)")

    assert PursuitStore(base_dir).get_terminal_outbox_retention_admission(
        "ptora_" + "a" * 24
    ) is None


def test_terminal_outbox_retention_preview_excludes_recent_disposition(
    tmp_path,
) -> None:
    store, _outbox_id, _failure, receipt = _abandoned_store(tmp_path)

    preview = build_terminal_outbox_retention_preview(
        store.preview_terminal_outbox_retention(
            assessed_at=receipt.abandoned_at + 10 * 86_400,
            retention_days=30,
        ),
        workspace_root=str(tmp_path.resolve()),
    )

    assert preview.abandoned_count == 1
    assert preview.eligible_count == preview.scanned_count == 0
    assert preview.candidates == ()


@pytest.mark.asyncio
async def test_terminal_outbox_retention_tool_is_read_only_and_shared(
    tmp_path,
) -> None:
    store, _outbox_id, _failure, receipt = _abandoned_store(tmp_path)
    page = store.preview_terminal_outbox_retention(
        assessed_at=receipt.abandoned_at + 31 * 86_400,
        retention_days=30,
        limit=1,
        scan_limit=1,
    )
    preview = build_terminal_outbox_retention_preview(
        page,
        workspace_root=str(tmp_path.resolve()),
    )
    calls: list[tuple[int, int, int, str | None]] = []

    async def runner(
        retention_days: int,
        limit: int,
        scan_limit: int,
        assessed_at: str | None,
    ) -> PursuitTerminalOutboxRetentionPreview:
        calls.append((retention_days, limit, scan_limit, assessed_at))
        return preview

    tool = next(
        item
        for item in create_pursuit_tool(
            terminal_outbox_retention_preview=runner,
        )
        if item.name == "pursuit_terminal_outbox_retention_preview"
    )
    assert tool.metadata.read_only
    assert tool.metadata.concurrency_safe
    assert not tool.metadata.requires_confirmation
    rendered = await tool.execute(
        retention_days=30,
        limit=1,
        scan_limit=1,
        assessed_at="2026-08-11T00:00:00+00:00",
    )
    assert calls == [(30, 1, 1, "2026-08-11T00:00:00+00:00")]
    assert "只读候选预演，不是删除授权" in rendered
    rule = TOOL_PERMISSIONS[tool.name]
    assert set(rule.allowed_modes) == set(PermissionMode)
    assert not rule.requires_confirmation

    with pytest.raises(ValueError, match="limit 必须是整数"):
        await tool.execute(limit=True)
    assert len(calls) == 1


def test_terminal_outbox_retention_shared_argument_parser_is_strict() -> None:
    assert parse_terminal_outbox_retention_preview_args([
        "--limit", "5",
        "--scan-limit", "10",
        "--retention-days", "45",
        "--assessed-at", "2026-08-11T08:00:00+08:00",
    ]) == {
        "retention_days": 45,
        "limit": 5,
        "scan_limit": 10,
        "assessed_at": "2026-08-11T08:00:00+08:00",
    }
    with pytest.raises(ValueError, match="参数重复"):
        parse_terminal_outbox_retention_preview_args([
            "--limit", "5", "--limit", "6",
        ])
    with pytest.raises(ValueError, match="scan-limit"):
        parse_terminal_outbox_retention_preview_args([
            "--limit", "10", "--scan-limit", "5",
        ])


def test_terminal_outbox_retention_admission_parser_binds_exact_preview() -> None:
    digest = "a" * 64
    parsed = parse_terminal_outbox_retention_admission_args([
        f"ptorpv_{digest[:24]}",
        digest,
        "--assessed-at",
        "2026-08-11T08:00:00+08:00",
        "--retention-days",
        "45",
        "--limit",
        "5",
        "--scan-limit",
        "10",
    ])
    assert parsed == {
        "preview_id": f"ptorpv_{digest[:24]}",
        "preview_sha256": digest,
        "retention_days": 45,
        "limit": 5,
        "scan_limit": 10,
        "assessed_at": "2026-08-11T08:00:00+08:00",
    }
    with pytest.raises(ValueError, match="必须提供原 preview"):
        parse_terminal_outbox_retention_admission_args([
            f"ptorpv_{digest[:24]}", digest,
        ])
    with pytest.raises(ValueError, match="ID 与摘要不一致"):
        parse_terminal_outbox_retention_admission_args([
            "ptorpv_" + "b" * 24,
            digest,
            "--assessed-at",
            "2026-08-11T00:00:00+00:00",
        ])


@pytest.mark.asyncio
async def test_terminal_outbox_retention_admission_tool_is_shared_and_bounded(
    tmp_path,
) -> None:
    store, _outbox_id, _failure, receipt = _abandoned_store(tmp_path)
    preview = _preview_for_admission(store, receipt, tmp_path)
    calls: list[tuple[object, ...]] = []

    async def runner(*args):
        calls.append(args)
        return _admit(store, preview, receipt, tmp_path, source=str(args[-1]))

    tool = next(
        item
        for item in create_pursuit_tool(
            terminal_outbox_retention_admission=runner,
        )
        if item.name == "pursuit_terminal_outbox_retention_admission"
    )
    assert not tool.metadata.read_only
    assert tool.metadata.concurrency_safe
    assert not tool.metadata.requires_confirmation
    rendered = await tool.execute(
        preview_id=preview.preview_id,
        preview_sha256=preview.preview_sha256,
        retention_days=30,
        limit=1,
        scan_limit=1,
        assessed_at=datetime.fromtimestamp(
            receipt.abandoned_at + 31 * 86_400,
            UTC,
        ).isoformat(),
    )
    assert len(calls) == 1
    assert calls[0][:6] == (
        preview.preview_id,
        preview.preview_sha256,
        30,
        1,
        1,
        datetime.fromtimestamp(
            receipt.abandoned_at + 31 * 86_400,
            UTC,
        ).isoformat(),
    )
    assert "execution_authority=false" in rendered
    rule = TOOL_PERMISSIONS[tool.name]
    assert PermissionMode.BYPASS in rule.allowed_modes
    assert not rule.requires_confirmation

    with pytest.raises(ValueError, match="十进制整数"):
        await tool.execute(
            preview_id=preview.preview_id,
            preview_sha256=preview.preview_sha256,
            retention_days=True,
            limit=1,
            scan_limit=1,
            assessed_at="2026-08-11T00:00:00+00:00",
        )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_engine_terminal_outbox_retention_preview_uses_reproducible_time(
    tmp_path,
) -> None:
    from naumi_agent.orchestrator.engine import AgentEngine

    store, _outbox_id, _failure, receipt = _abandoned_store(tmp_path)
    assessment = receipt.abandoned_at + 31 * 86_400
    engine = SimpleNamespace(
        pursuit_store=store,
        workspace_root=tmp_path.resolve(),
    )

    preview = await AgentEngine.preview_pursuit_terminal_outbox_retention(
        engine,
        30,
        1,
        1,
        datetime.fromtimestamp(assessment, UTC).isoformat(),
    )

    assert preview.assessed_at.endswith("+00:00")
    assert preview.selected_count == 1
    with pytest.raises(ValueError, match="必须包含时区"):
        await AgentEngine.preview_pursuit_terminal_outbox_retention(
            engine,
            assessed_at="2026-08-11T08:00:00",
        )


@pytest.mark.asyncio
async def test_engine_terminal_outbox_retention_admission_uses_store_authority(
    tmp_path,
) -> None:
    from naumi_agent.orchestrator.engine import AgentEngine

    store, _outbox_id, _failure, receipt = _abandoned_store(tmp_path)
    preview = _preview_for_admission(store, receipt, tmp_path)
    engine = SimpleNamespace(
        pursuit_store=store,
        workspace_root=tmp_path.resolve(),
    )

    admission = await AgentEngine.admit_pursuit_terminal_outbox_retention(
        engine,
        preview.preview_id,
        preview.preview_sha256,
        30,
        1,
        1,
        datetime.fromtimestamp(
            receipt.abandoned_at + 31 * 86_400,
            UTC,
        ).isoformat(),
        "engine-admission",
    )

    assert admission.status == "admitted"
    assert store.get_terminal_outbox_retention_admission(
        admission.admission_id
    ) == admission
    with pytest.raises(ValueError, match="必须包含时区"):
        await AgentEngine.admit_pursuit_terminal_outbox_retention(
            engine,
            preview.preview_id,
            preview.preview_sha256,
            30,
            1,
            1,
            "2026-08-11T08:00:00",
            "invalid-engine-admission",
        )


def test_terminal_outbox_retention_preview_fails_closed_on_tampered_receipt(
    tmp_path,
) -> None:
    store, _outbox_id, _failure, receipt = _abandoned_store(tmp_path)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE pursuit_terminal_outbox_dead_letter_abandons "
            "SET payload_json = replace(payload_json, 'no_longer_required', "
            "'superseded')"
        )

    with pytest.raises(PursuitStoreError, match="receipt digest 不匹配"):
        PursuitStore(store.base_dir).preview_terminal_outbox_retention(
            assessed_at=receipt.abandoned_at + 31 * 86_400,
            retention_days=30,
        )


def test_terminal_outbox_retention_preview_does_not_initialize_empty_store(
    tmp_path,
) -> None:
    base_dir = tmp_path / "empty-pursuit"
    base_dir.mkdir()
    database = base_dir / "pursuit.db"
    database.touch()

    page = PursuitStore(base_dir).preview_terminal_outbox_retention(
        assessed_at=1_800_000_000,
    )

    assert page.total_outbox_count == 0
    assert page.records == ()
    assert database.read_bytes() == b""


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"assessed_at": float("nan")}, "策略无效"),
        ({"assessed_at": 1_800_000_000, "retention_days": 0}, "策略无效"),
        (
            {"assessed_at": 1_800_000_000, "limit": 2, "scan_limit": 1},
            "策略无效",
        ),
    ],
)
def test_terminal_outbox_retention_preview_rejects_invalid_policy(
    tmp_path,
    kwargs,
    message,
) -> None:
    with pytest.raises(ValueError, match=message):
        PursuitStore(tmp_path / "pursuit").preview_terminal_outbox_retention(
            **kwargs
        )
