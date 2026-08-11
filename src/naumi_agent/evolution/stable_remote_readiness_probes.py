"""Fresh, installation-signed Release Store probes for remote stable readiness."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Self
from weakref import WeakValueDictionary

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.stable_remote_readiness_claims import (
    EvolutionStableRemoteReadinessClaimError,
    EvolutionStableRemoteReadinessClaimService,
)
from naumi_agent.release.installation_keys import (
    ReleaseInstallationKeyError,
    ReleaseInstallationKeyService,
    ReleaseInstallationSignature,
    verify_release_installation_signature,
)
from naumi_agent.release.population_registry import (
    ReleaseManagedInstallationCredential,
    ReleasePopulationRegistryError,
)
from naumi_agent.release.slots import ReleaseSlotError, ReleaseSlotStore

EVOLUTION_STABLE_REMOTE_READINESS_PROBE_POLICY = (
    "evolution-stable-remote-readiness-probe-v1"
)
_CHALLENGE_RE = re.compile(r"^evstableremoteprobechal_[0-9a-f]{24}$")
_RECEIPT_RE = re.compile(r"^evstableremoteprobereceipt_[0-9a-f]{24}$")
_MAX_ENCODED_BYTES = 512 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionStableRemoteReadinessProbeChallenge(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-stable-remote-readiness-probe-v1"] = (
        EVOLUTION_STABLE_REMOTE_READINESS_PROBE_POLICY
    )
    challenge_id: str = Field(pattern=r"^evstableremoteprobechal_[0-9a-f]{24}$")
    challenge_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    claim_receipt_id: str = Field(pattern=r"^evstableremoteclaim_[0-9a-f]{24}$")
    claim_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    completion_receipt_id: str = Field(pattern=r"^evstablepopcomplete_[0-9a-f]{24}$")
    completion_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    population_snapshot_id: str = Field(pattern=r"^relpopsnapshot_[0-9a-f]{24}$")
    population_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    installation_member_id: str = Field(pattern=r"^relpopmember_[0-9a-f]{24}$")
    installation_credential_id: str = Field(pattern=r"^relpopcred_[0-9a-f]{24}$")
    installation_credential_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    installation_public_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stable_intent_id: str = Field(pattern=r"^evrestableintent_[0-9a-f]{24}$")
    candidate_version: str = Field(min_length=1, max_length=128)
    candidate_target: str = Field(pattern=r"^(?:macos|linux|windows)-(?:arm64|x64)$")
    expected_active_pointer_id: str = Field(pattern=r"^relactive_[0-9a-f]{24}$")
    expected_active_pointer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_active_pointer_generation: int = Field(ge=2, le=1_000_000_000)
    expected_candidate_slot_id: str = Field(pattern=r"^relslot_[0-9a-f]{24}$")
    expected_candidate_slot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_candidate_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_rollback_pointer_id: str = Field(pattern=r"^relactive_[0-9a-f]{24}$")
    expected_rollback_pointer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_rollback_pointer_generation: int = Field(ge=1, le=999_999_999)
    expected_rollback_slot_id: str = Field(pattern=r"^relslot_[0-9a-f]{24}$")
    expected_rollback_slot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_rollback_boot_receipt_id: str = Field(pattern=r"^relboot_[0-9a-f]{24}$")
    expected_rollback_boot_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    nonce_base64: str = Field(pattern=r"^[A-Za-z0-9+/]{43}=$")
    nonce_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validity_seconds: int = Field(ge=60, le=300)
    maximum_probe_seconds: Literal[30] = 30
    issued_at: str = Field(min_length=1, max_length=100)
    expires_at: str = Field(min_length=1, max_length=100)
    release_store_mutation_authority: Literal[False] = False
    source_runtime_revalidation_authority: Literal[False] = False
    binary_rollback_readiness_authority: Literal[False] = False
    stable_rollout_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        nonce = _decode_base64(self.nonce_base64, 32, "probe nonce")
        issued = _aware(self.issued_at)
        if not (
            hashlib.sha256(nonce).hexdigest() == self.nonce_sha256
            and _aware(self.expires_at)
            == issued + timedelta(seconds=self.validity_seconds)
            and self.expected_rollback_pointer_generation + 1
            == self.expected_active_pointer_generation
            and self.expected_candidate_slot_id != self.expected_rollback_slot_id
        ):
            raise ValueError("Remote Readiness Probe challenge binding 无效。")
        digest = _digest(
            self.model_dump(mode="json", exclude={"challenge_id", "challenge_sha256"})
        )
        if self.challenge_sha256 != digest or self.challenge_id != (
            f"evstableremoteprobechal_{digest[:24]}"
        ):
            raise ValueError("Remote Readiness Probe challenge identity 不一致。")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.model_dump(mode="json"))


class EvolutionStableRemoteReadinessProbeResult(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-stable-remote-readiness-probe-v1"] = (
        EVOLUTION_STABLE_REMOTE_READINESS_PROBE_POLICY
    )
    result_id: str = Field(pattern=r"^evstableremoteproberesult_[0-9a-f]{24}$")
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    challenge_id: str = Field(pattern=r"^evstableremoteprobechal_[0-9a-f]{24}$")
    challenge_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    claim_receipt_id: str = Field(pattern=r"^evstableremoteclaim_[0-9a-f]{24}$")
    claim_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    installation_member_id: str = Field(pattern=r"^relpopmember_[0-9a-f]{24}$")
    installation_credential_id: str = Field(pattern=r"^relpopcred_[0-9a-f]{24}$")
    release_root_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    active_pointer_id: str = Field(pattern=r"^relactive_[0-9a-f]{24}$")
    active_pointer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    active_pointer_generation: int = Field(ge=2, le=1_000_000_000)
    candidate_version: str = Field(min_length=1, max_length=128)
    candidate_target: str = Field(pattern=r"^(?:macos|linux|windows)-(?:arm64|x64)$")
    candidate_slot_id: str = Field(pattern=r"^relslot_[0-9a-f]{24}$")
    candidate_slot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_boot_receipt_id: str = Field(pattern=r"^relboot_[0-9a-f]{24}$")
    candidate_boot_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_binary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rollback_pointer_id: str = Field(pattern=r"^relactive_[0-9a-f]{24}$")
    rollback_pointer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rollback_pointer_generation: int = Field(ge=1, le=999_999_999)
    rollback_version: str = Field(min_length=1, max_length=128)
    rollback_target: str = Field(pattern=r"^(?:macos|linux|windows)-(?:arm64|x64)$")
    rollback_slot_id: str = Field(pattern=r"^relslot_[0-9a-f]{24}$")
    rollback_slot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rollback_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rollback_boot_receipt_id: str = Field(pattern=r"^relboot_[0-9a-f]{24}$")
    rollback_boot_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rollback_binary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    probe_started_at: str = Field(min_length=1, max_length=100)
    probe_completed_at: str = Field(min_length=1, max_length=100)
    expires_at: str = Field(min_length=1, max_length=100)
    active_pointer_reread_consistent: Literal[True] = True
    candidate_bytes_revalidated: Literal[True] = True
    rollback_bytes_revalidated: Literal[True] = True
    previous_slot_retained: Literal[True] = True
    previous_slot_bootable: Literal[True] = True
    expected_pointer_cas_ready: Literal[True] = True
    source_runtime_revalidated: Literal[True] = True
    release_store_mutation_performed: Literal[False] = False
    stable_rollout_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        started = _aware(self.probe_started_at)
        completed = _aware(self.probe_completed_at)
        if not (
            started <= completed < _aware(self.expires_at)
            and (completed - started).total_seconds() <= 30
            and self.rollback_pointer_generation + 1
            == self.active_pointer_generation
            and self.candidate_slot_id != self.rollback_slot_id
            and self.candidate_target == self.rollback_target
            and self.source_set_sha256 == _digest(_source_projection(self))
        ):
            raise ValueError("Remote Readiness Probe result source projection 无效。")
        digest = _digest(
            self.model_dump(mode="json", exclude={"result_id", "result_sha256"})
        )
        if self.result_sha256 != digest or self.result_id != (
            f"evstableremoteproberesult_{digest[:24]}"
        ):
            raise ValueError("Remote Readiness Probe result identity 不一致。")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.model_dump(mode="json"))


class EvolutionStableRemoteReadinessProbeSubmission(_StrictModel):
    schema_version: Literal[1] = 1
    result: EvolutionStableRemoteReadinessProbeResult
    signature: ReleaseInstallationSignature

    @model_validator(mode="after")
    def _binding(self) -> Self:
        if not (
            self.signature.credential_id == self.result.installation_credential_id
            and self.signature.installation_member_id
            == self.result.installation_member_id
            and self.signature.payload_sha256
            == hashlib.sha256(self.result.canonical_bytes()).hexdigest()
            and self.signature.payload_bytes == len(self.result.canonical_bytes())
            and _aware(self.result.probe_completed_at)
            <= _aware(self.signature.signed_at)
            < _aware(self.result.expires_at)
        ):
            raise ValueError("Remote Readiness Probe submission signature binding 无效。")
        return self


class EvolutionStableRemoteReadinessProbeReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-stable-remote-readiness-probe-v1"] = (
        EVOLUTION_STABLE_REMOTE_READINESS_PROBE_POLICY
    )
    receipt_id: str = Field(pattern=r"^evstableremoteprobereceipt_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    challenge: EvolutionStableRemoteReadinessProbeChallenge
    submission: EvolutionStableRemoteReadinessProbeSubmission
    recorded_at: str = Field(min_length=1, max_length=100)
    authenticated_installation_claim_observed: Literal[True] = True
    source_runtime_revalidated: Literal[True] = True
    binary_rollback_material_verified: Literal[True] = True
    config_data_rollback_readiness_authority: Literal[False] = False
    stable_rollout_authority: Literal[False] = False
    remote_execution_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        result = self.submission.result
        if not (
            result.challenge_id == self.challenge.challenge_id
            and result.challenge_sha256 == self.challenge.challenge_sha256
            and result.claim_receipt_id == self.challenge.claim_receipt_id
            and result.claim_receipt_sha256 == self.challenge.claim_receipt_sha256
            and result.installation_member_id
            == self.challenge.installation_member_id
            and result.installation_credential_id
            == self.challenge.installation_credential_id
            and _aware(self.submission.signature.signed_at)
            <= _aware(self.recorded_at)
            < _aware(self.challenge.expires_at)
        ):
            raise ValueError("Remote Readiness Probe receipt binding/time 无效。")
        digest = _digest(
            self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        )
        if self.receipt_sha256 != digest or self.receipt_id != (
            f"evstableremoteprobereceipt_{digest[:24]}"
        ):
            raise ValueError("Remote Readiness Probe receipt identity 不一致。")
        return self


class EvolutionStableRemoteReadinessProbeView(_StrictModel):
    receipt: EvolutionStableRemoteReadinessProbeReceipt
    durable_challenge_current: bool
    authenticated_claim_current: bool
    credential_current: bool
    signature_valid: bool
    freshness_current: bool
    source_runtime_revalidation_authority: bool
    binary_rollback_readiness_authority: bool
    config_data_rollback_readiness_authority: Literal[False] = False
    stable_rollout_authority: Literal[False] = False
    remote_execution_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        expected = bool(
            self.durable_challenge_current
            and self.authenticated_claim_current
            and self.credential_current
            and self.signature_valid
            and self.freshness_current
        )
        if not (
            self.source_runtime_revalidation_authority is expected
            and self.binary_rollback_readiness_authority is expected
        ):
            raise ValueError("Remote Readiness Probe authority projection 不一致。")
        return self


class EvolutionStableRemoteReadinessProbeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionStableRemoteReadinessProbeStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def get_challenge(
        self,
        challenge_id: str,
    ) -> EvolutionStableRemoteReadinessProbeChallenge | None:
        _challenge_id(challenge_id)
        return await self._read(
            table="evolution_stable_remote_readiness_probe_challenges",
            column="challenge_json",
            where="challenge_id = ?",
            parameters=(challenge_id,),
            restore=EvolutionStableRemoteReadinessProbeChallenge.model_validate_json,
        )

    async def get_challenge_by_claim(
        self,
        claim_receipt_id: str,
    ) -> EvolutionStableRemoteReadinessProbeChallenge | None:
        _claim_receipt_id(claim_receipt_id)
        return await self._read(
            table="evolution_stable_remote_readiness_probe_challenges",
            column="challenge_json",
            where="claim_receipt_id = ?",
            parameters=(str(claim_receipt_id),),
            restore=EvolutionStableRemoteReadinessProbeChallenge.model_validate_json,
        )

    async def get_receipt(
        self,
        receipt_id: str,
    ) -> EvolutionStableRemoteReadinessProbeReceipt | None:
        _receipt_id(receipt_id)
        return await self._read(
            table="evolution_stable_remote_readiness_probe_receipts",
            column="receipt_json",
            where="receipt_id = ?",
            parameters=(receipt_id,),
            restore=EvolutionStableRemoteReadinessProbeReceipt.model_validate_json,
        )

    async def get_receipt_by_challenge(
        self,
        challenge_id: str,
    ) -> EvolutionStableRemoteReadinessProbeReceipt | None:
        return await self._read(
            table="evolution_stable_remote_readiness_probe_receipts",
            column="receipt_json",
            where="challenge_id = ?",
            parameters=(challenge_id,),
            restore=EvolutionStableRemoteReadinessProbeReceipt.model_validate_json,
        )

    async def _read(self, *, table, column, where, parameters, restore):
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        f"SELECT {column} FROM {table} WHERE {where}",
                        parameters,
                    )
                ).fetchone()
            return None if row is None else restore(row[column])
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStableRemoteReadinessProbeError(
                "stable_remote_probe_store_corrupt",
                "Remote Readiness Probe durable artifact 损坏或无法读取。",
            ) from exc

    async def record_challenge(
        self,
        challenge: EvolutionStableRemoteReadinessProbeChallenge,
    ) -> EvolutionStableRemoteReadinessProbeChallenge:
        item = EvolutionStableRemoteReadinessProbeChallenge.model_validate_json(
            challenge.model_dump_json()
        )
        encoded = item.model_dump_json()
        _bounded(encoded)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT challenge_json FROM "
                    "evolution_stable_remote_readiness_probe_challenges "
                    "WHERE claim_receipt_id = ?",
                    (item.claim_receipt_id,),
                )
            ).fetchone()
            if row is not None:
                restored = EvolutionStableRemoteReadinessProbeChallenge.model_validate_json(
                    row["challenge_json"]
                )
                await db.rollback()
                return restored
            await db.execute(
                "INSERT INTO evolution_stable_remote_readiness_probe_challenges "
                "(challenge_id, challenge_sha256, claim_receipt_id, challenge_json, "
                "issued_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    item.challenge_id,
                    item.challenge_sha256,
                    item.claim_receipt_id,
                    encoded,
                    item.issued_at,
                    item.expires_at,
                ),
            )
            await db.commit()
        return item

    async def record_receipt(
        self,
        receipt: EvolutionStableRemoteReadinessProbeReceipt,
    ) -> EvolutionStableRemoteReadinessProbeReceipt:
        item = EvolutionStableRemoteReadinessProbeReceipt.model_validate_json(
            receipt.model_dump_json()
        )
        encoded = item.model_dump_json()
        _bounded(encoded)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            source = await (
                await db.execute(
                    "SELECT challenge_json FROM "
                    "evolution_stable_remote_readiness_probe_challenges "
                    "WHERE challenge_id = ?",
                    (item.challenge.challenge_id,),
                )
            ).fetchone()
            if source is None or (
                EvolutionStableRemoteReadinessProbeChallenge.model_validate_json(
                    source["challenge_json"]
                )
                != item.challenge
            ):
                await db.rollback()
                raise EvolutionStableRemoteReadinessProbeError(
                    "stable_remote_probe_challenge_changed",
                    "Remote Readiness Probe challenge durable source 已变化。",
                )
            existing = await (
                await db.execute(
                    "SELECT receipt_json FROM "
                    "evolution_stable_remote_readiness_probe_receipts "
                    "WHERE challenge_id = ?",
                    (item.challenge.challenge_id,),
                )
            ).fetchone()
            if existing is not None:
                restored = EvolutionStableRemoteReadinessProbeReceipt.model_validate_json(
                    existing["receipt_json"]
                )
                await db.rollback()
                if restored != item:
                    raise EvolutionStableRemoteReadinessProbeError(
                        "stable_remote_probe_receipt_conflict",
                        "同一 Probe challenge 已绑定不同结果。",
                    )
                return restored
            await db.execute(
                "INSERT INTO evolution_stable_remote_readiness_probe_receipts "
                "(receipt_id, receipt_sha256, challenge_id, claim_receipt_id, "
                "receipt_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    item.receipt_id,
                    item.receipt_sha256,
                    item.challenge.challenge_id,
                    item.challenge.claim_receipt_id,
                    encoded,
                    item.recorded_at,
                ),
            )
            await db.commit()
        return item


class EvolutionStableRemoteReadinessProbeService:
    def __init__(
        self,
        *,
        claim_service: EvolutionStableRemoteReadinessClaimService,
        store: EvolutionStableRemoteReadinessProbeStore,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        if claim_service.store.db_path != store.db_path:
            raise ValueError("Remote Probe 必须与 Remote Claim 共用 authority DB。")
        self.claim_service = claim_service
        self.store = store
        self.clock = clock
        self.random_bytes = random_bytes
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def prepare(
        self,
        *,
        claim_receipt_id: str,
        validity_seconds: int = 180,
    ) -> EvolutionStableRemoteReadinessProbeChallenge:
        if not 60 <= validity_seconds <= 300:
            raise ValueError("validity_seconds 必须在 60..300。")
        lock = self._locks.setdefault(str(claim_receipt_id), asyncio.Lock())
        async with lock:
            existing = await self.store.get_challenge_by_claim(claim_receipt_id)
            if existing is not None:
                return existing
            claim = await self._current_claim(claim_receipt_id)
            receipt = claim.receipt
            expected = receipt.assertion
            nonce = self.random_bytes(32)
            if not isinstance(nonce, bytes) or len(nonce) != 32:
                raise EvolutionStableRemoteReadinessProbeError(
                    "stable_remote_probe_nonce_invalid",
                    "Probe nonce generator 必须返回 32 bytes。",
                )
            now = _aware(self.clock())
            claim_expiry = _aware(receipt.challenge.expires_at)
            actual_validity = min(
                validity_seconds,
                int((claim_expiry - now).total_seconds()),
            )
            if actual_validity < 60:
                raise EvolutionStableRemoteReadinessProbeError(
                    "stable_remote_probe_claim_window_too_short",
                    "Remote Claim 剩余窗口不足 60 秒，不能签发 Probe。",
                )
            expires = now + timedelta(seconds=actual_validity)
            core = {
                "schema_version": 1,
                "policy_version": EVOLUTION_STABLE_REMOTE_READINESS_PROBE_POLICY,
                "claim_receipt_id": receipt.receipt_id,
                "claim_receipt_sha256": receipt.receipt_sha256,
                "completion_receipt_id": receipt.challenge.completion_receipt_id,
                "completion_receipt_sha256": receipt.challenge.completion_receipt_sha256,
                "population_snapshot_id": receipt.challenge.population_snapshot_id,
                "population_snapshot_sha256": receipt.challenge.population_snapshot_sha256,
                "installation_member_id": receipt.challenge.installation_member_id,
                "installation_credential_id": receipt.challenge.installation_credential_id,
                "installation_credential_sha256": receipt.challenge.installation_credential_sha256,
                "installation_public_key_sha256": receipt.challenge.installation_public_key_sha256,
                "stable_intent_id": receipt.challenge.stable_intent_id,
                "candidate_version": receipt.challenge.candidate_version,
                "candidate_target": receipt.challenge.candidate_target,
                "expected_active_pointer_id": expected.active_pointer_id,
                "expected_active_pointer_sha256": expected.active_pointer_sha256,
                "expected_active_pointer_generation": expected.active_pointer_generation,
                "expected_candidate_slot_id": expected.candidate_slot_id,
                "expected_candidate_slot_sha256": expected.candidate_slot_sha256,
                "expected_candidate_manifest_sha256": expected.candidate_manifest_sha256,
                "expected_rollback_pointer_id": expected.rollback_pointer_id,
                "expected_rollback_pointer_sha256": expected.rollback_pointer_sha256,
                "expected_rollback_pointer_generation": expected.rollback_pointer_generation,
                "expected_rollback_slot_id": expected.rollback_slot_id,
                "expected_rollback_slot_sha256": expected.rollback_slot_sha256,
                "expected_rollback_boot_receipt_id": expected.rollback_boot_receipt_id,
                "expected_rollback_boot_receipt_sha256": expected.rollback_boot_receipt_sha256,
                "nonce_base64": base64.b64encode(nonce).decode("ascii"),
                "nonce_sha256": hashlib.sha256(nonce).hexdigest(),
                "validity_seconds": actual_validity,
                "maximum_probe_seconds": 30,
                "issued_at": now.isoformat(),
                "expires_at": expires.isoformat(),
                "release_store_mutation_authority": False,
                "source_runtime_revalidation_authority": False,
                "binary_rollback_readiness_authority": False,
                "stable_rollout_authority": False,
                "promotion_authority": False,
            }
            digest = _digest(core)
            challenge = EvolutionStableRemoteReadinessProbeChallenge.model_validate({
                **core,
                "challenge_id": f"evstableremoteprobechal_{digest[:24]}",
                "challenge_sha256": digest,
            })
            return await self.store.record_challenge(challenge)

    async def ingest(
        self,
        *,
        challenge_id: str,
        submission_base64: str,
    ) -> EvolutionStableRemoteReadinessProbeView:
        _challenge_id(challenge_id)
        submission = decode_stable_remote_readiness_probe_submission(
            submission_base64
        )
        lock = self._locks.setdefault(challenge_id, asyncio.Lock())
        async with lock:
            existing = await self.store.get_receipt_by_challenge(challenge_id)
            if existing is not None:
                if existing.submission != submission:
                    raise EvolutionStableRemoteReadinessProbeError(
                        "stable_remote_probe_receipt_conflict",
                        "同一 Probe challenge 已绑定不同结果。",
                    )
                return await self.inspect(receipt_id=existing.receipt_id)
            challenge = await self.store.get_challenge(challenge_id)
            if challenge is None:
                raise EvolutionStableRemoteReadinessProbeError(
                    "stable_remote_probe_challenge_missing",
                    "Remote Readiness Probe challenge 不存在。",
                )
            now = _aware(self.clock())
            if now >= _aware(challenge.expires_at):
                raise EvolutionStableRemoteReadinessProbeError(
                    "stable_remote_probe_challenge_expired",
                    "Remote Readiness Probe challenge 已过期。",
                )
            self._validate_result_binding(challenge, submission.result)
            claim = await self._current_claim(challenge.claim_receipt_id)
            credential = await self._current_credential(challenge, claim)
            try:
                verify_release_installation_signature(
                    credential=credential,
                    payload=submission.result.canonical_bytes(),
                    artifact=submission.signature,
                )
            except ReleaseInstallationKeyError as exc:
                raise EvolutionStableRemoteReadinessProbeError(
                    exc.code,
                    "Remote Readiness Probe installation signature 无效。",
                ) from exc
            core = {
                "schema_version": 1,
                "policy_version": EVOLUTION_STABLE_REMOTE_READINESS_PROBE_POLICY,
                "challenge": challenge.model_dump(mode="json"),
                "submission": submission.model_dump(mode="json"),
                "recorded_at": now.isoformat(),
                "authenticated_installation_claim_observed": True,
                "source_runtime_revalidated": True,
                "binary_rollback_material_verified": True,
                "config_data_rollback_readiness_authority": False,
                "stable_rollout_authority": False,
                "remote_execution_authority": False,
                "promotion_authority": False,
            }
            digest = _digest(core)
            receipt = EvolutionStableRemoteReadinessProbeReceipt.model_validate({
                **core,
                "receipt_id": f"evstableremoteprobereceipt_{digest[:24]}",
                "receipt_sha256": digest,
            })
            await self.store.record_receipt(receipt)
        return await self.inspect(receipt_id=receipt.receipt_id)

    async def inspect(
        self,
        *,
        receipt_id: str,
    ) -> EvolutionStableRemoteReadinessProbeView:
        receipt = await self.store.get_receipt(receipt_id)
        if receipt is None:
            raise EvolutionStableRemoteReadinessProbeError(
                "stable_remote_probe_receipt_missing",
                "Remote Readiness Probe receipt 不存在。",
            )
        durable = claim_ok = credential_ok = signature_ok = False
        try:
            durable_challenge = await self.store.get_challenge(
                receipt.challenge.challenge_id
            )
            durable = durable_challenge == receipt.challenge
            claim = await self._current_claim(receipt.challenge.claim_receipt_id)
            claim_ok = claim.receipt.receipt_sha256 == (
                receipt.challenge.claim_receipt_sha256
            )
            credential = await self._current_credential(receipt.challenge, claim)
            credential_ok = True
            verify_release_installation_signature(
                credential=credential,
                payload=receipt.submission.result.canonical_bytes(),
                artifact=receipt.submission.signature,
            )
            signature_ok = True
        except (
            EvolutionStableRemoteReadinessClaimError,
            EvolutionStableRemoteReadinessProbeError,
            ReleaseInstallationKeyError,
            ReleasePopulationRegistryError,
            OSError,
            TypeError,
            ValueError,
        ):
            pass
        fresh = _aware(self.clock()) < _aware(receipt.submission.result.expires_at)
        authority = bool(durable and claim_ok and credential_ok and signature_ok and fresh)
        return EvolutionStableRemoteReadinessProbeView(
            receipt=receipt,
            durable_challenge_current=durable,
            authenticated_claim_current=claim_ok,
            credential_current=credential_ok,
            signature_valid=signature_ok,
            freshness_current=fresh,
            source_runtime_revalidation_authority=authority,
            binary_rollback_readiness_authority=authority,
        )

    async def _current_claim(self, claim_receipt_id: str):
        try:
            claim = await self.claim_service.inspect(receipt_id=claim_receipt_id)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise EvolutionStableRemoteReadinessProbeError(
                "stable_remote_probe_claim_unavailable",
                "Authenticated Remote Readiness Claim 不可用。",
            ) from exc
        if not claim.authenticated_claim_authority:
            raise EvolutionStableRemoteReadinessProbeError(
                "stable_remote_probe_claim_not_current",
                "Authenticated Remote Readiness Claim 已失效。",
            )
        return claim

    async def _current_credential(self, challenge, claim):
        try:
            snapshot = await self.claim_service.population_store.inspect(
                snapshot_id=challenge.population_snapshot_id
            )
            if not snapshot.population_snapshot_authority:
                raise ValueError("snapshot stale")
            credential = next(
                item
                for item in snapshot.snapshot.payload.credentials
                if item.payload.member_id == challenge.installation_member_id
            )
        except (ReleasePopulationRegistryError, StopIteration, ValueError) as exc:
            raise EvolutionStableRemoteReadinessProbeError(
                "stable_remote_probe_credential_not_current",
                "Population Credential 当前不可用。",
            ) from exc
        if not (
            credential.credential_id == challenge.installation_credential_id
            and credential.credential_sha256
            == challenge.installation_credential_sha256
            and credential.payload.installation_public_key_sha256
            == challenge.installation_public_key_sha256
            and claim.receipt.challenge.population_snapshot_sha256
            == challenge.population_snapshot_sha256
        ):
            raise EvolutionStableRemoteReadinessProbeError(
                "stable_remote_probe_credential_changed",
                "Population Credential 或 Snapshot 已变化。",
            )
        return credential

    @staticmethod
    def _validate_result_binding(challenge, result) -> None:
        pairs = (
            (result.challenge_id, challenge.challenge_id),
            (result.challenge_sha256, challenge.challenge_sha256),
            (result.claim_receipt_id, challenge.claim_receipt_id),
            (result.claim_receipt_sha256, challenge.claim_receipt_sha256),
            (result.installation_member_id, challenge.installation_member_id),
            (result.installation_credential_id, challenge.installation_credential_id),
            (result.active_pointer_id, challenge.expected_active_pointer_id),
            (result.active_pointer_sha256, challenge.expected_active_pointer_sha256),
            (
                result.active_pointer_generation,
                challenge.expected_active_pointer_generation,
            ),
            (result.candidate_version, challenge.candidate_version),
            (result.candidate_target, challenge.candidate_target),
            (result.candidate_slot_id, challenge.expected_candidate_slot_id),
            (result.candidate_slot_sha256, challenge.expected_candidate_slot_sha256),
            (
                result.candidate_manifest_sha256,
                challenge.expected_candidate_manifest_sha256,
            ),
            (result.rollback_pointer_id, challenge.expected_rollback_pointer_id),
            (
                result.rollback_pointer_sha256,
                challenge.expected_rollback_pointer_sha256,
            ),
            (
                result.rollback_pointer_generation,
                challenge.expected_rollback_pointer_generation,
            ),
            (result.rollback_slot_id, challenge.expected_rollback_slot_id),
            (result.rollback_slot_sha256, challenge.expected_rollback_slot_sha256),
            (
                result.rollback_boot_receipt_id,
                challenge.expected_rollback_boot_receipt_id,
            ),
            (
                result.rollback_boot_receipt_sha256,
                challenge.expected_rollback_boot_receipt_sha256,
            ),
            (result.expires_at, challenge.expires_at),
        )
        if any(actual != expected for actual, expected in pairs):
            raise EvolutionStableRemoteReadinessProbeError(
                "stable_remote_probe_result_mismatch",
                "Remote Readiness Probe result 与 challenge 不一致。",
            )


def execute_stable_remote_readiness_probe(
    *,
    challenge: EvolutionStableRemoteReadinessProbeChallenge,
    credential: ReleaseManagedInstallationCredential,
    release_slot_store: ReleaseSlotStore,
    installation_key_service: ReleaseInstallationKeyService,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> EvolutionStableRemoteReadinessProbeSubmission:
    if not isinstance(release_slot_store, ReleaseSlotStore):
        raise TypeError("Remote Probe 需要 ReleaseSlotStore。")
    if not isinstance(installation_key_service, ReleaseInstallationKeyService):
        raise TypeError("Remote Probe 需要 ReleaseInstallationKeyService。")
    if release_slot_store.release_root != installation_key_service.release_root:
        raise EvolutionStableRemoteReadinessProbeError(
            "stable_remote_probe_release_root_mismatch",
            "Release Store 与 installation key 不属于同一 install root。",
        )
    started = _aware(clock())
    if not (_aware(challenge.issued_at) <= started < _aware(challenge.expires_at)):
        raise EvolutionStableRemoteReadinessProbeError(
            "stable_remote_probe_challenge_not_current",
            "Remote Readiness Probe challenge 尚未生效或已经过期。",
        )
    if not (
        credential.credential_id == challenge.installation_credential_id
        and credential.credential_sha256 == challenge.installation_credential_sha256
        and credential.payload.member_id == challenge.installation_member_id
        and credential.payload.installation_public_key_sha256
        == challenge.installation_public_key_sha256
    ):
        raise EvolutionStableRemoteReadinessProbeError(
            "stable_remote_probe_credential_mismatch",
            "本机 Population Credential 与 Probe challenge 不匹配。",
        )
    try:
        active = release_slot_store.active()
        if active is None:
            raise ReleaseSlotError(
                "release_active_pointer_missing",
                "本机不存在 active release pointer。",
            )
        prior = release_slot_store.get_activation_event(active.generation - 1)
        if prior is None:
            raise ReleaseSlotError(
                "release_previous_pointer_missing",
                "本机不存在 previous activation event。",
            )
        candidate = release_slot_store.resolve_booted_slot(
            active.current_slot_id,
            active.boot_receipt_id,
        )
        rollback = release_slot_store.resolve_booted_slot(
            prior.current_slot_id,
            prior.boot_receipt_id,
        )
        active_after = release_slot_store.active()
    except (ReleaseSlotError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise EvolutionStableRemoteReadinessProbeError(
            _safe_code(getattr(exc, "code", "stable_remote_probe_release_store_failed")),
            "远端 Release Store 机械重验失败。",
        ) from exc
    if active_after != active:
        raise EvolutionStableRemoteReadinessProbeError(
            "stable_remote_probe_pointer_changed_during_read",
            "Release active pointer 在 Probe 期间发生变化。",
        )
    expected = (
        (active.pointer_id, challenge.expected_active_pointer_id),
        (active.pointer_sha256, challenge.expected_active_pointer_sha256),
        (active.generation, challenge.expected_active_pointer_generation),
        (candidate.slot.slot_id, challenge.expected_candidate_slot_id),
        (candidate.slot.slot_sha256, challenge.expected_candidate_slot_sha256),
        (
            candidate.slot.manifest_sha256,
            challenge.expected_candidate_manifest_sha256,
        ),
        (candidate.slot.version, challenge.candidate_version),
        (candidate.slot.target, challenge.candidate_target),
        (prior.pointer_id, challenge.expected_rollback_pointer_id),
        (prior.pointer_sha256, challenge.expected_rollback_pointer_sha256),
        (prior.generation, challenge.expected_rollback_pointer_generation),
        (rollback.slot.slot_id, challenge.expected_rollback_slot_id),
        (rollback.slot.slot_sha256, challenge.expected_rollback_slot_sha256),
        (rollback.boot_receipt.receipt_id, challenge.expected_rollback_boot_receipt_id),
        (
            rollback.boot_receipt.receipt_sha256,
            challenge.expected_rollback_boot_receipt_sha256,
        ),
        (active.previous_pointer_sha256, prior.pointer_sha256),
        (active.previous_slot_id, rollback.slot.slot_id),
        (active.previous_slot_sha256, rollback.slot.slot_sha256),
    )
    if any(actual != wanted for actual, wanted in expected):
        raise EvolutionStableRemoteReadinessProbeError(
            "stable_remote_probe_release_store_mismatch",
            "远端 Release Store 与 Probe challenge 期望不一致。",
        )
    completed = _aware(clock())
    if not (
        started <= completed < _aware(challenge.expires_at)
        and (completed - started).total_seconds() <= challenge.maximum_probe_seconds
    ):
        raise EvolutionStableRemoteReadinessProbeError(
            "stable_remote_probe_execution_window_exceeded",
            "Release Store Probe 超出执行或 freshness 窗口。",
        )
    handle = installation_key_service.inspect()
    facts = {
        "release_root_sha256": handle.release_root_sha256,
        "active_pointer_id": active.pointer_id,
        "active_pointer_sha256": active.pointer_sha256,
        "active_pointer_generation": active.generation,
        "candidate_version": candidate.slot.version,
        "candidate_target": candidate.slot.target,
        "candidate_slot_id": candidate.slot.slot_id,
        "candidate_slot_sha256": candidate.slot.slot_sha256,
        "candidate_manifest_sha256": candidate.slot.manifest_sha256,
        "candidate_boot_receipt_id": candidate.boot_receipt.receipt_id,
        "candidate_boot_receipt_sha256": candidate.boot_receipt.receipt_sha256,
        "candidate_binary_sha256": candidate.boot_receipt.binary_sha256,
        "rollback_pointer_id": prior.pointer_id,
        "rollback_pointer_sha256": prior.pointer_sha256,
        "rollback_pointer_generation": prior.generation,
        "rollback_version": rollback.slot.version,
        "rollback_target": rollback.slot.target,
        "rollback_slot_id": rollback.slot.slot_id,
        "rollback_slot_sha256": rollback.slot.slot_sha256,
        "rollback_manifest_sha256": rollback.slot.manifest_sha256,
        "rollback_boot_receipt_id": rollback.boot_receipt.receipt_id,
        "rollback_boot_receipt_sha256": rollback.boot_receipt.receipt_sha256,
        "rollback_binary_sha256": rollback.boot_receipt.binary_sha256,
    }
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_STABLE_REMOTE_READINESS_PROBE_POLICY,
        "challenge_id": challenge.challenge_id,
        "challenge_sha256": challenge.challenge_sha256,
        "claim_receipt_id": challenge.claim_receipt_id,
        "claim_receipt_sha256": challenge.claim_receipt_sha256,
        "installation_member_id": challenge.installation_member_id,
        "installation_credential_id": challenge.installation_credential_id,
        **facts,
        "source_set_sha256": _digest(facts),
        "probe_started_at": started.isoformat(),
        "probe_completed_at": completed.isoformat(),
        "expires_at": challenge.expires_at,
        "active_pointer_reread_consistent": True,
        "candidate_bytes_revalidated": True,
        "rollback_bytes_revalidated": True,
        "previous_slot_retained": True,
        "previous_slot_bootable": True,
        "expected_pointer_cas_ready": True,
        "source_runtime_revalidated": True,
        "release_store_mutation_performed": False,
        "stable_rollout_authority": False,
        "promotion_authority": False,
    }
    digest = _digest(core)
    result = EvolutionStableRemoteReadinessProbeResult.model_validate({
        **core,
        "result_id": f"evstableremoteproberesult_{digest[:24]}",
        "result_sha256": digest,
    })
    try:
        signature = installation_key_service.sign_remote_readiness_probe(
            credential=credential,
            payload=result.canonical_bytes(),
        )
    except ReleaseInstallationKeyError as exc:
        raise EvolutionStableRemoteReadinessProbeError(
            exc.code,
            "远端 installation key 无法签署 Probe result。",
        ) from exc
    if not (
        _aware(result.probe_completed_at)
        <= _aware(signature.signed_at)
        < _aware(result.expires_at)
    ):
        raise EvolutionStableRemoteReadinessProbeError(
            "stable_remote_probe_signature_outside_window",
            "Probe result 签名超出 challenge freshness 窗口。",
        )
    return EvolutionStableRemoteReadinessProbeSubmission(
        result=result,
        signature=signature,
    )


def encode_stable_remote_readiness_probe_challenge(
    challenge: EvolutionStableRemoteReadinessProbeChallenge,
) -> str:
    return base64.b64encode(challenge.canonical_bytes()).decode("ascii")


def decode_stable_remote_readiness_probe_challenge(
    value: str,
) -> EvolutionStableRemoteReadinessProbeChallenge:
    return _decode_model(
        value,
        EvolutionStableRemoteReadinessProbeChallenge.model_validate_json,
        "stable_remote_probe_challenge_invalid",
        "Remote Readiness Probe challenge 编码或结构无效。",
    )


def encode_stable_remote_readiness_probe_submission(
    submission: EvolutionStableRemoteReadinessProbeSubmission,
) -> str:
    return base64.b64encode(
        _canonical_bytes(submission.model_dump(mode="json"))
    ).decode("ascii")


def decode_stable_remote_readiness_probe_submission(
    value: str,
) -> EvolutionStableRemoteReadinessProbeSubmission:
    return _decode_model(
        value,
        EvolutionStableRemoteReadinessProbeSubmission.model_validate_json,
        "stable_remote_probe_submission_invalid",
        "Remote Readiness Probe submission 编码或结构无效。",
    )


def render_stable_remote_readiness_probe_challenge(
    challenge: EvolutionStableRemoteReadinessProbeChallenge,
) -> str:
    encoded = encode_stable_remote_readiness_probe_challenge(challenge)
    return "\n".join((
        "## Stable Remote Readiness Probe Challenge",
        "",
        f"- Challenge：`{challenge.challenge_id}`",
        f"- Claim：`{challenge.claim_receipt_id}`",
        f"- Member：`{challenge.installation_member_id}`",
        f"- 过期：`{challenge.expires_at}`",
        f"- Challenge Base64：`{encoded}`",
        "- Release Store mutation authority：`false`",
        "- Binary rollback readiness authority：`false`",
    ))


def render_stable_remote_readiness_probe_submission(
    submission: EvolutionStableRemoteReadinessProbeSubmission,
) -> str:
    encoded = encode_stable_remote_readiness_probe_submission(submission)
    return "\n".join((
        "## Stable Remote Readiness Probe Submission",
        "",
        "- 状态：**本机 Release Store 已机械重验并签名**",
        f"- Result：`{submission.result.result_id}`",
        f"- Member：`{submission.result.installation_member_id}`",
        f"- Source set SHA-256：`{submission.result.source_set_sha256}`",
        f"- Submission Base64：`{encoded}`",
        "- Control Plane authority：`false`（尚未 ingest）",
    ))


def render_stable_remote_readiness_probe(
    view: EvolutionStableRemoteReadinessProbeView,
) -> str:
    result = view.receipt.submission.result
    status = (
        "Binary rollback ready"
        if view.binary_rollback_readiness_authority
        else "已失效"
    )
    source_authority = str(view.source_runtime_revalidation_authority).lower()
    rollback_authority = str(view.binary_rollback_readiness_authority).lower()
    return "\n".join((
        "## Stable Remote Readiness Probe",
        "",
        f"- 状态：**{status}**",
        f"- Receipt：`{view.receipt.receipt_id}`",
        f"- Member：`{result.installation_member_id}`",
        f"- Active pointer：`{result.active_pointer_id}`",
        f"- Candidate slot：`{result.candidate_slot_id}`",
        f"- Rollback slot：`{result.rollback_slot_id}`",
        f"- Source set SHA-256：`{result.source_set_sha256}`",
        f"- Source runtime revalidated：`{source_authority}`",
        f"- Binary rollback readiness authority：`{rollback_authority}`",
        "- Config/data rollback authority：`false`",
        "- Stable rollout authority：`false`",
        "- Remote execution authority：`false`",
        "- Promotion authority：`false`",
    ))


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS "
        "evolution_stable_remote_readiness_probe_challenges ("
        "challenge_id TEXT PRIMARY KEY, challenge_sha256 TEXT NOT NULL UNIQUE, "
        "claim_receipt_id TEXT NOT NULL UNIQUE, challenge_json TEXT NOT NULL, "
        "issued_at TEXT NOT NULL, expires_at TEXT NOT NULL)"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_stable_remote_readiness_probe_receipts ("
        "receipt_id TEXT PRIMARY KEY, receipt_sha256 TEXT NOT NULL UNIQUE, "
        "challenge_id TEXT NOT NULL UNIQUE, claim_receipt_id TEXT NOT NULL UNIQUE, "
        "receipt_json TEXT NOT NULL, recorded_at TEXT NOT NULL)"
    )


def _source_projection(result: EvolutionStableRemoteReadinessProbeResult) -> dict:
    return {
        "release_root_sha256": result.release_root_sha256,
        "active_pointer_id": result.active_pointer_id,
        "active_pointer_sha256": result.active_pointer_sha256,
        "active_pointer_generation": result.active_pointer_generation,
        "candidate_version": result.candidate_version,
        "candidate_target": result.candidate_target,
        "candidate_slot_id": result.candidate_slot_id,
        "candidate_slot_sha256": result.candidate_slot_sha256,
        "candidate_manifest_sha256": result.candidate_manifest_sha256,
        "candidate_boot_receipt_id": result.candidate_boot_receipt_id,
        "candidate_boot_receipt_sha256": result.candidate_boot_receipt_sha256,
        "candidate_binary_sha256": result.candidate_binary_sha256,
        "rollback_pointer_id": result.rollback_pointer_id,
        "rollback_pointer_sha256": result.rollback_pointer_sha256,
        "rollback_pointer_generation": result.rollback_pointer_generation,
        "rollback_version": result.rollback_version,
        "rollback_target": result.rollback_target,
        "rollback_slot_id": result.rollback_slot_id,
        "rollback_slot_sha256": result.rollback_slot_sha256,
        "rollback_manifest_sha256": result.rollback_manifest_sha256,
        "rollback_boot_receipt_id": result.rollback_boot_receipt_id,
        "rollback_boot_receipt_sha256": result.rollback_boot_receipt_sha256,
        "rollback_binary_sha256": result.rollback_binary_sha256,
    }


def _decode_model(value, restore, code, message):
    try:
        raw = base64.b64decode(str(value), validate=True)
        if not 1 <= len(raw) <= _MAX_ENCODED_BYTES:
            raise ValueError("encoded artifact size")
        return restore(raw)
    except (TypeError, ValueError) as exc:
        raise EvolutionStableRemoteReadinessProbeError(code, message) from exc


def _bounded(encoded: str) -> None:
    if len(encoded.encode("utf-8")) > _MAX_ENCODED_BYTES:
        raise EvolutionStableRemoteReadinessProbeError(
            "stable_remote_probe_artifact_oversized",
            "Remote Readiness Probe artifact 超过 512 KiB。",
        )


def _challenge_id(value: str) -> str:
    normalized = str(value)
    if _CHALLENGE_RE.fullmatch(normalized) is None:
        raise ValueError("probe challenge id 格式无效。")
    return normalized


def _receipt_id(value: str) -> str:
    normalized = str(value)
    if _RECEIPT_RE.fullmatch(normalized) is None:
        raise ValueError("probe receipt id 格式无效。")
    return normalized


def _claim_receipt_id(value: str) -> str:
    normalized = str(value)
    if re.fullmatch(r"^evstableremoteclaim_[0-9a-f]{24}$", normalized) is None:
        raise ValueError("remote claim receipt id 格式无效。")
    return normalized


def _decode_base64(value: str, expected: int, label: str) -> bytes:
    decoded = base64.b64decode(value, validate=True)
    if not (
        len(decoded) == expected
        and base64.b64encode(decoded).decode("ascii") == value
    ):
        raise ValueError(f"{label} 不是 canonical Base64。")
    return decoded


def _safe_code(value: object) -> str:
    normalized = re.sub(r"[^a-z0-9_.-]+", "_", str(value).strip().lower())
    return normalized[:128] or "stable_remote_probe_release_store_failed"


def _aware(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("时间戳必须包含时区。")
    return parsed.astimezone(UTC)


def _canonical_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(payload: object) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


__all__ = [
    "EVOLUTION_STABLE_REMOTE_READINESS_PROBE_POLICY",
    "EvolutionStableRemoteReadinessProbeChallenge",
    "EvolutionStableRemoteReadinessProbeError",
    "EvolutionStableRemoteReadinessProbeReceipt",
    "EvolutionStableRemoteReadinessProbeResult",
    "EvolutionStableRemoteReadinessProbeService",
    "EvolutionStableRemoteReadinessProbeStore",
    "EvolutionStableRemoteReadinessProbeSubmission",
    "EvolutionStableRemoteReadinessProbeView",
    "decode_stable_remote_readiness_probe_challenge",
    "decode_stable_remote_readiness_probe_submission",
    "encode_stable_remote_readiness_probe_challenge",
    "encode_stable_remote_readiness_probe_submission",
    "execute_stable_remote_readiness_probe",
    "render_stable_remote_readiness_probe",
    "render_stable_remote_readiness_probe_challenge",
    "render_stable_remote_readiness_probe_submission",
]
