"""Real AES-GCM contracts with a deterministic in-memory credential backend."""

from __future__ import annotations

import base64
import json
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from naumi_agent.personal import connection_vault as module
from naumi_agent.personal.connection_vault import (
    KEYRING_SERVICE,
    MAX_ENVELOPE_BYTES,
    MAX_VALUE_BYTES,
    ConnectionVault,
)
from naumi_agent.personal.models import PersonalAgentError

OWNER = "a" * 64
OTHER = "b" * 64
SLOT = "conn_" + "1" * 32
PURPOSE = "oauth-token"
SECRET = "DO_NOT_EXPOSE_TOKEN_b983af"


class Backend:
    def __init__(self):
        self.values = {}
        self.calls = []
        self.fail = set()
        self.lock = threading.Lock()

    def __repr__(self):
        return SECRET

    def _call(self, operation, service, account):
        self.calls.append((operation, service, account))
        if operation in self.fail:
            raise RuntimeError(SECRET)

    def get_password(self, service, account):
        with self.lock:
            self._call("get", service, account)
            return self.values.get((service, account))

    def set_password(self, service, account, value):
        with self.lock:
            self._call("set", service, account)
            self.values[(service, account)] = value

    def delete_password(self, service, account):
        with self.lock:
            self._call("delete", service, account)
            del self.values[(service, account)]


@pytest.fixture
def backend():
    return Backend()


@pytest.fixture
def vault(backend):
    result = ConnectionVault(backend)
    result.provision(owner_key=OWNER, slot=SLOT)
    return result


def seal(vault, value=None, **kwargs):
    return vault.seal(
        owner_key=OWNER,
        slot=SLOT,
        purpose=PURPOSE,
        value={"access_token": SECRET} if value is None else value,
        **kwargs,
    )


def open_envelope(vault, envelope, **kwargs):
    fields = {"owner_key": OWNER, "slot": SLOT, "purpose": PURPOSE, "envelope": envelope}
    return vault.open(**(fields | kwargs))


def assert_code(code, operation):
    with pytest.raises(PersonalAgentError) as caught:
        operation()
    error = caught.value
    assert error.code == code
    assert SECRET not in str(error) + repr(error)
    assert error.__cause__ is None and error.__context__ is None
    assert SECRET not in "".join(traceback.format_exception(error))


def test_constructor_has_no_credential_access_and_repr_does_not_reveal_backend(backend):
    vault = ConnectionVault(backend)
    assert backend.calls == []
    assert repr(vault) == "ConnectionVault()" and SECRET not in repr(vault)


def test_default_backend_uses_only_explicit_native_adapter(backend, monkeypatch):
    calls = []

    def native():
        calls.append("native")
        return backend

    monkeypatch.setattr(module, "_native_backend", native)
    vault = ConnectionVault()
    assert calls == []
    vault.provision(owner_key=OWNER, slot=SLOT)
    assert open_envelope(vault, seal(vault)) == {"access_token": SECRET}
    assert vault.delete(owner_key=OWNER, slot=SLOT) == {"status": "confirmed"}
    assert len(calls) == 8


def test_unsupported_native_store_fails_closed_without_file_fallback(monkeypatch):
    monkeypatch.setattr(module.sys, "platform", "unsupported-fixture")
    vault = ConnectionVault()
    assert_code("credential_error", lambda: vault.provision(owner_key=OWNER, slot=SLOT))
    assert vault.delete(owner_key=OWNER, slot=SLOT) == {"status": "error"}


@pytest.mark.parametrize("slot", [SLOT, "oauth_" + "2" * 32, "client_" + "3" * 64])
def test_supported_slots_roundtrip_and_only_key_enters_os_store(backend, slot):
    vault = ConnectionVault(backend)
    vault.provision(owner_key=OWNER, slot=slot)
    value = {"access_token": SECRET, "refresh_token": "更新凭据", "nested": [True, 0, None]}
    envelope = vault.seal(owner_key=OWNER, slot=slot, purpose=PURPOSE, value=value)
    assert vault.open(owner_key=OWNER, slot=slot, purpose=PURPOSE, envelope=envelope) == value
    stored = backend.values[(KEYRING_SERVICE, OWNER + "/" + slot)]
    assert len(stored) == 44 and len(base64.b64decode(stored, validate=True)) == 32
    assert SECRET not in stored + envelope
    assert all(call[1] == KEYRING_SERVICE for call in backend.calls)
    assert json.loads(envelope)["alg"] == "A256GCM"


def test_existing_key_is_verified_and_never_overwritten(vault, backend):
    before = dict(backend.values)
    sets = sum(call[0] == "set" for call in backend.calls)
    vault.provision(owner_key=OWNER, slot=SLOT)
    assert backend.values == before
    assert sum(call[0] == "set" for call in backend.calls) == sets


def test_noncanonical_system_key_base64_is_rejected_without_overwrite(backend):
    canonical = base64.b64encode(b"\x00" * 32).decode()
    noncanonical = canonical[:-2] + "B="
    assert base64.b64decode(noncanonical, validate=True) == b"\x00" * 32
    backend.values[(KEYRING_SERVICE, OWNER + "/" + SLOT)] = noncanonical
    vault = ConnectionVault(backend)
    assert_code("credential_error", lambda: vault.provision(owner_key=OWNER, slot=SLOT))
    assert not any(call[0] == "set" for call in backend.calls)


def test_every_seal_and_open_reloads_key_without_stale_cache(vault, backend):
    envelope = seal(vault)
    before = len(backend.calls)
    assert open_envelope(vault, envelope) == {"access_token": SECRET}
    assert seal(vault) != envelope
    assert [call[0] for call in backend.calls[before:]] == ["get", "get"]
    backend.values.clear()
    assert_code("credential_missing", lambda: open_envelope(vault, envelope))
    assert_code("credential_missing", lambda: seal(vault))
    assert backend.values == {}


def test_key_rotation_is_observed_not_cached(vault, backend):
    envelope = seal(vault)
    backend.values[(KEYRING_SERVICE, OWNER + "/" + SLOT)] = base64.b64encode(b"z" * 32).decode()
    assert_code("vault_corrupt", lambda: open_envelope(vault, envelope))
    assert open_envelope(vault, seal(vault)) == {"access_token": SECRET}


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner_key", "A" * 64),
        ("owner_key", "a" * 63),
        ("owner_key", None),
        ("slot", "conn_" + "a" * 31),
        ("slot", "oauth_" + "A" * 32),
        ("slot", "client_" + "a" * 32),
        ("slot", "../" + SLOT),
        ("slot", SLOT + "\n"),
        ("slot", 1),
        ("purpose", ""),
        ("purpose", "x" * 65),
        ("purpose", "a\nb"),
        ("purpose", "令牌"),
        ("purpose", None),
    ],
)
def test_identity_and_purpose_are_strict_without_backend_access(backend, field, value):
    vault = ConnectionVault(backend)
    fields = {"owner_key": OWNER, "slot": SLOT, "purpose": PURPOSE, "value": {}}
    assert_code("invalid_input", lambda: vault.seal(**(fields | {field: value})))
    assert backend.calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner_key", OTHER),
        ("slot", "conn_" + "2" * 32),
        ("purpose", "another-purpose"),
    ],
)
def test_aad_prevents_owner_slot_and_purpose_substitution(vault, backend, field, value):
    envelope = seal(vault)
    original_key = backend.values[(KEYRING_SERVICE, OWNER + "/" + SLOT)]
    owner = value if field == "owner_key" else OWNER
    slot = value if field == "slot" else SLOT
    backend.values[(KEYRING_SERVICE, owner + "/" + slot)] = original_key
    assert_code("vault_corrupt", lambda: open_envelope(vault, envelope, **{field: value}))


def test_separate_owner_and_slot_keys_are_distinct(backend):
    vault = ConnectionVault(backend)
    for owner, slot in [(OWNER, SLOT), (OTHER, SLOT), (OWNER, "oauth_" + "2" * 32)]:
        vault.provision(owner_key=owner, slot=slot)
    assert len(set(backend.values.values())) == 3


@pytest.mark.parametrize("field", ["nonce", "ciphertext"])
def test_ciphertext_and_nonce_tamper_fail_authentication(vault, field):
    envelope = json.loads(seal(vault))
    damaged = bytearray(base64.b64decode(envelope[field]))
    damaged[0] ^= 1
    envelope[field] = base64.b64encode(damaged).decode()
    assert_code("vault_corrupt", lambda: open_envelope(vault, json.dumps(envelope)))


@pytest.mark.parametrize(
    "damage",
    [
        lambda value: value | {"v": True},
        lambda value: value | {"v": 2},
        lambda value: value | {"alg": "none"},
        lambda value: value | {"extra": SECRET},
        lambda value: value | {"nonce": value["nonce"] + "\n"},
        lambda value: value | {"nonce": "!" * 16},
        lambda value: value | {"nonce": base64.b64encode(b"short").decode()},
        lambda value: value | {"ciphertext": ""},
        lambda value: value | {"ciphertext": base64.b64encode(b"short").decode()},
        lambda value: value | {"ciphertext": None},
    ],
)
def test_envelope_contract_rejects_unknown_fields_versions_and_base64(vault, damage):
    assert_code(
        "invalid_input", lambda: open_envelope(vault, json.dumps(damage(json.loads(seal(vault)))))
    )


@pytest.mark.parametrize(
    "raw",
    [
        "[]",
        "null",
        "{",
        '{"v":1,"v":1}',
        '{"v":NaN}',
        "\ud800",
        "{" + '"nested":' + "[" * 2000 + "0" + "]" * 2000 + "}",
    ],
)
def test_malformed_envelope_json_is_sanitized(vault, raw):
    assert_code("invalid_input", lambda: open_envelope(vault, raw))


@pytest.mark.parametrize(
    "value",
    [
        [],
        None,
        {1: "wrong-key"},
        {"x": (1, 2)},
        {"x": {1, 2}},
        {"x": float("nan")},
        {"x": float("inf")},
        {"x": "\ud800"},
        {"x": object()},
    ],
)
def test_plaintext_is_strict_detached_json(vault, value):
    assert_code(
        "invalid_input",
        lambda: vault.seal(
            owner_key=OWNER,
            slot=SLOT,
            purpose=PURPOSE,
            value=value,
        ),
    )


def test_cycles_depth_and_node_limits_are_rejected(vault):
    cycle = {}
    cycle["cycle"] = cycle
    assert_code("invalid_input", lambda: seal(vault, cycle))
    deep = current = {}
    for _ in range(33):
        current["x"] = {}
        current = current["x"]
    assert_code("invalid_input", lambda: seal(vault, deep))
    assert_code("invalid_input", lambda: seal(vault, {"nodes": [0] * 100_001}))


def test_payload_capacity_and_envelope_limits_are_enforced(vault):
    # The canonical object has eight bytes of syntax around this ASCII value.
    largest = {"v": "x" * (MAX_VALUE_BYTES - 8)}
    envelope = seal(vault, largest)
    assert len(envelope.encode("utf-8")) <= MAX_ENVELOPE_BYTES
    assert open_envelope(vault, envelope) == largest
    assert_code("invalid_input", lambda: seal(vault, {"v": "x" * (MAX_VALUE_BYTES - 7)}))
    assert_code("invalid_input", lambda: seal(vault, {"v": "字" * (MAX_VALUE_BYTES // 3)}))
    assert_code("invalid_input", lambda: open_envelope(vault, "x" * (MAX_ENVELOPE_BYTES + 1)))
    repeated = "x" * (MAX_VALUE_BYTES // 2)
    assert_code("invalid_input", lambda: seal(vault, {"repeated": [repeated] * 20}))
    assert_code("invalid_input", lambda: seal(vault, {"escaped": "\x00" * MAX_VALUE_BYTES}))


def test_repeated_seals_have_independent_random_nonces_and_detached_results(vault):
    envelopes = [seal(vault) for _ in range(512)]
    assert len(set(envelopes)) == 512
    assert len({json.loads(value)["nonce"] for value in envelopes}) == 512
    result = open_envelope(vault, envelopes[0])
    result["access_token"] = "changed"
    assert open_envelope(vault, envelopes[0]) == {"access_token": SECRET}


def test_plaintext_snapshot_is_detached_before_keyring_call(vault, backend):
    value = {"nested": [{"token": SECRET}]}
    original = backend.get_password

    def mutate_after_snapshot(service, account):
        value["nested"][0]["token"] = "caller-changed"
        value["nested"].append({1: "not-json"})
        return original(service, account)

    backend.get_password = mutate_after_snapshot
    envelope = seal(vault, value)
    assert open_envelope(vault, envelope) == {"nested": [{"token": SECRET}]}


def test_concurrent_provision_with_required_external_lease_and_parallel_roundtrips(backend):
    lease = threading.Lock()

    def operate(index):
        independent = ConnectionVault(backend)
        with lease:
            independent.provision(owner_key=OWNER, slot=SLOT)
        value = {"index": index, "token": SECRET}
        envelope = seal(independent, value)
        assert open_envelope(independent, envelope) == value
        return json.loads(envelope)["nonce"]

    with ThreadPoolExecutor(max_workers=12) as pool:
        nonces = list(pool.map(operate, range(80)))
    assert len(set(nonces)) == 80
    assert sum(call[0] == "set" for call in backend.calls) == 1


@pytest.mark.parametrize("encoded", ["", "!" * 44, SECRET, base64.b64encode(b"x" * 31).decode()])
def test_corrupt_existing_system_key_is_never_overwritten(backend, encoded):
    backend.values[(KEYRING_SERVICE, OWNER + "/" + SLOT)] = encoded
    vault = ConnectionVault(backend)
    assert_code("credential_error", lambda: vault.provision(owner_key=OWNER, slot=SLOT))
    assert_code("credential_error", lambda: seal(vault))
    assert backend.values[(KEYRING_SERVICE, OWNER + "/" + SLOT)] == encoded
    assert not any(call[0] == "set" for call in backend.calls)


@pytest.mark.parametrize("operation", ["get", "set"])
def test_provision_backend_failures_are_sanitized_and_never_fallback(backend, operation, tmp_path):
    backend.fail.add(operation)
    vault = ConnectionVault(backend)
    assert_code("credential_error", lambda: vault.provision(owner_key=OWNER, slot=SLOT))
    assert list(tmp_path.iterdir()) == []


def test_set_receipt_and_readback_conflicts_do_not_trigger_overwrite_or_delete(backend):
    original = backend.set_password

    def altered(service, account, value):
        original(service, account, base64.b64encode(b"x" * 32).decode())

    backend.set_password = altered
    assert_code(
        "credential_error",
        lambda: ConnectionVault(backend).provision(
            owner_key=OWNER,
            slot=SLOT,
        ),
    )
    assert [call[0] for call in backend.calls] == ["get", "set", "get"]


def test_unconfirmed_set_readback_is_error_and_is_not_automatically_retried(backend):
    backend.set_password = lambda _service, _account, _value: None
    assert_code(
        "credential_error",
        lambda: ConnectionVault(backend).provision(
            owner_key=OWNER,
            slot=SLOT,
        ),
    )
    assert [call[0] for call in backend.calls] == ["get", "get"]


def test_seal_and_open_backend_failure_do_not_expose_exception(vault, backend):
    envelope = seal(vault)
    backend.fail.add("get")
    assert_code("credential_error", lambda: seal(vault))
    assert_code("credential_error", lambda: open_envelope(vault, envelope))


def test_delete_reports_confirmed_missing_and_existing_envelope_becomes_unreadable(vault):
    envelope = seal(vault)
    assert vault.delete(owner_key=OWNER, slot=SLOT) == {"status": "confirmed"}
    assert vault.delete(owner_key=OWNER, slot=SLOT) == {"status": "missing"}
    assert_code("credential_missing", lambda: open_envelope(vault, envelope))


@pytest.mark.parametrize("operation", ["get", "delete"])
def test_delete_os_failures_are_explicit_and_redacted(vault, backend, operation):
    backend.fail.add(operation)
    result = vault.delete(owner_key=OWNER, slot=SLOT)
    assert result == {"status": "error"} and SECRET not in repr(result)


def test_delete_requires_readback_absence(vault, backend):
    backend.delete_password = lambda _service, _account: None
    assert vault.delete(owner_key=OWNER, slot=SLOT) == {"status": "error"}


@pytest.mark.parametrize("plaintext", [b"[]", b'{"x":NaN}', b'{"x":1,"x":2}', b"\xff"])
def test_authenticated_but_invalid_plaintext_is_rejected(vault, backend, plaintext):
    key = base64.b64decode(backend.values[(KEYRING_SERVICE, OWNER + "/" + SLOT)])
    _, aad = module._aad(OWNER, SLOT, PURPOSE)
    nonce = b"n" * 12
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
    envelope = json.dumps(
        {
            "v": 1,
            "alg": "A256GCM",
            "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(ciphertext).decode(),
        }
    )
    assert_code("vault_corrupt", lambda: open_envelope(vault, envelope))
