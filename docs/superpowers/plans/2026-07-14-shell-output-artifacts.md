# Shell Output Artifacts Implementation Plan

> **For agentic workers:** Execute each task in order with test-driven development. This plan intentionally uses only targeted tests; do not run the full suite.

**Goal:** Preserve complete foreground Shell output without flooding model context.

**Architecture:** A managed `ShellOutputStore` streams merged subprocess output to a private runtime log. `BashRunTool` returns full small output or a bounded head/tail summary plus a recoverable path for large output. Engine injects the production runtime directory.

**Tech Stack:** Python 3.14, asyncio subprocesses, pathlib, pytest, Ruff.

## Global Constraints

- Do not change `background_run` behavior in this slice.
- Do not hold unbounded stdout/stderr in memory.
- Do not silently discard any large output.
- Do not run full tests.
- All user-visible messages are Chinese; code comments are English.

---

### Task 1: Managed foreground Shell output store

**Files:**
- Create: `src/naumi_agent/runtime/shell_output.py`
- Test: `tests/unit/test_shell_output.py`

**Interfaces:**
- Produces: `ShellOutputStore.allocate() -> ShellOutputArtifact`.
- Produces: `ShellOutputStore.summarize(artifact) -> ShellOutputSummary`.
- Produces: `ShellOutputStore.discard(artifact)` and `prune()`.

- [ ] Write failing tests for exclusive private allocation, small-output cleanup, head/tail large-output summary, invalid UTF-8, count/age pruning, symlink refusal, and active artifact preservation.
- [ ] Run `NAUMI_MODELS__API_KEY=unit-test-placeholder .venv/bin/python -m pytest tests/unit/test_shell_output.py -q` and confirm the missing module failure.
- [ ] Implement the smallest store that satisfies the tests with 50,000-byte inline limit, 24,000-byte head/tail windows, 7-day retention, and 100-artifact cap.
- [ ] Run the same module and `.venv/bin/ruff check src/naumi_agent/runtime/shell_output.py tests/unit/test_shell_output.py`.

### Task 2: Stream BashRunTool output into the store

**Files:**
- Modify: `src/naumi_agent/tools/builtin.py`
- Modify: `tests/unit/test_tool_registry.py`

**Interfaces:**
- `BashRunTool(..., output_dir: str | Path | None = None)` consumes `ShellOutputStore`.
- `bash_run` keeps its public tool name and command/cwd/timeout arguments.

- [ ] Add failing tests for small output compatibility, large output path and tail recovery, nonzero exit with output, timeout output preservation, cwd escape, invalid timeout, and log allocation failure.
- [ ] Run `NAUMI_MODELS__API_KEY=unit-test-placeholder .venv/bin/python -m pytest tests/unit/test_tool_registry.py -k BashRunTool -q` and confirm failures.
- [ ] Replace PIPE/`communicate()` capture with merged streaming to the allocated log; preserve existing process-tree cleanup and background-syntax guidance.
- [ ] Format truthful Chinese status and recovery metadata for every terminal outcome.
- [ ] Run the BashRunTool subset and targeted Ruff.

### Task 3: Production wiring and real scenario

**Files:**
- Modify: `src/naumi_agent/tools/builtin.py`
- Modify: `src/naumi_agent/orchestrator/engine.py`
- Test: `tests/unit/test_engine.py`

**Interfaces:**
- `create_builtin_tools(workspace_root, *, shell_output_dir=None)` forwards the directory.
- Engine passes `_runtime_data_dir / "shell-output"`.

- [ ] Add a focused Engine registration assertion that `bash_run` uses the configured runtime output directory.
- [ ] Implement the injection without changing other built-in tool construction.
- [ ] Run `NAUMI_MODELS__API_KEY=unit-test-placeholder .venv/bin/python -m pytest tests/unit/test_engine.py -k 'builtin or shell_output' -q`.
- [ ] Run targeted import smoke and Ruff for changed Python files.
- [ ] Execute a real command that emits more than 50,000 bytes with a unique tail marker; verify the summary, stored byte count, tail marker, and recoverable file.
- [ ] Self-review security, cleanup, memory bounds, timeout behavior, and Chinese UX.
- [ ] Commit with `git commit -m "feat: preserve complete shell output"`.

