from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from naumi_agent.evolution.experiment_leases import (
    EvolutionExperimentLeaseStore,
    ExperimentLeaseState,
    ExperimentWorktreeLease,
)
from naumi_agent.evolution.self_review_comparison import (
    EvolutionSelfReviewComparisonError,
    EvolutionSelfReviewComparisonExecutor,
)
from naumi_agent.evolution.self_review_green_cohort import (
    EvolutionSelfReviewGreenCohortError,
    EvolutionSelfReviewGreenCohortExecutor,
    EvolutionSelfReviewGreenCohortRequestBuilder,
)
from naumi_agent.evolution.self_review_red_baseline import (
    EvolutionSelfReviewRedBaselineError,
    EvolutionSelfReviewRedBaselineExecutor,
    EvolutionSelfReviewRedCohortReceipt,
)
from naumi_agent.evolution.validation_cohorts import (
    BASELINE_COHORT_REQUEST_POLICY,
    BaselineCohortCheckCase,
    BaselineCohortMetricCase,
    EvolutionBaselineCohortRequest,
    _sample_seeds,
)
from naumi_agent.evolution.validation_metric_bindings import (
    EvolutionMetricRunnerBindingBuilder,
    EvolutionMetricRunnerRegistry,
)
from naumi_agent.evolution.validation_plans import (
    VALIDATION_PLAN_POLICY,
    EvolutionValidationPlan,
    ValidationCheckCoverage,
    ValidationFileRequirement,
    ValidationMetricPair,
)
from naumi_agent.harness.eval_models import (
    EvalCaseStatus,
    EvalRunStatus,
    HarnessEvalSuiteResult,
)
from naumi_agent.harness.eval_statistics import (
    EvalStatisticalVerdict,
    compare_eval_repetitions,
)
from naumi_agent.harness.store import HarnessStore, HarnessStoreError
from naumi_agent.harness.trust import HarnessTrustStore


def _sha256(payload: object) -> str:
    return hashlib.sha256(json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
    ).stdout


def _repository(
    tmp_path: Path,
    *,
    symlink: bool = False,
    operation: str = "modify",
) -> tuple[Path, str, str, str | None, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    candidate = b"def clean() -> None:\n    pass\n"
    baseline: bytes | None
    if operation == "create":
        baseline = None
        (root / "README.md").write_text("baseline\n")
    elif symlink:
        baseline = b"target.py"
        (root / "target.py").write_text("def target() -> None:\n    pass\n")
        (root / "sample.py").symlink_to("target.py")
    else:
        baseline = (
            b"def risky():\n"
            b"    try:\n"
            b"        return 1\n"
            b"    except Exception:\n"
            b"        return 0\n"
        )
        (root / "sample.py").write_bytes(baseline)
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    commit = _git(root, "rev-parse", "HEAD").decode().strip()
    listing = _git(root, "ls-tree", "-r", "-z", "--full-tree", commit)
    if operation == "create":
        (root / "sample.py").write_bytes(candidate)
    return (
        root,
        commit,
        hashlib.sha256(listing).hexdigest(),
        hashlib.sha256(baseline).hexdigest() if baseline is not None else None,
        hashlib.sha256(candidate).hexdigest(),
    )


def _plan(
    *,
    commit: str,
    tree_sha256: str,
    baseline_sha256: str | None,
    candidate_sha256: str,
    operation: str = "modify",
    file_kind: str = "python",
):
    contract_id = f"evx_{'1' * 24}"
    contract_manifest_sha256 = "2" * 64
    lease_digest = hashlib.sha256(
        f"{contract_id}:{contract_manifest_sha256}".encode()
    ).hexdigest()
    metric = ValidationMetricPair(
        order=1,
        metric_name="self_review.broad_except.count",
        direction="decrease",
        target=0,
        verifier="self_review_static",
        procedure="统计指定 Python 文件中的 broad_except finding。",
    )
    payload = {
        "schema_version": 2,
        "policy_version": VALIDATION_PLAN_POLICY,
        "contract_id": contract_id,
        "contract_manifest_sha256": contract_manifest_sha256,
        "lease_id": f"evl_{lease_digest[:24]}",
        "source_snapshot_id": f"evs_{'4' * 24}",
        "source_snapshot_sha256": "5" * 64,
        "mutation_receipt_id": f"evmr_{'6' * 24}",
        "mutation_receipt_sha256": "7" * 64,
        "candidate_id": f"evc_{'8' * 24}",
        "candidate_revision": 1,
        "seed": 42,
        "baseline_commit": commit,
        "baseline_tree_sha256": tree_sha256,
        "profile_sha256": "9" * 64,
        "experiment_config_sha256": "a" * 64,
        "toolset_sha256": "b" * 64,
        "candidate_files_sha256": "c" * 64,
        "files": [ValidationFileRequirement(
            path="sample.py",
            file_kind=file_kind,
            required_checks=("unit",),
            operation=operation,
            baseline_sha256=baseline_sha256,
            candidate_sha256=candidate_sha256,
        ).model_dump(mode="json")],
        "metrics": [metric.model_dump(mode="json")],
        "required_check_kinds": ["unit"],
        "baseline_first": True,
        "identical_environment_required": True,
        "har08_comparison_receipt_required": True,
        "validation_ready": True,
        "runner_binding_status": "required",
        "execution_ready": False,
        "promotion_ready": False,
    }
    digest = _sha256(payload)
    return EvolutionValidationPlan.model_validate({
        **payload,
        "validation_plan_id": f"evvplan_{digest[:24]}",
        "validation_plan_sha256": digest,
    })


def _request(plan: EvolutionValidationPlan, *, samples: int = 5):
    coverage = ValidationCheckCoverage(
        path="sample.py",
        check_kind="unit",
        check_id="unit",
    )
    requirements_sha256 = _sha256([{"path": "sample.py", "check_kind": "unit"}])
    check = BaselineCohortCheckCase(
        order=1,
        check_id="unit",
        spec_sha256="d" * 64,
        argv_sha256="e" * 64,
        timeout_seconds=1,
        coverage=(coverage,),
    )
    plan_metric = plan.metrics[0]
    metric = BaselineCohortMetricCase(
        order=1,
        metric_name=plan_metric.metric_name,
        direction=plan_metric.direction,
        target=plan_metric.target,
        verifier=plan_metric.verifier,
        procedure_sha256=hashlib.sha256(plan_metric.procedure.encode()).hexdigest(),
    )
    payload = {
        "schema_version": 1,
        "policy_version": BASELINE_COHORT_REQUEST_POLICY,
        "contract_id": plan.contract_id,
        "contract_manifest_sha256": plan.contract_manifest_sha256,
        "validation_plan_id": plan.validation_plan_id,
        "validation_plan_sha256": plan.validation_plan_sha256,
        "profile_binding_id": f"evvbind_{'f' * 24}",
        "profile_binding_sha256": "0" * 64,
        "profile_binding_requirements_sha256": requirements_sha256,
        "candidate_id": plan.candidate_id,
        "candidate_revision": plan.candidate_revision,
        "phase": "red",
        "suite_id": f"evo_{plan.validation_plan_sha256[:24]}",
        "batch_id": f"evo:red:{plan.validation_plan_sha256[:24]}",
        "requested_samples": samples,
        "base_seed": plan.seed,
        "sample_seeds": list(_sample_seeds(
            plan.seed,
            plan.validation_plan_sha256,
            samples,
        )),
        "baseline_commit": plan.baseline_commit,
        "baseline_tree_sha256": plan.baseline_tree_sha256,
        "profile_sha256": plan.profile_sha256,
        "experiment_config_sha256": plan.experiment_config_sha256,
        "toolset_sha256": plan.toolset_sha256,
        "source_materialization": "arc04_ephemeral_git_worktree",
        "checks": [check.model_dump(mode="json")],
        "metrics": [metric.model_dump(mode="json")],
        "check_timeout_seconds_per_sample": 1,
        "max_total_duration_seconds": 300,
        "network_access": False,
        "dependency_installation": False,
        "runtime_identity_required": True,
        "profile_trust_revalidation_required": True,
        "metric_timeout_binding_required": True,
        "continuous_sample_indexes_required": True,
        "harness_result_store_required": True,
        "har08_comparison_receipt_required": True,
        "candidate_request_allowed": False,
        "request_ready": True,
        "arc04_worker_required": True,
        "execution_ready": False,
    }
    digest = _sha256(payload)
    return EvolutionBaselineCohortRequest.model_validate({
        **payload,
        "request_id": f"evvred_{digest[:24]}",
        "request_sha256": digest,
    })


async def _authority(
    tmp_path: Path,
    *,
    symlink: bool = False,
    operation: str = "modify",
    baseline_sha256_override: str | None = None,
    file_kind="python",
):
    root, commit, tree_sha256, baseline_sha256, candidate_sha256 = _repository(
        tmp_path,
        symlink=symlink,
        operation=operation,
    )
    plan = _plan(
        commit=commit,
        tree_sha256=tree_sha256,
        baseline_sha256=(
            baseline_sha256_override
            if baseline_sha256_override is not None
            else baseline_sha256
        ),
        candidate_sha256=candidate_sha256,
        operation=operation,
        file_kind=file_kind,
    )
    request = _request(plan)
    binding = EvolutionMetricRunnerBindingBuilder().build(
        baseline_request=request,
        validation_plan=plan,
    )
    store = HarnessStore(tmp_path / "harness.db")
    trust = HarnessTrustStore(tmp_path / "trust.db")
    await trust.trust(root, plan.profile_sha256, source="test")
    return root, plan, request, binding, store, trust


class _StaticLeaseStore(EvolutionExperimentLeaseStore):
    def __init__(self, db_path: Path, lease: ExperimentWorktreeLease) -> None:
        super().__init__(db_path)
        self.current = lease

    async def get(self, contract_id: str):
        assert contract_id == self.current.contract_id
        return self.current


async def _green_authority(tmp_path: Path, *, operation: str = "modify"):
    root, plan, request, binding, store, trust = await _authority(
        tmp_path,
        operation=operation,
    )
    red = await EvolutionSelfReviewRedBaselineExecutor(
        store=store,
        trust_store=trust,
    ).execute(
        workspace_root=root,
        baseline_request=request,
        metric_binding=binding,
        validation_plan=plan,
    )
    storage = tmp_path / "worktrees"
    storage.mkdir()
    worktree_name = f"experiment-{plan.contract_id.removeprefix('evx_')[:16]}"
    candidate = storage / worktree_name
    branch = f"codex/evo-green-{operation}"
    _git(
        root,
        "worktree",
        "add",
        "-q",
        "-b",
        branch,
        str(candidate),
        plan.baseline_commit,
    )
    (candidate / "sample.py").write_text("def clean() -> None:\n    pass\n")
    lease = ExperimentWorktreeLease(
        lease_id=plan.lease_id,
        contract_id=plan.contract_id,
        manifest_sha256=plan.contract_manifest_sha256,
        session_id="session-green",
        mission_id="mission-green",
        task_id="task-green",
        owner="test",
        state=ExperimentLeaseState.ACTIVE,
        worktree_name=worktree_name,
        worktree_path=str(candidate),
        branch=branch,
        baseline_commit=plan.baseline_commit,
        expires_at=datetime(2030, 1, 1, tzinfo=UTC).isoformat(),
        created_at=datetime(2026, 7, 19, tzinfo=UTC).isoformat(),
        updated_at=datetime(2026, 7, 19, tzinfo=UTC).isoformat(),
        worktree_ready=True,
        execution_ready=False,
    )
    lease_store = _StaticLeaseStore(tmp_path / "leases.db", lease)
    green_request = EvolutionSelfReviewGreenCohortRequestBuilder().build(
        baseline_request=request,
        metric_binding=binding,
        validation_plan=plan,
        red_receipt=red,
        lease=lease,
    )
    return (
        root,
        candidate,
        storage,
        plan,
        request,
        binding,
        red,
        green_request,
        lease,
        lease_store,
        store,
        trust,
    )


@pytest.mark.asyncio
async def test_exact_committed_source_is_repeated_persisted_and_idempotent(
    tmp_path: Path,
) -> None:
    root, plan, request, binding, store, trust = await _authority(tmp_path)
    (root / "sample.py").write_text("def clean() -> None:\n    pass\n")
    status_before = _git(root, "status", "--porcelain")
    executor = EvolutionSelfReviewRedBaselineExecutor(
        store=store,
        trust_store=trust,
    )

    first = await executor.execute(
        workspace_root=root,
        baseline_request=request,
        metric_binding=binding,
        validation_plan=plan,
    )
    second = await executor.execute(
        workspace_root=root,
        baseline_request=request,
        metric_binding=binding,
        validation_plan=plan,
    )
    records = await store.list_eval_results(root, request.batch_id, request.suite_id)

    assert first == second
    assert first.persisted_samples == first.requested_samples == 5
    assert first.metrics[0].sample_values == (1, 1, 1, 1, 1)
    assert first.project_code_executed is False
    assert first.arc04_worker_used is False
    assert [item.sample_index for item in records] == list(range(5))
    assert all(item.result.status is EvalRunStatus.FAILED for item in records)
    assert all(
        item.result.cases[0].status is EvalCaseStatus.IMPLEMENTATION_FAILURE
        for item in records
    )
    assert all(
        item.result.baseline_identity.source.commit == plan.baseline_commit
        for item in records
    )
    assert _git(root, "status", "--porcelain") == status_before
    tampered = first.model_dump(mode="json")
    tampered["metrics"][0]["sample_values"][0] = 2
    with pytest.raises(ValidationError, match="摘要"):
        EvolutionSelfReviewRedCohortReceipt.model_validate(tampered)


@pytest.mark.asyncio
async def test_created_file_uses_an_empty_red_fixture_without_reading_candidate(
    tmp_path: Path,
) -> None:
    root, plan, request, binding, store, trust = await _authority(
        tmp_path,
        operation="create",
    )
    status_before = _git(root, "status", "--porcelain")

    receipt = await EvolutionSelfReviewRedBaselineExecutor(
        store=store,
        trust_store=trust,
    ).execute(
        workspace_root=root,
        baseline_request=request,
        metric_binding=binding,
        validation_plan=plan,
    )
    records = await store.list_eval_results(root, request.batch_id, request.suite_id)

    assert receipt.metrics[0].sample_values == (0, 0, 0, 0, 0)
    assert all(item.result.status is EvalRunStatus.PASSED for item in records)
    assert plan.files[0].operation == "create"
    assert plan.files[0].baseline_sha256 is None
    assert _git(root, "status", "--porcelain") == status_before


def test_legacy_validation_plan_v1_remains_readable_but_operation_unbound(
    tmp_path: Path,
) -> None:
    _, commit, tree_sha256, baseline_sha256, candidate_sha256 = _repository(tmp_path)
    current = _plan(
        commit=commit,
        tree_sha256=tree_sha256,
        baseline_sha256=baseline_sha256,
        candidate_sha256=candidate_sha256,
    )
    legacy = current.model_dump(
        mode="json",
        exclude={"validation_plan_id", "validation_plan_sha256"},
    )
    legacy["schema_version"] = 1
    legacy["policy_version"] = "evolution-validation-plan-v1"
    for file in legacy["files"]:
        file.pop("operation")
        file.pop("baseline_sha256")
        file.pop("candidate_sha256")
    digest = _sha256(legacy)

    restored = EvolutionValidationPlan.model_validate({
        **legacy,
        "validation_plan_id": f"evvplan_{digest[:24]}",
        "validation_plan_sha256": digest,
    })

    assert restored.schema_version == 1
    assert restored.files[0].operation == "unknown"


class _FailAfterTwoStore(HarnessStore):
    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path)
        self.calls = 0

    async def record_eval_result(self, **kwargs):
        self.calls += 1
        if self.calls == 3:
            raise HarnessStoreError("injected interruption")
        return await super().record_eval_result(**kwargs)


@pytest.mark.asyncio
async def test_matching_partial_prefix_resumes_to_a_continuous_cohort(
    tmp_path: Path,
) -> None:
    root, plan, request, binding, _, trust = await _authority(tmp_path)
    failing = _FailAfterTwoStore(tmp_path / "harness.db")
    with pytest.raises(HarnessStoreError, match="interruption"):
        await EvolutionSelfReviewRedBaselineExecutor(
            store=failing,
            trust_store=trust,
        ).execute(
            workspace_root=root,
            baseline_request=request,
            metric_binding=binding,
            validation_plan=plan,
        )

    store = HarnessStore(tmp_path / "harness.db")
    partial = await store.list_eval_results(root, request.batch_id, request.suite_id)
    assert [item.sample_index for item in partial] == [0, 1]

    receipt = await EvolutionSelfReviewRedBaselineExecutor(
        store=store,
        trust_store=trust,
    ).execute(
        workspace_root=root,
        baseline_request=request,
        metric_binding=binding,
        validation_plan=plan,
    )

    assert receipt.persisted_samples == 5
    records = await store.list_eval_results(root, request.batch_id, request.suite_id)
    assert [item.sample_index for item in records] == list(range(5))


@pytest.mark.asyncio
async def test_conflicting_existing_prefix_is_rejected_without_appending(
    tmp_path: Path,
) -> None:
    root, plan, request, binding, _, trust = await _authority(tmp_path)
    source_store = HarnessStore(tmp_path / "source.db")
    await EvolutionSelfReviewRedBaselineExecutor(
        store=source_store,
        trust_store=trust,
    ).execute(
        workspace_root=root,
        baseline_request=request,
        metric_binding=binding,
        validation_plan=plan,
    )
    source = await source_store.get_eval_result(
        root,
        request.batch_id,
        request.suite_id,
        0,
    )
    assert source is not None
    payload = source.result.model_dump(mode="json")
    payload["cases"][0]["metric_observations"][0]["value"] = 2
    payload["cases"][0]["message"] = "conflicting count"
    conflicting = HarnessEvalSuiteResult.model_validate(payload)
    store = HarnessStore(tmp_path / "harness.db")
    await store.record_eval_result(
        workspace_root=root,
        batch_id=request.batch_id,
        sample_index=0,
        result=conflicting,
        created_at=source.created_at,
    )

    with pytest.raises(
        EvolutionSelfReviewRedBaselineError,
        match="已有.*不一致",
    ) as error:
        await EvolutionSelfReviewRedBaselineExecutor(
            store=store,
            trust_store=trust,
        ).execute(
            workspace_root=root,
            baseline_request=request,
            metric_binding=binding,
            validation_plan=plan,
        )

    assert error.value.code == "existing_cohort_conflict"
    records = await store.list_eval_results(root, request.batch_id, request.suite_id)
    assert [item.sample_index for item in records] == [0]


@pytest.mark.asyncio
async def test_revoked_profile_trust_fails_before_h5a_write(tmp_path: Path) -> None:
    root, plan, request, binding, store, trust = await _authority(tmp_path)
    await trust.untrust(root)

    with pytest.raises(
        EvolutionSelfReviewRedBaselineError,
        match="信任已失效",
    ) as error:
        await EvolutionSelfReviewRedBaselineExecutor(
            store=store,
            trust_store=trust,
        ).execute(
            workspace_root=root,
            baseline_request=request,
            metric_binding=binding,
            validation_plan=plan,
        )

    assert error.value.code == "profile_trust_revalidation_failed"
    assert await store.list_eval_results(root, request.batch_id, request.suite_id) == ()


@pytest.mark.asyncio
async def test_modify_before_digest_mismatch_fails_before_h5a_write(
    tmp_path: Path,
) -> None:
    root, plan, request, binding, store, trust = await _authority(
        tmp_path,
        baseline_sha256_override="0" * 64,
    )

    with pytest.raises(
        EvolutionSelfReviewRedBaselineError,
        match="before digest",
    ) as error:
        await EvolutionSelfReviewRedBaselineExecutor(
            store=store,
            trust_store=trust,
        ).execute(
            workspace_root=root,
            baseline_request=request,
            metric_binding=binding,
            validation_plan=plan,
        )

    assert error.value.code == "baseline_blob_digest_mismatch"
    assert await store.list_eval_results(root, request.batch_id, request.suite_id) == ()


@pytest.mark.asyncio
async def test_git_symlink_is_rejected_before_scan_or_persistence(tmp_path: Path) -> None:
    root, plan, request, binding, store, trust = await _authority(
        tmp_path,
        symlink=True,
    )

    with pytest.raises(
        EvolutionSelfReviewRedBaselineError,
        match="不是普通 Git blob",
    ) as error:
        await EvolutionSelfReviewRedBaselineExecutor(
            store=store,
            trust_store=trust,
        ).execute(
            workspace_root=root,
            baseline_request=request,
            metric_binding=binding,
            validation_plan=plan,
        )

    assert error.value.code == "baseline_path_type_unsafe"
    assert await store.list_eval_results(root, request.batch_id, request.suite_id) == ()


@pytest.mark.asyncio
async def test_non_python_validation_path_is_rejected_before_git_read(tmp_path: Path) -> None:
    root, plan, request, binding, store, trust = await _authority(
        tmp_path,
        file_kind="markdown",
    )

    with pytest.raises(
        EvolutionSelfReviewRedBaselineError,
        match="只接受.*Python",
    ) as error:
        await EvolutionSelfReviewRedBaselineExecutor(
            store=store,
            trust_store=trust,
        ).execute(
            workspace_root=root,
            baseline_request=request,
            metric_binding=binding,
            validation_plan=plan,
        )

    assert error.value.code == "self_review_python_paths_required"
    assert await store.list_eval_results(root, request.batch_id, request.suite_id) == ()


def test_self_review_count_runner_rejects_invalid_metric_contract() -> None:
    resolution = EvolutionMetricRunnerRegistry().resolve(
        BaselineCohortMetricCase(
            order=1,
            metric_name="self_review.broad_except.count",
            direction="increase",
            target=0.5,
            verifier="self_review_static",
            procedure_sha256="a" * 64,
        ),
        validation_paths=("sample.py",),
    )

    assert resolution.status == "blocked"
    assert resolution.blocking_code == "self_review_metric_contract_invalid"


@pytest.mark.asyncio
async def test_green_modify_cohort_is_idempotent_and_statistically_improved(
    tmp_path: Path,
) -> None:
    (
        root,
        candidate,
        storage,
        plan,
        request,
        binding,
        red,
        green_request,
        lease,
        lease_store,
        store,
        trust,
    ) = await _green_authority(tmp_path)
    status_before = _git(candidate, "status", "--porcelain")
    executor = EvolutionSelfReviewGreenCohortExecutor(
        store=store,
        trust_store=trust,
        lease_store=lease_store,
        worktree_storage_dir=storage,
        clock=lambda: datetime(2026, 7, 19, 1, tzinfo=UTC),
    )

    first = await executor.execute(
        workspace_root=root,
        green_request=green_request,
        baseline_request=request,
        metric_binding=binding,
        validation_plan=plan,
        red_receipt=red,
        lease=lease,
    )
    second = await executor.execute(
        workspace_root=root,
        green_request=green_request,
        baseline_request=request,
        metric_binding=binding,
        validation_plan=plan,
        red_receipt=red,
        lease=lease,
    )
    red_records = await store.list_eval_results(
        root,
        request.batch_id,
        request.suite_id,
    )
    green_records = await store.list_eval_results(
        root,
        green_request.batch_id,
        green_request.suite_id,
    )
    statistical = compare_eval_repetitions(
        tuple(item.result for item in red_records),
        tuple(item.result for item in green_records),
    )

    assert first == second
    assert first.metrics[0].sample_values == (0, 0, 0, 0, 0)
    assert red.metrics[0].sample_values == (1, 1, 1, 1, 1)
    assert green_request.sample_seeds == request.sample_seeds
    assert [item.sample_index for item in green_records] == list(range(5))
    assert statistical.verdict is EvalStatisticalVerdict.IMPROVED
    assert all(item.result.status is EvalRunStatus.PASSED for item in green_records)
    assert all(
        item.result.baseline_identity is not None
        and item.result.baseline_identity.source.dirty
        and not item.result.baseline_identity.baseline_eligible
        for item in green_records
    )
    assert _git(candidate, "status", "--porcelain") == status_before


@pytest.mark.asyncio
async def test_green_create_cohort_uses_candidate_after_empty_red_fixture(
    tmp_path: Path,
) -> None:
    (
        root,
        _,
        storage,
        plan,
        request,
        binding,
        red,
        green_request,
        lease,
        lease_store,
        store,
        trust,
    ) = await _green_authority(tmp_path, operation="create")

    green = await EvolutionSelfReviewGreenCohortExecutor(
        store=store,
        trust_store=trust,
        lease_store=lease_store,
        worktree_storage_dir=storage,
        clock=lambda: datetime(2026, 7, 19, 1, tzinfo=UTC),
    ).execute(
        workspace_root=root,
        green_request=green_request,
        baseline_request=request,
        metric_binding=binding,
        validation_plan=plan,
        red_receipt=red,
        lease=lease,
    )
    comparison = await EvolutionSelfReviewComparisonExecutor(store).execute(
        workspace_root=root,
        baseline_request=request,
        metric_binding=binding,
        validation_plan=plan,
        red_receipt=red,
        green_request=green_request,
        green_receipt=green,
    )

    assert plan.files[0].operation == "create"
    assert red.metrics[0].sample_values == (0, 0, 0, 0, 0)
    assert green.metrics[0].sample_values == (0, 0, 0, 0, 0)
    assert comparison.receipt.statistical_verdict == "unchanged"
    assert comparison.receipt.decision == "passed"


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["extra_path", "digest"])
async def test_green_candidate_drift_fails_before_h5a_write(
    tmp_path: Path,
    drift: str,
) -> None:
    (
        root,
        candidate,
        storage,
        plan,
        request,
        binding,
        red,
        green_request,
        lease,
        lease_store,
        store,
        trust,
    ) = await _green_authority(tmp_path)
    if drift == "extra_path":
        (candidate / "extra.py").write_text("EXTRA = True\n")
        expected_code = "candidate_status_mismatch"
    else:
        (candidate / "sample.py").write_text("def drifted() -> int:\n    return 2\n")
        expected_code = "candidate_file_digest_mismatch"

    with pytest.raises(EvolutionSelfReviewGreenCohortError) as error:
        await EvolutionSelfReviewGreenCohortExecutor(
            store=store,
            trust_store=trust,
            lease_store=lease_store,
            worktree_storage_dir=storage,
            clock=lambda: datetime(2026, 7, 19, 1, tzinfo=UTC),
        ).execute(
            workspace_root=root,
            green_request=green_request,
            baseline_request=request,
            metric_binding=binding,
            validation_plan=plan,
            red_receipt=red,
            lease=lease,
        )

    assert error.value.code == expected_code
    assert await store.list_eval_results(
        root,
        green_request.batch_id,
        green_request.suite_id,
    ) == ()


@pytest.mark.asyncio
async def test_green_rejects_a_stale_lease_store_record(tmp_path: Path) -> None:
    (
        root,
        _,
        storage,
        plan,
        request,
        binding,
        red,
        green_request,
        lease,
        lease_store,
        store,
        trust,
    ) = await _green_authority(tmp_path)
    lease_store.current = lease.model_copy(update={
        "state": ExperimentLeaseState.RELEASED,
        "worktree_ready": False,
    })

    with pytest.raises(EvolutionSelfReviewGreenCohortError) as error:
        await EvolutionSelfReviewGreenCohortExecutor(
            store=store,
            trust_store=trust,
            lease_store=lease_store,
            worktree_storage_dir=storage,
            clock=lambda: datetime(2026, 7, 19, 1, tzinfo=UTC),
        ).execute(
            workspace_root=root,
            green_request=green_request,
            baseline_request=request,
            metric_binding=binding,
            validation_plan=plan,
            red_receipt=red,
            lease=lease,
        )

    assert error.value.code == "candidate_lease_stale"


@pytest.mark.asyncio
async def test_green_matching_partial_prefix_resumes_safely(tmp_path: Path) -> None:
    (
        root,
        _,
        storage,
        plan,
        request,
        binding,
        red,
        green_request,
        lease,
        lease_store,
        _,
        trust,
    ) = await _green_authority(tmp_path)
    failing = _FailAfterTwoStore(tmp_path / "harness.db")
    with pytest.raises(HarnessStoreError, match="interruption"):
        await EvolutionSelfReviewGreenCohortExecutor(
            store=failing,
            trust_store=trust,
            lease_store=lease_store,
            worktree_storage_dir=storage,
            clock=lambda: datetime(2026, 7, 19, 1, tzinfo=UTC),
        ).execute(
            workspace_root=root,
            green_request=green_request,
            baseline_request=request,
            metric_binding=binding,
            validation_plan=plan,
            red_receipt=red,
            lease=lease,
        )
    store = HarnessStore(tmp_path / "harness.db")
    partial = await store.list_eval_results(
        root,
        green_request.batch_id,
        green_request.suite_id,
    )
    assert [item.sample_index for item in partial] == [0, 1]

    receipt = await EvolutionSelfReviewGreenCohortExecutor(
        store=store,
        trust_store=trust,
        lease_store=lease_store,
        worktree_storage_dir=storage,
        clock=lambda: datetime(2026, 7, 19, 1, tzinfo=UTC),
    ).execute(
        workspace_root=root,
        green_request=green_request,
        baseline_request=request,
        metric_binding=binding,
        validation_plan=plan,
        red_receipt=red,
        lease=lease,
    )

    assert receipt.persisted_samples == 5


@pytest.mark.asyncio
async def test_self_review_comparison_persists_native_h5c_idempotently(
    tmp_path: Path,
) -> None:
    (
        root,
        _,
        storage,
        plan,
        request,
        binding,
        red,
        green_request,
        lease,
        lease_store,
        store,
        trust,
    ) = await _green_authority(tmp_path)
    green = await EvolutionSelfReviewGreenCohortExecutor(
        store=store,
        trust_store=trust,
        lease_store=lease_store,
        worktree_storage_dir=storage,
        clock=lambda: datetime(2026, 7, 19, 1, tzinfo=UTC),
    ).execute(
        workspace_root=root,
        green_request=green_request,
        baseline_request=request,
        metric_binding=binding,
        validation_plan=plan,
        red_receipt=red,
        lease=lease,
    )
    executor = EvolutionSelfReviewComparisonExecutor(store)

    first = await executor.execute(
        workspace_root=root,
        baseline_request=request,
        metric_binding=binding,
        validation_plan=plan,
        red_receipt=red,
        green_request=green_request,
        green_receipt=green,
    )
    second = await executor.execute(
        workspace_root=root,
        baseline_request=request,
        metric_binding=binding,
        validation_plan=plan,
        red_receipt=red,
        green_request=green_request,
        green_receipt=green,
    )
    reference = await store.get_eval_baseline_by_batch(
        root,
        request.suite_id,
        request.batch_id,
    )

    assert second == first
    assert reference is not None and reference.purpose == "comparison_reference"
    assert await store.get_active_eval_baseline(root, request.suite_id) is None
    assert first.receipt.statistical_verdict == "improved"
    assert first.receipt.decision == "passed"
    assert first.receipt.baseline_batch_id == request.batch_id
    assert first.receipt.current_batch_id == green_request.batch_id
    assert first.receipt.baseline_samples_sha256 == reference.samples_sha256


@pytest.mark.asyncio
async def test_self_review_comparison_rejects_missing_green_h5a_before_reference(
    tmp_path: Path,
) -> None:
    (
        root,
        _,
        storage,
        plan,
        request,
        binding,
        red,
        green_request,
        lease,
        lease_store,
        store,
        trust,
    ) = await _green_authority(tmp_path)
    green = await EvolutionSelfReviewGreenCohortExecutor(
        store=store,
        trust_store=trust,
        lease_store=lease_store,
        worktree_storage_dir=storage,
        clock=lambda: datetime(2026, 7, 19, 1, tzinfo=UTC),
    ).execute(
        workspace_root=root,
        green_request=green_request,
        baseline_request=request,
        metric_binding=binding,
        validation_plan=plan,
        red_receipt=red,
        lease=lease,
    )
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "DELETE FROM harness_eval_results "
            "WHERE batch_id = ? AND sample_index = 4",
            (green_request.batch_id,),
        )
        db.commit()

    with pytest.raises(EvolutionSelfReviewComparisonError) as error:
        await EvolutionSelfReviewComparisonExecutor(store).execute(
            workspace_root=root,
            baseline_request=request,
            metric_binding=binding,
            validation_plan=plan,
            red_receipt=red,
            green_request=green_request,
            green_receipt=green,
        )

    assert error.value.code == "green_cohort_evidence_mismatch"
    assert await store.get_eval_baseline_by_batch(
        root,
        request.suite_id,
        request.batch_id,
    ) is None


@pytest.mark.asyncio
async def test_self_review_comparison_rejects_tampered_green_receipt(
    tmp_path: Path,
) -> None:
    (
        root,
        _,
        storage,
        plan,
        request,
        binding,
        red,
        green_request,
        lease,
        lease_store,
        store,
        trust,
    ) = await _green_authority(tmp_path)
    green = await EvolutionSelfReviewGreenCohortExecutor(
        store=store,
        trust_store=trust,
        lease_store=lease_store,
        worktree_storage_dir=storage,
        clock=lambda: datetime(2026, 7, 19, 1, tzinfo=UTC),
    ).execute(
        workspace_root=root,
        green_request=green_request,
        baseline_request=request,
        metric_binding=binding,
        validation_plan=plan,
        red_receipt=red,
        lease=lease,
    )

    with pytest.raises(EvolutionSelfReviewComparisonError) as error:
        await EvolutionSelfReviewComparisonExecutor(store).execute(
            workspace_root=root,
            baseline_request=request,
            metric_binding=binding,
            validation_plan=plan,
            red_receipt=red,
            green_request=green_request,
            green_receipt=green.model_copy(update={"candidate_revision": 2}),
        )

    assert error.value.code == "comparison_authority_invalid"
