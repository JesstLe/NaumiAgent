from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from naumi_agent.daemons.worker_registry import WorkerRegistryStore
from naumi_agent.evolution.failure_attribution import (
    EvolutionFailureAttributionReceipt,
    EvolutionFailureAttributionStore,
)
from naumi_agent.evolution.revalidation_final_evaluations import (
    EvolutionRevalidationFinalEvaluationReceipt,
)
from naumi_agent.evolution.revalidation_reapproval_authorities import (
    EvolutionRevalidationReapprovalAuthorityError,
    EvolutionRevalidationReapprovalAuthorityService,
    EvolutionRevalidationReapprovalAuthorityStore,
)
from naumi_agent.harness.eval_identity import capture_eval_platform_identity
from tests.unit.test_evolution_revalidation_adversarial_cohorts import (
    _cohort as _adversarial_cohort,
)
from tests.unit.test_evolution_revalidation_adversarial_matrices import _service
from tests.unit.test_evolution_revalidation_adversarial_samples import (
    _executor as _adversarial_sample,
)
from tests.unit.test_evolution_revalidation_final_evaluations import _final
from tests.unit.test_evolution_revalidation_interventional_cohorts import (
    _cohort_executor,
)
from tests.unit.test_evolution_revalidation_interventional_samples import (
    _executor_scenario,
)


def _digest(payload):
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()


def _eligible_attribution(receipt):
    payload = receipt.model_dump(
        mode="json", exclude={"attribution_id", "attribution_sha256"}
    )
    payload.update({
        "category": "none",
        "reason_code": "verified_improvement",
        "action": "continue_to_reflection",
        "candidate_fault": False,
        "retryable": False,
        "requires_rerun": False,
        "reflection_eligible": True,
    })
    digest = _digest(payload)
    return EvolutionFailureAttributionReceipt.model_validate({
        **payload,
        "attribution_id": f"evattr_{digest[:24]}",
        "attribution_sha256": digest,
    })


def _eligible_final(receipt, attribution):
    payload = receipt.model_dump(
        mode="json", exclude={"receipt_id", "receipt_sha256"}
    )
    payload["interventional"]["attribution"] = attribution.model_dump(mode="json")
    payload["attribution_ids"][0] = attribution.attribution_id
    payload["all_reflection_eligible"] = True
    payload["reapproval_eligible"] = True
    digest = _digest(payload)
    return EvolutionRevalidationFinalEvaluationReceipt.model_validate({
        **payload,
        "receipt_id": f"evrevalfinal_{digest[:24]}",
        "receipt_sha256": digest,
    })


@pytest.mark.asyncio
async def test_reapproval_blocks_negative_final_and_requires_fresh_signatures(
    tmp_path: Path,
):
    sample, contract, parent, harness_store, _kernel, _state = (
        await _executor_scenario(tmp_path)
    )
    interventional = _cohort_executor(sample, contract, harness_store)
    await interventional.execute(
        contract_id=contract.contract_id,
        parent_receipt_id=parent.receipt_id,
    )
    adversarial = _adversarial_sample(sample, harness_store)
    adversarial_cohort = _adversarial_cohort(adversarial, harness_store)
    await adversarial_cohort.execute(
        contract_id=contract.contract_id,
        platform=capture_eval_platform_identity().system,
        parent_receipt_id=parent.receipt_id,
    )
    matrix = _service(
        adversarial, WorkerRegistryStore(tmp_path / ".naumi" / "workers.db")
    )
    final_executor = _final(
        sample, harness_store, interventional, adversarial_cohort, matrix
    )
    final = await final_executor.execute(contract_id=contract.contract_id)
    service = EvolutionRevalidationReapprovalAuthorityService(
        workspace_root=tmp_path,
        contract_service=sample.contract_service,
        final_store=final_executor.receipt_store,
        store=EvolutionRevalidationReapprovalAuthorityStore(
            sample.receipt_store._db_path
        ),
    )

    with pytest.raises(EvolutionRevalidationReapprovalAuthorityError) as blocked:
        await service.issue(contract_id=contract.contract_id)
    assert blocked.value.code == "fresh_reapproval_evaluation_blocked"

    eligible_attribution = _eligible_attribution(final.interventional.attribution)
    db_path = sample.receipt_store._db_path
    with sqlite3.connect(db_path) as db:
        db.execute(
            "DELETE FROM evolution_failure_attributions WHERE comparison_id = ?",
            (eligible_attribution.comparison_id,),
        )
        db.execute(
            "DELETE FROM evolution_revalidation_final_evaluations WHERE contract_id = ?",
            (contract.contract_id,),
        )
    await EvolutionFailureAttributionStore(db_path).record(eligible_attribution)
    eligible_final = _eligible_final(final, eligible_attribution)
    await final_executor.receipt_store.record(eligible_final)

    authority = await service.issue(contract_id=contract.contract_id)
    repeated = await service.issue(contract_id=contract.contract_id)
    assert repeated == authority
    assert authority.reapproval_authorized
    assert not authority.prior_approval_reusable
    assert not authority.prior_signature_reusable
    assert authority.professional_role_resign_required
    assert authority.fresh_interaction_required
    assert authority.final_evaluation_id == eligible_final.receipt_id
    assert not authority.promotion_authority
