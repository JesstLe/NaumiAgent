from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from naumi_agent.daemons.execution_grants import ExecutionGrantStore
from naumi_agent.daemons.shell_admission import ShellWorkerAdmissionComposer
from naumi_agent.daemons.shell_worker import AuthenticatedLocalShellTransport
from naumi_agent.daemons.tool_jobs import ToolJobStore
from naumi_agent.daemons.worker_registry import WorkerRegistryStore
from naumi_agent.evolution.revalidation_adversarial_samples import (
    EvolutionRevalidationAdversarialSampleError,
    EvolutionRevalidationAdversarialSampleExecutor,
    EvolutionRevalidationAdversarialSampleStore,
)
from naumi_agent.harness.eval_identity import capture_eval_platform_identity
from naumi_agent.harness.sandbox_checks import HarnessSandboxCheckRunner
from naumi_agent.harness.sandbox_eval import HarnessSandboxEvalExecutionKernel
from tests.unit.test_evolution_revalidation_interventional_samples import (
    _executor_scenario,
    _require_real_backend,
)


def _executor(sample, harness_store):
    return EvolutionRevalidationAdversarialSampleExecutor(
        workspace_root=sample.workspace_root,
        harness_store=harness_store,
        receipt_store=EvolutionRevalidationAdversarialSampleStore(
            sample.receipt_store._db_path
        ),
        permission_store=sample.permission_store,
        run_grant_authority=sample.run_grant_authority,
        profile_service=sample.profile_service,
        sandbox_eval_kernel=sample.sandbox_eval_kernel,
        contract_service=sample.contract_service,
        source_service=sample.source_service,
    )


@pytest.mark.asyncio
async def test_fresh_adversarial_sample_executes_exact_platform_pair(
    tmp_path: Path,
) -> None:
    sample, contract, parent, harness_store, kernel, _state = await _executor_scenario(
        tmp_path
    )
    platform = capture_eval_platform_identity().system
    executor = _executor(sample, harness_store)

    receipt = await executor.execute(
        contract_id=contract.contract_id,
        platform=platform,
        parent_receipt_id=parent.receipt_id,
        sample_index=0,
    )
    repeated = await executor.execute(
        contract_id=contract.contract_id,
        platform=platform,
        parent_receipt_id="not-needed-after-completion",
        sample_index=0,
    )

    assert repeated == receipt
    assert receipt.platform == platform
    assert receipt.pair_complete and not receipt.cohort_complete
    assert not receipt.comparison_authority and not receipt.promotion_authority
    assert receipt.red_run_grant_sha256 == receipt.green_run_grant_sha256
    assert receipt.red_identity_sha256 != receipt.green_identity_sha256
    assert kernel.calls == ["adversarial", "adversarial"]
    with sqlite3.connect(tmp_path / ".naumi" / "run-grants.db") as db:
        assert db.execute(
            "SELECT state, revoke_reason FROM run_delegation_grants"
        ).fetchall() == [("revoked", "fresh_adversarial_sample_finished")]


@pytest.mark.asyncio
async def test_fresh_adversarial_sample_rejects_wrong_platform_and_missing_h5a(
    tmp_path: Path,
) -> None:
    sample, contract, parent, harness_store, _kernel, _state = await _executor_scenario(
        tmp_path
    )
    platform = capture_eval_platform_identity().system
    executor = _executor(sample, harness_store)
    wrong = next(item for item in ("linux", "macos", "windows") if item != platform)
    with pytest.raises(EvolutionRevalidationAdversarialSampleError) as mismatch:
        await executor.execute(
            contract_id=contract.contract_id,
            platform=wrong,
            parent_receipt_id=parent.receipt_id,
            sample_index=0,
        )
    assert mismatch.value.code in {
        "fresh_adversarial_platform_not_required",
        "fresh_adversarial_platform_mismatch",
    }

    receipt = await executor.execute(
        contract_id=contract.contract_id,
        platform=platform,
        parent_receipt_id=parent.receipt_id,
        sample_index=0,
    )
    async with harness_store._connection() as db:
        await db.execute(
            "DELETE FROM harness_eval_results WHERE batch_id = ? AND sample_index = 0",
            (receipt.green_batch_id,),
        )
        await db.commit()
    with pytest.raises(EvolutionRevalidationAdversarialSampleError) as missing:
        await executor.execute(
            contract_id=contract.contract_id,
            platform=platform,
            parent_receipt_id="not-needed",
            sample_index=0,
        )
    assert missing.value.code == "fresh_adversarial_existing_evidence_missing"


@pytest.mark.asyncio
async def test_fresh_adversarial_sample_runs_through_real_arc04_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_real_backend()
    sample, contract, parent, harness_store, _fake, _state = await _executor_scenario(
        tmp_path
    )
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    verify = binary_dir / "verify"
    verify.write_text(
        "#!/bin/sh\npython3 -c 'import ast; from pathlib import Path; "
        "[ast.parse(p.read_text()) for p in Path(\".\").rglob(\"*.py\")]'\n"
    )
    verify.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary_dir}{os.pathsep}{os.environ['PATH']}")
    composer = ShellWorkerAdmissionComposer(
        worker_registry=WorkerRegistryStore(tmp_path / ".naumi" / "workers.db"),
        harness_store=harness_store,
        permission_store=sample.permission_store,
        execution_grant_store=ExecutionGrantStore(
            tmp_path / ".naumi" / "execution-grants.db"
        ),
        tool_job_store=ToolJobStore(tmp_path / ".naumi" / "tool-jobs.db"),
        transport=AuthenticatedLocalShellTransport(
            runtime_dir=tmp_path / ".naumi" / "transport"
        ),
        software_version="test",
        run_delegation_grant_authority=sample.run_grant_authority,
    )
    kernel = HarnessSandboxEvalExecutionKernel(
        workspace_root=tmp_path,
        permission_store=sample.permission_store,
        run_grant_authority=sample.run_grant_authority,
        sandbox_runner=HarnessSandboxCheckRunner(
            workspace_root=tmp_path,
            sandbox_root=tmp_path / ".naumi" / "sandboxes",
            artifact_root=tmp_path / ".naumi" / "artifacts",
        ),
        shell_admission_composer=composer,
        now=sample.now,
    )
    executor = EvolutionRevalidationAdversarialSampleExecutor(
        workspace_root=tmp_path,
        harness_store=harness_store,
        receipt_store=EvolutionRevalidationAdversarialSampleStore(
            sample.receipt_store._db_path
        ),
        permission_store=sample.permission_store,
        run_grant_authority=sample.run_grant_authority,
        profile_service=sample.profile_service,
        sandbox_eval_kernel=kernel,
        contract_service=sample.contract_service,
        source_service=sample.source_service,
    )

    receipt = await executor.execute(
        contract_id=contract.contract_id,
        platform=capture_eval_platform_identity().system,
        parent_receipt_id=parent.receipt_id,
        sample_index=0,
    )

    assert receipt.arc04_worker_used and receipt.project_code_executed
    assert receipt.red_lifecycle_sha256[0] != "2" * 64
    assert receipt.green_lifecycle_sha256[0] != "2" * 64
