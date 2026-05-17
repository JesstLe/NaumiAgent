"""Autonomous browser subagent with LLM planning and CAPTCHA handling.

Ported from browser-debugging-daemon/scripts/subagent/BrowserSubagent.js (748 lines).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from ..runtime.browser_runtime import BrowserRuntime
from .planner import LLMPlanner

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Compaction helpers
# ---------------------------------------------------------------------------


def compact_elements(
    elements: list[dict[str, Any]], limit: int = 50
) -> list[dict[str, Any]]:
    return [
        {
            "id": el.get("id"),
            "tag": el.get("tag"),
            "text": el.get("text"),
            "placeholder": el.get("placeholder"),
            "ariaLabel": el.get("ariaLabel"),
            "role": el.get("role"),
            "type": el.get("type"),
            "href": el.get("href"),
        }
        for el in elements[:limit]
    ]


def compact_debug_state(
    debug_state: dict[str, Any] | None, limit: int = 8
) -> dict[str, Any]:
    if not debug_state:
        return {
            "recentConsole": [],
            "recentNetwork": [],
            "recentErrors": [],
            "capabilities": None,
            "counts": {
                "console": 0,
                "network": 0,
                "errors": 0,
                "observedElements": 0,
            },
        }
    return {
        "recentConsole": (debug_state.get("recentConsole") or [])[
            -limit:
        ],
        "recentNetwork": (debug_state.get("recentNetwork") or [])[
            -limit:
        ],
        "recentErrors": (debug_state.get("recentErrors") or [])[
            -limit:
        ],
        "capabilities": debug_state.get("capabilities"),
        "counts": debug_state.get("counts", {}),
    }


def compact_operator_messages(
    messages: list[dict[str, Any]], limit: int = 8
) -> list[dict[str, Any]]:
    return [
        {
            "role": m.get("role"),
            "content": m.get("content"),
            "timestamp": m.get("timestamp"),
        }
        for m in (messages or [])[-limit:]
    ]


def _default_verification() -> dict[str, Any]:
    return {
        "goal_status": "incomplete",
        "confidence": "low",
        "summary": (
            "Verification was skipped because no verifier "
            "was configured."
        ),
        "evidence": [],
        "next_hint": "",
    }


def _format_action(action: dict[str, Any] | None) -> str:
    if not action:
        return "no-op"
    detail = (
        action.get("url")
        or action.get("text")
        or action.get("key")
        or action.get("direction")
        or action.get("id")
        or ""
    )
    if detail:
        return f"{action.get('type', '?')} ({detail})"
    return action.get("type", "unknown")


def _format_duration_ms(ms: float | int | None) -> str:
    if ms is None or ms < 0:
        return "unknown"
    if ms < 1000:
        return f"{round(ms)}ms"
    return f"{ms / 1000:.2f}s"


def _format_video_timestamp(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "00:00"
    total = int(seconds)
    mins = total // 60
    secs = total % 60
    return f"{mins:02d}:{secs:02d}"


def _to_artifact_relative_path(
    absolute_path: str | None,
    session_dir: str | None,
) -> str | None:
    if not absolute_path or not session_dir:
        return absolute_path or None
    try:
        rel = Path(absolute_path).relative_to(session_dir)
        if str(rel).startswith(".."):
            return absolute_path
        return str(rel).replace("\\", "/")
    except (ValueError, TypeError):
        return absolute_path


def _includes_any(text: str, patterns: list[str]) -> bool:
    normalized = (text or "").lower()
    return any(p in normalized for p in patterns)


def _collect_element_text(
    elements: list[dict[str, Any]],
) -> str:
    parts: list[str] = []
    for el in elements:
        parts.extend([
            el.get("text", ""),
            el.get("placeholder", ""),
            el.get("ariaLabel", ""),
            el.get("href", ""),
            el.get("role", ""),
            el.get("type", ""),
        ])
    return " ".join(p for p in parts if p).lower()


# ---------------------------------------------------------------------------
# Guidance detection
# ---------------------------------------------------------------------------


def detect_guidance_request(
    *,
    task_instruction: str,
    page: dict[str, Any] | None,
    elements: list[dict[str, Any]],
    debug_state: dict[str, Any] | None,
    raised_signals: set[str],
) -> dict[str, Any] | None:
    page_text = " ".join([
        (page or {}).get("title", ""),
        (page or {}).get("url", ""),
        (page or {}).get("textPreview", ""),
    ]).lower()
    element_text = _collect_element_text(elements)
    combined_text = f"{page_text} {element_text}"
    task_text = (task_instruction or "").lower()

    if (
        "page_errors" not in raised_signals
        and (debug_state or {}).get("recentErrors")
    ):
        error_summary = " | ".join(
            e.get("message", "Unknown page error")
            for e in (debug_state["recentErrors"] or [])[-2:]
        )
        return {
            "signal": "page_errors",
            "question": (
                "The page is throwing runtime errors. Should I "
                "keep going, refresh, or stop for inspection?"
            ),
            "details": error_summary,
            "suggestedReply": (
                "Stop and inspect the page errors before "
                "continuing."
            ),
        }

    login_patterns = [
        "sign in", "log in", "login", "sign up", "password",
        "continue with google", "continue with github",
        "continue with apple", "enter your email",
    ]
    task_expects_auth = _includes_any(
        task_text,
        [
            "login", "log in", "sign in", "authenticate",
            "auth", "credentials",
        ],
    )
    if (
        "login_wall" not in raised_signals
        and not task_expects_auth
        and _includes_any(combined_text, login_patterns)
    ):
        return {
            "signal": "login_wall",
            "question": (
                "I hit a login or authentication wall. Do you "
                "want to provide guidance or take over manually?"
            ),
            "details": (
                "The page content looks like a sign-in flow "
                "rather than the intended task surface."
            ),
            "suggestedReply": (
                "Pause here and let me handle login manually."
            ),
        }

    permission_patterns = [
        "allow notifications", "show notifications",
        "allow location", "use your location",
        "allow camera", "allow microphone",
        "permission", "notifications", "microphone",
        "camera", "location access",
    ]
    task_expects_perm = _includes_any(
        task_text,
        [
            "notification", "permission", "camera",
            "microphone", "location",
        ],
    )
    if (
        "permission_prompt" not in raised_signals
        and not task_expects_perm
        and _includes_any(combined_text, permission_patterns)
    ):
        return {
            "signal": "permission_prompt",
            "question": (
                "A browser permission prompt may need a human "
                "decision. Should I continue, deny it, or stop?"
            ),
            "details": (
                "The page appears to be asking for browser "
                "permissions such as notifications, camera, "
                "microphone, or location."
            ),
            "suggestedReply": (
                "Deny the permission and continue with the task."
            ),
        }

    return None


# ---------------------------------------------------------------------------
# Timeline builder
# ---------------------------------------------------------------------------


def build_structured_timeline(result: dict[str, Any]) -> dict[str, Any]:
    session_dir = (result.get("artifacts") or {}).get("sessionDir")
    video_files = (result.get("artifacts") or {}).get("videoFiles")
    primary_video = (
        video_files[0]
        if video_files
        else None
    )
    primary_rel = _to_artifact_relative_path(
        primary_video, session_dir
    )
    history = result.get("history") or []

    steps: list[dict[str, Any]] = []
    for i, entry in enumerate(history):
        ar = entry.get("actionResult") or {}
        screenshot_rel = _to_artifact_relative_path(
            ar.get("screenshotPath"), session_dir
        )
        video_offset = entry.get("videoOffsetSeconds")
        video_anchor = (
            max(0, int(video_offset))
            if isinstance(video_offset, (int, float))
            else None
        )
        steps.append({
            "index": i,
            "step": entry.get("step"),
            "actionType": (entry.get("action") or {}).get(
                "type", "unknown"
            ),
            "action": entry.get("action"),
            "plannerSummary": entry.get("summary", ""),
            "plannerThinking": entry.get("thinking", ""),
            "verification": entry.get("verification"),
            "timestamps": {
                "startedAt": entry.get("stepStartedAt"),
                "finishedAt": entry.get("stepFinishedAt"),
                "actionDurationMs": entry.get("actionDurationMs"),
                "elapsedMs": entry.get("elapsedMs"),
                "videoOffsetSeconds": video_offset,
                "videoTimestamp": (
                    _format_video_timestamp(video_offset)
                    if isinstance(video_offset, (int, float))
                    else None
                ),
            },
            "page": entry.get("page"),
            "artifacts": {
                "screenshotPath": screenshot_rel,
                "videoPath": primary_rel,
                "videoJump": (
                    f"{primary_rel}#t={video_anchor}"
                    if primary_rel and video_anchor is not None
                    else None
                ),
                "eventsPath": _to_artifact_relative_path(
                    (result.get("artifacts") or {}).get("eventsPath"),
                    session_dir,
                ),
                "tracePath": _to_artifact_relative_path(
                    (result.get("artifacts") or {}).get("tracePath"),
                    session_dir,
                ),
            },
        })

    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now().isoformat(),
        "status": result.get("status"),
        "summary": result.get("summary"),
        "finalPage": result.get("page"),
        "session": {
            "sessionDir": session_dir,
            "primaryVideoPath": primary_rel,
            "eventsPath": _to_artifact_relative_path(
                (result.get("artifacts") or {}).get("eventsPath"),
                session_dir,
            ),
            "tracePath": _to_artifact_relative_path(
                (result.get("artifacts") or {}).get("tracePath"),
                session_dir,
            ),
        },
        "steps": steps,
    }


# ---------------------------------------------------------------------------
# BrowserSubagent
# ---------------------------------------------------------------------------


class BrowserSubagent:
    def __init__(
        self,
        runtime: BrowserRuntime,
        planner: LLMPlanner,
        *,
        default_max_steps: int = 12,
    ) -> None:
        self.runtime = runtime
        self.planner = planner
        self.default_max_steps = default_max_steps
        self._captcha_steps: int = 0

    async def delegate_task(
        self,
        task_instruction: str,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        options = options or {}
        max_steps = options.get("maxSteps", self.default_max_steps)
        on_progress = options.get("onProgress")
        on_needs_input = options.get("onNeedsInput")
        should_abort: Callable[[], bool] | None = options.get(
            "shouldAbort"
        )
        get_abort_reason: Callable[[], str] | None = options.get(
            "getAbortReason"
        )
        pull_handoff: Callable[[], dict | None] | None = options.get(
            "pullHandoffRequest"
        )
        start_options: dict[str, Any] = options.get(
            "startOptions", {}
        )

        history: list[dict[str, Any]] = []
        operator_messages: list[dict[str, Any]] = []
        raised_signals: set[str] = set()
        task_started_ms = _now_ms()

        await self.runtime.ensure_started(start_options)
        self.runtime.record_event("subagent_task_started", {
            "taskInstruction": task_instruction,
            "maxSteps": max_steps,
        })

        for step in range(1, max_steps + 1):
            if should_abort and should_abort():
                result = await self._build_result(
                    status="aborted",
                    step=step - 1,
                    summary=(
                        (get_abort_reason and get_abort_reason())
                        or "Browser task aborted."
                    ),
                    history=history,
                    page=await self._safe_page_metadata(),
                    verification=None,
                    operator_messages=operator_messages,
                )
                self.runtime.record_event(
                    "subagent_task_aborted", result
                )
                return result

            observation = await self.runtime.observe()
            page = await self.runtime.get_page_metadata()
            debug_state = self.runtime.get_debug_state(10)

            # External handoff
            external = pull_handoff() if pull_handoff else None
            if external:
                outcome = await self._handle_pending_input(
                    step=step,
                    pending_input={
                        **external,
                        "mode": external.get(
                            "mode", "manual_control"
                        ),
                    },
                    page=page,
                    history=history,
                    operator_messages=operator_messages,
                    on_progress=on_progress,
                    on_needs_input=on_needs_input,
                )
                if outcome.get("result"):
                    return outcome["result"]
                continue

            # Guidance detection
            guidance = detect_guidance_request(
                task_instruction=task_instruction,
                page=page,
                elements=observation.get("elements", []),
                debug_state=debug_state,
                raised_signals=raised_signals,
            )
            if guidance:
                raised_signals.add(guidance["signal"])
                outcome = await self._handle_pending_input(
                    step=step,
                    pending_input={
                        "step": step,
                        "question": guidance["question"],
                        "details": guidance["details"],
                        "suggestedReply": guidance["suggestedReply"],
                    },
                    page=page,
                    history=history,
                    operator_messages=operator_messages,
                    on_progress=on_progress,
                    on_needs_input=on_needs_input,
                )
                if outcome.get("result"):
                    return outcome["result"]
                continue

            # CAPTCHA handling
            captcha_info = observation.get("captchaChallenge")
            captcha_hint = None
            captcha_screenshot = None
            if captcha_info:
                types = ", ".join(sorted({
                    c.get("label", "unknown") for c in captcha_info
                }))
                captcha_hint = (
                    f"[CAPTCHA DETECTED: {types}] A human "
                    f"verification challenge is present on the "
                    f"page. Analyze the screenshot image to solve "
                    f"it. Click the correct elements step by step."
                )
                try:
                    captcha_screenshot = (
                        await self.runtime.screenshot_base64()
                    )
                except Exception:
                    pass

                self._captcha_steps += 1
                if self._captcha_steps >= 5:
                    outcome = await self._handle_pending_input(
                        step=step,
                        pending_input={
                            "step": step,
                            "mode": "guidance",
                            "question": (
                                f"CAPTCHA detected: {types}. "
                                f"The planner could not solve it "
                                f"after {self._captcha_steps} "
                                f"attempts. Please solve it "
                                f"manually."
                            ),
                            "details": (
                                "A human verification challenge "
                                "is blocking the task. Switch to "
                                "manual control or solve it in "
                                "the browser, then resume."
                            ),
                            "suggestedReply": (
                                "CAPTCHA solved. Continue the task."
                            ),
                        },
                        page=page,
                        history=history,
                        operator_messages=operator_messages,
                        on_progress=on_progress,
                        on_needs_input=on_needs_input,
                    )
                    if outcome.get("result"):
                        return outcome["result"]
                    self._captcha_steps = 0
                    continue
            else:
                self._captcha_steps = 0

            # Planner decision
            decision = await self.planner.decide({
                "taskInstruction": task_instruction,
                "page": page,
                "elements": compact_elements(
                    observation.get("elements", [])
                ),
                "tabs": observation.get("tabs", []),
                "history": history,
                "operatorMessages": compact_operator_messages(
                    operator_messages
                ),
                "debugState": debug_state,
                "captchaHint": captcha_hint,
                "captchaScreenshot": captcha_screenshot,
            })

            self.runtime.record_event("subagent_decision", {
                "step": step,
                "decision": decision,
            })

            next_action = decision.get("next_action", {})
            status = decision.get("status", "continue")

            if status == "completed" or next_action.get("type") == "finish":
                result = await self._build_result(
                    status="completed",
                    step=step,
                    summary=(
                        decision.get("summary")
                        or next_action.get("result")
                        or "Task completed."
                    ),
                    history=history,
                    page=page,
                    verification=None,
                    operator_messages=operator_messages,
                )
                self.runtime.record_event(
                    "subagent_task_completed", result
                )
                return result

            if status == "failed" or next_action.get("type") == "fail":
                result = await self._build_result(
                    status="failed",
                    step=step,
                    summary=(
                        decision.get("summary")
                        or next_action.get("reason")
                        or "Task failed."
                    ),
                    history=history,
                    page=page,
                    verification=None,
                    operator_messages=operator_messages,
                )
                self.runtime.record_event(
                    "subagent_task_failed", result
                )
                return result

            if (
                status == "needs_input"
                or next_action.get("type") == "ask_main_agent"
            ):
                pending_input = {
                    "step": step,
                    "question": (
                        next_action.get("question")
                        or decision.get("summary")
                        or "Need guidance before continuing."
                    ),
                    "details": (
                        next_action.get("details")
                        or decision.get("thinking", "")
                    ),
                    "suggestedReply": next_action.get(
                        "suggested_reply", ""
                    ),
                }
                outcome = await self._handle_pending_input(
                    step=step,
                    pending_input=pending_input,
                    page=page,
                    history=history,
                    operator_messages=operator_messages,
                    on_progress=on_progress,
                    on_needs_input=on_needs_input,
                )
                if outcome.get("result"):
                    return outcome["result"]
                continue

            # Execute action
            step_started_ms = _now_ms()

            if captcha_hint:
                delay = 0.6 + random.random() * 1.4
                await asyncio.sleep(delay)

            action_result = await self._execute_action(next_action)
            post_obs = await self.runtime.observe()
            post_page = await self.runtime.get_page_metadata()
            post_debug = self.runtime.get_debug_state(10)

            verification = await self._verify_progress(
                task_instruction=task_instruction,
                page=post_page,
                elements=compact_elements(
                    post_obs.get("elements", [])
                ),
                history=history,
                last_action=next_action,
                last_action_result=action_result,
                debug_state=post_debug,
                captcha_screenshot=captcha_screenshot,
            )

            self.runtime.record_event("subagent_verification", {
                "step": step,
                "verification": verification,
            })

            step_finished_ms = _now_ms()

            history.append({
                "step": step,
                "stepStartedAt": _to_iso(step_started_ms),
                "stepFinishedAt": _to_iso(step_finished_ms),
                "actionDurationMs": max(
                    0, step_finished_ms - step_started_ms
                ),
                "elapsedMs": max(
                    0, step_finished_ms - task_started_ms
                ),
                "videoOffsetSeconds": max(
                    0, (step_started_ms - task_started_ms) / 1000
                ),
                "thinking": decision.get("thinking", ""),
                "summary": decision.get("summary", ""),
                "action": next_action,
                "actionResult": action_result,
                "verification": verification,
                "page": post_page,
                "debug": compact_debug_state(post_debug),
            })

            if on_progress:
                await on_progress(
                    self._build_progress_snapshot(
                        status="running",
                        step=step,
                        summary=(
                            verification.get("summary")
                            or decision.get("summary")
                            or f"Completed step {step}."
                        ),
                        history=history,
                        page=post_page,
                        verification=verification,
                        operator_messages=operator_messages,
                        pending_input=None,
                    )
                )

            if verification.get("goal_status") == "completed":
                result = await self._build_result(
                    status="completed",
                    step=step,
                    summary=(
                        verification.get("summary")
                        or decision.get("summary")
                        or "Task completed."
                    ),
                    history=history,
                    page=post_page,
                    verification=verification,
                    operator_messages=operator_messages,
                )
                self.runtime.record_event(
                    "subagent_task_completed", result
                )
                return result

            if verification.get("goal_status") == "blocked":
                result = await self._build_result(
                    status="failed",
                    step=step,
                    summary=(
                        verification.get("summary")
                        or "Task is blocked."
                    ),
                    history=history,
                    page=post_page,
                    verification=verification,
                    operator_messages=operator_messages,
                )
                self.runtime.record_event(
                    "subagent_task_failed", result
                )
                return result

        # Exhausted steps
        result = await self._build_result(
            status="failed",
            step=max_steps,
            summary=(
                f"Stopped after {max_steps} steps without "
                f"completing the task."
            ),
            history=history,
            page=await self._safe_page_metadata(),
            verification=None,
            operator_messages=operator_messages,
        )
        self.runtime.record_event("subagent_task_failed", result)
        return result

    # ── Verification ──

    async def _verify_progress(self, **kwargs: Any) -> dict[str, Any]:
        try:
            return await self.planner.verify(kwargs)
        except Exception as exc:
            return {
                "goal_status": "incomplete",
                "confidence": "low",
                "summary": f"Verification failed: {exc}",
                "evidence": [],
                "next_hint": "Continue with caution.",
            }

    # ── Pending input / handoff ──

    async def _handle_pending_input(
        self,
        *,
        step: int,
        pending_input: dict[str, Any],
        page: dict[str, Any] | None,
        history: list[dict[str, Any]],
        operator_messages: list[dict[str, Any]],
        on_progress: Any,
        on_needs_input: Any,
    ) -> dict[str, Any]:
        mode = pending_input.get("mode", "guidance")
        waiting_status = (
            "manual_control"
            if mode == "manual_control"
            else "waiting_for_instruction"
        )

        request_msg = {
            "role": (
                "system" if mode == "manual_control" else "subagent"
            ),
            "content": pending_input.get("question", ""),
            "timestamp": datetime.now().isoformat(),
        }
        operator_messages.append(request_msg)
        self.runtime.record_event(
            "subagent_input_requested", pending_input
        )

        if on_progress:
            await on_progress(
                self._build_progress_snapshot(
                    status=waiting_status,
                    step=step,
                    summary=pending_input.get("question", ""),
                    history=history,
                    page=page,
                    verification=None,
                    operator_messages=operator_messages,
                    pending_input=pending_input,
                )
            )

        if not on_needs_input:
            result = await self._build_result(
                status="failed",
                step=step,
                summary=pending_input.get("question", ""),
                history=history,
                page=page,
                verification=None,
                operator_messages=operator_messages,
                pending_input=pending_input,
            )
            self.runtime.record_event(
                "subagent_task_failed", result
            )
            return {"result": result}

        reply = await on_needs_input(pending_input)
        if reply and isinstance(reply, dict) and reply.get("abort"):
            result = await self._build_result(
                status="aborted",
                step=step,
                summary=(
                    reply.get("reason") or "Browser task aborted."
                ),
                history=history,
                page=page,
                verification=None,
                operator_messages=operator_messages,
            )
            self.runtime.record_event(
                "subagent_task_aborted", result
            )
            return {"result": result}

        operator_msg = {
            "role": "main_agent",
            "content": (
                reply
                if isinstance(reply, str)
                else (reply or {}).get("instruction", "")
            ),
            "timestamp": datetime.now().isoformat(),
        }
        operator_messages.append(operator_msg)
        self.runtime.record_event(
            "subagent_input_received", operator_msg
        )

        if on_progress:
            await on_progress(
                self._build_progress_snapshot(
                    status="running",
                    step=step,
                    summary=(
                        "Manual control complete. Resuming "
                        "browser task."
                        if mode == "manual_control"
                        else "Guidance received. Continuing "
                        "browser task."
                    ),
                    history=history,
                    page=page,
                    verification=None,
                    operator_messages=operator_messages,
                    pending_input=None,
                )
            )

        return {"result": None}

    # ── Action execution ──

    async def _execute_action(
        self, action: dict[str, Any]
    ) -> dict[str, Any]:
        action_type = action.get("type")
        if action_type == "goto":
            return await self.runtime.goto(action["url"])
        if action_type == "click":
            return await self.runtime.click(int(action["id"]))
        if action_type == "type":
            return await self.runtime.type_text(
                int(action["id"]), action.get("text", "")
            )
        if action_type == "hover":
            return await self.runtime.hover(int(action["id"]))
        if action_type == "keypress":
            return await self.runtime.keypress(action["key"])
        if action_type == "scroll":
            return await self.runtime.scroll(
                action.get("direction", "down")
            )
        if action_type == "switch_tab":
            return await self.runtime.tab_action(
                "select", index=int(action["index"])
            )
        if action_type == "new_tab":
            return await self.runtime.tab_action(
                "new", url=action.get("url")
            )
        if action_type == "close_tab":
            return await self.runtime.tab_action(
                "close", index=int(action["index"])
            )
        raise ValueError(
            f"Unsupported subagent action: {action_type}"
        )

    # ── Result building ──

    def _build_progress_snapshot(
        self,
        *,
        status: str,
        step: int,
        summary: str,
        history: list[dict[str, Any]],
        page: dict[str, Any] | None,
        verification: dict[str, Any] | None,
        operator_messages: list[dict[str, Any]],
        pending_input: dict[str, Any] | None,
    ) -> dict[str, Any]:
        debug_state = self.runtime.get_debug_state(20)
        return {
            "status": status or "running",
            "step": step,
            "summary": summary,
            "history": list(history),
            "artifacts": debug_state.get("artifacts"),
            "page": page,
            "verification": verification,
            "operatorMessages": compact_operator_messages(
                operator_messages, 12
            ),
            "pendingInput": pending_input,
            "debug": compact_debug_state(debug_state, 12),
        }

    async def _build_result(
        self,
        *,
        status: str,
        step: int,
        summary: str,
        history: list[dict[str, Any]],
        page: dict[str, Any] | None,
        verification: dict[str, Any] | None,
        operator_messages: list[dict[str, Any]],
        pending_input: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        debug_state = self.runtime.get_debug_state(10)
        artifacts = debug_state.get("artifacts")
        result: dict[str, Any] = {
            "status": status,
            "step": step,
            "summary": summary,
            "history": history,
            "artifacts": artifacts,
            "page": page,
            "verification": verification,
            "operatorMessages": compact_operator_messages(
                operator_messages, 12
            ),
            "pendingInput": pending_input,
            "debug": compact_debug_state(
                self.runtime.get_debug_state(20), 12
            ),
        }
        result["capabilities"] = (
            (result.get("debug") or {}).get("capabilities")
        )

        report_paths = self._write_reports(result)
        return {**result, "reports": report_paths}

    def _write_reports(
        self, result: dict[str, Any]
    ) -> dict[str, str]:
        report_path = str(
            self.runtime.write_artifact_json(
                "task-report.json", result
            )
        )
        walkthrough_path = str(
            self.runtime.write_artifact_text(
                "walkthrough.md",
                self._render_walkthrough(result),
            )
        )
        timeline_path = str(
            self.runtime.write_artifact_json(
                "task-timeline.json",
                build_structured_timeline(result),
            )
        )
        return {
            "reportJsonPath": report_path,
            "walkthroughPath": walkthrough_path,
            "timelineJsonPath": timeline_path,
        }

    def _render_walkthrough(
        self, result: dict[str, Any]
    ) -> str:
        artifacts = result.get("artifacts") or {}
        session_dir = artifacts.get("sessionDir")
        video_files = artifacts.get("videoFiles") or []
        primary_video = video_files[0] if video_files else None
        primary_rel = _to_artifact_relative_path(
            primary_video, session_dir
        )

        lines = [
            "# Browser Task Report",
            "",
            f"Status: {result.get('status', 'unknown')}",
            f"Summary: {result.get('summary', 'unknown')}",
            f"Final URL: {(result.get('page') or {}).get('url', 'unknown')}",
            f"Final Title: {(result.get('page') or {}).get('title', 'unknown')}",
            "",
            "## Steps",
        ]

        history = result.get("history") or []
        if not history:
            lines.extend(["", "No browser actions were executed."])

        for entry in history:
            lines.extend([
                "",
                f"### Step {entry.get('step', '?')}",
                f"Action: {_format_action(entry.get('action'))}",
            ])
            if entry.get("summary"):
                lines.append(
                    f"Plan Summary: {entry['summary']}"
                )
            verification = entry.get("verification") or {}
            if verification.get("summary"):
                lines.append(
                    f"Verification: {verification['summary']}"
                )
            if entry.get("stepStartedAt") or entry.get("stepFinishedAt"):
                lines.append(
                    f"Timing: {entry.get('stepStartedAt', 'unknown')} "
                    f"-> {entry.get('stepFinishedAt', 'unknown')} "
                    f"({_format_duration_ms(entry.get('actionDurationMs'))})"
                )
            page = entry.get("page") or {}
            if page.get("url"):
                lines.append(f"Page URL: {page['url']}")
            if page.get("title"):
                lines.append(f"Page Title: {page['title']}")

            ar = entry.get("actionResult") or {}
            screenshot_rel = _to_artifact_relative_path(
                ar.get("screenshotPath"), session_dir
            )
            if screenshot_rel:
                lines.append(f"Screenshot: {screenshot_rel}")

            video_offset = entry.get("videoOffsetSeconds")
            if primary_rel and isinstance(video_offset, (int, float)):
                anchor = max(0, int(video_offset))
                lines.append(
                    f"Video Timestamp: "
                    f"{_format_video_timestamp(video_offset)} "
                    f"(+{anchor}s)"
                )
                lines.append(
                    f"Video Jump: {primary_rel}#t={anchor}"
                )

        lines.extend([
            "",
            "## Artifacts",
            f"Session Dir: {session_dir or 'unknown'}",
            f"Trace: {artifacts.get('tracePath', 'pending')}",
            f"Events: {artifacts.get('eventsPath', 'unknown')}",
            f"Primary Video: {primary_rel or 'pending'}",
        ])

        return "\n".join(lines) + "\n"

    async def _safe_page_metadata(
        self,
    ) -> dict[str, Any] | None:
        try:
            return await self.runtime.get_page_metadata()
        except Exception:
            return None


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


def _to_iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.now().isoformat()
