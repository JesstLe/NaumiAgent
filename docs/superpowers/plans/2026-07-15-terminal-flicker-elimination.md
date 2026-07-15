# Terminal UI Flicker Elimination Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate startup double-clears and in-frame tearing in the new Terminal UI without disabling animation or slowing model streaming.

**Architecture:** Introduce a deterministic redraw scheduler that debounces only the unpainted initial frame, while retaining the existing 16ms coalescing for normal frames. Keep the screen painter's full/diff decisions, but wrap each non-empty terminal write in one synchronized-output transaction so supporting terminals present the whole frame atomically and other terminals retain the current diff fallback.

**Tech Stack:** Node.js ES modules, ANSI/DEC terminal control sequences, `node:test`, existing process-level Terminal UI fixture.

## Global Constraints

- Do not run the full Node or Python test suite; run only the named Terminal UI modules and single process test.
- Keep all user-visible messages in Chinese and code comments in English.
- Do not change animation frames, colors, 120ms animation timing, backend protocol, message state, scrolling, TUI fallback, or persistence.
- Every production-code change must be preceded by a failing focused test.
- A full/diff frame must use exactly one `write()` call containing both synchronized-output boundaries; a none frame must perform zero writes.
- The initial settle window is 32ms; the normal redraw coalescing window remains 16ms.
- Complete this feature on `codex/terminal-flicker-elimination`, commit it independently, then fast-forward `main` only after focused and real-scenario verification.

---

### Task 1: Atomic screen-frame presentation

**Files:**
- Modify: `frontend/terminal-ui/src/ansi.js`
- Modify: `frontend/terminal-ui/src/screen-painter.js`
- Modify: `frontend/terminal-ui/test/screen-painter.test.js`

**Interfaces:**
- Consumes: existing `ANSI.clear`, `ANSI.cursorTo(row, column)` and `createScreenPainter({ write })`.
- Produces: `ANSI.synchronizedOutputOn`, `ANSI.synchronizedOutputOff`; full and diff writes wrapped by those constants without changing the painter return shape.

- [ ] **Step 1: Write the failing synchronized full/diff/none tests**

Update the first painter test so it requires exact transaction boundaries and zero writes for an unchanged frame:

```js
assert.equal(
  writes[0],
  `${ANSI.synchronizedOutputOn}${ANSI.clear}header\nworking-0\nfooter${ANSI.synchronizedOutputOff}`,
);
assert.equal(
  writes[1],
  `${ANSI.synchronizedOutputOn}${ANSI.cursorTo(2, 1)}working-1${ANSI.synchronizedOutputOff}`,
);
assert.equal(writes.length, 2);
```

Update the resize and failed-write assertions to require the same paired boundary. Add an assertion that each captured write contains exactly one start and one end sequence.

- [ ] **Step 2: Run the painter test and verify RED**

Run:

```bash
cd frontend/terminal-ui
node --test test/screen-painter.test.js
```

Expected: FAIL because `ANSI.synchronizedOutputOn` / `Off` do not exist and writes are not wrapped.

- [ ] **Step 3: Add named ANSI constants**

Add to `ANSI` in `src/ansi.js`:

```js
synchronizedOutputOn: "\x1b[?2026h",
synchronizedOutputOff: "\x1b[?2026l",
```

- [ ] **Step 4: Route every actual painter write through one transaction helper**

In `src/screen-painter.js`, replace both direct `write(output)` calls with a local helper:

```js
function commit(output) {
  write(`${ANSI.synchronizedOutputOn}${output}${ANSI.synchronizedOutputOff}`);
}
```

Call `commit(output)` for full paint and `commit(changes.join(""))` for diff paint. Do not call it in the `changes.length === 0` branch. Keep `remember(frame)` after the write returns so a failed write remains retryable.

- [ ] **Step 5: Run focused GREEN verification**

Run:

```bash
cd frontend/terminal-ui
node --test test/screen-painter.test.js test/terminal-session.test.js
```

Expected: 7 tests pass, zero failures, and no warnings.

- [ ] **Step 6: Review and commit atomic frame presentation**

Review:

```bash
git diff --check
git diff -- frontend/terminal-ui/src/ansi.js frontend/terminal-ui/src/screen-painter.js frontend/terminal-ui/test/screen-painter.test.js
```

Commit:

```bash
git add frontend/terminal-ui/src/ansi.js frontend/terminal-ui/src/screen-painter.js frontend/terminal-ui/test/screen-painter.test.js
git commit -m "fix(ui): present terminal frames atomically"
```

### Task 2: Stable initial-frame scheduling

**Files:**
- Create: `frontend/terminal-ui/src/redraw-scheduler.js`
- Create: `frontend/terminal-ui/test/redraw-scheduler.test.js`
- Create: `frontend/terminal-ui/test/index-render-scheduling.test.js`
- Modify: `frontend/terminal-ui/src/index.js`
- Modify: `frontend/terminal-ui/test/index-process.test.js`

**Interfaces:**
- Consumes: `redraw()` in `index.js`, injected timer functions, `screenPainter.paint()`.
- Produces: `createRedrawScheduler({ onRedraw, setTimer, clearTimer, frameDelayMs = 16, initialSettleMs = 32 })` returning `schedule()`, `settleInitial()`, `markPainted()`, `cancel()`, `pending`, and `painted`.

- [ ] **Step 1: Write failing deterministic scheduler tests**

Create `test/redraw-scheduler.test.js` with a tiny controllable timer harness and these behaviors:

```js
test("redraw scheduler restarts only the unpainted settle window", () => {
  const clock = createFakeTimers();
  const frames = [];
  const scheduler = createRedrawScheduler({
    onRedraw: () => frames.push("paint"),
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
    frameDelayMs: 16,
    initialSettleMs: 32,
  });

  assert.equal(scheduler.settleInitial(), true);
  assert.equal(clock.lastDelay, 32);
  const firstTimer = clock.lastId;
  assert.equal(scheduler.settleInitial(), true);
  assert.deepEqual(clock.cleared, [firstTimer]);
  assert.equal(clock.lastDelay, 32);
  clock.fire(clock.lastId);
  assert.deepEqual(frames, ["paint"]);
});
```

Add separate tests proving:

- repeated `schedule()` calls coalesce into one timer;
- after `markPainted()`, `schedule()` uses 16ms and `settleInitial()` no longer restarts an existing timer;
- `cancel()` clears exactly one pending timer and becomes idempotent;
- a callback that throws leaves `painted === false` until `markPainted()` is explicitly called.

- [ ] **Step 2: Run scheduler test and verify RED**

Run:

```bash
cd frontend/terminal-ui
node --test test/redraw-scheduler.test.js
```

Expected: FAIL with module-not-found for `src/redraw-scheduler.js`.

- [ ] **Step 3: Implement the minimal scheduler**

Create `src/redraw-scheduler.js` with validated callbacks, one timer slot, clamped numeric delays, and this behavior:

```js
export function createRedrawScheduler({
  onRedraw,
  setTimer = globalThis.setTimeout,
  clearTimer = globalThis.clearTimeout,
  frameDelayMs = 16,
  initialSettleMs = 32,
}) {
  if (typeof onRedraw !== "function") {
    throw new TypeError("重绘调度器需要 onRedraw 回调");
  }
  if (typeof setTimer !== "function" || typeof clearTimer !== "function") {
    throw new TypeError("重绘调度器需要有效的计时器函数");
  }

  const frameDelay = normalizeDelay(frameDelayMs, 16);
  const initialDelay = normalizeDelay(initialSettleMs, 32);
  let timer = null;
  let painted = false;

  const arm = (delay, restart) => {
    if (timer !== null) {
      if (!restart) return false;
      clearTimer(timer);
      timer = null;
    }
    timer = setTimer(() => {
      timer = null;
      onRedraw();
    }, delay);
    timer?.unref?.();
    return true;
  };

  return {
    get pending() { return timer !== null; },
    get painted() { return painted; },
    schedule() { return arm(painted ? frameDelay : initialDelay, false); },
    settleInitial() {
      return painted ? arm(frameDelay, false) : arm(initialDelay, true);
    },
    markPainted() { painted = true; },
    cancel() {
      if (timer === null) return false;
      clearTimer(timer);
      timer = null;
      return true;
    },
  };
}

function normalizeDelay(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, Math.trunc(number)) : fallback;
}
```

- [ ] **Step 4: Run scheduler tests and verify GREEN**

Run:

```bash
cd frontend/terminal-ui
node --test test/redraw-scheduler.test.js
```

Expected: all scheduler tests pass.

- [ ] **Step 5: Write the failing process acceptance assertion**

Extend only `terminal UI animates active work without repeatedly clearing the screen` in `test/index-process.test.js`:

```js
const syncStarts = output.text.split(ANSI.synchronizedOutputOn).length - 1;
const syncEnds = output.text.split(ANSI.synchronizedOutputOff).length - 1;
assert(syncStarts >= 3);
assert.equal(syncStarts, syncEnds);
assert.equal(clearsDuringAnimation, clearsBeforeRun);
```

Run just that test:

```bash
node --test --test-name-pattern="animates active work without repeatedly clearing" test/index-process.test.js
```

Expected: PASS for atomic painter assertions but this does not yet prove scheduler wiring. Create
`test/index-render-scheduling.test.js` with the following focused architecture test:

```js
import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const source = fs.readFileSync(new URL("../src/index.js", import.meta.url), "utf8");

test("terminal entrypoint delegates first-frame and normal redraw scheduling", () => {
  assert.match(source, /import \{ createRedrawScheduler \} from "\.\/redraw-scheduler\.js"/);
  assert.match(source, /const redrawScheduler = createRedrawScheduler\(\{ onRedraw: redraw \}\)/);
  assert.doesNotMatch(source, /let redrawTimer\b/);
  assert.match(source, /redrawScheduler\.settleInitial\(\)/);
  assert.match(source, /const paint = screenPainter\.paint[\s\S]*redrawScheduler\.markPainted\(\)/);
  assert.match(source, /function restoreTerminal\(\)[\s\S]*redrawScheduler\.cancel\(\)/);
});
```

Run:

```bash
node --test test/index-render-scheduling.test.js
```

Expected: FAIL because `index.js` still owns `redrawTimer` and does not import or use the scheduler.

- [ ] **Step 6: Wire the scheduler into `index.js`**

Make these narrow changes:

```js
import { createRedrawScheduler } from "./redraw-scheduler.js";
```

Remove `let redrawTimer = null`. After the screen painter, construct:

```js
const redrawScheduler = createRedrawScheduler({ onRedraw: redraw });
```

Replace `scheduleRedraw()` internals with `redrawScheduler.schedule()`. In `main()`, replace the immediate `redraw()` with `redrawScheduler.settleInitial()`. At the start of `handleTerminalResize()`, before anchor work:

```js
if (!redrawScheduler.painted) {
  viewportWidth = width;
  viewportHeight = height;
  redrawScheduler.settleInitial();
  return;
}
```

After `screenPainter.paint()` returns successfully, call `redrawScheduler.markPainted()`. In `restoreTerminal()`, call `redrawScheduler.cancel()` before restoring terminal controls. Preserve all existing debug payloads and resize-anchor behavior after the first paint.

- [ ] **Step 7: Run the focused scheduler and process tests**

Run:

```bash
cd frontend/terminal-ui
node --test test/redraw-scheduler.test.js test/screen-painter.test.js test/terminal-session.test.js test/index-render-scheduling.test.js
node --test --test-name-pattern="animates active work without repeatedly clearing" test/index-process.test.js
```

Expected: focused unit tests pass; the selected process test passes while unrelated process tests are skipped by name filtering.

- [ ] **Step 8: Run syntax and diff checks**

Run:

```bash
cd frontend/terminal-ui
node scripts/check-syntax.js
cd ../..
git diff --check
```

Expected: syntax check and diff check pass.

- [ ] **Step 9: Review and commit stable initial scheduling**

Review the exact scope:

```bash
git diff --stat HEAD
git diff -- frontend/terminal-ui/src/redraw-scheduler.js frontend/terminal-ui/src/index.js frontend/terminal-ui/test/redraw-scheduler.test.js frontend/terminal-ui/test/index-process.test.js
```

Commit:

```bash
git add frontend/terminal-ui/src/redraw-scheduler.js frontend/terminal-ui/src/index.js frontend/terminal-ui/test/redraw-scheduler.test.js frontend/terminal-ui/test/index-render-scheduling.test.js frontend/terminal-ui/test/index-process.test.js
git commit -m "fix(ui): stabilize the initial terminal frame"
```

### Task 3: Focused acceptance, real PTY smoke, and documentation evidence

**Files:**
- Modify: `docs/superpowers/specs/2026-07-15-terminal-flicker-elimination-design.md`
- Modify: `docs/superpowers/plans/2026-07-15-terminal-flicker-elimination.md`

**Interfaces:**
- Consumes: debug events `render.screen`, `render.error`, `terminal_ui.fatal`; painter payload `paint_mode`, `changed_rows`, `terminal_write`.
- Produces: recorded acceptance evidence and an auditable feature branch ready for fast-forward integration.

- [ ] **Step 1: Run the complete focused test set once**

```bash
cd frontend/terminal-ui
node --test test/redraw-scheduler.test.js test/screen-painter.test.js test/terminal-session.test.js test/index-render-scheduling.test.js
node --test --test-name-pattern="animates active work without repeatedly clearing" test/index-process.test.js
node scripts/check-syntax.js
```

Expected: all selected tests and syntax checks pass; no full suite is invoked.

- [ ] **Step 2: Run a real interactive PTY launch/quit smoke**

Use the installed `expect` tool when available to start the actual `node src/index.js` with the real Python Bridge, wait for `NaumiAgent`, then send Ctrl-C. Set a temporary `NAUMI_TERMINAL_UI_DEBUG_LOG` so evidence is isolated. If `expect` is unavailable, use macOS `script` with the same actual entrypoint and controlled Ctrl-C; do not substitute `--self-test`.

Inspect the isolated JSONL with `jq` and require:

```text
render.error count = 0
terminal_ui.fatal count = 0
first stable-size render.screen paint_mode = full
all later same-size render.screen paint_mode != full
```

- [ ] **Step 3: Record exact acceptance evidence**

Append an “实施状态与验收证据” section to the design and mark the plan tasks complete. Record only actual counts, commands, terminal dimensions, and limitations observed in this run; do not claim Windows/Linux live verification from the macOS smoke.

- [ ] **Step 4: Final self-review**

Check:

```bash
rg -n 'T[B]D|T[O]DO|后续补|待定|placeholde[r]' docs/superpowers/specs/2026-07-15-terminal-flicker-elimination-design.md docs/superpowers/plans/2026-07-15-terminal-flicker-elimination.md
git diff --check
git status --short
git log --oneline --decorate -5
```

Expected: no placeholders, no unrelated changes, clean diff, and one feature per code commit.

- [ ] **Step 5: Commit acceptance evidence**

```bash
git add docs/superpowers/specs/2026-07-15-terminal-flicker-elimination-design.md docs/superpowers/plans/2026-07-15-terminal-flicker-elimination.md
git commit -m "docs: record terminal flicker acceptance"
```

- [ ] **Step 6: Fast-forward main and push after verification**

From the primary checkout, verify `main` is clean and still at the feature base, then:

```bash
git merge --ff-only codex/terminal-flicker-elimination
git push origin main
```

After push, compare `git rev-parse main` with `git rev-parse origin/main`, remove the worktree, delete the merged feature branch, and resume ARC-01.4b.
