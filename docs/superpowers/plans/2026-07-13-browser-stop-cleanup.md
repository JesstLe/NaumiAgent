# Browser Stop Cleanup Implementation Plan

**Goal:** Make browser cleanup and terminal run lifecycle bounded, idempotent, and truthful.

**Architecture:** BrowserRuntime owns ordered best-effort cleanup and warnings. Engine
shutdown isolates resource failures. Bridge completion events are authoritative, and the
terminal UI never infers run completion from a generic error.

## Task 1: Bounded browser cleanup

**Files:**
- Modify: `src/naumi_agent/tools/browser/runtime/browser_runtime.py`
- Test: `tests/unit/test_browser_runtime.py`

- [ ] Add failing tests for cleanup order, hung CDP, closed context, warnings, and idempotency.
- [ ] Add a bounded cleanup-step helper and structured warning payloads.
- [ ] Reorder stop operations and guarantee teardown/reset in `finally`.
- [ ] Run browser runtime tests and ruff.
- [ ] Commit the completed browser cleanup behavior.

## Task 2: Shutdown and run lifecycle

**Files:**
- Modify: `src/naumi_agent/orchestrator/engine.py`
- Modify: `src/naumi_agent/ui/bridge.py`
- Test: `tests/unit/test_engine.py`
- Test: `tests/unit/test_ui_bridge.py`

- [ ] Add failing tests for browser cleanup failure during shutdown.
- [ ] Add failing tests for one terminal completion event after run exceptions.
- [ ] Implement isolated engine cleanup and bridge completion invariants.
- [ ] Run engine and bridge tests and ruff.

## Task 3: Terminal UI truthfulness and end-to-end validation

**Files:**
- Modify: `frontend/terminal-ui/src/state.js`
- Test: `frontend/terminal-ui/test/state.test.js`

- [ ] Add a failing test proving `run_in_progress` does not clear `running`.
- [ ] Make run lifecycle events authoritative.
- [ ] Run all Node tests.
- [ ] Execute a real Windows TUI browser task, submit a second message, exit, and inspect
      remaining processes.
- [ ] Commit the completed lifecycle behavior.
