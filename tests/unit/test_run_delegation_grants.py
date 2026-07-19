from __future__ import annotations

import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from naumi_agent.daemons.permission_decisions import (
    PermissionDecisionActor,
    PermissionDecisionOutcome,
    PermissionDecisionReceiptConflictError,
    PermissionDecisionReceiptStore,
    PermissionDecisionSource,
)
from naumi_agent.daemons.run_delegation_grants import (
    RUN_DELEGATION_GRANT_SCHEMA_VERSION,
    RunDelegationGrantAuthority,
    RunDelegationGrantConflictError,
    RunDelegationGrantError,
    RunDelegationGrantRequest,
    RunDelegationGrantStore,
    RunDelegationValidationReason,
)
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.store import HarnessStore
from naumi_agent.safety.permissions import PermissionMode

T0 = "2026-07-19T08:00:00+00:00"
T1 = "2026-07-19T08:00:01+00:00"
T301 = "2026-07-19T08:05:01+00:00"
T601 = "2026-07-19T08:10:01+00:00"


async def _authority(tmp_path: Path, *, lease_seconds: int = 1_800):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    permission_store = PermissionDecisionReceiptStore(
        tmp_path / "runtime" / "permission-decisions.db"
    )
    grant_store = RunDelegationGrantStore(
        tmp_path / "runtime" / "run-delegation-grants.db"
    )
    harness_store = HarnessStore(tmp_path / "state" / "harness.db")
    parent = await permission_store.issue(
        request_id="cohort-call",
        session_id="session-a",
        run_id="cohort-run",
        call_id="cohort-call",
        agent_name="main",
        tool_name="evolution_run_baseline",
        tool_family="evolution",
        arguments={"request_id": "red-cohort"},
        outcome=PermissionDecisionOutcome.BYPASS_ENABLED,
        actor=PermissionDecisionActor.RUNTIME,
        source=PermissionDecisionSource.BYPASS,
        permission_mode=PermissionMode.BYPASS,
        risk_level="high",
        delegated_tool_names=("bash_run",),
        decided_at=T0,
    )
    lease = await harness_store.acquire_run_lease(
        workspace_root=workspace,
        run_kind=HarnessRunKind.RUNTIME,
        run_id=parent.run_id,
        owner_id="cohort-owner",
        now=T1,
        lease_seconds=lease_seconds,
    )
    assert lease is not None
    authority = RunDelegationGrantAuthority(
        store=grant_store,
        permission_store=permission_store,
        harness_store=harness_store,
        workspace_root=workspace,
    )
    request = RunDelegationGrantRequest(
        idempotency_key="red-cohort-authority",
        parent_receipt_id=parent.receipt_id,
        run_kind=HarnessRunKind.RUNTIME,
        lease_owner_id=lease.owner_id,
        lease_epoch=lease.epoch,
        delegated_tool_names=("bash_run",),
    )
    return (
        authority,
        request,
        parent,
        lease,
        grant_store,
        permission_store,
        harness_store,
        workspace,
    )


@pytest.mark.asyncio
async def test_run_grant_survives_parent_freshness_window_and_reopens(
    tmp_path: Path,
) -> None:
    (
        authority,
        request,
        parent,
        lease,
        store,
        permissions,
        harness,
        workspace,
    ) = await _authority(tmp_path)

    issued = await authority.issue(request, now=T1, ttl_seconds=1_200)
    reopened = RunDelegationGrantAuthority(
        store=RunDelegationGrantStore(store.db_path),
        permission_store=PermissionDecisionReceiptStore(permissions.db_path),
        harness_store=HarnessStore(harness.db_path),
        workspace_root=workspace,
    )
    validation = await reopened.validate(
        grant_id=issued.contract.grant_id,
        now=T601,
    )
    regressed = await reopened.validate(
        grant_id=issued.contract.grant_id,
        now=T0,
    )

    assert validation.allowed
    assert validation.reasons == (RunDelegationValidationReason.VALID,)
    assert not regressed.allowed
    assert RunDelegationValidationReason.CLOCK_REGRESSION in regressed.reasons
    assert issued.contract.parent_receipt_sha256 == parent.receipt_sha256
    assert issued.contract.lease_owner_id == lease.owner_id
    assert issued.contract.lease_epoch == lease.epoch
    assert issued.contract.expires_at == "2026-07-19T08:20:01+00:00"
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == (
            RUN_DELEGATION_GRANT_SCHEMA_VERSION
        )
    if os.name != "nt":
        assert store.db_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_issue_is_idempotent_but_scope_expansion_conflicts(
    tmp_path: Path,
) -> None:
    authority, request, *_ = await _authority(tmp_path)
    first = await authority.issue(request, now=T1, ttl_seconds=1_200)
    replay = await authority.issue(request, now="2026-07-19T08:00:02+00:00", ttl_seconds=600)

    assert replay == first
    with pytest.raises(RunDelegationGrantConflictError, match="工具范围"):
        await authority.issue(
            replace(request, delegated_tool_names=("browser_click",)),
            now=T1,
            ttl_seconds=1_200,
        )


@pytest.mark.asyncio
async def test_issue_rejects_stale_parent_and_caps_expiry_to_lease(
    tmp_path: Path,
) -> None:
    authority, request, *_ = await _authority(tmp_path, lease_seconds=600)

    with pytest.raises(RunDelegationGrantConflictError, match="父权限回执已过期"):
        await authority.issue(request, now=T301, ttl_seconds=300)

    issued = await authority.issue(request, now=T1, ttl_seconds=3_600)
    assert issued.contract.expires_at == "2026-07-19T08:10:01+00:00"


@pytest.mark.asyncio
async def test_revoke_and_run_lease_release_fail_closed(tmp_path: Path) -> None:
    (
        authority,
        request,
        _parent,
        lease,
        store,
        _permissions,
        harness,
        workspace,
    ) = await _authority(tmp_path)
    issued = await authority.issue(request, now=T1, ttl_seconds=1_200)

    await store.revoke(
        grant_id=issued.contract.grant_id,
        reason="user_cancelled",
        revoked_at="2026-07-19T08:00:02+00:00",
    )
    revoked = await authority.validate(
        grant_id=issued.contract.grant_id,
        now="2026-07-19T08:00:03+00:00",
    )
    assert not revoked.allowed
    assert RunDelegationValidationReason.REVOKED in revoked.reasons

    second = await authority.issue(
        replace(request, idempotency_key="red-cohort-authority-2"),
        now="2026-07-19T08:00:04+00:00",
        ttl_seconds=1_200,
    )
    released = await harness.release_run_lease(
        workspace_root=workspace,
        run_kind=lease.run_kind,
        run_id=lease.run_id,
        owner_id=lease.owner_id,
        epoch=lease.epoch,
        now="2026-07-19T08:00:05+00:00",
    )
    assert released is not None
    fenced = await authority.validate(
        grant_id=second.contract.grant_id,
        now="2026-07-19T08:00:06+00:00",
    )
    assert not fenced.allowed
    assert RunDelegationValidationReason.LEASE_RELEASED in fenced.reasons
    assert RunDelegationValidationReason.LEASE_EXPIRED in fenced.reasons

    takeover = await harness.acquire_run_lease(
        workspace_root=workspace,
        run_kind=lease.run_kind,
        run_id=lease.run_id,
        owner_id="replacement-owner",
        now="2026-07-19T08:00:07+00:00",
        lease_seconds=600,
    )
    assert takeover is not None
    assert takeover.epoch == lease.epoch + 1
    stale_owner = await authority.validate(
        grant_id=second.contract.grant_id,
        now="2026-07-19T08:00:08+00:00",
    )
    assert not stale_owner.allowed
    assert RunDelegationValidationReason.LEASE_MISMATCH in stale_owner.reasons


@pytest.mark.asyncio
async def test_tampered_contract_is_rejected_on_reopen(tmp_path: Path) -> None:
    authority, request, _parent, _lease, store, *_ = await _authority(tmp_path)
    issued = await authority.issue(request, now=T1, ttl_seconds=1_200)
    with sqlite3.connect(store.db_path) as db:
        raw = db.execute(
            "SELECT contract_json FROM run_delegation_grants WHERE grant_id = ?",
            (issued.contract.grant_id,),
        ).fetchone()[0]
        db.execute(
            "UPDATE run_delegation_grants SET contract_json = ? WHERE grant_id = ?",
            (raw.replace("bash_run", "browser_click"), issued.contract.grant_id),
        )

    with pytest.raises(RunDelegationGrantError, match="无法读取"):
        await RunDelegationGrantStore(store.db_path).get(issued.contract.grant_id)


@pytest.mark.asyncio
async def test_run_grant_issues_exact_short_child_after_parent_window(
    tmp_path: Path,
) -> None:
    authority, request, parent, _lease, store, permissions, *_ = await _authority(
        tmp_path
    )
    issued = await authority.issue(request, now=T1, ttl_seconds=1_200)

    child = await permissions.issue_run_delegated(
        run_grant_authority=authority,
        run_grant_id=issued.contract.grant_id,
        request_id="late-shell-child",
        call_id="late-shell-child",
        tool_name="bash_run",
        tool_family="shell",
        arguments={"argv": ["/usr/bin/true"]},
        risk_level="high",
        decided_at=T601,
        ttl_seconds=120,
    )

    assert child.parent_receipt_id == parent.receipt_id
    assert child.run_delegation_grant_id == issued.contract.grant_id
    assert child.run_delegation_grant_sha256 == issued.contract.grant_sha256
    assert child.expires_at == "2026-07-19T08:12:01+00:00"
    assert await PermissionDecisionReceiptStore(permissions.db_path).get(
        child.receipt_id
    ) == child

    await store.revoke(
        grant_id=issued.contract.grant_id,
        reason="user_cancelled",
        revoked_at="2026-07-19T08:10:02+00:00",
    )
    with pytest.raises(PermissionDecisionReceiptConflictError, match="当前无效"):
        await permissions.issue_run_delegated(
            run_grant_authority=authority,
            run_grant_id=issued.contract.grant_id,
            request_id="revoked-shell-child",
            call_id="revoked-shell-child",
            tool_name="bash_run",
            tool_family="shell",
            arguments={"argv": ["/usr/bin/true"]},
            risk_level="high",
            decided_at="2026-07-19T08:10:03+00:00",
        )
