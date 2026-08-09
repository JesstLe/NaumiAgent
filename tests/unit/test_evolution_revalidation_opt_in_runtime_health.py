from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from naumi_agent.evolution.revalidation_opt_in_runtime_health import (
    EvolutionRevalidationOptInRuntimeHealthError,
    EvolutionRevalidationOptInRuntimeHealthService,
    EvolutionRevalidationOptInRuntimeHealthStore,
)
from naumi_agent.validation.executor import ValidationExecutor
from tests.unit.test_evolution_revalidation_opt_in_deployments import (
    _deployment_service,
)


class _CapturingExecutor(ValidationExecutor):
    def __init__(self) -> None:
        super().__init__(output_limit_bytes=64 * 1024)
        self.environments: list[dict[str, str]] = []

    async def run(self, **kwargs):
        self.environments.append(dict(kwargs["env"]))
        return await super().run(**kwargs)


def _runtime_backend(*, valid_health: bool, delay_seconds: float = 0) -> bytes:
    if valid_health:
        health = f"""
import time
time.sleep({delay_seconds!r})
from pathlib import Path
from naumi_agent.release.runtime_health import inspect_runtime_health
from naumi_agent.release.slots import ReleaseSlotStore
report = inspect_runtime_health(
    ReleaseSlotStore(os.environ["NAUMI_INSTALL_ROOT"]),
    environment=os.environ,
    runtime_path=Path(__file__),
)
print(report.model_dump_json())
"""
    else:
        health = "print('not-json')\n"
    return (
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import sys\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('naumi 1.0.0')\n"
        "elif sys.argv[1:] == ['--runtime-health-check']:\n"
        + "\n".join(f"    {line}" for line in health.strip().splitlines())
        + "\nelse:\n"
        "    raise SystemExit(64)\n"
    ).encode()


async def _health_service(
    root: Path,
    *,
    backend: bytes,
    executor: _CapturingExecutor,
):
    build, completion, _admission, slots, _original, deployment_store = (
        await _deployment_service(root, candidate_backend_content=backend)
    )
    deployment_service = build()
    deployment = await deployment_service.deploy(
        completion_id=completion.completion_id
    )
    store = EvolutionRevalidationOptInRuntimeHealthStore(
        deployment_store.db_path,
        deployment_store=deployment_store,
        release_slot_store=slots,
    )
    service = EvolutionRevalidationOptInRuntimeHealthService(
        workspace_root=root,
        deployment_service=deployment_service,
        store=store,
        executor=executor,
        timeout_seconds=1,
        lease_seconds=10,
    )
    return service, store, deployment, completion, slots


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实执行夹具使用 POSIX shebang")
async def test_concurrent_health_observation_reclaims_lease_and_fences_old_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _CapturingExecutor()
    service, store, deployment, completion, slots = await _health_service(
        tmp_path,
        backend=_runtime_backend(valid_health=True, delay_seconds=0.15),
        executor=executor,
    )
    old_claim, terminal = await store.claim(
        deployment=deployment.receipt,
        now=datetime.now(UTC) - timedelta(seconds=2),
        lease_seconds=0.1,
    )
    assert old_claim is not None and terminal is None
    monkeypatch.setenv("NAUMI_TEST_SECRET", "must-not-reach-runtime")

    views = await asyncio.gather(
        *(
            service.observe(completion_id=completion.completion_id)
            for _ in range(8)
        )
    )
    view = views[0]

    assert all(item == view for item in views)
    assert len(executor.environments) == 1
    assert "NAUMI_TEST_SECRET" not in executor.environments[0]
    assert view.receipt.outcome == "healthy"
    assert view.receipt.runtime_health_authority
    assert view.runtime_health_authority
    assert view.receipt.health_report is not None
    assert view.receipt.health_report.user_session_started is False
    assert view.receipt.opt_in_stage_completion_authority is False
    assert view.receipt.percentage_rollout_authority is False
    assert view.receipt.stable_rollout_authority is False
    assert (
        slots.get_launch_resolution(view.receipt.launch_resolution.resolution_id)
        == view.receipt.launch_resolution
    )

    with pytest.raises(EvolutionRevalidationOptInRuntimeHealthError) as fenced:
        await store.finish(
            claim=old_claim,
            receipt=view.receipt,
            now=datetime.now(UTC),
        )
    assert fenced.value.code == "opt_in_runtime_health_fenced"

    slots.rollback()
    historical = await service.inspect(completion_id=completion.completion_id)
    assert historical.receipt == view.receipt
    assert not historical.active_deployment_authority
    assert not historical.runtime_health_authority


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实执行夹具使用 POSIX shebang")
async def test_invalid_health_output_persists_one_unhealthy_terminal_receipt(
    tmp_path: Path,
) -> None:
    executor = _CapturingExecutor()
    service, store, deployment, completion, _slots = await _health_service(
        tmp_path,
        backend=_runtime_backend(valid_health=False),
        executor=executor,
    )

    first = await service.observe(completion_id=completion.completion_id)
    second = await service.reconcile(completion_id=completion.completion_id)

    assert first == second
    assert len(executor.environments) == 1
    assert first.receipt.outcome == "unhealthy"
    assert first.receipt.failure_code == "runtime_health_report_invalid"
    assert first.receipt.health_report is None
    assert first.receipt.process_started
    assert not first.receipt.runtime_health_authority
    assert not first.runtime_health_authority
    assert await store.get_by_deployment(deployment.receipt.receipt_id) == first.receipt
