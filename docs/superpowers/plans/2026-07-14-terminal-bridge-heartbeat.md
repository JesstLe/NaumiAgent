# Terminal Bridge Heartbeat Implementation Plan

**Goal:** Detect a live-but-unresponsive Python Bridge without confusing slow model work with transport failure.

**Architecture:** A standalone Node heartbeat controller owns monotonic timing and one outstanding ping. `index.js` starts it after `ready`, forwards correlated `pong` records, and stops it on every exit path. Python enriches the existing pong with bounded runtime facts. UI state only renders degraded/recovered transitions.

## Task 1: Heartbeat controller TDD

Create `frontend/terminal-ui/test/heartbeat.test.js` with fake time and timers. Prove single-flight probes, 15-second stale transition, recovery, idempotent start, and stop cleanup fail before adding `src/heartbeat.js`; then implement until green.

## Task 2: UI state and footer TDD

Add reducer tests for heartbeat health updates and footer tests for stale-only display. Implement bounded state fields and Chinese presentation without persisting heartbeat state across launches.

## Task 3: Integrate process lifecycle

Wire controller creation, `ready`, `pong`, debug logging, redraw, and all exit cleanup paths in `src/index.js`. Extend a process fixture to answer ping and a second fixture mode to drop it; verify ping/pong traffic and stale/recovery rendering with accelerated test-only timing supplied through explicit environment variables.

## Task 4: Enrich Bridge pong

Write a focused Python test asserting request correlation, active-run truth and queue depth. Update `JsonlEngineBridge` without reading sensitive state or blocking the control plane.

## Task 5: Verify and commit

Run only heartbeat/protocol/footer/process test selections, Bridge ping tests, Ruff for touched Python, and `git diff --check`. Review timer leaks, stale pongs and active-run false positives, update integration docs, then commit independently.
