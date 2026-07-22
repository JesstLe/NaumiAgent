from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.evolution.approval_decisions import (
    EvolutionPromotionApprovalDecisionStatus,
)
from naumi_agent.evolution.revalidation_requests import (
    EVOLUTION_REVALIDATION_REQUEST_POLICY,
    EvolutionRevalidationRequest,
    EvolutionRevalidationRequestBuilder,
    EvolutionRevalidationRequestError,
    EvolutionRevalidationRequestService,
    EvolutionRevalidationRequestStore,
    render_evolution_revalidation_request,
)
from naumi_agent.safety.permissions import (
    TOOL_PERMISSIONS,
    PermissionMode,
    PermissionRiskLevel,
)
from naumi_agent.tools.evolution_review import (
    EvolutionRevalidationRequestAuthorityTool,
    EvolutionRevalidationRequestTool,
)
from tests.unit.test_evolution_approval_decisions import (
    _fully_approve,
    _keypair,
    _respond,
    _setup,
)
from tests.unit.test_evolution_promotion_packages import _git


def _service(authority) -> EvolutionRevalidationRequestService:
    return EvolutionRevalidationRequestService(
        decision_service=authority.decision_service,
        package_executor=authority.package_executor,
        request_store=EvolutionRevalidationRequestStore(authority.db_path),
    )


async def _approved(authority):
    await _fully_approve(authority)
    view = await authority.decision_service.execute(
        workspace_root=authority.root,
        requirement_id=authority.requirement_view.requirement.requirement_id,
    )
    assert view.receipt.status is EvolutionPromotionApprovalDecisionStatus.APPROVED
    return view


def _rehash_request(payload: dict) -> dict:
    artifact = {
        key: value
        for key, value in payload.items()
        if key not in {"request_id", "request_sha256"}
    }
    digest = hashlib.sha256(
        json.dumps(
            artifact,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    return {
        **artifact,
        "request_id": f"evrevalidation_{digest[:24]}",
        "request_sha256": digest,
    }


@pytest.mark.asyncio
async def test_current_approval_issues_one_durable_non_executing_request(
    tmp_path: Path,
) -> None:
    authority = await _setup(tmp_path)
    approved = await _approved(authority)
    service = _service(authority)

    views = await asyncio.gather(
        *(
            service.issue(
                workspace_root=tmp_path,
                decision_id=approved.receipt.decision_id,
            )
            for _ in range(8)
        )
    )

    assert all(view == views[0] for view in views)
    view = views[0]
    request = view.request
    assert view.current_status == "ready"
    assert view.execution_eligible
    assert request.policy_version == EVOLUTION_REVALIDATION_REQUEST_POLICY
    assert request.decision_id == approved.receipt.decision_id
    assert request.package_id == approved.receipt.package_id
    assert request.operation in {
        "validate_exact_tree",
        "rebase_then_validate",
        "block_for_reconciliation",
    }
    assert request.manual_reconciliation_required is (
        request.operation == "block_for_reconciliation"
    )
    assert request.sandbox_required
    assert request.current_approval_required
    assert request.validation_receipts_must_be_reissued
    assert not request.network_allowed
    assert not request.dependency_install_allowed
    assert not request.main_worktree_write_allowed
    assert not request.target_branch_write_allowed
    assert not request.execution_started
    assert not request.rebase_executed
    assert not request.validation_executed
    assert not request.promotion_authority
    assert not request.merge_executed
    assert not request.push_executed
    assert not request.publish_executed
    assert not request.contains_source_code
    assert not request.contains_freeform_narrative
    assert not request.llm_generated
    with sqlite3.connect(authority.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM evolution_revalidation_requests").fetchone() == (1,)

    rendered = render_evolution_revalidation_request(view)
    assert request.request_id in rendered
    assert "只授权未来隔离 rebase/revalidation 输入" in rendered
    assert "当前没有执行 Git write" in rendered

    engine = SimpleNamespace(
        workspace_root=tmp_path,
        evolution_revalidation_request_service=service,
    )
    outputs = (
        await EvolutionRevalidationRequestTool(engine).execute(approved.receipt.decision_id),
        await EvolutionRevalidationRequestAuthorityTool(engine).execute(request.request_id),
        await execute_slash_command(
            engine,
            f"/evolution revalidation-request {approved.receipt.decision_id}",
        ),
        await execute_slash_command(
            engine,
            f"/evolution revalidation-request show {request.request_id}",
        ),
    )
    assert all(request.request_id in output for output in outputs)
    assert all("Promotion" in output for output in outputs)


@pytest.mark.asyncio
async def test_target_relation_selects_exact_rebase_or_manual_reconciliation(
    tmp_path: Path,
) -> None:
    authority = await _setup(tmp_path)
    approved = await _approved(authority)
    current = await _service(authority).issue(
        workspace_root=tmp_path,
        decision_id=approved.receipt.decision_id,
    )
    same = current.request
    assert same.operation == "validate_exact_tree"
    assert not same.manual_reconciliation_required

    advanced_payload = same.model_dump(mode="json")
    advanced_payload.update(
        target_head="a" * 40,
        target_relation="advanced",
        operation="rebase_then_validate",
        manual_reconciliation_required=False,
    )
    advanced = EvolutionRevalidationRequest.model_validate(
        _rehash_request(advanced_payload)
    )
    assert advanced.operation == "rebase_then_validate"

    diverged_payload = advanced.model_dump(mode="json")
    diverged_payload.update(
        target_relation="diverged",
        operation="block_for_reconciliation",
        manual_reconciliation_required=True,
    )
    diverged = EvolutionRevalidationRequest.model_validate(
        _rehash_request(diverged_payload)
    )
    assert diverged.operation == "block_for_reconciliation"
    assert diverged.manual_reconciliation_required

    diverged_payload["manual_reconciliation_required"] = False
    with pytest.raises(ValueError, match="reconciliation"):
        EvolutionRevalidationRequest.model_validate(
            _rehash_request(diverged_payload)
        )


@pytest.mark.asyncio
async def test_pending_or_rejected_decision_cannot_issue_request(tmp_path: Path) -> None:
    authority = await _setup(tmp_path)
    pending = await authority.decision_service.execute(
        workspace_root=tmp_path,
        requirement_id=authority.requirement_view.requirement.requirement_id,
    )
    assert pending.receipt.status is EvolutionPromotionApprovalDecisionStatus.PENDING

    with pytest.raises(EvolutionRevalidationRequestError) as blocked:
        await _service(authority).issue(
            workspace_root=tmp_path,
            decision_id=pending.receipt.decision_id,
        )
    assert blocked.value.code == "revalidation_request_decision_ineligible"

    await _respond(authority, "independent_reviewer", "reject")
    rejected = await authority.decision_service.execute(
        workspace_root=tmp_path,
        requirement_id=authority.requirement_view.requirement.requirement_id,
    )
    assert rejected.receipt.status is EvolutionPromotionApprovalDecisionStatus.REJECTED
    with pytest.raises(EvolutionRevalidationRequestError) as rejected_blocked:
        await _service(authority).issue(
            workspace_root=tmp_path,
            decision_id=rejected.receipt.decision_id,
        )
    assert rejected_blocked.value.code == "revalidation_request_decision_ineligible"

    with sqlite3.connect(authority.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name = 'evolution_revalidation_requests'"
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_target_move_makes_existing_request_stale(tmp_path: Path) -> None:
    authority = await _setup(tmp_path)
    approved = await _approved(authority)
    service = _service(authority)
    issued = await service.issue(
        workspace_root=tmp_path,
        decision_id=approved.receipt.decision_id,
    )

    (tmp_path / "target-moved.txt").write_text("advanced\n", encoding="utf-8")
    _git(tmp_path, "add", "target-moved.txt")
    _git(tmp_path, "commit", "-m", "advance revalidation target")
    stale = await service.inspect(
        workspace_root=tmp_path,
        request_id=issued.request.request_id,
    )

    assert stale.current_status == "stale"
    assert not stale.package_current
    assert not stale.target_current
    assert not stale.execution_eligible


@pytest.mark.asyncio
async def test_principal_rotation_makes_request_ineligible(tmp_path: Path) -> None:
    authority = await _setup(tmp_path)
    approved = await _approved(authority)
    service = _service(authority)
    issued = await service.issue(
        workspace_root=tmp_path,
        decision_id=approved.receipt.decision_id,
    )
    principal = authority.principals["independent_reviewer"].principal.principal
    _private_key, replacement_public_key = _keypair()
    await authority.principal_service.rotate_key(
        workspace_root=tmp_path,
        principal_id=principal.principal_id,
        public_key_base64=replacement_public_key,
    )

    current = await service.inspect(
        workspace_root=tmp_path,
        request_id=issued.request.request_id,
    )
    assert current.current_status == "ineligible"
    assert not current.decision_current
    assert current.package_current
    assert current.target_current
    assert not current.execution_eligible


@pytest.mark.asyncio
async def test_store_revalidates_complete_sources_and_fails_closed_on_tamper(
    tmp_path: Path,
) -> None:
    authority = await _setup(tmp_path)
    approved = await _approved(authority)
    decision_view = await authority.decision_service.inspect(
        workspace_root=tmp_path,
        decision_id=approved.receipt.decision_id,
    )
    package_view = await authority.package_executor.inspect(
        workspace_root=tmp_path,
        package_id=approved.receipt.package_id,
    )
    request = EvolutionRevalidationRequestBuilder().build(
        decision_view=decision_view,
        package_view=package_view,
    )
    with sqlite3.connect(authority.db_path) as db:
        package_json = db.execute(
            "SELECT package_json FROM evolution_promotion_packages WHERE package_id = ?",
            (request.package_id,),
        ).fetchone()[0]
        package_payload = json.loads(package_json)
        package_payload["target"]["target_branch"] = "tampered"
        db.execute(
            "UPDATE evolution_promotion_packages SET package_json = ? WHERE package_id = ?",
            (
                json.dumps(package_payload, sort_keys=True, separators=(",", ":")),
                request.package_id,
            ),
        )
        db.commit()

    with pytest.raises(EvolutionRevalidationRequestError) as corrupted_source:
        await EvolutionRevalidationRequestStore(authority.db_path).record(
            request,
            decision=decision_view.receipt,
            package=package_view.package,
        )
    assert corrupted_source.value.code == "revalidation_request_source_store_corrupt"


@pytest.mark.asyncio
async def test_request_digest_index_and_workspace_tamper_fail_closed(
    tmp_path: Path,
) -> None:
    authority = await _setup(tmp_path)
    approved = await _approved(authority)
    service = _service(authority)
    issued = await service.issue(
        workspace_root=tmp_path,
        decision_id=approved.receipt.decision_id,
    )
    request = issued.request
    forged = request.model_dump(mode="json")
    forged["network_allowed"] = True
    with pytest.raises(ValueError):
        EvolutionRevalidationRequest.model_validate(forged)

    with pytest.raises(EvolutionRevalidationRequestError) as wrong_workspace:
        await service.inspect(
            workspace_root=tmp_path / "other",
            request_id=request.request_id,
        )
    assert wrong_workspace.value.code == "revalidation_request_workspace_mismatch"

    with sqlite3.connect(authority.db_path) as db:
        db.execute(
            "DELETE FROM evolution_promotion_approval_decisions WHERE decision_id = ?",
            (request.decision_id,),
        )
        db.commit()
    unreadable = await service.inspect(
        workspace_root=tmp_path,
        request_id=request.request_id,
    )
    assert not unreadable.source_readable
    assert unreadable.current_status == "ineligible"
    assert not unreadable.execution_eligible

    with sqlite3.connect(authority.db_path) as db:
        db.execute(
            "UPDATE evolution_revalidation_requests SET target_head = ? WHERE request_id = ?",
            ("0" * 40, request.request_id),
        )
        db.commit()
    with pytest.raises(EvolutionRevalidationRequestError) as corrupt_index:
        await EvolutionRevalidationRequestStore(authority.db_path).get(request.request_id)
    assert corrupt_index.value.code == "revalidation_request_store_corrupt"


def test_service_permissions_tools_and_lazy_exports_are_exact(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="Decision Service"):
        EvolutionRevalidationRequestService(
            decision_service=object(),  # type: ignore[arg-type]
            package_executor=object(),  # type: ignore[arg-type]
            request_store=EvolutionRevalidationRequestStore(tmp_path / "state.db"),
        )

    rule = TOOL_PERMISSIONS["evolution_revalidation_request"]
    assert rule.allowed_modes == [
        PermissionMode.BYPASS,
        PermissionMode.PERMISSIVE,
        PermissionMode.MODERATE,
        PermissionMode.STRICT,
    ]
    assert rule.risk_level is PermissionRiskLevel.MEDIUM
    assert rule.max_calls_per_session == 50
    assert not rule.requires_confirmation
    assert rule.tool_family == "evolution_promotion_artifact"
    assert not EvolutionRevalidationRequestTool(SimpleNamespace()).metadata.read_only
    assert EvolutionRevalidationRequestAuthorityTool(SimpleNamespace()).metadata.read_only

    import naumi_agent.evolution as evolution

    assert evolution.EvolutionRevalidationRequest is EvolutionRevalidationRequest
    assert evolution.EVOLUTION_REVALIDATION_REQUEST_POLICY == EVOLUTION_REVALIDATION_REQUEST_POLICY
