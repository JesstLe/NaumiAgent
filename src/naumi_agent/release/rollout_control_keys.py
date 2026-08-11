"""Independent rollout-control signing identity and installer-owned trust policy."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import stat
import threading
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, Self

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

RELEASE_ROLLOUT_CONTROL_KEY_POLICY = "release-rollout-control-key-v1"
RELEASE_ROLLOUT_CONTROL_TRUST_POLICY = "release-rollout-control-trust-v1"
RELEASE_ROLLOUT_CONTROL_SIGNATURE_DOMAIN = (
    "naumi.release.stable-rollout-authorization.v1"
)
RELEASE_ROLLOUT_CONTROL_EXECUTION_GRANT_SIGNATURE_DOMAIN = (
    "naumi.release.stable-remote-finalization-execution-grant.v1"
)
_SERVICE_NAME = "NaumiAgent"
_CONTROL_PLANE_RE = re.compile(r"^[a-z][a-z0-9._-]{2,63}$")
_CHANNEL_RE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_MAX_METADATA_BYTES = 64 * 1024
_MAX_TRUST_POLICY_BYTES = 512 * 1024
_MAX_SIGNED_PAYLOAD_BYTES = 256 * 1024


class RolloutControlCredentialBackend(Protocol):
    def set_password(self, service: str, account: str, value: str) -> None: ...

    def get_password(self, service: str, account: str) -> str | None: ...

    def delete_password(self, service: str, account: str) -> None: ...


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class ReleaseRolloutControlSignerIdentity(_StrictModel):
    schema_version: Literal[1] = 1
    control_plane_id: str = Field(pattern=r"^[a-z][a-z0-9._-]{2,63}$")
    key_id: str = Field(pattern=r"^relrolloutkey_[0-9a-f]{24}$")
    key_generation: int = Field(ge=1, le=1_000_000)
    public_key_base64: str = Field(pattern=r"^[A-Za-z0-9+/]{43}=$")
    public_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _exact(self) -> Self:
        public = _decode_canonical_base64(
            self.public_key_base64,
            expected_bytes=32,
            label="rollout control public key",
        )
        if hashlib.sha256(public).hexdigest() != self.public_key_sha256:
            raise ValueError("Rollout Control public identity 不一致。")
        return self


class ReleaseRolloutControlKeyHandle(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["release-rollout-control-key-v1"] = (
        RELEASE_ROLLOUT_CONTROL_KEY_POLICY
    )
    key_id: str = Field(pattern=r"^relrolloutkey_[0-9a-f]{24}$")
    key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    control_plane_id: str = Field(pattern=r"^[a-z][a-z0-9._-]{2,63}$")
    key_generation: int = Field(ge=1, le=1_000_000)
    release_root_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    public_key_base64: str = Field(pattern=r"^[A-Za-z0-9+/]{43}=$")
    public_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    keyring_service: Literal["NaumiAgent"] = _SERVICE_NAME
    keyring_account: str = Field(min_length=1, max_length=255)
    private_key_stored_in_os_keyring: Literal[True] = True
    private_key_written_to_metadata: Literal[False] = False
    private_key_returned_to_caller: Literal[False] = False
    automatic_trust_policy_installation: Literal[False] = False
    raw_path_written_to_metadata: Literal[False] = False
    generated_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        public = _decode_canonical_base64(
            self.public_key_base64,
            expected_bytes=32,
            label="rollout control public key",
        )
        expected_account = _account(
            control_plane_id=self.control_plane_id,
            release_root_sha256=self.release_root_sha256,
            public_key_sha256=self.public_key_sha256,
        )
        if not (
            hashlib.sha256(public).hexdigest() == self.public_key_sha256
            and self.keyring_account == expected_account
            and _aware(self.generated_at)
        ):
            raise ValueError("Rollout Control key public identity 不一致。")
        digest = _digest(
            self.model_dump(mode="json", exclude={"key_id", "key_sha256"})
        )
        if self.key_sha256 != digest or self.key_id != f"relrolloutkey_{digest[:24]}":
            raise ValueError("Rollout Control key handle identity 不一致。")
        return self

    def signer_identity(self) -> ReleaseRolloutControlSignerIdentity:
        return ReleaseRolloutControlSignerIdentity(
            control_plane_id=self.control_plane_id,
            key_id=self.key_id,
            key_generation=self.key_generation,
            public_key_base64=self.public_key_base64,
            public_key_sha256=self.public_key_sha256,
        )


class ReleaseTrustedRolloutControlKey(_StrictModel):
    schema_version: Literal[1] = 1
    identity: ReleaseRolloutControlSignerIdentity
    state: Literal["active", "revoked"]
    channels: tuple[str, ...] = Field(min_length=1, max_length=32)
    valid_from: str = Field(min_length=1, max_length=100)
    valid_until: str | None = Field(default=None, min_length=1, max_length=100)
    revoked_at: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if not (
            self.channels == tuple(sorted(set(self.channels)))
            and all(_channel(item) == item for item in self.channels)
        ):
            raise ValueError("Rollout Control trusted channels 必须唯一且有序。")
        valid_from = _aware(self.valid_from)
        valid_until = (
            None if self.valid_until is None else _aware(self.valid_until)
        )
        revoked_at = None if self.revoked_at is None else _aware(self.revoked_at)
        if valid_until is not None and valid_until <= valid_from:
            raise ValueError("Rollout Control key validity window 无效。")
        if (self.state == "revoked") is (revoked_at is None):
            raise ValueError("Rollout Control key revocation projection 无效。")
        if revoked_at is not None and not (
            valid_from <= revoked_at
            and (valid_until is None or revoked_at <= valid_until)
        ):
            raise ValueError("Rollout Control key revoked_at 超出 validity window。")
        return self


class ReleaseRolloutControlTrustPolicyDocument(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["release-rollout-control-trust-v1"] = (
        RELEASE_ROLLOUT_CONTROL_TRUST_POLICY
    )
    policy_id: str = Field(pattern=r"^relrollouttrust_[0-9a-f]{24}$")
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    keys: tuple[ReleaseTrustedRolloutControlKey, ...] = Field(
        min_length=1,
        max_length=64,
    )

    @model_validator(mode="after")
    def _exact(self) -> Self:
        identities = tuple(_identity_key(item.identity) for item in self.keys)
        generations = tuple(
            (item.identity.control_plane_id, item.identity.key_generation)
            for item in self.keys
        )
        key_ids = tuple(item.identity.key_id for item in self.keys)
        if not (
            identities == tuple(sorted(set(identities)))
            and len(set(generations)) == len(generations)
            and len(set(key_ids)) == len(key_ids)
        ):
            raise ValueError("Rollout Control Trust Policy keys 必须唯一且有序。")
        core = self.model_dump(mode="json", exclude={"policy_id", "policy_sha256"})
        digest = _digest(core)
        if self.policy_sha256 != digest or self.policy_id != (
            f"relrollouttrust_{digest[:24]}"
        ):
            raise ValueError("Rollout Control Trust Policy identity 不一致。")
        return self

    def find(
        self,
        identity: ReleaseRolloutControlSignerIdentity,
    ) -> ReleaseTrustedRolloutControlKey | None:
        return next(
            (item for item in self.keys if item.identity == identity),
            None,
        )


class ReleaseRolloutControlSignature(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["release-rollout-control-key-v1"] = (
        RELEASE_ROLLOUT_CONTROL_KEY_POLICY
    )
    signature_id: str = Field(pattern=r"^relrolloutsig_[0-9a-f]{24}$")
    signature_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    domain: Literal[
        "naumi.release.stable-rollout-authorization.v1",
        "naumi.release.stable-remote-finalization-execution-grant.v1",
    ] = (
        RELEASE_ROLLOUT_CONTROL_SIGNATURE_DOMAIN
    )
    signer: ReleaseRolloutControlSignerIdentity
    channel: str = Field(pattern=r"^[a-z][a-z0-9._-]{0,63}$")
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload_bytes: int = Field(ge=1, le=_MAX_SIGNED_PAYLOAD_BYTES)
    signature_base64: str = Field(pattern=r"^[A-Za-z0-9+/]{86}==$")
    signature_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    signed_at: str = Field(min_length=1, max_length=100)
    private_key_exposed: Literal[False] = False
    rollout_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        signature = _decode_canonical_base64(
            self.signature_base64,
            expected_bytes=64,
            label="rollout control signature",
        )
        if not (
            hashlib.sha256(signature).hexdigest() == self.signature_sha256
            and _aware(self.signed_at)
        ):
            raise ValueError("Rollout Control signature identity 无效。")
        digest = _digest(self.model_dump(
            mode="json",
            exclude={"signature_id", "signature_artifact_sha256"},
        ))
        if self.signature_artifact_sha256 != digest or self.signature_id != (
            f"relrolloutsig_{digest[:24]}"
        ):
            raise ValueError("Rollout Control signature artifact identity 不一致。")
        return self


class ReleaseRolloutControlKeyError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ReleaseRolloutControlKeyService:
    """Explicit control-plane key provisioning with enumerated-domain signing."""

    def __init__(
        self,
        release_root: str | Path,
        *,
        backend: RolloutControlCredentialBackend | None = None,
        key_factory: Callable[[int], bytes] = secrets.token_bytes,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.release_root = Path(release_root).expanduser().resolve()
        self.metadata_path = self.release_root / "trust" / "rollout-control-key.json"
        self.lock_path = self.release_root / "state" / "rollout-control-key.lock"
        self._backend_override = backend
        self.key_factory = key_factory
        self.clock = clock
        self._cache_lock = threading.Lock()
        self._cached_key: tuple[str, Ed25519PrivateKey] | None = None

    def inspect(self) -> ReleaseRolloutControlKeyHandle:
        handle = _read_handle(self.metadata_path)
        if handle is None:
            raise ReleaseRolloutControlKeyError(
                "release_rollout_control_key_missing",
                "Rollout Control signing key 尚未显式初始化。",
            )
        if handle.release_root_sha256 != _root_sha256(self.release_root):
            raise ReleaseRolloutControlKeyError(
                "release_rollout_control_key_root_mismatch",
                "Rollout Control signing key 不属于当前 release root。",
            )
        return handle

    def provision(
        self,
        *,
        control_plane_id: str = "naumi-control-plane",
        key_generation: int = 1,
    ) -> ReleaseRolloutControlKeyHandle:
        normalized_id = _control_plane_id(control_plane_id)
        if not 1 <= int(key_generation) <= 1_000_000:
            raise ValueError("key_generation 必须在 1..1000000。")
        generation = int(key_generation)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.metadata_path.parent.mkdir(parents=True, exist_ok=True)
        with _exclusive_file_lock(self.lock_path):
            existing = _read_handle(self.metadata_path)
            if existing is not None:
                if (
                    existing.control_plane_id != normalized_id
                    or existing.key_generation != generation
                ):
                    raise ReleaseRolloutControlKeyError(
                        "release_rollout_control_key_identity_conflict",
                        "当前 release root 已绑定不同 Rollout Control identity。",
                    )
                self._load_private_key(existing)
                return existing
            seed = self.key_factory(32)
            if not isinstance(seed, bytes) or len(seed) != 32:
                raise ValueError("rollout control key factory 必须返回 32 bytes。")
            private = Ed25519PrivateKey.from_private_bytes(seed)
            public = private.public_key().public_bytes_raw()
            public_base64 = base64.b64encode(public).decode("ascii")
            public_sha256 = hashlib.sha256(public).hexdigest()
            root_sha256 = _root_sha256(self.release_root)
            base = {
                "schema_version": 1,
                "policy_version": RELEASE_ROLLOUT_CONTROL_KEY_POLICY,
                "control_plane_id": normalized_id,
                "key_generation": generation,
                "release_root_sha256": root_sha256,
                "public_key_base64": public_base64,
                "public_key_sha256": public_sha256,
                "keyring_service": _SERVICE_NAME,
                "private_key_stored_in_os_keyring": True,
                "private_key_written_to_metadata": False,
                "private_key_returned_to_caller": False,
                "automatic_trust_policy_installation": False,
                "raw_path_written_to_metadata": False,
                "generated_at": _aware(self.clock()).isoformat(),
            }
            account = _account(
                control_plane_id=normalized_id,
                release_root_sha256=root_sha256,
                public_key_sha256=public_sha256,
            )
            core = {**base, "keyring_account": account}
            digest = _digest(core)
            handle = ReleaseRolloutControlKeyHandle.model_validate({
                **core,
                "key_id": f"relrolloutkey_{digest[:24]}",
                "key_sha256": digest,
            })
            backend = self._backend()
            try:
                backend.set_password(
                    handle.keyring_service,
                    handle.keyring_account,
                    base64.b64encode(seed).decode("ascii"),
                )
                _write_handle_once(self.metadata_path, handle)
            except Exception as exc:
                _remove_handle_if_exact(self.metadata_path, handle)
                try:
                    backend.delete_password(
                        handle.keyring_service,
                        handle.keyring_account,
                    )
                except Exception:
                    pass
                if isinstance(exc, ReleaseRolloutControlKeyError):
                    raise
                raise ReleaseRolloutControlKeyError(
                    "release_rollout_control_key_provision_failed",
                    "无法安全写入 Rollout Control signing key；未返回私钥材料。",
                ) from exc
            with self._cache_lock:
                self._cached_key = (handle.key_id, private)
            return handle

    def sign_authorization(
        self,
        *,
        channel: str,
        payload: bytes,
    ) -> ReleaseRolloutControlSignature:
        return self._sign(
            domain=RELEASE_ROLLOUT_CONTROL_SIGNATURE_DOMAIN,
            channel=channel,
            payload=payload,
        )

    def sign_remote_finalization_execution_grant(
        self,
        *,
        channel: str,
        payload: bytes,
    ) -> ReleaseRolloutControlSignature:
        return self._sign(
            domain=RELEASE_ROLLOUT_CONTROL_EXECUTION_GRANT_SIGNATURE_DOMAIN,
            channel=channel,
            payload=payload,
        )

    def _sign(
        self,
        *,
        domain: str,
        channel: str,
        payload: bytes,
    ) -> ReleaseRolloutControlSignature:
        normalized_channel = _channel(channel)
        _payload(payload)
        handle = self.inspect()
        signed_at = _aware(self.clock()).isoformat()
        signer = handle.signer_identity()
        private = self._load_private_key(handle)
        signature = private.sign(rollout_control_signature_message(
            domain=domain,
            signer=signer,
            channel=normalized_channel,
            payload=payload,
            signed_at=signed_at,
        ))
        core = {
            "schema_version": 1,
            "policy_version": RELEASE_ROLLOUT_CONTROL_KEY_POLICY,
            "domain": domain,
            "signer": signer.model_dump(mode="json"),
            "channel": normalized_channel,
            "payload_sha256": hashlib.sha256(payload).hexdigest(),
            "payload_bytes": len(payload),
            "signature_base64": base64.b64encode(signature).decode("ascii"),
            "signature_sha256": hashlib.sha256(signature).hexdigest(),
            "signed_at": signed_at,
            "private_key_exposed": False,
            "rollout_authority": False,
        }
        digest = _digest(core)
        return ReleaseRolloutControlSignature.model_validate({
            **core,
            "signature_id": f"relrolloutsig_{digest[:24]}",
            "signature_artifact_sha256": digest,
        })

    def _load_private_key(
        self,
        handle: ReleaseRolloutControlKeyHandle,
    ) -> Ed25519PrivateKey:
        with self._cache_lock:
            if self._cached_key is not None and self._cached_key[0] == handle.key_id:
                return self._cached_key[1]
            backend = self._backend()
            try:
                encoded = backend.get_password(
                    handle.keyring_service,
                    handle.keyring_account,
                )
            except Exception as exc:
                raise ReleaseRolloutControlKeyError(
                    "release_rollout_control_keyring_unavailable",
                    "无法读取 Rollout Control signing key。",
                ) from exc
            if encoded is None:
                raise ReleaseRolloutControlKeyError(
                    "release_rollout_control_private_key_missing",
                    "系统凭据库中缺少 Rollout Control signing key。",
                )
            try:
                seed = _decode_canonical_base64(
                    encoded,
                    expected_bytes=32,
                    label="rollout control private key",
                )
                private = Ed25519PrivateKey.from_private_bytes(seed)
            except (TypeError, ValueError) as exc:
                raise ReleaseRolloutControlKeyError(
                    "release_rollout_control_private_key_invalid",
                    "系统凭据库中的 Rollout Control signing key 格式无效。",
                ) from exc
            public = private.public_key().public_bytes_raw()
            if not (
                base64.b64encode(public).decode("ascii")
                == handle.public_key_base64
                and hashlib.sha256(public).hexdigest()
                == handle.public_key_sha256
            ):
                raise ReleaseRolloutControlKeyError(
                    "release_rollout_control_private_key_mismatch",
                    "系统凭据库私钥与 Rollout Control public handle 不一致。",
                )
            self._cached_key = (handle.key_id, private)
            return private

    def _backend(self) -> RolloutControlCredentialBackend:
        if self._backend_override is not None:
            return self._backend_override
        try:
            import keyring
        except ImportError as exc:
            raise ReleaseRolloutControlKeyError(
                "release_rollout_control_keyring_missing",
                "系统凭据组件未安装，请重新安装 NaumiAgent。",
            ) from exc
        return keyring


def create_release_rollout_control_trust_policy(
    keys: tuple[ReleaseTrustedRolloutControlKey, ...],
) -> ReleaseRolloutControlTrustPolicyDocument:
    ordered = tuple(sorted(keys, key=lambda item: _identity_key(item.identity)))
    core = {
        "schema_version": 1,
        "policy_version": RELEASE_ROLLOUT_CONTROL_TRUST_POLICY,
        "keys": [item.model_dump(mode="json") for item in ordered],
    }
    digest = _digest(core)
    return ReleaseRolloutControlTrustPolicyDocument.model_validate({
        **core,
        "policy_id": f"relrollouttrust_{digest[:24]}",
        "policy_sha256": digest,
    })


def load_release_rollout_control_trust_policy(
    path: str | Path,
) -> ReleaseRolloutControlTrustPolicyDocument:
    source = Path(path).expanduser()
    try:
        before = source.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise ReleaseRolloutControlKeyError(
                "release_rollout_control_trust_file_invalid",
                "Rollout Control Trust Policy 必须是普通文件，不能是符号链接。",
            )
        if os.name != "nt" and before.st_mode & 0o022:
            raise ReleaseRolloutControlKeyError(
                "release_rollout_control_trust_permissions_invalid",
                "Rollout Control Trust Policy 不能允许 group/world 写入。",
            )
        if not 1 <= before.st_size <= _MAX_TRUST_POLICY_BYTES:
            raise ReleaseRolloutControlKeyError(
                "release_rollout_control_trust_size_invalid",
                "Rollout Control Trust Policy 为空或超过 512 KiB。",
            )
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(source, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or (
                before.st_dev,
                before.st_ino,
            ) != (opened.st_dev, opened.st_ino):
                raise ReleaseRolloutControlKeyError(
                    "release_rollout_control_trust_changed",
                    "打开期间 Rollout Control Trust Policy identity 发生变化。",
                )
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                encoded = stream.read(_MAX_TRUST_POLICY_BYTES + 1)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        final = source.lstat()
        if (
            stat.S_ISLNK(final.st_mode)
            or not stat.S_ISREG(final.st_mode)
            or len(encoded) != before.st_size
            or (
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
            ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
            ) != (
            final.st_dev,
            final.st_ino,
            final.st_size,
            final.st_mtime_ns,
            final.st_ctime_ns,
            )
        ):
            raise ReleaseRolloutControlKeyError(
                "release_rollout_control_trust_changed",
                "读取期间 Rollout Control Trust Policy 发生变化。",
            )
    except ReleaseRolloutControlKeyError:
        raise
    except OSError as exc:
        raise ReleaseRolloutControlKeyError(
            "release_rollout_control_trust_unreadable",
            "无法读取 Rollout Control Trust Policy。",
        ) from exc
    try:
        return ReleaseRolloutControlTrustPolicyDocument.model_validate_json(encoded)
    except ValueError as exc:
        raise ReleaseRolloutControlKeyError(
            "release_rollout_control_trust_invalid",
            "Rollout Control Trust Policy 不是受支持的 exact artifact。",
        ) from exc


def rollout_control_signature_message(
    *,
    domain: str = RELEASE_ROLLOUT_CONTROL_SIGNATURE_DOMAIN,
    signer: ReleaseRolloutControlSignerIdentity,
    channel: str,
    payload: bytes,
    signed_at: str,
) -> bytes:
    normalized_channel = _channel(channel)
    _payload(payload)
    return _canonical_bytes({
        "domain": _signature_domain(domain),
        "signer": signer.model_dump(mode="json"),
        "channel": normalized_channel,
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "payload_bytes": len(payload),
        "signed_at": _aware(signed_at).isoformat(),
    })


def verify_release_rollout_control_signature(
    *,
    trust_policy: ReleaseRolloutControlTrustPolicyDocument,
    channel: str,
    payload: bytes,
    artifact: ReleaseRolloutControlSignature,
    expected_domain: str = RELEASE_ROLLOUT_CONTROL_SIGNATURE_DOMAIN,
) -> ReleaseTrustedRolloutControlKey:
    normalized_channel = _channel(channel)
    _payload(payload)
    if not (
        artifact.domain == _signature_domain(expected_domain)
        and artifact.channel == normalized_channel
        and artifact.payload_sha256 == hashlib.sha256(payload).hexdigest()
        and artifact.payload_bytes == len(payload)
    ):
        raise ReleaseRolloutControlKeyError(
            "release_rollout_control_signature_binding_mismatch",
            "Rollout Control signature 与 channel 或 payload 绑定不一致。",
        )
    trusted = trust_policy.find(artifact.signer)
    signed_at = _aware(artifact.signed_at)
    if trusted is None:
        raise ReleaseRolloutControlKeyError(
            "release_rollout_control_signer_untrusted",
            "Rollout Control signer 不在 installer-owned Trust Policy 中。",
        )
    if not (
        trusted.state == "active"
        and normalized_channel in trusted.channels
        and _aware(trusted.valid_from) <= signed_at
        and (
            trusted.valid_until is None
            or signed_at < _aware(trusted.valid_until)
        )
        and trusted.revoked_at is None
    ):
        raise ReleaseRolloutControlKeyError(
            "release_rollout_control_signer_not_authorized",
            "Rollout Control signer 在签名时刻或目标 channel 未获授权。",
        )
    try:
        public = _decode_canonical_base64(
            trusted.identity.public_key_base64,
            expected_bytes=32,
            label="rollout control public key",
        )
        signature = _decode_canonical_base64(
            artifact.signature_base64,
            expected_bytes=64,
            label="rollout control signature",
        )
        Ed25519PublicKey.from_public_bytes(public).verify(
            signature,
            rollout_control_signature_message(
                domain=artifact.domain,
                signer=artifact.signer,
                channel=artifact.channel,
                payload=payload,
                signed_at=artifact.signed_at,
            ),
        )
    except (InvalidSignature, TypeError, ValueError) as exc:
        raise ReleaseRolloutControlKeyError(
            "release_rollout_control_signature_untrusted",
            "Rollout Control signature 无法由 trusted public key 验证。",
        ) from exc
    return trusted


def _signature_domain(value: str) -> str:
    if value not in {
        RELEASE_ROLLOUT_CONTROL_SIGNATURE_DOMAIN,
        RELEASE_ROLLOUT_CONTROL_EXECUTION_GRANT_SIGNATURE_DOMAIN,
    }:
        raise ValueError("Rollout Control signature domain 无效。")
    return value


def render_release_rollout_control_key(
    handle: ReleaseRolloutControlKeyHandle,
) -> str:
    return "\n".join((
        "## Rollout Control Signing Key",
        "",
        "- 状态：**已初始化**",
        f"- Control Plane：`{handle.control_plane_id}`",
        f"- Key：`{handle.key_id}`",
        f"- Generation：`{handle.key_generation}`",
        f"- Public key SHA-256：`{handle.public_key_sha256}`",
        "- Private key storage：`OS keyring`",
        "- Private key exposed：`false`",
        "- Trust Policy 自动安装：`false`",
        "- 自动启动读取 keyring：`false`",
    ))


def _read_handle(path: Path) -> ReleaseRolloutControlKeyHandle | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    try:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError("metadata type")
        if info.st_size > _MAX_METADATA_BYTES or info.st_mode & 0o022:
            raise ValueError("metadata bounds or permissions")
        raw = path.read_bytes()
        if len(raw) > _MAX_METADATA_BYTES:
            raise ValueError("metadata bounds")
        return ReleaseRolloutControlKeyHandle.model_validate_json(raw)
    except (OSError, TypeError, ValueError) as exc:
        raise ReleaseRolloutControlKeyError(
            "release_rollout_control_key_metadata_invalid",
            "Rollout Control key metadata 损坏、权限过宽或不安全。",
        ) from exc


def _write_handle_once(
    path: Path,
    handle: ReleaseRolloutControlKeyHandle,
) -> None:
    existing = _read_handle(path)
    if existing is not None:
        if existing != handle:
            raise ReleaseRolloutControlKeyError(
                "release_rollout_control_key_metadata_conflict",
                "Rollout Control key metadata 已绑定不同 identity。",
            )
        return
    encoded = handle.model_dump_json().encode("utf-8")
    if len(encoded) > _MAX_METADATA_BYTES:
        raise ReleaseRolloutControlKeyError(
            "release_rollout_control_key_metadata_oversized",
            "Rollout Control key metadata 超过 64 KiB。",
        )
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _remove_handle_if_exact(
    path: Path,
    handle: ReleaseRolloutControlKeyHandle,
) -> None:
    try:
        if _read_handle(path) == handle:
            path.unlink()
            if os.name != "nt":
                directory = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
    except (OSError, ReleaseRolloutControlKeyError):
        pass


@contextmanager
def _exclusive_file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        os.chmod(path, 0o600)
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _control_plane_id(value: str) -> str:
    normalized = str(value).strip().lower()
    if _CONTROL_PLANE_RE.fullmatch(normalized) is None:
        raise ValueError(
            "control_plane_id 必须以字母开头，只含小写字母、数字、点、"
            "下划线或短横线，长度为 3..64。"
        )
    return normalized


def _channel(value: str) -> str:
    normalized = str(value).strip().lower()
    if _CHANNEL_RE.fullmatch(normalized) is None:
        raise ValueError("channel 格式无效，长度必须为 1..64。")
    return normalized


def _payload(value: bytes) -> bytes:
    if not isinstance(value, bytes) or not 1 <= len(value) <= (
        _MAX_SIGNED_PAYLOAD_BYTES
    ):
        raise ValueError("rollout authorization payload 必须为 1..262144 bytes。")
    return value


def _account(
    *,
    control_plane_id: str,
    release_root_sha256: str,
    public_key_sha256: str,
) -> str:
    return (
        f"release.rollout-control.{control_plane_id}."
        f"{release_root_sha256[:16]}.{public_key_sha256[:24]}.ed25519-seed.v1"
    )


def _identity_key(
    identity: ReleaseRolloutControlSignerIdentity,
) -> tuple[str, int, str]:
    return (
        identity.control_plane_id,
        identity.key_generation,
        identity.key_id,
    )


def _root_sha256(path: Path) -> str:
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()


def _decode_canonical_base64(
    value: str,
    *,
    expected_bytes: int,
    label: str,
) -> bytes:
    decoded = base64.b64decode(value, validate=True)
    if not (
        len(decoded) == expected_bytes
        and base64.b64encode(decoded).decode("ascii") == value
    ):
        raise ValueError(f"{label} 不是 canonical Base64。")
    return decoded


def _aware(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("时间戳必须包含时区。")
    return parsed.astimezone(UTC)


def _canonical_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(payload: object) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


__all__ = [
    "RELEASE_ROLLOUT_CONTROL_EXECUTION_GRANT_SIGNATURE_DOMAIN",
    "RELEASE_ROLLOUT_CONTROL_KEY_POLICY",
    "RELEASE_ROLLOUT_CONTROL_SIGNATURE_DOMAIN",
    "RELEASE_ROLLOUT_CONTROL_TRUST_POLICY",
    "ReleaseRolloutControlKeyError",
    "ReleaseRolloutControlKeyHandle",
    "ReleaseRolloutControlKeyService",
    "ReleaseRolloutControlSignature",
    "ReleaseRolloutControlSignerIdentity",
    "ReleaseRolloutControlTrustPolicyDocument",
    "ReleaseTrustedRolloutControlKey",
    "RolloutControlCredentialBackend",
    "create_release_rollout_control_trust_policy",
    "load_release_rollout_control_trust_policy",
    "render_release_rollout_control_key",
    "rollout_control_signature_message",
    "verify_release_rollout_control_signature",
]
