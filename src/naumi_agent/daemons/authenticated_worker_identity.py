"""Supervisor-attested Ed25519 identity for exact Worker incarnations."""

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
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.config.credentials import resolve_runtime_payload_key
from naumi_agent.daemons.worker_contract import WorkerContract, verify_worker_contract
from naumi_agent.daemons.worker_registry import (
    WorkerRegistrationState,
    WorkerRegistryStore,
    WorkerRegistryStoreError,
)

AUTHENTICATED_WORKER_IDENTITY_POLICY = "naumi-authenticated-worker-identity-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_B64_32_RE = r"^[A-Za-z0-9+/]{43}=$"
_MAX_ARTIFACT_BYTES = 64 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class AuthenticatedWorkerIdentity(_StrictModel):
    """Public identity only; the Worker private key never crosses this boundary."""

    schema_version: Literal[1] = 1
    policy_version: Literal["naumi-authenticated-worker-identity-v1"] = (
        AUTHENTICATED_WORKER_IDENTITY_POLICY
    )
    identity_id: str = Field(pattern=r"^workeridentity_[0-9a-f]{24}$")
    identity_sha256: str = Field(pattern=_SHA256_RE)
    worker_id: str = Field(min_length=1, max_length=128)
    worker_instance_id: str = Field(min_length=1, max_length=128)
    worker_epoch: int = Field(ge=1)
    worker_contract_sha256: str = Field(pattern=_SHA256_RE)
    signature_algorithm: Literal["ed25519"] = "ed25519"
    public_key_base64: str = Field(pattern=_B64_32_RE)
    public_key_sha256: str = Field(pattern=_SHA256_RE)
    enrolled_at: str = Field(min_length=1, max_length=100)
    attestation_algorithm: Literal["hmac-sha256"] = "hmac-sha256"
    supervisor_attestation_sha256: str = Field(pattern=_SHA256_RE)
    private_key_stored: Literal[False] = False
    claim_signature_eligible: Literal[True] = True
    execution_authority: Literal[False] = False
    result_authority: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        raw = _decode_canonical_base64(
            self.public_key_base64,
            expected_bytes=32,
            field="Worker public key",
        )
        if not hmac.compare_digest(
            self.public_key_sha256,
            hashlib.sha256(raw).hexdigest(),
        ):
            raise ValueError("Worker Identity public key 摘要不一致。")
        _aware(self.enrolled_at)
        core = self.model_dump(
            mode="json",
            exclude={
                "identity_id",
                "identity_sha256",
                "supervisor_attestation_sha256",
            },
        )
        digest = _digest(core)
        if not (
            hmac.compare_digest(self.identity_sha256, digest)
            and self.identity_id == f"workeridentity_{digest[:24]}"
        ):
            raise ValueError("Worker Identity identity/digest 不一致。")
        if any(
            (
                self.private_key_stored,
                self.execution_authority,
                self.result_authority,
                self.learning_authority,
                self.promotion_authority,
            )
        ):
            raise ValueError("Worker Identity 不得保存私钥或扩大执行权威。")
        return self


class AuthenticatedWorkerIdentityView(_StrictModel):
    identity: AuthenticatedWorkerIdentity
    durable_source_valid: bool
    supervisor_attestation_valid: bool
    active_incarnation_valid: bool
    identity_authority: bool
    execution_authority: Literal[False] = False
    result_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        current = bool(
            self.durable_source_valid
            and self.supervisor_attestation_valid
            and self.active_incarnation_valid
        )
        if self.identity_authority is not current:
            raise ValueError("Worker Identity authority 投影不一致。")
        return self


class AuthenticatedWorkerIdentityError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def issue_authenticated_worker_identity(
    *,
    contract: WorkerContract,
    public_key_base64: str,
    enrolled_at: str,
    supervisor_key: bytes,
) -> AuthenticatedWorkerIdentity:
    """Seal one public key to an exact contract using control-plane key material."""
    if not isinstance(contract, WorkerContract):
        raise TypeError("contract 必须是 WorkerContract。")
    if not verify_worker_contract(contract):
        raise ValueError("Worker Contract 摘要校验失败。")
    key = _supervisor_key(supervisor_key)
    raw_public_key = _decode_canonical_base64(
        public_key_base64,
        expected_bytes=32,
        field="Worker public key",
    )
    normalized_public_key = base64.b64encode(raw_public_key).decode("ascii")
    core = {
        "schema_version": 1,
        "policy_version": AUTHENTICATED_WORKER_IDENTITY_POLICY,
        "worker_id": contract.worker_id,
        "worker_instance_id": contract.instance_id,
        "worker_epoch": contract.epoch,
        "worker_contract_sha256": contract.contract_sha256,
        "signature_algorithm": "ed25519",
        "public_key_base64": normalized_public_key,
        "public_key_sha256": hashlib.sha256(raw_public_key).hexdigest(),
        "enrolled_at": _aware(enrolled_at).isoformat(),
        "attestation_algorithm": "hmac-sha256",
        "private_key_stored": False,
        "claim_signature_eligible": True,
        "execution_authority": False,
        "result_authority": False,
        "learning_authority": False,
        "promotion_authority": False,
    }
    digest = _digest(core)
    attestation = hmac.new(key, _canonical(core), hashlib.sha256).hexdigest()
    return AuthenticatedWorkerIdentity.model_validate(
        {
            **core,
            "identity_id": f"workeridentity_{digest[:24]}",
            "identity_sha256": digest,
            "supervisor_attestation_sha256": attestation,
        }
    )


def verify_authenticated_worker_identity(
    identity: AuthenticatedWorkerIdentity,
    *,
    supervisor_key: bytes,
) -> bool:
    if not isinstance(identity, AuthenticatedWorkerIdentity):
        return False
    try:
        key = _supervisor_key(supervisor_key)
        restored = AuthenticatedWorkerIdentity.model_validate_json(
            identity.model_dump_json()
        )
    except (TypeError, ValueError):
        return False
    core = restored.model_dump(
        mode="json",
        exclude={
            "identity_id",
            "identity_sha256",
            "supervisor_attestation_sha256",
        },
    )
    expected = hmac.new(key, _canonical(core), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, restored.supervisor_attestation_sha256)


class AuthenticatedWorkerIdentityStore:
    def __init__(self, db_path: str | Path) -> None:
        unresolved = Path(db_path).expanduser()
        if not unresolved.is_absolute():
            raise ValueError("Worker Identity store 路径必须是绝对路径。")
        self.db_path = unresolved.resolve(strict=False)

    async def record(
        self,
        identity: AuthenticatedWorkerIdentity,
    ) -> AuthenticatedWorkerIdentity:
        try:
            item = AuthenticatedWorkerIdentity.model_validate_json(
                identity.model_dump_json()
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise AuthenticatedWorkerIdentityError(
                "authenticated_worker_identity_invalid",
                "Worker Identity artifact 无效。",
            ) from exc
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
            raise AuthenticatedWorkerIdentityError(
                "authenticated_worker_identity_oversized",
                "Worker Identity artifact 超过 64 KiB。",
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                existing = await (
                    await db.execute(
                        "SELECT identity_json FROM authenticated_worker_identities "
                        "WHERE worker_id = ? AND worker_instance_id = ? AND worker_epoch = ?",
                        (item.worker_id, item.worker_instance_id, item.worker_epoch),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _restore(existing["identity_json"])
                    await db.rollback()
                    if restored != item:
                        raise AuthenticatedWorkerIdentityError(
                            "authenticated_worker_identity_conflict",
                            "同一 Worker incarnation 已绑定其他认证密钥；换钥必须提升 epoch。",
                        )
                    return restored
                await db.execute(
                    "INSERT INTO authenticated_worker_identities "
                    "(identity_id, identity_sha256, worker_id, worker_instance_id, "
                    "worker_epoch, worker_contract_sha256, public_key_sha256, "
                    "identity_json, enrolled_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.identity_id,
                        item.identity_sha256,
                        item.worker_id,
                        item.worker_instance_id,
                        item.worker_epoch,
                        item.worker_contract_sha256,
                        item.public_key_sha256,
                        encoded,
                        item.enrolled_at,
                    ),
                )
                await db.commit()
        except AuthenticatedWorkerIdentityError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise AuthenticatedWorkerIdentityError(
                "authenticated_worker_identity_store_error",
                "Worker Identity 无法持久化。",
            ) from exc
        return item

    async def get(self, identity_id: str) -> AuthenticatedWorkerIdentity | None:
        identity = _identity_id(identity_id)
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT identity_json FROM authenticated_worker_identities "
                        "WHERE identity_id = ?",
                        (identity,),
                    )
                ).fetchone()
            return None if row is None else _restore(row["identity_json"])
        except AuthenticatedWorkerIdentityError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise AuthenticatedWorkerIdentityError(
                "authenticated_worker_identity_store_corrupt",
                "Worker Identity 损坏或无法读取。",
            ) from exc

    async def get_for_contract(
        self,
        contract: WorkerContract,
    ) -> AuthenticatedWorkerIdentity | None:
        if not isinstance(contract, WorkerContract):
            raise TypeError("contract 必须是 WorkerContract。")
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT identity_json FROM authenticated_worker_identities "
                        "WHERE worker_id = ? AND worker_instance_id = ? AND worker_epoch = ? "
                        "AND worker_contract_sha256 = ?",
                        (
                            contract.worker_id,
                            contract.instance_id,
                            contract.epoch,
                            contract.contract_sha256,
                        ),
                    )
                ).fetchone()
            return None if row is None else _restore(row["identity_json"])
        except AuthenticatedWorkerIdentityError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise AuthenticatedWorkerIdentityError(
                "authenticated_worker_identity_store_corrupt",
                "Worker Identity 损坏或无法读取。",
            ) from exc


class AuthenticatedWorkerIdentityAuthority:
    def __init__(
        self,
        *,
        worker_registry: WorkerRegistryStore,
        store: AuthenticatedWorkerIdentityStore,
        supervisor_key_provider: Callable[[], bytes] = resolve_runtime_payload_key,
    ) -> None:
        if not callable(supervisor_key_provider):
            raise TypeError("supervisor_key_provider 必须可调用。")
        self.worker_registry = worker_registry
        self.store = store
        self._supervisor_key_provider = supervisor_key_provider

    async def enroll(
        self,
        identity: AuthenticatedWorkerIdentity,
    ) -> AuthenticatedWorkerIdentityView:
        item = AuthenticatedWorkerIdentity.model_validate_json(
            identity.model_dump_json()
        )
        if not self._verify_attestation(item):
            raise AuthenticatedWorkerIdentityError(
                "authenticated_worker_identity_attestation_invalid",
                "Worker Identity supervisor attestation 无效。",
            )
        registration = await self.worker_registry.get_active(item.worker_id)
        if registration is None or not _registration_matches(item, registration.contract):
            raise AuthenticatedWorkerIdentityError(
                "authenticated_worker_identity_worker_stale",
                "Worker Identity 未绑定 current active Worker incarnation。",
            )
        if _aware(item.enrolled_at) < _aware(registration.registered_at):
            raise AuthenticatedWorkerIdentityError(
                "authenticated_worker_identity_time_invalid",
                "Worker Identity enrolled_at 早于 Worker registration。",
            )
        recorded = await self.store.record(item)
        view = await self.inspect(recorded)
        if not view.identity_authority:
            raise AuthenticatedWorkerIdentityError(
                "authenticated_worker_identity_changed",
                "Worker Identity 持久化期间 authority 已变化。",
            )
        return view

    async def resolve_for_contract(
        self,
        contract: WorkerContract,
    ) -> AuthenticatedWorkerIdentityView | None:
        identity = await self.store.get_for_contract(contract)
        return None if identity is None else await self.inspect(identity)

    async def inspect(
        self,
        identity: AuthenticatedWorkerIdentity,
    ) -> AuthenticatedWorkerIdentityView:
        item = AuthenticatedWorkerIdentity.model_validate_json(
            identity.model_dump_json()
        )
        durable = False
        attested = False
        active = False
        try:
            stored = await self.store.get(item.identity_id)
            registration = await self.worker_registry.get_active(item.worker_id)
            durable = stored == item
            attested = self._verify_attestation(item)
            active = bool(
                registration is not None
                and registration.state is WorkerRegistrationState.ACTIVE
                and _registration_matches(item, registration.contract)
                and _aware(item.enrolled_at) >= _aware(registration.registered_at)
            )
        except (
            AuthenticatedWorkerIdentityError,
            WorkerRegistryStoreError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            durable = attested = active = False
        current = durable and attested and active
        return AuthenticatedWorkerIdentityView(
            identity=item,
            durable_source_valid=durable,
            supervisor_attestation_valid=attested,
            active_incarnation_valid=active,
            identity_authority=current,
        )

    def _verify_attestation(self, identity: AuthenticatedWorkerIdentity) -> bool:
        try:
            key = self._supervisor_key_provider()
        except (OSError, RuntimeError, TypeError, ValueError):
            return False
        return verify_authenticated_worker_identity(
            identity,
            supervisor_key=key,
        )


def _registration_matches(
    identity: AuthenticatedWorkerIdentity,
    contract: WorkerContract,
) -> bool:
    return bool(
        identity.worker_id == contract.worker_id
        and identity.worker_instance_id == contract.instance_id
        and identity.worker_epoch == contract.epoch
        and identity.worker_contract_sha256 == contract.contract_sha256
    )


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS authenticated_worker_identities (
            identity_id TEXT PRIMARY KEY,
            identity_sha256 TEXT NOT NULL UNIQUE,
            worker_id TEXT NOT NULL,
            worker_instance_id TEXT NOT NULL,
            worker_epoch INTEGER NOT NULL,
            worker_contract_sha256 TEXT NOT NULL,
            public_key_sha256 TEXT NOT NULL,
            identity_json TEXT NOT NULL,
            enrolled_at TEXT NOT NULL,
            UNIQUE(worker_id, worker_instance_id, worker_epoch)
        );
        CREATE INDEX IF NOT EXISTS authenticated_worker_identity_contract
        ON authenticated_worker_identities (
            worker_id, worker_instance_id, worker_epoch, worker_contract_sha256
        );
        """
    )
    await db.commit()


def _restore(encoded: str) -> AuthenticatedWorkerIdentity:
    try:
        if len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
            raise ValueError("oversized")
        return AuthenticatedWorkerIdentity.model_validate_json(encoded)
    except (TypeError, ValueError) as exc:
        raise AuthenticatedWorkerIdentityError(
            "authenticated_worker_identity_store_corrupt",
            "Worker Identity 无法验证。",
        ) from exc


def _supervisor_key(value: bytes) -> bytes:
    if not isinstance(value, bytes) or len(value) < 32:
        raise ValueError("Worker Identity supervisor key 至少需要 32 bytes。")
    return value


def _identity_id(value: str) -> str:
    normalized = str(value or "").strip()
    if re.fullmatch(r"workeridentity_[0-9a-f]{24}", normalized) is None:
        raise AuthenticatedWorkerIdentityError(
            "authenticated_worker_identity_id_invalid",
            "Worker Identity ID 格式无效。",
        )
    return normalized


def _decode_canonical_base64(
    value: str,
    *,
    expected_bytes: int,
    field: str,
) -> bytes:
    try:
        raw = base64.b64decode(value, validate=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} Base64 无效。") from exc
    if len(raw) != expected_bytes:
        raise ValueError(f"{field} 长度无效。")
    canonical = base64.b64encode(raw).decode("ascii")
    if not hmac.compare_digest(value, canonical):
        raise ValueError(f"{field} 必须使用 canonical Base64。")
    return raw


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Worker Identity 时间必须包含时区。")
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


__all__ = [
    "AUTHENTICATED_WORKER_IDENTITY_POLICY",
    "AuthenticatedWorkerIdentity",
    "AuthenticatedWorkerIdentityAuthority",
    "AuthenticatedWorkerIdentityError",
    "AuthenticatedWorkerIdentityStore",
    "AuthenticatedWorkerIdentityView",
    "issue_authenticated_worker_identity",
    "verify_authenticated_worker_identity",
]
