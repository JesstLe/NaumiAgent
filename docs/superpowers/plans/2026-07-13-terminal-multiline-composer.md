# Terminal Multiline Composer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the first independently usable M2 slice: a Unicode-safe multiline terminal composer with explicit submit keys, atomic bracketed paste, responsive rendering, and session-scoped draft recovery.

**Architecture:** Extend the existing `input-buffer.js` state operations instead of creating a second editor. Add a stateful input tokenizer dedicated to terminal framing, keep editing functions pure over the shared frontend state, and persist only presentation data through the existing versioned UI snapshot store. `index.js` remains the integration boundary for key precedence and Bridge submission.

**Tech Stack:** Node.js 20 ESM, ANSI/CSI terminal input, existing zero-dependency Terminal UI component model, `node:test`.

## Global Constraints

- Chinese remains the default user-visible language.
- `Enter` submits, `Shift+Enter` inserts a newline, and `Ctrl+Enter` submits multiline input.
- Bracketed paste (`CSI 200~` through `CSI 201~`) is one atomic edit even when delimiters or content span input chunks.
- Cursor offsets use Unicode code points and never index UTF-16 code units.
- Drafts are keyed by the existing session snapshot key and survive page redraw, session replay, and normal process restart.
- Existing permission and task-panel key precedence remains unchanged.
- No backend protocol change is included in this slice.
- Run focused Node tests for each task; run the complete Node suite only after the slice is complete.

---

### Task 1: Multiline editor primitives

**Files:**
- Modify: `frontend/terminal-ui/src/input-buffer.js`
- Modify: `frontend/terminal-ui/src/state.js`
- Test: `frontend/terminal-ui/test/input-buffer.test.js`

**Interfaces:**
- Consumes: `state.input: string` and `state.inputCursor: number | null`.
- Produces: `insertInputNewline(state) -> void`.
- Produces: `moveInputCursorVertical(state, direction: "up" | "down") -> boolean`.
- Produces: `moveInputCursorToLineBoundary(state, boundary: "start" | "end") -> void`.
- Produces: `getInputCursorLocation(state) -> { line: number, column: number }`.

- [ ] **Step 1: Write failing tests for newline insertion and vertical movement**

```javascript
test("multiline input inserts newline at the unicode cursor", () => {
  const state = createInitialState();
  setInputText(state, "第一行第二行", 3);
  insertInputNewline(state);
  assert.equal(state.input, "第一\n行第二行");
  assert.equal(getInputCursor(state), 4);
});

test("vertical cursor movement preserves the preferred visual column", () => {
  const state = createInitialState();
  setInputText(state, "abcd\n你我\n12345", 3);
  assert.equal(moveInputCursorVertical(state, "down"), true);
  assert.deepEqual(getInputCursorLocation(state), { line: 1, column: 2 });
  assert.equal(moveInputCursorVertical(state, "down"), true);
  assert.deepEqual(getInputCursorLocation(state), { line: 2, column: 3 });
});
```

- [ ] **Step 2: Run the focused test and confirm RED**

Run: `cd frontend/terminal-ui && node --test test/input-buffer.test.js`

Expected: FAIL because `insertInputNewline`, `moveInputCursorVertical`, and `getInputCursorLocation` are not exported.

- [ ] **Step 3: Implement line-aware cursor helpers**

```javascript
export function insertInputNewline(state) {
  insertInputText(state, "\n");
}

export function getInputCursorLocation(state) {
  const before = Array.from(state.input ?? "").slice(0, getInputCursor(state)).join("");
  const lines = before.split("\n");
  return { line: lines.length - 1, column: Array.from(lines.at(-1) ?? "").length };
}

export function moveInputCursorVertical(state, direction) {
  const lines = String(state.input ?? "").split("\n").map((line) => Array.from(line));
  const location = getInputCursorLocation(state);
  const targetLine = location.line + (direction === "up" ? -1 : 1);
  if (targetLine < 0 || targetLine >= lines.length) return false;
  const preferred = state.inputPreferredColumn ?? location.column;
  const targetColumn = Math.min(preferred, lines[targetLine].length);
  state.inputPreferredColumn = preferred;
  state.inputCursor = lines.slice(0, targetLine).reduce((total, line) => total + line.length + 1, 0) + targetColumn;
  return true;
}
```

Horizontal edits and explicit line-boundary moves reset `state.inputPreferredColumn` to `null`. `Home` and `End` use the current line; `Ctrl+A` and `Ctrl+E` retain whole-buffer semantics.

- [ ] **Step 4: Run editor tests and confirm GREEN**

Run: `cd frontend/terminal-ui && node --test test/input-buffer.test.js`

Expected: all input-buffer tests pass.

- [ ] **Step 5: Commit the editor primitive**

```bash
git add frontend/terminal-ui/src/input-buffer.js frontend/terminal-ui/src/state.js frontend/terminal-ui/test/input-buffer.test.js
git commit -m "feat: add multiline terminal editor primitives"
```

---

### Task 2: Stateful terminal tokenizer and atomic paste

**Files:**
- Modify: `frontend/terminal-ui/src/input-buffer.js`
- Modify: `frontend/terminal-ui/src/index.js`
- Test: `frontend/terminal-ui/test/input-buffer.test.js`
- Test: `frontend/terminal-ui/test/index-process.test.js`

**Interfaces:**
- Produces: `createInputTokenizerState() -> { pendingEscape: string, pasteBuffer: string | null }`.
- Produces: `tokenizeInputChunk(chunk, tokenizerState) -> Array<{ type: "key" | "paste", value: string }>`.
- Recognizes: `INPUT_KEYS.shiftEnter = "\x1b[13;2u"` and `INPUT_KEYS.ctrlEnter = "\x1b[13;5u"` plus `CSI 27;2;13~` and `CSI 27;5;13~` compatibility forms.

- [ ] **Step 1: Write failing framing tests**

```javascript
test("bracketed paste is emitted once across arbitrary chunks", () => {
  const tokenizer = createInputTokenizerState();
  assert.deepEqual(tokenizeInputChunk("\x1b[20", tokenizer), []);
  assert.deepEqual(tokenizeInputChunk("0~第一行\n", tokenizer), []);
  assert.deepEqual(tokenizeInputChunk("第二行\x1b[201~", tokenizer), [
    { type: "paste", value: "第一行\n第二行" },
  ]);
});

test("modified enter sequences normalize to explicit keys", () => {
  const tokenizer = createInputTokenizerState();
  assert.deepEqual(tokenizeInputChunk("\x1b[13;2u\x1b[27;5;13~", tokenizer), [
    { type: "key", value: INPUT_KEYS.shiftEnter },
    { type: "key", value: INPUT_KEYS.ctrlEnter },
  ]);
});
```

- [ ] **Step 2: Run tokenizer tests and confirm RED**

Run: `cd frontend/terminal-ui && node --test test/input-buffer.test.js`

Expected: FAIL because the stateful tokenizer API and modified Enter constants do not exist.

- [ ] **Step 3: Implement framed input tokenization**

The tokenizer retains incomplete CSI data and paste content in `tokenizerState`; it must not expose paste delimiters as editable text. Outside paste mode it delegates complete key text to the existing `splitInputChunk()`. Normalize both supported Shift/Ctrl Enter encodings to one constant per action.

```javascript
const BRACKETED_PASTE_START = "\x1b[200~";
const BRACKETED_PASTE_END = "\x1b[201~";

export function createInputTokenizerState() {
  return { pendingEscape: "", pasteBuffer: null };
}

export function tokenizeInputChunk(chunk, state) {
  let text = `${state.pendingEscape}${String(chunk ?? "")}`;
  state.pendingEscape = "";
  const tokens = [];

  while (text) {
    if (state.pasteBuffer !== null) {
      const end = text.indexOf(BRACKETED_PASTE_END);
      if (end < 0) {
        const overlap = longestSuffixPrefix(text, BRACKETED_PASTE_END);
        state.pasteBuffer += text.slice(0, text.length - overlap);
        state.pendingEscape = text.slice(text.length - overlap);
        return tokens;
      }
      state.pasteBuffer += text.slice(0, end);
      tokens.push({ type: "paste", value: state.pasteBuffer });
      state.pasteBuffer = null;
      text = text.slice(end + BRACKETED_PASTE_END.length);
      continue;
    }

    const start = text.indexOf(BRACKETED_PASTE_START);
    if (start >= 0) {
      for (const key of splitInputChunk(text.slice(0, start))) {
        tokens.push({ type: "key", value: normalizeModifiedEnter(key) });
      }
      state.pasteBuffer = "";
      text = text.slice(start + BRACKETED_PASTE_START.length);
      continue;
    }

    const trailing = extractTrailingIncompleteEscape(text);
    const complete = trailing ? text.slice(0, -trailing.length) : text;
    for (const key of splitInputChunk(complete)) {
      tokens.push({ type: "key", value: normalizeModifiedEnter(key) });
    }
    state.pendingEscape = trailing;
    break;
  }
  return tokens;
}

function longestSuffixPrefix(text, marker) {
  for (let length = Math.min(text.length, marker.length - 1); length > 0; length -= 1) {
    if (text.endsWith(marker.slice(0, length))) return length;
  }
  return 0;
}

function normalizeModifiedEnter(key) {
  if (key === "\x1b[27;2;13~") return INPUT_KEYS.shiftEnter;
  if (key === "\x1b[27;5;13~") return INPUT_KEYS.ctrlEnter;
  return key;
}
```

- [ ] **Step 4: Integrate key precedence in `index.js`**

Integration rules:

```javascript
if (token.type === "paste") {
  insertInputText(state, token.value);
  persistUiSnapshot();
  scheduleRedraw();
  return;
}
if (key === INPUT_KEYS.shiftEnter) insertInputNewline(state);
if (key === INPUT_KEYS.ctrlEnter) submitComposer();
```

Plain `Enter` continues to submit. Pasted newlines never submit. Permission handling remains before composer handling.

- [ ] **Step 5: Run tokenizer and process tests and confirm GREEN**

Run: `cd frontend/terminal-ui && node --test test/input-buffer.test.js test/index-process.test.js`

Expected: all selected tests pass, including a spawned-process test that pastes two lines and submits them only after a later Enter.

- [ ] **Step 6: Commit terminal framing**

```bash
git add frontend/terminal-ui/src/input-buffer.js frontend/terminal-ui/src/index.js frontend/terminal-ui/test/input-buffer.test.js frontend/terminal-ui/test/index-process.test.js
git commit -m "feat: support terminal multiline input framing"
```

---

### Task 3: Responsive multiline composer rendering

**Files:**
- Modify: `frontend/terminal-ui/src/input-buffer.js`
- Modify: `frontend/terminal-ui/src/components/footer.js`
- Modify: `frontend/terminal-ui/src/render.js`
- Test: `frontend/terminal-ui/test/components.test.js`
- Test: `frontend/terminal-ui/test/render.test.js`

**Interfaces:**
- Produces: `renderInputLinesWithCursor(state, availableWidth, maxLines) -> string[]`.
- Consumes: terminal width and the footer clamp that always retains prompt lines.
- Guarantees: composer height is 1 through 6 lines and the cursor line remains visible.

- [ ] **Step 1: Write failing rendering tests**

```javascript
test("prompt renders multiline text without flattening newlines", () => {
  const state = createInitialState();
  setInputText(state, "检查 API\n然后修复测试");
  const lines = renderComponent(PromptFooter({ state }), createRenderContext({ width: 40, state }));
  assert.equal(lines.length, 2);
  assert.match(lines[0], /检查 API/);
  assert.match(lines[1], /然后修复测试.*▌/);
});

test("composer caps visible height and keeps cursor line", () => {
  const state = createInitialState();
  setInputText(state, Array.from({ length: 10 }, (_, index) => `line-${index}`).join("\n"));
  const screen = renderScreen(state, 80, 20);
  assert.ok(screen.some((line) => line.includes("line-9")));
  assert.ok(screen.filter((line) => line.includes("│")).length <= 6);
});
```

- [ ] **Step 2: Run component tests and confirm RED**

Run: `cd frontend/terminal-ui && node --test test/components.test.js test/render.test.js`

Expected: FAIL because the current prompt passes embedded newlines into a single wrapped string.

- [ ] **Step 3: Implement bounded line rendering**

`renderInputLinesWithCursor()` splits logical lines, wraps each line to available width, inserts the cursor marker at the code-point offset, and selects a maximum-six-line window around the cursor. `PromptFooter` prefixes the first visible row with the mode and continuation rows with equal-width indentation. Empty input remains one row.

- [ ] **Step 4: Keep footer clamping composer-first**

When terminal height is constrained, `clampFooterSections()` retains the latest composer rows before help, command completion, activity, and status sections. It must never remove the row containing `▌`.

- [ ] **Step 5: Run rendering tests and confirm GREEN**

Run: `cd frontend/terminal-ui && node --test test/components.test.js test/render.test.js`

Expected: all selected tests pass at 60, 80, and 120 columns.

- [ ] **Step 6: Commit composer rendering**

```bash
git add frontend/terminal-ui/src/input-buffer.js frontend/terminal-ui/src/components/footer.js frontend/terminal-ui/src/render.js frontend/terminal-ui/test/components.test.js frontend/terminal-ui/test/render.test.js
git commit -m "feat: render bounded multiline composer"
```

---

### Task 4: Session-scoped draft persistence

**Files:**
- Modify: `frontend/terminal-ui/src/state.js`
- Modify: `frontend/terminal-ui/src/index.js`
- Modify: `frontend/terminal-ui/src/ui-state-store.js`
- Test: `frontend/terminal-ui/test/state.test.js`
- Test: `frontend/terminal-ui/test/ui-state-store.test.js`
- Test: `frontend/terminal-ui/test/index-process.test.js`

**Interfaces:**
- Extends UI snapshot with `composer: { text: string, cursor: number, preferredColumn: number | null }`.
- Increments store version from 1 to 2 and migrates valid version-1 session snapshots without deleting them.
- Persists after every edit using a 100 ms trailing debounce and synchronously on normal exit or session switch.

- [ ] **Step 1: Write failing snapshot and migration tests**

```javascript
test("ui snapshot restores a multiline draft and cursor", () => {
  const source = createInitialState();
  setInputText(source, "先检查\n再修复", 3);
  const target = createInitialState();
  applyUiSnapshot(target, createUiSnapshot(source));
  assert.equal(target.input, "先检查\n再修复");
  assert.equal(target.inputCursor, 3);
});

test("version one stores migrate to an empty composer", () => {
  writeStore({ version: 1, sessions: { abc: { scrollOffset: 4, folds: {} } } });
  const store = loadUiStateStore(tempDir);
  assert.equal(getUiSnapshot(store, "abc").scrollOffset, 4);
  assert.deepEqual(getUiSnapshot(store, "abc").composer, { text: "", cursor: 0, preferredColumn: null });
});
```

- [ ] **Step 2: Run persistence tests and confirm RED**

Run: `cd frontend/terminal-ui && node --test test/state.test.js test/ui-state-store.test.js`

Expected: FAIL because composer fields are not included and version 1 is currently discarded.

- [ ] **Step 3: Implement snapshot sanitization and migration**

Draft text is capped at 200,000 code points during load, cursor is clamped through `setInputText()`, malformed composer objects become empty drafts, and unknown future store versions are ignored without overwriting the source file.

- [ ] **Step 4: Persist edits and session transitions**

Replace direct per-keystroke disk writes with `scheduleUiSnapshotPersist()` while retaining immediate in-memory snapshot updates. Before `restoreUiSnapshot(nextSessionId)`, persist the outgoing session. On successful submit, clear and persist the draft only after the submit event has been written to Bridge stdin.

- [ ] **Step 5: Run persistence and process tests and confirm GREEN**

Run: `cd frontend/terminal-ui && node --test test/state.test.js test/ui-state-store.test.js test/index-process.test.js`

Expected: all selected tests pass, including restart recovery of a multiline draft.

- [ ] **Step 6: Commit draft recovery**

```bash
git add frontend/terminal-ui/src/state.js frontend/terminal-ui/src/index.js frontend/terminal-ui/src/ui-state-store.js frontend/terminal-ui/test/state.test.js frontend/terminal-ui/test/ui-state-store.test.js frontend/terminal-ui/test/index-process.test.js
git commit -m "feat: persist terminal composer drafts"
```

---

### Task 5: Slice release gate and documentation evidence

**Files:**
- Modify: `docs/product/terminal-ui/02-conversation-timeline-and-composer.md`
- Modify: `docs/product/terminal-ui/README.md`

**Interfaces:**
- Records the completed M2 slice without claiming `follow_tail`, send acknowledgement, history search, or task linkage are complete.

- [ ] **Step 1: Run focused real process scenarios**

Run: `cd frontend/terminal-ui && node --test test/index-process.test.js --test-name-pattern="multiline|paste|draft"`

Expected: a real spawned Terminal UI process proves multiline paste does not submit early, explicit Enter submits exact text, and restart restores the draft.

- [ ] **Step 2: Run the complete Terminal UI module gate**

Run: `cd frontend/terminal-ui && node scripts/check-syntax.js`

Expected: syntax check passes for every frontend JavaScript file.

Run: `cd frontend/terminal-ui && node scripts/run-tests.js`

Expected: all Node tests pass with zero failures.

- [ ] **Step 3: Run the Python Bridge regression gate**

Run: `.venv/bin/python -m pytest -q tests/unit/test_ui_bridge.py tests/unit/test_ui_protocol.py`

Expected: all selected Python tests pass.

- [ ] **Step 4: Update product documentation accurately**

Add an implementation-status section listing multiline editing, explicit submit keys, atomic paste, bounded composer rendering, and draft recovery as complete. Keep `follow_tail`, send lifecycle, history search, and conversation-to-task linkage listed as pending slices.

- [ ] **Step 5: Verify the diff and commit**

Run: `git diff --check`

Expected: no output and exit code 0.

```bash
git add docs/product/terminal-ui/02-conversation-timeline-and-composer.md docs/product/terminal-ui/README.md
git commit -m "docs: record multiline composer delivery"
```

- [ ] **Step 6: Push the feature branch**

```bash
git push -u origin codex/terminal-multiline-composer
```

## Self-Review

- **Spec coverage:** This plan covers the multiline editor, modified Enter behavior, bracketed paste, responsive composer rendering, and session draft recovery from sections 4, 5, 7, and 8 of the M2 specification.
- **Explicit exclusions:** `follow_tail`, unread output indication, optimistic queued/accepted/failed messages, `Ctrl+R` history search, and chat/task mode linkage remain separate future slices and are not marked complete here.
- **Type consistency:** Every task uses the existing mutable frontend state and session snapshot key; no parallel composer state store or backend protocol is introduced.
- **Placeholder scan:** The plan contains no TBD/TODO markers. The tokenizer algorithm is constrained by exact framing behavior and tests rather than an unspecified parser dependency.
