"""Actual independent AES-GCM envelopes with explicit isolated credential fixtures."""

from __future__ import annotations

import base64
import json
import traceback
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from naumi_agent.personal import workspace_vault as module
from naumi_agent.personal.connection_vault import ConnectionVault
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.workspace_vault import (
    KEYRING_SERVICE,
    MAX_ENVELOPE_BYTES,
    MAX_VALUE_BYTES,
    WorkspaceVault,
)
from tests.unit.test_personal_connection_vault import SECRET, Backend

OWNER, OTHER = "a" * 64, "b" * 64
SLOT, SECOND = "wg_" + "1" * 32, "wc_" + "2" * 32
PURPOSE = "federation_ticket"


@pytest.fixture
def backend():
    return Backend()


@pytest.fixture
def vault(backend):
    value = WorkspaceVault(backend)
    value.provision(owner_key=OWNER, slot=SLOT)
    return value


def seal(vault, value=None):
    return vault.seal(
        owner_key=OWNER,
        slot=SLOT,
        purpose=PURPOSE,
        value={"secret": SECRET} if value is None else value,
    )


def open_envelope(vault, envelope, **kwargs):
    return vault.open(
        **({"owner_key": OWNER, "slot": SLOT, "purpose": PURPOSE, "envelope": envelope} | kwargs)
    )


def assert_code(code, operation):
    with pytest.raises(PersonalAgentError) as caught:
        operation()
    error = caught.value
    assert error.code == code
    assert error.__cause__ is error.__context__ is None
    assert SECRET not in str(error) + repr(error) + "".join(traceback.format_exception(error))


def test_constructor_and_repr_do_not_consult_or_reveal_backend(backend):
    value = WorkspaceVault(backend)
    assert backend.calls == [] and repr(value) == "WorkspaceVault()"
    assert SECRET not in repr(value)


def test_default_uses_explicit_native_selector_and_dedicated_service(backend, monkeypatch):
    calls = []

    def native():
        calls.append(True)
        return backend

    monkeypatch.setattr(module, "_native_backend", native)
    vault = WorkspaceVault()
    assert calls == []
    vault.provision(owner_key=OWNER, slot=SLOT)
    assert open_envelope(vault, seal(vault)) == {"secret": SECRET}
    assert vault.delete(owner_key=OWNER, slot=SLOT) == {"status": "confirmed"}
    assert len(calls) == 8 and all(
        value[1] == "NaumiAgent.PersonalWorkspaces.v1" for value in backend.calls
    )


def test_unavailable_native_backend_has_no_fallback_or_secret_error_context(monkeypatch):
    def unavailable():
        raise RuntimeError(SECRET)

    monkeypatch.setattr(module, "_native_backend", unavailable)
    vault = WorkspaceVault()
    assert_code("credential_error", lambda: vault.provision(owner_key=OWNER, slot=SLOT))
    assert vault.delete(owner_key=OWNER, slot=SLOT) == {"status": "error"}


@pytest.mark.parametrize("slot", [SLOT, SECOND])
def test_supported_grant_and_connection_slots_real_crypto_roundtrip(backend, slot):
    vault = WorkspaceVault(backend)
    vault.provision(owner_key=OWNER, slot=slot)
    body = {"secret": SECRET, "中文": [True, None, 0, {"value": "工作区"}]}
    envelope = vault.seal(owner_key=OWNER, slot=slot, purpose=PURPOSE, value=body)
    assert vault.open(owner_key=OWNER, slot=slot, purpose=PURPOSE, envelope=envelope) == body
    stored = backend.values[(KEYRING_SERVICE, OWNER + "/" + slot)]
    assert len(base64.b64decode(stored, validate=True)) == 32
    assert SECRET not in envelope + stored and json.loads(envelope)["alg"] == "A256GCM"


def test_slots_keys_and_aead_domain_are_independent_of_calendar_vault(vault, backend):
    connection = ConnectionVault(backend)
    conn_slot = "conn_" + "1" * 32
    connection.provision(owner_key=OWNER, slot=conn_slot)
    original = connection.seal(
        owner_key=OWNER, slot=conn_slot, purpose=PURPOSE, value={"secret": SECRET}
    )
    backend.values[(KEYRING_SERVICE, OWNER + "/" + SLOT)] = backend.values[
        ("NaumiAgent.PersonalConnections.v1", OWNER + "/" + conn_slot)
    ]
    assert_code("vault_corrupt", lambda: open_envelope(vault, original))
    key = base64.b64decode(backend.values[(KEYRING_SERVICE, OWNER + "/" + SLOT)])
    nonce = bytes(range(12))
    without_domain = {"v": 1, "alg": "A256GCM", "owner": OWNER, "slot": SLOT, "purpose": PURPOSE}
    forged = json.dumps(
        {
            "v": 1,
            "alg": "A256GCM",
            "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(
                AESGCM(key).encrypt(
                    nonce,
                    b'{"secret":"fixture"}',
                    json.dumps(without_domain, sort_keys=True, separators=(",", ":")).encode(),
                )
            ).decode(),
        }
    )
    assert_code("vault_corrupt", lambda: open_envelope(vault, forged))


def test_repeated_provision_is_idempotent_and_same_instance_thread_safe(vault, backend):
    before = dict(backend.values)
    sets = sum(call[0] == "set" for call in backend.calls)
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(lambda _: vault.provision(owner_key=OWNER, slot=SLOT), range(5)))
    assert backend.values == before and sum(call[0] == "set" for call in backend.calls) == sets


def test_readback_unknown_keeps_written_key_and_retry_never_overwrites():
    class UnknownOnce(Backend):
        armed = False

        def set_password(self, *args):
            super().set_password(*args)
            self.armed = True

        def get_password(self, *args):
            if self.armed:
                self.armed = False
                raise RuntimeError(SECRET)
            return super().get_password(*args)

    backend, vault = UnknownOnce(), None
    vault = WorkspaceVault(backend)
    assert_code("credential_error", lambda: vault.provision(owner_key=OWNER, slot=SLOT))
    saved = dict(backend.values)
    assert saved and not any(call[0] == "delete" for call in backend.calls)
    vault.provision(owner_key=OWNER, slot=SLOT)
    assert backend.values == saved and sum(call[0] == "set" for call in backend.calls) == 1
    assert open_envelope(vault, seal(vault)) == {"secret": SECRET}


@pytest.mark.parametrize(
    "encoded",
    [
        "",
        "not-base64",
        base64.b64encode(b"short").decode(),
        base64.b64encode(bytes(32)).decode()[:-2] + "B=",
    ],
)
def test_invalid_existing_native_key_is_never_replaced(backend, encoded):
    backend.values[(KEYRING_SERVICE, OWNER + "/" + SLOT)] = encoded
    value = WorkspaceVault(backend)
    assert_code("credential_error", lambda: value.provision(owner_key=OWNER, slot=SLOT))
    assert not any(call[0] == "set" for call in backend.calls)


def test_seal_open_reload_native_key_and_missing_is_not_corrupt(vault, backend):
    envelope = seal(vault)
    before = len(backend.calls)
    assert open_envelope(vault, envelope) == {"secret": SECRET}
    assert seal(vault) != envelope
    assert [call[0] for call in backend.calls[before:]] == ["get", "get"]
    backend.values.clear()
    assert_code("credential_missing", lambda: open_envelope(vault, envelope))
    assert_code("credential_missing", lambda: seal(vault))


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner_key", None),
        ("owner_key", "A" * 64),
        ("owner_key", "a" * 63),
        ("slot", "conn_" + "1" * 32),
        ("slot", "wg_" + "A" * 32),
        ("slot", "wc_" + "1" * 31),
        ("slot", "../" + SLOT),
        ("slot", SLOT + "\n"),
        ("slot", True),
        ("purpose", ""),
        ("purpose", "中文"),
        ("purpose", "a" * 65),
        ("purpose", "a\nb"),
    ],
)
def test_identity_and_purpose_reject_calendar_slots_or_coercion_without_key_access(
    backend, field, value
):
    vault = WorkspaceVault(backend)
    arguments = {"owner_key": OWNER, "slot": SLOT, "purpose": PURPOSE, "value": {}}
    assert_code("invalid_input", lambda: vault.seal(**(arguments | {field: value})))
    assert backend.calls == []


@pytest.mark.parametrize(
    "field,value", [("owner_key", OTHER), ("slot", SECOND), ("purpose", "other")]
)
def test_aad_binds_original_owner_slot_and_purpose_even_when_native_key_is_copied(
    vault, backend, field, value
):
    envelope = seal(vault)
    owner, slot = (value if field == "owner_key" else OWNER), (value if field == "slot" else SLOT)
    backend.values[(KEYRING_SERVICE, owner + "/" + slot)] = backend.values[
        (KEYRING_SERVICE, OWNER + "/" + SLOT)
    ]
    assert_code("vault_corrupt", lambda: open_envelope(vault, envelope, **{field: value}))


@pytest.mark.parametrize("field", ["nonce", "ciphertext"])
def test_nonce_and_ciphertext_tamper_are_authenticated(vault, field):
    envelope = json.loads(seal(vault))
    raw = bytearray(base64.b64decode(envelope[field]))
    raw[0] ^= 1
    envelope[field] = base64.b64encode(raw).decode()
    assert_code("vault_corrupt", lambda: open_envelope(vault, json.dumps(envelope)))


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        b"body",
        {"key": float("nan")},
        {"key": float("inf")},
        {"key": {1, 2}},
        {1: "bad"},
        {"key": (1, 2)},
        {"key": "\ud800"},
        {"key": "x" * MAX_VALUE_BYTES},
    ],
)
def test_json_is_strict_and_bounded_before_credential_read(backend, value):
    vault = WorkspaceVault(backend)
    assert_code(
        "invalid_input",
        lambda: (
            seal(vault, value=value)
            if value is not None
            else vault.seal(owner_key=OWNER, slot=SLOT, purpose=PURPOSE, value=None)
        ),
    )
    assert backend.calls == []


def test_json_cycles_and_depth_fail_before_credential_read(backend):
    vault = WorkspaceVault(backend)
    cyclic = {}
    cyclic["self"] = cyclic
    assert_code("invalid_input", lambda: seal(vault, cyclic))
    nested = {}
    for _ in range(40):
        nested = {"child": nested}
    assert_code("invalid_input", lambda: seal(vault, nested))
    assert backend.calls == []


@pytest.mark.parametrize(
    "envelope",
    [
        "",
        "[]",
        '{"v":1,"v":1}',
        "非ASCII",
        "x" * (MAX_ENVELOPE_BYTES + 1),
        '{"v":true,"alg":"A256GCM","nonce":"","ciphertext":""}',
    ],
    ids=["empty", "array", "duplicate", "non-ascii", "oversize", "boolean-version"],
)
def test_malformed_envelopes_fail_without_native_key_access(backend, envelope):
    vault = WorkspaceVault(backend)
    assert_code("invalid_input", lambda: open_envelope(vault, envelope))
    assert backend.calls == []


@pytest.mark.parametrize("failure", ["get", "set", "delete"])
def test_backend_errors_are_sanitized_and_delete_never_falsely_confirms(backend, failure):
    vault = WorkspaceVault(backend)
    if failure == "delete":
        vault.provision(owner_key=OWNER, slot=SLOT)
    backend.fail.add(failure)
    if failure == "delete":
        assert vault.delete(owner_key=OWNER, slot=SLOT) == {"status": "error"}
        assert backend.values
    else:
        assert_code("credential_error", lambda: vault.provision(owner_key=OWNER, slot=SLOT))


def test_delete_has_three_truthful_states_and_does_not_reprovision(vault, backend):
    envelope = seal(vault)
    assert vault.delete(owner_key=OWNER, slot=SLOT) == {"status": "confirmed"}
    assert vault.delete(owner_key=OWNER, slot=SLOT) == {"status": "missing"}
    assert_code("credential_missing", lambda: open_envelope(vault, envelope))
    assert not backend.values
