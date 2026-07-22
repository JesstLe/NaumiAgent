from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.evolution.decision_states import EvolutionDecisionStateValue
from naumi_agent.evolution.promotion_package_inputs import (
    EVOLUTION_PROMOTION_PACKAGE_INPUT_POLICY,
    EvolutionPromotionEvidenceKind,
    EvolutionPromotionEvidenceRef,
    EvolutionPromotionPackageInput,
    EvolutionPromotionPackageInputError,
    EvolutionPromotionPackageInputStore,
    _baseline,
    _migration_assessment,
    _patch_manifest,
    _rollback_plan,
    _sha256_payload,
)
from naumi_agent.evolution.reflection_memories import (
    EVOLUTION_REFLECTION_MEMORY_POLICY,
    EvolutionReflectionAction,
    EvolutionReflectionEvidenceKind,
    EvolutionReflectionEvidenceRef,
    EvolutionReflectionLessonKind,
    EvolutionReflectionMemory,
    EvolutionReflectionMemoryStore,
    EvolutionReflectionRevocationReason,
    EvolutionReflectionSignal,
)
from naumi_agent.evolution.reflection_memories import (
    _sha256_payload as _reflection_sha256,
)


def _accepted_reflection(workspace: Path) -> EvolutionReflectionMemory:
    decision_id = f"evdecision_{'d' * 24}"
    decision_sha = "d" * 64
    refs = (
        EvolutionReflectionEvidenceRef(
            kind=EvolutionReflectionEvidenceKind.DECISION_INPUT,
            authority_id=f"evdin_{'1' * 24}",
            authority_sha256="1" * 64,
        ),
        EvolutionReflectionEvidenceRef(
            kind=EvolutionReflectionEvidenceKind.MECHANICAL_GATE,
            authority_id=f"evgate_{'2' * 24}",
            authority_sha256="2" * 64,
        ),
        EvolutionReflectionEvidenceRef(
            kind=EvolutionReflectionEvidenceKind.INDEPENDENT_REVIEW,
            authority_id=f"evreview_{'3' * 24}",
            authority_sha256="3" * 64,
        ),
        EvolutionReflectionEvidenceRef(
            kind=EvolutionReflectionEvidenceKind.DECISION_STATE,
            authority_id=decision_id,
            authority_sha256=decision_sha,
        ),
        EvolutionReflectionEvidenceRef(
            kind=EvolutionReflectionEvidenceKind.COUNTERFACTUAL,
            authority_id=f"evcounter_{'4' * 24}",
            authority_sha256="4" * 64,
        ),
        EvolutionReflectionEvidenceRef(
            kind=EvolutionReflectionEvidenceKind.REWARD_HACKING,
            authority_id=f"evreward_{'5' * 24}",
            authority_sha256="5" * 64,
        ),
    )
    payload = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REFLECTION_MEMORY_POLICY,
        "workspace_root": str(workspace.resolve()),
        "decision_input_id": f"evdin_{'1' * 24}",
        "decision_state_id": decision_id,
        "decision_state_sha256": decision_sha,
        "resolution_id": None,
        "resolution_sha256": None,
        "candidate_id": f"evc_{'a' * 24}",
        "candidate_revision": 3,
        "risk_level": "medium",
        "decision_state": EvolutionDecisionStateValue.ACCEPTED_EXPERIMENT.value,
        "resolution_outcome": None,
        "lesson_kind": EvolutionReflectionLessonKind.VALIDATED_EXPERIMENT.value,
        "required_action": EvolutionReflectionAction.REVIEW_FOR_PROMOTION.value,
        "signals": [EvolutionReflectionSignal.ALL_STRUCTURED_EVIDENCE_CLEAR.value],
        "evidence_refs": [item.model_dump(mode="json") for item in refs],
        "changed_files": 1,
        "changed_lines": 5,
        "candidate_acceptance_decided": True,
        "candidate_accepted": True,
        "promotion_review_ready": True,
        "promotion_executed": False,
        "promotion_authority": False,
        "eligible_for_policy_learning": True,
        "vector_indexed": False,
        "automatic_recall_allowed": False,
        "system_prompt_injection_allowed": False,
        "contains_freeform_narrative": False,
        "contains_user_custom_text": False,
        "llm_generated": False,
        "revocable": True,
        "created_at": datetime(2026, 7, 22, 12, 0, tzinfo=UTC).isoformat(),
    }
    digest = _reflection_sha256(payload)
    return EvolutionReflectionMemory.model_validate(
        {
            **payload,
            "reflection_id": f"evreflection_{digest[:24]}",
            "reflection_sha256": digest,
        }
    )


def _package(workspace: Path, memory: EvolutionReflectionMemory) -> EvolutionPromotionPackageInput:
    mutation_file = SimpleNamespace(
        path="src/naumi_agent/state/schema.py",
        operation="modify",
        before_sha256="6" * 64,
        after_sha256="7" * 64,
        unified_diff_sha256="8" * 64,
        added_lines=4,
        deleted_lines=1,
        api_change="additive",
    )
    mutation_file.fact_sha256 = _sha256_payload(vars(mutation_file))
    mutation = SimpleNamespace(
        mutation_receipt_id=f"evmr_{'6' * 24}",
        receipt_sha256="6" * 64,
        files=(mutation_file,),
        files_sha256=_sha256_payload(
            [{**vars(mutation_file), "fact_sha256": mutation_file.fact_sha256}]
        ),
        total_added_lines=4,
        total_deleted_lines=1,
    )
    patch = _patch_manifest(mutation)
    request = SimpleNamespace(
        baseline_commit="a" * 40,
        source_snapshot_id=f"evs_{'7' * 24}",
        source_snapshot_sha256="7" * 64,
        baseline_tree_sha256="8" * 64,
        profile_sha256="9" * 64,
        experiment_config_sha256="a" * 64,
        toolset_sha256="b" * 64,
    )
    baseline = _baseline(False, request)
    migration = _migration_assessment(patch.files)
    rollback = _rollback_plan(patch.files, baseline, migration)
    fixed = [
        (EvolutionPromotionEvidenceKind.EXPERIMENT_CONTRACT, f"evx_{'1' * 24}", "1" * 64),
        (
            EvolutionPromotionEvidenceKind.SOURCE_SNAPSHOT,
            request.source_snapshot_id,
            request.source_snapshot_sha256,
        ),
        (
            EvolutionPromotionEvidenceKind.MUTATION_RECEIPT,
            mutation.mutation_receipt_id,
            mutation.receipt_sha256,
        ),
        (EvolutionPromotionEvidenceKind.VALIDATION_PLAN, f"evvplan_{'2' * 24}", "2" * 64),
        (EvolutionPromotionEvidenceKind.EVALUATION_AGGREGATION, f"evagg_{'3' * 24}", "3" * 64),
        (EvolutionPromotionEvidenceKind.FINAL_EVALUATION, f"evfinal_{'4' * 24}", "4" * 64),
    ]
    for digit in ("a", "b"):
        fixed.extend(
            [
                (
                    EvolutionPromotionEvidenceKind.EVALUATION_LANE,
                    f"evlane_{digit * 24}",
                    digit * 64,
                ),
                (
                    EvolutionPromotionEvidenceKind.RED_COMPLETION,
                    f"evvredrun_{digit * 24}",
                    digit * 64,
                ),
                (
                    EvolutionPromotionEvidenceKind.GREEN_COMPLETION,
                    f"evvgreenrun_{digit * 24}",
                    digit * 64,
                ),
                (EvolutionPromotionEvidenceKind.COMPARISON_RECEIPT, digit * 64, digit * 64),
                (
                    EvolutionPromotionEvidenceKind.FAILURE_ATTRIBUTION,
                    f"evattr_{digit * 24}",
                    digit * 64,
                ),
            ]
        )
    fixed.extend(
        [
            (EvolutionPromotionEvidenceKind.MECHANICAL_GATE, f"evgate_{'2' * 24}", "2" * 64),
            (EvolutionPromotionEvidenceKind.INDEPENDENT_REVIEW, f"evreview_{'3' * 24}", "3" * 64),
            (EvolutionPromotionEvidenceKind.COUNTERFACTUAL, f"evcounter_{'4' * 24}", "4" * 64),
            (EvolutionPromotionEvidenceKind.REWARD_HACKING, f"evreward_{'5' * 24}", "5" * 64),
            (
                EvolutionPromotionEvidenceKind.DECISION_STATE,
                memory.decision_state_id,
                memory.decision_state_sha256,
            ),
            (
                EvolutionPromotionEvidenceKind.REFLECTION_MEMORY,
                memory.reflection_id,
                memory.reflection_sha256,
            ),
        ]
    )
    refs = tuple(
        EvolutionPromotionEvidenceRef(
            order=index, kind=kind, authority_id=identity, authority_sha256=digest
        )
        for index, (kind, identity, digest) in enumerate(fixed, start=1)
    )
    payload = {
        "schema_version": 1,
        "policy_version": EVOLUTION_PROMOTION_PACKAGE_INPUT_POLICY,
        "workspace_root": str(workspace.resolve()),
        "candidate_id": memory.candidate_id,
        "candidate_revision": memory.candidate_revision,
        "candidate_sha256": "c" * 64,
        "risk_level": memory.risk_level,
        "reflection_id": memory.reflection_id,
        "reflection_sha256": memory.reflection_sha256,
        "decision_state_id": memory.decision_state_id,
        "decision_state_sha256": memory.decision_state_sha256,
        "experiment_contract_id": fixed[0][1],
        "experiment_contract_sha256": fixed[0][2],
        "mutation_receipt_id": mutation.mutation_receipt_id,
        "mutation_receipt_sha256": mutation.receipt_sha256,
        "final_evaluation_receipt_id": fixed[5][1],
        "final_evaluation_receipt_sha256": fixed[5][2],
        "required_platforms": ["macos"],
        "evaluation_lane_count": 2,
        "patch": patch.model_dump(mode="json"),
        "baseline": baseline.model_dump(mode="json"),
        "migration": migration.model_dump(mode="json"),
        "rollback": rollback.model_dump(mode="json"),
        "evidence_refs": [item.model_dump(mode="json") for item in refs],
        "source_reflection_active_at_issue": True,
        "stale_on_reflection_revocation": True,
        "package_input_complete": True,
        "promotion_package_ready": False,
        "approval_decided": False,
        "promotion_executed": False,
        "git_write_executed": False,
        "merge_executed": False,
        "push_executed": False,
        "publish_executed": False,
        "contains_source_code": False,
        "contains_freeform_narrative": False,
        "contains_user_custom_text": False,
        "llm_generated": False,
        "created_at": memory.created_at,
    }
    digest = _sha256_payload(payload)
    return EvolutionPromotionPackageInput.model_validate(
        {**payload, "input_id": f"evpromoin_{digest[:24]}", "input_sha256": digest}
    )


@pytest.mark.asyncio
async def test_package_input_becomes_ineligible_after_reflection_revocation(tmp_path: Path) -> None:
    db_path = tmp_path / "evolution.db"
    reflection_store = EvolutionReflectionMemoryStore(db_path)
    package_store = EvolutionPromotionPackageInputStore(db_path)
    memory = _accepted_reflection(tmp_path)
    await reflection_store.record(memory)

    issued = await package_store.record(_package(tmp_path, memory), reflection=memory)
    assert issued.reflection_active
    assert issued.promotion_review_eligible
    assert not issued.package_input.promotion_package_ready
    assert not issued.package_input.git_write_executed
    assert issued.package_input.migration.data_backup_required

    await reflection_store.revoke(
        memory=memory,
        reason=EvolutionReflectionRevocationReason.SUPERSEDED,
        revoked_at=datetime(2026, 7, 22, 12, 1, tzinfo=UTC).isoformat(),
    )
    stale = await package_store.get(issued.package_input.input_id)
    assert stale is not None
    assert not stale.reflection_active
    assert not stale.promotion_review_eligible
    with pytest.raises(EvolutionPromotionPackageInputError) as blocked:
        await package_store.record(issued.package_input, reflection=memory)
    assert blocked.value.code == "promotion_input_reflection_revoked"


def test_package_input_rejects_recomputed_execution_authority(tmp_path: Path) -> None:
    memory = _accepted_reflection(tmp_path)
    package = _package(tmp_path, memory)
    tampered = package.model_dump(mode="json")
    tampered["git_write_executed"] = True
    tampered["input_sha256"] = _sha256_payload(
        {key: value for key, value in tampered.items() if key not in {"input_id", "input_sha256"}}
    )
    tampered["input_id"] = f"evpromoin_{tampered['input_sha256'][:24]}"
    with pytest.raises(ValueError):
        EvolutionPromotionPackageInput.model_validate(tampered)


def test_package_input_public_exports_are_lazy() -> None:
    from naumi_agent import evolution

    assert evolution.EvolutionPromotionPackageInputStore is EvolutionPromotionPackageInputStore
