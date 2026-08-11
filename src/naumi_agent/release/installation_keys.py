"""Explicit OS-keyring provisioning for managed installation identities."""

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

from naumi_agent.release.population_registry import (
    ReleaseManagedInstallationCredential,
)

RELEASE_INSTALLATION_KEY_POLICY = "release-installation-key-v1"
RELEASE_INSTALLATION_SIGNATURE_DOMAIN = (
    "naumi.release.stable-remote-readiness-probe.v1"
)
RELEASE_INSTALLATION_FINALIZATION_SIGNATURE_DOMAIN = (
    "naumi.release.stable-remote-finalization-result.v1"
)
RELEASE_INSTALLATION_FINALIZATION_DELIVERY_ACK_SIGNATURE_DOMAIN = (
    "naumi.release.stable-remote-finalization-delivery-ack.v1"
)
_SERVICE_NAME = "NaumiAgent"
_CHANNEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_MAX_METADATA_BYTES = 64 * 1024
_MAX_SIGNED_PAYLOAD_BYTES = 64 * 1024


class InstallationCredentialBackend(Protocol):
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


class ReleaseInstallationKeyHandle(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["release-installation-key-v1"] = (
        RELEASE_INSTALLATION_KEY_POLICY
    )
    key_id: str = Field(pattern=r"^relinstallkey_[0-9a-f]{24}$")
    key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    channel: str = Field(min_length=1, max_length=64)
    release_root_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    public_key_base64: str = Field(pattern=r"^[A-Za-z0-9+/]{43}=$")
    public_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    keyring_service: Literal["NaumiAgent"] = _SERVICE_NAME
    keyring_account: str = Field(min_length=1, max_length=255)
    private_key_stored_in_os_keyring: Literal[True] = True
    private_key_written_to_metadata: Literal[False] = False
    private_key_returned_to_caller: Literal[False] = False
    raw_machine_identifier_collected: Literal[False] = False
    raw_user_identifier_collected: Literal[False] = False
    raw_path_written_to_metadata: Literal[False] = False
    generated_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if _CHANNEL_RE.fullmatch(self.channel) is None:
            raise ValueError("installation key channel 格式无效。")
        public = _decode_canonical_base64(
            self.public_key_base64,
            expected_bytes=32,
            label="installation public key",
        )
        expected_account = _account(
            channel=self.channel,
            release_root_sha256=self.release_root_sha256,
            public_key_sha256=self.public_key_sha256,
        )
        if not (
            hashlib.sha256(public).hexdigest() == self.public_key_sha256
            and self.keyring_account == expected_account
            and _aware(self.generated_at)
        ):
            raise ValueError("installation key public identity 不一致。")
        digest = _digest(
            self.model_dump(mode="json", exclude={"key_id", "key_sha256"})
        )
        if self.key_sha256 != digest or self.key_id != f"relinstallkey_{digest[:24]}":
            raise ValueError("installation key handle identity 不一致。")
        return self


class ReleaseInstallationSignature(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["release-installation-key-v1"] = (
        RELEASE_INSTALLATION_KEY_POLICY
    )
    signature_id: str = Field(pattern=r"^relinstallsig_[0-9a-f]{24}$")
    signature_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    domain: Literal[
        "naumi.release.stable-remote-readiness-probe.v1",
        "naumi.release.stable-remote-finalization-result.v1",
        "naumi.release.stable-remote-finalization-delivery-ack.v1",
    ] = (
        RELEASE_INSTALLATION_SIGNATURE_DOMAIN
    )
    key_id: str = Field(pattern=r"^relinstallkey_[0-9a-f]{24}$")
    key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    credential_id: str = Field(pattern=r"^relpopcred_[0-9a-f]{24}$")
    credential_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    installation_member_id: str = Field(pattern=r"^relpopmember_[0-9a-f]{24}$")
    public_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload_bytes: int = Field(ge=1, le=_MAX_SIGNED_PAYLOAD_BYTES)
    signature_base64: str = Field(pattern=r"^[A-Za-z0-9+/]{86}==$")
    signature_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    signed_at: str = Field(min_length=1, max_length=100)
    private_key_exposed: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        signature = _decode_canonical_base64(
            self.signature_base64,
            expected_bytes=64,
            label="installation signature",
        )
        if not (
            hashlib.sha256(signature).hexdigest() == self.signature_sha256
            and _aware(self.signed_at)
        ):
            raise ValueError("installation signature identity 无效。")
        digest = _digest(
            self.model_dump(
                mode="json",
                exclude={"signature_id", "signature_artifact_sha256"},
            )
        )
        if self.signature_artifact_sha256 != digest or self.signature_id != (
            f"relinstallsig_{digest[:24]}"
        ):
            raise ValueError("installation signature artifact identity 不一致。")
        return self


class ReleaseInstallationKeyError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ReleaseInstallationKeyService:
    """Provision once, inspect publicly, and sign only enumerated protocol domains."""

    def __init__(
        self,
        release_root: str | Path,
        *,
        backend: InstallationCredentialBackend | None = None,
        key_factory: Callable[[int], bytes] = secrets.token_bytes,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.release_root = Path(release_root).expanduser().resolve()
        self.metadata_path = self.release_root / "trust" / "installation-key.json"
        self.lock_path = self.release_root / "state" / "installation-key.lock"
        self._backend_override = backend
        self.key_factory = key_factory
        self.clock = clock
        self._cache_lock = threading.Lock()
        self._cached_key: tuple[str, Ed25519PrivateKey] | None = None

    def inspect(self) -> ReleaseInstallationKeyHandle:
        handle = _read_handle(self.metadata_path)
        if handle is None:
            raise ReleaseInstallationKeyError(
                "release_installation_key_missing",
                "安装身份密钥尚未显式初始化。",
            )
        if handle.release_root_sha256 != _root_sha256(self.release_root):
            raise ReleaseInstallationKeyError(
                "release_installation_key_root_mismatch",
                "安装身份密钥不属于当前 release root。",
            )
        return handle

    def provision(self, *, channel: str = "stable") -> ReleaseInstallationKeyHandle:
        normalized = _channel(channel)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.metadata_path.parent.mkdir(parents=True, exist_ok=True)
        with _exclusive_file_lock(self.lock_path):
            existing = _read_handle(self.metadata_path)
            if existing is not None:
                if existing.channel != normalized:
                    raise ReleaseInstallationKeyError(
                        "release_installation_key_channel_conflict",
                        "当前安装已绑定不同 release channel；必须走显式轮换流程。",
                    )
                self._load_private_key(existing)
                return existing
            seed = self.key_factory(32)
            if not isinstance(seed, bytes) or len(seed) != 32:
                raise ValueError("installation key factory 必须返回 32 bytes。")
            private = Ed25519PrivateKey.from_private_bytes(seed)
            public = private.public_key().public_bytes_raw()
            public_base64 = base64.b64encode(public).decode("ascii")
            public_sha256 = hashlib.sha256(public).hexdigest()
            root_sha256 = _root_sha256(self.release_root)
            core = {
                "schema_version": 1,
                "policy_version": RELEASE_INSTALLATION_KEY_POLICY,
                "channel": normalized,
                "release_root_sha256": root_sha256,
                "public_key_base64": public_base64,
                "public_key_sha256": public_sha256,
                "keyring_service": _SERVICE_NAME,
                "keyring_account": _account(
                    channel=normalized,
                    release_root_sha256=root_sha256,
                    public_key_sha256=public_sha256,
                ),
                "private_key_stored_in_os_keyring": True,
                "private_key_written_to_metadata": False,
                "private_key_returned_to_caller": False,
                "raw_machine_identifier_collected": False,
                "raw_user_identifier_collected": False,
                "raw_path_written_to_metadata": False,
                "generated_at": _aware(self.clock()).isoformat(),
            }
            digest = _digest(core)
            handle = ReleaseInstallationKeyHandle.model_validate({
                **core,
                "key_id": f"relinstallkey_{digest[:24]}",
                "key_sha256": digest,
            })
            backend = self._backend()
            encoded_seed = base64.b64encode(seed).decode("ascii")
            try:
                backend.set_password(
                    handle.keyring_service,
                    handle.keyring_account,
                    encoded_seed,
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
                if isinstance(exc, ReleaseInstallationKeyError):
                    raise
                raise ReleaseInstallationKeyError(
                    "release_installation_key_provision_failed",
                    "无法安全写入安装身份密钥；未返回私钥材料。",
                ) from exc
            with self._cache_lock:
                self._cached_key = (handle.key_id, private)
            return handle

    def sign_remote_readiness_probe(
        self,
        *,
        credential: ReleaseManagedInstallationCredential,
        payload: bytes,
    ) -> ReleaseInstallationSignature:
        return self._sign(
            domain=RELEASE_INSTALLATION_SIGNATURE_DOMAIN,
            credential=credential,
            payload=payload,
        )

    def sign_remote_finalization_result(
        self,
        *,
        credential: ReleaseManagedInstallationCredential,
        payload: bytes,
    ) -> ReleaseInstallationSignature:
        return self._sign(
            domain=RELEASE_INSTALLATION_FINALIZATION_SIGNATURE_DOMAIN,
            credential=credential,
            payload=payload,
        )

    def sign_remote_finalization_delivery_ack(
        self,
        *,
        credential: ReleaseManagedInstallationCredential,
        payload: bytes,
    ) -> ReleaseInstallationSignature:
        return self._sign(
            domain=RELEASE_INSTALLATION_FINALIZATION_DELIVERY_ACK_SIGNATURE_DOMAIN,
            credential=credential,
            payload=payload,
        )

    def _sign(
        self,
        *,
        domain: str,
        credential: ReleaseManagedInstallationCredential,
        payload: bytes,
    ) -> ReleaseInstallationSignature:
        if not isinstance(credential, ReleaseManagedInstallationCredential):
            raise TypeError("credential 必须是 managed installation credential。")
        if not isinstance(payload, bytes) or not 1 <= len(payload) <= (
            _MAX_SIGNED_PAYLOAD_BYTES
        ):
            raise ValueError("installation signature payload 必须为 1..65536 bytes。")
        handle = self.inspect()
        if not (
            credential.payload.channel == handle.channel
            and credential.payload.installation_public_key_base64
            == handle.public_key_base64
            and credential.payload.installation_public_key_sha256
            == handle.public_key_sha256
        ):
            raise ReleaseInstallationKeyError(
                "release_installation_credential_mismatch",
                "Population Credential 与当前安装私钥不匹配。",
            )
        private = self._load_private_key(handle)
        signed_at = _aware(self.clock()).isoformat()
        signature = private.sign(installation_signature_message(
            domain=domain,
            key_id=handle.key_id,
            key_sha256=handle.key_sha256,
            credential_id=credential.credential_id,
            credential_sha256=credential.credential_sha256,
            installation_member_id=credential.payload.member_id,
            public_key_sha256=handle.public_key_sha256,
            payload=payload,
            signed_at=signed_at,
        ))
        core = {
            "schema_version": 1,
            "policy_version": RELEASE_INSTALLATION_KEY_POLICY,
            "domain": domain,
            "key_id": handle.key_id,
            "key_sha256": handle.key_sha256,
            "credential_id": credential.credential_id,
            "credential_sha256": credential.credential_sha256,
            "installation_member_id": credential.payload.member_id,
            "public_key_sha256": handle.public_key_sha256,
            "payload_sha256": hashlib.sha256(payload).hexdigest(),
            "payload_bytes": len(payload),
            "signature_base64": base64.b64encode(signature).decode("ascii"),
            "signature_sha256": hashlib.sha256(signature).hexdigest(),
            "signed_at": signed_at,
            "private_key_exposed": False,
        }
        digest = _digest(core)
        return ReleaseInstallationSignature.model_validate({
            **core,
            "signature_id": f"relinstallsig_{digest[:24]}",
            "signature_artifact_sha256": digest,
        })

    def _load_private_key(
        self,
        handle: ReleaseInstallationKeyHandle,
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
                raise ReleaseInstallationKeyError(
                    "release_installation_keyring_unavailable",
                    "无法读取系统安装身份密钥。",
                ) from exc
            if encoded is None:
                raise ReleaseInstallationKeyError(
                    "release_installation_private_key_missing",
                    "系统凭据库中缺少当前安装私钥。",
                )
            try:
                seed = _decode_canonical_base64(
                    encoded,
                    expected_bytes=32,
                    label="installation private key",
                )
                private = Ed25519PrivateKey.from_private_bytes(seed)
            except (TypeError, ValueError) as exc:
                raise ReleaseInstallationKeyError(
                    "release_installation_private_key_invalid",
                    "系统凭据库中的安装私钥格式无效。",
                ) from exc
            public = private.public_key().public_bytes_raw()
            if not (
                base64.b64encode(public).decode("ascii")
                == handle.public_key_base64
                and hashlib.sha256(public).hexdigest() == handle.public_key_sha256
            ):
                raise ReleaseInstallationKeyError(
                    "release_installation_private_key_mismatch",
                    "系统凭据库私钥与公开 installation key handle 不一致。",
                )
            self._cached_key = (handle.key_id, private)
            return private

    def _backend(self) -> InstallationCredentialBackend:
        if self._backend_override is not None:
            return self._backend_override
        try:
            import keyring
        except ImportError as exc:
            raise ReleaseInstallationKeyError(
                "release_installation_keyring_missing",
                "系统凭据组件未安装，请重新安装 NaumiAgent。",
            ) from exc
        return keyring


def installation_signature_message(
    *,
    domain: str = RELEASE_INSTALLATION_SIGNATURE_DOMAIN,
    key_id: str,
    key_sha256: str,
    credential_id: str,
    credential_sha256: str,
    installation_member_id: str,
    public_key_sha256: str,
    payload: bytes,
    signed_at: str,
) -> bytes:
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= (
        _MAX_SIGNED_PAYLOAD_BYTES
    ):
        raise ValueError("installation signature payload 必须为 1..65536 bytes。")
    return _canonical_bytes({
        "domain": _signature_domain(domain),
        "key_id": key_id,
        "key_sha256": key_sha256,
        "credential_id": credential_id,
        "credential_sha256": credential_sha256,
        "installation_member_id": installation_member_id,
        "public_key_sha256": public_key_sha256,
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "payload_bytes": len(payload),
        "signed_at": _aware(signed_at).isoformat(),
    })


def verify_release_installation_signature(
    *,
    credential: ReleaseManagedInstallationCredential,
    payload: bytes,
    artifact: ReleaseInstallationSignature,
    expected_domain: str = RELEASE_INSTALLATION_SIGNATURE_DOMAIN,
) -> None:
    if not (
        artifact.domain == _signature_domain(expected_domain)
        and artifact.credential_id == credential.credential_id
        and artifact.credential_sha256 == credential.credential_sha256
        and artifact.installation_member_id == credential.payload.member_id
        and artifact.public_key_sha256
        == credential.payload.installation_public_key_sha256
        and artifact.payload_sha256 == hashlib.sha256(payload).hexdigest()
        and artifact.payload_bytes == len(payload)
    ):
        raise ReleaseInstallationKeyError(
            "release_installation_signature_binding_mismatch",
            "安装签名与 Credential 或 payload 绑定不一致。",
        )
    try:
        public = _decode_canonical_base64(
            credential.payload.installation_public_key_base64,
            expected_bytes=32,
            label="installation public key",
        )
        signature = _decode_canonical_base64(
            artifact.signature_base64,
            expected_bytes=64,
            label="installation signature",
        )
        Ed25519PublicKey.from_public_bytes(public).verify(
            signature,
            installation_signature_message(
                domain=artifact.domain,
                key_id=artifact.key_id,
                key_sha256=artifact.key_sha256,
                credential_id=artifact.credential_id,
                credential_sha256=artifact.credential_sha256,
                installation_member_id=artifact.installation_member_id,
                public_key_sha256=artifact.public_key_sha256,
                payload=payload,
                signed_at=artifact.signed_at,
            ),
        )
    except (InvalidSignature, TypeError, ValueError) as exc:
        raise ReleaseInstallationKeyError(
            "release_installation_signature_untrusted",
            "安装签名无法由 Population Credential 验证。",
        ) from exc


def _signature_domain(value: str) -> str:
    if value not in {
        RELEASE_INSTALLATION_SIGNATURE_DOMAIN,
        RELEASE_INSTALLATION_FINALIZATION_SIGNATURE_DOMAIN,
        RELEASE_INSTALLATION_FINALIZATION_DELIVERY_ACK_SIGNATURE_DOMAIN,
    }:
        raise ValueError("installation signature domain 无效。")
    return value


def render_release_installation_key(handle: ReleaseInstallationKeyHandle) -> str:
    return "\n".join((
        "## Managed Installation Key",
        "",
        "- 状态：**已初始化**",
        f"- Key：`{handle.key_id}`",
        f"- Channel：`{handle.channel}`",
        f"- Public key SHA-256：`{handle.public_key_sha256}`",
        "- Private key storage：`OS keyring`",
        "- Private key exposed：`false`",
        "- 自动启动读取 keyring：`false`",
    ))


def _read_handle(path: Path) -> ReleaseInstallationKeyHandle | None:
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
        return ReleaseInstallationKeyHandle.model_validate_json(raw)
    except (OSError, TypeError, ValueError) as exc:
        raise ReleaseInstallationKeyError(
            "release_installation_key_metadata_invalid",
            "installation key metadata 损坏、权限过宽或不安全。",
        ) from exc


def _write_handle_once(path: Path, handle: ReleaseInstallationKeyHandle) -> None:
    existing = _read_handle(path)
    if existing is not None:
        if existing != handle:
            raise ReleaseInstallationKeyError(
                "release_installation_key_metadata_conflict",
                "installation key metadata 已绑定不同 identity。",
            )
        return
    encoded = handle.model_dump_json().encode("utf-8")
    if len(encoded) > _MAX_METADATA_BYTES:
        raise ReleaseInstallationKeyError(
            "release_installation_key_metadata_oversized",
            "installation key metadata 超过 64 KiB。",
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


def _remove_handle_if_exact(path: Path, handle: ReleaseInstallationKeyHandle) -> None:
    try:
        if _read_handle(path) == handle:
            path.unlink()
            if os.name != "nt":
                directory = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
    except (OSError, ReleaseInstallationKeyError):
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


def _channel(value: str) -> str:
    normalized = str(value).strip().lower()
    if _CHANNEL_RE.fullmatch(normalized) is None:
        raise ValueError(
            "channel 必须由字母、数字、点、下划线或短横线组成，长度为 1..64。"
        )
    return normalized


def _account(
    *,
    channel: str,
    release_root_sha256: str,
    public_key_sha256: str,
) -> str:
    return (
        f"release.installation.{channel}.{release_root_sha256[:16]}."
        f"{public_key_sha256[:24]}.ed25519-seed.v1"
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
    "RELEASE_INSTALLATION_FINALIZATION_SIGNATURE_DOMAIN",
    "RELEASE_INSTALLATION_FINALIZATION_DELIVERY_ACK_SIGNATURE_DOMAIN",
    "RELEASE_INSTALLATION_KEY_POLICY",
    "RELEASE_INSTALLATION_SIGNATURE_DOMAIN",
    "InstallationCredentialBackend",
    "ReleaseInstallationKeyError",
    "ReleaseInstallationKeyHandle",
    "ReleaseInstallationKeyService",
    "ReleaseInstallationSignature",
    "installation_signature_message",
    "render_release_installation_key",
    "verify_release_installation_signature",
]
