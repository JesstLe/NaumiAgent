from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace

import pytest

from naumi_agent.safety.payload_envelope import (
    PayloadEnvelope,
    PayloadEnvelopeError,
    RuntimePayloadKey,
    open_runtime_payload,
    seal_runtime_payload,
)


def _key(offset: int = 0) -> RuntimePayloadKey:
    return RuntimePayloadKey.from_bytes(
        bytes((index + offset) % 256 for index in range(32))
    )


def test_payload_round_trip_is_authenticated_bounded_and_low_sensitivity() -> None:
    plaintext = b'{"task":"private prompt","context":"private context"}'
    aad = b"agent-request:" + b"a" * 64
    envelope = seal_runtime_payload(
        plaintext,
        aad=aad,
        key=_key(),
        nonce_factory=lambda size: b"\x07" * size,
    )
    serialized = json.dumps(envelope.to_dict(), sort_keys=True)

    assert envelope.algorithm == "aes-256-gcm"
    assert envelope.plaintext_bytes == len(plaintext)
    assert envelope.aad_sha256 == hashlib.sha256(aad).hexdigest()
    assert envelope.key_id.startswith("runtime-payload-v1:")
    assert hashlib.sha256(plaintext).hexdigest() not in serialized
    assert "private prompt" not in serialized
    assert "private context" not in serialized
    assert "private prompt" not in repr(envelope)
    assert open_runtime_payload(envelope, aad=aad, key=_key()) == plaintext
    assert PayloadEnvelope.from_dict(envelope.to_dict()) == envelope


def test_random_nonce_produces_distinct_ciphertext_for_same_payload() -> None:
    counter = 0

    def nonce(size: int) -> bytes:
        nonlocal counter
        counter += 1
        return bytes([counter]) * size

    first = seal_runtime_payload(b"same", aad=b"request-a", key=_key(), nonce_factory=nonce)
    second = seal_runtime_payload(b"same", aad=b"request-a", key=_key(), nonce_factory=nonce)

    assert first.nonce_base64 != second.nonce_base64
    assert first.ciphertext_base64 != second.ciphertext_base64
    assert first.plaintext_bytes == second.plaintext_bytes


@pytest.mark.parametrize("wrong", ["key", "aad"])
def test_wrong_key_or_aad_fails_without_exposing_sensitive_inputs(wrong: str) -> None:
    plaintext = b"highly-private-agent-payload"
    aad = b"request-authority"
    envelope = seal_runtime_payload(plaintext, aad=aad, key=_key())

    with pytest.raises(PayloadEnvelopeError) as exc_info:
        open_runtime_payload(
            envelope,
            aad=b"other-authority" if wrong == "aad" else aad,
            key=_key(1) if wrong == "key" else _key(),
        )

    message = str(exc_info.value)
    assert "highly-private" not in message
    assert "request-authority" not in message
    assert "InvalidTag" not in message


def test_ciphertext_tampering_fails_even_with_recomputed_public_digest() -> None:
    envelope = seal_runtime_payload(
        b"payload",
        aad=b"request",
        key=_key(),
        nonce_factory=lambda size: b"\x01" * size,
    )
    ciphertext = bytearray(base64.b64decode(envelope.ciphertext_base64))
    ciphertext[0] ^= 1
    changed = base64.b64encode(bytes(ciphertext)).decode("ascii")
    payload = envelope.to_dict()
    payload["ciphertext_base64"] = changed
    payload.pop("envelope_sha256")
    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    tampered = replace(
        envelope,
        ciphertext_base64=changed,
        envelope_sha256=digest,
    )

    with pytest.raises(PayloadEnvelopeError, match="无法认证或解密"):
        open_runtime_payload(tampered, aad=b"request", key=_key())


def test_envelope_rejects_structure_digest_nonce_and_size_abuse() -> None:
    envelope = seal_runtime_payload(b"payload", aad=b"request", key=_key())
    extra = {**envelope.to_dict(), "plaintext": "leak"}
    with pytest.raises(ValueError, match="字段集合"):
        PayloadEnvelope.from_dict(extra)
    with pytest.raises(ValueError, match="摘要校验失败"):
        replace(envelope, envelope_sha256="0" * 64)
    with pytest.raises(ValueError, match="key_id"):
        replace(envelope, key_id="runtime-payload-v1:" + "z" * 24)
    with pytest.raises(ValueError, match="nonce factory"):
        seal_runtime_payload(
            b"payload",
            aad=b"request",
            key=_key(),
            nonce_factory=lambda _size: b"short",
        )
    with pytest.raises(ValueError, match="plaintext 长度"):
        seal_runtime_payload(
            b"x" * (20 * 1024**2 + 1),
            aad=b"request",
            key=_key(),
        )


def test_runtime_key_repr_and_identity_never_expose_key_material() -> None:
    key = _key()

    assert bytes(range(32)).hex() not in repr(key)
    with pytest.raises(ValueError, match="identity"):
        replace(key, key_id="runtime-payload-v1:" + "0" * 24)
