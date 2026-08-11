"""Textual Workbench overview fallback parity tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from textual.widgets import Input, Markdown, Static

from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.runtime.composition import create_agent_engine
from naumi_agent.tasks.store import TaskStore
from naumi_agent.tui.app import NaumiApp
from naumi_agent.tui.workbench_overview import (
    ApprovalDecisionScreen,
    ExperimentContractIssueScreen,
    ProposalDecisionScreen,
    ProposalMergeScreen,
    WorkbenchOverviewScreen,
    WorkbenchSnapshotError,
    format_workbench_overview_markdown,
    format_workbench_release_markdown,
    format_workbench_reviews_markdown,
    format_workbench_timeline_markdown,
    format_workbench_worktrees_markdown,
)
from naumi_agent.workbench.models import EventSeverity
from naumi_agent.workbench.proposal_governance import ProposalAction
from naumi_agent.workbench.service import WorkbenchService
from naumi_agent.workbench.store import WorkbenchStore


def test_workbench_formatter_renders_authoritative_overview_fields() -> None:
    rendered = format_workbench_overview_markdown(_snapshot())

    assert "Workbench Overview" in rendered
    assert "revision 3" in rendered
    assert "Harness 评测裁判" in rendered
    assert "实现 TUI fallback" in rendered
    assert "codex/ui-10-7" in rendered
    assert "/tmp/ui-10-7" in rendered
    assert "验证通过" in rendered
    assert "高风险" in rendered
    assert "等待用户确认" in rendered


def test_workbench_formatter_has_bounded_empty_state() -> None:
    snapshot = {
        **_snapshot(),
        "missions": [],
        "tasks": [],
        "issues": [],
        "leases": [],
        "validation_runs": [],
        "failures": [],
        "approvals": [],
        "counts": {
            "missions": 0,
            "tasks": 0,
            "worktrees": 0,
            "reviews": 0,
            "failures": 0,
        },
        "active_selection": {
            "mission_id": "",
            "task_id": "",
            "worktree_id": "",
            "review_id": "",
        },
    }

    rendered = format_workbench_overview_markdown(snapshot)

    assert "暂无 Workbench 任务" in rendered
    assert "使用 `/task` 创建任务" in rendered
    assert len(rendered.splitlines()) < 20


def test_release_formatter_renders_completed_revoked_and_rejects_forged_authority() -> None:
    snapshot = _snapshot()
    snapshot["stable_population_finalization"] = {
        "schema_version": 1,
        "status": "completed",
        "receipt_id": f"evstableremotepopfinal_{'1' * 24}",
        "receipt_sha256": "2" * 64,
        "population_snapshot_id": f"relpopsnapshot_{'3' * 24}",
        "population_snapshot_sha256": "4" * 64,
        "candidate_version": "1.2.3",
        "completed_members": 2,
        "population_denominator": 2,
        "finalized_at": "2026-08-11T08:00:00+00:00",
        "historical_fact": True,
        "current_authority": True,
        "invalidation_reasons": [],
        "config_data_finalization_authority": False,
        "promotion_authority": False,
    }
    snapshot["stable_population_finalization_error"] = ""

    completed = format_workbench_release_markdown(snapshot)
    assert "已完成（authority current）" in completed
    assert "evstableremotepopfinal_" in completed
    assert "Promotion 否" in completed

    revoked = dict(snapshot)
    revoked_projection = dict(snapshot["stable_population_finalization"])
    revoked_projection.update(
        status="revoked",
        current_authority=False,
        invalidation_reasons=["population_snapshot_not_current"],
    )
    revoked["stable_population_finalization"] = revoked_projection
    rendered = format_workbench_release_markdown(revoked)
    assert "current authority 已撤销" in rendered
    assert "Population Snapshot 已失效" in rendered

    forged = dict(snapshot)
    forged_projection = dict(snapshot["stable_population_finalization"])
    forged_projection["promotion_authority"] = True
    forged["stable_population_finalization"] = forged_projection
    with pytest.raises(ValueError, match="projection 无效"):
        format_workbench_release_markdown(forged)


def test_worktree_formatter_renders_authoritative_detail_and_error_states() -> None:
    rendered = format_workbench_worktrees_markdown(_snapshot())

    assert "共 1 个" in rendered
    assert "ui-10-real" in rendered
    assert "有未提交改动" in rendered
    assert "codex/ui-10-worktrees" in rendered
    assert "实现 TUI fallback" in rendered
    assert "Workbench-Agent" in rendered
    assert "可安全删除：否" in rendered

    unavailable = format_workbench_worktrees_markdown(
        {
            **_snapshot(),
            "worktrees_status": "unavailable",
            "worktrees_code": "worktree_snapshot_failed",
            "worktrees": [],
        }
    )
    assert "暂时不可用" in unavailable
    assert "worktree_snapshot_failed" in unavailable


def test_worktree_formatter_bounds_one_hundred_items_around_selection() -> None:
    worktrees = [
        {
            "name": f"worktree-{index}",
            "path": f"/tmp/worktree-{index}",
            "branch": f"naumi/worktree-{index}",
            "status": "clean",
            "dirty_files": 0,
            "commits_ahead": 0,
            "removable": True,
        }
        for index in range(100)
    ]
    rendered = format_workbench_worktrees_markdown(
        {
            **_snapshot(),
            "worktrees": worktrees,
            "worktrees_total": 100,
        },
        selected_index=99,
    )

    assert "当前 100/100" in rendered
    assert "worktree-99" in rendered
    assert "worktree-50" not in rendered
    assert len(rendered.splitlines()) < 30


def test_reviews_formatter_renders_real_checks_files_and_diff() -> None:
    detail = {
        "evidence": {
            "approval": {
                "id": "approval-1",
                "title": "等待用户确认",
                "detail": "审查真实变更",
                "requester": "Workbench-Agent",
            },
            "worktree": {"name": "ui-10-real", "status": "present"},
            "validation_runs": [{"status": "failed", "exit_code": 1}],
            "changed_files": [{"path": "src/ui.py", "status": "modified"}],
            "diff_hunks": [{"path": "src/ui.py", "patch": "@@ -1 +1 @@\n-old\n+new"}],
        }
    }

    rendered = format_workbench_reviews_markdown(_snapshot(), detail=detail)

    assert "阻塞：1 项验证失败" in rendered
    assert "src/ui.py" in rendered
    assert "-old" in rendered
    assert "+new" in rendered


def test_reviews_formatter_renders_open_proposal_actions_and_policy_boundary() -> None:
    snapshot = _proposal_snapshot()

    rendered = format_workbench_reviews_markdown(snapshot)

    assert "Proposal · 收紧 Harness 裁判" in rendered
    assert "高风险" in rendered
    assert "harness/judge.py" in rendered
    assert "批准只进入下一 policy gate" in rendered
    assert "`a` 批准 · `x` 拒绝 · `d` 延后" in rendered


def test_reviews_formatter_renders_approved_proposal_contract_boundary() -> None:
    snapshot = _approved_proposal_snapshot()

    rendered = format_workbench_reviews_markdown(snapshot)

    assert "Proposal 已批准，但尚未执行代码" in rendered
    assert "`c` 签发或重开 Experiment Contract" in rendered
    assert "不会修改代码、运行实验或授予发布权限" in rendered


def test_reviews_formatter_renders_rollback_outcome_without_contract_action() -> None:
    snapshot = _rolled_back_proposal_snapshot()

    rendered = format_workbench_reviews_markdown(snapshot)

    assert "实施 Outcome" in rendered
    assert r"rolled\_back" in rendered
    assert "evrerollbackout" in rendered
    assert "治理状态仍保留 approved 审计" in rendered
    assert "不能再次签发 Experiment Contract" in rendered
    assert "已记录 · 2 lanes" in rendered
    assert "Before/After Evidence" in rendered
    assert "不代表回滚后评测" in rendered
    assert "fresh boot + launch identity" in rendered
    assert "Post-Rollback Verification" in rendered
    assert "回滚后行为矩阵：已记录 · 2 lanes · recovered" in rendered
    assert "Behavioral Matrix" in rendered
    assert "remote_result_ingestion" in rendered
    assert "长期指标 / Learning / Promotion：未授权" in rendered
    assert "`c` 签发" not in rendered


def test_reviews_formatter_renders_promoted_outcome_lineage() -> None:
    snapshot = _rolled_back_proposal_snapshot()
    proposal = snapshot["proposals"][0]  # type: ignore[index]
    proposal["outcome_status"] = "promoted"
    proposal["outcome"] = {
        "status": "promoted",
        "outcome_id": f"evstablepromout_{'1' * 24}",
        "authority_valid": True,
        "stable_promotion_sequence": 2,
        "stable_previous_outcome_id": f"evstablepromout_{'2' * 24}",
        "stable_decision_id": f"evstablepromdecision_{'3' * 24}",
        "stable_eligibility_id": f"evstablepromeligible_{'4' * 24}",
        "stable_observation_contract_id": f"evstablepromobserve_{'5' * 24}",
        "stable_population_assessment_id": f"evstableprompopobserve_{'6' * 24}",
        "stable_supersede_event_id": f"evstablepromoutsup_{'7' * 24}",
        "stable_promotion_outcome_authority": True,
        "projection_head_authority": True,
        "invalidation_reasons": [],
    }

    rendered = format_workbench_reviews_markdown(snapshot)

    assert "稳定推广 Outcome" in rendered
    assert "promoted · 可验证" in rendered
    assert "sequence 2" in rendered
    assert "Decision" in rendered
    assert "Eligibility" in rendered
    assert "已由当前 Outcome 替代" in rendered
    assert "Learning / Promotion / Execution authority：false" in rendered
    assert "不会自动进入 policy learning" in rendered
    assert "Rollback Receipt" not in rendered
    assert "`c` 签发" not in rendered


def test_reviews_formatter_renders_long_term_outcome_head() -> None:
    snapshot = _rolled_back_proposal_snapshot()
    proposal = snapshot["proposals"][0]  # type: ignore[index]
    root_id = proposal["outcome"]["outcome_id"]
    proposal["outcome_status"] = "rollback_recovery_observed"
    proposal["outcome"].update(
        status="rollback_recovery_observed",
        outcome_id=f"evpostlongout_{'8' * 24}",
        root_rollback_outcome_id=root_id,
        long_term_metrics_recorded=True,
        long_term_outcome_authority=True,
        current_long_term_health_authority=True,
        projection_head_authority=True,
        long_term_outcome={
            "outcome_id": f"evpostlongout_{'8' * 24}",
            "revision_sequence": 1,
            "assessment_id": f"evpostobservewindow_{'9' * 24}",
            "assessment_head_sequence": 14,
            "runtime_subject_id": "runtime-long-term",
            "runtime_binding_id": f"hrreleasebinding_{'a' * 24}",
            "observation_seconds": 3600,
            "operational_sample_count": 13,
        },
        long_term_supersede_event={
            "event_id": f"evpostoutsup_{'b' * 24}",
        },
    )

    rendered = format_workbench_reviews_markdown(snapshot)

    assert r"rollback\_recovery\_observed" in rendered
    assert "长期恢复观察" in rendered
    assert "长期指标：已记录" in rendered
    assert "revision 1" in rendered
    assert "3600s · 13 samples" in rendered
    assert "历史 rollback fact 保留" in rendered
    assert "health=true · head=true · outcome=true" in rendered
    assert "不能再次签发 Experiment Contract" in rendered


def test_workbench_formatter_escapes_store_markdown_and_control_characters() -> None:
    snapshot = _snapshot()
    snapshot["missions"][0]["title"] = "# injected\n[link](file:///secret)"  # type: ignore[index]
    snapshot["tasks"][0]["description"] = "**fake success**\x00"  # type: ignore[index]

    rendered = format_workbench_overview_markdown(snapshot)

    assert "\\# injected" in rendered
    assert "\\[link\\](file:///secret)" in rendered
    assert "\\*\\*fake success\\*\\*" in rendered
    assert "\x00" not in rendered


def test_workbench_timeline_formats_persisted_categories_and_bounded_payload() -> None:
    snapshot = _snapshot()
    snapshot["events"] = [
        {
            "id": "event-1",
            "session_id": "session-workbench-tui",
            "type": "validation.failed",
            "actor": "Harness",
            "subject_id": "task-1",
            "payload": {
                "exit_code": 1,
                "command": ["pytest", "-q"],
                "detail": {"private": "not-rendered"},
                "note": "line 1\nline 2",
            },
            "timestamp": "2026-08-11T12:00:00+00:00",
            "correlation_id": "run-1",
            "parent_event_id": None,
            "severity": "error",
        },
        {
            "id": "event-2",
            "session_id": "session-workbench-tui",
            "type": "worktree.created",
            "actor": "Git-Agent",
            "subject_id": "worktree-1",
            "payload": {"branch": "codex/ui-10-5a"},
            "timestamp": "2026-08-11T11:59:00+00:00",
            "correlation_id": None,
            "parent_event_id": None,
            "severity": "info",
        },
    ]

    rendered = format_workbench_timeline_markdown(snapshot)

    assert "Timeline" in rendered
    assert "工具" in rendered
    assert "Git" in rendered
    assert "🔴" in rendered
    assert "exit\\_code：1" in rendered
    assert "command：\\[2 项\\]" in rendered
    assert "private" not in rendered
    assert "note：line 1 line 2" in rendered


def test_workbench_timeline_rejects_control_characters_and_oversized_history() -> None:
    snapshot = _snapshot()
    event = {
        "id": "event-1",
        "session_id": "session-workbench-tui",
        "type": "agent.started",
        "actor": "Agent",
        "subject_id": "task-1",
        "payload": {},
        "timestamp": "2026-08-11T12:00:00+00:00",
        "severity": "info",
    }
    snapshot["events"] = [{**event, "actor": "bad\nactor"}]
    with pytest.raises(WorkbenchSnapshotError, match="Timeline 文本字段"):
        format_workbench_timeline_markdown(snapshot)

    snapshot["events"] = [event] * 101
    with pytest.raises(WorkbenchSnapshotError, match="不超过 100 项"):
        format_workbench_timeline_markdown(snapshot)

    snapshot["events"] = [{**event, "session_id": "other-session"}]
    with pytest.raises(WorkbenchSnapshotError, match="会话不匹配"):
        format_workbench_timeline_markdown(snapshot)

    snapshot["events"] = [{**event, "payload": {"duration": float("nan")}}]
    with pytest.raises(WorkbenchSnapshotError, match="数值无效"):
        format_workbench_timeline_markdown(snapshot)


@pytest.mark.asyncio
async def test_workbench_timeline_uses_real_sqlite_service_order_and_redaction(
    tmp_path,
) -> None:
    database = str(tmp_path / "workbench-timeline.db")
    store = WorkbenchStore(database)
    service = WorkbenchService(
        task_store=TaskStore(database),
        workbench_store=store,
    )
    await store.append_event(
        session_id="session-workbench-tui",
        type="worktree.created",
        actor="Git-Agent",
        subject_id="worktree-1",
        payload={"branch": "codex/ui-10-5a", "api_key": "must-not-render"},
    )
    latest = await store.append_event(
        session_id="session-workbench-tui",
        type="validation.failed",
        actor="Harness",
        subject_id="task-1",
        payload={"exit_code": 1},
        severity=EventSeverity.ERROR,
    )

    snapshot = await service.dashboard_snapshot("session-workbench-tui")
    rendered = format_workbench_timeline_markdown(snapshot)

    assert snapshot["events"][0]["id"] == latest.id
    assert "validation › failed" in rendered
    assert "worktree › created" in rendered
    assert "must-not-render" not in rendered


@pytest.mark.asyncio
async def test_textual_workbench_slash_route_refreshes_and_retains_last_snapshot() -> None:
    engine = create_agent_engine(AppConfig())
    engine._session = SimpleNamespace(id="session-workbench-tui")
    engine.workbench_service.dashboard_snapshot = AsyncMock(  # type: ignore[method-assign]
        side_effect=[_snapshot(), RuntimeError("store unavailable")]
    )
    app = NaumiApp(engine)

    async with app.run_test(size=(90, 30)) as pilot:
        app._handle_slash_command("/workbench")
        await pilot.pause(0.1)

        screen = app.screen
        assert isinstance(screen, WorkbenchOverviewScreen)
        assert "实现 TUI fallback" in screen.query_one(
            "#workbench-content", Markdown
        )._markdown
        engine.workbench_service.dashboard_snapshot.assert_awaited_once_with(
            "session-workbench-tui"
        )

        await pilot.press("tab")
        await pilot.pause(0.05)
        assert screen.selected_tab == "worktrees"
        assert "ui-10-real" in screen.query_one(
            "#workbench-content", Markdown
        )._markdown

        await pilot.press("1")
        await pilot.pause(0.05)
        assert screen.selected_tab == "overview"

        await pilot.press("4")
        await pilot.pause(0.05)
        assert screen.selected_tab == "release"
        assert "Stable Population Finalization Authority" in screen.query_one(
            "#workbench-content", Markdown
        )._markdown

        await pilot.press("1")
        await pilot.pause(0.05)

        await pilot.press("r")
        await pilot.pause(0.1)

        assert "实现 TUI fallback" in screen.query_one(
            "#workbench-content", Markdown
        )._markdown
        assert "已保留上一次快照" in str(
            screen.query_one("#workbench-error", Static).render()
        )

        await pilot.press("escape")
        await pilot.pause(0.05)
        assert not isinstance(app.screen, WorkbenchOverviewScreen)


@pytest.mark.asyncio
async def test_textual_workbench_timeline_tab_navigates_persisted_events() -> None:
    snapshot = _snapshot()
    snapshot["events"] = [
        {
            "id": f"event-{index}",
            "session_id": "session-workbench-tui",
            "type": "agent.started" if index == 0 else "worktree.created",
            "actor": "Agent",
            "subject_id": f"task-{index}",
            "payload": {},
            "timestamp": f"2026-08-11T12:00:0{index}+00:00",
            "severity": "info",
        }
        for index in range(2)
    ]
    refreshed = {
        **snapshot,
        "revision": 4,
        "events": list(reversed(snapshot["events"])),
    }
    engine = create_agent_engine(AppConfig())
    engine._session = SimpleNamespace(id="session-workbench-tui")
    engine.workbench_service.dashboard_snapshot = AsyncMock(  # type: ignore[method-assign]
        side_effect=[snapshot, refreshed]
    )
    app = NaumiApp(engine)

    async with app.run_test(size=(90, 30)) as pilot:
        app._handle_slash_command("/workbench")
        await pilot.pause(0.1)
        await pilot.press("5")
        await pilot.pause(0.05)

        screen = app.screen
        assert isinstance(screen, WorkbenchOverviewScreen)
        assert screen.selected_tab == "timeline"
        assert "task-0" in screen.query_one("#workbench-content", Markdown)._markdown

        await pilot.press("down")
        await pilot.pause(0.05)
        assert screen.selected_event_index == 1
        assert screen.selected_event_id == "event-1"
        assert "task-1" in screen.query_one("#workbench-content", Markdown)._markdown

        await pilot.press("r")
        await pilot.pause(0.1)
        assert screen.selected_event_id == "event-1"
        assert screen.selected_event_index == 0


@pytest.mark.asyncio
async def test_textual_workbench_timeline_applies_increment_and_recovers_gap() -> None:
    base = _snapshot()
    event_2 = {
        "id": "event-2",
        "session_id": "session-workbench-tui",
        "type": "issue.updated",
        "actor": "Agent",
        "subject_id": "task-2",
        "payload": {},
        "timestamp": "2026-08-11T12:00:02+00:00",
        "severity": "info",
        "cursor": 2,
    }
    initial = {
        **base,
        "timeline_stream_id": "timeline-a",
        "timeline_earliest_cursor": 1,
        "timeline_cursor": 2,
        "events": [event_2],
    }
    event_3 = {
        **event_2,
        "id": "event-3",
        "subject_id": "task-3",
        "timestamp": "2026-08-11T12:00:03+00:00",
        "cursor": 3,
    }
    event_5 = {
        **event_2,
        "id": "event-5",
        "subject_id": "task-5",
        "timestamp": "2026-08-11T12:00:05+00:00",
        "cursor": 5,
    }
    recovered = {
        **initial,
        "revision": 4,
        "timeline_earliest_cursor": 4,
        "timeline_cursor": 5,
        "events": [event_5],
    }
    engine = create_agent_engine(AppConfig())
    engine._session = SimpleNamespace(id="session-workbench-tui")
    engine.workbench_service.dashboard_snapshot = AsyncMock(  # type: ignore[method-assign]
        side_effect=[initial, recovered]
    )
    engine.workbench_service.timeline_replay_window = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "schema_version": 1,
            "session_id": "session-workbench-tui",
            "stream_id": "timeline-a",
            "requested_cursor": 2,
            "earliest_cursor": 1,
            "latest_cursor": 3,
            "gap": False,
            "gap_reason": "",
            "events": [event_3],
        }
    )
    app = NaumiApp(engine)

    async with app.run_test(size=(90, 30)) as pilot:
        app._handle_slash_command("/workbench")
        await pilot.pause(0.1)
        screen = app.screen
        assert isinstance(screen, WorkbenchOverviewScreen)
        assert screen._timeline_timer is not None
        screen._timeline_timer.stop()

        screen.refresh_timeline()
        await pilot.pause(0.1)
        assert screen.snapshot is not None
        assert screen.snapshot["timeline_cursor"] == 3
        assert screen.snapshot["events"][0]["id"] == "event-3"

        engine.workbench_service.timeline_replay_window.return_value = {
            "schema_version": 1,
            "session_id": "session-workbench-tui",
            "stream_id": "timeline-b",
            "requested_cursor": 3,
            "earliest_cursor": 4,
            "latest_cursor": 5,
            "gap": True,
            "gap_reason": "stream_changed",
            "events": [],
        }
        screen.refresh_timeline()
        await pilot.pause(0.2)
        assert screen.snapshot["timeline_cursor"] == 5
        assert screen.snapshot["events"][0]["id"] == "event-5"
        assert engine.workbench_service.dashboard_snapshot.await_count == 2


@pytest.mark.asyncio
async def test_textual_workbench_reviews_loads_selected_service_evidence() -> None:
    engine = create_agent_engine(AppConfig())
    engine._session = SimpleNamespace(id="session-workbench-tui")
    engine.workbench_service.dashboard_snapshot = AsyncMock(  # type: ignore[method-assign]
        return_value=_snapshot()
    )
    engine.workbench_service.get_review_evidence = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "approval": {
                "id": "approval-1",
                "title": "等待用户确认",
                "detail": "检查实际变更",
                "requester": "Workbench-Agent",
            },
            "worktree": {"name": "ui-10-real", "status": "present"},
            "validation_runs": [{"status": "passed", "exit_code": 0}],
            "changed_files": [{"path": "src/ui.py", "status": "modified"}],
            "diff_hunks": [{"path": "src/ui.py", "patch": "-old\n+new"}],
        }
    )
    app = NaumiApp(engine)

    async with app.run_test(size=(100, 30)) as pilot:
        app._handle_slash_command("/workbench")
        await pilot.pause(0.1)
        await pilot.press("3")
        await pilot.pause(0.1)

        screen = app.screen
        assert isinstance(screen, WorkbenchOverviewScreen)
        rendered = screen.query_one("#workbench-content", Markdown)._markdown
        assert "Reviews" in rendered
        assert "证据就绪" in rendered
        assert "src/ui.py" in rendered
        engine.workbench_service.get_review_evidence.assert_awaited_once_with(
            "session-workbench-tui", "approval-1"
        )


@pytest.mark.asyncio
async def test_textual_workbench_rejects_approval_with_required_reason() -> None:
    engine = create_agent_engine(AppConfig())
    engine._session = SimpleNamespace(id="session-workbench-tui")
    initial = _snapshot()
    completed = {
        **initial,
        "revision": 4,
        "approvals": [],
        "counts": {**initial["counts"], "reviews": 0},
    }
    engine.workbench_service.dashboard_snapshot = AsyncMock(  # type: ignore[method-assign]
        side_effect=[initial, completed]
    )
    engine.workbench_service.get_review_evidence = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "approval": initial["approvals"][0],
            "worktree": {"name": "ui-10-real", "status": "present"},
            "validation_runs": [],
            "changed_files": [],
            "diff_hunks": [],
        }
    )
    engine.workbench_service.resolve_approval = AsyncMock(  # type: ignore[method-assign]
        return_value={"id": "approval-1", "state": "rejected"}
    )
    app = NaumiApp(engine)

    async with app.run_test(size=(100, 32)) as pilot:
        app._handle_slash_command("/workbench")
        await pilot.pause(0.1)
        await pilot.press("3", "x")
        await pilot.pause(0.05)

        assert isinstance(app.screen, ApprovalDecisionScreen)
        note = app.screen.query_one("#approval-decision-note", Input)
        await pilot.press("enter")
        assert "不能为空" in str(
            app.screen.query_one("#approval-decision-error", Static).render()
        )
        note.value = "缺少真实回归证据"
        await pilot.press("enter")
        await pilot.pause(0.15)

        assert isinstance(app.screen, WorkbenchOverviewScreen)
        call = engine.workbench_service.resolve_approval.await_args
        assert call.kwargs["session_id"] == "session-workbench-tui"
        assert call.kwargs["approval_id"] == "approval-1"
        assert call.kwargs["actor"] == "Human"
        assert call.kwargs["state"].value == "rejected"
        assert call.kwargs["decision_note"] == "缺少真实回归证据"
        rendered = app.screen.query_one("#workbench-content", Markdown)._markdown
        assert "Approval 已拒绝" in rendered


@pytest.mark.asyncio
async def test_textual_workbench_bypass_approves_approval_without_modal() -> None:
    engine = create_agent_engine(AppConfig())
    engine.set_runtime_mode("bypass")
    engine._session = SimpleNamespace(id="session-workbench-tui")
    initial = _snapshot()
    completed = {
        **initial,
        "revision": 4,
        "approvals": [],
        "counts": {**initial["counts"], "reviews": 0},
    }
    engine.workbench_service.dashboard_snapshot = AsyncMock(  # type: ignore[method-assign]
        side_effect=[initial, completed]
    )
    engine.workbench_service.get_review_evidence = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "approval": initial["approvals"][0],
            "worktree": {"name": "ui-10-real", "status": "present"},
            "validation_runs": [],
            "changed_files": [],
            "diff_hunks": [],
        }
    )
    engine.workbench_service.resolve_approval = AsyncMock(  # type: ignore[method-assign]
        return_value={"id": "approval-1", "state": "approved"}
    )
    app = NaumiApp(engine)

    async with app.run_test(size=(100, 32)) as pilot:
        app._handle_slash_command("/workbench")
        await pilot.pause(0.1)
        await pilot.press("3", "a")
        await pilot.pause(0.15)

        assert isinstance(app.screen, WorkbenchOverviewScreen)
        call = engine.workbench_service.resolve_approval.await_args
        assert call.kwargs["state"].value == "approved"
        assert call.kwargs["decision_note"] == ""


@pytest.mark.asyncio
async def test_textual_workbench_rejects_proposal_with_required_reason() -> None:
    engine = create_agent_engine(AppConfig())
    engine._session = SimpleNamespace(id="session-workbench-tui")
    initial = _proposal_snapshot()
    completed = {
        **initial,
        "revision": 4,
        "proposals": [],
        "counts": {**initial["counts"], "reviews": 0},
    }
    engine.workbench_service.dashboard_snapshot = AsyncMock(  # type: ignore[method-assign]
        side_effect=[initial, completed]
    )
    engine.workbench_service.govern_proposal = AsyncMock(  # type: ignore[method-assign]
        return_value={"id": "proposal-1", "state": "rejected"}
    )
    app = NaumiApp(engine)

    async with app.run_test(size=(100, 32)) as pilot:
        app._handle_slash_command("/workbench")
        await pilot.pause(0.1)
        await pilot.press("3", "x")
        await pilot.pause(0.05)

        assert isinstance(app.screen, ProposalDecisionScreen)
        note = app.screen.query_one("#proposal-decision-note", Input)
        await pilot.press("enter")
        assert "不能为空" in str(
            app.screen.query_one("#proposal-decision-error", Static).render()
        )

        note.value = "缺少跨平台回归证据"
        await pilot.press("enter")
        await pilot.pause(0.15)

        assert isinstance(app.screen, WorkbenchOverviewScreen)
        engine.workbench_service.govern_proposal.assert_awaited_once_with(
            "session-workbench-tui",
            "proposal-1",
            action=ProposalAction.REJECT,
            reviewer="Human",
            decision_note="缺少跨平台回归证据",
        )
        rendered = app.screen.query_one("#workbench-content", Markdown)._markdown
        assert "Proposal 已拒绝" in rendered


@pytest.mark.asyncio
async def test_textual_workbench_defers_proposal_with_bounded_preset() -> None:
    engine = create_agent_engine(AppConfig())
    engine._session = SimpleNamespace(id="session-workbench-tui")
    initial = _proposal_snapshot()
    completed = {
        **initial,
        "revision": 4,
        "proposals": [],
        "counts": {**initial["counts"], "reviews": 0},
    }
    engine.workbench_service.dashboard_snapshot = AsyncMock(  # type: ignore[method-assign]
        side_effect=[initial, completed]
    )
    engine.workbench_service.govern_proposal = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "id": "proposal-1",
            "state": "deferred",
            "cooldown_until": "2026-08-12T00:00:00+00:00",
        }
    )
    app = NaumiApp(engine)
    before = datetime.now(UTC)

    async with app.run_test(size=(100, 32)) as pilot:
        app._handle_slash_command("/workbench")
        await pilot.pause(0.1)
        await pilot.press("3", "d")
        await pilot.pause(0.05)

        assert isinstance(app.screen, ProposalDecisionScreen)
        note = app.screen.query_one("#proposal-decision-note", Input)
        days = app.screen.query_one("#proposal-defer-days", Input)
        await pilot.press("enter")
        assert "延后原因不能为空" in str(
            app.screen.query_one("#proposal-decision-error", Static).render()
        )
        note.value = "等待跨平台回归证据"
        days.value = "7"
        note.focus()
        await pilot.press("enter", "enter")
        await pilot.pause(0.15)

        assert isinstance(app.screen, WorkbenchOverviewScreen)
        call = engine.workbench_service.govern_proposal.await_args
        assert call.args == ("session-workbench-tui", "proposal-1")
        assert call.kwargs["action"] is ProposalAction.DEFER
        assert call.kwargs["reviewer"] == "Human"
        assert call.kwargs["decision_note"] == "等待跨平台回归证据"
        defer_until = datetime.fromisoformat(call.kwargs["defer_until"])
        assert before + timedelta(days=7, seconds=-1) <= defer_until
        assert defer_until <= datetime.now(UTC) + timedelta(days=7)
        rendered = app.screen.query_one("#workbench-content", Markdown)._markdown
        assert "Proposal 已延后" in rendered


@pytest.mark.asyncio
async def test_textual_workbench_bypass_approves_proposal_without_modal() -> None:
    engine = create_agent_engine(AppConfig())
    engine.set_runtime_mode("bypass")
    engine._session = SimpleNamespace(id="session-workbench-tui")
    initial = _proposal_snapshot()
    completed = {
        **initial,
        "revision": 4,
        "proposals": [],
        "counts": {**initial["counts"], "reviews": 0},
    }
    engine.workbench_service.dashboard_snapshot = AsyncMock(  # type: ignore[method-assign]
        side_effect=[initial, completed]
    )
    engine.workbench_service.govern_proposal = AsyncMock(  # type: ignore[method-assign]
        return_value={"id": "proposal-1", "state": "approved"}
    )
    app = NaumiApp(engine)

    async with app.run_test(size=(100, 32)) as pilot:
        app._handle_slash_command("/workbench")
        await pilot.pause(0.1)
        await pilot.press("3", "a")
        await pilot.pause(0.15)

        assert isinstance(app.screen, WorkbenchOverviewScreen)
        engine.workbench_service.govern_proposal.assert_awaited_once_with(
            "session-workbench-tui",
            "proposal-1",
            action=ProposalAction.APPROVE,
            reviewer="Human",
            decision_note="",
        )


@pytest.mark.asyncio
async def test_textual_workbench_merges_into_selected_authority_target() -> None:
    engine = create_agent_engine(AppConfig())
    engine._session = SimpleNamespace(id="session-workbench-tui")
    initial = _merge_proposal_snapshot()
    completed = {
        **initial,
        "revision": 4,
        "proposals": [initial["proposals"][1]],  # type: ignore[index]
        "counts": {**initial["counts"], "reviews": 1},  # type: ignore[arg-type]
    }
    engine.workbench_service.dashboard_snapshot = AsyncMock(  # type: ignore[method-assign]
        side_effect=[initial, completed]
    )
    engine.workbench_service.govern_proposal = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "id": "proposal-1",
            "state": "merged",
            "merged_into_id": "proposal-2",
        }
    )
    app = NaumiApp(engine)

    async with app.run_test(size=(100, 32)) as pilot:
        app._handle_slash_command("/workbench")
        await pilot.pause(0.1)
        await pilot.press("3", "m")
        await pilot.pause(0.05)

        assert isinstance(app.screen, ProposalMergeScreen)
        await pilot.press("enter")
        await pilot.pause(0.15)

        assert isinstance(app.screen, WorkbenchOverviewScreen)
        engine.workbench_service.govern_proposal.assert_awaited_once_with(
            "session-workbench-tui",
            "proposal-1",
            action=ProposalAction.MERGE,
            reviewer="Human",
            decision_note="",
            merge_into_id="proposal-2",
        )
        rendered = app.screen.query_one("#workbench-content", Markdown)._markdown
        assert "Proposal 已合并到 proposal-2" in rendered


@pytest.mark.asyncio
async def test_textual_workbench_rejects_merge_without_authority_target() -> None:
    engine = create_agent_engine(AppConfig())
    engine._session = SimpleNamespace(id="session-workbench-tui")
    initial = _proposal_snapshot()
    engine.workbench_service.dashboard_snapshot = AsyncMock(  # type: ignore[method-assign]
        return_value=initial
    )
    engine.workbench_service.govern_proposal = AsyncMock()  # type: ignore[method-assign]
    app = NaumiApp(engine)

    async with app.run_test(size=(100, 32)) as pilot:
        app._handle_slash_command("/workbench")
        await pilot.pause(0.1)
        await pilot.press("3", "m")
        await pilot.pause(0.05)

        assert isinstance(app.screen, WorkbenchOverviewScreen)
        assert "没有同 Candidate" in app.screen.query_one(
            "#workbench-content", Markdown
        )._markdown
        engine.workbench_service.govern_proposal.assert_not_awaited()


@pytest.mark.asyncio
async def test_textual_workbench_rejects_malformed_merge_target_projection() -> None:
    engine = create_agent_engine(AppConfig())
    engine._session = SimpleNamespace(id="session-workbench-tui")
    initial = _merge_proposal_snapshot()
    source = initial["proposals"][0]  # type: ignore[index]
    source["merge_target_ids"] = ["proposal-2", "proposal-2"]
    engine.workbench_service.dashboard_snapshot = AsyncMock(  # type: ignore[method-assign]
        return_value=initial
    )
    engine.workbench_service.govern_proposal = AsyncMock()  # type: ignore[method-assign]
    app = NaumiApp(engine)

    async with app.run_test(size=(100, 32)) as pilot:
        app._handle_slash_command("/workbench")
        await pilot.pause(0.1)
        await pilot.press("3", "m")
        await pilot.pause(0.05)

        assert "目标快照格式无效" in app.screen.query_one(
            "#workbench-content", Markdown
        )._markdown
        engine.workbench_service.govern_proposal.assert_not_awaited()


@pytest.mark.asyncio
async def test_textual_workbench_issues_approved_contract_after_confirmation() -> None:
    engine = create_agent_engine(AppConfig())
    engine._session = SimpleNamespace(id="session-workbench-tui")
    initial = _approved_proposal_snapshot()
    refreshed = {**initial, "revision": 4}
    engine.workbench_service.dashboard_snapshot = AsyncMock(  # type: ignore[method-assign]
        side_effect=[initial, refreshed]
    )
    contract = SimpleNamespace(contract_id=f"evx_{'a' * 24}")
    authority = SimpleNamespace(contract_id=contract.contract_id)
    engine.evolution_experiment_contract_issuer.issue = AsyncMock(  # type: ignore[method-assign]
        return_value=contract
    )
    engine.evolution_experiment_contract_store.get = AsyncMock(  # type: ignore[method-assign]
        return_value=authority
    )
    app = NaumiApp(engine)

    async with app.run_test(size=(100, 32)) as pilot:
        app._handle_slash_command("/workbench")
        await pilot.pause(0.1)
        await pilot.press("3", "c")
        await pilot.pause(0.05)

        assert isinstance(app.screen, ExperimentContractIssueScreen)
        await pilot.press("enter")
        await pilot.pause(0.15)

        assert isinstance(app.screen, WorkbenchOverviewScreen)
        engine.evolution_experiment_contract_issuer.issue.assert_awaited_once()
        call = engine.evolution_experiment_contract_issuer.issue.await_args
        assert call.kwargs["session_id"] == "session-workbench-tui"
        assert call.kwargs["proposal_id"] == "proposal-1"
        assert isinstance(call.kwargs["seed"], int)
        rendered = app.screen.query_one("#workbench-content", Markdown)._markdown
        assert contract.contract_id.replace("_", "\\_") in rendered
        assert "execution\\_ready=false" in rendered


@pytest.mark.asyncio
async def test_textual_workbench_bypass_issues_contract_without_modal() -> None:
    engine = create_agent_engine(AppConfig())
    engine.set_runtime_mode("bypass")
    engine._session = SimpleNamespace(id="session-workbench-tui")
    initial = _approved_proposal_snapshot()
    engine.workbench_service.dashboard_snapshot = AsyncMock(  # type: ignore[method-assign]
        side_effect=[initial, {**initial, "revision": 4}]
    )
    contract = SimpleNamespace(contract_id=f"evx_{'b' * 24}")
    engine.evolution_experiment_contract_issuer.issue = AsyncMock(  # type: ignore[method-assign]
        return_value=contract
    )
    engine.evolution_experiment_contract_store.get = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(contract_id=contract.contract_id)
    )
    app = NaumiApp(engine)

    async with app.run_test(size=(100, 32)) as pilot:
        app._handle_slash_command("/workbench")
        await pilot.pause(0.1)
        await pilot.press("3", "c")
        await pilot.pause(0.15)

        assert isinstance(app.screen, WorkbenchOverviewScreen)
        engine.evolution_experiment_contract_issuer.issue.assert_awaited_once()


@pytest.mark.asyncio
async def test_textual_workbench_blocks_contract_when_rollback_outcome_exists() -> None:
    engine = create_agent_engine(AppConfig())
    engine._session = SimpleNamespace(id="session-workbench-tui")
    engine.workbench_service.dashboard_snapshot = AsyncMock(  # type: ignore[method-assign]
        return_value=_rolled_back_proposal_snapshot()
    )
    engine.evolution_experiment_contract_issuer.issue = AsyncMock()  # type: ignore[method-assign]
    app = NaumiApp(engine)

    async with app.run_test(size=(100, 32)) as pilot:
        app._handle_slash_command("/workbench")
        await pilot.pause(0.1)
        await pilot.press("3", "c")
        await pilot.pause(0.05)

        assert isinstance(app.screen, WorkbenchOverviewScreen)
        rendered = app.screen.query_one("#workbench-content", Markdown)._markdown
        assert "不能再次签发 Contract" in rendered
        engine.evolution_experiment_contract_issuer.issue.assert_not_awaited()


@pytest.mark.asyncio
async def test_textual_workbench_rejects_cross_session_snapshot() -> None:
    engine = create_agent_engine(AppConfig())
    engine._session = SimpleNamespace(id="session-current")
    engine.workbench_service.dashboard_snapshot = AsyncMock(  # type: ignore[method-assign]
        return_value=_snapshot()
    )
    app = NaumiApp(engine)

    async with app.run_test(size=(80, 24)) as pilot:
        app._handle_slash_command("/workbench")
        await pilot.pause(0.1)

        screen = app.screen
        assert isinstance(screen, WorkbenchOverviewScreen)
        assert "权威快照暂时不可用" in screen.query_one(
            "#workbench-content", Markdown
        )._markdown
        assert "加载失败" in str(
            screen.query_one("#workbench-error", Static).render()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [80, 120, 200])
async def test_textual_workbench_keeps_core_status_visible_at_supported_widths(
    width: int,
) -> None:
    engine = create_agent_engine(AppConfig())
    engine._session = SimpleNamespace(id="session-workbench-tui")
    engine.workbench_service.dashboard_snapshot = AsyncMock(  # type: ignore[method-assign]
        return_value=_snapshot()
    )
    app = NaumiApp(engine)

    async with app.run_test(size=(width, 30)) as pilot:
        app._handle_slash_command("/workbench")
        await pilot.pause(0.1)

        rendered = app.screen.query_one("#workbench-content", Markdown)._markdown
        assert "Workbench Overview" in rendered
        assert "任务 1" in rendered
        assert "实现 TUI fallback" in rendered


@pytest.mark.asyncio
async def test_real_store_service_and_textual_workbench_chain(tmp_path) -> None:
    engine = create_agent_engine(
        AppConfig(
            memory=MemoryConfig(
                session_db_path=str(tmp_path / "sessions.db"),
                vector_db_path=str(tmp_path / "chroma"),
            )
        )
    )
    session = await engine.get_or_create_session("TUI Workbench real chain")
    mission = await engine.workbench_service.create_mission(
        session_id=session.id,
        title="真实 Workbench 目标",
        goal="验证 SQLite 到 Textual 的只读链路",
    )
    await engine.workbench_service.create_issue(
        session_id=session.id,
        mission_id=mission.id,
        title="真实 TUI 任务",
        description="从真实 Store 读取",
    )
    app = NaumiApp(engine)

    async with app.run_test(size=(80, 28)) as pilot:
        app._handle_slash_command("/workbench")
        await pilot.pause(0.15)

        screen = app.screen
        assert isinstance(screen, WorkbenchOverviewScreen)
        for _ in range(200):
            if screen.snapshot is not None:
                break
            await pilot.pause(0.05)
        else:
            pytest.fail("真实 Workbench 权威快照未在 10 秒内加载完成")
        rendered = screen.query_one("#workbench-content", Markdown)._markdown
        assert "真实 Workbench 目标" in rendered
        assert "真实 TUI 任务" in rendered
        assert "SQLite 到 Textual" in rendered
        assert screen.snapshot is not None
        assert screen.snapshot["session_id"] == session.id


def _snapshot() -> dict[str, object]:
    return {
        "schema_version": 1,
        "stream_id": "stream-workbench-tui",
        "revision": 3,
        "generated_at": "2026-07-18T04:40:00+08:00",
        "full": True,
        "session_id": "session-workbench-tui",
        "counts": {
            "missions": 1,
            "tasks": 1,
            "worktrees": 1,
            "reviews": 1,
            "failures": 1,
        },
        "active_selection": {
            "mission_id": "mission-1",
            "task_id": "task-1",
            "worktree_id": "task-1",
            "review_id": "approval-1",
        },
        "missions": [{
            "id": "mission-1",
            "title": "Harness 评测裁判",
            "goal": "建立可信回归门",
            "status": "active",
        }],
        "tasks": [{
            "id": "task-1",
            "subject": "实现 TUI fallback",
            "description": "复用权威 Workbench 快照",
            "status": "in_progress",
            "owner": "naumi",
            "blocked_by": [],
        }],
        "issues": [{
            "id": "issue-1",
            "task_id": "task-1",
            "risk_level": "high",
            "related_branch": "codex/ui-10-7",
            "related_worktree": "/tmp/ui-10-7",
            "related_pr": "#107",
            "expected_artifacts": ["tui overview"],
        }],
        "leases": [{"task_id": "task-1", "agent_id": "naumi"}],
        "validation_runs": [{
            "task_id": "task-1",
            "status": "passed",
            "exit_code": 0,
            "command": ["pytest", "-q", "test_tui_workbench_overview.py"],
        }],
        "failures": [{
            "task_id": "task-1",
            "kind": "test_failed",
            "title": "旧平台测试失败",
        }],
        "approvals": [{
            "id": "approval-1",
            "session_id": "session-workbench-tui",
            "task_id": "task-1",
            "state": "waiting",
            "title": "等待用户确认",
        }],
        "proposals": [],
        "events": [],
        "worktrees_status": "ready",
        "worktrees_code": "",
        "worktrees_total": 1,
        "worktrees_truncated": False,
        "worktrees": [{
            "name": "ui-10-real",
            "path": "/tmp/ui-10-real",
            "branch": "codex/ui-10-worktrees",
            "status": "dirty",
            "dirty_files": 2,
            "commits_ahead": 1,
            "removable": False,
            "kept_reason": "",
            "task_id": "task-1",
            "task": {
                "id": "task-1",
                "subject": "实现 TUI fallback",
            },
            "lease": {"id": "lease-1", "agent_id": "Workbench-Agent"},
            "agent_id": "Workbench-Agent",
        }],
    }


def _proposal_snapshot() -> dict[str, object]:
    snapshot = _snapshot()
    snapshot["approvals"] = []
    snapshot["active_selection"] = {
        **snapshot["active_selection"],  # type: ignore[arg-type]
        "review_id": "proposal-1",
        "review_kind": "proposal",
    }
    snapshot["proposals"] = [{
        "id": "proposal-1",
        "session_id": "session-workbench-tui",
        "mission_id": "mission-1",
        "task_id": "task-1",
        "agent_id": "Harness-Agent",
        "title": "收紧 Harness 裁判",
        "impact_scope": "避免无证据通过",
        "intended_files": ["src/naumi_agent/harness/judge.py"],
        "validation_plan": ["运行裁判模块测试"],
        "risk_level": "high",
        "questions": [],
        "state": "open",
        "source_kind": "evolution_candidate",
        "source_id": "candidate-1",
        "source_revision": 2,
        "proposal_kind": "harness_policy",
    }]
    return snapshot


def _approved_proposal_snapshot() -> dict[str, object]:
    snapshot = _proposal_snapshot()
    proposal = snapshot["proposals"][0]  # type: ignore[index]
    proposal["state"] = "approved"
    proposal["contract_issue_allowed"] = True
    return snapshot


def _rolled_back_proposal_snapshot() -> dict[str, object]:
    snapshot = _approved_proposal_snapshot()
    proposal = snapshot["proposals"][0]  # type: ignore[index]
    proposal["contract_issue_allowed"] = False
    proposal["outcome_status"] = "rolled_back"
    proposal["outcome"] = {
        "outcome_id": f"evrerollbackout_{'1' * 24}",
        "rollback_receipt_id": f"evrerollbackexec_{'2' * 24}",
        "experiment_contract_id": f"evx_{'3' * 24}",
        "breach_reasons": ["runtime_guardrail_breach"],
        "authority_valid": True,
        "before_after_recorded": True,
        "before_after_evidence": {
            "evidence_id": f"evbeforeafter_{'4' * 24}",
            "lanes": [
                {"lane_kind": "interventional"},
                {"lane_kind": "adversarial"},
            ],
        },
        "post_rollback_verification_recorded": True,
        "post_rollback_verification": {
            "verification_id": f"evpostrollback_{'5' * 24}",
            "baseline_slot_id": f"relslot_{'6' * 24}",
            "baseline_version": "0.1.214",
        },
        "post_rollback_behavioral_evaluation_recorded": True,
        "post_rollback_behavioral_matrix": {
            "matrix_id": f"evpostmatrix_{'7' * 24}",
            "recovery_verdict": "recovered",
            "lanes": [
                {
                    "order": 1,
                    "lane_kind": "interventional",
                    "platform": "macos",
                    "recovery_status": "recovered",
                    "evidence_kind": "local_behavioral_lane",
                },
                {
                    "order": 2,
                    "lane_kind": "adversarial",
                    "platform": "windows",
                    "recovery_status": "recovered",
                    "evidence_kind": "remote_result_ingestion",
                },
            ],
        },
    }
    return snapshot


def _merge_proposal_snapshot() -> dict[str, object]:
    snapshot = _proposal_snapshot()
    source = snapshot["proposals"][0]  # type: ignore[index]
    source["merge_target_ids"] = ["proposal-2"]
    snapshot["proposals"].append(  # type: ignore[union-attr]
        {
            **source,
            "id": "proposal-2",
            "title": "收紧 Harness 裁判 revision 3",
            "source_revision": 3,
            "merge_target_ids": [],
        }
    )
    snapshot["counts"] = {**snapshot["counts"], "reviews": 2}  # type: ignore[arg-type]
    return snapshot
