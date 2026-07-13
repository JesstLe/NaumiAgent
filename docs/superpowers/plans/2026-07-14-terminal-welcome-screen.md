# Terminal Welcome Screen Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a responsive, render-only Terminal UI startup screen with a giant `NAUMI` wordmark and authoritative version, workspace, model, runtime-mode, and permission-mode facts that disappears after the first chat or task submission.

**Architecture:** Python adds the product version to the existing authoritative Bridge status payload. Node normalizes that status, tracks a short-lived non-persisted welcome lifecycle, and renders a pure responsive component before the normal timeline; no welcome content becomes a message or session record. The implementation is split into independently testable backend contract, frontend protocol, component, lifecycle, and process-integration commits.

**Tech Stack:** Python 3.13, `JsonlEngineBridge`, Node.js 20+ ESM, ANSI-aware terminal rendering, `node:test`, pytest, Ruff.

## Global Constraints

- Do not add Figlet, font files, network resources, child processes, timers, or runtime dependencies for the wordmark.
- The welcome screen is new Terminal UI only; do not modify Textual TUI, legacy CLI, model routing, budget behavior, or tool registration.
- The initial phase is `booting`; Bridge `ready` changes it to `ready_empty`; first chat/task submission, session replay, or actionable infrastructure error changes it permanently to `dismissed` for that process.
- `/clear`, mode changes, ordinary runtime status updates, drafts, cursor movement, and completion menus must not restore or dismiss the welcome screen.
- The welcome lifecycle must not be serialized by `createUiSnapshot()` or restored by `applyUiSnapshot()`.
- Wide is `width >= 100 && bodyHeight >= 16`; Medium is `width >= 56 && bodyHeight >= 10`; Minimal is `width < 24 || bodyHeight < 4`; every other size is Compact.
- Every rendered line must have ANSI-aware visible width no greater than the viewport width, and the returned body must contain exactly `bodyHeight` lines.
- User-visible copy is Chinese; the literal product wordmark and identifiers remain `NAUMI`, `NaumiAgent`, model names, paths, and mode values.
- Use `NAUMI_MODELS__API_KEY=unit-test-placeholder` for every Python import/test command to avoid macOS Keychain access.
- Use `uv run python -m pytest`, never bare `pytest`, so a fresh worktree cannot fall back to a system Python.
- Run only the targeted files and test-name filters listed below; do not run the full Python or Node suites.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/naumi_agent/ui/bridge.py` | Add backend-authoritative NaumiAgent version to the existing status payload. |
| `tests/unit/test_ui_bridge.py` | Lock the status and ready-event version contract. |
| `frontend/terminal-ui/src/protocol.js` | Normalize identity fields on `ready`, `runtime/status`, and nested `mode/changed.status`. |
| `frontend/terminal-ui/test/protocol.test.js` | Reject non-string identity fields and preserve valid fields. |
| `frontend/terminal-ui/src/components/welcome-screen.js` | Own glyphs, responsive layout selection, pure visibility predicate, centering, facts, and semantic colors. |
| `frontend/terminal-ui/test/welcome-screen.test.js` | Verify all four sizes, missing facts, colors-off semantics, exact height, and bounded width. |
| `frontend/terminal-ui/src/state.js` | Own non-persisted welcome phase and authoritative dismissal events. |
| `frontend/terminal-ui/src/index.js` | Mark direct transport/protocol/storage errors as welcome-dismissing infrastructure messages. |
| `frontend/terminal-ui/test/state.test.js` | Verify lifecycle, chat/task symmetry, replay, `/clear`, mode/status stability, and snapshot exclusion. |
| `frontend/terminal-ui/src/render.js` | Select welcome body before the normal conversation timeline without affecting Inspector or Agent pages. |
| `frontend/terminal-ui/test/render.test.js` | Verify main-viewport integration, footer coexistence, resize tiers, and page priority. |
| `frontend/terminal-ui/test/fixtures/*.js` | Supply authoritative version and optional ready delay in deterministic process fixtures. |
| `frontend/terminal-ui/test/index-process.test.js` | Exercise booting, ready facts, automatic dismissal, and the real Python JSONL Bridge fixture. |
| `docs/product/terminal-ui/README.md` | Record the delivered startup welcome slice. |
| `docs/product/terminal-ui/01-default-entry-and-runtime-shell.md` | Replace the stale “no product startup state” description with the implemented behavior and remaining runtime-shell gaps. |

---

### Task 1: Backend Product Identity Contract

**Files:**
- Modify: `src/naumi_agent/ui/bridge.py:1-35,458-510`
- Modify: `tests/unit/test_ui_bridge.py:1212-1265`

**Interfaces:**
- Consumes: `naumi_agent.__version__: str`.
- Produces: `JsonlEngineBridge.status_payload()["version"]: str`; every `ready` envelope carries the same value.

- [ ] **Step 1: Write failing status and ready-event tests**

Add the import and tests beside the existing status payload tests:

```python
from naumi_agent import __version__


def test_bridge_status_payload_includes_authoritative_product_identity() -> None:
    bridge = JsonlEngineBridge(_FakeEngine(), config_path="config.yaml")

    payload = bridge.status_payload()

    assert payload["version"] == __version__
    assert payload["workspace_root"] == str(Path.cwd())
    assert payload["model"] == "fake-capable"
    assert payload["mode"] == "default"
    assert payload["permission_mode"] == "moderate"


@pytest.mark.asyncio
async def test_bridge_ready_event_carries_authoritative_product_identity() -> None:
    writer = io.StringIO()
    bridge = JsonlEngineBridge(_FakeEngine(), config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.emit_ready()

    ready = _records(writer)[0]
    assert ready["type"] == "ready"
    assert ready["payload"]["version"] == __version__
    assert ready["payload"]["workspace_root"] == str(Path.cwd())
    assert ready["payload"]["model"] == "fake-capable"
    assert ready["payload"]["mode"] == "default"
    assert ready["payload"]["permission_mode"] == "moderate"
```

- [ ] **Step 2: Run the tests and verify the contract fails for the missing field**

Run:

```bash
NAUMI_MODELS__API_KEY=unit-test-placeholder uv run python -m pytest -q tests/unit/test_ui_bridge.py -k 'authoritative_product_identity'
```

Expected: both tests fail with `KeyError: 'version'`.

- [ ] **Step 3: Add the backend-authoritative version field**

At the Bridge imports add:

```python
from naumi_agent import __version__
```

In `status_payload()`, insert the version entry immediately before the current `mode` entry:

```python
payload = {
    "version": __version__,
    "mode": str(getattr(self.engine.runtime_mode, "value", self.engine.runtime_mode)),
}
```

Do not read `pyproject.toml` or package metadata in Node.

- [ ] **Step 4: Run the focused Bridge tests and Ruff**

Run:

```bash
NAUMI_MODELS__API_KEY=unit-test-placeholder uv run python -m pytest -q tests/unit/test_ui_bridge.py -k 'status_payload or ready_event_carries_authoritative_product_identity'
NAUMI_MODELS__API_KEY=unit-test-placeholder uv run ruff check src/naumi_agent/ui/bridge.py tests/unit/test_ui_bridge.py
```

Expected: selected tests pass; Ruff reports `All checks passed!`.

- [ ] **Step 5: Commit the backend contract**

```bash
git add src/naumi_agent/ui/bridge.py tests/unit/test_ui_bridge.py
git commit -m "feat: expose product identity to terminal ui"
```

---

### Task 2: Runtime Status Protocol Normalization

**Files:**
- Modify: `frontend/terminal-ui/src/protocol.js:108-210,690-720`
- Modify: `frontend/terminal-ui/test/protocol.test.js:430-475`

**Interfaces:**
- Consumes: Bridge status payload fields `version`, `workspace_root`, `model`, `mode`, `permission_mode`.
- Produces: `normalizeRuntimeStatus(payload): object` with bounded strings; used by `ready`, `runtime/status`, and `mode/changed.status`.

- [ ] **Step 1: Write failing protocol tests**

Add:

```javascript
test("normalizes authoritative terminal welcome identity fields", () => {
  const ready = normalizeServerRecord({
    type: "ready",
    version: 1,
    payload: {
      version: " 0.1.214 ",
      workspace_root: " /tmp/project ",
      model: " openai/gpt-5.4 ",
      mode: " DEFAULT ",
      permission_mode: " MODERATE ",
    },
  });
  const changed = normalizeServerRecord({
    type: "mode/changed",
    version: 1,
    payload: {
      mode: "bypass",
      status: {
        version: "0.1.214",
        workspace_root: "/tmp/project",
        model: "anthropic/claude-opus-4-6",
        mode: "bypass",
        permission_mode: "bypass",
      },
    },
  });

  assert.deepEqual(
    {
      version: ready.payload.version,
      workspace_root: ready.payload.workspace_root,
      model: ready.payload.model,
      mode: ready.payload.mode,
      permission_mode: ready.payload.permission_mode,
    },
    {
      version: "0.1.214",
      workspace_root: "/tmp/project",
      model: "openai/gpt-5.4",
      mode: "default",
      permission_mode: "moderate",
    },
  );
  assert.equal(changed.payload.status.model, "anthropic/claude-opus-4-6");
});

test("rejects non-string terminal welcome identity fields", () => {
  assert.throws(
    () => normalizeServerRecord({
      type: "ready",
      version: 1,
      payload: { version: { injected: true } },
    }),
    /ready.version 必须是字符串/,
  );
});
```

- [ ] **Step 2: Verify the missing normalization fails**

Run:

```bash
node --test --test-name-pattern='terminal welcome identity' test/protocol.test.js
```

from `frontend/terminal-ui`.

Expected: whitespace/case assertions fail and the object-valued version is not rejected.

- [ ] **Step 3: Implement one bounded status normalizer**

Add before `normalizeServerPayload` returns its generic payload:

```javascript
function normalizeRuntimeStatus(payload, source = "runtime/status") {
  const status = normalizeObject(payload);
  const normalized = { ...status };
  for (const key of ["version", "workspace_root", "model", "mode", "permission_mode"]) {
    if (!Object.hasOwn(status, key)) continue;
    const text = strictStatusText(status[key], `${source}.${key}`);
    normalized[key] = ["mode", "permission_mode"].includes(key)
      ? text.toLowerCase()
      : text;
  }
  return normalized;
}

function strictStatusText(value, name) {
  if (typeof value !== "string") {
    throw new Error(`${name} 必须是字符串`);
  }
  return publicText(value);
}
```

Route the three status-bearing events through it:

```javascript
if (type === "ready" || type === "runtime/status") {
  return normalizeRuntimeStatus(payload, type);
}
if (type === "mode/changed") {
  return {
    ...payload,
    mode: String(payload.mode ?? "").trim().toLowerCase(),
    status: normalizeRuntimeStatus(payload.status, "mode/changed.status"),
  };
}
```

- [ ] **Step 4: Run the focused protocol tests and syntax check**

```bash
node --test --test-name-pattern='terminal welcome identity|normalizes bridge' test/protocol.test.js
npm run check
```

Expected: selected tests pass and syntax check passes.

- [ ] **Step 5: Commit the protocol contract**

```bash
git add frontend/terminal-ui/src/protocol.js frontend/terminal-ui/test/protocol.test.js
git commit -m "feat: normalize terminal product identity"
```

---

### Task 3: Pure Responsive Welcome Component

**Files:**
- Create: `frontend/terminal-ui/src/components/welcome-screen.js`
- Create: `frontend/terminal-ui/test/welcome-screen.test.js`

**Interfaces:**
- Produces: `selectWelcomeLayout(width: number, bodyHeight: number): "wide" | "medium" | "compact" | "minimal"`.
- Produces: `shouldRenderWelcome(state: object): boolean`.
- Produces: `renderWelcomeScreen(state: object, width: number, bodyHeight: number, env?: object): string[]`.
- Consumes later: `renderMainViewport()` calls `shouldRenderWelcome()` and `renderWelcomeScreen()`.

- [ ] **Step 1: Write the failing component tests**

Create `test/welcome-screen.test.js`:

```javascript
import test from "node:test";
import assert from "node:assert/strict";
import { ANSI, stripAnsi, visibleWidth } from "../src/ansi.js";
import {
  renderWelcomeScreen,
  selectWelcomeLayout,
  shouldRenderWelcome,
} from "../src/components/welcome-screen.js";
import { createInitialState } from "../src/state.js";

function readyState() {
  const state = createInitialState();
  state.welcome.phase = "ready_empty";
  state.status = {
    version: "0.1.214",
    workspace_root: "/Users/lv/Workspace/NaumiAgent",
    model: "openai/gpt-5.4",
    mode: "default",
    permission_mode: "moderate",
  };
  return state;
}

test("selects the four exact welcome layouts", () => {
  assert.equal(selectWelcomeLayout(120, 20), "wide");
  assert.equal(selectWelcomeLayout(80, 12), "medium");
  assert.equal(selectWelcomeLayout(48, 8), "compact");
  assert.equal(selectWelcomeLayout(23, 20), "minimal");
  assert.equal(selectWelcomeLayout(120, 3), "minimal");
});

test("renders bounded authoritative facts in every layout", () => {
  for (const [width, height] of [[120, 20], [80, 12], [48, 8], [23, 3]]) {
    const lines = renderWelcomeScreen(readyState(), width, height, {
      home: "/Users/lv",
    });
    const plain = lines.map(stripAnsi).join("\n");
    assert.equal(lines.length, height);
    assert(lines.every((line) => visibleWidth(line) <= width));
    assert.match(plain, /NAUMI|NaumiAgent/);
    assert.match(plain, /已就绪/);
  }

  const wide = renderWelcomeScreen(readyState(), 120, 20, {
    home: "/Users/lv",
  }).map(stripAnsi).join("\n");
  assert.match(wide, /NaumiAgent v0\.1\.214/);
  assert.match(wide, /工作区 ~\/Workspace\/NaumiAgent/);
  assert.match(wide, /模型 openai\/gpt-5\.4/);
  assert.match(wide, /模式 default · 权限 moderate/);
});

test("booting and missing facts never invent runtime values", () => {
  const state = createInitialState();
  const booting = renderWelcomeScreen(state, 100, 16).map(stripAnsi).join("\n");
  assert.match(booting, /正在启动本地运行时/);
  assert.doesNotMatch(booting, /未解析/);

  state.welcome.phase = "ready_empty";
  const unresolved = renderWelcomeScreen(state, 100, 16).map(stripAnsi).join("\n");
  assert.match(unresolved, /NaumiAgent v未解析/);
  assert.match(unresolved, /模型 未解析/);
});

test("uses semantic ANSI colors including bypass warning", () => {
  const state = readyState();
  state.status.mode = "bypass";
  state.status.permission_mode = "bypass";
  const rendered = renderWelcomeScreen(state, 120, 20).join("\n");
  assert.match(rendered, new RegExp(`${ANSI.cyan}.*█`));
  assert.match(rendered, new RegExp(`${ANSI.green}已就绪`));
  assert.match(rendered, new RegExp(`${ANSI.yellow}bypass`));
});

test("welcome visibility is pure and excludes other pages", () => {
  const state = createInitialState();
  assert.equal(shouldRenderWelcome(state), true);
  state.inspector.open = true;
  assert.equal(shouldRenderWelcome(state), false);
  state.inspector.open = false;
  state.route = { name: "agents" };
  assert.equal(shouldRenderWelcome(state), false);
  state.route = { name: "conversation" };
  state.welcome.dismissed = true;
  assert.equal(shouldRenderWelcome(state), false);
});
```

- [ ] **Step 2: Verify the missing module fails**

Run from `frontend/terminal-ui`:

```bash
node --test test/welcome-screen.test.js
```

Expected: test collection fails with `ERR_MODULE_NOT_FOUND` for `welcome-screen.js`.

- [ ] **Step 3: Implement the pure component**

Create `src/components/welcome-screen.js` with this complete boundary:

```javascript
import {
  ANSI,
  color,
  shortPath,
  truncateAnsi,
  visibleWidth,
} from "../ansi.js";

const WIDE_LOGO = [
  "██   ██   █████   ██   ██  ██   ██  ███████",
  "███  ██  ██   ██  ██   ██  ███ ███    ███  ",
  "████ ██  ██   ██  ██   ██  ███████    ███  ",
  "██ ████  ███████  ██   ██  ██ █ ██    ███  ",
  "██  ███  ██   ██  ██   ██  ██   ██    ███  ",
  "██   ██  ██   ██  ██   ██  ██   ██    ███  ",
  "██   ██  ██   ██   █████   ██   ██  ███████",
];

const MEDIUM_LOGO = [
  "█  █   ██   █  █  █  █  ███",
  "██ █  █  █  █  █  ████   █ ",
  "████  ████  █  █  █ ██   █ ",
  "█ ██  █  █  █  █  █  █   █ ",
  "█  █  █  █   ██   █  █  ███",
];

export function selectWelcomeLayout(width, bodyHeight) {
  if (width < 24 || bodyHeight < 4) return "minimal";
  if (width >= 100 && bodyHeight >= 16) return "wide";
  if (width >= 56 && bodyHeight >= 10) return "medium";
  return "compact";
}

export function shouldRenderWelcome(state) {
  return state?.route?.name === "conversation"
    && state?.inspector?.open !== true
    && state?.welcome?.dismissed !== true;
}

export function renderWelcomeScreen(state, width, bodyHeight, env = {}) {
  const safeWidth = Math.max(1, Number(width) || 1);
  const safeHeight = Math.max(1, Number(bodyHeight) || 1);
  const layout = selectWelcomeLayout(safeWidth, safeHeight);
  const ready = state?.welcome?.phase === "ready_empty";
  const status = state?.status ?? {};
  const fact = (value) => String(value || "未解析");
  const mode = fact(status.mode || state?.mode);
  const permissionMode = fact(status.permission_mode);
  const renderedMode = mode === "bypass" ? color(ANSI.yellow, mode) : mode;
  const renderedPermissionMode = permissionMode === "bypass"
    ? color(ANSI.yellow, permissionMode)
    : permissionMode;
  const readiness = ready ? color(ANSI.green, "已就绪") : color(ANSI.yellow, "正在启动本地运行时…");
  const product = `NaumiAgent v${fact(status.version)}`;
  const workspace = shortPath(fact(status.workspace_root), env.home ?? "");
  const model = fact(status.model);

  let content;
  if (layout === "minimal") {
    content = [color(`${ANSI.bold}${ANSI.cyan}`, "NAUMI") + ` · ${readiness}`];
  } else if (!ready) {
    const logo = layout === "wide" ? WIDE_LOGO : layout === "medium" ? MEDIUM_LOGO : ["NAUMI"];
    content = [
      ...logo.map((line) => color(`${ANSI.bold}${ANSI.cyan}`, line)),
      "",
      readiness,
    ];
  } else {
    const logo = layout === "wide" ? WIDE_LOGO : layout === "medium" ? MEDIUM_LOGO : ["NAUMI"];
    content = [
      ...logo.map((line) => color(`${ANSI.bold}${ANSI.cyan}`, line)),
      "",
      `${color(ANSI.dim, "版本")} ${product} · ${readiness}`,
      `${color(ANSI.dim, "工作区")} ${workspace}`,
      `${color(ANSI.dim, "模型")} ${model}`,
      `${color(ANSI.dim, "模式")} ${renderedMode} · ${color(ANSI.dim, "权限")} ${renderedPermissionMode}`,
    ];
  }

  const bounded = content.map((line) => truncateAnsi(line, safeWidth));
  const top = Math.max(0, Math.floor((safeHeight - bounded.length) / 2));
  const lines = Array.from({ length: top }, () => "");
  for (const line of bounded) {
    const left = Math.max(0, Math.floor((safeWidth - visibleWidth(line)) / 2));
    lines.push(`${" ".repeat(left)}${line}`);
  }
  while (lines.length < safeHeight) lines.push("");
  return lines.slice(0, safeHeight);
}
```

If the literal glyph test shows a misspelled letter, correct the fixed constants in this task; do not replace them with a dependency.

- [ ] **Step 4: Run the focused component and syntax tests**

```bash
node --test test/welcome-screen.test.js
npm run check
```

Expected: 5 component tests pass; syntax check passes.

- [ ] **Step 5: Commit the isolated component**

```bash
git add frontend/terminal-ui/src/components/welcome-screen.js frontend/terminal-ui/test/welcome-screen.test.js
git commit -m "feat: add responsive naumi welcome component"
```

---

### Task 4: Non-Persisted Welcome Lifecycle

**Files:**
- Modify: `frontend/terminal-ui/src/state.js:175-230,285-325,510-620,1740-1830,1940-2040`
- Modify: `frontend/terminal-ui/src/index.js:100-145,210-230,705-735`
- Modify: `frontend/terminal-ui/test/state.test.js`

**Interfaces:**
- Produces: `dismissWelcome(state): boolean`, idempotently setting phase to `dismissed`.
- Consumes: `state.welcome` from `createInitialState()` and the component visibility predicate from Task 3.
- Guarantees: snapshots contain no `welcome` key.

- [ ] **Step 1: Write failing lifecycle tests**

Add `submitUserMessage` to the imports and add:

```javascript
test("welcome becomes ready without creating a timeline message", () => {
  const state = createInitialState();
  assert.deepEqual(state.welcome, { phase: "booting", dismissed: false });

  reduceServerEvent(state, {
    type: "ready",
    payload: {
      version: "0.1.214",
      workspace_root: "/tmp/project",
      model: "openai/gpt-5.4",
      mode: "default",
      permission_mode: "moderate",
    },
  });

  assert.deepEqual(state.welcome, { phase: "ready_empty", dismissed: false });
  assert.equal(state.messages.length, 0);
});

test("chat and task submissions dismiss welcome before transport completion", () => {
  for (const intent of ["chat", "task"]) {
    const state = createInitialState();
    const sent = [];
    state.welcome.phase = "ready_empty";
    const send = (type, payload) => sent.push({ type, payload });

    if (intent === "chat") submitUserMessage(state, "你好", send);
    else submitTaskMessage(state, "实现欢迎页", send);

    assert.deepEqual(state.welcome, { phase: "dismissed", dismissed: true });
    assert.equal(state.messages[0].deliveryStatus, "queued");
    assert.equal(sent[0].type, intent === "chat" ? "submit" : "task_submit");
  }
});

test("backend user events replay and errors dismiss welcome idempotently", () => {
  const cases = [
    { type: "user/message", payload: { content: "远端消息" } },
    { type: "task/created", payload: { task: { id: "1" }, mission: {}, issue: {} } },
    { type: "session/replayed", payload: { session_id: "old", title: "旧会话" } },
    { type: "error", payload: { code: "bridge_failed", message: "Bridge 失败" } },
  ];
  for (const record of cases) {
    const state = createInitialState();
    reduceServerEvent(state, record);
    assert.deepEqual(state.welcome, { phase: "dismissed", dismissed: true });
  }
});

test("clear mode status drafts and snapshots do not mutate welcome lifecycle", () => {
  const state = createInitialState();
  state.welcome.phase = "ready_empty";
  reduceServerEvent(state, {
    type: "runtime/status",
    payload: { model: "openai/gpt-5.4" },
  });
  reduceServerEvent(state, {
    type: "mode/changed",
    payload: { mode: "plan", status: { mode: "plan", permission_mode: "strict" } },
  });
  assert.deepEqual(state.welcome, { phase: "ready_empty", dismissed: false });

  submitUserMessage(state, "第一条", () => {});
  handleSubmitText(state, "/clear", () => {});
  assert.deepEqual(state.welcome, { phase: "dismissed", dismissed: true });
  assert.equal(Object.hasOwn(createUiSnapshot(state), "welcome"), false);

  applyUiSnapshot(state, { welcome: { phase: "ready_empty", dismissed: false } });
  assert.deepEqual(state.welcome, { phase: "dismissed", dismissed: true });
});
```

- [ ] **Step 2: Verify lifecycle tests fail**

```bash
node --test --test-name-pattern='welcome becomes|submissions dismiss welcome|replay and errors dismiss welcome|do not mutate welcome' test/state.test.js
```

Expected: initial state has no welcome object, ready still creates the old system message, and dismissal assertions fail.

- [ ] **Step 3: Implement the single lifecycle helper and event wiring**

Add to initial state:

```javascript
welcome: {
  phase: "booting",
  dismissed: false,
},
```

Add the helper:

```javascript
export function dismissWelcome(state) {
  if (!state.welcome || state.welcome.dismissed) return false;
  state.welcome.phase = "dismissed";
  state.welcome.dismissed = true;
  return true;
}
```

Change `ready` to merge facts without a synthetic message:

```javascript
case "ready":
  state.bridgeReady = true;
  mergeStatus(state, payload);
  if (!state.welcome.dismissed) state.welcome.phase = "ready_empty";
  break;
```

Insert `dismissWelcome(state);` as the first statement of the existing `user/message`, `task/created`, `session/replayed`, and `error` cases. The four insertions are identical:

```javascript
case "user/message":
  dismissWelcome(state);

case "task/created":
  dismissWelcome(state);

case "session/replayed":
  dismissWelcome(state);

case "error": {
  dismissWelcome(state);
}
```

In `submitMessage()`, dismiss immediately after the queued message is materialized and before transport send:

```javascript
if (!existingMessage) {
  state.messages.push(message);
}
dismissWelcome(state);

try {
  send(submission.eventType, { text: content, ...submission.payload }, { id: requestId });
```

Extend infrastructure notices without making all yellow mode notices dismiss:

```javascript
export function pushSystemMessage(state, title, content, level, options = {}) {
  if (!content) return;
  if (options.dismissWelcome === true) dismissWelcome(state);
}
```

In `index.js`, pass the explicit option for Bridge stderr/stdin/exit/protocol and UI-state persistence failures:

```javascript
pushSystemMessage(state, "bridge stderr", text, "warning", { dismissWelcome: true });
pushSystemMessage(state, "bridge stdin", `本地 Bridge 写入失败: ${error.message}`, "error", { dismissWelcome: true });
pushSystemMessage(state, "bridge exit", `后端桥接已退出 code=${code} signal=${signal}`, "error", { dismissWelcome: true });
pushSystemMessage(state, "bridge protocol", message, "error", { dismissWelcome: true });
pushSystemMessage(state, "ui state", `无法保存终端 UI 状态: ${error.message}`, "warning", { dismissWelcome: true });
```

Do not add `welcome` to either snapshot function.

- [ ] **Step 4: Run focused lifecycle tests and syntax check**

```bash
node --test --test-name-pattern='welcome|mode changed|snapshot|submit queues|task submit' test/state.test.js
npm run check
```

Expected: selected lifecycle/regression tests pass; syntax check passes.

- [ ] **Step 5: Commit the lifecycle**

```bash
git add frontend/terminal-ui/src/state.js frontend/terminal-ui/src/index.js frontend/terminal-ui/test/state.test.js
git commit -m "feat: manage terminal welcome lifecycle"
```

---

### Task 5: Main Viewport and Process-Level Acceptance

**Files:**
- Modify: `frontend/terminal-ui/src/render.js:1-55`
- Modify: `frontend/terminal-ui/test/render.test.js`
- Modify: `frontend/terminal-ui/test/fixtures/fake-bridge.js:1-30,422-435`
- Modify: `frontend/terminal-ui/test/fixtures/history-bridge.js:10-25`
- Modify: `frontend/terminal-ui/test/fixtures/message-lifecycle-bridge.js:45-65`
- Modify: `frontend/terminal-ui/test/index-process.test.js`
- Modify: `docs/product/terminal-ui/README.md`
- Modify: `docs/product/terminal-ui/01-default-entry-and-runtime-shell.md`

**Interfaces:**
- Consumes: `shouldRenderWelcome()` and `renderWelcomeScreen()` from Task 3; lifecycle from Task 4; normalized status from Task 2.
- Produces: startup screen visible in the real Terminal UI process, then normal timeline after dismissal.

- [ ] **Step 1: Write failing render-integration tests**

Add to `test/render.test.js`:

```javascript
test("conversation viewport renders welcome before the timeline and keeps footer usable", () => {
  const state = createInitialState();
  reduceServerEvent(state, {
    type: "ready",
    payload: {
      version: "0.1.214",
      workspace_root: "/Users/lv/Workspace/NaumiAgent",
      model: "openai/gpt-5.4",
      mode: "default",
      permission_mode: "moderate",
    },
  });

  const lines = renderScreen(state, 120, 24, {
    cwd: "/tmp",
    home: "/Users/lv",
  });
  const plain = lines.map(stripAnsi).join("\n");
  assert.match(plain, /NaumiAgent v0\.1\.214/);
  assert.match(plain, /模型 openai\/gpt-5\.4/);
  assert.match(plain, /chat >/);
  assert.equal(lines.length, 24);
  assert(lines.every((line) => visibleWidth(line) <= 120));
});

test("welcome resize tiers stay bounded and dismissed state reveals the timeline", () => {
  const state = createInitialState();
  state.welcome.phase = "ready_empty";
  state.status = {
    version: "0.1.214",
    workspace_root: "/very/long/workspace/path/for/naumi-agent",
    model: "anthropic/claude-opus-4-6",
    mode: "default",
    permission_mode: "moderate",
  };
  for (const [width, height] of [[120, 24], [80, 14], [48, 10], [23, 5]]) {
    const lines = renderScreen(state, width, height, { cwd: "/tmp" });
    assert.equal(lines.length, height);
    assert(lines.every((line) => visibleWidth(line) <= width));
  }

  state.welcome = { phase: "dismissed", dismissed: true };
  state.messages.push({ kind: "assistant", id: "a1", content: "正常时间线" });
  const dismissed = renderScreen(state, 100, 16).map(stripAnsi).join("\n");
  assert.doesNotMatch(dismissed, /NaumiAgent v0\.1\.214/);
  assert.match(dismissed, /正常时间线/);
});

test("inspector and agent pages take priority over the startup welcome", () => {
  const state = createInitialState();
  state.inspector.open = true;
  state.inspector.loading = true;
  assert.doesNotMatch(renderScreen(state, 80, 14).map(stripAnsi).join("\n"), /NAUMI/);

  state.inspector.open = false;
  state.route = { name: "agents", originAnchor: null };
  state.agents.open = true;
  state.agents.loading = true;
  assert.doesNotMatch(renderScreen(state, 80, 14).map(stripAnsi).join("\n"), /NAUMI/);
});
```

- [ ] **Step 2: Verify render integration fails**

```bash
node --test --test-name-pattern='conversation viewport renders welcome|welcome resize tiers|startup welcome' test/render.test.js
```

Expected: main viewport remains blank/timeline-based, so the identity assertions fail.

- [ ] **Step 3: Route only eligible conversation bodies through the component**

Add imports in `render.js`:

```javascript
import {
  renderWelcomeScreen,
  shouldRenderWelcome,
} from "./components/welcome-screen.js";
```

At the start of `renderMainViewport()`, before the current `const ctx = createRenderContext(...)` line, insert:

```javascript
if (shouldRenderWelcome(state)) {
  return renderWelcomeScreen(state, width, bodyHeight, env);
}
```

- [ ] **Step 4: Make process fixtures authoritative and add process tests**

Add `version: "0.1.214"` to every JavaScript fixture ready/status payload. In `fake-bridge.js`, make ready delay deterministic without affecting default tests:

```javascript
if (record.type === "hello") {
  const delayMs = Math.max(0, Number(process.env.NAUMI_TEST_READY_DELAY_MS) || 0);
  setTimeout(() => emit("ready", statusPayload()), delayMs);
  return;
}

function statusPayload(overrides = {}) {
  return {
    version: "0.1.214",
    session_id: sessionId,
    mode,
    permission_mode: permissionModeFor(mode),
    model: "openai/kimi-for-coding",
    workspace_root: "/Users/lv/Workspace/NaumiAgent",
    usage: { total_tokens: 0 },
    context: { used: 0, window: 256000, percentage: 0 },
    budget: { used_usd: 0, max_usd: 5 },
    ui: { show_reasoning: showReasoning },
    git: { branch: "main", dirty: true },
    ...overrides,
  };
}
```

In `index-process.test.js`, add:

```javascript
test("terminal UI startup welcome transitions from booting to ready and dismisses", async () => {
  const app = launchTerminalUi("fake-bridge.js", {
    env: { NAUMI_TEST_READY_DELAY_MS: "80" },
  });
  const output = collectOutput(app);

  try {
    await waitForOutput(output, "正在启动本地运行时", 7000);
    await waitForLatestScreen(output, "NaumiAgent v0.1.214", 7000);
    await waitForLatestScreen(output, "模型 openai/kimi-for-coding", 7000);
    await waitForLatestScreen(output, "模式 default · 权限 moderate", 7000);

    app.stdin.write("检查欢迎页\n");
    await waitForLatestScreenWithout(output, "NaumiAgent v0.1.214", 7000);
    await waitForLatestScreen(output, "检查欢迎页", 7000);
    assert.equal(await stopTerminalUi(app), 0);
  } finally {
    forceKill(app);
  }
});

test("terminal UI welcome consumes identity from the real Python JSONL Bridge", async () => {
  const app = launchTerminalUi("python-bridge-fixture.py", {
    bridgeCommandJson: [pythonExecutable(), "test/fixtures/python-bridge-fixture.py"],
  });
  const output = collectOutput(app);

  try {
    await waitForLatestScreen(output, "NaumiAgent v0.1.214", 7000);
    await waitForLatestScreen(output, "模型 python-fixture-capable", 7000);
    await waitForLatestScreen(output, "模式 default · 权限 moderate", 7000);
    assert.equal(await stopTerminalUi(app), 0);
  } finally {
    forceKill(app);
  }
});
```

Replace the 39 old readiness waits with a helper, so tests wait for authoritative ready rather than the removed synthetic message:

```javascript
async function waitForReadyWelcome(output, timeoutMs = 7000) {
  await waitForLatestScreen(output, "NaumiAgent v0.1.214", timeoutMs);
}
```

Each old call of:

```javascript
await waitForOutput(output, "新终端 UI 已连接 Python bridge。", 7000);
```

becomes:

```javascript
await waitForReadyWelcome(output, 7000);
```

Use `firstOutput` or `secondOutput` as the first argument where those variable names already exist.

- [ ] **Step 5: Update the active product documentation**

In `docs/product/terminal-ui/README.md`, add to module 01 completed work:

```markdown
响应式启动欢迎页（巨大 NAUMI、后端权威版本/工作区/模型/模式、首条消息后收起）
```

In `01-default-entry-and-runtime-shell.md`, replace the stale startup evidence with:

```markdown
- 新 Terminal UI 已具备渲染层专用启动欢迎页：进程启动立即显示响应式 NAUMI，Bridge ready 后展示权威版本、工作区、模型与权限模式；首条 chat/task 提交后收起，不写入消息、会话或 UI snapshot。
- 仍未完成的是启动前依赖诊断、ready 超时恢复选择、安装态资源解析和旧入口迁移；这些不由欢迎页伪装为完成。
```

- [ ] **Step 6: Run only the welcome-related Node, Bridge, and syntax checks**

From `frontend/terminal-ui`:

```bash
node --test test/welcome-screen.test.js
node --test --test-name-pattern='welcome|startup|real Python JSONL Bridge|terminal UI process handles submit' test/render.test.js test/state.test.js test/protocol.test.js test/index-process.test.js
npm run check
```

From repository root:

```bash
NAUMI_MODELS__API_KEY=unit-test-placeholder uv run python -m pytest -q tests/unit/test_ui_bridge.py -k 'status_payload or ready_event_carries_authoritative_product_identity'
NAUMI_MODELS__API_KEY=unit-test-placeholder uv run ruff check src/naumi_agent/ui/bridge.py tests/unit/test_ui_bridge.py
git diff --check
```

Expected: all selected tests pass, syntax and Ruff pass, and `git diff --check` has no output. Do not extrapolate these targeted results to the full repository suite.

- [ ] **Step 7: Perform the real terminal UX check**

Run the dedicated process test with the worktree Python explicitly pinned:

```bash
cd frontend/terminal-ui
NAUMI_TEST_PYTHON="$(cd ../.. && pwd)/.venv/bin/python" \
  node --test --test-name-pattern='startup welcome|real Python JSONL Bridge' test/index-process.test.js
```

Inspect the latest-screen assertions as evidence for booting, ready facts, dismissal, footer coexistence, and clean exit. This is a real Node process connected to the actual Python `JsonlEngineBridge`; only the model response is deterministic to avoid paid/network calls.

- [ ] **Step 8: Commit the integrated welcome screen**

```bash
git add frontend/terminal-ui/src/render.js \
  frontend/terminal-ui/test/render.test.js \
  frontend/terminal-ui/test/fixtures/fake-bridge.js \
  frontend/terminal-ui/test/fixtures/history-bridge.js \
  frontend/terminal-ui/test/fixtures/message-lifecycle-bridge.js \
  frontend/terminal-ui/test/index-process.test.js \
  docs/product/terminal-ui/README.md \
  docs/product/terminal-ui/01-default-entry-and-runtime-shell.md
git commit -m "feat: show terminal startup welcome screen"
```

---

## Final Slice Review

After Task 5, inspect the accumulated branch rather than trusting individual commits:

```bash
git status --short --branch
git diff --check origin/main...HEAD
git diff --stat origin/main...HEAD
git log --oneline origin/main..HEAD
rg -n '新终端 UI 已连接 Python bridge|T[B]D|T[O]DO|F[I]XME' \
  frontend/terminal-ui/src \
  frontend/terminal-ui/test \
  docs/product/terminal-ui \
  docs/superpowers/specs/2026-07-14-terminal-welcome-screen-design.md \
  docs/superpowers/plans/2026-07-14-terminal-welcome-screen.md
```

Expected:

- Worktree clean.
- No whitespace errors.
- No old synthetic ready copy in active frontend source/tests.
- No plan/spec placeholders.
- Exactly the approved welcome-screen scope plus its design/plan commits; no budget, model, tool, CLI, or TUI implementation changes.

Run the targeted commands from Task 5 once more immediately before claiming completion or merging. Merge and push only after the accumulated branch passes those fresh checks.
