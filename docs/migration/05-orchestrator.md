# Phase 5: Task Orchestrator (Queued Runs + Templates)

## Source Files
- `scripts/orchestrator/TaskRunner.js` (825 lines)
- `scripts/orchestrator/TaskRunStore.js` (93 lines)
- `scripts/orchestrator/RunTemplateStore.js` (30 lines)

## Objective

Port the queued task run system with state machine, persistence, recovery, and template evaluation.

## Files to Create

```
src/naumi_agent/tools/browser/
├── orchestrator/
│   ├── __init__.py
│   ├── task_runner.py       # TaskRunner class
│   ├── task_run_store.py    # TaskRunStore class
│   └── run_template_store.py # RunTemplateStore class
```

## Classes to Port

### 1. `TaskRunStore`

JSON file persistence:
- `load() -> list[dict]` — read from `task-runs/index.json`
- `persist(runs)` — write to `task-runs/index.json`
- `delete_run(run_id)` — remove individual run file if needed

### 2. `RunTemplateStore`

Template persistence:
- `load() -> list[dict]` — read from `task-runs/templates.json`
- `persist(templates)` — write to `task-runs/templates.json`

### 3. `TaskRunner`

Queued run state machine with template support.

#### State Machine

```
queued ──► running ──► completed
  │           │    ──► failed
  │           │    ──► aborted
  │           │
  │           ├──► waiting_for_instruction ──► running (via reply)
  │           ├──► manual_control ──► manual_control_requested ──► running (via resume)
  │           └──► aborting ──► aborted
  │
  └──► aborted (direct cancel)
```

Terminal states: `completed`, `failed`, `aborted`

#### Constructor
- `base_dir: str` — storage directory
- `runtime: BrowserRuntime`
- `subagent: BrowserSubagent`
- `max_concurrent: int` — from `BROWSER_MAX_CONCURRENT_RUNS` env (default 1)
- `handoff_timeout_ms: int` — default 5 minutes
- `run_history_limit: int` — from env (default 200)

#### Methods

- `create_run(task_instruction, options) -> dict`
  - Options: max_steps, browser_source, cdp_endpoint, handoff_timeout_ms, template_snapshot
  - Generates UUID, adds to queue, triggers process_queue

- `execute_run(run)` — main execution:
  - Start browser, run subagent.delegate_task with callbacks
  - onProgress: update run state, persist
  - onNeedsInput: set waiting status, wait for reply with timeout
  - shouldAbort: check abort flag
  - pullHandoffRequest: check manual control request
  - After completion: evaluate template assertions, finalize artifacts, persist
  - Parallel runs: create separate BrowserRuntime instances

- `reply_to_run(run_id, instruction) -> dict`
- `resume_run(run_id, instruction) -> dict`
- `request_manual_control(run_id, reason) -> dict`
- `abort_run(run_id, reason) -> dict`

- `list_runs(limit=20) -> list`
- `get_run(run_id) -> dict | None`

#### Template System

- `list_templates(limit=100)`
- `get_template(template_id)`
- `save_template(template_input) -> dict`
- `delete_template(template_id) -> dict`
- `create_run_from_template(template_id, overrides) -> dict`
- `compare_template_runs(template_id, options) -> dict`

Template structure:
```python
{
    "id": "uuid",
    "name": "Login Flow Check",
    "description": "...",
    "task_instruction": "...",
    "browser_source": "auto",
    "cdp_endpoint": None,
    "start_url": "https://example.com/login",
    "pre_login_checks": [
        {"id": "rule-1", "name": "URL Check", "kind": "url_includes", "expected": "/dashboard", "required": True}
    ],
    "assertion_rules": [
        {"id": "rule-2", "name": "Welcome Text", "kind": "text_includes", "expected": "Welcome", "required": True}
    ],
    "timeout_policy": {"max_steps": 12, "handoff_timeout_ms": 300000},
}
```

Rule kinds: `url_includes`, `title_includes`, `text_includes`

#### Recovery on Restart

- `recover_persisted_runs()` — on startup:
  - Mark interrupted runs (running/waiting/manual_control) as failed with interruption summary
  - Resume queued runs
  - Reset run controls

#### History Management

- `trim_run_history()` — keep only `run_history_limit` terminal runs
- Clean up storage for deleted runs

## Testing

- `tests/unit/test_task_runner.py`
- Test state machine transitions
- Test template CRUD and assertion evaluation
- Test recovery on restart
- Test handoff timeout
- Test run history trimming
- Test parallel run slot management

## Checklist

- [ ] TaskRunner with full state machine
- [ ] TaskRunStore and RunTemplateStore persistence
- [ ] Template system with assertion evaluation
- [ ] Recovery on restart
- [ ] History trimming
- [ ] All tests passing
- [ ] `ruff check` clean
