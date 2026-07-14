# Queued Chat Delivery Implementation Plan

> **For Codex:** Execute one task at a time with targeted tests and commit the completed feature independently.

**Goal:** Accept chat messages while a run is active and execute them serially with visible queue state.

**Architecture:** `JsonlEngineBridge` owns a bounded deque of immutable chat submissions. A single scheduling method starts one engine run and its finalizer advances the deque. The JSONL protocol exposes queue acknowledgement and runtime count; the terminal state maps that acknowledgement to a server-confirmed scheduled message.

**Tech Stack:** Python 3.13, asyncio, pytest, Node.js built-in test runner, terminal-ui state/components.

---

## Task 1: Define failing Bridge queue tests

**Files:**
- Modify: `tests/unit/test_ui_bridge.py`

Add focused tests for FIFO execution, queue-full rejection, failed-run advancement, cancellation advancement, shutdown cancellation, and `queued_conversations` status. Run only those new tests and confirm they fail for missing queue behavior.

## Task 2: Implement the Bridge queue and protocol

**Files:**
- Modify: `src/naumi_agent/ui/protocol.py`
- Modify: `src/naumi_agent/ui/bridge.py`

Add `run/queued`, an immutable queued-submission record, a 20-entry deque, a single-run scheduler, finalizer advancement, cancellation advancement, shutdown draining, and status count. Run the focused Bridge tests until green, then run related existing submit/cancel tests for regressions.

## Task 3: Define failing terminal state tests

**Files:**
- Modify: `frontend/terminal-ui/test/state.test.js`

Test that `run/queued` converts a local message to `scheduled`, records the queue position, excludes it from transport-failure/outbox handling, and that `run/started` converts it to `accepted`.

## Task 4: Implement scheduled-message rendering

**Files:**
- Modify: `frontend/terminal-ui/src/state.js`
- Modify: `frontend/terminal-ui/src/components/message.js`
- Modify: `frontend/terminal-ui/src/components/footer.js` if queue count formatting needs an explicit label

Handle the event, persist only retryable transport states, and render the Chinese queue-position label. Run the focused state tests and relevant footer/message component tests.

## Task 5: Replace the process-level retry scenario

**Files:**
- Modify: `frontend/terminal-ui/test/fixtures/message-lifecycle-bridge.js`
- Modify: `frontend/terminal-ui/test/index-process.test.js`

Make the fixture hold the first run, acknowledge the second as queued, then automatically start it. Assert two submissions, one bubble per message, visible queue feedback, and no retry command.

## Task 6: Verify, review, document, and commit

Run only:

```bash
.venv/bin/pytest tests/unit/test_ui_bridge.py -q -k 'queued or second_submit or cancels_active_run or shutdown'
.venv/bin/ruff check src/naumi_agent/ui/bridge.py src/naumi_agent/ui/protocol.py tests/unit/test_ui_bridge.py
npm --prefix frontend/terminal-ui test -- --test-name-pattern='queue|delivery lifecycle'
git diff --check
```

Perform a self-review against the design contract, correct any missed edge case with a failing test first, update relevant protocol documentation if present, then commit the complete feature with an English message.
