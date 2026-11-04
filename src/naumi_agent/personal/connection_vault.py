"""Owner-bound AEAD envelopes backed only by dedicated OS credential-store keys."""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import math
import re
import secrets
import sys
import threading
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from naumi_agent.personal.models import PersonalAgentError

KEYRING_SERVICE = "NaumiAgent.PersonalConnections.v1"
MAX_VALUE_BYTES = 1024 * 1024
MAX_ENVELOPE_BYTES = ((MAX_VALUE_BYTES + 18) // 3) * 4 + 1024
_MAX_DEPTH = 32
_MAX_NODES = 100_000
_OWNER = re.compile(r"[0-9a-f]{64}")
_SLOT = re.compile(r"(?:conn_[0-9a-f]{32}|oauth_[0-9a-f]{32}|client_[0-9a-f]{64})")
_PURPOSE = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,63}")
_VERSION = 1
_ALGORITHM = "A256GCM"


def _error(code: str) -> PersonalAgentError:
    messages = {
        "invalid_input": "连接凭据格式不正确或超过容量限制",
        "credential_missing": "连接密钥已缺失，请重新授权该连接",
        "credential_error": "系统凭据库暂不可用，请检查系统凭据服务后重试",
        "vault_corrupt": "连接凭据校验未通过，请重新授权该连接",
    }
    return PersonalAgentError(code, messages[code])


def _identity(owner_key: Any, slot: Any) -> str:
    if (
        type(owner_key) is not str
        or _OWNER.fullmatch(owner_key) is None
        or type(slot) is not str
        or _SLOT.fullmatch(slot) is None
    ):
        raise _error("invalid_input")
    return owner_key + "/" + slot


def _aad(owner_key: Any, slot: Any, purpose: Any) -> tuple[str, bytes]:
    account = _identity(owner_key, slot)
    if type(purpose) is not str or _PURPOSE.fullmatch(purpose) is None:
        raise _error("invalid_input")
    value = {
        "v": _VERSION,
        "alg": _ALGORITHM,
        "owner": owner_key,
        "slot": slot,
        "purpose": purpose,
    }
    return account, json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


def _json_bytes(value: Any, *, code: str) -> bytes:
    """Reject non-JSON types, nonfinite numbers, cycles and unbounded nesting."""
    nodes = 0

    def visit(current: Any, depth: int) -> Any:
        nonlocal nodes
        nodes += 1
        if depth > _MAX_DEPTH or nodes > _MAX_NODES:
            raise _error(code)
        kind = type(current)
        if current is None or kind in {bool, int}:
            return current
        if kind is float:
            if not math.isfinite(current):
                raise _error(code)
            return current
        if kind is str:
            if len(current) > MAX_VALUE_BYTES:
                raise _error(code)
            return current
        if kind is list:
            snapshot = current.copy()
            if len(snapshot) > _MAX_NODES:
                raise _error(code)
            return [visit(entry, depth + 1) for entry in snapshot]
        if kind is dict:
            snapshot = current.copy()
            if len(snapshot) > _MAX_NODES:
                raise _error(code)
            result = {}
            for key, entry in snapshot.items():
                if type(key) is not str or len(key) > MAX_VALUE_BYTES:
                    raise _error(code)
                visit(key, depth + 1)
                result[key] = visit(entry, depth + 1)
            return result
        raise _error(code)

    if type(value) is not dict:
        raise _error(code)
    detached = visit(value, 0)
    failed = False
    chunks = []
    total = 0
    try:
        encoder = json.JSONEncoder(
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        for chunk in encoder.iterencode(detached):
            encoded = chunk.encode("utf-8")
            total += len(encoded)
            if total > MAX_VALUE_BYTES:
                raise _error(code)
            chunks.append(encoded)
    except (UnicodeError, ValueError, TypeError, OverflowError, RecursionError):
        failed = True
    if failed:
        raise _error(code)
    return b"".join(chunks)


def _strict_loads(raw: str | bytes, *, code: str) -> dict:
    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise _error(code)
            result[key] = value
        return result

    def constant(_value):
        raise _error(code)

    failed = False
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        failed = True
    if failed or type(value) is not dict:
        raise _error(code)
    return value


def _decode_base64(value: Any, *, maximum: int, exact: int | None = None, code: str) -> bytes:
    if type(value) is not str or len(value) > ((maximum + 2) // 3) * 4:
        raise _error(code)
    failed = False
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        failed = True
    if failed:
        raise _error(code)
    if (
        len(decoded) > maximum
        or exact is not None
        and len(decoded) != exact
        or base64.b64encode(decoded).decode("ascii") != value
    ):
        raise _error(code)
    return decoded


def _native_backend():
    """Select a native OS store explicitly, never keyring's configurable file backends."""
    if sys.platform == "win32":
        from keyring.backends.Windows import WinVaultKeyring

        return WinVaultKeyring()
    if sys.platform == "darwin":
        from keyring.backends.macOS import Keyring

        return Keyring()
    if sys.platform.startswith("linux"):
        from keyring.backends.SecretService import Keyring

        return Keyring()
    raise _error("credential_error")


class ConnectionVault:
    """Seal SQLite-stored bundles; provision requires the caller's persistent slot lease.

    The OS keyring contains only one 256-bit key per owner/slot. No plaintext file
    fallback exists. Every seal/open fetches the key anew and retains no key cache.
    """

    def __init__(self, backend=None) -> None:
        self._backend = backend
        self._provision_lock = threading.RLock()

    def __repr__(self) -> str:
        return "ConnectionVault()"

    def _credential_backend(self):
        return _native_backend() if self._backend is None else self._backend

    def _get(self, account: str):
        failed = False
        try:
            result = self._credential_backend().get_password(KEYRING_SERVICE, account)
        except Exception:
            failed = True
        if failed:
            raise _error("credential_error")
        return result

    def _key(self, account: str) -> bytes:
        encoded = self._get(account)
        if encoded is None:
            raise _error("credential_missing")
        return _decode_base64(encoded, maximum=32, exact=32, code="credential_error")

    def provision(self, *, owner_key, slot) -> None:
        """Initialize once under a caller-held persistent lease; never replace a key."""
        account = _identity(owner_key, slot)
        with self._provision_lock:
            existing = self._get(account)
            if existing is not None:
                _decode_base64(existing, maximum=32, exact=32, code="credential_error")
                return
            key = secrets.token_bytes(32)
            encoded = base64.b64encode(key).decode("ascii")
            failed = False
            try:
                self._credential_backend().set_password(KEYRING_SERVICE, account, encoded)
            except Exception:
                failed = True
            if failed:
                raise _error("credential_error")
            # A read-back failure is UNKNOWN, not permission to overwrite or delete.
            observed = _decode_base64(
                self._get(account), maximum=32, exact=32, code="credential_error"
            )
            if not hmac.compare_digest(key, observed):
                raise _error("credential_error")

    def seal(self, *, owner_key, slot, purpose, value: dict) -> str:
        account, aad = _aad(owner_key, slot, purpose)
        plaintext = _json_bytes(value, code="invalid_input")
        key = self._key(account)
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
        return json.dumps(
            {
                "v": _VERSION,
                "alg": _ALGORITHM,
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def open(self, *, owner_key, slot, purpose, envelope: str) -> dict:
        account, aad = _aad(owner_key, slot, purpose)
        if (
            type(envelope) is not str
            or len(envelope) > MAX_ENVELOPE_BYTES
            or not envelope.isascii()
        ):
            raise _error("invalid_input")
        value = _strict_loads(envelope, code="invalid_input")
        if (
            set(value) != {"v", "alg", "nonce", "ciphertext"}
            or type(value["v"]) is not int
            or value["v"] != _VERSION
            or value["alg"] != _ALGORITHM
        ):
            raise _error("invalid_input")
        nonce = _decode_base64(value["nonce"], maximum=12, exact=12, code="invalid_input")
        ciphertext = _decode_base64(
            value["ciphertext"], maximum=MAX_VALUE_BYTES + 16, code="invalid_input"
        )
        if len(ciphertext) < 16:
            raise _error("invalid_input")
        key = self._key(account)
        failed = False
        try:
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, aad)
        except (InvalidTag, ValueError, OverflowError):
            failed = True
        if failed:
            raise _error("vault_corrupt")
        result = _strict_loads(plaintext, code="vault_corrupt")
        _json_bytes(result, code="vault_corrupt")
        return result

    def delete(self, *, owner_key, slot) -> dict:
        """Report confirmed removal, confirmed prior absence, or an OS-store error."""
        account = _identity(owner_key, slot)
        try:
            existing = self._get(account)
        except PersonalAgentError:
            return {"status": "error"}
        if existing is None:
            return {"status": "missing"}
        failed = False
        try:
            self._credential_backend().delete_password(KEYRING_SERVICE, account)
        except Exception:
            failed = True
        if failed:
            return {"status": "error"}
        try:
            remaining = self._get(account)
        except PersonalAgentError:
            return {"status": "error"}
        return {"status": "confirmed" if remaining is None else "error"}
