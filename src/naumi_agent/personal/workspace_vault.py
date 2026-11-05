"""Federation-only native credentials and domain-separated AEAD envelopes."""

from __future__ import annotations

import base64
import hmac
import json
import re
import secrets
import threading

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from naumi_agent.personal.connection_vault import (
    MAX_ENVELOPE_BYTES,
    MAX_VALUE_BYTES,
    _native_backend,
)
from naumi_agent.personal.connection_vault import _decode_base64 as _base64
from naumi_agent.personal.connection_vault import _json_bytes as _json
from naumi_agent.personal.connection_vault import _strict_loads as _loads
from naumi_agent.personal.models import PersonalAgentError

KEYRING_SERVICE = "NaumiAgent.PersonalWorkspaces.v1"
_OWNER = re.compile(r"[a-f0-9]{64}")
_SLOT = re.compile(r"(?:wg_|wc_)[a-f0-9]{32}")
_PURPOSE = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,63}")


def _error(code):
    messages = {
        "invalid_input": "工作区凭据格式不正确或超过容量限制",
        "credential_missing": "工作区连接密钥已缺失，请重新连接",
        "credential_error": "系统凭据库暂不可用，请稍后核对原连接",
        "vault_corrupt": "工作区凭据校验未通过，请重新连接",
    }
    return PersonalAgentError(code, messages[code])


def _strict(helper, *args, code, **kwargs):
    failed = False
    try:
        result = helper(*args, code=code, **kwargs)
    except PersonalAgentError:
        failed = True
    if failed:
        raise _error(code)
    return result


def _identity(owner_key, slot):
    if (
        type(owner_key) is not str
        or _OWNER.fullmatch(owner_key) is None
        or type(slot) is not str
        or _SLOT.fullmatch(slot) is None
    ):
        raise _error("invalid_input")
    return owner_key + "/" + slot


def _aad(owner_key, slot, purpose):
    account = _identity(owner_key, slot)
    if type(purpose) is not str or _PURPOSE.fullmatch(purpose) is None:
        raise _error("invalid_input")
    document = {
        "domain": KEYRING_SERVICE,
        "v": 1,
        "alg": "A256GCM",
        "owner": owner_key,
        "slot": slot,
        "purpose": purpose,
    }
    return account, json.dumps(document, sort_keys=True, separators=(",", ":")).encode("ascii")


class WorkspaceVault:
    """Read each key afresh; provisioning requires the caller's durable slot lease.

    Native key selection and bounded strict JSON/base64 primitives are shared with
    the connection vault. Its service, accepted slots, keys and AAD are not shared.
    """

    def __init__(self, backend=None):
        self._backend = backend
        self._provision_lock = threading.RLock()

    def __repr__(self):
        return "WorkspaceVault()"

    def _credential_backend(self):
        failed = False
        try:
            result = _native_backend() if self._backend is None else self._backend
        except Exception:
            failed = True
        if failed:
            raise _error("credential_error")
        return result

    def _get(self, account):
        failed = False
        try:
            result = self._credential_backend().get_password(KEYRING_SERVICE, account)
        except Exception:
            failed = True
        if failed:
            raise _error("credential_error")
        return result

    def _key(self, account):
        encoded = self._get(account)
        if encoded is None:
            raise _error("credential_missing")
        return _strict(_base64, encoded, maximum=32, exact=32, code="credential_error")

    def provision(self, *, owner_key, slot):
        account = _identity(owner_key, slot)
        with self._provision_lock:
            previous = self._get(account)
            if previous is not None:
                _strict(_base64, previous, maximum=32, exact=32, code="credential_error")
                return
            key = secrets.token_bytes(32)
            failed = False
            try:
                self._credential_backend().set_password(
                    KEYRING_SERVICE, account, base64.b64encode(key).decode("ascii")
                )
            except Exception:
                failed = True
            if failed:
                raise _error("credential_error")
            observed = _strict(
                _base64, self._get(account), maximum=32, exact=32, code="credential_error"
            )
            if not hmac.compare_digest(key, observed):
                raise _error("credential_error")

    def seal(self, *, owner_key, slot, purpose, value):
        account, aad = _aad(owner_key, slot, purpose)
        plaintext = _strict(_json, value, code="invalid_input")
        key = self._key(account)
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
        return json.dumps(
            {
                "v": 1,
                "alg": "A256GCM",
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def open(self, *, owner_key, slot, purpose, envelope):
        account, aad = _aad(owner_key, slot, purpose)
        if (
            type(envelope) is not str
            or len(envelope) > MAX_ENVELOPE_BYTES
            or not envelope.isascii()
        ):
            raise _error("invalid_input")
        value = _strict(_loads, envelope, code="invalid_input")
        if (
            set(value) != {"v", "alg", "nonce", "ciphertext"}
            or type(value["v"]) is not int
            or value["v"] != 1
            or value["alg"] != "A256GCM"
        ):
            raise _error("invalid_input")
        nonce = _strict(_base64, value["nonce"], maximum=12, exact=12, code="invalid_input")
        ciphertext = _strict(
            _base64, value["ciphertext"], maximum=MAX_VALUE_BYTES + 16, code="invalid_input"
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
        result = _strict(_loads, plaintext, code="vault_corrupt")
        _strict(_json, result, code="vault_corrupt")
        return result

    def delete(self, *, owner_key, slot):
        account = _identity(owner_key, slot)
        try:
            previous = self._get(account)
        except PersonalAgentError:
            return {"status": "error"}
        if previous is None:
            return {"status": "missing"}
        try:
            self._credential_backend().delete_password(KEYRING_SERVICE, account)
            remaining = self._get(account)
        except Exception:
            return {"status": "error"}
        return {"status": "confirmed" if remaining is None else "error"}
