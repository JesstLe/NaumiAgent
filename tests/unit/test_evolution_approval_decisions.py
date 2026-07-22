from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from naumi_agent.cli.completer import COMMANDS_META
from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.evolution.approval_decisions import (
    EvolutionPromotionApprovalDecisionBuilder,
    EvolutionPromotionApprovalDecisionError,
    EvolutionPromotionApprovalDecisionService,
    EvolutionPromotionApprovalDecisionStatus,
    EvolutionPromotionApprovalDecisionStore,
    EvolutionPromotionApprovalRoleOutcome,
    render_evolution_promotion_approval_decision,
)
from naumi_agent.evolution.approval_principals import (
    EvolutionApprovalPrincipalService,
    EvolutionApprovalPrincipalStore,
)
from naumi_agent.evolution.approval_requests import (
    EvolutionPromotionApprovalRequestService,
    EvolutionPromotionApprovalResponseStore,
)
from naumi_agent.evolution.approval_requirements import (
    EVOLUTION_PROMOTION_APPROVAL_REQUIREMENT_POLICY_V1,
    EvolutionPromotionApprovalRequirementBuilder,
    EvolutionPromotionApprovalRequirementExecutor,
    EvolutionPromotionApprovalRequirementStore,
    EvolutionPromotionApprovalRole,
)
from naumi_agent.evolution.approval_signatures import (
    EvolutionApprovalSignatureService,
    EvolutionApprovalSignatureStore,
)
from naumi_agent.harness.interaction_runtime import DurableInteractionAuthorityClient
from naumi_agent.harness.store import HarnessStore
from naumi_agent.safety.permissions import (
    TOOL_PERMISSIONS,
    PermissionChecker,
    PermissionMode,
    PermissionRiskLevel,
)
from naumi_agent.tools.evolution_review import (
    EvolutionApprovalDecisionAuthorityTool,
    EvolutionPromotionApprovalDecisionTool,
)
from tests.unit.test_evolution_approval_principals import _AnsweringCallback
from tests.unit.test_evolution_approval_requests import _answering_callback
from tests.unit.test_evolution_approval_requirements import _authority_chain, _Clock
from tests.unit.test_evolution_promotion_packages import _git


@dataclass
class _Authorities:
    root: Path
    db_path: Path
    requirement_clock: _Clock
    requirement_executor: EvolutionPromotionApprovalRequirementExecutor
    requirement_view: object
    package_executor: object
    harness_store: HarnessStore
    interaction_authority: DurableInteractionAuthorityClient
    response_store: EvolutionPromotionApprovalResponseStore
    principal_service: EvolutionApprovalPrincipalService
    principal_callback: _AnsweringCallback
    signature_store: EvolutionApprovalSignatureStore
    signature_service: EvolutionApprovalSignatureService
    decision_store: EvolutionPromotionApprovalDecisionStore
    decision_service: EvolutionPromotionApprovalDecisionService
    responses: dict[str, object] = field(default_factory=dict)
    principals: dict[str, object] = field(default_factory=dict)
    private_keys: dict[str, Ed25519PrivateKey] = field(default_factory=dict)


def _keypair() -> tuple[Ed25519PrivateKey, str]:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private_key, base64.b64encode(public_key).decode("ascii")


async def _setup(root: Path) -> _Authorities:
    _reflection_store, package_executor, package_view = await _authority_chain(
        root,
        patch_path="src/naumi_agent/ui/theme.py",
        api_change="unchanged",
    )
    db_path = root / ".naumi" / "state.db"
    requirement_clock = _Clock(datetime(2026, 7, 23, 8, 0, tzinfo=UTC))
    requirement_executor = EvolutionPromotionApprovalRequirementExecutor(
        package_executor=package_executor,
        requirement_store=EvolutionPromotionApprovalRequirementStore(db_path),
        clock=requirement_clock,
    )
    requirement_view = await requirement_executor.execute(
        workspace_root=root,
        package_id=package_view.package.package_id,
    )
    assert tuple(step.role.value for step in requirement_view.requirement.steps) == (
        "user",
        "independent_reviewer",
        "release_manager",
    )
    harness_store = HarnessStore(db_path)
    authority = DurableInteractionAuthorityClient(
        store=harness_store,
        workspace_root=root,
        owner_id="approval-decision-test-owner",
    )
    response_store = EvolutionPromotionApprovalResponseStore(
        db_path,
        interaction_store=harness_store,
    )
    principal_callback = _AnsweringCallback(authority)
    principal_service = EvolutionApprovalPrincipalService(
        store=EvolutionApprovalPrincipalStore(
            db_path,
            interaction_store=harness_store,
        ),
        interaction_store=harness_store,
        request_user_input=principal_callback,
    )
    signature_store = EvolutionApprovalSignatureStore(db_path)
    signature_service = EvolutionApprovalSignatureService(
        response_store=response_store,
        requirement_executor=requirement_executor,
        principal_service=principal_service,
        signature_store=signature_store,
        clock=lambda: datetime(2026, 7, 23, 9, 10, tzinfo=UTC),
    )
    decision_store = EvolutionPromotionApprovalDecisionStore(db_path)
    decision_service = EvolutionPromotionApprovalDecisionService(
        requirement_executor=requirement_executor,
        package_executor=package_executor,
        response_store=response_store,
        signature_store=signature_store,
        signature_service=signature_service,
        decision_store=decision_store,
        clock=lambda: datetime(2026, 7, 23, 9, 20, tzinfo=UTC),
    )
    return _Authorities(
        root=root,
        db_path=db_path,
        requirement_clock=requirement_clock,
        requirement_executor=requirement_executor,
        requirement_view=requirement_view,
        package_executor=package_executor,
        harness_store=harness_store,
        interaction_authority=authority,
        response_store=response_store,
        principal_service=principal_service,
        principal_callback=principal_callback,
        signature_store=signature_store,
        signature_service=signature_service,
        decision_store=decision_store,
        decision_service=decision_service,
    )


async def _respond(authority: _Authorities, role: str, answer: str = "approve"):
    observations: list[tuple[str, str]] = []
    view = await EvolutionPromotionApprovalRequestService(
        requirement_executor=authority.requirement_executor,
        interaction_store=authority.harness_store,
        response_store=authority.response_store,
        request_user_input=_answering_callback(
            authority=authority.interaction_authority,
            now=authority.requirement_clock.value,
            answer_value=answer,
            observations=observations,
        ),
        clock=authority.requirement_clock,
    ).execute(
        workspace_root=authority.root,
        requirement_id=authority.requirement_view.requirement.requirement_id,
        role=role,
    )
    authority.responses[role] = view
    return view


async def _sign(authority: _Authorities, role: str):
    response = authority.responses[role].receipt
    private_key, public_key = _keypair()
    registered = await authority.principal_service.register(
        workspace_root=authority.root,
        principal_name=role.replace("_", "."),
        roles=(role,),
        public_key_base64=public_key,
    )
    assert registered.principal is not None
    principal = registered.principal.principal
    challenge = await authority.signature_service.prepare(
        workspace_root=authority.root,
        approval_response_id=response.receipt_id,
        principal_id=principal.principal_id,
    )
    signature = base64.b64encode(
        private_key.sign(challenge.challenge.payload.canonical_bytes())
    ).decode("ascii")
    receipt = await authority.signature_service.submit(
        workspace_root=authority.root,
        challenge_id=challenge.challenge.challenge_id,
        signature_base64=signature,
    )
    authority.principals[role] = registered
    authority.private_keys[role] = private_key
    return receipt


async def _fully_approve(authority: _Authorities) -> None:
    for role in ("user", "independent_reviewer", "release_manager"):
        await _respond(authority, role)
    await _sign(authority, "independent_reviewer")
    await _sign(authority, "release_manager")


@pytest.mark.asyncio
async def test_pending_to_approved_chain_is_durable_idempotent_and_non_executing(
    tmp_path: Path,
) -> None:
    authority = await _setup(tmp_path)
    requirement_id = authority.requirement_view.requirement.requirement_id

    empty = await authority.decision_service.execute(
        workspace_root=tmp_path,
        requirement_id=requirement_id,
    )
    assert empty.receipt.status is EvolutionPromotionApprovalDecisionStatus.PENDING
    assert empty.receipt.sequence == 1
    assert empty.receipt.missing_roles == (
        EvolutionPromotionApprovalRole.USER,
        EvolutionPromotionApprovalRole.INDEPENDENT_REVIEWER,
        EvolutionPromotionApprovalRole.RELEASE_MANAGER,
    )

    for role in ("user", "independent_reviewer", "release_manager"):
        await _respond(authority, role)
    unsigned = await authority.decision_service.execute(
        workspace_root=tmp_path,
        requirement_id=requirement_id,
    )
    assert unsigned.receipt.sequence == 2
    assert unsigned.receipt.status is EvolutionPromotionApprovalDecisionStatus.PENDING
    assert tuple(item.outcome for item in unsigned.receipt.role_decisions) == (
        EvolutionPromotionApprovalRoleOutcome.APPROVED,
        EvolutionPromotionApprovalRoleOutcome.SIGNATURE_MISSING,
        EvolutionPromotionApprovalRoleOutcome.SIGNATURE_MISSING,
    )

    await _sign(authority, "independent_reviewer")
    await _sign(authority, "release_manager")
    approved = await authority.decision_service.execute(
        workspace_root=tmp_path,
        requirement_id=requirement_id,
    )
    assert approved.source_current
    assert approved.receipt.sequence == 3
    assert approved.receipt.status is EvolutionPromotionApprovalDecisionStatus.APPROVED
    assert approved.receipt.previous_decision_id == unsigned.receipt.decision_id
    assert approved.receipt.previous_decision_sha256 == unsigned.receipt.decision_sha256
    assert approved.receipt.final_quorum_reached
    assert approved.receipt.rebase_revalidation_eligible
    assert not approved.receipt.promotion_authority
    assert not approved.receipt.promotion_executed
    assert not approved.receipt.git_write_executed
    assert not approved.receipt.merge_executed
    assert not approved.receipt.push_executed
    assert not approved.receipt.publish_executed
    assert "不会执行 rebase、Git、merge、push、publish" in (
        render_evolution_promotion_approval_decision(approved)
    )

    engine = SimpleNamespace(
        workspace_root=tmp_path,
        evolution_promotion_approval_decision_service=authority.decision_service,
    )
    write_tool = await EvolutionPromotionApprovalDecisionTool(engine).execute(
        requirement_id
    )
    read_tool = await EvolutionApprovalDecisionAuthorityTool(engine).execute(
        approved.receipt.decision_id
    )
    slash_execute = await execute_slash_command(
        engine,
        f"/evolution approval-decision {requirement_id}",
    )
    slash_inspect = await execute_slash_command(
        engine,
        f"/evolution approval-decision show {approved.receipt.decision_id}",
    )
    for output in (write_tool, read_tool, slash_execute, slash_inspect):
        assert approved.receipt.decision_id in output
        assert "Git/Merge/Push/Publish/Promotion" in output
        assert "false" in output

    repeated = await asyncio.gather(
        *(
            authority.decision_service.execute(
                workspace_root=tmp_path,
                requirement_id=requirement_id,
            )
            for _ in range(8)
        )
    )
    assert all(item == approved for item in repeated)
    with sqlite3.connect(authority.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM evolution_promotion_approval_decisions"
        ).fetchone() == (3,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("reject", EvolutionPromotionApprovalDecisionStatus.REJECTED),
        ("request_changes", EvolutionPromotionApprovalDecisionStatus.CHANGES_REQUESTED),
    ],
)
async def test_negative_professional_decisions_override_missing_roles(
    tmp_path: Path,
    answer: str,
    expected: EvolutionPromotionApprovalDecisionStatus,
) -> None:
    authority = await _setup(tmp_path)
    await _respond(authority, "independent_reviewer", answer)

    view = await authority.decision_service.execute(
        workspace_root=tmp_path,
        requirement_id=authority.requirement_view.requirement.requirement_id,
    )

    assert view.receipt.status is expected
    assert view.receipt.approval_decided
    assert not view.receipt.final_quorum_reached
    assert not view.current_rebase_revalidation_eligible
    assert view.receipt.missing_roles == (
        EvolutionPromotionApprovalRole.USER,
        EvolutionPromotionApprovalRole.RELEASE_MANAGER,
    )


@pytest.mark.asyncio
async def test_signature_rotation_and_target_movement_make_decision_stale(
    tmp_path: Path,
) -> None:
    authority = await _setup(tmp_path)
    await _fully_approve(authority)
    requirement_id = authority.requirement_view.requirement.requirement_id
    approved = await authority.decision_service.execute(
        workspace_root=tmp_path,
        requirement_id=requirement_id,
    )
    principal = authority.principals["independent_reviewer"].principal.principal
    _private_key, replacement_public_key = _keypair()
    await authority.principal_service.rotate_key(
        workspace_root=tmp_path,
        principal_id=principal.principal_id,
        public_key_base64=replacement_public_key,
    )

    stale_view = await authority.decision_service.inspect(
        workspace_root=tmp_path,
        decision_id=approved.receipt.decision_id,
    )
    assert not stale_view.source_current
    assert stale_view.current_status is EvolutionPromotionApprovalDecisionStatus.STALE
    assert not stale_view.current_rebase_revalidation_eligible
    stale_receipt = await authority.decision_service.execute(
        workspace_root=tmp_path,
        requirement_id=requirement_id,
    )
    assert stale_receipt.receipt.sequence == 2
    assert stale_receipt.receipt.status is EvolutionPromotionApprovalDecisionStatus.STALE
    assert any(
        item.outcome is EvolutionPromotionApprovalRoleOutcome.SIGNATURE_STALE
        for item in stale_receipt.receipt.role_decisions
    )

    (tmp_path / "target.txt").write_text("advanced\n", encoding="utf-8")
    _git(tmp_path, "add", "target.txt")
    _git(tmp_path, "commit", "-m", "advance decision target")
    moved = await authority.decision_service.execute(
        workspace_root=tmp_path,
        requirement_id=requirement_id,
    )
    assert moved.receipt.sequence == 3
    assert moved.receipt.status is EvolutionPromotionApprovalDecisionStatus.STALE
    assert not moved.receipt.target_current


@pytest.mark.asyncio
async def test_expired_requirement_fails_closed_after_full_quorum(tmp_path: Path) -> None:
    authority = await _setup(tmp_path)
    await _fully_approve(authority)
    expires_at = datetime.fromisoformat(
        authority.requirement_view.requirement.expires_at
    )
    authority.requirement_clock.value = expires_at

    expired = await authority.decision_service.execute(
        workspace_root=tmp_path,
        requirement_id=authority.requirement_view.requirement.requirement_id,
    )

    assert expired.receipt.requirement_expired
    assert expired.receipt.status is EvolutionPromotionApprovalDecisionStatus.STALE
    assert not expired.receipt.final_quorum_reached
    assert not expired.current_rebase_revalidation_eligible


@pytest.mark.asyncio
async def test_post_commit_authority_change_is_not_reported_as_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = await _setup(tmp_path)
    await _fully_approve(authority)
    original_record = authority.decision_store.record
    rotated = False

    async def record_then_rotate(*args, **kwargs):
        nonlocal rotated
        receipt = await original_record(*args, **kwargs)
        if not rotated:
            rotated = True
            principal = authority.principals[
                "independent_reviewer"
            ].principal.principal
            _private_key, replacement_public_key = _keypair()
            await authority.principal_service.rotate_key(
                workspace_root=tmp_path,
                principal_id=principal.principal_id,
                public_key_base64=replacement_public_key,
            )
        return receipt

    monkeypatch.setattr(authority.decision_store, "record", record_then_rotate)
    view = await authority.decision_service.execute(
        workspace_root=tmp_path,
        requirement_id=authority.requirement_view.requirement.requirement_id,
    )

    assert view.receipt.status is EvolutionPromotionApprovalDecisionStatus.APPROVED
    assert not view.source_current
    assert view.current_status is EvolutionPromotionApprovalDecisionStatus.STALE
    assert not view.current_rebase_revalidation_eligible


@pytest.mark.asyncio
async def test_legacy_unsigned_professional_approval_never_counts_as_identity(
    tmp_path: Path,
) -> None:
    authority = await _setup(tmp_path)
    package_view = await authority.package_executor.inspect(
        workspace_root=tmp_path,
        package_id=authority.requirement_view.requirement.package_id,
    )
    legacy = EvolutionPromotionApprovalRequirementBuilder(
        clock=authority.requirement_clock,
        policy_version=EVOLUTION_PROMOTION_APPROVAL_REQUIREMENT_POLICY_V1,
    ).build(package=package_view.package)
    legacy_store = EvolutionPromotionApprovalRequirementStore(authority.db_path)
    await legacy_store.record(legacy, package=package_view.package)
    authority.requirement_view = await authority.requirement_executor.inspect(
        workspace_root=tmp_path,
        requirement_id=legacy.requirement_id,
    )
    steps = {item.role.value: item for item in legacy.steps}
    assert not steps["independent_reviewer"].signature_required
    assert steps["release_manager"].signature_required
    for role in ("user", "independent_reviewer", "release_manager"):
        await _respond(authority, role)
    await _sign(authority, "release_manager")

    view = await authority.decision_service.execute(
        workspace_root=tmp_path,
        requirement_id=legacy.requirement_id,
    )

    independent = next(
        item
        for item in view.receipt.role_decisions
        if item.role is EvolutionPromotionApprovalRole.INDEPENDENT_REVIEWER
    )
    assert independent.outcome is EvolutionPromotionApprovalRoleOutcome.IDENTITY_UNVERIFIED
    assert not independent.counts_toward_quorum
    assert view.receipt.status is EvolutionPromotionApprovalDecisionStatus.PENDING
    assert EvolutionPromotionApprovalRole.INDEPENDENT_REVIEWER in (
        view.receipt.missing_roles
    )


@pytest.mark.asyncio
async def test_store_and_builder_fail_closed_on_tamper_and_unscoped_signatures(
    tmp_path: Path,
) -> None:
    authority = await _setup(tmp_path)
    await _fully_approve(authority)
    requirement_id = authority.requirement_view.requirement.requirement_id
    approved = await authority.decision_service.execute(
        workspace_root=tmp_path,
        requirement_id=requirement_id,
    )
    sources = await authority.decision_service._collect_sources(tmp_path, requirement_id)
    signatures = sources["signatures"]

    forged = approved.receipt.model_dump(mode="json")
    forged["technical_gate_decisions"][0]["state"] = "blocking"
    forged["blocking_gates"] = [forged["technical_gate_decisions"][0]["gate"]]
    forged["status"] = "pending"
    forged["approval_decided"] = False
    forged["final_quorum_reached"] = False
    forged["rebase_revalidation_eligible"] = False
    source_keys = (
        "workspace_root",
        "requirement_id",
        "requirement_sha256",
        "requirement_policy_version",
        "package_id",
        "package_sha256",
        "target_branch",
        "target_head",
        "target_tree",
        "package_current",
        "input_active",
        "reflection_active",
        "target_current",
        "requirement_expired",
        "role_decisions",
        "technical_gate_decisions",
        "required_approvals",
        "approvals_collected",
        "required_signatures",
        "signatures_collected",
        "missing_roles",
        "blocking_gates",
        "status",
    )
    forged["source_set_sha256"] = hashlib.sha256(
        json.dumps(
            {key: forged[key] for key in source_keys},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    artifact = {
        key: value
        for key, value in forged.items()
        if key not in {"decision_id", "decision_sha256"}
    }
    digest = hashlib.sha256(
        json.dumps(
            artifact,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    forged["decision_id"] = f"evapprovaldecision_{digest[:24]}"
    forged["decision_sha256"] = digest
    with pytest.raises(ValueError, match="technical gate 投影"):
        type(approved.receipt).model_validate(forged)

    with pytest.raises(EvolutionPromotionApprovalDecisionError) as duplicate:
        EvolutionPromotionApprovalDecisionBuilder().build(
            **{**sources, "signatures": (*signatures, signatures[0])},
            sequence=2,
            previous_decision_id=approved.receipt.decision_id,
            previous_decision_sha256=approved.receipt.decision_sha256,
            decided_at=datetime(2026, 7, 23, 9, 30, tzinfo=UTC),
        )
    assert duplicate.value.code == "approval_decision_signature_set_invalid"

    with sqlite3.connect(authority.db_path) as db:
        db.execute(
            "UPDATE evolution_promotion_approval_decisions "
            "SET status = 'pending' WHERE decision_id = ?",
            (approved.receipt.decision_id,),
        )
        db.commit()
    with pytest.raises(EvolutionPromotionApprovalDecisionError) as corrupted:
        await authority.decision_store.get(approved.receipt.decision_id)
    assert corrupted.value.code == "approval_decision_store_corrupt"


def test_service_rejects_invalid_dependencies(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="Requirement Executor"):
        EvolutionPromotionApprovalDecisionService(
            requirement_executor=object(),  # type: ignore[arg-type]
            package_executor=object(),  # type: ignore[arg-type]
            response_store=object(),  # type: ignore[arg-type]
            signature_store=object(),  # type: ignore[arg-type]
            signature_service=object(),  # type: ignore[arg-type]
            decision_store=EvolutionPromotionApprovalDecisionStore(tmp_path / "state.db"),
        )


def test_permission_metadata_and_lazy_exports_are_exact() -> None:
    rule = TOOL_PERMISSIONS["evolution_promotion_approval_decision"]
    assert rule.tool_family == "evolution_promotion_artifact"
    assert rule.max_calls_per_session == 50
    assert rule.risk_level is PermissionRiskLevel.MEDIUM
    assert not rule.requires_confirmation
    for mode in (
        PermissionMode.BYPASS,
        PermissionMode.PERMISSIVE,
        PermissionMode.MODERATE,
        PermissionMode.STRICT,
    ):
        assert PermissionChecker(mode).check(
            "evolution_promotion_approval_decision",
            {},
        ).allowed
    assert not PermissionChecker(PermissionMode.LOCKDOWN).check(
        "evolution_promotion_approval_decision",
        {},
    ).allowed
    evolution_command = next(item for item in COMMANDS_META if item.name == "/evolution")
    assert "approval-decision" in evolution_command.arg_hint
    assert EvolutionPromotionApprovalDecisionTool(SimpleNamespace()).metadata.read_only is False
    assert EvolutionApprovalDecisionAuthorityTool(SimpleNamespace()).metadata.read_only is True

    from naumi_agent import evolution

    assert (
        evolution.EvolutionPromotionApprovalDecisionStore
        is EvolutionPromotionApprovalDecisionStore
    )
