# Terminal User Message Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render a user message immediately when it is submitted, reconcile it with Bridge acknowledgement without duplication, expose actionable delivery failures, and preserve ambiguous or failed outbox entries across UI restart without silently resending them.

**Architecture:** Keep the existing Bridge v1 envelope and use its client `id` / server `request_id` correlation. The canonical timeline message doubles as the local outbox entry, so acceptance mutates one object instead of appending an echoed duplicate. Only non-final delivery records enter the presentation snapshot; restored queued entries become `uncertain` until replay evidence or explicit user retry resolves them.

**Tech Stack:** Node.js 20 ESM, existing JSONL Bridge v1, mutable Terminal UI state/reducer, atomic UI state store, `node:test`, Python `pytest` Bridge contract tests.

## Global Constraints

- A non-command submit creates exactly one local user message before writing to Bridge stdin.
- Delivery states are `queued`, `accepted`, `failed`, and `uncertain`; accepted messages are backend-authoritative and are not stored in the local outbox.
- `user/message`, submit `ack`, or matching `run/started` accepts the local message by `request_id`.
- A matching Bridge `error` fails only a still-unconfirmed message; an error after acceptance describes run failure and must not rewrite delivery status.
- `/retry [request_id]` mutates the failed/uncertain message in place with a new request ID and incremented attempt count.
- Restart never automatically resubmits queued work. Restored queued entries become `uncertain` with an explicit duplicate-send warning.
- Replay user content may reconcile one matching uncertain outbox entry; identical later messages remain independent.
- Bridge stdin error or process exit terminates all queued messages with actionable Chinese copy.
- User content is right-aligned or right-indented and delivery state is communicated by text, not color alone.
- Existing slash commands remain command events and never create user chat bubbles.
- No Bridge protocol version bump and no engine execution changes are included.
- Run focused tests per task; run the complete Node suite only at the slice release gate.

---

### Task 1: Correlated local outbox state

**Files:**
- Modify: `frontend/terminal-ui/src/protocol.js`
- Modify: `frontend/terminal-ui/src/state.js`
- Test: `frontend/terminal-ui/test/protocol.test.js`
- Test: `frontend/terminal-ui/test/state.test.js`

**Interfaces:**
- Extends `send(type, payload, options = {})` with optional `options.id` while preserving generated `ui-N` IDs.
- Produces: `submitUserMessage(state, text, send, existingMessage = null) -> userMessage`.
- Produces: `acceptUserMessage(state, requestId, content = "") -> userMessage | null`.
- Produces: `failUserMessage(state, requestId, error) -> userMessage | null`.
- Produces: `failQueuedUserMessages(state, error) -> number`.
- User message fields: `id`, `requestId`, `content`, `deliveryStatus`, `attempt`, `errorCode`, `errorMessage`, `localOutbox`.

- [ ] **Step 1: Write failing protocol and reducer tests**

```javascript
test("event sender accepts a caller supplied request id", () => {
  const chunks = [];
  const send = createEventSender({ write: (chunk) => chunks.push(chunk) });
  assert.equal(send("submit", { text: "修复测试" }, { id: "submit-local-1" }), "submit-local-1");
  assert.equal(JSON.parse(chunks[0]).id, "submit-local-1");
  assert.equal(send("ping", {}), "ui-1");
});

test("submit queues one local message and echo accepts it without duplication", () => {
  const state = createInitialState();
  const send = (type, payload, options) => options.id;
  handleSubmitText(state, "修复测试", send);
  const pending = state.messages.at(-1);
  assert.equal(pending.deliveryStatus, "queued");

  reduceServerEvent(state, {
    type: "user/message",
    request_id: pending.requestId,
    payload: { content: "修复测试" },
  });
  assert.equal(state.messages.filter((message) => message.kind === "user").length, 1);
  assert.equal(pending.deliveryStatus, "accepted");
});
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `cd frontend/terminal-ui && node --test --test-name-pattern="caller supplied request id|queues one local message" test/protocol.test.js test/state.test.js`

Expected: FAIL because sender overrides and local outbox transitions are absent.

- [ ] **Step 3: Add optional sender IDs without changing default IDs**

```javascript
export function createEventSender(writable, { debugLog = null } = {}) {
  let nextClientId = 1;
  return function send(type, payload, options = {}) {
    const id = String(options.id || `ui-${nextClientId++}`);
    // Existing validation, JSONL write, debug logging, and return value remain unchanged.
  };
}
```

- [ ] **Step 4: Implement local submit and reducer reconciliation**

Generate a process-local request ID before sending, append the queued message, then call `send(..., { id: requestId })` in `try/catch`. A synchronous write exception changes that same message to `failed` and retains it for retry. `user/message`, submit `ack`, and `run/started` call `acceptUserMessage()`. A matching `error` calls `failUserMessage()` before adding the existing system error card.

For unmatched `user/message`, append an accepted message with a stable local ID. For replay `ui/message` type `user`, first reconcile one uncertain message with equal content, otherwise append an accepted historical message.

- [ ] **Step 5: Cover failure boundaries**

Add tests for:

- `run_in_progress` fails a queued message.
- run error after `user/message` leaves delivery accepted.
- synchronous sender throw produces one failed bubble.
- `failQueuedUserMessages()` fails every queued message but leaves accepted messages unchanged.
- an unmatched backend user message still renders once.

- [ ] **Step 6: Run focused tests and commit**

Run: `cd frontend/terminal-ui && node --test test/protocol.test.js test/state.test.js`

```bash
git add frontend/terminal-ui/src/protocol.js frontend/terminal-ui/src/state.js frontend/terminal-ui/test/protocol.test.js frontend/terminal-ui/test/state.test.js
git commit -m "feat: track terminal message delivery"
```

---

### Task 2: Delivery rendering and explicit retry

**Files:**
- Modify: `frontend/terminal-ui/src/state.js`
- Modify: `frontend/terminal-ui/src/components/message.js`
- Modify: `frontend/terminal-ui/src/index.js`
- Test: `frontend/terminal-ui/test/components.test.js`
- Test: `frontend/terminal-ui/test/state.test.js`
- Test: `frontend/terminal-ui/test/index-process.test.js`

**Interfaces:**
- Adds local `/retry [request_id]` command and slash-completion entry.
- Produces: `retryUserMessage(state, send, requestId = "") -> userMessage | null`.
- Produces a right-indented user renderer with visible queued, failed, and uncertain labels.

- [ ] **Step 1: Write failing render and retry tests**

```javascript
test("user delivery card is right indented and exposes failure recovery", () => {
  const lines = renderComponent(Message({ message: {
    kind: "user",
    content: "请修复测试",
    deliveryStatus: "failed",
    errorMessage: "Bridge 已断开",
  } }), { width: 80 }).map(stripAnsi);
  assert(lines.some((line) => line.startsWith(" ".repeat(20)) && line.includes("请修复测试")));
  assert.match(lines.join("\n"), /发送失败.*\/retry/);
});

test("retry reuses one bubble with a new request id", () => {
  const state = failedState();
  handleSubmitText(state, "/retry", send);
  assert.equal(state.messages.filter((message) => message.kind === "user").length, 1);
  assert.equal(state.messages[0].deliveryStatus, "queued");
  assert.equal(state.messages[0].attempt, 2);
  assert.notEqual(state.messages[0].requestId, "old-request");
});
```

- [ ] **Step 2: Run selected tests and confirm RED**

Run: `cd frontend/terminal-ui && node --test --test-name-pattern="delivery card|retry reuses|delivery lifecycle" test/components.test.js test/state.test.js test/index-process.test.js`

- [ ] **Step 3: Implement semantic user rendering**

Wrap each logical content line within at most 72% of terminal width, right-indent the resulting block, and add a textual status row:

- queued: `发送中...`
- failed: `发送失败: <safe message> · /retry 重试`
- uncertain: `发送状态待确认 · /retry 可能重复发送`
- accepted: no noisy status row

The role marker `你` remains visible even without color.

- [ ] **Step 4: Implement retry in place**

`/retry` targets the latest failed/uncertain user message; `/retry <request_id>` selects an explicit one. It allocates a new request ID, clears the old error, increments `attempt`, marks queued, and submits the same content. If no eligible message exists, add a Chinese warning system notice and do not send.

- [ ] **Step 5: Terminate pending delivery on transport failure**

Attach `bridge.stdin.on("error")` and reuse the bridge exit handler. Mark queued messages failed before rendering the connection error. Do not alter accepted messages or retry automatically.

- [ ] **Step 6: Add a spawned-process lifecycle test**

Use a fixture that delays the `user/message` response so the process test can observe `发送中...`, then accepts the same request ID and proves only one copy of the content remains in render/debug state. A second fixture response rejects a submit with `run_in_progress`; `/retry` sends a new request ID and returns the same bubble to queued.

- [ ] **Step 7: Run focused tests and commit**

Run: `cd frontend/terminal-ui && node --test test/components.test.js test/state.test.js test/index-process.test.js`

```bash
git add frontend/terminal-ui/src/state.js frontend/terminal-ui/src/components/message.js frontend/terminal-ui/src/index.js frontend/terminal-ui/test/components.test.js frontend/terminal-ui/test/state.test.js frontend/terminal-ui/test/index-process.test.js frontend/terminal-ui/test/fixtures/message-lifecycle-bridge.js
git commit -m "feat: retry failed terminal messages"
```

---

### Task 3: Crash-safe outbox snapshot

**Files:**
- Modify: `frontend/terminal-ui/src/state.js`
- Test: `frontend/terminal-ui/test/state.test.js`
- Test: `frontend/terminal-ui/test/index-process.test.js`

**Interfaces:**
- Extends the existing optional UI snapshot with `outbox: UserDeliverySnapshot[]` without changing store version 2.
- Snapshot fields are restricted to request ID, bounded content, delivery state, attempt, and safe error code/message.
- `applyUiSnapshot()` restores queued as uncertain and failed as failed; it never restores accepted messages.

- [ ] **Step 1: Write failing snapshot tests**

```javascript
test("outbox snapshot restores queued delivery as uncertain without auto send", () => {
  const source = queuedState();
  const snapshot = createUiSnapshot(source);
  const restored = createInitialState();
  applyUiSnapshot(restored, snapshot);
  const message = restored.messages.find((item) => item.localOutbox);
  assert.equal(message.deliveryStatus, "uncertain");
  assert.equal(message.content, source.messages[0].content);
});
```

Also prove accepted messages are excluded, malformed entries are ignored, content/array bounds apply, and applying a snapshot twice does not duplicate local outbox entries.

- [ ] **Step 2: Run snapshot tests and confirm RED**

Run: `cd frontend/terminal-ui && node --test --test-name-pattern="outbox snapshot|restores queued delivery" test/state.test.js test/index-process.test.js`

- [ ] **Step 3: Implement bounded outbox serialization and restore**

Store at most 20 failed/queued/uncertain messages and at most 200,000 code points per message. Before restore, remove prior `localOutbox` entries from the state. Restored queued entries become uncertain with a safe recovery message. Preserve accepted server/replay messages already in the timeline.

- [ ] **Step 4: Add restart process proof**

Stop the Terminal UI while a fixture leaves one request queued, restart against the same UI state file, verify the message appears once as `发送状态待确认`, and prove no `submit` is emitted until `/retry` is entered.

- [ ] **Step 5: Run focused tests and commit**

Run: `cd frontend/terminal-ui && node --test test/state.test.js test/ui-state-store.test.js test/index-process.test.js`

```bash
git add frontend/terminal-ui/src/state.js frontend/terminal-ui/test/state.test.js frontend/terminal-ui/test/index-process.test.js
git commit -m "feat: recover terminal message outbox"
```

---

### Task 4: Bridge correlation evidence, documentation, and release gate

**Files:**
- Modify: `tests/unit/test_ui_bridge.py`
- Modify: `docs/product/terminal-ui/02-conversation-timeline-and-composer.md`
- Modify: `docs/product/terminal-ui/README.md`

**Interfaces:**
- Records Bridge v1 `request_id` correlation as the compatibility contract for this slice.
- Keeps Bridge v2 idempotent reconnect, sequence-gap recovery, and multi-client outbox reconciliation explicitly pending.

- [ ] **Step 1: Strengthen Python Bridge request-correlation tests**

Assert `user/message`, `run/started`, `run/completed`, and pre-acceptance `error` preserve the original submit request ID. Cover `run_in_progress` rejection without a user-message echo.

- [ ] **Step 2: Run Bridge and protocol gates**

Run: `.venv/bin/python -m pytest -q tests/unit/test_ui_bridge.py`

Run: `cd frontend/terminal-ui && node --test test/protocol.test.js`

- [ ] **Step 3: Run real process scenarios**

Run the real Node frontend with the real `.venv` Python Bridge and execute a non-model `/doctor` smoke path to prove startup/shutdown remain healthy. Use the spawned lifecycle fixture for observable queued/accepted/failed/retry timing because the real Bridge accepts ordinary submits immediately before external model execution.

- [ ] **Step 4: Run the complete frontend release gate**

Run: `cd frontend/terminal-ui && node scripts/check-syntax.js`

Run: `cd frontend/terminal-ui && node scripts/run-tests.js`

Expected: all JavaScript syntax checks and Node tests pass with zero failures.

- [ ] **Step 5: Update docs honestly**

Mark optimistic local user messages, request correlation, duplicate suppression, explicit retry, right-indented rendering, and crash-safe uncertain outbox recovery complete. Keep `Ctrl+R` history search, chat/task mode, Bridge v2 idempotency, and full reconnect reconciliation pending.

- [ ] **Step 6: Verify, commit, push, and merge**

Run: `git diff --check`

```bash
git add tests/unit/test_ui_bridge.py docs/product/terminal-ui/02-conversation-timeline-and-composer.md docs/product/terminal-ui/README.md
git commit -m "docs: record terminal delivery lifecycle"
git push -u origin codex/terminal-send-lifecycle
```

After post-merge Node verification, push `main` and delete only the local feature branch. Preserve the user-owned untracked `.superpowers/` directory.

## Self-Review

- **Spec coverage:** Covers immediate local feedback, accepted/failed reconciliation, duplicate suppression, retry, right-indented user messages, and crash ambiguity from M2 sections 2, 5, 7, 8, and 9.
- **No prompt shell:** Delivery is driven by concrete protocol IDs, reducer state, stream failure handling, atomic snapshot persistence, and process tests.
- **Compatibility:** Reuses Bridge v1 envelopes and leaves default generated event IDs unchanged; no Python production code is required.
- **Safety:** Restored queued work is never auto-executed. Explicit retry warns about possible duplication when delivery is uncertain.
- **Explicit exclusions:** Durable request idempotency across a surviving Bridge, sequence replay, multi-client outbox reconciliation, history search, and chat/task linkage remain later protocol and M2 slices.
