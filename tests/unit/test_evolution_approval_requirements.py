from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from naumi_agent.evolution.approval_requirements import (
    EvolutionPromotionApprovalRequirement,
    EvolutionPromotionApprovalRequirementBuilder,
    EvolutionPromotionApprovalRequirementError,
    EvolutionPromotionApprovalRequirementExecutor,
    EvolutionPromotionApprovalRequirementStore,
    EvolutionPromotionApprovalRole,
    EvolutionPromotionTechnicalGate,
    _sha256_payload,
)
from naumi_agent.evolution.promotion_package_inputs import (
    EvolutionPromotionPackageInputStore,
)
from naumi_agent.evolution.promotion_packages import (
    EvolutionPromotionPackageBuilder,
    EvolutionPromotionPackageExecutor,
    EvolutionPromotionPackageStore,
    EvolutionPromotionTargetProbe,
)
from naumi_agent.evolution.reflection_memories import (
    EvolutionReflectionMemoryStore,
    EvolutionReflectionRevocationReason,
)
from tests.unit.test_evolution_promotion_package_inputs import (
    _accepted_reflection,
    _package,
)
from tests.unit.test_evolution_promotion_packages import _git, _repo


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


async def _authority_chain(
    root: Path,
    *,
    risk_level: str = "medium",
    patch_path: str = "src/naumi_agent/state/schema.py",
    api_change: str = "additive",
) -> tuple[
    EvolutionReflectionMemoryStore,
    EvolutionPromotionPackageExecutor,
    object,
]:
    baseline = _repo(root)
    db_path = root / ".naumi" / "state.db"
    reflection_store = EvolutionReflectionMemoryStore(db_path)
    input_store = EvolutionPromotionPackageInputStore(db_path)
    package_store = EvolutionPromotionPackageStore(db_path)
    memory = _accepted_reflection(root, risk_level=risk_level)
    await reflection_store.record(memory)
    source = await input_store.record(
        _package(
            root,
            memory,
            baseline_commit=baseline,
            patch_path=patch_path,
            api_change=api_change,
        ),
        reflection=memory,
    )
    package_executor = EvolutionPromotionPackageExecutor(
        input_store=input_store,
        package_store=package_store,
    )
    package = await package_executor.execute(
        workspace_root=root,
        promotion_input_id=source.package_input.input_id,
    )
    return reflection_store, package_executor, package


@pytest.mark.asyncio
async def test_requirement_is_singleflight_and_stales_with_package(
    tmp_path: Path,
) -> None:
    reflection_store, package_executor, package_view = await _authority_chain(tmp_path)
    db_path = tmp_path / ".naumi" / "state.db"
    clock = _Clock(datetime(2026, 7, 23, 8, 0, tzinfo=UTC))
    store = EvolutionPromotionApprovalRequirementStore(db_path)
    executor = EvolutionPromotionApprovalRequirementExecutor(
        package_executor=package_executor,
        requirement_store=store,
        clock=clock,
    )

    views = await asyncio.gather(
        *(
            executor.execute(
                workspace_root=tmp_path,
                package_id=package_view.package.package_id,
            )
            for _ in range(8)
        )
    )
    first = views[0]
    requirement = first.requirement
    assert len({item.requirement.requirement_id for item in views}) == 1
    assert first.approval_request_eligible
    assert requirement.required_roles == (
        EvolutionPromotionApprovalRole.USER,
        EvolutionPromotionApprovalRole.INDEPENDENT_REVIEWER,
        EvolutionPromotionApprovalRole.DATA_OWNER,
        EvolutionPromotionApprovalRole.RELEASE_MANAGER,
    )
    assert requirement.signature_required_roles == (
        EvolutionPromotionApprovalRole.DATA_OWNER,
        EvolutionPromotionApprovalRole.RELEASE_MANAGER,
    )
    assert requirement.minimum_approvals == 4
    assert requirement.minimum_signatures == 2
    assert requirement.protected_scope_human_gate
    assert not requirement.automatic_approval_allowed
    assert requirement.validity_seconds == 24 * 60 * 60
    assert requirement.blocking_gates == ()
    assert EvolutionPromotionTechnicalGate.MIGRATION_REVIEW_REQUIRED in (
        requirement.technical_gates
    )
    assert EvolutionPromotionTechnicalGate.DATA_BACKUP_REQUIRED in (requirement.technical_gates)
    assert not requirement.approval_decided
    assert not requirement.signatures_collected
    assert not requirement.interaction_created
    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM evolution_promotion_approval_requirements"
        ).fetchone() == (1,)

    later = EvolutionPromotionApprovalRequirementBuilder(
        clock=lambda: clock.value + timedelta(hours=1)
    ).build(package=package_view.package)
    restored = await store.record(later, package=package_view.package)
    assert restored == requirement

    downgraded_payload = requirement.model_dump(mode="json")
    downgraded_payload["steps"] = [
        step
        for step in downgraded_payload["steps"]
        if step["role"] != EvolutionPromotionApprovalRole.INDEPENDENT_REVIEWER.value
    ]
    for index, step in enumerate(downgraded_payload["steps"], start=1):
        step["order"] = index
    downgraded_payload["required_roles"] = [step["role"] for step in downgraded_payload["steps"]]
    downgraded_payload["minimum_approvals"] = len(downgraded_payload["steps"])
    policy_payload = {
        key: value
        for key, value in downgraded_payload.items()
        if key
        not in {
            "requirement_id",
            "requirement_sha256",
            "policy_projection_sha256",
            "issued_at",
            "expires_at",
        }
    }
    downgraded_payload["policy_projection_sha256"] = _sha256_payload(policy_payload)
    artifact_payload = {
        key: value
        for key, value in downgraded_payload.items()
        if key not in {"requirement_id", "requirement_sha256"}
    }
    downgraded_digest = _sha256_payload(artifact_payload)
    downgraded_payload["requirement_id"] = f"evapprovalreq_{downgraded_digest[:24]}"
    downgraded_payload["requirement_sha256"] = downgraded_digest
    downgraded = EvolutionPromotionApprovalRequirement.model_validate(downgraded_payload)
    with pytest.raises(EvolutionPromotionApprovalRequirementError) as policy_block:
        await store.record(downgraded, package=package_view.package)
    assert policy_block.value.code == "approval_requirement_policy_mismatch"

    clock.value = datetime.fromisoformat(requirement.expires_at)
    expired = await executor.inspect(
        workspace_root=tmp_path,
        requirement_id=requirement.requirement_id,
    )
    assert expired.expired
    assert not expired.approval_request_eligible

    clock.value -= timedelta(hours=1)
    (tmp_path / "target.txt").write_text("advanced\n", encoding="utf-8")
    _git(tmp_path, "add", "target.txt")
    _git(tmp_path, "commit", "-m", "advance approval target")
    stale = await executor.inspect(
        workspace_root=tmp_path,
        requirement_id=requirement.requirement_id,
    )
    assert not stale.target_current
    assert not stale.approval_request_eligible

    successor_package = await package_executor.execute(
        workspace_root=tmp_path,
        promotion_input_id=package_view.package.promotion_input_id,
    )
    successor = await executor.execute(
        workspace_root=tmp_path,
        package_id=successor_package.package.package_id,
    )
    assert successor.requirement.blocking_gates == (
        EvolutionPromotionTechnicalGate.REBASE_REQUIRED,
        EvolutionPromotionTechnicalGate.REVALIDATION_REQUIRED,
    )
    assert not successor.requirement.approval_request_ready
    assert not successor.approval_request_eligible
    assert successor.requirement.validity_seconds == 6 * 60 * 60

    memory_view = await reflection_store.get(package_view.package.reflection_id)
    assert memory_view is not None
    await reflection_store.revoke(
        memory=memory_view.memory,
        reason=EvolutionReflectionRevocationReason.SUPERSEDED,
        revoked_at=datetime(2026, 7, 23, 9, 0, tzinfo=UTC).isoformat(),
    )
    revoked = await executor.inspect(
        workspace_root=tmp_path,
        requirement_id=successor.requirement.requirement_id,
    )
    assert not revoked.package_active
    assert not revoked.approval_request_eligible

    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE evolution_promotion_approval_requirements "
            "SET target_head = ? WHERE requirement_id = ?",
            ("0" * 40, requirement.requirement_id),
        )
        db.commit()
    with pytest.raises(EvolutionPromotionApprovalRequirementError) as corrupt:
        await store.get(requirement.requirement_id)
    assert corrupt.value.code == "approval_requirement_store_corrupt"


@pytest.mark.asyncio
async def test_critical_requirement_adds_security_and_signed_review(
    tmp_path: Path,
) -> None:
    _reflection_store, package_executor, package_view = await _authority_chain(
        tmp_path,
        risk_level="critical",
    )
    clock = _Clock(datetime(2026, 7, 23, 10, 0, tzinfo=UTC))
    executor = EvolutionPromotionApprovalRequirementExecutor(
        package_executor=package_executor,
        requirement_store=EvolutionPromotionApprovalRequirementStore(
            tmp_path / ".naumi" / "state.db"
        ),
        clock=clock,
    )
    view = await executor.execute(
        workspace_root=tmp_path,
        package_id=package_view.package.package_id,
    )

    assert view.requirement.required_roles == (
        EvolutionPromotionApprovalRole.USER,
        EvolutionPromotionApprovalRole.INDEPENDENT_REVIEWER,
        EvolutionPromotionApprovalRole.SECURITY_REVIEWER,
        EvolutionPromotionApprovalRole.DATA_OWNER,
        EvolutionPromotionApprovalRole.RELEASE_MANAGER,
    )
    assert view.requirement.signature_required_roles == (
        EvolutionPromotionApprovalRole.INDEPENDENT_REVIEWER,
        EvolutionPromotionApprovalRole.SECURITY_REVIEWER,
        EvolutionPromotionApprovalRole.DATA_OWNER,
        EvolutionPromotionApprovalRole.RELEASE_MANAGER,
    )
    assert view.requirement.validity_seconds == 6 * 60 * 60


@pytest.mark.asyncio
async def test_authorization_scope_requires_human_security_signature(
    tmp_path: Path,
) -> None:
    _reflection_store, package_executor, package_view = await _authority_chain(
        tmp_path,
        risk_level="low",
        patch_path="src/naumi_agent/safety/permissions.py",
        api_change="unchanged",
    )
    executor = EvolutionPromotionApprovalRequirementExecutor(
        package_executor=package_executor,
        requirement_store=EvolutionPromotionApprovalRequirementStore(
            tmp_path / ".naumi" / "state.db"
        ),
        clock=lambda: datetime(2026, 7, 23, 11, 0, tzinfo=UTC),
    )
    view = await executor.execute(
        workspace_root=tmp_path,
        package_id=package_view.package.package_id,
    )

    assert view.requirement.required_roles == (
        EvolutionPromotionApprovalRole.USER,
        EvolutionPromotionApprovalRole.INDEPENDENT_REVIEWER,
        EvolutionPromotionApprovalRole.SECURITY_REVIEWER,
        EvolutionPromotionApprovalRole.RELEASE_MANAGER,
    )
    assert view.requirement.signature_required_roles == (
        EvolutionPromotionApprovalRole.SECURITY_REVIEWER,
        EvolutionPromotionApprovalRole.RELEASE_MANAGER,
    )
    assert view.requirement.protected_scope_human_gate
    assert view.requirement.validity_seconds == 7 * 24 * 60 * 60


def test_requirement_rejects_recomputed_approval_or_execution(tmp_path: Path) -> None:
    baseline = _repo(tmp_path)
    memory = _accepted_reflection(tmp_path)
    source = _package(tmp_path, memory, baseline_commit=baseline)
    package = EvolutionPromotionPackageBuilder().build(
        package_input=source,
        target=EvolutionPromotionTargetProbe().capture(
            workspace_root=tmp_path,
            target_branch="main",
            baseline_commit=baseline,
        ),
    )
    requirement = EvolutionPromotionApprovalRequirementBuilder(
        clock=lambda: datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    ).build(package=package)

    for field in ("approval_decided", "signatures_collected", "git_write_executed"):
        tampered = requirement.model_dump(mode="json")
        tampered[field] = True
        payload = {
            key: value
            for key, value in tampered.items()
            if key not in {"requirement_id", "requirement_sha256"}
        }
        digest = _sha256_payload(payload)
        tampered["requirement_id"] = f"evapprovalreq_{digest[:24]}"
        tampered["requirement_sha256"] = digest
        with pytest.raises(ValueError):
            EvolutionPromotionApprovalRequirement.model_validate(tampered)


def test_approval_requirement_public_exports_are_lazy() -> None:
    from naumi_agent import evolution

    assert (
        evolution.EvolutionPromotionApprovalRequirementStore
        is EvolutionPromotionApprovalRequirementStore
    )
