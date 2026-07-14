# Harness H1 Profile Loader and Doctor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan inline. Do not dispatch subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a secure, read-only Harness profile loader, persistent digest trust record, shared status/doctor service, manual slash commands, and Agent-readable diagnostic tools without executing any profile command.

**Architecture:** A new `naumi_agent.harness` domain owns strict Pydantic profile models, bounded YAML loading, workspace-contained path validation, and a SQLite trust store outside version-controlled profile data. `HarnessService` is the single facade used by two read-only Agent tools and `/harness` user commands; `AgentEngine` only constructs and registers it. H1 never injects profile text into model context and never runs configured checks.

**Tech Stack:** Python 3.12+, Pydantic v2, PyYAML `safe_load`, `aiosqlite`, SHA-256, pytest/pytest-asyncio.

## Global Constraints

- Execute inline only; the user explicitly prohibited further subagents.
- Implement only H1. Do not create H2 knowledge/context or H3 check-execution shells.
- `.naumi/harness.yaml` is untrusted repository input and may be modified by an Agent.
- Profile size is capped at 256 KiB; unknown fields, YAML object tags, unsupported schema versions, absolute paths, `..` escapes, symlink escapes, empty argv entries, duplicate check IDs, and invalid timeouts are rejected.
- Profile commands are argv arrays only. H1 displays them but never executes them.
- Trust is keyed by canonical workspace root plus the exact profile-byte SHA-256. Any content change invalidates trust.
- `/harness trust` only previews; `/harness trust --confirm` is the explicit user-only trust action. No Agent trust/untrust tools are registered.
- All visible errors are Chinese and include the next action; raw Pydantic, YAML, SQLite, or traceback text is not exposed.
- Do not run the full repository test suite. Run only H1, Engine registration, slash/completion, and a real-workspace doctor smoke test.
- Preserve the untracked `.superpowers/` directory.

---

### Task 1: Strict profile contracts and bounded loader

**Files:**
- Create: `src/naumi_agent/harness/__init__.py`
- Create: `src/naumi_agent/harness/models.py`
- Create: `src/naumi_agent/harness/profile.py`
- Create: `tests/unit/test_harness_profile.py`

**Interfaces:**
- Produces `HarnessProfile`, `HarnessCheckSpec`, `HarnessProfileSnapshot`, `HarnessProfileError`, `load_harness_profile(workspace_root, profile_path=None)`.
- `HarnessProfileSnapshot` always reports canonical workspace/profile paths and one of `missing`, `valid`, or `invalid`; valid snapshots include the exact raw-byte digest and parsed profile.

- [x] **Step 1: Write failing profile tests**

Cover a valid profile, missing file, empty file, >256 KiB file, unknown fields, unsupported schema, malicious YAML object tag, non-mapping root, duplicate check IDs, empty/string argv, absolute/parent paths, symlink escape, Unicode paths, and digest changes after one-byte edits.

```python
snapshot = load_harness_profile(workspace)
assert snapshot.status == HarnessProfileStatus.VALID
assert snapshot.profile.checks[0].argv == ("uv", "run", "pytest", "-q")
assert snapshot.digest == hashlib.sha256(profile_path.read_bytes()).hexdigest()
```

- [x] **Step 2: Verify RED**

Run: `uv run pytest -q tests/unit/test_harness_profile.py`

Expected: collection fails because `naumi_agent.harness.profile` does not exist.

- [x] **Step 3: Implement strict models and loader**

Use `ConfigDict(extra="forbid", frozen=True)`, tuple fields, field/model validators, `yaml.safe_load`, byte-size checks before decode, canonical `Path.resolve(strict=False)`, and a shared containment validator that resolves existing symlinks. Convert validation failures into stable `HarnessProfileError(code, message, hint)` records instead of leaking library exceptions.

- [x] **Step 4: Verify GREEN and Ruff**

Run: `uv run pytest -q tests/unit/test_harness_profile.py`

Run: `uv run ruff check src/naumi_agent/harness/models.py src/naumi_agent/harness/profile.py tests/unit/test_harness_profile.py`

Expected: all selected tests and Ruff pass.

---

### Task 2: Durable workspace-scoped trust

**Files:**
- Create: `src/naumi_agent/harness/trust.py`
- Create: `tests/unit/test_harness_trust.py`

**Interfaces:**
- Produces `HarnessTrustRecord` and `HarnessTrustStore(db_path)` with async `is_trusted(workspace_root, digest)`, `trust(workspace_root, digest, source)`, `get(workspace_root)`, and `untrust(workspace_root)`.
- SQLite table `harness_profile_trust` has canonical workspace root as primary key, current digest, UTC timestamp, and source.

- [x] **Step 1: Write failing trust tests**

Cover first trust, idempotent repeat, digest replacement, workspace isolation, untrust, database reopen persistence, invalid empty inputs, and concurrent trust writes.

```python
await store.trust(workspace, "a" * 64, source="user_slash")
assert await store.is_trusted(workspace, "a" * 64)
assert not await store.is_trusted(workspace, "b" * 64)
```

- [x] **Step 2: Verify RED**

Run: `uv run pytest -q tests/unit/test_harness_trust.py`

Expected: import failure for `naumi_agent.harness.trust`.

- [x] **Step 3: Implement the trust store**

Open short-lived `aiosqlite` connections, create the table idempotently, enable `busy_timeout`, use `INSERT ... ON CONFLICT DO UPDATE`, and serialize schema creation inside an async lock. Never store profile bodies or commands.

- [x] **Step 4: Verify GREEN and Ruff**

Run: `uv run pytest -q tests/unit/test_harness_trust.py`

Run: `uv run ruff check src/naumi_agent/harness/trust.py tests/unit/test_harness_trust.py`

Expected: persistence and concurrent-write tests pass.

---

### Task 3: Shared Harness service, doctor, and read-only Agent tools

**Files:**
- Create: `src/naumi_agent/harness/service.py`
- Create: `src/naumi_agent/harness/tools.py`
- Create: `tests/unit/test_harness_service.py`
- Create: `tests/unit/test_harness_tools.py`

**Interfaces:**
- Produces `HarnessService.status()`, `.doctor()`, `.trust()`, `.untrust()`, `render_harness_status()`, `render_harness_doctor()`, and `create_harness_tools(service)`.
- Registers only `harness_status` and `harness_doctor`; both have `read_only=True` and `concurrency_safe=True`.

- [x] **Step 1: Write failing service/tool tests**

Prove missing, invalid, valid-untrusted, trusted, and digest-changed states; doctor lists bounded command summaries and path findings but does not spawn a process; tools call the same service instance; no `harness_trust` tool exists.

```python
tools = create_harness_tools(service)
assert [tool.name for tool in tools] == ["harness_status", "harness_doctor"]
assert all(tool.metadata.read_only for tool in tools)
assert "下一步" in await tools[1].execute()
```

- [x] **Step 2: Verify RED**

Run: `uv run pytest -q tests/unit/test_harness_service.py tests/unit/test_harness_tools.py`

Expected: missing service/tool modules.

- [x] **Step 3: Implement service and renderers**

The service reloads profile bytes for every status/doctor/trust decision so stale digests cannot be trusted accidentally. Doctor emits structured findings for profile, trust, entrypoint paths, suite paths, check IDs/argv, and explicit H1 execution-disabled state. Renderers provide concise Chinese summaries and actionable next commands.

- [x] **Step 4: Implement read-only tools**

Both tools take no arguments, delegate directly to the service, and return renderer output. Do not expose mutation methods through ToolRegistry.

- [x] **Step 5: Verify GREEN and Ruff**

Run: `uv run pytest -q tests/unit/test_harness_service.py tests/unit/test_harness_tools.py`

Run: `uv run ruff check src/naumi_agent/harness/service.py src/naumi_agent/harness/tools.py tests/unit/test_harness_service.py tests/unit/test_harness_tools.py`

Expected: all selected tests pass and process-spawn sentinels remain untouched.

---

### Task 4: Engine registration and shared `/harness` user surface

**Files:**
- Modify: `src/naumi_agent/orchestrator/engine.py`
- Modify: `src/naumi_agent/main.py`
- Modify: `src/naumi_agent/cli/completer.py`
- Modify: `src/naumi_agent/ui/bridge.py`
- Modify: `frontend/terminal-ui/src/state.js`
- Create: `tests/unit/test_harness_surfaces.py`
- Modify: `tests/unit/test_cli_completer.py`
- Modify: `tests/unit/test_ui_bridge.py`
- Modify: `frontend/terminal-ui/test/state.test.js`

**Interfaces:**
- `AgentEngine.harness_service` uses `workspace_root` and `<session_db_parent>/harness-trust.db`.
- `/harness [status|doctor|trust [--confirm]|untrust]` delegates to that service.
- Completion registries expose `/harness` consistently in classic CLI, Textual TUI, and new terminal UI.

- [x] **Step 1: Write failing surface tests**

Assert Engine registers both read-only tools, `/harness status` and `/harness doctor` render service results, `/harness trust` previews without mutation, `--confirm` persists trust, `untrust` removes it, unknown/extra arguments show usage, and all completion registries contain `/harness`.

- [x] **Step 2: Verify RED**

Run: `uv run pytest -q tests/unit/test_harness_surfaces.py tests/unit/test_cli_completer.py tests/unit/test_ui_bridge.py -k 'harness'`

Run: `node --test --test-name-pattern 'harness' frontend/terminal-ui/test/state.test.js`

Expected: command/tool/completion entries are absent.

- [x] **Step 3: Wire service and tools into Engine**

Construct the service before `_register_builtin_tools()` and register only the two read-only Harness tools. Do not import `AgentEngine` from the Harness package.

- [x] **Step 4: Add the user-only command path**

Parse with `shlex.split`. Status is the default. `trust` without `--confirm` shows profile digest and all argv arrays; only the exact `trust --confirm` form writes trust. `untrust` is only reachable from the user slash command. Render through the existing Rich Markdown path.

- [x] **Step 5: Synchronize command metadata**

Add one `/harness` entry with `[status|doctor|trust|untrust]` hint to Python completers, bridge fallback metadata, and terminal UI state defaults.

- [x] **Step 6: Verify GREEN**

Run the Python and Node commands from Step 2 and require clean output.

---

### Task 5: Real NaumiAgent profile and H1 end-to-end proof

**Files:**
- Create: `.naumi/harness.yaml`
- Create: `tests/integration/test_harness_h1_real_workspace.py`
- Modify: `docs/superpowers/specs/2026-07-14-harness-engineering-design.md`

**Interfaces:**
- The repository profile declares `AGENTS.md`, `README.md`, and the Harness design as entrypoints plus H1-targeted lint/test argv commands.
- The integration test copies the real profile into a temporary real Git workspace, runs doctor, trusts its digest, mutates one byte, and proves trust invalidation without executing commands.

- [x] **Step 1: Write the failing real-workspace test**

Use actual filesystem, SQLite, YAML, and Git. Patch no subprocess API; instead install a sentinel that fails if H1 attempts command execution.

- [x] **Step 2: Verify RED**

Run: `uv run pytest -q tests/integration/test_harness_h1_real_workspace.py`

Expected: failure because the repository profile is absent.

- [x] **Step 3: Add the real profile**

Use schema version 1. Keep checks limited to `src/naumi_agent/harness`, H1 tests, and H1 surface tests. Do not configure a full-suite command.

- [x] **Step 4: Update design status**

Change the design status from draft to approved/in progress and record H1 implementation evidence. Do not claim H2-H7 completion.

- [x] **Step 5: Run focused acceptance checks**

Run: `uv run pytest -q tests/unit/test_harness_profile.py tests/unit/test_harness_trust.py tests/unit/test_harness_service.py tests/unit/test_harness_tools.py tests/unit/test_harness_surfaces.py tests/integration/test_harness_h1_real_workspace.py`

Run: `uv run ruff check src/naumi_agent/harness tests/unit/test_harness_*.py tests/integration/test_harness_h1_real_workspace.py`

Run: `node --test --test-name-pattern 'harness' frontend/terminal-ui/test/state.test.js`

Run: `git diff --check`

Expected: all focused tests pass; no full repository suite runs.

- [x] **Step 6: Real command smoke test**

Instantiate `HarnessService` against `/Users/lv/Workspace/NaumiAgent`, render status and doctor, confirm P95 target is comfortably below 2 seconds, trust a temporary copied profile database, mutate the copied profile, and confirm `trusted=False`. Never trust or execute the live repository profile as part of automation.

- [x] **Step 7: Self-review**

Check that H1 never executes configured commands, Agent tools cannot mutate trust, exact-byte changes invalidate trust, errors remain Chinese/actionable, paths are contained after symlink resolution, and no H2/H3 placeholders were introduced.

- [x] **Step 8: Commit feature branch**

```bash
git add .naumi/harness.yaml docs/superpowers/specs/2026-07-14-harness-engineering-design.md docs/superpowers/plans/2026-07-14-harness-profile-doctor.md src/naumi_agent/harness src/naumi_agent/orchestrator/engine.py src/naumi_agent/main.py src/naumi_agent/cli/completer.py src/naumi_agent/cli_completer.py src/naumi_agent/ui/bridge.py frontend/terminal-ui/src/state.js tests/unit/test_harness_*.py tests/unit/test_cli_completer.py tests/unit/test_ui_bridge.py frontend/terminal-ui/test/state.test.js tests/integration/test_harness_h1_real_workspace.py
git commit -m "feat: add trusted harness profile doctor"
git push origin main
```

## Plan Self-Review

- Spec coverage: H1 profile, trust, status/doctor, manual commands, read-only tools, digest invalidation, real workspace verification, and dual UI metadata are all assigned.
- Scope: H2 knowledge injection, H3 command execution, Eval, evidence storage, and long-running orchestration are explicitly excluded.
- Safety: trust mutation is not registered as an Agent tool; no profile command runs in H1; exact-byte digest and workspace containment are mandatory.
- Test fidelity: loader, SQLite, Git, filesystem, symlink, Engine registration, slash routing, and UI command metadata use real implementations; only command execution is guarded by a fail-fast sentinel because execution is forbidden in H1.
- User constraint: verification is focused and contains no full-suite command.
