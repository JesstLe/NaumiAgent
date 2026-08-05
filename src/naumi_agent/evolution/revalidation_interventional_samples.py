"""Execute one fresh current-target RED/GREEN interventional sample pair."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.daemons.permission_decisions import PermissionDecisionReceiptStore
from naumi_agent.daemons.run_delegation_grants import (
    RunDelegationGrantAuthority,
    RunDelegationGrantRequest,
)
from naumi_agent.evolution.revalidation_runtime_contracts import (
    EvolutionRevalidationRuntimeContractService,
)
from naumi_agent.evolution.revalidation_runtime_sources import (
    EvolutionRevalidationRuntimeSourceService,
)
from naumi_agent.evolution.self_review import SELF_REVIEW_STATIC_RUNNER_VERSION
from naumi_agent.evolution.self_review_eval_runtime import (
    SelfReviewEvalRuntimeError,
    run_bound_self_review_static_sample,
)
from naumi_agent.harness.eval_identity import (
    HarnessEvalConfigurationIdentity,
    build_eval_baseline_identity,
    capture_eval_platform_identity,
)
from naumi_agent.harness.eval_models import (
    EvalCaseStatus,
    EvalRunStatus,
    HarnessEvalCaseResult,
    HarnessEvalComparisonPolicy,
    HarnessEvalSuiteResult,
)
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.sandbox_checks import (
    HarnessSandboxCheckStatus,
)
from naumi_agent.harness.sandbox_eval import (
    HarnessSandboxEvalExecutionError,
    HarnessSandboxEvalExecutionKernel,
    HarnessSandboxEvalRunAuthority,
)
from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.store import HarnessStore

EVOLUTION_REVALIDATION_INTERVENTIONAL_SAMPLE_POLICY = (
    "evolution-revalidation-interventional-sample-v1"
)
EVOLUTION_REVALIDATION_INTERVENTIONAL_RUNNER = "evolution_revalidation_interventional@1"
_SHA256_RE = r"^[0-9a-f]{64}$"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionRevalidationInterventionalSampleReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-interventional-sample-v1"] = (
        EVOLUTION_REVALIDATION_INTERVENTIONAL_SAMPLE_POLICY
    )
    receipt_id: str = Field(pattern=r"^evrevalsample_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    contract_id: str = Field(pattern=r"^evrevalruntime_[0-9a-f]{24}$")
    contract_sha256: str = Field(pattern=_SHA256_RE)
    validation_plan_id: str = Field(pattern=r"^evrevalvplan_[0-9a-f]{24}$")
    validation_plan_sha256: str = Field(pattern=_SHA256_RE)
    source_snapshot_id: str = Field(pattern=r"^evrevalsrc_[0-9a-f]{24}$")
    source_snapshot_sha256: str = Field(pattern=_SHA256_RE)
    sample_index: int = Field(ge=0, le=99)
    sample_seed: int = Field(ge=0, le=9_223_372_036_854_775_807)
    red_batch_id: str = Field(min_length=1, max_length=128)
    green_batch_id: str = Field(min_length=1, max_length=128)
    red_result_id: str = Field(min_length=1, max_length=128)
    red_result_sha256: str = Field(pattern=_SHA256_RE)
    green_result_id: str = Field(min_length=1, max_length=128)
    green_result_sha256: str = Field(pattern=_SHA256_RE)
    red_identity_sha256: str = Field(pattern=_SHA256_RE)
    green_identity_sha256: str = Field(pattern=_SHA256_RE)
    platform_sha256: str = Field(pattern=_SHA256_RE)
    check_ids: tuple[str, ...] = Field(min_length=1, max_length=80)
    red_check_statuses: tuple[str, ...] = Field(min_length=1, max_length=80)
    green_check_statuses: tuple[str, ...] = Field(min_length=1, max_length=80)
    red_lifecycle_receipt_sha256: tuple[str, ...] = Field(min_length=1, max_length=80)
    green_lifecycle_receipt_sha256: tuple[str, ...] = Field(min_length=1, max_length=80)
    red_run_grant_sha256: str = Field(pattern=_SHA256_RE)
    green_run_grant_sha256: str = Field(pattern=_SHA256_RE)
    profile_trust_revalidated: Literal[True] = True
    source_current_after_execution: Literal[True] = True
    identical_environment_observed: Literal[True] = True
    arc04_worker_used: Literal[True] = True
    project_code_executed: Literal[True] = True
    metrics_executed: Literal[True] = True
    pair_complete: Literal[True] = True
    cohort_complete: Literal[False] = False
    promotion_authority: Literal[False] = False
    completed_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _receipt_is_exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Fresh sample workspace 必须 canonical。")
        lengths = {
            len(self.check_ids),
            len(self.red_check_statuses),
            len(self.green_check_statuses),
            len(self.red_lifecycle_receipt_sha256),
            len(self.green_lifecycle_receipt_sha256),
        }
        if len(lengths) != 1 or len(set(self.check_ids)) != len(self.check_ids):
            raise ValueError("Fresh sample check evidence 投影不一致。")
        if self.red_batch_id == self.green_batch_id:
            raise ValueError("Fresh sample RED/GREEN batch 必须隔离。")
        if datetime.fromisoformat(self.completed_at).utcoffset() is None:
            raise ValueError("Fresh sample completed_at 必须包含 offset。")
        digest = _sha256_payload(
            self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        )
        if not hmac.compare_digest(self.receipt_sha256, digest):
            raise ValueError("Fresh sample receipt digest 不一致。")
        if self.receipt_id != f"evrevalsample_{digest[:24]}":
            raise ValueError("Fresh sample receipt identity 不一致。")
        return self


class EvolutionRevalidationInterventionalSampleError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationInterventionalSampleStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()

    async def record(
        self, item: EvolutionRevalidationInterventionalSampleReceipt
    ) -> EvolutionRevalidationInterventionalSampleReceipt:
        receipt = EvolutionRevalidationInterventionalSampleReceipt.model_validate_json(
            item.model_dump_json()
        )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            dependency = await (
                await db.execute(
                    "SELECT contract_sha256 FROM "
                    "evolution_revalidation_runtime_contracts WHERE contract_id = ?",
                    (receipt.contract_id,),
                )
            ).fetchone()
            if dependency is None or dependency["contract_sha256"] != receipt.contract_sha256:
                await db.rollback()
                raise EvolutionRevalidationInterventionalSampleError(
                    "fresh_sample_contract_mismatch",
                    "持久化 Runtime Contract 不存在或 digest 不一致。",
                )
            row = await (
                await db.execute(
                    "SELECT receipt_json FROM "
                    "evolution_revalidation_interventional_samples "
                    "WHERE contract_id = ? AND sample_index = ?",
                    (receipt.contract_id, receipt.sample_index),
                )
            ).fetchone()
            if row is not None:
                restored = EvolutionRevalidationInterventionalSampleReceipt.model_validate_json(
                    row["receipt_json"]
                )
                await db.rollback()
                if restored != receipt:
                    raise EvolutionRevalidationInterventionalSampleError(
                        "fresh_sample_receipt_conflict",
                        "同一 Runtime Contract/sample 已绑定不同完成回执。",
                    )
                return restored
            await db.execute(
                "INSERT INTO evolution_revalidation_interventional_samples "
                "(receipt_id, receipt_sha256, contract_id, sample_index, "
                "receipt_json, completed_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    receipt.receipt_id,
                    receipt.receipt_sha256,
                    receipt.contract_id,
                    receipt.sample_index,
                    receipt.model_dump_json(),
                    receipt.completed_at,
                ),
            )
            await db.commit()
        return receipt

    async def get_by_sample(self, contract_id: str, sample_index: int):
        if not self._db_path.exists():
            return None
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT receipt_json FROM "
                    "evolution_revalidation_interventional_samples "
                    "WHERE contract_id = ? AND sample_index = ?",
                    (contract_id, sample_index),
                )
            ).fetchone()
        return (
            None
            if row is None
            else EvolutionRevalidationInterventionalSampleReceipt.model_validate_json(
                row["receipt_json"]
            )
        )


class EvolutionRevalidationInterventionalSampleExecutor:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        harness_store: HarnessStore,
        receipt_store: EvolutionRevalidationInterventionalSampleStore,
        permission_store: PermissionDecisionReceiptStore,
        run_grant_authority: RunDelegationGrantAuthority,
        profile_service: HarnessService,
        sandbox_eval_kernel: HarnessSandboxEvalExecutionKernel,
        contract_service: EvolutionRevalidationRuntimeContractService,
        source_service: EvolutionRevalidationRuntimeSourceService,
        now: Callable[[], str] | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.harness_store = harness_store
        self.receipt_store = receipt_store
        self.permission_store = permission_store
        self.run_grant_authority = run_grant_authority
        self.profile_service = profile_service
        self.sandbox_eval_kernel = sandbox_eval_kernel
        self.contract_service = contract_service
        self.source_service = source_service
        self.now = now or (lambda: datetime.now(UTC).isoformat())

    async def execute(
        self,
        *,
        contract_id: str,
        parent_receipt_id: str,
        sample_index: int,
    ) -> EvolutionRevalidationInterventionalSampleReceipt:
        existing = await self.receipt_store.get_by_sample(contract_id, sample_index)
        if existing is not None:
            await self._validate_existing(existing)
            return existing
        view = await self.contract_service.inspect(
            workspace_root=self.workspace_root,
            contract_id=contract_id,
        )
        contract = view.contract
        if not view.execution_eligible:
            raise EvolutionRevalidationInterventionalSampleError(
                "fresh_sample_runtime_contract_not_ready",
                f"Fresh Runtime Contract 当前为 {view.current_status}，不能执行。",
            )
        if isinstance(sample_index, bool) or not 0 <= sample_index < contract.requested_samples:
            raise EvolutionRevalidationInterventionalSampleError(
                "fresh_sample_index_invalid",
                "Fresh sample_index 超出 Runtime Contract 请求范围。",
            )
        pair = await self.source_service.materialize(
            workspace_root=self.workspace_root,
            validation_plan_id=contract.validation_plan_id,
        )
        _require_pair_matches_contract(pair, contract)
        checks = await self._current_checks(pair, contract)
        parent = await self.permission_store.get(parent_receipt_id)
        if parent is None or not parent.authorizes_execution or not parent.run_id:
            raise EvolutionRevalidationInterventionalSampleError(
                "fresh_sample_parent_permission_invalid",
                "Fresh sample 缺少可执行的父权限回执。",
            )
        if "bash_run" not in parent.delegated_tool_names:
            raise EvolutionRevalidationInterventionalSampleError(
                "fresh_sample_parent_delegation_missing",
                "父权限回执未授权 bash_run 运行委托。",
            )
        owner = f"evo-reval-{contract.contract_sha256[:16]}-{sample_index}"
        lease_seconds = min(
            3_600,
            max(
                30,
                2
                * (
                    contract.profile_timeout_seconds_per_sample
                    + contract.metric_timeout_seconds_per_sample
                )
                + 30,
            ),
        )
        lease = await self.harness_store.acquire_run_lease(
            workspace_root=self.workspace_root,
            run_kind=HarnessRunKind.RUNTIME,
            run_id=parent.run_id,
            owner_id=owner,
            now=self.now(),
            lease_seconds=lease_seconds,
        )
        if lease is None:
            raise EvolutionRevalidationInterventionalSampleError(
                "fresh_sample_runtime_lease_unavailable",
                "Fresh sample 无法取得独占 Runtime lease。",
            )
        grant = None
        try:
            grant = await self.run_grant_authority.issue(
                RunDelegationGrantRequest(
                    idempotency_key=(
                        f"evo-reval-{contract.contract_sha256[:20]}-{sample_index}-{lease.epoch}"
                    ),
                    parent_receipt_id=parent_receipt_id,
                    run_kind=HarnessRunKind.RUNTIME,
                    lease_owner_id=owner,
                    lease_epoch=lease.epoch,
                    delegated_tool_names=("bash_run",),
                ),
                now=self.now(),
                ttl_seconds=lease_seconds,
            )
            authority = HarnessSandboxEvalRunAuthority(
                parent_receipt_id=parent_receipt_id,
                run_id=parent.run_id,
                grant_id=grant.contract.grant_id,
                grant_sha256=grant.contract.grant_sha256,
            )
            platform = capture_eval_platform_identity()
            configuration = _configuration(
                contract,
                profile_sha256=pair.validation_plan.profile_sha256,
            )
            red_identity = build_eval_baseline_identity(
                self.workspace_root,
                configuration=configuration,
                platform_identity=platform,
                source_identity=pair.red_identity,
                profile_trusted=True,
            )
            green_identity = build_eval_baseline_identity(
                self.workspace_root,
                configuration=configuration,
                platform_identity=platform,
                source_identity=pair.green_identity,
                profile_trusted=True,
            )
            red = await self._load_or_execute_phase(
                phase="red",
                contract=contract,
                pair=pair,
                checks=checks,
                identity=red_identity,
                authority=authority,
                parent_receipt_id=parent_receipt_id,
                sample_index=sample_index,
            )
            green = await self._load_or_execute_phase(
                phase="green",
                contract=contract,
                pair=pair,
                checks=checks,
                identity=green_identity,
                authority=authority,
                parent_receipt_id=parent_receipt_id,
                sample_index=sample_index,
            )
            if not await pair.red.source_is_current():
                raise EvolutionRevalidationInterventionalSampleError(
                    "fresh_sample_source_changed_before_receipt",
                    "Fresh source 在完成回执持久化前发生变化。",
                )
            receipt = _build_receipt(
                contract=contract,
                pair=pair,
                sample_index=sample_index,
                red=red,
                green=green,
                completed_at=self.now(),
            )
            return await self.receipt_store.record(receipt)
        except HarnessSandboxEvalExecutionError as exc:
            raise EvolutionRevalidationInterventionalSampleError(exc.code, str(exc)) from exc
        finally:
            cleanup_errors = []
            if grant is not None:
                try:
                    await self.run_grant_authority.revoke(
                        grant_id=grant.contract.grant_id,
                        reason="fresh_sample_finished",
                        revoked_at=self.now(),
                    )
                except BaseException as exc:
                    cleanup_errors.append(exc)
            try:
                released = await self.harness_store.release_run_lease(
                    workspace_root=self.workspace_root,
                    run_kind=HarnessRunKind.RUNTIME,
                    run_id=parent.run_id,
                    owner_id=owner,
                    epoch=lease.epoch,
                    now=self.now(),
                )
                if released is None:
                    cleanup_errors.append(RuntimeError("Runtime lease 未释放。"))
            except BaseException as exc:
                cleanup_errors.append(exc)
            if cleanup_errors:
                raise EvolutionRevalidationInterventionalSampleError(
                    "fresh_sample_authority_cleanup_failed",
                    "Fresh sample 权限清理不完整："
                    + "; ".join(str(item) for item in cleanup_errors)[:300],
                )

    async def _current_checks(self, pair, contract):
        status = await self.profile_service.status()
        if not status.trusted or status.snapshot.profile is None:
            raise EvolutionRevalidationInterventionalSampleError(
                "fresh_sample_profile_untrusted",
                "Harness Profile 当前不受信任。",
            )
        plan = pair.validation_plan
        if status.profile_digest != plan.profile_sha256:
            raise EvolutionRevalidationInterventionalSampleError(
                "fresh_sample_profile_drifted",
                "Harness Profile digest 已偏离 Validation Plan。",
            )
        current = {item.id: item for item in status.snapshot.profile.checks}
        checks = tuple(current.get(item.id) for item in plan.checks)
        if any(item is None for item in checks) or any(
            _sha256_payload(observed.model_dump(mode="json"))
            != _sha256_payload(expected.model_dump(mode="json"))
            for observed, expected in zip(checks, plan.checks, strict=True)
        ):
            raise EvolutionRevalidationInterventionalSampleError(
                "fresh_sample_profile_checks_drifted",
                "Harness Profile checks 已偏离 Validation Plan。",
            )
        if (
            sum(item.timeout_seconds for item in checks)
            != contract.profile_timeout_seconds_per_sample
        ):
            raise EvolutionRevalidationInterventionalSampleError(
                "fresh_sample_profile_timeout_drifted",
                "Harness Profile timeout 已偏离 Runtime Contract。",
            )
        return checks

    async def _load_or_execute_phase(
        self,
        *,
        phase,
        contract,
        pair,
        checks,
        identity,
        authority,
        parent_receipt_id,
        sample_index,
    ):
        batch_id = _batch_id(contract, phase)
        existing = await self.harness_store.get_eval_result(
            self.workspace_root, batch_id, contract.suite_id, sample_index
        )
        if existing is not None:
            _validate_phase_result(existing, phase, contract, checks, identity)
            return existing
        results = await self.sandbox_eval_kernel.execute(
            lane=phase,
            authority_key=contract.contract_sha256,
            parent_receipt_id=parent_receipt_id,
            sample_index=sample_index,
            checks=checks,
            profile_digest=pair.validation_plan.profile_sha256,
            profile_is_current=lambda: self._profile_is_current(pair, contract),
            source=pair.red if phase == "red" else pair.green,
            run_authority=authority,
        )
        metric = await _run_metric(
            workspace_root=self.workspace_root,
            phase=phase,
            contract=contract,
            pair=pair,
            identity=identity,
        )
        suite = _build_suite(
            phase=phase,
            contract=contract,
            checks=results,
            metric=metric,
            identity=identity,
            run_grant_sha256=authority.grant_sha256,
        )
        return await self.harness_store.record_eval_result(
            workspace_root=self.workspace_root,
            batch_id=batch_id,
            sample_index=sample_index,
            result=suite,
            created_at=self.now(),
        )

    async def _profile_is_current(self, pair, contract) -> bool:
        try:
            await self._current_checks(pair, contract)
            return bool(await pair.red.source_is_current())
        except (OSError, TypeError, ValueError, RuntimeError):
            return False

    async def _validate_existing(self, receipt) -> None:
        view = await self.contract_service.inspect(
            workspace_root=self.workspace_root,
            contract_id=receipt.contract_id,
        )
        if not view.execution_eligible or view.contract.contract_sha256 != receipt.contract_sha256:
            raise EvolutionRevalidationInterventionalSampleError(
                "fresh_sample_existing_authority_stale",
                "已有 Fresh sample 的 Runtime Contract 已 stale。",
            )
        red = await self.harness_store.get_eval_result(
            self.workspace_root,
            receipt.red_batch_id,
            view.contract.suite_id,
            receipt.sample_index,
        )
        green = await self.harness_store.get_eval_result(
            self.workspace_root,
            receipt.green_batch_id,
            view.contract.suite_id,
            receipt.sample_index,
        )
        if (
            red is None
            or green is None
            or not (
                red.id == receipt.red_result_id
                and red.result_sha256 == receipt.red_result_sha256
                and green.id == receipt.green_result_id
                and green.result_sha256 == receipt.green_result_sha256
            )
        ):
            raise EvolutionRevalidationInterventionalSampleError(
                "fresh_sample_existing_evidence_missing",
                "已有 Fresh sample 回执引用的 H5a evidence 缺失或已损坏。",
            )


async def _run_metric(*, workspace_root, phase, contract, pair, identity):
    try:
        with TemporaryDirectory(prefix=f"naumi-revalidation-{phase}-") as temporary:
            root = Path(temporary).resolve()
            files = _materialize_metric_files(
                workspace_root=workspace_root,
                root=root,
                phase=phase,
                pair=pair,
            )
            return await run_bound_self_review_static_sample(
                files=files,
                scan_root=root,
                phase=phase,
                suite_id=contract.suite_id,
                validation_plan_id=contract.validation_plan_id,
                metric_entries=contract.metric_entries,
                configuration=identity.configuration,
                identity=identity,
                timeout_seconds=contract.metric_timeout_seconds_per_sample,
            )
    except SelfReviewEvalRuntimeError as exc:
        raise EvolutionRevalidationInterventionalSampleError(exc.code, str(exc)) from exc


def _materialize_metric_files(*, workspace_root, root, phase, pair):
    files = []
    overlays = {item.path: item for item in pair.overlays}
    for item in pair.validation_plan.files:
        if phase == "red":
            if item.red_sha256 is None:
                continue
            completed = subprocess.run(
                ["git", "-C", str(workspace_root), "show", f"{pair.red.revision}:{item.path}"],
                check=False,
                capture_output=True,
            )
            if completed.returncode != 0:
                raise EvolutionRevalidationInterventionalSampleError(
                    "fresh_sample_red_blob_missing",
                    f"RED revision 缺少可信文件：{item.path}",
                )
            content = completed.stdout
            expected_sha256 = item.red_sha256
        else:
            overlay = overlays.get(item.path)
            if overlay is None:
                raise EvolutionRevalidationInterventionalSampleError(
                    "fresh_sample_green_blob_missing",
                    f"GREEN immutable source 缺少文件：{item.path}",
                )
            content = overlay.content
            expected_sha256 = item.green_sha256
        if hashlib.sha256(content).hexdigest() != expected_sha256:
            raise EvolutionRevalidationInterventionalSampleError(
                "fresh_sample_metric_blob_digest_mismatch",
                f"Metric source digest 不一致：{item.path}",
            )
        destination = root.joinpath(*PurePosixPath(item.path).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        if os.name != "nt":
            destination.chmod(0o700 if item.executable else 0o600)
        files.append(destination)
    return files


def _configuration(contract, *, profile_sha256):
    policy = HarnessEvalComparisonPolicy()
    suite_sha256 = _sha256_payload(
        {
            "contract_sha256": contract.contract_sha256,
            "validation_plan_sha256": contract.validation_plan_sha256,
            "source_snapshot_sha256": contract.source_snapshot_sha256,
            "runner": EVOLUTION_REVALIDATION_INTERVENTIONAL_RUNNER,
        }
    )
    return HarnessEvalConfigurationIdentity.create(
        suite_id=contract.suite_id,
        suite_sha256=suite_sha256,
        profile_sha256=profile_sha256,
        policy_sha256=policy.sha256,
        runner_version=EVOLUTION_REVALIDATION_INTERVENTIONAL_RUNNER,
        repetitions=contract.requested_samples,
        live=False,
    )


def _build_suite(*, phase, contract, checks, metric, identity, run_grant_sha256):
    check_cases = tuple(_check_case(item, run_grant_sha256=run_grant_sha256) for item in checks)
    cases = check_cases + metric.cases
    status = (
        EvalRunStatus.EVALUATION_ERROR
        if any(item.status is EvalCaseStatus.EVALUATION_ERROR for item in cases)
        else EvalRunStatus.FAILED
        if any(item.status is EvalCaseStatus.IMPLEMENTATION_FAILURE for item in cases)
        else EvalRunStatus.PASSED
    )
    return HarnessEvalSuiteResult(
        suite_id=contract.suite_id,
        title=f"Fresh Interventional {phase.upper()} sample",
        suite_path=f"evolution/revalidation/{phase}/interventional",
        suite_sha256=identity.configuration.suite_sha256,
        status=status,
        cases=cases,
        code=f"fresh_interventional_{phase}_{status.value}",
        message="当前目标源码的 Profile checks 与可信 metrics 已真实执行。",
        comparison_policy=HarnessEvalComparisonPolicy(),
        baseline_identity=identity,
        duration_ms=sum(item.duration_ms for item in checks) + metric.duration_ms,
    )


def _check_case(result, *, run_grant_sha256):
    status = (
        EvalCaseStatus.PASSED
        if result.status is HarnessSandboxCheckStatus.PASSED
        else EvalCaseStatus.IMPLEMENTATION_FAILURE
        if result.status is HarnessSandboxCheckStatus.FAILED
        else EvalCaseStatus.EVALUATION_ERROR
    )
    return HarnessEvalCaseResult(
        case_id=result.check_id,
        runner="evolution_profile_check@1",
        status=status,
        code=result.status.value,
        message=(
            f"{result.message} lifecycle_sha256="
            f"{result.lifecycle_receipt_sha256 or 'missing'} "
            f"run_scope=sample run_grant_sha256={run_grant_sha256}"
        ),
        duration_ms=result.duration_ms,
    )


def _validate_phase_result(stored, phase, contract, checks, identity):
    result = stored.result
    check_cases = tuple(item for item in result.cases if item.runner == "evolution_profile_check@1")
    metric_cases = tuple(
        item for item in result.cases if item.runner == SELF_REVIEW_STATIC_RUNNER_VERSION
    )
    if not (
        result.baseline_identity == identity
        and result.suite_id == contract.suite_id
        and result.suite_path == f"evolution/revalidation/{phase}/interventional"
        and tuple(item.case_id for item in check_cases) == tuple(item.id for item in checks)
        and all(_lifecycle(item.message) for item in check_cases)
        and len({_run_grant(item.message) for item in check_cases}) == 1
        and len(metric_cases) == len(contract.metric_entries)
        and tuple(
            observation.metric for item in metric_cases for observation in item.metric_observations
        )
        == tuple(item.metric_name for item in contract.metric_entries)
        and len(result.cases) == len(check_cases) + len(metric_cases)
    ):
        raise EvolutionRevalidationInterventionalSampleError(
            "fresh_sample_existing_phase_conflict",
            f"已有 {phase.upper()} H5a 不属于当前 Fresh sample authority。",
        )


def _build_receipt(*, contract, pair, sample_index, red, green, completed_at):
    red_identity = red.result.baseline_identity
    green_identity = green.result.baseline_identity
    red_checks = tuple(
        item for item in red.result.cases if item.runner == "evolution_profile_check@1"
    )
    green_checks = tuple(
        item for item in green.result.cases if item.runner == "evolution_profile_check@1"
    )
    if not (
        red_identity is not None
        and green_identity is not None
        and red_identity.configuration == green_identity.configuration
        and red_identity.platform == green_identity.platform
        and tuple(item.case_id for item in red_checks)
        == tuple(item.case_id for item in green_checks)
    ):
        raise EvolutionRevalidationInterventionalSampleError(
            "fresh_sample_environment_mismatch",
            "RED/GREEN 的 configuration、platform 或 check set 不一致。",
        )
    platform_sha256 = _sha256_payload(red_identity.platform.model_dump(mode="json"))
    payload = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_INTERVENTIONAL_SAMPLE_POLICY,
        "workspace_root": str(Path(contract.workspace_root).resolve()),
        "contract_id": contract.contract_id,
        "contract_sha256": contract.contract_sha256,
        "validation_plan_id": contract.validation_plan_id,
        "validation_plan_sha256": contract.validation_plan_sha256,
        "source_snapshot_id": contract.source_snapshot_id,
        "source_snapshot_sha256": contract.source_snapshot_sha256,
        "sample_index": sample_index,
        "sample_seed": _sample_seed(contract.seed, sample_index),
        "red_batch_id": red.batch_id,
        "green_batch_id": green.batch_id,
        "red_result_id": red.id,
        "red_result_sha256": red.result_sha256,
        "green_result_id": green.id,
        "green_result_sha256": green.result_sha256,
        "red_identity_sha256": red.identity_sha256,
        "green_identity_sha256": green.identity_sha256,
        "platform_sha256": platform_sha256,
        "check_ids": [item.case_id for item in red_checks],
        "red_check_statuses": [item.code for item in red_checks],
        "green_check_statuses": [item.code for item in green_checks],
        "red_lifecycle_receipt_sha256": [_lifecycle(item.message) for item in red_checks],
        "green_lifecycle_receipt_sha256": [_lifecycle(item.message) for item in green_checks],
        "red_run_grant_sha256": _single_run_grant(red_checks),
        "green_run_grant_sha256": _single_run_grant(green_checks),
        "profile_trust_revalidated": True,
        "source_current_after_execution": True,
        "identical_environment_observed": True,
        "arc04_worker_used": True,
        "project_code_executed": True,
        "metrics_executed": True,
        "pair_complete": True,
        "cohort_complete": False,
        "promotion_authority": False,
        "completed_at": completed_at,
    }
    digest = _sha256_payload(payload)
    return EvolutionRevalidationInterventionalSampleReceipt.model_validate(
        {
            **payload,
            "receipt_id": f"evrevalsample_{digest[:24]}",
            "receipt_sha256": digest,
        }
    )


def _require_pair_matches_contract(pair, contract):
    plan = pair.validation_plan
    if not (
        plan.validation_plan_id == contract.validation_plan_id
        and plan.validation_plan_sha256 == contract.validation_plan_sha256
        and plan.source_snapshot_id == contract.source_snapshot_id
        and plan.source_snapshot_sha256 == contract.source_snapshot_sha256
        and plan.suite_id == contract.suite_id
        and plan.requested_samples == contract.requested_samples
        and plan.seed == contract.seed
    ):
        raise EvolutionRevalidationInterventionalSampleError(
            "fresh_sample_source_contract_mismatch",
            "Fresh Runtime Source 与 Runtime Contract 不一致。",
        )


def _batch_id(contract, phase):
    return f"evreval-{phase}-{contract.contract_sha256[:32]}"


def _sample_seed(seed, sample_index):
    return (
        int.from_bytes(
            hashlib.sha256(f"{seed}:{sample_index}".encode("ascii")).digest()[:8],
            "big",
        )
        & 0x7FFF_FFFF_FFFF_FFFF
    )


def _lifecycle(message):
    marker = "lifecycle_sha256="
    value = message.split(marker, 1)[1].split(" ", 1)[0] if marker in message else ""
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise EvolutionRevalidationInterventionalSampleError(
            "fresh_sample_lifecycle_evidence_missing",
            "Fresh sample 缺少 ARC-04 lifecycle evidence。",
        )
    return value


def _run_grant(message):
    marker = "run_grant_sha256="
    value = message.split(marker, 1)[1].split(" ", 1)[0] if marker in message else ""
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise EvolutionRevalidationInterventionalSampleError(
            "fresh_sample_run_grant_evidence_missing",
            "Fresh sample 缺少 Run Grant evidence。",
        )
    return value


def _single_run_grant(cases):
    values = {_run_grant(item.message) for item in cases}
    if len(values) != 1:
        raise EvolutionRevalidationInterventionalSampleError(
            "fresh_sample_run_grant_evidence_ambiguous",
            "Fresh sample 单 phase 绑定了多个 Run Grant。",
        )
    return values.pop()


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_interventional_samples ("
        "receipt_id TEXT PRIMARY KEY, receipt_sha256 TEXT NOT NULL UNIQUE, "
        "contract_id TEXT NOT NULL, sample_index INTEGER NOT NULL, "
        "receipt_json TEXT NOT NULL, completed_at TEXT NOT NULL, "
        "UNIQUE (contract_id, sample_index))"
    )
    await db.commit()


def _sha256_payload(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


__all__ = [
    "EVOLUTION_REVALIDATION_INTERVENTIONAL_RUNNER",
    "EVOLUTION_REVALIDATION_INTERVENTIONAL_SAMPLE_POLICY",
    "EvolutionRevalidationInterventionalSampleError",
    "EvolutionRevalidationInterventionalSampleExecutor",
    "EvolutionRevalidationInterventionalSampleReceipt",
    "EvolutionRevalidationInterventionalSampleStore",
]
