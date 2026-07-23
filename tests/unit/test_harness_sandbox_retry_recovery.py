from __future__ import annotations

from dataclasses import replace

import pytest
from pydantic import ValidationError

from naumi_agent.harness.sandbox_retry_recovery import (
    HarnessSandboxRetryRecoverySnapshot,
    build_sandbox_retry_recovery_snapshot,
    render_sandbox_retry_recovery_snapshot,
    unavailable_sandbox_retry_recovery_snapshot,
)
from naumi_agent.harness.store import (
    HarnessSandboxRetryCatalogItem,
    HarnessSandboxRetryCatalogPage,
    HarnessSandboxRetryDispatch,
)

NOW = "2026-07-24T01:00:00+00:00"
LEASE = "2026-07-24T01:05:00+00:00"


def _dispatch(index: int, *, pending: bool = False) -> HarnessSandboxRetryDispatch:
    return HarnessSandboxRetryDispatch(
        dispatch_id=f"hsard_{index:024x}",
        retry_action_id=f"hsar_{index:024x}",
        retry_receipt_id=f"hsarr_{index:024x}",
        retry_receipt_sha256=f"{index:x}" * 64,
        eval_request_sha256=f"{index + 1:x}" * 64,
        execution_authority_key=f"{index + 2:x}" * 64,
        state="pending" if pending else "claimed",
        owner_id="" if pending else f"private-owner-{index}",
        epoch=0 if pending else 1,
        ticket_id="" if pending else f"hsadm_{index:024x}",
        ticket_epoch=0 if pending else 1,
        created_at=NOW,
        updated_at=NOW,
        terminal_code="",
        request_sha256=f"{index + 3:x}" * 64,
    )


def _item(
    index: int,
    recovery_status: str,
) -> HarnessSandboxRetryCatalogItem:
    pending = recovery_status == "pending"
    return HarnessSandboxRetryCatalogItem(
        dispatch=_dispatch(index, pending=pending),
        cancel_receipt_id=f"hscan_{index:024x}",
        cancel_receipt_sha256=f"{index + 4:x}" * 64,
        source_ticket_id=f"hsadm_{index + 20:024x}",
        batch_id=f"batch-{index}",
        suite_id="startup-recovery",
        requested_samples=5,
        persisted_samples=2,
        ticket_state="" if pending else (
            "active" if recovery_status in {"live", "clock_regression"} else "expired"
        ),
        ticket_lease_expires_at="" if pending else LEASE,
        recovery_status=recovery_status,
    )


def _page(*items: HarnessSandboxRetryCatalogItem, next_cursor: str = ""):
    return HarnessSandboxRetryCatalogPage(
        workspace_root="/tmp/private-workspace",
        assessed_at=NOW,
        state_filter="open",
        limit=max(1, len(items)),
        items=items,
        next_cursor=next_cursor,
    )


def test_startup_recovery_snapshot_is_bounded_actionable_and_private() -> None:
    page = _page(
        _item(1, "pending"),
        _item(2, "live"),
        _item(3, "recovery_required"),
        _item(4, "reconcile_required"),
        _item(5, "clock_regression"),
        next_cursor="opaque-next-page",
    )

    snapshot = build_sandbox_retry_recovery_snapshot(page)
    payload = snapshot.model_dump(mode="json")
    rendered = render_sandbox_retry_recovery_snapshot(snapshot)

    assert snapshot.status == "ready"
    assert snapshot.total == 5
    assert snapshot.truncated is True
    assert snapshot.counts.model_dump() == {
        "pending": 1,
        "live": 1,
        "recovery_required": 1,
        "reconcile_required": 1,
        "clock_regression": 1,
        "actionable": 2,
    }
    assert [item.can_resume for item in snapshot.items] == [
        True,
        False,
        True,
        False,
        False,
    ]
    assert snapshot.items[0].resume_command.startswith(
        "/harness eval sandbox resume hsar_"
    )
    assert payload["workspace_sha256"] != page.workspace_root
    serialized = str(payload)
    assert page.workspace_root not in serialized
    assert "private-owner" not in serialized
    assert "execution_authority" not in serialized
    assert rendered.count("/harness eval sandbox resume") == 2
    assert "不会在启动时自动 claim" in rendered
    assert "当前 lease 尚有效" in rendered
    assert "队列已达到启动扫描上限" in rendered


def test_startup_recovery_snapshot_rejects_tampered_facts_and_digest() -> None:
    snapshot = build_sandbox_retry_recovery_snapshot(_page(_item(1, "pending")))
    payload = snapshot.model_dump(mode="json")

    tampered_command = dict(payload)
    tampered_command["items"] = [dict(payload["items"][0])]
    tampered_command["items"][0]["resume_command"] = (
        "/harness eval sandbox resume forged"
    )
    with pytest.raises(ValidationError, match="resume 命令"):
        HarnessSandboxRetryRecoverySnapshot.model_validate(tampered_command)

    tampered_digest = snapshot.model_dump(mode="json")
    tampered_digest["snapshot_sha256"] = "f" * 64
    with pytest.raises(ValidationError, match="SHA-256"):
        HarnessSandboxRetryRecoverySnapshot.model_validate(tampered_digest)


def test_unavailable_startup_recovery_snapshot_is_safe_and_self_consistent() -> None:
    snapshot = unavailable_sandbox_retry_recovery_snapshot(
        "/tmp/private-workspace",
        error_code="store_read_failed",
        assessed_at=NOW,
    )
    payload = snapshot.model_dump(mode="json")

    assert snapshot.status == "unavailable"
    assert snapshot.total == 0
    assert snapshot.items == ()
    assert snapshot.counts.classified_total == 0
    assert snapshot.error_code == "store_read_failed"
    assert "/tmp/private-workspace" not in str(payload)
    assert "没有自动 claim" in render_sandbox_retry_recovery_snapshot(snapshot)
    HarnessSandboxRetryRecoverySnapshot.model_validate(payload)


def test_startup_recovery_snapshot_rejects_wrong_catalog_scope_and_limit() -> None:
    item = _item(1, "pending")
    with pytest.raises(ValueError, match="open catalog"):
        build_sandbox_retry_recovery_snapshot(
            replace(_page(item), state_filter="all")
        )
    with pytest.raises(ValueError, match="1..20"):
        build_sandbox_retry_recovery_snapshot(
            replace(_page(item), limit=21)
        )
