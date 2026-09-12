from __future__ import annotations

import asyncio
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from naumi_agent.evolution.promotion_package_inputs import (
    EvolutionPromotionPackageInputStore,
)
from naumi_agent.evolution.promotion_packages import (
    EvolutionPromotionApprovalSignal,
    EvolutionPromotionPackage,
    EvolutionPromotionPackageBuilder,
    EvolutionPromotionPackageError,
    EvolutionPromotionPackageExecutor,
    EvolutionPromotionPackageStore,
    EvolutionPromotionProtectedScope,
    EvolutionPromotionTargetProbe,
    EvolutionPromotionTargetRelation,
    EvolutionPromotionTargetSnapshot,
    _sha256_payload,
)
from naumi_agent.evolution.reflection_memories import (
    EvolutionReflectionMemoryStore,
    EvolutionReflectionRevocationReason,
)
from tests.unit.test_evolution_promotion_package_inputs import (
    _accepted_reflection,
    _package,
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repo(root: Path) -> str:
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Naumi Test")
    _git(root, "config", "user.email", "naumi@example.invalid")
    _git(root, "config", "core.autocrlf", "false")
    source = root / "src" / "naumi_agent" / "state"
    source.mkdir(parents=True)
    (source / "schema.py").write_text("VERSION = 1\n", encoding="utf-8")
    _git(root, "add", "src/naumi_agent/state/schema.py")
    _git(root, "commit", "-m", "baseline")
    return _git(root, "rev-parse", "HEAD")


@pytest.mark.asyncio
async def test_package_binds_real_target_and_stales_on_move_or_revocation(
    tmp_path: Path,
) -> None:
    baseline = _repo(tmp_path)
    db_path = tmp_path / ".naumi" / "state.db"
    reflection_store = EvolutionReflectionMemoryStore(db_path)
    input_store = EvolutionPromotionPackageInputStore(db_path)
    package_store = EvolutionPromotionPackageStore(db_path)
    memory = _accepted_reflection(tmp_path)
    await reflection_store.record(memory)
    source_view = await input_store.record(
        _package(tmp_path, memory, baseline_commit=baseline),
        reflection=memory,
    )
    executor = EvolutionPromotionPackageExecutor(
        input_store=input_store,
        package_store=package_store,
    )

    real_target = EvolutionPromotionTargetProbe().capture(
        workspace_root=tmp_path,
        target_branch="main",
        baseline_commit=baseline,
    )
    forged_target_payload = real_target.model_dump(
        mode="json",
        exclude={"snapshot_sha256"},
    )
    forged_target_payload.update(
        {
            "target_head": "f" * 40,
            "target_tree": "e" * 40,
            "relation_to_baseline": EvolutionPromotionTargetRelation.ADVANCED.value,
            "rebase_required": True,
            "revalidation_required": True,
        }
    )
    forged_target = EvolutionPromotionTargetSnapshot.model_validate(
        {
            **forged_target_payload,
            "snapshot_sha256": _sha256_payload(forged_target_payload),
        }
    )
    forged_package = EvolutionPromotionPackageBuilder().build(
        package_input=source_view.package_input,
        target=forged_target,
    )
    with pytest.raises(EvolutionPromotionPackageError) as forged:
        await package_store.record(
            forged_package,
            package_input=source_view.package_input,
        )
    assert forged.value.code == "promotion_package_target_changed"

    views = await asyncio.gather(
        *(
            executor.execute(
                workspace_root=tmp_path,
                promotion_input_id=source_view.package_input.input_id,
            )
            for _ in range(8)
        )
    )
    first = views[0]
    assert len({item.package.package_id for item in views}) == 1
    assert first.package_review_eligible
    assert first.package.target.relation_to_baseline is EvolutionPromotionTargetRelation.SAME
    assert first.package.target.target_head == baseline
    assert not first.package.target.git_write_executed
    assert not first.package.signature_collected
    assert not first.package.approval_policy_evaluated
    assert first.package.approval_input.signals == (
        EvolutionPromotionApprovalSignal.DATA_BACKUP,
        EvolutionPromotionApprovalSignal.MIGRATION_REVIEW,
        EvolutionPromotionApprovalSignal.PROTECTED_SCOPE,
        EvolutionPromotionApprovalSignal.PROTECTED_TARGET,
    )
    assert {item.scope for item in first.package.approval_input.protected_paths} == {
        EvolutionPromotionProtectedScope.PERSISTENCE
    }
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM evolution_promotion_packages").fetchone() == (1,)

    (tmp_path / "README.md").write_text("target advanced\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-m", "advance target")
    advanced_head = _git(tmp_path, "rev-parse", "HEAD")

    stale = await executor.inspect(
        workspace_root=tmp_path,
        package_id=first.package.package_id,
    )
    assert stale.target_available
    assert not stale.target_current
    assert not stale.package_review_eligible
    assert stale.current_target_head == advanced_head

    successor = await executor.execute(
        workspace_root=tmp_path,
        promotion_input_id=source_view.package_input.input_id,
    )
    assert successor.package_review_eligible
    assert successor.package.package_id != first.package.package_id
    assert successor.package.target.relation_to_baseline is (
        EvolutionPromotionTargetRelation.ADVANCED
    )
    assert successor.package.target.rebase_required
    assert successor.package.target.revalidation_required
    assert EvolutionPromotionApprovalSignal.TARGET_ADVANCED in (
        successor.package.approval_input.signals
    )

    await reflection_store.revoke(
        memory=memory,
        reason=EvolutionReflectionRevocationReason.SUPERSEDED,
        revoked_at=datetime(2026, 7, 22, 13, 0, tzinfo=UTC).isoformat(),
    )
    revoked = await executor.inspect(
        workspace_root=tmp_path,
        package_id=successor.package.package_id,
    )
    assert not revoked.promotion_input_active
    assert not revoked.reflection_active
    assert not revoked.package_review_eligible
    with pytest.raises(EvolutionPromotionPackageError) as blocked:
        await executor.execute(
            workspace_root=tmp_path,
            promotion_input_id=source_view.package_input.input_id,
        )
    assert blocked.value.code == "promotion_package_input_ineligible"

    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE evolution_promotion_packages SET target_head = ? WHERE package_id = ?",
            ("0" * 40, successor.package.package_id),
        )
        db.commit()
    with pytest.raises(EvolutionPromotionPackageError) as corrupt:
        await package_store.get(successor.package.package_id)
    assert corrupt.value.code == "promotion_package_store_corrupt"


def test_package_rejects_forged_signature_or_git_execution(tmp_path: Path) -> None:
    baseline = _repo(tmp_path)
    memory = _accepted_reflection(tmp_path)
    source = _package(tmp_path, memory, baseline_commit=baseline)
    target = EvolutionPromotionTargetProbe().capture(
        workspace_root=tmp_path,
        target_branch="main",
        baseline_commit=baseline,
    )
    package = EvolutionPromotionPackageBuilder().build(
        package_input=source,
        target=target,
    )

    for field in ("signature_collected", "git_write_executed", "approval_decided"):
        tampered = package.model_dump(mode="json")
        tampered[field] = True
        payload = {
            key: value
            for key, value in tampered.items()
            if key not in {"package_id", "package_sha256"}
        }
        digest = _sha256_payload(payload)
        tampered["package_id"] = f"evpromopkg_{digest[:24]}"
        tampered["package_sha256"] = digest
        with pytest.raises(ValueError):
            EvolutionPromotionPackage.model_validate(tampered)

    with pytest.raises(EvolutionPromotionPackageError) as invalid_branch:
        EvolutionPromotionTargetProbe().capture(
            workspace_root=tmp_path,
            target_branch="main;touch-owned",
            baseline_commit=baseline,
        )
    assert invalid_branch.value.code == "promotion_package_target_branch_invalid"


def test_promotion_package_public_exports_are_lazy() -> None:
    from naumi_agent import evolution

    assert evolution.EvolutionPromotionPackageStore is EvolutionPromotionPackageStore
