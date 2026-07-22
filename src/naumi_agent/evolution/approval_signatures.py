"""Domain-separated Ed25519 signature challenges and verified receipts."""

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
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self
from weakref import WeakValueDictionary

import aiosqlite
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.approval_principals import (
    EvolutionApprovalPrincipalError,
    EvolutionApprovalPrincipalEvent,
    EvolutionApprovalPrincipalService,
    EvolutionApprovalPrincipalState,
)
from naumi_agent.evolution.approval_requests import (
    EvolutionPromotionApprovalRequestError,
    EvolutionPromotionApprovalResponse,
    EvolutionPromotionApprovalResponseReceipt,
    EvolutionPromotionApprovalResponseStore,
)
from naumi_agent.evolution.approval_requirements import (
    EvolutionPromotionApprovalRequirement,
    EvolutionPromotionApprovalRequirementError,
    EvolutionPromotionApprovalRequirementExecutor,
    EvolutionPromotionApprovalRequirementView,
    EvolutionPromotionApprovalRole,
)

EVOLUTION_APPROVAL_SIGNATURE_POLICY = "evolution-approval-signature-v1"
EVOLUTION_APPROVAL_SIGNATURE_DOMAIN = "naumi.evolution.approval-signature.v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_ARTIFACT_BYTES = 256 * 1_024
_DEFAULT_CHALLENGE_TTL_SECONDS = 900


class EvolutionApprovalSignatureChallengeStatus(StrEnum):
    PENDING = "pending"
    EXPIRED = "expired"
    CONSUMED = "consumed"
    SUPERSEDED = "superseded"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionApprovalSignaturePayload(_StrictModel):
    schema_version: Literal[1] = 1
    domain: Literal["naumi.evolution.approval-signature.v1"] = (
        EVOLUTION_APPROVAL_SIGNATURE_DOMAIN
    )
    workspace_root: str = Field(min_length=1, max_length=4_096)
    approval_response_id: str = Field(pattern=r"^evapprovalresp_[0-9a-f]{24}$")
    approval_response_sha256: str = Field(pattern=_SHA256_RE)
    approval_request_id: str = Field(pattern=r"^evapprovalrequest_[0-9a-f]{24}$")
    requirement_id: str = Field(pattern=r"^evapprovalreq_[0-9a-f]{24}$")
    requirement_sha256: str = Field(pattern=_SHA256_RE)
    package_id: str = Field(pattern=r"^evpromopkg_[0-9a-f]{24}$")
    package_sha256: str = Field(pattern=_SHA256_RE)
    role: EvolutionPromotionApprovalRole
    response: Literal["approve"] = "approve"
    source_signable_payload_sha256: str = Field(pattern=_SHA256_RE)
    principal_id: str = Field(pattern=r"^evprincipal_[0-9a-f]{24}$")
    principal_event_id: str = Field(pattern=r"^evprincipalevent_[0-9a-f]{24}$")
    principal_event_sha256: str = Field(pattern=_SHA256_RE)
    key_id: str = Field(pattern=r"^evapprovalkey_[0-9a-f]{24}$")
    key_generation: int = Field(ge=1, le=10_000)
    public_key_sha256: str = Field(pattern=_SHA256_RE)
    nonce_base64: str = Field(min_length=44, max_length=44)
    issued_at: str = Field(min_length=1, max_length=100)
    expires_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _payload_is_exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Signature payload workspace 必须是 canonical 路径。")
        nonce = _decode_base64(self.nonce_base64, expected_bytes=32, label="nonce")
        if not hmac.compare_digest(base64.b64encode(nonce).decode("ascii"), self.nonce_base64):
            raise ValueError("Signature nonce 必须使用 canonical Base64。")
        issued = _aware(self.issued_at)
        expires = _aware(self.expires_at)
        if expires <= issued or expires - issued > timedelta(seconds=3_600):
            raise ValueError("Signature challenge 有效期无效。")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.model_dump(mode="json"))


class EvolutionApprovalSignatureChallenge(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-approval-signature-v1"] = (
        EVOLUTION_APPROVAL_SIGNATURE_POLICY
    )
    challenge_id: str = Field(pattern=r"^evsigchallenge_[0-9a-f]{24}$")
    challenge_sha256: str = Field(pattern=_SHA256_RE)
    attempt: int = Field(ge=1, le=10_000)
    payload: EvolutionApprovalSignaturePayload
    signable_payload_base64: str = Field(min_length=4, max_length=32_768)
    signable_payload_sha256: str = Field(pattern=_SHA256_RE)
    signature_algorithm: Literal["ed25519"] = "ed25519"
    signature_collected: Literal[False] = False
    private_key_requested: Literal[False] = False
    private_key_stored: Literal[False] = False
    overall_approval_decided: Literal[False] = False
    promotion_authority: Literal[False] = False
    git_write_executed: Literal[False] = False
    llm_generated: Literal[False] = False

    @model_validator(mode="after")
    def _challenge_is_exact(self) -> Self:
        signable = self.payload.canonical_bytes()
        expected_base64 = base64.b64encode(signable).decode("ascii")
        if not hmac.compare_digest(self.signable_payload_base64, expected_base64):
            raise ValueError("Signature Challenge signable bytes 不一致。")
        if not hmac.compare_digest(
            self.signable_payload_sha256,
            hashlib.sha256(signable).hexdigest(),
        ):
            raise ValueError("Signature Challenge payload 摘要不一致。")
        core = self.model_dump(
            mode="json",
            exclude={"challenge_id", "challenge_sha256"},
        )
        digest = _sha256_payload(core)
        if not hmac.compare_digest(self.challenge_sha256, digest):
            raise ValueError("Signature Challenge artifact 摘要不一致。")
        if self.challenge_id != f"evsigchallenge_{digest[:24]}":
            raise ValueError("Signature Challenge identity 不一致。")
        return self


class EvolutionApprovalSignatureChallengeView(_StrictModel):
    challenge: EvolutionApprovalSignatureChallenge
    status: EvolutionApprovalSignatureChallengeStatus
    eligible_for_submission: bool
    closed_by_receipt_id: str = Field(
        default="",
        pattern=r"^(?:|evsigreceipt_[0-9a-f]{24})$",
    )

    @model_validator(mode="after")
    def _view_is_exact(self) -> Self:
        expected = self.status is EvolutionApprovalSignatureChallengeStatus.PENDING
        if self.eligible_for_submission is not expected:
            raise ValueError("Signature Challenge eligibility 投影不一致。")
        if self.status is EvolutionApprovalSignatureChallengeStatus.CONSUMED:
            if not self.closed_by_receipt_id:
                raise ValueError("Consumed Challenge 必须绑定 Receipt。")
        elif self.closed_by_receipt_id:
            raise ValueError("未消费 Challenge 不能绑定 Receipt。")
        return self


class EvolutionApprovalSignatureReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-approval-signature-v1"] = (
        EVOLUTION_APPROVAL_SIGNATURE_POLICY
    )
    receipt_id: str = Field(pattern=r"^evsigreceipt_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    challenge: EvolutionApprovalSignatureChallenge
    principal: EvolutionApprovalPrincipalEvent
    signature_algorithm: Literal["ed25519"] = "ed25519"
    signature_base64: str = Field(min_length=88, max_length=88)
    signature_sha256: str = Field(pattern=_SHA256_RE)
    verified_at: str = Field(min_length=1, max_length=100)
    signature_verified: Literal[True] = True
    identity_binding_verified: Literal[True] = True
    principal_key_current_at_verification: Literal[True] = True
    role_binding_verified: Literal[True] = True
    counts_toward_role_quorum: Literal[True] = True
    overall_approval_decided: Literal[False] = False
    final_quorum_reached: Literal[False] = False
    promotion_authority: Literal[False] = False
    promotion_executed: Literal[False] = False
    git_write_executed: Literal[False] = False
    private_key_requested: Literal[False] = False
    private_key_stored: Literal[False] = False
    llm_generated: Literal[False] = False

    @model_validator(mode="after")
    def _receipt_is_exact(self) -> Self:
        payload = self.challenge.payload
        if not (
            self.principal.workspace_root == payload.workspace_root
            and self.principal.principal_id == payload.principal_id
            and self.principal.event_id == payload.principal_event_id
            and self.principal.event_sha256 == payload.principal_event_sha256
            and self.principal.key_id == payload.key_id
            and self.principal.key_generation == payload.key_generation
            and self.principal.public_key_sha256 == payload.public_key_sha256
            and self.principal.state is EvolutionApprovalPrincipalState.ACTIVE
            and payload.role in self.principal.roles
        ):
            raise ValueError("Signature Receipt 未绑定 exact active Principal authority。")
        signature = _decode_base64(
            self.signature_base64,
            expected_bytes=64,
            label="signature",
        )
        if not hmac.compare_digest(
            base64.b64encode(signature).decode("ascii"),
            self.signature_base64,
        ):
            raise ValueError("Ed25519 signature 必须使用 canonical Base64。")
        if not hmac.compare_digest(
            self.signature_sha256,
            hashlib.sha256(signature).hexdigest(),
        ):
            raise ValueError("Signature Receipt signature 摘要不一致。")
        try:
            public_key = _decode_base64(
                self.principal.public_key_base64,
                expected_bytes=32,
                label="public key",
            )
            Ed25519PublicKey.from_public_bytes(public_key).verify(
                signature,
                self.challenge.payload.canonical_bytes(),
            )
        except (InvalidSignature, ValueError) as exc:
            raise ValueError("Signature Receipt 密码学验证失败。") from exc
        verified = _aware(self.verified_at)
        if not (_aware(payload.issued_at) <= verified < _aware(payload.expires_at)):
            raise ValueError("Signature Receipt verified_at 不在 Challenge 有效期内。")
        core = self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        digest = _sha256_payload(core)
        if not hmac.compare_digest(self.receipt_sha256, digest):
            raise ValueError("Signature Receipt artifact 摘要不一致。")
        if self.receipt_id != f"evsigreceipt_{digest[:24]}":
            raise ValueError("Signature Receipt identity 不一致。")
        return self


class EvolutionApprovalSignatureReceiptView(_StrictModel):
    receipt: EvolutionApprovalSignatureReceipt
    requirement_current: bool
    target_current: bool
    requirement_expired: bool
    response_current: bool
    principal_active: bool
    principal_key_current: bool
    role_binding_current: bool
    eligible_for_future_aggregation: bool

    @model_validator(mode="after")
    def _view_is_exact(self) -> Self:
        expected = bool(
            self.requirement_current
            and self.target_current
            and not self.requirement_expired
            and self.response_current
            and self.principal_active
            and self.principal_key_current
            and self.role_binding_current
        )
        if self.eligible_for_future_aggregation is not expected:
            raise ValueError("Signature Receipt aggregation eligibility 投影不一致。")
        return self


class EvolutionApprovalSignatureError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionApprovalSignatureBuilder:
    def __init__(self, *, nonce_factory: Callable[[], bytes] | None = None) -> None:
        self._nonce_factory = nonce_factory or (lambda: secrets.token_bytes(32))

    def build_challenge(
        self,
        *,
        response: EvolutionPromotionApprovalResponseReceipt,
        requirement: EvolutionPromotionApprovalRequirement,
        principal: EvolutionApprovalPrincipalEvent,
        attempt: int,
        issued_at: datetime,
        expires_at: datetime,
    ) -> EvolutionApprovalSignatureChallenge:
        try:
            source_response = EvolutionPromotionApprovalResponseReceipt.model_validate_json(
                response.model_dump_json()
            )
            source_requirement = EvolutionPromotionApprovalRequirement.model_validate_json(
                requirement.model_dump_json()
            )
            source_principal = EvolutionApprovalPrincipalEvent.model_validate_json(
                principal.model_dump_json()
            )
            _validate_sources(source_response, source_requirement, source_principal)
            if issued_at.utcoffset() is None or expires_at.utcoffset() is None:
                raise ValueError("Signature Challenge clock 必须包含时区。")
            nonce = self._nonce_factory()
            if not isinstance(nonce, bytes) or len(nonce) != 32:
                raise ValueError("Signature Challenge nonce factory 必须返回 32 bytes。")
            payload = EvolutionApprovalSignaturePayload(
                workspace_root=source_response.workspace_root,
                approval_response_id=source_response.receipt_id,
                approval_response_sha256=source_response.receipt_sha256,
                approval_request_id=source_response.approval_request_id,
                requirement_id=source_requirement.requirement_id,
                requirement_sha256=source_requirement.requirement_sha256,
                package_id=source_requirement.package_id,
                package_sha256=source_requirement.package_sha256,
                role=source_response.role,
                source_signable_payload_sha256=(
                    source_response.signature_entry.signable_payload_sha256
                ),
                principal_id=source_principal.principal_id,
                principal_event_id=source_principal.event_id,
                principal_event_sha256=source_principal.event_sha256,
                key_id=source_principal.key_id,
                key_generation=source_principal.key_generation,
                public_key_sha256=source_principal.public_key_sha256,
                nonce_base64=base64.b64encode(nonce).decode("ascii"),
                issued_at=issued_at.astimezone(UTC).isoformat(),
                expires_at=expires_at.astimezone(UTC).isoformat(),
            )
        except EvolutionApprovalSignatureError:
            raise
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionApprovalSignatureError(
                "approval_signature_challenge_input_invalid",
                "Approval Signature Challenge 输入无效。",
            ) from exc
        signable = payload.canonical_bytes()
        core = {
            "schema_version": 1,
            "policy_version": EVOLUTION_APPROVAL_SIGNATURE_POLICY,
            "attempt": attempt,
            "payload": payload.model_dump(mode="json"),
            "signable_payload_base64": base64.b64encode(signable).decode("ascii"),
            "signable_payload_sha256": hashlib.sha256(signable).hexdigest(),
            "signature_algorithm": "ed25519",
            "signature_collected": False,
            "private_key_requested": False,
            "private_key_stored": False,
            "overall_approval_decided": False,
            "promotion_authority": False,
            "git_write_executed": False,
            "llm_generated": False,
        }
        digest = _sha256_payload(core)
        try:
            return EvolutionApprovalSignatureChallenge.model_validate(
                {
                    **core,
                    "challenge_id": f"evsigchallenge_{digest[:24]}",
                    "challenge_sha256": digest,
                }
            )
        except ValueError as exc:
            raise EvolutionApprovalSignatureError(
                "approval_signature_challenge_invalid",
                "Approval Signature Challenge 无法验证。",
            ) from exc

    def build_receipt(
        self,
        *,
        challenge: EvolutionApprovalSignatureChallenge,
        principal: EvolutionApprovalPrincipalEvent,
        signature_base64: str,
        verified_at: datetime,
    ) -> EvolutionApprovalSignatureReceipt:
        try:
            source = EvolutionApprovalSignatureChallenge.model_validate_json(
                challenge.model_dump_json()
            )
            signer = EvolutionApprovalPrincipalEvent.model_validate_json(
                principal.model_dump_json()
            )
            signature = _normalize_signature(signature_base64)
            if verified_at.utcoffset() is None:
                raise ValueError("Signature verification clock 必须包含时区。")
            _verify_signature(source, signer, signature)
        except EvolutionApprovalSignatureError:
            raise
        except (AttributeError, InvalidSignature, TypeError, ValueError) as exc:
            raise EvolutionApprovalSignatureError(
                "approval_signature_invalid",
                "Ed25519 signature 无效或未绑定 exact Challenge。",
            ) from exc
        core = {
            "schema_version": 1,
            "policy_version": EVOLUTION_APPROVAL_SIGNATURE_POLICY,
            # Keep the already-validated objects here.  Principal embeds the
            # strict HarnessInteractionRecord model whose tuple fields must not
            # be coerced back from JSON lists during Python-mode validation.
            "challenge": source,
            "principal": signer,
            "signature_algorithm": "ed25519",
            "signature_base64": signature,
            "signature_sha256": hashlib.sha256(
                base64.b64decode(signature, validate=True)
            ).hexdigest(),
            "verified_at": verified_at.astimezone(UTC).isoformat(),
            "signature_verified": True,
            "identity_binding_verified": True,
            "principal_key_current_at_verification": True,
            "role_binding_verified": True,
            "counts_toward_role_quorum": True,
            "overall_approval_decided": False,
            "final_quorum_reached": False,
            "promotion_authority": False,
            "promotion_executed": False,
            "git_write_executed": False,
            "private_key_requested": False,
            "private_key_stored": False,
            "llm_generated": False,
        }
        digest = _sha256_payload(
            {
                **core,
                "challenge": source.model_dump(mode="json"),
                "principal": signer.model_dump(mode="json"),
            }
        )
        try:
            return EvolutionApprovalSignatureReceipt.model_validate(
                {
                    **core,
                    "receipt_id": f"evsigreceipt_{digest[:24]}",
                    "receipt_sha256": digest,
                }
            )
        except ValueError as exc:
            raise EvolutionApprovalSignatureError(
                "approval_signature_receipt_invalid",
                "Approval Signature Receipt 无法验证。",
            ) from exc


class EvolutionApprovalSignatureStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()

    async def record_challenge(
        self,
        challenge: EvolutionApprovalSignatureChallenge,
        *,
        response: EvolutionPromotionApprovalResponseReceipt,
        requirement: EvolutionPromotionApprovalRequirement,
        principal: EvolutionApprovalPrincipalEvent,
        now: datetime,
    ) -> EvolutionApprovalSignatureChallenge:
        item = _validated_challenge(challenge)
        _validate_sources(response, requirement, principal)
        _require_challenge_sources(item, response, requirement, principal)
        timestamp = _aware_datetime(now, "Signature Challenge store clock")
        encoded = item.model_dump_json()
        _require_bounded(encoded, "Signature Challenge")
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                await _assert_current_authorities(
                    db,
                    response=response,
                    requirement=requirement,
                    principal=principal,
                )
                existing_row = await (
                    await db.execute(
                        "SELECT * FROM evolution_approval_signature_challenges "
                        "WHERE approval_response_id = ? AND principal_id = ? "
                        "AND closed_reason = '' ORDER BY attempt DESC LIMIT 1",
                        (item.payload.approval_response_id, item.payload.principal_id),
                    )
                ).fetchone()
                if existing_row is not None:
                    existing = _challenge_from_row(existing_row)
                    if (
                        timestamp < _aware(existing.payload.expires_at)
                        and _same_challenge_authority(existing, item)
                    ):
                        await db.rollback()
                        return existing
                    reason = (
                        EvolutionApprovalSignatureChallengeStatus.EXPIRED.value
                        if timestamp >= _aware(existing.payload.expires_at)
                        else EvolutionApprovalSignatureChallengeStatus.SUPERSEDED.value
                    )
                    await db.execute(
                        "UPDATE evolution_approval_signature_challenges "
                        "SET closed_reason = ? WHERE challenge_id = ? AND closed_reason = ''",
                        (reason, existing.challenge_id),
                    )
                collision = await (
                    await db.execute(
                        "SELECT challenge_json FROM evolution_approval_signature_challenges "
                        "WHERE challenge_id = ?",
                        (item.challenge_id,),
                    )
                ).fetchone()
                if collision is not None:
                    restored = EvolutionApprovalSignatureChallenge.model_validate_json(
                        str(collision["challenge_json"])
                    )
                    if restored != item:
                        await db.rollback()
                        raise EvolutionApprovalSignatureError(
                            "approval_signature_challenge_conflict",
                            "Signature Challenge ID 已绑定不同内容。",
                        )
                    await db.rollback()
                    return restored
                await db.execute(
                    "INSERT INTO evolution_approval_signature_challenges "
                    "(challenge_id, challenge_sha256, workspace_root, approval_response_id, "
                    "requirement_id, role, principal_id, principal_event_sha256, key_id, "
                    "attempt, expires_at, closed_reason, consumed_receipt_id, challenge_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', ?)",
                    (
                        item.challenge_id,
                        item.challenge_sha256,
                        item.payload.workspace_root,
                        item.payload.approval_response_id,
                        item.payload.requirement_id,
                        item.payload.role.value,
                        item.payload.principal_id,
                        item.payload.principal_event_sha256,
                        item.payload.key_id,
                        item.attempt,
                        item.payload.expires_at,
                        encoded,
                    ),
                )
                await db.commit()
        except EvolutionApprovalSignatureError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionApprovalSignatureError(
                "approval_signature_challenge_store_error",
                "Approval Signature Challenge 无法持久化。",
            ) from exc
        restored = await self.get_challenge(item.challenge_id)
        assert restored is not None
        return restored.challenge

    async def record_receipt(
        self,
        receipt: EvolutionApprovalSignatureReceipt,
        *,
        response: EvolutionPromotionApprovalResponseReceipt,
        requirement: EvolutionPromotionApprovalRequirement,
        principal: EvolutionApprovalPrincipalEvent,
    ) -> EvolutionApprovalSignatureReceipt:
        item = _validated_receipt(receipt)
        _validate_sources(response, requirement, principal)
        _require_challenge_sources(item.challenge, response, requirement, principal)
        if item.principal != principal:
            raise EvolutionApprovalSignatureError(
                "approval_signature_principal_mismatch",
                "Signature Receipt 未绑定 exact Principal Event。",
            )
        encoded = item.model_dump_json()
        _require_bounded(encoded, "Signature Receipt")
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                await _assert_current_authorities(
                    db,
                    response=response,
                    requirement=requirement,
                    principal=principal,
                )
                existing = await (
                    await db.execute(
                        "SELECT * FROM evolution_approval_signature_receipts "
                        "WHERE approval_response_id = ?",
                        (item.challenge.payload.approval_response_id,),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _receipt_from_row(existing)
                    if restored != item:
                        await db.rollback()
                        raise EvolutionApprovalSignatureError(
                            "approval_signature_receipt_conflict",
                            "同一 Approval Response 已绑定不同 Signature Receipt。",
                        )
                    await db.rollback()
                    return restored
                challenge_row = await (
                    await db.execute(
                        "SELECT * FROM evolution_approval_signature_challenges "
                        "WHERE challenge_id = ?",
                        (item.challenge.challenge_id,),
                    )
                ).fetchone()
                if challenge_row is None or _challenge_from_row(challenge_row) != item.challenge:
                    await db.rollback()
                    raise EvolutionApprovalSignatureError(
                        "approval_signature_challenge_missing",
                        "Signature Challenge authority 缺失或不一致。",
                    )
                if str(challenge_row["closed_reason"]):
                    await db.rollback()
                    raise EvolutionApprovalSignatureError(
                        "approval_signature_challenge_closed",
                        "Signature Challenge 已关闭，不能再次消费。",
                    )
                if _aware(item.verified_at) >= _aware(item.challenge.payload.expires_at):
                    await db.rollback()
                    raise EvolutionApprovalSignatureError(
                        "approval_signature_challenge_expired",
                        "Signature Challenge 已过期。",
                    )
                await db.execute(
                    "INSERT INTO evolution_approval_signature_receipts "
                    "(receipt_id, receipt_sha256, challenge_id, workspace_root, "
                    "approval_response_id, requirement_id, role, principal_id, key_id, "
                    "signature_sha256, verified_at, receipt_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.receipt_id,
                        item.receipt_sha256,
                        item.challenge.challenge_id,
                        item.challenge.payload.workspace_root,
                        item.challenge.payload.approval_response_id,
                        item.challenge.payload.requirement_id,
                        item.challenge.payload.role.value,
                        item.challenge.payload.principal_id,
                        item.challenge.payload.key_id,
                        item.signature_sha256,
                        item.verified_at,
                        encoded,
                    ),
                )
                updated = await db.execute(
                    "UPDATE evolution_approval_signature_challenges "
                    "SET closed_reason = ?, consumed_receipt_id = ? "
                    "WHERE challenge_id = ? AND closed_reason = ''",
                    (
                        EvolutionApprovalSignatureChallengeStatus.CONSUMED.value,
                        item.receipt_id,
                        item.challenge.challenge_id,
                    ),
                )
                if updated.rowcount != 1:
                    await db.rollback()
                    raise EvolutionApprovalSignatureError(
                        "approval_signature_challenge_race",
                        "Signature Challenge 已被并发消费。",
                    )
                await db.commit()
        except EvolutionApprovalSignatureError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionApprovalSignatureError(
                "approval_signature_receipt_store_error",
                "Approval Signature Receipt 无法持久化。",
            ) from exc
        restored = await self.get_receipt(item.receipt_id)
        assert restored is not None
        return restored

    async def get_challenge(
        self,
        challenge_id: str,
        *,
        now: datetime | None = None,
    ) -> EvolutionApprovalSignatureChallengeView | None:
        if re.fullmatch(r"evsigchallenge_[0-9a-f]{24}", str(challenge_id)) is None:
            raise ValueError("signature challenge id 格式无效。")
        if not self._db_path.is_file():
            return None
        timestamp = _aware_datetime(now or datetime.now(UTC), "Signature Challenge read clock")
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_approval_signature_challenges "
                        "WHERE challenge_id = ?",
                        (challenge_id,),
                    )
                ).fetchone()
                if row is None:
                    return None
                challenge = _challenge_from_row(row)
                closed = str(row["closed_reason"])
                if closed:
                    status = EvolutionApprovalSignatureChallengeStatus(closed)
                elif timestamp >= _aware(challenge.payload.expires_at):
                    status = EvolutionApprovalSignatureChallengeStatus.EXPIRED
                else:
                    status = EvolutionApprovalSignatureChallengeStatus.PENDING
                receipt_id = str(row["consumed_receipt_id"])
                return EvolutionApprovalSignatureChallengeView(
                    challenge=challenge,
                    status=status,
                    eligible_for_submission=(
                        status is EvolutionApprovalSignatureChallengeStatus.PENDING
                    ),
                    closed_by_receipt_id=receipt_id,
                )
        except EvolutionApprovalSignatureError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionApprovalSignatureError(
                "approval_signature_challenge_store_corrupt",
                "Approval Signature Challenge 损坏或无法读取。",
            ) from exc

    async def get_receipt(
        self,
        receipt_id: str,
    ) -> EvolutionApprovalSignatureReceipt | None:
        if re.fullmatch(r"evsigreceipt_[0-9a-f]{24}", str(receipt_id)) is None:
            raise ValueError("signature receipt id 格式无效。")
        if not self._db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_approval_signature_receipts "
                        "WHERE receipt_id = ?",
                        (receipt_id,),
                    )
                ).fetchone()
                return None if row is None else _receipt_from_row(row)
        except EvolutionApprovalSignatureError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionApprovalSignatureError(
                "approval_signature_receipt_store_corrupt",
                "Approval Signature Receipt 损坏或无法读取。",
            ) from exc

    async def get_receipt_by_response(
        self,
        approval_response_id: str,
    ) -> EvolutionApprovalSignatureReceipt | None:
        if re.fullmatch(r"evapprovalresp_[0-9a-f]{24}", str(approval_response_id)) is None:
            raise ValueError("approval response receipt id 格式无效。")
        if not self._db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_approval_signature_receipts "
                        "WHERE approval_response_id = ?",
                        (approval_response_id,),
                    )
                ).fetchone()
                return None if row is None else _receipt_from_row(row)
        except EvolutionApprovalSignatureError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionApprovalSignatureError(
                "approval_signature_receipt_store_corrupt",
                "Approval Signature Receipt 损坏或无法读取。",
            ) from exc

    async def next_attempt(self, approval_response_id: str, principal_id: str) -> int:
        if not self._db_path.is_file():
            return 1
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT MAX(attempt) AS value FROM "
                        "evolution_approval_signature_challenges "
                        "WHERE approval_response_id = ? AND principal_id = ?",
                        (approval_response_id, principal_id),
                    )
                ).fetchone()
                attempt = int(row["value"] or 0) + 1
                if attempt > 10_000:
                    raise EvolutionApprovalSignatureError(
                        "approval_signature_attempts_exhausted",
                        "Signature Challenge 尝试次数已达上限。",
                    )
                return attempt
        except EvolutionApprovalSignatureError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionApprovalSignatureError(
                "approval_signature_challenge_store_corrupt",
                "无法读取 Signature Challenge attempt。",
            ) from exc


class EvolutionApprovalSignatureService:
    def __init__(
        self,
        *,
        response_store: EvolutionPromotionApprovalResponseStore,
        requirement_executor: EvolutionPromotionApprovalRequirementExecutor,
        principal_service: EvolutionApprovalPrincipalService,
        signature_store: EvolutionApprovalSignatureStore,
        builder: EvolutionApprovalSignatureBuilder | None = None,
        clock: Callable[[], datetime] | None = None,
        challenge_ttl_seconds: int = _DEFAULT_CHALLENGE_TTL_SECONDS,
    ) -> None:
        if not isinstance(response_store, EvolutionPromotionApprovalResponseStore):
            raise TypeError("Approval Signature service 需要 Response Store。")
        if not isinstance(
            requirement_executor,
            EvolutionPromotionApprovalRequirementExecutor,
        ):
            raise TypeError("Approval Signature service 需要 Requirement Executor。")
        if not isinstance(principal_service, EvolutionApprovalPrincipalService):
            raise TypeError("Approval Signature service 需要 Principal Service。")
        if not isinstance(signature_store, EvolutionApprovalSignatureStore):
            raise TypeError("Approval Signature service 需要 Signature Store。")
        if not 30 <= challenge_ttl_seconds <= 3_600:
            raise ValueError("Signature Challenge TTL 必须在 30..3600 秒。")
        self._response_store = response_store
        self._requirement_executor = requirement_executor
        self._principal_service = principal_service
        self._signature_store = signature_store
        self._builder = builder or EvolutionApprovalSignatureBuilder()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._challenge_ttl_seconds = challenge_ttl_seconds
        # A long-running Agent may see many distinct responses. Weakly-held
        # locks keep single-flight semantics while allowing completed keys to
        # disappear instead of becoming an unbounded process-lifetime cache.
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def prepare(
        self,
        *,
        workspace_root: str | Path,
        approval_response_id: str,
        principal_id: str,
    ) -> EvolutionApprovalSignatureChallengeView:
        key = f"{Path(workspace_root).expanduser()}::{approval_response_id}::{principal_id}"
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            response, requirement_view, principal = await self._resolve_sources(
                workspace_root=workspace_root,
                approval_response_id=approval_response_id,
                principal_id=principal_id,
            )
            _require_requirement_current(requirement_view)
            existing_receipt = await self._signature_store.get_receipt_by_response(
                response.receipt_id
            )
            if existing_receipt is not None:
                raise EvolutionApprovalSignatureError(
                    "approval_signature_already_recorded",
                    f"该 Approval Response 已有 Signature Receipt {existing_receipt.receipt_id}。",
                )
            now = self._now()
            expiry = min(
                _aware(requirement_view.requirement.expires_at),
                now + timedelta(seconds=self._challenge_ttl_seconds),
            )
            if expiry - now < timedelta(seconds=30):
                raise EvolutionApprovalSignatureError(
                    "approval_signature_challenge_window_too_short",
                    "Approval Requirement 剩余时间不足 30 秒，不能创建签名 Challenge。",
                )
            attempt = await self._signature_store.next_attempt(
                response.receipt_id,
                principal.principal_id,
            )
            challenge = self._builder.build_challenge(
                response=response,
                requirement=requirement_view.requirement,
                principal=principal,
                attempt=attempt,
                issued_at=now,
                expires_at=expiry,
            )
            stored = await self._signature_store.record_challenge(
                challenge,
                response=response,
                requirement=requirement_view.requirement,
                principal=principal,
                now=now,
            )
            view = await self._signature_store.get_challenge(stored.challenge_id, now=now)
            assert view is not None
            return view

    async def submit(
        self,
        *,
        workspace_root: str | Path,
        challenge_id: str,
        signature_base64: str,
    ) -> EvolutionApprovalSignatureReceiptView:
        lock = self._locks.setdefault(f"signature::{challenge_id}", asyncio.Lock())
        async with lock:
            now = self._now()
            challenge_view = await self._signature_store.get_challenge(
                challenge_id,
                now=now,
            )
            if challenge_view is None:
                raise EvolutionApprovalSignatureError(
                    "approval_signature_challenge_missing",
                    "Approval Signature Challenge 不存在。",
                )
            if not challenge_view.eligible_for_submission:
                if challenge_view.closed_by_receipt_id:
                    existing = await self._signature_store.get_receipt(
                        challenge_view.closed_by_receipt_id
                    )
                    if existing is None or not hmac.compare_digest(
                        existing.signature_base64,
                        _normalize_signature(signature_base64),
                    ):
                        raise EvolutionApprovalSignatureError(
                            "approval_signature_receipt_conflict",
                            "已消费 Challenge 不能绑定不同签名。",
                        )
                    return await self.inspect(
                        workspace_root=workspace_root,
                        receipt_id=challenge_view.closed_by_receipt_id,
                    )
                raise EvolutionApprovalSignatureError(
                    "approval_signature_challenge_closed",
                    f"Approval Signature Challenge 状态为 {challenge_view.status.value}。",
                )
            payload = challenge_view.challenge.payload
            response, requirement_view, principal = await self._resolve_sources(
                workspace_root=workspace_root,
                approval_response_id=payload.approval_response_id,
                principal_id=payload.principal_id,
            )
            _require_requirement_current(requirement_view)
            _require_challenge_sources(
                challenge_view.challenge,
                response,
                requirement_view.requirement,
                principal,
            )
            receipt = self._builder.build_receipt(
                challenge=challenge_view.challenge,
                principal=principal,
                signature_base64=signature_base64,
                verified_at=now,
            )
            response, requirement_view, principal = await self._resolve_sources(
                workspace_root=workspace_root,
                approval_response_id=payload.approval_response_id,
                principal_id=payload.principal_id,
            )
            _require_requirement_current(requirement_view)
            _require_challenge_sources(
                challenge_view.challenge,
                response,
                requirement_view.requirement,
                principal,
            )
            stored = await self._signature_store.record_receipt(
                receipt,
                response=response,
                requirement=requirement_view.requirement,
                principal=principal,
            )
            return _receipt_view(
                stored,
                response=response,
                requirement_view=requirement_view,
                principal=principal,
            )

    async def inspect(
        self,
        *,
        workspace_root: str | Path,
        receipt_id: str,
    ) -> EvolutionApprovalSignatureReceiptView:
        try:
            receipt = await self._signature_store.get_receipt(receipt_id)
        except (OSError, TypeError, ValueError) as exc:
            raise EvolutionApprovalSignatureError(
                "approval_signature_receipt_read_failed",
                "无法读取 Approval Signature Receipt。",
            ) from exc
        if receipt is None:
            raise EvolutionApprovalSignatureError(
                "approval_signature_receipt_missing",
                "Approval Signature Receipt 不存在。",
            )
        payload = receipt.challenge.payload
        response, requirement_view, principal = await self._resolve_sources(
            workspace_root=workspace_root,
            approval_response_id=payload.approval_response_id,
            principal_id=payload.principal_id,
            require_active_principal=False,
        )
        return _receipt_view(
            receipt,
            response=response,
            requirement_view=requirement_view,
            principal=principal,
        )

    async def _resolve_sources(
        self,
        *,
        workspace_root: str | Path,
        approval_response_id: str,
        principal_id: str,
        require_active_principal: bool = True,
    ) -> tuple[
        EvolutionPromotionApprovalResponseReceipt,
        EvolutionPromotionApprovalRequirementView,
        EvolutionApprovalPrincipalEvent,
    ]:
        try:
            response = await self._response_store.get(approval_response_id)
        except EvolutionPromotionApprovalRequestError as exc:
            raise EvolutionApprovalSignatureError(
                "approval_signature_response_read_failed",
                "无法读取 Approval Response authority。",
            ) from exc
        if response is None:
            raise EvolutionApprovalSignatureError(
                "approval_signature_response_missing",
                "Approval Response Receipt 不存在。",
            )
        workspace = str(Path(workspace_root).expanduser().resolve())
        if response.workspace_root != workspace:
            raise EvolutionApprovalSignatureError(
                "approval_signature_workspace_mismatch",
                "Approval Response 不属于当前工作区。",
            )
        try:
            requirement_view = await self._requirement_executor.inspect(
                workspace_root=workspace,
                requirement_id=response.requirement_id,
            )
        except EvolutionPromotionApprovalRequirementError as exc:
            raise EvolutionApprovalSignatureError(
                "approval_signature_requirement_read_failed",
                "无法读取 still-current Approval Requirement authority。",
            ) from exc
        try:
            principal_view = await self._principal_service.inspect(
                workspace_root=workspace,
                principal_id=principal_id,
            )
        except EvolutionApprovalPrincipalError as exc:
            raise EvolutionApprovalSignatureError(
                "approval_signature_principal_read_failed",
                "无法读取 Approval Principal authority。",
            ) from exc
        principal = principal_view.principal
        _validate_response_requirement(response, requirement_view.requirement)
        if principal.workspace_root != workspace:
            raise EvolutionApprovalSignatureError(
                "approval_signature_workspace_mismatch",
                "Approval Principal 不属于当前工作区。",
            )
        if require_active_principal and not principal_view.active:
            raise EvolutionApprovalSignatureError(
                "approval_signature_principal_revoked",
                "Approval Principal 已撤销。",
            )
        if require_active_principal and response.role not in principal.roles:
            raise EvolutionApprovalSignatureError(
                "approval_signature_role_mismatch",
                "Approval Principal 未绑定该审批角色。",
            )
        if (
            require_active_principal
            and not principal_view.signature_verification_eligible
        ):
            raise EvolutionApprovalSignatureError(
                "approval_signature_principal_ineligible",
                "Approval Principal 当前不能参与签名验证。",
            )
        return response, requirement_view, principal

    def _now(self) -> datetime:
        return _aware_datetime(self._clock(), "Approval Signature clock")


def render_evolution_approval_signature(
    value: EvolutionApprovalSignatureChallengeView | EvolutionApprovalSignatureReceiptView,
) -> str:
    if isinstance(value, EvolutionApprovalSignatureChallengeView):
        view = EvolutionApprovalSignatureChallengeView.model_validate(
            value.model_dump(mode="python")
        )
        item = view.challenge
        payload = item.payload
        return "\n".join(
            [
                f"# Approval Signature Challenge `{item.challenge_id}`",
                "",
                "**请在 Naumi 外部使用对应 Ed25519 私钥签署下方 exact Base64 解码后的 bytes。**",
                "",
                f"- Status：`{view.status.value}` · attempt {item.attempt}",
                f"- Role：`{payload.role.value}` · response `approve`",
                f"- Principal：`{payload.principal_id}`",
                f"- Key：`{payload.key_id}` · generation {payload.key_generation}",
                f"- Expires：`{payload.expires_at}`",
                f"- Payload SHA-256：`{item.signable_payload_sha256}`",
                "- Private key requested/stored：`false`",
                "- Final approval/Promotion/Git：`false`",
                "",
                "## Signable payload Base64",
                "",
                item.signable_payload_base64,
                "",
                "签名后提交：",
                f"`/evolution approval-signature submit {item.challenge_id} "
                "<ed25519-signature-base64>`",
            ]
        )
    view = EvolutionApprovalSignatureReceiptView.model_validate(
        value.model_dump(mode="python")
    )
    item = view.receipt
    payload = item.challenge.payload
    return "\n".join(
        [
            f"# Approval Signature Receipt `{item.receipt_id}`",
            "",
            "**Ed25519 signature 已通过 exact Challenge 验证；这仍不是最终 Promotion 决定。**",
            "",
            f"- Challenge：`{item.challenge.challenge_id}`",
            f"- Approval Response：`{payload.approval_response_id}`",
            f"- Role：`{payload.role.value}`",
            f"- Principal：`{payload.principal_id}`",
            f"- Key：`{payload.key_id}` · generation {payload.key_generation}",
            f"- Signature SHA-256：`{item.signature_sha256}`",
            f"- Verified at：`{item.verified_at}`",
            f"- Current eligibility：{'是' if view.eligible_for_future_aggregation else '否'}",
            "- Signature/Identity/Role verified：`true`",
            "- Private key requested/stored：`false`",
            "- Final quorum/Promotion/Git：`false`",
        ]
    )


def _validate_sources(
    response: EvolutionPromotionApprovalResponseReceipt,
    requirement: EvolutionPromotionApprovalRequirement,
    principal: EvolutionApprovalPrincipalEvent,
) -> None:
    _validate_response_requirement(response, requirement)
    if not (
        response.workspace_root == principal.workspace_root
        and principal.state is EvolutionApprovalPrincipalState.ACTIVE
        and response.role in principal.roles
    ):
        raise EvolutionApprovalSignatureError(
            "approval_signature_authority_mismatch",
            "Response、Requirement 与 Principal authority 不一致或不需要签名。",
        )


def _require_requirement_current(
    view: EvolutionPromotionApprovalRequirementView,
) -> None:
    if view.expired:
        raise EvolutionApprovalSignatureError(
            "approval_signature_requirement_expired",
            "Approval Requirement 已过期。",
        )
    if not view.package_active or not view.target_current:
        raise EvolutionApprovalSignatureError(
            "approval_signature_requirement_ineligible",
            "Approval Requirement 的 Package、Reflection 或 target 已失效。",
        )


def _validate_response_requirement(
    response: EvolutionPromotionApprovalResponseReceipt,
    requirement: EvolutionPromotionApprovalRequirement,
) -> None:
    if not (
        response.workspace_root == requirement.workspace_root
        and response.requirement_id == requirement.requirement_id
        and response.requirement_sha256 == requirement.requirement_sha256
        and response.package_id == requirement.package_id
        and response.package_sha256 == requirement.package_sha256
        and response.response is EvolutionPromotionApprovalResponse.APPROVE
        and response.signature_entry.required
        and response.signature_entry.signable_payload_sha256
        == requirement.signable_payload_sha256
    ):
        raise EvolutionApprovalSignatureError(
            "approval_signature_response_requirement_mismatch",
            "Approval Response 与 Requirement authority 不一致或不需要签名。",
        )


def _require_challenge_sources(
    challenge: EvolutionApprovalSignatureChallenge,
    response: EvolutionPromotionApprovalResponseReceipt,
    requirement: EvolutionPromotionApprovalRequirement,
    principal: EvolutionApprovalPrincipalEvent,
) -> None:
    payload = challenge.payload
    if not (
        payload.workspace_root == response.workspace_root
        and payload.approval_response_id == response.receipt_id
        and payload.approval_response_sha256 == response.receipt_sha256
        and payload.approval_request_id == response.approval_request_id
        and payload.requirement_id == requirement.requirement_id
        and payload.requirement_sha256 == requirement.requirement_sha256
        and payload.package_id == requirement.package_id
        and payload.package_sha256 == requirement.package_sha256
        and payload.role is response.role
        and payload.source_signable_payload_sha256
        == response.signature_entry.signable_payload_sha256
        and payload.principal_id == principal.principal_id
        and payload.principal_event_id == principal.event_id
        and payload.principal_event_sha256 == principal.event_sha256
        and payload.key_id == principal.key_id
        and payload.key_generation == principal.key_generation
        and payload.public_key_sha256 == principal.public_key_sha256
    ):
        raise EvolutionApprovalSignatureError(
            "approval_signature_challenge_authority_mismatch",
            "Signature Challenge authority binding 已变化。",
        )


def _same_challenge_authority(
    first: EvolutionApprovalSignatureChallenge,
    second: EvolutionApprovalSignatureChallenge,
) -> bool:
    left = first.payload.model_dump(
        mode="json",
        exclude={"nonce_base64", "issued_at", "expires_at"},
    )
    right = second.payload.model_dump(
        mode="json",
        exclude={"nonce_base64", "issued_at", "expires_at"},
    )
    return left == right


def _verify_signature(
    challenge: EvolutionApprovalSignatureChallenge,
    principal: EvolutionApprovalPrincipalEvent,
    signature_base64: str,
) -> None:
    payload = challenge.payload
    if not (
        principal.state is EvolutionApprovalPrincipalState.ACTIVE
        and principal.principal_id == payload.principal_id
        and principal.event_id == payload.principal_event_id
        and principal.event_sha256 == payload.principal_event_sha256
        and principal.key_id == payload.key_id
        and principal.key_generation == payload.key_generation
        and principal.public_key_sha256 == payload.public_key_sha256
        and payload.role in principal.roles
    ):
        raise ValueError("Principal 当前 key 或 role 与 Challenge 不一致。")
    signature = base64.b64decode(signature_base64, validate=True)
    public_key = base64.b64decode(principal.public_key_base64, validate=True)
    Ed25519PublicKey.from_public_bytes(public_key).verify(
        signature,
        challenge.payload.canonical_bytes(),
    )


def _receipt_view(
    receipt: EvolutionApprovalSignatureReceipt,
    *,
    response: EvolutionPromotionApprovalResponseReceipt,
    requirement_view: EvolutionPromotionApprovalRequirementView,
    principal: EvolutionApprovalPrincipalEvent,
) -> EvolutionApprovalSignatureReceiptView:
    payload = receipt.challenge.payload
    response_current = bool(
        response.receipt_id == payload.approval_response_id
        and response.receipt_sha256 == payload.approval_response_sha256
        and response.response is EvolutionPromotionApprovalResponse.APPROVE
    )
    principal_active = principal.state is EvolutionApprovalPrincipalState.ACTIVE
    key_current = bool(
        principal.event_id == payload.principal_event_id
        and principal.event_sha256 == payload.principal_event_sha256
        and principal.key_id == payload.key_id
        and principal.key_generation == payload.key_generation
        and principal.public_key_sha256 == payload.public_key_sha256
    )
    role_current = payload.role in principal.roles
    eligible = bool(
        requirement_view.package_active
        and requirement_view.target_current
        and not requirement_view.expired
        and response_current
        and principal_active
        and key_current
        and role_current
    )
    return EvolutionApprovalSignatureReceiptView(
        receipt=receipt,
        requirement_current=requirement_view.package_active,
        target_current=requirement_view.target_current,
        requirement_expired=requirement_view.expired,
        response_current=response_current,
        principal_active=principal_active,
        principal_key_current=key_current,
        role_binding_current=role_current,
        eligible_for_future_aggregation=eligible,
    )


async def _assert_current_authorities(
    db: aiosqlite.Connection,
    *,
    response: EvolutionPromotionApprovalResponseReceipt,
    requirement: EvolutionPromotionApprovalRequirement,
    principal: EvolutionApprovalPrincipalEvent,
) -> None:
    response_row = await (
        await db.execute(
            "SELECT receipt_sha256, requirement_sha256, role, response "
            "FROM evolution_promotion_approval_responses WHERE receipt_id = ?",
            (response.receipt_id,),
        )
    ).fetchone()
    requirement_row = await (
        await db.execute(
            "SELECT requirement_sha256 FROM evolution_promotion_approval_requirements "
            "WHERE requirement_id = ?",
            (requirement.requirement_id,),
        )
    ).fetchone()
    principal_row = await (
        await db.execute(
            "SELECT latest_event_sha256, state, key_id, key_generation, roles_json "
            "FROM evolution_approval_principals "
            "WHERE workspace_root = ? AND principal_id = ?",
            (principal.workspace_root, principal.principal_id),
        )
    ).fetchone()
    if principal_row is None:
        raise EvolutionApprovalSignatureError(
            "approval_signature_authority_changed",
            "Approval Principal authority 已不存在。",
        )
    try:
        principal_roles = json.loads(str(principal_row["roles_json"]))
    except (TypeError, ValueError) as exc:
        raise EvolutionApprovalSignatureError(
            "approval_signature_authority_store_corrupt",
            "Principal authority index 损坏。",
        ) from exc
    if not (
        response_row is not None
        and response_row["receipt_sha256"] == response.receipt_sha256
        and response_row["requirement_sha256"] == requirement.requirement_sha256
        and response_row["role"] == response.role.value
        and response_row["response"] == EvolutionPromotionApprovalResponse.APPROVE.value
        and requirement_row is not None
        and requirement_row["requirement_sha256"] == requirement.requirement_sha256
        and principal_row is not None
        and principal_row["latest_event_sha256"] == principal.event_sha256
        and principal_row["state"] == EvolutionApprovalPrincipalState.ACTIVE.value
        and principal_row["key_id"] == principal.key_id
        and int(principal_row["key_generation"]) == principal.key_generation
        and response.role.value in principal_roles
    ):
        raise EvolutionApprovalSignatureError(
            "approval_signature_authority_changed",
            "Response、Requirement 或 Principal authority 已变化。",
        )


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS evolution_approval_signature_challenges (
            challenge_id TEXT PRIMARY KEY,
            challenge_sha256 TEXT NOT NULL,
            workspace_root TEXT NOT NULL,
            approval_response_id TEXT NOT NULL,
            requirement_id TEXT NOT NULL,
            role TEXT NOT NULL,
            principal_id TEXT NOT NULL,
            principal_event_sha256 TEXT NOT NULL,
            key_id TEXT NOT NULL,
            attempt INTEGER NOT NULL,
            expires_at TEXT NOT NULL,
            closed_reason TEXT NOT NULL DEFAULT '',
            consumed_receipt_id TEXT NOT NULL DEFAULT '',
            challenge_json TEXT NOT NULL,
            UNIQUE(approval_response_id, principal_id, attempt)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS
            uq_evolution_approval_signature_active_challenge
        ON evolution_approval_signature_challenges(approval_response_id, principal_id)
        WHERE closed_reason = '';
        CREATE TABLE IF NOT EXISTS evolution_approval_signature_receipts (
            receipt_id TEXT PRIMARY KEY,
            receipt_sha256 TEXT NOT NULL,
            challenge_id TEXT NOT NULL UNIQUE,
            workspace_root TEXT NOT NULL,
            approval_response_id TEXT NOT NULL UNIQUE,
            requirement_id TEXT NOT NULL,
            role TEXT NOT NULL,
            principal_id TEXT NOT NULL,
            key_id TEXT NOT NULL,
            signature_sha256 TEXT NOT NULL,
            verified_at TEXT NOT NULL,
            receipt_json TEXT NOT NULL
        );
        """
    )


def _challenge_from_row(row: aiosqlite.Row) -> EvolutionApprovalSignatureChallenge:
    encoded = str(row["challenge_json"])
    _require_bounded(encoded, "Signature Challenge")
    item = EvolutionApprovalSignatureChallenge.model_validate_json(encoded)
    if not (
        row["challenge_id"] == item.challenge_id
        and row["challenge_sha256"] == item.challenge_sha256
        and row["workspace_root"] == item.payload.workspace_root
        and row["approval_response_id"] == item.payload.approval_response_id
        and row["requirement_id"] == item.payload.requirement_id
        and row["role"] == item.payload.role.value
        and row["principal_id"] == item.payload.principal_id
        and row["principal_event_sha256"] == item.payload.principal_event_sha256
        and row["key_id"] == item.payload.key_id
        and int(row["attempt"]) == item.attempt
        and row["expires_at"] == item.payload.expires_at
        and str(row["closed_reason"])
        in {
            "",
            EvolutionApprovalSignatureChallengeStatus.EXPIRED.value,
            EvolutionApprovalSignatureChallengeStatus.CONSUMED.value,
            EvolutionApprovalSignatureChallengeStatus.SUPERSEDED.value,
        }
        and (
            bool(str(row["consumed_receipt_id"]))
            == (
                str(row["closed_reason"])
                == EvolutionApprovalSignatureChallengeStatus.CONSUMED.value
            )
        )
    ):
        raise ValueError("Signature Challenge Store index 不一致。")
    return item


def _receipt_from_row(row: aiosqlite.Row) -> EvolutionApprovalSignatureReceipt:
    encoded = str(row["receipt_json"])
    _require_bounded(encoded, "Signature Receipt")
    item = EvolutionApprovalSignatureReceipt.model_validate_json(encoded)
    payload = item.challenge.payload
    if not (
        row["receipt_id"] == item.receipt_id
        and row["receipt_sha256"] == item.receipt_sha256
        and row["challenge_id"] == item.challenge.challenge_id
        and row["workspace_root"] == payload.workspace_root
        and row["approval_response_id"] == payload.approval_response_id
        and row["requirement_id"] == payload.requirement_id
        and row["role"] == payload.role.value
        and row["principal_id"] == payload.principal_id
        and row["key_id"] == payload.key_id
        and row["signature_sha256"] == item.signature_sha256
        and row["verified_at"] == item.verified_at
    ):
        raise ValueError("Signature Receipt Store index 不一致。")
    return item


def _validated_challenge(
    value: EvolutionApprovalSignatureChallenge,
) -> EvolutionApprovalSignatureChallenge:
    try:
        return EvolutionApprovalSignatureChallenge.model_validate_json(value.model_dump_json())
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionApprovalSignatureError(
            "approval_signature_challenge_invalid",
            "Approval Signature Challenge 无效。",
        ) from exc


def _validated_receipt(
    value: EvolutionApprovalSignatureReceipt,
) -> EvolutionApprovalSignatureReceipt:
    try:
        return EvolutionApprovalSignatureReceipt.model_validate_json(value.model_dump_json())
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionApprovalSignatureError(
            "approval_signature_receipt_invalid",
            "Approval Signature Receipt 无效。",
        ) from exc


def _normalize_signature(value: str) -> str:
    raw = _decode_base64(value, expected_bytes=64, label="signature")
    encoded = base64.b64encode(raw).decode("ascii")
    if not hmac.compare_digest(encoded, str(value or "").strip()):
        raise ValueError("Ed25519 signature 必须使用 canonical Base64。")
    return encoded


def _decode_base64(value: str, *, expected_bytes: int, label: str) -> bytes:
    try:
        raw = base64.b64decode(str(value or "").strip(), validate=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} Base64 无效。") from exc
    if len(raw) != expected_bytes:
        raise ValueError(f"{label} 必须是 {expected_bytes} bytes。")
    return raw


def _require_bounded(encoded: str, label: str) -> None:
    if len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
        raise EvolutionApprovalSignatureError(
            "approval_signature_artifact_oversized",
            f"{label} 超过 256 KiB 上限。",
        )


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("timestamp 必须包含时区。")
    return parsed.astimezone(UTC)


def _aware_datetime(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise EvolutionApprovalSignatureError(
            "approval_signature_clock_invalid",
            f"{label} 必须包含时区。",
        )
    return value.astimezone(UTC)


def _canonical_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _sha256_payload(payload: object) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


__all__ = [
    "EVOLUTION_APPROVAL_SIGNATURE_DOMAIN",
    "EVOLUTION_APPROVAL_SIGNATURE_POLICY",
    "EvolutionApprovalSignatureBuilder",
    "EvolutionApprovalSignatureChallenge",
    "EvolutionApprovalSignatureChallengeStatus",
    "EvolutionApprovalSignatureChallengeView",
    "EvolutionApprovalSignatureError",
    "EvolutionApprovalSignaturePayload",
    "EvolutionApprovalSignatureReceipt",
    "EvolutionApprovalSignatureReceiptView",
    "EvolutionApprovalSignatureService",
    "EvolutionApprovalSignatureStore",
    "render_evolution_approval_signature",
]
