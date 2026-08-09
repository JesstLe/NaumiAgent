"""Signed release-channel catalogs for trusted cross-platform update discovery."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal, Self
from urllib.parse import quote, urlsplit

import aiosqlite
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.release.build_attestations import (
    ReleaseBuildAttestation,
    ReleaseBuildAttestationError,
    ReleaseBuildTrustPolicyDocument,
    verify_release_build_attestation_signature,
)

RELEASE_CHANNEL_CATALOG_DOMAIN = "naumi.release.channel-catalog.v1"
RELEASE_CHANNEL_CATALOG_POLICY = "naumi-release-channel-catalog-v1"
RELEASE_CHANNEL_TRUST_POLICY = "naumi-release-channel-trust-v1"
RELEASE_CHANNEL_RESOLUTION_POLICY = "naumi-release-channel-resolution-v1"
_IDENTIFIER_RE = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_CHANNEL_RE = r"^[a-z][a-z0-9._-]{0,63}$"
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_CATALOG_BYTES = 4 * 1024 * 1024
_MAX_ENTRIES = 64


class ReleaseChannelCatalogError(RuntimeError):
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


class ReleaseChannelSignerIdentity(_StrictModel):
    schema_version: Literal[1] = 1
    signer_id: str = Field(pattern=_IDENTIFIER_RE)
    key_id: str = Field(pattern=_IDENTIFIER_RE)
    key_generation: int = Field(ge=1, le=1_000_000)
    public_key_base64: str = Field(min_length=44, max_length=44)
    public_key_sha256: str = Field(pattern=_SHA256_RE)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        public_key = _decode_base64(
            self.public_key_base64,
            expected_bytes=32,
            label="channel signer public key",
        )
        if not (
            hmac.compare_digest(
                base64.b64encode(public_key).decode("ascii"),
                self.public_key_base64,
            )
            and hmac.compare_digest(
                hashlib.sha256(public_key).hexdigest(),
                self.public_key_sha256,
            )
        ):
            raise ValueError("Channel signer public key identity 不一致。")
        return self


class ReleaseTrustedChannelKey(_StrictModel):
    schema_version: Literal[1] = 1
    identity: ReleaseChannelSignerIdentity
    state: Literal["active", "revoked"]
    channels: tuple[str, ...] = Field(min_length=1, max_length=32)
    valid_from: str = Field(min_length=1, max_length=100)
    valid_until: str | None = Field(default=None, min_length=1, max_length=100)
    revoked_at: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if not (
            self.channels == tuple(sorted(set(self.channels)))
            and all(_valid_channel(item) for item in self.channels)
        ):
            raise ValueError("Trusted Channel key channels 必须唯一且有序。")
        valid_from = _aware(self.valid_from)
        valid_until = _aware(self.valid_until) if self.valid_until else None
        revoked_at = _aware(self.revoked_at) if self.revoked_at else None
        if valid_until is not None and valid_until <= valid_from:
            raise ValueError("Channel key 有效期终点必须晚于起点。")
        if (self.state == "active") is not (revoked_at is None):
            raise ValueError("Channel key state/revoked_at 不一致。")
        return self


class ReleaseChannelTrustPolicyDocument(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "naumi-release-channel-trust-v1"
    ] = RELEASE_CHANNEL_TRUST_POLICY
    policy_id: str = Field(pattern=r"^relchanneltrust_[0-9a-f]{24}$")
    policy_sha256: str = Field(pattern=_SHA256_RE)
    keys: tuple[ReleaseTrustedChannelKey, ...] = Field(min_length=1, max_length=100)
    archive_origins: tuple[str, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        identities = tuple(_identity_key(item.identity) for item in self.keys)
        if len(identities) != len(set(identities)):
            raise ValueError("Channel Trust Policy 含重复 key identity。")
        if not (
            self.keys
            == tuple(sorted(self.keys, key=lambda item: _identity_key(item.identity)))
            and self.archive_origins == tuple(sorted(set(self.archive_origins)))
            and all(_valid_origin(item) for item in self.archive_origins)
        ):
            raise ValueError("Channel Trust Policy keys/origins 必须 canonical。")
        core = self.model_dump(mode="json", exclude={"policy_id", "policy_sha256"})
        digest = _digest(core)
        if not (
            hmac.compare_digest(self.policy_sha256, digest)
            and self.policy_id == f"relchanneltrust_{digest[:24]}"
        ):
            raise ValueError("Channel Trust Policy identity 不一致。")
        return self

    def find(
        self,
        identity: ReleaseChannelSignerIdentity,
    ) -> ReleaseTrustedChannelKey | None:
        return next((item for item in self.keys if item.identity == identity), None)


class ReleaseChannelEntry(_StrictModel):
    schema_version: Literal[1] = 1
    target: str = Field(pattern=_IDENTIFIER_RE)
    version: str = Field(pattern=_IDENTIFIER_RE)
    release_generation: int = Field(ge=1, le=1_000_000_000)
    archive_path: str = Field(min_length=1, max_length=1024)
    archive_name: str = Field(pattern=r"^naumi-[A-Za-z0-9._-]+\.(?:tar\.gz|zip)$")
    archive_sha256: str = Field(pattern=_SHA256_RE)
    archive_size_bytes: int = Field(gt=0, le=8 * 1024 * 1024 * 1024)
    manifest_sha256: str = Field(pattern=_SHA256_RE)
    build_attestation: ReleaseBuildAttestation

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if not _valid_archive_path(self.archive_path, self.archive_name):
            raise ValueError("Channel Entry archive path 不是安全相对路径。")
        payload = self.build_attestation.payload
        if not (
            payload.target == self.target
            and payload.version == self.version
            and payload.archive_name == self.archive_name
            and payload.archive_sha256 == self.archive_sha256
            and payload.manifest_sha256 == self.manifest_sha256
        ):
            raise ValueError("Channel Entry 与 Build Attestation projection 不一致。")
        return self


class ReleaseChannelCatalogPayload(_StrictModel):
    schema_version: Literal[1] = 1
    domain: Literal[
        "naumi.release.channel-catalog.v1"
    ] = RELEASE_CHANNEL_CATALOG_DOMAIN
    product: Literal["NaumiAgent"] = "NaumiAgent"
    signer: ReleaseChannelSignerIdentity
    channel: str = Field(pattern=_CHANNEL_RE)
    sequence: int = Field(ge=1, le=1_000_000)
    previous_catalog_id: str = Field(
        default="",
        pattern=r"^(?:|relchannelcatalog_[0-9a-f]{24})$",
    )
    previous_catalog_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    entries: tuple[ReleaseChannelEntry, ...] = Field(
        min_length=1,
        max_length=_MAX_ENTRIES,
    )
    generated_at: str = Field(min_length=1, max_length=100)
    valid_from: str = Field(min_length=1, max_length=100)
    expires_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        targets = tuple(item.target for item in self.entries)
        if not (
            targets == tuple(sorted(set(targets)))
            and (self.sequence == 1)
            is (not self.previous_catalog_id and not self.previous_catalog_sha256)
        ):
            raise ValueError("Channel Catalog target/chain projection 不一致。")
        generated = _aware(self.generated_at)
        valid_from = _aware(self.valid_from)
        expires = _aware(self.expires_at)
        if not generated <= valid_from < expires:
            raise ValueError("Channel Catalog 时间窗口无效。")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.model_dump(mode="json"))


class ReleaseChannelCatalog(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "naumi-release-channel-catalog-v1"
    ] = RELEASE_CHANNEL_CATALOG_POLICY
    catalog_id: str = Field(pattern=r"^relchannelcatalog_[0-9a-f]{24}$")
    catalog_sha256: str = Field(pattern=_SHA256_RE)
    payload: ReleaseChannelCatalogPayload
    signature_algorithm: Literal["ed25519"] = "ed25519"
    signature_base64: str = Field(min_length=88, max_length=88)
    signature_sha256: str = Field(pattern=_SHA256_RE)
    private_key_persisted: Literal[False] = False
    download_executed: Literal[False] = False
    deployment_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        signature = _decode_base64(
            self.signature_base64,
            expected_bytes=64,
            label="channel catalog signature",
        )
        if not (
            hmac.compare_digest(
                base64.b64encode(signature).decode("ascii"),
                self.signature_base64,
            )
            and hmac.compare_digest(
                hashlib.sha256(signature).hexdigest(),
                self.signature_sha256,
            )
        ):
            raise ValueError("Channel Catalog signature identity 不一致。")
        core = self.model_dump(mode="json", exclude={"catalog_id", "catalog_sha256"})
        digest = _digest(core)
        if not (
            hmac.compare_digest(self.catalog_sha256, digest)
            and self.catalog_id == f"relchannelcatalog_{digest[:24]}"
        ):
            raise ValueError("Channel Catalog identity 不一致。")
        return self


class ReleaseChannelCatalogView(_StrictModel):
    catalog: ReleaseChannelCatalog
    source_current: bool
    latest_for_channel: bool
    channel_trust_current: bool
    builder_trust_current: bool
    valid_now: bool
    invalidation_reasons: tuple[str, ...] = Field(max_length=8)
    catalog_resolution_authority: bool
    download_input_authority: bool
    download_executed: Literal[False] = False
    deployment_authority: Literal[False] = False

    @model_validator(mode="after")
    def _project(self) -> Self:
        current = bool(
            self.source_current
            and self.latest_for_channel
            and self.channel_trust_current
            and self.builder_trust_current
            and self.valid_now
        )
        if not (
            self.catalog_resolution_authority is current
            and self.download_input_authority is current
            and self.invalidation_reasons
            == tuple(sorted(set(self.invalidation_reasons)))
        ):
            raise ValueError("Channel Catalog View authority projection 不一致。")
        return self


class ReleaseChannelResolution(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "naumi-release-channel-resolution-v1"
    ] = RELEASE_CHANNEL_RESOLUTION_POLICY
    resolution_id: str = Field(pattern=r"^relchannelresolve_[0-9a-f]{24}$")
    resolution_sha256: str = Field(pattern=_SHA256_RE)
    catalog_id: str = Field(pattern=r"^relchannelcatalog_[0-9a-f]{24}$")
    catalog_sha256: str = Field(pattern=_SHA256_RE)
    channel_trust_policy_id: str = Field(pattern=r"^relchanneltrust_[0-9a-f]{24}$")
    channel_trust_policy_sha256: str = Field(pattern=_SHA256_RE)
    build_trust_policy_id: str = Field(pattern=r"^relbuildtrust_[0-9a-f]{24}$")
    build_trust_policy_sha256: str = Field(pattern=_SHA256_RE)
    channel: str = Field(pattern=_CHANNEL_RE)
    target: str = Field(pattern=_IDENTIFIER_RE)
    entry: ReleaseChannelEntry
    archive_urls: tuple[str, ...] = Field(min_length=1, max_length=8)
    resolved_at: str = Field(min_length=1, max_length=100)
    catalog_resolution_authority: Literal[True] = True
    download_input_authority: Literal[True] = True
    download_executed: Literal[False] = False
    installation_authority: Literal[False] = False
    deployment_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if not (
            self.target == self.entry.target
            and self.archive_urls == tuple(sorted(set(self.archive_urls)))
            and all(_url_matches_entry(item, self.entry.archive_path) for item in self.archive_urls)
        ):
            raise ValueError("Channel Resolution target/URL projection 不一致。")
        _aware(self.resolved_at)
        core = self.model_dump(mode="json", exclude={"resolution_id", "resolution_sha256"})
        digest = _digest(core)
        if not (
            self.resolution_sha256 == digest
            and self.resolution_id == f"relchannelresolve_{digest[:24]}"
        ):
            raise ValueError("Channel Resolution identity 不一致。")
        return self


@dataclass(frozen=True)
class ReleaseChannelCatalogSigner:
    identity: ReleaseChannelSignerIdentity
    _private_key: Ed25519PrivateKey = field(repr=False)

    @classmethod
    def from_private_key_base64(
        cls,
        *,
        signer_id: str,
        key_id: str,
        key_generation: int,
        private_key_base64: str,
    ) -> ReleaseChannelCatalogSigner:
        private_bytes = _decode_base64(
            private_key_base64,
            expected_bytes=32,
            label="channel signer private key",
        )
        if not hmac.compare_digest(
            base64.b64encode(private_bytes).decode("ascii"),
            private_key_base64,
        ):
            raise ReleaseChannelCatalogError(
                "release_channel_private_key_noncanonical",
                "Channel signer private key 必须使用 canonical Base64。",
            )
        private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
        public_key = private_key.public_key().public_bytes_raw()
        identity = ReleaseChannelSignerIdentity(
            signer_id=signer_id,
            key_id=key_id,
            key_generation=key_generation,
            public_key_base64=base64.b64encode(public_key).decode("ascii"),
            public_key_sha256=hashlib.sha256(public_key).hexdigest(),
        )
        return cls(identity=identity, _private_key=private_key)

    def issue(
        self,
        *,
        channel: str,
        entries: tuple[ReleaseChannelEntry, ...],
        previous: ReleaseChannelCatalog | None,
        generated_at: str,
        valid_from: str,
        expires_at: str,
    ) -> ReleaseChannelCatalog:
        payload = ReleaseChannelCatalogPayload(
            signer=self.identity,
            channel=channel,
            sequence=1 if previous is None else previous.payload.sequence + 1,
            previous_catalog_id="" if previous is None else previous.catalog_id,
            previous_catalog_sha256=("" if previous is None else previous.catalog_sha256),
            entries=tuple(sorted(entries, key=lambda item: item.target)),
            generated_at=_aware(generated_at).isoformat(),
            valid_from=_aware(valid_from).isoformat(),
            expires_at=_aware(expires_at).isoformat(),
        )
        signature = self._private_key.sign(payload.canonical_bytes())
        core = {
            "schema_version": 1,
            "policy_version": RELEASE_CHANNEL_CATALOG_POLICY,
            "payload": payload.model_dump(mode="json"),
            "signature_algorithm": "ed25519",
            "signature_base64": base64.b64encode(signature).decode("ascii"),
            "signature_sha256": hashlib.sha256(signature).hexdigest(),
            "private_key_persisted": False,
            "download_executed": False,
            "deployment_authority": False,
        }
        digest = _digest(core)
        return ReleaseChannelCatalog.model_validate(
            {
                **core,
                "payload": payload,
                "catalog_id": f"relchannelcatalog_{digest[:24]}",
                "catalog_sha256": digest,
            }
        )


def create_release_channel_trust_policy(
    keys: tuple[ReleaseTrustedChannelKey, ...],
    *,
    archive_origins: tuple[str, ...],
) -> ReleaseChannelTrustPolicyDocument:
    ordered_keys = tuple(sorted(keys, key=lambda item: _identity_key(item.identity)))
    origins = tuple(sorted(set(archive_origins)))
    core = {
        "schema_version": 1,
        "policy_version": RELEASE_CHANNEL_TRUST_POLICY,
        "keys": [item.model_dump(mode="json") for item in ordered_keys],
        "archive_origins": list(origins),
    }
    digest = _digest(core)
    return ReleaseChannelTrustPolicyDocument.model_validate(
        {
            **core,
            "policy_id": f"relchanneltrust_{digest[:24]}",
            "policy_sha256": digest,
        }
    )


class ReleaseChannelCatalogStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        channel_trust_policy_provider: Callable[[], ReleaseChannelTrustPolicyDocument],
        build_trust_policy_provider: Callable[[], ReleaseBuildTrustPolicyDocument],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(channel_trust_policy_provider) or not callable(
            build_trust_policy_provider
        ):
            raise TypeError("Channel Catalog Store 需要两个独立 Trust Policy provider。")
        self.db_path = Path(db_path).expanduser().resolve()
        self.channel_trust_policy_provider = channel_trust_policy_provider
        self.build_trust_policy_provider = build_trust_policy_provider
        self.clock = clock or (lambda: datetime.now(UTC))
        self._locks: dict[str, asyncio.Lock] = {}

    async def record(self, catalog: ReleaseChannelCatalog) -> ReleaseChannelCatalogView:
        item = _validated_catalog(catalog)
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_CATALOG_BYTES:
            raise ReleaseChannelCatalogError(
                "release_channel_catalog_oversized",
                "Release Channel Catalog 超过 4 MiB。",
            )
        self._verify(item)
        lock = self._locks.setdefault(item.payload.channel, asyncio.Lock())
        async with lock:
            await self._record_locked(item, encoded)
        return await self.inspect(catalog_id=item.catalog_id)

    async def _record_locked(self, item, encoded) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                self._verify(item)
                existing = await (
                    await db.execute(
                        "SELECT catalog_json FROM release_channel_catalogs "
                        "WHERE catalog_id = ?",
                        (item.catalog_id,),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _restore_catalog(existing["catalog_json"])
                    await db.rollback()
                    if restored != item:
                        raise ReleaseChannelCatalogError(
                            "release_channel_catalog_identity_conflict",
                            "同一 Channel Catalog ID 已绑定不同内容。",
                        )
                    return
                await self._require_next_link(db, item)
                await db.execute(
                    "INSERT INTO release_channel_catalogs "
                    "(catalog_id, catalog_sha256, channel, sequence, catalog_json, "
                    "generated_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.catalog_id,
                        item.catalog_sha256,
                        item.payload.channel,
                        item.payload.sequence,
                        encoded,
                        item.payload.generated_at,
                        item.payload.expires_at,
                    ),
                )
                await db.commit()
        except ReleaseChannelCatalogError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise ReleaseChannelCatalogError(
                "release_channel_catalog_store_error",
                "Release Channel Catalog 无法持久化。",
            ) from exc

    async def _require_next_link(self, db, item) -> None:
        row = await (
            await db.execute(
                "SELECT catalog_json FROM release_channel_catalogs "
                "WHERE channel = ? ORDER BY sequence DESC LIMIT 1",
                (item.payload.channel,),
            )
        ).fetchone()
        previous = None if row is None else _restore_catalog(row["catalog_json"])
        expected = (
            (1, "", "")
            if previous is None
            else (
                previous.payload.sequence + 1,
                previous.catalog_id,
                previous.catalog_sha256,
            )
        )
        actual = (
            item.payload.sequence,
            item.payload.previous_catalog_id,
            item.payload.previous_catalog_sha256,
        )
        if actual != expected:
            raise ReleaseChannelCatalogError(
                "release_channel_catalog_chain_conflict",
                "Channel Catalog 不是当前 channel 的 exact next link。",
            )
        if previous is not None:
            prior_generations = {
                entry.target: entry.release_generation
                for entry in previous.payload.entries
            }
            if any(
                entry.target in prior_generations
                and entry.release_generation <= prior_generations[entry.target]
                for entry in item.payload.entries
            ):
                raise ReleaseChannelCatalogError(
                    "release_channel_generation_rollback",
                    "Channel Catalog 不得降低或复用 target release generation。",
                )

    async def get(self, catalog_id: str) -> ReleaseChannelCatalog | None:
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT catalog_json FROM release_channel_catalogs "
                        "WHERE catalog_id = ?",
                        (catalog_id,),
                    )
                ).fetchone()
            return None if row is None else _restore_catalog(row["catalog_json"])
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise ReleaseChannelCatalogError(
                "release_channel_catalog_source_invalid",
                "Release Channel Catalog durable source 无效。",
            ) from exc

    async def latest(self, channel: str) -> ReleaseChannelCatalog | None:
        if not _valid_channel(channel) or not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT catalog_json FROM release_channel_catalogs "
                        "WHERE channel = ? ORDER BY sequence DESC LIMIT 1",
                        (channel,),
                    )
                ).fetchone()
            return None if row is None else _restore_catalog(row["catalog_json"])
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise ReleaseChannelCatalogError(
                "release_channel_catalog_source_invalid",
                "Release Channel Catalog durable source 无效。",
            ) from exc

    async def inspect(self, *, catalog_id: str) -> ReleaseChannelCatalogView:
        reasons: list[str] = []
        try:
            catalog = await self.get(catalog_id)
            if catalog is None:
                raise ReleaseChannelCatalogError(
                    "release_channel_catalog_missing",
                    "指定的 Release Channel Catalog 不存在。",
                )
            source_current = True
        except ReleaseChannelCatalogError as exc:
            if exc.code == "release_channel_catalog_missing":
                raise
            raise
        latest = await self.latest(catalog.payload.channel)
        latest_for_channel = latest == catalog
        if not latest_for_channel:
            reasons.append("newer_catalog_available")
        channel_trust_current, builder_trust_current = self._trust_state(catalog)
        if not channel_trust_current:
            reasons.append("channel_trust_changed")
        if not builder_trust_current:
            reasons.append("builder_trust_changed")
        now = _aware(self.clock())
        valid_now = _aware(catalog.payload.valid_from) <= now < _aware(
            catalog.payload.expires_at
        )
        if not valid_now:
            reasons.append("catalog_outside_validity")
        current = bool(
            source_current
            and latest_for_channel
            and channel_trust_current
            and builder_trust_current
            and valid_now
        )
        return ReleaseChannelCatalogView(
            catalog=catalog,
            source_current=source_current,
            latest_for_channel=latest_for_channel,
            channel_trust_current=channel_trust_current,
            builder_trust_current=builder_trust_current,
            valid_now=valid_now,
            invalidation_reasons=tuple(sorted(set(reasons))),
            catalog_resolution_authority=current,
            download_input_authority=current,
        )

    async def resolve(
        self,
        *,
        channel: str,
        target: str,
    ) -> ReleaseChannelResolution:
        catalog = await self.latest(channel)
        if catalog is None:
            raise ReleaseChannelCatalogError(
                "release_channel_catalog_missing",
                "指定 channel 尚无 Release Channel Catalog。",
            )
        view = await self.inspect(catalog_id=catalog.catalog_id)
        if not view.catalog_resolution_authority:
            raise ReleaseChannelCatalogError(
                "release_channel_catalog_not_authoritative",
                "当前 Release Channel Catalog 不允许解析更新。",
            )
        entry = next((item for item in catalog.payload.entries if item.target == target), None)
        if entry is None:
            raise ReleaseChannelCatalogError(
                "release_channel_target_missing",
                "当前 Release Channel Catalog 不支持该 target。",
            )
        channel_policy, build_policy = self._policies()
        _verify_channel_signature(catalog, channel_policy)
        _verify_builders(catalog, build_policy)
        if not (
            _aware(catalog.payload.valid_from)
            <= _aware(self.clock())
            < _aware(catalog.payload.expires_at)
        ):
            raise ReleaseChannelCatalogError(
                "release_channel_catalog_not_authoritative",
                "解析期间 Release Channel Catalog 已离开有效窗口。",
            )
        resolution = _build_resolution(
            catalog=catalog,
            entry=entry,
            channel_policy=channel_policy,
            build_policy=build_policy,
            resolved_at=_aware(self.clock()).isoformat(),
        )
        if await self.latest(channel) != catalog:
            raise ReleaseChannelCatalogError(
                "release_channel_catalog_changed",
                "解析期间 Release Channel Catalog 已更新，请重试。",
            )
        return resolution

    def _verify(self, catalog: ReleaseChannelCatalog) -> None:
        channel_policy, build_policy = self._policies()
        _verify_channel_signature(catalog, channel_policy)
        _verify_builders(catalog, build_policy)

    def _policies(
        self,
    ) -> tuple[ReleaseChannelTrustPolicyDocument, ReleaseBuildTrustPolicyDocument]:
        channel_policy = self.channel_trust_policy_provider()
        build_policy = self.build_trust_policy_provider()
        if not (
            isinstance(channel_policy, ReleaseChannelTrustPolicyDocument)
            and isinstance(build_policy, ReleaseBuildTrustPolicyDocument)
        ):
            raise ReleaseChannelCatalogError(
                "release_channel_trust_policy_invalid",
                "Release Channel 或 Build Trust Policy provider 返回无效文档。",
            )
        return channel_policy, build_policy

    def _trust_state(self, catalog: ReleaseChannelCatalog) -> tuple[bool, bool]:
        try:
            channel_policy, build_policy = self._policies()
        except ReleaseChannelCatalogError:
            return False, False
        try:
            _verify_channel_signature(catalog, channel_policy)
            channel_current = True
        except (ReleaseChannelCatalogError, TypeError, ValueError):
            channel_current = False
        try:
            _verify_builders(catalog, build_policy)
            builder_current = True
        except (ReleaseChannelCatalogError, TypeError, ValueError):
            builder_current = False
        return channel_current, builder_current


def _verify_channel_signature(
    catalog: ReleaseChannelCatalog,
    policy: ReleaseChannelTrustPolicyDocument,
) -> ReleaseTrustedChannelKey:
    key = policy.find(catalog.payload.signer)
    if key is None:
        raise ReleaseChannelCatalogError(
            "release_channel_signer_untrusted",
            "Channel signer key 不在当前 Trust Policy 中。",
        )
    generated = _aware(catalog.payload.generated_at)
    valid_from = _aware(key.valid_from)
    valid_until = _aware(key.valid_until) if key.valid_until else None
    if key.state != "active":
        raise ReleaseChannelCatalogError(
            "release_channel_signer_revoked",
            "Channel signer key 已撤销。",
        )
    if catalog.payload.channel not in key.channels:
        raise ReleaseChannelCatalogError(
            "release_channel_not_allowed",
            "Channel signer key 未获准签署该 channel。",
        )
    if generated < valid_from or (valid_until is not None and generated >= valid_until):
        raise ReleaseChannelCatalogError(
            "release_channel_signer_outside_validity",
            "Channel Catalog 签署时间不在 key 有效期内。",
        )
    signature = _decode_base64(
        catalog.signature_base64,
        expected_bytes=64,
        label="channel catalog signature",
    )
    public_key = _decode_base64(
        key.identity.public_key_base64,
        expected_bytes=32,
        label="channel signer public key",
    )
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            signature,
            catalog.payload.canonical_bytes(),
        )
    except (InvalidSignature, ValueError) as exc:
        raise ReleaseChannelCatalogError(
            "release_channel_signature_invalid",
            "Channel Catalog Ed25519 signature 无效。",
        ) from exc
    return key


def _build_resolution(
    *,
    catalog: ReleaseChannelCatalog,
    entry: ReleaseChannelEntry,
    channel_policy: ReleaseChannelTrustPolicyDocument,
    build_policy: ReleaseBuildTrustPolicyDocument,
    resolved_at: str,
) -> ReleaseChannelResolution:
    urls = tuple(
        sorted(
            f"{origin}/{quote(entry.archive_path, safe='/')}"
            for origin in channel_policy.archive_origins
        )
    )
    core = {
        "schema_version": 1,
        "policy_version": RELEASE_CHANNEL_RESOLUTION_POLICY,
        "catalog_id": catalog.catalog_id,
        "catalog_sha256": catalog.catalog_sha256,
        "channel_trust_policy_id": channel_policy.policy_id,
        "channel_trust_policy_sha256": channel_policy.policy_sha256,
        "build_trust_policy_id": build_policy.policy_id,
        "build_trust_policy_sha256": build_policy.policy_sha256,
        "channel": catalog.payload.channel,
        "target": entry.target,
        "entry": entry.model_dump(mode="json"),
        "archive_urls": urls,
        "resolved_at": resolved_at,
        "catalog_resolution_authority": True,
        "download_input_authority": True,
        "download_executed": False,
        "installation_authority": False,
        "deployment_authority": False,
    }
    digest = _digest(core)
    return ReleaseChannelResolution.model_validate(
        {
            **core,
            "entry": entry,
            "resolution_id": f"relchannelresolve_{digest[:24]}",
            "resolution_sha256": digest,
        }
    )


def _verify_builders(
    catalog: ReleaseChannelCatalog,
    policy: ReleaseBuildTrustPolicyDocument,
) -> None:
    try:
        for entry in catalog.payload.entries:
            verify_release_build_attestation_signature(
                entry.build_attestation,
                trust_policy=policy,
            )
    except ReleaseBuildAttestationError as exc:
        raise ReleaseChannelCatalogError(
            "release_channel_builder_untrusted",
            "Channel Catalog 含不受信 Build Attestation。",
        ) from exc


def _valid_channel(value: str) -> bool:
    return bool(
        value
        and len(value) <= 64
        and "a" <= value[0] <= "z"
        and all(
            char.isascii()
            and (char.islower() or char.isdigit() or char in "._-")
            for char in value
        )
    )


def _valid_origin(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and not parsed.username
        and not parsed.password
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
        and value == value.rstrip("/")
    )


def _valid_archive_path(value: str, archive_name: str) -> bool:
    path = PurePosixPath(value)
    return bool(
        value == path.as_posix()
        and not path.is_absolute()
        and len(path.parts) >= 2
        and all(part not in {"", ".", ".."} for part in path.parts)
        and all(re.fullmatch(r"[A-Za-z0-9._-]+", part) for part in path.parts)
        and path.name == archive_name
        and "\\" not in value
        and "\x00" not in value
        and "\n" not in value
        and "\r" not in value
        and "?" not in value
        and "#" not in value
    )


def _url_matches_entry(value: str, archive_path: str) -> bool:
    parsed = urlsplit(value)
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and not parsed.username
        and not parsed.password
        and parsed.path.endswith(f"/{archive_path}")
        and not parsed.query
        and not parsed.fragment
    )


def _identity_key(identity: ReleaseChannelSignerIdentity) -> tuple[str, int, str]:
    return identity.signer_id, identity.key_generation, identity.key_id


def _validated_catalog(value) -> ReleaseChannelCatalog:
    try:
        return ReleaseChannelCatalog.model_validate_json(value.model_dump_json())
    except (AttributeError, TypeError, ValueError) as exc:
        raise ReleaseChannelCatalogError(
            "release_channel_catalog_invalid",
            "Release Channel Catalog artifact 无效。",
        ) from exc


def _restore_catalog(value: str) -> ReleaseChannelCatalog:
    if len(value.encode()) > _MAX_CATALOG_BYTES:
        raise ValueError("Release Channel Catalog durable source 超过 4 MiB。")
    return ReleaseChannelCatalog.model_validate_json(value)


def _decode_base64(value, *, expected_bytes, label):
    try:
        decoded = base64.b64decode(value, validate=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} 不是 canonical Base64。") from exc
    if len(decoded) != expected_bytes:
        raise ValueError(f"{label} 长度无效。")
    return decoded


def _aware(value) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Release Channel timestamp 必须包含 offset。")
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


async def _ensure_schema(db) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS release_channel_catalogs ("
        "catalog_id TEXT PRIMARY KEY, catalog_sha256 TEXT NOT NULL UNIQUE, "
        "channel TEXT NOT NULL, sequence INTEGER NOT NULL, catalog_json TEXT NOT NULL, "
        "generated_at TEXT NOT NULL, expires_at TEXT NOT NULL, "
        "UNIQUE(channel, sequence))"
    )
    await db.commit()


__all__ = [
    "RELEASE_CHANNEL_CATALOG_DOMAIN",
    "RELEASE_CHANNEL_CATALOG_POLICY",
    "RELEASE_CHANNEL_RESOLUTION_POLICY",
    "RELEASE_CHANNEL_TRUST_POLICY",
    "ReleaseChannelCatalog",
    "ReleaseChannelCatalogError",
    "ReleaseChannelCatalogPayload",
    "ReleaseChannelCatalogSigner",
    "ReleaseChannelCatalogStore",
    "ReleaseChannelCatalogView",
    "ReleaseChannelEntry",
    "ReleaseChannelResolution",
    "ReleaseChannelSignerIdentity",
    "ReleaseChannelTrustPolicyDocument",
    "ReleaseTrustedChannelKey",
    "create_release_channel_trust_policy",
]
