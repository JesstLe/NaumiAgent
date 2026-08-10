"""Supervisor-attested X25519 transport keys for authenticated workers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.config.credentials import resolve_runtime_payload_key
from naumi_agent.daemons.authenticated_worker_identity import (
    AuthenticatedWorkerIdentity,
    AuthenticatedWorkerIdentityAuthority,
    AuthenticatedWorkerIdentityError,
)

AUTHENTICATED_WORKER_TRANSPORT_KEY_POLICY = (
    "authenticated-worker-transport-key-v1"
)
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


class AuthenticatedWorkerTransportKey(_StrictModel):
    """One public encryption key bound to an exact authenticated Worker."""

    schema_version: Literal[1] = 1
    policy_version: Literal["authenticated-worker-transport-key-v1"] = (
        AUTHENTICATED_WORKER_TRANSPORT_KEY_POLICY
    )
    transport_key_id: str = Field(pattern=r"^workertransportkey_[0-9a-f]{24}$")
    transport_key_sha256: str = Field(pattern=_SHA256_RE)
    identity_id: str = Field(pattern=r"^workeridentity_[0-9a-f]{24}$")
    identity_sha256: str = Field(pattern=_SHA256_RE)
    worker_id: str = Field(min_length=1, max_length=128)
    worker_instance_id: str = Field(min_length=1, max_length=128)
    worker_epoch: int = Field(ge=1)
    worker_contract_sha256: str = Field(pattern=_SHA256_RE)
    key_generation: int = Field(ge=1, le=1_000_000)
    public_key_base64: str = Field(pattern=_B64_32_RE)
    public_key_sha256: str = Field(pattern=_SHA256_RE)
    key_agreement_algorithm: Literal["x25519"] = "x25519"
    key_derivation_algorithm: Literal["hkdf-sha256"] = "hkdf-sha256"
    payload_algorithm: Literal["aes-256-gcm"] = "aes-256-gcm"
    enrolled_at: str = Field(min_length=1, max_length=100)
    supervisor_attestation_sha256: str = Field(pattern=_SHA256_RE)
    private_key_stored: Literal[False] = False
    transport_encryption_eligible: Literal[True] = True
    transport_delivered: Literal[False] = False
    execution_authority: Literal[False] = False
    result_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        raw = _decode_canonical_base64(
            self.public_key_base64,
            expected_bytes=32,
            field="Worker Transport public key",
        )
        _validate_x25519_public_key(raw)
        _aware(self.enrolled_at)
        if not hmac.compare_digest(
            self.public_key_sha256,
            hashlib.sha256(raw).hexdigest(),
        ):
            raise ValueError("Worker Transport public key 摘要不一致。")
        core = self.model_dump(
            mode="json",
            exclude={
                "transport_key_id",
                "transport_key_sha256",
                "supervisor_attestation_sha256",
            },
        )
        digest = _digest(core)
        if not (
            hmac.compare_digest(self.transport_key_sha256, digest)
            and self.transport_key_id == f"workertransportkey_{digest[:24]}"
        ):
            raise ValueError("Worker Transport Key identity 不一致。")
        if self.transport_delivered or self.execution_authority or self.result_authority:
            raise ValueError("Worker Transport Key 不得扩大 delivery/execution authority。")
        return self


class AuthenticatedWorkerTransportKeyView(_StrictModel):
    transport_key: AuthenticatedWorkerTransportKey
    durable_source_valid: bool
    supervisor_attestation_valid: bool
    identity_authority: bool
    latest_generation_valid: bool
    transport_key_authority: bool
    transport_delivered: Literal[False] = False
    execution_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        current = bool(
            self.durable_source_valid
            and self.supervisor_attestation_valid
            and self.identity_authority
            and self.latest_generation_valid
        )
        if self.transport_key_authority is not current:
            raise ValueError("Worker Transport Key authority 投影不一致。")
        return self


class AuthenticatedWorkerTransportKeyError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def issue_authenticated_worker_transport_key(
    *,
    identity: AuthenticatedWorkerIdentity,
    key_generation: int,
    public_key_base64: str,
    enrolled_at: str,
    supervisor_key: bytes,
) -> AuthenticatedWorkerTransportKey:
    item = AuthenticatedWorkerIdentity.model_validate_json(identity.model_dump_json())
    if not isinstance(key_generation, int) or isinstance(key_generation, bool):
        raise TypeError("Worker Transport key generation 必须是整数。")
    if not 1 <= key_generation <= 1_000_000:
        raise ValueError("Worker Transport key generation 超出范围。")
    raw = _decode_canonical_base64(
        public_key_base64,
        expected_bytes=32,
        field="Worker Transport public key",
    )
    _validate_x25519_public_key(raw)
    normalized_time = _aware(enrolled_at).isoformat()
    key = _supervisor_key(supervisor_key)
    core = {
        "schema_version": 1,
        "policy_version": AUTHENTICATED_WORKER_TRANSPORT_KEY_POLICY,
        "identity_id": item.identity_id,
        "identity_sha256": item.identity_sha256,
        "worker_id": item.worker_id,
        "worker_instance_id": item.worker_instance_id,
        "worker_epoch": item.worker_epoch,
        "worker_contract_sha256": item.worker_contract_sha256,
        "key_generation": key_generation,
        "public_key_base64": base64.b64encode(raw).decode("ascii"),
        "public_key_sha256": hashlib.sha256(raw).hexdigest(),
        "key_agreement_algorithm": "x25519",
        "key_derivation_algorithm": "hkdf-sha256",
        "payload_algorithm": "aes-256-gcm",
        "enrolled_at": normalized_time,
        "private_key_stored": False,
        "transport_encryption_eligible": True,
        "transport_delivered": False,
        "execution_authority": False,
        "result_authority": False,
    }
    digest = _digest(core)
    attestation = hmac.new(key, _canonical(core), hashlib.sha256).hexdigest()
    return AuthenticatedWorkerTransportKey.model_validate(
        {
            **core,
            "transport_key_id": f"workertransportkey_{digest[:24]}",
            "transport_key_sha256": digest,
            "supervisor_attestation_sha256": attestation,
        }
    )


def verify_authenticated_worker_transport_key(
    transport_key: AuthenticatedWorkerTransportKey,
    *,
    supervisor_key: bytes,
) -> bool:
    if not isinstance(transport_key, AuthenticatedWorkerTransportKey):
        return False
    try:
        key = _supervisor_key(supervisor_key)
        restored = AuthenticatedWorkerTransportKey.model_validate_json(
            transport_key.model_dump_json()
        )
    except (TypeError, ValueError):
        return False
    core = restored.model_dump(
        mode="json",
        exclude={
            "transport_key_id",
            "transport_key_sha256",
            "supervisor_attestation_sha256",
        },
    )
    expected = hmac.new(key, _canonical(core), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, restored.supervisor_attestation_sha256)


class AuthenticatedWorkerTransportKeyStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve(strict=False)

    async def record(
        self,
        transport_key: AuthenticatedWorkerTransportKey,
        *,
        identity: AuthenticatedWorkerIdentity,
    ) -> AuthenticatedWorkerTransportKey:
        try:
            item = AuthenticatedWorkerTransportKey.model_validate_json(
                transport_key.model_dump_json()
            )
            identity_item = AuthenticatedWorkerIdentity.model_validate_json(
                identity.model_dump_json()
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise AuthenticatedWorkerTransportKeyError(
                "authenticated_worker_transport_key_invalid",
                "Worker Transport Key artifact 无效。",
            ) from exc
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
            raise AuthenticatedWorkerTransportKeyError(
                "authenticated_worker_transport_key_oversized",
                "Worker Transport Key artifact 超过 64 KiB。",
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                identity_row = await (
                    await db.execute(
                        "SELECT identity_json FROM authenticated_worker_identities "
                        "WHERE identity_id = ?",
                        (item.identity_id,),
                    )
                ).fetchone()
                stored_identity = (
                    None
                    if identity_row is None
                    else _restore_identity(identity_row["identity_json"])
                )
                if (
                    stored_identity != identity_item
                    or not _binding_matches(item, identity_item)
                ):
                    await db.rollback()
                    raise AuthenticatedWorkerTransportKeyError(
                        "authenticated_worker_transport_key_identity_mismatch",
                        "Worker Transport Key 未绑定同库 exact Identity。",
                    )
                existing_row = await (
                    await db.execute(
                        "SELECT transport_key_json FROM authenticated_worker_transport_keys "
                        "WHERE identity_id = ? AND key_generation = ?",
                        (item.identity_id, item.key_generation),
                    )
                ).fetchone()
                if existing_row is not None:
                    existing = _restore(existing_row["transport_key_json"])
                    await db.rollback()
                    if existing == item:
                        return existing
                    raise AuthenticatedWorkerTransportKeyError(
                        "authenticated_worker_transport_key_conflict",
                        "Worker Transport Key generation 已绑定其他内容。",
                    )
                latest_row = await (
                    await db.execute(
                        "SELECT transport_key_json FROM authenticated_worker_transport_keys "
                        "WHERE identity_id = ? ORDER BY key_generation DESC LIMIT 1",
                        (item.identity_id,),
                    )
                ).fetchone()
                latest = (
                    None
                    if latest_row is None
                    else _restore(latest_row["transport_key_json"])
                )
                expected_generation = 1 if latest is None else latest.key_generation + 1
                if item.key_generation != expected_generation or (
                    latest is not None
                    and _aware(item.enrolled_at) <= _aware(latest.enrolled_at)
                ):
                    await db.rollback()
                    raise AuthenticatedWorkerTransportKeyError(
                        "authenticated_worker_transport_key_rotation_invalid",
                        "Worker Transport Key rotation generation/time 不连续。",
                    )
                try:
                    await db.execute(
                        "INSERT INTO authenticated_worker_transport_keys "
                        "(transport_key_id, transport_key_sha256, identity_id, "
                        "key_generation, public_key_sha256, transport_key_json, "
                        "enrolled_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            item.transport_key_id,
                            item.transport_key_sha256,
                            item.identity_id,
                            item.key_generation,
                            item.public_key_sha256,
                            encoded,
                            item.enrolled_at,
                        ),
                    )
                    await db.commit()
                except aiosqlite.IntegrityError as exc:
                    await db.rollback()
                    raise AuthenticatedWorkerTransportKeyError(
                        "authenticated_worker_transport_key_reuse",
                        "Worker Transport public key 不得跨 identity/generation 复用。",
                    ) from exc
            return item
        except AuthenticatedWorkerTransportKeyError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise AuthenticatedWorkerTransportKeyError(
                "authenticated_worker_transport_key_store_failed",
                "Worker Transport Key 无法持久化。",
            ) from exc

    async def get(
        self,
        transport_key_id: str,
    ) -> AuthenticatedWorkerTransportKey | None:
        normalized = _transport_key_id(transport_key_id)
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT transport_key_json FROM authenticated_worker_transport_keys "
                        "WHERE transport_key_id = ?",
                        (normalized,),
                    )
                ).fetchone()
            return None if row is None else _restore(row["transport_key_json"])
        except AuthenticatedWorkerTransportKeyError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise AuthenticatedWorkerTransportKeyError(
                "authenticated_worker_transport_key_store_corrupt",
                "Worker Transport Key 损坏或无法读取。",
            ) from exc

    async def get_latest(
        self,
        identity_id: str,
    ) -> AuthenticatedWorkerTransportKey | None:
        normalized = _identity_id(identity_id)
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT transport_key_json FROM authenticated_worker_transport_keys "
                        "WHERE identity_id = ? ORDER BY key_generation DESC LIMIT 1",
                        (normalized,),
                    )
                ).fetchone()
            return None if row is None else _restore(row["transport_key_json"])
        except AuthenticatedWorkerTransportKeyError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise AuthenticatedWorkerTransportKeyError(
                "authenticated_worker_transport_key_store_corrupt",
                "Worker Transport Key 损坏或无法读取。",
            ) from exc


class AuthenticatedWorkerTransportKeyAuthority:
    def __init__(
        self,
        *,
        identity_authority: AuthenticatedWorkerIdentityAuthority,
        store: AuthenticatedWorkerTransportKeyStore,
        supervisor_key_provider: Callable[[], bytes] = resolve_runtime_payload_key,
    ) -> None:
        if identity_authority.store.db_path != store.db_path:
            raise ValueError("Worker Identity 与 Transport Key 必须共享 SQLite authority。")
        if not callable(supervisor_key_provider):
            raise TypeError("supervisor_key_provider 必须可调用。")
        self.identity_authority = identity_authority
        self.store = store
        self._supervisor_key_provider = supervisor_key_provider

    async def enroll(
        self,
        transport_key: AuthenticatedWorkerTransportKey,
    ) -> AuthenticatedWorkerTransportKeyView:
        item = AuthenticatedWorkerTransportKey.model_validate_json(
            transport_key.model_dump_json()
        )
        if not self._verify_attestation(item):
            raise AuthenticatedWorkerTransportKeyError(
                "authenticated_worker_transport_key_attestation_invalid",
                "Worker Transport Key supervisor attestation 无效。",
            )
        identity = await self.identity_authority.store.get(item.identity_id)
        if identity is None or not _binding_matches(item, identity):
            raise AuthenticatedWorkerTransportKeyError(
                "authenticated_worker_transport_key_identity_missing",
                "Worker Transport Key 的 exact Identity 不存在。",
            )
        identity_view = await self.identity_authority.inspect(identity)
        if not identity_view.identity_authority:
            raise AuthenticatedWorkerTransportKeyError(
                "authenticated_worker_transport_key_identity_stale",
                "Worker Transport Key 的 Identity authority 已失效。",
            )
        if _aware(item.enrolled_at) < _aware(identity.enrolled_at):
            raise AuthenticatedWorkerTransportKeyError(
                "authenticated_worker_transport_key_time_invalid",
                "Worker Transport Key enrolled_at 早于 Identity enrollment。",
            )
        recorded = await self.store.record(item, identity=identity)
        view = await self.inspect(recorded)
        if not view.transport_key_authority:
            raise AuthenticatedWorkerTransportKeyError(
                "authenticated_worker_transport_key_changed",
                "Worker Transport Key 持久化期间 authority 已变化。",
            )
        return view

    async def resolve_for_identity(
        self,
        identity: AuthenticatedWorkerIdentity,
    ) -> AuthenticatedWorkerTransportKeyView | None:
        latest = await self.store.get_latest(identity.identity_id)
        return None if latest is None else await self.inspect(latest)

    async def inspect(
        self,
        transport_key: AuthenticatedWorkerTransportKey,
    ) -> AuthenticatedWorkerTransportKeyView:
        item = AuthenticatedWorkerTransportKey.model_validate_json(
            transport_key.model_dump_json()
        )
        durable = attested = identity_current = latest_current = False
        try:
            stored = await self.store.get(item.transport_key_id)
            latest = await self.store.get_latest(item.identity_id)
            identity = await self.identity_authority.store.get(item.identity_id)
            durable = stored == item
            attested = self._verify_attestation(item)
            latest_current = latest == item
            identity_current = bool(
                identity is not None
                and _binding_matches(item, identity)
                and (await self.identity_authority.inspect(identity)).identity_authority
                and _aware(item.enrolled_at) >= _aware(identity.enrolled_at)
            )
        except (
            AuthenticatedWorkerIdentityError,
            AuthenticatedWorkerTransportKeyError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            durable = attested = identity_current = latest_current = False
        current = durable and attested and identity_current and latest_current
        return AuthenticatedWorkerTransportKeyView(
            transport_key=item,
            durable_source_valid=durable,
            supervisor_attestation_valid=attested,
            identity_authority=identity_current,
            latest_generation_valid=latest_current,
            transport_key_authority=current,
        )

    def _verify_attestation(
        self,
        transport_key: AuthenticatedWorkerTransportKey,
    ) -> bool:
        try:
            key = self._supervisor_key_provider()
        except (OSError, RuntimeError, TypeError, ValueError):
            return False
        return verify_authenticated_worker_transport_key(
            transport_key,
            supervisor_key=key,
        )


def _binding_matches(
    transport_key: AuthenticatedWorkerTransportKey,
    identity: AuthenticatedWorkerIdentity,
) -> bool:
    return bool(
        transport_key.identity_id == identity.identity_id
        and transport_key.identity_sha256 == identity.identity_sha256
        and transport_key.worker_id == identity.worker_id
        and transport_key.worker_instance_id == identity.worker_instance_id
        and transport_key.worker_epoch == identity.worker_epoch
        and transport_key.worker_contract_sha256 == identity.worker_contract_sha256
    )


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS authenticated_worker_transport_keys (
            transport_key_id TEXT PRIMARY KEY,
            transport_key_sha256 TEXT NOT NULL UNIQUE,
            identity_id TEXT NOT NULL,
            key_generation INTEGER NOT NULL,
            public_key_sha256 TEXT NOT NULL UNIQUE,
            transport_key_json TEXT NOT NULL,
            enrolled_at TEXT NOT NULL,
            UNIQUE(identity_id, key_generation)
        );
        CREATE INDEX IF NOT EXISTS authenticated_worker_transport_key_identity
        ON authenticated_worker_transport_keys (identity_id, key_generation DESC);
        """
    )
    await db.commit()


def _restore(encoded: str) -> AuthenticatedWorkerTransportKey:
    try:
        if len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
            raise ValueError("oversized")
        return AuthenticatedWorkerTransportKey.model_validate_json(encoded)
    except (TypeError, ValueError) as exc:
        raise AuthenticatedWorkerTransportKeyError(
            "authenticated_worker_transport_key_store_corrupt",
            "Worker Transport Key 无法验证。",
        ) from exc


def _restore_identity(encoded: str) -> AuthenticatedWorkerIdentity:
    try:
        return AuthenticatedWorkerIdentity.model_validate_json(encoded)
    except (TypeError, ValueError) as exc:
        raise AuthenticatedWorkerTransportKeyError(
            "authenticated_worker_transport_key_identity_corrupt",
            "Worker Identity 无法验证。",
        ) from exc


def _supervisor_key(value: bytes) -> bytes:
    if not isinstance(value, bytes) or len(value) < 32:
        raise ValueError("Worker Transport supervisor key 至少需要 32 bytes。")
    return value


def _transport_key_id(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized.startswith("workertransportkey_") or len(normalized) != 43:
        raise AuthenticatedWorkerTransportKeyError(
            "authenticated_worker_transport_key_id_invalid",
            "Worker Transport Key ID 格式无效。",
        )
    suffix = normalized.removeprefix("workertransportkey_")
    if len(suffix) != 24 or any(char not in "0123456789abcdef" for char in suffix):
        raise AuthenticatedWorkerTransportKeyError(
            "authenticated_worker_transport_key_id_invalid",
            "Worker Transport Key ID 格式无效。",
        )
    return normalized


def _identity_id(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized.startswith("workeridentity_") or len(normalized) != 39:
        raise AuthenticatedWorkerTransportKeyError(
            "authenticated_worker_transport_key_identity_id_invalid",
            "Worker Identity ID 格式无效。",
        )
    suffix = normalized.removeprefix("workeridentity_")
    if len(suffix) != 24 or any(char not in "0123456789abcdef" for char in suffix):
        raise AuthenticatedWorkerTransportKeyError(
            "authenticated_worker_transport_key_identity_id_invalid",
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


def _validate_x25519_public_key(raw: bytes) -> None:
    public_key = X25519PublicKey.from_public_bytes(raw)
    validation_private = X25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
    try:
        validation_private.exchange(public_key)
    except ValueError as exc:
        raise ValueError("Worker Transport public key 不能产生安全 shared secret。") from exc


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Worker Transport Key 时间必须包含时区。")
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
    "AUTHENTICATED_WORKER_TRANSPORT_KEY_POLICY",
    "AuthenticatedWorkerTransportKey",
    "AuthenticatedWorkerTransportKeyAuthority",
    "AuthenticatedWorkerTransportKeyError",
    "AuthenticatedWorkerTransportKeyStore",
    "AuthenticatedWorkerTransportKeyView",
    "issue_authenticated_worker_transport_key",
    "verify_authenticated_worker_transport_key",
]
