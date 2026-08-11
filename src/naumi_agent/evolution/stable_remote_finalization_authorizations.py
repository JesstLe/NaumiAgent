"""Signed, short-lived authorization for one remote stable-member finalization."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol, Self, runtime_checkable
from weakref import WeakValueDictionary

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.revalidation_rollout_stage_entries import (
    EvolutionRevalidationRolloutControlEvent,
    EvolutionRevalidationRolloutControlState,
    EvolutionRevalidationRolloutControlStore,
)
from naumi_agent.evolution.stable_remote_readiness_probes import (
    EvolutionStableRemoteReadinessProbeService,
    EvolutionStableRemoteReadinessProbeView,
)
from naumi_agent.release.rollout_control_keys import (
    ReleaseRolloutControlKeyError,
    ReleaseRolloutControlKeyService,
    ReleaseRolloutControlSignature,
    ReleaseRolloutControlTrustPolicyDocument,
    load_release_rollout_control_trust_policy,
    verify_release_rollout_control_signature,
)

EVOLUTION_STABLE_REMOTE_FINALIZATION_AUTHORIZATION_POLICY = (
    "evolution-stable-remote-finalization-authorization-v1"
)
_AUTH_RE = re.compile(r"^evstableremotefinalauth_[0-9a-f]{24}$")
_PROBE_RE = re.compile(r"^evstableremoteprobereceipt_[0-9a-f]{24}$")
_MEMBER_RE = re.compile(r"^relpopmember_[0-9a-f]{24}$")
_B64_32_RE = r"^[A-Za-z0-9+/]{43}=$"
_MAX_ARTIFACT_BYTES = 512 * 1024
_MAX_ENCODED_CHARS = ((_MAX_ARTIFACT_BYTES + 2) // 3) * 4


@runtime_checkable
class EvolutionStableRemoteReadinessProbeInspectionPort(Protocol):
    async def inspect(
        self,
        *,
        receipt_id: str,
    ) -> EvolutionStableRemoteReadinessProbeView: ...


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionStableRemoteFinalizationAuthorization(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-stable-remote-finalization-authorization-v1"
    ] = EVOLUTION_STABLE_REMOTE_FINALIZATION_AUTHORIZATION_POLICY
    authorization_id: str = Field(
        pattern=r"^evstableremotefinalauth_[0-9a-f]{24}$"
    )
    authorization_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_root_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    probe_receipt_id: str = Field(
        pattern=r"^evstableremoteprobereceipt_[0-9a-f]{24}$"
    )
    probe_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    claim_receipt_id: str = Field(pattern=r"^evstableremoteclaim_[0-9a-f]{24}$")
    claim_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    completion_receipt_id: str = Field(
        pattern=r"^evstablepopcomplete_[0-9a-f]{24}$"
    )
    completion_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    population_snapshot_id: str = Field(pattern=r"^relpopsnapshot_[0-9a-f]{24}$")
    population_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    installation_member_id: str = Field(pattern=r"^relpopmember_[0-9a-f]{24}$")
    installation_credential_id: str = Field(pattern=r"^relpopcred_[0-9a-f]{24}$")
    installation_credential_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    installation_public_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    release_root_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stable_intent_id: str = Field(pattern=r"^evrestableintent_[0-9a-f]{24}$")
    candidate_version: str = Field(min_length=1, max_length=128)
    candidate_target: str = Field(pattern=r"^(?:macos|linux|windows)-(?:arm64|x64)$")
    expected_active_pointer_id: str = Field(pattern=r"^relactive_[0-9a-f]{24}$")
    expected_active_pointer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_active_pointer_generation: int = Field(ge=2, le=1_000_000_000)
    expected_candidate_slot_id: str = Field(pattern=r"^relslot_[0-9a-f]{24}$")
    expected_candidate_slot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_candidate_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_candidate_boot_receipt_id: str = Field(pattern=r"^relboot_[0-9a-f]{24}$")
    expected_candidate_boot_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_rollback_pointer_id: str = Field(pattern=r"^relactive_[0-9a-f]{24}$")
    expected_rollback_pointer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_rollback_pointer_generation: int = Field(ge=1, le=999_999_999)
    expected_rollback_slot_id: str = Field(pattern=r"^relslot_[0-9a-f]{24}$")
    expected_rollback_slot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_rollback_boot_receipt_id: str = Field(pattern=r"^relboot_[0-9a-f]{24}$")
    expected_rollback_boot_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    control_sequence: int = Field(ge=0, le=1_000_000)
    control_event_id: str = Field(default="", pattern=r"^(?:|evrerolloutctrl_[0-9a-f]{24})$")
    control_event_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    control_state: Literal["active"] = "active"
    attempt: int = Field(ge=1, le=10_000)
    previous_authorization_id: str = Field(
        default="",
        pattern=r"^(?:|evstableremotefinalauth_[0-9a-f]{24})$",
    )
    previous_authorization_sha256: str = Field(
        default="",
        pattern=r"^(?:|[0-9a-f]{64})$",
    )
    operation_scope: Literal["binary_only"] = "binary_only"
    allowed_operations: tuple[
        Literal["finalize_stable_population_member"], ...
    ] = ("finalize_stable_population_member",)
    start_nonce_base64: str = Field(pattern=_B64_32_RE)
    start_nonce_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validity_seconds: int = Field(ge=60, le=300)
    issued_at: str = Field(min_length=1, max_length=100)
    expires_at: str = Field(min_length=1, max_length=100)
    single_use_required: Literal[True] = True
    release_store_finalization_authority: Literal[True] = True
    remote_execution_authority: Literal[True] = True
    config_data_mutation_allowed: Literal[False] = False
    deployment_authority: Literal[False] = False
    rollback_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        nonce = _decode_base64(self.start_nonce_base64, 32, "authorization nonce")
        issued = _aware(self.issued_at)
        if not (
            hashlib.sha256(nonce).hexdigest() == self.start_nonce_sha256
            and _aware(self.expires_at)
            == issued + timedelta(seconds=self.validity_seconds)
            and self.expected_rollback_pointer_generation + 1
            == self.expected_active_pointer_generation
            and self.expected_candidate_slot_id != self.expected_rollback_slot_id
            and (self.control_sequence == 0) is (not self.control_event_id)
            and bool(self.control_event_id) is bool(self.control_event_sha256)
            and (self.attempt == 1) is (not self.previous_authorization_id)
            and bool(self.previous_authorization_id)
            is bool(self.previous_authorization_sha256)
        ):
            raise ValueError("Remote Finalization Authorization binding 无效。")
        if self.source_set_sha256 != _digest(_source_identity(self)):
            raise ValueError("Remote Finalization Authorization source-set 不一致。")
        core = self.model_dump(
            mode="json",
            exclude={"authorization_id", "authorization_sha256"},
        )
        digest = _digest(core)
        if self.authorization_sha256 != digest or self.authorization_id != (
            f"evstableremotefinalauth_{digest[:24]}"
        ):
            raise ValueError("Remote Finalization Authorization identity 不一致。")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.model_dump(mode="json"))


class EvolutionStableRemoteFinalizationAuthorizationEnvelope(_StrictModel):
    schema_version: Literal[1] = 1
    authorization: EvolutionStableRemoteFinalizationAuthorization
    signature: ReleaseRolloutControlSignature

    @model_validator(mode="after")
    def _binding(self) -> Self:
        payload = self.authorization.canonical_bytes()
        if not (
            self.signature.channel == "stable"
            and self.signature.payload_sha256 == hashlib.sha256(payload).hexdigest()
            and self.signature.payload_bytes == len(payload)
            and _aware(self.authorization.issued_at)
            <= _aware(self.signature.signed_at)
            < _aware(self.authorization.expires_at)
        ):
            raise ValueError("Remote Finalization signature envelope binding 无效。")
        return self


class EvolutionStableRemoteFinalizationConsumptionReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    receipt_id: str = Field(pattern=r"^evstableremotefinalconsume_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_id: str = Field(
        pattern=r"^evstableremotefinalauth_[0-9a-f]{24}$"
    )
    authorization_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature_id: str = Field(pattern=r"^relrolloutsig_[0-9a-f]{24}$")
    installation_member_id: str = Field(pattern=r"^relpopmember_[0-9a-f]{24}$")
    start_nonce_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    consumed_at: str = Field(min_length=1, max_length=100)
    single_use_consumed: Literal[True] = True

    @model_validator(mode="after")
    def _exact(self) -> Self:
        _aware(self.consumed_at)
        core = self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        digest = _digest(core)
        if self.receipt_sha256 != digest or self.receipt_id != (
            f"evstableremotefinalconsume_{digest[:24]}"
        ):
            raise ValueError("Remote Finalization Consumption identity 不一致。")
        return self


class EvolutionStableRemoteFinalizationAuthorizationView(_StrictModel):
    envelope: EvolutionStableRemoteFinalizationAuthorizationEnvelope
    source_current: bool
    probe_current: bool
    control_current: bool
    trust_policy_current: bool
    expired: bool
    consumed: bool
    invalidation_reasons: tuple[str, ...] = Field(max_length=16)
    remote_finalization_authority: bool
    config_data_mutation_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        expected = bool(
            self.source_current
            and self.probe_current
            and self.control_current
            and self.trust_policy_current
            and not self.expired
            and not self.consumed
        )
        if self.remote_finalization_authority is not expected:
            raise ValueError("Remote Finalization Authorization View 投影不一致。")
        return self


class EvolutionStableRemoteFinalizationAuthorizationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionStableRemoteFinalizationAuthorizationStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def get(
        self,
        authorization_id: str,
    ) -> EvolutionStableRemoteFinalizationAuthorizationEnvelope | None:
        item_id = _authorization_id(authorization_id)
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT envelope_json FROM "
                    "evolution_stable_remote_finalization_authorizations "
                    "WHERE authorization_id = ?",
                    (item_id,),
                )
            ).fetchone()
        return None if row is None else _restore_envelope(row["envelope_json"])

    async def consumption(
        self,
        authorization_id: str,
    ) -> EvolutionStableRemoteFinalizationConsumptionReceipt | None:
        item_id = _authorization_id(authorization_id)
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT receipt_json FROM "
                    "evolution_stable_remote_finalization_consumptions "
                    "WHERE authorization_id = ?",
                    (item_id,),
                )
            ).fetchone()
        return None if row is None else _restore_consumption(row["receipt_json"])

    async def issue(
        self,
        *,
        probe_receipt_id: str,
        workspace_root: Path,
        source_set_sha256: str,
        now: datetime,
        build: Callable[
            [EvolutionStableRemoteFinalizationAuthorizationEnvelope | None],
            EvolutionStableRemoteFinalizationAuthorizationEnvelope,
        ],
    ) -> EvolutionStableRemoteFinalizationAuthorizationEnvelope:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT envelope_json FROM "
                    "evolution_stable_remote_finalization_authorizations "
                    "WHERE probe_receipt_id = ? ORDER BY attempt DESC LIMIT 1",
                    (probe_receipt_id,),
                )
            ).fetchone()
            previous = None if row is None else _restore_envelope(row["envelope_json"])
            if (
                previous is not None
                and previous.authorization.source_set_sha256 == source_set_sha256
            ):
                consumed = await (
                    await db.execute(
                        "SELECT 1 FROM "
                        "evolution_stable_remote_finalization_consumptions "
                        "WHERE authorization_id = ?",
                        (previous.authorization.authorization_id,),
                    )
                ).fetchone()
                if consumed is None and _aware(previous.authorization.expires_at) > now:
                    await db.rollback()
                    return previous
            envelope = build(previous)
            await self._require_sources(db, envelope.authorization, workspace_root)
            encoded = envelope.model_dump_json()
            if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
                raise EvolutionStableRemoteFinalizationAuthorizationError(
                    "stable_remote_finalization_authorization_oversized",
                    "Remote Finalization Authorization 超过 512 KiB。",
                )
            item = envelope.authorization
            await db.execute(
                "INSERT INTO evolution_stable_remote_finalization_authorizations "
                "(authorization_id, authorization_sha256, probe_receipt_id, attempt, "
                "source_set_sha256, envelope_json, issued_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.authorization_id,
                    item.authorization_sha256,
                    item.probe_receipt_id,
                    item.attempt,
                    item.source_set_sha256,
                    encoded,
                    item.issued_at,
                    item.expires_at,
                ),
            )
            await db.commit()
        return envelope

    async def _require_sources(self, db, item, workspace_root: Path) -> None:
        probe = await (
            await db.execute(
                "SELECT receipt_json FROM "
                "evolution_stable_remote_readiness_probe_receipts "
                "WHERE receipt_id = ?",
                (item.probe_receipt_id,),
            )
        ).fetchone()
        if probe is None or json.loads(probe["receipt_json"]).get(
            "receipt_sha256"
        ) != item.probe_receipt_sha256:
            raise EvolutionStableRemoteFinalizationAuthorizationError(
                "stable_remote_finalization_probe_changed",
                "Remote Readiness Probe durable source 缺失或已变化。",
            )
        control = await (
            await db.execute(
                "SELECT event_json FROM evolution_revalidation_rollout_control_events "
                "WHERE workspace_root = ? ORDER BY sequence DESC LIMIT 1",
                (str(workspace_root),),
            )
        ).fetchone()
        event = (
            None
            if control is None
            else EvolutionRevalidationRolloutControlEvent.model_validate_json(
                control["event_json"]
            )
        )
        if _control_identity(event) != (
            item.control_sequence,
            item.control_event_id,
            item.control_event_sha256,
        ) or (
            event is not None
            and event.state is not EvolutionRevalidationRolloutControlState.ACTIVE
        ):
            raise EvolutionStableRemoteFinalizationAuthorizationError(
                "stable_remote_finalization_control_changed",
                "Rollout kill switch generation 已变化。",
            )

    async def consume(
        self,
        *,
        authorization_id: str,
        nonce_base64: str,
        consumed_at: datetime,
        workspace_root: Path,
    ) -> EvolutionStableRemoteFinalizationConsumptionReceipt:
        item_id = _authorization_id(authorization_id)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT envelope_json FROM "
                    "evolution_stable_remote_finalization_authorizations "
                    "WHERE authorization_id = ?",
                    (item_id,),
                )
            ).fetchone()
            if row is None:
                raise EvolutionStableRemoteFinalizationAuthorizationError(
                    "stable_remote_finalization_authorization_missing",
                    "Remote Finalization Authorization 不存在。",
                )
            envelope = _restore_envelope(row["envelope_json"])
            item = envelope.authorization
            existing = await (
                await db.execute(
                    "SELECT receipt_json FROM "
                    "evolution_stable_remote_finalization_consumptions "
                    "WHERE authorization_id = ?",
                    (item_id,),
                )
            ).fetchone()
            if existing is not None:
                receipt = _restore_consumption(existing["receipt_json"])
                if hmac.compare_digest(item.start_nonce_base64, str(nonce_base64)):
                    await db.rollback()
                    return receipt
                raise EvolutionStableRemoteFinalizationAuthorizationError(
                    "stable_remote_finalization_authorization_consumed",
                    "Remote Finalization Authorization 已被消费。",
                )
            await self._require_sources(db, item, workspace_root)
            if consumed_at >= _aware(item.expires_at):
                raise EvolutionStableRemoteFinalizationAuthorizationError(
                    "stable_remote_finalization_authorization_expired",
                    "Remote Finalization Authorization 已过期。",
                )
            if not hmac.compare_digest(item.start_nonce_base64, str(nonce_base64)):
                raise EvolutionStableRemoteFinalizationAuthorizationError(
                    "stable_remote_finalization_nonce_mismatch",
                    "Remote Finalization Authorization nonce 不匹配。",
                )
            receipt = _build_consumption(envelope, consumed_at)
            await db.execute(
                "INSERT INTO evolution_stable_remote_finalization_consumptions "
                "(receipt_id, authorization_id, receipt_json, consumed_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    receipt.receipt_id,
                    item_id,
                    receipt.model_dump_json(),
                    receipt.consumed_at,
                ),
            )
            await db.commit()
        return receipt


class EvolutionStableRemoteFinalizationAuthorizationService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        probe_service: EvolutionStableRemoteReadinessProbeInspectionPort,
        control_store: EvolutionRevalidationRolloutControlStore,
        rollout_key_service: ReleaseRolloutControlKeyService,
        trust_policy_path: str | Path,
        store: EvolutionStableRemoteFinalizationAuthorizationStore,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.probe_service = probe_service
        self.control_store = control_store
        self.rollout_key_service = rollout_key_service
        self.trust_policy_path = Path(trust_policy_path).expanduser()
        self.store = store
        self.clock = clock
        self.random_bytes = random_bytes
        if isinstance(probe_service, EvolutionStableRemoteReadinessProbeService):
            if probe_service.store.db_path != store.db_path:
                raise ValueError("Remote Finalization Authorization 必须与 Probe 共用 DB。")
        if control_store.db_path != store.db_path:
            raise ValueError("Remote Finalization Authorization 必须与 control 共用 DB。")
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def issue(
        self,
        *,
        probe_receipt_id: str,
        validity_seconds: int = 180,
    ) -> EvolutionStableRemoteFinalizationAuthorizationView:
        probe_id = _probe_receipt_id(probe_receipt_id)
        if not 60 <= int(validity_seconds) <= 300:
            raise ValueError("validity_seconds 必须在 60..300。")
        lock = self._locks.setdefault(probe_id, asyncio.Lock())
        async with lock:
            probe, control = await asyncio.gather(
                self.probe_service.inspect(receipt_id=probe_id),
                self.control_store.latest(self.workspace_root),
            )
            if not (
                probe.source_runtime_revalidation_authority
                and probe.binary_rollback_readiness_authority
                and not probe.config_data_rollback_readiness_authority
                and not probe.stable_rollout_authority
                and not probe.remote_execution_authority
            ):
                raise EvolutionStableRemoteFinalizationAuthorizationError(
                    "stable_remote_finalization_probe_not_current",
                    "Remote Readiness Probe 当前没有签发授权所需 authority。",
                )
            if control is not None and (
                control.state is not EvolutionRevalidationRolloutControlState.ACTIVE
            ):
                raise EvolutionStableRemoteFinalizationAuthorizationError(
                    "stable_remote_finalization_control_paused",
                    "Rollout kill switch 当前已暂停。",
                )
            now = _aware(self.clock())
            probe_expiry = _aware(probe.receipt.submission.result.expires_at)
            actual_validity = min(
                int(validity_seconds),
                int((probe_expiry - now).total_seconds()),
            )
            if actual_validity < 60:
                raise EvolutionStableRemoteFinalizationAuthorizationError(
                    "stable_remote_finalization_probe_window_too_short",
                    "Remote Readiness Probe 剩余窗口不足 60 秒。",
                )
            policy = load_release_rollout_control_trust_policy(
                self.trust_policy_path
            )
            control_identity = _control_identity(control)
            source_set = _source_values(
                probe,
                control_identity,
                _workspace_sha256(self.workspace_root),
            )
            source_sha = _digest(source_set)

            def build(previous):
                nonce = self.random_bytes(32)
                if not isinstance(nonce, bytes) or len(nonce) != 32:
                    raise EvolutionStableRemoteFinalizationAuthorizationError(
                        "stable_remote_finalization_nonce_invalid",
                        "Authorization nonce generator 必须返回 32 bytes。",
                    )
                artifact = _build_authorization(
                    workspace_root_sha256=_workspace_sha256(self.workspace_root),
                    probe=probe,
                    control_identity=control_identity,
                    previous=previous,
                    validity_seconds=actual_validity,
                    issued_at=now,
                    nonce=nonce,
                )
                signature = self.rollout_key_service.sign_authorization(
                    channel="stable",
                    payload=artifact.canonical_bytes(),
                )
                envelope = EvolutionStableRemoteFinalizationAuthorizationEnvelope(
                    authorization=artifact,
                    signature=signature,
                )
                verify_stable_remote_finalization_authorization(
                    trust_policy=policy,
                    envelope=envelope,
                    expected_member_id=artifact.installation_member_id,
                    now=self.clock(),
                )
                return envelope

            envelope = await self.store.issue(
                probe_receipt_id=probe_id,
                workspace_root=self.workspace_root,
                source_set_sha256=source_sha,
                now=now,
                build=build,
            )
        return await self.inspect(
            authorization_id=envelope.authorization.authorization_id
        )

    async def inspect(
        self,
        *,
        authorization_id: str,
    ) -> EvolutionStableRemoteFinalizationAuthorizationView:
        envelope = await self.store.get(authorization_id)
        if envelope is None:
            raise EvolutionStableRemoteFinalizationAuthorizationError(
                "stable_remote_finalization_authorization_missing",
                "Remote Finalization Authorization 不存在。",
            )
        item = envelope.authorization
        reasons: list[str] = []
        source_current = probe_current = control_current = trust_current = False
        consumption = None
        try:
            restored, probe, control, consumption = await asyncio.gather(
                self.store.get(item.authorization_id),
                self.probe_service.inspect(receipt_id=item.probe_receipt_id),
                self.control_store.latest(self.workspace_root),
                self.store.consumption(item.authorization_id),
            )
            source_current = restored == envelope
            probe_current = bool(
                probe.receipt.receipt_sha256 == item.probe_receipt_sha256
                and probe.source_runtime_revalidation_authority
                and probe.binary_rollback_readiness_authority
                and _probe_matches_authorization(probe, item)
            )
            control_current = _control_identity(control) == (
                item.control_sequence,
                item.control_event_id,
                item.control_event_sha256,
            ) and (
                control is None
                or control.state is EvolutionRevalidationRolloutControlState.ACTIVE
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            pass
        try:
            policy = load_release_rollout_control_trust_policy(
                self.trust_policy_path
            )
            _verify_trust_binding(
                trust_policy=policy,
                envelope=envelope,
                expected_member_id=item.installation_member_id,
                now=self.clock(),
            )
            trust_current = True
        except (
            EvolutionStableRemoteFinalizationAuthorizationError,
            OSError,
            ReleaseRolloutControlKeyError,
            TypeError,
            ValueError,
        ):
            pass
        expired = _aware(self.clock()) >= _aware(item.expires_at)
        consumed = consumption is not None
        if not source_current:
            reasons.append("authorization_source_changed")
        if not probe_current:
            reasons.append("remote_probe_changed")
        if not control_current:
            reasons.append("rollout_control_changed")
        if not trust_current:
            reasons.append("rollout_control_trust_changed")
        if expired:
            reasons.append("authorization_expired")
        if consumed:
            reasons.append("authorization_consumed")
        return EvolutionStableRemoteFinalizationAuthorizationView(
            envelope=envelope,
            source_current=source_current,
            probe_current=probe_current,
            control_current=control_current,
            trust_policy_current=trust_current,
            expired=expired,
            consumed=consumed,
            invalidation_reasons=tuple(sorted(set(reasons))),
            remote_finalization_authority=bool(
                source_current
                and probe_current
                and control_current
                and trust_current
                and not expired
                and not consumed
            ),
        )

    async def consume(
        self,
        *,
        authorization_id: str,
        nonce_base64: str,
    ) -> EvolutionStableRemoteFinalizationConsumptionReceipt:
        view = await self.inspect(authorization_id=authorization_id)
        if not view.remote_finalization_authority and not view.consumed:
            raise EvolutionStableRemoteFinalizationAuthorizationError(
                "stable_remote_finalization_authorization_not_current",
                "Remote Finalization Authorization 当前不可消费。",
            )
        return await self.store.consume(
            authorization_id=authorization_id,
            nonce_base64=nonce_base64,
            consumed_at=_aware(self.clock()),
            workspace_root=self.workspace_root,
        )


def verify_stable_remote_finalization_authorization(
    *,
    trust_policy: ReleaseRolloutControlTrustPolicyDocument,
    envelope: EvolutionStableRemoteFinalizationAuthorizationEnvelope,
    expected_member_id: str,
    now: str | datetime,
) -> EvolutionStableRemoteFinalizationAuthorization:
    member_id = _member_id(expected_member_id)
    item = envelope.authorization
    timestamp = _aware(now)
    _verify_trust_binding(
        trust_policy=trust_policy,
        envelope=envelope,
        expected_member_id=member_id,
        now=timestamp,
    )
    if not (
        _aware(item.issued_at)
        <= _aware(envelope.signature.signed_at)
        <= timestamp
        < _aware(item.expires_at)
    ):
        raise EvolutionStableRemoteFinalizationAuthorizationError(
            "stable_remote_finalization_authorization_expired",
            "Remote Finalization Authorization 不在有效时间窗口。",
        )
    return item


def _verify_trust_binding(
    *,
    trust_policy: ReleaseRolloutControlTrustPolicyDocument,
    envelope: EvolutionStableRemoteFinalizationAuthorizationEnvelope,
    expected_member_id: str,
    now: str | datetime,
) -> None:
    member_id = _member_id(expected_member_id)
    item = envelope.authorization
    if item.installation_member_id != member_id:
        raise EvolutionStableRemoteFinalizationAuthorizationError(
            "stable_remote_finalization_member_mismatch",
            "Remote Finalization Authorization 不属于当前 installation member。",
        )
    trusted = verify_release_rollout_control_signature(
        trust_policy=trust_policy,
        channel="stable",
        payload=item.canonical_bytes(),
        artifact=envelope.signature,
    )
    timestamp = _aware(now)
    if not (
        _aware(trusted.valid_from) <= timestamp
        and (
            trusted.valid_until is None
            or timestamp < _aware(trusted.valid_until)
        )
        and trusted.state == "active"
        and trusted.revoked_at is None
    ):
        raise EvolutionStableRemoteFinalizationAuthorizationError(
            "stable_remote_finalization_signer_not_current",
            "Rollout Control signer 在当前时刻不再受信任。",
        )


def encode_stable_remote_finalization_authorization(
    envelope: EvolutionStableRemoteFinalizationAuthorizationEnvelope,
) -> str:
    return base64.b64encode(envelope.model_dump_json().encode()).decode("ascii")


def decode_stable_remote_finalization_authorization(
    value: str,
) -> EvolutionStableRemoteFinalizationAuthorizationEnvelope:
    try:
        encoded = str(value).strip()
        if not 1 <= len(encoded) <= _MAX_ENCODED_CHARS:
            raise ValueError("artifact encoded size")
        raw = base64.b64decode(encoded, validate=True)
        if not 1 <= len(raw) <= _MAX_ARTIFACT_BYTES:
            raise ValueError("artifact size")
        return EvolutionStableRemoteFinalizationAuthorizationEnvelope.model_validate_json(
            raw
        )
    except (TypeError, ValueError) as exc:
        raise EvolutionStableRemoteFinalizationAuthorizationError(
            "stable_remote_finalization_envelope_invalid",
            "Remote Finalization Authorization envelope 无效。",
        ) from exc


def render_stable_remote_finalization_authorization(
    view: EvolutionStableRemoteFinalizationAuthorizationView,
    *,
    include_envelope: bool = False,
) -> str:
    item = view.envelope.authorization
    status = "可传输" if view.remote_finalization_authority else "已撤权"
    lines = [
        "## Signed Remote Finalization Authorization",
        "",
        f"- 状态：**{status}**",
        f"- Authorization：`{item.authorization_id}`",
        f"- Member：`{item.installation_member_id}`",
        f"- Probe：`{item.probe_receipt_id}`",
        f"- Signer：`{view.envelope.signature.signer.key_id}`",
        f"- Attempt：{item.attempt}",
        f"- Expires：`{item.expires_at}`",
        f"- Consumed：`{str(view.consumed).lower()}`",
        f"- Remote finalization authority：`{str(view.remote_finalization_authority).lower()}`",
        "- Scope：`binary_only / finalize_stable_population_member`",
        "- Config/Data mutation authority：`false`",
        "- Deployment/Rollback authority：`false`",
        "- Promotion authority：`false`",
    ]
    if include_envelope:
        lines.append(
            "- Authorization Envelope Base64：`"
            + encode_stable_remote_finalization_authorization(view.envelope)
            + "`"
        )
    return "\n".join(lines)


def _build_authorization(
    *,
    workspace_root_sha256,
    probe,
    control_identity,
    previous,
    validity_seconds,
    issued_at,
    nonce,
):
    receipt = probe.receipt
    challenge = receipt.challenge
    result = receipt.submission.result
    previous_item = None if previous is None else previous.authorization
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_STABLE_REMOTE_FINALIZATION_AUTHORIZATION_POLICY,
        "source_set_sha256": _digest(
            _source_values(probe, control_identity, workspace_root_sha256)
        ),
        "workspace_root_sha256": workspace_root_sha256,
        "probe_receipt_id": receipt.receipt_id,
        "probe_receipt_sha256": receipt.receipt_sha256,
        "claim_receipt_id": challenge.claim_receipt_id,
        "claim_receipt_sha256": challenge.claim_receipt_sha256,
        "completion_receipt_id": challenge.completion_receipt_id,
        "completion_receipt_sha256": challenge.completion_receipt_sha256,
        "population_snapshot_id": challenge.population_snapshot_id,
        "population_snapshot_sha256": challenge.population_snapshot_sha256,
        "installation_member_id": challenge.installation_member_id,
        "installation_credential_id": challenge.installation_credential_id,
        "installation_credential_sha256": challenge.installation_credential_sha256,
        "installation_public_key_sha256": challenge.installation_public_key_sha256,
        "release_root_sha256": result.release_root_sha256,
        "stable_intent_id": challenge.stable_intent_id,
        "candidate_version": result.candidate_version,
        "candidate_target": result.candidate_target,
        "expected_active_pointer_id": result.active_pointer_id,
        "expected_active_pointer_sha256": result.active_pointer_sha256,
        "expected_active_pointer_generation": result.active_pointer_generation,
        "expected_candidate_slot_id": result.candidate_slot_id,
        "expected_candidate_slot_sha256": result.candidate_slot_sha256,
        "expected_candidate_manifest_sha256": result.candidate_manifest_sha256,
        "expected_candidate_boot_receipt_id": result.candidate_boot_receipt_id,
        "expected_candidate_boot_receipt_sha256": result.candidate_boot_receipt_sha256,
        "expected_rollback_pointer_id": result.rollback_pointer_id,
        "expected_rollback_pointer_sha256": result.rollback_pointer_sha256,
        "expected_rollback_pointer_generation": result.rollback_pointer_generation,
        "expected_rollback_slot_id": result.rollback_slot_id,
        "expected_rollback_slot_sha256": result.rollback_slot_sha256,
        "expected_rollback_boot_receipt_id": result.rollback_boot_receipt_id,
        "expected_rollback_boot_receipt_sha256": result.rollback_boot_receipt_sha256,
        "control_sequence": control_identity[0],
        "control_event_id": control_identity[1],
        "control_event_sha256": control_identity[2],
        "control_state": "active",
        "attempt": 1 if previous_item is None else previous_item.attempt + 1,
        "previous_authorization_id": (
            "" if previous_item is None else previous_item.authorization_id
        ),
        "previous_authorization_sha256": (
            "" if previous_item is None else previous_item.authorization_sha256
        ),
        "operation_scope": "binary_only",
        "allowed_operations": ["finalize_stable_population_member"],
        "start_nonce_base64": base64.b64encode(nonce).decode("ascii"),
        "start_nonce_sha256": hashlib.sha256(nonce).hexdigest(),
        "validity_seconds": validity_seconds,
        "issued_at": issued_at.isoformat(),
        "expires_at": (issued_at + timedelta(seconds=validity_seconds)).isoformat(),
        "single_use_required": True,
        "release_store_finalization_authority": True,
        "remote_execution_authority": True,
        "config_data_mutation_allowed": False,
        "deployment_authority": False,
        "rollback_authority": False,
        "promotion_authority": False,
    }
    digest = _digest(core)
    return EvolutionStableRemoteFinalizationAuthorization.model_validate({
        **core,
        "authorization_id": f"evstableremotefinalauth_{digest[:24]}",
        "authorization_sha256": digest,
    })


def _build_consumption(envelope, consumed_at):
    item = envelope.authorization
    core = {
        "schema_version": 1,
        "authorization_id": item.authorization_id,
        "authorization_sha256": item.authorization_sha256,
        "signature_id": envelope.signature.signature_id,
        "installation_member_id": item.installation_member_id,
        "start_nonce_sha256": item.start_nonce_sha256,
        "consumed_at": consumed_at.isoformat(),
        "single_use_consumed": True,
    }
    digest = _digest(core)
    return EvolutionStableRemoteFinalizationConsumptionReceipt.model_validate({
        **core,
        "receipt_id": f"evstableremotefinalconsume_{digest[:24]}",
        "receipt_sha256": digest,
    })


def _source_values(probe, control_identity, workspace_root_sha256):
    receipt = probe.receipt
    result = receipt.submission.result
    return {
        "workspace_root_sha256": workspace_root_sha256,
        "probe_receipt_id": receipt.receipt_id,
        "probe_receipt_sha256": receipt.receipt_sha256,
        "claim_receipt_id": receipt.challenge.claim_receipt_id,
        "claim_receipt_sha256": receipt.challenge.claim_receipt_sha256,
        "completion_receipt_id": receipt.challenge.completion_receipt_id,
        "completion_receipt_sha256": receipt.challenge.completion_receipt_sha256,
        "installation_member_id": receipt.challenge.installation_member_id,
        "stable_intent_id": receipt.challenge.stable_intent_id,
        "release_root_sha256": result.release_root_sha256,
        "active_pointer_id": result.active_pointer_id,
        "active_pointer_sha256": result.active_pointer_sha256,
        "candidate_slot_id": result.candidate_slot_id,
        "candidate_slot_sha256": result.candidate_slot_sha256,
        "rollback_slot_id": result.rollback_slot_id,
        "rollback_slot_sha256": result.rollback_slot_sha256,
        "control_sequence": control_identity[0],
        "control_event_id": control_identity[1],
        "control_event_sha256": control_identity[2],
    }


def _source_identity(item):
    return {
        "workspace_root_sha256": item.workspace_root_sha256,
        "probe_receipt_id": item.probe_receipt_id,
        "probe_receipt_sha256": item.probe_receipt_sha256,
        "claim_receipt_id": item.claim_receipt_id,
        "claim_receipt_sha256": item.claim_receipt_sha256,
        "completion_receipt_id": item.completion_receipt_id,
        "completion_receipt_sha256": item.completion_receipt_sha256,
        "installation_member_id": item.installation_member_id,
        "stable_intent_id": item.stable_intent_id,
        "release_root_sha256": item.release_root_sha256,
        "active_pointer_id": item.expected_active_pointer_id,
        "active_pointer_sha256": item.expected_active_pointer_sha256,
        "candidate_slot_id": item.expected_candidate_slot_id,
        "candidate_slot_sha256": item.expected_candidate_slot_sha256,
        "rollback_slot_id": item.expected_rollback_slot_id,
        "rollback_slot_sha256": item.expected_rollback_slot_sha256,
        "control_sequence": item.control_sequence,
        "control_event_id": item.control_event_id,
        "control_event_sha256": item.control_event_sha256,
    }


def _probe_matches_authorization(probe, item) -> bool:
    try:
        return _digest(
            _source_values(
                probe,
                (
                    item.control_sequence,
                    item.control_event_id,
                    item.control_event_sha256,
                ),
                item.workspace_root_sha256,
            )
        ) == item.source_set_sha256
    except (AttributeError, TypeError, ValueError):
        return False


def _control_identity(control):
    if control is None:
        return 0, "", ""
    return control.sequence, control.event_id, control.event_sha256


def _workspace_sha256(path: Path) -> str:
    return hashlib.sha256(str(path).encode()).hexdigest()


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS "
        "evolution_stable_remote_finalization_authorizations ("
        "authorization_id TEXT PRIMARY KEY, authorization_sha256 TEXT NOT NULL UNIQUE, "
        "probe_receipt_id TEXT NOT NULL, attempt INTEGER NOT NULL, "
        "source_set_sha256 TEXT NOT NULL, envelope_json TEXT NOT NULL, "
        "issued_at TEXT NOT NULL, expires_at TEXT NOT NULL, "
        "UNIQUE(probe_receipt_id, attempt))"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS "
        "evolution_stable_remote_finalization_consumptions ("
        "receipt_id TEXT PRIMARY KEY, authorization_id TEXT NOT NULL UNIQUE, "
        "receipt_json TEXT NOT NULL, consumed_at TEXT NOT NULL)"
    )


def _restore_envelope(value: str):
    try:
        return EvolutionStableRemoteFinalizationAuthorizationEnvelope.model_validate_json(
            value
        )
    except (TypeError, ValueError) as exc:
        raise EvolutionStableRemoteFinalizationAuthorizationError(
            "stable_remote_finalization_store_corrupt",
            "Remote Finalization Authorization durable artifact 已损坏。",
        ) from exc


def _restore_consumption(value: str):
    try:
        return EvolutionStableRemoteFinalizationConsumptionReceipt.model_validate_json(
            value
        )
    except (TypeError, ValueError) as exc:
        raise EvolutionStableRemoteFinalizationAuthorizationError(
            "stable_remote_finalization_store_corrupt",
            "Remote Finalization Consumption durable artifact 已损坏。",
        ) from exc


def _authorization_id(value: str) -> str:
    normalized = str(value).strip()
    if _AUTH_RE.fullmatch(normalized) is None:
        raise ValueError("authorization_id 格式无效。")
    return normalized


def _probe_receipt_id(value: str) -> str:
    normalized = str(value).strip()
    if _PROBE_RE.fullmatch(normalized) is None:
        raise ValueError("probe_receipt_id 格式无效。")
    return normalized


def _member_id(value: str) -> str:
    normalized = str(value).strip()
    if _MEMBER_RE.fullmatch(normalized) is None:
        raise ValueError("installation_member_id 格式无效。")
    return normalized


def _decode_base64(value: str, expected_bytes: int, label: str) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} base64 无效。") from exc
    if len(decoded) != expected_bytes or base64.b64encode(decoded).decode() != value:
        raise ValueError(f"{label} bytes 无效。")
    return decoded


def _aware(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(parsed, datetime) or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp 必须包含 offset。")
    return parsed.astimezone(UTC)


def _canonical_bytes(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _digest(value) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


__all__ = [
    "EVOLUTION_STABLE_REMOTE_FINALIZATION_AUTHORIZATION_POLICY",
    "EvolutionStableRemoteFinalizationAuthorization",
    "EvolutionStableRemoteFinalizationAuthorizationEnvelope",
    "EvolutionStableRemoteFinalizationAuthorizationError",
    "EvolutionStableRemoteFinalizationAuthorizationService",
    "EvolutionStableRemoteFinalizationAuthorizationStore",
    "EvolutionStableRemoteFinalizationAuthorizationView",
    "EvolutionStableRemoteFinalizationConsumptionReceipt",
    "decode_stable_remote_finalization_authorization",
    "encode_stable_remote_finalization_authorization",
    "render_stable_remote_finalization_authorization",
    "verify_stable_remote_finalization_authorization",
]
