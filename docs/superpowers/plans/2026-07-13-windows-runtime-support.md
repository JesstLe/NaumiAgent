# Windows Runtime Support Implementation Plan

**Goal:** Run every non-macOS NaumiAgent entry point in native Windows while keeping the current macOS/Linux behavior and Bash command contract.

**Architecture:** Add one platform shell adapter that selects Git for Windows Bash on Windows and preserves the existing POSIX shell path elsewhere. Route synchronous tools, background commands, and hooks through it. Add an idempotent PowerShell bootstrap and verify the real Kimi, CLI, API, and Node terminal UI paths.

**Tech stack:** Python 3.12, asyncio, Git for Windows Bash, PowerShell, uv, Node.js 20+, pytest, Ruff.

## Constraints

- Do not modify `apps/macos`.
- Do not store or print the Kimi credential.
- Do not fall back to `cmd.exe` for Bash-oriented Agent commands.
- Keep public tool names and command schemas stable.
- Keep POSIX process launch and process-group termination behavior stable.
- Work on `chore/windows-runtime-support`; do not push without approval.

## Task 1: Platform shell adapter

**Files:**

- Create: `src/naumi_agent/runtime/shell.py`
- Create: `src/naumi_agent/runtime/__init__.py`
- Create: `tests/unit/test_runtime_shell.py`

Steps:

1. Add failing tests for Windows Git Bash discovery precedence, WSL launcher rejection, explicit override validation, POSIX command preservation, Windows paths with spaces/Unicode, nonzero exit output, timeout cleanup, and process-tree termination command construction.
2. Implement a small immutable shell command description and discovery error type.
3. Implement Windows discovery through explicit override, Git installation roots, and validated PATH candidates.
4. Implement foreground and background process creation helpers.
5. Implement graceful and forced platform-aware process-tree termination.
6. Run focused tests and Ruff.

## Task 2: Route all shell consumers through the adapter

**Files:**

- Modify: `src/naumi_agent/tools/builtin.py`
- Modify: `src/naumi_agent/background/runner.py`
- Modify: `src/naumi_agent/hooks/shell_hook.py`
- Modify: `tests/unit/test_tool_registry.py`
- Modify: `tests/unit/test_background.py`
- Modify: `tests/unit/test_shell_hooks.py`

Steps:

1. Add integration tests proving `bash_run`, background tasks, and hooks receive Bash semantics on Windows.
2. Replace direct `asyncio.create_subprocess_shell` calls with the adapter.
3. Ensure timeout paths terminate processes before returning.
4. Keep output, exit-code, hook JSON, and background persistence contracts stable.
5. Run the focused unit and integration tests.

## Task 3: Windows bootstrap and local configuration

**Files:**

- Create: `scripts/windows/setup.ps1`
- Modify: `README.md`
- Modify: `config.yaml.example` only when a platform-neutral correction is needed
- Create: `tests/unit/test_windows_setup_script.py`

Steps:

1. Add static tests for required dependency checks, idempotent config creation, credential redaction, and Git Bash diagnostics.
2. Implement the PowerShell setup script without accepting or echoing a plaintext key argument.
3. Create `.venv` through `uv sync --extra dev`.
4. Generate ignored `config.yaml` only when absent, with `workspace_root: "."`, `allowed_dirs: ["."]`, loopback API binding, and browser daemon disabled.
5. Document persistent user-level `NAUMI_MODELS__API_KEY` setup and the need to open a new terminal.
6. Run the script twice to prove idempotence.

## Task 4: Quality and regression verification

Steps:

1. Run `uv run ruff check src tests`.
2. Run Node syntax and unit checks.
3. Run focused Windows tests.
4. Run `uv run pytest tests -x`, confirming macOS-only E2E tests are skipped on Windows.
5. Inspect all changes and run `git diff --check`.

## Task 5: Real runtime verification

Steps:

1. Persist the Kimi credential in the Windows user environment without displaying it and inject it into the current verification process.
2. Verify configuration loading reports a present key without printing it.
3. Execute a minimal real `naumi run` request and confirm a model response.
4. Start and cleanly exit CLI Chat.
5. Start `naumi serve`, request the real health endpoint, and stop the server.
6. Start `naumi ui`, confirm the Python bridge handshake or equivalent live runtime evidence, and stop it cleanly.
7. Confirm no key is present in tracked or ignored project files.

## Task 6: Compatibility audit and delivery

Steps:

1. Verify no file under `apps/macos` changed.
2. Verify tests cover both Windows and POSIX branches of the adapter.
3. Review failure messages and edge cases against the design.
4. Commit the implementation in coherent units.
5. Leave the branch local and report exact verification evidence and any remaining limitations.
