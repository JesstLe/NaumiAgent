"""Signed remote result manifests and local H5a ingestion."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.config.credentials import resolve_runtime_payload_key
from naumi_agent.evolution.revalidation_adversarial_samples import (
    EvolutionRevalidationAdversarialSampleError,
    EvolutionRevalidationAdversarialSampleReceipt,
    EvolutionRevalidationAdversarialSampleStore,
    adversarial_batch_id,
    build_revalidation_adversarial_sample_receipt,
    require_revalidation_adversarial_source_pair,
    revalidation_adversarial_configuration,
    revalidation_adversarial_sample_seed,
    validate_revalidation_adversarial_h5a,
    validate_revalidation_adversarial_profile,
)
from naumi_agent.evolution.revalidation_platform_claims import (
    EvolutionRevalidationWorkerIdentity,
)
from naumi_agent.evolution.revalidation_platform_execution_authorizations import (
    EvolutionRevalidationPlatformExecutionAuthorization,
    EvolutionRevalidationPlatformExecutionAuthorizationService,
)
from naumi_agent.evolution.revalidation_runtime_sources import (
    EvolutionRevalidationRuntimeSourceService,
)
from naumi_agent.harness.eval_identity import (
    HarnessEvalPlatformIdentity,
    build_eval_baseline_identity,
)
from naumi_agent.harness.eval_models import HarnessEvalSuiteResult
from naumi_agent.harness.store import (
    HarnessStore,
    HarnessStoreConflictError,
    HarnessStoredEvalResult,
    HarnessStoreError,
    canonicalize_harness_eval_result,
    harness_eval_result_canonical_json,
    harness_eval_result_sha256,
)

EVOLUTION_REVALIDATION_PLATFORM_RESULT_POLICY = (
    "evolution-revalidation-platform-result-manifest-v1"
)
EVOLUTION_REVALIDATION_PLATFORM_RESULT_DOMAIN = (
    "naumi.evolution.revalidation-platform-result.v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_B64_64_RE = r"^[A-Za-z0-9+/]{86}==$"
type Phase = Literal["red", "green"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class EvolutionRevalidationPlatformResultArtifact(_StrictModel):
    sample_index: int = Field(ge=0, le=99)
    sample_seed: int = Field(ge=0, le=9_223_372_036_854_775_807)
    phase: Phase
    result_sha256: str = Field(pattern=_SHA256_RE)
    result_bytes: int = Field(ge=2, le=4 * 1024 * 1024)
    result: HarnessEvalSuiteResult

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        safe = canonicalize_harness_eval_result(self.result)
        if safe != self.result:
            raise ValueError("Platform result artifact 包含必须脱敏的内容。")
        encoded = harness_eval_result_canonical_json(safe).encode("utf-8")
        if self.result_bytes != len(encoded):
            raise ValueError("Platform result artifact 字节数不一致。")
        if not hmac.compare_digest(
            self.result_sha256,
            harness_eval_result_sha256(safe),
        ):
            raise ValueError("Platform result artifact digest 不一致。")
        return self


class EvolutionRevalidationPlatformResultPayload(_StrictModel):
    domain: Literal["naumi.evolution.revalidation-platform-result.v1"] = (
        EVOLUTION_REVALIDATION_PLATFORM_RESULT_DOMAIN
    )
    authorization_id: str = Field(pattern=r"^evrevalexecauth_[0-9a-f]{24}$")
    authorization_sha256: str = Field(pattern=_SHA256_RE)
    authorization_sequence: int = Field(ge=1, le=10_000)
    claim_receipt_id: str = Field(pattern=r"^evrevalclaimreceipt_[0-9a-f]{24}$")
    claim_receipt_sha256: str = Field(pattern=_SHA256_RE)
    identity_id: str = Field(pattern=r"^evrevalworkerid_[0-9a-f]{24}$")
    identity_sha256: str = Field(pattern=_SHA256_RE)
    worker_id: str = Field(min_length=1, max_length=128)
    worker_instance_id: str = Field(min_length=1, max_length=128)
    worker_epoch: int = Field(ge=1)
    contract_id: str = Field(pattern=r"^evrevalruntime_[0-9a-f]{24}$")
    contract_sha256: str = Field(pattern=_SHA256_RE)
    source_snapshot_id: str = Field(pattern=r"^evrevalsrc_[0-9a-f]{24}$")
    source_snapshot_sha256: str = Field(pattern=_SHA256_RE)
    platform: Literal["linux", "macos", "windows"]
    platform_identity: HarnessEvalPlatformIdentity
    platform_identity_sha256: str = Field(pattern=_SHA256_RE)
    suite_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    run_scope: Literal["cohort"] = "cohort"
    requested_samples: int = Field(ge=5, le=100)
    start_index: int = Field(ge=0, le=99)
    sample_count: int = Field(ge=1, le=100)
    artifacts: tuple[EvolutionRevalidationPlatformResultArtifact, ...] = Field(
        min_length=2, max_length=200
    )
    result_bytes: int = Field(ge=4, le=1024**3)
    run_grant_sha256: str = Field(pattern=_SHA256_RE)
    completed_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact_prefix(self) -> Self:
        if self.platform_identity.system != self.platform:
            raise ValueError("Platform result identity 与声明平台不一致。")
        expected_platform = _digest(self.platform_identity.model_dump(mode="json"))
        if self.platform_identity_sha256 != expected_platform:
            raise ValueError("Platform result platform identity digest 不一致。")
        expected_keys = tuple(
            (index, phase)
            for index in range(self.start_index, self.start_index + self.sample_count)
            for phase in ("red", "green")
        )
        keys = tuple((item.sample_index, item.phase) for item in self.artifacts)
        if (
            keys != expected_keys
            or self.start_index + self.sample_count > self.requested_samples
        ):
            raise ValueError("Platform result artifacts 必须是连续 RED/GREEN prefix。")
        if self.result_bytes != sum(item.result_bytes for item in self.artifacts):
            raise ValueError("Platform result aggregate bytes 不一致。")
        _aware(self.completed_at)
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical(self.model_dump(mode="json"))


class EvolutionRevalidationPlatformResultManifest(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-revalidation-platform-result-manifest-v1"
    ] = EVOLUTION_REVALIDATION_PLATFORM_RESULT_POLICY
    manifest_id: str = Field(pattern=r"^evrevalresultmanifest_[0-9a-f]{24}$")
    manifest_sha256: str = Field(pattern=_SHA256_RE)
    payload: EvolutionRevalidationPlatformResultPayload
    signature_algorithm: Literal["ed25519"] = "ed25519"
    signature_base64: str = Field(pattern=_B64_64_RE)
    signature_sha256: str = Field(pattern=_SHA256_RE)
    worker_signed: Literal[True] = True
    h5a_ingested: Literal[False] = False
    cohort_authority: Literal[False] = False
    comparison_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        signature = _decode_signature(self.signature_base64)
        if self.signature_sha256 != hashlib.sha256(signature).hexdigest():
            raise ValueError("Platform result signature digest 不一致。")
        digest = _digest(self.payload.model_dump(mode="json"))
        if self.manifest_sha256 != digest:
            raise ValueError("Platform result manifest digest 不一致。")
        if self.manifest_id != f"evrevalresultmanifest_{digest[:24]}":
            raise ValueError("Platform result manifest id 不一致。")
        return self


class EvolutionRevalidationPlatformResultIngestionReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-revalidation-platform-result-manifest-v1"
    ] = EVOLUTION_REVALIDATION_PLATFORM_RESULT_POLICY
    receipt_id: str = Field(pattern=r"^evrevalresultreceipt_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    manifest_id: str = Field(pattern=r"^evrevalresultmanifest_[0-9a-f]{24}$")
    manifest_sha256: str = Field(pattern=_SHA256_RE)
    authorization_id: str = Field(pattern=r"^evrevalexecauth_[0-9a-f]{24}$")
    authorization_sha256: str = Field(pattern=_SHA256_RE)
    contract_id: str = Field(pattern=r"^evrevalruntime_[0-9a-f]{24}$")
    platform: Literal["linux", "macos", "windows"]
    start_index: int = Field(ge=0, le=99)
    sample_count: int = Field(ge=1, le=100)
    accepted_through_index: int = Field(ge=0, le=99)
    red_result_sha256: tuple[str, ...] = Field(min_length=1, max_length=100)
    green_result_sha256: tuple[str, ...] = Field(min_length=1, max_length=100)
    pair_receipt_sha256: tuple[str, ...] = Field(min_length=1, max_length=100)
    result_received: Literal[True] = True
    h5a_ingested: Literal[True] = True
    continuous_prefix: Literal[True] = True
    full_cohort_received: bool
    cohort_authority: Literal[False] = False
    comparison_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    received_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if not (
            len(self.red_result_sha256)
            == len(self.green_result_sha256)
            == len(self.pair_receipt_sha256)
            == self.sample_count
        ):
            raise ValueError("Platform result ingestion evidence 数量不一致。")
        if self.accepted_through_index != self.start_index + self.sample_count - 1:
            raise ValueError("Platform result accepted prefix 投影不一致。")
        _aware(self.received_at)
        core = self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        digest = _digest(core)
        if self.receipt_sha256 != digest:
            raise ValueError("Platform result ingestion receipt digest 不一致。")
        if self.receipt_id != f"evrevalresultreceipt_{digest[:24]}":
            raise ValueError("Platform result ingestion receipt id 不一致。")
        return self


class EvolutionRevalidationPlatformResultError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationPlatformResultStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        control_plane_key_provider: Callable[[], bytes] = resolve_runtime_payload_key,
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self._control_plane_key_provider = control_plane_key_provider

    async def get_manifest(self, manifest_id: str):
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT * FROM evolution_revalidation_platform_result_manifests "
                    "WHERE manifest_id = ?",
                    (manifest_id,),
                )
            ).fetchone()
        return None if row is None else self._stored_manifest(row)

    async def get_window(self, authorization_id: str, start_index: int):
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT * FROM evolution_revalidation_platform_result_manifests "
                    "WHERE authorization_id = ? AND start_index = ?",
                    (authorization_id, start_index),
                )
            ).fetchone()
        return None if row is None else self._stored_manifest(row)

    async def get_receipt(self, manifest_id: str):
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT receipt_json FROM evolution_revalidation_platform_result_receipts "
                    "WHERE manifest_id = ?",
                    (manifest_id,),
                )
            ).fetchone()
        if row is None:
            return None
        receipt = _receipt(row["receipt_json"])
        manifest = await self.get_manifest(manifest_id)
        if manifest is None or not _receipt_matches_manifest(receipt, manifest):
            raise EvolutionRevalidationPlatformResultError(
                "platform_result_receipt_tampered",
                "Platform result ingestion receipt 与 durable admission 不一致。",
            )
        return receipt

    async def get_admitted_at(self, manifest_id: str):
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT * FROM evolution_revalidation_platform_result_manifests "
                    "WHERE manifest_id = ?",
                    (manifest_id,),
                )
            ).fetchone()
        if row is None:
            return None
        self._stored_manifest(row)
        return _aware(str(row["admitted_at"]))

    async def accepted_prefix(self, contract_id: str, platform: str) -> int:
        if not self.db_path.is_file():
            return 0
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            rows = await (
                await db.execute(
                    "SELECT manifest_id FROM "
                    "evolution_revalidation_platform_result_receipts "
                    "WHERE contract_id = ? AND platform = ? ORDER BY start_index",
                    (contract_id, platform),
                )
            ).fetchall()
        receipts = []
        for row in rows:
            receipt = await self.get_receipt(str(row["manifest_id"]))
            if receipt is not None:
                receipts.append(receipt)
        cursor = 0
        for receipt in receipts:
            if receipt.start_index != cursor:
                break
            cursor += receipt.sample_count
        return cursor

    async def list_receipts(self, contract_id: str, platform: str):
        if not self.db_path.is_file():
            return ()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            rows = await (
                await db.execute(
                    "SELECT manifest_id FROM "
                    "evolution_revalidation_platform_result_receipts "
                    "WHERE contract_id = ? AND platform = ? ORDER BY start_index",
                    (contract_id, platform),
                )
            ).fetchall()
        receipts = []
        for row in rows:
            receipt = await self.get_receipt(str(row["manifest_id"]))
            if receipt is None:
                raise EvolutionRevalidationPlatformResultError(
                    "platform_result_receipt_missing",
                    "Platform result receipt catalog 出现悬空引用。",
                )
            receipts.append(receipt)
        return tuple(receipts)

    async def record_manifest(
        self,
        manifest,
        *,
        admitted_at: str,
        admission_attestation_sha256: str,
    ):
        item = EvolutionRevalidationPlatformResultManifest.model_validate_json(
            manifest.model_dump_json()
        )
        admitted = _aware(admitted_at)
        if not (
            _aware(item.payload.completed_at) <= admitted
            and re.fullmatch(_SHA256_RE, admission_attestation_sha256)
        ):
            raise EvolutionRevalidationPlatformResultError(
                "platform_result_admission_invalid",
                "Platform result durable admission 时间或 attestation 无效。",
            )
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > item.payload.result_bytes * 2 + 1024 * 1024:
            raise EvolutionRevalidationPlatformResultError(
                "platform_result_manifest_overhead_excessive",
                "Platform result manifest 元数据开销异常。",
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            authority_row = await (
                await db.execute(
                    "SELECT authorization_json FROM "
                    "evolution_revalidation_platform_execution_authorizations "
                    "WHERE authorization_id = ?",
                    (item.payload.authorization_id,),
                )
            ).fetchone()
            identity_row = await (
                await db.execute(
                    "SELECT identity_json FROM evolution_revalidation_worker_identities "
                    "WHERE identity_id = ?",
                    (item.payload.identity_id,),
                )
            ).fetchone()
            authority = (
                None
                if authority_row is None
                else EvolutionRevalidationPlatformExecutionAuthorization.model_validate_json(
                    authority_row["authorization_json"]
                )
            )
            identity = (
                None
                if identity_row is None
                else EvolutionRevalidationWorkerIdentity.model_validate_json(
                    identity_row["identity_json"]
                )
            )
            if not (
                authority is not None
                and identity is not None
                and _payload_matches_authority(item.payload, authority)
                and identity.identity_sha256 == item.payload.identity_sha256
            ):
                await db.rollback()
                raise EvolutionRevalidationPlatformResultError(
                    "platform_result_authorization_mismatch",
                    "Platform result manifest 缺少 exact execution authorization。",
                )
            _verify_worker_signature(identity.public_key_base64, item)
            if not (
                admitted < _aware(authority.expires_at)
                and hmac.compare_digest(
                    admission_attestation_sha256,
                    _admission_attestation(
                        item,
                        admitted_at=admitted,
                        key=self._control_plane_key(),
                    ),
                )
            ):
                await db.rollback()
                raise EvolutionRevalidationPlatformResultError(
                    "platform_result_admission_invalid",
                    "Platform result durable admission 不在授权窗口或 attestation 无效。",
                )
            existing = await (
                await db.execute(
                    "SELECT * FROM evolution_revalidation_platform_result_manifests "
                    "WHERE authorization_id = ? AND start_index = ?",
                    (item.payload.authorization_id, item.payload.start_index),
                )
            ).fetchone()
            if existing is not None:
                restored = self._stored_manifest(existing)
                await db.rollback()
                if restored != item:
                    raise EvolutionRevalidationPlatformResultError(
                        "platform_result_manifest_conflict",
                        "同一 execution authorization/result window 已绑定不同 manifest。",
                    )
                return restored
            await db.execute(
                "INSERT INTO evolution_revalidation_platform_result_manifests "
                "(manifest_id, manifest_sha256, authorization_id, contract_id, platform, "
                "start_index, sample_count, manifest_json, completed_at, admitted_at, "
                "admission_attestation_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.manifest_id,
                    item.manifest_sha256,
                    item.payload.authorization_id,
                    item.payload.contract_id,
                    item.payload.platform,
                    item.payload.start_index,
                    item.payload.sample_count,
                    encoded,
                    item.payload.completed_at,
                    admitted.isoformat(),
                    admission_attestation_sha256,
                ),
            )
            await db.commit()
        return item

    def _stored_manifest(self, row):
        item = _manifest(row["manifest_json"])
        admitted = _aware(str(row["admitted_at"]))
        expected = _admission_attestation(
            item,
            admitted_at=admitted,
            key=self._control_plane_key(),
        )
        if not hmac.compare_digest(expected, str(row["admission_attestation_sha256"])):
            raise EvolutionRevalidationPlatformResultError(
                "platform_result_admission_tampered",
                "Platform result durable admission attestation 已损坏。",
            )
        return item

    def _control_plane_key(self):
        key = self._control_plane_key_provider()
        if not isinstance(key, bytes) or len(key) < 32:
            raise EvolutionRevalidationPlatformResultError(
                "platform_result_control_plane_key_invalid",
                "Platform result control-plane key 不可用。",
            )
        return key

    async def record_receipt(self, receipt):
        item = EvolutionRevalidationPlatformResultIngestionReceipt.model_validate_json(
            receipt.model_dump_json()
        )
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            manifest_row = await (
                await db.execute(
                    "SELECT * FROM "
                    "evolution_revalidation_platform_result_manifests WHERE manifest_id = ?",
                    (item.manifest_id,),
                )
            ).fetchone()
            manifest = (
                None
                if manifest_row is None
                else self._stored_manifest(manifest_row)
            )
            if manifest is None or not _receipt_matches_manifest(item, manifest):
                await db.rollback()
                raise EvolutionRevalidationPlatformResultError(
                    "platform_result_receipt_manifest_mismatch",
                    "Platform result receipt 缺少 exact manifest。",
                )
            rows = await (
                await db.execute(
                    "SELECT receipt_json FROM evolution_revalidation_adversarial_samples "
                    "WHERE contract_id = ? AND platform = ? AND run_scope = 'cohort' "
                    "AND sample_index BETWEEN ? AND ? ORDER BY sample_index",
                    (
                        item.contract_id,
                        item.platform,
                        item.start_index,
                        item.accepted_through_index,
                    ),
                )
            ).fetchall()
            pairs = tuple(
                EvolutionRevalidationAdversarialSampleReceipt.model_validate_json(
                    row["receipt_json"]
                )
                for row in rows
            )
            if not (
                tuple(pair.receipt_sha256 for pair in pairs)
                == item.pair_receipt_sha256
                and tuple(pair.red_result_sha256 for pair in pairs)
                == item.red_result_sha256
                and tuple(pair.green_result_sha256 for pair in pairs)
                == item.green_result_sha256
            ):
                await db.rollback()
                raise EvolutionRevalidationPlatformResultError(
                    "platform_result_receipt_h5a_mismatch",
                    "Platform result receipt 与本地 H5a pair evidence 不一致。",
                )
            existing = await (
                await db.execute(
                    "SELECT receipt_json FROM evolution_revalidation_platform_result_receipts "
                    "WHERE manifest_id = ?",
                    (item.manifest_id,),
                )
            ).fetchone()
            if existing is not None:
                restored = _receipt(existing["receipt_json"])
                await db.rollback()
                if restored != item:
                    raise EvolutionRevalidationPlatformResultError(
                        "platform_result_receipt_conflict",
                        "Platform result manifest 已绑定不同 ingestion receipt。",
                    )
                return restored
            await db.execute(
                "INSERT INTO evolution_revalidation_platform_result_receipts "
                "(receipt_id, receipt_sha256, manifest_id, authorization_id, contract_id, "
                "platform, start_index, sample_count, receipt_json, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.receipt_id,
                    item.receipt_sha256,
                    item.manifest_id,
                    item.authorization_id,
                    item.contract_id,
                    item.platform,
                    item.start_index,
                    item.sample_count,
                    item.model_dump_json(),
                    item.received_at,
                ),
            )
            await db.commit()
        return item


class EvolutionRevalidationPlatformResultService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        authorization_service: EvolutionRevalidationPlatformExecutionAuthorizationService,
        source_service: EvolutionRevalidationRuntimeSourceService,
        profile_service,
        harness_store: HarnessStore,
        sample_store: EvolutionRevalidationAdversarialSampleStore,
        store: EvolutionRevalidationPlatformResultStore,
        control_plane_key_provider: Callable[[], bytes] = resolve_runtime_payload_key,
        now: Callable[[], str] | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.authorization_service = authorization_service
        self.source_service = source_service
        self.profile_service = profile_service
        self.harness_store = harness_store
        self.sample_store = sample_store
        self.store = store
        self._control_plane_key_provider = control_plane_key_provider
        self.now = now or (lambda: datetime.now(UTC).isoformat())

    async def submit(
        self,
        manifest: EvolutionRevalidationPlatformResultManifest,
    ) -> EvolutionRevalidationPlatformResultIngestionReceipt:
        item = EvolutionRevalidationPlatformResultManifest.model_validate_json(
            manifest.model_dump_json()
        )
        existing_receipt = await self.store.get_receipt(item.manifest_id)
        if existing_receipt is not None:
            return existing_receipt
        window = await self.store.get_window(
            item.payload.authorization_id,
            item.payload.start_index,
        )
        if window is not None and window != item:
            raise EvolutionRevalidationPlatformResultError(
                "platform_result_manifest_conflict",
                "同一 result window 已持久化不同 manifest。",
            )
        now = _aware(self.now())
        if window is None:
            authority_view = await self.authorization_service.inspect(
                authorization_id=item.payload.authorization_id,
                assessed_at=now.isoformat(),
            )
            if authority_view.status != "current":
                raise EvolutionRevalidationPlatformResultError(
                    "platform_result_authorization_not_current",
                    "Platform result 到达时 execution authorization 已不可用。",
                )
            authority = authority_view.authorization
        else:
            authority = await self.authorization_service.store.get(
                item.payload.authorization_id
            )
            if authority is None:
                raise EvolutionRevalidationPlatformResultError(
                    "platform_result_authorization_missing",
                    "已准入 manifest 的 execution authorization 缺失。",
                )
        self._require_payload_binding(item.payload, authority, now=now)
        identity = await self.authorization_service.claim_service.store.get_identity(
            authority.identity_id
        )
        if identity is None or not (
            identity.identity_sha256 == authority.identity_sha256
            and identity.worker_id == authority.worker_id
            and identity.worker_instance_id == authority.worker_instance_id
            and identity.worker_epoch == authority.worker_epoch
        ):
            raise EvolutionRevalidationPlatformResultError(
                "platform_result_identity_stale",
                "Platform result Worker Identity 已失效。",
            )
        _verify_worker_signature(identity.public_key_base64, item)
        accepted = await self.store.accepted_prefix(
            authority.contract_id,
            authority.platform,
        )
        if item.payload.start_index != accepted:
            raise EvolutionRevalidationPlatformResultError(
                "platform_result_prefix_conflict",
                f"Platform result 必须从连续前缀 {accepted} 开始。",
            )
        contract_view = await self.authorization_service.contract_service.inspect(
            workspace_root=self.workspace_root,
            contract_id=authority.contract_id,
        )
        contract = contract_view.contract
        if not contract_view.execution_eligible:
            raise EvolutionRevalidationPlatformResultError(
                "platform_result_contract_not_current",
                "Platform result 对应 Runtime Contract 已不可执行。",
            )
        source_pair = await self.source_service.materialize(
            workspace_root=self.workspace_root,
            validation_plan_id=contract.validation_plan_id,
        )
        try:
            require_revalidation_adversarial_source_pair(source_pair, contract)
            profile = await self.profile_service.status()
            checks = validate_revalidation_adversarial_profile(
                profile,
                contract,
                source_pair.validation_plan.profile_sha256,
            )
        except EvolutionRevalidationAdversarialSampleError as exc:
            raise EvolutionRevalidationPlatformResultError(exc.code, str(exc)) from exc
        configuration = revalidation_adversarial_configuration(
            contract,
            source_pair.validation_plan.profile_sha256,
        )
        identities = {
            "red": build_eval_baseline_identity(
                self.workspace_root,
                configuration=configuration,
                platform_identity=item.payload.platform_identity,
                source_identity=source_pair.red_identity,
                profile_trusted=True,
            ),
            "green": build_eval_baseline_identity(
                self.workspace_root,
                configuration=configuration,
                platform_identity=item.payload.platform_identity,
                source_identity=source_pair.green_identity,
                profile_trusted=True,
            ),
        }
        prepared = []
        for artifact in item.payload.artifacts:
            expected_seed = revalidation_adversarial_sample_seed(
                contract.seed,
                artifact.sample_index,
            )
            if artifact.sample_seed != expected_seed:
                raise EvolutionRevalidationPlatformResultError(
                    "platform_result_sample_seed_mismatch",
                    "Platform result sample seed 不一致。",
                )
            if not all(
                f"run_grant_sha256={authority.run_grant.grant_sha256}" in case.message
                for case in artifact.result.cases
            ):
                raise EvolutionRevalidationPlatformResultError(
                    "platform_result_grant_mismatch",
                    "Platform result H5a 未绑定 execution Run Grant。",
                )
            batch_id = adversarial_batch_id(
                contract,
                authority.platform,
                artifact.phase,
                "cohort",
            )
            proposed = HarnessStoredEvalResult(
                id="remote-proposal",
                workspace_root=str(self.workspace_root),
                batch_id=batch_id,
                suite_id=contract.suite_id,
                sample_index=artifact.sample_index,
                identity_sha256=identities[artifact.phase].identity_sha256,
                result_sha256=artifact.result_sha256,
                result=artifact.result,
                created_at=item.payload.completed_at,
            )
            try:
                validate_revalidation_adversarial_h5a(
                    proposed,
                    contract,
                    authority.platform,
                    artifact.phase,
                    checks,
                    identities[artifact.phase],
                    "cohort",
                )
            except EvolutionRevalidationAdversarialSampleError as exc:
                raise EvolutionRevalidationPlatformResultError(
                    "platform_result_h5a_rejected",
                    f"Platform result H5a 本地验证失败：{exc}",
                ) from exc
            prepared.append((artifact, batch_id))
        await self._require_source_current(contract)
        if window is None:
            await self.store.record_manifest(
                item,
                admitted_at=now.isoformat(),
                admission_attestation_sha256=_admission_attestation(
                    item,
                    admitted_at=now,
                    key=self._control_plane_key(),
                ),
            )
        durable_admitted_at = await self.store.get_admitted_at(item.manifest_id)
        if durable_admitted_at is None:
            raise EvolutionRevalidationPlatformResultError(
                "platform_result_admission_missing",
                "Platform result durable admission 未成功持久化。",
            )
        stored_by_sample: dict[int, dict[str, HarnessStoredEvalResult]] = {}
        for artifact, batch_id in prepared:
            try:
                stored = await self.harness_store.record_eval_result(
                    workspace_root=self.workspace_root,
                    batch_id=batch_id,
                    sample_index=artifact.sample_index,
                    result=artifact.result,
                    created_at=item.payload.completed_at,
                )
            except HarnessStoreConflictError as exc:
                raise EvolutionRevalidationPlatformResultError(
                    "platform_result_h5a_conflict",
                    "Platform result 与既有 H5a 冲突。",
                ) from exc
            except HarnessStoreError as exc:
                raise EvolutionRevalidationPlatformResultError(
                    "platform_result_h5a_rejected",
                    f"Platform result H5a 本地验证失败：{exc}",
                ) from exc
            if stored.result_sha256 != artifact.result_sha256:
                raise EvolutionRevalidationPlatformResultError(
                    "platform_result_h5a_digest_changed",
                    "Platform result H5a 持久化摘要发生变化。",
                )
            stored_by_sample.setdefault(artifact.sample_index, {})[
                artifact.phase
            ] = stored
        pair_receipts = []
        for sample_index in range(
            item.payload.start_index,
            item.payload.start_index + item.payload.sample_count,
        ):
            pair = stored_by_sample[sample_index]
            receipt = build_revalidation_adversarial_sample_receipt(
                contract,
                item.payload.platform_identity,
                sample_index,
                pair["red"],
                pair["green"],
                completed_at=item.payload.completed_at,
                run_scope="cohort",
            )
            pair_receipts.append(await self.sample_store.record(receipt))
        await self._require_source_current(contract)
        return await self.store.record_receipt(
            _build_ingestion_receipt(
                item,
                pair_receipts,
                requested_samples=contract.requested_samples,
                received_at=durable_admitted_at,
            )
        )

    def _require_payload_binding(self, payload, authority, *, now):
        exact = (
            _payload_matches_authority(payload, authority)
            and _aware(authority.issued_at) <= _aware(payload.completed_at) <= now
            and _aware(payload.completed_at) < _aware(authority.expires_at)
        )
        if not exact:
            raise EvolutionRevalidationPlatformResultError(
                "platform_result_payload_authority_mismatch",
                "Platform result manifest 未绑定 exact execution authority。",
            )

    async def _require_source_current(self, contract):
        source_current = await self.source_service.materialize(
            workspace_root=self.workspace_root,
            validation_plan_id=contract.validation_plan_id,
        )
        try:
            require_revalidation_adversarial_source_pair(source_current, contract)
            profile = await self.profile_service.status()
            validate_revalidation_adversarial_profile(
                profile,
                contract,
                source_current.validation_plan.profile_sha256,
            )
        except EvolutionRevalidationAdversarialSampleError as exc:
            raise EvolutionRevalidationPlatformResultError(exc.code, str(exc)) from exc

    def _control_plane_key(self):
        key = self._control_plane_key_provider()
        if not isinstance(key, bytes) or len(key) < 32:
            raise EvolutionRevalidationPlatformResultError(
                "platform_result_control_plane_key_invalid",
                "Platform result control-plane key 不可用。",
            )
        return key


def issue_evolution_revalidation_platform_result_manifest(
    *,
    payload: EvolutionRevalidationPlatformResultPayload,
    private_key,
) -> EvolutionRevalidationPlatformResultManifest:
    """Sign one content-addressed result prefix using the claimed Worker key."""
    if not hasattr(private_key, "sign"):
        raise TypeError("private_key 必须支持 Ed25519 sign。")
    normalized = EvolutionRevalidationPlatformResultPayload.model_validate_json(
        payload.model_dump_json()
    )
    digest = _digest(normalized.model_dump(mode="json"))
    signature = private_key.sign(normalized.canonical_bytes())
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise ValueError("Worker result signature 必须是 64-byte Ed25519 signature。")
    return EvolutionRevalidationPlatformResultManifest.model_validate(
        {
            "schema_version": 1,
            "policy_version": EVOLUTION_REVALIDATION_PLATFORM_RESULT_POLICY,
            "manifest_id": f"evrevalresultmanifest_{digest[:24]}",
            "manifest_sha256": digest,
            "payload": normalized.model_dump(mode="json"),
            "signature_algorithm": "ed25519",
            "signature_base64": base64.b64encode(signature).decode("ascii"),
            "signature_sha256": hashlib.sha256(signature).hexdigest(),
            "worker_signed": True,
            "h5a_ingested": False,
            "cohort_authority": False,
            "comparison_authority": False,
            "promotion_authority": False,
        }
    )


def _build_ingestion_receipt(manifest, pairs, *, requested_samples, received_at):
    payload = manifest.payload
    red = tuple(
        artifact.result_sha256 for artifact in payload.artifacts if artifact.phase == "red"
    )
    green = tuple(
        artifact.result_sha256
        for artifact in payload.artifacts
        if artifact.phase == "green"
    )
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_PLATFORM_RESULT_POLICY,
        "manifest_id": manifest.manifest_id,
        "manifest_sha256": manifest.manifest_sha256,
        "authorization_id": payload.authorization_id,
        "authorization_sha256": payload.authorization_sha256,
        "contract_id": payload.contract_id,
        "platform": payload.platform,
        "start_index": payload.start_index,
        "sample_count": payload.sample_count,
        "accepted_through_index": payload.start_index + payload.sample_count - 1,
        "red_result_sha256": list(red),
        "green_result_sha256": list(green),
        "pair_receipt_sha256": [item.receipt_sha256 for item in pairs],
        "result_received": True,
        "h5a_ingested": True,
        "continuous_prefix": True,
        "full_cohort_received": (
            payload.start_index + payload.sample_count == requested_samples
        ),
        "cohort_authority": False,
        "comparison_authority": False,
        "promotion_authority": False,
        "received_at": received_at.isoformat(),
    }
    digest = _digest(core)
    return EvolutionRevalidationPlatformResultIngestionReceipt.model_validate(
        {
            **core,
            "receipt_id": f"evrevalresultreceipt_{digest[:24]}",
            "receipt_sha256": digest,
        }
    )


def _verify_worker_signature(public_key_base64, manifest):
    try:
        raw = base64.b64decode(public_key_base64, validate=True)
        Ed25519PublicKey.from_public_bytes(raw).verify(
            _decode_signature(manifest.signature_base64),
            manifest.payload.canonical_bytes(),
        )
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise EvolutionRevalidationPlatformResultError(
            "platform_result_signature_invalid",
            "Platform result Worker signature 无效。",
        ) from exc


def _admission_attestation(manifest, *, admitted_at, key):
    return hmac.new(
        key,
        _canonical(
            {
                "manifest_sha256": manifest.manifest_sha256,
                "authorization_sha256": manifest.payload.authorization_sha256,
                "admitted_at": admitted_at.isoformat(),
            }
        ),
        hashlib.sha256,
    ).hexdigest()


def _payload_matches_authority(payload, authority):
    return bool(
        payload.authorization_sha256 == authority.authorization_sha256
        and payload.authorization_sequence == authority.authorization_sequence
        and payload.claim_receipt_id == authority.claim_receipt_id
        and payload.claim_receipt_sha256 == authority.claim_receipt_sha256
        and payload.identity_id == authority.identity_id
        and payload.identity_sha256 == authority.identity_sha256
        and payload.worker_id == authority.worker_id
        and payload.worker_instance_id == authority.worker_instance_id
        and payload.worker_epoch == authority.worker_epoch
        and payload.contract_id == authority.contract_id
        and payload.contract_sha256 == authority.contract_sha256
        and payload.source_snapshot_id == authority.source_snapshot_id
        and payload.source_snapshot_sha256 == authority.source_snapshot_sha256
        and payload.platform == authority.platform
        and payload.suite_id == authority.suite_id
        and payload.requested_samples == authority.requested_samples
        and payload.run_grant_sha256 == authority.run_grant.grant_sha256
        and payload.start_index + payload.sample_count <= authority.requested_samples
        and payload.result_bytes <= authority.max_result_bytes
    )


def _receipt_matches_manifest(receipt, manifest):
    payload = manifest.payload
    red = tuple(
        artifact.result_sha256 for artifact in payload.artifacts if artifact.phase == "red"
    )
    green = tuple(
        artifact.result_sha256
        for artifact in payload.artifacts
        if artifact.phase == "green"
    )
    return bool(
        receipt.manifest_sha256 == manifest.manifest_sha256
        and receipt.authorization_id == payload.authorization_id
        and receipt.authorization_sha256 == payload.authorization_sha256
        and receipt.contract_id == payload.contract_id
        and receipt.platform == payload.platform
        and receipt.start_index == payload.start_index
        and receipt.sample_count == payload.sample_count
        and receipt.accepted_through_index
        == payload.start_index + payload.sample_count - 1
        and receipt.red_result_sha256 == red
        and receipt.green_result_sha256 == green
        and receipt.full_cohort_received
        is (payload.start_index + payload.sample_count == payload.requested_samples)
    )


def _decode_signature(value):
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("Platform result signature 不是 canonical base64。") from exc
    if len(raw) != 64 or base64.b64encode(raw).decode("ascii") != value:
        raise ValueError("Platform result signature 必须是 canonical 64-byte base64。")
    return raw


def _aware(value):
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.utcoffset() is None:
        raise ValueError("Platform result 时间必须包含 offset。")
    return parsed.astimezone(UTC)


def _canonical(payload):
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(payload):
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _manifest(value):
    return EvolutionRevalidationPlatformResultManifest.model_validate_json(value)


def _receipt(value):
    return EvolutionRevalidationPlatformResultIngestionReceipt.model_validate_json(value)


async def _ensure_schema(db):
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS evolution_revalidation_platform_result_manifests (
            manifest_id TEXT PRIMARY KEY,
            manifest_sha256 TEXT NOT NULL UNIQUE,
            authorization_id TEXT NOT NULL,
            contract_id TEXT NOT NULL,
            platform TEXT NOT NULL CHECK (platform IN ('linux', 'macos', 'windows')),
            start_index INTEGER NOT NULL,
            sample_count INTEGER NOT NULL,
            manifest_json TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            admitted_at TEXT NOT NULL,
            admission_attestation_sha256 TEXT NOT NULL,
            UNIQUE (authorization_id, start_index)
        );
        CREATE TABLE IF NOT EXISTS evolution_revalidation_platform_result_receipts (
            receipt_id TEXT PRIMARY KEY,
            receipt_sha256 TEXT NOT NULL UNIQUE,
            manifest_id TEXT NOT NULL UNIQUE,
            authorization_id TEXT NOT NULL,
            contract_id TEXT NOT NULL,
            platform TEXT NOT NULL CHECK (platform IN ('linux', 'macos', 'windows')),
            start_index INTEGER NOT NULL,
            sample_count INTEGER NOT NULL,
            receipt_json TEXT NOT NULL,
            received_at TEXT NOT NULL,
            UNIQUE (contract_id, platform, start_index)
        );
        """
    )
    await db.commit()


__all__ = [
    "EVOLUTION_REVALIDATION_PLATFORM_RESULT_DOMAIN",
    "EVOLUTION_REVALIDATION_PLATFORM_RESULT_POLICY",
    "EvolutionRevalidationPlatformResultArtifact",
    "EvolutionRevalidationPlatformResultError",
    "EvolutionRevalidationPlatformResultIngestionReceipt",
    "EvolutionRevalidationPlatformResultManifest",
    "EvolutionRevalidationPlatformResultPayload",
    "EvolutionRevalidationPlatformResultService",
    "EvolutionRevalidationPlatformResultStore",
    "issue_evolution_revalidation_platform_result_manifest",
]
