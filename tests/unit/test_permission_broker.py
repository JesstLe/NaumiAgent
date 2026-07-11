"""Permission broker tests for API-hosted interactive approvals."""

from __future__ import annotations

import asyncio

import pytest

from naumi_agent.api.permission_broker import PermissionApprovalBroker


@pytest.mark.asyncio
async def test_permission_resolution_unblocks_only_matching_session() -> None:
    broker = PermissionApprovalBroker(timeout_seconds=1)
    waiting = asyncio.create_task(
        broker.confirm({"session_id": "session-a", "call_id": "call-1"})
    )
    await asyncio.sleep(0)

    assert await broker.resolve("session-b", "call-1", "allow") is False
    assert await broker.resolve("session-a", "call-1", "allow") is True
    assert await waiting == "allow"


@pytest.mark.asyncio
async def test_permission_resolution_rejects_unknown_decision() -> None:
    broker = PermissionApprovalBroker(timeout_seconds=1)
    waiting = asyncio.create_task(
        broker.confirm({"session_id": "session-a", "call_id": "call-1"})
    )
    await asyncio.sleep(0)

    assert await broker.resolve("session-a", "call-1", "always_allow") is False
    assert await broker.resolve("session-a", "call-1", "deny") is True
    assert await waiting == "deny"
