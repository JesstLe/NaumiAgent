# Workbench Chat and Runtime Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Mac Workbench chat feel live and trustworthy while repairing the task persistence, permission-confirmation, delegation visibility, and memory-isolation failures in `naumi_agent_issues_report.md`.

**Architecture:** Preserve the existing `AgentEngine.run_streaming(...)` event pipeline. The API exposes safe stream envelopes and a short-lived authenticated permission-resolution endpoint; the Mac app maps those envelopes to a bounded execution presentation, never raw model reasoning. Task persistence migrates legacy SQLite schemas before task access. Automatic memory injection becomes session-aware while user preferences remain deliberately global.

**Tech Stack:** Python 3.12, FastAPI, SQLite/aiosqlite, Swift 6, SwiftUI, URLSession SSE, XCTest, pytest.

## Global Constraints

- Product copy is Chinese-first with English fallback through `AppStrings`.
- Never surface raw model thinking deltas, full tool arguments, API tokens, or secrets.
- Keep CLI/TUI confirmation callbacks working; the API broker is installed only for the daemon lifespan.
- Preserve atomic non-streaming chat-plus-Issue creation until the API explicitly supports a combined stream.
- Each task follows red-green-refactor, uses focused verification, commits independently, and pushes `codex/mac-workbench-mvp`.

---

### Task 1: Migrate Legacy Task Schemas

**Files:**
- Modify: `src/naumi_agent/tasks/store.py`
- Modify: `tests/unit/test_tasks.py`

**Interfaces:**
- Produces `TaskStore._ensure_table(db)` that upgrades a historical `tasks(id TEXT PRIMARY KEY, ...)` table to `PRIMARY KEY(session_id, id)`.
- Produces serialized allocation for per-session task IDs.

- [ ] **Step 1: Write the failing migration test**

~~~python
async def test_legacy_global_task_id_schema_is_migrated_before_creating_task(tmp_path):
    db_path = tmp_path / "legacy.db"
    # Seed the historical table where id alone is the primary key.
    ...
    store_a = TaskStore(str(db_path)); store_a.set_session("a")
    store_b = TaskStore(str(db_path)); store_b.set_session("b")
    assert (await store_a.create_task("A")).id == "1"
    assert (await store_b.create_task("B")).id == "1"
~~~

- [ ] **Step 2: Run the test to verify the historical error**

Run: `uv run pytest tests/unit/test_tasks.py::TestTaskStore::test_legacy_global_task_id_schema_is_migrated_before_creating_task -q`

Expected: FAIL with `UNIQUE constraint failed: tasks.id`.

- [ ] **Step 3: Add migration and allocation transaction**

~~~python
async def _ensure_table(self, db):
    if self._initialized:
        return
    await self._migrate_legacy_task_schema(db)
    await db.execute(_CREATE_TABLE)
    await db.commit()
    self._initialized = True
~~~

The migration creates a replacement composite-key table, copies all rows, swaps tables inside one transaction, and leaves data intact. `create_task` opens an immediate SQLite write transaction before calculating `MAX(id) + 1`.

- [ ] **Step 4: Verify**

Run: `uv run pytest tests/unit/test_tasks.py -q`

Expected: PASS.

- [ ] **Step 5: Commit and push**

~~~bash
git add src/naumi_agent/tasks/store.py tests/unit/test_tasks.py
git commit -m "fix: migrate legacy task identifiers"
git push origin codex/mac-workbench-mvp
~~~

### Task 2: Scope Automatic Memory Injection

**Files:**
- Modify: `src/naumi_agent/orchestrator/engine.py`
- Modify: `src/naumi_agent/memory/long_term.py`
- Modify: `tests/unit/test_engine.py`
- Modify: `tests/unit/test_long_term_memory.py`

**Interfaces:**
- `LongTermMemory.recall_for_session(query, session_id, top_k)` returns only active memories owned by the session plus explicit global preferences.
- `AgentEngine._inject_relevant_memories(...)` uses `recall_for_session`.

- [ ] **Step 1: Write failing scope tests**

~~~python
results = await memory.recall_for_session("项目", session_id="current", top_k=3)
assert [item.entry.id for item in results] == ["current-fact", "global-preference"]
~~~

- [ ] **Step 2: Verify red**

Run: `uv run pytest tests/unit/test_long_term_memory.py tests/unit/test_engine.py -k 'session_scoped_memory or inject_relevant_memories' -q`

Expected: FAIL because injection calls unrestricted `recall(...)`.

- [ ] **Step 3: Implement explicit memory scope**

~~~python
scope = "global" if candidate.category == "preference" else "session"
metadata = {"scope": scope, "session_id": session_id, ...}
~~~

Legacy automatic entries with `session_id` are session-local. Unscoped manually stored entries remain global. Non-preference memories never cross sessions through automatic injection.

- [ ] **Step 4: Verify**

Run: `uv run pytest tests/unit/test_long_term_memory.py tests/unit/test_engine.py -k 'memory or inject_relevant_memories' -q`

Expected: PASS.

- [ ] **Step 5: Commit and push**

~~~bash
git add src/naumi_agent/memory/long_term.py src/naumi_agent/orchestrator/engine.py tests/unit/test_long_term_memory.py tests/unit/test_engine.py
git commit -m "fix: scope automatic memory recall"
git push origin codex/mac-workbench-mvp
~~~

### Task 3: Stream Safe Chat Execution and Permission Requests

**Files:**
- Create: `src/naumi_agent/api/permission_broker.py`
- Modify: `src/naumi_agent/api/app.py`
- Modify: `src/naumi_agent/api/routes/messages.py`
- Modify: `src/naumi_agent/api/schemas.py`
- Modify: `src/naumi_agent/orchestrator/engine.py`
- Modify: `src/naumi_agent/streaming/events.py`
- Modify: `tests/unit/test_api.py`
- Create: `tests/unit/test_permission_broker.py`

**Interfaces:**
- `PermissionApprovalBroker.confirm(payload) -> str` waits for an authenticated resolution or expires as deny.
- `POST /sessions/{session_id}/permissions/{call_id}/resolve` accepts only `allow`, `deny`, `bypass`.
- SSE emits `permission_request` with a safe tool label, request ID, reason, and risk level, but no raw arguments.

- [ ] **Step 1: Write failing broker and API tests**

~~~python
async def test_permission_resolution_unblocks_only_matching_session():
    broker = PermissionApprovalBroker(timeout_seconds=1)
    waiting = asyncio.create_task(broker.confirm({"session_id": "s1", "call_id": "c1"}))
    assert await broker.resolve("s2", "c1", "allow") is False
    assert await broker.resolve("s1", "c1", "allow") is True
    assert await waiting == "allow"
~~~

- [ ] **Step 2: Verify red**

Run: `uv run pytest tests/unit/test_permission_broker.py tests/unit/test_api.py -k 'permission or stream_event' -q`

Expected: FAIL because the broker and resolver route do not exist.

- [ ] **Step 3: Implement broker and safe event mapping**

The engine attaches `session_id` and `call_id` to permission bubbles. Only `needs_confirmation` maps to a permission-request stream event. The broker is installed in FastAPI lifespan, uses the current authenticated request boundary, and never logs or returns raw tool arguments.

- [ ] **Step 4: Verify**

Run: `uv run pytest tests/unit/test_permission_broker.py tests/unit/test_api.py tests/unit/test_engine.py -k 'permission or stream' -q`

Expected: PASS.

- [ ] **Step 5: Commit and push**

~~~bash
git add src/naumi_agent/api src/naumi_agent/orchestrator/engine.py src/naumi_agent/streaming/events.py tests/unit/test_api.py tests/unit/test_permission_broker.py
git commit -m "feat: stream safe permission requests"
git push origin codex/mac-workbench-mvp
~~~

### Task 4: Present Live Chat Execution in the Mac App

**Files:**
- Create: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/API/ChatStreamClient.swift`
- Create: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Chat/ChatExecutionPresentation.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/API/WorkbenchAPIProviding.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/API/WorkbenchAPIClient.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Daemon/DaemonController.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Chat/ChatView.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Localization/AppStrings.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/App/PreviewWorkbenchAPIProvider.swift`
- Create: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/ChatExecutionPresentationTests.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/DaemonControllerTests.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/WorkbenchAPIClientTests.swift`

**Interfaces:**
- `ChatExecutionPresentation` maps verified events into preparing, analysing, running tool, awaiting approval, composing, completed, and failed stages.
- `WorkbenchAPIClient.streamMessage(...)` parses SSE defensively.
- `DaemonController.sendDailyMessage(...)` adds a local user message immediately, incrementally updates a transient assistant reply, and refreshes canonical history on completion.

- [ ] **Step 1: Write failing Swift event-presentation test**

~~~swift
func testPermissionEventProducesApprovalStageWithoutArguments() {
    let state = ChatExecutionPresentation()
    let next = state.applying(
        .permissionRequest(callID: "call-1", toolName: "bash_run", reason: "确认后执行")
    )
    XCTAssertEqual(next.stage, .awaitingApproval)
    XCTAssertNil(next.argumentPreview)
}
~~~

- [ ] **Step 2: Verify red**

Run: `swift test --package-path apps/macos/NaumiAgentWorkbench --filter ChatExecutionPresentationTests`

Expected: FAIL because the presentation type does not exist.

- [ ] **Step 3: Implement stream client, controller state, and execution card**

The UI immediately shows the user message and a compact assistant card with elapsed time. It renders only safe event labels and named tools, renders partial token text in the assistant bubble, persists a collapsible trace after completion, and shows a blocking approval panel when needed. Linked-Issue creation remains visibly non-streaming and atomic.

- [ ] **Step 4: Verify focused tests, build, and preview screenshot**

Run: `swift test --package-path apps/macos/NaumiAgentWorkbench --filter 'ChatExecutionPresentationTests|DaemonControllerTests|WorkbenchAPIClientTests'`

Run: `swift build --package-path apps/macos/NaumiAgentWorkbench`

Expected: PASS. Launch preview, send a normal message, inspect normal and compact window screenshots, and confirm the trace card, partial response, and approval card do not overlap the three-column layout.

- [ ] **Step 5: Commit and push**

~~~bash
git add apps/macos/NaumiAgentWorkbench
git commit -m "feat: add live chat execution trace"
git push origin codex/mac-workbench-mvp
~~~

### Task 5: Verify Delegation Result Transparency

**Files:**
- Modify only if reproduction finds a current regression: `src/naumi_agent/tools/subagent.py` and a focused test.
- Verify: `tests/unit/test_subagent_tools.py`, `tests/unit/test_subagent_manager.py`, and Mac chat trace tests.

**Interfaces:**
- A completed `delegate_task` returns the subagent response to the main agent and emits a completion trace. The Mac trace displays completion summary rather than a bare `ok`.

- [ ] **Step 1: Run existing focused delegation tests**

Run: `uv run pytest tests/unit/test_subagent_tools.py tests/unit/test_subagent_manager.py -q`

- [ ] **Step 2: Confirm the result boundary**

~~~python
result = await DelegateTaskTool(manager).execute(task="summarize", agent="researcher")
assert result == "validated summary"
~~~

- [ ] **Step 3: Only if reproduction fails, add one bounded fix and retest**

The historical report alone is not evidence to change a correct core result contract. The Mac trace will nevertheless record a real delegation completion summary.

## Final Verification

- [ ] Run targeted Python suites for task migration, memory scope, permission API, and delegation.
- [ ] Run targeted Mac XCTest suites plus `swift build`.
- [ ] Launch preview, exercise normal chat, tool/permission wait, linked-Issue fallback, and delegation completion; capture and inspect screenshots.
- [ ] Inspect `git diff --check`, `git status --short`, commits, and remote branch after the final push.

