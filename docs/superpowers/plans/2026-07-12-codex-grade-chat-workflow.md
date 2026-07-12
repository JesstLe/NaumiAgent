# Codex-Grade Chat Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the existing three-column Workbench Chat page into a real-data Codex-grade conversation, execution, artifact, and environment workflow.

**Architecture:** Keep the existing shell, navigation, `DaemonController`, and streaming endpoint. Split the monolithic SwiftUI Chat page into focused presentation components, then add persisted run/step/artifact records to the Python API and expose real Git, process, source, Mission, and Issue evidence through a typed Swift client. Each task is independently tested and committed.

**Tech Stack:** Swift 6, SwiftUI, Swift Testing, Python 3.14, FastAPI, Pydantic, SQLite, pytest, SSE, existing Workbench service/store.

## Global Constraints

- Preserve the Workbench top navigation and three-column page frame.
- Keep all three columns visible at every supported window size.
- Default to Chinese and provide English for every new user-visible string.
- Do not expose chain-of-thought, system prompts, secrets, or raw tool arguments.
- Do not display fake Git, process, source, validation, Mission, or Issue evidence.
- Keep card radii at 8 pt or less and use the existing native component theme.
- Run targeted tests for small tasks; run the complete Swift and related backend suites after each major module.
- Commit and push each independently verified task.

---

### Task 1: Chat Presentation Contracts and Component Boundaries

**Files:**
- Create: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Chat/ChatPresentation.swift`
- Create: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Chat/ChatMessageRow.swift`
- Create: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Chat/ChatContextRail.swift`
- Test: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/ChatPresentationTests.swift`

**Interfaces:**
- Consumes: `ChatMessageDTO`, `IssueDTO`, `MissionDTO`, `AppLocale`.
- Produces: `ChatMessageStyle`, `ChatContextSummary`, and pure formatting functions used by all later Chat components.

- [ ] **Step 1: Write failing presentation tests**

```swift
@Test func assistantUsesDocumentStyleAndUserUsesCompactBubble() {
    #expect(ChatPresentation.style(forRole: "assistant") == .document)
    #expect(ChatPresentation.style(forRole: "user") == .compactBubble)
}

@Test func issuesAreRiskSortedWithoutInventingCounts() {
    let summaries = ChatPresentation.issueSummaries(from: fixtures)
    #expect(summaries.map(\.riskLevel) == ["critical", "high", "medium"])
}
```

- [ ] **Step 2: Verify the tests fail**

Run: `cd apps/macos/NaumiAgentWorkbench && ./scripts/test.sh --filter ChatPresentationTests`

Expected: FAIL because `ChatPresentation` does not exist.

- [ ] **Step 3: Implement the contracts and native rows**

```swift
public enum ChatMessageStyle: Equatable, Sendable { case compactBubble, document }

public enum ChatPresentation {
    public static func style(forRole role: String) -> ChatMessageStyle {
        role == "user" ? .compactBubble : .document
    }
}
```

Use `ChatMessageRow` for unframed assistant text and a restrained user surface; use `ChatContextRail` for flat session, Mission, Issue, and recent-run groups.

- [ ] **Step 4: Run the focused tests**

Run: `cd apps/macos/NaumiAgentWorkbench && ./scripts/test.sh --filter ChatPresentationTests`

Expected: PASS.

- [ ] **Step 5: Commit and push**

```bash
git add apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Chat \
  apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/ChatPresentationTests.swift
git commit -m "feat: add codex chat presentation contracts"
git push origin codex/mac-workbench-mvp
```

### Task 2: Codex-Style Conversation and Floating Composer

**Files:**
- Create: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Chat/ChatConversationView.swift`
- Create: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Chat/ChatComposer.swift`
- Create: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Chat/ChatIssueDraftPanel.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Chat/ChatView.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Localization/AppStrings.swift`
- Test: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/ChatComposerPresentationTests.swift`
- Test: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/AppStringsChatTests.swift`

**Interfaces:**
- Consumes: Task 1 presentation contracts and existing `sendDailyMessage(content:issueDraft:)`.
- Produces: `ChatComposerMode`, `ChatComposerPresentation`, and a focused `ChatView` that only coordinates the three columns.

- [ ] **Step 1: Write failing composer state tests**

```swift
@Test func sendingReplacesSendWithStopAndKeepsDraftVisible() {
    let state = ChatComposerPresentation(isSending: true, hasError: false)
    #expect(state.primaryAction == .stop)
    #expect(state.isEditorEnabled)
}

@Test func createIssueExpandsDetailsOnlyInCreateMode() {
    #expect(ChatComposerMode.createIssue.showsIssueDetails)
    #expect(!ChatComposerMode.chat.showsIssueDetails)
}
```

- [ ] **Step 2: Verify failure**

Run: `cd apps/macos/NaumiAgentWorkbench && ./scripts/test.sh --filter ChatComposerPresentationTests`

Expected: FAIL because the composer presentation types do not exist.

- [ ] **Step 3: Implement the conversation and composer**

Use a maximum 760 pt reading column, `ChatMessageRow`, a bottom overlay composer, an attachment/source button, Mission menu, task-linkage menu, execution-policy menu, and icon-only send/stop/retry actions with Help text. Move the existing Issue form into `ChatIssueDraftPanel` and preserve all current validation and fallback-title behavior.

- [ ] **Step 4: Verify focused Chat and localization tests**

Run: `cd apps/macos/NaumiAgentWorkbench && ./scripts/test.sh --filter ChatComposerPresentationTests && ./scripts/test.sh --filter AppStringsChatTests`

Expected: PASS with Chinese and English strings covered.

- [ ] **Step 5: Build and commit**

Run: `cd apps/macos/NaumiAgentWorkbench && swift build`

Expected: build exits 0.

Commit: `git commit -m "feat: rebuild workbench chat conversation surface"` and push.

### Task 3: Execution Timeline and Structured Result Components

**Files:**
- Create: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Chat/ChatRunTimeline.swift`
- Create: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Chat/ChatArtifactView.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Chat/ChatExecutionPresentation.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/API/ChatStreamEvent.swift`
- Test: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/ChatExecutionPresentationTests.swift`
- Test: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/ChatArtifactPresentationTests.swift`

**Interfaces:**
- Consumes: existing sanitized SSE event payloads.
- Produces: stable timeline steps, compact completed-run summary, permission card, and artifact views for command, task, validation, file change, and subagent results.

- [ ] **Step 1: Add failing tests for step identity and completion collapse**

```swift
@Test func repeatedPermissionUpdatesReplaceTheSameStep() {
    let execution = fixture.applying(permissionPending).applying(permissionResolved)
    #expect(execution.steps.filter { $0.id == permissionPending.id }.count == 1)
}

@Test func completedRunUsesCompactElapsedSummary() {
    #expect(ChatRunSummary(stage: .completed, seconds: 727).isCollapsedByDefault)
}
```

- [ ] **Step 2: Verify failure, implement, and verify green**

Run red and green with: `cd apps/macos/NaumiAgentWorkbench && ./scripts/test.sh --filter ChatExecutionPresentationTests && ./scripts/test.sh --filter ChatArtifactPresentationTests`

Expected final result: PASS.

- [ ] **Step 3: Integrate timeline and result cards into `ChatConversationView`**

Render a thin vertical rail, stage icon, public summary, elapsed time, sanitized tool name, approval buttons, and failure/retry state. Do not render hidden reasoning or raw argument payloads.

- [ ] **Step 4: Run the complete Swift suite for the first major UI module**

Run: `cd apps/macos/NaumiAgentWorkbench && ./scripts/test.sh`

Expected: all suites pass.

- [ ] **Step 5: Package, visually inspect, commit, and push**

Run: `cd apps/macos/NaumiAgentWorkbench && ./scripts/package-dev-app.sh`

Commit: `git commit -m "feat: add codex chat execution timeline"` and push.

### Task 4: Persisted Chat Run, Step, and Artifact Backend

**Files:**
- Create: `src/naumi_agent/api/chat_runs.py`
- Modify: `src/naumi_agent/api/schemas.py`
- Modify: `src/naumi_agent/api/routes/messages.py`
- Modify: `src/naumi_agent/api/app.py`
- Test: `tests/unit/test_chat_runs.py`
- Test: `tests/unit/test_api.py`

**Interfaces:**
- Produces: `ChatRunStore`, `ChatRunRecord`, `ChatRunStepRecord`, `ChatArtifactRecord`, and session-scoped list/detail APIs.
- Run endpoint: `GET /api/v1/sessions/{session_id}/runs`.
- Detail endpoint: `GET /api/v1/sessions/{session_id}/runs/{run_id}`.

- [ ] **Step 1: Write failing store tests**

```python
async def test_run_store_restores_ordered_steps_and_artifacts_after_restart(tmp_path):
    store = ChatRunStore(tmp_path / "chat-runs.sqlite3")
    run = await store.start_run(session_id="s1", user_message_id="m1")
    await store.append_step(run.id, sequence=1, stage="tool", summary="运行测试")
    await store.append_artifact(run.id, kind="validation", summary={"passed": 2})
    restored = await ChatRunStore(tmp_path / "chat-runs.sqlite3").get_run("s1", run.id)
    assert [step.sequence for step in restored.steps] == [1]
    assert restored.artifacts[0].kind == "validation"
```

- [ ] **Step 2: Verify failure**

Run: `pytest tests/unit/test_chat_runs.py -q`

Expected: FAIL because the store is missing.

- [ ] **Step 3: Implement SQLite persistence and API models**

Use structured tables keyed by `session_id` and `run_id`, uniqueness on `(run_id, sequence)`, JSON only for typed artifact summaries, and idempotent event upserts.

- [ ] **Step 4: Connect streaming events to persisted runs**

Create the run before `engine.run_streaming`, append sanitized steps in `_stream_response`, complete/fail/cancel the run at the terminal event, and attach the final assistant message ID.

- [ ] **Step 5: Verify backend module**

Run: `ruff check src/naumi_agent/api/chat_runs.py src/naumi_agent/api/routes/messages.py src/naumi_agent/api/schemas.py && pytest tests/unit/test_chat_runs.py tests/unit/test_api.py -q`

Expected: PASS.

- [ ] **Step 6: Commit and push**

Commit: `git commit -m "feat: persist chat execution runs"` and push.

### Task 5: Swift Run Snapshot and Recovery Client

**Files:**
- Create: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/API/DTO/ChatRunDTO.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/API/WorkbenchAPIProviding.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/API/WorkbenchAPIClient.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/App/AppState.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Daemon/DaemonController.swift`
- Test: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/DTODecodeTests.swift`
- Test: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/WorkbenchAPIClientTests.swift`

**Interfaces:**
- Consumes: Task 4 run APIs.
- Produces: `chatRuns`, selected run, restart recovery, and deduplicated live updates in `AppState`.

- [ ] **Step 1: Add failing DTO and route tests**

Decode a fixture containing ordered steps, permission state, file-change artifact, and source reference; assert endpoint templates include session and run IDs.

- [ ] **Step 2: Implement typed DTOs and refresh flow**

On session selection, fetch messages and runs together. Merge SSE updates by stable run/step ID and sequence. Preserve a terminal run in history instead of deleting all execution evidence when streaming ends.

- [ ] **Step 3: Verify and commit**

Run: `cd apps/macos/NaumiAgentWorkbench && ./scripts/test.sh --filter DTODecodeTests && ./scripts/test.sh --filter WorkbenchAPIClientTests`

Commit: `git commit -m "feat: restore chat runs in workbench"` and push.

### Task 6: Real Environment, Git, Process, and Source Inspector

**Files:**
- Create: `src/naumi_agent/api/chat_environment.py`
- Modify: `src/naumi_agent/api/routes/messages.py`
- Create: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/API/DTO/ChatEnvironmentDTO.swift`
- Create: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Chat/ChatInspector.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/API/WorkbenchAPIClient.swift`
- Test: `tests/unit/test_chat_environment.py`
- Test: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/ChatInspectorPresentationTests.swift`

**Interfaces:**
- Produces: `GET /api/v1/sessions/{session_id}/environment` with real workspace, branch, changed files, additions/deletions, process, linked governance object, and source summaries.

- [ ] **Step 1: Write failing real-repository tests**

Create a temporary Git repository, modify one tracked file, and assert the environment summary reports the actual branch and diff counts. Verify a path outside the registered workspace is rejected.

- [ ] **Step 2: Implement safe evidence collectors**

Use `git status --porcelain=v2`, `git diff --numstat`, tracked Workbench process records, and persisted source references. Do not inspect arbitrary system processes or paths outside the workspace.

- [ ] **Step 3: Implement the right inspector**

Use one grouped container divided into Environment, Changes, Local Workspace, Background Processes, Linked Objects, and Sources. Empty states contain no fabricated numbers.

- [ ] **Step 4: Verify backend and Swift presentation tests**

Run: `ruff check src/naumi_agent/api/chat_environment.py && pytest tests/unit/test_chat_environment.py -q`

Run: `cd apps/macos/NaumiAgentWorkbench && ./scripts/test.sh --filter ChatInspectorPresentationTests`

- [ ] **Step 5: Commit and push**

Commit: `git commit -m "feat: add real chat environment inspector"` and push.

### Task 7: Review, Mission, Issue, Source, and Stop/Retry Navigation

**Files:**
- Create: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Chat/ChatNavigationCommand.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Chat/ChatComposer.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/App/AppState.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Daemon/DaemonController.swift`
- Modify: `src/naumi_agent/api/routes/messages.py`
- Test: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/ChatNavigationCommandTests.swift`
- Test: `tests/unit/test_api.py`

**Interfaces:**
- Produces: safe source attachment, existing-Issue linking, Review navigation, run cancellation, and idempotent failed-step retry.

- [ ] **Step 1: Write failing command tests**

Assert a file-change artifact maps to `.reviews` with the linked run/Issue selected; a task artifact maps to `.taskMarket`; unsupported artifacts do not navigate.

- [ ] **Step 2: Implement commands and backend cancellation/retry**

Cancellation marks the run cancelled and signals the active engine task. Retry creates a new attempt linked to the original failed step and never replays a completed side effect automatically.

- [ ] **Step 3: Verify focused tests and commit**

Run Swift navigation tests and `pytest tests/unit/test_api.py -q`.

Commit: `git commit -m "feat: link chat runs to workbench governance"` and push.

### Task 8: Localization, Accessibility, Full Verification, and Packaging

**Files:**
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Localization/AppStrings.swift`
- Modify: all new Chat SwiftUI components from Tasks 1–7.
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/App/WorkbenchPreviewLoader.swift`
- Test: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/AppStringsChatTests.swift`
- Test: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/WorkbenchPreviewLoaderTests.swift`

**Interfaces:**
- Produces: completed Chinese/English UI, deterministic preview states, keyboard/VoiceOver labels, signed development app, and visual QA evidence.

- [ ] **Step 1: Add failing string and preview coverage tests**

Require non-empty Chinese and English strings for every new stage, inspector section, composer mode, action, empty state, and error. Preview fixtures must include a normal reply, active tool run, approval, file change, process, and source.

- [ ] **Step 2: Implement missing localization and accessibility**

Add Help and accessibility labels to icon buttons, combine timeline rows in chronological reading order, keep text selectable, and expose disabled reasons.

- [ ] **Step 3: Run major-module verification**

Run:

```bash
ruff check src/naumi_agent/api/
pytest tests/unit/test_chat_runs.py tests/unit/test_chat_environment.py tests/unit/test_api.py tests/unit/test_api_middleware.py -q
cd apps/macos/NaumiAgentWorkbench
./scripts/test.sh
./scripts/package-dev-app.sh --include-fixtures
codesign --verify --deep --strict dist/NaumiAgentWorkbench.app
```

Expected: all commands exit 0.

- [ ] **Step 4: Run real application scenarios**

Start the local daemon, open the packaged app, run ordinary chat, subagent, permission, file modification, task linkage, service restart, Chinese, English, desktop, and minimum-window scenarios. Capture screenshots and compare them with the supplied Codex reference for component hierarchy, spacing, timeline, inspector, and composer behavior.

- [ ] **Step 5: Self-review and final commit**

Check for fake data, raw tool arguments, hidden columns, overlapping controls, missing translations, and uncommitted changes.

Commit: `git commit -m "feat: complete codex-grade workbench chat"` and push.

