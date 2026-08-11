"""Signed submission and control-plane receipt for stable runtime admissions."""

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

from naumi_agent.evolution.stable_promotion_observation_contracts import (
    EvolutionStablePromotionObservationContractService,
    EvolutionStablePromotionObservationContractStore,
)
from naumi_agent.evolution.stable_promotion_runtime_observation_admissions import (
    EvolutionStablePromotionRuntimeObservationAdmission,
    EvolutionStablePromotionRuntimeObservationAdmissionService,
    EvolutionStablePromotionRuntimeObservationAdmissionStore,
)
from naumi_agent.evolution.stable_remote_population_finalizations import (
    EvolutionStableRemotePopulationFinalizationService,
)
from naumi_agent.release.installation_keys import (
    RELEASE_INSTALLATION_STABLE_PROMOTION_ADMISSION_SIGNATURE_DOMAIN,
    ReleaseInstallationKeyService,
    ReleaseInstallationSignature,
    verify_release_installation_signature,
)
from naumi_agent.release.population_registry import ReleasePopulationSnapshotStore

EVOLUTION_STABLE_PROMOTION_RUNTIME_ADMISSION_DELIVERY_POLICY = (
    "evolution-stable-promotion-runtime-admission-delivery-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_SUBMISSION_BYTES = 64 * 1024
_MAX_ENCODED_CHARS = ((_MAX_SUBMISSION_BYTES + 2) // 3) * 4


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionStablePromotionRuntimeAdmissionSubmissionPayload(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-stable-promotion-runtime-admission-delivery-v1"
    ] = EVOLUTION_STABLE_PROMOTION_RUNTIME_ADMISSION_DELIVERY_POLICY
    admission: EvolutionStablePromotionRuntimeObservationAdmission
    submitted_at: str = Field(min_length=1, max_length=100)
    installation_local_harness_attested: Literal[True] = True
    remote_harness_directly_revalidated: Literal[False] = False
    observation_window_authority: Literal[False] = False
    promoted_outcome_authority: Literal[False] = False
    execution_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if _aware(self.submitted_at) < _aware(self.admission.admitted_at):
            raise ValueError("Runtime Admission Submission 早于 Admission。")
        if any(
            (
                self.remote_harness_directly_revalidated,
                self.observation_window_authority,
                self.promoted_outcome_authority,
                self.execution_authority,
            )
        ):
            raise ValueError("Runtime Admission Submission 不得扩张 authority。")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical(self.model_dump(mode="json"))


class EvolutionStablePromotionRuntimeAdmissionSubmission(_StrictModel):
    schema_version: Literal[1] = 1
    submission_id: str = Field(pattern=r"^evstablepromsubmit_[0-9a-f]{24}$")
    submission_sha256: str = Field(pattern=_SHA256_RE)
    payload: EvolutionStablePromotionRuntimeAdmissionSubmissionPayload
    signature: ReleaseInstallationSignature

    @model_validator(mode="after")
    def _binding(self) -> Self:
        payload = self.payload.canonical_bytes()
        admission = self.payload.admission
        if not (
            self.signature.domain
            == RELEASE_INSTALLATION_STABLE_PROMOTION_ADMISSION_SIGNATURE_DOMAIN
            and self.signature.installation_member_id
            == admission.installation_member_id
            and self.signature.payload_sha256 == hashlib.sha256(payload).hexdigest()
            and self.signature.payload_bytes == len(payload)
            and _aware(self.payload.submitted_at) <= _aware(self.signature.signed_at)
        ):
            raise ValueError("Runtime Admission Submission signature binding 无效。")
        digest = _digest(
            self.model_dump(
                mode="json",
                exclude={"submission_id", "submission_sha256"},
            )
        )
        if not (
            hmac.compare_digest(self.submission_sha256, digest)
            and self.submission_id == f"evstablepromsubmit_{digest[:24]}"
        ):
            raise ValueError("Runtime Admission Submission content identity 不一致。")
        return self


class EvolutionStablePromotionRuntimeAdmissionDeliveryReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-stable-promotion-runtime-admission-delivery-v1"
    ] = EVOLUTION_STABLE_PROMOTION_RUNTIME_ADMISSION_DELIVERY_POLICY
    receipt_id: str = Field(pattern=r"^evstablepromreceive_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    submission_id: str = Field(pattern=r"^evstablepromsubmit_[0-9a-f]{24}$")
    submission_sha256: str = Field(pattern=_SHA256_RE)
    signature_id: str = Field(pattern=r"^relinstallsig_[0-9a-f]{24}$")
    signature_artifact_sha256: str = Field(pattern=_SHA256_RE)
    admission_id: str = Field(pattern=r"^evstablepromadmit_[0-9a-f]{24}$")
    admission_sha256: str = Field(pattern=_SHA256_RE)
    observation_contract_id: str = Field(pattern=r"^evstablepromobserve_[0-9a-f]{24}$")
    observation_contract_sha256: str = Field(pattern=_SHA256_RE)
    population_finalization_receipt_id: str = Field(
        pattern=r"^evstableremotepopfinal_[0-9a-f]{24}$"
    )
    population_finalization_receipt_sha256: str = Field(pattern=_SHA256_RE)
    population_snapshot_id: str = Field(pattern=r"^relpopsnapshot_[0-9a-f]{24}$")
    population_snapshot_sha256: str = Field(pattern=_SHA256_RE)
    installation_member_id: str = Field(pattern=r"^relpopmember_[0-9a-f]{24}$")
    installation_credential_id: str = Field(pattern=r"^relpopcred_[0-9a-f]{24}$")
    installation_credential_sha256: str = Field(pattern=_SHA256_RE)
    installation_public_key_sha256: str = Field(pattern=_SHA256_RE)
    origin_sample_id: str = Field(pattern=r"^hrreleaseobservation_[0-9a-f]{24}$")
    origin_sample_sha256: str = Field(pattern=_SHA256_RE)
    received_at: str = Field(min_length=1, max_length=100)
    installation_signature_verified: Literal[True] = True
    current_population_credential_verified: Literal[True] = True
    exact_contract_member_verified: Literal[True] = True
    remote_admission_received: Literal[True] = True
    remote_harness_directly_revalidated: Literal[False] = False
    observation_window_authority: Literal[False] = False
    long_term_metrics_authority: Literal[False] = False
    promoted_outcome_authority: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    execution_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        _aware(self.received_at)
        if any(
            (
                self.remote_harness_directly_revalidated,
                self.observation_window_authority,
                self.long_term_metrics_authority,
                self.promoted_outcome_authority,
                self.learning_authority,
                self.promotion_authority,
                self.execution_authority,
            )
        ):
            raise ValueError("Runtime Admission Delivery Receipt 不得越权。")
        digest = _digest(
            self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        )
        if not (
            hmac.compare_digest(self.receipt_sha256, digest)
            and self.receipt_id == f"evstablepromreceive_{digest[:24]}"
        ):
            raise ValueError("Runtime Admission Delivery Receipt identity 不一致。")
        return self


class EvolutionStablePromotionRuntimeAdmissionDeliveryView(_StrictModel):
    submission: EvolutionStablePromotionRuntimeAdmissionSubmission
    receipt: EvolutionStablePromotionRuntimeAdmissionDeliveryReceipt
    status: Literal["received", "stale"]
    durable_receipt_valid: bool
    observation_contract_authority: bool
    population_member_authority: bool
    current_credential_authority: bool
    installation_signature_authority: bool
    remote_admission_delivery_authority: bool
    remote_harness_directly_revalidated: Literal[False] = False
    observation_window_authority: Literal[False] = False
    long_term_metrics_authority: Literal[False] = False
    promoted_outcome_authority: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    execution_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        expected = bool(
            self.durable_receipt_valid
            and self.observation_contract_authority
            and self.population_member_authority
            and self.current_credential_authority
            and self.installation_signature_authority
        )
        if not (
            self.remote_admission_delivery_authority is expected
            and (self.status == "received") is expected
        ):
            raise ValueError("Runtime Admission Delivery authority projection 不一致。")
        return self


class EvolutionStablePromotionRuntimeAdmissionDeliveryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionStablePromotionRuntimeAdmissionDeliveryStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def get_outbound(
        self, admission_id: str
    ) -> EvolutionStablePromotionRuntimeAdmissionSubmission | None:
        item_id = _admission_id(admission_id)
        row = await self._read_one(
            "SELECT * FROM "
            "evolution_stable_promotion_runtime_admission_outbox "
            "WHERE admission_id = ?",
            (item_id,),
        )
        if row is None:
            return None
        item = _restore_submission(row["submission_json"])
        if not (
            item.submission_id == row["submission_id"]
            and item.submission_sha256 == row["submission_sha256"]
            and item.payload.admission.admission_id == row["admission_id"]
            and item.payload.admission.installation_member_id
            == row["installation_member_id"]
            and item.payload.submitted_at == row["submitted_at"]
        ):
            raise EvolutionStablePromotionRuntimeAdmissionDeliveryError(
                "stable_promotion_admission_delivery_store_corrupt",
                "Runtime Admission Delivery outbound row identity 不一致。",
            )
        return item

    async def stage_outbound(
        self, submission: EvolutionStablePromotionRuntimeAdmissionSubmission
    ) -> EvolutionStablePromotionRuntimeAdmissionSubmission:
        item = _submission(submission)
        encoded = item.model_dump_json()
        _bounded(encoded)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                admission = item.payload.admission
                source = await (
                    await db.execute(
                        "SELECT admission_sha256 FROM "
                        "evolution_stable_promotion_runtime_admissions "
                        "WHERE admission_id = ?",
                        (admission.admission_id,),
                    )
                ).fetchone()
                if source is None or source["admission_sha256"] != admission.admission_sha256:
                    await db.rollback()
                    raise EvolutionStablePromotionRuntimeAdmissionDeliveryError(
                        "stable_promotion_admission_delivery_source_mismatch",
                        "Outbound Submission 缺少 exact durable Admission。",
                    )
                existing = await (
                    await db.execute(
                        "SELECT submission_json FROM "
                        "evolution_stable_promotion_runtime_admission_outbox "
                        "WHERE admission_id = ?",
                        (admission.admission_id,),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _restore_submission(existing["submission_json"])
                    await db.rollback()
                    if restored == item:
                        return restored
                    raise EvolutionStablePromotionRuntimeAdmissionDeliveryError(
                        "stable_promotion_admission_delivery_outbound_conflict",
                        "同一 Admission 已绑定不同 signed Submission。",
                    )
                await db.execute(
                    "INSERT INTO evolution_stable_promotion_runtime_admission_outbox "
                    "(submission_id, submission_sha256, admission_id, "
                    "installation_member_id, submission_json, submitted_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        item.submission_id,
                        item.submission_sha256,
                        admission.admission_id,
                        admission.installation_member_id,
                        encoded,
                        item.payload.submitted_at,
                    ),
                )
                await db.commit()
            return item
        except EvolutionStablePromotionRuntimeAdmissionDeliveryError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionRuntimeAdmissionDeliveryError(
                "stable_promotion_admission_delivery_outbox_failed",
                "Signed Runtime Admission Submission 无法持久化。",
            ) from exc

    async def get_received(
        self, admission_id: str
    ) -> tuple[
        EvolutionStablePromotionRuntimeAdmissionSubmission,
        EvolutionStablePromotionRuntimeAdmissionDeliveryReceipt,
    ] | None:
        item_id = _admission_id(admission_id)
        row = await self._read_one(
            "SELECT * FROM evolution_stable_promotion_runtime_admission_receipts "
            "WHERE admission_id = ?",
            (item_id,),
        )
        if row is None:
            return None
        submission = _restore_submission(row["submission_json"])
        receipt = _restore_receipt(row["receipt_json"])
        if not (
            submission.submission_id == row["submission_id"]
            and submission.submission_sha256 == row["submission_sha256"]
            and receipt.receipt_id == row["receipt_id"]
            and receipt.receipt_sha256 == row["receipt_sha256"]
            and receipt.admission_id == row["admission_id"]
            and receipt.submission_id == submission.submission_id
            and receipt.observation_contract_id == row["observation_contract_id"]
            and receipt.installation_member_id == row["installation_member_id"]
            and receipt.received_at == row["received_at"]
        ):
            raise EvolutionStablePromotionRuntimeAdmissionDeliveryError(
                "stable_promotion_admission_delivery_store_corrupt",
                "Runtime Admission Delivery durable row identity 不一致。",
            )
        return submission, receipt

    async def receive(
        self,
        submission: EvolutionStablePromotionRuntimeAdmissionSubmission,
        receipt: EvolutionStablePromotionRuntimeAdmissionDeliveryReceipt,
    ) -> tuple[
        EvolutionStablePromotionRuntimeAdmissionSubmission,
        EvolutionStablePromotionRuntimeAdmissionDeliveryReceipt,
    ]:
        item = _submission(submission)
        received = _receipt(receipt)
        if not _receipt_matches_submission(received, item):
            raise EvolutionStablePromotionRuntimeAdmissionDeliveryError(
                "stable_promotion_admission_delivery_receipt_mismatch",
                "Delivery Receipt 与 signed Submission 不一致。",
            )
        submission_json = item.model_dump_json()
        receipt_json = received.model_dump_json()
        _bounded(submission_json)
        _bounded(receipt_json)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                await _require_control_plane_sources(db, item.payload.admission)
                existing = await (
                    await db.execute(
                        "SELECT submission_json, receipt_json FROM "
                        "evolution_stable_promotion_runtime_admission_receipts "
                        "WHERE admission_id = ?",
                        (received.admission_id,),
                    )
                ).fetchone()
                if existing is not None:
                    old_submission = _restore_submission(existing["submission_json"])
                    old_receipt = _restore_receipt(existing["receipt_json"])
                    await db.rollback()
                    if old_submission == item:
                        return old_submission, old_receipt
                    raise EvolutionStablePromotionRuntimeAdmissionDeliveryError(
                        "stable_promotion_admission_delivery_receive_conflict",
                        "同一 Admission 已接收不同 signed Submission。",
                    )
                await db.execute(
                    "INSERT INTO evolution_stable_promotion_runtime_admission_receipts "
                    "(receipt_id, receipt_sha256, submission_id, submission_sha256, "
                    "admission_id, observation_contract_id, installation_member_id, "
                    "submission_json, receipt_json, received_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        received.receipt_id,
                        received.receipt_sha256,
                        item.submission_id,
                        item.submission_sha256,
                        received.admission_id,
                        received.observation_contract_id,
                        received.installation_member_id,
                        submission_json,
                        receipt_json,
                        received.received_at,
                    ),
                )
                await db.commit()
            return item, received
        except EvolutionStablePromotionRuntimeAdmissionDeliveryError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionRuntimeAdmissionDeliveryError(
                "stable_promotion_admission_delivery_receive_failed",
                "Runtime Admission Delivery Receipt 无法持久化。",
            ) from exc

    async def _read_one(self, query: str, params: tuple[str, ...]):
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                return await (await db.execute(query, params)).fetchone()
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionRuntimeAdmissionDeliveryError(
                "stable_promotion_admission_delivery_store_corrupt",
                "Runtime Admission Delivery Store 损坏或无法读取。",
            ) from exc


class EvolutionStablePromotionRuntimeAdmissionDeliveryService:
    def __init__(
        self,
        *,
        admission_store: EvolutionStablePromotionRuntimeObservationAdmissionStore,
        admission_service: EvolutionStablePromotionRuntimeObservationAdmissionService,
        contract_store: EvolutionStablePromotionObservationContractStore,
        contract_service: EvolutionStablePromotionObservationContractService,
        finalization_service: EvolutionStableRemotePopulationFinalizationService,
        population_store: ReleasePopulationSnapshotStore,
        installation_key_service: ReleaseInstallationKeyService,
        store: EvolutionStablePromotionRuntimeAdmissionDeliveryStore,
    ) -> None:
        paths = {
            admission_store.db_path,
            contract_store.db_path,
            finalization_service.store.db_path,
            finalization_service.member_store.db_path,
            store.db_path,
        }
        if len(paths) != 1:
            raise ValueError("Runtime Admission Delivery sources 必须共享 session SQLite。")
        if not (
            admission_service.store is admission_store
            and admission_service.contract_store is contract_store
            and contract_service.store is contract_store
            and contract_service.finalization_service is finalization_service
        ):
            raise ValueError("Runtime Admission Delivery authority composition 不一致。")
        self.admission_store = admission_store
        self.admission_service = admission_service
        self.contract_store = contract_store
        self.contract_service = contract_service
        self.finalization_service = finalization_service
        self.population_store = population_store
        self.installation_key_service = installation_key_service
        self.store = store

    async def prepare(
        self, *, admission_id: str
    ) -> EvolutionStablePromotionRuntimeAdmissionSubmission:
        item_id = _admission_id(admission_id)
        existing = await self.store.get_outbound(item_id)
        admission = await self.admission_store.get_by_id(item_id)
        if admission is None:
            raise EvolutionStablePromotionRuntimeAdmissionDeliveryError(
                "stable_promotion_admission_delivery_admission_missing",
                "指定 Runtime Admission 不存在。",
            )
        view = await self.admission_service.inspect(admission=admission)
        if not view.runtime_observation_input_authority:
            raise EvolutionStablePromotionRuntimeAdmissionDeliveryError(
                "stable_promotion_admission_delivery_admission_stale",
                "Runtime Admission 当前不具备 delivery authority。",
            )
        contract, member, credential = await self._current_sources(admission)
        if not _admission_matches_sources(admission, contract, member, credential):
            raise EvolutionStablePromotionRuntimeAdmissionDeliveryError(
                "stable_promotion_admission_delivery_lineage_mismatch",
                "Runtime Admission 与 current Contract/member credential 不一致。",
            )
        if existing is not None:
            if existing.payload.admission != admission:
                raise EvolutionStablePromotionRuntimeAdmissionDeliveryError(
                    "stable_promotion_admission_delivery_outbound_conflict",
                    "既有 signed Submission 与 current Admission 不一致。",
                )
            try:
                verify_release_installation_signature(
                    credential=credential,
                    payload=existing.payload.canonical_bytes(),
                    artifact=existing.signature,
                    expected_domain=(
                        RELEASE_INSTALLATION_STABLE_PROMOTION_ADMISSION_SIGNATURE_DOMAIN
                    ),
                )
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                raise EvolutionStablePromotionRuntimeAdmissionDeliveryError(
                    "stable_promotion_admission_delivery_signature_untrusted",
                    "既有 Runtime Admission Submission 已无法由 current Credential 验证。",
                ) from exc
            return existing
        payload = EvolutionStablePromotionRuntimeAdmissionSubmissionPayload(
            admission=admission,
            submitted_at=_aware(self.installation_key_service.clock()).isoformat(),
        )
        signature = self.installation_key_service.sign_stable_promotion_runtime_admission(
            credential=credential,
            payload=payload.canonical_bytes(),
        )
        core = {
            "schema_version": 1,
            "payload": payload.model_dump(mode="json"),
            "signature": signature.model_dump(mode="json"),
        }
        digest = _digest(core)
        submission = EvolutionStablePromotionRuntimeAdmissionSubmission.model_validate(
            {
                **core,
                "submission_id": f"evstablepromsubmit_{digest[:24]}",
                "submission_sha256": digest,
            }
        )
        return await self.store.stage_outbound(submission)

    async def receive(
        self,
        *,
        submission: EvolutionStablePromotionRuntimeAdmissionSubmission,
        received_at: str | datetime | None = None,
    ) -> EvolutionStablePromotionRuntimeAdmissionDeliveryView:
        item = _submission(submission)
        admission = item.payload.admission
        contract, member, credential = await self._current_sources(admission)
        if not _admission_matches_sources(admission, contract, member, credential):
            raise EvolutionStablePromotionRuntimeAdmissionDeliveryError(
                "stable_promotion_admission_delivery_lineage_mismatch",
                "Signed Admission 与 current Contract/member credential 不一致。",
            )
        try:
            verify_release_installation_signature(
                credential=credential,
                payload=item.payload.canonical_bytes(),
                artifact=item.signature,
                expected_domain=(
                    RELEASE_INSTALLATION_STABLE_PROMOTION_ADMISSION_SIGNATURE_DOMAIN
                ),
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionRuntimeAdmissionDeliveryError(
                "stable_promotion_admission_delivery_signature_untrusted",
                "Runtime Admission Submission 无法由 current Population Credential 验证。",
            ) from exc
        existing = await self.store.get_received(admission.admission_id)
        if existing is not None:
            stored_submission, stored_receipt = existing
            if stored_submission != item:
                raise EvolutionStablePromotionRuntimeAdmissionDeliveryError(
                    "stable_promotion_admission_delivery_receive_conflict",
                    "同一 Admission 已接收不同 signed Submission。",
                )
            return await self.inspect(
                submission=stored_submission,
                receipt=stored_receipt,
            )
        now = _aware(received_at or datetime.now(UTC))
        if now < _aware(item.signature.signed_at):
            raise EvolutionStablePromotionRuntimeAdmissionDeliveryError(
                "stable_promotion_admission_delivery_received_before_signed",
                "Runtime Admission Delivery 接收时间早于签名时间。",
            )
        receipt = _build_receipt(item, credential, received_at=now)
        stored_submission, stored_receipt = await self.store.receive(item, receipt)
        return await self.inspect(
            submission=stored_submission,
            receipt=stored_receipt,
        )

    async def inspect(
        self,
        *,
        submission: EvolutionStablePromotionRuntimeAdmissionSubmission,
        receipt: EvolutionStablePromotionRuntimeAdmissionDeliveryReceipt,
    ) -> EvolutionStablePromotionRuntimeAdmissionDeliveryView:
        item = _submission(submission)
        received = _receipt(receipt)
        durable = contract_authority = member_authority = False
        credential_authority = signature_authority = False
        try:
            stored = await self.store.get_received(item.payload.admission.admission_id)
            durable = bool(
                stored is not None
                and stored == (item, received)
                and _receipt_matches_submission(received, item)
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            durable = False
        admission = item.payload.admission
        contract = member = credential = None
        try:
            contract = await self.contract_store.get_by_finalization(
                admission.population_finalization_receipt_id
            )
            if contract is None:
                raise ValueError("stable promotion observation contract missing")
            contract_view = await self.contract_service.inspect(contract=contract)
            contract_authority = bool(
                contract_view.observation_contract_authority
                and contract.contract_id == received.observation_contract_id
                and contract.contract_sha256 == received.observation_contract_sha256
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            contract = None
        try:
            finalization_view = await self.finalization_service.inspect(
                receipt_id=admission.population_finalization_receipt_id
            )
            member = next(
                (
                    source
                    for source in finalization_view.receipt.members
                    if source.installation_member_id
                    == admission.installation_member_id
                ),
                None,
            )
            member_authority = bool(
                finalization_view.stable_population_finalization_authority
                and finalization_view.receipt.receipt_id
                == admission.population_finalization_receipt_id
                and finalization_view.receipt.receipt_sha256
                == admission.population_finalization_receipt_sha256
                and member is not None
                and member.installation_member_id == received.installation_member_id
                and member.member_source_sha256 == admission.member_source_sha256
                and member.member_receipt_id == admission.member_receipt_id
                and member.member_receipt_sha256 == admission.member_receipt_sha256
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            member = None
        try:
            population_view = await self.population_store.inspect(
                snapshot_id=admission.population_snapshot_id
            )
            credential = next(
                (
                    source
                    for source in population_view.snapshot.payload.credentials
                    if source.payload.member_id == admission.installation_member_id
                ),
                None,
            )
            credential_authority = bool(
                population_view.population_snapshot_authority
                and population_view.snapshot.snapshot_id
                == admission.population_snapshot_id
                and population_view.snapshot.snapshot_sha256
                == admission.population_snapshot_sha256
                and credential is not None
                and credential.credential_id == received.installation_credential_id
                and credential.credential_sha256
                == received.installation_credential_sha256
                and credential.payload.installation_public_key_sha256
                == received.installation_public_key_sha256
                and member is not None
                and member.installation_credential_id == credential.credential_id
                and member.installation_credential_sha256
                == credential.credential_sha256
                and member.installation_public_key_sha256
                == credential.payload.installation_public_key_sha256
                and item.signature.credential_id == credential.credential_id
                and item.signature.credential_sha256 == credential.credential_sha256
                and item.signature.public_key_sha256
                == credential.payload.installation_public_key_sha256
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            credential = None
        try:
            if credential is None:
                raise ValueError("stable promotion credential missing")
            verify_release_installation_signature(
                credential=credential,
                payload=item.payload.canonical_bytes(),
                artifact=item.signature,
                expected_domain=(
                    RELEASE_INSTALLATION_STABLE_PROMOTION_ADMISSION_SIGNATURE_DOMAIN
                ),
            )
            signature_authority = True
        except (OSError, RuntimeError, TypeError, ValueError):
            signature_authority = False
        authority = bool(
            durable
            and contract_authority
            and member_authority
            and credential_authority
            and signature_authority
        )
        return EvolutionStablePromotionRuntimeAdmissionDeliveryView(
            submission=item,
            receipt=received,
            status="received" if authority else "stale",
            durable_receipt_valid=durable,
            observation_contract_authority=contract_authority,
            population_member_authority=member_authority,
            current_credential_authority=credential_authority,
            installation_signature_authority=signature_authority,
            remote_admission_delivery_authority=authority,
        )

    async def _current_sources(self, admission):
        contract = await self.contract_store.get_by_finalization(
            admission.population_finalization_receipt_id
        )
        if contract is None:
            raise EvolutionStablePromotionRuntimeAdmissionDeliveryError(
                "stable_promotion_admission_delivery_contract_missing",
                "Stable Promotion Observation Contract 不存在。",
            )
        contract_view = await self.contract_service.inspect(contract=contract)
        finalization_view = await self.finalization_service.inspect(
            receipt_id=admission.population_finalization_receipt_id
        )
        if not (
            contract_view.observation_contract_authority
            and finalization_view.stable_population_finalization_authority
        ):
            raise EvolutionStablePromotionRuntimeAdmissionDeliveryError(
                "stable_promotion_admission_delivery_sources_stale",
                "Observation Contract 或 Population Finalization authority 已失效。",
            )
        member = next(
            (
                source
                for source in finalization_view.receipt.members
                if source.installation_member_id == admission.installation_member_id
            ),
            None,
        )
        population_view = await self.population_store.inspect(
            snapshot_id=admission.population_snapshot_id
        )
        credential = next(
            (
                source
                for source in population_view.snapshot.payload.credentials
                if source.payload.member_id == admission.installation_member_id
            ),
            None,
        )
        if not (
            member is not None
            and credential is not None
            and population_view.population_snapshot_authority
        ):
            raise EvolutionStablePromotionRuntimeAdmissionDeliveryError(
                "stable_promotion_admission_delivery_credential_missing",
                "Current Population Credential 不存在或已失效。",
            )
        return contract, member, credential


def encode_stable_promotion_runtime_admission_submission(submission) -> str:
    item = _submission(submission)
    raw = item.model_dump_json().encode("utf-8")
    _bounded(raw)
    return base64.b64encode(raw).decode("ascii")


def decode_stable_promotion_runtime_admission_submission(encoded: str):
    value = str(encoded or "").strip()
    if not value or len(value) > _MAX_ENCODED_CHARS:
        raise EvolutionStablePromotionRuntimeAdmissionDeliveryError(
            "stable_promotion_admission_delivery_payload_invalid",
            "Runtime Admission Submission 编码为空或超过上限。",
        )
    try:
        raw = base64.b64decode(value, validate=True)
        _bounded(raw)
        if base64.b64encode(raw).decode("ascii") != value:
            raise ValueError("non-canonical base64")
        return EvolutionStablePromotionRuntimeAdmissionSubmission.model_validate_json(raw)
    except (binascii.Error, TypeError, ValueError) as exc:
        raise EvolutionStablePromotionRuntimeAdmissionDeliveryError(
            "stable_promotion_admission_delivery_payload_invalid",
            "Runtime Admission Submission 编码或内容无效。",
        ) from exc


def render_stable_promotion_runtime_admission_submission(submission) -> str:
    item = _submission(submission)
    admission = item.payload.admission
    return "\n".join(
        [
            f"# Stable Promotion Runtime Admission Submission `{item.submission_id}`",
            "",
            f"- Admission：`{admission.admission_id}`",
            f"- Population Member：`{admission.installation_member_id}`",
            f"- Runtime Subject：`{admission.subject_id}`",
            f"- Signature：`{item.signature.signature_id}` · `ed25519`",
            "- Remote Harness directly revalidated：`false`",
            "- Window / Promoted Outcome / Execution authority：`false / false / false`",
        ]
    )


def render_stable_promotion_runtime_admission_delivery(view) -> str:
    item = view.receipt
    return "\n".join(
        [
            f"# Stable Promotion Runtime Admission Delivery `{item.receipt_id}`",
            "",
            f"- 状态：`{view.status}`",
            f"- Submission / Admission：`{item.submission_id}` / `{item.admission_id}`",
            f"- Population Member：`{item.installation_member_id}`",
            f"- Credential：`{item.installation_credential_id}`",
            "- Contract / Member / Credential / Signature authority："
            f"`{str(view.observation_contract_authority).lower()} / "
            f"{str(view.population_member_authority).lower()} / "
            f"{str(view.current_credential_authority).lower()} / "
            f"{str(view.installation_signature_authority).lower()}`",
            "- Remote Admission delivery authority："
            f"`{str(view.remote_admission_delivery_authority).lower()}`",
            "- Remote Harness directly revalidated：`false`",
            "- Window / Long-term / Promoted Outcome authority：`false / false / false`",
            "- Learning / Promotion / Execution authority：`false / false / false`",
        ]
    )


def _build_receipt(submission, credential, *, received_at):
    admission = submission.payload.admission
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_STABLE_PROMOTION_RUNTIME_ADMISSION_DELIVERY_POLICY,
        "submission_id": submission.submission_id,
        "submission_sha256": submission.submission_sha256,
        "signature_id": submission.signature.signature_id,
        "signature_artifact_sha256": submission.signature.signature_artifact_sha256,
        "admission_id": admission.admission_id,
        "admission_sha256": admission.admission_sha256,
        "observation_contract_id": admission.observation_contract_id,
        "observation_contract_sha256": admission.observation_contract_sha256,
        "population_finalization_receipt_id": (
            admission.population_finalization_receipt_id
        ),
        "population_finalization_receipt_sha256": (
            admission.population_finalization_receipt_sha256
        ),
        "population_snapshot_id": admission.population_snapshot_id,
        "population_snapshot_sha256": admission.population_snapshot_sha256,
        "installation_member_id": admission.installation_member_id,
        "installation_credential_id": credential.credential_id,
        "installation_credential_sha256": credential.credential_sha256,
        "installation_public_key_sha256": (
            credential.payload.installation_public_key_sha256
        ),
        "origin_sample_id": admission.origin_sample_id,
        "origin_sample_sha256": admission.origin_sample_sha256,
        "received_at": _aware(received_at).isoformat(),
        "installation_signature_verified": True,
        "current_population_credential_verified": True,
        "exact_contract_member_verified": True,
        "remote_admission_received": True,
        "remote_harness_directly_revalidated": False,
        "observation_window_authority": False,
        "long_term_metrics_authority": False,
        "promoted_outcome_authority": False,
        "learning_authority": False,
        "promotion_authority": False,
        "execution_authority": False,
    }
    digest = _digest(core)
    return EvolutionStablePromotionRuntimeAdmissionDeliveryReceipt.model_validate(
        {
            **core,
            "receipt_id": f"evstablepromreceive_{digest[:24]}",
            "receipt_sha256": digest,
        }
    )


def _admission_matches_sources(admission, contract, member, credential) -> bool:
    return bool(
        admission.observation_contract_id == contract.contract_id
        and admission.observation_contract_sha256 == contract.contract_sha256
        and admission.population_finalization_receipt_id
        == contract.population_finalization_receipt_id
        and admission.population_finalization_receipt_sha256
        == contract.population_finalization_receipt_sha256
        and admission.population_snapshot_id
        == contract.population_snapshot_id
        and admission.population_snapshot_sha256
        == contract.population_snapshot_sha256
        and admission.installation_member_id
        == member.installation_member_id
        == credential.payload.member_id
        and admission.member_source_sha256 == member.member_source_sha256
        and admission.member_receipt_id == member.member_receipt_id
        and admission.member_receipt_sha256 == member.member_receipt_sha256
        and member.installation_credential_id == credential.credential_id
        and member.installation_credential_sha256 == credential.credential_sha256
        and member.installation_public_key_sha256
        == credential.payload.installation_public_key_sha256
    )


def _receipt_matches_submission(receipt, submission) -> bool:
    admission = submission.payload.admission
    return bool(
        receipt.submission_id == submission.submission_id
        and receipt.submission_sha256 == submission.submission_sha256
        and receipt.signature_id == submission.signature.signature_id
        and receipt.signature_artifact_sha256
        == submission.signature.signature_artifact_sha256
        and receipt.admission_id == admission.admission_id
        and receipt.admission_sha256 == admission.admission_sha256
        and receipt.observation_contract_id == admission.observation_contract_id
        and receipt.observation_contract_sha256
        == admission.observation_contract_sha256
        and receipt.population_finalization_receipt_id
        == admission.population_finalization_receipt_id
        and receipt.population_finalization_receipt_sha256
        == admission.population_finalization_receipt_sha256
        and receipt.population_snapshot_id == admission.population_snapshot_id
        and receipt.population_snapshot_sha256
        == admission.population_snapshot_sha256
        and receipt.installation_member_id == admission.installation_member_id
        and receipt.installation_credential_id == submission.signature.credential_id
        and receipt.installation_credential_sha256
        == submission.signature.credential_sha256
        and receipt.installation_public_key_sha256
        == submission.signature.public_key_sha256
        and receipt.origin_sample_id == admission.origin_sample_id
        and receipt.origin_sample_sha256 == admission.origin_sample_sha256
        and _aware(receipt.received_at) >= _aware(submission.signature.signed_at)
    )


async def _require_control_plane_sources(db, admission) -> None:
    checks = (
        (
            "SELECT contract_sha256 FROM evolution_stable_promotion_observation_contracts "
            "WHERE contract_id = ?",
            (admission.observation_contract_id,),
            admission.observation_contract_sha256,
        ),
        (
            "SELECT receipt_sha256 FROM evolution_stable_remote_population_finalizations "
            "WHERE receipt_id = ?",
            (admission.population_finalization_receipt_id,),
            admission.population_finalization_receipt_sha256,
        ),
        (
            "SELECT receipt_sha256 FROM evolution_stable_remote_finalization_receipts "
            "WHERE receipt_id = ?",
            (admission.member_receipt_id,),
            admission.member_receipt_sha256,
        ),
    )
    for query, params, expected in checks:
        row = await (await db.execute(query, params)).fetchone()
        if row is None or row[0] != expected:
            await db.rollback()
            raise EvolutionStablePromotionRuntimeAdmissionDeliveryError(
                "stable_promotion_admission_delivery_control_source_mismatch",
                "Control plane 缺少 exact Contract/Finalization durable source。",
            )


def _submission(value) -> EvolutionStablePromotionRuntimeAdmissionSubmission:
    try:
        return EvolutionStablePromotionRuntimeAdmissionSubmission.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionStablePromotionRuntimeAdmissionDeliveryError(
            "stable_promotion_admission_delivery_submission_invalid",
            "Runtime Admission Submission 无效。",
        ) from exc


def _receipt(value) -> EvolutionStablePromotionRuntimeAdmissionDeliveryReceipt:
    try:
        return EvolutionStablePromotionRuntimeAdmissionDeliveryReceipt.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionStablePromotionRuntimeAdmissionDeliveryError(
            "stable_promotion_admission_delivery_receipt_invalid",
            "Runtime Admission Delivery Receipt 无效。",
        ) from exc


def _restore_submission(raw: str):
    try:
        _bounded(raw)
        return EvolutionStablePromotionRuntimeAdmissionSubmission.model_validate_json(raw)
    except (RuntimeError, TypeError, ValueError) as exc:
        raise EvolutionStablePromotionRuntimeAdmissionDeliveryError(
            "stable_promotion_admission_delivery_store_corrupt",
            "Runtime Admission Delivery Submission durable JSON 无效。",
        ) from exc


def _restore_receipt(raw: str):
    try:
        _bounded(raw)
        return EvolutionStablePromotionRuntimeAdmissionDeliveryReceipt.model_validate_json(
            raw
        )
    except (RuntimeError, TypeError, ValueError) as exc:
        raise EvolutionStablePromotionRuntimeAdmissionDeliveryError(
            "stable_promotion_admission_delivery_store_corrupt",
            "Runtime Admission Delivery Receipt durable JSON 无效。",
        ) from exc


def _admission_id(value: str) -> str:
    normalized = str(value or "").strip()
    if re.fullmatch(r"^evstablepromadmit_[0-9a-f]{24}$", normalized) is None:
        raise EvolutionStablePromotionRuntimeAdmissionDeliveryError(
            "stable_promotion_admission_delivery_admission_id_invalid",
            "Stable Promotion Runtime Admission ID 格式无效。",
        )
    return normalized


def _bounded(value: str | bytes) -> None:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    if not 1 <= len(raw) <= _MAX_SUBMISSION_BYTES:
        raise EvolutionStablePromotionRuntimeAdmissionDeliveryError(
            "stable_promotion_admission_delivery_payload_oversized",
            "Runtime Admission Delivery payload 必须为 1..65536 bytes。",
        )


def _aware(value) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(parsed, datetime) or parsed.utcoffset() is None:
        raise ValueError("Runtime Admission Delivery timestamp 必须包含 offset。")
    return parsed.astimezone(UTC)


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


async def _ensure_schema(db) -> None:
    await db.executescript(
        "CREATE TABLE IF NOT EXISTS evolution_stable_promotion_runtime_admission_outbox ("
        "submission_id TEXT PRIMARY KEY, submission_sha256 TEXT NOT NULL UNIQUE, "
        "admission_id TEXT NOT NULL UNIQUE, installation_member_id TEXT NOT NULL, "
        "submission_json TEXT NOT NULL, submitted_at TEXT NOT NULL);"
        "CREATE TABLE IF NOT EXISTS evolution_stable_promotion_runtime_admission_receipts ("
        "receipt_id TEXT PRIMARY KEY, receipt_sha256 TEXT NOT NULL UNIQUE, "
        "submission_id TEXT NOT NULL UNIQUE, submission_sha256 TEXT NOT NULL UNIQUE, "
        "admission_id TEXT NOT NULL UNIQUE, observation_contract_id TEXT NOT NULL, "
        "installation_member_id TEXT NOT NULL, submission_json TEXT NOT NULL, "
        "receipt_json TEXT NOT NULL, received_at TEXT NOT NULL);"
        "CREATE INDEX IF NOT EXISTS idx_stable_promotion_admission_receipt_contract_member "
        "ON evolution_stable_promotion_runtime_admission_receipts("
        "observation_contract_id, installation_member_id, received_at);"
    )


__all__ = [
    "EVOLUTION_STABLE_PROMOTION_RUNTIME_ADMISSION_DELIVERY_POLICY",
    "EvolutionStablePromotionRuntimeAdmissionDeliveryError",
    "EvolutionStablePromotionRuntimeAdmissionDeliveryReceipt",
    "EvolutionStablePromotionRuntimeAdmissionDeliveryService",
    "EvolutionStablePromotionRuntimeAdmissionDeliveryStore",
    "EvolutionStablePromotionRuntimeAdmissionDeliveryView",
    "EvolutionStablePromotionRuntimeAdmissionSubmission",
    "EvolutionStablePromotionRuntimeAdmissionSubmissionPayload",
    "decode_stable_promotion_runtime_admission_submission",
    "encode_stable_promotion_runtime_admission_submission",
    "render_stable_promotion_runtime_admission_delivery",
    "render_stable_promotion_runtime_admission_submission",
]
