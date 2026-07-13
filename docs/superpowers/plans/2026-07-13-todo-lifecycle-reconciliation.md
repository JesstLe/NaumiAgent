# Todo Lifecycle Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Require Agent-managed Todo state to reconcile before a run can finish.

**Architecture:** A focused reconciliation service reads and atomically blocks stale tasks. Both Engine loops share it; the first stale final response triggers one model correction turn, and the second stale response blocks the task with an audit reason.

**Tech Stack:** Python 3.14, asyncio, aiosqlite, pytest.

## Global Constraints

- Agent explicitly changes Todo state through existing tools.
- Backend never infers `completed` from unrelated tool success.
- At most one reconciliation model turn is allowed.
- User-visible text is Chinese; code comments and commit messages are English.

---

### Task 1: Atomic stale-task fallback

**Files:**
- Modify: `src/naumi_agent/tasks/store.py`
- Test: `tests/unit/test_tasks.py`

**Interfaces:**
- Produces: `TaskStore.block_unreconciled_tasks(reason: str) -> list[Task]`.

- [ ] **Step 1: Write failing SQLite tests** proving only `in_progress` tasks become `blocked`, `active_form` contains the reason, and completed/pending rows remain unchanged.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/unit/test_tasks.py -k unreconciled -q` and confirm failure because the method is absent.
- [ ] **Step 3: Implement** one transaction using `UPDATE tasks SET status = 'blocked', active_form = ?, updated_at = ? WHERE session_id = ? AND status = 'in_progress'`, then return the updated rows.
- [ ] **Step 4: Run** the targeted tests and `.venv/bin/ruff check src/naumi_agent/tasks/store.py tests/unit/test_tasks.py`.

### Task 2: Shared reconciliation decision

**Files:**
- Create: `src/naumi_agent/tasks/reconciliation.py`
- Test: `tests/unit/test_todo_reconciliation.py`

**Interfaces:**
- Produces: `TodoReconciliationAction(StrEnum)` with `NONE`, `RETRY`, `BLOCKED`.
- Produces: `reconcile_todos(store, *, attempted: bool) -> TodoReconciliationResult`.

- [ ] **Step 1: Write failing tests** for no active Todo, first stale attempt returning `RETRY`, second attempt persisting `BLOCKED`, and store exceptions returning a warning result.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/unit/test_todo_reconciliation.py -q` and confirm import failure.
- [ ] **Step 3: Implement** the dataclass result with action, system instruction, warning, and changed tasks; use the exact fallback reason `Agent 结束前未完成状态对账`.
- [ ] **Step 4: Run** reconciliation and TaskStore tests.

### Task 3: Engine terminal gate

**Files:**
- Modify: `src/naumi_agent/orchestrator/engine.py`
- Modify: `src/naumi_agent/orchestrator/system_prompt.py`
- Test: `tests/unit/test_engine.py`

**Interfaces:**
- Consumes: `reconcile_todos()`.
- Produces: identical terminal behavior in `_react_loop` and `_react_loop_streaming`.

- [ ] **Step 1: Write failing non-streaming test** with a real temporary TaskStore and two model responses: first final text with stale Todo, second `task_update` followed by final text.
- [ ] **Step 2: Write failing streaming test** proving stale first-pass text is not emitted and the corrected final text is emitted once.
- [ ] **Step 3: Add one local `todo_reconciliation_attempted` flag** to each loop. Before returning a final response, call reconciliation; on `RETRY`, append the system instruction and continue; on `BLOCKED`, emit a task snapshot then finish.
- [ ] **Step 4: Update system prompt** to require explicit terminal status before a final answer.
- [ ] **Step 5: Run** `.venv/bin/python -m pytest tests/unit/test_engine.py -k 'todo and reconcil' -q` and related task tests.
- [ ] **Step 6: Commit** with `git commit -m "feat: reconcile agent todo lifecycle"`.

