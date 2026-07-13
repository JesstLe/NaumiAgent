# Trackpad Scroll Smoothing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make terminal trackpad scrolling precise by converting SS3 wheel bursts into immediate one-line movement capped at roughly 31 accepted events per second.

**Architecture:** Add a deterministic stateful filter in a focused module, then wire only the main timeline SS3 path through it. The filter receives a direction and monotonic clock, so unit tests require no terminal process; a process test sends real SS3 input to prove the complete path.

**Tech Stack:** Node.js 20+, ECMAScript modules, built-in `node:test`, existing Naumi terminal JSONL debug harness.

## Global Constraints

- SS3 scroll step is exactly one rendered timeline line.
- Same-direction events are accepted no faster than once every 32ms.
- The first event and the first reversed-direction event are accepted immediately.
- Throttled events do not redraw or persist a UI snapshot.
- PageUp and PageDown retain their existing half-screen step.
- Do not enable terminal mouse tracking or add dependencies.
- Run only focused terminal UI tests, never the full repository suite.

---

### Task 1: Deterministic trackpad scroll filter

**Files:**
- Create: `frontend/terminal-ui/src/scroll-input.js`
- Create: `frontend/terminal-ui/test/scroll-input.test.js`

**Interfaces:**
- Consumes: an optional `{ intervalMs?: number, now?: () => number }` options object.
- Produces: `createTrackpadScrollFilter(options)` returning `{ accept(direction: "up" | "down"): boolean }` and `TRACKPAD_SCROLL_INTERVAL_MS = 32`.

- [ ] **Step 1: Write failing filter tests**

```js
import assert from "node:assert/strict";
import test from "node:test";
import {
  TRACKPAD_SCROLL_INTERVAL_MS,
  createTrackpadScrollFilter,
} from "../src/scroll-input.js";

test("trackpad filter accepts the first event then caps a same-direction burst", () => {
  let time = 100;
  const filter = createTrackpadScrollFilter({ now: () => time });

  assert.equal(filter.accept("down"), true);
  time += TRACKPAD_SCROLL_INTERVAL_MS - 1;
  assert.equal(filter.accept("down"), false);
  time += 1;
  assert.equal(filter.accept("down"), true);
});

test("trackpad filter accepts direction reversal immediately", () => {
  let time = 100;
  const filter = createTrackpadScrollFilter({ now: () => time });

  assert.equal(filter.accept("down"), true);
  assert.equal(filter.accept("up"), true);
});

test("trackpad filter rejects invalid directions and non-monotonic burst time", () => {
  let time = 100;
  const filter = createTrackpadScrollFilter({ now: () => time });

  assert.equal(filter.accept("down"), true);
  time = 90;
  assert.equal(filter.accept("down"), false);
  time = Number.NaN;
  assert.equal(filter.accept("down"), false);
  assert.equal(filter.accept("sideways"), false);
});
```

- [ ] **Step 2: Run tests and verify RED**

Run: `cd frontend/terminal-ui && node --test test/scroll-input.test.js`

Expected: FAIL with `ERR_MODULE_NOT_FOUND` for `src/scroll-input.js`.

- [ ] **Step 3: Implement the minimal filter**

```js
export const TRACKPAD_SCROLL_INTERVAL_MS = 32;

export function createTrackpadScrollFilter({
  intervalMs = TRACKPAD_SCROLL_INTERVAL_MS,
  now = () => performance.now(),
} = {}) {
  const safeInterval = Math.max(0, Number(intervalMs) || 0);
  let lastDirection = null;
  let lastAcceptedAt = Number.NEGATIVE_INFINITY;

  return {
    accept(direction) {
      if (direction !== "up" && direction !== "down") return false;
      const timestamp = Number(now());
      if (!Number.isFinite(timestamp)) return false;
      if (direction === lastDirection && timestamp - lastAcceptedAt < safeInterval) {
        return false;
      }
      lastDirection = direction;
      lastAcceptedAt = timestamp;
      return true;
    },
  };
}
```

- [ ] **Step 4: Run focused tests and syntax check**

Run: `cd frontend/terminal-ui && node --test test/scroll-input.test.js test/input-buffer.test.js && npm run check`

Expected: all selected tests pass and syntax check exits 0.

- [ ] **Step 5: Commit filter**

```bash
git add frontend/terminal-ui/src/scroll-input.js frontend/terminal-ui/test/scroll-input.test.js
git commit -m "feat: throttle trackpad scroll input"
```

### Task 2: Main timeline integration and real SS3 burst

**Files:**
- Modify: `frontend/terminal-ui/src/index.js:1-80,500-630`
- Modify: `frontend/terminal-ui/test/index-process.test.js:850-920`

**Interfaces:**
- Consumes: `createTrackpadScrollFilter()` from Task 1 and existing `scrollTimeline(state, delta)`.
- Produces: accepted SS3 up/down input changes `state.scrollOffset` by exactly `+1/-1`; ignored input has no visible side effect.

- [ ] **Step 1: Add a failing process regression test**

```js
test("terminal UI process limits a trackpad SS3 burst to precise line scrolling", async () => {
  const app = launchTerminalUi();
  const output = collectOutput(app);

  try {
    await waitForReadyWelcome(output, 7000);
    app.stdin.write("\x1bOA".repeat(40));
    const detached = await waitForDebugEvent(
      app.debugLogPath,
      (record) => record.event === "render.screen"
        && record.payload.follow_tail === false
        && record.payload.scroll_offset > 0,
    );
    assert.equal(detached.payload.scroll_offset, 1);
    assert.equal(await stopTerminalUi(app), 0);
  } finally {
    forceKill(app);
  }
});
```

- [ ] **Step 2: Run process test and verify RED**

Run: `cd frontend/terminal-ui && node --test --test-name-pattern "limits a trackpad SS3 burst" test/index-process.test.js`

Expected: FAIL because the current half-screen step produces `scroll_offset` greater than 1.

- [ ] **Step 3: Wire accepted SS3 input to one-line timeline movement**

Add the import and instance:

```js
import { createTrackpadScrollFilter } from "./scroll-input.js";

const trackpadScrollFilter = createTrackpadScrollFilter();
```

Replace the two SS3 branches and old half-screen helper:

```js
  if (chunk === INPUT_KEYS.upAlt || chunk === INPUT_KEYS.downAlt) {
    const direction = chunk === INPUT_KEYS.upAlt ? "up" : "down";
    if (!trackpadScrollFilter.accept(direction)) return;
    scrollTimeline(state, direction === "up" ? 1 : -1);
    persistUiSnapshot();
    scheduleRedraw();
    return;
  }
```

Delete `adjustScrollOffset()`; PageUp/PageDown keep their existing implementation.

- [ ] **Step 4: Run focused behavior tests**

Run: `cd frontend/terminal-ui && node --test --test-name-pattern "trackpad|tokenizer|timeline|scrolling|following|detached" test/scroll-input.test.js test/input-buffer.test.js test/timeline-follow.test.js`

Expected: selected tests pass.

Run: `cd frontend/terminal-ui && node --test --test-name-pattern "limits a trackpad SS3 burst|preserves detached scroll" test/index-process.test.js`

Expected: both process tests pass.

Run: `cd frontend/terminal-ui && npm run check`

Expected: syntax check exits 0.

- [ ] **Step 5: Real debug-log verification**

Launch the UI with the existing fake bridge, send one chunk containing 40 `ESC OA` sequences, then inspect `render.screen` records. Confirm the first detached record has `scroll_offset: 1`, no delayed scrolling occurs after input stops, and `Ctrl+L` restores `follow_tail: true` with offset 0.

- [ ] **Step 6: Commit integration**

```bash
git add frontend/terminal-ui/src/index.js frontend/terminal-ui/test/index-process.test.js
git commit -m "fix: smooth terminal trackpad scrolling"
```

### Task 3: Merge verification

**Files:**
- Verify only; no planned source changes.

**Interfaces:**
- Consumes: committed Task 1 and Task 2 behavior.
- Produces: merge-ready branch with focused evidence.

- [ ] **Step 1: Run final focused verification**

Run: `cd frontend/terminal-ui && node --test test/scroll-input.test.js test/input-buffer.test.js test/timeline-follow.test.js`

Expected: all selected unit tests pass.

Run: `cd frontend/terminal-ui && node --test --test-name-pattern "limits a trackpad SS3 burst|preserves detached scroll" test/index-process.test.js`

Expected: both selected process tests pass.

Run: `cd frontend/terminal-ui && npm run check`

Expected: syntax check exits 0.

- [ ] **Step 2: Review branch state**

Run: `git diff main...HEAD --check && git status --short`

Expected: no whitespace errors and no uncommitted implementation files.

- [ ] **Step 3: Merge, repeat the same focused commands on `main`, then push `origin/main`**

Expected: local `main` and `origin/main` resolve to the same commit.
