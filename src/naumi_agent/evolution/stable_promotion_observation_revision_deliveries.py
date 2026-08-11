"""Signed bounded delivery of stable-promotion observation revisions."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.stable_promotion_observation_chain_cursors import (
    EvolutionStablePromotionObservationChainCursor,
    EvolutionStablePromotionObservationChainCursorService,
    EvolutionStablePromotionObservationChainCursorStore,
    EvolutionStablePromotionObservationChainRevision,
)
from naumi_agent.evolution.stable_promotion_runtime_admission_deliveries import (
    EvolutionStablePromotionRuntimeAdmissionDeliveryReceipt,
    EvolutionStablePromotionRuntimeAdmissionDeliveryService,
    EvolutionStablePromotionRuntimeAdmissionSubmission,
)
from naumi_agent.release.installation_keys import (
    RELEASE_INSTALLATION_STABLE_PROMOTION_OBSERVATION_SIGNATURE_DOMAIN,
    ReleaseInstallationKeyService,
    ReleaseInstallationSignature,
    verify_release_installation_signature,
)
from naumi_agent.release.population_registry import (
    ReleaseManagedInstallationCredential,
    ReleasePopulationSnapshotStore,
)

EVOLUTION_STABLE_PROMOTION_OBSERVATION_REVISION_DELIVERY_POLICY = (
    "evolution-stable-promotion-observation-revision-delivery-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_ADMISSION_RE = re.compile(r"^evstablepromadmit_[0-9a-f]{24}$")
_SUBMISSION_RE = re.compile(r"^evstablepromrevsubmit_[0-9a-f]{24}$")
_RECEIPT_RE = re.compile(r"^evstablepromrevreceive_[0-9a-f]{24}$")
_MAX_BATCH_REVISIONS = 500
_MAX_SIGNED_PAYLOAD_BYTES = 64 * 1024
_MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
_MAX_ENCODED_CHARS = ((_MAX_ARTIFACT_BYTES + 2) // 3) * 4


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionStablePromotionObservationRevisionSubmissionPayload(_StrictModel):
    """Canonical installation-signed batch; no long-term verdict authority."""

    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-stable-promotion-observation-revision-delivery-v1"
    ] = EVOLUTION_STABLE_PROMOTION_OBSERVATION_REVISION_DELIVERY_POLICY
    admission_id: str = Field(pattern=r"^evstablepromadmit_[0-9a-f]{24}$")
    admission_sha256: str = Field(pattern=_SHA256_RE)
    admission_delivery_receipt_id: str = Field(
        pattern=r"^evstablepromreceive_[0-9a-f]{24}$"
    )
    admission_delivery_receipt_sha256: str = Field(pattern=_SHA256_RE)
    cursor_id: str = Field(pattern=r"^evstablepromcursor_[0-9a-f]{24}$")
    cursor_sha256: str = Field(pattern=_SHA256_RE)
    prior_remote_head_sequence: int = Field(ge=0, le=5_000)
    prior_remote_head_revision_sha256: str = Field(pattern=r"^(|[0-9a-f]{64})$")
    first_sequence: int = Field(ge=1, le=5_000)
    last_sequence: int = Field(ge=1, le=5_000)
    revision_count: int = Field(ge=1, le=_MAX_BATCH_REVISIONS)
    revisions: tuple[EvolutionStablePromotionObservationChainRevision, ...] = Field(
        min_length=1,
        max_length=_MAX_BATCH_REVISIONS,
    )
    submitted_at: str = Field(min_length=1, max_length=100)
    installation_cursor_source_attested: Literal[True] = True
    remote_revision_delivery_authority: Literal[False] = False
    observation_window_authority: Literal[False] = False
    long_term_metrics_authority: Literal[False] = False
    population_observation_authority: Literal[False] = False
    promoted_outcome_authority: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    execution_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        values = self.revisions
        if not (
            self.revision_count == len(values)
            and self.first_sequence == self.prior_remote_head_sequence + 1
            and self.first_sequence == values[0].revision_sequence
            and self.last_sequence == values[-1].revision_sequence
            and self.last_sequence - self.first_sequence + 1 == len(values)
            and (self.prior_remote_head_sequence == 0)
            is (not self.prior_remote_head_revision_sha256)
            and _aware(self.submitted_at)
            >= max(_aware(item.observation.observed_at) for item in values)
        ):
            raise ValueError("Observation revision batch range 或时间不一致。")
        expected_previous = self.prior_remote_head_revision_sha256
        for sequence, revision in enumerate(values, start=self.first_sequence):
            if not (
                revision.admission_id == self.admission_id
                and revision.admission_sha256 == self.admission_sha256
                and revision.delivery_receipt_id
                == self.admission_delivery_receipt_id
                and revision.delivery_receipt_sha256
                == self.admission_delivery_receipt_sha256
                and revision.revision_sequence == sequence
                and revision.previous_revision_sha256 == expected_previous
            ):
                raise ValueError("Observation revision batch lineage/hash chain 不一致。")
            expected_previous = revision.revision_sha256
        if any((
            self.remote_revision_delivery_authority,
            self.observation_window_authority,
            self.long_term_metrics_authority,
            self.population_observation_authority,
            self.promoted_outcome_authority,
            self.learning_authority,
            self.promotion_authority,
            self.execution_authority,
        )):
            raise ValueError("Observation revision Submission 不得扩张 authority。")
        if len(self.canonical_bytes()) > _MAX_SIGNED_PAYLOAD_BYTES:
            raise ValueError("Observation revision signed payload 超过 64 KiB。")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical(self.model_dump(mode="json"))


class EvolutionStablePromotionObservationRevisionSubmission(_StrictModel):
    schema_version: Literal[1] = 1
    submission_id: str = Field(pattern=r"^evstablepromrevsubmit_[0-9a-f]{24}$")
    submission_sha256: str = Field(pattern=_SHA256_RE)
    payload: EvolutionStablePromotionObservationRevisionSubmissionPayload
    signature: ReleaseInstallationSignature

    @model_validator(mode="after")
    def _binding(self) -> Self:
        payload = self.payload.canonical_bytes()
        if not (
            self.signature.domain
            == RELEASE_INSTALLATION_STABLE_PROMOTION_OBSERVATION_SIGNATURE_DOMAIN
            and self.signature.installation_member_id
            == self.payload.revisions[0].installation_member_id
            and self.signature.payload_sha256 == hashlib.sha256(payload).hexdigest()
            and self.signature.payload_bytes == len(payload)
            and _aware(self.signature.signed_at) >= _aware(self.payload.submitted_at)
        ):
            raise ValueError("Observation revision Submission signature binding 无效。")
        digest = _digest(
            self.model_dump(mode="json", exclude={"submission_id", "submission_sha256"})
        )
        if not (
            hmac.compare_digest(self.submission_sha256, digest)
            and self.submission_id == f"evstablepromrevsubmit_{digest[:24]}"
        ):
            raise ValueError("Observation revision Submission identity 不一致。")
        return self


class EvolutionStablePromotionObservationRevisionDeliveryReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-stable-promotion-observation-revision-delivery-v1"
    ] = EVOLUTION_STABLE_PROMOTION_OBSERVATION_REVISION_DELIVERY_POLICY
    receipt_id: str = Field(pattern=r"^evstablepromrevreceive_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    submission_id: str = Field(pattern=r"^evstablepromrevsubmit_[0-9a-f]{24}$")
    submission_sha256: str = Field(pattern=_SHA256_RE)
    signature_id: str = Field(pattern=r"^relinstallsig_[0-9a-f]{24}$")
    signature_artifact_sha256: str = Field(pattern=_SHA256_RE)
    admission_id: str = Field(pattern=r"^evstablepromadmit_[0-9a-f]{24}$")
    admission_sha256: str = Field(pattern=_SHA256_RE)
    admission_delivery_receipt_id: str = Field(
        pattern=r"^evstablepromreceive_[0-9a-f]{24}$"
    )
    admission_delivery_receipt_sha256: str = Field(pattern=_SHA256_RE)
    installation_member_id: str = Field(pattern=r"^relpopmember_[0-9a-f]{24}$")
    installation_credential_id: str = Field(pattern=r"^relpopcred_[0-9a-f]{24}$")
    installation_credential_sha256: str = Field(pattern=_SHA256_RE)
    installation_public_key_sha256: str = Field(pattern=_SHA256_RE)
    first_sequence: int = Field(ge=1, le=5_000)
    last_sequence: int = Field(ge=1, le=5_000)
    revision_count: int = Field(ge=1, le=_MAX_BATCH_REVISIONS)
    remote_head_revision_sha256: str = Field(pattern=_SHA256_RE)
    received_at: str = Field(min_length=1, max_length=100)
    installation_signature_verified: Literal[True] = True
    current_population_credential_verified: Literal[True] = True
    exact_admission_receipt_verified: Literal[True] = True
    revision_hash_chain_verified: Literal[True] = True
    remote_revision_delivery_recorded: Literal[True] = True
    observation_window_authority: Literal[False] = False
    long_term_metrics_authority: Literal[False] = False
    population_observation_authority: Literal[False] = False
    promoted_outcome_authority: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    execution_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        _aware(self.received_at)
        if not (
            self.revision_count == self.last_sequence - self.first_sequence + 1
            and not any((
                self.observation_window_authority,
                self.long_term_metrics_authority,
                self.population_observation_authority,
                self.promoted_outcome_authority,
                self.learning_authority,
                self.promotion_authority,
                self.execution_authority,
            ))
        ):
            raise ValueError("Observation revision Delivery Receipt authority/range 无效。")
        digest = _digest(
            self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        )
        if not (
            hmac.compare_digest(self.receipt_sha256, digest)
            and self.receipt_id == f"evstablepromrevreceive_{digest[:24]}"
        ):
            raise ValueError("Observation revision Delivery Receipt identity 不一致。")
        return self


class EvolutionStablePromotionObservationRevisionDeliveryView(_StrictModel):
    submission: EvolutionStablePromotionObservationRevisionSubmission
    receipt: EvolutionStablePromotionObservationRevisionDeliveryReceipt
    status: Literal["received", "stale"]
    durable_receipt_valid: bool
    admission_delivery_authority: bool
    current_credential_authority: bool
    installation_signature_authority: bool
    remote_revision_chain_current: bool
    installation_cursor_source_current: bool
    remote_revision_delivery_authority: bool
    observation_window_authority: Literal[False] = False
    long_term_metrics_authority: Literal[False] = False
    population_observation_authority: Literal[False] = False
    promoted_outcome_authority: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    execution_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        expected = bool(
            self.durable_receipt_valid
            and self.admission_delivery_authority
            and self.current_credential_authority
            and self.installation_signature_authority
            and self.remote_revision_chain_current
        )
        if not (
            self.remote_revision_delivery_authority is expected
            and (self.status == "received") is expected
        ):
            raise ValueError("Observation revision Delivery authority projection 不一致。")
        return self


class EvolutionStablePromotionObservationRevisionDeliveryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionStablePromotionObservationRevisionDeliveryStore:
    """Durable outbound batches plus a sequential Control Plane head."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def get_outbound(
        self, admission_id: str, first_sequence: int
    ) -> EvolutionStablePromotionObservationRevisionSubmission | None:
        item_id = _admission_id(admission_id)
        row = await self._read_one(
            "SELECT * FROM evolution_stable_promotion_observation_revision_outbox "
            "WHERE admission_id = ? AND first_sequence = ?",
            (item_id, int(first_sequence)),
        )
        return None if row is None else _submission_from_row(row)

    async def get_outbound_by_id(
        self, submission_id: str
    ) -> EvolutionStablePromotionObservationRevisionSubmission | None:
        item_id = _submission_id(submission_id)
        row = await self._read_one(
            "SELECT * FROM evolution_stable_promotion_observation_revision_outbox "
            "WHERE submission_id = ?",
            (item_id,),
        )
        return None if row is None else _submission_from_row(row)

    async def stage_outbound(
        self,
        submission: EvolutionStablePromotionObservationRevisionSubmission,
        *,
        cursor_store: EvolutionStablePromotionObservationChainCursorStore,
    ) -> EvolutionStablePromotionObservationRevisionSubmission:
        item = _submission(submission)
        if cursor_store.db_path != self.db_path:
            raise ValueError("Observation revision outbox 必须与 Cursor 共用 SQLite。")
        encoded = item.model_dump_json()
        _artifact_bound(encoded)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                payload = item.payload
                cursor_row = await (
                    await db.execute(
                        "SELECT cursor_id, cursor_json FROM "
                        "evolution_stable_promotion_observation_cursors "
                        "WHERE admission_id = ?",
                        (payload.admission_id,),
                    )
                ).fetchone()
                if cursor_row is None or cursor_row["cursor_id"] != payload.cursor_id:
                    await db.rollback()
                    raise EvolutionStablePromotionObservationRevisionDeliveryError(
                        "stable_promotion_observation_revision_cursor_mismatch",
                        "Signed revision batch 缺少 exact durable Cursor。",
                    )
                try:
                    durable_cursor = (
                        EvolutionStablePromotionObservationChainCursor.model_validate_json(
                            cursor_row["cursor_json"]
                        )
                    )
                except (TypeError, ValueError) as exc:
                    await db.rollback()
                    raise EvolutionStablePromotionObservationRevisionDeliveryError(
                        "stable_promotion_observation_revision_cursor_corrupt",
                        "Durable Observation Cursor artifact 损坏。",
                    ) from exc
                if not (
                    durable_cursor.cursor_id == payload.cursor_id
                    and durable_cursor.cursor_sha256 == payload.cursor_sha256
                    and durable_cursor.admission.admission_id == payload.admission_id
                    and durable_cursor.after_sequence >= payload.last_sequence
                ):
                    await db.rollback()
                    raise EvolutionStablePromotionObservationRevisionDeliveryError(
                        "stable_promotion_observation_revision_cursor_mismatch",
                        "Signed revision batch 与 durable Cursor head 不一致。",
                    )
                for revision in payload.revisions:
                    source = await (
                        await db.execute(
                            "SELECT revision_json FROM "
                            "evolution_stable_promotion_observation_cursor_revisions "
                            "WHERE admission_id = ? AND heartbeat_sequence = ?",
                            (payload.admission_id, revision.revision_sequence),
                        )
                    ).fetchone()
                    if source is None or _restore_revision(source["revision_json"]) != revision:
                        await db.rollback()
                        raise EvolutionStablePromotionObservationRevisionDeliveryError(
                            "stable_promotion_observation_revision_source_mismatch",
                            "Signed revision batch 与 durable Cursor revision 不一致。",
                        )
                existing = await (
                    await db.execute(
                        "SELECT * FROM evolution_stable_promotion_observation_revision_outbox "
                        "WHERE admission_id = ? AND first_sequence = ?",
                        (payload.admission_id, payload.first_sequence),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _submission_from_row(existing)
                    await db.rollback()
                    if restored.payload == item.payload:
                        return restored
                    raise EvolutionStablePromotionObservationRevisionDeliveryError(
                        "stable_promotion_observation_revision_outbound_conflict",
                        "同一起始 sequence 已绑定不同 signed revision batch。",
                    )
                await db.execute(
                    "INSERT INTO evolution_stable_promotion_observation_revision_outbox "
                    "(submission_id, submission_sha256, admission_id, first_sequence, "
                    "last_sequence, submission_json) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        item.submission_id,
                        item.submission_sha256,
                        payload.admission_id,
                        payload.first_sequence,
                        payload.last_sequence,
                        encoded,
                    ),
                )
                await db.commit()
            return item
        except EvolutionStablePromotionObservationRevisionDeliveryError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionObservationRevisionDeliveryError(
                "stable_promotion_observation_revision_outbox_failed",
                "Signed observation revision batch 无法持久化。",
            ) from exc

    async def receive(
        self,
        submission: EvolutionStablePromotionObservationRevisionSubmission,
        receipt: EvolutionStablePromotionObservationRevisionDeliveryReceipt,
    ) -> tuple[
        EvolutionStablePromotionObservationRevisionSubmission,
        EvolutionStablePromotionObservationRevisionDeliveryReceipt,
    ]:
        item = _submission(submission)
        ack = _receipt(receipt)
        if not _receipt_matches_submission(ack, item):
            raise EvolutionStablePromotionObservationRevisionDeliveryError(
                "stable_promotion_observation_revision_receipt_mismatch",
                "Observation revision Receipt 与 Submission 不一致。",
            )
        submission_json = item.model_dump_json()
        receipt_json = ack.model_dump_json()
        _artifact_bound(submission_json)
        _artifact_bound(receipt_json)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                await _require_admission_receipt(db, item)
                existing = await (
                    await db.execute(
                        "SELECT * FROM evolution_stable_promotion_observation_revision_receipts "
                        "WHERE submission_id = ?",
                        (item.submission_id,),
                    )
                ).fetchone()
                if existing is not None:
                    stored = (_submission_from_row(existing), _receipt_from_row(existing))
                    await db.rollback()
                    if stored[0] == item:
                        return stored
                    raise EvolutionStablePromotionObservationRevisionDeliveryError(
                        "stable_promotion_observation_revision_receive_conflict",
                        "同一 Submission 已绑定不同 Receipt。",
                    )
                head = await (
                    await db.execute(
                        "SELECT * FROM evolution_stable_promotion_observation_revision_heads "
                        "WHERE admission_id = ?",
                        (item.payload.admission_id,),
                    )
                ).fetchone()
                prior_sequence = 0 if head is None else int(head["last_sequence"])
                prior_sha = "" if head is None else str(head["last_revision_sha256"])
                if not (
                    item.payload.prior_remote_head_sequence == prior_sequence
                    and item.payload.prior_remote_head_revision_sha256 == prior_sha
                ):
                    await db.rollback()
                    raise EvolutionStablePromotionObservationRevisionDeliveryError(
                        "stable_promotion_observation_revision_remote_head_conflict",
                        "Control Plane revision head 与 Submission 前置 head 不一致。",
                    )
                await db.execute(
                    "INSERT INTO evolution_stable_promotion_observation_revision_receipts "
                    "(receipt_id, receipt_sha256, submission_id, submission_sha256, "
                    "admission_id, first_sequence, last_sequence, submission_json, "
                    "receipt_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        ack.receipt_id,
                        ack.receipt_sha256,
                        item.submission_id,
                        item.submission_sha256,
                        item.payload.admission_id,
                        item.payload.first_sequence,
                        item.payload.last_sequence,
                        submission_json,
                        receipt_json,
                    ),
                )
                await db.execute(
                    "INSERT INTO evolution_stable_promotion_observation_revision_heads "
                    "(admission_id, last_sequence, last_revision_sha256, receipt_id, "
                    "receipt_sha256) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(admission_id) DO UPDATE SET "
                    "last_sequence = excluded.last_sequence, "
                    "last_revision_sha256 = excluded.last_revision_sha256, "
                    "receipt_id = excluded.receipt_id, receipt_sha256 = excluded.receipt_sha256",
                    (
                        item.payload.admission_id,
                        item.payload.last_sequence,
                        item.payload.revisions[-1].revision_sha256,
                        ack.receipt_id,
                        ack.receipt_sha256,
                    ),
                )
                await db.commit()
            return item, ack
        except EvolutionStablePromotionObservationRevisionDeliveryError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionObservationRevisionDeliveryError(
                "stable_promotion_observation_revision_receive_failed",
                "Control Plane 无法持久化 observation revision Receipt。",
            ) from exc

    async def get_received_by_receipt(
        self, receipt_id: str
    ) -> tuple[
        EvolutionStablePromotionObservationRevisionSubmission,
        EvolutionStablePromotionObservationRevisionDeliveryReceipt,
    ] | None:
        item_id = _receipt_id(receipt_id)
        row = await self._read_one(
            "SELECT * FROM evolution_stable_promotion_observation_revision_receipts "
            "WHERE receipt_id = ?",
            (item_id,),
        )
        return None if row is None else (_submission_from_row(row), _receipt_from_row(row))

    async def get_received_by_submission(
        self, submission_id: str
    ) -> tuple[
        EvolutionStablePromotionObservationRevisionSubmission,
        EvolutionStablePromotionObservationRevisionDeliveryReceipt,
    ] | None:
        item_id = _submission_id(submission_id)
        row = await self._read_one(
            "SELECT * FROM evolution_stable_promotion_observation_revision_receipts "
            "WHERE submission_id = ?",
            (item_id,),
        )
        return None if row is None else (_submission_from_row(row), _receipt_from_row(row))

    async def remote_head(self, admission_id: str) -> tuple[int, str] | None:
        item_id = _admission_id(admission_id)
        row = await self._read_one(
            "SELECT last_sequence, last_revision_sha256 FROM "
            "evolution_stable_promotion_observation_revision_heads "
            "WHERE admission_id = ?",
            (item_id,),
        )
        return None if row is None else (int(row[0]), str(row[1]))

    async def received_chain(
        self, admission_id: str
    ) -> tuple[
        tuple[
            EvolutionStablePromotionObservationRevisionSubmission,
            EvolutionStablePromotionObservationRevisionDeliveryReceipt,
        ],
        ...,
    ]:
        item_id = _admission_id(admission_id)
        if not self.db_path.is_file():
            return ()
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                rows = await (
                    await db.execute(
                        "SELECT * FROM "
                        "evolution_stable_promotion_observation_revision_receipts "
                        "WHERE admission_id = ? ORDER BY first_sequence",
                        (item_id,),
                    )
                ).fetchall()
            return tuple(
                (_submission_from_row(row), _receipt_from_row(row)) for row in rows
            )
        except EvolutionStablePromotionObservationRevisionDeliveryError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionObservationRevisionDeliveryError(
                "stable_promotion_observation_revision_store_corrupt",
                "Observation revision received chain 损坏或无法读取。",
            ) from exc

    async def _read_one(self, query: str, params: tuple[object, ...]):
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                return await (await db.execute(query, params)).fetchone()
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionObservationRevisionDeliveryError(
                "stable_promotion_observation_revision_store_corrupt",
                "Observation revision Delivery Store 损坏或无法读取。",
            ) from exc


class EvolutionStablePromotionObservationRevisionDeliveryService:
    """Sign local cursor revisions and verify them at the Control Plane boundary."""

    def __init__(
        self,
        *,
        cursor_store: EvolutionStablePromotionObservationChainCursorStore,
        cursor_service: EvolutionStablePromotionObservationChainCursorService,
        admission_delivery_service: EvolutionStablePromotionRuntimeAdmissionDeliveryService,
        population_store: ReleasePopulationSnapshotStore,
        installation_key_service: ReleaseInstallationKeyService,
        store: EvolutionStablePromotionObservationRevisionDeliveryStore,
    ) -> None:
        if not (
            cursor_service.store is cursor_store
            and cursor_service.delivery_service is admission_delivery_service
            and admission_delivery_service.population_store is population_store
            and cursor_store.db_path == admission_delivery_service.store.db_path
            == store.db_path
        ):
            raise ValueError("Observation revision Delivery authority composition 不一致。")
        self.cursor_store = cursor_store
        self.cursor_service = cursor_service
        self.admission_delivery_service = admission_delivery_service
        self.population_store = population_store
        self.installation_key_service = installation_key_service
        self.store = store

    async def prepare(
        self, *, admission_id: str, after_sequence: int = 0
    ) -> EvolutionStablePromotionObservationRevisionSubmission:
        item_id = _admission_id(admission_id)
        after = int(after_sequence)
        if not 0 <= after < 5_000:
            raise ValueError("after_sequence 必须为 0..4999。")
        cursor_view = await self.cursor_service.inspect(admission_id=item_id)
        if not cursor_view.cursor_delivery_input_authority:
            raise EvolutionStablePromotionObservationRevisionDeliveryError(
                "stable_promotion_observation_revision_cursor_stale",
                "Observation Cursor 当前不具备 signed delivery input authority。",
            )
        cursor = cursor_view.cursor
        if after >= cursor.after_sequence:
            raise EvolutionStablePromotionObservationRevisionDeliveryError(
                "stable_promotion_observation_revision_no_new_samples",
                "指定 sequence 之后没有可交付 revision。",
            )
        expected_prior = ""
        revisions = await self.cursor_store.revisions(item_id)
        if after:
            if len(revisions) < after:
                raise EvolutionStablePromotionObservationRevisionDeliveryError(
                    "stable_promotion_observation_revision_after_missing",
                    "after_sequence 不存在于 durable Cursor。",
                )
            expected_prior = revisions[after - 1].revision_sha256
        existing = await self.store.get_outbound(item_id, after + 1)
        admission_submission, admission_receipt = await self._admission_sources(item_id)
        credential = await self._current_credential(admission_submission)
        if existing is not None:
            if not (
                existing.payload.prior_remote_head_sequence == after
                and existing.payload.prior_remote_head_revision_sha256 == expected_prior
            ):
                raise EvolutionStablePromotionObservationRevisionDeliveryError(
                    "stable_promotion_observation_revision_outbound_conflict",
                    "既有 outbound 与 requested after_sequence 不一致。",
                )
            self._verify_signature(existing, credential)
            return existing
        submitted_at = max(
            _aware(admission_receipt.received_at),
            _aware(cursor.latest_revision.observation.observed_at),
        ).isoformat()
        candidates: list[EvolutionStablePromotionObservationChainRevision] = []
        for revision in revisions[after : after + _MAX_BATCH_REVISIONS]:
            trial = tuple((*candidates, revision))
            try:
                _build_payload(
                    admission_submission=admission_submission,
                    admission_receipt=admission_receipt,
                    cursor_id=cursor.cursor_id,
                    cursor_sha256=cursor.cursor_sha256,
                    prior_sequence=after,
                    prior_sha256=expected_prior,
                    revisions=trial,
                    submitted_at=submitted_at,
                )
            except ValueError as exc:
                if "64 KiB" not in str(exc):
                    raise
                break
            candidates.append(revision)
        if not candidates:
            raise EvolutionStablePromotionObservationRevisionDeliveryError(
                "stable_promotion_observation_revision_payload_oversized",
                "单个 observation revision 已超过 64 KiB 签名上限。",
            )
        payload = _build_payload(
            admission_submission=admission_submission,
            admission_receipt=admission_receipt,
            cursor_id=cursor.cursor_id,
            cursor_sha256=cursor.cursor_sha256,
            prior_sequence=after,
            prior_sha256=expected_prior,
            revisions=tuple(candidates),
            submitted_at=submitted_at,
        )
        signature = self.installation_key_service.sign_stable_promotion_observation_revisions(
            credential=credential,
            payload=payload.canonical_bytes(),
        )
        core = {
            "schema_version": 1,
            "payload": payload.model_dump(mode="json"),
            "signature": signature.model_dump(mode="json"),
        }
        digest = _digest(core)
        submission = EvolutionStablePromotionObservationRevisionSubmission.model_validate({
            **core,
            "submission_id": f"evstablepromrevsubmit_{digest[:24]}",
            "submission_sha256": digest,
        })
        return await self.store.stage_outbound(submission, cursor_store=self.cursor_store)

    async def receive(
        self,
        *,
        submission: EvolutionStablePromotionObservationRevisionSubmission,
        received_at: str | datetime | None = None,
    ) -> EvolutionStablePromotionObservationRevisionDeliveryView:
        item = _submission(submission)
        admission_submission, admission_receipt = await self._admission_sources(
            item.payload.admission_id
        )
        if not _payload_matches_admission(item.payload, admission_submission, admission_receipt):
            raise EvolutionStablePromotionObservationRevisionDeliveryError(
                "stable_promotion_observation_revision_admission_mismatch",
                "Signed revision batch 与 current Runtime Admission Receipt 不一致。",
            )
        credential = await self._current_credential(admission_submission)
        self._verify_signature(item, credential)
        now = _aware(received_at or datetime.now(UTC))
        if now < _aware(item.signature.signed_at):
            raise EvolutionStablePromotionObservationRevisionDeliveryError(
                "stable_promotion_observation_revision_received_before_signed",
                "Observation revision 接收时间早于签名时间。",
            )
        already_received = await self.store.get_received_by_submission(
            item.submission_id
        )
        if already_received is not None:
            if already_received[0] != item:
                raise EvolutionStablePromotionObservationRevisionDeliveryError(
                    "stable_promotion_observation_revision_receive_conflict",
                    "Submission identity 已绑定不同 observation revision artifact。",
                )
            return await self.inspect(
                submission=already_received[0],
                receipt=already_received[1],
            )
        existing = await self.store.remote_head(item.payload.admission_id)
        expected_head = (
            item.payload.prior_remote_head_sequence,
            item.payload.prior_remote_head_revision_sha256,
        )
        if (existing or (0, "")) != expected_head:
            raise EvolutionStablePromotionObservationRevisionDeliveryError(
                "stable_promotion_observation_revision_remote_head_conflict",
                "Control Plane 只接受 exact next revision batch。",
            )
        receipt = _build_receipt(item, credential=credential, received_at=now)
        stored = await self.store.receive(item, receipt)
        return await self.inspect(submission=stored[0], receipt=stored[1])

    async def inspect(
        self,
        *,
        submission: EvolutionStablePromotionObservationRevisionSubmission,
        receipt: EvolutionStablePromotionObservationRevisionDeliveryReceipt,
    ) -> EvolutionStablePromotionObservationRevisionDeliveryView:
        item = _submission(submission)
        ack = _receipt(receipt)
        durable = admission_authority = credential_authority = False
        signature_authority = chain_current = source_current = False
        try:
            stored = await self.store.get_received_by_receipt(ack.receipt_id)
            durable = bool(
                stored == (item, ack) and _receipt_matches_submission(ack, item)
            )
            head = await self.store.remote_head(item.payload.admission_id)
            chain = await self.store.received_chain(item.payload.admission_id)
            chain_current = _received_chain_current(
                chain=chain,
                head=head,
                expected=(item, ack),
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            pass
        admission_submission = admission_receipt = credential = None
        try:
            admission_submission, admission_receipt = await self._admission_sources(
                item.payload.admission_id
            )
            admission_authority = _payload_matches_admission(
                item.payload, admission_submission, admission_receipt
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            pass
        try:
            if admission_submission is None:
                raise ValueError("admission submission missing")
            credential = await self._current_credential(admission_submission)
            credential_authority = bool(
                credential.credential_id == ack.installation_credential_id
                and credential.credential_sha256 == ack.installation_credential_sha256
                and credential.payload.installation_public_key_sha256
                == ack.installation_public_key_sha256
            )
            self._verify_signature(item, credential)
            signature_authority = True
        except (OSError, RuntimeError, TypeError, ValueError):
            pass
        try:
            cursor = await self.cursor_store.get(item.payload.admission_id)
            revisions = await self.cursor_store.revisions(item.payload.admission_id)
            source_current = bool(
                cursor is not None
                and cursor.cursor_id == item.payload.cursor_id
                and cursor.cursor_sha256 == item.payload.cursor_sha256
                and tuple(
                    revisions[item.payload.first_sequence - 1 : item.payload.last_sequence]
                )
                == item.payload.revisions
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            pass
        authority = bool(
            durable
            and admission_authority
            and credential_authority
            and signature_authority
            and chain_current
        )
        return EvolutionStablePromotionObservationRevisionDeliveryView(
            submission=item,
            receipt=ack,
            status="received" if authority else "stale",
            durable_receipt_valid=durable,
            admission_delivery_authority=admission_authority,
            current_credential_authority=credential_authority,
            installation_signature_authority=signature_authority,
            remote_revision_chain_current=chain_current,
            installation_cursor_source_current=source_current,
            remote_revision_delivery_authority=authority,
        )

    async def authoritative_received_chain(
        self,
        admission_id: str,
    ) -> tuple[
        tuple[
            EvolutionStablePromotionObservationRevisionSubmission,
            EvolutionStablePromotionObservationRevisionDeliveryReceipt,
        ],
        ...,
    ]:
        """Return only a complete chain reverified with current authority."""
        item_id = _admission_id(admission_id)
        admission_submission, admission_receipt = await self._admission_sources(item_id)
        credential = await self._current_credential(admission_submission)
        chain = await self.store.received_chain(item_id)
        head = await self.store.remote_head(item_id)
        if not chain or not _received_chain_current(
            chain=chain,
            head=head,
            expected=chain[-1],
        ):
            raise EvolutionStablePromotionObservationRevisionDeliveryError(
                "stable_promotion_observation_revision_chain_stale",
                "Control Plane observation revision chain 不完整或 head 已失效。",
            )
        for submission, receipt in chain:
            if not (
                _payload_matches_admission(
                    submission.payload,
                    admission_submission,
                    admission_receipt,
                )
                and _receipt_matches_submission(receipt, submission)
                and receipt.installation_credential_id == credential.credential_id
                and receipt.installation_credential_sha256
                == credential.credential_sha256
                and receipt.installation_public_key_sha256
                == credential.payload.installation_public_key_sha256
            ):
                raise EvolutionStablePromotionObservationRevisionDeliveryError(
                    "stable_promotion_observation_revision_chain_untrusted",
                    "Control Plane observation revision chain authority 不一致。",
                )
            self._verify_signature(submission, credential)
        return chain

    async def _admission_sources(self, admission_id: str) -> tuple[
        EvolutionStablePromotionRuntimeAdmissionSubmission,
        EvolutionStablePromotionRuntimeAdmissionDeliveryReceipt,
    ]:
        stored = await self.admission_delivery_service.store.get_received(admission_id)
        if stored is None:
            raise EvolutionStablePromotionObservationRevisionDeliveryError(
                "stable_promotion_observation_revision_admission_missing",
                "Control Plane 尚未持久化 Runtime Admission Receipt。",
            )
        view = await self.admission_delivery_service.inspect(
            submission=stored[0], receipt=stored[1]
        )
        if not view.remote_admission_delivery_authority:
            raise EvolutionStablePromotionObservationRevisionDeliveryError(
                "stable_promotion_observation_revision_admission_stale",
                "Runtime Admission Delivery authority 已失效。",
            )
        return stored

    async def _current_credential(
        self, admission_submission: EvolutionStablePromotionRuntimeAdmissionSubmission
    ) -> ReleaseManagedInstallationCredential:
        admission = admission_submission.payload.admission
        population = await self.population_store.inspect(
            snapshot_id=admission.population_snapshot_id
        )
        credential = next((
            item
            for item in population.snapshot.payload.credentials
            if item.payload.member_id == admission.installation_member_id
        ), None)
        if not (
            population.population_snapshot_authority
            and credential is not None
            and credential.credential_id == admission_submission.signature.credential_id
            and credential.credential_sha256
            == admission_submission.signature.credential_sha256
        ):
            raise EvolutionStablePromotionObservationRevisionDeliveryError(
                "stable_promotion_observation_revision_credential_stale",
                "Current Population Credential 不可用或与 Admission 不一致。",
            )
        return credential

    @staticmethod
    def _verify_signature(
        submission: EvolutionStablePromotionObservationRevisionSubmission,
        credential: ReleaseManagedInstallationCredential,
    ) -> None:
        try:
            verify_release_installation_signature(
                credential=credential,
                payload=submission.payload.canonical_bytes(),
                artifact=submission.signature,
                expected_domain=(
                    RELEASE_INSTALLATION_STABLE_PROMOTION_OBSERVATION_SIGNATURE_DOMAIN
                ),
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionObservationRevisionDeliveryError(
                "stable_promotion_observation_revision_signature_untrusted",
                "Observation revision Submission 无法由 current Credential 验证。",
            ) from exc


def encode_stable_promotion_observation_revision_submission(
    submission: EvolutionStablePromotionObservationRevisionSubmission,
) -> str:
    raw = _submission(submission).model_dump_json().encode()
    _artifact_bound(raw)
    return base64.b64encode(raw).decode("ascii")


def decode_stable_promotion_observation_revision_submission(
    encoded: str,
) -> EvolutionStablePromotionObservationRevisionSubmission:
    value = str(encoded or "").strip()
    try:
        if not 1 <= len(value) <= _MAX_ENCODED_CHARS:
            raise ValueError("encoded bounds")
        raw = base64.b64decode(value, validate=True)
        if base64.b64encode(raw).decode("ascii") != value:
            raise ValueError("non-canonical base64")
        _artifact_bound(raw)
        return EvolutionStablePromotionObservationRevisionSubmission.model_validate_json(raw)
    except (binascii.Error, TypeError, ValueError) as exc:
        raise EvolutionStablePromotionObservationRevisionDeliveryError(
            "stable_promotion_observation_revision_payload_invalid",
            "Signed observation revision payload 不是 canonical bounded artifact。",
        ) from exc


def encode_stable_promotion_observation_revision_receipt(
    receipt: EvolutionStablePromotionObservationRevisionDeliveryReceipt,
) -> str:
    raw = _receipt(receipt).model_dump_json().encode()
    _artifact_bound(raw)
    return base64.b64encode(raw).decode("ascii")


def decode_stable_promotion_observation_revision_receipt(
    encoded: str,
) -> EvolutionStablePromotionObservationRevisionDeliveryReceipt:
    value = str(encoded or "").strip()
    try:
        if not 1 <= len(value) <= _MAX_ENCODED_CHARS:
            raise ValueError("encoded bounds")
        raw = base64.b64decode(value, validate=True)
        if base64.b64encode(raw).decode("ascii") != value:
            raise ValueError("non-canonical base64")
        _artifact_bound(raw)
        return EvolutionStablePromotionObservationRevisionDeliveryReceipt.model_validate_json(
            raw
        )
    except (binascii.Error, TypeError, ValueError) as exc:
        raise EvolutionStablePromotionObservationRevisionDeliveryError(
            "stable_promotion_observation_revision_receipt_payload_invalid",
            "Observation revision Receipt 不是 canonical bounded artifact。",
        ) from exc


def render_stable_promotion_observation_revision_delivery(
    value: EvolutionStablePromotionObservationRevisionSubmission
    | EvolutionStablePromotionObservationRevisionDeliveryView,
) -> str:
    if isinstance(value, EvolutionStablePromotionObservationRevisionSubmission):
        item = value
        return "\n".join((
            "### Stable Promotion Observation Revision Submission",
            "",
            "- 状态：`已签名，待 Control Plane 接收`",
            f"- Submission：`{item.submission_id}`",
            f"- Admission：`{item.payload.admission_id}`",
            f"- Revision range：`{item.payload.first_sequence}..{item.payload.last_sequence}`",
            f"- Revisions：`{item.payload.revision_count}`",
            f"- Payload bytes：`{item.signature.payload_bytes}/65536`",
            f"- Signature：`{item.signature.signature_id}` · `ed25519`",
            "- Remote revision delivery authority：`false`",
            "- Window / Population / Promoted Outcome authority：`false`",
        ))
    view = EvolutionStablePromotionObservationRevisionDeliveryView.model_validate(value)
    item = view.submission
    return "\n".join((
        "### Stable Promotion Observation Revision Delivery",
        "",
        f"- 状态：`{view.status}`",
        f"- Receipt：`{view.receipt.receipt_id}`",
        f"- Admission：`{item.payload.admission_id}`",
        f"- Remote head：`{view.receipt.last_sequence}`",
        f"- Revision range：`{item.payload.first_sequence}..{item.payload.last_sequence}`",
        f"- Durable Receipt：`{str(view.durable_receipt_valid).lower()}`",
        f"- Admission current：`{str(view.admission_delivery_authority).lower()}`",
        f"- Credential current：`{str(view.current_credential_authority).lower()}`",
        f"- Signature current：`{str(view.installation_signature_authority).lower()}`",
        f"- Remote chain current：`{str(view.remote_revision_chain_current).lower()}`",
        f"- Installation Cursor current：`{str(view.installation_cursor_source_current).lower()}`",
        "- Remote revision delivery authority："
        f"`{str(view.remote_revision_delivery_authority).lower()}`",
        "- Window / Population / Promoted Outcome authority：`false`",
    ))


def stable_promotion_observation_revision_receipt_matches_submission(
    receipt: EvolutionStablePromotionObservationRevisionDeliveryReceipt,
    submission: EvolutionStablePromotionObservationRevisionSubmission,
) -> bool:
    """Return the strict typed Receipt-to-Submission binding for workers."""
    return _receipt_matches_submission(_receipt(receipt), _submission(submission))


def _build_payload(
    *,
    admission_submission,
    admission_receipt,
    cursor_id,
    cursor_sha256,
    prior_sequence,
    prior_sha256,
    revisions,
    submitted_at,
):
    admission = admission_submission.payload.admission
    return EvolutionStablePromotionObservationRevisionSubmissionPayload(
        admission_id=admission.admission_id,
        admission_sha256=admission.admission_sha256,
        admission_delivery_receipt_id=admission_receipt.receipt_id,
        admission_delivery_receipt_sha256=admission_receipt.receipt_sha256,
        cursor_id=cursor_id,
        cursor_sha256=cursor_sha256,
        prior_remote_head_sequence=prior_sequence,
        prior_remote_head_revision_sha256=prior_sha256,
        first_sequence=revisions[0].revision_sequence,
        last_sequence=revisions[-1].revision_sequence,
        revision_count=len(revisions),
        revisions=revisions,
        submitted_at=submitted_at,
    )


def _build_receipt(submission, *, credential, received_at):
    payload = submission.payload
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_STABLE_PROMOTION_OBSERVATION_REVISION_DELIVERY_POLICY,
        "submission_id": submission.submission_id,
        "submission_sha256": submission.submission_sha256,
        "signature_id": submission.signature.signature_id,
        "signature_artifact_sha256": submission.signature.signature_artifact_sha256,
        "admission_id": payload.admission_id,
        "admission_sha256": payload.admission_sha256,
        "admission_delivery_receipt_id": payload.admission_delivery_receipt_id,
        "admission_delivery_receipt_sha256": payload.admission_delivery_receipt_sha256,
        "installation_member_id": submission.signature.installation_member_id,
        "installation_credential_id": credential.credential_id,
        "installation_credential_sha256": credential.credential_sha256,
        "installation_public_key_sha256": credential.payload.installation_public_key_sha256,
        "first_sequence": payload.first_sequence,
        "last_sequence": payload.last_sequence,
        "revision_count": payload.revision_count,
        "remote_head_revision_sha256": payload.revisions[-1].revision_sha256,
        "received_at": _aware(received_at).isoformat(),
        "installation_signature_verified": True,
        "current_population_credential_verified": True,
        "exact_admission_receipt_verified": True,
        "revision_hash_chain_verified": True,
        "remote_revision_delivery_recorded": True,
        "observation_window_authority": False,
        "long_term_metrics_authority": False,
        "population_observation_authority": False,
        "promoted_outcome_authority": False,
        "learning_authority": False,
        "promotion_authority": False,
        "execution_authority": False,
    }
    digest = _digest(core)
    return EvolutionStablePromotionObservationRevisionDeliveryReceipt.model_validate({
        **core,
        "receipt_id": f"evstablepromrevreceive_{digest[:24]}",
        "receipt_sha256": digest,
    })


def _payload_matches_admission(payload, submission, receipt) -> bool:
    admission = submission.payload.admission
    return bool(
        payload.admission_id == admission.admission_id == receipt.admission_id
        and payload.admission_sha256
        == admission.admission_sha256
        == receipt.admission_sha256
        and payload.admission_delivery_receipt_id == receipt.receipt_id
        and payload.admission_delivery_receipt_sha256 == receipt.receipt_sha256
        and all(
            revision.installation_member_id == admission.installation_member_id
            for revision in payload.revisions
        )
    )


def _received_chain_current(*, chain, head, expected) -> bool:
    if not chain or head is None or expected not in chain:
        return False
    prior_sequence = 0
    prior_sha = ""
    for submission, receipt in chain:
        payload = submission.payload
        if not (
            payload.prior_remote_head_sequence == prior_sequence
            and payload.prior_remote_head_revision_sha256 == prior_sha
            and payload.first_sequence == prior_sequence + 1
            and _receipt_matches_submission(receipt, submission)
        ):
            return False
        prior_sequence = payload.last_sequence
        prior_sha = payload.revisions[-1].revision_sha256
    return head == (prior_sequence, prior_sha)


def _receipt_matches_submission(receipt, submission) -> bool:
    payload = submission.payload
    return bool(
        receipt.submission_id == submission.submission_id
        and receipt.submission_sha256 == submission.submission_sha256
        and receipt.signature_id == submission.signature.signature_id
        and receipt.signature_artifact_sha256
        == submission.signature.signature_artifact_sha256
        and receipt.admission_id == payload.admission_id
        and receipt.admission_sha256 == payload.admission_sha256
        and receipt.admission_delivery_receipt_id
        == payload.admission_delivery_receipt_id
        and receipt.admission_delivery_receipt_sha256
        == payload.admission_delivery_receipt_sha256
        and receipt.installation_member_id
        == submission.signature.installation_member_id
        and receipt.installation_credential_id == submission.signature.credential_id
        and receipt.installation_credential_sha256
        == submission.signature.credential_sha256
        and receipt.installation_public_key_sha256
        == submission.signature.public_key_sha256
        and receipt.first_sequence == payload.first_sequence
        and receipt.last_sequence == payload.last_sequence
        and receipt.revision_count == payload.revision_count
        and receipt.remote_head_revision_sha256
        == payload.revisions[-1].revision_sha256
        and _aware(receipt.received_at) >= _aware(submission.signature.signed_at)
    )


async def _require_admission_receipt(db, submission) -> None:
    payload = submission.payload
    row = await (
        await db.execute(
            "SELECT receipt_id, receipt_sha256 FROM "
            "evolution_stable_promotion_runtime_admission_receipts "
            "WHERE admission_id = ?",
            (payload.admission_id,),
        )
    ).fetchone()
    if row is None or not (
        row["receipt_id"] == payload.admission_delivery_receipt_id
        and row["receipt_sha256"] == payload.admission_delivery_receipt_sha256
    ):
        await db.rollback()
        raise EvolutionStablePromotionObservationRevisionDeliveryError(
            "stable_promotion_observation_revision_admission_source_mismatch",
            "Control Plane 缺少 exact Runtime Admission Receipt。",
        )


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_stable_promotion_observation_revision_outbox ("
        "submission_id TEXT PRIMARY KEY, submission_sha256 TEXT NOT NULL, "
        "admission_id TEXT NOT NULL, first_sequence INTEGER NOT NULL, "
        "last_sequence INTEGER NOT NULL, submission_json TEXT NOT NULL, "
        "UNIQUE(admission_id, first_sequence))"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_stable_promotion_observation_revision_receipts ("
        "receipt_id TEXT PRIMARY KEY, receipt_sha256 TEXT NOT NULL, "
        "submission_id TEXT NOT NULL UNIQUE, submission_sha256 TEXT NOT NULL, "
        "admission_id TEXT NOT NULL, first_sequence INTEGER NOT NULL, "
        "last_sequence INTEGER NOT NULL, submission_json TEXT NOT NULL, "
        "receipt_json TEXT NOT NULL, UNIQUE(admission_id, first_sequence))"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_stable_promotion_observation_revision_heads ("
        "admission_id TEXT PRIMARY KEY, last_sequence INTEGER NOT NULL, "
        "last_revision_sha256 TEXT NOT NULL, receipt_id TEXT NOT NULL, "
        "receipt_sha256 TEXT NOT NULL)"
    )


def _submission_from_row(row) -> EvolutionStablePromotionObservationRevisionSubmission:
    item = _restore_submission(str(row["submission_json"]))
    if not (
        row["submission_id"] == item.submission_id
        and row["submission_sha256"] == item.submission_sha256
        and row["admission_id"] == item.payload.admission_id
        and int(row["first_sequence"]) == item.payload.first_sequence
        and int(row["last_sequence"]) == item.payload.last_sequence
    ):
        raise EvolutionStablePromotionObservationRevisionDeliveryError(
            "stable_promotion_observation_revision_store_corrupt",
            "Observation revision Submission row identity 不一致。",
        )
    return item


def _receipt_from_row(row) -> EvolutionStablePromotionObservationRevisionDeliveryReceipt:
    item = _restore_receipt(str(row["receipt_json"]))
    if not (
        row["receipt_id"] == item.receipt_id
        and row["receipt_sha256"] == item.receipt_sha256
        and row["submission_id"] == item.submission_id
        and row["admission_id"] == item.admission_id
        and int(row["first_sequence"]) == item.first_sequence
        and int(row["last_sequence"]) == item.last_sequence
    ):
        raise EvolutionStablePromotionObservationRevisionDeliveryError(
            "stable_promotion_observation_revision_store_corrupt",
            "Observation revision Receipt row identity 不一致。",
        )
    return item


def _restore_submission(raw: str):
    try:
        _artifact_bound(raw)
        return EvolutionStablePromotionObservationRevisionSubmission.model_validate_json(raw)
    except (TypeError, ValueError) as exc:
        raise EvolutionStablePromotionObservationRevisionDeliveryError(
            "stable_promotion_observation_revision_store_corrupt",
            "Observation revision Submission artifact 损坏。",
        ) from exc


def _restore_receipt(raw: str):
    try:
        _artifact_bound(raw)
        return EvolutionStablePromotionObservationRevisionDeliveryReceipt.model_validate_json(raw)
    except (TypeError, ValueError) as exc:
        raise EvolutionStablePromotionObservationRevisionDeliveryError(
            "stable_promotion_observation_revision_store_corrupt",
            "Observation revision Receipt artifact 损坏。",
        ) from exc


def _restore_revision(raw: str):
    try:
        return EvolutionStablePromotionObservationChainRevision.model_validate_json(raw)
    except (TypeError, ValueError) as exc:
        raise EvolutionStablePromotionObservationRevisionDeliveryError(
            "stable_promotion_observation_revision_source_corrupt",
            "Durable Cursor revision artifact 损坏。",
        ) from exc


def _submission(value) -> EvolutionStablePromotionObservationRevisionSubmission:
    try:
        return EvolutionStablePromotionObservationRevisionSubmission.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionStablePromotionObservationRevisionDeliveryError(
            "stable_promotion_observation_revision_submission_invalid",
            "Observation revision Submission 无效。",
        ) from exc


def _receipt(value) -> EvolutionStablePromotionObservationRevisionDeliveryReceipt:
    try:
        return EvolutionStablePromotionObservationRevisionDeliveryReceipt.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionStablePromotionObservationRevisionDeliveryError(
            "stable_promotion_observation_revision_receipt_invalid",
            "Observation revision Delivery Receipt 无效。",
        ) from exc


def _admission_id(value: str) -> str:
    normalized = str(value or "").strip()
    if _ADMISSION_RE.fullmatch(normalized) is None:
        raise ValueError("稳定推广 Runtime Admission ID 无效。")
    return normalized


def _submission_id(value: str) -> str:
    normalized = str(value or "").strip()
    if _SUBMISSION_RE.fullmatch(normalized) is None:
        raise ValueError("Observation revision Submission ID 无效。")
    return normalized


def _receipt_id(value: str) -> str:
    normalized = str(value or "").strip()
    if _RECEIPT_RE.fullmatch(normalized) is None:
        raise ValueError("Observation revision Receipt ID 无效。")
    return normalized


def _artifact_bound(value: str | bytes) -> None:
    raw = value.encode() if isinstance(value, str) else value
    if not 1 <= len(raw) <= _MAX_ARTIFACT_BYTES:
        raise ValueError("Observation revision artifact 必须为 1 byte..2 MiB。")


def _aware(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Observation revision timestamp 必须包含 offset。")
    return parsed.astimezone(UTC)


def _canonical(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda item: item.model_dump(mode="json"),
    ).encode()


def _digest(payload: object) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


__all__ = [
    "EVOLUTION_STABLE_PROMOTION_OBSERVATION_REVISION_DELIVERY_POLICY",
    "EvolutionStablePromotionObservationRevisionDeliveryError",
    "EvolutionStablePromotionObservationRevisionDeliveryReceipt",
    "EvolutionStablePromotionObservationRevisionDeliveryService",
    "EvolutionStablePromotionObservationRevisionDeliveryStore",
    "EvolutionStablePromotionObservationRevisionDeliveryView",
    "EvolutionStablePromotionObservationRevisionSubmission",
    "EvolutionStablePromotionObservationRevisionSubmissionPayload",
    "decode_stable_promotion_observation_revision_receipt",
    "decode_stable_promotion_observation_revision_submission",
    "encode_stable_promotion_observation_revision_receipt",
    "encode_stable_promotion_observation_revision_submission",
    "render_stable_promotion_observation_revision_delivery",
    "stable_promotion_observation_revision_receipt_matches_submission",
]
