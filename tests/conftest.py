"""Shared test-only runtime isolation fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture
def runtime_payload_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep AgentJob tests away from the user's real credential backend."""
    monkeypatch.setenv(
        "NAUMI_RUNTIME_PAYLOAD_KEY",
        "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
    )
