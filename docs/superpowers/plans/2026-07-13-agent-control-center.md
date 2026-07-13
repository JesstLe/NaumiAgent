# Agent Control Center Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an authoritative `/agents` control center with real execution tracking and stopping, a strict revisioned Bridge protocol, and equivalent new Terminal UI and Textual TUI surfaces.

**Architecture:** `SubAgentManager` remains the execution owner and gains per-task child execution records plus bounded terminal history. `AgentControlService` converts manager and message-bus state into strict immutable snapshots. JSONL Bridge streams snapshots/updates and executes stop requests; both frontends render the same schema and never infer backend state.

**Tech Stack:** Python 3.12+, asyncio, dataclasses, pytest/pytest-asyncio, JSONL Bridge, Node.js terminal renderer/tests, Textual, SQLite-backed real `AgentEngine` E2E.

## Global Constraints

- Do not modify `src/naumi_agent/safety/permission_grants.py`, `src/naumi_agent/safety/permissions.py`, or the coworker's permission tests.
- One behavior at a time: write a failing test, observe the expected failure, implement, observe green, then commit.
- All user-visible copy and errors are Chinese; code comments and commit messages are English.
- No prompt wrappers, mock production data, client-side status inference, parallel execution path, or external model call in tests.
- `/agents` is this plan's only command page. Do not implement `/workbench`, Agent creation/configuration, or clickable receipt next actions.
- Stop targets an active execution `task_id`, never an Agent name, and must not cancel sibling executions or the parent run.
- New Terminal UI and Textual TUI consume `AgentControlSnapshot` semantics from the same backend service.

---

### Task 1: Track and stop real sub-Agent executions

**Files:**
- Modify: `src/naumi_agent/orchestrator/subagent_manager.py`
- Modify: `tests/unit/test_subagent_manager.py`

**Interfaces:**
- Produces: `AgentExecutionRecord`, `StopExecutionResult`, `SubAgentManager.list_executions(limit=100)`, and `await SubAgentManager.stop_execution(task_id, reason)`.
- Preserves: `delegate() -> AgentResult`, existing lifecycle events, callback forwarding, DAG/sequential/parallel callers.

- [ ] **Step 1: Write failing execution and stop tests**

Add real asyncio tests with an Agent whose `execute()` blocks on an `asyncio.Event`. Start two `manager.delegate()` calls with distinct `SubTask.id` values, assert both appear in `list_executions()`, stop one, and prove the other remains running:

```python
first = asyncio.create_task(manager.delegate(SubTask("agent-task-1", "first", "coder")))
second = asyncio.create_task(manager.delegate(SubTask("agent-task-2", "second", "coder")))
await started.wait()

accepted = await manager.stop_execution("agent-task-1", "用户停止。")
assert accepted.accepted is True
assert accepted.code == "accepted"
assert (await first).status == "cancelled"
assert not second.done()
assert manager.list_executions()[0].task_id in {"agent-task-1", "agent-task-2"}
```

Also test duplicate active task IDs, unknown/already-finished/repeated stop codes, parent task cancellation propagation, bounded history, and exactly one terminal event.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run python -m pytest tests/unit/test_subagent_manager.py -q`

Expected: FAIL because execution records and `stop_execution()` do not exist.

- [ ] **Step 3: Add immutable public records and private handles**

Implement public frozen records without exposing asyncio handles:

```python
@dataclass(frozen=True)
class AgentExecutionRecord:
    task_id: str
    agent_name: str
    description: str
    status: str
    phase: str
    started_at: float
    finished_at: float | None = None
    elapsed_ms: int = 0
    heartbeat_age_ms: int = 0
    current_tool: str = ""
    recent_tools: tuple[str, ...] = ()
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    turns: int = 0
    error: str = ""
    stop_supported: bool = False
    stop_requested: bool = False

@dataclass(frozen=True)
class StopExecutionResult:
    task_id: str
    accepted: bool
    code: str
    message: str
```

Keep mutable timing plus `asyncio.Task[AgentResult]` in a private `_ActiveExecution`. Guard registration, stop decisions and terminalization with one `asyncio.Lock`; return snapshots, never private objects.

- [ ] **Step 4: Run every Agent execution in its own child task**

Create the child task immediately before awaiting `agent.execute()`. The callback wrapper updates phase/current tool/recent tools/heartbeat and then awaits the original callback unchanged. When a directly cancelled child raises `CancelledError` and the delegate parent has no pending cancellation, return `AgentResult(status="cancelled", error=reason)`; otherwise re-raise.

- [ ] **Step 5: Implement deterministic stop outcomes**

Return these codes and Chinese messages: `accepted`, `missing_task_id`, `not_found`, `already_requested`, `already_finished`. Cancel only the stored child task after marking `stop_requested`; terminalization moves the public record to a 100-entry history.

- [ ] **Step 6: Verify GREEN and regressions**

Run: `uv run python -m pytest tests/unit/test_subagent_manager.py tests/unit/test_engine.py tests/unit/test_agents.py tests/unit/test_message_bus.py -q`

Expected: all pass with no leaked pending asyncio tasks.

- [ ] **Step 7: Commit**

```bash
git add src/naumi_agent/orchestrator/subagent_manager.py tests/unit/test_subagent_manager.py
git commit -m "feat: track and stop agent executions"
```

### Task 2: Build strict authoritative Agent snapshots

**Files:**
- Create: `src/naumi_agent/agent_control/__init__.py`
- Create: `src/naumi_agent/agent_control/models.py`
- Create: `src/naumi_agent/agent_control/service.py`
- Modify: `src/naumi_agent/orchestrator/engine.py`
- Create: `tests/unit/test_agent_control.py`

**Interfaces:**
- Produces: `AGENT_CONTROL_SCHEMA_VERSION = 1`, `AgentControlSnapshot.from_dict()/to_dict()`, `AgentControlService.snapshot()`, `changed_sections(previous, current)`.
- Consumes: Task 1 execution records, `SubAgentManager.list_agents()`, message bus history/blackboard/mailboxes, active session ID.

- [ ] **Step 1: Write failing strict-model and service tests**

Build a real `SubAgentManager`, publish `AgentMessage` values, add blackboard entries, and assert schema, truncation, source warnings, stable revision and session isolation. Assert malformed booleans/integers/enums, unknown sections and oversized arrays are rejected by `from_dict()`.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run python -m pytest tests/unit/test_agent_control.py -q`

Expected: import failure for `naumi_agent.agent_control`.

- [ ] **Step 3: Implement focused immutable models**

Define `AgentSummary`, `AgentDescriptor`, `ExecutionDescriptor`, `TeamMessageDescriptor`, `BlackboardDescriptor`, and `AgentControlSnapshot`. Bound public strings to 2,000 characters, arrays to 100, recent tools to 20, capabilities/tools to 50, and warnings to 20. `from_dict()` must reject wrong types instead of coercing arbitrary values.

- [ ] **Step 4: Implement service collection and revisions**

Collect sources independently so one failure creates `"Agent 数据读取失败：..."` or `"团队数据读取失败：..."` without discarding successful sections. Derive preset/dynamic from `ALL_AGENT_CONFIGS`, pending mail from non-consuming `peek()`, and serialize blackboard values through a bounded JSON-safe summary. Remove `revision`, `generated_at`, elapsed and heartbeat age fields from the comparison signature so wall-clock drift alone does not increment revision.

- [ ] **Step 5: Wire one service into AgentEngine**

After `SubAgentManager` registration, assign:

```python
self.agent_control = AgentControlService(
    self,
    session_id_getter=lambda: self._session.id if self._session else "",
)
```

The getter must reflect resume/new-session changes without constructing another manager.

- [ ] **Step 6: Verify GREEN and commit**

Run: `uv run python -m pytest tests/unit/test_agent_control.py tests/unit/test_engine.py -q`

```bash
git add src/naumi_agent/agent_control src/naumi_agent/orchestrator/engine.py tests/unit/test_agent_control.py
git commit -m "feat: build authoritative agent control snapshots"
```

### Task 3: Stream Agent snapshots and execute stop requests through Bridge

**Files:**
- Modify: `src/naumi_agent/ui/protocol.py`
- Modify: `src/naumi_agent/ui/bridge.py`
- Modify: `frontend/terminal-ui/protocol-contract.json`
- Modify: `frontend/terminal-ui/src/protocol.js`
- Modify: `tests/unit/test_ui_bridge.py`
- Modify: `frontend/terminal-ui/test/protocol.test.js`

**Interfaces:**
- Client: `agents/request`, `agents/stop`.
- Server: `agents/snapshot`, `agents/update`, `agents/action`.
- Produces frontend-normalized strict snapshots for Task 4.

- [ ] **Step 1: Write failing Python and Node protocol tests**

Cover full snapshot, one-section contiguous update, gap fallback, cross-session refusal, missing/unknown/repeated stop, accepted stop, event-driven refresh and last-snapshot retention after refresh errors. Node must reject unknown state/status, non-integer revision, missing sections and extra changed-section names.

- [ ] **Step 2: Verify RED**

Run: `uv run python -m pytest tests/unit/test_ui_bridge.py -k agent_control -q && node --test frontend/terminal-ui/test/protocol.test.js`

Expected: FAIL because protocol enums and normalizers are absent.

- [ ] **Step 3: Add protocol contract and strict normalization**

Add event names to Python enums and `protocol-contract.json`. Normalize every nested object in Node; do not spread untrusted server payloads directly into UI state.

- [ ] **Step 4: Implement Bridge subscription, delta and stop**

Store `_agents_open` plus `_agent_snapshot`. `agents/request` validates session and returns full data if closed, missing, stale, or revision differs; otherwise ACKs current revision. Relevant engine events call `_refresh_agents()`. Only emit a delta when revision is exactly previous + 1 and `changed_sections()` is non-empty; otherwise emit a full snapshot. `agents/stop` calls only `engine.subagent_manager.stop_execution()` and emits `agents/action` followed by a refreshed authoritative snapshot.

- [ ] **Step 5: Verify GREEN and commit**

Run: `uv run python -m pytest tests/unit/test_ui_bridge.py tests/unit/test_agent_control.py -q && node --test frontend/terminal-ui/test/protocol.test.js`

```bash
git add src/naumi_agent/ui/protocol.py src/naumi_agent/ui/bridge.py frontend/terminal-ui/protocol-contract.json frontend/terminal-ui/src/protocol.js tests/unit/test_ui_bridge.py frontend/terminal-ui/test/protocol.test.js
git commit -m "feat: stream agent control protocol"
```

### Task 4: Implement the new Terminal UI `/agents` page

**Files:**
- Create: `frontend/terminal-ui/src/components/agent-control-page.js`
- Modify: `frontend/terminal-ui/src/state.js`
- Modify: `frontend/terminal-ui/src/render.js`
- Modify: `frontend/terminal-ui/src/index.js`
- Modify: `frontend/terminal-ui/src/input-buffer.js`
- Modify: `frontend/terminal-ui/src/ui-state-store.js`
- Modify: `frontend/terminal-ui/src/components/footer.js`
- Modify: `frontend/terminal-ui/test/state.test.js`
- Modify: `frontend/terminal-ui/test/render.test.js`
- Modify: `frontend/terminal-ui/test/input-buffer.test.js`
- Modify: `frontend/terminal-ui/test/index-process.test.js`
- Modify: `frontend/terminal-ui/test/ui-state-store.test.js`
- Modify: `frontend/terminal-ui/test/fixtures/fake-bridge.js`

**Interfaces:**
- Consumes Task 3 normalized `agents/*` records.
- Produces route state `{ name: "agents", originAnchor }` and bounded persisted presentation state; emits `agents/request` and `agents/stop` only through returned effects.

- [ ] **Step 1: Write failing state, interaction and rendering tests**

Assert `/agents` opens without a timeline notice, draft/scroll/Inspector survive round trip, full snapshot and contiguous update apply, gap requests a refresh, three tabs have stable selection, narrow/wide views fit bounds, empty/loading/stale/error are distinct, `x -> y` sends exactly one stop, `n/Esc` cancels, and permission keys win over Agent page keys.

- [ ] **Step 2: Verify RED**

Run: `node --test frontend/terminal-ui/test/state.test.js frontend/terminal-ui/test/render.test.js frontend/terminal-ui/test/input-buffer.test.js frontend/terminal-ui/test/index-process.test.js frontend/terminal-ui/test/ui-state-store.test.js`

Expected: FAIL because route and component are absent.

- [ ] **Step 3: Add bounded state and revision recovery**

Store business snapshot only in memory. Persist version 5 presentation fields: `open`, `tab`, per-tab selected ID, detail ID and scroll offset. Migrate v4 by adding closed defaults. On refresh failure, retain snapshot and mark stale with Chinese error text.

- [ ] **Step 4: Render full-page responsive Agent UI**

At width `>=100`, render list and detail side by side with a minimum 42-column list; below 100 render the selected list or detail as one column. Use ANSI width helpers for CJK clipping. Show backend ages, tool/status fields and action availability exactly as received.

- [ ] **Step 5: Implement keyboard priority and confirmed stop**

Resolution order is permission, stop confirmation, Agent page, Inspector, composer. Do not mutate execution status on send; set only local `actionPendingTaskId` until `agents/action` arrives, then wait for a snapshot to display terminal state.

- [ ] **Step 6: Verify GREEN and commit**

Run the command from Step 2 and `node --check` on each modified source file.

```bash
git add frontend/terminal-ui/src frontend/terminal-ui/test
git commit -m "feat: render agent control center in terminal ui"
```

### Task 5: Synchronize the Textual TUI

**Files:**
- Create: `src/naumi_agent/tui/agent_control.py`
- Modify: `src/naumi_agent/tui/app.py`
- Modify: `src/naumi_agent/ui/keybindings.py`
- Create: `tests/unit/test_tui_agent_control.py`
- Modify: `tests/unit/test_tui.py`
- Modify: `tests/unit/test_keybindings.py`

**Interfaces:**
- Produces: `format_agent_control_markdown(snapshot, tab, selected_id)` and `AgentControlScreen`.
- Consumes: `engine.agent_control.snapshot()` and `engine.subagent_manager.stop_execution()`.

- [ ] **Step 1: Write failing formatter and Textual pilot tests**

Assert all three tabs render the same authoritative fields, empty and warning states are explicit, refresh failure keeps prior content, `/agents` opens the screen, selection/detail works, `x/y` confirmation stops only the chosen execution, and an active permission modal prevents page actions.

- [ ] **Step 2: Verify RED**

Run: `uv run python -m pytest tests/unit/test_tui_agent_control.py tests/unit/test_tui.py tests/unit/test_keybindings.py -q`

Expected: import/keybinding failures.

- [ ] **Step 3: Implement shared formatter and screen**

The formatter accepts only `AgentControlSnapshot`; the screen never calls `list_agents()` or message bus directly. Use Textual `TabbedContent`, `ListView`, detail Markdown, refresh state, Chinese notifications and an explicit confirmation state before stop.

- [ ] **Step 4: Wire `/agents` and a shared action**

Add `KeybindingAction.OPEN_AGENTS` with a non-conflicting default validated by the keybinding registry. Route the Textual slash command before generic slash execution so it pushes `AgentControlScreen`; completion events refresh an open screen.

- [ ] **Step 5: Verify GREEN and commit**

Run the command from Step 2 plus `uv run ruff check src/naumi_agent/tui/agent_control.py src/naumi_agent/tui/app.py src/naumi_agent/ui/keybindings.py`.

```bash
git add src/naumi_agent/tui/agent_control.py src/naumi_agent/tui/app.py src/naumi_agent/ui/keybindings.py tests/unit/test_tui_agent_control.py tests/unit/test_tui.py tests/unit/test_keybindings.py
git commit -m "feat: sync agent control center to textual tui"
```

### Task 6: Prove the real cross-frontend flow and publish 0.1.214

**Files:**
- Create: `tests/e2e/test_agent_control_center.py`
- Modify: `frontend/terminal-ui/test/fixtures/python-bridge-fixture.py`
- Modify: `frontend/terminal-ui/test/index-process.test.js`
- Modify: `docs/product/terminal-ui/04-inspector-and-command-pages.md`
- Modify: `docs/product/terminal-ui/README.md`
- Modify: `docs/superpowers/specs/2026-07-13-terminal-ui-productization-design.md`
- Modify: `docs/13-cli-tui-claude-code-roadmap.md`
- Modify: `CHANGELOG.md`
- Modify: `pyproject.toml`
- Modify: `src/naumi_agent/__init__.py`
- Modify: `uv.lock`

**Interfaces:**
- Verifies Tasks 1-5 as one real flow; version becomes exactly `0.1.214`.

- [ ] **Step 1: Write the real failing E2E**

Use a real `AgentEngine`, real manager and message bus, and a local deterministic Agent whose `execute()` awaits an event. Start it through `manager.delegate()`, request `/agents` through real `JsonlEngineBridge`, pipe exact JSON through the Node state/renderer, send `agents/stop`, assert `AgentResult.status == "cancelled"`, then format the resulting service snapshot with Textual. A sibling execution must complete normally. No external model call and no fabricated snapshot are allowed.

- [ ] **Step 2: Verify RED then GREEN**

Run: `PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring uv run python -m pytest tests/e2e/test_agent_control_center.py -q`

Expected before fixture completion: FAIL at the missing real process path. Expected after fixture completion: all tests pass.

- [ ] **Step 3: Update process fixture and docs**

Exercise `/agents`, detail navigation, confirmed stop and return-to-chat through the real Python JSONL fixture. Document implemented evidence and retain explicit gaps: Agent creation/reconfiguration, remote persistence, Workbench mapping and `/workbench` remain incomplete.

- [ ] **Step 4: Bump and lock version**

Set `pyproject.toml`, `src/naumi_agent/__init__.py` and local package in `uv.lock` to `0.1.214`; run `uv lock` rather than manually rewriting dependency resolution.

- [ ] **Step 5: Run release gates**

```bash
uv run ruff check src tests/e2e/test_agent_control_center.py frontend/terminal-ui/test/fixtures/python-bridge-fixture.py
uv run python -c 'import naumi_agent; from naumi_agent.agent_control import AgentControlService; assert naumi_agent.__version__ == "0.1.214"'
PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring uv run python -m pytest tests/ -x
node frontend/terminal-ui/scripts/run-tests.js
uv build
git diff --check
```

Expected: zero failures, versioned wheel/sdist, and no permission-system paths in the diff.

- [ ] **Step 6: Self-review and commit**

Check every design requirement against code/tests, scan production files for placeholders/mock data, verify coworker checkout remains untouched, and state remaining gaps honestly.

```bash
git add CHANGELOG.md docs pyproject.toml src/naumi_agent/__init__.py uv.lock tests/e2e/test_agent_control_center.py frontend/terminal-ui/test/fixtures/python-bridge-fixture.py frontend/terminal-ui/test/index-process.test.js
git commit -m "test: verify agent control center end to end"
git push origin codex/terminal-completion-receipt
```
