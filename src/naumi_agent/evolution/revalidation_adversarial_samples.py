"""Execute one Fresh Adversarial RED/GREEN sample pair on an exact platform."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
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
from naumi_agent.harness.eval_identity import (
    HarnessEvalConfigurationIdentity,
    HarnessEvalPlatformIdentity,
    build_eval_baseline_identity,
    capture_eval_platform_identity,
)
from naumi_agent.harness.eval_models import (
    EvalCaseStatus,
    EvalRunStatus,
    HarnessEvalCaseResult,
    HarnessEvalComparisonPolicy,
    HarnessEvalMetricObservation,
    HarnessEvalSuiteResult,
)
from naumi_agent.harness.models import HarnessCheckSpec
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.sandbox_checks import (
    HarnessSandboxCheckResult,
    HarnessSandboxCheckStatus,
)
from naumi_agent.harness.sandbox_eval import (
    HarnessSandboxEvalExecutionError,
    HarnessSandboxEvalExecutionKernel,
    HarnessSandboxEvalRunAuthority,
)
from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.store import (
    HarnessStore,
    HarnessStoreConflictError,
    HarnessStoreError,
)

EVOLUTION_REVALIDATION_ADVERSARIAL_SAMPLE_POLICY = (
    "evolution-revalidation-adversarial-sample-v1"
)
EVOLUTION_REVALIDATION_ADVERSARIAL_RUNNER = (
    "evolution_revalidation_adversarial_probe@1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
type Platform = Literal["linux", "macos", "windows"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionRevalidationAdversarialSampleReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-revalidation-adversarial-sample-v1"
    ] = EVOLUTION_REVALIDATION_ADVERSARIAL_SAMPLE_POLICY
    receipt_id: str = Field(pattern=r"^evrevaladvsample_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    contract_id: str = Field(pattern=r"^evrevalruntime_[0-9a-f]{24}$")
    contract_sha256: str = Field(pattern=_SHA256_RE)
    validation_plan_id: str = Field(pattern=r"^evrevalvplan_[0-9a-f]{24}$")
    validation_plan_sha256: str = Field(pattern=_SHA256_RE)
    source_snapshot_id: str = Field(pattern=r"^evrevalsrc_[0-9a-f]{24}$")
    source_snapshot_sha256: str = Field(pattern=_SHA256_RE)
    platform: Platform
    platform_identity: HarnessEvalPlatformIdentity
    platform_sha256: str = Field(pattern=_SHA256_RE)
    sample_index: int = Field(ge=0, le=99)
    sample_seed: int = Field(ge=0, le=9_223_372_036_854_775_807)
    run_scope: Literal["sample", "cohort"]
    red_batch_id: str = Field(min_length=1, max_length=128)
    green_batch_id: str = Field(min_length=1, max_length=128)
    red_result_sha256: str = Field(pattern=_SHA256_RE)
    green_result_sha256: str = Field(pattern=_SHA256_RE)
    red_identity_sha256: str = Field(pattern=_SHA256_RE)
    green_identity_sha256: str = Field(pattern=_SHA256_RE)
    check_ids: tuple[str, ...] = Field(min_length=1, max_length=80)
    red_statuses: tuple[
        Literal["passed", "implementation_failure", "evaluation_error"], ...
    ] = Field(min_length=1, max_length=80)
    green_statuses: tuple[
        Literal["passed", "implementation_failure", "evaluation_error"], ...
    ] = Field(min_length=1, max_length=80)
    red_lifecycle_sha256: tuple[str, ...] = Field(min_length=1, max_length=80)
    green_lifecycle_sha256: tuple[str, ...] = Field(min_length=1, max_length=80)
    red_run_grant_sha256: str = Field(pattern=_SHA256_RE)
    green_run_grant_sha256: str = Field(pattern=_SHA256_RE)
    profile_trust_revalidated: Literal[True] = True
    source_current_after_execution: Literal[True] = True
    exact_platform_worker_used: Literal[True] = True
    arc04_worker_used: Literal[True] = True
    project_code_executed: Literal[True] = True
    pair_complete: Literal[True] = True
    cohort_complete: Literal[False] = False
    comparison_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    completed_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        count = len(self.check_ids)
        if not (
            count
            == len(self.red_statuses)
            == len(self.green_statuses)
            == len(self.red_lifecycle_sha256)
            == len(self.green_lifecycle_sha256)
            and self.check_ids == tuple(sorted(set(self.check_ids)))
        ):
            raise ValueError("Fresh Adversarial sample check evidence 不完整。")
        if self.platform_identity.system != self.platform or self.platform_sha256 != (
            _sha256_payload(self.platform_identity.model_dump(mode="json"))
        ):
            raise ValueError("Fresh Adversarial platform identity 不一致。")
        if (
            self.red_identity_sha256 == self.green_identity_sha256
            or self.red_run_grant_sha256 != self.green_run_grant_sha256
        ):
            raise ValueError("Fresh Adversarial RED/GREEN identity 或 Run Grant 无效。")
        if datetime.fromisoformat(self.completed_at).utcoffset() is None:
            raise ValueError("Fresh Adversarial completed_at 必须包含 offset。")
        digest = _sha256_payload(
            self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        )
        if not hmac.compare_digest(self.receipt_sha256, digest):
            raise ValueError("Fresh Adversarial sample receipt digest 不一致。")
        if self.receipt_id != f"evrevaladvsample_{digest[:24]}":
            raise ValueError("Fresh Adversarial sample receipt identity 不一致。")
        return self


class EvolutionRevalidationAdversarialSampleError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationAdversarialSampleStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()

    async def get(self, contract_id, platform, sample_index, run_scope):
        if not self._db_path.exists():
            return None
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT receipt_json FROM evolution_revalidation_adversarial_samples "
                    "WHERE contract_id = ? AND platform = ? AND sample_index = ? "
                    "AND run_scope = ?",
                    (contract_id, platform, sample_index, run_scope),
                )
            ).fetchone()
        return None if row is None else (
            EvolutionRevalidationAdversarialSampleReceipt.model_validate_json(
                row["receipt_json"]
            )
        )

    async def record(self, receipt):
        item = EvolutionRevalidationAdversarialSampleReceipt.model_validate_json(
            receipt.model_dump_json()
        )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            dependency = await (
                await db.execute(
                    "SELECT contract_sha256 FROM evolution_revalidation_runtime_contracts "
                    "WHERE contract_id = ?",
                    (item.contract_id,),
                )
            ).fetchone()
            if dependency is None or dependency["contract_sha256"] != item.contract_sha256:
                await db.rollback()
                raise EvolutionRevalidationAdversarialSampleError(
                    "fresh_adversarial_contract_mismatch",
                    "持久化 Runtime Contract 不存在或 digest 不一致。",
                )
            existing = await (
                await db.execute(
                    "SELECT receipt_json FROM evolution_revalidation_adversarial_samples "
                    "WHERE contract_id = ? AND platform = ? AND sample_index = ? "
                    "AND run_scope = ?",
                    (item.contract_id, item.platform, item.sample_index, item.run_scope),
                )
            ).fetchone()
            if existing is not None:
                restored = EvolutionRevalidationAdversarialSampleReceipt.model_validate_json(
                    existing["receipt_json"]
                )
                await db.rollback()
                if restored != item:
                    raise EvolutionRevalidationAdversarialSampleError(
                        "fresh_adversarial_receipt_conflict",
                        "同一 Fresh Adversarial sample 已绑定不同回执。",
                    )
                return restored
            await db.execute(
                "INSERT INTO evolution_revalidation_adversarial_samples "
                "(receipt_id, receipt_sha256, contract_id, platform, sample_index, "
                "run_scope, receipt_json, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.receipt_id,
                    item.receipt_sha256,
                    item.contract_id,
                    item.platform,
                    item.sample_index,
                    item.run_scope,
                    item.model_dump_json(),
                    item.completed_at,
                ),
            )
            await db.commit()
        return item


class EvolutionRevalidationAdversarialSampleExecutor:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        harness_store: HarnessStore,
        receipt_store: EvolutionRevalidationAdversarialSampleStore,
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
        platform: Platform,
        parent_receipt_id: str,
        sample_index: int,
        run_scope: Literal["sample", "cohort"] = "sample",
        run_authority: HarnessSandboxEvalRunAuthority | None = None,
    ) -> EvolutionRevalidationAdversarialSampleReceipt:
        if run_scope not in {"sample", "cohort"}:
            raise EvolutionRevalidationAdversarialSampleError(
                "fresh_adversarial_run_scope_invalid",
                "Fresh Adversarial run scope 无效。",
            )
        existing = await self.receipt_store.get(
            contract_id, platform, sample_index, run_scope
        )
        if existing is not None:
            await self._validate_existing(existing)
            return existing
        view = await self.contract_service.inspect(
            workspace_root=self.workspace_root,
            contract_id=contract_id,
        )
        contract = view.contract
        if not view.execution_eligible:
            raise EvolutionRevalidationAdversarialSampleError(
                "fresh_adversarial_runtime_contract_not_ready",
                f"Fresh Runtime Contract 当前为 {view.current_status}。",
            )
        if platform not in contract.required_platforms:
            raise EvolutionRevalidationAdversarialSampleError(
                "fresh_adversarial_platform_not_required",
                f"平台 {platform} 不在 Runtime Contract matrix 中。",
            )
        captured = capture_eval_platform_identity()
        if captured.system != platform:
            raise EvolutionRevalidationAdversarialSampleError(
                "fresh_adversarial_platform_mismatch",
                f"当前 Worker 平台 {captured.system} 不能执行 {platform} lane。",
            )
        if isinstance(sample_index, bool) or not 0 <= sample_index < contract.requested_samples:
            raise EvolutionRevalidationAdversarialSampleError(
                "fresh_adversarial_sample_index_invalid",
                "Fresh Adversarial sample_index 超出请求范围。",
            )
        if (run_scope == "sample" and run_authority is not None) or (
            run_scope == "cohort" and run_authority is None
        ):
            raise EvolutionRevalidationAdversarialSampleError(
                "fresh_adversarial_run_scope_invalid",
                "Fresh Adversarial run scope/authority 组合无效。",
            )
        pair = await self.source_service.materialize(
            workspace_root=self.workspace_root,
            validation_plan_id=contract.validation_plan_id,
        )
        _require_pair_matches_contract(pair, contract)
        checks = await self._current_checks(
            contract,
            pair.validation_plan.profile_sha256,
        )
        parent = await self.permission_store.get(parent_receipt_id)
        if parent is None or not parent.authorizes_execution or not parent.run_id or (
            "bash_run" not in parent.delegated_tool_names
        ):
            raise EvolutionRevalidationAdversarialSampleError(
                "fresh_adversarial_parent_permission_invalid",
                "Fresh Adversarial 缺少 bash_run 父权限委托。",
            )
        owner = f"evo-reval-adv-{contract.contract_sha256[:12]}-{platform}-{sample_index}"
        lease_seconds = min(3_600, max(30, 2 * contract.profile_timeout_seconds_per_sample + 30))
        lease = grant = None
        try:
            if run_authority is None:
                lease = await self.harness_store.acquire_run_lease(
                    workspace_root=self.workspace_root,
                    run_kind=HarnessRunKind.RUNTIME,
                    run_id=parent.run_id,
                    owner_id=owner,
                    now=self.now(),
                    lease_seconds=lease_seconds,
                )
                if lease is None:
                    raise EvolutionRevalidationAdversarialSampleError(
                        "fresh_adversarial_runtime_lease_unavailable",
                        "Fresh Adversarial 无法取得 Runtime lease。",
                    )
                grant = await self.run_grant_authority.issue(
                    RunDelegationGrantRequest(
                        idempotency_key=f"{owner}-{lease.epoch}",
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
            else:
                authority = HarnessSandboxEvalRunAuthority.model_validate(
                    run_authority.model_dump(mode="json")
                )
            configuration = _configuration(contract, pair.validation_plan.profile_sha256)
            red_identity = build_eval_baseline_identity(
                self.workspace_root,
                configuration=configuration,
                platform_identity=captured,
                source_identity=pair.red_identity,
                profile_trusted=True,
            )
            green_identity = build_eval_baseline_identity(
                self.workspace_root,
                configuration=configuration,
                platform_identity=captured,
                source_identity=pair.green_identity,
                profile_trusted=True,
            )
            red = await self._phase(
                "red", contract, platform, pair, checks, red_identity, authority,
                parent_receipt_id, sample_index, run_scope,
            )
            green = await self._phase(
                "green", contract, platform, pair, checks, green_identity, authority,
                parent_receipt_id, sample_index, run_scope,
            )
            if not await pair.red.source_is_current():
                raise EvolutionRevalidationAdversarialSampleError(
                    "fresh_adversarial_source_changed",
                    "Fresh Adversarial source 在回执持久化前发生变化。",
                )
            return await self.receipt_store.record(_build_receipt(
                contract, platform, captured, sample_index, run_scope, red, green,
                completed_at=self.now(),
            ))
        except HarnessSandboxEvalExecutionError as exc:
            raise EvolutionRevalidationAdversarialSampleError(exc.code, str(exc)) from exc
        finally:
            errors = []
            if lease is not None and grant is not None:
                try:
                    await self.run_grant_authority.revoke(
                        grant_id=grant.contract.grant_id,
                        reason="fresh_adversarial_sample_finished",
                        revoked_at=self.now(),
                    )
                except BaseException as exc:
                    errors.append(exc)
            if lease is not None:
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
                        errors.append(RuntimeError("Runtime lease 未释放。"))
                except BaseException as exc:
                    errors.append(exc)
            if errors:
                raise EvolutionRevalidationAdversarialSampleError(
                    "fresh_adversarial_cleanup_failed",
                    "; ".join(str(item) for item in errors)[:300],
                )

    async def _current_checks(self, contract, profile_sha256):
        status = await self.profile_service.status()
        return validate_revalidation_adversarial_profile(
            status,
            contract,
            profile_sha256,
        )

    async def _phase(
        self, phase, contract, platform, pair, checks, identity, authority,
        parent_receipt_id, sample_index, run_scope,
    ):
        batch_id = adversarial_batch_id(contract, platform, phase, run_scope)
        existing = await self.harness_store.get_eval_result(
            self.workspace_root, batch_id, contract.suite_id, sample_index
        )
        if existing is not None:
            _validate_stored(existing, contract, platform, phase, checks, identity, run_scope)
            return existing
        source = pair.red if phase == "red" else pair.green
        results = await self.sandbox_eval_kernel.execute(
            lane="adversarial",
            authority_key=_authority_key(contract, platform, run_scope),
            parent_receipt_id=parent_receipt_id,
            sample_index=sample_index,
            checks=checks,
            profile_digest=pair.validation_plan.profile_sha256,
            profile_is_current=lambda: self._profile_is_current(
                contract,
                pair.validation_plan.profile_sha256,
            ),
            source=source,
            run_authority=authority,
        )
        suite = _suite(
            contract,
            platform,
            phase,
            identity,
            checks,
            results,
            authority.grant_sha256,
            run_scope,
        )
        try:
            return await self.harness_store.record_eval_result(
                workspace_root=self.workspace_root,
                batch_id=batch_id,
                sample_index=sample_index,
                result=suite,
                created_at=self.now(),
            )
        except HarnessStoreConflictError as exc:
            raise EvolutionRevalidationAdversarialSampleError(
                "fresh_adversarial_result_conflict",
                "同一 Fresh Adversarial phase/sample 已存在不同 H5a。",
            ) from exc
        except HarnessStoreError as exc:
            raise EvolutionRevalidationAdversarialSampleError(
                "fresh_adversarial_result_store_failed",
                "Fresh Adversarial H5a 无法持久化。",
            ) from exc

    async def _profile_is_current(self, contract, profile_sha256):
        try:
            await self._current_checks(contract, profile_sha256)
        except EvolutionRevalidationAdversarialSampleError:
            return False
        return True

    async def _validate_existing(self, receipt):
        view = await self.contract_service.inspect(
            workspace_root=self.workspace_root,
            contract_id=receipt.contract_id,
        )
        if not view.execution_eligible or view.contract.contract_sha256 != receipt.contract_sha256:
            raise EvolutionRevalidationAdversarialSampleError(
                "fresh_adversarial_existing_stale", "Fresh Adversarial receipt 已 stale。"
            )
        pair = await self.source_service.materialize(
            workspace_root=self.workspace_root,
            validation_plan_id=view.contract.validation_plan_id,
        )
        _require_pair_matches_contract(pair, view.contract)
        checks = await self._current_checks(
            view.contract,
            pair.validation_plan.profile_sha256,
        )
        configuration = _configuration(view.contract, pair.validation_plan.profile_sha256)
        red_identity = build_eval_baseline_identity(
            self.workspace_root, configuration=configuration,
            platform_identity=receipt.platform_identity,
            source_identity=pair.red_identity, profile_trusted=True,
        )
        green_identity = build_eval_baseline_identity(
            self.workspace_root, configuration=configuration,
            platform_identity=receipt.platform_identity,
            source_identity=pair.green_identity, profile_trusted=True,
        )
        for phase, identity, digest in (
            ("red", red_identity, receipt.red_result_sha256),
            ("green", green_identity, receipt.green_result_sha256),
        ):
            stored = await self.harness_store.get_eval_result(
                self.workspace_root,
                adversarial_batch_id(view.contract, receipt.platform, phase, receipt.run_scope),
                view.contract.suite_id,
                receipt.sample_index,
            )
            if stored is None or stored.result_sha256 != digest:
                raise EvolutionRevalidationAdversarialSampleError(
                    "fresh_adversarial_existing_evidence_missing",
                    "Fresh Adversarial H5a evidence 缺失。",
                )
            _validate_stored(
                stored, view.contract, receipt.platform, phase, checks, identity,
                receipt.run_scope,
            )


def validate_revalidation_adversarial_profile(status, contract, profile_sha256):
    """Select the exact current Profile checks bound by a Runtime Contract."""
    if not status.trusted:
        raise EvolutionRevalidationAdversarialSampleError(
            "fresh_adversarial_profile_untrusted",
            "Harness Profile 信任已失效。",
        )
    if status.profile_digest != profile_sha256:
        raise EvolutionRevalidationAdversarialSampleError(
            "fresh_adversarial_profile_drifted",
            "Harness Profile 已偏离 Fresh Validation Plan。",
        )
    profile = status.snapshot.profile
    if profile is None:
        raise EvolutionRevalidationAdversarialSampleError(
            "fresh_adversarial_profile_missing", "Harness Profile 不存在。"
        )
    by_id = {item.id: item for item in profile.checks}
    checks = []
    for binding in contract.probe_checks:
        check = by_id.get(binding.check_id)
        if check is None or not _check_matches(check, binding):
            raise EvolutionRevalidationAdversarialSampleError(
                "fresh_adversarial_profile_check_drifted",
                f"Adversarial check {binding.check_id} 已漂移。",
            )
        checks.append(check)
    if not checks or {item.check_id for item in contract.probe_coverage} != {
        item.id for item in checks
    }:
        raise EvolutionRevalidationAdversarialSampleError(
            "fresh_adversarial_probe_coverage_invalid",
            "Runtime Contract probe coverage 无法由当前 checks 完整覆盖。",
        )
    return tuple(checks)


def _configuration(contract, profile_sha256):
    policy = HarnessEvalComparisonPolicy()
    return HarnessEvalConfigurationIdentity.create(
        suite_id=contract.suite_id,
        suite_sha256=contract.contract_sha256,
        profile_sha256=profile_sha256,
        policy_sha256=policy.sha256,
        runner_version=EVOLUTION_REVALIDATION_ADVERSARIAL_RUNNER,
        repetitions=contract.requested_samples,
        live=False,
    )


def _suite(contract, platform, phase, identity, checks, results, grant, run_scope):
    cases = tuple(
        _case(
            result,
            next(
                item
                for item in contract.probe_checks
                if item.check_id == result.check_id
            ),
            grant,
            run_scope,
        )
        for result in results
    )
    status = (
        EvalRunStatus.EVALUATION_ERROR
        if any(item.status is EvalCaseStatus.EVALUATION_ERROR for item in cases)
        else EvalRunStatus.FAILED
        if any(item.status is EvalCaseStatus.IMPLEMENTATION_FAILURE for item in cases)
        else EvalRunStatus.PASSED
    )
    return HarnessEvalSuiteResult(
        suite_id=contract.suite_id,
        title=f"Fresh adversarial {platform} {phase.upper()} sample",
        suite_path=f"evolution/revalidation/adversarial/{platform}/{phase}",
        suite_sha256=contract.contract_sha256,
        status=status,
        cases=cases,
        code=f"fresh_adversarial_{phase}_{status.value}",
        message="Fresh adversarial probes 已通过 ARC-04 Worker 执行。",
        comparison_policy=HarnessEvalComparisonPolicy(),
        baseline_identity=identity,
        duration_ms=sum(item.duration_ms for item in results),
    )


def _case(result: HarnessSandboxCheckResult, binding, grant, run_scope):
    status = (
        EvalCaseStatus.PASSED
        if result.status is HarnessSandboxCheckStatus.PASSED
        else EvalCaseStatus.IMPLEMENTATION_FAILURE
        if result.status is HarnessSandboxCheckStatus.FAILED
        else EvalCaseStatus.EVALUATION_ERROR
    )
    metric = f"adversarial.{result.check_id}.exit_zero"
    observations = () if status is EvalCaseStatus.EVALUATION_ERROR else (
        HarnessEvalMetricObservation(
            metric=metric,
            value=1.0 if status is EvalCaseStatus.PASSED else 0.0,
            unit="scalar",
            direction="increase",
            target=1.0,
            primary=True,
        ),
    )
    return HarnessEvalCaseResult(
        case_id=result.check_id,
        runner=EVOLUTION_REVALIDATION_ADVERSARIAL_RUNNER,
        status=status,
        primary_metric=metric if observations else "",
        metric_observations=observations,
        code=result.status.value,
        message=(
            f"{result.message} lifecycle_sha256={result.lifecycle_receipt_sha256 or 'missing'} "
            f"run_scope={run_scope} run_grant_sha256={grant} "
            f"probe_kinds={','.join(binding.probes)}"
        ),
        duration_ms=result.duration_ms,
    )


def _validate_stored(stored, contract, platform, phase, checks, identity, run_scope):
    result = stored.result
    expected = {item.check_id: item for item in contract.probe_checks}
    if not (
        stored.batch_id == adversarial_batch_id(contract, platform, phase, run_scope)
        and stored.suite_id == contract.suite_id
        and result.suite_sha256 == contract.contract_sha256
        and result.baseline_identity == identity
        and tuple(item.case_id for item in result.cases) == tuple(item.id for item in checks)
        and all(item.runner == EVOLUTION_REVALIDATION_ADVERSARIAL_RUNNER for item in result.cases)
        and all(_case_is_bound(item, expected[item.case_id]) for item in result.cases)
        and all(f"run_scope={run_scope}" in item.message for item in result.cases)
        and len({_grant(item.message) for item in result.cases}) == 1
        and None not in {_grant(item.message) for item in result.cases}
        and all(_lifecycle(item.message) for item in result.cases)
    ):
        raise EvolutionRevalidationAdversarialSampleError(
            "fresh_adversarial_stored_result_invalid",
            "Fresh Adversarial H5a 与当前 authority 不一致。",
        )


def _build_receipt(
    contract,
    platform,
    platform_identity,
    sample_index,
    run_scope,
    red,
    green,
    *,
    completed_at,
):
    red_cases = red.result.cases
    green_cases = green.result.cases
    payload = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_ADVERSARIAL_SAMPLE_POLICY,
        "workspace_root": contract.workspace_root,
        "contract_id": contract.contract_id,
        "contract_sha256": contract.contract_sha256,
        "validation_plan_id": contract.validation_plan_id,
        "validation_plan_sha256": contract.validation_plan_sha256,
        "source_snapshot_id": contract.source_snapshot_id,
        "source_snapshot_sha256": contract.source_snapshot_sha256,
        "platform": platform,
        "platform_identity": platform_identity.model_dump(mode="json"),
        "platform_sha256": _sha256_payload(platform_identity.model_dump(mode="json")),
        "sample_index": sample_index,
        "sample_seed": _sample_seed(contract.seed, sample_index),
        "run_scope": run_scope,
        "red_batch_id": red.batch_id,
        "green_batch_id": green.batch_id,
        "red_result_sha256": red.result_sha256,
        "green_result_sha256": green.result_sha256,
        "red_identity_sha256": red.identity_sha256,
        "green_identity_sha256": green.identity_sha256,
        "check_ids": [item.case_id for item in red_cases],
        "red_statuses": [item.status.value for item in red_cases],
        "green_statuses": [item.status.value for item in green_cases],
        "red_lifecycle_sha256": [_lifecycle(item.message) for item in red_cases],
        "green_lifecycle_sha256": [_lifecycle(item.message) for item in green_cases],
        "red_run_grant_sha256": _single_grant(red_cases),
        "green_run_grant_sha256": _single_grant(green_cases),
        "profile_trust_revalidated": True,
        "source_current_after_execution": True,
        "exact_platform_worker_used": True,
        "arc04_worker_used": True,
        "project_code_executed": True,
        "pair_complete": True,
        "cohort_complete": False,
        "comparison_authority": False,
        "promotion_authority": False,
        "completed_at": completed_at,
    }
    digest = _sha256_payload(payload)
    return EvolutionRevalidationAdversarialSampleReceipt.model_validate({
        **payload,
        "receipt_id": f"evrevaladvsample_{digest[:24]}",
        "receipt_sha256": digest,
    })


def _check_matches(check: HarnessCheckSpec, binding) -> bool:
    return bool(
        _sha256_payload(check.model_dump(mode="json")) == binding.spec_sha256
        and _sha256_payload(list(check.argv)) == binding.argv_sha256
        and check.timeout_seconds == binding.timeout_seconds
        and check.adversarial_probes == binding.probes
    )


def _case_is_bound(case, binding) -> bool:
    expected_metric = f"adversarial.{case.case_id}.exit_zero"
    observations = case.metric_observations
    return bool(
        f"probe_kinds={','.join(binding.probes)}" in case.message
        and (
            case.status is EvalCaseStatus.EVALUATION_ERROR
            and not observations
            and not case.primary_metric
            or len(observations) == 1
            and observations[0].metric == expected_metric
            and observations[0].unit == "scalar"
            and observations[0].direction == "increase"
            and observations[0].target == 1.0
            and observations[0].value
            == (1.0 if case.status is EvalCaseStatus.PASSED else 0.0)
            and case.primary_metric == expected_metric
        )
    )


def _require_pair_matches_contract(pair, contract):
    plan = pair.validation_plan
    if not (
        plan.validation_plan_id == contract.validation_plan_id
        and plan.validation_plan_sha256 == contract.validation_plan_sha256
        and plan.source_snapshot_id == contract.source_snapshot_id
        and plan.source_snapshot_sha256 == contract.source_snapshot_sha256
        and plan.candidate_id == contract.candidate_id
        and plan.candidate_revision == contract.candidate_revision
        and plan.suite_id == contract.suite_id
        and plan.requested_samples == contract.requested_samples
        and plan.seed == contract.seed
        and plan.required_platforms == contract.required_platforms
    ):
        raise EvolutionRevalidationAdversarialSampleError(
            "fresh_adversarial_source_contract_mismatch",
            "Fresh Runtime Source 与 Adversarial Contract 不一致。",
        )


def _sample_seed(seed, sample_index):
    return (
        int.from_bytes(
            hashlib.sha256(f"{seed}:{sample_index}".encode("ascii")).digest()[:8],
            "big",
        )
        & 0x7FFF_FFFF_FFFF_FFFF
    )


def adversarial_batch_id(contract, platform, phase, run_scope):
    return f"evreval-adv-{platform}-{run_scope}-{phase}-{contract.contract_sha256[:24]}"


def revalidation_adversarial_configuration(contract, profile_sha256):
    """Build the canonical H5a configuration identity for remote ingestion."""
    return _configuration(contract, profile_sha256)


def revalidation_adversarial_sample_seed(seed, sample_index):
    """Derive the canonical deterministic sample seed."""
    return _sample_seed(seed, sample_index)


def require_revalidation_adversarial_source_pair(pair, contract):
    """Fail closed unless an immutable source pair matches the Runtime Contract."""
    _require_pair_matches_contract(pair, contract)


def validate_revalidation_adversarial_h5a(
    stored,
    contract,
    platform,
    phase,
    checks,
    identity,
    run_scope="cohort",
):
    """Revalidate one stored or proposed H5a result against exact authority."""
    _validate_stored(
        stored,
        contract,
        platform,
        phase,
        checks,
        identity,
        run_scope,
    )


def build_revalidation_adversarial_sample_receipt(
    contract,
    platform_identity,
    sample_index,
    red,
    green,
    *,
    completed_at,
    run_scope="cohort",
):
    """Build the same pair receipt used by the local adversarial executor."""
    return _build_receipt(
        contract,
        platform_identity.system,
        platform_identity,
        sample_index,
        run_scope,
        red,
        green,
        completed_at=completed_at,
    )


def _authority_key(contract, platform, run_scope):
    return hashlib.sha256(
        f"{contract.contract_sha256}:{platform}:{run_scope}".encode()
    ).hexdigest()


def _lifecycle(message):
    marker = "lifecycle_sha256="
    value = message.split(marker, 1)[1].split(" ", 1)[0] if marker in message else ""
    return value if _is_sha256(value) else None


def _grant(message):
    marker = "run_grant_sha256="
    value = message.split(marker, 1)[1].split(" ", 1)[0] if marker in message else ""
    return value if _is_sha256(value) else None


def _is_sha256(value):
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _single_grant(cases):
    values = {_grant(item.message) for item in cases}
    if len(values) != 1 or None in values:
        raise EvolutionRevalidationAdversarialSampleError(
            "fresh_adversarial_grant_ambiguous", "Fresh Adversarial Run Grant 不唯一。"
        )
    return values.pop()


async def _ensure_schema(db):
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_adversarial_samples ("
        "receipt_id TEXT PRIMARY KEY, receipt_sha256 TEXT NOT NULL UNIQUE, "
        "contract_id TEXT NOT NULL, "
        "platform TEXT NOT NULL CHECK (platform IN ('linux', 'macos', 'windows')), "
        "sample_index INTEGER NOT NULL, "
        "run_scope TEXT NOT NULL CHECK (run_scope IN ('sample', 'cohort')), "
        "receipt_json TEXT NOT NULL, completed_at TEXT NOT NULL, "
        "UNIQUE (contract_id, platform, sample_index, run_scope))"
    )
    await db.commit()


def _sha256_payload(payload):
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()


__all__ = [
    "EVOLUTION_REVALIDATION_ADVERSARIAL_RUNNER",
    "EVOLUTION_REVALIDATION_ADVERSARIAL_SAMPLE_POLICY",
    "EvolutionRevalidationAdversarialSampleError",
    "EvolutionRevalidationAdversarialSampleExecutor",
    "EvolutionRevalidationAdversarialSampleReceipt",
    "EvolutionRevalidationAdversarialSampleStore",
    "adversarial_batch_id",
    "build_revalidation_adversarial_sample_receipt",
    "require_revalidation_adversarial_source_pair",
    "revalidation_adversarial_configuration",
    "revalidation_adversarial_sample_seed",
    "validate_revalidation_adversarial_h5a",
    "validate_revalidation_adversarial_profile",
]
