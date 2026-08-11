"""Writer-fenced remote stable-member finalization with signed result ingest."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self
from weakref import WeakValueDictionary

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.revalidation_rollout_stage_entries import (
    EvolutionRevalidationRolloutControlEvent,
    EvolutionRevalidationRolloutControlState,
)
from naumi_agent.evolution.stable_remote_finalization_authorizations import (
    EvolutionStableRemoteFinalizationAuthorizationEnvelope,
    EvolutionStableRemoteFinalizationAuthorizationService,
    EvolutionStableRemoteFinalizationConsumptionReceipt,
    verify_stable_remote_finalization_authorization,
)
from naumi_agent.release.installation_keys import (
    RELEASE_INSTALLATION_FINALIZATION_SIGNATURE_DOMAIN,
    ReleaseInstallationKeyError,
    ReleaseInstallationKeyService,
    ReleaseInstallationSignature,
    verify_release_installation_signature,
)
from naumi_agent.release.population_registry import ReleaseManagedInstallationCredential
from naumi_agent.release.rollout_control_keys import (
    RELEASE_ROLLOUT_CONTROL_EXECUTION_GRANT_SIGNATURE_DOMAIN,
    ReleaseRolloutControlKeyError,
    ReleaseRolloutControlKeyService,
    ReleaseRolloutControlSignature,
    ReleaseRolloutControlTrustPolicyDocument,
    load_release_rollout_control_trust_policy,
    verify_release_rollout_control_signature,
)
from naumi_agent.release.slots import (
    ReleaseSlotError,
    ReleaseSlotStore,
    ReleaseStableMemberFinalization,
    ReleaseStableMemberFinalizationAuthority,
)

EVOLUTION_STABLE_REMOTE_FINALIZATION_POLICY = (
    "evolution-stable-remote-finalization-executor-v1"
)
_AUTH_RE = re.compile(r"^evstableremotefinalauth_[0-9a-f]{24}$")
_GRANT_RE = re.compile(r"^evstableremotefinalgrant_[0-9a-f]{24}$")
_RECEIPT_RE = re.compile(r"^evstableremotefinalreceipt_[0-9a-f]{24}$")
_MAX_ARTIFACT_BYTES = 512 * 1024
_MAX_ENCODED_CHARS = ((_MAX_ARTIFACT_BYTES + 2) // 3) * 4
_MAX_LATE_RECOVERY_SECONDS = 24 * 60 * 60


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class EvolutionStableRemoteFinalizationExecutionGrant(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-stable-remote-finalization-executor-v1"
    ] = EVOLUTION_STABLE_REMOTE_FINALIZATION_POLICY
    grant_id: str = Field(pattern=r"^evstableremotefinalgrant_[0-9a-f]{24}$")
    grant_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_id: str = Field(pattern=r"^evstableremotefinalauth_[0-9a-f]{24}$")
    authorization_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_signature_id: str = Field(pattern=r"^relrolloutsig_[0-9a-f]{24}$")
    consumption_receipt_id: str = Field(
        pattern=r"^evstableremotefinalconsume_[0-9a-f]{24}$"
    )
    consumption_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    installation_member_id: str = Field(pattern=r"^relpopmember_[0-9a-f]{24}$")
    installation_credential_id: str = Field(pattern=r"^relpopcred_[0-9a-f]{24}$")
    installation_credential_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    installation_public_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    release_root_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_pointer_id: str = Field(pattern=r"^relactive_[0-9a-f]{24}$")
    expected_pointer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_pointer_generation: int = Field(ge=2, le=1_000_000_000)
    issued_at: str = Field(min_length=1, max_length=100)
    expires_at: str = Field(min_length=1, max_length=100)
    operation: Literal["finalize_stable_population_member"] = (
        "finalize_stable_population_member"
    )
    operation_scope: Literal["binary_only"] = "binary_only"
    authorization_consumed: Literal[True] = True
    single_execution_required: Literal[True] = True
    expected_pointer_cas_required: Literal[True] = True
    config_data_mutation_allowed: Literal[False] = False
    deployment_authority: Literal[False] = False
    rollback_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if not _aware(self.issued_at) < _aware(self.expires_at):
            raise ValueError("Remote Finalization Execution Grant 时间窗口无效。")
        digest = _digest(
            self.model_dump(mode="json", exclude={"grant_id", "grant_sha256"})
        )
        if self.grant_sha256 != digest or self.grant_id != (
            f"evstableremotefinalgrant_{digest[:24]}"
        ):
            raise ValueError("Remote Finalization Execution Grant identity 不一致。")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.model_dump(mode="json"))


class EvolutionStableRemoteFinalizationExecutionPackage(_StrictModel):
    schema_version: Literal[1] = 1
    authorization: EvolutionStableRemoteFinalizationAuthorizationEnvelope
    consumption: EvolutionStableRemoteFinalizationConsumptionReceipt
    grant: EvolutionStableRemoteFinalizationExecutionGrant
    signature: ReleaseRolloutControlSignature

    @model_validator(mode="after")
    def _binding(self) -> Self:
        auth = self.authorization.authorization
        grant = self.grant
        consumption = self.consumption
        payload = grant.canonical_bytes()
        if not (
            grant.authorization_id == auth.authorization_id
            and grant.authorization_sha256 == auth.authorization_sha256
            and grant.authorization_signature_id == self.authorization.signature.signature_id
            and grant.consumption_receipt_id == consumption.receipt_id
            and grant.consumption_receipt_sha256 == consumption.receipt_sha256
            and consumption.authorization_id == auth.authorization_id
            and consumption.authorization_sha256 == auth.authorization_sha256
            and grant.installation_member_id == auth.installation_member_id
            and grant.installation_credential_id == auth.installation_credential_id
            and grant.installation_credential_sha256
            == auth.installation_credential_sha256
            and grant.installation_public_key_sha256
            == auth.installation_public_key_sha256
            and grant.release_root_sha256 == auth.release_root_sha256
            and grant.expected_pointer_id == auth.expected_active_pointer_id
            and grant.expected_pointer_sha256 == auth.expected_active_pointer_sha256
            and grant.expected_pointer_generation
            == auth.expected_active_pointer_generation
            and grant.issued_at == consumption.consumed_at
            and grant.expires_at == auth.expires_at
            and self.signature.domain
            == RELEASE_ROLLOUT_CONTROL_EXECUTION_GRANT_SIGNATURE_DOMAIN
            and self.signature.channel == "stable"
            and self.signature.payload_sha256 == hashlib.sha256(payload).hexdigest()
            and self.signature.payload_bytes == len(payload)
            and _aware(grant.issued_at)
            <= _aware(self.signature.signed_at)
            < _aware(grant.expires_at)
        ):
            raise ValueError("Remote Finalization Execution Package binding 无效。")
        return self


class EvolutionStableRemoteFinalizationResult(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-stable-remote-finalization-executor-v1"
    ] = EVOLUTION_STABLE_REMOTE_FINALIZATION_POLICY
    result_id: str = Field(pattern=r"^evstableremotefinalresult_[0-9a-f]{24}$")
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    grant_id: str = Field(pattern=r"^evstableremotefinalgrant_[0-9a-f]{24}$")
    grant_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_id: str = Field(pattern=r"^evstableremotefinalauth_[0-9a-f]{24}$")
    authorization_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    consumption_receipt_id: str = Field(
        pattern=r"^evstableremotefinalconsume_[0-9a-f]{24}$"
    )
    consumption_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    installation_member_id: str = Field(pattern=r"^relpopmember_[0-9a-f]{24}$")
    installation_credential_id: str = Field(pattern=r"^relpopcred_[0-9a-f]{24}$")
    release_root_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    release_finalization: ReleaseStableMemberFinalization
    completed_at: str = Field(min_length=1, max_length=100)
    target_sources_reread: Literal[True] = True
    authorization_signature_verified: Literal[True] = True
    execution_grant_signature_verified: Literal[True] = True
    expected_pointer_cas_satisfied: Literal[True] = True
    binary_stable_member_finalized: Literal[True] = True
    config_data_mutation_executed: Literal[False] = False
    deployment_executed: Literal[False] = False
    rollback_executed: Literal[False] = False
    promotion_executed: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        final = self.release_finalization
        if not (
            final.authority.kind
            == "evolution_stable_remote_finalization_authorization"
            and final.authority.authority_id == self.authorization_id
            and final.authority.authority_sha256 == self.authorization_sha256
            and final.authority.installation_member_id == self.installation_member_id
            and final.finalized_at == self.completed_at
        ):
            raise ValueError("Remote Finalization Result writer projection 不一致。")
        digest = _digest(
            self.model_dump(mode="json", exclude={"result_id", "result_sha256"})
        )
        if self.result_sha256 != digest or self.result_id != (
            f"evstableremotefinalresult_{digest[:24]}"
        ):
            raise ValueError("Remote Finalization Result identity 不一致。")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.model_dump(mode="json"))


class EvolutionStableRemoteFinalizationSubmission(_StrictModel):
    schema_version: Literal[1] = 1
    result: EvolutionStableRemoteFinalizationResult
    signature: ReleaseInstallationSignature

    @model_validator(mode="after")
    def _binding(self) -> Self:
        payload = self.result.canonical_bytes()
        if not (
            self.signature.domain
            == RELEASE_INSTALLATION_FINALIZATION_SIGNATURE_DOMAIN
            and self.signature.credential_id
            == self.result.installation_credential_id
            and self.signature.installation_member_id
            == self.result.installation_member_id
            and self.signature.payload_sha256 == hashlib.sha256(payload).hexdigest()
            and self.signature.payload_bytes == len(payload)
            and _aware(self.result.completed_at) <= _aware(self.signature.signed_at)
        ):
            raise ValueError("Remote Finalization Submission signature binding 无效。")
        return self


class EvolutionStableRemoteFinalizationReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-stable-remote-finalization-executor-v1"
    ] = EVOLUTION_STABLE_REMOTE_FINALIZATION_POLICY
    receipt_id: str = Field(pattern=r"^evstableremotefinalreceipt_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_package: EvolutionStableRemoteFinalizationExecutionPackage
    submission: EvolutionStableRemoteFinalizationSubmission
    recorded_at: str = Field(min_length=1, max_length=100)
    control_plane_sources_revalidated: Literal[True] = True
    installation_signature_verified: Literal[True] = True
    durable_single_use_observed: Literal[True] = True
    stable_member_completion_fact: Literal[True] = True
    stable_population_completion_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        package = self.execution_package
        result = self.submission.result
        if not (
            result.grant_id == package.grant.grant_id
            and result.grant_sha256 == package.grant.grant_sha256
            and result.authorization_id == package.grant.authorization_id
            and result.authorization_sha256 == package.grant.authorization_sha256
            and result.consumption_receipt_id == package.consumption.receipt_id
            and result.consumption_receipt_sha256 == package.consumption.receipt_sha256
            and _aware(self.submission.signature.signed_at)
            <= _aware(self.recorded_at)
        ):
            raise ValueError("Remote Finalization Receipt binding/time 无效。")
        digest = _digest(
            self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        )
        if self.receipt_sha256 != digest or self.receipt_id != (
            f"evstableremotefinalreceipt_{digest[:24]}"
        ):
            raise ValueError("Remote Finalization Receipt identity 不一致。")
        return self


class EvolutionStableRemoteFinalizationView(_StrictModel):
    receipt: EvolutionStableRemoteFinalizationReceipt
    durable_grant_valid: bool
    authorization_sources_current: bool
    credential_current: bool
    installation_signature_valid: bool
    result_binding_valid: bool
    stable_member_completion_fact: bool
    current_control_plane_authority: bool
    remote_active_pointer_current_unverified: Literal[True] = True
    stable_population_completion_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        fact = bool(
            self.durable_grant_valid
            and self.installation_signature_valid
            and self.result_binding_valid
        )
        current = bool(fact and self.authorization_sources_current and self.credential_current)
        if not (
            self.stable_member_completion_fact is fact
            and self.current_control_plane_authority is current
        ):
            raise ValueError("Remote Finalization View 投影不一致。")
        return self


class EvolutionStableRemoteFinalizationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionStableRemoteFinalizationStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def get_package(
        self, grant_id: str
    ) -> EvolutionStableRemoteFinalizationExecutionPackage | None:
        item_id = _grant_id(grant_id)
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT package_json FROM evolution_stable_remote_finalization_grants "
                    "WHERE grant_id = ?",
                    (item_id,),
                )
            ).fetchone()
        return None if row is None else _restore_package(row["package_json"])

    async def get_package_by_authorization(
        self, authorization_id: str
    ) -> EvolutionStableRemoteFinalizationExecutionPackage | None:
        item_id = _authorization_id(authorization_id)
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT package_json FROM evolution_stable_remote_finalization_grants "
                    "WHERE authorization_id = ?",
                    (item_id,),
                )
            ).fetchone()
        return None if row is None else _restore_package(row["package_json"])

    async def create_package(
        self,
        *,
        authorization_id: str,
        workspace_root: Path,
        build: Callable[[], EvolutionStableRemoteFinalizationExecutionPackage],
    ) -> EvolutionStableRemoteFinalizationExecutionPackage:
        item_id = _authorization_id(authorization_id)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            existing = await (
                await db.execute(
                    "SELECT package_json FROM evolution_stable_remote_finalization_grants "
                    "WHERE authorization_id = ?",
                    (item_id,),
                )
            ).fetchone()
            if existing is not None:
                package = _restore_package(existing["package_json"])
                await db.rollback()
                return package
            package = build()
            await _require_exact_authorization_and_consumption(db, package)
            await _require_control_current(db, package, workspace_root)
            encoded = package.model_dump_json()
            _bounded(encoded)
            await db.execute(
                "INSERT INTO evolution_stable_remote_finalization_grants "
                "(grant_id, grant_sha256, authorization_id, consumption_receipt_id, "
                "package_json, issued_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    package.grant.grant_id,
                    package.grant.grant_sha256,
                    item_id,
                    package.consumption.receipt_id,
                    encoded,
                    package.grant.issued_at,
                    package.grant.expires_at,
                ),
            )
            await db.commit()
        return package

    async def get_receipt(
        self, receipt_id: str
    ) -> EvolutionStableRemoteFinalizationReceipt | None:
        item_id = _receipt_id(receipt_id)
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT receipt_json FROM evolution_stable_remote_finalization_receipts "
                    "WHERE receipt_id = ?",
                    (item_id,),
                )
            ).fetchone()
        return None if row is None else _restore_receipt(row["receipt_json"])

    async def get_receipt_by_grant(
        self, grant_id: str
    ) -> EvolutionStableRemoteFinalizationReceipt | None:
        item_id = _grant_id(grant_id)
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT receipt_json FROM evolution_stable_remote_finalization_receipts "
                    "WHERE grant_id = ?",
                    (item_id,),
                )
            ).fetchone()
        return None if row is None else _restore_receipt(row["receipt_json"])

    async def record_receipt(
        self,
        receipt: EvolutionStableRemoteFinalizationReceipt,
        *,
        workspace_root: Path,
    ) -> EvolutionStableRemoteFinalizationReceipt:
        item = EvolutionStableRemoteFinalizationReceipt.model_validate_json(
            receipt.model_dump_json()
        )
        encoded = item.model_dump_json()
        _bounded(encoded)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            await _require_exact_authorization_and_consumption(
                db, item.execution_package
            )
            await _require_control_current(db, item.execution_package, workspace_root)
            row = await (
                await db.execute(
                    "SELECT receipt_json FROM evolution_stable_remote_finalization_receipts "
                    "WHERE grant_id = ?",
                    (item.execution_package.grant.grant_id,),
                )
            ).fetchone()
            if row is not None:
                existing = _restore_receipt(row["receipt_json"])
                await db.rollback()
                if existing.submission.result != item.submission.result:
                    raise EvolutionStableRemoteFinalizationError(
                        "stable_remote_finalization_receipt_conflict",
                        "同一 Execution Grant 已绑定不同 Finalization Result。",
                    )
                return existing
            await db.execute(
                "INSERT INTO evolution_stable_remote_finalization_receipts "
                "(receipt_id, receipt_sha256, grant_id, authorization_id, receipt_json, "
                "recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    item.receipt_id,
                    item.receipt_sha256,
                    item.execution_package.grant.grant_id,
                    item.execution_package.grant.authorization_id,
                    encoded,
                    item.recorded_at,
                ),
            )
            await db.commit()
        return item


class EvolutionStableRemoteFinalizationService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        authorization_service: EvolutionStableRemoteFinalizationAuthorizationService,
        rollout_key_service: ReleaseRolloutControlKeyService,
        trust_policy_path: str | Path,
        store: EvolutionStableRemoteFinalizationStore,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.authorization_service = authorization_service
        self.rollout_key_service = rollout_key_service
        self.trust_policy_path = Path(trust_policy_path).expanduser().resolve()
        self.store = store
        self.clock = clock
        if not (
            authorization_service.workspace_root == self.workspace_root
            and authorization_service.store.db_path == store.db_path
            and authorization_service.rollout_key_service is rollout_key_service
            and authorization_service.trust_policy_path == self.trust_policy_path
        ):
            raise ValueError("Remote Finalization Service authority composition 不一致。")
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def prepare(
        self, *, authorization_id: str
    ) -> EvolutionStableRemoteFinalizationExecutionPackage:
        item_id = _authorization_id(authorization_id)
        lock = self._locks.setdefault(item_id, asyncio.Lock())
        async with lock:
            existing = await self.store.get_package_by_authorization(item_id)
            if existing is not None:
                return await self.current_package(grant_id=existing.grant.grant_id)
            view = await self.authorization_service.inspect(authorization_id=item_id)
            if not view.remote_finalization_authority and not view.consumed:
                raise EvolutionStableRemoteFinalizationError(
                    "stable_remote_finalization_authorization_not_current",
                    "Remote Finalization Authorization 当前不可执行。",
                )
            envelope = view.envelope
            item = envelope.authorization
            consumption = await self.authorization_service.store.consumption(item_id)
            if consumption is None:
                consumption = await self.authorization_service.consume(
                    authorization_id=item_id,
                    nonce_base64=item.start_nonce_base64,
                )
            policy = load_release_rollout_control_trust_policy(self.trust_policy_path)

            def build() -> EvolutionStableRemoteFinalizationExecutionPackage:
                grant = _build_grant(envelope, consumption)
                signature = self.rollout_key_service.sign_remote_finalization_execution_grant(
                    channel="stable", payload=grant.canonical_bytes()
                )
                package = EvolutionStableRemoteFinalizationExecutionPackage(
                    authorization=envelope,
                    consumption=consumption,
                    grant=grant,
                    signature=signature,
                )
                verify_stable_remote_finalization_execution_package(
                    trust_policy=policy,
                    package=package,
                    expected_member_id=item.installation_member_id,
                    now=self.clock(),
                )
                return package

            return await self.store.create_package(
                authorization_id=item_id,
                workspace_root=self.workspace_root,
                build=build,
            )

    async def ingest(
        self,
        *,
        grant_id: str,
        submission_base64: str,
    ) -> EvolutionStableRemoteFinalizationView:
        return await self._ingest(
            grant_id=grant_id,
            submission_base64=submission_base64,
            allow_late_recovery=False,
        )

    async def ingest_late_recovery(
        self,
        *,
        grant_id: str,
        submission_base64: str,
    ) -> EvolutionStableRemoteFinalizationView:
        """Ingest a result for a writer that committed before grant expiry.

        This path never grants writer authority.  It only accepts an installation-signed
        projection of the already durable release finalization, within a bounded recovery
        window and while every original control-plane source is still current.
        """
        return await self._ingest(
            grant_id=grant_id,
            submission_base64=submission_base64,
            allow_late_recovery=True,
        )

    async def _ingest(
        self,
        *,
        grant_id: str,
        submission_base64: str,
        allow_late_recovery: bool,
    ) -> EvolutionStableRemoteFinalizationView:
        item_id = _grant_id(grant_id)
        submission = decode_stable_remote_finalization_submission(submission_base64)
        lock = self._locks.setdefault(item_id, asyncio.Lock())
        async with lock:
            package = await self.store.get_package(item_id)
            if package is None:
                raise EvolutionStableRemoteFinalizationError(
                    "stable_remote_finalization_grant_missing",
                    "Remote Finalization Execution Grant 不存在。",
                )
            existing = await self.store.get_receipt_by_grant(item_id)
            if existing is not None:
                if existing.submission != submission:
                    raise EvolutionStableRemoteFinalizationError(
                        "stable_remote_finalization_receipt_conflict",
                        "同一 Execution Grant 已绑定不同 Submission。",
                    )
                return await self.inspect(receipt_id=existing.receipt_id)
            now = _aware(self.clock())
            expiry = _aware(package.grant.expires_at)
            if now >= expiry and not allow_late_recovery:
                raise EvolutionStableRemoteFinalizationError(
                    "stable_remote_finalization_grant_expired",
                    "Remote Finalization Execution Grant 已过期。",
                )
            auth_view = await self.authorization_service.inspect(
                authorization_id=package.grant.authorization_id
            )
            sources_current = _authorization_sources_current(auth_view)
            if allow_late_recovery:
                sources_current = bool(
                    auth_view.source_current
                    and auth_view.probe_current
                    and auth_view.control_current
                    and auth_view.consumed
                )
                try:
                    verify_stable_remote_finalization_execution_package(
                        trust_policy=load_release_rollout_control_trust_policy(
                            self.trust_policy_path
                        ),
                        package=package,
                        expected_member_id=package.grant.installation_member_id,
                        now=submission.result.completed_at,
                    )
                except (OSError, RuntimeError, TypeError, ValueError):
                    sources_current = False
            if not sources_current:
                raise EvolutionStableRemoteFinalizationError(
                    "stable_remote_finalization_sources_changed",
                    "Control Plane authority source 在结果接收前已变化。",
                )
            probe_service = self.authorization_service.probe_service
            credential = await probe_service.current_credential_for_probe(
                receipt_id=package.authorization.authorization.probe_receipt_id,
            )
            _validate_result_binding(package, submission.result)
            try:
                verify_release_installation_signature(
                    credential=credential,
                    payload=submission.result.canonical_bytes(),
                    artifact=submission.signature,
                    expected_domain=RELEASE_INSTALLATION_FINALIZATION_SIGNATURE_DOMAIN,
                )
            except ReleaseInstallationKeyError as exc:
                raise EvolutionStableRemoteFinalizationError(
                    exc.code,
                    "Remote Finalization installation signature 无效。",
                ) from exc
            completed = _aware(submission.result.completed_at)
            signed = _aware(submission.signature.signed_at)
            ordinary_window = (
                _aware(package.grant.issued_at)
                <= completed
                <= signed
                <= now
                < expiry
            )
            late_window = (
                allow_late_recovery
                and _aware(package.grant.issued_at) <= completed < expiry
                and expiry <= signed <= now
                and (now - expiry).total_seconds() <= _MAX_LATE_RECOVERY_SECONDS
            )
            if not (ordinary_window or late_window):
                raise EvolutionStableRemoteFinalizationError(
                    "stable_remote_finalization_result_outside_window",
                    "Remote Finalization Result 超出 Execution Grant 时间窗口。",
                )
            receipt = _build_receipt(package, submission, now)
            receipt = await self.store.record_receipt(
                receipt, workspace_root=self.workspace_root
            )
        return await self.inspect(receipt_id=receipt.receipt_id)

    async def current_package(
        self,
        *,
        grant_id: str,
    ) -> EvolutionStableRemoteFinalizationExecutionPackage:
        package = await self.store.get_package(grant_id)
        if package is None:
            raise EvolutionStableRemoteFinalizationError(
                "stable_remote_finalization_grant_missing",
                "Remote Finalization Execution Grant 不存在。",
            )
        auth_view = await self.authorization_service.inspect(
            authorization_id=package.grant.authorization_id
        )
        if not _authorization_sources_current(auth_view):
            raise EvolutionStableRemoteFinalizationError(
                "stable_remote_finalization_grant_not_current",
                "Remote Finalization Execution Grant 当前不可导出。",
            )
        policy = load_release_rollout_control_trust_policy(self.trust_policy_path)
        verify_stable_remote_finalization_execution_package(
            trust_policy=policy,
            package=package,
            expected_member_id=package.grant.installation_member_id,
            now=self.clock(),
        )
        return package

    async def inspect(
        self, *, receipt_id: str
    ) -> EvolutionStableRemoteFinalizationView:
        receipt = await self.store.get_receipt(receipt_id)
        if receipt is None:
            raise EvolutionStableRemoteFinalizationError(
                "stable_remote_finalization_receipt_missing",
                "Remote Finalization Receipt 不存在。",
            )
        durable = sources = credential_ok = signature_ok = binding_ok = False
        try:
            package = await self.store.get_package(receipt.execution_package.grant.grant_id)
            durable = package == receipt.execution_package
            auth_view = await self.authorization_service.inspect(
                authorization_id=receipt.execution_package.grant.authorization_id
            )
            sources = _authorization_sources_current(auth_view)
            probe_service = self.authorization_service.probe_service
            credential = await probe_service.current_credential_for_probe(
                receipt_id=(
                    receipt.execution_package.authorization.authorization.probe_receipt_id
                ),
            )
            credential_ok = True
            verify_release_installation_signature(
                credential=credential,
                payload=receipt.submission.result.canonical_bytes(),
                artifact=receipt.submission.signature,
                expected_domain=RELEASE_INSTALLATION_FINALIZATION_SIGNATURE_DOMAIN,
            )
            signature_ok = True
            _validate_result_binding(receipt.execution_package, receipt.submission.result)
            binding_ok = True
        except (OSError, RuntimeError, TypeError, ValueError):
            pass
        fact = durable and signature_ok and binding_ok
        return EvolutionStableRemoteFinalizationView(
            receipt=receipt,
            durable_grant_valid=durable,
            authorization_sources_current=sources,
            credential_current=credential_ok,
            installation_signature_valid=signature_ok,
            result_binding_valid=binding_ok,
            stable_member_completion_fact=fact,
            current_control_plane_authority=bool(fact and sources and credential_ok),
        )


def verify_stable_remote_finalization_execution_package(
    *,
    trust_policy: ReleaseRolloutControlTrustPolicyDocument,
    package: EvolutionStableRemoteFinalizationExecutionPackage,
    expected_member_id: str,
    now: str | datetime,
) -> EvolutionStableRemoteFinalizationExecutionGrant:
    timestamp = _aware(now)
    auth = verify_stable_remote_finalization_authorization(
        trust_policy=trust_policy,
        envelope=package.authorization,
        expected_member_id=expected_member_id,
        now=timestamp,
    )
    try:
        verify_release_rollout_control_signature(
            trust_policy=trust_policy,
            channel="stable",
            payload=package.grant.canonical_bytes(),
            artifact=package.signature,
            expected_domain=RELEASE_ROLLOUT_CONTROL_EXECUTION_GRANT_SIGNATURE_DOMAIN,
        )
    except ReleaseRolloutControlKeyError as exc:
        raise EvolutionStableRemoteFinalizationError(
            exc.code, "Remote Finalization Execution Grant signature 无效。"
        ) from exc
    if not (
        package.grant.installation_member_id == expected_member_id
        and package.consumption.single_use_consumed
        and package.consumption.authorization_id == auth.authorization_id
        and _aware(package.grant.issued_at)
        <= _aware(package.signature.signed_at)
        <= timestamp
        < _aware(package.grant.expires_at)
    ):
        raise EvolutionStableRemoteFinalizationError(
            "stable_remote_finalization_grant_not_current",
            "Remote Finalization Execution Grant 不属于本机或已过期。",
        )
    return package.grant


def execute_stable_remote_finalization(
    *,
    package: EvolutionStableRemoteFinalizationExecutionPackage,
    trust_policy: ReleaseRolloutControlTrustPolicyDocument,
    credential: ReleaseManagedInstallationCredential,
    release_slot_store: ReleaseSlotStore,
    installation_key_service: ReleaseInstallationKeyService,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> EvolutionStableRemoteFinalizationSubmission:
    if release_slot_store.release_root != installation_key_service.release_root:
        raise EvolutionStableRemoteFinalizationError(
            "stable_remote_finalization_release_root_mismatch",
            "Release Store 与 installation key 不属于同一 install root。",
        )
    now = _aware(clock())
    grant = verify_stable_remote_finalization_execution_package(
        trust_policy=trust_policy,
        package=package,
        expected_member_id=credential.payload.member_id,
        now=now,
    )
    auth = package.authorization.authorization
    handle = installation_key_service.inspect()
    if not (
        credential.credential_id == grant.installation_credential_id
        and credential.credential_sha256 == grant.installation_credential_sha256
        and credential.payload.installation_public_key_sha256
        == grant.installation_public_key_sha256
        and handle.public_key_sha256 == grant.installation_public_key_sha256
        and handle.release_root_sha256 == grant.release_root_sha256
    ):
        raise EvolutionStableRemoteFinalizationError(
            "stable_remote_finalization_credential_mismatch",
            "本机 Population Credential、installation key 或 release root 不匹配。",
        )
    authority = ReleaseStableMemberFinalizationAuthority(
        kind="evolution_stable_remote_finalization_authorization",
        authority_id=auth.authorization_id,
        authority_sha256=auth.authorization_sha256,
        completion_receipt_id=auth.completion_receipt_id,
        completion_receipt_sha256=auth.completion_receipt_sha256,
        installation_member_id=auth.installation_member_id,
        expected_pointer_id=auth.expected_active_pointer_id,
        expected_pointer_sha256=auth.expected_active_pointer_sha256,
        expected_pointer_generation=auth.expected_active_pointer_generation,
    )
    try:
        finalization = release_slot_store.get_stable_member_finalization(
            auth.authorization_id
        )
    except (ReleaseSlotError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise EvolutionStableRemoteFinalizationError(
            getattr(exc, "code", "stable_remote_finalization_recovery_failed"),
            "目标 Release Store finalization recovery 失败。",
        ) from exc
    if finalization is not None and finalization.authority != authority:
        raise EvolutionStableRemoteFinalizationError(
            "stable_remote_finalization_recovery_conflict",
            "目标 Release Store 已绑定不同 finalization authority。",
        )
    if finalization is None:
        finalization = _execute_target_writer(
            auth=auth,
            authority=authority,
            release_slot_store=release_slot_store,
            finalized_at=now,
        )
    result = _build_result(package, finalization, handle.release_root_sha256)
    try:
        signature = installation_key_service.sign_remote_finalization_result(
            credential=credential, payload=result.canonical_bytes()
        )
    except ReleaseInstallationKeyError as exc:
        raise EvolutionStableRemoteFinalizationError(
            exc.code, "目标 installation key 无法签署 Finalization Result。"
        ) from exc
    if not _aware(signature.signed_at) < _aware(grant.expires_at):
        raise EvolutionStableRemoteFinalizationError(
            "stable_remote_finalization_signature_outside_window",
            "Finalization Result 签名超出 Execution Grant 窗口。",
        )
    return EvolutionStableRemoteFinalizationSubmission(
        result=result, signature=signature
    )


def recover_stable_remote_finalization_submission(
    *,
    package: EvolutionStableRemoteFinalizationExecutionPackage,
    trust_policy: ReleaseRolloutControlTrustPolicyDocument,
    credential: ReleaseManagedInstallationCredential,
    release_slot_store: ReleaseSlotStore,
    installation_key_service: ReleaseInstallationKeyService,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> EvolutionStableRemoteFinalizationSubmission:
    """Sign only an existing writer result; never execute or reopen writer authority."""
    if release_slot_store.release_root != installation_key_service.release_root:
        raise EvolutionStableRemoteFinalizationError(
            "stable_remote_finalization_release_root_mismatch",
            "Release Store 与 installation key 不属于同一 install root。",
        )
    auth = package.authorization.authorization
    try:
        finalization = release_slot_store.get_stable_member_finalization(
            auth.authorization_id
        )
    except (ReleaseSlotError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise EvolutionStableRemoteFinalizationError(
            getattr(exc, "code", "stable_remote_finalization_recovery_failed"),
            "目标 Release Store finalization recovery 失败。",
        ) from exc
    if finalization is None:
        raise EvolutionStableRemoteFinalizationError(
            "stable_remote_finalization_recovery_fact_missing",
            "目标 Release Store 不存在可恢复的 durable finalization；禁止补执行 writer。",
        )
    completed = _aware(finalization.finalized_at)
    if not (_aware(package.grant.issued_at) <= completed < _aware(package.grant.expires_at)):
        raise EvolutionStableRemoteFinalizationError(
            "stable_remote_finalization_recovery_fact_outside_window",
            "既有 durable finalization 不在原 Execution Grant 窗口内。",
        )
    verify_stable_remote_finalization_execution_package(
        trust_policy=trust_policy,
        package=package,
        expected_member_id=credential.payload.member_id,
        now=completed,
    )
    handle = installation_key_service.inspect()
    if not (
        credential.credential_id == package.grant.installation_credential_id
        and credential.credential_sha256
        == package.grant.installation_credential_sha256
        and credential.payload.installation_public_key_sha256
        == package.grant.installation_public_key_sha256
        and handle.public_key_sha256
        == package.grant.installation_public_key_sha256
        and handle.release_root_sha256 == package.grant.release_root_sha256
        and finalization.authority.authority_id == auth.authorization_id
        and finalization.authority.authority_sha256 == auth.authorization_sha256
    ):
        raise EvolutionStableRemoteFinalizationError(
            "stable_remote_finalization_recovery_binding_mismatch",
            "既有 finalization、Population Credential 或 installation key 绑定不一致。",
        )
    now = _aware(clock())
    if now < completed:
        raise EvolutionStableRemoteFinalizationError(
            "stable_remote_finalization_recovery_clock_invalid",
            "补签时间不得早于 durable finalization。",
        )
    result = _build_result(package, finalization, handle.release_root_sha256)
    try:
        signature = installation_key_service.sign_remote_finalization_result(
            credential=credential, payload=result.canonical_bytes()
        )
    except ReleaseInstallationKeyError as exc:
        raise EvolutionStableRemoteFinalizationError(
            exc.code, "目标 installation key 无法补签既有 Finalization Result。"
        ) from exc
    return EvolutionStableRemoteFinalizationSubmission(result=result, signature=signature)


def _execute_target_writer(
    *,
    auth,
    authority: ReleaseStableMemberFinalizationAuthority,
    release_slot_store: ReleaseSlotStore,
    finalized_at: datetime,
) -> ReleaseStableMemberFinalization:
    try:
        active = release_slot_store.active()
        prior = (
            None
            if active is None
            else release_slot_store.get_activation_event(active.generation - 1)
        )
        if active is None or prior is None:
            raise ReleaseSlotError(
                "release_active_pointer_missing", "本机缺少 active/previous pointer。"
            )
        candidate = release_slot_store.resolve_booted_slot(
            active.current_slot_id, active.boot_receipt_id
        )
        rollback = release_slot_store.resolve_booted_slot(
            prior.current_slot_id, prior.boot_receipt_id
        )
        active_after = release_slot_store.active()
    except (ReleaseSlotError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise EvolutionStableRemoteFinalizationError(
            getattr(exc, "code", "stable_remote_finalization_release_store_failed"),
            "目标 Release Store 机械重验失败。",
        ) from exc
    expected = (
        (active.pointer_id, auth.expected_active_pointer_id),
        (active.pointer_sha256, auth.expected_active_pointer_sha256),
        (active.generation, auth.expected_active_pointer_generation),
        (candidate.slot.slot_id, auth.expected_candidate_slot_id),
        (candidate.slot.slot_sha256, auth.expected_candidate_slot_sha256),
        (candidate.slot.manifest_sha256, auth.expected_candidate_manifest_sha256),
        (candidate.boot_receipt.receipt_id, auth.expected_candidate_boot_receipt_id),
        (
            candidate.boot_receipt.receipt_sha256,
            auth.expected_candidate_boot_receipt_sha256,
        ),
        (prior.pointer_id, auth.expected_rollback_pointer_id),
        (prior.pointer_sha256, auth.expected_rollback_pointer_sha256),
        (prior.generation, auth.expected_rollback_pointer_generation),
        (rollback.slot.slot_id, auth.expected_rollback_slot_id),
        (rollback.slot.slot_sha256, auth.expected_rollback_slot_sha256),
        (rollback.boot_receipt.receipt_id, auth.expected_rollback_boot_receipt_id),
        (
            rollback.boot_receipt.receipt_sha256,
            auth.expected_rollback_boot_receipt_sha256,
        ),
        (active.previous_pointer_sha256, prior.pointer_sha256),
        (active.previous_slot_id, rollback.slot.slot_id),
        (active.previous_slot_sha256, rollback.slot.slot_sha256),
        (active_after, active),
    )
    if any(actual != wanted for actual, wanted in expected):
        raise EvolutionStableRemoteFinalizationError(
            "stable_remote_finalization_release_store_mismatch",
            "目标 Release Store 与 Execution Grant 期望不一致。",
        )
    try:
        return release_slot_store.finalize_stable_member(
            authority, finalized_at=finalized_at.isoformat()
        )
    except (ReleaseSlotError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise EvolutionStableRemoteFinalizationError(
            getattr(exc, "code", "stable_remote_finalization_writer_failed"),
            "目标 Release Store stable-member CAS 失败。",
        ) from exc


def encode_stable_remote_finalization_execution_package(
    package: EvolutionStableRemoteFinalizationExecutionPackage,
) -> str:
    return base64.b64encode(
        _canonical_bytes(package.model_dump(mode="json"))
    ).decode("ascii")


def decode_stable_remote_finalization_execution_package(
    value: str,
) -> EvolutionStableRemoteFinalizationExecutionPackage:
    return _decode_model(
        value,
        EvolutionStableRemoteFinalizationExecutionPackage.model_validate_json,
        "stable_remote_finalization_package_invalid",
        "Remote Finalization Execution Package 编码或结构无效。",
    )


def encode_stable_remote_finalization_submission(
    submission: EvolutionStableRemoteFinalizationSubmission,
) -> str:
    return base64.b64encode(
        _canonical_bytes(submission.model_dump(mode="json"))
    ).decode("ascii")


def decode_stable_remote_finalization_submission(
    value: str,
) -> EvolutionStableRemoteFinalizationSubmission:
    return _decode_model(
        value,
        EvolutionStableRemoteFinalizationSubmission.model_validate_json,
        "stable_remote_finalization_submission_invalid",
        "Remote Finalization Submission 编码或结构无效。",
    )


def render_stable_remote_finalization(
    value: EvolutionStableRemoteFinalizationExecutionPackage
    | EvolutionStableRemoteFinalizationView,
    *,
    include_package: bool = False,
) -> str:
    if isinstance(value, EvolutionStableRemoteFinalizationExecutionPackage):
        lines = [
            "## Remote Stable Member Finalization Execution Grant",
            "",
            "- 状态：**已消费并待目标执行**",
            f"- Grant：`{value.grant.grant_id}`",
            f"- Authorization：`{value.grant.authorization_id}`",
            f"- Installation：`{value.grant.installation_member_id}`",
            f"- Expires：`{value.grant.expires_at}`",
            "- Scope：`binary_only`",
        ]
        if include_package:
            lines.extend((
                "",
                "### Execution Package Base64",
                "",
                encode_stable_remote_finalization_execution_package(value),
            ))
        return "\n".join(lines)
    receipt = value.receipt
    status = "当前控制面有效" if value.current_control_plane_authority else "历史完成"
    return "\n".join((
        "## Remote Stable Member Finalization",
        "",
        f"- 状态：**{status}**",
        f"- Receipt：`{receipt.receipt_id}`",
        f"- Grant：`{receipt.execution_package.grant.grant_id}`",
        f"- Result：`{receipt.submission.result.result_id}`",
        "- Release finalization：`"
        f"{receipt.submission.result.release_finalization.finalization_id}`",
        f"- Installation signature：`{receipt.submission.signature.signature_id}`",
        f"- Completion fact：`{str(value.stable_member_completion_fact).lower()}`",
        "- Remote active pointer current：`unverified; 下一次 Probe 重验`",
        "- Population completion authority：`false`",
        "- Promotion authority：`false`",
    ))


def render_stable_remote_finalization_submission(
    submission: EvolutionStableRemoteFinalizationSubmission,
) -> str:
    return "\n".join((
        "## Remote Stable Member Finalization Submission",
        "",
        "- 状态：**目标执行完成，等待 Control Plane ingest**",
        f"- Grant：`{submission.result.grant_id}`",
        f"- Result：`{submission.result.result_id}`",
        f"- Release finalization：`{submission.result.release_finalization.finalization_id}`",
        f"- Installation signature：`{submission.signature.signature_id}`",
        "",
        "### Finalization Submission Base64",
        "",
        encode_stable_remote_finalization_submission(submission),
    ))


def _build_grant(envelope, consumption):
    auth = envelope.authorization
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_STABLE_REMOTE_FINALIZATION_POLICY,
        "authorization_id": auth.authorization_id,
        "authorization_sha256": auth.authorization_sha256,
        "authorization_signature_id": envelope.signature.signature_id,
        "consumption_receipt_id": consumption.receipt_id,
        "consumption_receipt_sha256": consumption.receipt_sha256,
        "installation_member_id": auth.installation_member_id,
        "installation_credential_id": auth.installation_credential_id,
        "installation_credential_sha256": auth.installation_credential_sha256,
        "installation_public_key_sha256": auth.installation_public_key_sha256,
        "release_root_sha256": auth.release_root_sha256,
        "expected_pointer_id": auth.expected_active_pointer_id,
        "expected_pointer_sha256": auth.expected_active_pointer_sha256,
        "expected_pointer_generation": auth.expected_active_pointer_generation,
        "issued_at": consumption.consumed_at,
        "expires_at": auth.expires_at,
        "operation": "finalize_stable_population_member",
        "operation_scope": "binary_only",
        "authorization_consumed": True,
        "single_execution_required": True,
        "expected_pointer_cas_required": True,
        "config_data_mutation_allowed": False,
        "deployment_authority": False,
        "rollback_authority": False,
        "promotion_authority": False,
    }
    digest = _digest(core)
    return EvolutionStableRemoteFinalizationExecutionGrant.model_validate({
        **core,
        "grant_id": f"evstableremotefinalgrant_{digest[:24]}",
        "grant_sha256": digest,
    })


def _build_result(package, finalization, release_root_sha256):
    grant = package.grant
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_STABLE_REMOTE_FINALIZATION_POLICY,
        "grant_id": grant.grant_id,
        "grant_sha256": grant.grant_sha256,
        "authorization_id": grant.authorization_id,
        "authorization_sha256": grant.authorization_sha256,
        "consumption_receipt_id": grant.consumption_receipt_id,
        "consumption_receipt_sha256": grant.consumption_receipt_sha256,
        "installation_member_id": grant.installation_member_id,
        "installation_credential_id": grant.installation_credential_id,
        "release_root_sha256": release_root_sha256,
        "release_finalization": finalization.model_dump(mode="json"),
        "completed_at": finalization.finalized_at,
        "target_sources_reread": True,
        "authorization_signature_verified": True,
        "execution_grant_signature_verified": True,
        "expected_pointer_cas_satisfied": True,
        "binary_stable_member_finalized": True,
        "config_data_mutation_executed": False,
        "deployment_executed": False,
        "rollback_executed": False,
        "promotion_executed": False,
    }
    digest = _digest(core)
    return EvolutionStableRemoteFinalizationResult.model_validate({
        **core,
        "result_id": f"evstableremotefinalresult_{digest[:24]}",
        "result_sha256": digest,
    })


def _build_receipt(package, submission, recorded_at):
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_STABLE_REMOTE_FINALIZATION_POLICY,
        "execution_package": package.model_dump(mode="json"),
        "submission": submission.model_dump(mode="json"),
        "recorded_at": recorded_at.isoformat(),
        "control_plane_sources_revalidated": True,
        "installation_signature_verified": True,
        "durable_single_use_observed": True,
        "stable_member_completion_fact": True,
        "stable_population_completion_authority": False,
        "promotion_authority": False,
    }
    digest = _digest(core)
    return EvolutionStableRemoteFinalizationReceipt.model_validate({
        **core,
        "receipt_id": f"evstableremotefinalreceipt_{digest[:24]}",
        "receipt_sha256": digest,
    })


def _validate_result_binding(package, result) -> None:
    grant = package.grant
    final = result.release_finalization
    expected = (
        (result.grant_id, grant.grant_id),
        (result.grant_sha256, grant.grant_sha256),
        (result.authorization_id, grant.authorization_id),
        (result.authorization_sha256, grant.authorization_sha256),
        (result.consumption_receipt_id, grant.consumption_receipt_id),
        (result.consumption_receipt_sha256, grant.consumption_receipt_sha256),
        (result.installation_member_id, grant.installation_member_id),
        (result.installation_credential_id, grant.installation_credential_id),
        (result.release_root_sha256, grant.release_root_sha256),
        (final.authority.expected_pointer_id, grant.expected_pointer_id),
        (final.authority.expected_pointer_sha256, grant.expected_pointer_sha256),
        (final.authority.expected_pointer_generation, grant.expected_pointer_generation),
        (final.active_pointer.pointer_id, grant.expected_pointer_id),
        (final.active_pointer.pointer_sha256, grant.expected_pointer_sha256),
        (final.active_pointer.generation, grant.expected_pointer_generation),
    )
    if any(actual != wanted for actual, wanted in expected):
        raise EvolutionStableRemoteFinalizationError(
            "stable_remote_finalization_result_mismatch",
            "Remote Finalization Result 与 Execution Grant 不一致。",
        )


def _authorization_sources_current(view) -> bool:
    return bool(
        view.source_current
        and view.probe_current
        and view.control_current
        and view.trust_policy_current
        and not view.expired
        and view.consumed
    )


async def _require_exact_authorization_and_consumption(db, package) -> None:
    auth_row = await (
        await db.execute(
            "SELECT envelope_json FROM evolution_stable_remote_finalization_authorizations "
            "WHERE authorization_id = ?",
            (package.grant.authorization_id,),
        )
    ).fetchone()
    consume_row = await (
        await db.execute(
            "SELECT receipt_json FROM evolution_stable_remote_finalization_consumptions "
            "WHERE authorization_id = ?",
            (package.grant.authorization_id,),
        )
    ).fetchone()
    if not (
        auth_row is not None
        and consume_row is not None
        and EvolutionStableRemoteFinalizationAuthorizationEnvelope.model_validate_json(
            auth_row["envelope_json"]
        )
        == package.authorization
        and EvolutionStableRemoteFinalizationConsumptionReceipt.model_validate_json(
            consume_row["receipt_json"]
        )
        == package.consumption
    ):
        raise EvolutionStableRemoteFinalizationError(
            "stable_remote_finalization_durable_source_changed",
            "Remote Finalization Authorization 或 Consumption durable source 已变化。",
        )


async def _require_control_current(db, package, workspace_root: Path) -> None:
    row = await (
        await db.execute(
            "SELECT event_json FROM evolution_revalidation_rollout_control_events "
            "WHERE workspace_root = ? ORDER BY sequence DESC LIMIT 1",
            (str(workspace_root),),
        )
    ).fetchone()
    event = (
        None
        if row is None
        else EvolutionRevalidationRolloutControlEvent.model_validate_json(
            row["event_json"]
        )
    )
    auth = package.authorization.authorization
    identity = (0, "", "") if event is None else (
        event.sequence,
        event.event_id,
        event.event_sha256,
    )
    if identity != (
        auth.control_sequence,
        auth.control_event_id,
        auth.control_event_sha256,
    ) or (
        event is not None
        and event.state is not EvolutionRevalidationRolloutControlState.ACTIVE
    ):
        raise EvolutionStableRemoteFinalizationError(
            "stable_remote_finalization_control_changed",
            "Rollout kill switch generation 在结果持久化前已变化。",
        )


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_stable_remote_finalization_grants ("
        "grant_id TEXT PRIMARY KEY, grant_sha256 TEXT NOT NULL UNIQUE, "
        "authorization_id TEXT NOT NULL UNIQUE, consumption_receipt_id TEXT NOT NULL UNIQUE, "
        "package_json TEXT NOT NULL, issued_at TEXT NOT NULL, expires_at TEXT NOT NULL)"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_stable_remote_finalization_receipts ("
        "receipt_id TEXT PRIMARY KEY, receipt_sha256 TEXT NOT NULL UNIQUE, "
        "grant_id TEXT NOT NULL UNIQUE, authorization_id TEXT NOT NULL UNIQUE, "
        "receipt_json TEXT NOT NULL, recorded_at TEXT NOT NULL)"
    )


def _decode_model(value, validator, code, message):
    if not isinstance(value, str) or not value or len(value) > _MAX_ENCODED_CHARS:
        raise EvolutionStableRemoteFinalizationError(code, message)
    try:
        raw = base64.b64decode(value, validate=True)
        if len(raw) > _MAX_ARTIFACT_BYTES or base64.b64encode(raw).decode() != value:
            raise ValueError("bounds")
        return validator(raw)
    except (TypeError, ValueError) as exc:
        raise EvolutionStableRemoteFinalizationError(code, message) from exc


def _restore_package(value: str):
    try:
        return EvolutionStableRemoteFinalizationExecutionPackage.model_validate_json(value)
    except (TypeError, ValueError) as exc:
        raise EvolutionStableRemoteFinalizationError(
            "stable_remote_finalization_store_corrupt",
            "Remote Finalization durable package 已损坏。",
        ) from exc


def _restore_receipt(value: str):
    try:
        return EvolutionStableRemoteFinalizationReceipt.model_validate_json(value)
    except (TypeError, ValueError) as exc:
        raise EvolutionStableRemoteFinalizationError(
            "stable_remote_finalization_store_corrupt",
            "Remote Finalization durable receipt 已损坏。",
        ) from exc


def _bounded(value: str) -> None:
    if len(value.encode()) > _MAX_ARTIFACT_BYTES:
        raise EvolutionStableRemoteFinalizationError(
            "stable_remote_finalization_artifact_oversized",
            "Remote Finalization artifact 超过 512 KiB。",
        )


def _authorization_id(value: str) -> str:
    item = str(value).strip()
    if _AUTH_RE.fullmatch(item) is None:
        raise ValueError("authorization_id 格式无效。")
    return item


def _grant_id(value: str) -> str:
    item = str(value).strip()
    if _GRANT_RE.fullmatch(item) is None:
        raise ValueError("grant_id 格式无效。")
    return item


def _receipt_id(value: str) -> str:
    item = str(value).strip()
    if _RECEIPT_RE.fullmatch(item) is None:
        raise ValueError("receipt_id 格式无效。")
    return item


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
    "EVOLUTION_STABLE_REMOTE_FINALIZATION_POLICY",
    "EvolutionStableRemoteFinalizationError",
    "EvolutionStableRemoteFinalizationExecutionGrant",
    "EvolutionStableRemoteFinalizationExecutionPackage",
    "EvolutionStableRemoteFinalizationReceipt",
    "EvolutionStableRemoteFinalizationResult",
    "EvolutionStableRemoteFinalizationService",
    "EvolutionStableRemoteFinalizationStore",
    "EvolutionStableRemoteFinalizationSubmission",
    "EvolutionStableRemoteFinalizationView",
    "decode_stable_remote_finalization_execution_package",
    "decode_stable_remote_finalization_submission",
    "encode_stable_remote_finalization_execution_package",
    "encode_stable_remote_finalization_submission",
    "execute_stable_remote_finalization",
    "recover_stable_remote_finalization_submission",
    "render_stable_remote_finalization",
    "render_stable_remote_finalization_submission",
    "verify_stable_remote_finalization_execution_package",
]
