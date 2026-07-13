# Terminal Completion Receipt Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为每次流式 Agent 运行生成一份后端权威、可持久化、可补发的完成收据，并在新 Terminal UI 与 Textual TUI 中用同一结构展示真实改动、验证、未验证项、审批、风险、Git 状态和下一步。

**Architecture:** 将现有 `api/chat_runs.py` 的持久运行模型下沉到中立的 `naumi_agent.runs` 包，由 `AgentEngine.run_streaming()` 包装器统一创建 `ChatRunRecorder`、观察原始引擎事件、生成 `CompletionReceipt` 并写入同一个 `chat-runs.db`。Bridge 只负责把权威收据映射为 `completion/receipt` 和补发协议；新 Terminal UI 与 Textual TUI 都消费共享 `RunReceiptMessage`，不得从本地工具卡临时拼装事实。

**Tech Stack:** Python 3.12+、dataclasses、asyncio subprocess、SQLite/aiosqlite、现有 UIMessage/JSONL Bridge、Node.js 20+ terminal frontend、Textual/Rich。

## Global Constraints

- 中文是默认用户文案；代码注释使用英文；commit message 使用英文。
- 本切片不得修改 `src/naumi_agent/safety/permissions.py`、`tests/unit/test_permissions.py` 或 `codex/terminal-scoped-permissions` 分支上的任何文件。
- 收据事实只能来自引擎事件、工作区探测、权限事件和真实工具结果；模型文本只能提供 `summary` 候选，不能覆盖验证/Git 事实。
- `run/completed` 仍是生命周期信号，只引用 `receipt_id`；完整收据使用版本化 `completion/receipt` 事件。
- 客户端收到 `run/completed` 但未收到收据时必须发送补发请求，不能自行拼接不完整收据。
- 失败、部分成功和取消都必须生成收据；无证据的声明进入 `unverified`。
- Git 探测必须使用参数数组调用 `git`，设置超时，不通过 shell，不读取工作区外文件。
- 命令、路径、输出和错误在写入 SQLite/协议前必须脱敏、限长，不能持久化 token、Cookie、Authorization 或完整环境变量。
- 当前 `origin/main` 的 Python 基线有两项无关失败：worktree 中配置回退路径断言、`/browse` 命令表断言；实现不得顺手修改它们，最终报告必须单列。
- 每个任务先写失败测试并确认 RED，再写实现；完成一个任务立即定向验证并提交。

---

### Task 1: Neutral Run Domain And Durable Receipt Schema

**Files:**
- Create: `src/naumi_agent/runs/__init__.py`
- Create: `src/naumi_agent/runs/models.py`
- Create: `src/naumi_agent/runs/store.py`
- Modify: `src/naumi_agent/api/chat_runs.py`
- Modify: `src/naumi_agent/api/app.py`
- Modify: `src/naumi_agent/api/routes/messages.py`
- Modify: `src/naumi_agent/api/chat_environment.py`
- Test: `tests/unit/test_chat_runs.py`
- Test: `tests/unit/test_api.py`

**Interfaces:**
- Produces: immutable receipt value objects and `CompletionReceipt.to_dict()/from_dict()`.
- Produces: `ChatRunStore.finish_run(..., receipt: CompletionReceipt | None)` and `get_receipt(session_id, receipt_id)`.
- Preserves: `naumi_agent.api.chat_runs` as a compatibility re-export so existing imports do not break.
- Consumes later: Tasks 2–5 import only from `naumi_agent.runs`, never from `naumi_agent.api`.

- [ ] **Step 1: Write failing receipt round-trip and migration tests**

Add tests that build the complete value object, finish a run, reopen the SQLite file and assert every field survives:

```python
receipt = CompletionReceipt(
    schema_version=1,
    receipt_id="receipt-1",
    run_id=run.id,
    outcome="partial",
    summary="完成实现，但验证失败。",
    changes=(ReceiptChange(path="src/app.py", status="modified", source_tool="file_edit"),),
    validations=(ReceiptValidation(
        command="python3 -m pytest tests/unit/test_app.py -q",
        scope="tests/unit/test_app.py",
        status="failed",
        exit_code=1,
        passed=3,
        failed=1,
        log_ref="run:run-1:tool:call-2",
    ),),
    unverified=("未运行完整测试套件",),
    approvals=(ReceiptApproval(call_id="call-1", tool_name="bash_run", decision="allowed_once"),),
    risks=(ReceiptRisk(code="validation_failed", level="high", message="1 项验证失败"),),
    git_state=ReceiptGitState(available=True, branch="codex/test", dirty=True, commit="abc123"),
    next_actions=(ReceiptAction(id="retry-validation", label="重试失败验证", kind="retry_validation"),),
    evidence_refs=("run:run-1:tool:call-2",),
    started_at="2026-07-13T00:00:00+00:00",
    completed_at="2026-07-13T00:00:02+00:00",
    duration_ms=2000,
)
await store.finish_run(run.id, status="partial", receipt=receipt)
restored = await ChatRunStore(db_path).get_receipt("s1", "receipt-1")
assert restored == receipt
```

Also create an old-schema database without `receipt_json`, reopen it and assert `_ensure_tables()` adds the column without deleting runs.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/unit/test_chat_runs.py -q`

Expected: FAIL because `naumi_agent.runs` and receipt persistence do not exist.

- [ ] **Step 3: Implement receipt value objects with strict parsing**

Use frozen/slots dataclasses and tuple collections. `from_dict()` must reject unsupported `schema_version`, invalid `outcome`, blank IDs, negative durations/counts and non-mapping nested data. `to_dict()` must return JSON-safe primitives only.

```python
@dataclass(frozen=True, slots=True)
class CompletionReceipt:
    schema_version: int
    receipt_id: str
    run_id: str
    outcome: Literal["completed", "partial", "failed", "cancelled"]
    summary: str
    changes: tuple[ReceiptChange, ...] = ()
    validations: tuple[ReceiptValidation, ...] = ()
    unverified: tuple[str, ...] = ()
    approvals: tuple[ReceiptApproval, ...] = ()
    risks: tuple[ReceiptRisk, ...] = ()
    git_state: ReceiptGitState = field(default_factory=ReceiptGitState)
    next_actions: tuple[ReceiptAction, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    started_at: str = ""
    completed_at: str = ""
    duration_ms: int = 0
```

Bound collections at 100 changes, 50 validations/approvals/risks/actions, 100 evidence refs; bound public strings at 500 characters and summaries at 2,000 characters.

- [ ] **Step 4: Move the store behind a compatibility module and migrate SQLite**

Move the existing store implementation to `naumi_agent.runs.store`. Add `receipt_json TEXT NOT NULL DEFAULT ''` to `chat_runs`; `_ensure_tables()` must inspect `PRAGMA table_info(chat_runs)` and add the column for old databases. `finish_run()` serializes `receipt.to_dict()` only after validating `receipt.run_id == run_id`; `get_run()` and `list_runs()` hydrate `record.receipt`.

`src/naumi_agent/api/chat_runs.py` becomes an explicit compatibility module:

```python
from naumi_agent.runs.models import *  # noqa: F403
from naumi_agent.runs.store import ChatRunStore

__all__ = ["ChatRunStore", "ChatRunRecord", "ChatRunStepRecord", "ChatArtifactRecord", "SourceReferenceRecord"]
```

Update runtime imports to the neutral path and make `api.app.lifespan()` use `engine.chat_run_store` once Task 2 adds it; until then keep the same resolved DB location.

- [ ] **Step 5: Verify and commit Task 1**

Run: `uv run pytest tests/unit/test_chat_runs.py tests/unit/test_api.py -q`

Run: `uv run ruff check src/naumi_agent/runs src/naumi_agent/api/chat_runs.py src/naumi_agent/api/app.py src/naumi_agent/api/routes/messages.py src/naumi_agent/api/chat_environment.py tests/unit/test_chat_runs.py tests/unit/test_api.py`

Expected: all selected tests and ruff pass.

```bash
git add src/naumi_agent/runs src/naumi_agent/api/chat_runs.py src/naumi_agent/api/app.py src/naumi_agent/api/routes/messages.py src/naumi_agent/api/chat_environment.py tests/unit/test_chat_runs.py tests/unit/test_api.py
git commit -m "refactor: centralize durable chat runs"
```

---

### Task 2: Authoritative Evidence Collector And Engine Lifecycle

**Files:**
- Create: `src/naumi_agent/runs/git_probe.py`
- Create: `src/naumi_agent/runs/receipt_builder.py`
- Create: `src/naumi_agent/runs/recorder.py`
- Modify: `src/naumi_agent/orchestrator/engine.py`
- Modify: `src/naumi_agent/api/routes/messages.py`
- Test: `tests/unit/test_run_receipts.py`
- Test: `tests/unit/test_engine.py`
- Test: `tests/unit/test_api.py`

**Interfaces:**
- Produces: `GitWorkspaceProbe.capture()` and `diff_run_changes(before, after)`.
- Produces: `ChatRunRecorder.start()`, `observe(event, data)`, `finish(result_status, summary)` and `cancel()`.
- Extends: `AgentResult.receipt: CompletionReceipt | None`.
- Emits: raw engine event `completion_receipt` before callers emit `run/completed`.

- [ ] **Step 1: Write failing real-Git delta tests**

Use a real temporary repository. Commit `tracked.txt`, make an unrelated dirty edit before the run baseline, then modify a different file, modify the already-dirty file again, create an untracked file, stage one file and delete one file. Assert the delta includes only net changes made after the baseline, preserves paths with spaces, and reports probe warnings instead of inventing facts when `git` is unavailable.

```python
before = await GitWorkspaceProbe(repo).capture()
(repo / "new file.txt").write_text("new\n", encoding="utf-8")
(repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
after = await GitWorkspaceProbe(repo).capture()
delta = diff_run_changes(before, after)
assert {item.path for item in delta.changes} == {"new file.txt", "tracked.txt"}
assert delta.git_state.branch == "main"
assert delta.git_state.dirty is True
```

- [ ] **Step 2: Write failing validation, approval, outcome and cancellation tests**

Feed `tool_start/tool_end`, `permission_bubble`, `task_snapshot`, error and cancellation events into a recorder. Cover:

- pytest output with passed/failed/skipped counts and `[exit code: 1]`;
- `ruff check` success without fabricated test counts;
- `node --test` TAP counts;
- denied and bypass approvals;
- changed files without validation → `partial` plus `unverified`;
- failed validation → `partial` even if the model result says completed;
- cancelled run preserving changes and offering a recovery action;
- secret-bearing command arguments redacted before SQLite serialization.

- [ ] **Step 3: Run tests and verify RED**

Run: `uv run pytest tests/unit/test_run_receipts.py tests/unit/test_engine.py -k 'receipt or run_streaming' -q`

Expected: FAIL because probe, recorder and `AgentResult.receipt` do not exist.

- [ ] **Step 4: Implement bounded Git snapshots and run delta**

Use `git status --porcelain=v1 -z -uall`, `git rev-parse`, `git rev-list --left-right --count @{upstream}...HEAD`, and bounded content fingerprints for dirty/untracked paths. Preserve pre-existing dirty paths in the baseline and compare status plus fingerprint so another process's existing edits are not automatically attributed to the run. Cap at 500 paths and 8 MiB per fingerprint; when caps, timeouts or parse errors occur, add a precise `probe_warning` that becomes `unverified`.

Never use `shell=True`. Every subprocess has a 3-second timeout and process cleanup.

- [ ] **Step 5: Implement deterministic evidence aggregation**

`ChatRunRecorder.observe()` must:

- persist step state idempotently by stable call/event IDs;
- retain redacted, bounded command summaries only for validation-like commands;
- parse exit code and common pytest/Node/Swift counts from real output;
- persist permission decisions from `permission_bubble` events;
- retain failed tool references and task reconciliation warnings;
- never infer a successful validation from assistant text.

Outcome precedence:

```python
if requested_status == "cancelled": outcome = "cancelled"
elif requested_status in {"error", "failed", "max_turns", "budget_exceeded"}: outcome = "failed"
elif any(validation.status == "failed" for validation in validations): outcome = "partial"
elif changes and not validations: outcome = "partial"
elif unverified or any(risk.level in {"high", "critical"} for risk in risks): outcome = "partial"
else: outcome = "completed"
```

Next actions are deterministic IDs (`review_changes`, `retry_validation`, `request_approval`, `continue_run`, `commit_changes`) and never executable command strings supplied by the model.

- [ ] **Step 6: Wrap `run_streaming()` so every terminal path finishes a record**

Keep the existing body as `_run_streaming_impl()`. The public wrapper must create the session and recorder, attach `run_id` to every event, call the implementation, then finish and emit the receipt. `CancelledError` must generate a cancelled receipt before being re-raised; unexpected wrapper errors generate a failed receipt and preserve the original exception.

```python
async def run_streaming(self, task, on_event, turn_context=""):
    session = await self.get_or_create_session()
    recorder = await ChatRunRecorder.start(
        store=self.chat_run_store,
        workspace_root=self.workspace_root,
        session_id=session.id,
        task=task,
    )
    async def recorded_event(event, data):
        payload = {**data, "run_id": recorder.run_id}
        await recorder.observe(event, payload)
        await on_event(event, payload)
    try:
        result = await self._run_streaming_impl(task, recorded_event, turn_context)
    except asyncio.CancelledError:
        receipt = await recorder.finish("cancelled", "运行已取消")
        await on_event("completion_receipt", receipt.to_dict())
        raise
    receipt = await recorder.finish(result.status, result.response or result.error or "")
    result.receipt = receipt
    await on_event("completion_receipt", receipt.to_dict())
    return result
```

Initialize exactly one `ChatRunStore` on `AgentEngine` at `<session_db_parent>/chat-runs.db`; API lifespan must reuse it. Remove API-side duplicate run/step persistence while retaining source/linked-task artifact attachment on the engine-provided `run_id`.

- [ ] **Step 7: Verify and commit Task 2**

Run: `uv run pytest tests/unit/test_run_receipts.py tests/unit/test_chat_runs.py tests/unit/test_engine.py -k 'receipt or run_streaming or chat_run' -q`

Run: `uv run pytest tests/unit/test_api.py -q`

Run: `uv run ruff check src/naumi_agent/runs src/naumi_agent/orchestrator/engine.py src/naumi_agent/api/routes/messages.py tests/unit/test_run_receipts.py tests/unit/test_engine.py tests/unit/test_api.py`

Expected: all selected tests pass; real Git tests prove pre-existing dirty changes are not mislabeled.

```bash
git add src/naumi_agent/runs src/naumi_agent/orchestrator/engine.py src/naumi_agent/api/app.py src/naumi_agent/api/routes/messages.py tests/unit/test_run_receipts.py tests/unit/test_engine.py tests/unit/test_api.py
git commit -m "feat: persist authoritative run receipts"
```

---

### Task 3: Bridge Receipt Protocol, Correlation And Replay

**Files:**
- Modify: `src/naumi_agent/ui/protocol.py`
- Modify: `src/naumi_agent/ui/bridge.py`
- Modify: `src/naumi_agent/ui/messages/base.py`
- Modify: `src/naumi_agent/ui/messages/events.py`
- Modify: `src/naumi_agent/ui/messages/adapter.py`
- Modify: `src/naumi_agent/ui/messages/replay.py`
- Modify: `frontend/terminal-ui/protocol-contract.json`
- Test: `tests/unit/test_ui_protocol.py`
- Test: `tests/unit/test_ui_bridge.py`
- Test: `tests/unit/test_ui_message_adapter.py`
- Test: `tests/unit/test_ui_message_replay.py`

**Interfaces:**
- Adds server event: `completion/receipt`.
- Adds client event: `completion/receipt_get` with `receipt_id`.
- Adds `run/completed.payload.receipt_id`.
- Produces shared `RunReceiptMessage(type=MessageType.RUN_RECEIPT, receipt=CompletionReceipt)`.

- [ ] **Step 1: Write failing protocol and event-order tests**

Assert the Bridge emits exactly one `completion/receipt` before correlated `run/completed`, both carry the same `run_id/receipt_id`, and failed/cancelled runs also emit a receipt. Assert `completion/receipt_get` returns only a receipt in the current loaded session, returns a Chinese `receipt_not_found` error for missing/cross-session IDs, and never leaks raw command secrets.

- [ ] **Step 2: Write failing replay tests**

Create a real session plus two durable runs, resume the session, and assert replay includes typed receipt messages ordered by `completed_at`. A duplicate live receipt followed by replay must be deduplicable by `receipt_id`.

- [ ] **Step 3: Run tests and verify RED**

Run: `uv run pytest tests/unit/test_ui_protocol.py tests/unit/test_ui_bridge.py tests/unit/test_ui_message_adapter.py tests/unit/test_ui_message_replay.py -k 'receipt' -q`

Expected: FAIL because receipt events/messages are absent.

- [ ] **Step 4: Implement versioned bridge events and compensation request**

Register protocol enums and contract fields. Bridge `on_event("completion_receipt")` emits `completion/receipt` directly, records its ID in active run context, and suppresses the generic `ui/message` duplicate. When the engine returns, `run/completed` references the authoritative receipt ID. The getter loads through `engine.chat_run_store.get_receipt(current_session_id, receipt_id)` and emits the same event.

`run/completed` without a receipt is allowed only for legacy engines and must include `receipt_status="unavailable"`; current engines use `receipt_status="ready"`.

- [ ] **Step 5: Add shared UIMessage adapter and durable replay**

`RunReceiptMessage` exposes only bounded public fields, not a generic untyped dict. `ui_message_payload()` serializes it with `schema_version` and `receipt_id`. `resume_session()` loads stored runs after message replay and emits their receipts in completion order.

- [ ] **Step 6: Verify and commit Task 3**

Run: `uv run pytest tests/unit/test_ui_protocol.py tests/unit/test_ui_bridge.py tests/unit/test_ui_message_adapter.py tests/unit/test_ui_message_replay.py -k 'receipt' -q`

Run: `uv run ruff check src/naumi_agent/ui tests/unit/test_ui_protocol.py tests/unit/test_ui_bridge.py tests/unit/test_ui_message_adapter.py tests/unit/test_ui_message_replay.py`

Expected: receipt-specific tests pass. Also run the full Bridge file while excluding the two recorded mainline failures:

`uv run pytest tests/unit/test_ui_bridge.py -q -k 'not resolve_config_path_falls_back_to_repo_config and not status_payload_exposes_runtime_slash_commands'`

```bash
git add src/naumi_agent/ui frontend/terminal-ui/protocol-contract.json tests/unit/test_ui_protocol.py tests/unit/test_ui_bridge.py tests/unit/test_ui_message_adapter.py tests/unit/test_ui_message_replay.py
git commit -m "feat: add completion receipt protocol"
```

---

### Task 4: New Terminal UI Receipt State, Rendering And Missing-Receipt Recovery

**Files:**
- Create: `frontend/terminal-ui/src/components/completion-receipt-card.js`
- Modify: `frontend/terminal-ui/src/components/message.js`
- Modify: `frontend/terminal-ui/src/state.js`
- Modify: `frontend/terminal-ui/src/protocol.js`
- Modify: `frontend/terminal-ui/src/render.js`
- Test: `frontend/terminal-ui/test/state.test.js`
- Test: `frontend/terminal-ui/test/components.test.js`
- Test: `frontend/terminal-ui/test/protocol.test.js`
- Test: `frontend/terminal-ui/test/flow.test.js`

**Interfaces:**
- Consumes: `completion/receipt` and `run/completed.receipt_id`.
- Produces: one durable `completion_receipt` timeline message keyed by `receiptId`.
- Produces: outbound `completion/receipt_get` when a receipt is missing after a bounded grace period.

- [ ] **Step 1: Write failing reducer tests for all outcomes and correlation**

Cover receipt-before-completed, completed-before-receipt, duplicate receipt, unrelated run, cancelled/failed/partial, session replay, reconnect, and missing-receipt compensation. The reducer must never mutate a receipt's facts from tool-card state.

- [ ] **Step 2: Write failing component tests**

At widths 40, 64 and 120, assert collapsed rows show outcome, duration, changed/validation/unverified counts; expanded rows prioritize failures and include paths, validation scope, approvals, risks, Git state and next action labels. Assert ANSI/control characters are rendered as text and every line stays within width.

- [ ] **Step 3: Run tests and verify RED**

Run: `node --test frontend/terminal-ui/test/state.test.js frontend/terminal-ui/test/components.test.js frontend/terminal-ui/test/protocol.test.js frontend/terminal-ui/test/flow.test.js --test-name-pattern='receipt|completion'`

Expected: FAIL because the receipt component and reducer state do not exist.

- [ ] **Step 4: Implement authoritative receipt state and recovery action**

Keep `receiptsById`, `receiptMessageIds`, and `pendingReceiptIds` bounded to 100 entries. On `completion/receipt`, normalize strictly through the protocol contract, dedupe by ID and append/update one message. On `run/completed` with a ready `receipt_id` not present, queue one getter action; clear it when the receipt arrives. Legacy unavailable receipts render a small diagnostic, never a fabricated success card.

- [ ] **Step 5: Implement the compact/expanded receipt card**

Use semantic text plus existing component primitives. Do not use success confetti or color-only meaning. Default collapsed order:

```text
完成收据 · 部分完成 · 2.4s
改动 3 · 验证 2（失败 1）· 未验证 1
下一步：重试失败验证
```

Expanded sections render only real non-empty fields and cap visible changes/validations at 20 with an explicit hidden-count line.

- [ ] **Step 6: Verify and commit Task 4**

Run: `node --test frontend/terminal-ui/test/state.test.js frontend/terminal-ui/test/components.test.js frontend/terminal-ui/test/protocol.test.js frontend/terminal-ui/test/flow.test.js`

Run: `node frontend/terminal-ui/scripts/check-syntax.js`

Expected: all selected Node tests and syntax checks pass.

```bash
git add frontend/terminal-ui/src frontend/terminal-ui/test
git commit -m "feat: render authoritative completion receipts"
```

---

### Task 5: Textual TUI Receipt Rendering And Replay

**Files:**
- Modify: `src/naumi_agent/tui/renderers/registry.py`
- Modify: `src/naumi_agent/tui/app.py`
- Test: `tests/unit/test_tui_renderers.py`
- Test: `tests/unit/test_tui.py`

**Interfaces:**
- Consumes: the same `RunReceiptMessage` used by Bridge/new UI.
- Produces: compact Rich/Textual receipt widget and status text.
- Preserves: no new TUI-only receipt model or persistence path.

- [ ] **Step 1: Write failing renderer and replay tests**

Assert completed/partial/failed/cancelled styles include textual outcome, validation failures precede successes, paths are escaped, control sequences cannot alter the terminal, and loading a session replays stored receipts after the message history.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/unit/test_tui_renderers.py tests/unit/test_tui.py -k 'receipt' -q`

Expected: FAIL because `MessageType.RUN_RECEIPT` is unregistered.

- [ ] **Step 3: Implement shared receipt rendering**

Register `_render_run_receipt`. Render a compact header, counts, failures/unverified, Git state and first next action. Escape every user/workspace-derived string with `rich.markup.escape`; never render raw ANSI. Reuse a shared pure formatting helper where CLI-style text and TUI need the same ordering.

When TUI loads a session, query `engine.chat_run_store.list_runs(session_id)` and render each non-null receipt ordered by completion time. Live receipts arrive from the engine adapter and must be deduplicated by receipt ID.

- [ ] **Step 4: Verify and commit Task 5**

Run: `uv run pytest tests/unit/test_tui_renderers.py tests/unit/test_tui.py -k 'receipt or replay' -q`

Run: `uv run ruff check src/naumi_agent/tui tests/unit/test_tui_renderers.py tests/unit/test_tui.py`

Expected: all selected tests pass.

```bash
git add src/naumi_agent/tui tests/unit/test_tui_renderers.py tests/unit/test_tui.py
git commit -m "feat: show completion receipts in textual tui"
```

---

### Task 6: Real End-To-End Evidence, Documentation And Publication

**Files:**
- Create: `tests/e2e/test_terminal_completion_receipt.py`
- Modify: `docs/product/terminal-ui/README.md`
- Modify: `docs/product/terminal-ui/06-completion-receipt-and-validation.md`
- Modify: `docs/13-cli-tui-claude-code-roadmap.md`
- Modify: `CHANGELOG.md`
- Modify: `pyproject.toml`

**Interfaces:**
- Proves: real Git edit + validation + Bridge event + new UI reducer + TUI renderer + SQLite replay.
- Records: exact remaining limitations and the two unrelated baseline failures.

- [ ] **Step 1: Write a real process-level acceptance test**

Create a temporary Git repository, configure a real `AgentEngine` with deterministic tool/model fixtures, execute one `file_write` and one real `python3 -m pytest` command through the actual tool path, and capture Bridge JSONL. Assert:

- SQLite contains one receipt;
- `changes` names the real file and excludes the pre-existing dirty file;
- validation scope/count/status match actual pytest output;
- `completion/receipt` precedes `run/completed`;
- replay/getter returns the same receipt ID and facts;
- the Node reducer and TUI renderer consume that payload without fixtures that bypass protocol parsing.

- [ ] **Step 2: Add failure/cancel/no-Git real scenarios**

Use real subprocess/Git operations for a failing pytest run, cancellation after a file write, a no-change read-only run, and a directory without Git. Assert each outcome, unverified item, risk and next action is truthful.

- [ ] **Step 3: Run focused quality gates**

Run: `uv run ruff check src/ tests/e2e/test_terminal_completion_receipt.py`

Run: `uv run python3 -c "from naumi_agent.runs import CompletionReceipt, ChatRunStore; from naumi_agent.ui.messages.events import RunReceiptMessage"`

Run: `uv run pytest tests/unit/test_chat_runs.py tests/unit/test_run_receipts.py tests/unit/test_ui_protocol.py tests/unit/test_ui_message_adapter.py tests/unit/test_ui_message_replay.py tests/unit/test_tui_renderers.py tests/e2e/test_terminal_completion_receipt.py -q`

Run: `uv run pytest tests/unit/test_ui_bridge.py -q -k 'not resolve_config_path_falls_back_to_repo_config and not status_payload_exposes_runtime_slash_commands'`

Run: `node --test frontend/terminal-ui/test/state.test.js frontend/terminal-ui/test/components.test.js frontend/terminal-ui/test/protocol.test.js frontend/terminal-ui/test/flow.test.js`

Expected: all feature-related checks pass; only the two documented baseline failures remain outside the selected Bridge command.

- [ ] **Step 4: Perform a manual real-workspace smoke without touching the colleague checkout**

Inside a temporary Git repository outside `/Users/lv/Workspace/NaumiAgent`, run the Bridge, edit a file, run one passing test, finish, request the receipt again and resume the session. Save only test output paths/IDs in the verification note; remove the temporary repository afterward.

- [ ] **Step 5: Self-review against the product spec**

For every field in `docs/product/terminal-ui/06-completion-receipt-and-validation.md`, point to a test and live evidence. Explicitly audit: complete success, partial success, test failure, cancellation, no file change, Git unavailable, uncommitted change, approval denial and disconnect/missing-receipt compensation. Search for placeholders and secrets:

`rg -n 'TODO|TBD|FIXME|XXX|Bearer |api[_-]?key|authorization' src/naumi_agent/runs frontend/terminal-ui/src/components/completion-receipt-card.js tests/e2e/test_terminal_completion_receipt.py`

- [ ] **Step 6: Update docs/version, commit and push**

Mark only M4 receipt items as completed; do not claim the full Terminal UI productization goal. Add an honest limitations section if cross-process concurrent repository edits cannot be attributed perfectly. Increment the patch version and add one changelog entry.

```bash
git add tests/e2e/test_terminal_completion_receipt.py docs/product/terminal-ui/README.md docs/product/terminal-ui/06-completion-receipt-and-validation.md docs/13-cli-tui-claude-code-roadmap.md CHANGELOG.md pyproject.toml
git commit -m "test: verify terminal completion receipts end to end"
git push -u origin codex/terminal-completion-receipt
```

Expected final state: branch clean, synced, feature tests green, real smoke evidence recorded, colleague checkout unchanged.
