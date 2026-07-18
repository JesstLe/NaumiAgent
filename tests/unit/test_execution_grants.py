from __future__ import annotations

import asyncio
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from naumi_agent.daemons.execution_grants import (
    EXECUTION_GRANT_SCHEMA_VERSION,
    ExecutionGrantAuthority,
    ExecutionGrantConflictError,
    ExecutionGrantError,
    ExecutionGrantRequest,
    ExecutionGrantSource,
    ExecutionGrantState,
    ExecutionGrantStore,
    ExecutionGrantValidationReason,
    execution_arguments_sha256,
)
from naumi_agent.daemons.permission_decisions import (
    PermissionDecisionActor,
    PermissionDecisionOutcome,
    PermissionDecisionReceiptStore,
    PermissionDecisionSource,
)
from naumi_agent.daemons.worker_contract import (
    WorkerCapability,
    WorkerIsolationContract,
    WorkerKind,
    WorkerResourceEnvelope,
    detect_worker_platform,
    issue_worker_contract,
)
from naumi_agent.daemons.worker_registry import WorkerRegistryStore
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.store import HarnessStore
from naumi_agent.safety.permissions import (
    PermissionChecker,
    PermissionDecision,
    PermissionMode,
    PermissionOutcome,
)

T0 = "2026-07-19T00:00:00+00:00"
T1 = "2026-07-19T00:00:01+00:00"
T2 = "2026-07-19T00:00:02+00:00"


def _contract(epoch: int = 1, *, kind: WorkerKind = WorkerKind.TOOL):
    capability = (
        WorkerCapability.SHELL_NON_PTY
        if kind is WorkerKind.TOOL
        else WorkerCapability.BROWSER_PROFILE_ISOLATION
    )
    return issue_worker_contract(
        worker_id="tool-worker-a",
        instance_id=f"process-{epoch}",
        epoch=epoch,
        kind=kind,
        protocol_min=1,
        protocol_max=1,
        software_version="0.1.214",
        platform=detect_worker_platform(
            system="Linux",
            machine="x86_64",
            python_implementation="CPython",
            python_version="3.13.5",
        ),
        capabilities=(capability,),
        resources=WorkerResourceEnvelope(
            max_concurrent_jobs=2,
            max_memory_bytes=512 * 1024 * 1024,
            max_cpu_seconds=60,
            max_wall_seconds=120,
            max_output_bytes=8 * 1024 * 1024,
        ),
        isolation=WorkerIsolationContract(False, False, False, False, False, False),
        issued_at=T0 if epoch == 1 else T2,
    )


def _request(*, arguments=None, idempotency_key: str = "job-key-1"):
    return ExecutionGrantRequest(
        session_id="session-a",
        run_id="tool-run-a",
        call_id="call-a",
        tool_name="bash_run",
        arguments=arguments or {"command": "printf safe", "cwd": "/workspace"},
        idempotency_key=idempotency_key,
        worker_id="tool-worker-a",
        authorization_reference="runtime-bypass",
    )


async def _authority(tmp_path: Path):
    registry = WorkerRegistryStore(tmp_path / "runtime" / "worker-registry.db")
    harness = HarnessStore(tmp_path / "state" / "harness.db")
    store = ExecutionGrantStore(tmp_path / "runtime" / "execution-grants.db")
    decision_store = PermissionDecisionReceiptStore(
        tmp_path / "runtime" / "permission-decisions.db"
    )
    contract = _contract()
    await registry.register(contract, registered_at=T1)
    lease = await harness.acquire_run_lease(
        workspace_root=tmp_path / "workspace",
        run_kind=HarnessRunKind.TOOL,
        run_id="tool-run-a",
        owner_id=contract.instance_id,
        now=T1,
        lease_seconds=60,
    )
    assert lease is not None
    authority = ExecutionGrantAuthority(
        store=store,
        worker_registry=registry,
        harness_store=harness,
        permission_decision_store=decision_store,
        workspace_root=tmp_path / "workspace",
    )
    return authority, store, registry, harness, contract, lease, decision_store


async def _authorize(
    store: PermissionDecisionReceiptStore,
    request: ExecutionGrantRequest,
    *,
    source: ExecutionGrantSource = ExecutionGrantSource.BYPASS,
) -> ExecutionGrantRequest:
    receipt_source, outcome, mode = {
        ExecutionGrantSource.BYPASS: (
            PermissionDecisionSource.BYPASS,
            PermissionDecisionOutcome.BYPASS_ENABLED,
            PermissionMode.BYPASS,
        ),
        ExecutionGrantSource.USER_CONFIRMATION: (
            PermissionDecisionSource.USER_CONFIRMATION,
            PermissionDecisionOutcome.ALLOW_ONCE,
            PermissionMode.MODERATE,
        ),
        ExecutionGrantSource.SESSION_GRANT: (
            PermissionDecisionSource.SESSION_GRANT,
            PermissionDecisionOutcome.SESSION_GRANTED,
            PermissionMode.MODERATE,
        ),
    }[source]
    receipt = await store.issue(
        request_id=request.call_id,
        session_id=request.session_id,
        run_id=request.run_id,
        call_id=request.call_id,
        agent_name="main",
        tool_name=request.tool_name,
        tool_family="shell",
        arguments=request.arguments,
        outcome=outcome,
        actor=PermissionDecisionActor.USER,
        source=receipt_source,
        permission_mode=mode,
        risk_level="high",
        source_grant_id="grant-session-a" if source is ExecutionGrantSource.SESSION_GRANT else "",
        decided_at=T2,
    )
    return replace(request, authorization_reference=receipt.receipt_id)


def _bypass_decision() -> PermissionDecision:
    return PermissionChecker(PermissionMode.BYPASS).check("bash_run", {})


@pytest.mark.asyncio
async def test_issue_reopen_and_validate_without_persisting_raw_arguments(
    tmp_path: Path,
) -> None:
    authority, store, registry, harness, contract, lease, decision_store = await _authority(
        tmp_path
    )
    request = await _authorize(
        decision_store,
        _request(arguments={"command": "printf super-secret-token"}),
    )

    issued = await authority.issue(
        request,
        decision=_bypass_decision(),
        permission_mode=PermissionMode.BYPASS,
        source=ExecutionGrantSource.BYPASS,
        now=T2,
        ttl_seconds=30,
    )
    reopened_store = ExecutionGrantStore(store.db_path)
    reopened = await reopened_store.get(issued.contract.grant_id)
    reopened_authority = ExecutionGrantAuthority(
        store=reopened_store,
        worker_registry=WorkerRegistryStore(registry.db_path),
        harness_store=HarnessStore(harness.db_path),
        permission_decision_store=PermissionDecisionReceiptStore(decision_store.db_path),
        workspace_root=tmp_path / "workspace",
    )
    validation = await reopened_authority.validate(
        grant_id=issued.contract.grant_id,
        request=request,
        now="2026-07-19T00:00:10+00:00",
    )

    assert reopened == issued
    assert issued.state is ExecutionGrantState.ACTIVE
    assert issued.contract.worker_contract_sha256 == contract.contract_sha256
    assert issued.contract.worker_epoch == contract.epoch
    assert issued.contract.lease_owner_id == lease.owner_id
    assert issued.contract.lease_epoch == lease.epoch
    assert issued.contract.expires_at == "2026-07-19T00:00:32+00:00"
    assert validation.allowed
    assert validation.reasons == (ExecutionGrantValidationReason.VALID,)
    assert b"super-secret-token" not in store.db_path.read_bytes()
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        raw = db.execute("SELECT contract_json FROM execution_grants").fetchone()[0]
    assert "super-secret-token" not in raw
    if os.name != "nt":
        assert store.db_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_idempotent_retry_reuses_first_grant_and_conflicting_args_fail(
    tmp_path: Path,
) -> None:
    authority, *_, decision_store = await _authority(tmp_path)
    first_request = await _authorize(decision_store, _request(arguments={"b": 2, "a": 1}))
    reordered = replace(first_request, arguments={"a": 1, "b": 2})
    changed = replace(first_request, arguments={"a": 1, "b": 3})

    first = await authority.issue(
        first_request,
        decision=_bypass_decision(),
        permission_mode=PermissionMode.BYPASS,
        source=ExecutionGrantSource.BYPASS,
        now=T2,
    )
    replay = await authority.issue(
        reordered,
        decision=_bypass_decision(),
        permission_mode=PermissionMode.BYPASS,
        source=ExecutionGrantSource.BYPASS,
        now="2026-07-19T00:00:03+00:00",
    )

    assert replay == first
    assert execution_arguments_sha256(first_request.arguments) == execution_arguments_sha256(
        reordered.arguments
    )
    with pytest.raises(ExecutionGrantConflictError, match="回执与执行请求不匹配"):
        await authority.issue(
            changed,
            decision=_bypass_decision(),
            permission_mode=PermissionMode.BYPASS,
            source=ExecutionGrantSource.BYPASS,
            now="2026-07-19T00:00:04+00:00",
        )


@pytest.mark.asyncio
async def test_concurrent_same_key_converges_on_one_durable_grant(tmp_path: Path) -> None:
    authority, store, *_, decision_store = await _authority(tmp_path)
    request = await _authorize(decision_store, _request())

    issued = await asyncio.gather(
        *(
            authority.issue(
                request,
                decision=_bypass_decision(),
                permission_mode=PermissionMode.BYPASS,
                source=ExecutionGrantSource.BYPASS,
                now=T2,
            )
            for _ in range(8)
        )
    )

    assert len({item.contract.grant_id for item in issued}) == 1
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM execution_grants").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_validation_detects_argument_change_expiry_and_revocation(
    tmp_path: Path,
) -> None:
    authority, store, *_, decision_store = await _authority(tmp_path)
    request = await _authorize(decision_store, _request())
    issued = await authority.issue(
        request,
        decision=_bypass_decision(),
        permission_mode=PermissionMode.BYPASS,
        source=ExecutionGrantSource.BYPASS,
        now=T2,
        ttl_seconds=10,
    )

    changed = await authority.validate(
        grant_id=issued.contract.grant_id,
        request=replace(request, arguments={"command": "printf changed"}),
        now="2026-07-19T00:00:05+00:00",
    )
    expired = await authority.validate(
        grant_id=issued.contract.grant_id,
        request=request,
        now="2026-07-19T00:00:12+00:00",
    )
    await store.revoke(
        grant_id=issued.contract.grant_id,
        reason="operator_stop",
        revoked_at="2026-07-19T00:00:06+00:00",
    )
    revoked = await authority.validate(
        grant_id=issued.contract.grant_id,
        request=request,
        now="2026-07-19T00:00:07+00:00",
    )

    assert changed.reasons == (ExecutionGrantValidationReason.REQUEST_MISMATCH,)
    assert expired.reasons == (ExecutionGrantValidationReason.EXPIRED,)
    assert revoked.reasons == (ExecutionGrantValidationReason.REVOKED,)


@pytest.mark.asyncio
async def test_worker_takeover_and_lease_release_fence_existing_grant(
    tmp_path: Path,
) -> None:
    authority, _, registry, harness, _, lease, decision_store = await _authority(tmp_path)
    request = await _authorize(decision_store, _request())
    issued = await authority.issue(
        request,
        decision=_bypass_decision(),
        permission_mode=PermissionMode.BYPASS,
        source=ExecutionGrantSource.BYPASS,
        now=T2,
    )
    await registry.register(_contract(2), registered_at="2026-07-19T00:00:03+00:00")
    fenced = await authority.validate(
        grant_id=issued.contract.grant_id,
        request=request,
        now="2026-07-19T00:00:04+00:00",
    )
    await harness.release_run_lease(
        workspace_root=tmp_path / "workspace",
        run_kind="tool",
        run_id=request.run_id,
        owner_id=lease.owner_id,
        epoch=lease.epoch,
        now="2026-07-19T00:00:05+00:00",
    )
    released = await authority.validate(
        grant_id=issued.contract.grant_id,
        request=request,
        now="2026-07-19T00:00:06+00:00",
    )

    assert ExecutionGrantValidationReason.WORKER_FENCED in fenced.reasons
    assert ExecutionGrantValidationReason.LEASE_RELEASED in released.reasons


@pytest.mark.asyncio
async def test_authorization_source_rules_reject_unproven_bypass_or_confirmation(
    tmp_path: Path,
) -> None:
    authority, *_, decision_store = await _authority(tmp_path)
    confirm = PermissionDecision(
        allowed=True,
        requires_confirmation=True,
        outcome=PermissionOutcome.CONFIRM,
        tool_family="shell",
        allow_session_grant=True,
    )
    with pytest.raises(ValueError, match="bypass grant"):
        await authority.issue(
            _request(),
            decision=confirm,
            permission_mode=PermissionMode.MODERATE,
            source=ExecutionGrantSource.BYPASS,
            now=T2,
        )
    with pytest.raises(ValueError, match="用户确认 grant"):
        await authority.issue(
            _request(),
            decision=_bypass_decision(),
            permission_mode=PermissionMode.BYPASS,
            source=ExecutionGrantSource.USER_CONFIRMATION,
            now=T2,
        )

    user_request = await _authorize(
        decision_store,
        _request(idempotency_key="confirmed-key"),
        source=ExecutionGrantSource.USER_CONFIRMATION,
    )
    session_request = await _authorize(
        decision_store,
        replace(
            _request(idempotency_key="session-key"),
            call_id="call-session",
        ),
        source=ExecutionGrantSource.SESSION_GRANT,
    )
    user_confirmed = await authority.issue(
        user_request,
        decision=confirm,
        permission_mode=PermissionMode.MODERATE,
        source=ExecutionGrantSource.USER_CONFIRMATION,
        now=T2,
    )
    session_confirmed = await authority.issue(
        session_request,
        decision=confirm,
        permission_mode=PermissionMode.MODERATE,
        source=ExecutionGrantSource.SESSION_GRANT,
        now=T2,
    )
    assert user_confirmed.contract.source is ExecutionGrantSource.USER_CONFIRMATION
    assert session_confirmed.contract.source is ExecutionGrantSource.SESSION_GRANT


@pytest.mark.asyncio
async def test_issue_rejects_missing_denied_or_mismatched_decision_receipt(
    tmp_path: Path,
) -> None:
    authority, *_, decision_store = await _authority(tmp_path)
    request = _request()
    with pytest.raises(ExecutionGrantConflictError, match="回执不存在"):
        await authority.issue(
            request,
            decision=_bypass_decision(),
            permission_mode=PermissionMode.BYPASS,
            source=ExecutionGrantSource.BYPASS,
            now=T2,
        )

    denied = await decision_store.issue(
        request_id="denied-call",
        session_id=request.session_id,
        run_id=request.run_id,
        call_id="denied-call",
        agent_name="main",
        tool_name=request.tool_name,
        tool_family="shell",
        arguments=request.arguments,
        outcome=PermissionDecisionOutcome.DENIED,
        actor=PermissionDecisionActor.USER,
        source=PermissionDecisionSource.USER_CONFIRMATION,
        permission_mode=PermissionMode.MODERATE,
        risk_level="high",
        decided_at=T2,
    )
    with pytest.raises(ExecutionGrantConflictError, match="回执与执行请求不匹配"):
        await authority.issue(
            replace(
                request,
                call_id="denied-call",
                authorization_reference=denied.receipt_id,
            ),
            decision=PermissionDecision(
                allowed=True,
                requires_confirmation=True,
                outcome=PermissionOutcome.CONFIRM,
                tool_family="shell",
            ),
            permission_mode=PermissionMode.MODERATE,
            source=ExecutionGrantSource.USER_CONFIRMATION,
            now=T2,
        )

    authorized = await _authorize(decision_store, replace(request, call_id="other-call"))
    with pytest.raises(ExecutionGrantConflictError, match="回执与执行请求不匹配"):
        await authority.issue(
            replace(authorized, call_id="tampered-call"),
            decision=_bypass_decision(),
            permission_mode=PermissionMode.BYPASS,
            source=ExecutionGrantSource.BYPASS,
            now=T2,
        )

    stale_request = replace(request, call_id="stale-call")
    stale = await decision_store.issue(
        request_id=stale_request.call_id,
        session_id=stale_request.session_id,
        run_id=stale_request.run_id,
        call_id=stale_request.call_id,
        agent_name="main",
        tool_name=stale_request.tool_name,
        tool_family="shell",
        arguments=stale_request.arguments,
        outcome=PermissionDecisionOutcome.BYPASS_ENABLED,
        actor=PermissionDecisionActor.USER,
        source=PermissionDecisionSource.BYPASS,
        permission_mode=PermissionMode.BYPASS,
        risk_level="high",
        decided_at=T0,
    )
    with pytest.raises(ExecutionGrantConflictError, match="已过期"):
        await authority.issue(
            replace(stale_request, authorization_reference=stale.receipt_id),
            decision=_bypass_decision(),
            permission_mode=PermissionMode.BYPASS,
            source=ExecutionGrantSource.BYPASS,
            now="2026-07-19T00:10:00+00:00",
        )


@pytest.mark.asyncio
async def test_issue_rejects_non_tool_worker_even_with_matching_lease_owner(
    tmp_path: Path,
) -> None:
    registry = WorkerRegistryStore(tmp_path / "worker-registry.db")
    harness = HarnessStore(tmp_path / "harness.db")
    browser = _contract(kind=WorkerKind.BROWSER)
    await registry.register(browser, registered_at=T1)
    await harness.acquire_run_lease(
        workspace_root=tmp_path,
        run_kind="tool",
        run_id="tool-run-a",
        owner_id=browser.instance_id,
        now=T1,
        lease_seconds=60,
    )
    authority = ExecutionGrantAuthority(
        store=ExecutionGrantStore(tmp_path / "execution-grants.db"),
        worker_registry=registry,
        harness_store=harness,
        permission_decision_store=PermissionDecisionReceiptStore(
            tmp_path / "permission-decisions.db"
        ),
        workspace_root=tmp_path,
    )
    request = await _authorize(authority._permission_decision_store, _request())

    with pytest.raises(ExecutionGrantConflictError, match="Tool Worker"):
        await authority.issue(
            request,
            decision=_bypass_decision(),
            permission_mode=PermissionMode.BYPASS,
            source=ExecutionGrantSource.BYPASS,
            now=T2,
        )


@pytest.mark.asyncio
async def test_issue_requires_live_lease_owned_by_active_worker(tmp_path: Path) -> None:
    registry = WorkerRegistryStore(tmp_path / "worker-registry.db")
    harness = HarnessStore(tmp_path / "harness.db")
    worker = _contract()
    await registry.register(worker, registered_at=T1)
    authority = ExecutionGrantAuthority(
        store=ExecutionGrantStore(tmp_path / "execution-grants.db"),
        worker_registry=registry,
        harness_store=harness,
        permission_decision_store=PermissionDecisionReceiptStore(
            tmp_path / "permission-decisions.db"
        ),
        workspace_root=tmp_path,
    )
    request = await _authorize(authority._permission_decision_store, _request())
    with pytest.raises(ExecutionGrantConflictError, match="lease 不存在"):
        await authority.issue(
            request,
            decision=_bypass_decision(),
            permission_mode=PermissionMode.BYPASS,
            source=ExecutionGrantSource.BYPASS,
            now=T2,
        )

    await harness.acquire_run_lease(
        workspace_root=tmp_path,
        run_kind="tool",
        run_id="tool-run-a",
        owner_id="different-process",
        now=T1,
        lease_seconds=60,
    )
    with pytest.raises(ExecutionGrantConflictError, match="owner"):
        await authority.issue(
            request,
            decision=_bypass_decision(),
            permission_mode=PermissionMode.BYPASS,
            source=ExecutionGrantSource.BYPASS,
            now=T2,
        )


@pytest.mark.asyncio
async def test_store_rejects_tamper_future_schema_and_wrong_type(tmp_path: Path) -> None:
    authority, store, *_, decision_store = await _authority(tmp_path)
    request = await _authorize(decision_store, _request())
    issued = await authority.issue(
        request,
        decision=_bypass_decision(),
        permission_mode=PermissionMode.BYPASS,
        source=ExecutionGrantSource.BYPASS,
        now=T2,
    )
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE execution_grants SET contract_json = '{}' WHERE grant_id = ?",
            (issued.contract.grant_id,),
        )
        db.commit()
    with pytest.raises(ExecutionGrantError, match="无法读取"):
        await ExecutionGrantStore(store.db_path).get(issued.contract.grant_id)

    future = tmp_path / "future.db"
    with sqlite3.connect(future) as db:
        db.execute(f"PRAGMA user_version = {EXECUTION_GRANT_SCHEMA_VERSION + 1}")
    with pytest.raises(ExecutionGrantError, match="不受支持"):
        await ExecutionGrantStore(future).get("a" * 32)

    wrong_type = tmp_path / "directory"
    wrong_type.mkdir()
    with pytest.raises(ExecutionGrantError, match="不是文件"):
        await ExecutionGrantStore(wrong_type).get("a" * 32)


def test_argument_digest_rejects_non_finite_and_oversized_inputs() -> None:
    with pytest.raises(ValueError, match="非有限"):
        execution_arguments_sha256({"temperature": float("nan")})
    with pytest.raises(ValueError, match="大小上限"):
        execution_arguments_sha256({"payload": "x" * (256 * 1024 + 1)})
    with pytest.raises(TypeError, match="键必须是字符串"):
        execution_arguments_sha256({"nested": {1: "ambiguous"}})
