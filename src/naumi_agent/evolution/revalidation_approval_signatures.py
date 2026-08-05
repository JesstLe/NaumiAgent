"""Fresh Ed25519 approval signatures bound to revalidation authority."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
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
from naumi_agent.evolution.approval_requests import EvolutionPromotionApprovalResponse
from naumi_agent.evolution.approval_requirements import EvolutionPromotionApprovalRole
from naumi_agent.evolution.revalidation_approval_requests import (
    EvolutionRevalidationApprovalResponseStore,
)
from naumi_agent.evolution.revalidation_approval_requirements import (
    EvolutionRevalidationApprovalRequirementService,
)

EVOLUTION_REVALIDATION_APPROVAL_SIGNATURE_POLICY = "evolution-revalidation-approval-signature-v1"
EVOLUTION_REVALIDATION_PROFESSIONAL_SIGNATURE_DOMAIN = (
    "naumi.evolution.revalidation-approval-signature.v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_ARTIFACT_BYTES = 256 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class EvolutionRevalidationApprovalSignatureStatus(StrEnum):
    PENDING = "pending"
    CONSUMED = "consumed"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"


class EvolutionRevalidationApprovalSignaturePayload(_StrictModel):
    schema_version: Literal[1] = 1
    domain: Literal["naumi.evolution.revalidation-approval-signature.v1"] = (
        EVOLUTION_REVALIDATION_PROFESSIONAL_SIGNATURE_DOMAIN
    )
    workspace_root: str = Field(min_length=1, max_length=4096)
    requirement_id: str = Field(pattern=r"^evreapprovalreq_[0-9a-f]{24}$")
    requirement_sha256: str = Field(pattern=_SHA256_RE)
    promotion_input_id: str = Field(pattern=r"^evrevalpromoin_[0-9a-f]{24}$")
    promotion_input_sha256: str = Field(pattern=_SHA256_RE)
    approval_response_id: str = Field(pattern=r"^evreapprovalresp_[0-9a-f]{24}$")
    approval_response_sha256: str = Field(pattern=_SHA256_RE)
    approval_request_id: str = Field(pattern=r"^evreapprovalrequest_[0-9a-f]{24}$")
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
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Fresh Signature workspace 必须 canonical。")
        nonce = _decode(self.nonce_base64, 32, "nonce")
        if not hmac.compare_digest(base64.b64encode(nonce).decode(), self.nonce_base64):
            raise ValueError("Fresh Signature nonce 必须 canonical Base64。")
        if not _aware(self.issued_at) < _aware(self.expires_at):
            raise ValueError("Fresh Signature challenge 有效期无效。")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical(self.model_dump(mode="json"))


class EvolutionRevalidationApprovalSignatureChallenge(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-approval-signature-v1"] = (
        EVOLUTION_REVALIDATION_APPROVAL_SIGNATURE_POLICY
    )
    challenge_id: str = Field(pattern=r"^evreapprovalsigchallenge_[0-9a-f]{24}$")
    challenge_sha256: str = Field(pattern=_SHA256_RE)
    attempt: int = Field(ge=1, le=10_000)
    payload: EvolutionRevalidationApprovalSignaturePayload
    signable_payload_base64: str = Field(min_length=4, max_length=32_768)
    signable_payload_sha256: str = Field(pattern=_SHA256_RE)
    signature_algorithm: Literal["ed25519"] = "ed25519"
    fresh_signature_required: Literal[True] = True
    prior_signature_reused: Literal[False] = False
    signature_collected: Literal[False] = False
    private_key_requested: Literal[False] = False
    private_key_stored: Literal[False] = False
    decision_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        signable = self.payload.canonical_bytes()
        if not hmac.compare_digest(
            self.signable_payload_base64, base64.b64encode(signable).decode()
        ) or not hmac.compare_digest(
            self.signable_payload_sha256, hashlib.sha256(signable).hexdigest()
        ):
            raise ValueError("Fresh Signature challenge signable payload 不一致。")
        digest = _digest(self.model_dump(mode="json", exclude={"challenge_id", "challenge_sha256"}))
        if (
            self.challenge_sha256 != digest
            or self.challenge_id != f"evreapprovalsigchallenge_{digest[:24]}"
        ):
            raise ValueError("Fresh Signature challenge identity 不一致。")
        return self


class EvolutionRevalidationApprovalSignatureChallengeView(_StrictModel):
    challenge: EvolutionRevalidationApprovalSignatureChallenge
    status: EvolutionRevalidationApprovalSignatureStatus
    eligible_for_submission: bool
    closed_by_receipt_id: str = Field(default="", pattern=r"^(?:|evreapprovalsig_[0-9a-f]{24})$")

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.eligible_for_submission is not (
            self.status is EvolutionRevalidationApprovalSignatureStatus.PENDING
        ):
            raise ValueError("Fresh Signature challenge eligibility 不一致。")
        if bool(self.closed_by_receipt_id) is not (
            self.status is EvolutionRevalidationApprovalSignatureStatus.CONSUMED
        ):
            raise ValueError("Fresh Signature challenge closure 不一致。")
        return self


class EvolutionRevalidationApprovalSignatureReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-approval-signature-v1"] = (
        EVOLUTION_REVALIDATION_APPROVAL_SIGNATURE_POLICY
    )
    receipt_id: str = Field(pattern=r"^evreapprovalsig_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    challenge: EvolutionRevalidationApprovalSignatureChallenge
    principal: EvolutionApprovalPrincipalEvent
    signature_algorithm: Literal["ed25519"] = "ed25519"
    signature_base64: str = Field(min_length=88, max_length=88)
    signature_sha256: str = Field(pattern=_SHA256_RE)
    verified_at: str = Field(min_length=1, max_length=100)
    signature_verified: Literal[True] = True
    fresh_signature_verified: Literal[True] = True
    identity_binding_verified: Literal[True] = True
    principal_key_current_at_verification: Literal[True] = True
    role_binding_verified: Literal[True] = True
    counts_toward_role_quorum: Literal[True] = True
    prior_signature_reused: Literal[False] = False
    decision_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    git_write_executed: Literal[False] = False
    private_key_requested: Literal[False] = False
    private_key_stored: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
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
            raise ValueError("Fresh Signature 未绑定 exact current Principal。")
        signature = _decode(self.signature_base64, 64, "signature")
        if hashlib.sha256(signature).hexdigest() != self.signature_sha256:
            raise ValueError("Fresh Signature digest 不一致。")
        _verify(self.principal, signature, payload.canonical_bytes())
        verified = _aware(self.verified_at)
        if not _aware(payload.issued_at) <= verified < _aware(payload.expires_at):
            raise ValueError("Fresh Signature verified_at 超出 challenge 有效期。")
        digest = _digest(self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"}))
        if self.receipt_sha256 != digest or self.receipt_id != f"evreapprovalsig_{digest[:24]}":
            raise ValueError("Fresh Signature receipt identity 不一致。")
        return self


class EvolutionRevalidationApprovalSignatureReceiptView(_StrictModel):
    receipt: EvolutionRevalidationApprovalSignatureReceipt
    requirement_current: bool
    requirement_expired: bool
    response_current: bool
    principal_active: bool
    principal_key_current: bool
    role_binding_current: bool
    eligible_for_decision_aggregation: bool

    @model_validator(mode="after")
    def _exact(self) -> Self:
        expected = bool(
            self.requirement_current
            and not self.requirement_expired
            and self.response_current
            and self.principal_active
            and self.principal_key_current
            and self.role_binding_current
        )
        if self.eligible_for_decision_aggregation is not expected:
            raise ValueError("Fresh Signature aggregation eligibility 不一致。")
        return self


class EvolutionRevalidationApprovalSignatureError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationApprovalSignatureStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()

    async def get_challenge(
        self, challenge_id: str, *, now: datetime
    ) -> EvolutionRevalidationApprovalSignatureChallengeView | None:
        if not self._db_path.is_file():
            return None
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT challenge_json, status, closed_by_receipt_id FROM "
                    "evolution_revalidation_approval_signature_challenges "
                    "WHERE challenge_id = ?",
                    (challenge_id,),
                )
            ).fetchone()
            if row is None:
                return None
            challenge = _challenge(row["challenge_json"])
            status = EvolutionRevalidationApprovalSignatureStatus(row["status"])
            if status is EvolutionRevalidationApprovalSignatureStatus.PENDING and now >= _aware(
                challenge.payload.expires_at
            ):
                status = EvolutionRevalidationApprovalSignatureStatus.EXPIRED
                await db.execute(
                    "UPDATE evolution_revalidation_approval_signature_challenges "
                    "SET status = 'expired' WHERE challenge_id = ? "
                    "AND status = 'pending'",
                    (challenge_id,),
                )
                await db.commit()
            return EvolutionRevalidationApprovalSignatureChallengeView(
                challenge=challenge,
                status=status,
                eligible_for_submission=status
                is EvolutionRevalidationApprovalSignatureStatus.PENDING,
                closed_by_receipt_id=row["closed_by_receipt_id"],
            )

    async def get_receipt(
        self, receipt_id: str
    ) -> EvolutionRevalidationApprovalSignatureReceipt | None:
        if not self._db_path.is_file():
            return None
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT receipt_json FROM "
                    "evolution_revalidation_approval_signature_receipts "
                    "WHERE receipt_id = ?",
                    (receipt_id,),
                )
            ).fetchone()
        return None if row is None else _receipt(row["receipt_json"])

    async def get_receipt_by_response(
        self, response_id: str
    ) -> EvolutionRevalidationApprovalSignatureReceipt | None:
        if not self._db_path.is_file():
            return None
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT receipt_json FROM "
                    "evolution_revalidation_approval_signature_receipts "
                    "WHERE approval_response_id = ?",
                    (response_id,),
                )
            ).fetchone()
        return None if row is None else _receipt(row["receipt_json"])

    async def get_pending(
        self,
        response_id: str,
        principal_event_id: str,
        *,
        now: datetime,
    ) -> EvolutionRevalidationApprovalSignatureChallengeView | None:
        if not self._db_path.is_file():
            return None
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT challenge_id FROM "
                    "evolution_revalidation_approval_signature_challenges "
                    "WHERE approval_response_id = ? AND principal_event_id = ? "
                    "AND status = 'pending' ORDER BY attempt DESC LIMIT 1",
                    (response_id, principal_event_id),
                )
            ).fetchone()
        if row is None:
            return None
        view = await self.get_challenge(row["challenge_id"], now=now)
        if view is None or not view.eligible_for_submission:
            return None
        return view

    async def next_attempt(self, response_id: str, principal_id: str) -> int:
        if not self._db_path.is_file():
            return 1
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT MAX(attempt) AS value FROM "
                    "evolution_revalidation_approval_signature_challenges "
                    "WHERE approval_response_id = ? AND principal_id = ?",
                    (response_id, principal_id),
                )
            ).fetchone()
        value = int(row["value"] or 0) + 1
        if value > 10_000:
            raise EvolutionRevalidationApprovalSignatureError(
                "fresh_signature_attempts_exhausted", "Fresh Signature challenge 尝试次数已达上限。"
            )
        return value

    async def record_challenge(
        self, challenge: EvolutionRevalidationApprovalSignatureChallenge
    ) -> EvolutionRevalidationApprovalSignatureChallenge:
        encoded = challenge.model_dump_json()
        _bounded(encoded)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "UPDATE evolution_revalidation_approval_signature_challenges "
                "SET status = 'superseded' WHERE approval_response_id = ? "
                "AND status = 'pending'",
                (challenge.payload.approval_response_id,),
            )
            await db.execute(
                "INSERT OR IGNORE INTO "
                "evolution_revalidation_approval_signature_challenges "
                "(challenge_id, approval_response_id, requirement_id, principal_id, "
                "principal_event_id, key_generation, attempt, status, "
                "closed_by_receipt_id, challenge_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', '', ?)",
                (
                    challenge.challenge_id,
                    challenge.payload.approval_response_id,
                    challenge.payload.requirement_id,
                    challenge.payload.principal_id,
                    challenge.payload.principal_event_id,
                    challenge.payload.key_generation,
                    challenge.attempt,
                    encoded,
                ),
            )
            await db.commit()
        return challenge

    async def record_receipt(
        self, receipt: EvolutionRevalidationApprovalSignatureReceipt
    ) -> EvolutionRevalidationApprovalSignatureReceipt:
        encoded = receipt.model_dump_json()
        _bounded(encoded)
        challenge = receipt.challenge
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            await _require_current_dependencies(db, receipt)
            row = await (
                await db.execute(
                    "SELECT status, closed_by_receipt_id, challenge_json FROM "
                    "evolution_revalidation_approval_signature_challenges "
                    "WHERE challenge_id = ?",
                    (challenge.challenge_id,),
                )
            ).fetchone()
            if row is None or _challenge(row["challenge_json"]) != challenge:
                await db.rollback()
                raise EvolutionRevalidationApprovalSignatureError(
                    "fresh_signature_challenge_mismatch",
                    "Fresh Signature challenge authority 不一致。",
                )
            existing = await (
                await db.execute(
                    "SELECT receipt_json FROM "
                    "evolution_revalidation_approval_signature_receipts "
                    "WHERE approval_response_id = ?",
                    (challenge.payload.approval_response_id,),
                )
            ).fetchone()
            if existing is not None:
                restored = _receipt(existing["receipt_json"])
                await db.rollback()
                if restored != receipt:
                    raise EvolutionRevalidationApprovalSignatureError(
                        "fresh_signature_receipt_conflict", "Fresh Response 已绑定不同签名。"
                    )
                return restored
            if row["status"] != "pending":
                await db.rollback()
                raise EvolutionRevalidationApprovalSignatureError(
                    "fresh_signature_challenge_closed", "Fresh Signature challenge 已关闭。"
                )
            await db.execute(
                "INSERT INTO evolution_revalidation_approval_signature_receipts "
                "(receipt_id, approval_response_id, requirement_id, principal_id, "
                "role, receipt_json, verified_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    receipt.receipt_id,
                    challenge.payload.approval_response_id,
                    challenge.payload.requirement_id,
                    challenge.payload.principal_id,
                    challenge.payload.role.value,
                    encoded,
                    receipt.verified_at,
                ),
            )
            await db.execute(
                "UPDATE evolution_revalidation_approval_signature_challenges "
                "SET status = 'consumed', closed_by_receipt_id = ? "
                "WHERE challenge_id = ?",
                (receipt.receipt_id, challenge.challenge_id),
            )
            await db.commit()
        return receipt


class EvolutionRevalidationApprovalSignatureService:
    def __init__(
        self,
        *,
        response_store: EvolutionRevalidationApprovalResponseStore,
        requirement_service: EvolutionRevalidationApprovalRequirementService,
        principal_service: EvolutionApprovalPrincipalService,
        signature_store: EvolutionRevalidationApprovalSignatureStore,
        clock: Callable[[], datetime] | None = None,
        challenge_ttl_seconds: int = 900,
    ) -> None:
        if not 30 <= challenge_ttl_seconds <= 3600:
            raise ValueError("Fresh Signature TTL 必须在 30..3600 秒。")
        self.response_store = response_store
        self.requirement_service = requirement_service
        self.principal_service = principal_service
        self.signature_store = signature_store
        self.clock = clock or (lambda: datetime.now(UTC))
        self.challenge_ttl_seconds = challenge_ttl_seconds
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def prepare(
        self,
        *,
        workspace_root: str | Path,
        contract_id: str,
        requirement_id: str,
        role: EvolutionPromotionApprovalRole | str,
        principal_id: str,
    ) -> EvolutionRevalidationApprovalSignatureChallengeView:
        role_value = EvolutionPromotionApprovalRole(role)
        key = f"{contract_id}:{requirement_id}:{role_value.value}:{principal_id}"
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            response, requirement, principal = await self._sources(
                workspace_root, contract_id, requirement_id, role_value, principal_id
            )
            existing = await self.signature_store.get_receipt_by_response(response.receipt_id)
            if existing is not None:
                raise EvolutionRevalidationApprovalSignatureError(
                    "fresh_signature_already_recorded",
                    f"Fresh Response 已有签名 {existing.receipt_id}。",
                )
            now = self._now()
            pending = await self.signature_store.get_pending(
                response.receipt_id,
                principal.event_id,
                now=now,
            )
            if pending is not None:
                return pending
            expires = min(
                _aware(requirement.expires_at), now + timedelta(seconds=self.challenge_ttl_seconds)
            )
            if expires - now < timedelta(seconds=30):
                raise EvolutionRevalidationApprovalSignatureError(
                    "fresh_signature_window_too_short", "Fresh Requirement 剩余签名时间不足 30 秒。"
                )
            attempt = await self.signature_store.next_attempt(
                response.receipt_id, principal.principal_id
            )
            challenge = _build_challenge(response, requirement, principal, attempt, now, expires)
            await self.signature_store.record_challenge(challenge)
            view = await self.signature_store.get_challenge(challenge.challenge_id, now=now)
            assert view is not None
            return view

    async def submit(
        self,
        *,
        workspace_root: str | Path,
        contract_id: str,
        challenge_id: str,
        signature_base64: str,
    ) -> EvolutionRevalidationApprovalSignatureReceiptView:
        lock = self._locks.setdefault(f"submit:{challenge_id}", asyncio.Lock())
        async with lock:
            now = self._now()
            view = await self.signature_store.get_challenge(challenge_id, now=now)
            if view is None:
                raise EvolutionRevalidationApprovalSignatureError(
                    "fresh_signature_challenge_missing", "Fresh Signature challenge 不存在。"
                )
            if not view.eligible_for_submission:
                if view.closed_by_receipt_id:
                    existing = await self.signature_store.get_receipt(view.closed_by_receipt_id)
                    if existing is not None and hmac.compare_digest(
                        existing.signature_base64, _normalize_signature(signature_base64)
                    ):
                        return await self.inspect(
                            workspace_root=workspace_root,
                            contract_id=contract_id,
                            receipt_id=existing.receipt_id,
                        )
                raise EvolutionRevalidationApprovalSignatureError(
                    "fresh_signature_challenge_closed",
                    f"Fresh Signature challenge 状态为 {view.status.value}。",
                )
            payload = view.challenge.payload
            response, requirement, principal = await self._sources(
                workspace_root,
                contract_id,
                payload.requirement_id,
                payload.role,
                payload.principal_id,
            )
            _match(view.challenge, response, requirement, principal)
            signature = _normalize_signature(signature_base64)
            _verify(principal, _decode(signature, 64, "signature"), payload.canonical_bytes())
            receipt = _build_receipt(view.challenge, principal, signature, now)
            # Re-read all mutable authorities immediately before the atomic consume.
            response, requirement, principal = await self._sources(
                workspace_root,
                contract_id,
                payload.requirement_id,
                payload.role,
                payload.principal_id,
            )
            _match(view.challenge, response, requirement, principal)
            stored = await self.signature_store.record_receipt(receipt)
            return _receipt_view(stored, response, requirement, principal, now)

    async def inspect(
        self, *, workspace_root: str | Path, contract_id: str, receipt_id: str
    ) -> EvolutionRevalidationApprovalSignatureReceiptView:
        receipt = await self.signature_store.get_receipt(receipt_id)
        if receipt is None:
            raise EvolutionRevalidationApprovalSignatureError(
                "fresh_signature_receipt_missing", "Fresh Signature receipt 不存在。"
            )
        payload = receipt.challenge.payload
        requirement = await self.requirement_service.issue(contract_id=contract_id)
        response = await self.response_store.get_by_requirement_role(
            payload.requirement_id,
            payload.role,
        )
        try:
            principal_view = await self.principal_service.inspect(
                workspace_root=workspace_root,
                principal_id=payload.principal_id,
            )
        except (EvolutionApprovalPrincipalError, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationApprovalSignatureError(
                "fresh_signature_principal_unavailable",
                "无法读取 Fresh Signature 的 current Principal。",
            ) from exc
        return _receipt_view(
            receipt,
            response,
            requirement,
            principal_view.principal,
            self._now(),
        )

    async def _sources(self, workspace_root, contract_id, requirement_id, role, principal_id):
        requirement = await self.requirement_service.issue(contract_id=contract_id)
        if requirement.requirement_id != requirement_id:
            raise EvolutionRevalidationApprovalSignatureError(
                "fresh_signature_requirement_stale", "Fresh Requirement 已被替换。"
            )
        if self._now() >= _aware(requirement.expires_at):
            raise EvolutionRevalidationApprovalSignatureError(
                "fresh_signature_requirement_expired", "Fresh Requirement 已过期。"
            )
        response = await self.response_store.get_by_requirement_role(requirement_id, role)
        if response is None:
            raise EvolutionRevalidationApprovalSignatureError(
                "fresh_signature_response_missing", "Fresh professional Response 不存在。"
            )
        if (
            response.response is not EvolutionPromotionApprovalResponse.APPROVE
            or not response.eligible_for_fresh_signature
        ):
            raise EvolutionRevalidationApprovalSignatureError(
                "fresh_signature_response_ineligible", "Fresh Response 不具备签名资格。"
            )
        try:
            principal_view = await self.principal_service.inspect(
                workspace_root=workspace_root, principal_id=principal_id
            )
        except (EvolutionApprovalPrincipalError, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationApprovalSignatureError(
                "fresh_signature_principal_unavailable", "无法读取 current Approval Principal。"
            ) from exc
        principal = principal_view.principal
        if not principal_view.active or role not in principal.roles:
            raise EvolutionRevalidationApprovalSignatureError(
                "fresh_signature_role_unverified", "Approval Principal 未激活或未绑定该专业角色。"
            )
        return response, requirement, principal

    def _now(self) -> datetime:
        value = self.clock()
        if value.utcoffset() is None:
            raise EvolutionRevalidationApprovalSignatureError(
                "fresh_signature_clock_invalid", "Fresh Signature clock 必须包含时区。"
            )
        return value.astimezone(UTC)


def _build_challenge(response, requirement, principal, attempt, issued, expires):
    nonce = secrets.token_bytes(32)
    payload = EvolutionRevalidationApprovalSignaturePayload(
        workspace_root=response.workspace_root,
        requirement_id=requirement.requirement_id,
        requirement_sha256=requirement.requirement_sha256,
        promotion_input_id=requirement.promotion_input_id,
        promotion_input_sha256=requirement.promotion_input_sha256,
        approval_response_id=response.receipt_id,
        approval_response_sha256=response.receipt_sha256,
        approval_request_id=response.approval_request_id,
        role=response.role,
        source_signable_payload_sha256=requirement.signable_payload_sha256,
        principal_id=principal.principal_id,
        principal_event_id=principal.event_id,
        principal_event_sha256=principal.event_sha256,
        key_id=principal.key_id,
        key_generation=principal.key_generation,
        public_key_sha256=principal.public_key_sha256,
        nonce_base64=base64.b64encode(nonce).decode(),
        issued_at=issued.isoformat(),
        expires_at=expires.isoformat(),
    )
    signable = payload.canonical_bytes()
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_APPROVAL_SIGNATURE_POLICY,
        "attempt": attempt,
        "payload": payload.model_dump(mode="json"),
        "signable_payload_base64": base64.b64encode(signable).decode(),
        "signable_payload_sha256": hashlib.sha256(signable).hexdigest(),
        "signature_algorithm": "ed25519",
        "fresh_signature_required": True,
        "prior_signature_reused": False,
        "signature_collected": False,
        "private_key_requested": False,
        "private_key_stored": False,
        "decision_authority": False,
        "promotion_authority": False,
    }
    digest = _digest(core)
    return EvolutionRevalidationApprovalSignatureChallenge.model_validate(
        {
            **core,
            "challenge_id": f"evreapprovalsigchallenge_{digest[:24]}",
            "challenge_sha256": digest,
        }
    )


def _build_receipt(challenge, principal, signature, now):
    raw = _decode(signature, 64, "signature")
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_APPROVAL_SIGNATURE_POLICY,
        "challenge": challenge,
        "principal": principal,
        "signature_algorithm": "ed25519",
        "signature_base64": signature,
        "signature_sha256": hashlib.sha256(raw).hexdigest(),
        "verified_at": now.isoformat(),
        "signature_verified": True,
        "fresh_signature_verified": True,
        "identity_binding_verified": True,
        "principal_key_current_at_verification": True,
        "role_binding_verified": True,
        "counts_toward_role_quorum": True,
        "prior_signature_reused": False,
        "decision_authority": False,
        "promotion_authority": False,
        "git_write_executed": False,
        "private_key_requested": False,
        "private_key_stored": False,
    }
    digest = _digest(
        {
            **core,
            "challenge": challenge.model_dump(mode="json"),
            "principal": principal.model_dump(mode="json"),
        }
    )
    return EvolutionRevalidationApprovalSignatureReceipt.model_validate(
        {**core, "receipt_id": f"evreapprovalsig_{digest[:24]}", "receipt_sha256": digest}
    )


def _match(challenge, response, requirement, principal):
    payload = challenge.payload
    if not (
        payload.approval_response_id == response.receipt_id
        and payload.approval_response_sha256 == response.receipt_sha256
        and payload.requirement_id == requirement.requirement_id
        and payload.requirement_sha256 == requirement.requirement_sha256
        and payload.source_signable_payload_sha256 == requirement.signable_payload_sha256
        and payload.principal_event_id == principal.event_id
        and payload.principal_event_sha256 == principal.event_sha256
        and payload.key_id == principal.key_id
        and payload.key_generation == principal.key_generation
        and payload.public_key_sha256 == principal.public_key_sha256
        and payload.role in principal.roles
    ):
        raise EvolutionRevalidationApprovalSignatureError(
            "fresh_signature_authority_changed",
            "Fresh Signature authority 已变化，必须重新创建 challenge。",
        )


def _receipt_view(receipt, response, requirement, principal, now):
    payload = receipt.challenge.payload
    requirement_current = bool(
        requirement.requirement_id == payload.requirement_id
        and requirement.requirement_sha256 == payload.requirement_sha256
    )
    current = requirement_current and _aware(requirement.expires_at) > now
    response_current = (
        response is not None
        and response.receipt_id == payload.approval_response_id
        and response.receipt_sha256 == payload.approval_response_sha256
    )
    principal_active = principal.state is EvolutionApprovalPrincipalState.ACTIVE
    principal_key_current = (
        principal.event_id == payload.principal_event_id
        and principal.key_generation == payload.key_generation
        and principal.key_id == payload.key_id
    )
    role_current = payload.role in principal.roles
    return EvolutionRevalidationApprovalSignatureReceiptView(
        receipt=receipt,
        requirement_current=requirement_current,
        requirement_expired=not current,
        response_current=response_current,
        principal_active=principal_active,
        principal_key_current=principal_key_current,
        role_binding_current=role_current,
        eligible_for_decision_aggregation=current
        and response_current
        and principal_active
        and principal_key_current
        and role_current,
    )


def _verify(principal, signature: bytes, payload: bytes) -> None:
    try:
        Ed25519PublicKey.from_public_bytes(
            _decode(principal.public_key_base64, 32, "public key")
        ).verify(signature, payload)
    except (InvalidSignature, ValueError) as exc:
        raise EvolutionRevalidationApprovalSignatureError(
            "fresh_signature_invalid", "Ed25519 signature 无效或未绑定 exact Fresh Challenge。"
        ) from exc


def _normalize_signature(value: str) -> str:
    try:
        raw = _decode(value, 64, "signature")
    except (TypeError, ValueError) as exc:
        raise EvolutionRevalidationApprovalSignatureError(
            "fresh_signature_encoding_invalid",
            "Fresh Signature Base64 或长度无效。",
        ) from exc
    canonical = base64.b64encode(raw).decode()
    if not hmac.compare_digest(value, canonical):
        raise EvolutionRevalidationApprovalSignatureError(
            "fresh_signature_encoding_invalid", "Fresh Signature 必须使用 canonical Base64。"
        )
    return canonical


def _decode(value: str, size: int, label: str) -> bytes:
    try:
        raw = base64.b64decode(value, validate=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} Base64 无效。") from exc
    if len(raw) != size:
        raise ValueError(f"{label} 长度无效。")
    return raw


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("时间必须包含时区。")
    return parsed.astimezone(UTC)


def _canonical(payload: object) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _digest(payload: object) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _bounded(encoded: str) -> None:
    if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
        raise EvolutionRevalidationApprovalSignatureError(
            "fresh_signature_oversized", "Fresh Signature artifact 超过 256 KiB。"
        )


def _challenge(value: object) -> EvolutionRevalidationApprovalSignatureChallenge:
    return EvolutionRevalidationApprovalSignatureChallenge.model_validate_json(value)


def _receipt(value: object) -> EvolutionRevalidationApprovalSignatureReceipt:
    return EvolutionRevalidationApprovalSignatureReceipt.model_validate_json(value)


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        """CREATE TABLE IF NOT EXISTS
        evolution_revalidation_approval_signature_challenges (
        challenge_id TEXT PRIMARY KEY,
        approval_response_id TEXT NOT NULL,
        requirement_id TEXT NOT NULL,
        principal_id TEXT NOT NULL,
        principal_event_id TEXT NOT NULL,
        key_generation INTEGER NOT NULL,
        attempt INTEGER NOT NULL,
        status TEXT NOT NULL,
        closed_by_receipt_id TEXT NOT NULL DEFAULT '',
        challenge_json TEXT NOT NULL,
        UNIQUE(approval_response_id, principal_id, attempt))"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS
        evolution_revalidation_approval_signature_receipts (
        receipt_id TEXT PRIMARY KEY,
        approval_response_id TEXT NOT NULL UNIQUE,
        requirement_id TEXT NOT NULL,
        principal_id TEXT NOT NULL,
        role TEXT NOT NULL,
        receipt_json TEXT NOT NULL,
        verified_at TEXT NOT NULL)"""
    )
    await db.commit()


async def _require_current_dependencies(
    db: aiosqlite.Connection,
    receipt: EvolutionRevalidationApprovalSignatureReceipt,
) -> None:
    payload = receipt.challenge.payload
    requirement = await (
        await db.execute(
            "SELECT requirement_sha256, promotion_input_id FROM "
            "evolution_revalidation_approval_requirements WHERE requirement_id = ?",
            (payload.requirement_id,),
        )
    ).fetchone()
    response = await (
        await db.execute(
            "SELECT receipt_sha256, requirement_sha256, promotion_input_id, role, "
            "response FROM evolution_revalidation_approval_responses "
            "WHERE receipt_id = ?",
            (payload.approval_response_id,),
        )
    ).fetchone()
    principal = await (
        await db.execute(
            "SELECT state, latest_event_id, latest_event_sha256, key_id, "
            "key_generation, public_key_sha256, roles_json FROM "
            "evolution_approval_principals "
            "WHERE workspace_root = ? AND principal_id = ?",
            (payload.workspace_root, payload.principal_id),
        )
    ).fetchone()
    try:
        roles = () if principal is None else tuple(json.loads(principal["roles_json"]))
    except (TypeError, ValueError):
        roles = ()
    if not (
        requirement is not None
        and requirement["requirement_sha256"] == payload.requirement_sha256
        and requirement["promotion_input_id"] == payload.promotion_input_id
        and response is not None
        and response["receipt_sha256"] == payload.approval_response_sha256
        and response["requirement_sha256"] == payload.requirement_sha256
        and response["promotion_input_id"] == payload.promotion_input_id
        and response["role"] == payload.role.value
        and response["response"] == "approve"
        and principal is not None
        and principal["state"] == EvolutionApprovalPrincipalState.ACTIVE.value
        and principal["latest_event_id"] == payload.principal_event_id
        and principal["latest_event_sha256"] == payload.principal_event_sha256
        and principal["key_id"] == payload.key_id
        and principal["key_generation"] == payload.key_generation
        and principal["public_key_sha256"] == payload.public_key_sha256
        and payload.role.value in roles
    ):
        await db.rollback()
        raise EvolutionRevalidationApprovalSignatureError(
            "fresh_signature_atomic_authority_mismatch",
            "Fresh Signature 的 Requirement、Response 或 Principal authority 已变化。",
        )


__all__ = [
    "EVOLUTION_REVALIDATION_APPROVAL_SIGNATURE_POLICY",
    "EVOLUTION_REVALIDATION_PROFESSIONAL_SIGNATURE_DOMAIN",
    "EvolutionRevalidationApprovalSignatureChallenge",
    "EvolutionRevalidationApprovalSignatureChallengeView",
    "EvolutionRevalidationApprovalSignatureError",
    "EvolutionRevalidationApprovalSignaturePayload",
    "EvolutionRevalidationApprovalSignatureReceipt",
    "EvolutionRevalidationApprovalSignatureReceiptView",
    "EvolutionRevalidationApprovalSignatureService",
    "EvolutionRevalidationApprovalSignatureStatus",
    "EvolutionRevalidationApprovalSignatureStore",
]
