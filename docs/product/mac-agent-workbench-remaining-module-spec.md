# NaumiAgent Mac Agent Workbench Remaining Module Specification

> Date: 2026-07-08
> Branch context: `codex/mac-workbench-mvp`
> Purpose: turn every fake-data, preview-only, partially implemented, or not-yet-productized Mac Workbench capability into fine-grained implementation modules.

## 1. Why This Document Exists

The current Mac Agent Workbench can build, render the full navigation set, load preview fixtures, call many Workbench REST endpoints, and show a convincing governance UI. It is not yet a fully real local Mac product because several screens still mix live data with deterministic fixture rows, some product flows still depend on preview-only helpers, and the app does not yet manage the local daemon or produce a distributable app bundle.

This document is the module backlog for moving from:

```text
Previewable SwiftUI shell with strong backend/API contracts
```

to:

```text
Local Mac app that connects to a real NaumiAgent daemon, shows only authoritative state, lets the user govern work, and can be packaged for internal use.
```

## 2. Current-State Evidence

The module breakdown below is grounded in the current repository state:

- SwiftUI app package: `apps/macos/NaumiAgentWorkbench/Package.swift`
- App entrypoint: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbench/NaumiAgentWorkbenchApp.swift`
- Shared state and environment:
  - `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/App/AppState.swift`
  - `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/App/AppEnvironment.swift`
- API client and protocol:
  - `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/API/WorkbenchAPIProviding.swift`
  - `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/API/WorkbenchAPIClient.swift`
- Controller:
  - `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Daemon/DaemonController.swift`
  - `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Daemon/WorkbenchRefreshCoordinator.swift`
- Preview-only and fixture sources:
  - `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/App/WorkbenchPreviewLoader.swift`
  - `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/App/PreviewWorkbenchAPIProvider.swift`
  - `apps/macos/NaumiAgentWorkbench/Fixtures/workbench_snapshot_zh.json`
  - `apps/macos/NaumiAgentWorkbench/Fixtures/workbench_snapshot_en.json`
- Known fixture-mixing views:
  - `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/TaskMarket/TaskMarketDesignPresentation.swift`
  - `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Reviews/ReviewsDesignPresentation.swift`
- Backend route surface:
  - `src/naumi_agent/api/routes/workbench.py`
  - `src/naumi_agent/workbench/service.py`
  - `src/naumi_agent/workbench/store.py`
  - `src/naumi_agent/workbench/market.py`
  - `src/naumi_agent/workbench/validation.py`

## 3. Global Product Rules

Every module must follow these rules:

1. **No silent fixture mixing in real mode.** Preview data may exist only behind explicit `--preview-fixture` or SwiftUI preview flags.
2. **Snapshot is authoritative.** SwiftUI may derive presentation rows, but it must not invent business state.
3. **Every write operation must return or trigger a fresh state path.** Preferred path is `include_snapshot=true`; fallback is targeted list refresh plus event refresh.
4. **Every async write must guard stale selected session.** If `selectedSessionID` changes before a response returns, the response must not mutate current state.
5. **Chinese first, English fallback.** All user-visible copy must go through `AppStrings` or an equivalent localization layer.
6. **Local-first boundary.** Default daemon host is `127.0.0.1`; no default LAN exposure.
7. **One module slice, one verification, one commit.** Keep implementation changes reviewable.
8. **No app-store constraints in MVP.** Development and internal builds can use local daemon mode before notarized distribution exists.

## 4. Status Legend

| Status | Meaning |
|--------|---------|
| Implemented | Code exists and is wired to real backend state. |
| Partially Implemented | Core code exists, but real-mode UX, coverage, or data completeness is missing. |
| Preview Only | Works only through fixtures or design presentation fallbacks. |
| Missing | Needs new backend, SwiftUI, packaging, or test work. |

## 5. Module Dependency Order

```text
M00 Inventory and fake-data boundary
  -> M01 Real app bootstrap
  -> M02 Manual daemon connection
  -> M03 Session and workspace registry
  -> M04 Snapshot completeness and empty states
  -> M05 Event stream reliability
  -> M06 Dashboard real-data mode
  -> M07 Task Market real-data mode
  -> M08 Chat and issue linkage
  -> M09 Worktrees real operations
  -> M10 Validation and failure operations
  -> M11 Reviews and approvals real-data mode
  -> M12 Governance policy surface
  -> M13 Agent profiles and activity
  -> M14 Timeline replay and causality
  -> M15 Proposal mode and next-step pool
  -> M16 Localization completeness
  -> M17 Dev packaging
  -> M18 Security, tokens, and privacy
  -> M19 E2E smoke and release gates
  -> M20 Internal signed build and distribution
```

## M00. Fake-Data Boundary and Real-Mode Inventory

**Status:** Missing as a tracked product gate; preview code exists.

**Problem:** Several pages look complete because fixture rows fill gaps. This is acceptable for preview screenshots, but dangerous in real mode because the user may think fake bids, files, diffs, or validations are real.

**Current fake-data sources:**

- `WorkbenchPreviewLoader.applyPreviewState(...)` injects sessions, chat messages, validation runs, context snapshots, worktrees, approvals, and timeline events.
- `PreviewWorkbenchAPIProvider` returns preview objects for nearly every API method when compiled with local previews.
- `TaskMarketDesignPresentation` appends fixture issues, bids, and active leases.
- `ReviewsDesignPresentation` appends fixture queues, changed files, diff rows, timeline events, and agent notes.
- SwiftUI preview blocks in individual views create local sample state.

**Target behavior:**

- In real mode, if the backend returns no bids, no diff rows, no reviews, or no validation runs, the UI shows a polished empty state.
- In preview mode, all fixture-derived rows are visibly allowed because preview mode is explicit.
- The app exposes a developer-visible diagnostic flag: `appState.isPreviewFixture`.

**Implementation slices:**

1. Add a `RealDataPolicy` presentation helper that answers:
   - `canUseDesignFillers`
   - `shouldShowEmptyState`
   - `shouldLabelPreviewData`
2. Add tests proving `TaskMarketDesignPresentation` does not append fixture rows when `isPreviewFixture == false`.
3. Add tests proving `ReviewsDesignPresentation` does not append fixture rows when `isPreviewFixture == false`.
4. Add a debug-only UI label for preview fixture mode.
5. Add a documentation check in `docs/quality/mac-agent-workbench-acceptance.md` that real mode must not contain `fixture-` IDs.

**Files:**

- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/App/AppState.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/TaskMarket/TaskMarketDesignPresentation.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Reviews/ReviewsDesignPresentation.swift`
- Test: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/TaskMarketDesignPresentationTests.swift`
- Test: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/ReviewsDesignPresentationTests.swift`
- Update: `docs/quality/mac-agent-workbench-acceptance.md`

**Acceptance criteria:**

- Real mode never shows `fixture-lease-*`, `design-*`, fake diff rows, or fake review notes.
- Preview mode still renders rich screenshots.
- Empty states explain what must happen next, such as “当前没有待审批项” or “还没有 Agent 投标记录”.

**Verification:**

```bash
apps/macos/NaumiAgentWorkbench/scripts/test.sh --filter TaskMarketDesignPresentation
apps/macos/NaumiAgentWorkbench/scripts/test.sh --filter ReviewsDesignPresentation
apps/macos/NaumiAgentWorkbench/scripts/capture-preview-screens.sh zh /tmp/naumi-workbench-preview-zh
```

## M01. Real App Bootstrap

**Status:** Partially Implemented.

**Current behavior:**

- `NaumiAgentWorkbenchApp` starts periodic refresh and event-stream health probes in normal mode.
- Preview mode is explicit through `--preview-fixture`.
- `AppEnvironment` creates `WorkbenchAPIClient()` with default base URL.

**Missing behavior:**

- First launch does not present a clear connection setup flow.
- There is no persisted endpoint configuration.
- The app does not distinguish “daemon not running”, “protocol mismatch”, “auth failed”, and “workspace unavailable” in a user-friendly first-run path.

**Target behavior:**

On launch:

```text
Read saved endpoint
  -> fetch daemon status
  -> fetch capabilities
  -> fetch sessions
  -> select recent session or show session creation
  -> fetch snapshot
  -> start event stream
```

If connection fails, show:

```text
无法连接本地 NaumiAgent 服务
地址: http://127.0.0.1:8765
操作: 重试 / 修改地址 / 查看启动命令
```

**Implementation slices:**

1. Create persisted `WorkbenchConnectionSettings` under Application Support.
2. Add UI state for first-run setup.
3. Add connection-state copy for:
   - disconnected
   - connecting
   - connected
   - stale
   - authFailed
   - protocolMismatch
4. Add tests for startup state transitions in `DaemonController`.
5. Add a manual setup sheet accessible from Settings and the launch failure state.

**Files:**

- Create: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Daemon/WorkbenchConnectionSettings.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/App/AppEnvironment.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Daemon/DaemonController.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/App/WorkbenchShellView.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Localization/AppStrings.swift`
- Test: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/WorkbenchConnectionSettingsTests.swift`
- Test: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/DaemonControllerTests.swift`

**Acceptance criteria:**

- A fresh app install can connect to `http://127.0.0.1:8765` without editing code.
- A failed connection gives a specific reason and next action.
- Refresh does not overwrite preview mode.

**Verification:**

```bash
apps/macos/NaumiAgentWorkbench/scripts/test.sh --filter WorkbenchConnectionSettings
apps/macos/NaumiAgentWorkbench/scripts/test.sh --filter DaemonControllerTests
cd apps/macos/NaumiAgentWorkbench && swift build
```

## M02. Manual Daemon Connection and Health

**Status:** Partially Implemented.

**Current behavior:**

- Backend docs define `GET /api/v1/workbench/daemon/status`.
- Swift DTO exists: `DaemonStatusDTO`.
- `DaemonController.refreshConnection()` consumes status and capabilities.

**Missing behavior:**

- The app does not help the user start the daemon.
- Health failures are not grouped into user actions.
- No daemon logs are visible.

**Target behavior:**

Manual daemon mode remains the first real mode:

```bash
naumi-agent api --host 127.0.0.1 --port 8765
```

The Mac app should:

- detect whether the daemon is reachable,
- show the exact command to start it,
- retry with backoff,
- expose last health-check time,
- keep a concise connection log.

**Implementation slices:**

1. Add `DaemonHealthPresentation`.
2. Add Settings panel for API base URL and health status.
3. Add command-copy button for daemon startup.
4. Add error-specific retries:
   - network failure
   - invalid response
   - unsupported protocol
   - invalid API key
5. Add tests for health presentation copy in Chinese and English.

**Files:**

- Create: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Daemon/DaemonHealthPresentation.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Settings/SettingsView.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Localization/AppStrings.swift`
- Test: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/DaemonHealthPresentationTests.swift`

**Acceptance criteria:**

- If the API server is down, the app shows “未连接” and a start command.
- If protocol version is unsupported, write actions are disabled.
- User can change endpoint and refresh without restarting the app.

## M03. App-Managed Light Daemon

**Status:** Missing.

**Target behavior:**

The Mac app can start and supervise a local daemon without bundling Python runtime:

```text
Find naumi-agent command
  -> select available port 8765-8799
  -> start process
  -> wait for health
  -> save pid and endpoint
  -> stream stdout/stderr to log store
```

**Non-goals for this module:**

- No bundled Python runtime.
- No LaunchAgent.
- No notarized install flow.

**Implementation slices:**

1. Add `DaemonLaunchConfiguration`.
2. Add `DaemonProcessController` wrapping `Process`.
3. Add port scanner for `127.0.0.1:8765...8799`.
4. Add stdout/stderr log ring buffer.
5. Add “启动本地服务” and “停止本地服务” controls.
6. Add shutdown prompt: keep daemon running or stop it.

**Files:**

- Create: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Daemon/DaemonLaunchConfiguration.swift`
- Create: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Daemon/DaemonProcessController.swift`
- Create: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Daemon/DaemonLogStore.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Settings/SettingsView.swift`
- Test: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/DaemonProcessControllerTests.swift`

**Acceptance criteria:**

- The app can start a daemon using an existing local `naumi-agent` binary.
- The selected port is saved and reused.
- Logs are visible without exposing tokens.
- If daemon exits, connection state becomes stale with a restart action.

## M04. Session and Workspace Registry

**Status:** Partially Implemented.

**Current behavior:**

- Sessions API and DTOs exist.
- `refreshConnection()` can auto-select sessions.
- Workspace name can be read from daemon status.

**Missing behavior:**

- No workspace selector.
- No recent workspace registry.
- Session list is not a first-class UI rail outside Chat.
- Workspace path is not validated before dangerous actions.

**Target behavior:**

The app tracks:

```text
Workspace root
Workspace name
Recent sessions
Selected session
Last connected endpoint
Protocol version
```

**Implementation slices:**

1. Add `WorkspaceRegistry` persisted in Application Support.
2. Add workspace switcher in Settings and top navigation.
3. Add session rail presentation shared by Chat and Dashboard detail panels.
4. Add “create workbench session” flow that returns bootstrap payload.
5. Add stale selected-session clearing tests for every session switch path.

**Files:**

- Create: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/App/WorkspaceRegistry.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/App/AppState.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Daemon/DaemonController.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Chat/ChatView.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Settings/SettingsView.swift`
- Test: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/WorkspaceRegistryTests.swift`
- Test: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/DaemonControllerTests.swift`

**Acceptance criteria:**

- User can see which workspace and session are active.
- Switching sessions clears stale selected details and fetches new snapshot.
- Missing workspace produces a blocking error before worktree operations.

## M05. Snapshot Completeness and Empty States

**Status:** Partially Implemented.

**Current behavior:**

- Backend snapshot exists.
- Swift `WorkbenchSnapshotDTO` includes mission, agents, locks, decisions, tasks, issues, leases, failures, events, validation runs, approvals, worktrees, and context snapshots.
- Dashboard can render a fixture-rich snapshot.

**Missing behavior:**

- Some pages still supplement missing snapshot data with fixtures.
- Empty states are inconsistent.
- Snapshot freshness is not visible enough.

**Target behavior:**

Each page must have three states:

```text
No session selected
Session selected but no data
Session selected with real data
```

**Implementation slices:**

1. Add `SnapshotFreshnessPresentation`.
2. Add consistent empty states for:
   - no mission
   - no issues
   - no leases
   - no approvals
   - no validation runs
   - no events
3. Add “Refresh Snapshot” action in the inspector.
4. Add tests that empty backend arrays do not trigger fixture rows.

**Files:**

- Create: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/App/SnapshotFreshnessPresentation.swift`
- Modify: all page presentation files under `Features/*/*Presentation.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Localization/AppStrings.swift`
- Test: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/*PresentationTests.swift`

**Acceptance criteria:**

- Empty real data is visibly empty, not fake-filled.
- Snapshot refresh failures preserve old data and explain the error.
- User can tell when the snapshot was last refreshed.

## M06. Event Stream Reliability

**Status:** Partially Implemented.

**Current behavior:**

- `WorkbenchEventClient` exists.
- `DaemonController` has event stream lifecycle tests.
- Event stream health probes exist.

**Missing behavior:**

- UI does not make event stream state obvious.
- Reconnect policy is not presented to user.
- Event-stream lag is not shown.

**Target behavior:**

Event stream is the live heartbeat of the Workbench:

```text
Connected
Stale
Reconnecting
Stopped by session switch
Stopped by auth/protocol error
```

**Implementation slices:**

1. Add `EventStreamStatusPresentation`.
2. Show event-stream status in top bar or Timeline header.
3. Add reconnect with bounded backoff.
4. Add manual “重新连接事件流” action.
5. Add tests for stale stream not mutating new session state.

**Files:**

- Create: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Daemon/EventStreamStatusPresentation.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Daemon/WorkbenchRefreshCoordinator.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/App/WorkbenchShellView.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Timeline/TimelineView.swift`
- Test: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/DaemonControllerTests.swift`

**Acceptance criteria:**

- User sees when updates are live versus stale.
- Session switch stops old streams.
- Stream errors never clear unrelated selected-session state.

## M07. Dashboard Real-Data Mode

**Status:** Partially Implemented.

**Current behavior:**

- Dashboard exists and is visually aligned.
- Dashboard presentation maps snapshot state.
- Preview screenshots are rich.

**Missing behavior:**

- Some cards assume enough data exists.
- User actions from cards are not fully wired to detail loading.
- No strict real-mode screenshot audit.

**Target behavior:**

Dashboard answers:

1. 当前 Mission 是什么？
2. 哪些 Agent 在工作？
3. 哪些 Issue 阻塞、失败或待审批？
4. 哪些 worktree 需要处理？
5. 用户下一步应该介入哪里？

**Implementation slices:**

1. Remove real-mode fake fillers from Dashboard presentation.
2. Add all-card empty states.
3. Wire card clicks to:
   - load issue
   - load agent profile
   - load failure
   - load worktree
   - load event
4. Add inspector detail per selected item.
5. Add real snapshot screenshot test fixture with minimal data and no fake fillers.

**Files:**

- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Dashboard/DashboardSnapshotPresentation.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Dashboard/DashboardView.swift`
- Test: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/DashboardSnapshotPresentationTests.swift`
- Snapshot tool: `apps/macos/NaumiAgentWorkbench/Tools/NaumiAgentWorkbenchSnapshot/main.swift`

**Acceptance criteria:**

- Real empty session shows no fake issues.
- Clicking a Dashboard item loads the matching detail state.
- Dashboard can be captured at 1440x900 without overlapping UI.

## M08. Task Market Real-Data Mode

**Status:** Partially Implemented.

**Current behavior:**

- Issues and leases can be read from snapshot or refresh lists.
- Claim, release, and lease expiry APIs exist.
- `TaskMarketDesignPresentation` still injects fixture rows, fixture bids, and fixture leases.

**Missing backend concept:**

- Real agent bids do not appear to have a persisted model yet.
- Bid confidence, estimate, ETA, and note are currently design-only data.

**Target behavior:**

Task Market must show only real data in real mode:

```text
Issue queue
Risk
Parallel mode
Dependencies
Current lease
Related worktree
Real bids if bid model exists
Empty bid state if bid model does not exist
```

**Implementation slices:**

1. Add real-mode presentation policy to stop fixture row filling.
2. Add `workbench_bids` model and store methods, or explicitly show “暂无投标”.
3. Add backend routes:
   - `GET /workbench/sessions/{session_id}/issues/{task_id}/bids`
   - `POST /workbench/sessions/{session_id}/issues/{task_id}/bids`
4. Add Swift DTO:
   - `IssueBidDTO`
   - `IssueBidsDTO`
5. Add claim action flow from selected issue.
6. Add release and expire actions.
7. Add dependency status display using real TaskStore `blocked_by`.

**Files:**

- Modify: `src/naumi_agent/workbench/models.py`
- Modify: `src/naumi_agent/workbench/store.py`
- Modify: `src/naumi_agent/workbench/service.py`
- Modify: `src/naumi_agent/api/routes/workbench.py`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/API/WorkbenchAPIProviding.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/API/WorkbenchAPIClient.swift`
- Create: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/API/DTO/IssueBidDTO.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/TaskMarket/TaskMarketDesignPresentation.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/TaskMarket/TaskMarketView.swift`
- Test: `tests/unit/test_workbench_store.py`
- Test: `tests/unit/test_api_workbench.py`
- Test: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/TaskMarketDesignPresentationTests.swift`

**Acceptance criteria:**

- Real mode never shows fixture bids.
- A real bid appears after POST and survives snapshot refresh.
- Claim conflict returns visible owner and does not mutate local state incorrectly.

## M09. Chat and Issue Linkage

**Status:** Partially Implemented.

**Current behavior:**

- `sendMessage(sessionID:content:workbenchIssue:)` exists.
- Chat issue draft can create linked workbench issue through backend metadata.
- Preview chat messages are fixture-generated.

**Missing behavior:**

- Real chat history must be proven end-to-end with issue creation.
- Non-streaming issue creation path needs stronger UI state.
- Streaming chat with issue creation remains intentionally disabled or unsupported.

**Target behavior:**

User can chat naturally and optionally create an issue:

```text
Message only
Message + issue draft
Issue created -> Task Market refresh
Metadata contains workbench issue
Timeline includes issue.created
```

**Implementation slices:**

1. Add sending state and retry for chat composer.
2. Add issue draft validation:
   - title fallback from message first 36 Chinese characters or equivalent substring
   - acceptance criteria required for high-risk issue
3. Add real-mode empty history state.
4. Add tests for linked issue metadata and stale session guard.
5. Add backend API test for `stream=true` plus `workbenchIssue` returning 400.

**Files:**

- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Chat/ChatView.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Daemon/DaemonController.swift`
- Modify: `src/naumi_agent/api/routes/messages.py`
- Test: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/DaemonControllerTests.swift`
- Test: `tests/unit/test_api_messages.py`

**Acceptance criteria:**

- Sending a linked issue updates chat, issues, snapshot, and timeline.
- Failed send keeps composer content.
- Stale response after session switch is ignored.

## M10. Worktrees Real Operations

**Status:** Partially Implemented.

**Current behavior:**

- Worktree list/detail routes exist.
- Keep/remove worktree routes exist.
- Swift local action executor exists.

**Missing behavior:**

- Create/bind worktree from Mac app is not fully productized.
- Dirty file inspection is limited.
- Open in Finder/Terminal/Editor actions need real UX confirmation.

**Target behavior:**

Worktrees page becomes the local workspace operations console:

```text
Worktree list
Dirty count
Missing path
Open in Finder
Open terminal
Keep for review
Remove clean worktree
Force remove dirty worktree with explicit confirmation
```

**Implementation slices:**

1. Add create/bind worktree API if not present in backend route surface.
2. Add Swift command for create worktree.
3. Add dirty file detail endpoint or enrich existing worktree DTO.
4. Add safety confirmation for dirty remove.
5. Add missing-path and permission-denied states.

**Files:**

- Modify: `src/naumi_agent/api/routes/workbench.py`
- Modify: `src/naumi_agent/workbench/service.py`
- Modify: `src/naumi_agent/worktree.py` or existing worktree manager module
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Worktrees/WorktreesView.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Worktrees/WorktreeLocalActionExecutor.swift`
- Test: `tests/unit/test_api_workbench.py`
- Test: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/WorktreesDashboardPresentationTests.swift`

**Acceptance criteria:**

- Clean worktree can be removed after confirmation.
- Dirty worktree cannot be removed by default.
- Kept worktree displays kept reason.
- Missing worktree is visible and actionable.

## M11. Validation Runs and Failure Cards

**Status:** Partially Implemented.

**Current behavior:**

- Validation API and DTOs exist.
- Validation runner backend records pass/fail and failure cards.
- Reviews page and Dashboard can show validation and failure data.

**Missing behavior:**

- User-friendly validation command presets are incomplete.
- Failure remediation actions are not fully wired.
- Validation output detail needs better real-mode handling.

**Target behavior:**

Validation is a first-class operation:

```text
Choose issue
Choose preset command
Run validation
See live/pending state
See pass/fail result
Failure card appears with source validation run
User can rerun or assign follow-up
```

**Implementation slices:**

1. Add validation preset registry from backend capabilities or settings.
2. Add command allowlist display in Settings.
3. Add failure detail view with source run, output preview, and suggested actions.
4. Add retry validation action from Failure Card.
5. Add tests for validation failure creating visible failure state after snapshot refresh.

**Files:**

- Modify: `src/naumi_agent/workbench/validation.py`
- Modify: `src/naumi_agent/api/routes/workbench.py`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Reviews/ReviewsView.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Dashboard/DashboardView.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Settings/SettingsView.swift`
- Test: `tests/unit/test_workbench_validation.py`
- Test: `tests/unit/test_api_workbench.py`
- Test: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/ValidationRunPresentationTests.swift`

**Acceptance criteria:**

- Non-allowlisted command is rejected with Chinese error copy.
- Failed validation creates a Failure Card.
- Rerun updates validation runs and timeline.

## M12. Reviews, Diffs, and Human Approval

**Status:** Preview Only for diff/review details; partially implemented for approvals.

**Current behavior:**

- Approval list/detail and resolve APIs exist.
- Reviews page maps approvals and validations but still uses fixture files, diffs, timeline, and agent notes.

**Missing backend concepts:**

- Diff summary endpoint.
- Changed files list.
- Agent review notes.
- Approval evidence bundle.

**Target behavior:**

Reviews page uses real evidence:

```text
Approval card
Risk reason
Issue/worktree
Validation runs
Changed files
Diff summary
Agent notes
Timeline
Decision buttons
```

**Implementation slices:**

1. Add backend review evidence endpoint:
   - `GET /workbench/sessions/{session_id}/approvals/{approval_id}/evidence`
2. Define evidence DTO:
   - approval
   - issue
   - worktree
   - validation_runs
   - changed_files
   - diff_hunks
   - agent_notes
   - events
3. Implement diff collection from local git worktree.
4. Replace fixture files/diff/timeline/agent notes in real mode.
5. Add request-changes action semantics:
   - reopen issue or create follow-up issue
   - audit event
6. Add convert-to-proposal action semantics.

**Files:**

- Create: `src/naumi_agent/workbench/review_evidence.py`
- Modify: `src/naumi_agent/api/routes/workbench.py`
- Create: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/API/DTO/ReviewEvidenceDTO.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/API/WorkbenchAPIProviding.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/API/WorkbenchAPIClient.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Reviews/ReviewsDesignPresentation.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Reviews/ReviewsView.swift`
- Test: `tests/unit/test_api_workbench.py`
- Test: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/ReviewsDesignPresentationTests.swift`

**Acceptance criteria:**

- Reviews real mode shows no fixture diff rows.
- Approval evidence can be loaded for a real approval.
- Request changes writes an audit event and changes approval state.

## M13. Governance Policy, Intent Locks, and Decision Log

**Status:** Partially Implemented.

**Current behavior:**

- Intent lock create/list/detail APIs exist.
- Decision create/list/detail APIs exist.
- Settings page includes creation forms.

**Missing behavior:**

- Policy effects are not fully visible at the point of action.
- Decision strength and enforcement are not surfaced.
- Intent locks need editable/deactivate lifecycle.

**Target behavior:**

Settings and governance panels show:

```text
Active intent locks
Policy hit history
Decision log
Decision strength
Proposal-required thresholds
Who created the rule
Which issue/action was blocked
```

**Implementation slices:**

1. Add intent lock deactivate endpoint.
2. Add decision strength field if missing from model.
3. Add policy-hit audit event.
4. Add governance inspector in Settings.
5. Add read-before-action guard for high-risk operations.

**Files:**

- Modify: `src/naumi_agent/workbench/models.py`
- Modify: `src/naumi_agent/workbench/policy.py`
- Modify: `src/naumi_agent/workbench/store.py`
- Modify: `src/naumi_agent/api/routes/workbench.py`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Settings/SettingsView.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/API/DTO/IntentLockDTO.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/API/DTO/DecisionDTO.swift`
- Test: `tests/unit/test_workbench_policy.py`
- Test: `tests/unit/test_api_workbench.py`
- Test: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/AppStringsSettingsTests.swift`

**Acceptance criteria:**

- User can see why a task needs proposal mode.
- Deactivated intent locks no longer block actions.
- Decision log explains whether an entry is advisory, required, or blocking.

## M14. Agent Profiles, Activity, and Heartbeats

**Status:** Partially Implemented.

**Current behavior:**

- Agent profile list/detail/upsert APIs exist.
- Dashboard can render agent cards from snapshot.

**Missing behavior:**

- Real agent activity and heartbeat are not established.
- Busy/idle/inactive status is derived weakly.
- Permissions are not visible enough before actions.

**Target behavior:**

Agent state model:

```text
agent_id
role
capabilities
permissions
current_issue
current_lease
last_heartbeat_at
status: idle | busy | stale | offline
```

**Implementation slices:**

1. Add heartbeat endpoint:
   - `POST /workbench/sessions/{session_id}/agents/{agent_id}/heartbeat`
2. Add current activity derivation from active lease.
3. Add stale/offline thresholds in capabilities or settings.
4. Add agent detail inspector.
5. Add permission display and risk warnings.

**Files:**

- Modify: `src/naumi_agent/workbench/models.py`
- Modify: `src/naumi_agent/workbench/store.py`
- Modify: `src/naumi_agent/api/routes/workbench.py`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/API/DTO/AgentProfileDTO.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Dashboard/DashboardSnapshotPresentation.swift`
- Test: `tests/unit/test_workbench_store.py`
- Test: `tests/unit/test_api_workbench.py`
- Test: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/DashboardSnapshotPresentationTests.swift`

**Acceptance criteria:**

- Dashboard distinguishes busy, idle, stale, and offline agents.
- Current issue and lease are visible for busy agents.
- Missing heartbeat does not look like active work.

## M15. Timeline Replay and Causality

**Status:** Partially Implemented.

**Current behavior:**

- Event list/detail APIs exist.
- Timeline page exists.
- Preview timeline is enriched by generated preview events.

**Missing behavior:**

- Causal grouping is not fully modeled.
- Replay filters need real empty states and detail drill-down.
- Export/audit log workflow is missing.

**Target behavior:**

Timeline can answer:

```text
What happened?
Who did it?
Which issue/worktree/validation did it affect?
What came before and after?
What should the user inspect?
```

**Implementation slices:**

1. Add event correlation fields:
   - `correlation_id`
   - `parent_event_id`
   - `severity`
2. Add timeline grouping presentation.
3. Add event detail drawer.
4. Add filter chips:
   - actor
   - event type
   - subject
   - severity
5. Add audit export command.

**Files:**

- Modify: `src/naumi_agent/workbench/models.py`
- Modify: `src/naumi_agent/workbench/store.py`
- Modify: `src/naumi_agent/api/routes/workbench.py`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/API/DTO/EventDTO.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Timeline/TimelineDashboardPresentation.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Timeline/TimelineView.swift`
- Test: `tests/unit/test_workbench_store.py`
- Test: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/TimelineDashboardPresentationTests.swift`

**Acceptance criteria:**

- User can filter timeline to one issue and see all related events.
- Event detail shows raw payload safely.
- Audit export excludes secrets.

## M16. Proposal Mode and Next-Step Pool

**Status:** Missing as a first-class module.

**Current related behavior:**

- Intent locks can require proposal.
- Review page has a proposal conversion command name.
- Product docs define Proposal Mode.

**Missing backend concepts:**

- Proposal model.
- Proposal lifecycle.
- Next Step Pool.
- Proposal approval and conversion to issue.

**Target behavior:**

When direct execution is unsafe, Agent creates proposal:

```text
Proposal
  -> impact scope
  -> intended files
  -> validation plan
  -> risk
  -> questions for human
  -> approve / request changes / convert to issue
```

**Implementation slices:**

1. Add `WorkbenchProposal` model.
2. Add store and API routes:
   - list proposals
   - create proposal
   - resolve proposal
   - convert proposal to issue
3. Add Proposal panel in Dashboard or Reviews.
4. Add policy integration when intent lock blocks direct work.
5. Add audit events:
   - `proposal.created`
   - `proposal.approved`
   - `proposal.rejected`
   - `proposal.converted`

**Files:**

- Modify: `src/naumi_agent/workbench/models.py`
- Modify: `src/naumi_agent/workbench/store.py`
- Modify: `src/naumi_agent/workbench/service.py`
- Modify: `src/naumi_agent/api/routes/workbench.py`
- Create: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/API/DTO/ProposalDTO.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Reviews/ReviewsView.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Dashboard/DashboardView.swift`
- Test: `tests/unit/test_workbench_store.py`
- Test: `tests/unit/test_api_workbench.py`
- Test: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/ReviewsDesignPresentationTests.swift`

**Acceptance criteria:**

- High-risk or intent-locked work can produce a proposal instead of direct mutation.
- Proposal conversion creates a real issue with acceptance criteria.
- Human decision is logged.

## M17. Localization Completeness

**Status:** Partially Implemented.

**Current behavior:**

- `AppStrings` contains many Chinese/English strings.
- Preview mode supports Chinese and English.

**Missing behavior:**

- Some static design labels remain English-only in presentation models.
- Date/time formatting is inconsistent.
- Error copy needs full Chinese coverage.

**Target behavior:**

Default locale is Chinese; English fallback is complete:

```text
Navigation
Empty states
Errors
Buttons
Menus
Settings
Validation commands
Review actions
Daemon connection states
```

**Implementation slices:**

1. Audit hard-coded user-visible strings in `Features`.
2. Move all visible copy into `AppStrings`.
3. Add tests for every route and major action label in zh/en.
4. Add locale switch persistence.
5. Add date/time presentation helpers.

**Files:**

- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Localization/AppStrings.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Localization/AppLocale.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/**/*.swift`
- Test: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/AppStrings*Tests.swift`

**Acceptance criteria:**

- Default launch uses Chinese.
- All interactive controls have Chinese and English text.
- No user-facing hard-coded English remains in real-mode UI.

## M18. Development App Packaging

**Status:** Missing as a committed script.

**Current behavior:**

- `swift build` and `swift build -c release` pass.
- `scripts/run-preview.sh` builds a temporary preview `.app`.
- No reusable dev app packaging script exists.

**Target behavior:**

Create a local internal build artifact:

```text
dist/NaumiAgentWorkbench.app
dist/NaumiAgentWorkbench-dev.zip
```

This is not notarized and not for public distribution.

**Implementation slices:**

1. Add `scripts/package-dev-app.sh`.
2. Build release executable.
3. Create `.app` bundle with Info.plist.
4. Copy fixtures only when `--include-fixtures` is passed.
5. Add ad-hoc signing option for local launch.
6. Zip the app.
7. Add README instructions.

**Files:**

- Create: `apps/macos/NaumiAgentWorkbench/scripts/package-dev-app.sh`
- Modify: `apps/macos/NaumiAgentWorkbench/README.md`
- Modify: `apps/macos/NaumiAgentWorkbench/.gitignore`

**Acceptance criteria:**

- User can run one script and get a double-clickable `.app`.
- Release executable is used.
- Preview fixtures are optional and explicit.
- Generated artifacts are ignored by git.

**Verification:**

```bash
cd apps/macos/NaumiAgentWorkbench
./scripts/package-dev-app.sh --include-fixtures
open dist/NaumiAgentWorkbench.app
```

## M19. Security, Tokens, and Privacy

**Status:** Partially specified, not fully implemented.

**Current behavior:**

- Docs require localhost-only and token hygiene.
- API client supports configured base URL.

**Missing behavior:**

- Dev token storage is not productized.
- Keychain storage is not implemented.
- Audit redaction needs verification.

**Target behavior:**

Security boundaries:

```text
127.0.0.1 default only
No tokens in UI logs
No tokens in audit events
Keychain token storage for app-managed daemon
Dangerous actions require confirmation
Dirty worktree delete requires explicit force
```

**Implementation slices:**

1. Add token provider abstraction.
2. Add Keychain-backed token store.
3. Add audit-event redaction tests.
4. Add dangerous action confirmation models.
5. Add settings view for token rotation without displaying token value.

**Files:**

- Create: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Daemon/LocalAuthTokenStore.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/API/WorkbenchAPIClient.swift`
- Modify: `src/naumi_agent/workbench/store.py`
- Test: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/LocalAuthTokenStoreTests.swift`
- Test: `tests/unit/test_workbench_store.py`

**Acceptance criteria:**

- Token never appears in rendered UI.
- Token never appears in audit event payloads.
- Keychain failures produce clear user recovery text.

## M20. End-to-End Smoke and Release Gates

**Status:** Missing for the native Mac app loop.

**Current behavior:**

- Backend and terminal-ui tests exist.
- Swift presentation/controller tests exist.
- Screenshot generator exists.

**Missing behavior:**

- No native app E2E smoke from a real local daemon.
- No single release gate combining backend, Swift, screenshots, and packaging.

**Target smoke flow:**

```text
Start local daemon
Create session
Create mission
Create issue
Claim lease
Run validation
Record context health
Resolve approval or inspect failure
Refresh dashboard
Capture screenshots
Package dev app
```

**Implementation slices:**

1. Add Python E2E that drives Workbench API with a temp workspace.
2. Add Swift smoke that connects to the local daemon and fetches bootstrap.
3. Add screenshot smoke using real snapshot exported from the E2E.
4. Add `scripts/verify-dev-build.sh`.
5. Add CI-friendly target that skips notarization.

**Files:**

- Create: `tests/e2e/test_mac_workbench_local_loop.py`
- Create: `apps/macos/NaumiAgentWorkbench/scripts/verify-dev-build.sh`
- Modify: `apps/macos/NaumiAgentWorkbench/scripts/capture-preview-screens.sh`
- Modify: `docs/quality/mac-agent-workbench-acceptance.md`

**Acceptance criteria:**

- A real local flow creates visible real data for all major pages.
- The verification script fails if Swift build, targeted backend tests, screenshots, or packaging fail.
- The smoke flow can run without network access beyond localhost.

## M21. Internal Signed Build and Distribution

**Status:** Missing.

**Target behavior:**

After dev packaging is reliable:

```text
Developer ID signed app
Notarized zip or dmg
Versioned release notes
Protocol compatibility check
Migration warning
```

**Implementation slices:**

1. Add build configuration for bundle ID and version.
2. Add signing identity environment variables.
3. Add notarization script.
4. Add DMG or zip packaging.
5. Add compatibility check between app protocol version and daemon protocol version.
6. Add release checklist.

**Files:**

- Create: `apps/macos/NaumiAgentWorkbench/scripts/package-signed-app.sh`
- Create: `apps/macos/NaumiAgentWorkbench/scripts/notarize-app.sh`
- Create: `docs/release/mac-agent-workbench-release-checklist.md`
- Modify: `apps/macos/NaumiAgentWorkbench/README.md`

**Acceptance criteria:**

- Internal signed app launches on another Mac without build tools.
- App refuses unsafe operations against incompatible daemon protocol.
- Release checklist records test and notarization evidence.

## 6. Completion Gates by Phase

### Phase A: Real Data Boundary

Required modules:

- M00
- M04
- M05
- M06
- M07 partial without persisted bids if bid model is deferred
- M11 partial without diff endpoint if evidence endpoint is next

Gate:

```bash
apps/macos/NaumiAgentWorkbench/scripts/test.sh --filter Presentation
apps/macos/NaumiAgentWorkbench/scripts/test.sh --filter DaemonControllerTests
```

Manual check:

- Launch real mode against empty daemon.
- Confirm no fake fixture rows appear.

### Phase B: Real Local Product Loop

Required modules:

- M01
- M02
- M03
- M08
- M09
- M10
- M14
- M20 smoke without packaging.

Gate:

```bash
ruff check src/ tests/
pytest tests/unit/test_workbench_models.py tests/unit/test_workbench_store.py tests/unit/test_workbench_service.py tests/unit/test_api_workbench.py -q
apps/macos/NaumiAgentWorkbench/scripts/test.sh --filter DaemonControllerTests
cd apps/macos/NaumiAgentWorkbench && swift build -c release
```

Manual check:

- User can create mission, create issue, claim lease, run validation, and see Dashboard refresh.

### Phase C: Governance-Complete Internal MVP

Required modules:

- M12
- M13
- M15
- M16
- M17
- M18
- M19

Gate:

```bash
pytest tests/e2e/test_mac_workbench_local_loop.py -q
apps/macos/NaumiAgentWorkbench/scripts/verify-dev-build.sh
```

Manual check:

- High-risk change requires approval.
- Review page shows real evidence, not fixture diff rows.
- Dev `.app` launches from `dist/`.

### Phase D: Distributable Product

Required modules:

- M21
- Cloud sync remains excluded unless a separate product spec is approved.

Gate:

```bash
apps/macos/NaumiAgentWorkbench/scripts/package-signed-app.sh
apps/macos/NaumiAgentWorkbench/scripts/notarize-app.sh
```

Manual check:

- Install package on a clean Mac.
- Connect to local daemon.
- Run the Phase B smoke flow.

## 7. Self-Review

### Spec coverage

- Fake data removal is covered by M00, M05, M07, M11, and Phase A.
- Real daemon connection is covered by M01, M02, and M03.
- Dashboard, Task Market, Worktrees, Reviews, Timeline, Settings, and Chat each have module coverage.
- Human governance workflows are covered by M12, M13, M15, and M19.
- Packaging and distribution are covered by M18 and M21.
- End-to-end smoke is covered by M20.

### Known intentional boundaries

- Cloud sync remains excluded.
- Public notarized distribution waits until dev packaging and real local smoke are reliable.
- Bundled Python runtime is not part of the next internal MVP; app-managed local daemon uses the existing local Python/uv environment.

### No-placeholder check

This document intentionally avoids open placeholders. Each module has target behavior, implementation slices, files, acceptance criteria, and verification or phase gates.
