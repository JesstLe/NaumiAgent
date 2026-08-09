"""Install and boot-check an exact approved candidate bundle without activation."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.revalidation_rollout_plans import (
    EvolutionRevalidationRolloutPlan,
    EvolutionRevalidationRolloutPlanService,
)
from naumi_agent.evolution.revalidation_rollout_stage_advances import (
    EvolutionRevalidationRolloutStageAdvanceError,
    EvolutionRevalidationRolloutStageAdvanceReceipt,
    EvolutionRevalidationRolloutStageAdvanceService,
)
from naumi_agent.release.build_attestations import (
    ReleaseBuildAttestation,
    ReleaseBuildAttestationError,
    ReleaseBuildTrustPolicyDocument,
    ReleaseTrustedBuilderKey,
    load_release_build_attestation,
    verify_release_build_attestation,
)
from naumi_agent.release.slots import (
    ReleaseActivePointer,
    ReleaseInstalledSlot,
    ReleaseSlotBootReceipt,
    ReleaseSlotError,
    ReleaseSlotStore,
)

EVOLUTION_REVALIDATION_CANDIDATE_BUNDLE_ADMISSION_POLICY = (
    "evolution-revalidation-candidate-bundle-admission-v2"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_ARTIFACT_BYTES = 2 * 1024 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class EvolutionRevalidationCandidateBundleAdmission(_StrictModel):
    schema_version: Literal[2] = 2
    policy_version: Literal["evolution-revalidation-candidate-bundle-admission-v2"] = (
        EVOLUTION_REVALIDATION_CANDIDATE_BUNDLE_ADMISSION_POLICY
    )
    admission_id: str = Field(pattern=r"^evrecandidatebundle_[0-9a-f]{24}$")
    admission_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    stage_advance_receipt_id: str = Field(pattern=r"^evrerolloutadvance_[0-9a-f]{24}$")
    stage_advance_receipt_sha256: str = Field(pattern=_SHA256_RE)
    completion_id: str = Field(pattern=r"^evrerolloutcomplete_[0-9a-f]{24}$")
    plan_id: str = Field(pattern=r"^evrerolloutplan_[0-9a-f]{24}$")
    plan_sha256: str = Field(pattern=_SHA256_RE)
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    target_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    target_tree_sha256: str = Field(pattern=_SHA256_RE)
    rollback_baseline_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    rollback_baseline_tree_sha256: str = Field(pattern=_SHA256_RE)
    previous_pointer: ReleaseActivePointer
    previous_slot: ReleaseInstalledSlot
    candidate_slot: ReleaseInstalledSlot
    boot_receipt: ReleaseSlotBootReceipt
    build_trust_policy_id: str = Field(pattern=r"^relbuildtrust_[0-9a-f]{24}$")
    build_trust_policy_sha256: str = Field(pattern=_SHA256_RE)
    build_attestation: ReleaseBuildAttestation
    trusted_builder_key: ReleaseTrustedBuilderKey
    trusted_build_signature_verified: Literal[True] = True
    trusted_builder_current_at_admission: Literal[True] = True
    exact_candidate_source_verified: Literal[True] = True
    rollback_slot_verified: Literal[True] = True
    immutable_slot_installed: Literal[True] = True
    boot_probe_passed: Literal[True] = True
    active_pointer_unchanged: Literal[True] = True
    bundle_admitted: Literal[True] = True
    activation_input_authority: Literal[True] = True
    deployment_authority: Literal[False] = False
    active_pointer_switched: Literal[False] = False
    process_started: Literal[False] = False
    rollback_executed: Literal[False] = False
    promotion_authority: Literal[False] = False
    admitted_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Candidate Bundle Admission workspace 必须 canonical。")
        if not (
            self.previous_pointer.current_slot_id == self.previous_slot.slot_id
            and self.previous_pointer.current_slot_sha256 == self.previous_slot.slot_sha256
            and self.previous_slot.source_commit == self.rollback_baseline_commit
            and self.previous_slot.source_tree_sha256 == self.rollback_baseline_tree_sha256
        ):
            raise ValueError("Candidate Bundle Admission rollback slot 投影不一致。")
        if not (
            self.candidate_slot.source_commit == self.target_commit
            and self.candidate_slot.source_tree_sha256 == self.target_tree_sha256
            and self.boot_receipt.slot_id == self.candidate_slot.slot_id
            and self.boot_receipt.slot_sha256 == self.candidate_slot.slot_sha256
            and self.boot_receipt.manifest_sha256 == self.candidate_slot.manifest_sha256
        ):
            raise ValueError("Candidate Bundle Admission candidate/boot 投影不一致。")
        payload = self.build_attestation.payload
        if not (
            payload.builder == self.trusted_builder_key.identity
            and self.trusted_builder_key.state == "active"
            and payload.version == self.candidate_slot.version
            and payload.target == self.candidate_slot.target
            and payload.source_commit == self.candidate_slot.source_commit
            and payload.source_tree_sha256 == self.candidate_slot.source_tree_sha256
            and payload.manifest_sha256 == self.candidate_slot.manifest_sha256
        ):
            raise ValueError("Candidate Bundle Admission trusted build 投影不一致。")
        _aware(self.admitted_at)
        core = self.model_dump(mode="json", exclude={"admission_id", "admission_sha256"})
        digest = _digest(core)
        if self.admission_sha256 != digest or self.admission_id != (
            f"evrecandidatebundle_{digest[:24]}"
        ):
            raise ValueError("Candidate Bundle Admission identity 不一致。")
        return self


class EvolutionRevalidationCandidateBundleAdmissionView(_StrictModel):
    admission: EvolutionRevalidationCandidateBundleAdmission
    stage_advance_current: bool
    active_pointer_current: bool
    slot_current: bool
    boot_receipt_current: bool
    trust_policy_current: bool
    build_attestation_current: bool
    activation_input_authority: bool

    @model_validator(mode="after")
    def _project(self) -> Self:
        expected = bool(
            self.admission.activation_input_authority
            and self.stage_advance_current
            and self.active_pointer_current
            and self.slot_current
            and self.boot_receipt_current
            and self.trust_policy_current
            and self.build_attestation_current
        )
        if self.activation_input_authority is not expected:
            raise ValueError("Candidate Bundle Admission view authority 投影不一致。")
        return self


class EvolutionRevalidationCandidateBundleAdmissionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationCandidateBundleAdmissionStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def get_by_stage_advance(self, receipt_id: str):
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT admission_json FROM "
                    "evolution_revalidation_candidate_bundle_admissions_v2 "
                    "WHERE stage_advance_receipt_id = ?",
                    (receipt_id,),
                )
            ).fetchone()
        return None if row is None else _restore(row["admission_json"])

    async def record(self, admission):
        try:
            item = EvolutionRevalidationCandidateBundleAdmission.model_validate_json(
                admission.model_dump_json()
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationCandidateBundleAdmissionError(
                "candidate_bundle_admission_invalid",
                "Candidate Bundle Admission artifact 无效。",
            ) from exc
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
            raise EvolutionRevalidationCandidateBundleAdmissionError(
                "candidate_bundle_admission_oversized",
                "Candidate Bundle Admission 超过 2 MiB。",
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                advance = await (
                    await db.execute(
                        "SELECT receipt_sha256, decision, receipt_json FROM "
                        "evolution_revalidation_rollout_stage_advances "
                        "WHERE receipt_id = ?",
                        (item.stage_advance_receipt_id,),
                    )
                ).fetchone()
                plan = await (
                    await db.execute(
                        "SELECT plan_sha256, plan_json FROM "
                        "evolution_revalidation_rollout_plans "
                        "WHERE plan_id = ?",
                        (item.plan_id,),
                    )
                ).fetchone()
                source_advance = (
                    None
                    if advance is None
                    else EvolutionRevalidationRolloutStageAdvanceReceipt.model_validate_json(
                        advance["receipt_json"]
                    )
                )
                source_plan = (
                    None
                    if plan is None
                    else EvolutionRevalidationRolloutPlan.model_validate_json(plan["plan_json"])
                )
                if not (
                    advance is not None
                    and advance["receipt_sha256"] == item.stage_advance_receipt_sha256
                    and advance["decision"] == "advance"
                    and plan is not None
                    and plan["plan_sha256"] == item.plan_sha256
                    and source_advance is not None
                    and source_plan is not None
                    and _matches_sources(item, source_advance, source_plan)
                ):
                    await db.rollback()
                    raise EvolutionRevalidationCandidateBundleAdmissionError(
                        "candidate_bundle_admission_dependency_changed",
                        "Stage Advance 或 Rollout Plan 持久化依赖已变化。",
                    )
                existing = await (
                    await db.execute(
                        "SELECT admission_json FROM "
                        "evolution_revalidation_candidate_bundle_admissions_v2 "
                        "WHERE stage_advance_receipt_id = ?",
                        (item.stage_advance_receipt_id,),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _restore(existing["admission_json"])
                    await db.rollback()
                    if restored != item:
                        raise EvolutionRevalidationCandidateBundleAdmissionError(
                            "candidate_bundle_admission_conflict",
                            "同一 Stage Advance 已绑定不同 candidate bundle。",
                        )
                    return restored
                await db.execute(
                    "INSERT INTO evolution_revalidation_candidate_bundle_admissions_v2 "
                    "(admission_id, admission_sha256, stage_advance_receipt_id, "
                    "plan_id, slot_id, admission_json, admitted_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.admission_id,
                        item.admission_sha256,
                        item.stage_advance_receipt_id,
                        item.plan_id,
                        item.candidate_slot.slot_id,
                        encoded,
                        item.admitted_at,
                    ),
                )
                await db.commit()
        except EvolutionRevalidationCandidateBundleAdmissionError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationCandidateBundleAdmissionError(
                "candidate_bundle_admission_store_error",
                "Candidate Bundle Admission 无法持久化。",
            ) from exc
        return item


class EvolutionRevalidationCandidateBundleAdmissionService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        stage_advance_service: EvolutionRevalidationRolloutStageAdvanceService,
        plan_service: EvolutionRevalidationRolloutPlanService,
        release_slot_store: ReleaseSlotStore,
        trust_policy_provider: Callable[[], ReleaseBuildTrustPolicyDocument],
        store: EvolutionRevalidationCandidateBundleAdmissionStore,
        now=None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.stage_advance_service = stage_advance_service
        self.plan_service = plan_service
        self.release_slot_store = release_slot_store
        self.trust_policy_provider = trust_policy_provider
        self.store = store
        self.now = now or (lambda: datetime.now(UTC).isoformat())
        self._locks: dict[str, asyncio.Lock] = {}

    async def admit(
        self,
        *,
        completion_id: str,
        bundle_dir: str | Path,
        build_attestation_path: str | Path,
    ):
        lock = self._locks.setdefault(completion_id, asyncio.Lock())
        async with lock:
            advance = await self._current_advance(completion_id)
            bundle_path, attestation, trust_policy, trusted_key = await asyncio.to_thread(
                self._verified_build,
                bundle_dir,
                build_attestation_path,
            )
            existing = await self.store.get_by_stage_advance(advance.receipt.receipt_id)
            if existing is not None:
                if existing.build_attestation != attestation:
                    raise EvolutionRevalidationCandidateBundleAdmissionError(
                        "candidate_build_attestation_conflict",
                        "同一 Stage Advance 已绑定不同构建证明。",
                    )
                return await self._view(existing, completion_id)
            plan_view = await self.plan_service.inspect(plan_id=advance.receipt.plan_id)
            if not plan_view.current_rollout_eligible:
                raise EvolutionRevalidationCandidateBundleAdmissionError(
                    "candidate_bundle_plan_stale", "Rollout Plan 已失效。"
                )
            plan = plan_view.plan
            promotion_input = await self.plan_service.promotion_input_service.issue(
                contract_id=plan.contract_id
            )
            rollback = promotion_input.prior_input.rollback
            if not (
                attestation.payload.source_commit == plan.target_head
                and attestation.payload.source_tree_sha256 == plan.target_tree_sha256
            ):
                raise EvolutionRevalidationCandidateBundleAdmissionError(
                    "candidate_build_attestation_source_mismatch",
                    "可信构建证明的 source provenance 与获批 target 不一致。",
                )
            previous_pointer, previous_slot = await asyncio.to_thread(
                self._rollback_slot,
                rollback.baseline_commit,
                rollback.baseline_tree_sha256,
            )
            try:
                candidate_slot = await asyncio.to_thread(
                    self.release_slot_store.install,
                    bundle_path,
                    expected_manifest_sha256=attestation.payload.manifest_sha256,
                )
            except (ReleaseSlotError, OSError, TypeError, ValueError) as exc:
                raise EvolutionRevalidationCandidateBundleAdmissionError(
                    "candidate_bundle_install_failed",
                    "候选发行 bundle 安装失败。",
                ) from exc
            if not (
                candidate_slot.source_commit == plan.target_head
                and candidate_slot.source_tree_sha256 == plan.target_tree_sha256
                and candidate_slot.manifest_sha256 == attestation.payload.manifest_sha256
                and candidate_slot.version == attestation.payload.version
                and candidate_slot.target == attestation.payload.target
            ):
                raise EvolutionRevalidationCandidateBundleAdmissionError(
                    "candidate_bundle_source_mismatch",
                    "候选 bundle source provenance 与获批 target 不一致。",
                )
            try:
                boot = await asyncio.to_thread(
                    self.release_slot_store.verify_bootable, candidate_slot.slot_id
                )
            except (ReleaseSlotError, OSError, TypeError, ValueError) as exc:
                raise EvolutionRevalidationCandidateBundleAdmissionError(
                    "candidate_bundle_boot_failed",
                    "候选版本槽未通过真实启动探测。",
                ) from exc
            refreshed = await self._current_advance(completion_id)
            current_pointer = await asyncio.to_thread(self.release_slot_store.active)
            if refreshed != advance or current_pointer != previous_pointer:
                raise EvolutionRevalidationCandidateBundleAdmissionError(
                    "candidate_bundle_authority_changed",
                    "安装/启动探测期间 Stage Advance 或 active pointer 已变化。",
                )
            refreshed_policy, refreshed_key = await asyncio.to_thread(
                self._verify_stored_build,
                attestation,
                Path(candidate_slot.bundle_dir) / "manifest.json",
            )
            if refreshed_policy != trust_policy or refreshed_key != trusted_key:
                raise EvolutionRevalidationCandidateBundleAdmissionError(
                    "candidate_build_trust_policy_changed",
                    "安装/启动探测期间 Build Trust Policy 已变化。",
                )
            item = _build(
                workspace_root=self.workspace_root,
                advance=advance,
                plan=plan,
                rollback=rollback,
                previous_pointer=previous_pointer,
                previous_slot=previous_slot,
                candidate_slot=candidate_slot,
                boot=boot,
                trust_policy=trust_policy,
                attestation=attestation,
                trusted_key=trusted_key,
                admitted_at=self.now(),
            )
            stored = await self.store.record(item)
            return await self._view(stored, completion_id)

    async def inspect(self, *, completion_id: str):
        advance = await self._current_advance(completion_id)
        item = await self.store.get_by_stage_advance(advance.receipt.receipt_id)
        if item is None:
            raise EvolutionRevalidationCandidateBundleAdmissionError(
                "candidate_bundle_admission_missing", "尚未形成 Candidate Bundle Admission。"
            )
        return await self._view(item, completion_id)

    async def _current_advance(self, completion_id):
        try:
            view = await self.stage_advance_service.inspect(completion_id=completion_id)
        except EvolutionRevalidationRolloutStageAdvanceError as exc:
            raise EvolutionRevalidationCandidateBundleAdmissionError(
                "candidate_bundle_stage_advance_stale",
                "Stage Advance authority 已失效。",
            ) from exc
        if not view.next_stage_entry_authority:
            raise EvolutionRevalidationCandidateBundleAdmissionError(
                "candidate_bundle_stage_advance_denied",
                "当前 Stage Advance 未授权进入 opt-in。",
            )
        return view

    def _rollback_slot(self, baseline_commit, baseline_tree):
        try:
            resolved = self.release_slot_store.resolve_active_backend()
        except ReleaseSlotError as exc:
            raise EvolutionRevalidationCandidateBundleAdmissionError(
                "candidate_bundle_rollback_slot_missing",
                "激活候选前必须存在可启动的 current rollback slot。",
            ) from exc
        pointer = resolved.pointer
        slot = resolved.slot
        if not (slot.source_commit == baseline_commit and slot.source_tree_sha256 == baseline_tree):
            raise EvolutionRevalidationCandidateBundleAdmissionError(
                "candidate_bundle_rollback_slot_mismatch",
                "Current slot 不是获批 Rollback Plan 的 exact baseline。",
            )
        return pointer, slot

    def _verified_build(self, bundle_dir, attestation_path):
        try:
            bundle = Path(bundle_dir).expanduser().resolve(strict=True)
            if not bundle.is_dir():
                raise ValueError("candidate bundle 不是目录")
            attestation = load_release_build_attestation(
                Path(attestation_path).expanduser().resolve(strict=True)
            )
            policy, trusted_key = self._verify_stored_build(
                attestation,
                bundle / "manifest.json",
            )
        except EvolutionRevalidationCandidateBundleAdmissionError:
            raise
        except (OSError, TypeError, ValueError, ReleaseBuildAttestationError) as exc:
            raise EvolutionRevalidationCandidateBundleAdmissionError(
                "candidate_build_attestation_invalid",
                "候选 bundle 缺少有效的可信构建证明。",
            ) from exc
        return bundle, attestation, policy, trusted_key

    def _verify_stored_build(self, attestation, manifest_path):
        try:
            policy = self.trust_policy_provider()
            if not isinstance(policy, ReleaseBuildTrustPolicyDocument):
                raise TypeError("trust policy provider 返回类型无效")
            trusted_key = verify_release_build_attestation(
                attestation,
                trust_policy=policy,
                manifest_path=manifest_path,
            )
        except EvolutionRevalidationCandidateBundleAdmissionError:
            raise
        except (OSError, TypeError, ValueError, ReleaseBuildAttestationError) as exc:
            raise EvolutionRevalidationCandidateBundleAdmissionError(
                "candidate_build_attestation_invalid",
                "候选 bundle 的可信构建证明当前无效。",
            ) from exc
        return policy, trusted_key

    async def _view(self, item, completion_id):
        try:
            advance = await self._current_advance(completion_id)
            advance_current = (
                advance.receipt.receipt_id == item.stage_advance_receipt_id
                and advance.receipt.receipt_sha256 == item.stage_advance_receipt_sha256
            )
        except EvolutionRevalidationCandidateBundleAdmissionError:
            advance_current = False
        pointer = await asyncio.to_thread(self.release_slot_store.active)
        try:
            resolved = await asyncio.to_thread(
                self.release_slot_store.resolve_booted_slot,
                item.candidate_slot.slot_id,
                item.boot_receipt.receipt_id,
            )
            slot = resolved.slot
            boot = resolved.boot_receipt
        except ReleaseSlotError:
            slot = None
            boot = None
        pointer_current = pointer == item.previous_pointer
        slot_current = slot == item.candidate_slot
        boot_current = boot == item.boot_receipt
        try:
            policy, trusted_key = await asyncio.to_thread(
                self._verify_stored_build,
                item.build_attestation,
                Path(item.candidate_slot.bundle_dir) / "manifest.json",
            )
            trust_policy_current = bool(
                policy.policy_id == item.build_trust_policy_id
                and policy.policy_sha256 == item.build_trust_policy_sha256
            )
            build_attestation_current = trusted_key == item.trusted_builder_key
        except EvolutionRevalidationCandidateBundleAdmissionError:
            trust_policy_current = False
            build_attestation_current = False
        return EvolutionRevalidationCandidateBundleAdmissionView(
            admission=item,
            stage_advance_current=advance_current,
            active_pointer_current=pointer_current,
            slot_current=slot_current,
            boot_receipt_current=boot_current,
            trust_policy_current=trust_policy_current,
            build_attestation_current=build_attestation_current,
            activation_input_authority=(
                advance_current
                and pointer_current
                and slot_current
                and boot_current
                and trust_policy_current
                and build_attestation_current
            ),
        )


def _build(
    *,
    workspace_root,
    advance,
    plan,
    rollback,
    previous_pointer,
    previous_slot,
    candidate_slot,
    boot,
    trust_policy,
    attestation,
    trusted_key,
    admitted_at,
):
    core = {
        "schema_version": 2,
        "policy_version": EVOLUTION_REVALIDATION_CANDIDATE_BUNDLE_ADMISSION_POLICY,
        "workspace_root": str(workspace_root),
        "stage_advance_receipt_id": advance.receipt.receipt_id,
        "stage_advance_receipt_sha256": advance.receipt.receipt_sha256,
        "completion_id": advance.receipt.completion_id,
        "plan_id": plan.plan_id,
        "plan_sha256": plan.plan_sha256,
        "candidate_id": plan.candidate_id,
        "candidate_revision": plan.candidate_revision,
        "target_commit": plan.target_head,
        "target_tree_sha256": plan.target_tree_sha256,
        "rollback_baseline_commit": rollback.baseline_commit,
        "rollback_baseline_tree_sha256": rollback.baseline_tree_sha256,
        "previous_pointer": previous_pointer,
        "previous_slot": previous_slot,
        "candidate_slot": candidate_slot,
        "boot_receipt": boot,
        "build_trust_policy_id": trust_policy.policy_id,
        "build_trust_policy_sha256": trust_policy.policy_sha256,
        "build_attestation": attestation,
        "trusted_builder_key": trusted_key,
        "trusted_build_signature_verified": True,
        "trusted_builder_current_at_admission": True,
        "exact_candidate_source_verified": True,
        "rollback_slot_verified": True,
        "immutable_slot_installed": True,
        "boot_probe_passed": True,
        "active_pointer_unchanged": True,
        "bundle_admitted": True,
        "activation_input_authority": True,
        "deployment_authority": False,
        "active_pointer_switched": False,
        "process_started": False,
        "rollback_executed": False,
        "promotion_authority": False,
        "admitted_at": _aware(admitted_at).isoformat(),
    }
    digest = _digest(core)
    return EvolutionRevalidationCandidateBundleAdmission.model_validate(
        {**core, "admission_id": f"evrecandidatebundle_{digest[:24]}", "admission_sha256": digest}
    )


def _matches_sources(item, advance, plan) -> bool:
    return bool(
        item.workspace_root == advance.workspace_root == plan.workspace_root
        and item.stage_advance_receipt_id == advance.receipt_id
        and item.stage_advance_receipt_sha256 == advance.receipt_sha256
        and item.completion_id == advance.completion_id
        and item.plan_id == advance.plan_id == plan.plan_id
        and item.plan_sha256 == advance.plan_sha256 == plan.plan_sha256
        and item.candidate_id == plan.candidate_id
        and item.candidate_revision == plan.candidate_revision
        and item.target_commit == plan.target_head
        and item.target_tree_sha256 == plan.target_tree_sha256
        and advance.decision == "advance"
    )


def _aware(value):
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.utcoffset() is None:
        raise ValueError("Candidate Bundle Admission timestamp 必须包含 offset。")
    return parsed.astimezone(UTC)


def _digest(payload) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
            default=lambda value: value.model_dump(mode="json"),
        ).encode()
    ).hexdigest()


def _restore(encoded: str):
    if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
        raise ValueError("Candidate Bundle Admission 超过 2 MiB。")
    return EvolutionRevalidationCandidateBundleAdmission.model_validate_json(encoded)


async def _ensure_schema(db) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_candidate_bundle_admissions_v2 ("
        "admission_id TEXT PRIMARY KEY, admission_sha256 TEXT NOT NULL UNIQUE, "
        "stage_advance_receipt_id TEXT NOT NULL UNIQUE, plan_id TEXT NOT NULL, "
        "slot_id TEXT NOT NULL, admission_json TEXT NOT NULL, admitted_at TEXT NOT NULL)"
    )
    await db.commit()


__all__ = [
    "EVOLUTION_REVALIDATION_CANDIDATE_BUNDLE_ADMISSION_POLICY",
    "EvolutionRevalidationCandidateBundleAdmission",
    "EvolutionRevalidationCandidateBundleAdmissionError",
    "EvolutionRevalidationCandidateBundleAdmissionService",
    "EvolutionRevalidationCandidateBundleAdmissionStore",
    "EvolutionRevalidationCandidateBundleAdmissionView",
]
