# Parallel Tool Batches Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Execute independent tool calls concurrently while preserving mutation barriers and provider message order.

**Architecture:** A pure scheduler partitions calls by Tool metadata. A bounded batch executor runs safe calls concurrently. AgentEngine delegates both streaming and non-streaming tool-call paths to one orchestration method.

**Tech Stack:** Python 3.14 asyncio TaskGroup, pydantic-settings, pytest.

## Global Constraints

- Only `concurrency_safe=True` tools run concurrently.
- Unsafe tools are strict serial barriers.
- Result messages retain original model call order.
- Default parallelism is 4; accepted range is 1 through 16.

---

### Task 1: Configuration and pure scheduler

**Files:**
- Modify: `src/naumi_agent/config/settings.py`
- Create: `src/naumi_agent/orchestrator/tool_batches.py`
- Test: `tests/unit/test_tool_batches.py`
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Produces: `SafetyConfig.max_parallel_tools: int = 4` constrained to 1..16.
- Produces: `ScheduledToolCall(index: int, call: ToolCall)` and `ToolBatch(calls: tuple[ScheduledToolCall, ...], parallel: bool)`.
- Produces: `build_tool_batches(calls, registry, max_parallel_tools) -> tuple[ToolBatch, ...]`.

- [ ] **Step 1: Write failing config boundary tests** for 0, 1, 4, and 17.
- [ ] **Step 2: Write failing scheduler tests** for safe-safe-unsafe-safe partitioning and max batch size.
- [ ] **Step 3: Implement** constrained config and deterministic batch partitioning.
- [ ] **Step 4: Run** config and scheduler tests plus ruff.

### Task 2: Bounded concurrent executor

**Files:**
- Modify: `src/naumi_agent/orchestrator/tool_batches.py`
- Test: `tests/unit/test_tool_batches.py`

**Interfaces:**
- Produces: `execute_tool_batch(batch, execute) -> tuple[ScheduledToolResult, ...]` where each result preserves index, call, value, and exception.

- [ ] **Step 1: Write failing barrier-based test** proving two safe calls enter before either is released.
- [ ] **Step 2: Write failing mixed-result test** proving one exception does not cancel the successful sibling.
- [ ] **Step 3: Implement** TaskGroup workers that capture ordinary exceptions and propagate cancellation.
- [ ] **Step 4: Run** scheduler tests and inspect elapsed wall time assertion.

### Task 3: Shared Engine tool orchestration

**Files:**
- Modify: `src/naumi_agent/orchestrator/engine.py`
- Test: `tests/unit/test_engine.py`

**Interfaces:**
- Consumes: `build_tool_batches()` and `execute_tool_batch()`.
- Produces: `AgentEngine._execute_tool_calls(calls, *, on_event=None, history) -> list[str]`.

- [ ] **Step 1: Write failing non-streaming test** with two safe delay tools and assert overlap plus original message order.
- [ ] **Step 2: Write failing streaming test** asserting per-call start/end events include `batch_id`, `batch_size`, and `parallel`.
- [ ] **Step 3: Extract** parse, repeat detection, Hook start/end, execution, event emission and tool-message append into the shared method.
- [ ] **Step 4: Replace** both sequential loops with the shared method while preserving skip semantics and budget checks.
- [ ] **Step 5: Run** focused Engine tests, then all `tests/unit/test_engine.py`.
- [ ] **Step 6: Commit** with `git commit -m "feat: execute safe tool calls in parallel"`.

### Task 4: Cross-module release gate

**Files:**
- Test: `tests/e2e/test_live_api.py`

**Interfaces:**
- Verifies the three modules together without changing their public contracts.

- [ ] **Step 1: Run targeted suites** for tasks, background, tool batches, Engine, bridge, and Node TUI.
- [ ] **Step 2: Run real scenarios**: Todo correction in temporary SQLite, completed background subprocess cleanup, and two concurrent read-only tools.
- [ ] **Step 3: Run full gates**: `.venv/bin/ruff check src/ tests/`, `.venv/bin/python -m pytest tests/ -x -q`, Node syntax and test scripts, and `git diff --check`.
- [ ] **Step 4: Push** the feature branch after every module commit and after final verification.

