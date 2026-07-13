"""Session-scoped permission grant tests."""

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta

import pytest

from naumi_agent.safety.permission_grants import PermissionGrantStore


class TestPermissionGrantStore:
    def test_grant_matches_only_same_session_and_family(self) -> None:
        store = PermissionGrantStore()

        grant = store.create("session-a", "shell", "call-1")

        assert store.allows("session-a", "shell") is True
        assert store.allows("session-a", "code_execution") is False
        assert store.allows("session-b", "shell") is False
        assert grant.source_request_id == "call-1"

    def test_revoke_and_session_cleanup_are_idempotent(self) -> None:
        store = PermissionGrantStore()
        first = store.create("session-a", "shell", "call-1")
        store.create("session-a", "code_execution", "call-2")

        assert store.revoke(first.grant_id, "session-a") is True
        assert store.revoke(first.grant_id, "session-a") is False
        assert store.revoke_session("session-a") == 1
        assert store.revoke_session("session-a") == 0

    def test_revoke_rejects_a_grant_from_another_session(self) -> None:
        store = PermissionGrantStore()
        grant = store.create("session-a", "shell", "call-1")

        assert store.revoke(grant.grant_id, "session-b") is False
        assert store.allows("session-a", "shell") is True

    def test_clear_removes_every_grant(self) -> None:
        store = PermissionGrantStore()
        store.create("session-a", "shell", "call-1")
        store.create("session-b", "code_execution", "call-2")

        store.clear()

        assert store.allows("session-a", "shell") is False
        assert store.allows("session-b", "code_execution") is False
        assert store.list_session("session-a") == ()
        assert store.list_session("session-b") == ()

    def test_create_deduplicates_a_session_and_family(self) -> None:
        store = PermissionGrantStore()

        first = store.create("session-a", "shell", "call-1")
        second = store.create("session-a", "shell", "call-2")

        assert second == first
        assert store.list_session("session-a") == (first,)

    def test_created_grant_has_uuid_hex_and_timezone_aware_utc_timestamp(self) -> None:
        store = PermissionGrantStore()

        grant = store.create("session-a", "shell", "call-1")
        created_at = datetime.fromisoformat(grant.created_at)

        assert len(grant.grant_id) == 32
        assert all(character in "0123456789abcdef" for character in grant.grant_id)
        assert created_at.tzinfo is not None
        assert created_at.utcoffset() == timedelta(0)
        assert grant.expires_at is None

    @pytest.mark.parametrize(
        ("session_id", "tool_family"),
        [("", "shell"), ("session-a", ""), ("  ", "shell"), ("session-a", "  ")],
    )
    def test_create_rejects_blank_scope(self, session_id: str, tool_family: str) -> None:
        store = PermissionGrantStore()

        with pytest.raises(ValueError):
            store.create(session_id, tool_family, "call-1")

    def test_list_returns_immutable_records(self) -> None:
        store = PermissionGrantStore()
        grant = store.create("session-a", "shell", "call-1")

        with pytest.raises(FrozenInstanceError):
            grant.tool_family = "code_execution"  # type: ignore[misc]
