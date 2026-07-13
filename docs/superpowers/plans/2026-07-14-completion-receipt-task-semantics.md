# Completion Receipt Task Semantics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make successful file-deletion tasks produce a compact, evidence-backed completed receipt while preserving strict validation requirements for code changes.

**Architecture:** Extend the existing schema-v1 receipt values with a backward-compatible change scope, derive deletion postconditions from structured tool evidence, and render task effects separately from known Naumi runtime artifacts in both terminal clients.

**Tech Stack:** Python 3.12+, dataclasses, async Git probe, `pytest`, Node.js ESM, built-in `node:test`.

## Global Constraints

- Execute inline only; the user explicitly prohibited subagents.
- Keep schema version 1 and make new fields backward compatible.
- Parse only literal workspace-local delete targets; never guess from assistant prose.
- Background paths use a narrow allowlist and never hide explicitly targeted paths.
- Code changes without validation remain partial.
- Run only focused receipt/UI/TUI tests, never the full repository suite.

---

### Task 1: Correct Git deletion semantics and change scope

**Files:**
- Modify: `src/naumi_agent/runs/models.py`
- Modify: `src/naumi_agent/runs/git_probe.py`
- Modify: `frontend/terminal-ui/src/protocol.js`
- Modify: `tests/unit/test_run_receipts.py`

- [ ] Add failing tests for removing a preexisting untracked file and for schema-v1 `scope` defaulting to `task`.
- [ ] Run the two focused tests and confirm the old implementation reports `restored` / lacks scope.
- [ ] Add `ReceiptChange.scope`, map untracked disappearance to `removed_untracked`, and normalize the optional frontend field.
- [ ] Run the focused tests and confirm they pass.

### Task 2: Derive real delete postconditions and isolate runtime artifacts

**Files:**
- Modify: `src/naumi_agent/runs/receipt_builder.py`
- Modify: `tests/unit/test_run_receipts.py`

- [ ] Add failing tests for successful `rm -rf`, nonzero shell exit, residual target, background debug trace, and unchanged code-validation behavior.
- [ ] Confirm the tests fail for the expected semantic reasons.
- [ ] Parse literal `rm` targets, attribute descendant paths, evaluate path absence after successful tool completion, and record filesystem validation evidence.
- [ ] Classify known runtime evidence as background unless explicitly targeted.
- [ ] Make outcome, risks, and next actions depend on task changes rather than background changes; suppress review/commit actions for `removed_untracked` and `restored`.
- [ ] Run `tests/unit/test_run_receipts.py` only.

### Task 3: Compact new UI and TUI rendering

**Files:**
- Modify: `frontend/terminal-ui/src/components/completion-receipt-card.js`
- Modify: `frontend/terminal-ui/test/components.test.js`
- Modify: `src/naumi_agent/tui/completion_receipt.py`
- Modify: `tests/unit/test_tui.py`

- [ ] Add failing renderer tests using the screenshot-equivalent completed deletion receipt.
- [ ] Confirm old renderers expose `0/0`, per-file rows, successful approval noise, and generic actions.
- [ ] Render a shared hierarchy: outcome, summary, real validation, aggregated task impact, compact background count, then actionable failures/risks/actions.
- [ ] Keep successful approvals hidden and make `removed_untracked` user-facing text “删除”.
- [ ] Run only `tests/unit/test_tui.py` and `frontend/terminal-ui/test/components.test.js`.

### Task 4: Real scenario and focused completion checks

**Files:**
- Verify only unless a defect is found.

- [ ] In a temporary Git repo, create an untracked directory and a preexisting runtime trace, capture a baseline, execute real `rm -rf`, emit matching tool events, and finish the receipt.
- [ ] Assert `outcome=completed`, filesystem validation passed, deleted paths are task-scoped, runtime trace is background-scoped, and no generic next action remains.
- [ ] Render the same receipt through TUI formatter and Node completion card; inspect the bounded output.
- [ ] Run targeted Ruff on changed Python files, import compile checks, focused Python tests, focused Node tests, and `git diff --check`.
- [ ] Commit the feature, merge into `main`, repeat focused checks on `main`, fetch before push, and push `origin/main`.

