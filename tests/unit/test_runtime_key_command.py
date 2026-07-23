from __future__ import annotations

import pytest
from typer.testing import CliRunner

from naumi_agent.config import credentials
from naumi_agent.config.credentials import CredentialStoreError
from naumi_agent.main import app
from naumi_agent.safety.payload_envelope import RuntimePayloadKey

runner = CliRunner()
KEY = bytes(range(32))
ENCODED_KEY = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="


@pytest.fixture(autouse=True)
def _without_runtime_key_environment(monkeypatch):
    monkeypatch.delenv("NAUMI_RUNTIME_PAYLOAD_KEY", raising=False)


def test_runtime_key_status_missing_is_read_only_and_actionable(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        credentials,
        "load_runtime_payload_key",
        lambda: None,
    )
    provisioned = False

    def unexpected_provision() -> bytes:
        nonlocal provisioned
        provisioned = True
        return KEY

    monkeypatch.setattr(
        credentials,
        "provision_runtime_payload_key",
        unexpected_provision,
    )

    result = runner.invoke(app, ["runtime-key", "status"])

    assert result.exit_code == 1
    assert "尚未初始化" in result.output
    assert "naumi runtime-key init" in result.output
    assert provisioned is False


def test_runtime_key_status_shows_only_nonsecret_identity(monkeypatch) -> None:
    monkeypatch.setattr(
        credentials,
        "load_runtime_payload_key",
        lambda: KEY,
    )

    result = runner.invoke(app, ["runtime-key", "status"])

    assert result.exit_code == 0
    assert "已就绪" in result.output
    assert RuntimePayloadKey.from_bytes(KEY).key_id in result.output
    assert KEY.hex() not in result.output


def test_runtime_key_environment_override_avoids_backend(monkeypatch) -> None:
    monkeypatch.setenv("NAUMI_RUNTIME_PAYLOAD_KEY", ENCODED_KEY)

    def unexpected_backend() -> bytes | None:
        raise AssertionError("environment override must avoid credential backend")

    monkeypatch.setattr(
        credentials,
        "load_runtime_payload_key",
        unexpected_backend,
    )
    monkeypatch.setattr(
        credentials,
        "provision_runtime_payload_key",
        lambda: (_ for _ in ()).throw(
            AssertionError("environment override must not provision")
        ),
    )

    status = runner.invoke(app, ["runtime-key", "status"])
    initialize = runner.invoke(app, ["runtime-key", "init"])

    assert status.exit_code == 0
    assert "环境变量" in status.output
    assert initialize.exit_code == 0
    assert "未写入系统凭据" in initialize.output
    assert ENCODED_KEY not in status.output + initialize.output


def test_runtime_key_init_creates_once_without_printing_key(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        credentials,
        "load_runtime_payload_key",
        lambda: calls.append("load") or None,
    )
    monkeypatch.setattr(
        credentials,
        "provision_runtime_payload_key",
        lambda: calls.append("provision") or KEY,
    )

    result = runner.invoke(app, ["runtime-key", "init"])

    assert result.exit_code == 0
    assert calls == ["load", "provision"]
    assert "已安全初始化" in result.output
    assert RuntimePayloadKey.from_bytes(KEY).key_id in result.output
    assert KEY.hex() not in result.output


def test_runtime_key_init_existing_key_never_claims_rotation(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        credentials,
        "load_runtime_payload_key",
        lambda: KEY,
    )
    monkeypatch.setattr(
        credentials,
        "provision_runtime_payload_key",
        lambda: KEY,
    )

    result = runner.invoke(app, ["runtime-key", "init"])

    assert result.exit_code == 0
    assert "已存在" in result.output
    assert "未执行轮换" in result.output
    assert "已安全初始化" not in result.output


def test_runtime_key_backend_error_is_sanitized(monkeypatch) -> None:
    def fail() -> bytes | None:
        raise CredentialStoreError("无法读取 Runtime payload 系统密钥。")

    monkeypatch.setattr(credentials, "load_runtime_payload_key", fail)

    result = runner.invoke(app, ["runtime-key", "init"])

    assert result.exit_code == 1
    assert "无法读取 Runtime payload 系统密钥" in result.output
    assert "Traceback" not in result.output


def test_runtime_key_group_has_explicit_help() -> None:
    result = runner.invoke(app, ["runtime-key", "--help"])

    assert result.exit_code == 0
    assert "Agent 持久 payload" in result.output
    assert "init" in result.output
    assert "status" in result.output
