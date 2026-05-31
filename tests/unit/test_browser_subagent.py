"""Tests for browser subagent BrowserSubagent and helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from naumi_agent.tools.browser.subagent.browser_subagent import (
    BrowserSubagent,
    _default_verification,
    _format_action,
    _format_duration_ms,
    _format_video_timestamp,
    _to_artifact_relative_path,
    build_structured_timeline,
    compact_debug_state,
    compact_elements,
    compact_operator_messages,
    detect_guidance_request,
)
from naumi_agent.tools.browser.subagent.planner import LLMPlanner

# ---------------------------------------------------------------------------
# Compaction helpers
# ---------------------------------------------------------------------------


class TestCompactElements:
    def test_compacts_to_limited_fields(self):
        elements = [
            {
                "id": 1,
                "tag": "input",
                "text": "Search",
                "placeholder": "Type here",
                "ariaLabel": "search-box",
                "role": "search",
                "type": "text",
                "href": None,
                "extra": "should be dropped",
            }
        ]
        result = compact_elements(elements)
        assert len(result) == 1
        assert result[0]["id"] == 1
        assert result[0]["tag"] == "input"
        assert "extra" not in result[0]

    def test_respects_limit(self):
        elements = [{"id": i, "tag": "div"} for i in range(100)]
        result = compact_elements(elements, limit=10)
        assert len(result) == 10

    def test_empty_list(self):
        assert compact_elements([]) == []


class TestCompactDebugState:
    def test_none_returns_defaults(self):
        result = compact_debug_state(None)
        assert result["recentConsole"] == []
        assert result["recentNetwork"] == []
        assert result["recentErrors"] == []
        assert result["counts"]["console"] == 0

    def test_truncates_entries(self):
        ds = {
            "recentConsole": [{"msg": f"c{i}"} for i in range(20)],
            "recentNetwork": [],
            "recentErrors": [],
            "counts": {"console": 20},
        }
        result = compact_debug_state(ds, limit=5)
        assert len(result["recentConsole"]) == 5
        assert result["recentConsole"][0]["msg"] == "c15"

    def test_preserves_capabilities(self):
        ds = {"capabilities": {"cdp": True}, "counts": {}}
        result = compact_debug_state(ds)
        assert result["capabilities"]["cdp"] is True


class TestCompactOperatorMessages:
    def test_compacts_to_limited_fields(self):
        msgs = [
            {
                "role": "subagent",
                "content": "Need help",
                "timestamp": "2025-01-01",
                "extra": "drop me",
            }
        ]
        result = compact_operator_messages(msgs)
        assert len(result) == 1
        assert result[0]["role"] == "subagent"
        assert "extra" not in result[0]

    def test_respects_limit(self):
        msgs = [{"role": "u", "content": f"m{i}"} for i in range(20)]
        result = compact_operator_messages(msgs, limit=3)
        assert len(result) == 3

    def test_none_messages(self):
        result = compact_operator_messages(None)
        assert result == []


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


class TestFormatAction:
    def test_none_action(self):
        assert _format_action(None) == "no-op"

    def test_action_with_url(self):
        assert _format_action({"type": "goto", "url": "https://x.com"}) == "goto (https://x.com)"

    def test_action_with_text(self):
        assert _format_action({"type": "type", "text": "hello"}) == "type (hello)"

    def test_action_with_key(self):
        assert _format_action({"type": "keypress", "key": "Enter"}) == "keypress (Enter)"

    def test_action_with_direction(self):
        assert _format_action({"type": "scroll", "direction": "down"}) == "scroll (down)"

    def test_action_with_id_only(self):
        assert _format_action({"type": "click", "id": 5}) == "click (5)"

    def test_action_no_detail(self):
        assert _format_action({"type": "finish"}) == "finish"


class TestFormatDurationMs:
    def test_none(self):
        assert _format_duration_ms(None) == "unknown"

    def test_negative(self):
        assert _format_duration_ms(-1) == "unknown"

    def test_milliseconds(self):
        assert _format_duration_ms(500) == "500ms"

    def test_seconds(self):
        assert _format_duration_ms(2500) == "2.50s"

    def test_zero(self):
        assert _format_duration_ms(0) == "0ms"


class TestFormatVideoTimestamp:
    def test_none(self):
        assert _format_video_timestamp(None) == "00:00"

    def test_negative(self):
        assert _format_video_timestamp(-5) == "00:00"

    def test_seconds(self):
        assert _format_video_timestamp(65) == "01:05"

    def test_zero(self):
        assert _format_video_timestamp(0) == "00:00"


class TestToArtifactRelativePath:
    def test_none_absolute(self):
        assert _to_artifact_relative_path(None, "/dir") is None

    def test_none_session_returns_absolute(self):
        assert _to_artifact_relative_path("/abs", None) == "/abs"

    def test_relative_within_session(self):
        result = _to_artifact_relative_path(
            "/sessions/abc/file.json", "/sessions/abc"
        )
        assert result == "file.json"

    def test_outside_session_returns_absolute(self):
        result = _to_artifact_relative_path(
            "/other/path/file.json", "/sessions/abc"
        )
        assert result == "/other/path/file.json"


class TestDefaultVerification:
    def test_structure(self):
        v = _default_verification()
        assert v["goal_status"] == "incomplete"
        assert v["confidence"] == "low"
        assert "Verification was skipped" in v["summary"]


# ---------------------------------------------------------------------------
# Guidance detection
# ---------------------------------------------------------------------------


class TestDetectGuidanceRequest:
    def test_no_guidance_needed(self):
        result = detect_guidance_request(
            task_instruction="Search for cats",
            page={"title": "Search", "url": "https://google.com"},
            elements=[{"text": "Search box"}],
            debug_state=None,
            raised_signals=set(),
        )
        assert result is None

    def test_detects_page_errors(self):
        result = detect_guidance_request(
            task_instruction="Browse news",
            page=None,
            elements=[],
            debug_state={"recentErrors": [{"message": "TypeError: X"}]},
            raised_signals=set(),
        )
        assert result is not None
        assert result["signal"] == "page_errors"

    def test_page_errors_not_raised_twice(self):
        result = detect_guidance_request(
            task_instruction="Browse news",
            page=None,
            elements=[],
            debug_state={"recentErrors": [{"message": "err"}]},
            raised_signals={"page_errors"},
        )
        assert result is None

    def test_detects_login_wall(self):
        result = detect_guidance_request(
            task_instruction="Read article",
            page={"title": "Sign In", "url": "https://x.com/login"},
            elements=[{"text": "Sign in to continue"}],
            debug_state=None,
            raised_signals=set(),
        )
        assert result is not None
        assert result["signal"] == "login_wall"

    def test_login_not_detected_for_auth_tasks(self):
        result = detect_guidance_request(
            task_instruction="Login to the website",
            page={"title": "Sign In", "url": "https://x.com/login"},
            elements=[{"text": "Sign in"}],
            debug_state=None,
            raised_signals=set(),
        )
        assert result is None

    def test_detects_permission_prompt(self):
        result = detect_guidance_request(
            task_instruction="Read news",
            page=None,
            elements=[{"text": "Allow notifications"}],
            debug_state=None,
            raised_signals=set(),
        )
        assert result is not None
        assert result["signal"] == "permission_prompt"

    def test_permission_not_detected_for_perm_tasks(self):
        result = detect_guidance_request(
            task_instruction="Test camera permission",
            page=None,
            elements=[{"text": "Allow camera"}],
            debug_state=None,
            raised_signals=set(),
        )
        assert result is None

    def test_errors_take_priority_over_login(self):
        result = detect_guidance_request(
            task_instruction="Read article",
            page={"title": "Sign In"},
            elements=[{"text": "Sign in"}],
            debug_state={"recentErrors": [{"message": "err"}]},
            raised_signals=set(),
        )
        assert result["signal"] == "page_errors"


# ---------------------------------------------------------------------------
# Timeline builder
# ---------------------------------------------------------------------------


class TestBuildStructuredTimeline:
    def test_basic_structure(self):
        result = {
            "status": "completed",
            "summary": "Done",
            "page": {"url": "https://example.com"},
            "artifacts": {
                "sessionDir": "/sessions/abc",
                "videoFiles": ["/sessions/abc/video.webm"],
                "eventsPath": "/sessions/abc/events.json",
                "tracePath": "/sessions/abc/trace.json",
            },
            "history": [
                {
                    "step": 1,
                    "action": {"type": "goto", "url": "https://example.com"},
                    "summary": "Navigated",
                    "thinking": "Need to go to page",
                    "actionResult": {"screenshotPath": "/sessions/abc/s1.png"},
                    "verification": {"goal_status": "incomplete"},
                    "videoOffsetSeconds": 0.5,
                }
            ],
        }
        timeline = build_structured_timeline(result)

        assert timeline["schemaVersion"] == 1
        assert timeline["status"] == "completed"
        assert len(timeline["steps"]) == 1
        assert timeline["steps"][0]["actionType"] == "goto"
        assert timeline["steps"][0]["artifacts"]["screenshotPath"] == "s1.png"
        assert timeline["session"]["primaryVideoPath"] == "video.webm"

    def test_empty_history(self):
        result = {
            "status": "failed",
            "history": [],
            "artifacts": {},
        }
        timeline = build_structured_timeline(result)
        assert timeline["steps"] == []

    def test_null_video_offset(self):
        result = {
            "status": "completed",
            "history": [
                {"step": 1, "action": {"type": "click"}, "videoOffsetSeconds": None},
            ],
            "artifacts": {},
        }
        timeline = build_structured_timeline(result)
        assert timeline["steps"][0]["timestamps"]["videoTimestamp"] is None


# ---------------------------------------------------------------------------
# BrowserSubagent
# ---------------------------------------------------------------------------


def _make_mock_runtime():
    rt = MagicMock()
    rt.ensure_started = AsyncMock()
    rt.observe = AsyncMock(return_value={"elements": [], "tabs": []})
    rt.get_page_metadata = AsyncMock(return_value={
        "url": "https://example.com",
        "title": "Example",
    })
    rt.get_debug_state = MagicMock(return_value={
        "recentConsole": [],
        "recentNetwork": [],
        "recentErrors": [],
        "capabilities": None,
        "counts": {},
    })
    rt.record_event = MagicMock()
    rt.write_artifact_json = MagicMock(return_value="/tmp/report.json")
    rt.write_artifact_text = MagicMock(return_value="/tmp/walkthrough.md")
    rt.screenshot_base64 = AsyncMock(return_value="base64data")
    return rt


def _make_mock_planner():
    planner = AsyncMock(spec=LLMPlanner)
    planner.decide = AsyncMock()
    planner.verify = AsyncMock()
    return planner


class TestDelegateTaskCompleted:
    @pytest.mark.asyncio
    async def test_completes_on_first_step(self):
        rt = _make_mock_runtime()
        planner = _make_mock_planner()
        planner.decide.return_value = {
            "status": "completed",
            "summary": "Already done",
            "next_action": {"type": "finish"},
        }

        subagent = BrowserSubagent(rt, planner)
        result = await subagent.delegate_task("Open example.com")

        assert result["status"] == "completed"
        assert result["summary"] == "Already done"
        assert result["step"] == 1

    @pytest.mark.asyncio
    async def test_completes_via_verification(self):
        rt = _make_mock_runtime()
        planner = _make_mock_planner()

        # Step 1: click, verification says completed
        planner.decide.return_value = {
            "status": "continue",
            "summary": "Clicking button",
            "thinking": "Need to click",
            "next_action": {"type": "click", "id": 1},
        }
        planner.verify.return_value = {
            "goal_status": "completed",
            "confidence": "high",
            "summary": "Task done after click",
        }
        rt.click = AsyncMock(return_value={"success": True})

        subagent = BrowserSubagent(rt, planner)
        result = await subagent.delegate_task("Click the button")

        assert result["status"] == "completed"
        assert result["step"] == 1
        assert len(result["history"]) == 1


class TestDelegateTaskFailed:
    @pytest.mark.asyncio
    async def test_fails_on_planner_failure(self):
        rt = _make_mock_runtime()
        planner = _make_mock_planner()
        planner.decide.return_value = {
            "status": "failed",
            "summary": "Cannot proceed",
            "next_action": {"type": "fail", "reason": "Blocked"},
        }

        subagent = BrowserSubagent(rt, planner)
        result = await subagent.delegate_task("Impossible task")

        assert result["status"] == "failed"
        assert "Cannot proceed" in result["summary"]

    @pytest.mark.asyncio
    async def test_fails_on_blocked_verification(self):
        rt = _make_mock_runtime()
        planner = _make_mock_planner()

        planner.decide.return_value = {
            "status": "continue",
            "summary": "Trying",
            "next_action": {"type": "scroll", "direction": "down"},
        }
        planner.verify.return_value = {
            "goal_status": "blocked",
            "confidence": "high",
            "summary": "Cannot proceed further",
        }
        rt.scroll = AsyncMock(return_value={"success": True})

        subagent = BrowserSubagent(rt, planner)
        result = await subagent.delegate_task("Blocked task")

        assert result["status"] == "failed"

    @pytest.mark.asyncio
    async def test_exhausts_steps(self):
        rt = _make_mock_runtime()
        planner = _make_mock_planner()

        planner.decide.return_value = {
            "status": "continue",
            "summary": "Still working",
            "next_action": {"type": "scroll", "direction": "down"},
        }
        planner.verify.return_value = {
            "goal_status": "incomplete",
            "confidence": "low",
            "summary": "Not done yet",
        }
        rt.scroll = AsyncMock(return_value={"success": True})

        subagent = BrowserSubagent(rt, planner, default_max_steps=2)
        result = await subagent.delegate_task("Never-ending task")

        assert result["status"] == "failed"
        assert "2 steps" in result["summary"]


class TestDelegateTaskAborted:
    @pytest.mark.asyncio
    async def test_aborted_via_callback(self):
        rt = _make_mock_runtime()
        planner = _make_mock_planner()

        call_count = 0

        def should_abort():
            nonlocal call_count
            call_count += 1
            return call_count >= 2

        planner.decide.return_value = {
            "status": "continue",
            "summary": "Working",
            "next_action": {"type": "scroll", "direction": "down"},
        }
        planner.verify.return_value = {
            "goal_status": "incomplete",
            "summary": "Not done",
        }
        rt.scroll = AsyncMock(return_value={"success": True})

        subagent = BrowserSubagent(rt, planner)
        result = await subagent.delegate_task(
            "Abort test",
            options={
                "shouldAbort": should_abort,
                "getAbortReason": lambda: "User cancelled",
            },
        )

        assert result["status"] == "aborted"
        assert result["summary"] == "User cancelled"


class TestDelegateTaskNeedsInput:
    @pytest.mark.asyncio
    async def test_needs_input_without_handler_fails(self):
        rt = _make_mock_runtime()
        planner = _make_mock_planner()

        planner.decide.return_value = {
            "status": "needs_input",
            "summary": "Need guidance",
            "next_action": {
                "type": "ask_main_agent",
                "question": "What should I do?",
            },
        }

        subagent = BrowserSubagent(rt, planner)
        result = await subagent.delegate_task("Confusing task")

        assert result["status"] == "failed"
        assert result["pendingInput"] is not None

    @pytest.mark.asyncio
    async def test_needs_input_with_handler_resumes(self):
        rt = _make_mock_runtime()
        planner = _make_mock_planner()

        decide_count = 0

        async def mock_decide(inp):
            nonlocal decide_count
            decide_count += 1
            if decide_count == 1:
                return {
                    "status": "needs_input",
                    "summary": "Need guidance",
                    "next_action": {
                        "type": "ask_main_agent",
                        "question": "Continue?",
                        "suggested_reply": "Yes",
                    },
                }
            return {
                "status": "completed",
                "summary": "Done after guidance",
                "next_action": {"type": "finish"},
            }

        planner.decide.side_effect = mock_decide

        on_needs_input = AsyncMock(return_value="Yes, continue")

        subagent = BrowserSubagent(rt, planner)
        result = await subagent.delegate_task(
            "Guided task",
            options={"onNeedsInput": on_needs_input},
        )

        assert result["status"] == "completed"
        assert on_needs_input.called

    @pytest.mark.asyncio
    async def test_needs_input_abort_reply(self):
        rt = _make_mock_runtime()
        planner = _make_mock_planner()

        planner.decide.return_value = {
            "status": "needs_input",
            "summary": "Need guidance",
            "next_action": {"type": "ask_main_agent"},
        }

        on_needs_input = AsyncMock(return_value={"abort": True, "reason": "Stop it"})

        subagent = BrowserSubagent(rt, planner)
        result = await subagent.delegate_task(
            "Abort via input",
            options={"onNeedsInput": on_needs_input},
        )

        assert result["status"] == "aborted"
        assert "Stop it" in result["summary"]


class TestCaptchaHandling:
    @pytest.mark.asyncio
    async def test_captcha_escalates_after_5_attempts(self):
        rt = _make_mock_runtime()
        planner = _make_mock_planner()

        # CAPTCHA detected on each observe
        rt.observe = AsyncMock(return_value={
            "elements": [],
            "tabs": [],
            "captchaChallenge": [{"label": "recaptcha"}],
        })

        decide_count = 0

        async def mock_decide(inp):
            nonlocal decide_count
            decide_count += 1
            if decide_count <= 4:
                return {
                    "status": "continue",
                    "summary": "Trying CAPTCHA",
                    "next_action": {"type": "click", "id": 1},
                }
            # 5th attempt triggers escalation
            return {
                "status": "continue",
                "summary": "Still CAPTCHA",
                "next_action": {"type": "click", "id": 1},
            }

        planner.decide.side_effect = mock_decide
        planner.verify.return_value = {
            "goal_status": "incomplete",
            "summary": "Not solved",
        }
        rt.click = AsyncMock(return_value={"success": True})

        # No input handler — escalation will fail the task
        subagent = BrowserSubagent(rt, planner, default_max_steps=10)
        result = await subagent.delegate_task("Solve CAPTCHA")

        # Should fail because no handler after 5 CAPTCHA steps
        assert result["status"] == "failed"
        assert subagent._captcha_steps >= 5 or result.get("pendingInput") is not None


class TestActionExecution:
    @pytest.mark.asyncio
    async def test_goto_action(self):
        rt = _make_mock_runtime()
        rt.goto = AsyncMock(return_value={"success": True})
        planner = _make_mock_planner()

        planner.decide.return_value = {
            "status": "continue",
            "summary": "Navigating",
            "next_action": {"type": "goto", "url": "https://x.com"},
        }
        planner.verify.return_value = {
            "goal_status": "completed",
            "summary": "Arrived",
        }

        subagent = BrowserSubagent(rt, planner)
        result = await subagent.delegate_task("Go to x.com")
        assert result["status"] == "completed"
        rt.goto.assert_called_once_with("https://x.com")

    @pytest.mark.asyncio
    async def test_type_action(self):
        rt = _make_mock_runtime()
        rt.type_text = AsyncMock(return_value={"success": True})
        planner = _make_mock_planner()

        planner.decide.return_value = {
            "status": "continue",
            "summary": "Typing",
            "next_action": {"type": "type", "id": 3, "text": "hello"},
        }
        planner.verify.return_value = {
            "goal_status": "completed",
            "summary": "Typed",
        }

        subagent = BrowserSubagent(rt, planner)
        await subagent.delegate_task("Type hello")
        rt.type_text.assert_called_once_with(3, "hello")

    @pytest.mark.asyncio
    async def test_keypress_action(self):
        rt = _make_mock_runtime()
        rt.keypress = AsyncMock(return_value={"success": True})
        planner = _make_mock_planner()

        planner.decide.return_value = {
            "status": "continue",
            "summary": "Pressing Enter",
            "next_action": {"type": "keypress", "key": "Enter"},
        }
        planner.verify.return_value = {
            "goal_status": "completed",
            "summary": "Pressed",
        }

        subagent = BrowserSubagent(rt, planner)
        await subagent.delegate_task("Press Enter")
        rt.keypress.assert_called_once_with("Enter")

    @pytest.mark.asyncio
    async def test_switch_tab_action(self):
        rt = _make_mock_runtime()
        rt.tab_action = AsyncMock(return_value={"success": True})
        planner = _make_mock_planner()

        planner.decide.return_value = {
            "status": "continue",
            "summary": "Switching tab",
            "next_action": {"type": "switch_tab", "index": 1},
        }
        planner.verify.return_value = {
            "goal_status": "completed",
            "summary": "Switched",
        }

        subagent = BrowserSubagent(rt, planner)
        await subagent.delegate_task("Switch to tab 1")
        rt.tab_action.assert_called_once_with("select", index=1)

    @pytest.mark.asyncio
    async def test_unsupported_action_raises(self):
        rt = _make_mock_runtime()
        planner = _make_mock_planner()

        planner.decide.return_value = {
            "status": "continue",
            "summary": "Bad action",
            "next_action": {"type": "unknown_action"},
        }

        subagent = BrowserSubagent(rt, planner, default_max_steps=1)
        with pytest.raises(ValueError, match="Unsupported subagent action"):
            await subagent.delegate_task("Bad action")


class TestExternalHandoff:
    @pytest.mark.asyncio
    async def test_handoff_without_handler_fails(self):
        rt = _make_mock_runtime()
        planner = _make_mock_planner()

        pull_count = 0

        def pull():
            nonlocal pull_count
            pull_count += 1
            if pull_count == 1:
                return {
                    "mode": "manual_control",
                    "question": "Take over?",
                }
            return None

        subagent = BrowserSubagent(rt, planner)
        result = await subagent.delegate_task(
            "Handoff test",
            options={"pullHandoffRequest": pull},
        )

        # Without on_needs_input, handoff results in failure
        assert result["status"] == "failed"
        assert result.get("pendingInput") is not None

    @pytest.mark.asyncio
    async def test_handoff_with_handler_resumes(self):
        rt = _make_mock_runtime()
        planner = _make_mock_planner()

        decide_count = 0

        async def mock_decide(inp):
            nonlocal decide_count
            decide_count += 1
            if decide_count == 1:
                # After handoff resumes, second decide completes
                pass
            return {
                "status": "completed",
                "summary": "Done after handoff",
                "next_action": {"type": "finish"},
            }

        planner.decide.side_effect = mock_decide

        pull_count = 0

        def pull():
            nonlocal pull_count
            pull_count += 1
            if pull_count == 1:
                return {
                    "mode": "manual_control",
                    "question": "Take over?",
                }
            return None

        on_needs_input = AsyncMock(return_value="Done, continue")

        subagent = BrowserSubagent(rt, planner)
        result = await subagent.delegate_task(
            "Handoff test",
            options={
                "pullHandoffRequest": pull,
                "onNeedsInput": on_needs_input,
            },
        )

        assert result["status"] == "completed"
        assert on_needs_input.called


class TestProgressCallback:
    @pytest.mark.asyncio
    async def test_progress_called_after_action(self):
        rt = _make_mock_runtime()
        planner = _make_mock_planner()

        planner.decide.return_value = {
            "status": "continue",
            "summary": "Working",
            "next_action": {"type": "scroll", "direction": "down"},
        }
        planner.verify.return_value = {
            "goal_status": "completed",
            "summary": "Done",
        }
        rt.scroll = AsyncMock(return_value={"success": True})

        progress_snapshots = []
        on_progress = AsyncMock(side_effect=progress_snapshots.append)

        subagent = BrowserSubagent(rt, planner)
        await subagent.delegate_task(
            "Progress test",
            options={"onProgress": on_progress},
        )

        assert len(progress_snapshots) >= 1
        snap = progress_snapshots[0]
        assert snap["status"] == "running"
        assert snap["step"] == 1


class TestReportWriting:
    @pytest.mark.asyncio
    async def test_reports_included_in_result(self):
        rt = _make_mock_runtime()
        planner = _make_mock_planner()

        planner.decide.return_value = {
            "status": "completed",
            "summary": "Done",
            "next_action": {"type": "finish"},
        }

        subagent = BrowserSubagent(rt, planner)
        result = await subagent.delegate_task("Report test")

        assert "reports" in result
        assert "reportJsonPath" in result["reports"]
        assert "walkthroughPath" in result["reports"]
        assert "timelineJsonPath" in result["reports"]
        rt.write_artifact_json.assert_called()
        rt.write_artifact_text.assert_called()
