"""Signed, privacy-bounded managed-installation population snapshots."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

RELEASE_POPULATION_CREDENTIAL_POLICY = "naumi-release-population-credential-v1"
RELEASE_POPULATION_CREDENTIAL_DOMAIN = "naumi.release.population-credential.v1"
RELEASE_POPULATION_SNAPSHOT_POLICY = "naumi-release-population-snapshot-v1"
RELEASE_POPULATION_SNAPSHOT_DOMAIN = "naumi.release.population-snapshot.v1"
RELEASE_POPULATION_TRUST_POLICY = "naumi-release-population-trust-v1"

_SHA256_RE = r"^[0-9a-f]{64}$"
_IDENTIFIER_RE = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_CHANNEL_RE = r"^[a-z][a-z0-9._-]{0,63}$"
_MAX_MEMBERS = 10_000
_MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024


class ReleasePopulationRegistryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class ReleasePopulationRegistryIdentity(_StrictModel):
    schema_version: Literal[1] = 1
    registry_id: str = Field(pattern=_IDENTIFIER_RE)
    key_id: str = Field(pattern=_IDENTIFIER_RE)
    key_generation: int = Field(ge=1, le=1_000_000)
    public_key_base64: str = Field(min_length=44, max_length=44)
    public_key_sha256: str = Field(pattern=_SHA256_RE)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        public_key = _decode_base64(
            self.public_key_base64,
            expected_bytes=32,
            label="population registry public key",
        )
        if not hmac.compare_digest(
            base64.b64encode(public_key).decode("ascii"),
            self.public_key_base64,
        ):
            raise ValueError("Population Registry public key 必须使用 canonical Base64。")
        if not hmac.compare_digest(
            hashlib.sha256(public_key).hexdigest(),
            self.public_key_sha256,
        ):
            raise ValueError("Population Registry public key 摘要不一致。")
        return self


class ReleaseTrustedPopulationRegistryKey(_StrictModel):
    schema_version: Literal[1] = 1
    identity: ReleasePopulationRegistryIdentity
    state: Literal["active", "revoked"]
    valid_from: str = Field(min_length=1, max_length=100)
    valid_until: str | None = Field(default=None, min_length=1, max_length=100)
    revoked_at: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def _window(self) -> Self:
        valid_from = _aware(self.valid_from)
        valid_until = _aware(self.valid_until) if self.valid_until else None
        revoked_at = _aware(self.revoked_at) if self.revoked_at else None
        if valid_until is not None and valid_until <= valid_from:
            raise ValueError("Population Registry key 有效期终点必须晚于起点。")
        if self.state == "active" and revoked_at is not None:
            raise ValueError("Active Population Registry key 不得声明 revoked_at。")
        if self.state == "revoked" and revoked_at is None:
            raise ValueError("Revoked Population Registry key 必须声明 revoked_at。")
        return self


class ReleasePopulationTrustPolicyDocument(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "naumi-release-population-trust-v1"
    ] = RELEASE_POPULATION_TRUST_POLICY
    policy_id: str = Field(pattern=r"^relpoptrust_[0-9a-f]{24}$")
    policy_sha256: str = Field(pattern=_SHA256_RE)
    keys: tuple[ReleaseTrustedPopulationRegistryKey, ...] = Field(
        min_length=1,
        max_length=1_000,
    )

    @model_validator(mode="after")
    def _exact(self) -> Self:
        identities = tuple(
            (
                item.identity.registry_id,
                item.identity.key_id,
                item.identity.key_generation,
            )
            for item in self.keys
        )
        if len(identities) != len(set(identities)):
            raise ValueError("Population Trust Policy 含重复 registry key identity。")
        core = self.model_dump(mode="json", exclude={"policy_id", "policy_sha256"})
        digest = _digest(core)
        if self.policy_sha256 != digest or self.policy_id != (
            f"relpoptrust_{digest[:24]}"
        ):
            raise ValueError("Population Trust Policy identity 不一致。")
        return self

    def find(
        self,
        identity: ReleasePopulationRegistryIdentity,
    ) -> ReleaseTrustedPopulationRegistryKey | None:
        return next((item for item in self.keys if item.identity == identity), None)


class ReleaseManagedInstallationCredentialPayload(_StrictModel):
    schema_version: Literal[1] = 1
    domain: Literal[
        "naumi.release.population-credential.v1"
    ] = RELEASE_POPULATION_CREDENTIAL_DOMAIN
    product: Literal["NaumiAgent"] = "NaumiAgent"
    member_id: str = Field(pattern=r"^relpopmember_[0-9a-f]{24}$")
    installation_public_key_base64: str = Field(min_length=44, max_length=44)
    installation_public_key_sha256: str = Field(pattern=_SHA256_RE)
    channel: str = Field(pattern=_CHANNEL_RE)
    registry: ReleasePopulationRegistryIdentity
    registered_at: str = Field(min_length=1, max_length=100)
    expires_at: str = Field(min_length=1, max_length=100)
    pseudonym_scheme: Literal["hmac-sha256-registry-v1"] = (
        "hmac-sha256-registry-v1"
    )
    raw_machine_identifier_collected: Literal[False] = False
    raw_user_identifier_collected: Literal[False] = False
    raw_path_collected: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        public_key = _decode_base64(
            self.installation_public_key_base64,
            expected_bytes=32,
            label="installation public key",
        )
        if not hmac.compare_digest(
            base64.b64encode(public_key).decode("ascii"),
            self.installation_public_key_base64,
        ) or not hmac.compare_digest(
            hashlib.sha256(public_key).hexdigest(),
            self.installation_public_key_sha256,
        ):
            raise ValueError("Installation public key identity 不一致。")
        if _aware(self.expires_at) <= _aware(self.registered_at):
            raise ValueError("Managed Installation Credential 有效期无效。")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.model_dump(mode="json"))


class ReleaseManagedInstallationCredential(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "naumi-release-population-credential-v1"
    ] = RELEASE_POPULATION_CREDENTIAL_POLICY
    credential_id: str = Field(pattern=r"^relpopcred_[0-9a-f]{24}$")
    credential_sha256: str = Field(pattern=_SHA256_RE)
    payload: ReleaseManagedInstallationCredentialPayload
    signature_algorithm: Literal["ed25519"] = "ed25519"
    signature_base64: str = Field(min_length=88, max_length=88)
    signature_sha256: str = Field(pattern=_SHA256_RE)
    private_key_persisted: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        _canonical_signature(
            self.signature_base64,
            self.signature_sha256,
            label="population credential signature",
        )
        core = self.model_dump(
            mode="json",
            exclude={"credential_id", "credential_sha256"},
        )
        digest = _digest(core)
        if self.credential_sha256 != digest or self.credential_id != (
            f"relpopcred_{digest[:24]}"
        ):
            raise ValueError("Managed Installation Credential identity 不一致。")
        return self


class ReleasePopulationSnapshotPayload(_StrictModel):
    schema_version: Literal[1] = 1
    domain: Literal[
        "naumi.release.population-snapshot.v1"
    ] = RELEASE_POPULATION_SNAPSHOT_DOMAIN
    product: Literal["NaumiAgent"] = "NaumiAgent"
    registry: ReleasePopulationRegistryIdentity
    channel: str = Field(pattern=_CHANNEL_RE)
    sequence: int = Field(ge=1, le=1_000_000)
    previous_snapshot_id: str = Field(
        default="",
        pattern=r"^(?:|relpopsnapshot_[0-9a-f]{24})$",
    )
    previous_snapshot_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    credentials: tuple[ReleaseManagedInstallationCredential, ...] = Field(
        min_length=1,
        max_length=_MAX_MEMBERS,
    )
    population_denominator: int = Field(ge=1, le=_MAX_MEMBERS)
    generated_at: str = Field(min_length=1, max_length=100)
    valid_from: str = Field(min_length=1, max_length=100)
    expires_at: str = Field(min_length=1, max_length=100)
    complete_registry_export: Literal[True] = True
    deleted_installations_excluded: Literal[True] = True
    raw_identifiers_excluded: Literal[True] = True

    @model_validator(mode="after")
    def _exact(self) -> Self:
        generated = _aware(self.generated_at)
        valid_from = _aware(self.valid_from)
        expires = _aware(self.expires_at)
        if not generated <= valid_from < expires:
            raise ValueError("Population Snapshot validity window 无效。")
        if self.sequence == 1:
            if self.previous_snapshot_id or self.previous_snapshot_sha256:
                raise ValueError("首个 Population Snapshot 不得声明 previous link。")
        elif not self.previous_snapshot_id or not self.previous_snapshot_sha256:
            raise ValueError("后续 Population Snapshot 必须声明 previous link。")
        member_ids = tuple(item.payload.member_id for item in self.credentials)
        credential_ids = tuple(item.credential_id for item in self.credentials)
        public_keys = tuple(
            item.payload.installation_public_key_sha256 for item in self.credentials
        )
        if not (
            self.population_denominator == len(self.credentials)
            and member_ids == tuple(sorted(member_ids))
            and len(member_ids) == len(set(member_ids))
            and len(credential_ids) == len(set(credential_ids))
            and len(public_keys) == len(set(public_keys))
            and all(
                item.payload.registry == self.registry
                and item.payload.channel == self.channel
                and _aware(item.payload.registered_at) <= valid_from
                and _aware(item.payload.expires_at) > expires
                for item in self.credentials
            )
        ):
            raise ValueError("Population Snapshot member projection 不完整。")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.model_dump(mode="json"))


class ReleasePopulationSnapshot(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "naumi-release-population-snapshot-v1"
    ] = RELEASE_POPULATION_SNAPSHOT_POLICY
    snapshot_id: str = Field(pattern=r"^relpopsnapshot_[0-9a-f]{24}$")
    snapshot_sha256: str = Field(pattern=_SHA256_RE)
    payload: ReleasePopulationSnapshotPayload
    signature_algorithm: Literal["ed25519"] = "ed25519"
    signature_base64: str = Field(min_length=88, max_length=88)
    signature_sha256: str = Field(pattern=_SHA256_RE)
    private_key_persisted: Literal[False] = False
    population_snapshot_authority: Literal[True] = True
    cohort_assignment_authority: Literal[False] = False
    percentage_rollout_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        _canonical_signature(
            self.signature_base64,
            self.signature_sha256,
            label="population snapshot signature",
        )
        core = self.model_dump(
            mode="json",
            exclude={"snapshot_id", "snapshot_sha256"},
        )
        digest = _digest(core)
        if self.snapshot_sha256 != digest or self.snapshot_id != (
            f"relpopsnapshot_{digest[:24]}"
        ):
            raise ValueError("Population Snapshot identity 不一致。")
        return self


class ReleasePopulationSnapshotView(_StrictModel):
    snapshot: ReleasePopulationSnapshot
    source_current: bool
    latest_for_channel: bool
    trust_current: bool
    expired: bool
    invalidation_reasons: tuple[str, ...] = Field(max_length=8)
    population_snapshot_authority: bool
    cohort_assignment_authority: Literal[False] = False
    percentage_rollout_authority: Literal[False] = False

    @model_validator(mode="after")
    def _project(self) -> Self:
        expected = bool(
            self.source_current
            and self.latest_for_channel
            and self.trust_current
            and not self.expired
        )
        if not (
            self.population_snapshot_authority is expected
            and tuple(sorted(set(self.invalidation_reasons)))
            == self.invalidation_reasons
        ):
            raise ValueError("Population Snapshot View authority projection 不一致。")
        return self


@dataclass(frozen=True)
class ReleasePopulationRegistrySigner:
    identity: ReleasePopulationRegistryIdentity
    _private_key: Ed25519PrivateKey = field(repr=False)
    _pseudonym_key: bytes = field(repr=False)

    @classmethod
    def from_private_keys_base64(
        cls,
        *,
        registry_id: str,
        key_id: str,
        key_generation: int,
        signing_private_key_base64: str,
        pseudonym_key_base64: str,
    ) -> ReleasePopulationRegistrySigner:
        signing_bytes = _decode_base64(
            signing_private_key_base64,
            expected_bytes=32,
            label="population registry signing private key",
        )
        pseudonym_key = _decode_base64(
            pseudonym_key_base64,
            expected_bytes=32,
            label="population registry pseudonym key",
        )
        if not (
            hmac.compare_digest(
                base64.b64encode(signing_bytes).decode("ascii"),
                signing_private_key_base64,
            )
            and hmac.compare_digest(
                base64.b64encode(pseudonym_key).decode("ascii"),
                pseudonym_key_base64,
            )
        ):
            raise ReleasePopulationRegistryError(
                "population_registry_private_key_noncanonical",
                "Population Registry private keys 必须使用 canonical Base64。",
            )
        try:
            private_key = Ed25519PrivateKey.from_private_bytes(signing_bytes)
        except ValueError as exc:
            raise ReleasePopulationRegistryError(
                "population_registry_private_key_invalid",
                "Population Registry signing key 不是有效 Ed25519 seed。",
            ) from exc
        public_bytes = private_key.public_key().public_bytes_raw()
        identity = ReleasePopulationRegistryIdentity(
            registry_id=registry_id,
            key_id=key_id,
            key_generation=key_generation,
            public_key_base64=base64.b64encode(public_bytes).decode("ascii"),
            public_key_sha256=hashlib.sha256(public_bytes).hexdigest(),
        )
        return cls(identity, private_key, pseudonym_key)

    def issue_credential(
        self,
        *,
        installation_public_key_base64: str,
        channel: str,
        registered_at: str,
        expires_at: str,
    ) -> ReleaseManagedInstallationCredential:
        installation_key = _decode_base64(
            installation_public_key_base64,
            expected_bytes=32,
            label="installation public key",
        )
        installation_sha = hashlib.sha256(installation_key).hexdigest()
        member_digest = hmac.new(
            self._pseudonym_key,
            _canonical_bytes(
                {
                    "domain": "naumi.release.population-member.v1",
                    "channel": channel,
                    "installation_public_key_sha256": installation_sha,
                }
            ),
            hashlib.sha256,
        ).hexdigest()
        payload = ReleaseManagedInstallationCredentialPayload(
            member_id=f"relpopmember_{member_digest[:24]}",
            installation_public_key_base64=installation_public_key_base64,
            installation_public_key_sha256=installation_sha,
            channel=channel,
            registry=self.identity,
            registered_at=_aware(registered_at).isoformat(),
            expires_at=_aware(expires_at).isoformat(),
        )
        return _signed_credential(payload, self._private_key)

    def issue_snapshot(
        self,
        *,
        channel: str,
        credentials: tuple[ReleaseManagedInstallationCredential, ...],
        previous: ReleasePopulationSnapshot | None,
        generated_at: str,
        valid_from: str,
        expires_at: str,
    ) -> ReleasePopulationSnapshot:
        ordered = tuple(sorted(credentials, key=lambda item: item.payload.member_id))
        sequence = 1 if previous is None else previous.payload.sequence + 1
        payload = ReleasePopulationSnapshotPayload(
            registry=self.identity,
            channel=channel,
            sequence=sequence,
            previous_snapshot_id="" if previous is None else previous.snapshot_id,
            previous_snapshot_sha256=("" if previous is None else previous.snapshot_sha256),
            credentials=ordered,
            population_denominator=len(ordered),
            generated_at=_aware(generated_at).isoformat(),
            valid_from=_aware(valid_from).isoformat(),
            expires_at=_aware(expires_at).isoformat(),
        )
        return _signed_snapshot(payload, self._private_key)


def create_release_population_trust_policy(
    keys: tuple[ReleaseTrustedPopulationRegistryKey, ...],
) -> ReleasePopulationTrustPolicyDocument:
    ordered = tuple(
        sorted(
            keys,
            key=lambda item: (
                item.identity.registry_id,
                item.identity.key_generation,
                item.identity.key_id,
            ),
        )
    )
    core = {
        "schema_version": 1,
        "policy_version": RELEASE_POPULATION_TRUST_POLICY,
        "keys": [item.model_dump(mode="json") for item in ordered],
    }
    digest = _digest(core)
    return ReleasePopulationTrustPolicyDocument.model_validate(
        {
            **core,
            "policy_id": f"relpoptrust_{digest[:24]}",
            "policy_sha256": digest,
        }
    )


class ReleasePopulationSnapshotStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        trust_policy_provider: Callable[[], ReleasePopulationTrustPolicyDocument],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(trust_policy_provider):
            raise TypeError("Population Snapshot Store 需要 Trust Policy provider。")
        self.db_path = Path(db_path).expanduser().resolve()
        self.trust_policy_provider = trust_policy_provider
        self.clock = clock or (lambda: datetime.now(UTC))
        self._locks: dict[str, asyncio.Lock] = {}

    async def record(
        self,
        snapshot: ReleasePopulationSnapshot,
    ) -> ReleasePopulationSnapshotView:
        item = _validated_snapshot(snapshot)
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_SNAPSHOT_BYTES:
            raise ReleasePopulationRegistryError(
                "population_snapshot_oversized",
                "Population Snapshot 超过 16 MiB。",
            )
        self._verify(item)
        channel = item.payload.channel
        lock = self._locks.setdefault(channel, asyncio.Lock())
        async with lock:
            stored = await self._record_locked(item, encoded)
        return await self.inspect(snapshot_id=stored.snapshot_id)

    async def _record_locked(
        self,
        item: ReleasePopulationSnapshot,
        encoded: str,
    ) -> ReleasePopulationSnapshot:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                self._verify(item)
                existing = await (
                    await db.execute(
                        "SELECT snapshot_json FROM release_population_snapshots "
                        "WHERE snapshot_id = ?",
                        (item.snapshot_id,),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _restore_snapshot(existing["snapshot_json"])
                    await db.rollback()
                    if restored != item:
                        raise ReleasePopulationRegistryError(
                            "population_snapshot_identity_conflict",
                            "同一 Population Snapshot ID 已绑定不同内容。",
                        )
                    return restored
                await self._require_next_link(db, item)
                await db.execute(
                    "INSERT INTO release_population_snapshots "
                    "(snapshot_id, snapshot_sha256, channel, sequence, snapshot_json, "
                    "generated_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.snapshot_id,
                        item.snapshot_sha256,
                        item.payload.channel,
                        item.payload.sequence,
                        encoded,
                        item.payload.generated_at,
                        item.payload.expires_at,
                    ),
                )
                await db.commit()
        except ReleasePopulationRegistryError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise ReleasePopulationRegistryError(
                "population_snapshot_store_error",
                "Population Snapshot 无法持久化。",
            ) from exc
        return item

    async def _require_next_link(
        self,
        db: aiosqlite.Connection,
        item: ReleasePopulationSnapshot,
    ) -> None:
        latest_row = await (
            await db.execute(
                "SELECT snapshot_json FROM release_population_snapshots "
                "WHERE channel = ? ORDER BY sequence DESC LIMIT 1",
                (item.payload.channel,),
            )
        ).fetchone()
        latest = (
            None
            if latest_row is None
            else _restore_snapshot(latest_row["snapshot_json"])
        )
        expected = (
            (1, "", "")
            if latest is None
            else (
                latest.payload.sequence + 1,
                latest.snapshot_id,
                latest.snapshot_sha256,
            )
        )
        actual = (
            item.payload.sequence,
            item.payload.previous_snapshot_id,
            item.payload.previous_snapshot_sha256,
        )
        if actual != expected:
            raise ReleasePopulationRegistryError(
                "population_snapshot_chain_conflict",
                "Population Snapshot sequence/previous link 已变化。",
            )

    async def get(self, snapshot_id: str) -> ReleasePopulationSnapshot | None:
        return await self._read("WHERE snapshot_id = ?", (snapshot_id,))

    async def latest(self, channel: str) -> ReleasePopulationSnapshot | None:
        return await self._read(
            "WHERE channel = ? ORDER BY sequence DESC LIMIT 1",
            (channel,),
        )

    async def inspect(self, *, snapshot_id: str) -> ReleasePopulationSnapshotView:
        snapshot = await self.get(snapshot_id)
        if snapshot is None:
            raise ReleasePopulationRegistryError(
                "population_snapshot_missing",
                "指定的 Population Snapshot 不存在。",
            )
        reasons: list[str] = []
        source_current = True
        try:
            restored = await self.get(snapshot_id)
            source_current = restored == snapshot
        except ReleasePopulationRegistryError:
            source_current = False
        if not source_current:
            reasons.append("snapshot_source_changed")
        try:
            latest = await self.latest(snapshot.payload.channel)
            latest_for_channel = latest == snapshot
        except ReleasePopulationRegistryError:
            latest_for_channel = False
        if not latest_for_channel:
            reasons.append("newer_snapshot_exists")
        try:
            self._verify(snapshot)
            trust_current = True
        except ReleasePopulationRegistryError:
            trust_current = False
        if not trust_current:
            reasons.append("registry_trust_changed")
        expired = _aware(self.clock()) >= _aware(snapshot.payload.expires_at)
        if expired:
            reasons.append("snapshot_expired")
        authority = bool(
            source_current and latest_for_channel and trust_current and not expired
        )
        return ReleasePopulationSnapshotView(
            snapshot=snapshot,
            source_current=source_current,
            latest_for_channel=latest_for_channel,
            trust_current=trust_current,
            expired=expired,
            invalidation_reasons=tuple(sorted(set(reasons))),
            population_snapshot_authority=authority,
        )

    async def _read(self, where: str, parameters: tuple[object, ...]):
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT snapshot_json FROM release_population_snapshots " + where,
                        parameters,
                    )
                ).fetchone()
            return None if row is None else _restore_snapshot(row["snapshot_json"])
        except ReleasePopulationRegistryError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise ReleasePopulationRegistryError(
                "population_snapshot_source_invalid",
                "Population Snapshot durable source 无效。",
            ) from exc

    def _verify(self, snapshot: ReleasePopulationSnapshot) -> None:
        try:
            policy = self.trust_policy_provider()
        except (OSError, TypeError, ValueError) as exc:
            raise ReleasePopulationRegistryError(
                "population_trust_policy_unavailable",
                "Population Trust Policy 当前不可读取。",
            ) from exc
        if not isinstance(policy, ReleasePopulationTrustPolicyDocument):
            raise ReleasePopulationRegistryError(
                "population_trust_policy_invalid",
                "Population Trust Policy provider 返回无效对象。",
            )
        verify_release_population_snapshot(snapshot, trust_policy=policy)


def verify_release_population_credential(
    credential: ReleaseManagedInstallationCredential,
    *,
    trust_policy: ReleasePopulationTrustPolicyDocument,
) -> ReleaseTrustedPopulationRegistryKey:
    trusted = _trusted_key(
        credential.payload.registry,
        trust_policy,
        signed_at=credential.payload.registered_at,
    )
    _verify_signature(
        credential.signature_base64,
        credential.payload.canonical_bytes(),
        trusted.identity.public_key_base64,
        code="population_credential_signature_invalid",
        message="Managed Installation Credential signature 无效。",
    )
    return trusted


def verify_release_population_snapshot(
    snapshot: ReleasePopulationSnapshot,
    *,
    trust_policy: ReleasePopulationTrustPolicyDocument,
) -> ReleaseTrustedPopulationRegistryKey:
    payload = snapshot.payload
    trusted = _trusted_key(
        payload.registry,
        trust_policy,
        signed_at=payload.generated_at,
    )
    _verify_signature(
        snapshot.signature_base64,
        payload.canonical_bytes(),
        trusted.identity.public_key_base64,
        code="population_snapshot_signature_invalid",
        message="Population Snapshot signature 无效。",
    )
    for credential in payload.credentials:
        verify_release_population_credential(credential, trust_policy=trust_policy)
    return trusted


def _trusted_key(identity, policy, *, signed_at):
    trusted = policy.find(identity)
    if trusted is None:
        raise ReleasePopulationRegistryError(
            "population_registry_untrusted",
            "Population artifact 的 registry key 不在当前信任策略中。",
        )
    if trusted.state != "active":
        raise ReleasePopulationRegistryError(
            "population_registry_revoked",
            "Population artifact 的 registry key 已撤销。",
        )
    timestamp = _aware(signed_at)
    valid_from = _aware(trusted.valid_from)
    valid_until = _aware(trusted.valid_until) if trusted.valid_until else None
    if timestamp < valid_from or (valid_until is not None and timestamp >= valid_until):
        raise ReleasePopulationRegistryError(
            "population_registry_outside_validity",
            "Population artifact 的签发时间不在 registry key 有效期内。",
        )
    return trusted


def _signed_credential(payload, private_key):
    signature = private_key.sign(payload.canonical_bytes())
    signature_base64 = base64.b64encode(signature).decode("ascii")
    core = {
        "schema_version": 1,
        "policy_version": RELEASE_POPULATION_CREDENTIAL_POLICY,
        "payload": payload.model_dump(mode="json"),
        "signature_algorithm": "ed25519",
        "signature_base64": signature_base64,
        "signature_sha256": hashlib.sha256(signature).hexdigest(),
        "private_key_persisted": False,
    }
    digest = _digest(core)
    return ReleaseManagedInstallationCredential.model_validate(
        {
            **core,
            "credential_id": f"relpopcred_{digest[:24]}",
            "credential_sha256": digest,
        }
    )


def _signed_snapshot(payload, private_key):
    signature = private_key.sign(payload.canonical_bytes())
    signature_base64 = base64.b64encode(signature).decode("ascii")
    core = {
        "schema_version": 1,
        "policy_version": RELEASE_POPULATION_SNAPSHOT_POLICY,
        "payload": payload.model_dump(mode="json"),
        "signature_algorithm": "ed25519",
        "signature_base64": signature_base64,
        "signature_sha256": hashlib.sha256(signature).hexdigest(),
        "private_key_persisted": False,
        "population_snapshot_authority": True,
        "cohort_assignment_authority": False,
        "percentage_rollout_authority": False,
    }
    digest = _digest(core)
    return ReleasePopulationSnapshot.model_validate(
        {
            **core,
            "snapshot_id": f"relpopsnapshot_{digest[:24]}",
            "snapshot_sha256": digest,
        }
    )


def _verify_signature(signature_base64, payload, public_key_base64, *, code, message):
    signature = _decode_base64(signature_base64, expected_bytes=64, label="signature")
    public_key = _decode_base64(
        public_key_base64,
        expected_bytes=32,
        label="registry public key",
    )
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, payload)
    except (InvalidSignature, ValueError) as exc:
        raise ReleasePopulationRegistryError(code, message) from exc


def _canonical_signature(value, digest, *, label):
    signature = _decode_base64(value, expected_bytes=64, label=label)
    if not hmac.compare_digest(
        base64.b64encode(signature).decode("ascii"),
        value,
    ) or not hmac.compare_digest(hashlib.sha256(signature).hexdigest(), digest):
        raise ValueError(f"{label} identity 不一致。")


def _decode_base64(value: str, *, expected_bytes: int, label: str) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{label} 不是 canonical Base64。") from exc
    if len(decoded) != expected_bytes:
        raise ValueError(f"{label} 长度无效。")
    return decoded


def _validated_snapshot(value) -> ReleasePopulationSnapshot:
    try:
        return ReleasePopulationSnapshot.model_validate_json(value.model_dump_json())
    except (AttributeError, TypeError, ValueError) as exc:
        raise ReleasePopulationRegistryError(
            "population_snapshot_invalid",
            "Population Snapshot artifact 无效。",
        ) from exc


def _restore_snapshot(value: str) -> ReleasePopulationSnapshot:
    if len(value.encode()) > _MAX_SNAPSHOT_BYTES:
        raise ValueError("Population Snapshot durable source 超过 16 MiB。")
    return ReleasePopulationSnapshot.model_validate_json(value)


def _aware(value) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Population timestamp 必须包含 offset。")
    return parsed.astimezone(UTC)


def _canonical_bytes(payload) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _digest(payload) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS release_population_snapshots ("
        "snapshot_id TEXT PRIMARY KEY, snapshot_sha256 TEXT NOT NULL UNIQUE, "
        "channel TEXT NOT NULL, sequence INTEGER NOT NULL, snapshot_json TEXT NOT NULL, "
        "generated_at TEXT NOT NULL, expires_at TEXT NOT NULL, "
        "UNIQUE(channel, sequence))"
    )
    await db.commit()


__all__ = [
    "RELEASE_POPULATION_CREDENTIAL_DOMAIN",
    "RELEASE_POPULATION_CREDENTIAL_POLICY",
    "RELEASE_POPULATION_SNAPSHOT_DOMAIN",
    "RELEASE_POPULATION_SNAPSHOT_POLICY",
    "RELEASE_POPULATION_TRUST_POLICY",
    "ReleaseManagedInstallationCredential",
    "ReleaseManagedInstallationCredentialPayload",
    "ReleasePopulationRegistryError",
    "ReleasePopulationRegistryIdentity",
    "ReleasePopulationRegistrySigner",
    "ReleasePopulationSnapshot",
    "ReleasePopulationSnapshotPayload",
    "ReleasePopulationSnapshotStore",
    "ReleasePopulationSnapshotView",
    "ReleasePopulationTrustPolicyDocument",
    "ReleaseTrustedPopulationRegistryKey",
    "create_release_population_trust_policy",
    "verify_release_population_credential",
    "verify_release_population_snapshot",
]
