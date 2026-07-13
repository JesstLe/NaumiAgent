from __future__ import annotations

import json

import pytest

from naumi_agent.ui.permission_confirmation import (
    PermissionChallengeStore,
    summarize_arguments,
)
from naumi_agent.ui.protocol import (
    ClientEventType,
    ServerEventType,
    normalize_client_record,
)


@pytest.mark.parametrize(
    ("choice", "token", "expected"),
    [
        ("allow_once", None, "allow_once"),
        ("deny", None, "deny"),
        ("grant_session", None, "grant_session"),
        ("confirm", "challenge-token", "confirm"),
        ("allow", None, "allow"),
        ("bypass", None, "bypass"),
    ],
)
def test_permission_choice_normalization_preserves_supported_choices(
    choice: str,
    token: str | None,
    expected: str,
) -> None:
    payload: dict[str, object] = {"request_id": 123, "choice": choice.upper()}
    if token is not None:
        payload["confirmation_token"] = f"  {token}  "

    record = normalize_client_record(
        {"type": ClientEventType.PERMISSION_RESPONSE, "payload": payload}
    )

    assert record["payload"] == {
        "request_id": "123",
        "choice": expected,
        **({"confirmation_token": token} if token is not None else {}),
    }


@pytest.mark.parametrize("token", [None, "", "   "])
def test_permission_choice_confirm_requires_a_token(token: str | None) -> None:
    with pytest.raises(ValueError, match="确认令牌不能为空"):
        normalize_client_record(
            {
                "type": ClientEventType.PERMISSION_RESPONSE,
                "payload": {
                    "request_id": "perm-1",
                    "choice": "confirm",
                    "confirmation_token": token,
                },
            }
        )


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"grant_id": 42}, {"grant_id": "42"}),
        ({"scope": "ALL"}, {"scope": "all"}),
    ],
)
def test_permission_revoke_accepts_one_grant_or_all(
    payload: dict[str, object], expected: dict[str, str]
) -> None:
    record = normalize_client_record(
        {"type": ClientEventType.PERMISSION_REVOKE, "payload": payload}
    )

    assert record["payload"] == expected


@pytest.mark.parametrize("payload", [{}, {"grant_id": " "}, {"scope": "session"}])
def test_permission_revoke_rejects_empty_or_unsupported_scope(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="撤销权限"):
        normalize_client_record(
            {"type": ClientEventType.PERMISSION_REVOKE, "payload": payload}
        )


def test_protocol_exposes_confirmation_and_grant_events() -> None:
    assert ServerEventType.PERMISSION_CONFIRMATION_REQUIRED == "permission/confirmation_required"
    assert ServerEventType.PERMISSION_GRANTS_CHANGED == "permission/grants_changed"


def test_argument_summary_redacts_and_bounds_output() -> None:
    summary = summarize_arguments(
        {
            "command": "x" * 400,
            "authorization": "Bearer private",
            "nested": {"password": "private"},
            "items": list(range(80)),
            "opaque": object(),
        }
    )

    assert summary["authorization"] == "[已隐藏]"
    assert summary["nested"]["password"] == "[已隐藏]"
    assert len(summary["command"]) <= 160
    assert len(summary["items"]) == 50
    assert summary["opaque"] == "<object>"
    assert len(json.dumps(summary, ensure_ascii=False)) <= 1200


def test_permission_challenge_is_one_use_and_request_bound() -> None:
    clock = [100.0]
    store = PermissionChallengeStore(clock=lambda: clock[0])
    token = store.issue("request-1", "session-1", "call-1")

    assert store.consume(token, "request-1", "session-1", "call-1") == "valid"
    assert store.consume(token, "request-1", "session-1", "call-1") == "consumed"


def test_permission_challenge_reports_unknown_mismatch_and_expiry() -> None:
    clock = [100.0]
    store = PermissionChallengeStore(clock=lambda: clock[0])
    token = store.issue("request-1", "session-1", "call-1")

    assert store.consume("unknown", "request-1", "session-1", "call-1") == "unknown"
    assert store.consume(token, "request-2", "session-1", "call-1") == "mismatch"
    clock[0] += 31
    assert store.consume(token, "request-1", "session-1", "call-1") == "expired"
