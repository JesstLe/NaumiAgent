# Windows Runtime Support Design

## Summary

NaumiAgent will support native Windows development for every non-macOS entry point while preserving the existing macOS and Linux behavior. Python, uv, and Node.js run natively on Windows. Agent-generated shell commands run through Git for Windows Bash so the existing Bash-oriented prompts and command contracts remain valid.

The supported Windows entry points are:

- `naumi chat`
- `naumi ui`
- `naumi serve`
- non-interactive commands such as `naumi run`

The Swift application under `apps/macos` is outside this work.

## Goals

1. Install the development environment into the repository-local `.venv` with uv.
2. Run CLI Chat, the Node terminal UI, and the REST API on Windows.
3. Preserve the existing Bash command contract for tools, hooks, and background tasks.
4. Keep macOS and Linux runtime behavior unchanged.
5. Keep the Kimi API key outside the repository and outside local configuration files.
6. Provide repeatable Windows setup and diagnostics rather than relying on undocumented manual steps.

## Non-goals

- Building, running, or modifying the native Swift application.
- Rewriting Agent shell prompts or commands for PowerShell.
- Moving the project into WSL.
- Enabling the optional browser debugging daemon during the initial Windows bring-up.
- Publishing the branch or changing remote infrastructure.

## Runtime Architecture

### Native host processes

Python 3.12, uv, Node.js 20 or newer, SQLite, ChromaDB, CLI rendering, the terminal UI, and FastAPI run as native Windows processes. This keeps paths, editors, browsers, and project files in one Windows environment.

### Shell adapter

A focused platform shell adapter owns shell discovery, command construction, process launch, and process-tree termination.

On Windows it:

1. Honors an explicit Git Bash executable override when configured.
2. Discovers Bash from the Git for Windows installation.
3. Uses a PATH candidate only when it is Git Bash, rejecting the Windows WSL launcher.
4. executes commands as `bash.exe -lc <command>` with the requested Windows working directory.
5. terminates the complete child process tree on cancellation, timeout, or forced shutdown.

On macOS and Linux it delegates to the current POSIX shell and process-group behavior. Platform branching stays inside the adapter so callers do not accumulate operating-system checks.

The following consumers use the adapter:

- `bash_run`
- background command execution
- configured shell hooks

### Data flow

The launch path is:

```text
PowerShell launcher
  -> native Windows Python or Node.js
  -> NaumiAgent configuration and engine
  -> platform shell adapter
  -> Git Bash for Agent shell commands
```

CLI Chat, the Node terminal UI bridge, and the REST API share the same Python environment and configuration.

## Local Configuration

The setup creates `.venv` with:

```powershell
uv sync --extra dev
```

It creates an ignored `config.yaml` from the repository example with these Windows-safe values:

- Kimi Coding remains the configured model provider.
- `workspace_root` is `.`.
- `safety.allowed_dirs` contains `.` so the permission layer resolves the active workspace without a Unix-only path.
- the API binds to `127.0.0.1:8765`.
- the external browser daemon is disabled for the initial bring-up.

The local configuration contains no API key.

## Credential Handling

The Kimi credential is stored in the Windows user environment as `NAUMI_MODELS__API_KEY`. Setup and diagnostics report only whether the variable exists. They never print its value.

The configuration file, `.env`, scripts, logs, tests, and Git commits must not contain the credential. The setup process injects the same value into its current child-process environment for immediate verification because a newly persisted Windows user variable is not automatically visible to an already-running parent process.

Because the initial credential was shared through a chat channel, it should be rotated in the Kimi console after setup validation.

## Windows Setup and Diagnostics

A reusable PowerShell setup command will:

1. Verify Python 3.12 or newer, uv, Node.js 20 or newer, and Git for Windows Bash.
2. Create or update `.venv` with the development dependencies.
3. Create `config.yaml` only when it does not already exist.
4. Verify that the user-level Kimi environment variable exists without displaying it.
5. Run configuration and launcher diagnostics with actionable Chinese error messages.

The script must be idempotent. Existing user configuration is preserved.

## Error Handling

- Missing Git Bash produces a clear installation or override instruction; execution does not silently fall back to `cmd.exe`.
- The WSL `C:\Windows\System32\bash.exe` launcher is not accepted as Git Bash.
- Paths with spaces, Chinese characters, and backslashes are covered by command-launch tests.
- Shell results preserve exit status, stdout, and stderr.
- Timeout, Ctrl+C, and cancellation clean up the Windows process tree.
- Missing credentials and unsupported runtime versions fail before an interactive interface is launched.
- macOS and Linux keep their existing shell and signal handling.

## Verification

The implementation is complete only when all applicable checks pass:

1. `uv sync --extra dev`
2. `uv run ruff check src tests`
3. focused unit tests for shell discovery, quoting, working directories, failures, timeouts, and process cleanup
4. `uv run pytest tests -x`, with macOS-only end-to-end tests explicitly skipped on Windows
5. one minimal real Kimi request through `naumi run`
6. CLI Chat startup and controlled exit
7. REST API startup and successful health request
8. Node terminal UI startup, Python bridge handshake, and controlled exit
9. final Git and filesystem checks proving that no plaintext credential is tracked or left in project configuration

## Compatibility and Delivery

Implementation is isolated on `chore/windows-runtime-support`. Windows-only behavior is selected at runtime and is covered by platform-specific tests. The macOS Swift tree is untouched. No branch is pushed without explicit approval.

The expected result is one repository that keeps its existing macOS workflow and can also be installed, launched, and tested from native Windows with Git Bash providing the established shell semantics.
