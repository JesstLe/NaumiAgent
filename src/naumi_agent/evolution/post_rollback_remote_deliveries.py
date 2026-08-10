"""Encrypted baseline descriptors and authenticated Worker delivery ACKs."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Self
from weakref import WeakValueDictionary

import aiosqlite
from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.daemons.authenticated_worker_identity import (
    AuthenticatedWorkerIdentity,
    AuthenticatedWorkerIdentityAuthority,
    AuthenticatedWorkerIdentityError,
)
from naumi_agent.daemons.authenticated_worker_transport_key import (
    AuthenticatedWorkerTransportKey,
    AuthenticatedWorkerTransportKeyAuthority,
    AuthenticatedWorkerTransportKeyError,
)
from naumi_agent.evolution.post_rollback_remote_claims import (
    EvolutionPostRollbackRemoteClaimError,
    EvolutionPostRollbackRemoteClaimReceipt,
    EvolutionPostRollbackRemoteClaimService,
    EvolutionPostRollbackRemoteClaimStore,
)
from naumi_agent.evolution.post_rollback_remote_dispatches import (
    EvolutionPostRollbackRemoteDispatch,
    EvolutionPostRollbackRemoteDispatchError,
    EvolutionPostRollbackRemoteDispatchService,
    EvolutionPostRollbackRemoteDispatchStore,
)
from naumi_agent.evolution.post_rollback_target_baselines import (
    EvolutionPostRollbackTargetBaseline,
    EvolutionPostRollbackTargetBaselineError,
    EvolutionPostRollbackTargetBaselineService,
    EvolutionPostRollbackTargetBaselineStore,
)
from naumi_agent.release.artifact_fetch import (
    ReleaseArtifactDownloadView,
    ReleaseArtifactFetchError,
    ReleaseArtifactFetchService,
)
from naumi_agent.release.channel_catalog import ReleaseChannelResolution

EVOLUTION_POST_ROLLBACK_REMOTE_DELIVERY_POLICY = (
    "evolution-post-rollback-remote-delivery-v1"
)
EVOLUTION_POST_ROLLBACK_REMOTE_DELIVERY_DOMAIN = (
    "naumi.evolution.post-rollback-remote-delivery.v1"
)
EVOLUTION_POST_ROLLBACK_REMOTE_DELIVERY_ACK_DOMAIN = (
    "naumi.evolution.post-rollback-remote-delivery-ack.v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_B64_12_RE = r"^[A-Za-z0-9+/]{16}$"
_B64_32_RE = r"^[A-Za-z0-9+/]{43}=$"
_B64_64_RE = r"^[A-Za-z0-9+/]{86}==$"
_MAX_DESCRIPTOR_BYTES = 2 * 1024 * 1024
_MAX_OFFER_BYTES = 4 * 1024 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionPostRollbackRemoteDeliveryDescriptor(_StrictModel):
    schema_version: Literal[1] = 1
    domain: Literal["naumi.evolution.post-rollback-remote-delivery.v1"] = (
        EVOLUTION_POST_ROLLBACK_REMOTE_DELIVERY_DOMAIN
    )
    delivery_id: str = Field(pattern=r"^evpostdelivery_[0-9a-f]{24}$")
    dispatch_id: str = Field(pattern=r"^evpostdispatch_[0-9a-f]{24}$")
    dispatch_sha256: str = Field(pattern=_SHA256_RE)
    claim_id: str = Field(pattern=r"^evpostclaim_[0-9a-f]{24}$")
    claim_receipt_id: str = Field(pattern=r"^evpostclaimreceipt_[0-9a-f]{24}$")
    claim_receipt_sha256: str = Field(pattern=_SHA256_RE)
    claim_lease_epoch: int = Field(ge=1, le=1_000_000)
    transport_key_id: str = Field(pattern=r"^workertransportkey_[0-9a-f]{24}$")
    transport_key_sha256: str = Field(pattern=_SHA256_RE)
    transport_key_generation: int = Field(ge=1, le=1_000_000)
    identity_id: str = Field(pattern=r"^workeridentity_[0-9a-f]{24}$")
    identity_sha256: str = Field(pattern=_SHA256_RE)
    worker_id: str = Field(min_length=1, max_length=128)
    worker_instance_id: str = Field(min_length=1, max_length=128)
    worker_epoch: int = Field(ge=1)
    baseline_resolution_id: str = Field(pattern=r"^evpostbaseline_[0-9a-f]{24}$")
    baseline_resolution_sha256: str = Field(pattern=_SHA256_RE)
    release_target: str = Field(pattern=r"^(?:macos|linux|windows)-(?:arm64|x64)$")
    channel_resolution: ReleaseChannelResolution
    build_attestation_id: str = Field(pattern=r"^relbuildatt_[0-9a-f]{24}$")
    build_attestation_sha256: str = Field(pattern=_SHA256_RE)
    download_receipt_id: str = Field(pattern=r"^reldownload_[0-9a-f]{24}$")
    download_receipt_sha256: str = Field(pattern=_SHA256_RE)
    download_source_id: str = Field(pattern=r"^reldownloadsource_[0-9a-f]{24}$")
    download_source_sha256: str = Field(pattern=_SHA256_RE)
    archive_name: str = Field(min_length=1, max_length=512)
    archive_sha256: str = Field(pattern=_SHA256_RE)
    archive_size_bytes: int = Field(gt=0, le=8 * 1024**3)
    manifest_sha256: str = Field(pattern=_SHA256_RE)
    ack_nonce_base64: str = Field(pattern=_B64_32_RE)
    issued_at: str = Field(min_length=1, max_length=100)
    expires_at: str = Field(min_length=1, max_length=100)
    installation_authority: Literal[False] = False
    execution_authority: Literal[False] = False
    result_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        _decode_base64(self.ack_nonce_base64, expected=32, field="Delivery ACK nonce")
        if _aware(self.expires_at) <= _aware(self.issued_at):
            raise ValueError("Remote Delivery descriptor expiry 无效。")
        entry = self.channel_resolution.entry
        attestation = entry.build_attestation
        if not (
            self.release_target == self.channel_resolution.target == entry.target
            and self.build_attestation_id == attestation.attestation_id
            and self.build_attestation_sha256 == attestation.attestation_sha256
            and self.archive_name == entry.archive_name
            and self.archive_sha256 == entry.archive_sha256
            and self.archive_size_bytes == entry.archive_size_bytes
            and self.manifest_sha256 == entry.manifest_sha256
        ):
            raise ValueError("Remote Delivery descriptor release lineage 不一致。")
        if self.installation_authority or self.execution_authority or self.result_authority:
            raise ValueError("Remote Delivery descriptor 不得扩大执行权。")
        expected = _delivery_id(
            dispatch_sha256=self.dispatch_sha256,
            claim_receipt_sha256=self.claim_receipt_sha256,
            transport_key_sha256=self.transport_key_sha256,
            baseline_resolution_sha256=self.baseline_resolution_sha256,
            download_receipt_sha256=self.download_receipt_sha256,
        )
        if self.delivery_id != expected:
            raise ValueError("Remote Delivery identity 不一致。")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical(self.model_dump(mode="json"))


class EvolutionPostRollbackRemoteDeliveryEnvelope(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-post-rollback-remote-delivery-v1"] = (
        EVOLUTION_POST_ROLLBACK_REMOTE_DELIVERY_POLICY
    )
    delivery_id: str = Field(pattern=r"^evpostdelivery_[0-9a-f]{24}$")
    envelope_sha256: str = Field(pattern=_SHA256_RE)
    descriptor_sha256: str = Field(pattern=_SHA256_RE)
    dispatch_sha256: str = Field(pattern=_SHA256_RE)
    claim_receipt_sha256: str = Field(pattern=_SHA256_RE)
    transport_key_id: str = Field(pattern=r"^workertransportkey_[0-9a-f]{24}$")
    transport_key_sha256: str = Field(pattern=_SHA256_RE)
    archive_sha256: str = Field(pattern=_SHA256_RE)
    manifest_sha256: str = Field(pattern=_SHA256_RE)
    expires_at: str = Field(min_length=1, max_length=100)
    key_agreement_algorithm: Literal["x25519"] = "x25519"
    key_derivation_algorithm: Literal["hkdf-sha256"] = "hkdf-sha256"
    payload_algorithm: Literal["aes-256-gcm"] = "aes-256-gcm"
    ephemeral_public_key_base64: str = Field(pattern=_B64_32_RE)
    ephemeral_public_key_sha256: str = Field(pattern=_SHA256_RE)
    hkdf_salt_base64: str = Field(pattern=_B64_32_RE)
    nonce_base64: str = Field(pattern=_B64_12_RE)
    ciphertext_base64: str = Field(min_length=24, max_length=3_000_000, repr=False)
    ciphertext_sha256: str = Field(pattern=_SHA256_RE)
    plaintext_bytes: int = Field(gt=0, le=_MAX_DESCRIPTOR_BYTES)
    aad_sha256: str = Field(pattern=_SHA256_RE)
    private_key_stored: Literal[False] = False
    transport_delivered: Literal[False] = False
    execution_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        ephemeral = _decode_base64(
            self.ephemeral_public_key_base64,
            expected=32,
            field="Delivery ephemeral public key",
        )
        _decode_base64(self.hkdf_salt_base64, expected=32, field="Delivery HKDF salt")
        _decode_base64(self.nonce_base64, expected=12, field="Delivery nonce")
        ciphertext = _decode_base64(
            self.ciphertext_base64,
            expected=self.plaintext_bytes + 16,
            field="Delivery ciphertext",
        )
        if not (
            hmac.compare_digest(
                self.ephemeral_public_key_sha256,
                hashlib.sha256(ephemeral).hexdigest(),
            )
            and hmac.compare_digest(
                self.ciphertext_sha256,
                hashlib.sha256(ciphertext).hexdigest(),
            )
            and hmac.compare_digest(
                self.aad_sha256,
                hashlib.sha256(_delivery_aad(self)).hexdigest(),
            )
        ):
            raise ValueError("Remote Delivery envelope key/ciphertext/AAD 摘要不一致。")
        core = self.model_dump(mode="json", exclude={"envelope_sha256"})
        if not hmac.compare_digest(self.envelope_sha256, _digest(core)):
            raise ValueError("Remote Delivery envelope 摘要不一致。")
        if self.transport_delivered or self.execution_authority:
            raise ValueError("Remote Delivery envelope 不得冒充 delivery authority。")
        return self


class EvolutionPostRollbackRemoteDeliveryAckPayload(_StrictModel):
    schema_version: Literal[1] = 1
    domain: Literal["naumi.evolution.post-rollback-remote-delivery-ack.v1"] = (
        EVOLUTION_POST_ROLLBACK_REMOTE_DELIVERY_ACK_DOMAIN
    )
    delivery_id: str = Field(pattern=r"^evpostdelivery_[0-9a-f]{24}$")
    envelope_sha256: str = Field(pattern=_SHA256_RE)
    descriptor_sha256: str = Field(pattern=_SHA256_RE)
    ciphertext_sha256: str = Field(pattern=_SHA256_RE)
    dispatch_sha256: str = Field(pattern=_SHA256_RE)
    claim_receipt_sha256: str = Field(pattern=_SHA256_RE)
    transport_key_sha256: str = Field(pattern=_SHA256_RE)
    identity_id: str = Field(pattern=r"^workeridentity_[0-9a-f]{24}$")
    identity_sha256: str = Field(pattern=_SHA256_RE)
    worker_id: str = Field(min_length=1, max_length=128)
    worker_instance_id: str = Field(min_length=1, max_length=128)
    worker_epoch: int = Field(ge=1)
    archive_sha256: str = Field(pattern=_SHA256_RE)
    archive_size_bytes: int = Field(gt=0, le=8 * 1024**3)
    manifest_sha256: str = Field(pattern=_SHA256_RE)
    ack_nonce_base64: str = Field(pattern=_B64_32_RE)
    descriptor_decrypted: Literal[True] = True
    archive_downloaded: Literal[True] = True
    archive_digest_verified: Literal[True] = True
    manifest_digest_verified: Literal[True] = True
    installation_executed: Literal[False] = False
    evaluation_executed: Literal[False] = False

    def canonical_bytes(self) -> bytes:
        return _canonical(self.model_dump(mode="json"))


class EvolutionPostRollbackRemoteDeliveryOffer(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-post-rollback-remote-delivery-v1"] = (
        EVOLUTION_POST_ROLLBACK_REMOTE_DELIVERY_POLICY
    )
    offer_id: str = Field(pattern=r"^evpostdeliveryoffer_[0-9a-f]{24}$")
    offer_sha256: str = Field(pattern=_SHA256_RE)
    delivery_id: str = Field(pattern=r"^evpostdelivery_[0-9a-f]{24}$")
    dispatch_id: str = Field(pattern=r"^evpostdispatch_[0-9a-f]{24}$")
    dispatch_sha256: str = Field(pattern=_SHA256_RE)
    claim_id: str = Field(pattern=r"^evpostclaim_[0-9a-f]{24}$")
    claim_receipt_id: str = Field(pattern=r"^evpostclaimreceipt_[0-9a-f]{24}$")
    claim_receipt_sha256: str = Field(pattern=_SHA256_RE)
    claim_lease_epoch: int = Field(ge=1, le=1_000_000)
    transport_key_id: str = Field(pattern=r"^workertransportkey_[0-9a-f]{24}$")
    transport_key_sha256: str = Field(pattern=_SHA256_RE)
    identity_id: str = Field(pattern=r"^workeridentity_[0-9a-f]{24}$")
    identity_sha256: str = Field(pattern=_SHA256_RE)
    baseline_resolution_id: str = Field(pattern=r"^evpostbaseline_[0-9a-f]{24}$")
    baseline_resolution_sha256: str = Field(pattern=_SHA256_RE)
    build_attestation_sha256: str = Field(pattern=_SHA256_RE)
    download_receipt_id: str = Field(pattern=r"^reldownload_[0-9a-f]{24}$")
    download_receipt_sha256: str = Field(pattern=_SHA256_RE)
    download_source_id: str = Field(pattern=r"^reldownloadsource_[0-9a-f]{24}$")
    download_source_sha256: str = Field(pattern=_SHA256_RE)
    issued_at: str = Field(min_length=1, max_length=100)
    envelope: EvolutionPostRollbackRemoteDeliveryEnvelope
    ack_payload: EvolutionPostRollbackRemoteDeliveryAckPayload
    ack_signable_payload_sha256: str = Field(pattern=_SHA256_RE)
    state: Literal["awaiting_ack"] = "awaiting_ack"
    transport_delivered: Literal[False] = False
    execution_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if _aware(self.envelope.expires_at) <= _aware(self.issued_at):
            raise ValueError("Remote Delivery offer 时间窗口无效。")
        if not (
            self.delivery_id == self.envelope.delivery_id == self.ack_payload.delivery_id
            and self.dispatch_sha256 == self.envelope.dispatch_sha256
            and self.dispatch_sha256 == self.ack_payload.dispatch_sha256
            and self.claim_receipt_sha256 == self.envelope.claim_receipt_sha256
            and self.claim_receipt_sha256 == self.ack_payload.claim_receipt_sha256
            and self.transport_key_id == self.envelope.transport_key_id
            and self.transport_key_sha256 == self.envelope.transport_key_sha256
            and self.transport_key_sha256 == self.ack_payload.transport_key_sha256
            and self.identity_id == self.ack_payload.identity_id
            and self.identity_sha256 == self.ack_payload.identity_sha256
            and self.envelope.envelope_sha256 == self.ack_payload.envelope_sha256
            and self.envelope.descriptor_sha256 == self.ack_payload.descriptor_sha256
            and self.envelope.ciphertext_sha256 == self.ack_payload.ciphertext_sha256
            and hmac.compare_digest(
                self.ack_signable_payload_sha256,
                hashlib.sha256(self.ack_payload.canonical_bytes()).hexdigest(),
            )
        ):
            raise ValueError("Remote Delivery offer envelope/ACK binding 不一致。")
        core = self.model_dump(mode="json", exclude={"offer_id", "offer_sha256"})
        digest = _digest(core)
        if not (
            hmac.compare_digest(self.offer_sha256, digest)
            and self.offer_id == f"evpostdeliveryoffer_{digest[:24]}"
        ):
            raise ValueError("Remote Delivery offer identity 不一致。")
        return self


class EvolutionPostRollbackRemoteDeliveryReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-post-rollback-remote-delivery-v1"] = (
        EVOLUTION_POST_ROLLBACK_REMOTE_DELIVERY_POLICY
    )
    receipt_id: str = Field(pattern=r"^evpostdeliveryreceipt_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    delivery_id: str = Field(pattern=r"^evpostdelivery_[0-9a-f]{24}$")
    offer_id: str = Field(pattern=r"^evpostdeliveryoffer_[0-9a-f]{24}$")
    offer_sha256: str = Field(pattern=_SHA256_RE)
    envelope_sha256: str = Field(pattern=_SHA256_RE)
    descriptor_sha256: str = Field(pattern=_SHA256_RE)
    dispatch_id: str = Field(pattern=r"^evpostdispatch_[0-9a-f]{24}$")
    dispatch_sha256: str = Field(pattern=_SHA256_RE)
    claim_id: str = Field(pattern=r"^evpostclaim_[0-9a-f]{24}$")
    claim_receipt_id: str = Field(pattern=r"^evpostclaimreceipt_[0-9a-f]{24}$")
    claim_receipt_sha256: str = Field(pattern=_SHA256_RE)
    claim_lease_epoch: int = Field(ge=1, le=1_000_000)
    transport_key_id: str = Field(pattern=r"^workertransportkey_[0-9a-f]{24}$")
    transport_key_sha256: str = Field(pattern=_SHA256_RE)
    identity_id: str = Field(pattern=r"^workeridentity_[0-9a-f]{24}$")
    identity_sha256: str = Field(pattern=_SHA256_RE)
    worker_id: str = Field(min_length=1, max_length=128)
    worker_instance_id: str = Field(min_length=1, max_length=128)
    worker_epoch: int = Field(ge=1)
    baseline_resolution_id: str = Field(pattern=r"^evpostbaseline_[0-9a-f]{24}$")
    baseline_resolution_sha256: str = Field(pattern=_SHA256_RE)
    build_attestation_sha256: str = Field(pattern=_SHA256_RE)
    download_receipt_sha256: str = Field(pattern=_SHA256_RE)
    download_source_id: str = Field(pattern=r"^reldownloadsource_[0-9a-f]{24}$")
    download_source_sha256: str = Field(pattern=_SHA256_RE)
    archive_sha256: str = Field(pattern=_SHA256_RE)
    archive_size_bytes: int = Field(gt=0, le=8 * 1024**3)
    manifest_sha256: str = Field(pattern=_SHA256_RE)
    ack_payload_sha256: str = Field(pattern=_SHA256_RE)
    worker_signature_base64: str = Field(pattern=_B64_64_RE)
    worker_signature_sha256: str = Field(pattern=_SHA256_RE)
    worker_signature_verified: Literal[True] = True
    acknowledged_at: str = Field(min_length=1, max_length=100)
    state: Literal["delivered"] = "delivered"
    transport_delivered: Literal[True] = True
    installation_authority: Literal[False] = False
    execution_authority: Literal[False] = False
    result_authority: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        signature = _decode_base64(
            self.worker_signature_base64,
            expected=64,
            field="Delivery ACK signature",
        )
        _aware(self.acknowledged_at)
        if not hmac.compare_digest(
            self.worker_signature_sha256,
            hashlib.sha256(signature).hexdigest(),
        ):
            raise ValueError("Remote Delivery ACK signature 摘要不一致。")
        core = self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        digest = _digest(core)
        if not (
            hmac.compare_digest(self.receipt_sha256, digest)
            and self.receipt_id == f"evpostdeliveryreceipt_{digest[:24]}"
        ):
            raise ValueError("Remote Delivery receipt identity 不一致。")
        if (
            self.installation_authority
            or self.execution_authority
            or self.result_authority
            or self.learning_authority
            or self.promotion_authority
        ):
            raise ValueError("Remote Delivery receipt 不得扩大 execution/result authority。")
        return self


class EvolutionPostRollbackRemoteDeliveryView(_StrictModel):
    offer: EvolutionPostRollbackRemoteDeliveryOffer
    receipt: EvolutionPostRollbackRemoteDeliveryReceipt | None
    status: Literal["awaiting_ack", "delivered", "expired", "stale"]
    claim_authority: bool
    transport_key_authority: bool
    baseline_authority: bool
    download_authority: bool
    transport_delivered: bool
    execution_authority: Literal[False] = False
    result_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        current = bool(
            self.status == "delivered"
            and self.receipt is not None
            and self.claim_authority
            and self.transport_key_authority
            and self.baseline_authority
            and self.download_authority
        )
        if self.transport_delivered is not current:
            raise ValueError("Remote Delivery authority 投影不一致。")
        return self


class EvolutionPostRollbackRemoteDeliveryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionPostRollbackRemoteDeliveryStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve(strict=False)

    async def get_offer(
        self,
        delivery_id: str,
    ) -> EvolutionPostRollbackRemoteDeliveryOffer | None:
        normalized = _delivery_id_value(delivery_id)
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT offer_json FROM evolution_post_rollback_remote_delivery_offers "
                        "WHERE delivery_id = ?",
                        (normalized,),
                    )
                ).fetchone()
            return None if row is None else _restore_offer(row["offer_json"])
        except EvolutionPostRollbackRemoteDeliveryError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackRemoteDeliveryError(
                "post_rollback_remote_delivery_store_corrupt",
                "Remote Delivery offer 损坏或无法读取。",
            ) from exc

    async def get_receipt(
        self,
        delivery_id: str,
    ) -> EvolutionPostRollbackRemoteDeliveryReceipt | None:
        normalized = _delivery_id_value(delivery_id)
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT receipt_json FROM evolution_post_rollback_remote_delivery_receipts "
                        "WHERE delivery_id = ?",
                        (normalized,),
                    )
                ).fetchone()
            return None if row is None else _restore_receipt(row["receipt_json"])
        except EvolutionPostRollbackRemoteDeliveryError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackRemoteDeliveryError(
                "post_rollback_remote_delivery_store_corrupt",
                "Remote Delivery receipt 损坏或无法读取。",
            ) from exc

    async def record_offer(
        self,
        offer: EvolutionPostRollbackRemoteDeliveryOffer,
    ) -> EvolutionPostRollbackRemoteDeliveryOffer:
        item = _validate_offer(offer)
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_OFFER_BYTES:
            raise EvolutionPostRollbackRemoteDeliveryError(
                "post_rollback_remote_delivery_offer_oversized",
                "Remote Delivery offer 超过 4 MiB。",
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                await _require_atomic_offer_authority(db, item)
                existing_row = await (
                    await db.execute(
                        "SELECT offer_json FROM evolution_post_rollback_remote_delivery_offers "
                        "WHERE delivery_id = ?",
                        (item.delivery_id,),
                    )
                ).fetchone()
                if existing_row is not None:
                    existing = _restore_offer(existing_row["offer_json"])
                    await db.rollback()
                    if _same_logical_offer(existing, item):
                        return existing
                    raise EvolutionPostRollbackRemoteDeliveryError(
                        "post_rollback_remote_delivery_offer_conflict",
                        "同一 Remote Delivery identity 已绑定不同 lineage。",
                    )
                await db.execute(
                    "INSERT INTO evolution_post_rollback_remote_delivery_offers "
                    "(delivery_id, offer_id, offer_sha256, claim_id, claim_receipt_id, "
                    "transport_key_id, baseline_resolution_id, offer_json, state, "
                    "expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_ack', ?)",
                    (
                        item.delivery_id,
                        item.offer_id,
                        item.offer_sha256,
                        item.claim_id,
                        item.claim_receipt_id,
                        item.transport_key_id,
                        item.baseline_resolution_id,
                        encoded,
                        item.envelope.expires_at,
                    ),
                )
                await db.commit()
            return item
        except EvolutionPostRollbackRemoteDeliveryError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackRemoteDeliveryError(
                "post_rollback_remote_delivery_store_failed",
                "Remote Delivery offer 无法持久化。",
            ) from exc

    async def acknowledge(
        self,
        *,
        delivery_id: str,
        worker_signature_base64: str,
        acknowledged_at: str,
    ) -> EvolutionPostRollbackRemoteDeliveryReceipt:
        normalized = _delivery_id_value(delivery_id)
        signature = _decode_base64(
            worker_signature_base64,
            expected=64,
            field="Delivery ACK signature",
        )
        now = _aware(acknowledged_at)
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                row = await (
                    await db.execute(
                        "SELECT offer_json, state FROM "
                        "evolution_post_rollback_remote_delivery_offers "
                        "WHERE delivery_id = ?",
                        (normalized,),
                    )
                ).fetchone()
                if row is None:
                    await db.rollback()
                    raise EvolutionPostRollbackRemoteDeliveryError(
                        "post_rollback_remote_delivery_offer_missing",
                        "Remote Delivery offer 不存在。",
                    )
                offer = _restore_offer(row["offer_json"])
                if str(row["state"]) == "delivered":
                    receipt_row = await (
                        await db.execute(
                            "SELECT receipt_json FROM "
                            "evolution_post_rollback_remote_delivery_receipts "
                            "WHERE delivery_id = ?",
                            (normalized,),
                        )
                    ).fetchone()
                    await db.rollback()
                    if receipt_row is None:
                        raise EvolutionPostRollbackRemoteDeliveryError(
                            "post_rollback_remote_delivery_atomic_gap",
                            "Remote Delivery delivered state 缺少 receipt。",
                        )
                    receipt = _restore_receipt(receipt_row["receipt_json"])
                    if hmac.compare_digest(
                        receipt.worker_signature_base64,
                        worker_signature_base64,
                    ):
                        return receipt
                    raise EvolutionPostRollbackRemoteDeliveryError(
                        "post_rollback_remote_delivery_ack_conflict",
                        "Remote Delivery 已由不同 ACK 关闭。",
                    )
                if not _aware(offer.issued_at) <= now < _aware(
                    offer.envelope.expires_at
                ):
                    await db.rollback()
                    raise EvolutionPostRollbackRemoteDeliveryError(
                        "post_rollback_remote_delivery_ack_time_invalid",
                        "Remote Delivery ACK 时间不在 offer 窗口内。",
                    )
                identity = await _require_atomic_offer_authority(db, offer)
                public_key = Ed25519PublicKey.from_public_bytes(
                    _decode_base64(
                        identity.public_key_base64,
                        expected=32,
                        field="Worker Identity public key",
                    )
                )
                try:
                    public_key.verify(signature, offer.ack_payload.canonical_bytes())
                except InvalidSignature as exc:
                    await db.rollback()
                    raise EvolutionPostRollbackRemoteDeliveryError(
                        "post_rollback_remote_delivery_ack_signature_invalid",
                        "Remote Delivery Worker ACK signature 无效。",
                    ) from exc
                receipt = _build_receipt(
                    offer=offer,
                    signature_base64=worker_signature_base64,
                    acknowledged_at=now.isoformat(),
                )
                await db.execute(
                    "INSERT INTO evolution_post_rollback_remote_delivery_receipts "
                    "(receipt_id, receipt_sha256, delivery_id, offer_id, receipt_json, "
                    "acknowledged_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        receipt.receipt_id,
                        receipt.receipt_sha256,
                        receipt.delivery_id,
                        receipt.offer_id,
                        receipt.model_dump_json(),
                        receipt.acknowledged_at,
                    ),
                )
                await db.execute(
                    "UPDATE evolution_post_rollback_remote_delivery_offers "
                    "SET state = 'delivered' WHERE delivery_id = ? AND state = 'awaiting_ack'",
                    (normalized,),
                )
                await db.commit()
            return receipt
        except EvolutionPostRollbackRemoteDeliveryError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackRemoteDeliveryError(
                "post_rollback_remote_delivery_store_failed",
                "Remote Delivery ACK 无法持久化。",
            ) from exc


class EvolutionPostRollbackRemoteDeliveryService:
    def __init__(
        self,
        *,
        claim_service: EvolutionPostRollbackRemoteClaimService,
        claim_store: EvolutionPostRollbackRemoteClaimStore,
        dispatch_service: EvolutionPostRollbackRemoteDispatchService,
        dispatch_store: EvolutionPostRollbackRemoteDispatchStore,
        baseline_service: EvolutionPostRollbackTargetBaselineService,
        baseline_store: EvolutionPostRollbackTargetBaselineStore,
        identity_authority: AuthenticatedWorkerIdentityAuthority,
        transport_key_authority: AuthenticatedWorkerTransportKeyAuthority,
        artifact_fetch_service: ReleaseArtifactFetchService,
        store: EvolutionPostRollbackRemoteDeliveryStore,
        random_bytes: Callable[[int], bytes] = os.urandom,
        ephemeral_key_factory: Callable[[], X25519PrivateKey] = X25519PrivateKey.generate,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        paths = {
            claim_store.db_path,
            dispatch_store.db_path,
            baseline_store.db_path,
            identity_authority.store.db_path,
            transport_key_authority.store.db_path,
            store.db_path,
        }
        if len(paths) != 1:
            raise ValueError(
                "Remote Delivery 的 Claim/Dispatch/Baseline/Identity/Transport/Delivery "
                "store 必须共享 SQLite authority。"
            )
        if not callable(random_bytes) or not callable(ephemeral_key_factory):
            raise TypeError("Remote Delivery crypto factory 必须可调用。")
        self.claim_service = claim_service
        self.claim_store = claim_store
        self.dispatch_service = dispatch_service
        self.dispatch_store = dispatch_store
        self.baseline_service = baseline_service
        self.baseline_store = baseline_store
        self.identity_authority = identity_authority
        self.transport_key_authority = transport_key_authority
        self.artifact_fetch_service = artifact_fetch_service
        self.store = store
        self.random_bytes = random_bytes
        self.ephemeral_key_factory = ephemeral_key_factory
        self.clock = clock or (lambda: datetime.now(UTC))
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def prepare(
        self,
        *,
        claim_id: str,
        issued_at: str | None = None,
        offer_ttl_seconds: int = 120,
    ) -> EvolutionPostRollbackRemoteDeliveryView:
        if not 1 <= offer_ttl_seconds <= 300:
            raise EvolutionPostRollbackRemoteDeliveryError(
                "post_rollback_remote_delivery_ttl_invalid",
                "Remote Delivery offer TTL 必须为 1..300 秒。",
            )
        explicit_time = issued_at is not None
        now = _aware(issued_at) if explicit_time else _aware_datetime(self.clock())
        lock = self._locks.setdefault(f"prepare:{claim_id}", asyncio.Lock())
        async with lock:
            context = await self._context(claim_id=claim_id, now=now, fetch=True)
            if not explicit_time:
                now = _aware_datetime(self.clock())
                context = await self._context(
                    claim_id=claim_id,
                    now=now,
                    fetch=False,
                    download_source_id=context.download.receipt.source_id,
                )
            expiry = min(
                now + timedelta(seconds=offer_ttl_seconds),
                _aware(context.claim.lease_expires_at),
            )
            if expiry <= now:
                raise EvolutionPostRollbackRemoteDeliveryError(
                    "post_rollback_remote_delivery_window_unavailable",
                    "Remote Claim 剩余 lease 不足以建立 delivery offer。",
                )
            descriptor = _build_descriptor(
                context=context,
                issued_at=now,
                expires_at=expiry,
                ack_nonce=self.random_bytes(32),
            )
            envelope = seal_post_rollback_remote_delivery_descriptor(
                descriptor,
                transport_key=context.transport_key,
                random_bytes=self.random_bytes,
                ephemeral_private_key=self.ephemeral_key_factory(),
            )
            offer = _build_offer(descriptor=descriptor, envelope=envelope)
            recorded = await self.store.record_offer(offer)
            assessed = now if explicit_time else _aware_datetime(self.clock())
            view = await self.inspect(
                delivery_id=recorded.delivery_id,
                assessed_at=assessed.isoformat(),
            )
            if view.status not in {"awaiting_ack", "delivered"}:
                raise EvolutionPostRollbackRemoteDeliveryError(
                    "post_rollback_remote_delivery_authority_changed",
                    "Remote Delivery offer 持久化期间 authority 已变化。",
                )
            return view

    async def submit_ack(
        self,
        *,
        delivery_id: str,
        worker_signature_base64: str,
        acknowledged_at: str | None = None,
    ) -> EvolutionPostRollbackRemoteDeliveryView:
        now = (
            _aware(acknowledged_at)
            if acknowledged_at is not None
            else _aware_datetime(self.clock())
        )
        lock = self._locks.setdefault(f"ack:{delivery_id}", asyncio.Lock())
        async with lock:
            offer = await self.store.get_offer(delivery_id)
            if offer is None:
                raise EvolutionPostRollbackRemoteDeliveryError(
                    "post_rollback_remote_delivery_offer_missing",
                    "Remote Delivery offer 不存在。",
                )
            await self._context(claim_id=offer.claim_id, now=now, fetch=False, offer=offer)
            await self.store.acknowledge(
                delivery_id=offer.delivery_id,
                worker_signature_base64=worker_signature_base64,
                acknowledged_at=now.isoformat(),
            )
            view = await self.inspect(
                delivery_id=offer.delivery_id,
                assessed_at=now.isoformat(),
            )
            if view.status != "delivered":
                raise EvolutionPostRollbackRemoteDeliveryError(
                    "post_rollback_remote_delivery_fenced_during_ack",
                    "Remote Delivery ACK 提交期间 authority 已变化。",
                )
            return view

    async def inspect(
        self,
        *,
        delivery_id: str,
        assessed_at: str | None = None,
    ) -> EvolutionPostRollbackRemoteDeliveryView:
        now = (
            _aware(assessed_at)
            if assessed_at is not None
            else _aware_datetime(self.clock())
        )
        offer = await self.store.get_offer(delivery_id)
        if offer is None:
            raise EvolutionPostRollbackRemoteDeliveryError(
                "post_rollback_remote_delivery_offer_missing",
                "Remote Delivery offer 不存在。",
            )
        receipt = await self.store.get_receipt(delivery_id)
        flags = {"claim": False, "transport": False, "baseline": False, "download": False}
        if now >= _aware(offer.envelope.expires_at):
            status: Literal["awaiting_ack", "delivered", "expired", "stale"] = "expired"
        else:
            try:
                context = await self._context(
                    claim_id=offer.claim_id,
                    now=now,
                    fetch=False,
                    offer=offer,
                )
                flags = {
                    "claim": True,
                    "transport": True,
                    "baseline": True,
                    "download": context.download.receipt.receipt_id
                    == offer.download_receipt_id
                    and context.download.receipt.receipt_sha256
                    == offer.download_receipt_sha256,
                }
                status = "delivered" if receipt is not None else "awaiting_ack"
            except (
                AuthenticatedWorkerIdentityError,
                AuthenticatedWorkerTransportKeyError,
                EvolutionPostRollbackRemoteClaimError,
                EvolutionPostRollbackRemoteDeliveryError,
                EvolutionPostRollbackRemoteDispatchError,
                EvolutionPostRollbackTargetBaselineError,
                ReleaseArtifactFetchError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
            ):
                status = "stale"
        delivered = status == "delivered" and all(flags.values())
        return EvolutionPostRollbackRemoteDeliveryView(
            offer=offer,
            receipt=receipt,
            status=status,
            claim_authority=flags["claim"],
            transport_key_authority=flags["transport"],
            baseline_authority=flags["baseline"],
            download_authority=flags["download"],
            transport_delivered=delivered,
        )

    async def _context(
        self,
        *,
        claim_id: str,
        now: datetime,
        fetch: bool,
        offer: EvolutionPostRollbackRemoteDeliveryOffer | None = None,
        download_source_id: str | None = None,
    ) -> _DeliveryContext:
        claim_view = await self.claim_service.inspect(
            claim_id=claim_id,
            assessed_at=now.isoformat(),
        )
        if claim_view.status != "current":
            raise EvolutionPostRollbackRemoteDeliveryError(
                "post_rollback_remote_delivery_claim_stale",
                "Remote Delivery Claim authority 已失效。",
            )
        claim = claim_view.receipt
        dispatch = await self.dispatch_store.get_by_id(claim.dispatch_id)
        if dispatch is None:
            raise EvolutionPostRollbackRemoteDeliveryError(
                "post_rollback_remote_delivery_dispatch_missing",
                "Remote Delivery Dispatch 不存在。",
            )
        dispatch_view = await self.dispatch_service.inspect(
            dispatch=dispatch,
            assessed_at=now.isoformat(),
        )
        baseline = await self.baseline_store.get_by_id(dispatch.baseline_resolution_id)
        if baseline is None:
            raise EvolutionPostRollbackRemoteDeliveryError(
                "post_rollback_remote_delivery_baseline_missing",
                "Remote Delivery Target Baseline 不存在。",
            )
        baseline_view = await self.baseline_service.inspect(baseline=baseline)
        identity = await self.identity_authority.store.get(claim.identity_id)
        if identity is None:
            raise EvolutionPostRollbackRemoteDeliveryError(
                "post_rollback_remote_delivery_identity_missing",
                "Remote Delivery Worker Identity 不存在。",
            )
        identity_view = await self.identity_authority.inspect(identity)
        transport_view = await self.transport_key_authority.resolve_for_identity(identity)
        if not (
            dispatch_view.dispatch_authority
            and baseline_view.baseline_resolution_authority
            and identity_view.identity_authority
            and transport_view is not None
            and transport_view.transport_key_authority
            and _context_lineage_matches(claim, dispatch, baseline, identity)
        ):
            raise EvolutionPostRollbackRemoteDeliveryError(
                "post_rollback_remote_delivery_lineage_stale",
                "Remote Delivery Claim/Dispatch/Baseline/Identity lineage 已失效。",
            )
        transport_key = transport_view.transport_key
        download = (
            await self.artifact_fetch_service.fetch(baseline.channel_resolution)
            if fetch
            else await self.artifact_fetch_service.inspect(
                source_id=(
                    offer.download_source_id
                    if offer is not None
                    else str(download_source_id or "")
                )
            )
        )
        if not download.verified_archive_authority:
            raise EvolutionPostRollbackRemoteDeliveryError(
                "post_rollback_remote_delivery_download_stale",
                "Remote Delivery archive download authority 已失效。",
            )
        context = _DeliveryContext(
            claim=claim,
            dispatch=dispatch,
            baseline=baseline,
            identity=identity,
            transport_key=transport_key,
            download=download,
        )
        if offer is not None and not _offer_matches_context(offer, context):
            raise EvolutionPostRollbackRemoteDeliveryError(
                "post_rollback_remote_delivery_offer_stale",
                "Remote Delivery offer 与 current authority 不一致。",
            )
        return context


@dataclass(frozen=True, slots=True)
class _DeliveryContext:
    claim: EvolutionPostRollbackRemoteClaimReceipt
    dispatch: EvolutionPostRollbackRemoteDispatch
    baseline: EvolutionPostRollbackTargetBaseline
    identity: AuthenticatedWorkerIdentity
    transport_key: AuthenticatedWorkerTransportKey
    download: ReleaseArtifactDownloadView


def _build_descriptor(
    *,
    context: _DeliveryContext,
    issued_at: datetime,
    expires_at: datetime,
    ack_nonce: bytes,
) -> EvolutionPostRollbackRemoteDeliveryDescriptor:
    if not isinstance(ack_nonce, bytes) or len(ack_nonce) != 32:
        raise ValueError("Delivery ACK nonce generator 必须返回 32 bytes。")
    claim = context.claim
    dispatch = context.dispatch
    baseline = context.baseline
    transport_key = context.transport_key
    download = context.download.receipt
    resolution = baseline.channel_resolution
    attestation = resolution.entry.build_attestation
    delivery_id = _delivery_id(
        dispatch_sha256=dispatch.dispatch_sha256,
        claim_receipt_sha256=claim.receipt_sha256,
        transport_key_sha256=transport_key.transport_key_sha256,
        baseline_resolution_sha256=baseline.baseline_resolution_sha256,
        download_receipt_sha256=download.receipt_sha256,
    )
    return EvolutionPostRollbackRemoteDeliveryDescriptor(
        delivery_id=delivery_id,
        dispatch_id=dispatch.dispatch_id,
        dispatch_sha256=dispatch.dispatch_sha256,
        claim_id=claim.claim_id,
        claim_receipt_id=claim.receipt_id,
        claim_receipt_sha256=claim.receipt_sha256,
        claim_lease_epoch=claim.lease_epoch,
        transport_key_id=transport_key.transport_key_id,
        transport_key_sha256=transport_key.transport_key_sha256,
        transport_key_generation=transport_key.key_generation,
        identity_id=context.identity.identity_id,
        identity_sha256=context.identity.identity_sha256,
        worker_id=context.identity.worker_id,
        worker_instance_id=context.identity.worker_instance_id,
        worker_epoch=context.identity.worker_epoch,
        baseline_resolution_id=baseline.baseline_resolution_id,
        baseline_resolution_sha256=baseline.baseline_resolution_sha256,
        release_target=baseline.release_target,
        channel_resolution=resolution,
        build_attestation_id=attestation.attestation_id,
        build_attestation_sha256=attestation.attestation_sha256,
        download_receipt_id=download.receipt_id,
        download_receipt_sha256=download.receipt_sha256,
        download_source_id=download.source_id,
        download_source_sha256=download.source_sha256,
        archive_name=download.resolution.entry.archive_name,
        archive_sha256=download.archive_sha256,
        archive_size_bytes=download.archive_size_bytes,
        manifest_sha256=download.resolution.entry.manifest_sha256,
        ack_nonce_base64=base64.b64encode(ack_nonce).decode("ascii"),
        issued_at=issued_at.isoformat(),
        expires_at=expires_at.isoformat(),
    )


def _build_offer(
    *,
    descriptor: EvolutionPostRollbackRemoteDeliveryDescriptor,
    envelope: EvolutionPostRollbackRemoteDeliveryEnvelope,
) -> EvolutionPostRollbackRemoteDeliveryOffer:
    ack = EvolutionPostRollbackRemoteDeliveryAckPayload(
        delivery_id=descriptor.delivery_id,
        envelope_sha256=envelope.envelope_sha256,
        descriptor_sha256=envelope.descriptor_sha256,
        ciphertext_sha256=envelope.ciphertext_sha256,
        dispatch_sha256=descriptor.dispatch_sha256,
        claim_receipt_sha256=descriptor.claim_receipt_sha256,
        transport_key_sha256=descriptor.transport_key_sha256,
        identity_id=descriptor.identity_id,
        identity_sha256=descriptor.identity_sha256,
        worker_id=descriptor.worker_id,
        worker_instance_id=descriptor.worker_instance_id,
        worker_epoch=descriptor.worker_epoch,
        archive_sha256=descriptor.archive_sha256,
        archive_size_bytes=descriptor.archive_size_bytes,
        manifest_sha256=descriptor.manifest_sha256,
        ack_nonce_base64=descriptor.ack_nonce_base64,
    )
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_POST_ROLLBACK_REMOTE_DELIVERY_POLICY,
        "delivery_id": descriptor.delivery_id,
        "dispatch_id": descriptor.dispatch_id,
        "dispatch_sha256": descriptor.dispatch_sha256,
        "claim_id": descriptor.claim_id,
        "claim_receipt_id": descriptor.claim_receipt_id,
        "claim_receipt_sha256": descriptor.claim_receipt_sha256,
        "claim_lease_epoch": descriptor.claim_lease_epoch,
        "transport_key_id": descriptor.transport_key_id,
        "transport_key_sha256": descriptor.transport_key_sha256,
        "identity_id": descriptor.identity_id,
        "identity_sha256": descriptor.identity_sha256,
        "baseline_resolution_id": descriptor.baseline_resolution_id,
        "baseline_resolution_sha256": descriptor.baseline_resolution_sha256,
        "build_attestation_sha256": descriptor.build_attestation_sha256,
        "download_receipt_id": descriptor.download_receipt_id,
        "download_receipt_sha256": descriptor.download_receipt_sha256,
        "download_source_id": descriptor.download_source_id,
        "download_source_sha256": descriptor.download_source_sha256,
        "issued_at": descriptor.issued_at,
        "envelope": envelope.model_dump(mode="json"),
        "ack_payload": ack.model_dump(mode="json"),
        "ack_signable_payload_sha256": hashlib.sha256(ack.canonical_bytes()).hexdigest(),
        "state": "awaiting_ack",
        "transport_delivered": False,
        "execution_authority": False,
    }
    digest = _digest(core)
    return EvolutionPostRollbackRemoteDeliveryOffer.model_validate(
        {
            **core,
            "offer_id": f"evpostdeliveryoffer_{digest[:24]}",
            "offer_sha256": digest,
        }
    )


def _build_receipt(
    *,
    offer: EvolutionPostRollbackRemoteDeliveryOffer,
    signature_base64: str,
    acknowledged_at: str,
) -> EvolutionPostRollbackRemoteDeliveryReceipt:
    signature = _decode_base64(
        signature_base64,
        expected=64,
        field="Delivery ACK signature",
    )
    ack = offer.ack_payload
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_POST_ROLLBACK_REMOTE_DELIVERY_POLICY,
        "delivery_id": offer.delivery_id,
        "offer_id": offer.offer_id,
        "offer_sha256": offer.offer_sha256,
        "envelope_sha256": offer.envelope.envelope_sha256,
        "descriptor_sha256": offer.envelope.descriptor_sha256,
        "dispatch_id": offer.dispatch_id,
        "dispatch_sha256": offer.dispatch_sha256,
        "claim_id": offer.claim_id,
        "claim_receipt_id": offer.claim_receipt_id,
        "claim_receipt_sha256": offer.claim_receipt_sha256,
        "claim_lease_epoch": offer.claim_lease_epoch,
        "transport_key_id": offer.transport_key_id,
        "transport_key_sha256": offer.transport_key_sha256,
        "identity_id": offer.identity_id,
        "identity_sha256": offer.identity_sha256,
        "worker_id": ack.worker_id,
        "worker_instance_id": ack.worker_instance_id,
        "worker_epoch": ack.worker_epoch,
        "baseline_resolution_id": offer.baseline_resolution_id,
        "baseline_resolution_sha256": offer.baseline_resolution_sha256,
        "build_attestation_sha256": offer.build_attestation_sha256,
        "download_receipt_sha256": offer.download_receipt_sha256,
        "download_source_id": offer.download_source_id,
        "download_source_sha256": offer.download_source_sha256,
        "archive_sha256": ack.archive_sha256,
        "archive_size_bytes": ack.archive_size_bytes,
        "manifest_sha256": ack.manifest_sha256,
        "ack_payload_sha256": offer.ack_signable_payload_sha256,
        "worker_signature_base64": base64.b64encode(signature).decode("ascii"),
        "worker_signature_sha256": hashlib.sha256(signature).hexdigest(),
        "worker_signature_verified": True,
        "acknowledged_at": _aware(acknowledged_at).isoformat(),
        "state": "delivered",
        "transport_delivered": True,
        "installation_authority": False,
        "execution_authority": False,
        "result_authority": False,
        "learning_authority": False,
        "promotion_authority": False,
    }
    digest = _digest(core)
    return EvolutionPostRollbackRemoteDeliveryReceipt.model_validate(
        {
            **core,
            "receipt_id": f"evpostdeliveryreceipt_{digest[:24]}",
            "receipt_sha256": digest,
        }
    )


def _context_lineage_matches(
    claim: EvolutionPostRollbackRemoteClaimReceipt,
    dispatch: EvolutionPostRollbackRemoteDispatch,
    baseline: EvolutionPostRollbackTargetBaseline,
    identity: AuthenticatedWorkerIdentity,
) -> bool:
    return bool(
        claim.dispatch_id == dispatch.dispatch_id
        and claim.dispatch_sha256 == dispatch.dispatch_sha256
        and claim.identity_id == identity.identity_id
        and claim.identity_sha256 == identity.identity_sha256
        and claim.worker_id == identity.worker_id == dispatch.worker_id == baseline.worker_id
        and claim.worker_instance_id
        == identity.worker_instance_id
        == dispatch.worker_instance_id
        == baseline.worker_instance_id
        and claim.worker_epoch
        == identity.worker_epoch
        == dispatch.worker_epoch
        == baseline.worker_epoch
        and dispatch.baseline_resolution_id == baseline.baseline_resolution_id
        and dispatch.baseline_resolution_sha256 == baseline.baseline_resolution_sha256
    )


def _offer_matches_context(
    offer: EvolutionPostRollbackRemoteDeliveryOffer,
    context: _DeliveryContext,
) -> bool:
    receipt = context.download.receipt
    return bool(
        offer.claim_receipt_id == context.claim.receipt_id
        and offer.claim_receipt_sha256 == context.claim.receipt_sha256
        and offer.claim_lease_epoch == context.claim.lease_epoch
        and offer.dispatch_id == context.dispatch.dispatch_id
        and offer.dispatch_sha256 == context.dispatch.dispatch_sha256
        and offer.identity_id == context.identity.identity_id
        and offer.identity_sha256 == context.identity.identity_sha256
        and offer.transport_key_id == context.transport_key.transport_key_id
        and offer.transport_key_sha256 == context.transport_key.transport_key_sha256
        and offer.baseline_resolution_id == context.baseline.baseline_resolution_id
        and offer.baseline_resolution_sha256
        == context.baseline.baseline_resolution_sha256
        and offer.build_attestation_sha256
        == context.baseline.channel_resolution.entry.build_attestation.attestation_sha256
        and offer.download_receipt_id == receipt.receipt_id
        and offer.download_receipt_sha256 == receipt.receipt_sha256
        and offer.download_source_id == receipt.source_id
        and offer.download_source_sha256 == receipt.source_sha256
    )


async def _require_atomic_offer_authority(
    db: aiosqlite.Connection,
    offer: EvolutionPostRollbackRemoteDeliveryOffer,
) -> AuthenticatedWorkerIdentity:
    dispatch_row = await (
        await db.execute(
            "SELECT dispatch_json FROM evolution_post_rollback_remote_dispatches "
            "WHERE dispatch_id = ?",
            (offer.dispatch_id,),
        )
    ).fetchone()
    claim_row = await (
        await db.execute(
            "SELECT receipt_json FROM evolution_post_rollback_remote_claim_receipts "
            "WHERE claim_id = ? ORDER BY sequence DESC LIMIT 1",
            (offer.claim_id,),
        )
    ).fetchone()
    identity_row = await (
        await db.execute(
            "SELECT identity_json FROM authenticated_worker_identities "
            "WHERE identity_id = ?",
            (offer.identity_id,),
        )
    ).fetchone()
    transport_row = await (
        await db.execute(
            "SELECT transport_key_json FROM authenticated_worker_transport_keys "
            "WHERE identity_id = ? ORDER BY key_generation DESC LIMIT 1",
            (offer.identity_id,),
        )
    ).fetchone()
    baseline_row = await (
        await db.execute(
            "SELECT baseline_json FROM evolution_post_rollback_target_baselines "
            "WHERE baseline_resolution_id = ?",
            (offer.baseline_resolution_id,),
        )
    ).fetchone()
    if any(
        row is None
        for row in (dispatch_row, claim_row, identity_row, transport_row, baseline_row)
    ):
        raise EvolutionPostRollbackRemoteDeliveryError(
            "post_rollback_remote_delivery_atomic_authority_missing",
            "Remote Delivery atomic authority dependency 缺失。",
        )
    try:
        dispatch = EvolutionPostRollbackRemoteDispatch.model_validate_json(
            dispatch_row["dispatch_json"]
        )
        claim = EvolutionPostRollbackRemoteClaimReceipt.model_validate_json(
            claim_row["receipt_json"]
        )
        identity = AuthenticatedWorkerIdentity.model_validate_json(
            identity_row["identity_json"]
        )
        transport = AuthenticatedWorkerTransportKey.model_validate_json(
            transport_row["transport_key_json"]
        )
        baseline = EvolutionPostRollbackTargetBaseline.model_validate_json(
            baseline_row["baseline_json"]
        )
    except ValueError as exc:
        raise EvolutionPostRollbackRemoteDeliveryError(
            "post_rollback_remote_delivery_atomic_authority_corrupt",
            "Remote Delivery atomic authority dependency 损坏。",
        ) from exc
    ack = offer.ack_payload
    build = baseline.channel_resolution.entry.build_attestation
    if not (
        _context_lineage_matches(claim, dispatch, baseline, identity)
        and offer.claim_receipt_id == claim.receipt_id
        and offer.claim_receipt_sha256 == claim.receipt_sha256
        and offer.claim_lease_epoch == claim.lease_epoch
        and offer.transport_key_id == transport.transport_key_id
        and offer.transport_key_sha256 == transport.transport_key_sha256
        and transport.identity_id == identity.identity_id
        and transport.identity_sha256 == identity.identity_sha256
        and offer.baseline_resolution_sha256 == baseline.baseline_resolution_sha256
        and offer.build_attestation_sha256 == build.attestation_sha256
        and ack.archive_sha256 == baseline.channel_resolution.entry.archive_sha256
        and ack.archive_size_bytes == baseline.channel_resolution.entry.archive_size_bytes
        and ack.manifest_sha256 == baseline.channel_resolution.entry.manifest_sha256
        and _aware(offer.envelope.expires_at) <= _aware(claim.lease_expires_at)
    ):
        raise EvolutionPostRollbackRemoteDeliveryError(
            "post_rollback_remote_delivery_atomic_authority_mismatch",
            "Remote Delivery atomic authority dependency 已变化。",
        )
    return identity


def _same_logical_offer(
    left: EvolutionPostRollbackRemoteDeliveryOffer,
    right: EvolutionPostRollbackRemoteDeliveryOffer,
) -> bool:
    return bool(
        left.delivery_id == right.delivery_id
        and left.dispatch_sha256 == right.dispatch_sha256
        and left.claim_receipt_sha256 == right.claim_receipt_sha256
        and left.transport_key_sha256 == right.transport_key_sha256
        and left.baseline_resolution_sha256 == right.baseline_resolution_sha256
        and left.download_receipt_sha256 == right.download_receipt_sha256
    )


def seal_post_rollback_remote_delivery_descriptor(
    descriptor: EvolutionPostRollbackRemoteDeliveryDescriptor,
    *,
    transport_key: AuthenticatedWorkerTransportKey,
    random_bytes: Callable[[int], bytes] = os.urandom,
    ephemeral_private_key: X25519PrivateKey | None = None,
) -> EvolutionPostRollbackRemoteDeliveryEnvelope:
    item = EvolutionPostRollbackRemoteDeliveryDescriptor.model_validate_json(
        descriptor.model_dump_json()
    )
    key = AuthenticatedWorkerTransportKey.model_validate_json(
        transport_key.model_dump_json()
    )
    if not (
        item.transport_key_id == key.transport_key_id
        and item.transport_key_sha256 == key.transport_key_sha256
        and item.transport_key_generation == key.key_generation
        and item.identity_id == key.identity_id
        and item.identity_sha256 == key.identity_sha256
    ):
        raise ValueError("Remote Delivery descriptor 未绑定 exact Transport Key。")
    private = ephemeral_private_key or X25519PrivateKey.generate()
    ephemeral_raw = private.public_key().public_bytes_raw()
    receiver = X25519PublicKey.from_public_bytes(
        _decode_base64(key.public_key_base64, expected=32, field="Transport public key")
    )
    shared = private.exchange(receiver)
    salt = random_bytes(32)
    nonce = random_bytes(12)
    if not isinstance(salt, bytes) or len(salt) != 32:
        raise ValueError("Delivery HKDF salt generator 必须返回 32 bytes。")
    if not isinstance(nonce, bytes) or len(nonce) != 12:
        raise ValueError("Delivery nonce generator 必须返回 12 bytes。")
    descriptor_bytes = item.canonical_bytes()
    if len(descriptor_bytes) > _MAX_DESCRIPTOR_BYTES:
        raise ValueError("Remote Delivery descriptor 超过 2 MiB。")
    descriptor_sha = hashlib.sha256(descriptor_bytes).hexdigest()
    base = {
        "schema_version": 1,
        "policy_version": EVOLUTION_POST_ROLLBACK_REMOTE_DELIVERY_POLICY,
        "delivery_id": item.delivery_id,
        "descriptor_sha256": descriptor_sha,
        "dispatch_sha256": item.dispatch_sha256,
        "claim_receipt_sha256": item.claim_receipt_sha256,
        "transport_key_id": item.transport_key_id,
        "transport_key_sha256": item.transport_key_sha256,
        "archive_sha256": item.archive_sha256,
        "manifest_sha256": item.manifest_sha256,
        "expires_at": item.expires_at,
        "key_agreement_algorithm": "x25519",
        "key_derivation_algorithm": "hkdf-sha256",
        "payload_algorithm": "aes-256-gcm",
        "ephemeral_public_key_base64": base64.b64encode(ephemeral_raw).decode("ascii"),
        "ephemeral_public_key_sha256": hashlib.sha256(ephemeral_raw).hexdigest(),
        "hkdf_salt_base64": base64.b64encode(salt).decode("ascii"),
        "nonce_base64": base64.b64encode(nonce).decode("ascii"),
        "plaintext_bytes": len(descriptor_bytes),
        "private_key_stored": False,
        "transport_delivered": False,
        "execution_authority": False,
    }
    provisional = EvolutionPostRollbackRemoteDeliveryEnvelope.model_construct(
        **base,
        envelope_sha256="0" * 64,
        ciphertext_base64=base64.b64encode(b"x" * (len(descriptor_bytes) + 16)).decode(),
        ciphertext_sha256="0" * 64,
        aad_sha256="0" * 64,
    )
    aad = _delivery_aad(provisional)
    aes_key = _derive_delivery_key(
        shared,
        salt=salt,
        delivery_id=item.delivery_id,
        transport_key_sha256=item.transport_key_sha256,
        ephemeral_public_key_sha256=base["ephemeral_public_key_sha256"],
    )
    ciphertext = AESGCM(aes_key).encrypt(nonce, descriptor_bytes, aad)
    core = {
        **base,
        "ciphertext_base64": base64.b64encode(ciphertext).decode("ascii"),
        "ciphertext_sha256": hashlib.sha256(ciphertext).hexdigest(),
        "aad_sha256": hashlib.sha256(aad).hexdigest(),
    }
    return EvolutionPostRollbackRemoteDeliveryEnvelope.model_validate(
        {**core, "envelope_sha256": _digest(core)}
    )


def open_post_rollback_remote_delivery_envelope(
    envelope: EvolutionPostRollbackRemoteDeliveryEnvelope,
    *,
    transport_private_key: X25519PrivateKey,
) -> EvolutionPostRollbackRemoteDeliveryDescriptor:
    item = EvolutionPostRollbackRemoteDeliveryEnvelope.model_validate_json(
        envelope.model_dump_json()
    )
    if not isinstance(transport_private_key, X25519PrivateKey):
        raise TypeError("transport_private_key 必须是 X25519 private key。")
    ephemeral = X25519PublicKey.from_public_bytes(
        _decode_base64(
            item.ephemeral_public_key_base64,
            expected=32,
            field="Delivery ephemeral public key",
        )
    )
    shared = transport_private_key.exchange(ephemeral)
    salt = _decode_base64(item.hkdf_salt_base64, expected=32, field="Delivery HKDF salt")
    nonce = _decode_base64(item.nonce_base64, expected=12, field="Delivery nonce")
    ciphertext = _decode_base64(
        item.ciphertext_base64,
        expected=item.plaintext_bytes + 16,
        field="Delivery ciphertext",
    )
    key = _derive_delivery_key(
        shared,
        salt=salt,
        delivery_id=item.delivery_id,
        transport_key_sha256=item.transport_key_sha256,
        ephemeral_public_key_sha256=item.ephemeral_public_key_sha256,
    )
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, _delivery_aad(item))
    except InvalidTag as exc:
        raise EvolutionPostRollbackRemoteDeliveryError(
            "post_rollback_remote_delivery_decrypt_failed",
            "Remote Delivery envelope 无法认证或解密。",
        ) from exc
    try:
        descriptor = EvolutionPostRollbackRemoteDeliveryDescriptor.model_validate_json(
            plaintext
        )
    except ValueError as exc:
        raise EvolutionPostRollbackRemoteDeliveryError(
            "post_rollback_remote_delivery_descriptor_invalid",
            "Remote Delivery descriptor 无效。",
        ) from exc
    if not (
        hmac.compare_digest(item.descriptor_sha256, hashlib.sha256(plaintext).hexdigest())
        and descriptor.delivery_id == item.delivery_id
        and descriptor.dispatch_sha256 == item.dispatch_sha256
        and descriptor.claim_receipt_sha256 == item.claim_receipt_sha256
        and descriptor.transport_key_sha256 == item.transport_key_sha256
        and descriptor.archive_sha256 == item.archive_sha256
        and descriptor.manifest_sha256 == item.manifest_sha256
        and descriptor.expires_at == item.expires_at
    ):
        raise EvolutionPostRollbackRemoteDeliveryError(
            "post_rollback_remote_delivery_descriptor_mismatch",
            "Remote Delivery descriptor 与 envelope fence 不一致。",
        )
    return descriptor


def _derive_delivery_key(
    shared_secret: bytes,
    *,
    salt: bytes,
    delivery_id: str,
    transport_key_sha256: str,
    ephemeral_public_key_sha256: str,
) -> bytes:
    info = _canonical(
        {
            "domain": "naumi.evolution.post-rollback-remote-delivery-key.v1",
            "delivery_id": delivery_id,
            "transport_key_sha256": transport_key_sha256,
            "ephemeral_public_key_sha256": ephemeral_public_key_sha256,
        }
    )
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=info,
    ).derive(shared_secret)


def _delivery_aad(envelope: EvolutionPostRollbackRemoteDeliveryEnvelope) -> bytes:
    return _canonical(
        {
            "domain": "naumi.evolution.post-rollback-remote-delivery-aad.v1",
            "delivery_id": envelope.delivery_id,
            "descriptor_sha256": envelope.descriptor_sha256,
            "dispatch_sha256": envelope.dispatch_sha256,
            "claim_receipt_sha256": envelope.claim_receipt_sha256,
            "transport_key_id": envelope.transport_key_id,
            "transport_key_sha256": envelope.transport_key_sha256,
            "archive_sha256": envelope.archive_sha256,
            "manifest_sha256": envelope.manifest_sha256,
            "expires_at": envelope.expires_at,
            "ephemeral_public_key_sha256": envelope.ephemeral_public_key_sha256,
            "plaintext_bytes": envelope.plaintext_bytes,
        }
    )


def _delivery_id(
    *,
    dispatch_sha256: str,
    claim_receipt_sha256: str,
    transport_key_sha256: str,
    baseline_resolution_sha256: str,
    download_receipt_sha256: str,
) -> str:
    digest = _digest(
        {
            "domain": EVOLUTION_POST_ROLLBACK_REMOTE_DELIVERY_DOMAIN,
            "dispatch_sha256": dispatch_sha256,
            "claim_receipt_sha256": claim_receipt_sha256,
            "transport_key_sha256": transport_key_sha256,
            "baseline_resolution_sha256": baseline_resolution_sha256,
            "download_receipt_sha256": download_receipt_sha256,
        }
    )
    return f"evpostdelivery_{digest[:24]}"


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS evolution_post_rollback_remote_delivery_offers (
            delivery_id TEXT PRIMARY KEY,
            offer_id TEXT NOT NULL UNIQUE,
            offer_sha256 TEXT NOT NULL UNIQUE,
            claim_id TEXT NOT NULL,
            claim_receipt_id TEXT NOT NULL UNIQUE,
            transport_key_id TEXT NOT NULL,
            baseline_resolution_id TEXT NOT NULL,
            offer_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('awaiting_ack', 'delivered')),
            expires_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS evolution_post_rollback_remote_delivery_receipts (
            receipt_id TEXT PRIMARY KEY,
            receipt_sha256 TEXT NOT NULL UNIQUE,
            delivery_id TEXT NOT NULL UNIQUE,
            offer_id TEXT NOT NULL UNIQUE,
            receipt_json TEXT NOT NULL,
            acknowledged_at TEXT NOT NULL
        );
        """
    )
    await db.commit()


def _validate_offer(
    value: EvolutionPostRollbackRemoteDeliveryOffer,
) -> EvolutionPostRollbackRemoteDeliveryOffer:
    try:
        return EvolutionPostRollbackRemoteDeliveryOffer.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionPostRollbackRemoteDeliveryError(
            "post_rollback_remote_delivery_offer_invalid",
            "Remote Delivery offer artifact 无效。",
        ) from exc


def _restore_offer(encoded: str) -> EvolutionPostRollbackRemoteDeliveryOffer:
    try:
        if len(encoded.encode("utf-8")) > _MAX_OFFER_BYTES:
            raise ValueError("oversized")
        return EvolutionPostRollbackRemoteDeliveryOffer.model_validate_json(encoded)
    except (TypeError, ValueError) as exc:
        raise EvolutionPostRollbackRemoteDeliveryError(
            "post_rollback_remote_delivery_store_corrupt",
            "Remote Delivery offer 无法验证。",
        ) from exc


def _restore_receipt(encoded: str) -> EvolutionPostRollbackRemoteDeliveryReceipt:
    try:
        if len(encoded.encode("utf-8")) > _MAX_OFFER_BYTES:
            raise ValueError("oversized")
        return EvolutionPostRollbackRemoteDeliveryReceipt.model_validate_json(encoded)
    except (TypeError, ValueError) as exc:
        raise EvolutionPostRollbackRemoteDeliveryError(
            "post_rollback_remote_delivery_store_corrupt",
            "Remote Delivery receipt 无法验证。",
        ) from exc


def _delivery_id_value(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized.startswith("evpostdelivery_") or len(normalized) != 39:
        raise EvolutionPostRollbackRemoteDeliveryError(
            "post_rollback_remote_delivery_id_invalid",
            "Remote Delivery ID 格式无效。",
        )
    suffix = normalized.removeprefix("evpostdelivery_")
    if len(suffix) != 24 or any(char not in "0123456789abcdef" for char in suffix):
        raise EvolutionPostRollbackRemoteDeliveryError(
            "post_rollback_remote_delivery_id_invalid",
            "Remote Delivery ID 格式无效。",
        )
    return normalized


def render_post_rollback_remote_delivery_offer(
    view: EvolutionPostRollbackRemoteDeliveryView,
) -> str:
    offer = view.offer
    if view.receipt is not None:
        return render_post_rollback_remote_delivery(view)
    return "\n".join(
        [
            "## 回滚后远端 Baseline 加密交付",
            "",
            f"- 状态：`{view.status}`",
            f"- Delivery：`{offer.delivery_id}`",
            f"- Envelope：`{offer.envelope.envelope_sha256}`",
            f"- Archive：`{offer.ack_payload.archive_sha256}` · "
            f"{offer.ack_payload.archive_size_bytes} bytes",
            f"- Manifest：`{offer.ack_payload.manifest_sha256}`",
            f"- Offer expiry：`{offer.envelope.expires_at}`",
            f"- ACK signable digest：`{offer.ack_signable_payload_sha256}`",
            "- 权威边界：descriptor 已加密；尚未收到 Worker ACK，无执行权。",
            "",
            "### Worker 传输对象（canonical JSON）",
            "```json",
            offer.model_dump_json(),
            "```",
        ]
    )


def render_post_rollback_remote_delivery(
    view: EvolutionPostRollbackRemoteDeliveryView,
) -> str:
    receipt = view.receipt
    if receipt is None:
        return render_post_rollback_remote_delivery_offer(view)
    return "\n".join(
        [
            "## 回滚后远端 Baseline 交付回执",
            "",
            f"- 状态：`{view.status}`",
            f"- Delivery：`{receipt.delivery_id}`",
            f"- Receipt：`{receipt.receipt_id}`",
            f"- Worker：`{receipt.worker_id}` epoch {receipt.worker_epoch}",
            f"- Archive：`{receipt.archive_sha256}` · {receipt.archive_size_bytes} bytes",
            f"- Manifest：`{receipt.manifest_sha256}`",
            f"- ACK：Ed25519 verified · `{receipt.ack_payload_sha256}`",
            f"- 确认时间：`{receipt.acknowledged_at}`",
            "- 权威边界：baseline transport 已确认；未安装、未执行、无结果权。",
        ]
    )


def _decode_base64(value: str, *, expected: int, field: str) -> bytes:
    try:
        raw = base64.b64decode(value, validate=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} Base64 无效。") from exc
    if len(raw) != expected or not hmac.compare_digest(
        value,
        base64.b64encode(raw).decode("ascii"),
    ):
        raise ValueError(f"{field} 长度或 canonical 编码无效。")
    return raw


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Remote Delivery 时间必须包含时区。")
    return parsed.astimezone(UTC)


def _aware_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("Remote Delivery clock 必须返回 datetime。")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Remote Delivery clock 必须返回带时区时间。")
    return value.astimezone(UTC)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


__all__ = [
    "EVOLUTION_POST_ROLLBACK_REMOTE_DELIVERY_POLICY",
    "EvolutionPostRollbackRemoteDeliveryAckPayload",
    "EvolutionPostRollbackRemoteDeliveryDescriptor",
    "EvolutionPostRollbackRemoteDeliveryEnvelope",
    "EvolutionPostRollbackRemoteDeliveryError",
    "EvolutionPostRollbackRemoteDeliveryOffer",
    "EvolutionPostRollbackRemoteDeliveryReceipt",
    "EvolutionPostRollbackRemoteDeliveryService",
    "EvolutionPostRollbackRemoteDeliveryStore",
    "EvolutionPostRollbackRemoteDeliveryView",
    "open_post_rollback_remote_delivery_envelope",
    "render_post_rollback_remote_delivery",
    "render_post_rollback_remote_delivery_offer",
    "seal_post_rollback_remote_delivery_descriptor",
]
