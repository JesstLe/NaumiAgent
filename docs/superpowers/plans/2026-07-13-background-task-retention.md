# Background Task Retention Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Release completed background activity from live UI while retaining a bounded, safe audit history.

**Architecture:** BackgroundTaskStore owns pruning and artifact safety. BackgroundRunner triggers pruning at lifecycle boundaries. Bridge status counts only active or unacknowledged tasks, and task panel exposes explicit history filtering.

**Tech Stack:** Python 3.14, asyncio subprocesses, JSON persistence, pytest, Node test runner.

## Global Constraints

- Running task records are never pruned.
- Terminal history is retained for 7 days and at most 100 records.
- Artifact deletion is restricted to the managed artifacts directory.
- Consumed failures disappear from footer attention immediately.

---

### Task 1: Safe bounded pruning

**Files:**
- Modify: `src/naumi_agent/background/store.py`
- Test: `tests/unit/test_background.py`

**Interfaces:**
- Produces: `BackgroundPruneResult(records_deleted: int, artifacts_deleted: int, errors: tuple[str, ...])`.
- Produces: `BackgroundTaskStore.prune(*, now: datetime | None = None, retention_days: int = 7, max_records: int = 100) -> BackgroundPruneResult`.

- [ ] **Step 1: Write failing tests** for age expiry, count expiry, running preservation, artifact deletion, and external-path refusal.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/unit/test_background.py -k prune -q` and confirm missing method failure.
- [ ] **Step 3: Implement** deterministic terminal ordering by `completed_at or started_at`; atomically rewrite records after safe artifact cleanup.
- [ ] **Step 4: Run** background tests and ruff.

### Task 2: Runner lifecycle and acknowledgement

**Files:**
- Modify: `src/naumi_agent/background/runner.py`
- Modify: `src/naumi_agent/background/tools.py`
- Test: `tests/unit/test_background.py`

**Interfaces:**
- Consumes: `BackgroundTaskStore.prune()`.
- Produces: `BackgroundRunner.list_active_tasks()` and `list_history()`.

- [ ] **Step 1: Write failing tests** proving watcher/process maps empty at terminal state, collect notification marks failure acknowledged, and cleanup reports prune counts.
- [ ] **Step 2: Implement** prune calls after terminal save, notification collection, and cleanup; add active/history filtered list methods.
- [ ] **Step 3: Update tools** so default list returns active/unacknowledged items and `history=True` returns acknowledged history.
- [ ] **Step 4: Run** the complete background test module.

### Task 3: Footer and `/tasks history`

**Files:**
- Modify: `src/naumi_agent/ui/bridge.py`
- Modify: `src/naumi_agent/ui/task_panel.py`
- Modify: `frontend/terminal-ui/src/state.js`
- Test: `tests/unit/test_ui_bridge.py`
- Test: `tests/unit/test_task_panel.py`
- Test: `frontend/terminal-ui/test/state.test.js`

**Interfaces:**
- Produces: task panel payload filter `history: bool`.

- [ ] **Step 1: Write failing bridge test** proving notified failed tasks do not increment `background_attention`.
- [ ] **Step 2: Write failing task-panel tests** proving default excludes acknowledged history and history mode includes it.
- [ ] **Step 3: Implement** the filtering without deleting persisted records.
- [ ] **Step 4: Run** Python bridge/task-panel tests and `node frontend/terminal-ui/scripts/run-tests.js`.
- [ ] **Step 5: Commit** with `git commit -m "feat: bound background task history"`.

