# Terminal Working Animation Implementation Plan

> **Goal:** Add a terminal-native animated Naumi working image to the Node Terminal UI and synchronize a compact form to Textual without terminal-specific image protocols.

**Architecture:** A pure Node component renders stable wide, compact, and ASCII frames from authoritative run state. A separately testable single-timer controller advances an ephemeral frame counter and uses the existing redraw coalescer. The animation is a non-persistent body tail, pauses for permission/cancellation, and stops on every terminal path. Textual reuses its existing timer but delegates frame content to a pure Rich formatter.

**Verification policy:** Run only the new animation tests, the directly affected render/state tests, a small Textual selector, syntax/Ruff checks, and a real pure-render smoke. Do not run full Node or Python suites. Do not use subagents.

---

## Task 1: Build the pure Node frame renderer and timer controller

**Files:**

- Create: `frontend/terminal-ui/src/components/working-indicator.js`
- Create: `frontend/terminal-ui/src/working-animation.js`
- Create: `frontend/terminal-ui/test/working-animation.test.js`

### RED tests

Cover:

1. four Unicode frames have identical stripped width but visibly different eye/core glyphs;
2. wide layout is three lines and compact layout one line;
3. ASCII mode contains no non-ASCII glyph and remains static;
4. `awaiting_permission` and `cancelPending` produce explicit static labels;
5. `executing` says “工具执行中”; generating/summarizing say “模型工作中”;
6. every line remains within the requested width and ends with bounded styles;
7. controller creates at most one timer, advances modulo frame count, stops/clears exactly once, and can resume;
8. `shouldAnimateWorkingIndicator()` rejects non-TTY, `TERM=dumb`, reduced motion, permission wait, cancel wait and idle state.

Run only the new test and capture the missing-module failure.

### Implementation

`working-indicator.js` exports:

```js
export const WORKING_FRAME_COUNT = 4;
export function workingIndicatorStatus(state) {}
export function shouldAnimateWorkingIndicator(state, capabilities) {}
export function renderWorkingIndicator(state, width, options = {}) {}
```

`working-animation.js` exports a scheduler-injected controller:

```js
export function createWorkingAnimationController({ onFrame, setTimer, clearTimer, intervalMs }) {}
```

The controller must call `unref()` when the returned timer supports it. Rendering sanitizes phase labels and uses only existing ANSI helpers.

Run the new test and commit this pure capability independently.

## Task 2: Integrate the animation into real Node state, viewport and process lifecycle

**Files:**

- Modify: `frontend/terminal-ui/src/state.js`
- Modify: `frontend/terminal-ui/src/render.js`
- Modify: `frontend/terminal-ui/src/index.js`
- Modify: `frontend/terminal-ui/test/state.test.js`
- Modify: `frontend/terminal-ui/test/render.test.js`
- Modify if needed: `frontend/terminal-ui/test/index-process.test.js`

### RED tests

- initial state has frame `0` but no visible indicator;
- `run/started` renders the image, `run/completed` and `run/cancelled` remove it;
- permission request renders a static waiting line and resolved permission resumes active semantics;
- a wide/adequate body reserves three tail lines while a small body reserves one;
- `renderViewportLayout()` and visible rendering use the same tail height;
- scroll/resize anchors remain bounded with an active animation;
- an injected process/timer path clears animation on exit if an existing process fixture can prove it without broad tests.

### Implementation

- Add `workingAnimationFrame: 0` only to in-memory state; do not add it to UI snapshots.
- Reset the frame on run start/terminal events and session replay.
- Replace the old `运行中...` tail with `renderWorkingIndicator()`.
- Pass `bodyHeight` and terminal capability context consistently to every `renderBodyTail()` caller.
- Create one controller in `index.js`; call `sync()` after every reduced server record and `stop()` before terminal restoration/exit.
- Use `process.stdout.isTTY`, `TERM`, `CI`, and `NAUMI_REDUCE_MOTION` only for animation playback. Static status remains visible when playback is disabled.
- Do not write directly from the interval; call the existing `scheduleRedraw()`.

Run only animation, state tests filtered to the new names, render tests filtered to working/viewport names, and any one process test added. Commit independently.

## Task 3: Synchronize the compact working image to Textual

**Files:**

- Create: `src/naumi_agent/tui/working_indicator.py`
- Modify: `src/naumi_agent/tui/app.py`
- Modify: `tests/unit/test_tui.py`

### RED tests

Test the pure formatter, not a live Textual event loop:

- four frames have stable plain width and unique core glyphs;
- plain text includes `Naumi 工作中`;
- outline/core/text receive cyan/magenta-or-blue/green Rich styles;
- frame indices normalize safely for negative and very large values.

### Implementation

Add `render_working_indicator_frame(index: int) -> Text`. Replace `Spinner._FRAMES` string lookup with the formatter while preserving the existing 80ms Textual timer, activation calls, single-line height and pause/clear behavior.

Run the focused TUI selector, Ruff and py_compile. Commit independently.

## Task 4: Focused real verification and documentation closeout

Run only:

```bash
cd frontend/terminal-ui
node --test test/working-animation.test.js
node --test --test-name-pattern='working indicator|working animation|active working tail' \
  test/state.test.js test/render.test.js test/index-process.test.js
npm run check

cd ../..
PYTHONPATH=src .venv/bin/pytest tests/unit/test_tui.py -k working_indicator -q
.venv/bin/ruff check src/naumi_agent/tui/working_indicator.py src/naumi_agent/tui/app.py tests/unit/test_tui.py
.venv/bin/python -m py_compile src/naumi_agent/tui/working_indicator.py
.venv/bin/python scripts/check_docs.py
git diff --check
```

Run a real pure-render smoke that advances all four frames, renders wide/compact/ASCII/waiting states, and asserts:

- all widths are bounded;
- every active frame contains text status;
- waiting/cancel states do not change between frame values;
- the Node controller leaves no timer after stop;
- Textual Rich frames keep stable plain width.

Self-review:

- no raster/private image protocol added;
- no timer while idle, waiting for permission, cancelling, non-TTY, CI, dumb terminal or reduced motion;
- no frame state persisted;
- no direct interval writes or scroll-offset mutation;
- live and replay completion clear the indicator;
- animation does not reveal hidden reasoning;
- untracked root files remain untouched;
- no full test suite was run.

Fast-forward/push the completed feature to `main` only after all focused checks pass.
