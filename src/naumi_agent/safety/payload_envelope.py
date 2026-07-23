"""Bounded authenticated encryption for durable Runtime payloads."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_SHA256_LENGTH = 64
_KEY_BYTES = 32
_NONCE_BYTES = 12
_MAX_PLAINTEXT_BYTES = 16 * 1024**2
_MAX_AAD_BYTES = 4 * 1024
_MAX_CIPHERTEXT_BYTES = _MAX_PLAINTEXT_BYTES + 16
_KEY_ID_RE = re.compile(r"^runtime-payload-v1:[0-9a-f]{24}$")


class PayloadEnvelopeError(RuntimeError):
    """Raised without exposing key, plaintext, AAD, or cryptographic details."""


@dataclass(frozen=True, slots=True)
class RuntimePayloadKey:
    """Validated secret key with a non-secret rotation identity."""

    key_bytes: bytes = field(repr=False)
    key_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.key_bytes, bytes) or len(self.key_bytes) != _KEY_BYTES:
            raise ValueError("Runtime payload key 必须为 32 bytes。")
        expected = _key_id(self.key_bytes)
        if not hmac.compare_digest(self.key_id, expected):
            raise ValueError("Runtime payload key identity 校验失败。")

    @classmethod
    def from_bytes(cls, value: bytes) -> RuntimePayloadKey:
        if not isinstance(value, bytes) or len(value) != _KEY_BYTES:
            raise ValueError("Runtime payload key 必须为 32 bytes。")
        return cls(key_bytes=value, key_id=_key_id(value))


@dataclass(frozen=True, slots=True)
class PayloadEnvelope:
    """Serializable AES-256-GCM envelope; ciphertext is omitted from repr."""

    schema_version: int
    algorithm: str
    key_id: str
    nonce_base64: str
    ciphertext_base64: str = field(repr=False)
    plaintext_bytes: int
    aad_sha256: str
    envelope_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("Payload envelope schema_version 必须为 1。")
        if self.algorithm != "aes-256-gcm":
            raise ValueError("Payload envelope algorithm 无效。")
        if not isinstance(self.key_id, str) or not _KEY_ID_RE.fullmatch(self.key_id):
            raise ValueError("Payload envelope key_id 无效。")
        nonce = _decode_base64(
            self.nonce_base64,
            field_name="nonce_base64",
            maximum=_NONCE_BYTES,
        )
        if len(nonce) != _NONCE_BYTES:
            raise ValueError("Payload envelope nonce 长度无效。")
        ciphertext = _decode_base64(
            self.ciphertext_base64,
            field_name="ciphertext_base64",
            maximum=_MAX_CIPHERTEXT_BYTES,
        )
        if not 16 <= len(ciphertext) <= _MAX_CIPHERTEXT_BYTES:
            raise ValueError("Payload envelope ciphertext 长度无效。")
        _require_int(
            self.plaintext_bytes,
            field_name="plaintext_bytes",
            minimum=0,
            maximum=_MAX_PLAINTEXT_BYTES,
        )
        if len(ciphertext) != self.plaintext_bytes + 16:
            raise ValueError("Payload envelope ciphertext 与 plaintext 长度不一致。")
        _require_sha256(self.aad_sha256, field_name="aad_sha256")
        _require_sha256(self.envelope_sha256, field_name="envelope_sha256")
        if not hmac.compare_digest(
            self.envelope_sha256,
            _digest(_envelope_payload(self)),
        ):
            raise ValueError("Payload envelope 摘要校验失败。")

    def to_dict(self) -> dict[str, Any]:
        return {
            **_envelope_payload(self),
            "envelope_sha256": self.envelope_sha256,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PayloadEnvelope:
        if not isinstance(value, Mapping):
            raise TypeError("Payload envelope 必须是对象。")
        expected = {
            "schema_version",
            "algorithm",
            "key_id",
            "nonce_base64",
            "ciphertext_base64",
            "plaintext_bytes",
            "aad_sha256",
            "envelope_sha256",
        }
        if set(value) != expected:
            raise ValueError("Payload envelope 字段集合无效。")
        return cls(
            schema_version=value["schema_version"],
            algorithm=value["algorithm"],
            key_id=value["key_id"],
            nonce_base64=value["nonce_base64"],
            ciphertext_base64=value["ciphertext_base64"],
            plaintext_bytes=value["plaintext_bytes"],
            aad_sha256=value["aad_sha256"],
            envelope_sha256=value["envelope_sha256"],
        )


def seal_runtime_payload(
    plaintext: bytes,
    *,
    aad: bytes,
    key: RuntimePayloadKey,
    nonce_factory: Callable[[int], bytes] = os.urandom,
) -> PayloadEnvelope:
    """Encrypt one bounded payload and bind it to exact caller authority bytes."""
    _require_bytes(
        plaintext,
        field_name="plaintext",
        minimum=0,
        maximum=_MAX_PLAINTEXT_BYTES,
    )
    _require_bytes(
        aad,
        field_name="aad",
        minimum=1,
        maximum=_MAX_AAD_BYTES,
    )
    if not isinstance(key, RuntimePayloadKey):
        raise TypeError("key 必须是 RuntimePayloadKey。")
    nonce = nonce_factory(_NONCE_BYTES)
    if not isinstance(nonce, bytes) or len(nonce) != _NONCE_BYTES:
        raise ValueError("Payload envelope nonce factory 必须返回 12 bytes。")
    ciphertext = AESGCM(key.key_bytes).encrypt(nonce, plaintext, aad)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "algorithm": "aes-256-gcm",
        "key_id": key.key_id,
        "nonce_base64": base64.b64encode(nonce).decode("ascii"),
        "ciphertext_base64": base64.b64encode(ciphertext).decode("ascii"),
        "plaintext_bytes": len(plaintext),
        "aad_sha256": hashlib.sha256(aad).hexdigest(),
    }
    return PayloadEnvelope(
        **payload,
        envelope_sha256=_digest(payload),
    )


def open_runtime_payload(
    envelope: PayloadEnvelope,
    *,
    aad: bytes,
    key: RuntimePayloadKey,
) -> bytes:
    """Authenticate and decrypt without returning partial or unverified bytes."""
    if not isinstance(envelope, PayloadEnvelope):
        raise TypeError("envelope 必须是 PayloadEnvelope。")
    _require_bytes(
        aad,
        field_name="aad",
        minimum=1,
        maximum=_MAX_AAD_BYTES,
    )
    if not isinstance(key, RuntimePayloadKey):
        raise TypeError("key 必须是 RuntimePayloadKey。")
    if not hmac.compare_digest(envelope.key_id, key.key_id):
        raise PayloadEnvelopeError("Runtime payload 无法认证或解密。")
    if not hmac.compare_digest(
        envelope.aad_sha256,
        hashlib.sha256(aad).hexdigest(),
    ):
        raise PayloadEnvelopeError("Runtime payload 无法认证或解密。")
    nonce = _decode_base64(
        envelope.nonce_base64,
        field_name="nonce_base64",
        maximum=_NONCE_BYTES,
    )
    ciphertext = _decode_base64(
        envelope.ciphertext_base64,
        field_name="ciphertext_base64",
        maximum=_MAX_CIPHERTEXT_BYTES,
    )
    try:
        plaintext = AESGCM(key.key_bytes).decrypt(nonce, ciphertext, aad)
    except InvalidTag as exc:
        raise PayloadEnvelopeError("Runtime payload 无法认证或解密。") from exc
    if len(plaintext) != envelope.plaintext_bytes:
        raise PayloadEnvelopeError("Runtime payload 完整性校验失败。")
    return plaintext


def _envelope_payload(envelope: PayloadEnvelope) -> dict[str, Any]:
    return {
        "schema_version": envelope.schema_version,
        "algorithm": envelope.algorithm,
        "key_id": envelope.key_id,
        "nonce_base64": envelope.nonce_base64,
        "ciphertext_base64": envelope.ciphertext_base64,
        "plaintext_bytes": envelope.plaintext_bytes,
        "aad_sha256": envelope.aad_sha256,
    }


def _key_id(value: bytes) -> str:
    return f"runtime-payload-v1:{hashlib.sha256(value).hexdigest()[:24]}"


def _digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _decode_base64(value: Any, *, field_name: str, maximum: int) -> bytes:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} 必须是 Base64 字符串。")
    if len(value) > 4 * ((maximum + 2) // 3):
        raise ValueError(f"{field_name} 超过上限。")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{field_name} 格式无效。") from exc
    if base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError(f"{field_name} 不是 canonical Base64。")
    return decoded


def _require_sha256(value: Any, *, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_LENGTH
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{field_name} 必须是小写 SHA-256。")


def _require_int(
    value: Any,
    *,
    field_name: str,
    minimum: int,
    maximum: int,
) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} 必须是整数。")
    if not minimum <= value <= maximum:
        raise ValueError(f"{field_name} 必须在 {minimum} 到 {maximum} 之间。")


def _require_bytes(
    value: Any,
    *,
    field_name: str,
    minimum: int,
    maximum: int,
) -> None:
    if not isinstance(value, bytes):
        raise TypeError(f"{field_name} 必须是 bytes。")
    if not minimum <= len(value) <= maximum:
        raise ValueError(f"{field_name} 长度必须在 {minimum} 到 {maximum} bytes 之间。")


__all__ = [
    "PayloadEnvelope",
    "PayloadEnvelopeError",
    "RuntimePayloadKey",
    "open_runtime_payload",
    "seal_runtime_payload",
]
