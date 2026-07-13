# Terminal Follow Tail Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep live Agent output visible while the user follows the timeline, preserve the reading position after manual scroll and terminal resize, and expose deduplicated unread-output feedback with an explicit return-to-latest action.

**Architecture:** Add a small pure timeline-follow state module consumed by the Node shell. Server records map to stable semantic output keys so streaming tokens update one unread item instead of inflating the count. The renderer exposes message-level viewport anchors used only during resize; normal rendering keeps the existing tail-window fast path.

**Tech Stack:** Node.js 20 ESM, existing Terminal UI state/components/render cache, `node:test`.

## Global Constraints

- `followTail=true` forces `scrollOffset=0` after timeline output.
- Any upward user scroll sets `followTail=false` before the next event is reduced.
- While detached, output never changes `scrollOffset` and each semantic entry contributes at most one unread item.
- `PageDown`, alternate Down, `End` on an empty composer, and `Ctrl+L` re-enable follow mode when they reach the tail.
- The unread prompt is visible outside the scrollable body and uses Chinese copy by default.
- Resize anchoring preserves the top visible message, not an obsolete absolute line offset.
- No Bridge or Python protocol changes are included.
- Run focused Node tests per task and the complete Node suite only at the slice release gate.

---

### Task 1: Follow state and semantic unread keys

**Files:**
- Create: `frontend/terminal-ui/src/timeline-follow.js`
- Modify: `frontend/terminal-ui/src/state.js`
- Test: `frontend/terminal-ui/test/timeline-follow.test.js`

**Interfaces:**
- Produces: `initializeTimelineFollow(state) -> void`.
- Produces: `markTimelineOutput(state, record, entryId = "") -> boolean`.
- Produces: `detachTimeline(state, scrollOffset) -> void`.
- Produces: `jumpTimelineToLatest(state) -> void`.
- Produces: `scrollTimeline(state, delta) -> void`.
- Produces: `timelineOutputKey(record, entryId) -> string | null`.

- [ ] **Step 1: Write failing state-machine tests**

```javascript
test("following output stays pinned without unread items", () => {
  const state = createInitialState();
  state.scrollOffset = 9;
  assert.equal(markTimelineOutput(state, assistantToken(1), "assistant-1"), true);
  assert.equal(state.scrollOffset, 0);
  assert.equal(state.unreadOutputCount, 0);
});

test("detached streaming tokens count one semantic assistant entry", () => {
  const state = createInitialState();
  detachTimeline(state, 12);
  markTimelineOutput(state, assistantToken(1), "assistant-1");
  markTimelineOutput(state, assistantToken(2), "assistant-1");
  markTimelineOutput(state, toolResult(3, "call-1"), "call-1");
  assert.equal(state.scrollOffset, 12);
  assert.equal(state.unreadOutputCount, 2);
});
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `cd frontend/terminal-ui && node --test test/timeline-follow.test.js`

Expected: FAIL because `timeline-follow.js` does not exist.

- [ ] **Step 3: Implement pure follow transitions**

```javascript
export function initializeTimelineFollow(state) {
  state.followTail = true;
  state.unreadOutputCount = 0;
  state.unreadOutputKeys = {};
}

export function markTimelineOutput(state, record, entryId = "") {
  const key = timelineOutputKey(record, entryId);
  if (!key) return false;
  if (state.followTail) {
    state.scrollOffset = 0;
    state.unreadOutputCount = 0;
    state.unreadOutputKeys = {};
    return true;
  }
  if (!state.unreadOutputKeys[key]) {
    state.unreadOutputKeys[key] = true;
    state.unreadOutputCount += 1;
  }
  return true;
}

export function detachTimeline(state, scrollOffset) {
  state.followTail = false;
  state.scrollOffset = Math.max(1, Number(scrollOffset) || 1);
}

export function jumpTimelineToLatest(state) {
  state.followTail = true;
  state.scrollOffset = 0;
  state.unreadOutputCount = 0;
  state.unreadOutputKeys = {};
}
```

Implement `timelineOutputKey()` with explicit event filtering and stable identities:

```javascript
export function timelineOutputKey(record, entryId = "") {
  const payload = record?.payload ?? {};
  const sequence = record?.seq ?? entryId;
  if (record?.type === "user/message") return sequence ? `user:${sequence}` : null;
  if (record?.type === "error") return sequence ? `error:${sequence}` : null;
  if (record?.type !== "ui/message") return null;

  if (payload.type === "assistant_stream") {
    return entryId ? `assistant:${entryId}` : null;
  }
  if (["tool_prepare", "tool_use", "tool_result"].includes(payload.type)) {
    const toolId = payload.tool_call_id || entryId;
    return toolId ? `tool:${toolId}` : null;
  }
  if (payload.type === "thinking") {
    return entryId ? `thinking:${entryId}` : null;
  }
  if (payload.type === "permission_bubble") {
    const requestId = payload.request_id || entryId;
    return requestId ? `permission:${requestId}` : null;
  }
  if (["recovery", "context_compact", "runtime_notification", "subagent_event", "team_event", "hook_trace", "error", "system_notice"].includes(payload.type)) {
    return sequence ? `${payload.type}:${sequence}` : null;
  }
  return null;
}
```

Runtime/status, todo-only footer updates, run lifecycle records, workbench snapshots, debug traces, and permission resolution acknowledgements return `null`.

- [ ] **Step 4: Initialize the fields in `createInitialState()`**

Use `followTail: true`, `unreadOutputCount: 0`, and `unreadOutputKeys: {}`. Session replay and a newly submitted user request call `jumpTimelineToLatest()`.

- [ ] **Step 5: Run tests and confirm GREEN**

Run: `cd frontend/terminal-ui && node --test test/timeline-follow.test.js test/state.test.js`

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add frontend/terminal-ui/src/timeline-follow.js frontend/terminal-ui/src/state.js frontend/terminal-ui/test/timeline-follow.test.js
git commit -m "feat: model terminal timeline follow state"
```

---

### Task 2: Shell integration and unread output control

**Files:**
- Modify: `frontend/terminal-ui/src/index.js`
- Modify: `frontend/terminal-ui/src/components/footer.js`
- Test: `frontend/terminal-ui/test/components.test.js`
- Test: `frontend/terminal-ui/test/index-process.test.js`

**Interfaces:**
- Consumes: pure functions from `timeline-follow.js`.
- Produces: `NewOutputFooter({ state })`.
- Produces: stable entry identity selection after each reduced server record.

- [ ] **Step 1: Write failing component and process tests**

```javascript
test("new output footer appears only while detached with unread output", () => {
  const state = createInitialState();
  detachTimeline(state, 10);
  markTimelineOutput(state, assistantToken(1), "assistant-1");
  const text = renderComponent(NewOutputFooter({ state }), { width: 80 }).join("\n");
  assert.match(stripAnsi(text), /有 1 条新输出/);
  jumpTimelineToLatest(state);
  assert.deepEqual(renderComponent(NewOutputFooter({ state }), { width: 80 }), []);
});
```

The spawned-process test submits a streaming request, sends PageUp while output is active, verifies `scroll_offset` remains non-zero across later `ui/message` records, sees “有 1 条新输出”, sends `Ctrl+L`, and verifies the prompt disappears and `scroll_offset` returns to zero.

- [ ] **Step 2: Run selected tests and confirm RED**

Run: `cd frontend/terminal-ui && node --test --test-name-pattern="new output|follow tail" test/components.test.js test/index-process.test.js`

Expected: FAIL because shell integration and `NewOutputFooter` are absent.

- [ ] **Step 3: Mark reduced records with stable entry IDs**

After `reduceServerEvent()` completes, derive an entry ID as follows:

```javascript
function timelineEntryId(record) {
  const message = record.payload ?? {};
  if (record.type === "ui/message" && message.type === "assistant_stream") {
    return state.activeAssistant?.id
      || [...state.messages].reverse().find((item) => item.kind === "assistant")?.id
      || `assistant:${record.seq}`;
  }
  if (record.type === "ui/message" && ["tool_use", "tool_result", "tool_prepare"].includes(message.type)) {
    return message.tool_call_id || `tool:${record.seq}`;
  }
  return record.request_id || record.seq || "";
}
```

Call `markTimelineOutput(state, record, timelineEntryId(record))` before scheduling redraw.

- [ ] **Step 4: Route scroll keys through the follow state**

PageUp and alternate Up call `scrollTimeline(state, +pageSize)`. PageDown and alternate Down call `scrollTimeline(state, -pageSize)`. `Ctrl+L` always calls `jumpTimelineToLatest()`. `End` calls it only when the composer is empty; otherwise End retains current-line editor behavior. Successful submit calls it before Bridge submission.

- [ ] **Step 5: Add a fixed unread footer**

```javascript
export function NewOutputFooter({ state }) {
  return {
    render(ctx) {
      if (state.followTail || state.unreadOutputCount <= 0) return [];
      return wrapAnsiLine(
        color(ANSI.cyan, `↓ 有 ${state.unreadOutputCount} 条新输出 · End/Ctrl+L 跳到最新`),
        ctx.width,
      );
    },
  };
}
```

Insert the section immediately before status and prompt so tiny-terminal clamping still preserves the composer.

- [ ] **Step 6: Run selected tests and confirm GREEN**

Run: `cd frontend/terminal-ui && node --test test/components.test.js test/index-process.test.js`

Expected: all selected tests pass.

- [ ] **Step 7: Commit**

```bash
git add frontend/terminal-ui/src/index.js frontend/terminal-ui/src/components/footer.js frontend/terminal-ui/test/components.test.js frontend/terminal-ui/test/index-process.test.js
git commit -m "feat: control terminal timeline following"
```

---

### Task 3: Message-level resize anchors

**Files:**
- Modify: `frontend/terminal-ui/src/render.js`
- Modify: `frontend/terminal-ui/src/index.js`
- Test: `frontend/terminal-ui/test/render.test.js`
- Test: `frontend/terminal-ui/test/index-process.test.js`

**Interfaces:**
- Produces: `captureViewportAnchor(state, width, height) -> { messageId: string, messageIndex: number } | null`.
- Produces: `restoreViewportAnchor(state, anchor, width, height) -> number` returning the new `scrollOffset`.

- [ ] **Step 1: Write failing resize anchor tests**

Create three long uniquely identified messages. Detach the viewport at width 120, capture the top visible message, restore at width 60, and assert a second capture returns the same message ID. Add a render integration test proving follow mode stays pinned across the same width change.

- [ ] **Step 2: Run tests and confirm RED**

Run: `cd frontend/terminal-ui && node --test --test-name-pattern="resize anchor" test/render.test.js`

Expected: FAIL because anchor functions do not exist.

- [ ] **Step 3: Implement anchor capture and restoration**

On resize only, render each message as a separate segment using the existing render cache. Calculate the current first visible body line from total rendered lines, footer height, body height, and `scrollOffset`. Capture the segment's stable message ID (falling back to its index). At the new width, find that segment's first line and set:

```javascript
const nextOffset = Math.max(
  0,
  totalBodyLines - nextBodyHeight - anchorSegmentStart,
);
```

Clamp to the total available history. Do not run this full-history path during ordinary streaming redraws.

- [ ] **Step 4: Integrate resize handling**

Track the last rendered width/height. Before a detached resize redraw, capture with the previous dimensions, restore with current dimensions, and write `viewport.resize_anchor` to the debug log. In follow mode, skip anchoring and keep `scrollOffset=0`.

- [ ] **Step 5: Run selected tests and confirm GREEN**

Run: `cd frontend/terminal-ui && node --test test/render.test.js test/index-process.test.js`

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add frontend/terminal-ui/src/render.js frontend/terminal-ui/src/index.js frontend/terminal-ui/test/render.test.js frontend/terminal-ui/test/index-process.test.js
git commit -m "feat: preserve timeline anchor on resize"
```

---

### Task 4: Persistence, documentation, and release gate

**Files:**
- Modify: `frontend/terminal-ui/src/state.js`
- Modify: `docs/product/terminal-ui/02-conversation-timeline-and-composer.md`
- Modify: `docs/product/terminal-ui/README.md`
- Test: `frontend/terminal-ui/test/state.test.js`

**Interfaces:**
- UI snapshot keeps `scrollOffset`; `applyUiSnapshot()` derives `followTail = scrollOffset === 0` and always resets unread keys/count because unread output is process-local evidence.

- [ ] **Step 1: Add and run snapshot tests**

```javascript
test("follow tail snapshot derives detached state without stale unread", () => {
  const source = createInitialState();
  source.scrollOffset = 7;
  source.followTail = false;
  source.unreadOutputCount = 3;
  source.unreadOutputKeys = { "assistant:old": true };

  const restored = createInitialState();
  applyUiSnapshot(restored, createUiSnapshot(source));
  assert.equal(restored.scrollOffset, 7);
  assert.equal(restored.followTail, false);
  assert.equal(restored.unreadOutputCount, 0);
  assert.deepEqual(restored.unreadOutputKeys, {});

  source.scrollOffset = 0;
  applyUiSnapshot(restored, createUiSnapshot(source));
  assert.equal(restored.followTail, true);
});
```

Run: `cd frontend/terminal-ui && node --test --test-name-pattern="follow tail snapshot" test/state.test.js`

Expected after implementation: a restored positive scroll offset is detached with zero unread items; offset zero restores follow mode.

- [ ] **Step 2: Run the complete frontend gate**

Run: `cd frontend/terminal-ui && node scripts/check-syntax.js`

Run: `cd frontend/terminal-ui && node scripts/run-tests.js`

Expected: all syntax and Node tests pass with zero failures.

- [ ] **Step 3: Run Bridge regression**

Run: `.venv/bin/python -m pytest -q tests/unit/test_ui_bridge.py`

Expected: all Python Bridge tests pass.

- [ ] **Step 4: Update status docs honestly**

Mark follow mode, unread feedback, jump-to-latest, and resize message anchors complete. Keep optimistic send lifecycle, history search, and chat/task linkage pending.

- [ ] **Step 5: Verify, commit, and push**

Run: `git diff --check`

```bash
git add frontend/terminal-ui/src/state.js frontend/terminal-ui/test/state.test.js docs/product/terminal-ui/02-conversation-timeline-and-composer.md docs/product/terminal-ui/README.md
git commit -m "docs: record terminal follow tail delivery"
git push -u origin codex/terminal-follow-tail
```

## Self-Review

- **Spec coverage:** Covers follow state, manual detach, unread feedback, jump-to-latest, session snapshot semantics, and resize message anchoring from M2 sections 3, 7, 8, and 9.
- **Performance:** Full-history rendering is restricted to resize while detached; streaming updates use O(1) semantic keys.
- **Event fidelity:** Runtime/status and footer-only updates do not create false unread output.
- **Explicit exclusions:** Optimistic send acknowledgement, history search, and chat/task linkage remain later M2 slices.
