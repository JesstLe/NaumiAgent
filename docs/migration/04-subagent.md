# Phase 4: Browser Subagent (Autonomous Task Execution)

## Source Files
- `scripts/subagent/BrowserSubagent.js` (748 lines)
- `scripts/subagent/OpenAIPlanner.js` (423 lines)

## Objective

Port the autonomous browser subagent that can execute multi-step browser tasks with LLM planning, verification, CAPTCHA handling, and human handoff.

## Files to Create

```
src/naumi_agent/tools/browser/
├── subagent/
│   ├── __init__.py
│   ├── planner.py           # LLMPlanner class
│   └── browser_subagent.py  # BrowserSubagent class
```

## Classes to Port

### 1. `LLMPlanner` (port of `OpenAIPlanner`)

LLM-based planner that decides the next browser action. Uses NaumiAgent's existing `ModelRouter` instead of raw OpenAI API calls.

#### Constructor
- `model_router: ModelRouter` — NaumiAgent's model router (reuse existing)
- `model_tier: str = "capable"` — which tier to use for planning
- `request_timeout_ms: int = 45000`
- `max_retries: int = 2`
- `base_retry_delay_ms: int = 700`

#### Methods

- `decide(input) -> dict` — main decision loop
  - Build system prompt (with optional CAPTCHA mode)
  - Build user prompt with task, page state, elements, history, debug state, tabs
  - Request JSON response via ModelRouter
  - Parse response: `{thinking, status, summary, next_action: {type, url?, id?, text?, key?, direction?, ...}}`

- `verify(input) -> dict` — verify progress after each action
  - Returns `{goal_status, confidence, summary, evidence, next_hint}`

- `build_decision_system_prompt(captcha_mode=False) -> str`
  - Available actions: goto, click, type, hover, keypress, scroll, switch_tab, new_tab, close_tab, finish, fail, ask_main_agent
  - JSON-only output requirement
  - Proactive guidance for login walls, errors, CAPTCHAs
  - Multi-tab workflow instructions

- `build_captcha_system_prompt() -> str`
  - CAPTCHA solving protocol for checkbox/image/text/audio CAPTCHAs

- `build_decision_user_prompt(input) -> str`
  - Structured JSON with task, page, elements, history, operator_messages, debug_state, tabs, captcha_hint, required_output_schema

- `build_verification_system_prompt() -> str`
- `build_verification_user_prompt(input) -> str`

#### Integration with NaumiAgent's ModelRouter

Instead of raw OpenAI API calls, use `model_router.chat(messages, tier="capable")` with `response_format={"type": "json_object"}` parameter. Handle model discovery/fallback through NaumiAgent's existing model infrastructure.

### 2. `BrowserSubagent` (port of `BrowserSubagent`)

Autonomous task executor with step-by-step planning loop.

#### Constructor
- `runtime: BrowserRuntime`
- `planner: LLMPlanner`
- `default_max_steps: int = 12`

#### `delegate_task(task_instruction, options) -> dict`

Main loop (1..max_steps):
1. Check abort flag
2. Observe current page state
3. Check for external handoff request (manual control)
4. Detect guidance signals (page errors, login walls, permission prompts) via `detect_guidance_request()`
5. Handle CAPTCHA: detect → capture screenshot → pass to planner with image → track consecutive attempts → escalate to human after 5
6. Call planner to decide next action
7. Execute action via `execute_action()`
8. Verify progress
9. Record history entry with timing, artifacts, page state
10. Report progress via callback
11. Check verification goal_status (completed/blocked)

Returns: `{status, step, summary, history, artifacts, page, verification, operator_messages, reports, capabilities}`

#### Helper Functions to Port

- `compact_elements(elements, limit=50)` — trim element data for planner context
- `compact_debug_state(debug_state, limit=8)` — trim debug data
- `compact_operator_messages(messages, limit=8)` — trim messages
- `detect_guidance_request(task_instruction, page, elements, debug_state, raised_signals)` — detect login walls, permission prompts, page errors
- `format_action(action) -> str` — human-readable action description
- `build_structured_timeline(result)` — JSON timeline with video anchors
- `render_walkthrough(result) -> str` — Markdown walkthrough report

#### `handle_pending_input(step, pending_input, ...)`

Handoff flow:
- If `on_needs_input` is None → fail the task
- Otherwise await `on_needs_input(pending_input)` which returns `{abort, instruction}`
- Support modes: "guidance" (waiting_for_instruction), "manual_control"

#### `execute_action(action) -> dict`

Map action types to runtime methods:
- goto → runtime.goto
- click → runtime.click
- type → runtime.type
- hover → runtime.hover
- keypress → runtime.keypress
- scroll → runtime.scroll
- switch_tab → runtime.tab_action("select")
- new_tab → runtime.tab_action("new")
- close_tab → runtime.tab_action("close")

#### Report Writing

- `write_reports(result)` — task-report.json, walkthrough.md, task-timeline.json
- `render_walkdown(result) -> str` — full Markdown report with step details, timing, artifact links, video timestamps

## Testing

- `tests/unit/test_planner.py` — prompt building, JSON parsing, retry logic
- `tests/unit/test_browser_subagent.py` — delegate loop with mocked runtime
- Test CAPTCHA handling flow
- Test guidance detection (login wall, errors, permissions)
- Test handoff flow (waiting_for_instruction, manual_control)

## Checklist

- [ ] LLMPlanner using NaumiAgent's ModelRouter
- [ ] BrowserSubagent with full delegate loop
- [ ] CAPTCHA handling (5-attempt escalation)
- [ ] Guidance detection (login walls, errors, permissions)
- [ ] Handoff flow (guidance + manual control)
- [ ] Walkthrough report generation
- [ ] Structured timeline with video anchors
- [ ] All tests passing
- [ ] `ruff check` clean
