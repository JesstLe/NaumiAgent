"""HAR-06 lifecycle policy contract tests."""

from __future__ import annotations

import pytest

from naumi_agent.harness.retention import (
    LifecycleActor,
    LifecyclePolicy,
    decide_lifecycle_transition,
    permits_automatic_cleanup,
    policy_from_session_status,
)


def test_same_policy_transition_is_idempotent_for_every_actor() -> None:
    for policy in LifecyclePolicy:
        for actor in LifecycleActor:
            decision = decide_lifecycle_transition(policy, policy, actor=actor)
            assert decision.allowed is True
            assert decision.idempotent is True
            assert decision.effective_policy is policy


def test_retention_worker_only_changes_archive_to_delete() -> None:
    allowed = decide_lifecycle_transition(
        LifecyclePolicy.ARCHIVE,
        LifecyclePolicy.DELETE,
        actor=LifecycleActor.RETENTION_WORKER,
    )
    blocked_retain = decide_lifecycle_transition(
        LifecyclePolicy.RETAIN,
        LifecyclePolicy.ARCHIVE,
        actor=LifecycleActor.RETENTION_WORKER,
    )
    blocked_hold = decide_lifecycle_transition(
        LifecyclePolicy.LEGAL_HOLD,
        LifecyclePolicy.DELETE,
        actor=LifecycleActor.RETENTION_WORKER,
    )

    assert allowed.allowed is True
    assert allowed.automatic_cleanup_allowed is True
    assert blocked_retain.allowed is False
    assert blocked_hold.allowed is False


def test_legal_hold_entry_and_exit_require_user_audit_note() -> None:
    enter_without_note = decide_lifecycle_transition(
        "retain",
        "legal_hold",
        actor="user",
    )
    enter = decide_lifecycle_transition(
        "retain",
        "legal_hold",
        actor="user",
        audit_note="诉讼证据保全",
    )
    exit_without_note = decide_lifecycle_transition(
        "legal_hold",
        "archive",
        actor="user",
    )
    exit_hold = decide_lifecycle_transition(
        "legal_hold",
        "archive",
        actor="user",
        audit_note="保全要求已解除",
    )

    assert enter_without_note.allowed is False
    assert enter.allowed is True
    assert enter.requires_audit is True
    assert exit_without_note.allowed is False
    assert exit_hold.allowed is True
    assert exit_hold.requires_audit is True


def test_system_recovery_cannot_change_policy() -> None:
    decision = decide_lifecycle_transition(
        LifecyclePolicy.DELETE,
        LifecyclePolicy.RETAIN,
        actor=LifecycleActor.SYSTEM_RECOVERY,
    )

    assert decision.allowed is False
    assert decision.effective_policy is LifecyclePolicy.DELETE


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (LifecyclePolicy.RETAIN, False),
        (LifecyclePolicy.ARCHIVE, False),
        (LifecyclePolicy.DELETE, True),
        (LifecyclePolicy.LEGAL_HOLD, False),
    ],
)
def test_only_delete_policy_permits_automatic_cleanup(
    policy: LifecyclePolicy,
    expected: bool,
) -> None:
    assert permits_automatic_cleanup(policy) is expected


def test_current_session_statuses_map_to_shared_policy() -> None:
    assert policy_from_session_status("active") is LifecyclePolicy.RETAIN
    assert policy_from_session_status(" archived ") is LifecyclePolicy.ARCHIVE


def test_unknown_session_status_fails_closed() -> None:
    with pytest.raises(ValueError, match="未知"):
        policy_from_session_status("deleted-ish")


@pytest.mark.parametrize(
    ("current", "requested", "actor"),
    [
        ("unknown", "retain", "user"),
        ("retain", "unknown", "user"),
        ("retain", "archive", "robot"),
    ],
)
def test_unknown_persisted_values_fail_closed(
    current: str,
    requested: str,
    actor: str,
) -> None:
    with pytest.raises(ValueError, match="未知"):
        decide_lifecycle_transition(current, requested, actor=actor)
