"""Durable Harness validation for exact or rebased Evolution replay results."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.experiment_leases import (
    EvolutionExperimentLeaseStore,
    ExperimentLeaseState,
    ExperimentWorktreeLease,
)
from naumi_agent.evolution.promotion_package_inputs import (
    EvolutionPromotionPackageInput,
    EvolutionPromotionPackageInputStore,
)
from naumi_agent.evolution.revalidation_execution import (
    EvolutionRevalidationExecutionOutcome,
    EvolutionRevalidationExecutionService,
)
from naumi_agent.evolution.revalidation_rebases import (
    EvolutionRevalidationRebaseOutcome,
    EvolutionRevalidationRebaseStatus,
    _merge_file,
)
from naumi_agent.evolution.revalidation_replays import (
    EvolutionRevalidationReplayReceipt,
    _capture_candidate,
)
from naumi_agent.evolution.revalidation_requests import (
    EvolutionRevalidationRequestService,
)
from naumi_agent.harness.evolution_revalidation import (
    HarnessEvolutionRevalidationPlan,
    HarnessEvolutionRevalidationRun,
    HarnessEvolutionRevalidationRunError,
    HarnessEvolutionRevalidationSource,
    git_revision_tree_sha256,
    overlay_set_sha256,
)
from naumi_agent.harness.sandbox_checks import HarnessSandboxSourceOverlay

EVOLUTION_REVALIDATION_VALIDATION_POLICY = "evolution-revalidation-validation-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_CLAIM_SECONDS = 300
_MAX_RECEIPT_BYTES = 512 * 1_024
_MAX_SOURCE_BYTES = 2 * 1_024 * 1_024


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionRevalidationValidationStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"


class EvolutionRevalidationCheckEvidence(_StrictModel):
    order: int = Field(ge=1, le=80)
    check_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    status: str = Field(min_length=1, max_length=64)
    source_tree_sha256: str = Field(pattern=_SHA256_RE)
    snapshot_manifest_sha256: str = Field(pattern=_SHA256_RE)
    profile_sha256: str = Field(pattern=_SHA256_RE)
    job_id: str = Field(min_length=1, max_length=256)
    lifecycle_receipt_sha256: str = Field(pattern=_SHA256_RE)
    output_sha256: str = Field(pattern=_SHA256_RE)
    exit_code: int | None = None
    duration_ms: int = Field(ge=0)


class EvolutionRevalidationValidationReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-validation-v1"] = (
        EVOLUTION_REVALIDATION_VALIDATION_POLICY
    )
    receipt_id: str = Field(pattern=r"^evrevalidate_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    request_id: str = Field(pattern=r"^evrevalidation_[0-9a-f]{24}$")
    request_sha256: str = Field(pattern=_SHA256_RE)
    execution_kind: Literal["exact", "rebase"]
    execution_id: str = Field(pattern=r"^(?:evreplay|evrebase)_[0-9a-f]{24}$")
    execution_sha256: str = Field(pattern=_SHA256_RE)
    target_head: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    target_tree_sha256: str = Field(pattern=_SHA256_RE)
    overlay_source_sha256: str = Field(pattern=_SHA256_RE)
    profile_sha256: str = Field(pattern=_SHA256_RE)
    harness_plan_sha256: str = Field(pattern=_SHA256_RE)
    epoch: int = Field(ge=1)
    status: EvolutionRevalidationValidationStatus
    checks: tuple[EvolutionRevalidationCheckEvidence, ...] = Field(max_length=80)
    failure_code: str = Field(default="", pattern=r"^(?:|[a-z][a-z0-9_.-]{0,127})$")
    all_checks_passed: bool
    project_code_executed: bool
    new_validation_evidence_issued: bool
    old_evidence_invalidated: Literal[False] = False
    promotion_authority: Literal[False] = False
    created_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _receipt_is_exact(self) -> Self:
        if tuple(item.order for item in self.checks) != tuple(
            range(1, len(self.checks) + 1)
        ):
            raise ValueError("Revalidation checks 顺序不连续。")
        ids = tuple(item.check_id for item in self.checks)
        if len(ids) != len(set(ids)):
            raise ValueError("Revalidation checks 不得重复。")
        executed = bool(self.checks) and all(
            item.job_id and item.lifecycle_receipt_sha256 for item in self.checks
        )
        observed_passed = executed and all(
            item.status == "passed" for item in self.checks
        )
        if self.project_code_executed != executed:
            raise ValueError("Revalidation project execution 投影不一致。")
        if self.all_checks_passed != (
            self.status is EvolutionRevalidationValidationStatus.PASSED
        ):
            raise ValueError("Revalidation passed 投影不一致。")
        if self.new_validation_evidence_issued != executed:
            raise ValueError("Revalidation evidence 投影不一致。")
        if self.status is EvolutionRevalidationValidationStatus.PASSED:
            if not observed_passed or self.failure_code:
                raise ValueError("Passed Revalidation Receipt 不完整。")
        elif self.all_checks_passed or not self.failure_code:
            raise ValueError("Failed Revalidation Receipt 不完整。")
        if datetime.fromisoformat(self.created_at).utcoffset() is None:
            raise ValueError("Revalidation created_at 必须包含 UTC offset。")
        digest = _sha256_payload(
            self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        )
        if not hmac.compare_digest(self.receipt_sha256, digest):
            raise ValueError("Revalidation Receipt 摘要不一致。")
        if self.receipt_id != f"evrevalidate_{digest[:24]}":
            raise ValueError("Revalidation Receipt identity 不一致。")
        return self


class EvolutionRevalidationValidationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionHarnessRevalidationRunner(Protocol):
    async def prepare_evolution_revalidation(
        self, *, changed_paths: tuple[str, ...]
    ) -> HarnessEvolutionRevalidationPlan: ...

    async def run_evolution_revalidation(
        self,
        *,
        request_id: str,
        authority_sha256: str,
        plan: HarnessEvolutionRevalidationPlan,
        source: HarnessEvolutionRevalidationSource,
        source_is_current: Callable[[], Awaitable[bool]],
    ) -> HarnessEvolutionRevalidationRun: ...


@dataclass(frozen=True, slots=True)
class _PreparedValidationSource:
    execution: EvolutionRevalidationExecutionOutcome
    request_sha256: str
    package: EvolutionPromotionPackageInput
    lease: ExperimentWorktreeLease
    source: HarnessEvolutionRevalidationSource
    plan: HarnessEvolutionRevalidationPlan


class _Claim(_StrictModel):
    authority_sha256: str
    epoch: int
    owner_token: str


class EvolutionRevalidationValidationStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()

    async def claim(
        self,
        *,
        authority_sha256: str,
        request_id: str,
        execution_sha256: str,
        profile_sha256: str,
        now: datetime,
    ) -> tuple[_Claim | None, EvolutionRevalidationValidationReceipt | None]:
        current = _aware(now)
        owner = secrets.token_hex(32)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT * FROM evolution_revalidation_validations "
                    "WHERE authority_sha256 = ?",
                    (authority_sha256,),
                )
            ).fetchone()
            if row is not None and row["receipt_json"]:
                receipt = EvolutionRevalidationValidationReceipt.model_validate_json(
                    row["receipt_json"]
                )
                if not (
                    receipt.request_id == request_id
                    and receipt.execution_sha256 == execution_sha256
                    and receipt.profile_sha256 == profile_sha256
                    and _receipt_authority_sha256(receipt) == authority_sha256
                ):
                    await db.rollback()
                    raise EvolutionRevalidationValidationError(
                        "revalidation_validation_store_corrupt",
                        "Revalidation Validation Receipt 与索引 authority 不一致。",
                    )
                await db.rollback()
                return None, receipt
            if row is not None and datetime.fromisoformat(row["expires_at"]) > current:
                await db.rollback()
                raise EvolutionRevalidationValidationError(
                    "revalidation_validation_claim_busy",
                    "同一 Revalidation validation 正由另一个执行者处理。",
                )
            epoch = 1 if row is None else int(row["epoch"]) + 1
            expires = (current + timedelta(seconds=_CLAIM_SECONDS)).isoformat()
            owner_sha = hashlib.sha256(owner.encode()).hexdigest()
            if row is None:
                await db.execute(
                    "INSERT INTO evolution_revalidation_validations "
                    "(authority_sha256, request_id, execution_sha256, profile_sha256, "
                    "epoch, owner_sha256, expires_at, receipt_json, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, '', ?)",
                    (
                        authority_sha256,
                        request_id,
                        execution_sha256,
                        profile_sha256,
                        epoch,
                        owner_sha,
                        expires,
                        current.isoformat(),
                    ),
                )
            else:
                if not (
                    row["request_id"] == request_id
                    and row["execution_sha256"] == execution_sha256
                    and row["profile_sha256"] == profile_sha256
                ):
                    await db.rollback()
                    raise EvolutionRevalidationValidationError(
                        "revalidation_validation_claim_conflict",
                        "Revalidation validation authority 已绑定不同输入。",
                    )
                await db.execute(
                    "UPDATE evolution_revalidation_validations SET epoch = ?, "
                    "owner_sha256 = ?, expires_at = ?, updated_at = ? "
                    "WHERE authority_sha256 = ?",
                    (epoch, owner_sha, expires, current.isoformat(), authority_sha256),
                )
            await db.commit()
        return _Claim(
            authority_sha256=authority_sha256,
            epoch=epoch,
            owner_token=owner,
        ), None

    async def finish(
        self,
        *,
        claim: _Claim,
        receipt: EvolutionRevalidationValidationReceipt,
        now: datetime,
    ) -> EvolutionRevalidationValidationReceipt:
        if _receipt_authority_sha256(receipt) != claim.authority_sha256:
            raise EvolutionRevalidationValidationError(
                "revalidation_validation_receipt_authority_mismatch",
                "Revalidation Validation Receipt 与 claim authority 不一致。",
            )
        encoded = receipt.model_dump_json()
        if len(encoded.encode()) > _MAX_RECEIPT_BYTES:
            raise EvolutionRevalidationValidationError(
                "revalidation_validation_receipt_oversized",
                "Revalidation Validation Receipt 超过 512 KiB 上限。",
            )
        owner_sha = hashlib.sha256(claim.owner_token.encode()).hexdigest()
        async with aiosqlite.connect(self._db_path) as db:
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "UPDATE evolution_revalidation_validations SET receipt_json = ?, "
                "updated_at = ? WHERE authority_sha256 = ? AND epoch = ? "
                "AND owner_sha256 = ? AND receipt_json = ''",
                (
                    encoded,
                    _aware(now).isoformat(),
                    claim.authority_sha256,
                    claim.epoch,
                    owner_sha,
                ),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise EvolutionRevalidationValidationError(
                    "revalidation_validation_fenced",
                    "Revalidation validation 执行者 epoch 已失效。",
                )
            await db.commit()
        return receipt

    async def get_by_request(
        self,
        request_id: str,
    ) -> EvolutionRevalidationValidationReceipt | None:
        """Return the terminal receipt for one request without claiming execution."""
        if re.fullmatch(r"evrevalidation_[0-9a-f]{24}", str(request_id)) is None:
            raise ValueError("Revalidation Request ID 格式无效。")
        if not self._db_path.exists():
            return None
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT receipt_json FROM evolution_revalidation_validations "
                    "WHERE request_id = ? AND receipt_json != '' "
                    "ORDER BY updated_at DESC, rowid DESC LIMIT 1",
                    (request_id,),
                )
            ).fetchone()
        if row is None:
            return None
        return EvolutionRevalidationValidationReceipt.model_validate_json(
            row["receipt_json"]
        )


class EvolutionRevalidationValidationService:
    def __init__(
        self,
        *,
        execution_service: EvolutionRevalidationExecutionService,
        request_service: EvolutionRevalidationRequestService,
        package_input_store: EvolutionPromotionPackageInputStore,
        lease_store: EvolutionExperimentLeaseStore,
        harness: EvolutionHarnessRevalidationRunner,
        store: EvolutionRevalidationValidationStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._execution_service = execution_service
        self._request_service = request_service
        self._package_input_store = package_input_store
        self._lease_store = lease_store
        self._harness = harness
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))

    async def execute(
        self,
        *,
        workspace_root: str | Path,
        request_id: str,
    ) -> EvolutionRevalidationValidationReceipt:
        prepared = await self._prepare_source(
            workspace_root=workspace_root, request_id=request_id
        )
        execution = prepared.execution
        package = prepared.package
        lease = prepared.lease
        source = prepared.source
        plan = prepared.plan
        now = _aware(self._clock())
        execution_id, execution_sha = _execution_identity(execution)
        authority_sha = _sha256_payload(
            {
                "policy": EVOLUTION_REVALIDATION_VALIDATION_POLICY,
                "request_sha256": prepared.request_sha256,
                "execution_sha256": execution_sha,
                "overlay_source_sha256": source.overlay_source_sha256,
                "harness_plan_sha256": plan.plan_sha256,
            }
        )
        claim, terminal = await self._store.claim(
            authority_sha256=authority_sha,
            request_id=request_id,
            execution_sha256=execution_sha,
            profile_sha256=plan.profile_sha256,
            now=now,
        )
        if terminal is not None:
            return terminal
        assert claim is not None

        async def source_is_current() -> bool:
            try:
                current = await self._request_service.inspect(
                    workspace_root=workspace_root,
                    request_id=request_id,
                )
                current_lease = await self._lease_store.get(
                    package.experiment_contract_id
                )
                return (
                    current.execution_eligible
                    and current.current_target_head == source.revision
                    and current_lease == lease
                )
            except (OSError, TypeError, ValueError):
                return False

        try:
            run = await self._harness.run_evolution_revalidation(
                request_id=request_id,
                authority_sha256=authority_sha,
                plan=plan,
                source=source,
                source_is_current=source_is_current,
            )
            receipt = _build_receipt(
                view_request_sha256=prepared.request_sha256,
                request_id=request_id,
                execution=execution,
                execution_id=execution_id,
                execution_sha256=execution_sha,
                source=source,
                plan=plan,
                epoch=claim.epoch,
                run=run,
                now=self._clock(),
            )
        except HarnessEvolutionRevalidationRunError as exc:
            receipt = _build_failed_receipt(
                view_request_sha256=prepared.request_sha256,
                request_id=request_id,
                execution=execution,
                execution_id=execution_id,
                execution_sha256=execution_sha,
                source=source,
                plan=plan,
                epoch=claim.epoch,
                failure_code=exc.code,
                run=HarnessEvolutionRevalidationRun(
                    run_id=exc.run_id,
                    results=exc.partial_results,
                ),
                now=self._clock(),
            )
        except (OSError, subprocess.SubprocessError, TypeError, ValueError) as exc:
            receipt = _build_failed_receipt(
                view_request_sha256=prepared.request_sha256,
                request_id=request_id,
                execution=execution,
                execution_id=execution_id,
                execution_sha256=execution_sha,
                source=source,
                plan=plan,
                epoch=claim.epoch,
                failure_code=getattr(exc, "code", "harness_revalidation_failed"),
                now=self._clock(),
            )
        return await self._store.finish(
            claim=claim,
            receipt=receipt,
            now=self._clock(),
        )

    async def materialize_current_source(
        self,
        *,
        workspace_root: str | Path,
        request_id: str,
    ) -> tuple[HarnessEvolutionRevalidationSource, HarnessEvolutionRevalidationPlan]:
        """Rebuild the exact current source without executing Harness checks."""
        prepared = await self._prepare_source(
            workspace_root=workspace_root,
            request_id=request_id,
        )
        return prepared.source, prepared.plan

    async def _prepare_source(
        self,
        *,
        workspace_root: str | Path,
        request_id: str,
    ) -> _PreparedValidationSource:
        execution = await self._execution_service.execute(
            workspace_root=workspace_root,
            request_id=request_id,
        )
        _require_successful_execution(execution)
        view = await self._request_service.inspect(
            workspace_root=workspace_root,
            request_id=request_id,
        )
        package_view = await self._package_input_store.get(
            view.request.promotion_input_id
        )
        if package_view is None or not package_view.promotion_review_eligible:
            raise EvolutionRevalidationValidationError(
                "revalidation_validation_package_ineligible",
                "Promotion Package Input 当前不可用于再验证。",
            )
        package = package_view.package_input
        lease = await self._lease_store.get(package.experiment_contract_id)
        if lease is None:
            raise EvolutionRevalidationValidationError(
                "revalidation_validation_lease_missing",
                "Candidate Lease 不存在，无法重新构造验证源码。",
            )
        _require_live_lease(lease, package, _aware(self._clock()))
        source = _materialize_source(
            Path(workspace_root).resolve(strict=True),
            execution,
            package,
            lease,
        )
        plan = await self._harness.prepare_evolution_revalidation(
            changed_paths=tuple(item.path for item in package.patch.files)
        )
        return _PreparedValidationSource(
            execution=execution,
            request_sha256=view.request.request_sha256,
            package=package,
            lease=lease,
            source=source,
            plan=plan,
        )


def _materialize_source(
    workspace: Path,
    execution: EvolutionRevalidationExecutionOutcome,
    package: EvolutionPromotionPackageInput,
    lease: ExperimentWorktreeLease,
) -> HarnessEvolutionRevalidationSource:
    candidate = _capture_candidate(
        Path(lease.worktree_path).resolve(strict=True),
        package.patch.files,
        package.baseline.baseline_commit,
    )
    target_head = (
        execution.target_head
        if isinstance(execution, EvolutionRevalidationReplayReceipt)
        else execution.current_target_head
    )
    by_path = {item.path: item for item in execution.files}
    overlays: list[HarnessSandboxSourceOverlay] = []
    for item in package.patch.files:
        baseline, candidate_bytes, candidate_executable = candidate[item.path]
        evidence = by_path.get(item.path)
        if evidence is None:
            raise EvolutionRevalidationValidationError(
                "revalidation_validation_file_evidence_missing",
                "Replay/Rebase artifact 缺少批准文件证据。",
            )
        if isinstance(execution, EvolutionRevalidationReplayReceipt):
            result = candidate_bytes
            executable = candidate_executable
            expected_sha = evidence.replay_sha256
        else:
            current, current_executable = _target_blob(
                workspace,
                target_head,
                item.path,
            )
            strategy, result = _merge_file(
                operation=item.operation,
                baseline=baseline,
                current=current,
                candidate=candidate_bytes,
                mode_compatible=(
                    current is None or current_executable == candidate_executable
                ),
            )
            if result is None or strategy != evidence.merge_strategy:
                raise EvolutionRevalidationValidationError(
                    "revalidation_validation_rebase_not_reproducible",
                    "Rebase result 已无法按原策略重新构造。",
                )
            executable = candidate_executable
            expected_sha = evidence.result_sha256
        digest = hashlib.sha256(result).hexdigest()
        if expected_sha is None or not hmac.compare_digest(digest, expected_sha):
            raise EvolutionRevalidationValidationError(
                "revalidation_validation_overlay_digest_mismatch",
                "重新构造的验证源码与 Replay/Rebase artifact 不一致。",
            )
        overlays.append(
            HarnessSandboxSourceOverlay(
                path=item.path,
                content=result,
                sha256=digest,
                executable=executable,
            )
        )
    overlay_tuple = tuple(overlays)
    return HarnessEvolutionRevalidationSource(
        revision=target_head,
        revision_tree_sha256=git_revision_tree_sha256(workspace, target_head),
        overlays=overlay_tuple,
        overlay_source_sha256=overlay_set_sha256(overlay_tuple),
    )


def _target_blob(
    workspace: Path,
    revision: str,
    path: str,
) -> tuple[bytes | None, bool]:
    completed = subprocess.run(
        ["git", "ls-tree", revision, "--", path],
        cwd=workspace,
        check=True,
        capture_output=True,
        timeout=20,
    )
    if not completed.stdout:
        return None, False
    fields = completed.stdout.split(b"\t", 1)[0].split()
    if len(fields) != 3 or fields[1] != b"blob":
        raise EvolutionRevalidationValidationError(
            "revalidation_validation_target_file_unsafe",
            "Target 文件不是普通 Git blob。",
        )
    mode = fields[0]
    if mode not in {b"100644", b"100755"}:
        raise EvolutionRevalidationValidationError(
            "revalidation_validation_target_mode_unsafe",
            "Target 文件 mode 不受支持。",
        )
    object_id = fields[2].decode("ascii", errors="strict")
    if len(object_id) not in {40, 64} or any(
        char not in "0123456789abcdef" for char in object_id
    ):
        raise EvolutionRevalidationValidationError(
            "revalidation_validation_target_object_invalid",
            "Target Git blob identity 无效。",
        )
    size = int(
        subprocess.run(
            ["git", "cat-file", "-s", object_id],
            cwd=workspace,
            check=True,
            capture_output=True,
            timeout=20,
        ).stdout
    )
    if not 0 <= size <= _MAX_SOURCE_BYTES:
        raise EvolutionRevalidationValidationError(
            "revalidation_validation_target_blob_oversized",
            "Target Git blob 超过 Revalidation 安全上限。",
        )
    content = subprocess.run(
        ["git", "cat-file", "blob", object_id],
        cwd=workspace,
        check=True,
        capture_output=True,
        timeout=20,
    ).stdout
    return content, mode == b"100755"


def _require_successful_execution(
    execution: EvolutionRevalidationExecutionOutcome,
) -> None:
    if isinstance(execution, EvolutionRevalidationReplayReceipt):
        return
    if (
        isinstance(execution, EvolutionRevalidationRebaseOutcome)
        and execution.status is EvolutionRevalidationRebaseStatus.SUCCEEDED
    ):
        return
    raise EvolutionRevalidationValidationError(
        "revalidation_validation_execution_ineligible",
        "只有成功的 Replay/Rebase artifact 可以进入 Harness 再验证。",
    )


def _require_live_lease(
    lease: ExperimentWorktreeLease,
    package: EvolutionPromotionPackageInput,
    now: datetime,
) -> None:
    if not (
        lease.state is ExperimentLeaseState.ACTIVE
        and lease.worktree_ready
        and lease.contract_id == package.experiment_contract_id
        and lease.manifest_sha256 == package.experiment_contract_sha256
        and datetime.fromisoformat(lease.expires_at) > now
    ):
        raise EvolutionRevalidationValidationError(
            "revalidation_validation_lease_ineligible",
            "Candidate Lease 已变化、过期或不再可读取。",
        )


def _execution_identity(
    execution: EvolutionRevalidationExecutionOutcome,
) -> tuple[str, str]:
    if isinstance(execution, EvolutionRevalidationReplayReceipt):
        return execution.replay_id, execution.replay_sha256
    return execution.outcome_id, execution.outcome_sha256


def _check_evidence(
    run: HarnessEvolutionRevalidationRun,
) -> tuple[EvolutionRevalidationCheckEvidence, ...]:
    return tuple(
        EvolutionRevalidationCheckEvidence(
            order=index,
            check_id=item.check_id,
            status=item.status.value,
            source_tree_sha256=item.source_tree_sha256,
            snapshot_manifest_sha256=item.snapshot_manifest_sha256,
            profile_sha256=item.profile_digest,
            job_id=item.job_id or "",
            lifecycle_receipt_sha256=item.lifecycle_receipt_sha256 or "",
            output_sha256=hashlib.sha256(item.output.encode()).hexdigest(),
            exit_code=item.exit_code,
            duration_ms=item.duration_ms,
        )
        for index, item in enumerate(run.results, start=1)
    )


def _base_payload(
    *,
    view_request_sha256: str,
    request_id: str,
    execution: EvolutionRevalidationExecutionOutcome,
    execution_id: str,
    execution_sha256: str,
    source: HarnessEvolutionRevalidationSource,
    plan: HarnessEvolutionRevalidationPlan,
    epoch: int,
    checks: tuple[EvolutionRevalidationCheckEvidence, ...],
    failure_code: str,
    now: datetime,
) -> dict[str, object]:
    passed = (
        len(checks) == len(plan.checks)
        and all(item.status == "passed" for item in checks)
        and not failure_code
    )
    executed = bool(checks)
    return {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_VALIDATION_POLICY,
        "request_id": request_id,
        "request_sha256": view_request_sha256,
        "execution_kind": (
            "exact" if isinstance(execution, EvolutionRevalidationReplayReceipt) else "rebase"
        ),
        "execution_id": execution_id,
        "execution_sha256": execution_sha256,
        "target_head": source.revision,
        "target_tree_sha256": source.revision_tree_sha256,
        "overlay_source_sha256": source.overlay_source_sha256,
        "profile_sha256": plan.profile_sha256,
        "harness_plan_sha256": plan.plan_sha256,
        "epoch": epoch,
        "status": "passed" if passed else "failed",
        "checks": [item.model_dump(mode="json") for item in checks],
        "failure_code": "" if passed else failure_code,
        "all_checks_passed": passed,
        "project_code_executed": executed,
        "new_validation_evidence_issued": executed,
        "old_evidence_invalidated": False,
        "promotion_authority": False,
        "created_at": _aware(now).isoformat(),
    }


def _build_receipt(**kwargs) -> EvolutionRevalidationValidationReceipt:
    run = kwargs.pop("run")
    checks = _check_evidence(run)
    failure = "" if all(item.status == "passed" for item in checks) else "checks_failed"
    payload = _base_payload(checks=checks, failure_code=failure, **kwargs)
    digest = _sha256_payload(payload)
    return EvolutionRevalidationValidationReceipt.model_validate(
        {**payload, "receipt_id": f"evrevalidate_{digest[:24]}", "receipt_sha256": digest}
    )


def _build_failed_receipt(**kwargs) -> EvolutionRevalidationValidationReceipt:
    run = kwargs.pop("run", None)
    checks = () if run is None else _check_evidence(run)
    payload = _base_payload(checks=checks, **kwargs)
    digest = _sha256_payload(payload)
    return EvolutionRevalidationValidationReceipt.model_validate(
        {**payload, "receipt_id": f"evrevalidate_{digest[:24]}", "receipt_sha256": digest}
    )


def render_evolution_revalidation_validation(
    receipt: EvolutionRevalidationValidationReceipt,
) -> str:
    item = EvolutionRevalidationValidationReceipt.model_validate_json(
        receipt.model_dump_json()
    )
    return "\n".join(
        [
            f"# Evolution Harness Revalidation `{item.receipt_id}`",
            "",
            f"- Status：`{item.status.value}`",
            f"- Execution：`{item.execution_kind}` / `{item.execution_id}`",
            f"- Target：`{item.target_head}`",
            f"- Harness checks：{len(item.checks)}",
            f"- Project code executed：`{str(item.project_code_executed).lower()}`",
            f"- New evidence：`{str(item.new_validation_evidence_issued).lower()}`",
            "- Old evidence invalidated / Promotion authority：`false` / `false`",
        ]
        + ([f"- Failure：`{item.failure_code}`"] if item.failure_code else [])
    )


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_validations ("
        "authority_sha256 TEXT PRIMARY KEY, request_id TEXT NOT NULL, "
        "execution_sha256 TEXT NOT NULL, profile_sha256 TEXT NOT NULL, "
        "epoch INTEGER NOT NULL, owner_sha256 TEXT NOT NULL, expires_at TEXT NOT NULL, "
        "receipt_json TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL)"
    )
    await db.commit()


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise EvolutionRevalidationValidationError(
            "revalidation_validation_clock_invalid",
            "Revalidation validation 时钟必须包含 UTC offset。",
        )
    return value


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


def _receipt_authority_sha256(
    receipt: EvolutionRevalidationValidationReceipt,
) -> str:
    return _sha256_payload(
        {
            "policy": EVOLUTION_REVALIDATION_VALIDATION_POLICY,
            "request_sha256": receipt.request_sha256,
            "execution_sha256": receipt.execution_sha256,
            "overlay_source_sha256": receipt.overlay_source_sha256,
            "harness_plan_sha256": receipt.harness_plan_sha256,
        }
    )


__all__ = [
    "EVOLUTION_REVALIDATION_VALIDATION_POLICY",
    "EvolutionRevalidationCheckEvidence",
    "EvolutionRevalidationValidationError",
    "EvolutionRevalidationValidationReceipt",
    "EvolutionRevalidationValidationService",
    "EvolutionRevalidationValidationStatus",
    "EvolutionRevalidationValidationStore",
    "render_evolution_revalidation_validation",
]
