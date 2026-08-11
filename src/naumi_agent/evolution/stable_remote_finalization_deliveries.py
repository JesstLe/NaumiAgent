"""Durable delivery and bounded recovery for remote stable finalization."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.stable_remote_finalizations import (
    EvolutionStableRemoteFinalizationError,
    EvolutionStableRemoteFinalizationExecutionPackage,
    EvolutionStableRemoteFinalizationReceipt,
    EvolutionStableRemoteFinalizationService,
    EvolutionStableRemoteFinalizationSubmission,
    execute_stable_remote_finalization,
    recover_stable_remote_finalization_submission,
)
from naumi_agent.release.installation_keys import (
    RELEASE_INSTALLATION_FINALIZATION_DELIVERY_ACK_SIGNATURE_DOMAIN,
    ReleaseInstallationKeyError,
    ReleaseInstallationKeyService,
    ReleaseInstallationSignature,
    verify_release_installation_signature,
)
from naumi_agent.release.population_registry import ReleaseManagedInstallationCredential
from naumi_agent.release.rollout_control_keys import (
    ReleaseRolloutControlTrustPolicyDocument,
)
from naumi_agent.release.slots import ReleaseSlotStore, ReleaseStableMemberFinalization

EVOLUTION_STABLE_REMOTE_FINALIZATION_DELIVERY_POLICY = (
    "evolution-stable-remote-finalization-delivery-v1"
)
_MAX_ARTIFACT_BYTES = 768 * 1024
_MAX_ENCODED_CHARS = ((_MAX_ARTIFACT_BYTES + 2) // 3) * 4
_SHA256_RE = r"^[0-9a-f]{64}$"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class EvolutionStableRemoteFinalizationDeliveryPackage(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-stable-remote-finalization-delivery-v1"
    ] = EVOLUTION_STABLE_REMOTE_FINALIZATION_DELIVERY_POLICY
    delivery_id: str = Field(pattern=r"^evstableremotedelivery_[0-9a-f]{24}$")
    delivery_sha256: str = Field(pattern=_SHA256_RE)
    execution_package: EvolutionStableRemoteFinalizationExecutionPackage
    enqueued_at: str = Field(min_length=1, max_length=100)
    installation_member_id: str = Field(pattern=r"^relpopmember_[0-9a-f]{24}$")
    execution_authority_expanded: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        _aware(self.enqueued_at)
        grant = self.execution_package.grant
        if self.installation_member_id != grant.installation_member_id:
            raise ValueError("Remote Finalization Delivery member binding 不一致。")
        digest = _digest(
            self.model_dump(mode="json", exclude={"delivery_id", "delivery_sha256"})
        )
        if self.delivery_sha256 != digest or self.delivery_id != (
            f"evstableremotedelivery_{digest[:24]}"
        ):
            raise ValueError("Remote Finalization Delivery identity 不一致。")
        return self


class EvolutionStableRemoteFinalizationDeliveryAckPayload(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-stable-remote-finalization-delivery-v1"
    ] = EVOLUTION_STABLE_REMOTE_FINALIZATION_DELIVERY_POLICY
    delivery_id: str = Field(pattern=r"^evstableremotedelivery_[0-9a-f]{24}$")
    delivery_sha256: str = Field(pattern=_SHA256_RE)
    grant_id: str = Field(pattern=r"^evstableremotefinalgrant_[0-9a-f]{24}$")
    grant_sha256: str = Field(pattern=_SHA256_RE)
    installation_member_id: str = Field(pattern=r"^relpopmember_[0-9a-f]{24}$")
    received_at: str = Field(min_length=1, max_length=100)
    package_verified: Literal[True] = True
    durable_journal_written: Literal[True] = True
    writer_executed: Literal[False] = False
    result_authority: Literal[False] = False

    def canonical_bytes(self) -> bytes:
        return _canonical(self.model_dump(mode="json"))


class EvolutionStableRemoteFinalizationDeliveryAck(_StrictModel):
    payload: EvolutionStableRemoteFinalizationDeliveryAckPayload
    signature: ReleaseInstallationSignature

    @model_validator(mode="after")
    def _binding(self) -> Self:
        payload = self.payload.canonical_bytes()
        if not (
            self.signature.domain
            == RELEASE_INSTALLATION_FINALIZATION_DELIVERY_ACK_SIGNATURE_DOMAIN
            and self.signature.installation_member_id
            == self.payload.installation_member_id
            and self.signature.payload_sha256 == hashlib.sha256(payload).hexdigest()
            and self.signature.payload_bytes == len(payload)
            and _aware(self.payload.received_at) <= _aware(self.signature.signed_at)
        ):
            raise ValueError("Remote Finalization Delivery ACK signature binding 无效。")
        return self


class EvolutionStableRemoteFinalizationDeliveryEvent(_StrictModel):
    schema_version: Literal[1] = 1
    event_id: str = Field(pattern=r"^evstableremotedeliveryevent_[0-9a-f]{24}$")
    event_sha256: str = Field(pattern=_SHA256_RE)
    delivery_id: str = Field(pattern=r"^evstableremotedelivery_[0-9a-f]{24}$")
    sequence: int = Field(ge=1, le=1_000_000)
    state: Literal[
        "queued", "in_flight", "acknowledged", "completed", "dead_letter"
    ]
    owner_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    claim_epoch: int = Field(ge=0, le=1_000_000)
    attempt_count: int = Field(ge=0, le=1_000_000)
    lease_expires_at: str | None = Field(default=None, min_length=1, max_length=100)
    next_attempt_at: str = Field(min_length=1, max_length=100)
    ack_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    receipt_id: str | None = Field(
        default=None, pattern=r"^evstableremotefinalreceipt_[0-9a-f]{24}$"
    )
    failure_code: str | None = Field(default=None, min_length=1, max_length=128)
    occurred_at: str = Field(min_length=1, max_length=100)
    previous_event_sha256: str | None = Field(default=None, pattern=_SHA256_RE)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        _aware(self.next_attempt_at)
        _aware(self.occurred_at)
        if self.lease_expires_at is not None:
            _aware(self.lease_expires_at)
        if self.state == "in_flight" and not (
            self.owner_sha256 and self.lease_expires_at
        ):
            raise ValueError("in_flight Delivery event 缺少 owner/lease。")
        if self.state != "in_flight" and (
            self.owner_sha256 is not None or self.lease_expires_at is not None
        ):
            raise ValueError("非 in_flight Delivery event 不得携带 owner/lease。")
        if self.state in {"acknowledged", "completed"} and self.ack_sha256 is None:
            raise ValueError("ACK 后 Delivery event 必须绑定 ACK。")
        if (
            self.state not in {"acknowledged", "completed"}
            and self.ack_sha256 is not None
        ):
            raise ValueError("ACK 前 Delivery event 不得绑定 ACK。")
        if self.state == "completed" and self.receipt_id is None:
            raise ValueError("completed Delivery event 必须绑定 finalization receipt。")
        if self.state != "completed" and self.receipt_id is not None:
            raise ValueError("非 completed Delivery event 不得绑定 finalization receipt。")
        if self.state == "dead_letter" and self.failure_code is None:
            raise ValueError("dead-letter Delivery event 必须绑定 failure code。")
        if (
            self.state not in {"queued", "dead_letter"}
            and self.failure_code is not None
        ):
            raise ValueError("仅 retry/dead-letter Delivery event 可绑定 failure code。")
        core = self.model_dump(mode="json", exclude={"event_id", "event_sha256"})
        digest = _digest(core)
        if self.event_sha256 != digest or self.event_id != (
            f"evstableremotedeliveryevent_{digest[:24]}"
        ):
            raise ValueError("Remote Finalization Delivery event identity 不一致。")
        return self


class EvolutionStableRemoteFinalizationDeliveryView(_StrictModel):
    package: EvolutionStableRemoteFinalizationDeliveryPackage
    latest_event: EvolutionStableRemoteFinalizationDeliveryEvent
    ack: EvolutionStableRemoteFinalizationDeliveryAck | None = None
    receipt: EvolutionStableRemoteFinalizationReceipt | None = None


class EvolutionStableRemoteFinalizationTargetJournalEntry(_StrictModel):
    """Strict public projection of one installation-local journal row."""

    package: EvolutionStableRemoteFinalizationDeliveryPackage
    state: Literal["received", "writer_committed", "result_signed"]
    ack: EvolutionStableRemoteFinalizationDeliveryAck
    writer: ReleaseStableMemberFinalization | None = None
    submission: EvolutionStableRemoteFinalizationSubmission | None = None

    @model_validator(mode="after")
    def _state_binding(self) -> Self:
        if not (
            self.ack.payload.delivery_id == self.package.delivery_id
            and self.ack.payload.delivery_sha256 == self.package.delivery_sha256
            and self.ack.payload.grant_id
            == self.package.execution_package.grant.grant_id
            and self.ack.payload.grant_sha256
            == self.package.execution_package.grant.grant_sha256
            and self.ack.payload.installation_member_id
            == self.package.installation_member_id
        ):
            raise ValueError("Target Journal ACK 与 package 不一致。")
        if self.state == "received" and (
            self.writer is not None or self.submission is not None
        ):
            raise ValueError("received Target Journal 不得携带 writer/result。")
        if self.state == "writer_committed" and (
            self.writer is None or self.submission is not None
        ):
            raise ValueError("writer_committed Target Journal artifact 不完整。")
        if self.state == "result_signed" and (
            self.writer is None or self.submission is None
        ):
            raise ValueError("result_signed Target Journal artifact 不完整。")
        if self.writer is not None and self.writer.authority.authority_id != (
            self.package.execution_package.grant.authorization_id
        ):
            raise ValueError("Target Journal writer 与 package authority 不一致。")
        if self.submission is not None and not (
            self.submission.result.release_finalization == self.writer
            and self.submission.result.grant_id
            == self.package.execution_package.grant.grant_id
            and self.submission.result.installation_member_id
            == self.package.installation_member_id
        ):
            raise ValueError("Target Journal submission 与 package/writer 不一致。")
        return self


class EvolutionStableRemoteFinalizationDeliveryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionStableRemoteFinalizationDeliveryStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def enqueue(
        self,
        execution_package: EvolutionStableRemoteFinalizationExecutionPackage,
        *,
        enqueued_at: str,
    ) -> EvolutionStableRemoteFinalizationDeliveryView:
        now = _aware(enqueued_at)
        package = _build_delivery_package(execution_package, now)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT * FROM evolution_stable_remote_finalization_deliveries "
                    "WHERE grant_id = ?",
                    (execution_package.grant.grant_id,),
                )
            ).fetchone()
            if row is None:
                event = _event(
                    package=package,
                    sequence=1,
                    state="queued",
                    claim_epoch=0,
                    attempt_count=0,
                    next_attempt_at=now,
                    occurred_at=now,
                )
                await db.execute(
                    "INSERT INTO evolution_stable_remote_finalization_deliveries "
                    "(delivery_id, grant_id, package_json, latest_event_json, ack_json, "
                    "receipt_json) VALUES (?, ?, ?, ?, NULL, NULL)",
                    (
                        package.delivery_id,
                        package.execution_package.grant.grant_id,
                        package.model_dump_json(),
                        event.model_dump_json(),
                    ),
                )
                await _insert_event(db, event)
                await db.commit()
                return EvolutionStableRemoteFinalizationDeliveryView(
                    package=package, latest_event=event
                )
            await db.rollback()
            view = _restore_view(row)
            await _verify_chain(db, view)
            if view.package.execution_package != execution_package:
                raise EvolutionStableRemoteFinalizationDeliveryError(
                    "stable_remote_delivery_conflict",
                    "同一 Delivery identity 已绑定不同 Execution Package。",
                )
            return view

    async def get(
        self, delivery_id: str
    ) -> EvolutionStableRemoteFinalizationDeliveryView | None:
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await _row(db, _delivery_id(delivery_id))
            if row is None:
                return None
            view = _restore_view(row)
            await _verify_chain(db, view)
            return view

    async def claim(
        self,
        *,
        owner_id: str,
        now: str,
        lease_seconds: int = 60,
    ) -> EvolutionStableRemoteFinalizationDeliveryView | None:
        owner = _owner_sha256(owner_id)
        timestamp = _aware(now)
        if not 3 <= lease_seconds <= 300:
            raise ValueError("Delivery claim lease 必须为 3..300 秒。")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            rows = await (
                await db.execute(
                    "SELECT * FROM evolution_stable_remote_finalization_deliveries "
                    "ORDER BY rowid LIMIT 1000"
                )
            ).fetchall()
            for row in rows:
                view = _restore_view(row)
                await _verify_chain(db, view)
                event = view.latest_event
                due = _aware(event.next_attempt_at) <= timestamp
                expired = event.lease_expires_at is not None and (
                    _aware(event.lease_expires_at) <= timestamp
                )
                if event.state == "queued" and due or (
                    event.state == "in_flight" and expired
                ):
                    claimed = _event(
                        package=view.package,
                        sequence=event.sequence + 1,
                        state="in_flight",
                        owner_sha256=owner,
                        claim_epoch=event.claim_epoch + 1,
                        attempt_count=event.attempt_count + 1,
                        lease_expires_at=timestamp + timedelta(seconds=lease_seconds),
                        next_attempt_at=timestamp,
                        occurred_at=timestamp,
                        previous_event_sha256=event.event_sha256,
                    )
                    await _update_event(db, view.package.delivery_id, claimed)
                    await db.commit()
                    return view.model_copy(update={"latest_event": claimed})
            await db.rollback()
            return None

    async def retry(
        self,
        *,
        delivery_id: str,
        owner_id: str,
        claim_epoch: int,
        failure_code: str,
        now: str,
        base_seconds: float = 5,
        max_seconds: float = 300,
    ) -> EvolutionStableRemoteFinalizationDeliveryView:
        timestamp = _aware(now)
        owner = _owner_sha256(owner_id)
        code = _failure_code(failure_code)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            row = await _row(db, _delivery_id(delivery_id))
            if row is None:
                raise EvolutionStableRemoteFinalizationDeliveryError(
                    "stable_remote_delivery_missing", "Delivery 不存在。"
                )
            view = _restore_view(row)
            await _verify_chain(db, view)
            event = view.latest_event
            if not (
                event.state == "in_flight"
                and event.owner_sha256 == owner
                and event.claim_epoch == claim_epoch
                and event.lease_expires_at is not None
                and timestamp < _aware(event.lease_expires_at)
            ):
                raise EvolutionStableRemoteFinalizationDeliveryError(
                    "stable_remote_delivery_claim_fenced", "Delivery retry claim 已失效。"
                )
            delay = _retry_delay(
                event.attempt_count, base_seconds=base_seconds, max_seconds=max_seconds
            )
            queued = _event(
                package=view.package,
                sequence=event.sequence + 1,
                state="queued",
                claim_epoch=event.claim_epoch,
                attempt_count=event.attempt_count,
                next_attempt_at=timestamp + timedelta(seconds=delay),
                occurred_at=timestamp,
                previous_event_sha256=event.event_sha256,
                failure_code=code,
            )
            await _update_event(db, view.package.delivery_id, queued)
            await db.commit()
            return view.model_copy(update={"latest_event": queued})

    async def dead_letter(
        self,
        *,
        delivery_id: str,
        owner_id: str,
        claim_epoch: int,
        failure_code: str,
        now: str,
    ) -> EvolutionStableRemoteFinalizationDeliveryView:
        """Fence the live claim and make the exhausted record non-claimable."""
        timestamp = _aware(now)
        owner = _owner_sha256(owner_id)
        code = _failure_code(failure_code)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            row = await _row(db, _delivery_id(delivery_id))
            if row is None:
                raise EvolutionStableRemoteFinalizationDeliveryError(
                    "stable_remote_delivery_missing", "Delivery 不存在。"
                )
            view = _restore_view(row)
            await _verify_chain(db, view)
            event = view.latest_event
            if not (
                event.state == "in_flight"
                and event.owner_sha256 == owner
                and event.claim_epoch == claim_epoch
                and event.lease_expires_at is not None
                and timestamp < _aware(event.lease_expires_at)
            ):
                raise EvolutionStableRemoteFinalizationDeliveryError(
                    "stable_remote_delivery_claim_fenced",
                    "Delivery dead-letter claim 已失效。",
                )
            dead_letter = _event(
                package=view.package,
                sequence=event.sequence + 1,
                state="dead_letter",
                claim_epoch=event.claim_epoch,
                attempt_count=event.attempt_count,
                next_attempt_at=timestamp,
                occurred_at=timestamp,
                previous_event_sha256=event.event_sha256,
                failure_code=code,
            )
            await _update_event(db, view.package.delivery_id, dead_letter)
            await db.commit()
            return view.model_copy(update={"latest_event": dead_letter})

    async def acknowledge(
        self,
        *,
        delivery_id: str,
        ack: EvolutionStableRemoteFinalizationDeliveryAck,
        credential: ReleaseManagedInstallationCredential,
        now: str,
    ) -> EvolutionStableRemoteFinalizationDeliveryView:
        timestamp = _aware(now)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            row = await _row(db, _delivery_id(delivery_id))
            if row is None:
                raise EvolutionStableRemoteFinalizationDeliveryError(
                    "stable_remote_delivery_missing", "Delivery 不存在。"
                )
            view = _restore_view(row)
            await _verify_chain(db, view)
            ack_sha = _digest(ack.model_dump(mode="json"))
            if view.ack is not None:
                await db.rollback()
                if view.ack == ack:
                    return view
                raise EvolutionStableRemoteFinalizationDeliveryError(
                    "stable_remote_delivery_ack_conflict", "Delivery 已绑定不同 ACK。"
                )
            if not (
                view.latest_event.state == "in_flight"
                and view.latest_event.lease_expires_at is not None
                and timestamp < _aware(view.latest_event.lease_expires_at)
                and ack.payload.delivery_id == view.package.delivery_id
                and ack.payload.delivery_sha256 == view.package.delivery_sha256
                and ack.payload.grant_id
                == view.package.execution_package.grant.grant_id
                and ack.payload.grant_sha256
                == view.package.execution_package.grant.grant_sha256
                and ack.payload.installation_member_id
                == view.package.installation_member_id
                and _aware(ack.payload.received_at) <= timestamp
                and _aware(ack.signature.signed_at)
                < _aware(view.package.execution_package.grant.expires_at)
            ):
                raise EvolutionStableRemoteFinalizationDeliveryError(
                    "stable_remote_delivery_ack_binding_invalid",
                    "Delivery ACK 与 current in-flight package 不一致。",
                )
            try:
                verify_release_installation_signature(
                    credential=credential,
                    payload=ack.payload.canonical_bytes(),
                    artifact=ack.signature,
                    expected_domain=(
                        RELEASE_INSTALLATION_FINALIZATION_DELIVERY_ACK_SIGNATURE_DOMAIN
                    ),
                )
            except ReleaseInstallationKeyError as exc:
                raise EvolutionStableRemoteFinalizationDeliveryError(
                    exc.code, "Delivery ACK installation signature 无效。"
                ) from exc
            acknowledged = _event(
                package=view.package,
                sequence=view.latest_event.sequence + 1,
                state="acknowledged",
                claim_epoch=view.latest_event.claim_epoch,
                attempt_count=view.latest_event.attempt_count,
                next_attempt_at=timestamp,
                occurred_at=timestamp,
                previous_event_sha256=view.latest_event.event_sha256,
                ack_sha256=ack_sha,
            )
            await db.execute(
                "UPDATE evolution_stable_remote_finalization_deliveries SET "
                "latest_event_json = ?, ack_json = ? WHERE delivery_id = ?",
                (acknowledged.model_dump_json(), ack.model_dump_json(), delivery_id),
            )
            await _insert_event(db, acknowledged)
            await db.commit()
            return view.model_copy(update={"latest_event": acknowledged, "ack": ack})

    async def complete(
        self,
        *,
        delivery_id: str,
        receipt: EvolutionStableRemoteFinalizationReceipt,
        completed_at: str,
    ) -> EvolutionStableRemoteFinalizationDeliveryView:
        timestamp = _aware(completed_at)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            row = await _row(db, _delivery_id(delivery_id))
            if row is None:
                raise EvolutionStableRemoteFinalizationDeliveryError(
                    "stable_remote_delivery_missing", "Delivery 不存在。"
                )
            view = _restore_view(row)
            await _verify_chain(db, view)
            if view.latest_event.state == "completed":
                await db.rollback()
                if view.receipt == receipt:
                    return view
                raise EvolutionStableRemoteFinalizationDeliveryError(
                    "stable_remote_delivery_receipt_conflict",
                    "Delivery 已绑定不同 finalization receipt。",
                )
            if not (
                view.latest_event.state == "acknowledged"
                and view.ack is not None
                and receipt.execution_package == view.package.execution_package
            ):
                raise EvolutionStableRemoteFinalizationDeliveryError(
                    "stable_remote_delivery_completion_invalid",
                    "Delivery 尚未 ACK 或 Receipt 不属于该 package。",
                )
            completed = _event(
                package=view.package,
                sequence=view.latest_event.sequence + 1,
                state="completed",
                claim_epoch=view.latest_event.claim_epoch,
                attempt_count=view.latest_event.attempt_count,
                next_attempt_at=timestamp,
                occurred_at=timestamp,
                previous_event_sha256=view.latest_event.event_sha256,
                ack_sha256=_digest(view.ack.model_dump(mode="json")),
                receipt_id=receipt.receipt_id,
            )
            await db.execute(
                "UPDATE evolution_stable_remote_finalization_deliveries SET "
                "latest_event_json = ?, receipt_json = ? WHERE delivery_id = ?",
                (completed.model_dump_json(), receipt.model_dump_json(), delivery_id),
            )
            await _insert_event(db, completed)
            await db.commit()
            return view.model_copy(
                update={"latest_event": completed, "receipt": receipt}
            )


class EvolutionStableRemoteFinalizationTargetJournal:
    """Installation-local journal; it stores public protocol artifacts, never secrets."""

    def __init__(self, release_root: str | Path) -> None:
        self.release_root = Path(release_root).expanduser().resolve()
        self.db_path = self.release_root / "state" / "stable-finalization-delivery.sqlite3"

    def receive(
        self,
        *,
        package: EvolutionStableRemoteFinalizationDeliveryPackage,
        trust_policy: ReleaseRolloutControlTrustPolicyDocument,
        credential: ReleaseManagedInstallationCredential,
        installation_key_service: ReleaseInstallationKeyService,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> EvolutionStableRemoteFinalizationDeliveryAck:
        from naumi_agent.evolution.stable_remote_finalizations import (
            verify_stable_remote_finalization_execution_package,
        )

        now = _aware(clock())
        verify_stable_remote_finalization_execution_package(
            trust_policy=trust_policy,
            package=package.execution_package,
            expected_member_id=credential.payload.member_id,
            now=now,
        )
        if installation_key_service.release_root != self.release_root:
            raise EvolutionStableRemoteFinalizationDeliveryError(
                "stable_remote_delivery_journal_root_mismatch",
                "Target Journal 与 installation key 不属于同一 release root。",
            )
        existing = self._artifact(package, kind="ack")
        if existing is not None:
            return EvolutionStableRemoteFinalizationDeliveryAck.model_validate_json(
                existing
            )
        payload = EvolutionStableRemoteFinalizationDeliveryAckPayload(
            delivery_id=package.delivery_id,
            delivery_sha256=package.delivery_sha256,
            grant_id=package.execution_package.grant.grant_id,
            grant_sha256=package.execution_package.grant.grant_sha256,
            installation_member_id=package.installation_member_id,
            received_at=now.isoformat(),
        )
        signature = installation_key_service.sign_remote_finalization_delivery_ack(
            credential=credential, payload=payload.canonical_bytes()
        )
        ack = EvolutionStableRemoteFinalizationDeliveryAck(
            payload=payload, signature=signature
        )
        self._record(package=package, state="received", artifact=ack.model_dump_json())
        return ack

    def execute(
        self,
        *,
        package: EvolutionStableRemoteFinalizationDeliveryPackage,
        trust_policy: ReleaseRolloutControlTrustPolicyDocument,
        credential: ReleaseManagedInstallationCredential,
        release_slot_store: ReleaseSlotStore,
        installation_key_service: ReleaseInstallationKeyService,
        recover_existing_only: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> EvolutionStableRemoteFinalizationSubmission:
        existing = self._artifact(package, kind="result")
        if existing is not None:
            return EvolutionStableRemoteFinalizationSubmission.model_validate_json(
                existing
            )
        if self._state(package.delivery_id) is None:
            raise EvolutionStableRemoteFinalizationDeliveryError(
                "stable_remote_delivery_not_received",
                "Delivery 必须先完成 package verify、durable journal 与 ACK。",
            )
        executor = (
            recover_stable_remote_finalization_submission
            if recover_existing_only
            else execute_stable_remote_finalization
        )
        try:
            submission = executor(
                package=package.execution_package,
                trust_policy=trust_policy,
                credential=credential,
                release_slot_store=release_slot_store,
                installation_key_service=installation_key_service,
                clock=clock,
            )
        except EvolutionStableRemoteFinalizationError:
            finalization = release_slot_store.get_stable_member_finalization(
                package.execution_package.grant.authorization_id
            )
            if finalization is not None:
                self._record(
                    package=package,
                    state="writer_committed",
                    artifact=finalization.model_dump_json(),
                )
            raise
        self._record(
            package=package,
            state="writer_committed",
            artifact=submission.result.release_finalization.model_dump_json(),
        )
        self._record(
            package=package,
            state="result_signed",
            artifact=submission.model_dump_json(),
        )
        return submission

    def get(
        self, delivery_id: str
    ) -> EvolutionStableRemoteFinalizationTargetJournalEntry | None:
        """Read one row while verifying every stored artifact digest and binding."""
        if not self.db_path.is_file():
            return None
        with sqlite3.connect(self.db_path) as db:
            _ensure_target_schema(db)
            row = db.execute(
                "SELECT package_json, state, ack_json, ack_sha256, writer_json, "
                "writer_sha256, result_json, result_sha256 FROM "
                "stable_finalization_delivery_journal WHERE delivery_id = ?",
                (_delivery_id(delivery_id),),
            ).fetchone()
        return None if row is None else self._restore_entry(row)

    def list_entries(
        self,
        *,
        states: tuple[Literal["received", "writer_committed", "result_signed"], ...],
        limit: int = 20,
    ) -> tuple[EvolutionStableRemoteFinalizationTargetJournalEntry, ...]:
        """Return a bounded deterministic prefix for recovery workers."""
        if not states or any(
            item not in {"received", "writer_committed", "result_signed"}
            for item in states
        ):
            raise ValueError("Target Journal states 无效。")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("Target Journal limit 必须为 1..1000。")
        if not self.db_path.is_file():
            return ()
        placeholders = ",".join("?" for _ in states)
        with sqlite3.connect(self.db_path) as db:
            _ensure_target_schema(db)
            rows = db.execute(
                "SELECT package_json, state, ack_json, ack_sha256, writer_json, "
                "writer_sha256, result_json, result_sha256 FROM "
                "stable_finalization_delivery_journal WHERE state IN ("
                f"{placeholders}) ORDER BY delivery_id LIMIT ?",  # noqa: S608
                (*states, limit),
            ).fetchall()
        return tuple(self._restore_entry(row) for row in rows)

    @staticmethod
    def _restore_entry(row) -> EvolutionStableRemoteFinalizationTargetJournalEntry:
        (
            package_json, state, ack_json, ack_sha, writer_json, writer_sha,
            result_json, result_sha,
        ) = row
        for label, artifact, digest in (
            ("ACK", ack_json, ack_sha),
            ("writer", writer_json, writer_sha),
            ("result", result_json, result_sha),
        ):
            if (artifact is None) != (digest is None) or (
                artifact is not None
                and hashlib.sha256(str(artifact).encode()).hexdigest() != digest
            ):
                raise EvolutionStableRemoteFinalizationDeliveryError(
                    "stable_remote_delivery_journal_corrupt",
                    f"Target Journal {label} digest 无效。",
                )
        try:
            return EvolutionStableRemoteFinalizationTargetJournalEntry(
                package=EvolutionStableRemoteFinalizationDeliveryPackage.model_validate_json(
                    package_json
                ),
                state=state,
                ack=EvolutionStableRemoteFinalizationDeliveryAck.model_validate_json(
                    ack_json
                ),
                writer=None if writer_json is None else (
                    ReleaseStableMemberFinalization.model_validate_json(writer_json)
                ),
                submission=None if result_json is None else (
                    EvolutionStableRemoteFinalizationSubmission.model_validate_json(
                        result_json
                    )
                ),
            )
        except (TypeError, ValueError) as exc:
            raise EvolutionStableRemoteFinalizationDeliveryError(
                "stable_remote_delivery_journal_corrupt",
                "Target Journal artifact 结构或绑定无效。",
            ) from exc

    def _state(self, delivery_id: str) -> str | None:
        if not self.db_path.is_file():
            return None
        with sqlite3.connect(self.db_path) as db:
            _ensure_target_schema(db)
            row = db.execute(
                "SELECT state FROM stable_finalization_delivery_journal WHERE delivery_id = ?",
                (_delivery_id(delivery_id),),
            ).fetchone()
        return None if row is None else str(row[0])

    def _artifact(self, package, *, kind: Literal["ack", "result"]) -> str | None:
        entry = self.get(package.delivery_id)
        if entry is None:
            return None
        if entry.package != package:
            raise EvolutionStableRemoteFinalizationDeliveryError(
                "stable_remote_delivery_journal_conflict",
                "Target Journal 已绑定不同 package。",
            )
        artifact = entry.ack if kind == "ack" else entry.submission
        return None if artifact is None else artifact.model_dump_json()

    def _record(self, *, package, state: str, artifact: str) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_sha = hashlib.sha256(artifact.encode()).hexdigest()
        with sqlite3.connect(self.db_path) as db:
            _ensure_target_schema(db)
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT package_json, state, ack_sha256, writer_sha256, "
                "result_sha256 FROM "
                "stable_finalization_delivery_journal WHERE delivery_id = ?",
                (package.delivery_id,),
            ).fetchone()
            if row is not None:
                stored = EvolutionStableRemoteFinalizationDeliveryPackage.model_validate_json(
                    row[0]
                )
                if stored != package:
                    raise EvolutionStableRemoteFinalizationDeliveryError(
                        "stable_remote_delivery_journal_conflict",
                        "Target Journal 已绑定不同 package。",
                    )
                digest_index = {
                    "received": 2,
                    "writer_committed": 3,
                    "result_signed": 4,
                }[state]
                if row[1] == state and row[digest_index] == artifact_sha:
                    db.rollback()
                    return
                if (row[1], state) not in {
                    ("received", "writer_committed"),
                    ("writer_committed", "result_signed"),
                }:
                    raise EvolutionStableRemoteFinalizationDeliveryError(
                        "stable_remote_delivery_journal_transition_invalid",
                        "Target Journal 状态转换无效。",
                    )
                if state == "writer_committed":
                    db.execute(
                        "UPDATE stable_finalization_delivery_journal SET state = ?, "
                        "writer_json = ?, writer_sha256 = ? WHERE delivery_id = ?",
                        (state, artifact, artifact_sha, package.delivery_id),
                    )
                else:
                    db.execute(
                        "UPDATE stable_finalization_delivery_journal SET state = ?, "
                        "result_json = ?, result_sha256 = ? WHERE delivery_id = ?",
                        (state, artifact, artifact_sha, package.delivery_id),
                    )
            else:
                if state != "received":
                    raise EvolutionStableRemoteFinalizationDeliveryError(
                        "stable_remote_delivery_not_received",
                        "Target Journal 缺少 received 状态。",
                    )
                db.execute(
                    "INSERT INTO stable_finalization_delivery_journal "
                    "(delivery_id, package_json, state, ack_json, ack_sha256, "
                    "writer_json, writer_sha256, result_json, result_sha256) "
                    "VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL)",
                    (
                        package.delivery_id,
                        package.model_dump_json(),
                        state,
                        artifact,
                        artifact_sha,
                    ),
                )
            db.commit()


class EvolutionStableRemoteFinalizationDeliveryService:
    def __init__(
        self,
        *,
        finalization_service: EvolutionStableRemoteFinalizationService,
        store: EvolutionStableRemoteFinalizationDeliveryStore,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if finalization_service.store.db_path != store.db_path:
            raise ValueError("Finalization Delivery 必须与 Grant Store 共享 SQLite。")
        self.finalization_service = finalization_service
        self.store = store
        self.clock = clock

    async def queue(self, *, grant_id: str):
        package = await self.finalization_service.current_package(grant_id=grant_id)
        return await self.store.enqueue(package, enqueued_at=_aware(self.clock()).isoformat())

    async def acknowledge(self, *, delivery_id: str, ack_base64: str):
        ack = decode_stable_remote_finalization_delivery_ack(ack_base64)
        delivery = await self.store.get(delivery_id)
        if delivery is None:
            raise EvolutionStableRemoteFinalizationDeliveryError(
                "stable_remote_delivery_missing", "Delivery 不存在。"
            )
        receipt_id = (
            delivery.package.execution_package.authorization.authorization.probe_receipt_id
        )
        credential = await (
            self.finalization_service.authorization_service.probe_service
            .current_credential_for_probe(receipt_id=receipt_id)
        )
        return await self.store.acknowledge(
            delivery_id=delivery_id,
            ack=ack,
            credential=credential,
            now=_aware(self.clock()).isoformat(),
        )

    async def ingest_result(
        self,
        *,
        delivery_id: str,
        submission_base64: str,
        late_recovery: bool = False,
    ):
        delivery = await self.store.get(delivery_id)
        if delivery is None:
            raise EvolutionStableRemoteFinalizationDeliveryError(
                "stable_remote_delivery_missing", "Delivery 不存在。"
            )
        ingest = (
            self.finalization_service.ingest_late_recovery
            if late_recovery
            else self.finalization_service.ingest
        )
        view = await ingest(
            grant_id=delivery.package.execution_package.grant.grant_id,
            submission_base64=submission_base64,
        )
        return await self.store.complete(
            delivery_id=delivery_id,
            receipt=view.receipt,
            completed_at=_aware(self.clock()).isoformat(),
        )


def encode_stable_remote_finalization_delivery_package(package) -> str:
    return _encode(package.model_dump(mode="json"))


def decode_stable_remote_finalization_delivery_package(value: str):
    return _decode(value, EvolutionStableRemoteFinalizationDeliveryPackage)


def encode_stable_remote_finalization_delivery_ack(ack) -> str:
    return _encode(ack.model_dump(mode="json"))


def decode_stable_remote_finalization_delivery_ack(value: str):
    return _decode(value, EvolutionStableRemoteFinalizationDeliveryAck)


def render_stable_remote_finalization_delivery(view, *, include_package=False) -> str:
    event = view.latest_event
    lines = [
        "## Remote Stable Finalization Delivery",
        "",
        f"- 状态：**{event.state}**",
        f"- Delivery：`{view.package.delivery_id}`",
        f"- Grant：`{view.package.execution_package.grant.grant_id}`",
        f"- Attempt：`{event.attempt_count}`",
        f"- Claim epoch：`{event.claim_epoch}`",
        f"- Next attempt：`{event.next_attempt_at}`",
        f"- ACK：`{'present' if view.ack else 'missing'}`",
        f"- Finalization Receipt：`{view.receipt.receipt_id if view.receipt else 'missing'}`",
    ]
    if event.failure_code is not None:
        lines.append(f"- Failure code：`{event.failure_code}`")
    if include_package:
        lines.extend((
            "",
            "### Delivery Package Base64",
            "",
            encode_stable_remote_finalization_delivery_package(view.package),
        ))
    return "\n".join(lines)


def _build_delivery_package(execution_package, now):
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_STABLE_REMOTE_FINALIZATION_DELIVERY_POLICY,
        "execution_package": execution_package.model_dump(mode="json"),
        "enqueued_at": now.isoformat(),
        "installation_member_id": execution_package.grant.installation_member_id,
        "execution_authority_expanded": False,
    }
    digest = _digest(core)
    return EvolutionStableRemoteFinalizationDeliveryPackage.model_validate({
        **core,
        "delivery_id": f"evstableremotedelivery_{digest[:24]}",
        "delivery_sha256": digest,
    })


def _event(*, package, sequence, state, claim_epoch, attempt_count, next_attempt_at,
           occurred_at, owner_sha256=None, lease_expires_at=None, ack_sha256=None,
           receipt_id=None, failure_code=None, previous_event_sha256=None):
    core = {
        "schema_version": 1,
        "delivery_id": package.delivery_id,
        "sequence": sequence,
        "state": state,
        "owner_sha256": owner_sha256,
        "claim_epoch": claim_epoch,
        "attempt_count": attempt_count,
        "lease_expires_at": None if lease_expires_at is None else lease_expires_at.isoformat(),
        "next_attempt_at": next_attempt_at.isoformat(),
        "ack_sha256": ack_sha256,
        "receipt_id": receipt_id,
        "failure_code": failure_code,
        "occurred_at": occurred_at.isoformat(),
        "previous_event_sha256": previous_event_sha256,
    }
    digest = _digest(core)
    return EvolutionStableRemoteFinalizationDeliveryEvent.model_validate({
        **core,
        "event_id": f"evstableremotedeliveryevent_{digest[:24]}",
        "event_sha256": digest,
    })


async def _ensure_schema(db):
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_stable_remote_finalization_deliveries ("
        "delivery_id TEXT PRIMARY KEY, grant_id TEXT NOT NULL UNIQUE, package_json TEXT NOT NULL, "
        "latest_event_json TEXT NOT NULL, ack_json TEXT, receipt_json TEXT)"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_stable_remote_finalization_delivery_events ("
        "delivery_id TEXT NOT NULL, sequence INTEGER NOT NULL, event_json TEXT NOT NULL, "
        "PRIMARY KEY(delivery_id, sequence))"
    )


def _ensure_target_schema(db):
    db.execute(
        "CREATE TABLE IF NOT EXISTS stable_finalization_delivery_journal ("
        "delivery_id TEXT PRIMARY KEY, package_json TEXT NOT NULL, state TEXT NOT NULL, "
        "ack_json TEXT NOT NULL, ack_sha256 TEXT NOT NULL, "
        "writer_json TEXT, writer_sha256 TEXT, "
        "result_json TEXT, result_sha256 TEXT)"
    )


async def _row(db, delivery_id):
    return await (
        await db.execute(
            "SELECT * FROM evolution_stable_remote_finalization_deliveries "
            "WHERE delivery_id = ?",
            (delivery_id,),
        )
    ).fetchone()


def _restore_view(row):
    return EvolutionStableRemoteFinalizationDeliveryView(
        package=EvolutionStableRemoteFinalizationDeliveryPackage.model_validate_json(
            row["package_json"]
        ),
        latest_event=EvolutionStableRemoteFinalizationDeliveryEvent.model_validate_json(
            row["latest_event_json"]
        ),
        ack=None if row["ack_json"] is None else (
            EvolutionStableRemoteFinalizationDeliveryAck.model_validate_json(row["ack_json"])
        ),
        receipt=None if row["receipt_json"] is None else (
            EvolutionStableRemoteFinalizationReceipt.model_validate_json(row["receipt_json"])
        ),
    )


async def _insert_event(db, event):
    await db.execute(
        "INSERT INTO evolution_stable_remote_finalization_delivery_events "
        "(delivery_id, sequence, event_json) VALUES (?, ?, ?)",
        (event.delivery_id, event.sequence, event.model_dump_json()),
    )


async def _update_event(db, delivery_id, event):
    await db.execute(
        "UPDATE evolution_stable_remote_finalization_deliveries SET latest_event_json = ? "
        "WHERE delivery_id = ?",
        (event.model_dump_json(), delivery_id),
    )
    await _insert_event(db, event)


async def _verify_chain(db, view):
    rows = await (
        await db.execute(
            "SELECT event_json FROM evolution_stable_remote_finalization_delivery_events "
            "WHERE delivery_id = ? ORDER BY sequence",
            (view.package.delivery_id,),
        )
    ).fetchall()
    events = [
        EvolutionStableRemoteFinalizationDeliveryEvent.model_validate_json(row[0])
        for row in rows
    ]
    if not events or events[-1] != view.latest_event:
        raise EvolutionStableRemoteFinalizationDeliveryError(
            "stable_remote_delivery_chain_corrupt", "Delivery event chain 与主记录不一致。"
        )
    if not (
        events[0].state == "queued"
        and events[0].claim_epoch == 0
        and events[0].attempt_count == 0
        and events[0].previous_event_sha256 is None
    ):
        raise EvolutionStableRemoteFinalizationDeliveryError(
            "stable_remote_delivery_chain_corrupt", "Delivery 首事件无效。"
        )
    allowed = {
        ("queued", "in_flight"),
        ("in_flight", "queued"),
        ("in_flight", "in_flight"),
        ("in_flight", "acknowledged"),
        ("in_flight", "dead_letter"),
        ("acknowledged", "completed"),
    }
    for index, event in enumerate(events):
        if event.sequence != index + 1 or event.previous_event_sha256 != (
            None if index == 0 else events[index - 1].event_sha256
        ):
            raise EvolutionStableRemoteFinalizationDeliveryError(
                "stable_remote_delivery_chain_corrupt", "Delivery event chain 不连续。"
            )
        if index:
            previous = events[index - 1]
            if (previous.state, event.state) not in allowed:
                raise EvolutionStableRemoteFinalizationDeliveryError(
                    "stable_remote_delivery_chain_corrupt", "Delivery 状态转换无效。"
                )
            claim_transition = event.state == "in_flight"
            if claim_transition and not (
                event.claim_epoch == previous.claim_epoch + 1
                and event.attempt_count == previous.attempt_count + 1
            ):
                raise EvolutionStableRemoteFinalizationDeliveryError(
                    "stable_remote_delivery_chain_corrupt", "Delivery claim 计数不连续。"
                )
            if not claim_transition and not (
                event.claim_epoch == previous.claim_epoch
                and event.attempt_count == previous.attempt_count
            ):
                raise EvolutionStableRemoteFinalizationDeliveryError(
                    "stable_remote_delivery_chain_corrupt", "Delivery 非 claim 计数发生漂移。"
                )
    ack_sha = (
        None if view.ack is None else _digest(view.ack.model_dump(mode="json"))
    )
    if view.latest_event.state in {"acknowledged", "completed"} and not (
        view.ack is not None and view.latest_event.ack_sha256 == ack_sha
    ):
        raise EvolutionStableRemoteFinalizationDeliveryError(
            "stable_remote_delivery_chain_corrupt", "Delivery ACK 与事件链不一致。"
        )
    if view.latest_event.state == "completed" and not (
        view.receipt is not None
        and view.latest_event.receipt_id == view.receipt.receipt_id
        and view.receipt.execution_package == view.package.execution_package
    ):
        raise EvolutionStableRemoteFinalizationDeliveryError(
            "stable_remote_delivery_chain_corrupt", "Delivery Receipt 与事件链不一致。"
        )


def _encode(value):
    raw = _canonical(value)
    if len(raw) > _MAX_ARTIFACT_BYTES:
        raise EvolutionStableRemoteFinalizationDeliveryError(
            "stable_remote_delivery_artifact_oversized", "Delivery artifact 超过大小上限。"
        )
    return base64.b64encode(raw).decode()


def _decode(value, model):
    if not isinstance(value, str) or not value or len(value) > _MAX_ENCODED_CHARS:
        raise EvolutionStableRemoteFinalizationDeliveryError(
            "stable_remote_delivery_artifact_invalid", "Delivery artifact 编码无效。"
        )
    try:
        raw = base64.b64decode(value, validate=True)
        if len(raw) > _MAX_ARTIFACT_BYTES or base64.b64encode(raw).decode() != value:
            raise ValueError("bounds")
        return model.model_validate_json(raw)
    except (ValueError, TypeError) as exc:
        raise EvolutionStableRemoteFinalizationDeliveryError(
            "stable_remote_delivery_artifact_invalid", "Delivery artifact 编码或结构无效。"
        ) from exc


def _delivery_id(value):
    if not isinstance(value, str) or not value.startswith("evstableremotedelivery_"):
        raise ValueError("Delivery ID 无效。")
    return value


def _owner_sha256(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 256:
        raise ValueError("Delivery owner_id 必须为 1..256 个字符。")
    return hashlib.sha256(value.strip().encode()).hexdigest()


def _failure_code(value):
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError("Delivery failure_code 无效。")
    return value


def _retry_delay(attempt, *, base_seconds, max_seconds):
    if not all(
        isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item)
        for item in (base_seconds, max_seconds)
    ) or not (0.1 <= base_seconds <= max_seconds <= 3600):
        raise ValueError("Delivery retry backoff 无效。")
    return min(max_seconds, base_seconds * (2 ** min(max(attempt - 1, 0), 16)))


def _aware(value):
    item = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if item.tzinfo is None or item.utcoffset() is None:
        raise ValueError("时间必须包含时区。")
    return item.astimezone(UTC)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


__all__ = [
    "EVOLUTION_STABLE_REMOTE_FINALIZATION_DELIVERY_POLICY",
    "EvolutionStableRemoteFinalizationDeliveryAck",
    "EvolutionStableRemoteFinalizationDeliveryAckPayload",
    "EvolutionStableRemoteFinalizationDeliveryError",
    "EvolutionStableRemoteFinalizationDeliveryEvent",
    "EvolutionStableRemoteFinalizationDeliveryPackage",
    "EvolutionStableRemoteFinalizationDeliveryService",
    "EvolutionStableRemoteFinalizationDeliveryStore",
    "EvolutionStableRemoteFinalizationDeliveryView",
    "EvolutionStableRemoteFinalizationTargetJournal",
    "EvolutionStableRemoteFinalizationTargetJournalEntry",
    "decode_stable_remote_finalization_delivery_ack",
    "decode_stable_remote_finalization_delivery_package",
    "encode_stable_remote_finalization_delivery_ack",
    "encode_stable_remote_finalization_delivery_package",
    "render_stable_remote_finalization_delivery",
]
