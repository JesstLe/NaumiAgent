from __future__ import annotations

from dataclasses import asdict, replace

import pytest

from naumi_agent.daemons.agent_worker_contract import (
    AgentWorkerResultStatus,
    issue_agent_worker_request,
    issue_agent_worker_result,
)

T0 = "2026-07-24T10:00:00+08:00"
T1 = "2026-07-24T10:00:05+08:00"


def _request():
    return issue_agent_worker_request(
        task_id="task-contract-1",
        session_id="session-1",
        agent_name="coder",
        task="修复精确的队列竞争",
        context="只允许读取给定工作区。",
        tool_scope=("file_read", "file_edit"),
        permission_mode="moderate",
        model_tier="capable",
        max_turns=50,
        max_budget_usd=1.25,
        timeout_seconds=300,
        message_topic="task.task-contract-1.completed",
        issued_at=T0,
    )


def test_request_binds_exact_scope_budget_and_content_without_raw_text() -> None:
    request = _request()
    encoded = repr(asdict(request))

    assert request.request_id.startswith("agent-request-")
    assert request.tool_scope == ("file_edit", "file_read")
    assert request.max_turns == 50
    assert request.max_cost_microusd == 1_250_000
    assert request.timeout_milliseconds == 300_000
    assert request.task_bytes == len("修复精确的队列竞争".encode())
    assert request.context_bytes == len("只允许读取给定工作区。".encode())
    assert "修复精确的队列竞争" not in encoded
    assert "只允许读取给定工作区" not in encoded
    assert "task.task-contract-1.completed" not in encoded


def test_request_is_deterministic_and_rejects_policy_or_digest_tampering() -> None:
    request = _request()
    replay = _request()

    assert replay == request
    with pytest.raises(ValueError, match="摘要校验失败"):
        replace(request, max_turns=49)
    with pytest.raises(ValueError, match="tool_scope"):
        issue_agent_worker_request(
            task_id="task-contract-1",
            session_id="session-1",
            agent_name="coder",
            task="work",
            context="",
            tool_scope=("file_read", "bad tool"),
            permission_mode="moderate",
            model_tier="capable",
            max_turns=50,
            max_budget_usd=None,
            timeout_seconds=300,
            message_topic="task.task-contract-1.completed",
            issued_at=T0,
        )
    with pytest.raises(ValueError, match="预算"):
        issue_agent_worker_request(
            task_id="task-contract-1",
            session_id="session-1",
            agent_name="coder",
            task="work",
            context="",
            tool_scope=(),
            permission_mode="moderate",
            model_tier="capable",
            max_turns=50,
            max_budget_usd=float("nan"),
            timeout_seconds=300,
            message_topic="task.task-contract-1.completed",
            issued_at=T0,
        )


@pytest.mark.parametrize(
    ("status", "normalized", "reason"),
    [
        ("completed", "completed", "agent_completed"),
        ("error", "error", "agent_failed"),
        ("failed", "error", "agent_failed"),
        ("timeout", "timeout", "agent_timeout"),
        ("max_turns", "max_turns", "agent_max_turns"),
        ("cancelled", "cancelled", "agent_cancelled"),
    ],
)
def test_terminal_result_is_bound_low_sensitivity_evidence(
    status: str,
    normalized: str,
    reason: str,
) -> None:
    receipt = issue_agent_worker_result(
        request=_request(),
        status=status,
        response="sensitive response",
        error="sensitive error",
        total_tokens=42,
        total_cost_usd=0.012345,
        turns=3,
        completed_at=T1,
    )
    encoded = repr(asdict(receipt))

    assert receipt.status is AgentWorkerResultStatus(normalized)
    assert receipt.reason_code == reason
    assert receipt.total_cost_microusd == 12_345
    assert receipt.response_bytes == len("sensitive response")
    assert "sensitive response" not in encoded
    assert "sensitive error" not in encoded
    with pytest.raises(ValueError, match="摘要校验失败"):
        replace(receipt, total_tokens=43)


def test_contract_rejects_oversized_or_malformed_execution_facts() -> None:
    with pytest.raises(ValueError, match="task 不能为空"):
        issue_agent_worker_request(
            task_id="task-1",
            session_id="",
            agent_name="coder",
            task="",
            context="",
            tool_scope=(),
            permission_mode="moderate",
            model_tier="capable",
            max_turns=50,
            max_budget_usd=None,
            timeout_seconds=300,
            message_topic="task.task-1.completed",
            issued_at=T0,
        )
    with pytest.raises(ValueError, match="max_turns"):
        issue_agent_worker_request(
            task_id="task-1",
            session_id="",
            agent_name="coder",
            task="work",
            context="",
            tool_scope=(),
            permission_mode="moderate",
            model_tier="capable",
            max_turns=0,
            max_budget_usd=None,
            timeout_seconds=300,
            message_topic="task.task-1.completed",
            issued_at=T0,
        )
    with pytest.raises(ValueError, match="task 超过"):
        issue_agent_worker_request(
            task_id="task-1",
            session_id="",
            agent_name="coder",
            task="x" * (2 * 1024**2 + 1),
            context="",
            tool_scope=(),
            permission_mode="moderate",
            model_tier="capable",
            max_turns=50,
            max_budget_usd=None,
            timeout_seconds=300,
            message_topic="task.task-1.completed",
            issued_at=T0,
        )
    with pytest.raises(ValueError, match="status"):
        issue_agent_worker_result(
            request=_request(),
            status="unknown",
            response="",
            error=None,
            total_tokens=0,
            total_cost_usd=0,
            turns=0,
            completed_at=T1,
        )
