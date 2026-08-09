"""Deterministic percentage-cohort assignments over signed population snapshots."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import math
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.revalidation_opt_in_stage_advances import (
    EvolutionRevalidationOptInStageAdvanceError,
    EvolutionRevalidationOptInStageAdvanceReceipt,
    EvolutionRevalidationOptInStageAdvanceService,
)
from naumi_agent.evolution.revalidation_rollout_plans import (
    EvolutionRevalidationRolloutPlanError,
    EvolutionRevalidationRolloutPlanService,
)
from naumi_agent.release.population_registry import (
    ReleaseManagedInstallationCredential,
    ReleasePopulationRegistryError,
    ReleasePopulationSnapshot,
    ReleasePopulationSnapshotStore,
)

EVOLUTION_REVALIDATION_PERCENTAGE_COHORT_ASSIGNMENT_POLICY = (
    "evolution-revalidation-percentage-cohort-assignment-v1"
)
EVOLUTION_REVALIDATION_PERCENTAGE_ASSIGNMENT_PROOF_DOMAIN = (
    "naumi.evolution.percentage-assignment-proof.v1"
)
EVOLUTION_REVALIDATION_PERCENTAGE_ASSIGNMENT_ALGORITHM = (
    "sha256-ranked-population-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_CHANNEL_RE = r"^[a-z][a-z0-9._-]{0,63}$"
_MAX_MEMBERS = 10_000
_MAX_ARTIFACT_BYTES = 4 * 1024 * 1024

SignAssignmentChallenge = Callable[[bytes], Awaitable[str]]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionRevalidationPercentageAssignmentProofPayload(_StrictModel):
    schema_version: Literal[1] = 1
    domain: Literal[
        "naumi.evolution.percentage-assignment-proof.v1"
    ] = EVOLUTION_REVALIDATION_PERCENTAGE_ASSIGNMENT_PROOF_DOMAIN
    workspace_root: str = Field(min_length=1, max_length=4096)
    stage_advance_receipt_id: str = Field(pattern=r"^evreoptinadvance_[0-9a-f]{24}$")
    stage_advance_receipt_sha256: str = Field(pattern=_SHA256_RE)
    population_snapshot_id: str = Field(pattern=r"^relpopsnapshot_[0-9a-f]{24}$")
    population_snapshot_sha256: str = Field(pattern=_SHA256_RE)
    credential_id: str = Field(pattern=r"^relpopcred_[0-9a-f]{24}$")
    credential_sha256: str = Field(pattern=_SHA256_RE)
    member_id: str = Field(pattern=r"^relpopmember_[0-9a-f]{24}$")
    plan_id: str = Field(pattern=r"^evrerolloutplan_[0-9a-f]{24}$")
    plan_sha256: str = Field(pattern=_SHA256_RE)
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    channel: str = Field(pattern=r"^[a-z][a-z0-9._-]{0,63}$")
    assignment_algorithm: Literal[
        "sha256-ranked-population-v1"
    ] = EVOLUTION_REVALIDATION_PERCENTAGE_ASSIGNMENT_ALGORITHM
    assignment_seed_sha256: str = Field(pattern=_SHA256_RE)
    selected_set_sha256: str = Field(pattern=_SHA256_RE)
    population_denominator: int = Field(ge=1, le=_MAX_MEMBERS)
    exposure_percent: int = Field(ge=1, le=99)
    target_member_count: int = Field(ge=1, le=_MAX_MEMBERS)
    member_rank: int = Field(ge=1, le=_MAX_MEMBERS)
    member_selected: bool
    signed_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Percentage Assignment Proof workspace 必须 canonical。")
        expected_count = max(
            1,
            math.ceil(self.population_denominator * self.exposure_percent / 100),
        )
        if not (
            self.target_member_count == expected_count
            and self.member_rank <= self.population_denominator
            and self.member_selected is (self.member_rank <= expected_count)
        ):
            raise ValueError("Percentage Assignment Proof selection projection 不一致。")
        _aware(self.signed_at)
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.model_dump(mode="json"))


class EvolutionRevalidationPercentageAssignmentProof(_StrictModel):
    schema_version: Literal[1] = 1
    proof_id: str = Field(pattern=r"^evrepercentproof_[0-9a-f]{24}$")
    proof_sha256: str = Field(pattern=_SHA256_RE)
    payload: EvolutionRevalidationPercentageAssignmentProofPayload
    installation_public_key_base64: str = Field(min_length=44, max_length=44)
    installation_public_key_sha256: str = Field(pattern=_SHA256_RE)
    signature_algorithm: Literal["ed25519"] = "ed25519"
    signature_base64: str = Field(min_length=88, max_length=88)
    signature_sha256: str = Field(pattern=_SHA256_RE)
    private_key_persisted: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        public_key = _decode_base64(
            self.installation_public_key_base64,
            expected_bytes=32,
            label="installation public key",
        )
        signature = _decode_base64(
            self.signature_base64,
            expected_bytes=64,
            label="percentage assignment signature",
        )
        if not (
            hmac.compare_digest(
                base64.b64encode(public_key).decode("ascii"),
                self.installation_public_key_base64,
            )
            and hmac.compare_digest(
                hashlib.sha256(public_key).hexdigest(),
                self.installation_public_key_sha256,
            )
            and hmac.compare_digest(
                base64.b64encode(signature).decode("ascii"),
                self.signature_base64,
            )
            and hmac.compare_digest(
                hashlib.sha256(signature).hexdigest(),
                self.signature_sha256,
            )
        ):
            raise ValueError("Percentage Assignment Proof key/signature identity 不一致。")
        try:
            Ed25519PublicKey.from_public_bytes(public_key).verify(
                signature,
                self.payload.canonical_bytes(),
            )
        except (InvalidSignature, ValueError) as exc:
            raise ValueError("Percentage Assignment proof-of-possession 无效。") from exc
        core = self.model_dump(mode="json", exclude={"proof_id", "proof_sha256"})
        digest = _digest(core)
        if self.proof_sha256 != digest or self.proof_id != (
            f"evrepercentproof_{digest[:24]}"
        ):
            raise ValueError("Percentage Assignment Proof identity 不一致。")
        return self


class EvolutionRevalidationPercentageCohortAssignment(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-revalidation-percentage-cohort-assignment-v1"
    ] = EVOLUTION_REVALIDATION_PERCENTAGE_COHORT_ASSIGNMENT_POLICY
    assignment_id: str = Field(pattern=r"^evrepercentassign_[0-9a-f]{24}$")
    assignment_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    stage_advance: EvolutionRevalidationOptInStageAdvanceReceipt
    population_snapshot_id: str = Field(pattern=r"^relpopsnapshot_[0-9a-f]{24}$")
    population_snapshot_sha256: str = Field(pattern=_SHA256_RE)
    population_snapshot_sequence: int = Field(ge=1, le=1_000_000)
    channel: str = Field(pattern=r"^[a-z][a-z0-9._-]{0,63}$")
    credential_id: str = Field(pattern=r"^relpopcred_[0-9a-f]{24}$")
    credential_sha256: str = Field(pattern=_SHA256_RE)
    member_id: str = Field(pattern=r"^relpopmember_[0-9a-f]{24}$")
    installation_public_key_sha256: str = Field(pattern=_SHA256_RE)
    plan_id: str = Field(pattern=r"^evrerolloutplan_[0-9a-f]{24}$")
    plan_sha256: str = Field(pattern=_SHA256_RE)
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    candidate_target: str = Field(min_length=1, max_length=255)
    assignment_algorithm: Literal[
        "sha256-ranked-population-v1"
    ] = EVOLUTION_REVALIDATION_PERCENTAGE_ASSIGNMENT_ALGORITHM
    assignment_seed_sha256: str = Field(pattern=_SHA256_RE)
    population_denominator: int = Field(ge=1, le=_MAX_MEMBERS)
    exposure_percent: int = Field(ge=1, le=99)
    target_member_count: int = Field(ge=1, le=_MAX_MEMBERS)
    selected_member_ids: tuple[str, ...] = Field(
        min_length=1,
        max_length=_MAX_MEMBERS,
    )
    selected_set_sha256: str = Field(pattern=_SHA256_RE)
    member_rank: int = Field(ge=1, le=_MAX_MEMBERS)
    member_selected: bool
    proof: EvolutionRevalidationPercentageAssignmentProof
    assigned_at: str = Field(min_length=1, max_length=100)
    stage_entry_current_at_issue: Literal[True] = True
    population_snapshot_current_at_issue: Literal[True] = True
    proof_of_possession_verified: Literal[True] = True
    population_assignment_enforced: Literal[True] = True
    percentage_cohort_membership_authority: bool
    percentage_rollout_authority: Literal[False] = False
    deployment_authority: Literal[False] = False
    process_started: Literal[False] = False
    stable_rollout_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    git_write_executed: Literal[False] = False
    publish_executed: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Percentage Cohort Assignment workspace 必须 canonical。")
        _validate_assignment_advance(self)
        _validate_assignment_selection(self)
        _validate_assignment_proof(self)
        core = self.model_dump(
            mode="json",
            exclude={"assignment_id", "assignment_sha256"},
        )
        digest = _digest(core)
        if self.assignment_sha256 != digest or self.assignment_id != (
            f"evrepercentassign_{digest[:24]}"
        ):
            raise ValueError("Percentage Cohort Assignment identity 不一致。")
        return self


def _validate_assignment_advance(
    item: EvolutionRevalidationPercentageCohortAssignment,
) -> None:
    advance = item.stage_advance
    if not (
        advance.workspace_root == item.workspace_root
        and advance.decision == "advance"
        and advance.completed_stage == "opt_in"
        and advance.next_stage == "percentage"
        and advance.percentage_stage_entry_authority
        and advance.percentage_exposure_percent == item.exposure_percent
        and item.plan_id == advance.plan_id
        and item.plan_sha256 == advance.plan_sha256
    ):
        raise ValueError("Percentage Assignment Stage Advance projection 不一致。")


def _validate_assignment_selection(
    item: EvolutionRevalidationPercentageCohortAssignment,
) -> None:
    members = item.selected_member_ids
    if not (
        len(members) == item.target_member_count
        and len(set(members)) == len(members)
        and all(
            re.fullmatch(r"^relpopmember_[0-9a-f]{24}$", member_id) is not None
            for member_id in members
        )
        and item.selected_set_sha256 == _digest(members)
        and item.population_denominator >= item.target_member_count
        and item.member_rank <= item.population_denominator
        and item.member_selected is (item.member_id in members)
        and item.percentage_cohort_membership_authority is item.member_selected
    ):
        raise ValueError("Percentage Assignment cohort projection 不一致。")


def _validate_assignment_proof(
    item: EvolutionRevalidationPercentageCohortAssignment,
) -> None:
    proof = item.proof
    payload = proof.payload
    expected = (
        (payload.workspace_root, item.workspace_root),
        (payload.stage_advance_receipt_id, item.stage_advance.receipt_id),
        (payload.stage_advance_receipt_sha256, item.stage_advance.receipt_sha256),
        (payload.population_snapshot_id, item.population_snapshot_id),
        (payload.population_snapshot_sha256, item.population_snapshot_sha256),
        (payload.credential_id, item.credential_id),
        (payload.credential_sha256, item.credential_sha256),
        (payload.member_id, item.member_id),
        (payload.plan_id, item.plan_id),
        (payload.plan_sha256, item.plan_sha256),
        (payload.candidate_id, item.candidate_id),
        (payload.candidate_revision, item.candidate_revision),
        (payload.channel, item.channel),
        (payload.assignment_algorithm, item.assignment_algorithm),
        (payload.assignment_seed_sha256, item.assignment_seed_sha256),
        (payload.selected_set_sha256, item.selected_set_sha256),
        (payload.population_denominator, item.population_denominator),
        (payload.exposure_percent, item.exposure_percent),
        (payload.target_member_count, item.target_member_count),
        (payload.member_rank, item.member_rank),
        (payload.member_selected, item.member_selected),
        (proof.installation_public_key_sha256, item.installation_public_key_sha256),
        (_aware(payload.signed_at), _aware(item.assigned_at)),
    )
    if not all(left == right for left, right in expected):
        raise ValueError("Percentage Assignment proof projection 不一致。")


class EvolutionRevalidationPercentageCohortAssignmentView(_StrictModel):
    assignment: EvolutionRevalidationPercentageCohortAssignment
    assignment_source_current: bool
    stage_advance_current: bool
    population_snapshot_current: bool
    plan_source_current: bool
    credential_current: bool
    deterministic_assignment_current: bool
    invalidation_reasons: tuple[str, ...] = Field(max_length=8)
    population_assignment_enforced: bool
    percentage_cohort_membership_authority: bool
    percentage_rollout_authority: Literal[False] = False
    deployment_authority: Literal[False] = False
    stable_rollout_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _project(self) -> Self:
        current = bool(
            self.assignment_source_current
            and self.stage_advance_current
            and self.population_snapshot_current
            and self.plan_source_current
            and self.credential_current
            and self.deterministic_assignment_current
        )
        if not (
            self.population_assignment_enforced is current
            and self.percentage_cohort_membership_authority
            is (current and self.assignment.member_selected)
            and tuple(sorted(set(self.invalidation_reasons)))
            == self.invalidation_reasons
        ):
            raise ValueError("Percentage Assignment View authority projection 不一致。")
        return self


class EvolutionRevalidationPercentageCohortAssignmentError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationPercentageCohortAssignmentStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        advance_service: EvolutionRevalidationOptInStageAdvanceService,
        population_store: ReleasePopulationSnapshotStore,
        plan_service: EvolutionRevalidationRolloutPlanService,
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        if not isinstance(advance_service, EvolutionRevalidationOptInStageAdvanceService):
            raise TypeError("Percentage Assignment Store 需要 Stage Advance Service。")
        if not isinstance(population_store, ReleasePopulationSnapshotStore):
            raise TypeError("Percentage Assignment Store 需要 Population Snapshot Store。")
        if not isinstance(plan_service, EvolutionRevalidationRolloutPlanService):
            raise TypeError("Percentage Assignment Store 需要 Rollout Plan Service。")
        if not (
            self.db_path == advance_service.store.db_path == plan_service.store.db_path
            and advance_service.plan_service is plan_service
        ):
            raise ValueError("Percentage Assignment 必须共用 exact Evolution evidence DB。")
        self.advance_service = advance_service
        self.advance_store = advance_service.store
        self.population_store = population_store
        self.plan_service = plan_service

    async def get_by_source(
        self,
        *,
        advance_receipt_id: str,
        snapshot_id: str,
        member_id: str,
    ) -> EvolutionRevalidationPercentageCohortAssignment | None:
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT assignment_json FROM "
                        "evolution_revalidation_percentage_cohort_assignments "
                        "WHERE advance_receipt_id = ? AND snapshot_id = ? AND member_id = ?",
                        (advance_receipt_id, snapshot_id, member_id),
                    )
                ).fetchone()
            return None if row is None else _restore(row["assignment_json"])
        except EvolutionRevalidationPercentageCohortAssignmentError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationPercentageCohortAssignmentError(
                "percentage_assignment_source_invalid",
                "Percentage Assignment durable source 无效。",
            ) from exc

    async def record(
        self,
        assignment: EvolutionRevalidationPercentageCohortAssignment,
    ) -> EvolutionRevalidationPercentageCohortAssignment:
        item = _validated(assignment)
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
            raise EvolutionRevalidationPercentageCohortAssignmentError(
                "percentage_assignment_oversized",
                "Percentage Assignment 超过 4 MiB。",
            )
        await self._require_live_sources(item)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            return await self._record_transaction(item, encoded)
        except EvolutionRevalidationPercentageCohortAssignmentError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationPercentageCohortAssignmentError(
                "percentage_assignment_store_error",
                "Percentage Assignment 无法持久化。",
            ) from exc

    async def _record_transaction(self, item, encoded):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            await self._require_evolution_sources(db, item)
            existing = await (
                await db.execute(
                    "SELECT assignment_json FROM "
                    "evolution_revalidation_percentage_cohort_assignments "
                    "WHERE advance_receipt_id = ? AND snapshot_id = ? AND member_id = ?",
                    (
                        item.stage_advance.receipt_id,
                        item.population_snapshot_id,
                        item.member_id,
                    ),
                )
            ).fetchone()
            if existing is not None:
                restored = _restore(existing["assignment_json"])
                await db.rollback()
                if not _same_source(restored, item):
                    raise EvolutionRevalidationPercentageCohortAssignmentError(
                        "percentage_assignment_conflict",
                        "同一 Advance/Snapshot/Member 已绑定不同 Assignment。",
                    )
                return restored
            await db.execute(
                "INSERT INTO evolution_revalidation_percentage_cohort_assignments "
                "(assignment_id, assignment_sha256, advance_receipt_id, snapshot_id, "
                "member_id, assignment_json, assigned_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    item.assignment_id,
                    item.assignment_sha256,
                    item.stage_advance.receipt_id,
                    item.population_snapshot_id,
                    item.member_id,
                    encoded,
                    item.assigned_at,
                ),
            )
            await db.commit()
        return item

    async def _require_live_sources(self, item) -> None:
        try:
            advance_view = await self.advance_service.inspect(
                evidence_id=item.stage_advance.stage_completion_evidence_id
            )
            snapshot_view = await self.population_store.inspect(
                snapshot_id=item.population_snapshot_id
            )
            plan_view = await self.plan_service.inspect(plan_id=item.plan_id)
        except (
            EvolutionRevalidationOptInStageAdvanceError,
            ReleasePopulationRegistryError,
            EvolutionRevalidationRolloutPlanError,
        ) as exc:
            raise EvolutionRevalidationPercentageCohortAssignmentError(
                "percentage_assignment_source_unavailable",
                "Percentage Assignment 的 current source 当前不可用。",
            ) from exc
        if not _sources_match(
            item,
            advance_view,
            snapshot_view,
            plan_view,
        ):
            raise EvolutionRevalidationPercentageCohortAssignmentError(
                "percentage_assignment_source_changed",
                "Stage Advance、Population Snapshot、Plan 或 Credential 已变化。",
            )

    async def _require_evolution_sources(self, db, item) -> None:
        advance_row = await (
            await db.execute(
                "SELECT receipt_sha256, receipt_json FROM "
                "evolution_revalidation_opt_in_stage_advances WHERE receipt_id = ?",
                (item.stage_advance.receipt_id,),
            )
        ).fetchone()
        plan_row = await (
            await db.execute(
                "SELECT plan_sha256 FROM evolution_revalidation_rollout_plans "
                "WHERE plan_id = ?",
                (item.plan_id,),
            )
        ).fetchone()
        try:
            advance = (
                None
                if advance_row is None
                else EvolutionRevalidationOptInStageAdvanceReceipt.model_validate_json(
                    advance_row["receipt_json"]
                )
            )
        except (TypeError, ValueError) as exc:
            raise EvolutionRevalidationPercentageCohortAssignmentError(
                "percentage_assignment_dependency_invalid",
                "Percentage Assignment durable Advance source 无效。",
            ) from exc
        if not (
            advance == item.stage_advance
            and advance_row["receipt_sha256"] == item.stage_advance.receipt_sha256
            and plan_row is not None
            and plan_row["plan_sha256"] == item.plan_sha256
        ):
            raise EvolutionRevalidationPercentageCohortAssignmentError(
                "percentage_assignment_dependency_changed",
                "Percentage Assignment 的 durable Advance/Plan source 已变化。",
            )


class EvolutionRevalidationPercentageCohortAssignmentService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        channel: str,
        advance_service: EvolutionRevalidationOptInStageAdvanceService,
        population_store: ReleasePopulationSnapshotStore,
        plan_service: EvolutionRevalidationRolloutPlanService,
        store: EvolutionRevalidationPercentageCohortAssignmentStore,
        sign_assignment_challenge: SignAssignmentChallenge,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if re.fullmatch(_CHANNEL_RE, channel) is None:
            raise ValueError("Percentage Assignment channel 无效。")
        if not callable(sign_assignment_challenge):
            raise TypeError("Percentage Assignment Service 需要 installation signer port。")
        if not (
            isinstance(advance_service, EvolutionRevalidationOptInStageAdvanceService)
            and isinstance(population_store, ReleasePopulationSnapshotStore)
            and isinstance(plan_service, EvolutionRevalidationRolloutPlanService)
            and isinstance(store, EvolutionRevalidationPercentageCohortAssignmentStore)
            and store.advance_service is advance_service
            and store.population_store is population_store
            and store.plan_service is plan_service
        ):
            raise ValueError("Percentage Assignment Service durable dependency 不一致。")
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        if self.workspace_root != advance_service.workspace_root:
            raise ValueError("Percentage Assignment workspace 必须一致。")
        self.channel = channel
        self.advance_service = advance_service
        self.population_store = population_store
        self.plan_service = plan_service
        self.store = store
        self.sign_assignment_challenge = sign_assignment_challenge
        self.clock = clock or (lambda: datetime.now(UTC))
        self._locks: dict[str, asyncio.Lock] = {}

    async def assign(
        self,
        *,
        stage_completion_evidence_id: str,
        member_id: str,
    ) -> EvolutionRevalidationPercentageCohortAssignmentView:
        lock_key = f"{stage_completion_evidence_id}:{member_id}"
        lock = self._locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            return await self._assign_current(stage_completion_evidence_id, member_id)

    async def _assign_current(self, evidence_id, member_id):
        sources = await self._current_sources(
            evidence_id=evidence_id,
            member_id=member_id,
        )
        advance_view, snapshot_view, plan_view, credential = sources
        advance = advance_view.receipt
        snapshot = snapshot_view.snapshot
        existing = await self.store.get_by_source(
            advance_receipt_id=advance.receipt_id,
            snapshot_id=snapshot.snapshot_id,
            member_id=member_id,
        )
        if existing is not None:
            return await self._view(existing)
        selection = _selection(plan_view.plan, snapshot, member_id)
        proof_payload = _proof_payload(
            workspace_root=self.workspace_root,
            advance=advance,
            snapshot=snapshot,
            credential=credential,
            plan=plan_view.plan,
            selection=selection,
            signed_at=_aware(self.clock()).isoformat(),
        )
        signature_base64 = await self._sign(proof_payload)
        proof = _proof(
            proof_payload,
            credential=credential,
            signature_base64=signature_base64,
        )
        refreshed = await self._current_sources(
            evidence_id=evidence_id,
            member_id=member_id,
        )
        if refreshed != sources:
            raise EvolutionRevalidationPercentageCohortAssignmentError(
                "percentage_assignment_context_changed",
                "Installation 签名期间 Assignment source 已变化。",
            )
        assignment = _assignment(
            workspace_root=self.workspace_root,
            advance=advance,
            snapshot=snapshot,
            credential=credential,
            plan=plan_view.plan,
            selection=selection,
            proof=proof,
        )
        return await self._view(await self.store.record(assignment))

    async def _sign(self, payload):
        try:
            return await self.sign_assignment_challenge(payload.canonical_bytes())
        except (OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationPercentageCohortAssignmentError(
                "percentage_assignment_signer_unavailable",
                "Installation proof signer 当前不可用。",
            ) from exc

    async def inspect(
        self,
        *,
        advance_receipt_id: str,
        snapshot_id: str,
        member_id: str,
    ) -> EvolutionRevalidationPercentageCohortAssignmentView:
        assignment = await self.store.get_by_source(
            advance_receipt_id=advance_receipt_id,
            snapshot_id=snapshot_id,
            member_id=member_id,
        )
        if assignment is None:
            raise EvolutionRevalidationPercentageCohortAssignmentError(
                "percentage_assignment_missing",
                "指定的 Percentage Assignment 不存在。",
            )
        return await self._view(assignment)

    async def _current_sources(self, *, evidence_id, member_id):
        try:
            advance_view = await self.advance_service.inspect(evidence_id=evidence_id)
            snapshot = await self.population_store.latest(self.channel)
            if snapshot is None:
                raise ReleasePopulationRegistryError(
                    "population_snapshot_missing",
                    "Percentage Assignment channel 尚无 Population Snapshot。",
                )
            snapshot_view = await self.population_store.inspect(
                snapshot_id=snapshot.snapshot_id
            )
            plan_view = await self.plan_service.inspect(
                plan_id=advance_view.receipt.plan_id
            )
        except (
            EvolutionRevalidationOptInStageAdvanceError,
            ReleasePopulationRegistryError,
            EvolutionRevalidationRolloutPlanError,
        ) as exc:
            raise EvolutionRevalidationPercentageCohortAssignmentError(
                "percentage_assignment_source_unavailable",
                "Percentage Assignment 缺少 current Advance/Snapshot/Plan。",
            ) from exc
        if not (
            advance_view.percentage_stage_entry_authority
            and snapshot_view.population_snapshot_authority
            and plan_view.current_rollout_eligible
            and plan_view.plan.plan_id == advance_view.receipt.plan_id
            and plan_view.plan.plan_sha256 == advance_view.receipt.plan_sha256
            and snapshot.payload.channel == self.channel
        ):
            raise EvolutionRevalidationPercentageCohortAssignmentError(
                "percentage_assignment_source_denied",
                "Advance、Population Snapshot 或 Plan 当前不允许分配。",
            )
        credential = _credential(snapshot, member_id)
        if credential is None:
            raise EvolutionRevalidationPercentageCohortAssignmentError(
                "percentage_assignment_member_missing",
                "当前 Installation Credential 不在 authoritative Population Snapshot 中。",
            )
        return advance_view, snapshot_view, plan_view, credential

    async def _assignment_current(self, assignment):
        try:
            restored = await self.store.get_by_source(
                advance_receipt_id=assignment.stage_advance.receipt_id,
                snapshot_id=assignment.population_snapshot_id,
                member_id=assignment.member_id,
            )
            return restored == assignment
        except EvolutionRevalidationPercentageCohortAssignmentError:
            return False

    async def _advance_current(self, assignment):
        try:
            advance_view = await self.advance_service.inspect(
                evidence_id=assignment.stage_advance.stage_completion_evidence_id
            )
            return bool(
                advance_view.receipt == assignment.stage_advance
                and advance_view.percentage_stage_entry_authority
            )
        except EvolutionRevalidationOptInStageAdvanceError:
            return False

    async def _population_current(self, assignment):
        try:
            snapshot_view = await self.population_store.inspect(
                snapshot_id=assignment.population_snapshot_id
            )
            snapshot_current = bool(
                snapshot_view.snapshot.snapshot_sha256
                == assignment.population_snapshot_sha256
                and snapshot_view.population_snapshot_authority
            )
            credential = _credential(snapshot_view.snapshot, assignment.member_id)
            credential_current = bool(
                credential is not None
                and credential.credential_id == assignment.credential_id
                and credential.credential_sha256 == assignment.credential_sha256
                and credential.payload.installation_public_key_sha256
                == assignment.installation_public_key_sha256
            )
            return snapshot_view, snapshot_current, credential_current
        except ReleasePopulationRegistryError:
            return None, False, False

    async def _plan_current(self, assignment):
        try:
            plan_view = await self.plan_service.inspect(plan_id=assignment.plan_id)
            current = bool(
                plan_view.current_rollout_eligible
                and plan_view.plan.plan_sha256 == assignment.plan_sha256
                and plan_view.plan.candidate_id == assignment.candidate_id
                and plan_view.plan.candidate_revision == assignment.candidate_revision
            )
            return plan_view, current
        except EvolutionRevalidationRolloutPlanError:
            return None, False

    def _deterministic_current(self, assignment, snapshot_view, plan_view):
        if snapshot_view is not None and plan_view is not None:
            try:
                return _selection_matches(
                    assignment,
                    _selection(
                        plan_view.plan,
                        snapshot_view.snapshot,
                        assignment.member_id,
                    ),
                )
            except (TypeError, ValueError):
                return False
        return False

    async def _view(self, assignment):
        assignment_current = await self._assignment_current(assignment)
        advance_current = await self._advance_current(assignment)
        snapshot_view, snapshot_current, credential_current = (
            await self._population_current(assignment)
        )
        plan_view, plan_current = await self._plan_current(assignment)
        deterministic_current = self._deterministic_current(
            assignment,
            snapshot_view,
            plan_view,
        )
        checks = (
            ("assignment_source_changed", assignment_current),
            ("stage_advance_changed", advance_current),
            ("population_snapshot_changed", snapshot_current),
            ("credential_changed", credential_current),
            ("plan_source_changed", plan_current),
            ("deterministic_assignment_changed", deterministic_current),
        )
        reasons = tuple(sorted(reason for reason, passed in checks if not passed))
        current = bool(
            assignment_current
            and advance_current
            and snapshot_current
            and plan_current
            and credential_current
            and deterministic_current
        )
        return EvolutionRevalidationPercentageCohortAssignmentView(
            assignment=assignment,
            assignment_source_current=assignment_current,
            stage_advance_current=advance_current,
            population_snapshot_current=snapshot_current,
            plan_source_current=plan_current,
            credential_current=credential_current,
            deterministic_assignment_current=deterministic_current,
            invalidation_reasons=reasons,
            population_assignment_enforced=current,
            percentage_cohort_membership_authority=(
                current and assignment.member_selected
            ),
        )


def _selection(plan, snapshot, member_id):
    member_ids = tuple(item.payload.member_id for item in snapshot.payload.credentials)
    if member_id not in member_ids:
        raise EvolutionRevalidationPercentageCohortAssignmentError(
            "percentage_assignment_member_missing",
            "Member 不在当前 Population Snapshot。",
        )
    exposure = plan.stages[2].exposure_percent
    if not 1 <= exposure <= 99:
        raise EvolutionRevalidationPercentageCohortAssignmentError(
            "percentage_assignment_exposure_invalid",
            "Rollout Plan percentage exposure 无效。",
        )
    seed = _digest(
        {
            "domain": "naumi.evolution.percentage-assignment-seed.v1",
            "plan_id": plan.plan_id,
            "plan_sha256": plan.plan_sha256,
            "candidate_id": plan.candidate_id,
            "candidate_revision": plan.candidate_revision,
            "channel": snapshot.payload.channel,
        }
    )
    ranked = tuple(
        sorted(
            member_ids,
            key=lambda item: (
                hashlib.sha256(f"{seed}:{item}".encode()).hexdigest(),
                item,
            ),
        )
    )
    denominator = len(ranked)
    target_count = max(1, math.ceil(denominator * exposure / 100))
    selected = ranked[:target_count]
    return {
        "assignment_seed_sha256": seed,
        "population_denominator": denominator,
        "exposure_percent": exposure,
        "target_member_count": target_count,
        "selected_member_ids": selected,
        "selected_set_sha256": _digest(selected),
        "member_rank": ranked.index(member_id) + 1,
        "member_selected": member_id in selected,
    }


def _proof_payload(*, workspace_root, advance, snapshot, credential, plan, selection, signed_at):
    return EvolutionRevalidationPercentageAssignmentProofPayload(
        workspace_root=str(workspace_root),
        stage_advance_receipt_id=advance.receipt_id,
        stage_advance_receipt_sha256=advance.receipt_sha256,
        population_snapshot_id=snapshot.snapshot_id,
        population_snapshot_sha256=snapshot.snapshot_sha256,
        credential_id=credential.credential_id,
        credential_sha256=credential.credential_sha256,
        member_id=credential.payload.member_id,
        plan_id=plan.plan_id,
        plan_sha256=plan.plan_sha256,
        candidate_id=plan.candidate_id,
        candidate_revision=plan.candidate_revision,
        channel=snapshot.payload.channel,
        assignment_seed_sha256=selection["assignment_seed_sha256"],
        selected_set_sha256=selection["selected_set_sha256"],
        population_denominator=selection["population_denominator"],
        exposure_percent=selection["exposure_percent"],
        target_member_count=selection["target_member_count"],
        member_rank=selection["member_rank"],
        member_selected=selection["member_selected"],
        signed_at=signed_at,
    )


def _proof(payload, *, credential, signature_base64):
    try:
        signature = _decode_base64(
            signature_base64,
            expected_bytes=64,
            label="percentage assignment signature",
        )
    except ValueError as exc:
        raise EvolutionRevalidationPercentageCohortAssignmentError(
            "percentage_assignment_signature_invalid",
            "Installation signer 返回无效 Ed25519 signature。",
        ) from exc
    core = {
        "schema_version": 1,
        "payload": payload.model_dump(mode="json"),
        "installation_public_key_base64": (
            credential.payload.installation_public_key_base64
        ),
        "installation_public_key_sha256": (
            credential.payload.installation_public_key_sha256
        ),
        "signature_algorithm": "ed25519",
        "signature_base64": signature_base64,
        "signature_sha256": hashlib.sha256(signature).hexdigest(),
        "private_key_persisted": False,
    }
    digest = _digest(core)
    try:
        return EvolutionRevalidationPercentageAssignmentProof.model_validate(
            {
                **core,
                "proof_id": f"evrepercentproof_{digest[:24]}",
                "proof_sha256": digest,
            }
        )
    except ValueError as exc:
        raise EvolutionRevalidationPercentageCohortAssignmentError(
            "percentage_assignment_proof_invalid",
            "Installation proof-of-possession 验证失败。",
        ) from exc


def _assignment(*, workspace_root, advance, snapshot, credential, plan, selection, proof):
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_PERCENTAGE_COHORT_ASSIGNMENT_POLICY,
        "workspace_root": str(workspace_root),
        "stage_advance": advance.model_dump(mode="json"),
        "population_snapshot_id": snapshot.snapshot_id,
        "population_snapshot_sha256": snapshot.snapshot_sha256,
        "population_snapshot_sequence": snapshot.payload.sequence,
        "channel": snapshot.payload.channel,
        "credential_id": credential.credential_id,
        "credential_sha256": credential.credential_sha256,
        "member_id": credential.payload.member_id,
        "installation_public_key_sha256": (
            credential.payload.installation_public_key_sha256
        ),
        "plan_id": plan.plan_id,
        "plan_sha256": plan.plan_sha256,
        "candidate_id": plan.candidate_id,
        "candidate_revision": plan.candidate_revision,
        "candidate_target": plan.target_head,
        "assignment_algorithm": EVOLUTION_REVALIDATION_PERCENTAGE_ASSIGNMENT_ALGORITHM,
        **selection,
        "proof": proof.model_dump(mode="json"),
        "assigned_at": proof.payload.signed_at,
        "stage_entry_current_at_issue": True,
        "population_snapshot_current_at_issue": True,
        "proof_of_possession_verified": True,
        "population_assignment_enforced": True,
        "percentage_cohort_membership_authority": selection["member_selected"],
        "percentage_rollout_authority": False,
        "deployment_authority": False,
        "process_started": False,
        "stable_rollout_authority": False,
        "promotion_authority": False,
        "git_write_executed": False,
        "publish_executed": False,
    }
    digest = _digest(core)
    return EvolutionRevalidationPercentageCohortAssignment.model_validate(
        {
            **core,
            "stage_advance": advance,
            "proof": proof,
            "assignment_id": f"evrepercentassign_{digest[:24]}",
            "assignment_sha256": digest,
        }
    )


def _sources_match(item, advance_view, snapshot_view, plan_view) -> bool:
    credential = _credential(snapshot_view.snapshot, item.member_id)
    if not (
        advance_view.receipt == item.stage_advance
        and advance_view.percentage_stage_entry_authority
        and snapshot_view.population_snapshot_authority
        and snapshot_view.snapshot.snapshot_id == item.population_snapshot_id
        and snapshot_view.snapshot.snapshot_sha256 == item.population_snapshot_sha256
        and plan_view.current_rollout_eligible
        and plan_view.plan.plan_id == item.plan_id
        and plan_view.plan.plan_sha256 == item.plan_sha256
        and credential is not None
        and credential.credential_id == item.credential_id
        and credential.credential_sha256 == item.credential_sha256
    ):
        return False
    return _selection_matches(
        item,
        _selection(plan_view.plan, snapshot_view.snapshot, item.member_id),
    )


def _selection_matches(item, selection) -> bool:
    return bool(
        item.assignment_seed_sha256 == selection["assignment_seed_sha256"]
        and item.population_denominator == selection["population_denominator"]
        and item.exposure_percent == selection["exposure_percent"]
        and item.target_member_count == selection["target_member_count"]
        and item.selected_member_ids == selection["selected_member_ids"]
        and item.selected_set_sha256 == selection["selected_set_sha256"]
        and item.member_rank == selection["member_rank"]
        and item.member_selected is selection["member_selected"]
    )


def _credential(
    snapshot: ReleasePopulationSnapshot,
    member_id: str,
) -> ReleaseManagedInstallationCredential | None:
    return next(
        (
            item
            for item in snapshot.payload.credentials
            if item.payload.member_id == member_id
        ),
        None,
    )


def _same_source(left, right) -> bool:
    return bool(
        left.stage_advance.receipt_id == right.stage_advance.receipt_id
        and left.stage_advance.receipt_sha256 == right.stage_advance.receipt_sha256
        and left.population_snapshot_id == right.population_snapshot_id
        and left.population_snapshot_sha256 == right.population_snapshot_sha256
        and left.member_id == right.member_id
        and left.credential_sha256 == right.credential_sha256
        and left.assignment_seed_sha256 == right.assignment_seed_sha256
        and left.selected_set_sha256 == right.selected_set_sha256
        and left.member_rank == right.member_rank
        and left.member_selected is right.member_selected
    )


def _validated(value) -> EvolutionRevalidationPercentageCohortAssignment:
    try:
        return EvolutionRevalidationPercentageCohortAssignment.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionRevalidationPercentageCohortAssignmentError(
            "percentage_assignment_invalid",
            "Percentage Cohort Assignment artifact 无效。",
        ) from exc


def _restore(value: str) -> EvolutionRevalidationPercentageCohortAssignment:
    if len(value.encode()) > _MAX_ARTIFACT_BYTES:
        raise ValueError("Percentage Assignment durable source 超过 4 MiB。")
    return EvolutionRevalidationPercentageCohortAssignment.model_validate_json(value)


def _decode_base64(value, *, expected_bytes, label):
    try:
        decoded = base64.b64decode(value, validate=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} 不是 canonical Base64。") from exc
    if len(decoded) != expected_bytes:
        raise ValueError(f"{label} 长度无效。")
    return decoded


def _aware(value) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Percentage Assignment timestamp 必须包含 offset。")
    return parsed.astimezone(UTC)


def _canonical_bytes(payload) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _digest(payload) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


async def _ensure_schema(db) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS "
        "evolution_revalidation_percentage_cohort_assignments ("
        "assignment_id TEXT PRIMARY KEY, assignment_sha256 TEXT NOT NULL UNIQUE, "
        "advance_receipt_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, "
        "member_id TEXT NOT NULL, assignment_json TEXT NOT NULL, assigned_at TEXT NOT NULL, "
        "UNIQUE(advance_receipt_id, snapshot_id, member_id))"
    )
    await db.commit()


__all__ = [
    "EVOLUTION_REVALIDATION_PERCENTAGE_ASSIGNMENT_ALGORITHM",
    "EVOLUTION_REVALIDATION_PERCENTAGE_ASSIGNMENT_PROOF_DOMAIN",
    "EVOLUTION_REVALIDATION_PERCENTAGE_COHORT_ASSIGNMENT_POLICY",
    "EvolutionRevalidationPercentageAssignmentProof",
    "EvolutionRevalidationPercentageAssignmentProofPayload",
    "EvolutionRevalidationPercentageCohortAssignment",
    "EvolutionRevalidationPercentageCohortAssignmentError",
    "EvolutionRevalidationPercentageCohortAssignmentService",
    "EvolutionRevalidationPercentageCohortAssignmentStore",
    "EvolutionRevalidationPercentageCohortAssignmentView",
]
