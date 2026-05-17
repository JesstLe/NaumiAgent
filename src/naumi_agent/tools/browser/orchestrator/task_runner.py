"""Queued browser task runner with state machine, templates, and recovery.

Ported from browser-debugging-daemon/scripts/orchestrator/TaskRunner.js (825 lines).
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from naumi_agent.tools.browser.orchestrator.run_template_store import (
    RunTemplateStore,
)
from naumi_agent.tools.browser.orchestrator.task_run_store import TaskRunStore
from naumi_agent.tools.browser.runtime.browser_runtime import BrowserRuntime
from naumi_agent.tools.browser.subagent.browser_subagent import BrowserSubagent
from naumi_agent.tools.browser.subagent.planner import LLMPlanner

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset({"completed", "failed", "aborted"})
_INTERRUPTED_STATUSES = frozenset({
    "running",
    "aborting",
    "waiting_for_instruction",
    "manual_control_requested",
    "manual_control",
})
_RULE_KINDS = frozenset({"url_includes", "title_includes", "text_includes"})


def _safe_create_task(coro: Any) -> None:
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(coro)
    except RuntimeError:
        pass


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _format_timeout_duration(ms: int) -> str:
    if ms < 1000:
        return f"{ms}ms"
    return f"{max(1, -(-ms // 1000))} seconds"


def _normalize_positive_int(
    value: Any, fallback: int | None = None, minimum: int = 1
) -> int | None:
    try:
        parsed = int(value)
        if parsed >= minimum:
            return parsed
    except (TypeError, ValueError):
        pass
    return fallback


def _normalize_template_rule(
    inp: dict[str, Any] | None = None,
    index: int = 0,
    default_prefix: str = "Rule",
) -> dict[str, Any] | None:
    inp = inp or {}
    kind = str(inp.get("kind", "")).strip().lower()
    expected = (
        str(inp.get("expected", "")).strip()
        if inp.get("expected") is not None
        else str(inp.get("value", "")).strip()
        if inp.get("value") is not None
        else ""
    )
    if kind not in _RULE_KINDS or not expected:
        return None
    return {
        "id": (
            str(inp["id"]).strip()
            if inp.get("id") and str(inp["id"]).strip()
            else f"rule-{index + 1}"
        ),
        "name": (
            str(inp["name"]).strip()
            if inp.get("name") and str(inp["name"]).strip()
            else f"{default_prefix} {index + 1}"
        ),
        "kind": kind,
        "expected": expected,
        "required": inp.get("required", True) is not False,
    }


def _normalize_template_rules(
    inp: Any, default_prefix: str = "Rule"
) -> list[dict[str, Any]]:
    if not isinstance(inp, list):
        return []
    return [
        r
        for i, item in enumerate(inp)
        if (r := _normalize_template_rule(item, i, default_prefix)) is not None
    ]


def _normalize_template_input(
    template_input: dict[str, Any],
    current: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = _now_iso()
    existing = current or None
    tp_input = template_input.get("timeoutPolicy") or {}
    existing_tp = (existing or {}).get("timeoutPolicy") or {}
    timeout_policy = {
        "maxSteps": _normalize_positive_int(
            tp_input.get("maxSteps"),
            _normalize_positive_int(existing_tp.get("maxSteps"), 12),
            1,
        ),
        "handoffTimeoutMs": _normalize_positive_int(
            tp_input.get("handoffTimeoutMs"),
            _normalize_positive_int(
                existing_tp.get("handoffTimeoutMs"), 5 * 60 * 1000
            ),
            1000,
        ),
    }

    normalized: dict[str, Any] = {
        "id": (existing or {}).get("id") or str(uuid.uuid4()),
        "name": (
            str(template_input["name"]).strip()
            if isinstance(template_input.get("name"), str)
            and template_input["name"].strip()
            else (existing or {}).get("name", "")
        ),
        "description": (
            str(template_input["description"]).strip()
            if isinstance(template_input.get("description"), str)
            else (existing or {}).get("description", "")
        ),
        "taskInstruction": (
            str(template_input["taskInstruction"]).strip()
            if isinstance(template_input.get("taskInstruction"), str)
            else (existing or {}).get("taskInstruction", "")
        ),
        "browserSource": (
            str(template_input["browserSource"]).strip().lower()
            if isinstance(template_input.get("browserSource"), str)
            else (existing or {}).get("browserSource", "auto")
        ),
        "cdpEndpoint": (
            str(template_input["cdpEndpoint"]).strip()
            if isinstance(template_input.get("cdpEndpoint"), str)
            else (existing or {}).get("cdpEndpoint")
        ),
        "startUrl": (
            str(template_input["startUrl"]).strip()
            if isinstance(template_input.get("startUrl"), str)
            else (existing or {}).get("startUrl", "")
        ),
        "preLoginChecks": _normalize_template_rules(
            template_input.get("preLoginChecks")
            if "preLoginChecks" in template_input
            else (existing or {}).get("preLoginChecks"),
            "Login Check",
        ),
        "assertionRules": _normalize_template_rules(
            template_input.get("assertionRules")
            if "assertionRules" in template_input
            else (existing or {}).get("assertionRules"),
            "Assertion",
        ),
        "timeoutPolicy": timeout_policy,
        "createdAt": (existing or {}).get("createdAt") or now,
        "updatedAt": now,
    }

    if not normalized["name"]:
        raise ValueError("Template name is required.")
    if not normalized["taskInstruction"] and not normalized["startUrl"]:
        raise ValueError(
            "Template requires taskInstruction or startUrl."
        )

    return normalized


def build_templated_instruction(
    task_instruction: str, template: dict[str, Any] | None
) -> str:
    if not template:
        return task_instruction

    parts: list[str] = []
    base = task_instruction or template.get("taskInstruction") or ""
    if base:
        parts.append(base)
    if template.get("startUrl"):
        parts.append(
            f"Always start by navigating to this URL: "
            f"{template['startUrl']}"
        )
    pre_checks = template.get("preLoginChecks") or []
    if pre_checks:
        checks = "\n".join(
            f"{i + 1}. [{r['kind']}] {r['expected']} ({r['name']})"
            for i, r in enumerate(pre_checks)
        )
        parts.append(
            "Before executing the main task, verify these login "
            "checks and ask_main_agent immediately if any "
            f"required check fails:\n{checks}"
        )
    assertions = template.get("assertionRules") or []
    if assertions:
        lines = "\n".join(
            f"{i + 1}. [{r['kind']}] {r['expected']} ({r['name']})"
            for i, r in enumerate(assertions)
        )
        parts.append(
            "Treat the run as complete only when all assertions "
            f"pass:\n{lines}"
        )

    return "\n\n".join(p for p in parts if p)


def evaluate_rule(
    rule: dict[str, Any], page: dict[str, Any] | None
) -> dict[str, Any]:
    page = page or {}
    sources: dict[str, str] = {
        "url_includes": page.get("url") or "",
        "title_includes": page.get("title") or "",
        "text_includes": page.get("textPreview") or "",
    }
    source = str(sources.get(rule["kind"], ""))
    expected = str(rule.get("expected", ""))
    passed = expected.lower() in source.lower()
    return {
        "id": rule["id"],
        "name": rule["name"],
        "kind": rule["kind"],
        "expected": rule["expected"],
        "required": rule.get("required", True) is not False,
        "actual": source[:500],
        "passed": passed,
    }


def evaluate_template(
    template: dict[str, Any] | None,
    result: dict[str, Any],
) -> dict[str, Any] | None:
    if not template:
        return None

    page = result.get("page") if result else None
    login_checks = [
        evaluate_rule(r, page)
        for r in (template.get("preLoginChecks") or [])
    ]
    assertions = [
        evaluate_rule(r, page)
        for r in (template.get("assertionRules") or [])
    ]
    all_results = login_checks + assertions
    failures = [
        item for item in all_results if item["required"] and not item["passed"]
    ]

    return {
        "templateId": template["id"],
        "templateName": template.get("name", ""),
        "evaluatedAt": _now_iso(),
        "page": (
            {
                "url": (page or {}).get("url", ""),
                "title": (page or {}).get("title", ""),
            }
            if page
            else None
        ),
        "loginChecks": login_checks,
        "assertions": assertions,
        "passed": len(failures) == 0,
        "failureMessages": [
            f'{item["name"]} ({item["kind"]}) expected '
            f'"{item["expected"]}"'
            for item in failures
        ],
    }


class TaskRunner:
    def __init__(
        self,
        base_dir: str,
        options: dict[str, Any] | None = None,
    ) -> None:
        options = options or {}
        self._base_dir = base_dir

        runtime = options.get("runtime")
        if runtime is None:
            runtime = BrowserRuntime(base_dir)
        self.runtime = runtime

        planner = options.get("planner")
        if planner is None:
            from naumi_agent.model.router import ModelRouter

            router = options.get("model_router") or ModelRouter()
            planner = LLMPlanner(router)
        self.subagent = BrowserSubagent(self.runtime, planner)

        self._store = TaskRunStore(base_dir)
        self._template_store = RunTemplateStore(base_dir)

        self.runs: list[dict[str, Any]] = self._store.load()
        self.templates: list[dict[str, Any]] = [
            t
            for t in self._template_store.load()
            if isinstance(t, dict) and isinstance(t.get("id"), str)
        ]

        self._processing = False
        self._active_slots = 0
        self._max_concurrent = int(
            os.environ.get("BROWSER_MAX_CONCURRENT_RUNS", "1") or "1"
        )
        self._listeners: list[Callable[..., Any]] = []
        self._pending_replies: dict[str, asyncio.Future[Any]] = {}
        self._reply_timeouts: dict[str, asyncio.TimerHandle | None] = {}
        self._run_controls: dict[str, dict[str, Any]] = {}
        self._handoff_timeout_ms = options.get(
            "handoff_timeout_ms", 5 * 60 * 1000
        )

        env_limit = os.environ.get("BROWSER_RUN_HISTORY_LIMIT")
        self._run_history_limit = (
            int(env_limit) if env_limit and int(env_limit) > 0 else 200
        )

        self._recover_persisted_runs()

    # ── History management ──

    def _trim_run_history(self) -> None:
        if (
            not self._run_history_limit
            or self._run_history_limit <= 0
            or len(self.runs) <= self._run_history_limit
        ):
            return

        active_count = sum(
            1 for r in self.runs if r["status"] not in _TERMINAL_STATUSES
        )
        allowed_terminal = max(0, self._run_history_limit - active_count)
        kept_terminal = 0
        trimmed: list[dict[str, Any]] = []

        for run in self.runs:
            if run["status"] not in _TERMINAL_STATUSES:
                trimmed.append(run)
                continue
            if kept_terminal < allowed_terminal:
                trimmed.append(run)
                kept_terminal += 1
            else:
                self._store.delete_run(run["id"])

        self.runs = trimmed

    def _persist_runs(self) -> None:
        self._trim_run_history()
        self._store.persist(self.runs)

    # ── Recovery ──

    def _recover_persisted_runs(self) -> None:
        changed = False
        self.runs = [
            r
            for r in self.runs
            if isinstance(r, dict) and r.get("id")
        ]

        for run in self.runs:
            if run["status"] in _INTERRUPTED_STATUSES:
                msg = (
                    "Run interrupted because the daemon "
                    "restarted before completion."
                )
                run["status"] = "failed"
                run["summary"] = msg
                run["pendingInput"] = None
                run["finishedAt"] = run.get("finishedAt") or _now_iso()
                run["error"] = run.get("error") or {
                    "message": msg,
                    "stack": None,
                }
                if run.get("result"):
                    run["result"]["status"] = "failed"
                    run["result"]["summary"] = msg
                    run["result"]["pendingInput"] = None
                changed = True

            if run["status"] == "queued":
                self._run_controls[run["id"]] = {
                    "aborted": False,
                    "reason": None,
                    "manualRequest": None,
                }

        if changed:
            self._trim_run_history()
            self._store.persist(self.runs)

        if any(r["status"] == "queued" for r in self.runs):
            _safe_create_task(self.process_queue())

    # ── Run CRUD ──

    def create_run(
        self,
        task_instruction: str,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        options = options or {}
        template_snapshot = options.get("templateSnapshot")

        resolved = str(task_instruction).strip() if task_instruction else ""
        fallback = (
            template_snapshot.get("taskInstruction", "")
            if template_snapshot
            else ""
        )
        final_instruction = resolved or fallback
        if not final_instruction and not (
            template_snapshot and template_snapshot.get("startUrl")
        ):
            raise ValueError(
                "taskInstruction is required when template has no startUrl."
            )

        max_steps = _normalize_positive_int(
            options.get("maxSteps"),
            _normalize_positive_int(
                (template_snapshot or {}).get(
                    "timeoutPolicy", {}
                ).get("maxSteps"),
                12,
            ),
            1,
        ) or 12

        handoff_ms = _normalize_positive_int(
            options.get("handoffTimeoutMs"),
            _normalize_positive_int(
                (template_snapshot or {})
                .get("timeoutPolicy", {})
                .get("handoffTimeoutMs"),
                self._handoff_timeout_ms,
            ),
            1000,
        ) or self._handoff_timeout_ms

        run: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "taskInstruction": final_instruction,
            "maxSteps": max_steps,
            "browserSource": options.get("browserSource", "auto"),
            "cdpEndpoint": options.get("cdpEndpoint"),
            "handoffTimeoutMs": handoff_ms,
            "template": (
                {
                    **template_snapshot,
                    "timeoutPolicy": {
                        **(template_snapshot or {}).get(
                            "timeoutPolicy", {}
                        ),
                        "maxSteps": max_steps,
                        "handoffTimeoutMs": handoff_ms,
                    },
                }
                if template_snapshot
                else None
            ),
            "status": "queued",
            "createdAt": _now_iso(),
            "startedAt": None,
            "finishedAt": None,
            "summary": "",
            "error": None,
            "result": None,
            "artifacts": None,
            "reports": None,
            "pendingInput": None,
            "templateEvaluation": None,
        }

        self.runs.insert(0, run)
        self._trim_run_history()
        self._run_controls[run["id"]] = {
            "aborted": False,
            "reason": None,
            "manualRequest": None,
        }
        self._store.persist(self.runs)
        self._emit_update("run_created", run)
        _safe_create_task(self.process_queue())
        return run

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.runs[:limit]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        for r in self.runs:
            if r["id"] == run_id:
                return r
        return None

    def _get_run_control(
        self, run_id: str
    ) -> dict[str, Any]:
        if run_id not in self._run_controls:
            self._run_controls[run_id] = {
                "aborted": False,
                "reason": None,
                "manualRequest": None,
            }
        return self._run_controls[run_id]

    # ── Run lifecycle ──

    async def reply_to_run(
        self, run_id: str, instruction: str
    ) -> dict[str, Any]:
        return await self.resume_run(run_id, instruction)

    async def resume_run(
        self,
        run_id: str,
        instruction: str = (
            "Manual control complete. Continue from the "
            "current page."
        ),
    ) -> dict[str, Any]:
        run = self.get_run(run_id)
        if not run:
            raise ValueError(f"Run not found: {run_id}")

        if (
            run["status"]
            not in {"waiting_for_instruction", "manual_control"}
            or not run.get("pendingInput")
        ):
            raise ValueError(
                f"Run {run_id} is not waiting for a resume "
                f"instruction."
            )

        future = self._pending_replies.get(run_id)
        if not future or future.done():
            raise ValueError(
                f"Run {run_id} does not have a pending reply "
                f"channel."
            )

        was_manual = (
            run["status"] == "manual_control"
            or (run.get("pendingInput") or {}).get("mode")
            == "manual_control"
        )

        if was_manual:
            await self.runtime.exit_manual_control()

        run["summary"] = (
            "Instruction received. Resuming browser task."
        )
        run["status"] = "running"
        run["pendingInput"] = None
        if run.get("result"):
            run["result"]["status"] = "running"
            run["result"]["summary"] = run["summary"]
            run["result"]["pendingInput"] = None

        self._store.persist(self.runs)
        self._emit_update("run_resumed", run)

        timeout_handle = self._reply_timeouts.pop(run_id, None)
        if timeout_handle:
            timeout_handle.cancel()
        self._pending_replies.pop(run_id, None)

        future.set_result({
            "instruction": instruction,
            "respondedAt": _now_iso(),
        })

        return run

    async def request_manual_control(
        self,
        run_id: str,
        reason: str = "Manual control requested by operator.",
    ) -> dict[str, Any]:
        run = self.get_run(run_id)
        if not run:
            raise ValueError(f"Run not found: {run_id}")

        if run["status"] in _TERMINAL_STATUSES:
            raise ValueError(f"Run {run_id} is already finished.")

        if run["status"] == "queued":
            raise ValueError(
                f"Run {run_id} has not started yet, so manual "
                f"control is not available."
            )

        control = self._get_run_control(run_id)
        pending_input = {
            "step": (run.get("result") or {}).get("step", 0),
            "mode": "manual_control",
            "question": (
                "Manual control is active. Use the live session "
                "controls, then resume when you are ready."
            ),
            "details": (
                f"{reason} Use the browser tools against the "
                f"same live browser session."
            ),
            "suggestedReply": (
                "Manual control complete. Continue from the "
                "current page."
            ),
        }

        self.runtime.record_event(
            "task_runner_manual_control_requested",
            {"runId": run_id, "reason": reason},
        )

        if run["status"] == "waiting_for_instruction":
            await self.runtime.enter_manual_control()

            timeout_handle = self._reply_timeouts.get(run_id)
            if timeout_handle:
                timeout_handle.cancel()
                self._reply_timeouts[run_id] = None

            run["status"] = "manual_control"
            run["summary"] = pending_input["question"]
            run["pendingInput"] = pending_input
            if run.get("result"):
                run["result"]["status"] = "manual_control"
                run["result"]["summary"] = pending_input["question"]
                run["result"]["pendingInput"] = pending_input
            self._store.persist(self.runs)
            self._emit_update("run_manual_control", run)
            return run

        if run["status"] == "manual_control":
            return run

        control["manualRequest"] = {
            **pending_input,
            "requestedAt": _now_iso(),
        }
        run["status"] = "manual_control_requested"
        run["summary"] = (
            "Manual control requested. Waiting for the next "
            "safe pause."
        )
        self._store.persist(self.runs)
        self._emit_update("run_manual_control_requested", run)
        return run

    def abort_run(
        self,
        run_id: str,
        reason: str = "Run aborted by operator.",
    ) -> dict[str, Any]:
        run = self.get_run(run_id)
        if not run:
            raise ValueError(f"Run not found: {run_id}")

        if run["status"] in _TERMINAL_STATUSES:
            raise ValueError(f"Run {run_id} is already finished.")

        control = self._get_run_control(run_id)
        control["aborted"] = True
        control["reason"] = reason
        self.runtime.record_event(
            "task_runner_abort_requested",
            {"runId": run_id, "reason": reason},
        )

        if run["status"] == "queued":
            run["status"] = "aborted"
            run["summary"] = reason
            run["finishedAt"] = _now_iso()
            run["pendingInput"] = None
            run["error"] = None
            self._run_controls.pop(run_id, None)
            self._trim_run_history()
            self._store.persist(self.runs)
            self._emit_update("run_aborted", run)
            return run

        run["status"] = "aborting"
        run["summary"] = reason
        self._store.persist(self.runs)
        self._emit_update("run_aborting", run)

        future = self._pending_replies.get(run_id)
        if future and not future.done():
            timeout_handle = self._reply_timeouts.pop(run_id, None)
            if timeout_handle:
                timeout_handle.cancel()
            self._pending_replies.pop(run_id, None)
            future.set_result({"abort": True, "reason": reason})

        return run

    # ── Queue processing ──

    async def process_queue(self) -> None:
        if self._processing:
            return
        self._processing = True

        try:
            while True:
                if self._active_slots >= self._max_concurrent:
                    break
                next_run = None
                for r in self.runs:
                    if r["status"] == "queued":
                        next_run = r
                        break
                if not next_run:
                    break
                self._active_slots += 1
                _safe_create_task(self._execute_run(next_run))
        finally:
            self._processing = False

    async def _execute_run(self, run: dict[str, Any]) -> None:
        is_parallel = (
            self._max_concurrent > 1 and self._active_slots > 1
        )
        run_runtime = self.runtime
        run_subagent = self.subagent

        if is_parallel:
            run_runtime = BrowserRuntime(self._base_dir)
            run_subagent = BrowserSubagent(
                run_runtime,
                self.subagent.planner,
            )

        run["status"] = "running"
        run["startedAt"] = _now_iso()
        run["summary"] = "Starting browser task..."
        run["result"] = {
            "status": "running",
            "step": 0,
            "summary": run["summary"],
            "history": [],
            "artifacts": None,
            "page": None,
            "verification": None,
            "operatorMessages": [],
            "pendingInput": None,
            "debug": None,
            "templateEvaluation": None,
        }
        self._store.persist(self.runs)
        self._emit_update("run_started", run)

        try:
            effective_instruction = build_templated_instruction(
                run["taskInstruction"], run.get("template")
            )
            run_handoff_ms = (
                _normalize_positive_int(
                    run.get("handoffTimeoutMs"),
                    self._handoff_timeout_ms,
                    1000,
                )
                or self._handoff_timeout_ms
            )

            async def _on_progress(progress: dict[str, Any]) -> None:
                run["summary"] = progress.get("summary") or run["summary"]
                if run["status"] not in {"aborting", "aborted"}:
                    if progress.get("status") in {
                        "waiting_for_instruction",
                        "manual_control",
                        "manual_control_requested",
                    }:
                        run["status"] = progress["status"]
                run["result"] = progress
                run["artifacts"] = (
                    progress.get("artifacts") or run["artifacts"]
                )
                run["pendingInput"] = (
                    progress.get("pendingInput") or None
                )
                self._store.persist(self.runs)
                self._emit_update("run_updated", run)

            async def _on_needs_input(
                pending_input: dict[str, Any],
            ) -> dict[str, Any]:
                waiting_status = (
                    "manual_control"
                    if pending_input.get("mode") == "manual_control"
                    else "waiting_for_instruction"
                )
                if pending_input.get("mode") == "manual_control":
                    await run_runtime.enter_manual_control()

                run["status"] = waiting_status
                run["pendingInput"] = {
                    **pending_input,
                    "requestedAt": _now_iso(),
                }
                run["summary"] = (
                    pending_input.get("question")
                    or "Waiting for instruction."
                )
                if run.get("result"):
                    run["result"]["status"] = waiting_status
                    run["result"]["pendingInput"] = run["pendingInput"]
                self._store.persist(self.runs)
                self._emit_update(
                    "run_manual_control"
                    if waiting_status == "manual_control"
                    else "run_waiting",
                    run,
                )

                loop = asyncio.get_running_loop()
                future: asyncio.Future[Any] = loop.create_future()
                self._pending_replies[run["id"]] = future

                if pending_input.get("mode") != "manual_control":
                    timeout_reason = (
                        "Timed out waiting for instruction after "
                        f"{_format_timeout_duration(run_handoff_ms)}."
                    )
                    run_id = run["id"]

                    def _on_timeout() -> None:
                        self._pending_replies.pop(run_id, None)
                        self._reply_timeouts.pop(run_id, None)
                        ctrl = self._get_run_control(run_id)
                        ctrl["aborted"] = True
                        ctrl["reason"] = timeout_reason
                        if not future.done():
                            future.set_result({
                                "abort": True,
                                "reason": timeout_reason,
                            })

                    self._reply_timeouts[run["id"]] = (
                        loop.call_later(
                            run_handoff_ms / 1000, _on_timeout
                        )
                    )

                return await future

            def _should_abort() -> bool:
                return self._get_run_control(run["id"])["aborted"]

            def _get_abort_reason() -> str:
                return (
                    self._get_run_control(run["id"]).get("reason")
                    or "Browser task aborted."
                )

            def _pull_handoff() -> dict[str, Any] | None:
                ctrl = self._get_run_control(run["id"])
                request = ctrl["manualRequest"]
                ctrl["manualRequest"] = None
                return request

            result = await run_subagent.delegate_task(
                effective_instruction,
                options={
                    "maxSteps": run["maxSteps"],
                    "startOptions": {
                        "source": run["browserSource"],
                        "cdpEndpoint": run.get("cdpEndpoint"),
                    },
                    "onProgress": _on_progress,
                    "onNeedsInput": _on_needs_input,
                    "shouldAbort": _should_abort,
                    "getAbortReason": _get_abort_reason,
                    "pullHandoffRequest": _pull_handoff,
                },
            )

            template_eval = evaluate_template(
                run.get("template"), result
            )
            if template_eval:
                result["templateEvaluation"] = template_eval
                run["templateEvaluation"] = template_eval
                if not template_eval["passed"]:
                    result["status"] = "failed"
                    first_failure = (
                        template_eval["failureMessages"][0]
                        if template_eval["failureMessages"]
                        else "Template checks failed."
                    )
                    result["summary"] = (
                        f"Template checks failed: {first_failure}"
                    )

            run["status"] = (
                "completed"
                if result.get("status") == "completed"
                else "aborted"
                if result.get("status") == "aborted"
                else "failed"
            )
            run["summary"] = result.get("summary", "")
            run["result"] = result
            run["artifacts"] = result.get("artifacts")
            run["reports"] = result.get("reports")
            run["pendingInput"] = result.get("pendingInput")
            run["error"] = None
            self._emit_update("run_updated", run)

        except Exception as exc:
            run["status"] = "failed"
            run["summary"] = str(exc)
            run["error"] = {"message": str(exc), "stack": None}
            self._emit_update("run_updated", run)

        finally:
            try:
                stop_result = await run_runtime.stop()
                if stop_result and stop_result.get("artifacts"):
                    run["artifacts"] = stop_result["artifacts"]
                    if run.get("result"):
                        run["result"]["artifacts"] = (
                            stop_result["artifacts"]
                        )
            except Exception:
                pass

            run["finishedAt"] = _now_iso()

            timeout_handle = self._reply_timeouts.pop(run["id"], None)
            if timeout_handle:
                timeout_handle.cancel()
            self._pending_replies.pop(run["id"], None)
            self._run_controls.pop(run["id"], None)
            self._trim_run_history()
            self._store.persist(self.runs)
            self._emit_update("run_finished", run)
            self._active_slots = max(0, self._active_slots - 1)
            await self.process_queue()

    # ── Templates ──

    def list_templates(
        self, limit: int = 100
    ) -> list[dict[str, Any]]:
        return self.templates[: max(1, limit)]

    def get_template(
        self, template_id: str
    ) -> dict[str, Any] | None:
        for t in self.templates:
            if t["id"] == template_id:
                return t
        return None

    def save_template(
        self, template_input: dict[str, Any]
    ) -> dict[str, Any]:
        current = (
            self.get_template(template_input["id"])
            if isinstance(template_input.get("id"), str)
            else None
        )
        template = _normalize_template_input(
            template_input or {}, current
        )
        existing_idx = None
        for i, t in enumerate(self.templates):
            if t["id"] == template["id"]:
                existing_idx = i
                break

        if existing_idx is not None:
            self.templates[existing_idx] = template
        else:
            self.templates.insert(0, template)

        self._template_store.persist(self.templates)
        self._emit_update("template_saved", None)
        return template

    def delete_template(
        self, template_id: str
    ) -> dict[str, Any]:
        existing = self.get_template(template_id)
        if not existing:
            raise ValueError(f"Template not found: {template_id}")
        self.templates = [
            t for t in self.templates if t["id"] != template_id
        ]
        self._template_store.persist(self.templates)
        self._emit_update("template_deleted", None)
        return existing

    def create_run_from_template(
        self,
        template_id: str,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        overrides = overrides or {}
        template = self.get_template(template_id)
        if not template:
            raise ValueError(f"Template not found: {template_id}")

        return self.create_run(
            overrides.get("taskInstruction")
            or template.get("taskInstruction")
            or "",
            options={
                "maxSteps": _normalize_positive_int(
                    overrides.get("maxSteps"),
                    template.get("timeoutPolicy", {}).get("maxSteps"),
                    1,
                ),
                "browserSource": overrides.get(
                    "browserSource"
                ) or template.get("browserSource", "auto"),
                "cdpEndpoint": overrides.get(
                    "cdpEndpoint"
                ) or template.get("cdpEndpoint"),
                "handoffTimeoutMs": _normalize_positive_int(
                    overrides.get("handoffTimeoutMs"),
                    template.get("timeoutPolicy", {}).get(
                        "handoffTimeoutMs"
                    ),
                    1000,
                ),
                "templateSnapshot": template,
            },
        )

    def compare_template_runs(
        self,
        template_id: str,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        options = options or {}
        limit = _normalize_positive_int(options.get("limit"), 8, 1) or 8
        template_runs = [
            r for r in self.runs if (r.get("template") or {}).get("id") == template_id
        ][:limit]

        comparisons: list[dict[str, Any]] = []
        for i in range(len(template_runs) - 1):
            current = template_runs[i]
            previous = template_runs[i + 1]
            current_passed = (
                (current.get("templateEvaluation") or {}).get("passed")
            )
            previous_passed = (
                (previous.get("templateEvaluation") or {}).get("passed")
            )

            def _duration_seconds(run: dict[str, Any]) -> int | None:
                start = run.get("startedAt")
                end = run.get("finishedAt")
                if not start or not end:
                    return None
                try:
                    dt_start = datetime.fromisoformat(start)
                    dt_end = datetime.fromisoformat(end)
                    return max(
                        0,
                        int(
                            (dt_end - dt_start).total_seconds()
                        ),
                    )
                except (ValueError, TypeError):
                    return None

            comparisons.append({
                "currentRunId": current["id"],
                "previousRunId": previous["id"],
                "statusChanged": current["status"] != previous["status"],
                "assertionPassedChanged": current_passed != previous_passed,
                "summaryChanged": current["summary"] != previous["summary"],
                "currentDurationSeconds": _duration_seconds(current),
                "previousDurationSeconds": _duration_seconds(previous),
            })

        return {
            "template": self.get_template(template_id),
            "runs": template_runs,
            "comparisons": comparisons,
        }

    # ── Events ──

    def subscribe(
        self, listener: Callable[..., Any]
    ) -> Callable[[], None]:
        self._listeners.append(listener)

        def unsubscribe() -> None:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

        return unsubscribe

    def _emit_update(
        self, event_type: str, run: dict[str, Any] | None
    ) -> None:
        event = {
            "type": event_type,
            "runId": (run or {}).get("id"),
            "timestamp": _now_iso(),
        }
        for listener in self._listeners:
            try:
                listener(event)
            except Exception:
                logger.debug(
                    "Event listener error", exc_info=True
                )
