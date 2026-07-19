"""UI-17.2b golden parity for permission and user-interaction controls."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from naumi_agent.tui.app import NaumiApp, PermissionConfirmScreen
from naumi_agent.ui.bridge import JsonlEngineBridge
from naumi_agent.ui.permission_confirmation import (
    normalize_backend_permission_choices,
    public_permission_request_payload,
)
from naumi_agent.ui.protocol import ServerEventType
from naumi_agent.user_interaction import (
    normalize_interaction_request,
    normalize_interaction_response,
    public_interaction_request_payload,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_PATH = (
    PROJECT_ROOT
    / "tests"
    / "fixtures"
    / "ui17"
    / "permission-interaction-golden.json"
)


def _golden() -> dict[str, object]:
    document = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert set(document) == {"schema_version", "permission", "interaction"}
    assert document["schema_version"] == 1
    assert isinstance(document["permission"], dict)
    assert isinstance(document["interaction"], dict)
    return document


def test_shared_permission_projection_matches_redacted_golden() -> None:
    permission = _golden()["permission"]
    assert isinstance(permission, dict)
    payload = permission["input"]
    assert isinstance(payload, dict)
    choices = normalize_backend_permission_choices(payload["choices"])
    assert choices is not None

    public = public_permission_request_payload(
        payload,
        request_id="call-golden-1",
        choices=choices,
    )

    assert public == permission["expected_request"]
    serialized = json.dumps(public, ensure_ascii=False)
    assert "private-token" not in serialized
    assert "[已隐藏]" in serialized
    assert public["choices"][-1] == "bypass"
    assert public["requires_double_confirm"] is False


@pytest.mark.parametrize(
    "choices",
    [
        ("allow_once", "deny", "unknown"),
        ("allow_once",),
        ("deny",),
    ],
)
def test_shared_permission_projection_rejects_untrusted_choices(
    choices: tuple[str, ...],
) -> None:
    permission = _golden()["permission"]
    assert isinstance(permission, dict)
    payload = permission["input"]
    assert isinstance(payload, dict)

    with pytest.raises(ValueError, match="权限选择"):
        public_permission_request_payload(
            payload,
            request_id="call-golden-1",
            choices=choices,
        )


@pytest.mark.asyncio
async def test_bridge_and_tui_publish_same_permission_request_golden() -> None:
    permission = _golden()["permission"]
    assert isinstance(permission, dict)
    payload = permission["input"]
    expected = permission["expected_request"]
    assert isinstance(payload, dict)

    emitted: list[tuple[object, dict[str, object], str]] = []

    async def emit(event, public, *, request_id=None):
        emitted.append((event, public, str(request_id or "")))

    bridge_context = SimpleNamespace(
        _closed=False,
        _pending_permissions={},
        emit=emit,
    )
    bridge_task = asyncio.create_task(
        JsonlEngineBridge.confirm_permission(bridge_context, payload)
    )
    await asyncio.sleep(0)
    assert emitted == [
        (ServerEventType.PERMISSION_REQUEST, expected, "call-golden-1")
    ]
    bridge_context._pending_permissions["call-golden-1"].future.set_result(
        "deny"
    )
    assert await bridge_task == "deny"

    captured: list[PermissionConfirmScreen] = []
    status = SimpleNamespace(status_text="", mode_text="default")

    def push_screen(screen, callback):
        captured.append(screen)
        callback("grant_session")

    tui_context = SimpleNamespace(
        debug_trace=None,
        push_screen=push_screen,
        query_one=lambda _widget: status,
    )
    choice = await NaumiApp.confirm_permission(tui_context, payload)

    assert choice == "grant_session"
    assert len(captured) == 1
    assert captured[0].payload == expected


@pytest.mark.asyncio
async def test_tui_permission_choices_fail_closed_before_opening_modal() -> None:
    status = SimpleNamespace(status_text="", mode_text="default")
    push_screen = AsyncMock()
    context = SimpleNamespace(
        debug_trace=None,
        push_screen=push_screen,
        query_one=lambda _widget: status,
    )

    choice = await NaumiApp.confirm_permission(
        context,
        {"tool_name": "bash_run", "arguments": {"token": "private"}},
    )

    assert choice == "deny"
    assert "安全拒绝" in status.status_text
    push_screen.assert_not_called()


def test_shared_interaction_request_and_responses_match_golden() -> None:
    interaction = _golden()["interaction"]
    assert isinstance(interaction, dict)
    request = normalize_interaction_request(interaction["input"])
    public = public_interaction_request_payload(
        request,
        request_id=str(interaction["request_id"]),
        session_id=str(interaction["session_id"]),
        run_id=str(interaction["run_id"]),
        agent_name=str(interaction["agent_name"]),
    )

    assert public == interaction["expected_request"]
    for name in ("option_response", "custom_response"):
        response = interaction[name]
        assert isinstance(response, dict)
        assert normalize_interaction_response(
            request,
            response["raw"],
        ) == response["expected"]


@pytest.mark.asyncio
async def test_bridge_and_tui_publish_same_interaction_and_normalize_answer() -> None:
    interaction = _golden()["interaction"]
    assert isinstance(interaction, dict)
    payload = {
        **interaction["input"],
        "_interaction_id": interaction["request_id"],
        "agent_name": interaction["agent_name"],
    }
    expected = interaction["expected_request"]
    assert isinstance(expected, dict)
    emitted: list[tuple[object, dict[str, object], str]] = []

    async def emit(event, public, *, request_id=None):
        emitted.append((event, public, str(request_id or "")))

    bridge_context = SimpleNamespace(
        _closed=False,
        _interaction_authority=lambda: None,
        engine=SimpleNamespace(_session=SimpleNamespace(id="session-golden")),
        _active_run_context={},
        _pending_interactions={},
        emit=emit,
        _schedule_pending_interaction_timeout=lambda _request_id: None,
        _schedule_pending_interaction_owner_renewal=lambda _request_id: None,
    )
    bridge_task = asyncio.create_task(
        JsonlEngineBridge.request_user_interaction(bridge_context, payload)
    )
    await asyncio.sleep(0)
    assert emitted == [
        (ServerEventType.INTERACTION_REQUEST, expected, "ask-golden-1")
    ]
    option_expected = interaction["option_response"]["expected"]
    bridge_context._pending_interactions["ask-golden-1"].future.set_result(
        option_expected
    )
    assert await bridge_task == option_expected

    presented: list[dict[str, object]] = []

    async def present(public, *, record):
        assert record is None
        presented.append(public)
        return interaction["option_response"]["raw"]

    tui_context = SimpleNamespace(
        _interaction_authority=lambda: None,
        engine=SimpleNamespace(_session=SimpleNamespace(id="session-golden")),
        _active_interaction_ids=set(),
        _interaction_owner_tasks={},
        _interaction_records={},
        _interaction_lock=asyncio.Lock(),
        _present_user_interaction=present,
    )
    answer = await NaumiApp.request_user_interaction(tui_context, payload)

    assert presented == [expected]
    assert answer == option_expected
