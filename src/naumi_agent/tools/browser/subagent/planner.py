"""LLM-based planner for the browser subagent.

Ported from browser-debugging-daemon/scripts/subagent/OpenAIPlanner.js (423 lines).
Uses NaumiAgent's ModelPort instead of raw API calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from naumi_agent.model.router import ModelTier
from naumi_agent.runtime.ports.model import ModelPort

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JSON extraction helpers
# ---------------------------------------------------------------------------


def _extract_json_object(text: str) -> dict[str, Any] | None:
    trimmed = text.strip()
    parsed = _try_parse_json(trimmed)
    if parsed is not None:
        return parsed

    start = trimmed.find("{")
    end = trimmed.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return _try_parse_json(trimmed[start : end + 1])


def _try_parse_json(text: str) -> dict[str, Any] | None:
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except (json.JSONDecodeError, ValueError):
        pass
    return None


# ---------------------------------------------------------------------------
# LLMPlanner
# ---------------------------------------------------------------------------


class LLMPlanner:
    def __init__(
        self,
        model_router: ModelPort,
        *,
        tier: str = "capable",
        request_timeout_ms: int = 45000,
        max_retries: int = 2,
        base_retry_delay_ms: int = 700,
    ) -> None:
        self._router = model_router
        self._tier = ModelTier(tier)
        self._request_timeout_ms = request_timeout_ms
        self._max_retries = max_retries
        self._base_retry_delay_ms = base_retry_delay_ms

    # ── System prompts ──

    def build_decision_system_prompt(
        self, *, captcha_mode: bool = False
    ) -> str:
        prompt = " ".join([
            "You are an autonomous browser subagent controller.",
            "Your job is to complete the user's task by choosing",
            "exactly one next browser action at a time.",
            "You must output valid JSON only.",
            "Prefer the simplest action that makes forward progress.",
            "If the task is complete, return status=completed and",
            "next_action.type=finish.",
            "If the task is blocked or impossible, return status=failed",
            "and next_action.type=fail with a clear reason.",
            "If you need guidance from the main agent or a human",
            "operator, return status=needs_input and",
            "next_action.type=ask_main_agent.",
            "Be proactive about asking for guidance when you detect",
            "a login wall, permission dialog, or unexpected",
            "page/runtime error.",
            "Never invent elements that are not present in the",
            "observed element list.",
            "Use goto only when you know the exact URL to open.",
            "MULTI-TAB WORKFLOW: The tabs field lists all open",
            "browser tabs with index, url, title, and active status.",
            "Use switch_tab with an index to switch to a different tab.",
            "Use new_tab with an optional url to open a new tab.",
            "The new tab becomes active immediately.",
            "Use close_tab with an index to close a tab.",
            "For cross-page workflows (e.g. login on tab A, operate",
            "on tab B): use new_tab or switch_tab to move between",
            "pages.",
            "Available action types: goto, click, type, hover,",
            "keypress, scroll, switch_tab, new_tab, close_tab,",
            "finish, fail, ask_main_agent.",
        ])
        if captcha_mode:
            prompt += " " + self.build_captcha_system_prompt()
        return prompt

    def build_captcha_system_prompt(self) -> str:
        return " ".join([
            "CAPTCHA SOLVING PROTOCOL ACTIVE:",
            "A screenshot of the current page is provided as an",
            "image for visual analysis.",
            "Analyze the CAPTCHA challenge visible in the screenshot.",
            "For checkbox CAPTCHAs ('I'm not a robot'): find the",
            "checkbox element in the observed_elements list and",
            "click it.",
            "For image grid CAPTCHAs: read the instruction text at",
            "the top of the CAPTCHA, examine each grid cell image",
            "in the screenshot, click the cells that match.",
            "For text CAPTCHAs: read the distorted text from the",
            "screenshot and type it into the input field.",
            "For audio CAPTCHAs: if an audio option is visible,",
            "prefer clicking it.",
            "Click ONE element at a time, then observe the result",
            "before clicking more.",
            "Do NOT click rapidly — CAPTCHA systems detect",
            "automation speed.",
            "If the CAPTCHA cannot be solved after examining the",
            "screenshot, return status=needs_input for human",
            "assistance.",
        ])

    def build_decision_user_prompt(self, inp: dict[str, Any]) -> str:
        prompt = {
            "task": inp.get("taskInstruction"),
            "current_page": inp.get("page"),
            "observed_elements": inp.get("elements"),
            "history": inp.get("history"),
            "operator_messages": inp.get("operatorMessages", []),
            "debug_state": {
                "recentErrors": (inp.get("debugState") or {}).get(
                    "recentErrors"
                ),
                "recentConsole": (inp.get("debugState") or {}).get(
                    "recentConsole"
                ),
                "recentNetwork": (inp.get("debugState") or {}).get(
                    "recentNetwork"
                ),
            },
            "tabs": inp.get("tabs", []),
            "captcha_hint": inp.get("captchaHint"),
            "required_output_schema": {
                "thinking": "string",
                "status": (
                    "continue|completed|failed|needs_input"
                ),
                "summary": "string",
                "next_action": {
                    "type": (
                        "goto|click|type|hover|keypress|scroll|"
                        "switch_tab|new_tab|close_tab|finish|"
                        "fail|ask_main_agent"
                    ),
                    "url": "string?",
                    "id": "number?",
                    "index": "number?",
                    "text": "string?",
                    "key": "string?",
                    "direction": "down|up|top|bottom?",
                    "result": "string?",
                    "reason": "string?",
                    "question": "string?",
                    "details": "string?",
                    "suggested_reply": "string?",
                },
            },
        }
        return json.dumps(prompt, indent=2, default=str)

    def build_verification_system_prompt(self) -> str:
        return " ".join([
            "You are a browser task verifier.",
            "You evaluate whether the browser task is complete",
            "after the last action.",
            "You must output valid JSON only.",
            "Be conservative: only mark completed when the page",
            "state clearly satisfies the task.",
            "If progress was made but the task is not finished,",
            "return goal_status=incomplete.",
            "If the task appears blocked or impossible from the",
            "current state, return goal_status=blocked.",
        ])

    def build_verification_user_prompt(self, inp: dict[str, Any]) -> str:
        return json.dumps(
            {
                "task": inp.get("taskInstruction"),
                "page_after_action": inp.get("page"),
                "observed_elements_after_action": inp.get("elements"),
                "last_action": inp.get("lastAction"),
                "last_action_result": inp.get("lastActionResult"),
                "history": inp.get("history"),
                "debug_state": {
                    "recentErrors": (inp.get("debugState") or {}).get(
                        "recentErrors"
                    ),
                    "recentConsole": (inp.get("debugState") or {}).get(
                        "recentConsole"
                    ),
                    "recentNetwork": (inp.get("debugState") or {}).get(
                        "recentNetwork"
                    ),
                },
                "captcha_hint": inp.get("captchaHint"),
                "required_output_schema": {
                    "goal_status": (
                        "incomplete|completed|blocked"
                    ),
                    "confidence": "low|medium|high",
                    "summary": "string",
                    "evidence": ["string"],
                    "next_hint": "string",
                },
            },
            indent=2,
            default=str,
        )

    # ── Model request with retries ──

    async def _request_json(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        attempts = max(1, self._max_retries + 1)

        for attempt in range(1, attempts + 1):
            try:
                response = await asyncio.wait_for(
                    self._router.call(
                        [
                            {"role": "system", "content": system_prompt},
                            {
                                "role": "user",
                                "content": user_prompt,
                            },
                        ],
                        tier=self._tier,
                        temperature=0,
                        response_format="json",
                    ),
                    timeout=self._request_timeout_ms / 1000,
                )
                parsed = _extract_json_object(response.content)
                if parsed is None:
                    raise ValueError(
                        f"Planner returned non-JSON: "
                        f"{response.content[:200]}"
                    )
                return parsed
            except Exception as exc:
                last_error = exc
                if attempt >= attempts:
                    raise
                delay = self._base_retry_delay_ms * (
                    2 ** (attempt - 1)
                )
                await asyncio.sleep(delay / 1000)

        raise last_error or RuntimeError("Planner request failed.")

    # ── Public API ──

    async def decide(self, inp: dict[str, Any]) -> dict[str, Any]:
        captcha_mode = bool(inp.get("captchaHint"))
        return await self._request_json(
            self.build_decision_system_prompt(
                captcha_mode=captcha_mode
            ),
            self.build_decision_user_prompt(inp),
        )

    async def verify(self, inp: dict[str, Any]) -> dict[str, Any]:
        return await self._request_json(
            self.build_verification_system_prompt(),
            self.build_verification_user_prompt(inp),
        )
