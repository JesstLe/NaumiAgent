# Runtime Inspector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在后端生成当前会话的权威 Runtime Inspector 快照与增量更新，并在新 Terminal UI 和 Textual TUI 中以同一份 Plan、Tools、Context、Changes、Tests 数据实现响应式 Inspector。

**Architecture:** `AgentEngine` 持有中立的 `RuntimeInspectorService`：事件跟踪器只保留有界、脱敏的工具与审批证据，快照构建器实时读取 TaskStore、引擎运行状态和当前会话最近的持久化完成回执。JSONL Bridge 提供 `inspector/request`、`inspector/snapshot`、`inspector/update`，用会话边界和单调 revision 防止跨会话与乱序覆盖；新 Terminal UI 和 Textual TUI 只消费类型化快照，不从时间线反推事实。

**Tech Stack:** Python 3.12+、frozen dataclasses、asyncio、现有 TaskStore/ChatRunStore、JSONL protocol v1、Node.js 20+、Textual/Rich。

## Global Constraints

- 用户可见文案使用中文，代码注释和 commit message 使用英文。
- Inspector 固定包含 Plan、Tools、Context、Changes、Tests 五个标签；没有权威数据时必须明确显示“尚未产生”，不得填充 mock 或零值冒充事实。
- 新 UI 宽度 `>= 120` 使用 38-46 列右侧抽屉且主时间线至少 72 列，`100-119` 使用覆盖式抽屉，`< 100` 使用全屏 Inspector 页。
- `Ctrl+I` 打开/关闭；Inspector 未显式聚焦时普通文本继续进入 Composer；权限请求始终优先处理。
- 所有字符串、列表和事件历史必须有界并经过现有 `OutputGuardrail` 脱敏；协议不得传输原始完整命令输出、环境变量或凭据。
- 快照仅可查询当前会话；update revision 必须严格递增，前端发现 revision 跳号必须请求完整快照。
- 不修改同事分支正在编辑的 `src/naumi_agent/safety/permission_grants.py`、`src/naumi_agent/safety/permissions.py` 及对应权限测试。
- 每一任务遵循 RED → GREEN → targeted regression → commit；最终执行 ruff、import、完整 pytest、完整 Node、语法检查、真实 E2E 和 `uv build`。

---

### Task 1: Authoritative inspector domain and service

**Files:**
- Create: `src/naumi_agent/inspector/__init__.py`
- Create: `src/naumi_agent/inspector/models.py`
- Create: `src/naumi_agent/inspector/tracker.py`
- Create: `src/naumi_agent/inspector/service.py`
- Modify: `src/naumi_agent/orchestrator/engine.py`
- Test: `tests/unit/test_runtime_inspector.py`

**Interfaces:**
- Produces: `RuntimeInspectorSnapshot.to_dict()` / `from_dict()` with fixed `plan`, `tools`, `context`, `changes`, `tests` fields.
- Produces: `RuntimeInspectorService.observe(event, data)`, `await snapshot()`, and `diff(previous, current)`.
- Preserves: raw engine events are not serialized directly; public tool data is bounded to 20 entries and 500 characters per field.

- [ ] **Step 1: Write failing model-boundary tests**

```python
def test_snapshot_rejects_unknown_schema_and_bounds_public_data() -> None:
    with pytest.raises(ValueError, match="schema_version"):
        RuntimeInspectorSnapshot.from_dict({"schema_version": 2})
    tracker = RuntimeInspectorTracker(max_tools=2)
    for index in range(3):
        tracker.observe("tool_start", {"name": "bash_run", "call_id": f"c{index}", "args": "x" * 4000})
    assert [item.call_id for item in tracker.tools] == ["c1", "c2"]
    assert all(len(item.summary) <= 500 for item in tracker.tools)
```

- [ ] **Step 2: Run RED**

Run: `uv run python -m pytest tests/unit/test_runtime_inspector.py -q`

Expected: import failure because `naumi_agent.inspector` does not exist.

- [ ] **Step 3: Implement frozen value objects and the event tracker**

Define fixed records `InspectorTodo`, `InspectorTool`, `InspectorApproval`, `InspectorContext`, `InspectorChanges`, `InspectorTests`, and `RuntimeInspectorSnapshot`. Every collection becomes a tuple; outcomes and tab states accept only `ready | empty | loading | stale | error`; `to_dict()` returns JSON-safe bounded values. `RuntimeInspectorTracker.observe()` correlates tool start/end/error by `call_id`, records permission bubbles, remembers the active run id, and clears only run-local active state at terminal events.

- [ ] **Step 4: Write failing real-source snapshot tests**

```python
@pytest.mark.asyncio
async def test_service_builds_five_tabs_from_tasks_engine_and_latest_receipt(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    await create_real_todo(engine.task_store, subject="补充验证", status="in_progress")
    receipt = await persist_real_receipt(engine, changed_path="src/demo.py", command="pytest -q")
    engine.runtime_inspector.observe("tool_start", {"name": "file_edit", "call_id": "edit-1", "args": '{"path":"src/demo.py"}'})
    engine.runtime_inspector.observe("tool_end", {"name": "file_edit", "call_id": "edit-1", "status": "success"})
    snapshot = await engine.runtime_inspector.snapshot()
    assert snapshot.session_id == engine._session.id
    assert snapshot.plan.items[0].subject == "补充验证"
    assert snapshot.tools.items[0].name == "file_edit"
    assert snapshot.context.workspace_root == str(engine.workspace_root)
    assert snapshot.changes.receipt_id == receipt.receipt_id
    assert snapshot.tests.validations[0].command == "pytest -q"
```

- [ ] **Step 5: Implement the service and engine lifecycle hook**

Instantiate `self.runtime_inspector = RuntimeInspectorService(self)` after engine stores are ready. In `run_streaming.recorded_event`, call `self.runtime_inspector.observe(event, public_data)` before persistence/UI delivery; observe `completion_receipt` before delivering it on success, failure, and cancellation. `snapshot()` reads the current session only, uses `TaskStore.list_tasks()`, engine model/context/budget/usage, the existing async Git probe, and `ChatRunStore.list_runs(session_id, limit=1)`. It computes a content fingerprint excluding timestamps; revision increases only when authoritative content changes.

- [ ] **Step 6: Verify and commit**

Run: `uv run python -m pytest tests/unit/test_runtime_inspector.py tests/unit/test_engine.py -q`

Expected: all pass.

```bash
git add src/naumi_agent/inspector src/naumi_agent/orchestrator/engine.py tests/unit/test_runtime_inspector.py
git commit -m "feat: build authoritative runtime inspector snapshots"
```

---

### Task 2: Session-isolated inspector protocol and live updates

**Files:**
- Modify: `src/naumi_agent/ui/protocol.py`
- Modify: `src/naumi_agent/ui/protocol-contract.json`
- Modify: `src/naumi_agent/ui/bridge.py`
- Modify: `frontend/terminal-ui/src/protocol.js`
- Test: `tests/unit/test_ui_bridge.py`
- Test: `frontend/terminal-ui/test/protocol.test.js`

**Interfaces:**
- Consumes: `await engine.runtime_inspector.snapshot()` and `RuntimeInspectorService.diff()`.
- Produces client event: `inspector/request` with `{open, known_revision, session_id}`.
- Produces server events: `inspector/snapshot` with the full snapshot and `inspector/update` with `{schema_version, session_id, revision, generated_at, changed_tabs}`.

- [ ] **Step 1: Write failing protocol validation tests**

```python
def test_inspector_request_normalizes_subscription_and_revision() -> None:
    event, payload = validate_client_event({"type": "inspector/request", "payload": {"open": True, "known_revision": 7}})
    assert event == ClientEventType.INSPECTOR_REQUEST
    assert payload == {"open": True, "known_revision": 7, "session_id": ""}
```

```javascript
test('normalizes inspector snapshot and rejects unknown tab state', () => {
  const record = normalizeServerRecord(inspectorSnapshotFixture());
  assert.equal(record.payload.revision, 1);
  assert.throws(() => normalizeServerRecord(badInspectorFixture('invented')));
});
```

- [ ] **Step 2: Run RED**

Run: `uv run python -m pytest tests/unit/test_ui_bridge.py -q -k inspector`

Run: `node --test frontend/terminal-ui/test/protocol.test.js`

Expected: unknown event/type assertions fail.

- [ ] **Step 3: Add strict protocol types and normalizers**

Add enum/contract entries and bounded integer validation. JavaScript normalization requires schema v1, the five fixed tabs, non-negative integer revision, matching session ids, known tab states, arrays for items, and bounded public strings. Unknown fields may be ignored, but required fields may not be synthesized.

- [ ] **Step 4: Write failing Bridge subscription/update/session tests**

```python
@pytest.mark.asyncio
async def test_bridge_emits_snapshot_then_monotonic_changed_tab_updates(engine) -> None:
    bridge, records = bound_bridge(engine)
    await bridge.handle_record(inspector_request(open=True, known_revision=0))
    assert records[-1]["type"] == "inspector/snapshot"
    first = records[-1]["payload"]["revision"]
    await bridge.handle_engine_event("tool_start", {"name": "file_read", "call_id": "r1"})
    assert records[-1]["type"] == "inspector/update"
    assert records[-1]["payload"]["revision"] > first
    assert set(records[-1]["payload"]["changed_tabs"]) == {"tools"}
```

Also assert cross-session requests return `inspector_session_mismatch`, closed subscriptions emit no updates, unchanged events emit no update, and a known revision gap returns a full snapshot.

- [ ] **Step 5: Implement Bridge subscription and updates**

`show_inspector()` validates requested/current session, stores `_inspector_subscribed` and `_inspector_revision`, sends a full snapshot on open/refresh, and ACKs close. `handle_engine_event()` first lets the engine service observe the event (already done by the engine for production; service deduplicates identical event ids), then after normal UI delivery calls `_emit_inspector_update()` only for run/tool/todo/permission/receipt/runtime terminal events. If `diff()` is empty, emit nothing; if revisions are non-contiguous, emit a full snapshot.

- [ ] **Step 6: Verify and commit**

Run: `uv run python -m pytest tests/unit/test_ui_bridge.py -q`

Run: `node --test frontend/terminal-ui/test/protocol.test.js`

Expected: all pass.

```bash
git add src/naumi_agent/ui/protocol.py src/naumi_agent/ui/protocol-contract.json src/naumi_agent/ui/bridge.py frontend/terminal-ui/src/protocol.js tests/unit/test_ui_bridge.py frontend/terminal-ui/test/protocol.test.js
git commit -m "feat: stream runtime inspector protocol"
```

---

### Task 3: New Terminal UI Inspector state and responsive rendering

**Files:**
- Create: `frontend/terminal-ui/src/components/runtime-inspector.js`
- Modify: `frontend/terminal-ui/src/state.js`
- Modify: `frontend/terminal-ui/src/render.js`
- Modify: `frontend/terminal-ui/src/components/footer.js`
- Test: `frontend/terminal-ui/test/state.test.js`
- Test: `frontend/terminal-ui/test/components.test.js`
- Test: `frontend/terminal-ui/test/render.test.js`

**Interfaces:**
- Consumes: normalized `inspector/snapshot` and `inspector/update` records.
- Produces: `state.inspector` with open/loading/focused/selectedTab/revision/snapshot/selection/expanded/scroll state.
- Produces: `renderRuntimeInspector(snapshot, viewState, width, height)` returning exactly bounded terminal lines.

- [ ] **Step 1: Write failing reducer ordering and empty-state tests**

```javascript
test('inspector ignores stale updates and refreshes revision gaps', () => {
  const state = createInitialState();
  reduceServerEvent(state, inspectorSnapshotRecord(4));
  assert.equal(reduceServerEvent(state, inspectorUpdateRecord(3)).length, 0);
  const actions = reduceServerEvent(state, inspectorUpdateRecord(6));
  assert.deepEqual(actions, [{ type: 'refresh_inspector', knownRevision: 4 }]);
  assert.equal(state.inspector.revision, 4);
});
```

Verify each absent tab renders “尚未产生” and an error update retains previous items while marking the tab stale/error.

- [ ] **Step 2: Run RED**

Run: `node --test frontend/terminal-ui/test/state.test.js frontend/terminal-ui/test/components.test.js`

Expected: missing inspector state/component failures.

- [ ] **Step 3: Implement reducer and component**

The reducer applies a full snapshot atomically. Updates apply only when `revision === current + 1`; lower revisions are ignored and gaps return `refresh_inspector`. `runtime-inspector.js` renders the five fixed Chinese labels, selected/focus markers, source freshness, bounded item rows, expanded detail, loading/error/stale states, and evidence-specific summaries without computing new facts.

- [ ] **Step 4: Write failing three-width layout tests**

```javascript
for (const [width, mode] of [[140, 'drawer'], [110, 'overlay'], [80, 'page']]) {
  test(`runtime inspector uses ${mode} layout at ${width} columns`, () => {
    const state = inspectorOpenState();
    const lines = renderScreen(state, width, 28);
    assert.equal(lines.length, 28);
    assert(lines.every((line) => visibleWidth(line) <= width));
    assert(strip(lines).includes('Inspector'));
    if (width >= 120) assert(strip(lines).includes('时间线证据'));
    if (width < 100) assert(!strip(lines).includes('时间线证据'));
  });
}
```

Also assert wide main timeline receives at least 72 columns, overlay does not mutate timeline anchor, resize retains selected tab/expanded item, and pending permission footer remains visible.

- [ ] **Step 5: Refactor screen composition and implement layouts**

Split `renderScreen()` into body/footer calculation plus a bounded `composeInspectorLayout()`. Wide mode renders the main body at `width - drawerWidth - 1`; medium mode renders the main body at full width and replaces only the rightmost drawer columns; narrow mode renders the Inspector body alone. Footer/composer remains full width in all modes and continues to display permission prompts.

- [ ] **Step 6: Verify and commit**

Run: `node --test frontend/terminal-ui/test/state.test.js frontend/terminal-ui/test/components.test.js frontend/terminal-ui/test/render.test.js`

Expected: all pass.

```bash
git add frontend/terminal-ui/src/components/runtime-inspector.js frontend/terminal-ui/src/components/footer.js frontend/terminal-ui/src/state.js frontend/terminal-ui/src/render.js frontend/terminal-ui/test/state.test.js frontend/terminal-ui/test/components.test.js frontend/terminal-ui/test/render.test.js
git commit -m "feat: render responsive runtime inspector"
```

---

### Task 4: New Terminal UI Inspector interaction and persistence

**Files:**
- Modify: `frontend/terminal-ui/src/index.js`
- Modify: `frontend/terminal-ui/src/input-buffer.js`
- Modify: `frontend/terminal-ui/src/state.js`
- Modify: `frontend/terminal-ui/src/ui-state-store.js`
- Test: `frontend/terminal-ui/test/state.test.js`
- Test: `frontend/terminal-ui/test/ui-state-store.test.js`
- Test: `frontend/terminal-ui/test/index-process.test.js`
- Test: `frontend/terminal-ui/test/fixtures/fake-bridge.js`

**Interfaces:**
- Produces: `toggleRuntimeInspector(state, send)`, `handleRuntimeInspectorKey(state, key)`, and versioned persisted presentation state.
- Preserves: Composer input, cursor, timeline scroll anchor, permission priority, and active streaming while Inspector is open.

- [ ] **Step 1: Write failing keyboard/focus tests**

```javascript
test('Ctrl+I opens inspector without stealing ordinary composer input', () => {
  const state = createInitialState();
  const sent = [];
  toggleRuntimeInspector(state, (type, payload) => sent.push({ type, payload }));
  assert.equal(state.inspector.open, true);
  assert.equal(state.inspector.focused, false);
  insertInputText(state, '继续执行');
  assert.equal(state.input, '继续执行');
  assert.equal(sent[0].type, 'inspector/request');
});
```

Add RED tests for Tab focus, `[`/`]` and left/right tab switching, up/down selection, Enter expansion, Esc focus-back then close, Ctrl+I close, and permission keystrokes winning over Inspector.

- [ ] **Step 2: Run RED**

Run: `node --test frontend/terminal-ui/test/state.test.js frontend/terminal-ui/test/index-process.test.js --test-name-pattern inspector`

Expected: key and process behavior absent.

- [ ] **Step 3: Implement interaction and outbound refresh actions**

Add `INPUT_KEYS.ctrlI`. In `handleSingleKeyInput`, handle permission first, then Ctrl+I globally. Inspector consumes Tab/arrows/brackets/Enter/Esc only when open, Composer is empty, and Inspector is focused; an unfocused open Inspector never consumes printable input. `handleServerRecord` sends `inspector/request` for reducer `refresh_inspector` actions.

- [ ] **Step 4: Write failing persistence/migration tests**

Persist only `{open, selected_tab, focused:false, selection_by_tab, expanded_by_tab}` per session; never persist server snapshot data or revision. Unknown tabs fall back to `plan`; restored open state sends a new snapshot request after ready/resume.

- [ ] **Step 5: Implement persisted state migration and process fixture**

Bump the UI state schema by one version, migrate older sessions with a closed Plan Inspector, and preserve unknown future versions. Extend the fake Bridge to answer `inspector/request` and emit a live tool update while the panel is open. Process tests verify streaming continues, draft survives open/close, and permission confirmation remains usable.

- [ ] **Step 6: Verify and commit**

Run: `node frontend/terminal-ui/scripts/run-tests.js`

Run: `node frontend/terminal-ui/scripts/check-syntax.js`

Expected: all tests and syntax checks pass.

```bash
git add frontend/terminal-ui/src frontend/terminal-ui/test
git commit -m "feat: add runtime inspector interaction"
```

---

### Task 5: Textual TUI parity from the shared snapshot

**Files:**
- Create: `src/naumi_agent/tui/runtime_inspector.py`
- Modify: `src/naumi_agent/ui/keybindings.py`
- Modify: `src/naumi_agent/tui/app.py`
- Test: `tests/unit/test_keybindings.py`
- Test: `tests/unit/test_tui.py`
- Test: `tests/unit/test_tui_renderers.py`

**Interfaces:**
- Consumes: `RuntimeInspectorSnapshot` from `engine.runtime_inspector.snapshot()`.
- Produces: `format_runtime_inspector_markdown(snapshot, tab)` and a Textual `RuntimeInspectorScreen` with the same five tabs.
- Preserves: no TUI-only source scan, receipt lookup, or client-side fact derivation.

- [ ] **Step 1: Write failing shared formatter tests**

```python
@pytest.mark.parametrize("tab,label", [("plan", "计划"), ("tools", "工具"), ("context", "上下文"), ("changes", "改动"), ("tests", "验证")])
def test_textual_inspector_formats_each_authoritative_tab(snapshot, tab, label) -> None:
    rendered = format_runtime_inspector_markdown(snapshot, tab)
    assert f"Runtime Inspector · {label}" in rendered
    assert "尚未产生" not in rendered
```

Add explicit empty/error/stale tests and assert secret markers are absent.

- [ ] **Step 2: Run RED**

Run: `uv run python -m pytest tests/unit/test_tui_renderers.py -q -k inspector`

Expected: formatter import failure.

- [ ] **Step 3: Implement formatter and Textual screen**

Add `TOGGLE_INSPECTOR` with `c-i` and Textual action `toggle_inspector`. `RuntimeInspectorScreen` loads one service snapshot on mount, uses five stable tab ids, supports `[`/`]`, left/right, manual refresh, and Esc, keeps the last good content on refresh error, and visibly labels stale/error states. The app action toggles this screen and updates its snapshot after run completion when open.

- [ ] **Step 4: Verify Textual interaction and no-duplication contract**

Use Textual pilot tests to open with Ctrl+I, change tabs, close with Esc, simulate an empty repository, and confirm the screen calls the engine service exactly once per refresh rather than querying Git/TaskStore directly.

- [ ] **Step 5: Verify and commit**

Run: `uv run python -m pytest tests/unit/test_keybindings.py tests/unit/test_tui.py tests/unit/test_tui_renderers.py -q`

Expected: all pass.

```bash
git add src/naumi_agent/tui/runtime_inspector.py src/naumi_agent/ui/keybindings.py src/naumi_agent/tui/app.py tests/unit/test_keybindings.py tests/unit/test_tui.py tests/unit/test_tui_renderers.py
git commit -m "feat: sync runtime inspector to textual tui"
```

---

### Task 6: Real end-to-end acceptance, documentation, version, and release gates

**Files:**
- Create: `tests/e2e/test_runtime_inspector.py`
- Modify: `frontend/terminal-ui/test/fixtures/python-bridge-fixture.py`
- Modify: `docs/product/terminal-ui/04-inspector-and-command-pages.md`
- Modify: `docs/product/terminal-ui/README.md`
- Modify: `docs/superpowers/specs/2026-07-13-terminal-ui-productization-design.md`
- Modify: `docs/13-cli-tui-claude-code-roadmap.md`
- Modify: `CHANGELOG.md`
- Modify: `pyproject.toml`
- Modify: `src/naumi_agent/__init__.py`
- Modify: `uv.lock`

**Interfaces:**
- Proves: one real TaskStore todo + real Git edit + real pytest process + persisted completion receipt → service snapshot → Bridge snapshot/update → Node three-width render → Textual formatter.
- Records: exact remaining M5 gaps, if any; M5 is marked complete only if every requirement in product document section 2-4 and snapshot ordering requirements are proven.

- [ ] **Step 1: Write the real-process acceptance test**

Create a temporary real Git repository, commit a baseline, create a real Todo through `TaskStore`, run a real file edit and pytest subprocess through `ChatRunRecorder`, reopen SQLite, build the Inspector, request it through a real `JsonlEngineBridge`, and pass the exact JSON to a Node subprocess that renders widths 140/110/80. Assert the same path/test/branch/todo appears in Python, Bridge, Node, and Textual outputs. Add a second scenario for no Git, failed validation, empty Todo, revision gap recovery, and cross-session rejection.

- [ ] **Step 2: Run E2E RED then GREEN**

Run: `uv run python -m pytest tests/e2e/test_runtime_inspector.py -q`

Expected RED: missing final wiring before implementation; expected GREEN: all cases pass using real subprocesses and SQLite.

- [ ] **Step 3: Update product truth and bump version to 0.1.213**

Document implemented snapshot sources, protocol events, response breakpoints, Textual parity, test evidence, and honest limitations. Do not mark `/agents` or `/workbench` command pages complete; those remain later M6 work.

- [ ] **Step 4: Run self-review scans**

Run: `git diff --check`

Run: `rg -n 'TODO|TBD|FIXME|XXX|Bearer |api[_-]?key|authorization' src/naumi_agent/inspector frontend/terminal-ui/src/components/runtime-inspector.js tests/e2e/test_runtime_inspector.py`

Expected: no placeholder or secret value; a deliberate redaction matcher is acceptable only after manual inspection.

- [ ] **Step 5: Run complete release gates**

Run: `uv run ruff check src/ tests/e2e/test_runtime_inspector.py`

Run: `uv run python -c "from naumi_agent.inspector import RuntimeInspectorService, RuntimeInspectorSnapshot; from naumi_agent.tui.runtime_inspector import format_runtime_inspector_markdown; print('imports-ok')"`

Run: `PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring uv run python -m pytest tests/ -x`

Run: `node frontend/terminal-ui/scripts/run-tests.js`

Run: `node frontend/terminal-ui/scripts/check-syntax.js`

Run: `uv build`

Expected: all offline tests, lint, syntax and build pass; live API tests remain skipped when no real model credential is supplied.

- [ ] **Step 6: Verify coworker isolation, commit, and push**

Run: `git -C /Users/lv/Workspace/NaumiAgent status --short --branch`

Expected: coworker remains on `codex/terminal-scoped-permissions`; no Inspector files appear in that checkout.

```bash
git add CHANGELOG.md docs pyproject.toml src/naumi_agent/__init__.py uv.lock tests/e2e/test_runtime_inspector.py frontend/terminal-ui/test/fixtures/python-bridge-fixture.py
git commit -m "test: verify runtime inspector end to end"
git push origin codex/terminal-completion-receipt
```
