# ARC-01.3e EventSink Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` task-by-task. The user has explicitly required inline execution by the primary agent; do not dispatch subagents. Every task uses RED→GREEN and an independent commit.

**Goal:** Build a typed, ordered, injectable EventSink as the only Runtime event-output boundary while preserving CLI, TUI, New UI, SSE, WebSocket, Harness, Inspector and durable receipt behavior.

**Architecture:** Runtime emits immutable `RuntimeEvent` values through a run-scoped `RuntimeEventPublisher`. The publisher assigns identity/context/sequence and awaits a deterministic `CompositeEventSink`; callback and transport adapters live after this boundary. Engine retains one temporary legacy callback adapter for third-party compatibility, but repository product surfaces pass explicit sinks.

**Tech Stack:** Python 3.12+, asyncio, Protocol/dataclass/StrEnum, existing AgentEngine, ChatRunRecorder, RuntimeInspectorTracker, FastAPI SSE/WebSocket, Textual TUI, JSONL New UI, pytest, Ruff.

## Global Constraints

- Implement one task, run only its focused tests, self-review, then commit before starting the next task.
- Use `PYTHONPATH=src /Users/lv/Workspace/NaumiAgent/.venv/bin/python`; never use the worktree through the editable main install without `PYTHONPATH`.
- Do not run `pytest tests/` or any full suite.
- Do not call external models or network services in verification.
- User-visible errors are Chinese; code comments and commit messages are English.
- Do not delete old CLI or `streaming.EventEmitter` source.
- Do not add a background dispatcher, unbounded queue, event dropping, sampling or token coalescing.
- `CancelledError` must propagate; ordinary Sink errors must not be silently swallowed.
- Every Runtime payload must be recursively JSON-safe and immutable after construction.
- Same-run event sequence begins at 1 and is strictly increasing in actual Sink delivery order.
- Completion receipt is durably persisted before its event is delivered and is emitted at most once per run.
- ARC-01.3e must not implement ARC-01.4 composition root or ARC-01.6 import rules.

---

### Task 1: Establish the EventSink contract RED suite

**Files:**
- Create: `tests/unit/test_event_sink_port.py`
- Future create: `src/naumi_agent/runtime/ports/events.py`
- Future modify: `src/naumi_agent/runtime/ports/__init__.py`

**Interfaces:**
- Consumes: no new production interface.
- Produces: failing import and contract expectations for `RuntimeEventType`, `RuntimeEvent`, `EventSink`, `JsonValue`, `LegacyEventCallback`, `freeze_json_value`, `thaw_event_data`.

- [ ] **Step 1: Write failing imports and exact Protocol test**

```python
def test_event_sink_exposes_only_emit() -> None:
    methods = {
        name for name, value in vars(EventSink).items()
        if not name.startswith("_") and inspect.isfunction(value)
    }
    assert methods == {"emit"}
```

- [ ] **Step 2: Add the authoritative event manifest assertion**

Assert the enum values equal the 30 audited literal events plus `tool_error`; use a literal `frozenset`, not a count-only assertion.

- [ ] **Step 3: Run RED**

```bash
PYTHONPATH=src /Users/lv/Workspace/NaumiAgent/.venv/bin/python -m pytest -q tests/unit/test_event_sink_port.py
```

Expected: collection fails because `naumi_agent.runtime.ports.events` does not exist.

- [ ] **Step 4: Commit RED tests**

```bash
git add tests/unit/test_event_sink_port.py
git commit -m "test(runtime): define event sink contract [ARC-01.3e]"
```

### Task 2: Implement immutable RuntimeEvent values

**Files:**
- Create: `src/naumi_agent/runtime/ports/events.py`
- Modify: `src/naumi_agent/runtime/ports/__init__.py`
- Modify: `tests/unit/test_event_sink_port.py`

**Interfaces:**
- Produces: `RuntimeEventType`, `RuntimeEvent`, `EventSink`, `JsonScalar`, `JsonValue`, `LegacyEventCallback`, `freeze_json_value(value, path="$data")`, `thaw_event_data(data)`.

- [ ] **Step 1: Add RED value-validation cases**

Cover empty/unknown type, empty id, naive timestamp, negative/bool turn and sequence, non-string/empty keys, bytes, Path, set, object, NaN and Infinity. Add mutation tests proving nested source changes and thawed-result changes cannot alter `RuntimeEvent.data`.

- [ ] **Step 2: Verify the new cases fail for missing behavior**

Run the single file and confirm failures are validation/immutability failures, not fixture errors.

- [ ] **Step 3: Implement recursive JSON freeze/thaw**

```python
def freeze_json_value(value: object, *, path: str = "$data") -> JsonValue:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, Mapping):
        frozen = {require_key(key, path): freeze_json_value(item, path=f"{path}.{key}")
                  for key, item in value.items()}
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json_value(item, path=f"{path}[{index}]")
                     for index, item in enumerate(value))
    raise TypeError(f"事件数据 {path} 不是可序列化 JSON 值")
```

Implement `RuntimeEvent.__post_init__` with `object.__setattr__` only for the frozen copied payload. Parse timestamp through `datetime.fromisoformat()` and require non-null `utcoffset()`.

- [ ] **Step 4: Export the stable API and run GREEN**

Run `test_event_sink_port.py`, Ruff both changed files, and `py_compile` both production files.

- [ ] **Step 5: Commit**

```bash
git add src/naumi_agent/runtime/ports tests/unit/test_event_sink_port.py
git commit -m "feat(runtime): add typed event sink contract [ARC-01.3e]"
```

### Task 3: Build deterministic Sink adapters

**Files:**
- Create: `src/naumi_agent/streaming/sinks.py`
- Create: `tests/unit/test_event_sinks.py`

**Interfaces:**
- Consumes: `EventSink`, `RuntimeEvent`, `LegacyEventCallback`, `thaw_event_data`.
- Produces: `NullEventSink`, `CallbackEventSink`, `CompositeEventSink`, `coerce_event_sink`.

- [ ] **Step 1: Write RED adapter tests**

Test structural Protocol conformance, falsey callback/sink preservation, empty/invalid Composite rejection, stable order, same object identity for typed sinks, independent thawed callback payload, ordinary failure short-circuit, cancellation propagation and slow-sink backpressure.

- [ ] **Step 2: Run RED and inspect expected import failure**

```bash
PYTHONPATH=src /Users/lv/Workspace/NaumiAgent/.venv/bin/python -m pytest -q tests/unit/test_event_sinks.py
```

- [ ] **Step 3: Implement adapters without tasks or queues**

```python
class CompositeEventSink:
    def __init__(self, sinks: Iterable[EventSink]) -> None:
        resolved = tuple(sinks)
        if not resolved or any(not isinstance(sink, EventSink) for sink in resolved):
            raise TypeError("组合 EventSink 必须包含完整的 EventSink 实现")
        self._sinks = resolved

    async def emit(self, event: RuntimeEvent) -> None:
        for sink in self._sinks:
            await sink.emit(event)
```

`CallbackEventSink.emit()` must merge metadata into a fresh payload and await the callback directly. `coerce_event_sink()` accepts EventSink first, then callable legacy callback, and otherwise raises Chinese `TypeError`.

- [ ] **Step 4: Run GREEN, Ruff and compile**

- [ ] **Step 5: Commit**

```bash
git add src/naumi_agent/streaming/sinks.py tests/unit/test_event_sinks.py
git commit -m "feat(streaming): add deterministic event sink adapters [ARC-01.3e]"
```

### Task 4: Implement the run-scoped RuntimeEventPublisher

**Files:**
- Create: `src/naumi_agent/streaming/publisher.py`
- Modify: `src/naumi_agent/streaming/__init__.py`
- Create: `tests/unit/test_runtime_event_publisher.py`

**Interfaces:**
- Consumes: `EventSink`, `RuntimeEvent`, `RuntimeEventType`, `LegacyEventCallback`.
- Produces: `RuntimeEventPublisher(sink, session_id, run_id)`, `publish(...)`, `legacy_callback()`.

- [ ] **Step 1: Write RED identity/order/concurrency tests**

Use a recording Sink with an async gate. Assert one run emits 1..N, separate runs each begin at 1, 50 concurrent publishers deliver sequence 1..50 in observed order, all ids are unique/non-empty, timestamps have offsets, and callback unknown names fail before the Sink.

- [ ] **Step 2: Run RED**

- [ ] **Step 3: Implement Publisher with one per-run lock**

```python
async def publish(self, event_type, data, *, turn=0):
    async with self._lock:
        self._sequence += 1
        event = RuntimeEvent.create(
            event_type=event_type,
            data=data,
            session_id=self._session_id,
            run_id=self._run_id,
            turn=turn,
            sequence=self._sequence,
        )
        await self._sink.emit(event)
        return event
```

Do not use `create_task`, Queue, sleep or a shared/global lock.

- [ ] **Step 4: Run GREEN plus cancellation and slow-sink nodes**

- [ ] **Step 5: Commit**

```bash
git add src/naumi_agent/streaming tests/unit/test_runtime_event_publisher.py
git commit -m "feat(streaming): add ordered runtime event publisher [ARC-01.3e]"
```

### Task 5: Inject EventSink into AgentEngine before runtime I/O

**Files:**
- Modify: `src/naumi_agent/orchestrator/engine.py`
- Create: `tests/unit/test_event_sink_injection.py`

**Interfaces:**
- Consumes: `EventSink`, `NullEventSink`.
- Produces: `AgentEngine(..., event_sink=...)` and read-only `engine.event_sink`.

- [ ] **Step 1: Write RED injection tests**

Cover default Null sink, recording sink identity, explicit falsey sink, incomplete sink Chinese failure before `.naumi` creation, and unchanged legacy `engine.emitter` availability.

- [ ] **Step 2: Run RED**

- [ ] **Step 3: Add keyword-only injection using `is None`**

Validate `EventSink` alongside existing Session/Permission/Model/Tool ports before `workspace_root` and runtime data initialization. Do not add `close()` because these Sink contracts have no owned lifecycle.

- [ ] **Step 4: Run GREEN, the four existing Port injection files, Ruff and compile**

- [ ] **Step 5: Commit**

```bash
git add src/naumi_agent/orchestrator/engine.py tests/unit/test_event_sink_injection.py
git commit -m "refactor(runtime): inject event sink port [ARC-01.3e]"
```

### Task 6: Compose recorder, Inspector and terminal delivery exactly once

**Files:**
- Modify: `src/naumi_agent/runs/recorder.py`
- Modify: `src/naumi_agent/inspector/tracker.py`
- Modify: `src/naumi_agent/orchestrator/engine.py`
- Modify: `tests/unit/test_run_receipts.py`
- Modify: `tests/unit/test_runtime_inspector.py`
- Create: `tests/unit/test_engine_event_pipeline.py`

**Interfaces:**
- Produces: `ChatRunRecorderEventSink`, `RuntimeInspectorEventSink`, Engine `_finish_streaming_run(...)` helper and one Publisher per `run_streaming()` call.

- [ ] **Step 1: Write RED terminal-pipeline tests**

Assert recorder→injected sink→caller sink order; identical event id/sequence in Inspector, recorder and caller; normal/error/cancel each persist exactly one receipt before delivery; a failing caller cannot erase the stored receipt; cancellation remains cancellation.

- [ ] **Step 2: Run only the new pipeline and named receipt/Inspector nodes**

- [ ] **Step 3: Implement typed recorder/Inspector adapters**

Each adapter thaws a fresh payload, then calls the existing `observe(event.type.value, payload)` method. Do not duplicate `_step_fields` or receipt building.

- [ ] **Step 4: Replace local `recorded_event()` with Composite + Publisher**

Keep public callback union only at `run_streaming` entry. Consolidate terminal receipt logic in one helper that calls `recorder.finish()` before `publisher.publish(COMPLETION_RECEIPT, ...)`.

- [ ] **Step 5: Run GREEN and real SQLite receipt persistence node**

- [ ] **Step 6: Commit**

```bash
git add src/naumi_agent/runs/recorder.py src/naumi_agent/inspector/tracker.py src/naumi_agent/orchestrator/engine.py tests/unit/test_run_receipts.py tests/unit/test_runtime_inspector.py tests/unit/test_engine_event_pipeline.py
git commit -m "refactor(runtime): compose durable event delivery [ARC-01.3e]"
```

### Task 7: Migrate all Engine event producers to RuntimeEventPublisher

**Files:**
- Modify: `src/naumi_agent/orchestrator/engine.py`
- Modify: `tests/unit/test_engine.py`
- Modify: `tests/unit/test_harness_evidence.py`
- Modify: `tests/unit/test_tool_batches.py`
- Modify: `tests/unit/test_tool_execution_port.py`

**Interfaces:**
- Consumes: `RuntimeEventPublisher.publish`, `legacy_callback`.
- Produces: Engine production code with no direct `await on_event(...)`.

- [ ] **Step 1: Add RED representative event-order tests**

Cover run→turn→thinking/response→receipt, tool_start/tool_end, permission_bubble, Harness knowledge/invalidation, runtime notification, recovery, task snapshot and two parallel tools. Assert exact event type, shared run/session, unique event id and contiguous sequence.

- [ ] **Step 2: Migrate one event family at a time**

Order commits inside the task only after each focused group is green: lifecycle/latency, model stream, tool/permission, Harness/task/recovery. Replace dynamic forwarding with `RuntimeEventType(event)`; do not use `getattr` or string fallback.

- [ ] **Step 3: Pass `publisher.legacy_callback()` only into ToolExecutionPort/Tool APIs**

- [ ] **Step 4: Run static audit**

```bash
rg -n 'await\s+on_event\s*\(' src/naumi_agent/orchestrator/engine.py
```

Expected: no production matches. Classify `on_event` union/coercion references separately.

- [ ] **Step 5: Run focused Engine/Harness/tool-batch tests, Ruff and compile**

- [ ] **Step 6: Commit**

```bash
git add src/naumi_agent/orchestrator/engine.py tests/unit/test_engine.py tests/unit/test_harness_evidence.py tests/unit/test_tool_batches.py tests/unit/test_tool_execution_port.py
git commit -m "refactor(runtime): publish typed engine events [ARC-01.3e]"
```

### Task 8: Centralize Tool, Agent, SubAgent and team callback compatibility

**Files:**
- Modify: `src/naumi_agent/runtime/ports/tool_execution.py`
- Modify: `src/naumi_agent/agents/base.py`
- Modify: `src/naumi_agent/agents/team_protocol.py`
- Modify: `src/naumi_agent/orchestrator/subagent_manager.py`
- Modify: `src/naumi_agent/tools/subagent.py`
- Modify: affected `tests/unit/test_agents.py`, `test_subagent_manager.py`, `test_team_protocol.py`, `test_subagent_tool.py`

**Interfaces:**
- Consumes: central `LegacyEventCallback`, Publisher legacy adapter.
- Produces: no locally redefined callback aliases and strict dynamic event-name validation.

- [ ] **Step 1: Write RED tests for unknown team/subagent events and production Publisher wiring**

- [ ] **Step 2: Replace duplicate aliases with central import**

Do not rename public Tool keyword `event_callback`; changing Tool schemas is out of scope.

- [ ] **Step 3: Prove Engine-created SubAgent events enter the same Publisher sequence**

- [ ] **Step 4: Run only affected Agent/SubAgent/team tests**

- [ ] **Step 5: Commit**

```bash
git commit -am "refactor(agents): centralize event callback adapters [ARC-01.3e]"
```

### Task 9: Migrate CLI, TUI and New UI to explicit CallbackEventSink

**Files:**
- Modify: `src/naumi_agent/main.py`
- Modify: `src/naumi_agent/cli/commands_meta.py`
- Modify: `src/naumi_agent/tui/app.py`
- Modify: `src/naumi_agent/ui/bridge.py`
- Modify: focused CLI/TUI/UI bridge tests.

**Interfaces:**
- Consumes: `CallbackEventSink` and unchanged existing handler functions.
- Produces: repository product calls `run_streaming(task, event_sink)` without relying on callback coercion.

- [ ] **Step 1: Write RED surface assertions**

Use recording handlers to prove each surface receives `event_id`, `run_id`, `session_id`, `sequence` and unchanged user payload. New UI must still create the same typed UIMessage.

- [ ] **Step 2: Wrap each existing handler at the call site**

Do not move rendering into EventSink and do not change colors/layout/Markdown in this task.

- [ ] **Step 3: Run named CLI, TUI and New UI bridge nodes only**

- [ ] **Step 4: Static audit repository product surfaces**

Expected direct callback calls exist only inside adapter/Tool compatibility files, not main/CLI/TUI/bridge.

- [ ] **Step 5: Commit**

```bash
git commit -am "refactor(ui): consume runtime event sink adapters [ARC-01.3e]"
```

### Task 10: Preserve event identity through SSE and WebSocket

**Files:**
- Modify: `src/naumi_agent/streaming/events.py`
- Modify: `src/naumi_agent/api/routes/messages.py`
- Modify: `src/naumi_agent/api/routes/ws.py`
- Modify: `tests/unit/test_streaming.py`
- Modify: `tests/unit/test_api.py`

**Interfaces:**
- Produces: `EventType.RUNTIME_EVENT`, StreamEvent `source_event/event_id/sequence`, explicit exhaustive RuntimeEvent transport mapping and `StreamEventSink`.

- [ ] **Step 1: Write RED parity tests**

Create one RuntimeEvent and feed SSE/WS adapters. Assert equal id, source_event, sequence and data. Parameterize all 31 RuntimeEventType values; mapped transport events retain existing EventType, remaining events use RUNTIME_EVENT with `{event, data}`. Assert no value becomes TURN_END by default.

- [ ] **Step 2: Extend StreamEvent backward-compatibly**

New fields have safe defaults so persisted/replayed version-1 payloads still parse. `to_dict/to_sse/to_ws` include fields only when non-empty/non-zero if existing clients require compact compatibility.

- [ ] **Step 3: Replace callback closures with StreamEventSink**

SSE queue and WebSocket send remain awaited. Do not route through legacy EventEmitter or add a second queue.

- [ ] **Step 4: Run focused streaming/API tests**

- [ ] **Step 5: Commit**

```bash
git add src/naumi_agent/streaming/events.py src/naumi_agent/api/routes/messages.py src/naumi_agent/api/routes/ws.py tests/unit/test_streaming.py tests/unit/test_api.py
git commit -m "refactor(api): preserve runtime event identity [ARC-01.3e]"
```

### Task 11: Complete static audit and focused acceptance

**Files:**
- Modify: `docs/development/architecture/ARC-01-3e-event-sink-design.md`
- Modify: `docs/development/architecture/ARC-01-domain-boundaries.md`

**Interfaces:**
- Produces: truthful implementation self-review and ARC-01.3 completion status.

- [ ] **Step 1: Classify every remaining callback/direct emission match**

Allowed: `CallbackEventSink`, `RuntimeEventPublisher.legacy_callback`, standalone Tool tests, Tool API compatibility. Forbidden: Engine, CLI, TUI, New UI and API transport direct callback invocation; free string event production; product use of `engine.emitter`.

- [ ] **Step 2: Run focused acceptance groups**

Run contract/sinks/publisher/injection; named Engine lifecycle/tool/permission/Harness/receipt nodes; Agent/SubAgent/team; CLI/TUI/New UI; streaming/API. Run these as separate small commands, never a full suite.

- [ ] **Step 3: Run Ruff and compile only changed Python files**

- [ ] **Step 4: Update design self-review and domain status**

Answer falsey/error/cancel, sequence/backpressure, receipt durability, transport parity and all-surface-path questions with test/static evidence. Mark ARC-01.3 complete only if no forbidden bypass remains.

- [ ] **Step 5: Commit**

```bash
git add docs/development/architecture/ARC-01-3e-event-sink-design.md docs/development/architecture/ARC-01-domain-boundaries.md
git commit -m "docs: record ARC-01.3e event sink audit"
```

### Task 12: Regenerate architecture artifacts and integrate main

**Files:**
- Modify: `docs/architecture/arc-01-import-graph-baseline.json`
- Modify: `docs/architecture/arc-01-domain-ownership.json`

**Interfaces:**
- Consumes: completed source/test/development-doc commit H1.
- Produces: deterministic artifacts bound to H1 and final merged/pushed main.

- [ ] **Step 1: Record H1 and generate each artifact twice**

Expected: 333 modules, ownership issues 0, new modules owned by runtime, SCC counts no greater than 0/1/2, no timestamps/absolute paths, matching import graph digest.

- [ ] **Step 2: Byte-compare both generations and official artifacts**

- [ ] **Step 3: Amend artifact files into H2**

Prove `git diff --quiet H1 H2 -- src tests docs/development` succeeds.

- [ ] **Step 4: Run architecture 40 tests and artifact regeneration cmp**

- [ ] **Step 5: Fetch origin and rebase if main advanced**

After a rebase, rerun EventSink contract/real Engine/architecture small groups.

- [ ] **Step 6: Fast-forward merge into main, repeat post-merge small verification, push and compare `ls-remote` SHA**

- [ ] **Step 7: Remove owned worktree, prune and delete merged feature branch**

## Self-review checklist

- [ ] Every design requirement maps to a numbered task.
- [ ] No task uses free-string events as the Runtime contract.
- [ ] No task silently drops or buffers events.
- [ ] All product surfaces migrate before ARC-01.3 is marked complete.
- [ ] Tool callback compatibility has an explicit static-audit allowance and ARC-01.5 removal path.
- [ ] Completion receipt durability and exactly-once event behavior cover normal/error/cancel/UI failure.
- [ ] API transport parity covers all 31 event types and prohibits TURN_END fallback.
- [ ] Every behavior-changing task starts with a witnessed RED test and ends with focused GREEN verification.
- [ ] No full test command, subagent dispatch, external model call or unrelated refactor appears in the plan.
