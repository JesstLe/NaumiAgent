"""Signed post-rollback remote Runtime Eval results and canonical H5 ingestion."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self
from weakref import WeakValueDictionary

import aiosqlite
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.config.credentials import resolve_runtime_payload_key
from naumi_agent.daemons.authenticated_worker_identity import (
    AuthenticatedWorkerIdentity,
)
from naumi_agent.evolution.post_rollback_behavioral_lanes import (
    EvolutionPostRollbackBehavioralLaneError,
    EvolutionPostRollbackBehavioralLaneService,
)
from naumi_agent.evolution.post_rollback_remote_execution_authorizations import (
    EvolutionPostRollbackRemoteExecutionAuthorization,
    EvolutionPostRollbackRemoteExecutionAuthorizationService,
    EvolutionPostRollbackRemoteExecutionAuthorizationStore,
)
from naumi_agent.evolution.post_rollback_target_baselines import (
    EvolutionPostRollbackTargetBaseline,
    EvolutionPostRollbackTargetBaselineError,
)
from naumi_agent.harness.eval_identity import HarnessEvalPlatformIdentity
from naumi_agent.harness.eval_receipt import HarnessEvalComparisonReceipt
from naumi_agent.harness.store import HarnessStore, HarnessStoreError
from naumi_agent.release.runtime_eval import (
    MAX_RUNTIME_EVAL_INPUT_BYTES,
    MAX_RUNTIME_EVAL_OUTPUT_BYTES,
    ReleaseRuntimeEvalProcessRequest,
    ReleaseRuntimeEvalReceipt,
    ReleaseRuntimeEvalRequest,
    ReleaseRuntimeEvalResponse,
    build_runtime_eval_process_request,
    build_runtime_eval_receipt,
)

EVOLUTION_POST_ROLLBACK_REMOTE_RESULT_POLICY = (
    "evolution-post-rollback-remote-result-v1"
)
EVOLUTION_POST_ROLLBACK_REMOTE_RESULT_DOMAIN = (
    "naumi.evolution.post-rollback-remote-result.v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_B64_64_RE = r"^[A-Za-z0-9+/]{86}==$"
_MAX_MANIFEST_OVERHEAD_BYTES = 1024 * 1024
MAX_REMOTE_RESULT_MANIFEST_BYTES = (
    100 * (MAX_RUNTIME_EVAL_INPUT_BYTES + MAX_RUNTIME_EVAL_OUTPUT_BYTES)
    + _MAX_MANIFEST_OVERHEAD_BYTES
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionPostRollbackRemoteResultArtifact(_StrictModel):
    """One bounded process request/response pair produced by the remote runtime."""

    sample_index: int = Field(ge=0, le=99)
    process_request: ReleaseRuntimeEvalProcessRequest
    response: ReleaseRuntimeEvalResponse
    input_bytes: int = Field(ge=1, le=MAX_RUNTIME_EVAL_INPUT_BYTES)
    output_bytes: int = Field(ge=1, le=MAX_RUNTIME_EVAL_OUTPUT_BYTES)
    exit_code: Literal[0] = 0
    runtime_process_executed: Literal[True] = True

    @model_validator(mode="after")
    def _exact(self) -> Self:
        expected_input = len(self.process_request.model_dump_json().encode("utf-8"))
        expected_output = len(self.response.model_dump_json().encode("utf-8"))
        if not (
            self.response.process_request_id == self.process_request.process_request_id
            and self.response.process_request_sha256
            == self.process_request.process_request_sha256
            and self.response.authority_request_id
            == self.process_request.authority_request_id
            and self.response.authority_request_sha256
            == self.process_request.authority_request_sha256
            and self.response.runner_version == self.process_request.runner_version
            and self.input_bytes == expected_input
            and self.output_bytes == expected_output
        ):
            raise ValueError("Remote result process request/response/bytes 不一致。")
        return self


class EvolutionPostRollbackRemoteResultPayload(_StrictModel):
    schema_version: Literal[1] = 1
    domain: Literal["naumi.evolution.post-rollback-remote-result.v1"] = (
        EVOLUTION_POST_ROLLBACK_REMOTE_RESULT_DOMAIN
    )
    authorization_id: str = Field(pattern=r"^evpostexecauth_[0-9a-f]{24}$")
    authorization_sha256: str = Field(pattern=_SHA256_RE)
    attempt_id: str = Field(pattern=r"^evpostattempt_[0-9a-f]{24}$")
    delivery_receipt_id: str = Field(
        pattern=r"^evpostdeliveryreceipt_[0-9a-f]{24}$"
    )
    delivery_receipt_sha256: str = Field(pattern=_SHA256_RE)
    dispatch_id: str = Field(pattern=r"^evpostdispatch_[0-9a-f]{24}$")
    dispatch_sha256: str = Field(pattern=_SHA256_RE)
    claim_receipt_id: str = Field(pattern=r"^evpostclaimreceipt_[0-9a-f]{24}$")
    claim_receipt_sha256: str = Field(pattern=_SHA256_RE)
    claim_lease_epoch: int = Field(ge=1, le=1_000_000)
    identity_id: str = Field(pattern=r"^workeridentity_[0-9a-f]{24}$")
    identity_sha256: str = Field(pattern=_SHA256_RE)
    worker_id: str = Field(min_length=1, max_length=128)
    worker_instance_id: str = Field(min_length=1, max_length=128)
    worker_epoch: int = Field(ge=1)
    baseline_resolution_id: str = Field(pattern=r"^evpostbaseline_[0-9a-f]{24}$")
    baseline_resolution_sha256: str = Field(pattern=_SHA256_RE)
    outcome_id: str = Field(pattern=r"^evrerollbackout_[0-9a-f]{24}$")
    request_id: str = Field(pattern=r"^evrerollbackreq_[0-9a-f]{24}$")
    comparison_id: str = Field(pattern=_SHA256_RE)
    release_target: str = Field(pattern=r"^(?:macos|linux|windows)-(?:arm64|x64)$")
    archive_sha256: str = Field(pattern=_SHA256_RE)
    manifest_sha256: str = Field(pattern=_SHA256_RE)
    installed_binary_sha256: str = Field(pattern=_SHA256_RE)
    suite_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    suite_sha256: str = Field(pattern=_SHA256_RE)
    repetitions: int = Field(ge=5, le=100)
    runtime_eval_request: ReleaseRuntimeEvalRequest
    platform_identity: HarnessEvalPlatformIdentity
    artifacts: tuple[EvolutionPostRollbackRemoteResultArtifact, ...] = Field(
        min_length=5,
        max_length=100,
    )
    result_bytes: int = Field(ge=5, le=100 * MAX_RUNTIME_EVAL_OUTPUT_BYTES)
    run_grant_sha256: str = Field(pattern=_SHA256_RE)
    runtime_lease_epoch: int = Field(ge=1)
    installed_manifest_verified: Literal[True] = True
    installed_binary_digest_recorded: Literal[True] = True
    network_default_denied: Literal[True] = True
    resource_limits_enforced: Literal[True] = True
    process_tree_contained: Literal[True] = True
    started_at: str = Field(min_length=1, max_length=100)
    completed_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        started = _aware(self.started_at)
        completed = _aware(self.completed_at)
        platform, architecture = self.release_target.split("-", 1)
        expected_process = build_runtime_eval_process_request(self.runtime_eval_request)
        indexes = tuple(item.sample_index for item in self.artifacts)
        response_ids = tuple(item.response.response_id for item in self.artifacts)
        evaluated = tuple(_aware(item.response.evaluated_at) for item in self.artifacts)
        if not (
            started <= completed
            and self.suite_id == self.runtime_eval_request.suite_id
            and self.suite_sha256 == self.runtime_eval_request.suite_sha256
            and len(self.artifacts) == self.repetitions
            and indexes == tuple(range(self.repetitions))
            and len(response_ids) == len(set(response_ids))
            and self.result_bytes == sum(item.output_bytes for item in self.artifacts)
            and self.platform_identity.system == platform
            and _architecture_matches(architecture, self.platform_identity.machine)
            and all(item.process_request == expected_process for item in self.artifacts)
            and all(
                item.response.runtime_platform == self.platform_identity
                for item in self.artifacts
            )
            and all(started <= item <= completed for item in evaluated)
        ):
            raise ValueError("Remote result cohort/suite/platform/time projection 不一致。")
        encoded = self.model_dump_json().encode("utf-8")
        bounded = (
            self.result_bytes
            + self.repetitions * MAX_RUNTIME_EVAL_INPUT_BYTES
            + _MAX_MANIFEST_OVERHEAD_BYTES
        )
        if len(encoded) > min(bounded, MAX_REMOTE_RESULT_MANIFEST_BYTES):
            raise ValueError("Remote result payload 超过有界 manifest 上限。")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical(self.model_dump(mode="json"))


class EvolutionPostRollbackRemoteResultManifest(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-post-rollback-remote-result-v1"] = (
        EVOLUTION_POST_ROLLBACK_REMOTE_RESULT_POLICY
    )
    manifest_id: str = Field(pattern=r"^evpostresultmanifest_[0-9a-f]{24}$")
    manifest_sha256: str = Field(pattern=_SHA256_RE)
    payload: EvolutionPostRollbackRemoteResultPayload
    signature_algorithm: Literal["ed25519"] = "ed25519"
    signature_base64: str = Field(pattern=_B64_64_RE, repr=False)
    signature_sha256: str = Field(pattern=_SHA256_RE)
    worker_signed: Literal[True] = True
    durable_admission: Literal[False] = False
    result_authority: Literal[False] = False
    h5a_ingested: Literal[False] = False
    h5c_recorded: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        signature = _decode_signature(self.signature_base64)
        digest = _digest(self.payload.model_dump(mode="json"))
        if not (
            hmac.compare_digest(
                self.signature_sha256,
                hashlib.sha256(signature).hexdigest(),
            )
            and hmac.compare_digest(self.manifest_sha256, digest)
            and self.manifest_id == f"evpostresultmanifest_{digest[:24]}"
        ):
            raise ValueError("Remote result manifest identity 不一致。")
        return self


class EvolutionPostRollbackRemoteResultReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-post-rollback-remote-result-v1"] = (
        EVOLUTION_POST_ROLLBACK_REMOTE_RESULT_POLICY
    )
    receipt_id: str = Field(pattern=r"^evpostresultreceipt_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    manifest_id: str = Field(pattern=r"^evpostresultmanifest_[0-9a-f]{24}$")
    manifest_sha256: str = Field(pattern=_SHA256_RE)
    authorization_id: str = Field(pattern=r"^evpostexecauth_[0-9a-f]{24}$")
    authorization_sha256: str = Field(pattern=_SHA256_RE)
    attempt_id: str = Field(pattern=r"^evpostattempt_[0-9a-f]{24}$")
    baseline_resolution_id: str = Field(pattern=r"^evpostbaseline_[0-9a-f]{24}$")
    outcome_id: str = Field(pattern=r"^evrerollbackout_[0-9a-f]{24}$")
    request_id: str = Field(pattern=r"^evrerollbackreq_[0-9a-f]{24}$")
    comparison_id: str = Field(pattern=_SHA256_RE)
    release_target: str = Field(pattern=r"^(?:macos|linux|windows)-(?:arm64|x64)$")
    repetitions: int = Field(ge=5, le=100)
    runtime_receipt_sha256: tuple[str, ...] = Field(min_length=5, max_length=100)
    h5a_batch_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    h5a_result_sha256: tuple[str, ...] = Field(min_length=5, max_length=100)
    h5c_comparison_id: str = Field(pattern=_SHA256_RE)
    h5c_comparison_sha256: str = Field(pattern=_SHA256_RE)
    result_received: Literal[True] = True
    worker_signature_verified: Literal[True] = True
    durable_admission: Literal[True] = True
    h5a_ingested: Literal[True] = True
    h5c_recorded: Literal[True] = True
    full_cohort_received: Literal[True] = True
    lane_evaluation_recorded: Literal[True] = True
    behavioral_matrix_recorded: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    admitted_at: str = Field(min_length=1, max_length=100)
    ingested_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if not (
            len(self.runtime_receipt_sha256)
            == len(self.h5a_result_sha256)
            == self.repetitions
            and _aware(self.admitted_at) <= _aware(self.ingested_at)
        ):
            raise ValueError("Remote result receipt evidence 数量或时间不一致。")
        core = self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        digest = _digest(core)
        if not (
            hmac.compare_digest(self.receipt_sha256, digest)
            and self.receipt_id == f"evpostresultreceipt_{digest[:24]}"
        ):
            raise ValueError("Remote result receipt identity 不一致。")
        return self


class EvolutionPostRollbackRemoteResultView(_StrictModel):
    manifest: EvolutionPostRollbackRemoteResultManifest
    receipt: EvolutionPostRollbackRemoteResultReceipt | None = None
    status: Literal["admitted", "ingested", "stale"]
    durable_admission: bool
    worker_signature_verified: bool
    h5a_authority: bool
    h5c_authority: bool
    lane_evaluation_authority: bool
    behavioral_matrix_authority: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        ingested = self.status == "ingested"
        if not (
            self.durable_admission
            and self.worker_signature_verified
            and self.h5a_authority is ingested
            and self.h5c_authority is ingested
            and self.lane_evaluation_authority is ingested
            and (not ingested or self.receipt is not None)
        ):
            raise ValueError("Remote result view authority 投影不一致。")
        return self


class EvolutionPostRollbackRemoteResultError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionPostRollbackRemoteResultStore:
    """Atomic admission in the same session SQLite as authorization and identity."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        control_plane_key_provider: Callable[[], bytes] = resolve_runtime_payload_key,
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve(strict=False)
        self._control_plane_key_provider = control_plane_key_provider

    async def get_manifest(
        self, manifest_id: str
    ) -> EvolutionPostRollbackRemoteResultManifest | None:
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_post_rollback_remote_result_manifests "
                        "WHERE manifest_id = ?",
                        (_manifest_id(manifest_id),),
                    )
                ).fetchone()
            return None if row is None else self._stored_manifest(row)
        except EvolutionPostRollbackRemoteResultError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackRemoteResultError(
                "post_rollback_remote_result_store_corrupt",
                "Remote result durable admission 损坏或无法读取。",
            ) from exc

    async def get_by_attempt(
        self, attempt_id: str
    ) -> EvolutionPostRollbackRemoteResultManifest | None:
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_post_rollback_remote_result_manifests "
                        "WHERE attempt_id = ?",
                        (_attempt_id(attempt_id),),
                    )
                ).fetchone()
            return None if row is None else self._stored_manifest(row)
        except EvolutionPostRollbackRemoteResultError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackRemoteResultError(
                "post_rollback_remote_result_store_corrupt",
                "Remote result durable admission 损坏或无法读取。",
            ) from exc

    async def list_by_lane(
        self,
        *,
        outcome_id: str,
        comparison_id: str,
        limit: int = 3,
    ) -> tuple[EvolutionPostRollbackRemoteResultManifest, ...]:
        """Return bounded admitted manifests for one exact behavioral lane."""
        outcome = _outcome_id(outcome_id)
        comparison = _comparison_id(comparison_id)
        bounded = max(1, min(int(limit), 10))
        if not self.db_path.is_file():
            return ()
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await self._backfill_lane_index(db)
                rows = await (
                    await db.execute(
                        "SELECT manifests.*, "
                        "lane_index.outcome_id AS indexed_outcome_id, "
                        "lane_index.comparison_id AS indexed_comparison_id, "
                        "lane_index.request_id AS indexed_request_id, "
                        "lane_index.suite_id AS indexed_suite_id FROM "
                        "evolution_post_rollback_remote_result_manifests AS manifests "
                        "JOIN evolution_post_rollback_remote_result_lane_index AS lane_index "
                        "ON lane_index.manifest_id = manifests.manifest_id "
                        "WHERE lane_index.outcome_id = ? AND "
                        "lane_index.comparison_id = ? "
                        "ORDER BY manifests.admitted_at, manifests.manifest_id LIMIT ?",
                        (outcome, comparison, bounded + 1),
                    )
                ).fetchall()
            if len(rows) > bounded:
                raise EvolutionPostRollbackRemoteResultError(
                    "post_rollback_remote_result_lane_conflict",
                    "同一 Post-Rollback lane 存在过多 Remote result admission。",
                )
            manifests = tuple(self._stored_manifest(row) for row in rows)
            for row, manifest in zip(rows, manifests, strict=True):
                payload = manifest.payload
                if not (
                    row["indexed_outcome_id"] == payload.outcome_id
                    and row["indexed_comparison_id"] == payload.comparison_id
                    and row["indexed_request_id"] == payload.request_id
                    and row["indexed_suite_id"] == payload.suite_id
                ):
                    raise EvolutionPostRollbackRemoteResultError(
                        "post_rollback_remote_result_lane_index_tampered",
                        "Remote result lane index 与 signed manifest 不一致。",
                    )
            return manifests
        except EvolutionPostRollbackRemoteResultError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackRemoteResultError(
                "post_rollback_remote_result_store_corrupt",
                "Remote result lane index 损坏或无法读取。",
            ) from exc

    async def get_receipt(
        self, manifest_id: str
    ) -> EvolutionPostRollbackRemoteResultReceipt | None:
        manifest = await self.get_manifest(manifest_id)
        if manifest is None:
            return None
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM "
                        "evolution_post_rollback_remote_result_receipts "
                        "WHERE manifest_id = ?",
                        (manifest.manifest_id,),
                    )
                ).fetchone()
            if row is None:
                return None
            receipt = EvolutionPostRollbackRemoteResultReceipt.model_validate_json(
                row["receipt_json"]
            )
            if not (
                receipt.receipt_id == row["receipt_id"]
                and receipt.receipt_sha256 == row["receipt_sha256"]
                and receipt.attempt_id == row["attempt_id"]
                and _receipt_matches_manifest(receipt, manifest)
            ):
                raise ValueError("receipt lineage mismatch")
            return receipt
        except EvolutionPostRollbackRemoteResultError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackRemoteResultError(
                "post_rollback_remote_result_store_corrupt",
                "Remote result receipt 损坏或无法读取。",
            ) from exc

    async def get_admitted_at(self, manifest_id: str) -> datetime | None:
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT * FROM evolution_post_rollback_remote_result_manifests "
                    "WHERE manifest_id = ?",
                    (_manifest_id(manifest_id),),
                )
            ).fetchone()
        if row is None:
            return None
        self._stored_manifest(row)
        return _aware(str(row["admitted_at"]))

    async def record_manifest(
        self,
        manifest: EvolutionPostRollbackRemoteResultManifest,
        *,
        admitted_at: str,
        admission_attestation_sha256: str,
    ) -> EvolutionPostRollbackRemoteResultManifest:
        item = _manifest(manifest)
        admitted = _aware(admitted_at)
        encoded = item.model_dump_json()
        bounded = (
            item.payload.result_bytes
            + item.payload.repetitions * MAX_RUNTIME_EVAL_INPUT_BYTES
            + _MAX_MANIFEST_OVERHEAD_BYTES
        )
        if len(encoded.encode("utf-8")) > min(
            bounded,
            MAX_REMOTE_RESULT_MANIFEST_BYTES,
        ):
            raise EvolutionPostRollbackRemoteResultError(
                "post_rollback_remote_result_manifest_oversized",
                "Remote result manifest 元数据开销异常。",
            )
        key = self._control_plane_key()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                authority_row = await (
                    await db.execute(
                        "SELECT authorization_json FROM "
                        "evolution_post_rollback_remote_execution_authorizations "
                        "WHERE authorization_id = ?",
                        (item.payload.authorization_id,),
                    )
                ).fetchone()
                identity_row = await (
                    await db.execute(
                        "SELECT identity_json FROM authenticated_worker_identities "
                        "WHERE identity_id = ?",
                        (item.payload.identity_id,),
                    )
                ).fetchone()
                authority = (
                    None
                    if authority_row is None
                    else EvolutionPostRollbackRemoteExecutionAuthorization.model_validate_json(
                        authority_row["authorization_json"]
                    )
                )
                identity = (
                    None
                    if identity_row is None
                    else AuthenticatedWorkerIdentity.model_validate_json(
                        identity_row["identity_json"]
                    )
                )
                if not (
                    authority is not None
                    and identity is not None
                    and _payload_matches_authorization(item.payload, authority)
                    and identity.identity_sha256 == item.payload.identity_sha256
                    and admitted < _aware(authority.expires_at)
                    and hmac.compare_digest(
                        admission_attestation_sha256,
                        _admission_attestation(item, admitted_at=admitted, key=key),
                    )
                ):
                    await db.rollback()
                    raise EvolutionPostRollbackRemoteResultError(
                        "post_rollback_remote_result_admission_invalid",
                        "Remote result 缺少 current exact authorization admission。",
                    )
                _verify_worker_signature(identity.public_key_base64, item)
                existing = await (
                    await db.execute(
                        "SELECT * FROM evolution_post_rollback_remote_result_manifests "
                        "WHERE attempt_id = ?",
                        (item.payload.attempt_id,),
                    )
                ).fetchone()
                if existing is not None:
                    restored = self._stored_manifest(existing)
                    if restored == item:
                        await _record_lane_index(db, restored)
                        await db.commit()
                        return restored
                    await db.rollback()
                    raise EvolutionPostRollbackRemoteResultError(
                        "post_rollback_remote_result_attempt_conflict",
                        "同一 Remote attempt 已绑定不同结果 manifest。",
                    )
                await db.execute(
                    "INSERT INTO evolution_post_rollback_remote_result_manifests "
                    "(manifest_id, manifest_sha256, attempt_id, authorization_id, "
                    "manifest_json, completed_at, admitted_at, "
                    "admission_attestation_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.manifest_id,
                        item.manifest_sha256,
                        item.payload.attempt_id,
                        item.payload.authorization_id,
                        encoded,
                        item.payload.completed_at,
                        admitted.isoformat(),
                        admission_attestation_sha256,
                    ),
                )
                await _record_lane_index(db, item)
                await db.commit()
            return item
        except EvolutionPostRollbackRemoteResultError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackRemoteResultError(
                "post_rollback_remote_result_admission_failed",
                "Remote result manifest 无法原子准入。",
            ) from exc

    async def record_receipt(
        self,
        receipt: EvolutionPostRollbackRemoteResultReceipt,
    ) -> EvolutionPostRollbackRemoteResultReceipt:
        item = EvolutionPostRollbackRemoteResultReceipt.model_validate_json(
            receipt.model_dump_json()
        )
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                manifest_row = await (
                    await db.execute(
                        "SELECT * FROM evolution_post_rollback_remote_result_manifests "
                        "WHERE manifest_id = ?",
                        (item.manifest_id,),
                    )
                ).fetchone()
                if manifest_row is None:
                    await db.rollback()
                    raise EvolutionPostRollbackRemoteResultError(
                        "post_rollback_remote_result_admission_missing",
                        "Remote result receipt 缺少 durable admission。",
                    )
                manifest = self._stored_manifest(manifest_row)
                if not _receipt_matches_manifest(item, manifest):
                    await db.rollback()
                    raise EvolutionPostRollbackRemoteResultError(
                        "post_rollback_remote_result_receipt_mismatch",
                        "Remote result receipt 与 manifest 不一致。",
                    )
                existing = await (
                    await db.execute(
                        "SELECT receipt_json FROM "
                        "evolution_post_rollback_remote_result_receipts "
                        "WHERE manifest_id = ?",
                        (item.manifest_id,),
                    )
                ).fetchone()
                if existing is not None:
                    restored = EvolutionPostRollbackRemoteResultReceipt.model_validate_json(
                        existing["receipt_json"]
                    )
                    await db.rollback()
                    if restored == item:
                        return restored
                    raise EvolutionPostRollbackRemoteResultError(
                        "post_rollback_remote_result_receipt_conflict",
                        "Remote result 已绑定不同 ingestion receipt。",
                    )
                await db.execute(
                    "INSERT INTO evolution_post_rollback_remote_result_receipts "
                    "(receipt_id, receipt_sha256, manifest_id, attempt_id, "
                    "receipt_json, ingested_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        item.receipt_id,
                        item.receipt_sha256,
                        item.manifest_id,
                        item.attempt_id,
                        item.model_dump_json(),
                        item.ingested_at,
                    ),
                )
                await db.commit()
            return item
        except EvolutionPostRollbackRemoteResultError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackRemoteResultError(
                "post_rollback_remote_result_receipt_store_failed",
                "Remote result ingestion receipt 无法持久化。",
            ) from exc

    def _stored_manifest(self, row) -> EvolutionPostRollbackRemoteResultManifest:
        manifest = EvolutionPostRollbackRemoteResultManifest.model_validate_json(
            row["manifest_json"]
        )
        admitted = _aware(str(row["admitted_at"]))
        if not (
            manifest.manifest_id == row["manifest_id"]
            and manifest.manifest_sha256 == row["manifest_sha256"]
            and manifest.payload.attempt_id == row["attempt_id"]
            and manifest.payload.authorization_id == row["authorization_id"]
            and manifest.payload.completed_at == row["completed_at"]
            and hmac.compare_digest(
                str(row["admission_attestation_sha256"]),
                _admission_attestation(
                    manifest,
                    admitted_at=admitted,
                    key=self._control_plane_key(),
                ),
            )
        ):
            raise EvolutionPostRollbackRemoteResultError(
                "post_rollback_remote_result_admission_tampered",
                "Remote result admission attestation 无效。",
            )
        return manifest

    def _control_plane_key(self) -> bytes:
        key = self._control_plane_key_provider()
        if not isinstance(key, bytes) or len(key) < 32:
            raise EvolutionPostRollbackRemoteResultError(
                "post_rollback_remote_result_control_plane_key_invalid",
                "Remote result control-plane key 不可用。",
            )
        return key

    async def _backfill_lane_index(self, db: aiosqlite.Connection) -> None:
        rows = await (
            await db.execute(
                "SELECT manifests.* FROM "
                "evolution_post_rollback_remote_result_manifests AS manifests "
                "LEFT JOIN evolution_post_rollback_remote_result_lane_index AS lane_index "
                "ON lane_index.manifest_id = manifests.manifest_id "
                "WHERE lane_index.manifest_id IS NULL "
                "ORDER BY manifests.admitted_at, manifests.manifest_id LIMIT 1001"
            )
        ).fetchall()
        if len(rows) > 1000:
            raise EvolutionPostRollbackRemoteResultError(
                "post_rollback_remote_result_lane_index_backlog",
                "Remote result lane index 待迁移记录超过安全上限。",
            )
        for row in rows:
            await _record_lane_index(db, self._stored_manifest(row))
        if rows:
            await db.commit()


class EvolutionPostRollbackRemoteResultService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        authorization_service: EvolutionPostRollbackRemoteExecutionAuthorizationService,
        authorization_store: EvolutionPostRollbackRemoteExecutionAuthorizationStore,
        behavioral_lane_service: EvolutionPostRollbackBehavioralLaneService,
        harness_store: HarnessStore,
        store: EvolutionPostRollbackRemoteResultStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        session_paths = {
            authorization_store.db_path,
            authorization_service.identity_authority.store.db_path,
            store.db_path,
        }
        if len(session_paths) != 1:
            raise ValueError(
                "Remote Result 的 Authorization/Identity/Result store 必须共享 session SQLite。"
            )
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.authorization_service = authorization_service
        self.authorization_store = authorization_store
        self.behavioral_lane_service = behavioral_lane_service
        self.harness_store = harness_store
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def submit(
        self,
        *,
        manifest: EvolutionPostRollbackRemoteResultManifest,
        received_at: str | None = None,
    ) -> EvolutionPostRollbackRemoteResultView:
        item = _manifest(manifest)
        now = _aware(received_at) if received_at is not None else _aware(self.clock())
        lock = self._locks.setdefault(item.payload.attempt_id, asyncio.Lock())
        async with lock:
            return await self._submit_locked(item, now=now)

    async def _submit_locked(
        self,
        item: EvolutionPostRollbackRemoteResultManifest,
        *,
        now: datetime,
    ) -> EvolutionPostRollbackRemoteResultView:
        existing = await self.store.get_by_attempt(item.payload.attempt_id)
        if existing is not None and existing != item:
            raise EvolutionPostRollbackRemoteResultError(
                "post_rollback_remote_result_attempt_conflict",
                "同一 Remote attempt 已提交不同结果。",
            )
        receipt = await self.store.get_receipt(item.manifest_id)
        if receipt is not None:
            return await self.inspect(manifest_id=item.manifest_id)
        if existing is None:
            authorization_view = await self.authorization_service.inspect(
                reference_id=item.payload.authorization_id,
                assessed_at=now.isoformat(),
            )
            if authorization_view.status != "current" or authorization_view.authorization is None:
                raise EvolutionPostRollbackRemoteResultError(
                    "post_rollback_remote_result_authorization_not_current",
                    "Remote result 到达时 execution authorization 已不可用。",
                )
            authorization = authorization_view.authorization
        else:
            authorization = await self.authorization_store.get_authorization_for_attempt(
                item.payload.attempt_id
            )
            if authorization is None:
                raise EvolutionPostRollbackRemoteResultError(
                    "post_rollback_remote_result_authorization_missing",
                    "已准入 Remote result 的 execution authorization 缺失。",
                )
        baseline = await self._require_binding(item, authorization, now=now)
        identity = await self.authorization_service.identity_authority.store.get(
            item.payload.identity_id
        )
        if identity is None:
            raise EvolutionPostRollbackRemoteResultError(
                "post_rollback_remote_result_identity_missing",
                "Remote result Worker Identity 不存在。",
            )
        _verify_worker_signature(identity.public_key_base64, item)
        runtime_receipts = _runtime_receipts(item, baseline)
        try:
            await self.behavioral_lane_service.validate_remote_runtime_receipts(
                request_id=baseline.request_id,
                comparison_id=baseline.comparison_id,
                receipts=runtime_receipts,
            )
        except EvolutionPostRollbackBehavioralLaneError as exc:
            raise EvolutionPostRollbackRemoteResultError(exc.code, str(exc)) from exc
        if existing is None:
            await self.store.record_manifest(
                item,
                admitted_at=now.isoformat(),
                admission_attestation_sha256=_admission_attestation(
                    item,
                    admitted_at=now,
                    key=self.store._control_plane_key(),
                ),
            )
        admitted_at = await self.store.get_admitted_at(item.manifest_id)
        if admitted_at is None:
            raise EvolutionPostRollbackRemoteResultError(
                "post_rollback_remote_result_admission_missing",
                "Remote result durable admission 未成功持久化。",
            )
        batch_id = _batch_id(item)
        try:
            records, comparison = (
                await self.behavioral_lane_service.record_remote_runtime_receipts(
                    request_id=baseline.request_id,
                    comparison_id=baseline.comparison_id,
                    batch_id=batch_id,
                    receipts=runtime_receipts,
                )
            )
        except EvolutionPostRollbackBehavioralLaneError as exc:
            raise EvolutionPostRollbackRemoteResultError(exc.code, str(exc)) from exc
        result = _build_receipt(
            manifest=item,
            runtime_receipts=runtime_receipts,
            h5a_batch_id=batch_id,
            h5a_result_sha256=tuple(record.result_sha256 for record in records),
            comparison=comparison,
            admitted_at=admitted_at,
            ingested_at=max(
                now,
                max(_aware(record.created_at) for record in records),
                _aware(comparison.created_at),
            ),
        )
        await self.store.record_receipt(result)
        return await self.inspect(manifest_id=item.manifest_id)

    async def inspect(
        self,
        *,
        manifest_id: str,
    ) -> EvolutionPostRollbackRemoteResultView:
        manifest = await self.store.get_manifest(manifest_id)
        if manifest is None:
            raise EvolutionPostRollbackRemoteResultError(
                "post_rollback_remote_result_manifest_missing",
                "Remote result manifest 不存在。",
            )
        receipt = await self.store.get_receipt(manifest.manifest_id)
        if receipt is None:
            return EvolutionPostRollbackRemoteResultView(
                manifest=manifest,
                receipt=None,
                status="admitted",
                durable_admission=True,
                worker_signature_verified=True,
                h5a_authority=False,
                h5c_authority=False,
                lane_evaluation_authority=False,
            )
        valid = False
        try:
            baseline_store = (
                self.authorization_service.delivery_service.baseline_store
            )
            baseline = await baseline_store.get_by_id(receipt.baseline_resolution_id)
            records = await self.harness_store.list_eval_results(
                self.workspace_root,
                receipt.h5a_batch_id,
                manifest.payload.suite_id,
                limit=receipt.repetitions + 1,
            )
            comparison = await self.harness_store.get_eval_comparison_receipt_by_id(
                self.workspace_root,
                receipt.h5c_comparison_id,
            )
            valid = bool(
                baseline is not None
                and tuple(
                    item.receipt_sha256
                    for item in _runtime_receipts(manifest, baseline)
                )
                == receipt.runtime_receipt_sha256
                and len(records) == receipt.repetitions
                and tuple(record.result_sha256 for record in records)
                == receipt.h5a_result_sha256
                and comparison is not None
                and _comparison_matches_receipt(receipt, comparison.receipt)
                and comparison.receipt.receipt_sha256 == receipt.h5c_comparison_sha256
            )
        except (HarnessStoreError, OSError, TypeError, ValueError):
            valid = False
        return EvolutionPostRollbackRemoteResultView(
            manifest=manifest,
            receipt=receipt,
            status="ingested" if valid else "stale",
            durable_admission=True,
            worker_signature_verified=True,
            h5a_authority=valid,
            h5c_authority=valid,
            lane_evaluation_authority=valid,
        )

    async def _require_binding(
        self,
        manifest: EvolutionPostRollbackRemoteResultManifest,
        authorization: EvolutionPostRollbackRemoteExecutionAuthorization,
        *,
        now: datetime,
    ) -> EvolutionPostRollbackTargetBaseline:
        payload = manifest.payload
        if not (
            _payload_matches_authorization(payload, authorization)
            and _aware(authorization.authorized_at) <= _aware(payload.started_at)
            and _aware(payload.started_at) <= _aware(payload.completed_at) <= now
            and _aware(payload.completed_at) < _aware(authorization.expires_at)
            and payload.result_bytes <= authorization.start_payload.max_result_bytes
        ):
            raise EvolutionPostRollbackRemoteResultError(
                "post_rollback_remote_result_authorization_mismatch",
                "Remote result 未绑定 exact execution authorization/window。",
            )
        dispatch = await self.authorization_service.dispatch_store.get_by_id(
            payload.dispatch_id
        )
        baseline = await self.authorization_service.delivery_service.baseline_store.get_by_id(
            payload.baseline_resolution_id
        )
        if dispatch is None or baseline is None or not (
            dispatch.dispatch_sha256 == payload.dispatch_sha256
            and dispatch.outcome_id == payload.outcome_id
            and dispatch.request_id == payload.request_id
            and dispatch.comparison_id == payload.comparison_id
            and dispatch.baseline_resolution_id == baseline.baseline_resolution_id
            and baseline.baseline_resolution_sha256
            == payload.baseline_resolution_sha256
            and baseline.outcome_id == payload.outcome_id
            and baseline.request_id == payload.request_id
            and baseline.comparison_id == payload.comparison_id
            and baseline.release_target == payload.release_target
        ):
            raise EvolutionPostRollbackRemoteResultError(
                "post_rollback_remote_result_lineage_mismatch",
                "Remote result Dispatch/Target Baseline lineage 不一致。",
            )
        try:
            baseline_service = (
                self.authorization_service.delivery_service.baseline_service
            )
            baseline_view = await baseline_service.inspect(baseline=baseline)
        except EvolutionPostRollbackTargetBaselineError as exc:
            raise EvolutionPostRollbackRemoteResultError(exc.code, str(exc)) from exc
        if not baseline_view.baseline_resolution_authority:
            raise EvolutionPostRollbackRemoteResultError(
                "post_rollback_remote_result_baseline_stale",
                "Remote result Target Baseline authority 已失效。",
            )
        return baseline


def issue_post_rollback_remote_result_manifest(
    *,
    payload: EvolutionPostRollbackRemoteResultPayload,
    private_key,
) -> EvolutionPostRollbackRemoteResultManifest:
    if not hasattr(private_key, "sign"):
        raise TypeError("private_key 必须支持 Ed25519 sign。")
    item = EvolutionPostRollbackRemoteResultPayload.model_validate_json(
        payload.model_dump_json()
    )
    digest = _digest(item.model_dump(mode="json"))
    signature = private_key.sign(item.canonical_bytes())
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise ValueError("Remote result signature 必须为 64-byte Ed25519。")
    return EvolutionPostRollbackRemoteResultManifest.model_validate(
        {
            "schema_version": 1,
            "policy_version": EVOLUTION_POST_ROLLBACK_REMOTE_RESULT_POLICY,
            "manifest_id": f"evpostresultmanifest_{digest[:24]}",
            "manifest_sha256": digest,
            "payload": item.model_dump(mode="json"),
            "signature_algorithm": "ed25519",
            "signature_base64": base64.b64encode(signature).decode("ascii"),
            "signature_sha256": hashlib.sha256(signature).hexdigest(),
            "worker_signed": True,
            "durable_admission": False,
            "result_authority": False,
            "h5a_ingested": False,
            "h5c_recorded": False,
            "learning_authority": False,
            "promotion_authority": False,
        }
    )


def render_post_rollback_remote_result(view: EvolutionPostRollbackRemoteResultView) -> str:
    item = view.manifest.payload
    receipt = view.receipt
    lines = [
        f"# Remote Runtime Result `{view.manifest.manifest_id}`",
        "",
        f"- 状态：`{view.status}`",
        f"- Attempt：`{item.attempt_id}`",
        f"- Target：`{item.release_target}`",
        f"- Worker：`{item.worker_id}` / epoch `{item.worker_epoch}`",
        f"- Suite：`{item.suite_id}` × `{item.repetitions}`",
        f"- Worker signature：`verified={str(view.worker_signature_verified).lower()}`",
        f"- Durable admission：`{str(view.durable_admission).lower()}`",
        f"- H5a/H5c：`{str(view.h5a_authority).lower()}` / `{str(view.h5c_authority).lower()}`",
        "- Behavioral Matrix：`false`",
        "- Learning / Promotion authority：`false / false`",
    ]
    if receipt is not None:
        lines.extend(
            [
                f"- H5a batch：`{receipt.h5a_batch_id}`",
                f"- H5c comparison：`{receipt.h5c_comparison_id}`",
                f"- Ingestion receipt：`{receipt.receipt_id}`",
            ]
        )
    return "\n".join(lines)


def _runtime_receipts(
    manifest: EvolutionPostRollbackRemoteResultManifest,
    baseline: EvolutionPostRollbackTargetBaseline,
) -> tuple[ReleaseRuntimeEvalReceipt, ...]:
    payload = manifest.payload
    return tuple(
        build_runtime_eval_receipt(
            slot_id=baseline.local_baseline_slot_id,
            slot_sha256=baseline.local_baseline_slot_sha256,
            manifest_sha256=payload.manifest_sha256,
            binary_sha256=payload.installed_binary_sha256,
            request=payload.runtime_eval_request,
            process_request=artifact.process_request,
            response=artifact.response,
            input_bytes=artifact.input_bytes,
            output_bytes=artifact.output_bytes,
        )
        for artifact in payload.artifacts
    )


def _build_receipt(
    *,
    manifest: EvolutionPostRollbackRemoteResultManifest,
    runtime_receipts: tuple[ReleaseRuntimeEvalReceipt, ...],
    h5a_batch_id: str,
    h5a_result_sha256: tuple[str, ...],
    comparison: HarnessEvalComparisonReceipt,
    admitted_at: datetime,
    ingested_at: datetime,
) -> EvolutionPostRollbackRemoteResultReceipt:
    payload = manifest.payload
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_POST_ROLLBACK_REMOTE_RESULT_POLICY,
        "manifest_id": manifest.manifest_id,
        "manifest_sha256": manifest.manifest_sha256,
        "authorization_id": payload.authorization_id,
        "authorization_sha256": payload.authorization_sha256,
        "attempt_id": payload.attempt_id,
        "baseline_resolution_id": payload.baseline_resolution_id,
        "outcome_id": payload.outcome_id,
        "request_id": payload.request_id,
        "comparison_id": payload.comparison_id,
        "release_target": payload.release_target,
        "repetitions": payload.repetitions,
        "runtime_receipt_sha256": [item.receipt_sha256 for item in runtime_receipts],
        "h5a_batch_id": h5a_batch_id,
        "h5a_result_sha256": list(h5a_result_sha256),
        "h5c_comparison_id": comparison.id,
        "h5c_comparison_sha256": comparison.receipt_sha256,
        "result_received": True,
        "worker_signature_verified": True,
        "durable_admission": True,
        "h5a_ingested": True,
        "h5c_recorded": True,
        "full_cohort_received": True,
        "lane_evaluation_recorded": True,
        "behavioral_matrix_recorded": False,
        "learning_authority": False,
        "promotion_authority": False,
        "admitted_at": admitted_at.isoformat(),
        "ingested_at": ingested_at.isoformat(),
    }
    digest = _digest(core)
    return EvolutionPostRollbackRemoteResultReceipt.model_validate(
        {
            **core,
            "receipt_id": f"evpostresultreceipt_{digest[:24]}",
            "receipt_sha256": digest,
        }
    )


def _payload_matches_authorization(payload, authorization) -> bool:
    start = authorization.start_payload
    return bool(
        payload.authorization_sha256 == authorization.authorization_sha256
        and payload.attempt_id == authorization.attempt_id == start.attempt_id
        and payload.delivery_receipt_id == start.delivery_receipt_id
        and payload.delivery_receipt_sha256 == start.delivery_receipt_sha256
        and payload.dispatch_id == start.dispatch_id
        and payload.dispatch_sha256 == start.dispatch_sha256
        and payload.claim_receipt_id == start.claim_receipt_id
        and payload.claim_receipt_sha256 == start.claim_receipt_sha256
        and payload.claim_lease_epoch == start.claim_lease_epoch
        and payload.identity_id == start.identity_id
        and payload.identity_sha256 == start.identity_sha256
        and payload.worker_id == start.worker_id
        and payload.worker_instance_id == start.worker_instance_id
        and payload.worker_epoch == start.worker_epoch
        and payload.baseline_resolution_sha256 == start.baseline_resolution_sha256
        and payload.release_target == start.release_target
        and payload.archive_sha256 == start.archive_sha256
        and payload.manifest_sha256 == start.manifest_sha256
        and payload.suite_id == start.suite_id
        and payload.suite_sha256 == start.suite_sha256
        and payload.repetitions == start.repetitions
        and payload.runtime_eval_request == start.runtime_eval_request
        and payload.run_grant_sha256 == authorization.run_grant.grant_sha256
        and payload.runtime_lease_epoch == authorization.runtime_lease_epoch
    )


def _verify_worker_signature(public_key_base64: str, manifest) -> None:
    try:
        public = base64.b64decode(public_key_base64, validate=True)
        Ed25519PublicKey.from_public_bytes(public).verify(
            _decode_signature(manifest.signature_base64),
            manifest.payload.canonical_bytes(),
        )
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise EvolutionPostRollbackRemoteResultError(
            "post_rollback_remote_result_signature_invalid",
            "Remote result Worker signature 无效。",
        ) from exc


def _admission_attestation(manifest, *, admitted_at: datetime, key: bytes) -> str:
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


def _receipt_matches_manifest(receipt, manifest) -> bool:
    payload = manifest.payload
    return bool(
        receipt.manifest_sha256 == manifest.manifest_sha256
        and receipt.authorization_id == payload.authorization_id
        and receipt.authorization_sha256 == payload.authorization_sha256
        and receipt.attempt_id == payload.attempt_id
        and receipt.baseline_resolution_id == payload.baseline_resolution_id
        and receipt.outcome_id == payload.outcome_id
        and receipt.request_id == payload.request_id
        and receipt.comparison_id == payload.comparison_id
        and receipt.release_target == payload.release_target
        and receipt.repetitions == payload.repetitions
    )


def _comparison_matches_receipt(receipt, comparison) -> bool:
    return bool(
        comparison.id == receipt.h5c_comparison_id
        and comparison.receipt_sha256 == receipt.h5c_comparison_sha256
        and comparison.current_batch_id == receipt.h5a_batch_id
        and comparison.current_samples == receipt.repetitions
    )


def _batch_id(manifest) -> str:
    digest = _digest(
        {
            "domain": "naumi.evolution.post-rollback-remote-h5a-batch.v1",
            "attempt_id": manifest.payload.attempt_id,
            "manifest_sha256": manifest.manifest_sha256,
        }
    )
    return f"postrollback-remote-{digest[:32]}"


def _architecture_matches(expected: str, actual: str) -> bool:
    normalized = actual.strip().casefold().replace("-", "_")
    aliases = {
        "arm64": {"arm64", "aarch64"},
        "x64": {"x86_64", "amd64", "x64"},
    }
    return normalized in aliases[expected]


def _decode_signature(value: str) -> bytes:
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("Remote result signature 不是 canonical base64。") from exc
    if len(raw) != 64 or base64.b64encode(raw).decode("ascii") != value:
        raise ValueError("Remote result signature 必须是 canonical 64-byte base64。")
    return raw


def _manifest(value) -> EvolutionPostRollbackRemoteResultManifest:
    try:
        return EvolutionPostRollbackRemoteResultManifest.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionPostRollbackRemoteResultError(
            "post_rollback_remote_result_manifest_invalid",
            "Remote result manifest 无效。",
        ) from exc


def _manifest_id(value: str) -> str:
    normalized = str(value or "").strip()
    if not re.fullmatch(r"^evpostresultmanifest_[0-9a-f]{24}$", normalized):
        raise EvolutionPostRollbackRemoteResultError(
            "post_rollback_remote_result_manifest_id_invalid",
            "Remote result manifest ID 格式无效。",
        )
    return normalized


def _attempt_id(value: str) -> str:
    normalized = str(value or "").strip()
    if not re.fullmatch(r"^evpostattempt_[0-9a-f]{24}$", normalized):
        raise EvolutionPostRollbackRemoteResultError(
            "post_rollback_remote_result_attempt_id_invalid",
            "Remote result attempt ID 格式无效。",
        )
    return normalized


def _outcome_id(value: str) -> str:
    normalized = str(value or "").strip()
    if re.fullmatch(r"^evrerollbackout_[0-9a-f]{24}$", normalized) is None:
        raise EvolutionPostRollbackRemoteResultError(
            "post_rollback_remote_result_outcome_id_invalid",
            "Rollback Outcome ID 格式无效。",
        )
    return normalized


def _comparison_id(value: str) -> str:
    normalized = str(value or "").strip()
    if re.fullmatch(_SHA256_RE, normalized) is None:
        raise EvolutionPostRollbackRemoteResultError(
            "post_rollback_remote_result_comparison_id_invalid",
            "Remote result 原 H5c Comparison ID 格式无效。",
        )
    return normalized


def _aware(value) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(parsed, datetime) or parsed.utcoffset() is None:
        raise ValueError("Remote result 时间必须包含 offset。")
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


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS evolution_post_rollback_remote_result_manifests (
            manifest_id TEXT PRIMARY KEY,
            manifest_sha256 TEXT NOT NULL UNIQUE,
            attempt_id TEXT NOT NULL UNIQUE,
            authorization_id TEXT NOT NULL UNIQUE,
            manifest_json TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            admitted_at TEXT NOT NULL,
            admission_attestation_sha256 TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS evolution_post_rollback_remote_result_receipts (
            receipt_id TEXT PRIMARY KEY,
            receipt_sha256 TEXT NOT NULL UNIQUE,
            manifest_id TEXT NOT NULL UNIQUE,
            attempt_id TEXT NOT NULL UNIQUE,
            receipt_json TEXT NOT NULL,
            ingested_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS evolution_post_rollback_remote_result_lane_index (
            manifest_id TEXT PRIMARY KEY,
            outcome_id TEXT NOT NULL,
            comparison_id TEXT NOT NULL,
            request_id TEXT NOT NULL,
            suite_id TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_post_rollback_remote_result_lane
        ON evolution_post_rollback_remote_result_lane_index(
            outcome_id, comparison_id, manifest_id
        );
        """
    )


async def _record_lane_index(
    db: aiosqlite.Connection,
    manifest: EvolutionPostRollbackRemoteResultManifest,
) -> None:
    payload = manifest.payload
    exact = (
        manifest.manifest_id,
        payload.outcome_id,
        payload.comparison_id,
        payload.request_id,
        payload.suite_id,
    )
    await db.execute(
        "INSERT OR IGNORE INTO evolution_post_rollback_remote_result_lane_index "
        "(manifest_id, outcome_id, comparison_id, request_id, suite_id) "
        "VALUES (?, ?, ?, ?, ?)",
        exact,
    )
    existing = await (
        await db.execute(
            "SELECT * FROM evolution_post_rollback_remote_result_lane_index "
            "WHERE manifest_id = ?",
            (manifest.manifest_id,),
        )
    ).fetchone()
    current = (
        None
        if existing is None
        else (
            existing["manifest_id"],
            existing["outcome_id"],
            existing["comparison_id"],
            existing["request_id"],
            existing["suite_id"],
        )
    )
    if current != exact:
        raise EvolutionPostRollbackRemoteResultError(
            "post_rollback_remote_result_lane_index_tampered",
            "Remote result lane index 与 signed manifest 不一致。",
        )


__all__ = [
    "EVOLUTION_POST_ROLLBACK_REMOTE_RESULT_DOMAIN",
    "EVOLUTION_POST_ROLLBACK_REMOTE_RESULT_POLICY",
    "MAX_REMOTE_RESULT_MANIFEST_BYTES",
    "EvolutionPostRollbackRemoteResultArtifact",
    "EvolutionPostRollbackRemoteResultError",
    "EvolutionPostRollbackRemoteResultManifest",
    "EvolutionPostRollbackRemoteResultPayload",
    "EvolutionPostRollbackRemoteResultReceipt",
    "EvolutionPostRollbackRemoteResultService",
    "EvolutionPostRollbackRemoteResultStore",
    "EvolutionPostRollbackRemoteResultView",
    "issue_post_rollback_remote_result_manifest",
    "render_post_rollback_remote_result",
]
