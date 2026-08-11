"""Authenticated remote stable-readiness facts from managed installations."""

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
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.stable_population_completions import (
    EvolutionStablePopulationCompletionError,
    EvolutionStablePopulationCompletionService,
)
from naumi_agent.release.population_registry import (
    ReleasePopulationRegistryError,
    ReleasePopulationSnapshotStore,
)

EVOLUTION_STABLE_REMOTE_READINESS_CLAIM_POLICY = (
    "evolution-stable-remote-readiness-claim-v1"
)
_CHALLENGE_RE = re.compile(r"^evstableremotechal_[0-9a-f]{24}$")
_RECEIPT_RE = re.compile(r"^evstableremoteclaim_[0-9a-f]{24}$")
_MEMBER_RE = re.compile(r"^relpopmember_[0-9a-f]{24}$")
_B64_32_RE = r"^[A-Za-z0-9+/]{43}=$"
_B64_64_RE = r"^[A-Za-z0-9+/]{86}==$"
_MAX_ARTIFACT_BYTES = 512 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionStableRemoteReadinessChallenge(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-stable-remote-readiness-claim-v1"] = (
        EVOLUTION_STABLE_REMOTE_READINESS_CLAIM_POLICY
    )
    challenge_id: str = Field(pattern=r"^evstableremotechal_[0-9a-f]{24}$")
    challenge_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_root: str = Field(min_length=1, max_length=4096)
    completion_receipt_id: str = Field(pattern=r"^evstablepopcomplete_[0-9a-f]{24}$")
    completion_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    population_snapshot_id: str = Field(pattern=r"^relpopsnapshot_[0-9a-f]{24}$")
    population_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    installation_member_id: str = Field(pattern=r"^relpopmember_[0-9a-f]{24}$")
    installation_credential_id: str = Field(pattern=r"^relpopcred_[0-9a-f]{24}$")
    installation_credential_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    installation_public_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stable_intent_id: str = Field(pattern=r"^evrestableintent_[0-9a-f]{24}$")
    subject_id: str = Field(min_length=1, max_length=255)
    candidate_version: str = Field(min_length=1, max_length=128)
    candidate_target: str = Field(pattern=r"^(?:macos|linux|windows)-(?:arm64|x64)$")
    nonce_base64: str = Field(pattern=_B64_32_RE)
    nonce_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validity_seconds: int = Field(ge=60, le=900)
    issued_at: str = Field(min_length=1, max_length=100)
    expires_at: str = Field(min_length=1, max_length=100)
    remote_execution_authority: Literal[False] = False
    stable_rollout_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Remote Readiness Challenge workspace 必须 canonical。")
        nonce = base64.b64decode(self.nonce_base64, validate=True)
        issued = _aware(self.issued_at)
        if not (
            len(nonce) == 32
            and hashlib.sha256(nonce).hexdigest() == self.nonce_sha256
            and _aware(self.expires_at)
            == issued + timedelta(seconds=self.validity_seconds)
        ):
            raise ValueError("Remote Readiness Challenge nonce/expiry 无效。")
        digest = _digest(
            self.model_dump(mode="json", exclude={"challenge_id", "challenge_sha256"})
        )
        if self.challenge_sha256 != digest or self.challenge_id != (
            f"evstableremotechal_{digest[:24]}"
        ):
            raise ValueError("Remote Readiness Challenge identity 不一致。")
        return self


class EvolutionStableRemoteReadinessAssertion(_StrictModel):
    schema_version: Literal[1] = 1
    challenge_id: str = Field(pattern=r"^evstableremotechal_[0-9a-f]{24}$")
    challenge_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    nonce_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    installation_member_id: str = Field(pattern=r"^relpopmember_[0-9a-f]{24}$")
    stable_intent_id: str = Field(pattern=r"^evrestableintent_[0-9a-f]{24}$")
    candidate_version: str = Field(min_length=1, max_length=128)
    candidate_target: str = Field(pattern=r"^(?:macos|linux|windows)-(?:arm64|x64)$")
    active_pointer_id: str = Field(pattern=r"^relactive_[0-9a-f]{24}$")
    active_pointer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    active_pointer_generation: int = Field(ge=2, le=1_000_000_000)
    candidate_slot_id: str = Field(pattern=r"^relslot_[0-9a-f]{24}$")
    candidate_slot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rollback_pointer_id: str = Field(pattern=r"^relactive_[0-9a-f]{24}$")
    rollback_pointer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rollback_pointer_generation: int = Field(ge=1, le=1_000_000_000)
    rollback_slot_id: str = Field(pattern=r"^relslot_[0-9a-f]{24}$")
    rollback_slot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rollback_boot_receipt_id: str = Field(pattern=r"^relboot_[0-9a-f]{24}$")
    rollback_boot_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_pointer_cas_observed: Literal[True] = True
    binary_rollback_material_observed: Literal[True] = True
    observed_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        _aware(self.observed_at)
        if not (
            self.active_pointer_generation > self.rollback_pointer_generation
            and self.active_pointer_id != self.rollback_pointer_id
            and self.active_pointer_sha256 != self.rollback_pointer_sha256
            and self.candidate_slot_id != self.rollback_slot_id
            and self.candidate_slot_sha256 != self.rollback_slot_sha256
        ):
            raise ValueError("Remote Readiness Assertion pointer/slot lineage 无效。")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.model_dump(mode="json"))


class EvolutionStableRemoteReadinessClaimReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-stable-remote-readiness-claim-v1"] = (
        EVOLUTION_STABLE_REMOTE_READINESS_CLAIM_POLICY
    )
    receipt_id: str = Field(pattern=r"^evstableremoteclaim_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    challenge: EvolutionStableRemoteReadinessChallenge
    assertion: EvolutionStableRemoteReadinessAssertion
    signature_base64: str = Field(pattern=_B64_64_RE)
    signature_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authenticated_installation_claim: Literal[True] = True
    source_runtime_revalidated: Literal[False] = False
    binary_rollback_readiness_authority: Literal[False] = False
    stable_rollout_authority: Literal[False] = False
    remote_execution_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    recorded_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        signature = base64.b64decode(self.signature_base64, validate=True)
        if not (
            len(signature) == 64
            and hashlib.sha256(signature).hexdigest() == self.signature_sha256
            and self.assertion.challenge_id == self.challenge.challenge_id
            and self.assertion.challenge_sha256 == self.challenge.challenge_sha256
            and self.assertion.nonce_sha256 == self.challenge.nonce_sha256
            and self.assertion.installation_member_id
            == self.challenge.installation_member_id
            and self.assertion.stable_intent_id == self.challenge.stable_intent_id
            and self.assertion.candidate_version == self.challenge.candidate_version
            and self.assertion.candidate_target == self.challenge.candidate_target
            and _aware(self.challenge.issued_at)
            <= _aware(self.assertion.observed_at)
            <= _aware(self.recorded_at)
            < _aware(self.challenge.expires_at)
        ):
            raise ValueError("Remote Readiness Claim binding/time 无效。")
        digest = _digest(
            self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        )
        if self.receipt_sha256 != digest or self.receipt_id != (
            f"evstableremoteclaim_{digest[:24]}"
        ):
            raise ValueError("Remote Readiness Claim identity 不一致。")
        return self


class EvolutionStableRemoteReadinessClaimView(_StrictModel):
    receipt: EvolutionStableRemoteReadinessClaimReceipt
    durable_source_valid: bool
    completion_authority: bool
    population_snapshot_authority: bool
    credential_current: bool
    signature_valid: bool
    challenge_unexpired: bool
    authenticated_claim_authority: bool
    binary_rollback_readiness_authority: Literal[False] = False
    stable_rollout_authority: Literal[False] = False
    remote_execution_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        expected = bool(
            self.durable_source_valid
            and self.completion_authority
            and self.population_snapshot_authority
            and self.credential_current
            and self.signature_valid
            and self.challenge_unexpired
        )
        if self.authenticated_claim_authority is not expected:
            raise ValueError("Remote Readiness Claim authority 投影不一致。")
        return self


class EvolutionStableRemoteReadinessClaimError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionStableRemoteReadinessClaimStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def get_challenge(
        self, challenge_id: str
    ) -> EvolutionStableRemoteReadinessChallenge | None:
        _challenge_id(challenge_id)
        return await self._read(
            table="evolution_stable_remote_readiness_challenges",
            column="challenge_json",
            where="challenge_id = ?",
            parameters=(challenge_id,),
            restore=EvolutionStableRemoteReadinessChallenge.model_validate_json,
        )

    async def get_receipt(
        self, receipt_id: str
    ) -> EvolutionStableRemoteReadinessClaimReceipt | None:
        _receipt_id(receipt_id)
        return await self._read(
            table="evolution_stable_remote_readiness_claims",
            column="receipt_json",
            where="receipt_id = ?",
            parameters=(receipt_id,),
            restore=EvolutionStableRemoteReadinessClaimReceipt.model_validate_json,
        )

    async def get_by_challenge(
        self, challenge_id: str
    ) -> EvolutionStableRemoteReadinessClaimReceipt | None:
        _challenge_id(challenge_id)
        return await self._read(
            table="evolution_stable_remote_readiness_claims",
            column="receipt_json",
            where="challenge_id = ?",
            parameters=(challenge_id,),
            restore=EvolutionStableRemoteReadinessClaimReceipt.model_validate_json,
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
            raise EvolutionStableRemoteReadinessClaimError(
                "stable_remote_readiness_store_corrupt",
                "Remote Readiness durable artifact 损坏或无法读取。",
            ) from exc

    async def record_challenge(
        self, challenge: EvolutionStableRemoteReadinessChallenge
    ) -> EvolutionStableRemoteReadinessChallenge:
        item = EvolutionStableRemoteReadinessChallenge.model_validate_json(
            challenge.model_dump_json()
        )
        encoded = item.model_dump_json()
        self._bounded(encoded)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            existing = await (
                await db.execute(
                    "SELECT challenge_json FROM "
                    "evolution_stable_remote_readiness_challenges "
                    "WHERE challenge_id = ?",
                    (item.challenge_id,),
                )
            ).fetchone()
            if existing is not None:
                restored = EvolutionStableRemoteReadinessChallenge.model_validate_json(
                    existing["challenge_json"]
                )
                await db.rollback()
                if restored != item:
                    raise EvolutionStableRemoteReadinessClaimError(
                        "stable_remote_readiness_challenge_conflict",
                        "Remote Readiness Challenge ID 冲突。",
                    )
                return restored
            await db.execute(
                "INSERT INTO evolution_stable_remote_readiness_challenges "
                "(challenge_id, challenge_sha256, completion_receipt_id, "
                "installation_member_id, challenge_json, issued_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    item.challenge_id,
                    item.challenge_sha256,
                    item.completion_receipt_id,
                    item.installation_member_id,
                    encoded,
                    item.issued_at,
                    item.expires_at,
                ),
            )
            await db.commit()
        return item

    async def record_receipt(
        self, receipt: EvolutionStableRemoteReadinessClaimReceipt
    ) -> EvolutionStableRemoteReadinessClaimReceipt:
        item = EvolutionStableRemoteReadinessClaimReceipt.model_validate_json(
            receipt.model_dump_json()
        )
        encoded = item.model_dump_json()
        self._bounded(encoded)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            source = await (
                await db.execute(
                    "SELECT challenge_json FROM "
                    "evolution_stable_remote_readiness_challenges "
                    "WHERE challenge_id = ?",
                    (item.challenge.challenge_id,),
                )
            ).fetchone()
            if source is None or (
                EvolutionStableRemoteReadinessChallenge.model_validate_json(
                    source["challenge_json"]
                )
                != item.challenge
            ):
                raise EvolutionStableRemoteReadinessClaimError(
                    "stable_remote_readiness_challenge_changed",
                    "Remote Readiness Challenge durable source 已变化。",
                )
            existing = await (
                await db.execute(
                    "SELECT receipt_json FROM evolution_stable_remote_readiness_claims "
                    "WHERE challenge_id = ?",
                    (item.challenge.challenge_id,),
                )
            ).fetchone()
            if existing is not None:
                restored = EvolutionStableRemoteReadinessClaimReceipt.model_validate_json(
                    existing["receipt_json"]
                )
                await db.rollback()
                if restored != item:
                    raise EvolutionStableRemoteReadinessClaimError(
                        "stable_remote_readiness_claim_conflict",
                        "同一 Challenge 已绑定不同 Remote Readiness Claim。",
                    )
                return restored
            await db.execute(
                "INSERT INTO evolution_stable_remote_readiness_claims "
                "(receipt_id, receipt_sha256, challenge_id, completion_receipt_id, "
                "installation_member_id, receipt_json, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    item.receipt_id,
                    item.receipt_sha256,
                    item.challenge.challenge_id,
                    item.challenge.completion_receipt_id,
                    item.challenge.installation_member_id,
                    encoded,
                    item.recorded_at,
                ),
            )
            await db.commit()
        return item

    @staticmethod
    def _bounded(encoded: str) -> None:
        if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
            raise EvolutionStableRemoteReadinessClaimError(
                "stable_remote_readiness_artifact_oversized",
                "Remote Readiness artifact 超过 512 KiB。",
            )


class EvolutionStableRemoteReadinessClaimService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        completion_service: EvolutionStablePopulationCompletionService,
        population_store: ReleasePopulationSnapshotStore,
        store: EvolutionStableRemoteReadinessClaimStore,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.completion_service = completion_service
        self.population_store = population_store
        self.store = store
        self.clock = clock
        self.random_bytes = random_bytes
        if completion_service.workspace_root != self.workspace_root:
            raise ValueError("Remote Readiness workspace identity 不一致。")
        if completion_service.store.db_path != store.db_path:
            raise ValueError("Remote Readiness 必须与 Completion 共用证据库。")
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def issue_challenge(
        self,
        *,
        completion_receipt_id: str,
        installation_member_id: str,
        validity_seconds: int = 300,
    ) -> EvolutionStableRemoteReadinessChallenge:
        if _MEMBER_RE.fullmatch(str(installation_member_id)) is None:
            raise ValueError("installation member id 格式无效。")
        if not 60 <= validity_seconds <= 900:
            raise ValueError("validity_seconds 必须在 60..900。")
        completion, snapshot, credential, index = await self._sources(
            completion_receipt_id,
            installation_member_id,
        )
        now = _aware(self.clock())
        nonce = self.random_bytes(32)
        if len(nonce) != 32:
            raise EvolutionStableRemoteReadinessClaimError(
                "stable_remote_readiness_nonce_invalid",
                "Challenge nonce 生成器必须返回 32 字节。",
            )
        core = {
            "schema_version": 1,
            "policy_version": EVOLUTION_STABLE_REMOTE_READINESS_CLAIM_POLICY,
            "workspace_root": str(self.workspace_root),
            "completion_receipt_id": completion.receipt.receipt_id,
            "completion_receipt_sha256": completion.receipt.receipt_sha256,
            "population_snapshot_id": snapshot.snapshot.snapshot_id,
            "population_snapshot_sha256": snapshot.snapshot.snapshot_sha256,
            "installation_member_id": installation_member_id,
            "installation_credential_id": credential.credential_id,
            "installation_credential_sha256": credential.credential_sha256,
            "installation_public_key_sha256": (
                credential.payload.installation_public_key_sha256
            ),
            "stable_intent_id": completion.receipt.intent_ids[index],
            "subject_id": completion.receipt.subject_ids[index],
            "candidate_version": completion.receipt.candidate_version,
            "candidate_target": completion.receipt.candidate_target,
            "nonce_base64": base64.b64encode(nonce).decode("ascii"),
            "nonce_sha256": hashlib.sha256(nonce).hexdigest(),
            "validity_seconds": validity_seconds,
            "issued_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=validity_seconds)).isoformat(),
            "remote_execution_authority": False,
            "stable_rollout_authority": False,
            "promotion_authority": False,
        }
        digest = _digest(core)
        challenge = EvolutionStableRemoteReadinessChallenge.model_validate({
            **core,
            "challenge_id": f"evstableremotechal_{digest[:24]}",
            "challenge_sha256": digest,
        })
        return await self.store.record_challenge(challenge)

    async def ingest(
        self,
        *,
        challenge_id: str,
        assertion_base64: str,
        signature_base64: str,
    ) -> EvolutionStableRemoteReadinessClaimView:
        _challenge_id(challenge_id)
        lock = self._locks.setdefault(challenge_id, asyncio.Lock())
        async with lock:
            existing = await self.store.get_by_challenge(challenge_id)
            if existing is not None:
                assertion = _decode_assertion(assertion_base64)
                signature = _decode_signature(signature_base64)
                if not (
                    assertion == existing.assertion
                    and base64.b64encode(signature).decode("ascii")
                    == existing.signature_base64
                ):
                    raise EvolutionStableRemoteReadinessClaimError(
                        "stable_remote_readiness_claim_conflict",
                        "同一 Challenge 已绑定不同 Remote Readiness Claim。",
                    )
                return await self.inspect(receipt_id=existing.receipt_id)
            challenge = await self.store.get_challenge(challenge_id)
            if challenge is None:
                raise EvolutionStableRemoteReadinessClaimError(
                    "stable_remote_readiness_challenge_missing",
                    "Remote Readiness Challenge 不存在。",
                )
            now = _aware(self.clock())
            if now >= _aware(challenge.expires_at):
                raise EvolutionStableRemoteReadinessClaimError(
                    "stable_remote_readiness_challenge_expired",
                    "Remote Readiness Challenge 已过期。",
                )
            assertion = _decode_assertion(assertion_base64)
            signature = _decode_signature(signature_base64)
            credential = await self._credential_for_challenge(challenge)
            _verify_signature(
                credential.payload.installation_public_key_base64,
                assertion,
                signature,
            )
            recorded_at = now.isoformat()
            core = {
                "schema_version": 1,
                "policy_version": EVOLUTION_STABLE_REMOTE_READINESS_CLAIM_POLICY,
                "challenge": challenge.model_dump(mode="json"),
                "assertion": assertion.model_dump(mode="json"),
                "signature_base64": base64.b64encode(signature).decode("ascii"),
                "signature_sha256": hashlib.sha256(signature).hexdigest(),
                "authenticated_installation_claim": True,
                "source_runtime_revalidated": False,
                "binary_rollback_readiness_authority": False,
                "stable_rollout_authority": False,
                "remote_execution_authority": False,
                "promotion_authority": False,
                "recorded_at": recorded_at,
            }
            digest = _digest(core)
            receipt = EvolutionStableRemoteReadinessClaimReceipt.model_validate({
                **core,
                "receipt_id": f"evstableremoteclaim_{digest[:24]}",
                "receipt_sha256": digest,
            })
            await self.store.record_receipt(receipt)
        return await self.inspect(receipt_id=receipt.receipt_id)

    async def inspect(
        self,
        *,
        receipt_id: str,
    ) -> EvolutionStableRemoteReadinessClaimView:
        receipt = await self.store.get_receipt(receipt_id)
        if receipt is None:
            raise EvolutionStableRemoteReadinessClaimError(
                "stable_remote_readiness_claim_missing",
                "Remote Readiness Claim Receipt 不存在。",
            )
        completion_ok = snapshot_ok = credential_ok = signature_ok = False
        try:
            completion, snapshot, credential, index = await self._sources(
                receipt.challenge.completion_receipt_id,
                receipt.challenge.installation_member_id,
            )
            completion_ok = bool(
                completion.receipt.receipt_sha256
                == receipt.challenge.completion_receipt_sha256
            )
            snapshot_ok = bool(
                snapshot.snapshot.snapshot_sha256
                == receipt.challenge.population_snapshot_sha256
            )
            credential_ok = bool(
                credential.credential_id
                == receipt.challenge.installation_credential_id
                and credential.credential_sha256
                == receipt.challenge.installation_credential_sha256
                and credential.payload.installation_public_key_sha256
                == receipt.challenge.installation_public_key_sha256
                and completion.receipt.intent_ids[index]
                == receipt.challenge.stable_intent_id
            )
            signature = base64.b64decode(receipt.signature_base64, validate=True)
            _verify_signature(
                credential.payload.installation_public_key_base64,
                receipt.assertion,
                signature,
            )
            signature_ok = True
        except (
            EvolutionStablePopulationCompletionError,
            ReleasePopulationRegistryError,
            EvolutionStableRemoteReadinessClaimError,
            OSError,
            TypeError,
            ValueError,
        ):
            pass
        unexpired = _aware(self.clock()) < _aware(receipt.challenge.expires_at)
        authority = bool(
            completion_ok and snapshot_ok and credential_ok and signature_ok and unexpired
        )
        return EvolutionStableRemoteReadinessClaimView(
            receipt=receipt,
            durable_source_valid=True,
            completion_authority=completion_ok,
            population_snapshot_authority=snapshot_ok,
            credential_current=credential_ok,
            signature_valid=signature_ok,
            challenge_unexpired=unexpired,
            authenticated_claim_authority=authority,
        )

    async def _credential_for_challenge(self, challenge):
        _completion, _snapshot, credential, _index = await self._sources(
            challenge.completion_receipt_id,
            challenge.installation_member_id,
        )
        if not (
            credential.credential_id == challenge.installation_credential_id
            and credential.credential_sha256 == challenge.installation_credential_sha256
            and credential.payload.installation_public_key_sha256
            == challenge.installation_public_key_sha256
        ):
            raise EvolutionStableRemoteReadinessClaimError(
                "stable_remote_readiness_credential_changed",
                "Population Credential 已变化。",
            )
        return credential

    async def _sources(self, completion_id: str, member_id: str):
        try:
            completion = await self.completion_service.inspect(receipt_id=completion_id)
            if not completion.stable_population_completion_authority:
                raise EvolutionStableRemoteReadinessClaimError(
                    "stable_remote_readiness_source_unavailable",
                    "Stable Population Completion authority 已失效。",
                )
            snapshot = await self.population_store.inspect(
                snapshot_id=completion.receipt.population_snapshot_id
            )
            if not snapshot.population_snapshot_authority:
                raise EvolutionStableRemoteReadinessClaimError(
                    "stable_remote_readiness_source_unavailable",
                    "Population Snapshot authority 已失效。",
                )
            index = completion.receipt.installation_member_ids.index(member_id)
            credential = snapshot.snapshot.payload.credentials[index]
            if credential.payload.member_id != member_id:
                raise ValueError("credential member mismatch")
            return completion, snapshot, credential, index
        except (EvolutionStablePopulationCompletionError, ReleasePopulationRegistryError) as exc:
            raise EvolutionStableRemoteReadinessClaimError(
                "stable_remote_readiness_source_unavailable",
                "Completion 或 Population Snapshot authority 不可用。",
            ) from exc
        except (IndexError, ValueError) as exc:
            raise EvolutionStableRemoteReadinessClaimError(
                "stable_remote_readiness_member_not_current",
                "Installation member 不属于 current Population Completion。",
            ) from exc


def render_stable_remote_readiness_challenge(
    challenge: EvolutionStableRemoteReadinessChallenge,
) -> str:
    return "\n".join((
        "## Stable Remote Readiness Challenge",
        "",
        f"- Challenge：`{challenge.challenge_id}`",
        f"- Completion：`{challenge.completion_receipt_id}`",
        f"- Member：`{challenge.installation_member_id}`",
        f"- Intent：`{challenge.stable_intent_id}`",
        f"- Target：`{challenge.candidate_target}`",
        f"- 过期：`{challenge.expires_at}`",
        "- Remote execution authority：`false`",
        "- Stable rollout authority：`false`",
    ))


def render_stable_remote_readiness_claim(
    view: EvolutionStableRemoteReadinessClaimView,
) -> str:
    item = view.receipt
    return "\n".join((
        "## Authenticated Stable Remote Readiness Claim",
        "",
        f"- 状态：**{'当前有效' if view.authenticated_claim_authority else '已失效'}**",
        f"- Receipt：`{item.receipt_id}`",
        f"- Challenge：`{item.challenge.challenge_id}`",
        f"- Member：`{item.challenge.installation_member_id}`",
        f"- Active pointer：`{item.assertion.active_pointer_id}`",
        f"- Rollback slot：`{item.assertion.rollback_slot_id}`",
        f"- Signature valid：`{str(view.signature_valid).lower()}`",
        "- Source runtime revalidated：`false`",
        "- Binary rollback readiness authority：`false`",
        "- Stable rollout authority：`false`",
        "- Remote execution authority：`false`",
        "- Promotion authority：`false`",
    ))


def encode_stable_remote_readiness_assertion(
    assertion: EvolutionStableRemoteReadinessAssertion,
) -> str:
    return base64.b64encode(assertion.canonical_bytes()).decode("ascii")


def _decode_assertion(value: str) -> EvolutionStableRemoteReadinessAssertion:
    try:
        raw = base64.b64decode(str(value), validate=True)
        if not 1 <= len(raw) <= 64 * 1024:
            raise ValueError("assertion size")
        return EvolutionStableRemoteReadinessAssertion.model_validate_json(raw)
    except (TypeError, ValueError) as exc:
        raise EvolutionStableRemoteReadinessClaimError(
            "stable_remote_readiness_assertion_invalid",
            "Remote Readiness Assertion 编码或结构无效。",
        ) from exc


def _decode_signature(value: str) -> bytes:
    try:
        signature = base64.b64decode(str(value), validate=True)
    except (TypeError, ValueError) as exc:
        raise EvolutionStableRemoteReadinessClaimError(
            "stable_remote_readiness_signature_invalid",
            "Remote Readiness signature 编码无效。",
        ) from exc
    if len(signature) != 64:
        raise EvolutionStableRemoteReadinessClaimError(
            "stable_remote_readiness_signature_invalid",
            "Remote Readiness signature 长度无效。",
        )
    return signature


def _verify_signature(public_key_base64: str, assertion, signature: bytes) -> None:
    try:
        public = base64.b64decode(public_key_base64, validate=True)
        Ed25519PublicKey.from_public_bytes(public).verify(
            signature,
            assertion.canonical_bytes(),
        )
    except (InvalidSignature, TypeError, ValueError) as exc:
        raise EvolutionStableRemoteReadinessClaimError(
            "stable_remote_readiness_signature_untrusted",
            "Remote Readiness signature 无法由 Population Credential 验证。",
        ) from exc


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_stable_remote_readiness_challenges ("
        "challenge_id TEXT PRIMARY KEY, challenge_sha256 TEXT NOT NULL UNIQUE, "
        "completion_receipt_id TEXT NOT NULL, installation_member_id TEXT NOT NULL, "
        "challenge_json TEXT NOT NULL, issued_at TEXT NOT NULL, expires_at TEXT NOT NULL)"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_stable_remote_readiness_claims ("
        "receipt_id TEXT PRIMARY KEY, receipt_sha256 TEXT NOT NULL UNIQUE, "
        "challenge_id TEXT NOT NULL UNIQUE, completion_receipt_id TEXT NOT NULL, "
        "installation_member_id TEXT NOT NULL, receipt_json TEXT NOT NULL, "
        "recorded_at TEXT NOT NULL)"
    )


def _challenge_id(value: str) -> str:
    if _CHALLENGE_RE.fullmatch(str(value)) is None:
        raise ValueError("challenge id 格式无效。")
    return str(value)


def _receipt_id(value: str) -> str:
    if _RECEIPT_RE.fullmatch(str(value)) is None:
        raise ValueError("claim receipt id 格式无效。")
    return str(value)


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
    ).encode()


def _digest(payload: object) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


__all__ = [
    "EVOLUTION_STABLE_REMOTE_READINESS_CLAIM_POLICY",
    "EvolutionStableRemoteReadinessAssertion",
    "EvolutionStableRemoteReadinessChallenge",
    "EvolutionStableRemoteReadinessClaimError",
    "EvolutionStableRemoteReadinessClaimReceipt",
    "EvolutionStableRemoteReadinessClaimService",
    "EvolutionStableRemoteReadinessClaimStore",
    "EvolutionStableRemoteReadinessClaimView",
    "encode_stable_remote_readiness_assertion",
    "render_stable_remote_readiness_challenge",
    "render_stable_remote_readiness_claim",
]
