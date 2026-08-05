from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from naumi_agent.daemons.worker_registry import WorkerCapacityReservationState
from naumi_agent.evolution.revalidation_adversarial_cohorts import (
    EvolutionRevalidationAdversarialCohortExecutor,
    EvolutionRevalidationAdversarialCohortStore,
)
from naumi_agent.evolution.revalidation_adversarial_matrices import (
    EvolutionRevalidationAdversarialMatrixService,
    EvolutionRevalidationAdversarialMatrixStore,
)
from naumi_agent.evolution.revalidation_adversarial_samples import (
    EvolutionRevalidationAdversarialSampleExecutor,
)
from naumi_agent.evolution.revalidation_platform_completions import (
    EvolutionRevalidationPlatformCompletionError,
    EvolutionRevalidationPlatformCompletionService,
    EvolutionRevalidationPlatformCompletionStore,
)
from tests.unit.test_evolution_revalidation_platform_results import (
    CONTROL_KEY,
    _scenario,
)

T4 = "2026-07-19T00:00:04+00:00"


def _completion_service(tmp_path, result_store, result_service, *, now=T4):
    authorization_service = result_service.authorization_service
    sample_executor = EvolutionRevalidationAdversarialSampleExecutor(
        workspace_root=tmp_path,
        harness_store=result_service.harness_store,
        receipt_store=result_service.sample_store,
        permission_store=authorization_service.permission_store,
        run_grant_authority=authorization_service.run_grant_authority,
        profile_service=result_service.profile_service,
        sandbox_eval_kernel=object(),
        contract_service=authorization_service.contract_service,
        source_service=result_service.source_service,
        now=lambda: now,
    )
    cohort_store = EvolutionRevalidationAdversarialCohortStore(result_store.db_path)
    cohort_executor = EvolutionRevalidationAdversarialCohortExecutor(
        workspace_root=tmp_path,
        harness_store=result_service.harness_store,
        sample_store=result_service.sample_store,
        receipt_store=cohort_store,
        permission_store=authorization_service.permission_store,
        run_grant_authority=authorization_service.run_grant_authority,
        sample_executor=sample_executor,
        contract_service=authorization_service.contract_service,
        now=lambda: now,
    )
    worker_registry = authorization_service.claim_service.worker_registry
    matrix_service = EvolutionRevalidationAdversarialMatrixService(
        workspace_root=tmp_path,
        contract_service=authorization_service.contract_service,
        cohort_store=cohort_store,
        matrix_store=EvolutionRevalidationAdversarialMatrixStore(result_store.db_path),
        worker_registry=worker_registry,
    )
    store = EvolutionRevalidationPlatformCompletionStore(
        result_store.db_path,
        control_plane_key_provider=lambda: CONTROL_KEY,
    )
    return EvolutionRevalidationPlatformCompletionService(
        workspace_root=tmp_path,
        contract_service=authorization_service.contract_service,
        dispatch_store=authorization_service.dispatch_store,
        claim_store=authorization_service.claim_service.store,
        authorization_service=authorization_service,
        result_store=result_store,
        cohort_executor=cohort_executor,
        matrix_service=matrix_service,
        worker_registry=worker_registry,
        store=store,
        control_plane_key_provider=lambda: CONTROL_KEY,
        now=lambda: now,
    )


@pytest.mark.asyncio
async def test_complete_remote_prefix_atomically_closes_platform_lane(
    tmp_path: Path,
) -> None:
    (
        contract,
        _private_key,
        authority,
        manifest,
        _clock,
        _harness_store,
        result_store,
        result_service,
    ) = await _scenario(tmp_path, sample_count=7)
    result = await result_service.submit(manifest)
    service = _completion_service(tmp_path, result_store, result_service)
    cohort = await service.cohort_executor.execute(
        contract_id=contract.contract_id,
        platform=authority.platform,
        parent_receipt_id="remote-platform-result-prefix-complete",
    )
    pending = await service.matrix_service.inspect(
        contract_id=contract.contract_id,
        assessed_at=T4,
    )
    assert pending.lanes[0].status == "pending"
    assert pending.lanes[0].reason_codes == ("platform_completion_pending",)

    view = await service.finalize(
        contract_id=contract.contract_id,
        platform=authority.platform,
    )
    repeated = await service.finalize(
        contract_id=contract.contract_id,
        platform=authority.platform,
    )

    assert repeated == view
    assert view.receipt.result_receipt_id == (result.receipt_id,)
    assert view.receipt.sample_receipt_sha256 == result.pair_receipt_sha256
    assert view.receipt.cohort_receipt_id == cohort.receipt_id
    assert view.receipt.comparison_authority is False
    assert view.receipt.promotion_authority is False
    assert view.matrix_complete is True
    assert view.matrix.lanes[0].status == "completed"
    revocation = await result_service.authorization_service.store.get_revocation(
        authority.authorization_id
    )
    assert revocation is not None
    assert revocation.reason_code == "platform_result_completed"
    dispatch = await result_service.authorization_service.dispatch_store.get(
        contract.contract_id, authority.platform
    )
    assert dispatch is not None
    reservation = await service.worker_registry.get_capacity_reservation(
        dispatch.reservation_id,
        assessed_at=T4,
    )
    assert reservation is not None
    assert reservation.state is WorkerCapacityReservationState.RELEASED

    with sqlite3.connect(result_store.db_path) as db:
        row = db.execute(
            "SELECT receipt_json FROM evolution_revalidation_platform_completions "
            "WHERE receipt_id = ?",
            (view.receipt.receipt_id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row[0])
        payload["control_plane_attestation_sha256"] = "0" * 64
        db.execute(
            "UPDATE evolution_revalidation_platform_completions "
            "SET receipt_json = ? WHERE receipt_id = ?",
            (json.dumps(payload), view.receipt.receipt_id),
        )
    with pytest.raises(EvolutionRevalidationPlatformCompletionError) as tampered:
        await service.store.get(contract.contract_id, authority.platform)
    assert tampered.value.code == "platform_completion_attestation_invalid"


@pytest.mark.asyncio
async def test_incomplete_remote_prefix_keeps_authority_and_capacity_open(
    tmp_path: Path,
) -> None:
    (
        contract,
        _private_key,
        authority,
        manifest,
        _clock,
        _harness_store,
        result_store,
        result_service,
    ) = await _scenario(tmp_path)
    await result_service.submit(manifest)
    service = _completion_service(tmp_path, result_store, result_service)

    with pytest.raises(EvolutionRevalidationPlatformCompletionError) as incomplete:
        await service.finalize(
            contract_id=contract.contract_id,
            platform=authority.platform,
        )

    assert incomplete.value.code == "platform_completion_result_prefix_incomplete"
    assert await service.store.get(contract.contract_id, authority.platform) is None
    assert (
        await result_service.authorization_service.store.get_revocation(
            authority.authorization_id
        )
        is None
    )
    dispatch = await result_service.authorization_service.dispatch_store.get(
        contract.contract_id, authority.platform
    )
    assert dispatch is not None
    reservation = await service.worker_registry.get_capacity_reservation(
        dispatch.reservation_id,
        assessed_at=T4,
    )
    assert reservation is not None
    assert reservation.state is WorkerCapacityReservationState.ACTIVE


@pytest.mark.asyncio
async def test_concurrent_completion_uses_first_terminal_facts(
    tmp_path: Path,
) -> None:
    (
        contract,
        _private_key,
        authority,
        manifest,
        _clock,
        _harness_store,
        result_store,
        result_service,
    ) = await _scenario(tmp_path, sample_count=7)
    await result_service.submit(manifest)
    base = datetime.fromisoformat(T4)
    services = tuple(
        _completion_service(
            tmp_path,
            result_store,
            result_service,
            now=(base + timedelta(microseconds=index)).isoformat(),
        )
        for index in range(8)
    )

    views = await asyncio.gather(*(
        service.finalize(
            contract_id=contract.contract_id,
            platform=authority.platform,
        )
        for service in services
    ))

    assert len({item.receipt.receipt_sha256 for item in views}) == 1
    assert len({item.matrix.matrix_sha256 for item in views}) == 1
