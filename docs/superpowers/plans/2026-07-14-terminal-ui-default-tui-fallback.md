# Terminal UI Default Entry and Textual Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan inline, task by task. The user explicitly disabled subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retire the public Prompt Toolkit chat entry, make the Node Terminal UI the single default interactive frontend, and automatically fall back to Textual when the new UI cannot run.

**Architecture:** Keep Typer as the command/control entry layer, route all default interactive aliases through one Python launch coordinator, and keep Textual as the only explicit and automatic fallback. Preserve shared onboarding and slash-command modules even though the Prompt Toolkit frontend becomes unreachable. Make Node optional at install time because a working Textual path must survive on macOS, Linux, and Windows.

**Tech Stack:** Python 3.12+, Typer, subprocess, Rich, Textual, Node.js 20+, Bash, PowerShell, pytest, Ruff.

## Global Constraints

- Implement one feature slice only: default Terminal UI, Prompt Toolkit public-entry retirement, and Textual fallback.
- Do not delete old Prompt Toolkit CLI source, tests, or required dependencies. This is a durable user decision, not merely a constraint for this slice.
- Do not implement semantic rendering, loading animation, full documentation governance, or the complete cross-platform runtime matrix.
- All user-visible copy is Chinese; code comments and commit messages are English.
- Never include keys, environment dumps, full tracebacks, or raw child-process environments in launch errors.
- Return codes `0`, `130`, and `143` must never trigger Textual fallback.
- Only run the targeted launcher/script tests listed here; do not run the full Python or Node test suites.
- Preserve root `.superpowers/` and the untracked Ollama design document.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/naumi_agent/main.py` | Own the shared interactive launch coordinator and Typer command surface. |
| `tests/unit/test_terminal_ui_launcher.py` | Lock default aliases, explicit TUI, fallback state transitions, exit-code semantics, cwd, and config forwarding. |
| `scripts/install.sh` | Keep macOS/Linux install usable when Node/npm is absent or unsupported. |
| `tests/unit/test_install_script.py` | Statically enforce the portable install and updated command hints. |
| `scripts/windows/setup.ps1` | Keep Windows install usable through Textual when Node is unavailable and align alias copy. |
| `tests/unit/test_windows_setup_script.py` | Enforce Windows entry/fallback wording without requiring PowerShell on macOS. |
| `src/naumi_agent/cli/onboarding.py` | Stop recommending the retired Prompt Toolkit entry when Node is absent. |
| `tests/unit/test_onboarding.py` | Lock onboarding guidance to automatic/explicit Textual fallback. |
| `README.md` | Publish only current default/fallback commands. |
| `docs/terminal-ui-integration.md` | Describe the actual launch state machine and compatibility aliases. |

---

### Task 1: Add the shared launch coordinator

**Files:**
- Modify: `src/naumi_agent/main.py:185-186,492-502,527-635`
- Test: `tests/unit/test_terminal_ui_launcher.py`

**Interfaces:**
- Consumes: `_launch_terminal_ui(config_path: str, *, cwd: Path | None = None) -> int`, `_launch_tui(config_path: str) -> None`, `OutputGuardrail.redact(text: str) -> str`.
- Produces: `_launch_interactive_ui(config_path: str) -> int` and `_safe_launch_error(exc: BaseException) -> str`.

- [ ] **Step 1: Write the failing coordinator tests**

Add imports for `_launch_interactive_ui` and parameterized tests with this exact behavior:

```python
@pytest.mark.parametrize("returncode", [0, 130, 143])
def test_interactive_launcher_does_not_fallback_for_terminal_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
) -> None:
    tui_calls: list[str] = []
    monkeypatch.setattr(
        "naumi_agent.main._launch_terminal_ui",
        lambda _config: returncode,
    )
    monkeypatch.setattr(
        "naumi_agent.main._launch_tui",
        lambda config: tui_calls.append(config),
    )

    assert _launch_interactive_ui("project.yaml") == returncode
    assert tui_calls == []


@pytest.mark.parametrize(
    "failure",
    [TerminalUiLaunchError("未找到 Node.js"), OSError("spawn failed")],
)
def test_interactive_launcher_falls_back_once_for_launch_errors(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    tui_calls: list[str] = []

    def fail_terminal(_config: str) -> int:
        raise failure

    monkeypatch.setattr("naumi_agent.main._launch_terminal_ui", fail_terminal)
    monkeypatch.setattr(
        "naumi_agent.main._launch_tui",
        lambda config: tui_calls.append(config),
    )

    assert _launch_interactive_ui("project.yaml") == 0
    assert tui_calls == ["project.yaml"]


def test_interactive_launcher_falls_back_once_for_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tui_calls: list[str] = []
    monkeypatch.setattr("naumi_agent.main._launch_terminal_ui", lambda _config: 7)
    monkeypatch.setattr(
        "naumi_agent.main._launch_tui",
        lambda config: tui_calls.append(config),
    )

    assert _launch_interactive_ui("project.yaml") == 0
    assert tui_calls == ["project.yaml"]


def test_interactive_launcher_reports_tui_failure_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal_calls = 0
    tui_calls = 0
    output: list[str] = []

    def fail_terminal(_config: str) -> int:
        nonlocal terminal_calls
        terminal_calls += 1
        raise TerminalUiLaunchError("missing terminal assets")

    def fail_tui(_config: str) -> None:
        nonlocal tui_calls
        tui_calls += 1
        raise RuntimeError("tui failed with sk-abcdefghijklmnopqrstuvwxyz")

    monkeypatch.setattr("naumi_agent.main._launch_terminal_ui", fail_terminal)
    monkeypatch.setattr("naumi_agent.main._launch_tui", fail_tui)
    monkeypatch.setattr(
        "naumi_agent.main.console.print",
        lambda message: output.append(str(message)),
    )

    assert _launch_interactive_ui("project.yaml") == 1
    assert terminal_calls == 1
    assert tui_calls == 1
    assert "正在切换到 Textual TUI" in "\n".join(output)
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in "\n".join(output)
```

- [ ] **Step 2: Run the coordinator tests and verify RED**

Run:

```bash
.venv/bin/pytest tests/unit/test_terminal_ui_launcher.py \
  -k 'interactive_launcher' -q
```

Expected: collection/import failure because `_launch_interactive_ui` does not exist.

- [ ] **Step 3: Implement safe coordinator behavior**

Import the existing guardrail:

```python
from naumi_agent.safety.guardrails import OutputGuardrail
```

Add constants and helpers next to `TerminalUiLaunchError`:

```python
_TERMINAL_UI_NO_FALLBACK_EXIT_CODES = frozenset({0, 130, 143})


def _safe_launch_error(exc: BaseException) -> str:
    raw = str(exc).strip()
    first_line = raw.splitlines()[0] if raw else type(exc).__name__
    return OutputGuardrail.redact(first_line)[:300]
```

Replace `_exit_after_terminal_ui()` with:

```python
def _launch_interactive_ui(
    config_path: str,
) -> int:
    failure: str | None = None
    try:
        returncode = _launch_terminal_ui(config_path)
    except (TerminalUiLaunchError, OSError) as exc:
        failure = _safe_launch_error(exc)
    else:
        if returncode in _TERMINAL_UI_NO_FALLBACK_EXIT_CODES:
            return returncode
        failure = f"新终端 UI 异常退出（退出码 {returncode}）"

    console.print(f"[yellow]新终端 UI 启动失败：{failure}[/yellow]")
    console.print("[yellow]正在切换到 Textual TUI。[/yellow]")
    try:
        _launch_tui(config_path)
    except Exception as exc:
        console.print(
            "[red]Textual TUI 也无法启动："
            f"{_safe_launch_error(exc)}[/red]"
        )
        return 1
    return 0


def _exit_after_terminal_ui(config: str) -> None:
    raise typer.Exit(_launch_interactive_ui(config))
```

Do not catch `KeyboardInterrupt` or `SystemExit`; they are user/process control signals, not fallback failures.

- [ ] **Step 4: Run targeted coordinator tests and verify GREEN**

Run:

```bash
.venv/bin/pytest tests/unit/test_terminal_ui_launcher.py \
  -k 'interactive_launcher or launch_terminal_ui_preserves_invocation_cwd' -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit the coordinator**

```bash
git add src/naumi_agent/main.py tests/unit/test_terminal_ui_launcher.py
git commit -m "feat: fallback terminal UI to Textual"
```

---

### Task 2: Retire the classic public entry and normalize aliases

**Files:**
- Modify: `src/naumi_agent/main.py:145-170,452-524`
- Test: `tests/unit/test_terminal_ui_launcher.py`

**Interfaces:**
- Consumes: `_launch_interactive_ui(config_path: str) -> int`, `_launch_tui(config_path: str) -> None`.
- Produces: public `naumi tui`, root `--tui`, `chat --tui`, compatible `ui --legacy`, and consistent `naumiagent [--tui]`.

- [ ] **Step 1: Replace classic-entry tests with the new command contract**

Delete `test_chat_classic_uses_prompt_toolkit_cli` and add:

```python
def test_classic_prompt_toolkit_option_is_not_registered() -> None:
    root_result = runner.invoke(naumi_app, ["--classic"])
    chat_result = runner.invoke(naumi_app, ["chat", "--classic"])

    assert root_result.exit_code != 0
    assert chat_result.exit_code != 0
    assert "No such option" in root_result.output
    assert "No such option" in chat_result.output


@pytest.mark.parametrize(
    "args",
    [
        ["--tui", "--config", "root.yaml"],
        ["chat", "--tui", "--config", "chat.yaml"],
        ["tui", "--config", "tui.yaml"],
    ],
)
def test_explicit_tui_entries_bypass_terminal_ui(
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
) -> None:
    tui_calls: list[str] = []
    monkeypatch.setattr(
        "naumi_agent.main._launch_tui",
        lambda config: tui_calls.append(config),
    )
    monkeypatch.setattr(
        "naumi_agent.main._launch_interactive_ui",
        lambda _config: pytest.fail("explicit TUI must bypass Node UI"),
    )

    result = runner.invoke(naumi_app, args)

    assert result.exit_code == 0
    assert tui_calls == [args[-1]]


def test_naumiagent_defaults_to_terminal_ui_and_tui_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal_calls: list[str] = []
    tui_calls: list[str] = []
    monkeypatch.setattr(
        "naumi_agent.main._launch_interactive_ui",
        lambda config: terminal_calls.append(config) or 0,
    )
    monkeypatch.setattr(
        "naumi_agent.main._launch_tui",
        lambda config: tui_calls.append(config),
    )

    default_result = runner.invoke(naumiagent_app, [])
    tui_result = runner.invoke(naumiagent_app, ["--tui"])

    assert default_result.exit_code == 0
    assert tui_result.exit_code == 0
    assert terminal_calls == [DEFAULT_CONFIG_PATH]
    assert tui_calls == [DEFAULT_CONFIG_PATH]
```

Update the legacy alias test so `naumi ui --legacy` asserts a Chinese migration warning containing `naumi tui`.

- [ ] **Step 2: Run command-contract tests and verify RED**

Run:

```bash
.venv/bin/pytest tests/unit/test_terminal_ui_launcher.py \
  -k 'classic_prompt or explicit_tui or naumiagent_defaults or legacy_flag' -q
```

Expected: failures because `--classic` still exists, `naumi tui` is missing, and `naumiagent` still shows help.

- [ ] **Step 3: Implement the normalized Typer surface**

Make the root callback options exactly `config`, `tui`, and `version`; remove `classic` and `legacy`. If `tui` is true, call `_launch_tui(config)` and return. Otherwise call `_exit_after_terminal_ui(config)`.

Make `chat()` accept only `config` and `tui`; remove the `_chat()` branch.

Add the canonical Textual command:

```python
@app.command("tui")
def textual_ui(
    config: str = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c", help="配置文件路径"),
) -> None:
    """显式启动 Textual TUI fallback。"""
    _ensure_onboarding_ready(config)
    _launch_tui(config)
```

Keep `ui --legacy`, but print `[yellow]“--legacy” 已弃用，请改用 “naumi tui”。[/yellow]` before direct Textual launch.

Change `naumiagent_entry()` so no arguments call `_ensure_onboarding_ready(config)` then `_exit_after_terminal_ui(config)`, while `--tui` calls `_launch_tui(config)` directly. All user-facing help becomes Chinese.

Do not delete `_chat()` or any Prompt Toolkit source/test code. It must simply have no public caller. Later directory governance may isolate or label the legacy implementation but must preserve it.

- [ ] **Step 4: Run the complete launcher module**

Run:

```bash
.venv/bin/pytest tests/unit/test_terminal_ui_launcher.py -q
```

Expected: the complete small launcher module passes.

- [ ] **Step 5: Commit the public-entry retirement**

```bash
git add src/naumi_agent/main.py tests/unit/test_terminal_ui_launcher.py
git commit -m "refactor: retire classic interactive CLI"
```

---

### Task 3: Keep installers usable without Node

**Files:**
- Modify: `scripts/install.sh`
- Modify: `scripts/windows/setup.ps1`
- Modify: `src/naumi_agent/cli/onboarding.py:180-190`
- Test: `tests/unit/test_install_script.py`
- Test: `tests/unit/test_windows_setup_script.py`
- Test: `tests/unit/test_onboarding.py:229-244`

**Interfaces:**
- Consumes: canonical commands `naumi` and `naumi tui` from Task 2.
- Produces: Node-optional macOS/Linux and Windows installation with accurate fallback guidance.

- [ ] **Step 1: Write failing script-contract assertions**

Replace `test_install_script_requires_supported_node_for_default_ui` and the classic/legacy assertions in `test_install_script.py` with:

```python
def test_install_script_keeps_textual_available_without_node() -> None:
    script = _script()

    assert 'terminal_ui_available=0' in script
    assert 'terminal_ui_available=1' in script
    assert 'if [ "$terminal_ui_available" = 1 ]' in script
    assert "将使用 Textual TUI fallback" in script
    assert 'log_info "  naumi tui"' in script
    assert "naumi chat --classic" not in script
    assert "naumi ui --legacy" not in script
```

Remove the `Require-Command "node"` assertion from `test_setup_script_checks_required_runtimes`, then add:

```python
def test_setup_script_keeps_textual_available_without_node() -> None:
    script = _script()

    assert 'Get-Command "node" -ErrorAction SilentlyContinue' in script
    assert 'Write-Warning "未检测到 Node.js 20+' in script
    assert 'Get-Command "naumi" -ErrorAction SilentlyContinue' in script
    assert 'Write-Host "  默认入口:  naumi"' in script
    assert 'Write-Host "  Textual:  naumi tui"' in script
    assert 'Windows compatibility alias:  naumiagent --tui' not in script
```

Update the final-hint assertions in `test_setup_script_checks_required_runtimes` to the same `默认入口` / `Textual` copy. Keep all existing secret, UTF-8 BOM, Git Bash, and config assertions unchanged.

Replace `test_node_check_recommends_only_explicit_legacy_fallbacks` in `test_onboarding.py` with:

```python
def test_node_check_recommends_automatic_and_explicit_textual_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = StringIO()
    monkeypatch.setattr(onboarding, "console", Console(file=output, force_terminal=False))
    monkeypatch.setattr(onboarding.shutil, "which", lambda _name: None)

    onboarding._check_node_ui(tmp_path)

    text = output.getvalue()
    assert "自动回退到 Textual TUI" in text
    assert "naumi tui" in text
    assert "naumi chat --classic" not in text
    assert "naumi ui --legacy" not in text
```

- [ ] **Step 2: Run script tests and verify RED**

Run:

```bash
.venv/bin/pytest \
  tests/unit/test_install_script.py \
  tests/unit/test_windows_setup_script.py \
  tests/unit/test_onboarding.py \
  -k 'install_script or setup_script or node_check' -q
```

Expected: failures on the old hard Node requirement and old classic/legacy hints.

- [ ] **Step 3: Update `scripts/install.sh` without weakening shell safety**

Replace the hard Node block with one `terminal_ui_available` state:

```bash
terminal_ui_available=0
if command -v node >/dev/null 2>&1; then
    node_version=$(node -p 'process.versions.node')
    node_major=${node_version%%.*}
    if [ "$node_major" -ge 20 ] && command -v npm >/dev/null 2>&1; then
        terminal_ui_available=1
        log_info "检测到 Node.js $node_version"
    else
        log_warn "Node.js 20+ 与 npm 不完整，将使用 Textual TUI fallback。"
    fi
else
    log_warn "未检测到 Node.js 20+，将使用 Textual TUI fallback。"
fi

if [ "$terminal_ui_available" = 1 ]; then
    ui_dir="$INSTALL_DIR/frontend/terminal-ui"
    log_info "安装 Node UI 依赖..."
    (cd "$ui_dir" && npm install --no-audit --no-fund)
    log_info "Node UI 依赖安装完成"
fi
```

End with only `naumi` and `naumi tui`, plus one line that the default entry automatically falls back.

- [ ] **Step 4: Update `scripts/windows/setup.ps1` while preserving its UTF-8 BOM**

Replace hard `Require-Command "node"` with this exact optional-runtime block, while keeping Python, uv, Git Bash, config, Keychain/environment and browser validation unchanged:

```powershell
$node = Get-Command "node" -ErrorAction SilentlyContinue
$npm = Get-Command "npm" -ErrorAction SilentlyContinue
$terminalUiAvailable = $false
$nodeVersion = "未安装"
if ($node -and $npm) {
    $nodeVersion = (& $node.Source --version 2>&1 | Select-Object -First 1)
    if ($nodeVersion -match "^v(?<major>\d+)" -and [int]$Matches.major -ge 20) {
        $terminalUiAvailable = $true
    } else {
        Write-Warning "Node.js 20+ 不可用，将使用 Textual TUI fallback。当前版本：$nodeVersion"
    }
} else {
    Write-Warning "未检测到 Node.js 20+ 与 npm，将使用 Textual TUI fallback。"
}
```

Print `Node.js: $nodeVersion` and `Terminal UI available: $terminalUiAvailable` in the runtime summary. Do not throw for either Node or npm.

After `uv tool install`, resolve both public commands:

```powershell
$naumi = Get-Command "naumi" -ErrorAction SilentlyContinue
$naumiagent = Get-Command "naumiagent" -ErrorAction SilentlyContinue
if (-not $naumi -or -not $naumiagent) {
    throw "NaumiAgent 已安装，但 naumi/naumiagent 不在 PATH。请运行 uv tool update-shell 后重新打开终端。"
}
Write-Host "  naumi: $($naumi.Source)"
Write-Host "  naumiagent: $($naumiagent.Source)"
```

Update `_check_node_ui()`'s missing-Node copy to exactly:

```python
console.print(
    "\n[yellow]未检测到 Node.js 20+，新 Terminal UI 暂不可用。[/yellow]"
)
console.print("默认入口会自动回退到 Textual TUI，也可直接执行 naumi tui。")
```

Keep the existing optional npm-install flow when Node is present; do not delete onboarding code.

Write the edited PowerShell file with its existing UTF-8 BOM. Final hints must be:

```powershell
Write-Host "  默认入口:  naumi"
Write-Host "  Textual:  naumi tui"
Write-Host "  兼容别名:  naumiagent"
Write-Host "  API:  uv run naumi serve"
```

- [ ] **Step 5: Run targeted script verification**

Run:

```bash
bash -n scripts/install.sh
.venv/bin/pytest \
  tests/unit/test_install_script.py \
  tests/unit/test_windows_setup_script.py \
  tests/unit/test_onboarding.py \
  -k 'install_script or setup_script or node_check' -q
```

Expected: Bash syntax and both small test modules pass. Do not claim live PowerShell execution because `pwsh` is not installed on this machine.

- [ ] **Step 6: Commit installer behavior**

```bash
git add scripts/install.sh scripts/windows/setup.ps1 \
  src/naumi_agent/cli/onboarding.py \
  tests/unit/test_install_script.py tests/unit/test_windows_setup_script.py \
  tests/unit/test_onboarding.py
git commit -m "fix: preserve Textual fallback without Node"
```

---

### Task 4: Update executable entry documentation and verify the slice

**Files:**
- Modify: `README.md:7-18,90-100,136-167,185-200`
- Modify: `docs/terminal-ui-integration.md:100-130`
- Test: `tests/unit/test_install_script.py`

**Interfaces:**
- Consumes: final command contract and fallback behavior from Tasks 1-3.
- Produces: current, non-contradictory startup documentation and final verification evidence.

- [ ] **Step 1: Add a failing README contract**

Extend `test_readme_declares_terminal_ui_as_default_entry()`:

```python
assert "启动失败时自动回退到 Textual TUI" in readme
assert "naumi tui" in readme
assert "naumi chat --classic" not in readme
assert "Prompt Toolkit 兼容 CLI" not in readme
```

- [ ] **Step 2: Run the README contract and verify RED**

Run:

```bash
.venv/bin/pytest tests/unit/test_install_script.py \
  -k 'readme_declares' -q
```

Expected: failure because README still advertises Prompt Toolkit and manual legacy fallback.

- [ ] **Step 3: Update only entry-related current documentation**

In README:

- state that `naumi` defaults to the Node Terminal UI and automatically falls back to Textual;
- show `naumi tui` as the explicit fallback;
- remove `naumi chat --classic`, `naumi ui --legacy`, and Prompt Toolkit from the current feature list;
- document `chat`, `ui`, `--tui`, `ui --legacy`, and `naumiagent` as temporary aliases without presenting them as separate frontends;
- state that Node 20+ is required for the new UI but not for Textual fallback.

In `docs/terminal-ui-integration.md`, replace manual fallback instructions with the exact state machine: `0/130/143` do not fall back; preflight/spawn/other nonzero failures fall back once; explicit `naumi tui` bypasses Node.

Do not rewrite historical roadmap or course documents in this commit; the next documentation-governance slice owns them.

- [ ] **Step 4: Run the complete targeted acceptance set**

Run exactly:

```bash
.venv/bin/pytest \
  tests/unit/test_terminal_ui_launcher.py \
  tests/unit/test_install_script.py \
  tests/unit/test_windows_setup_script.py \
  tests/unit/test_onboarding.py -q

.venv/bin/ruff check \
  src/naumi_agent/main.py \
  src/naumi_agent/cli/onboarding.py \
  tests/unit/test_terminal_ui_launcher.py \
  tests/unit/test_install_script.py \
  tests/unit/test_windows_setup_script.py \
  tests/unit/test_onboarding.py

.venv/bin/python -m py_compile \
  src/naumi_agent/main.py \
  src/naumi_agent/cli/onboarding.py
bash -n scripts/install.sh
git diff --check
```

Expected: all selected tests pass; Ruff, py_compile, Bash syntax, and diff check are clean. Do not run `pytest tests/`, `ruff check src/`, or the Node full suite.

- [ ] **Step 5: Run a real PTY fallback smoke**

Run this exact smoke from the repository root:

```bash
.venv/bin/python - <<'PY'
from __future__ import annotations

import os
import pty
import select
import signal
import subprocess
import tempfile
import time
from pathlib import Path

root = Path.cwd()
with tempfile.TemporaryDirectory(prefix="naumi-tui-fallback-") as raw_tmp:
    tmp = Path(raw_tmp)
    fake_bin = tmp / "bin"
    fake_bin.mkdir()
    node = fake_bin / "node"
    node.write_text(
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = \"--version\" ]; then\n"
        "  printf 'v20.11.1\\n'\n"
        "  exit 0\n"
        "fi\n"
        "exit 7\n",
        encoding="utf-8",
    )
    node.chmod(0o755)

    config = tmp / "config.yaml"
    config.write_text(
        "models:\n"
        "  provider: smoke\n"
        "  default_model: openai/smoke\n"
        "  fast_model: openai/smoke\n"
        "  reasoning_model: openai/smoke\n"
        "  api_base: http://127.0.0.1:9/v1\n"
        "memory:\n"
        "  session_db_path: data/sessions.db\n"
        "  vector_db_path: data/chroma\n"
        "  long_term_enabled: false\n"
        "workspace_root: .\n"
        "safety:\n"
        "  permission_mode: bypass\n"
        "  allowed_dirs: ['.']\n"
        "  max_budget_usd: null\n"
        "  max_turns: 50\n"
        "browser_daemon:\n"
        "  enabled: false\n"
        "log_level: INFO\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["NAUMI_MODELS__API_KEY"] = "smoke-placeholder-only"
    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(
        [str(root / ".venv/bin/naumi"), "--config", str(config)],
        cwd=tmp,
        env=env,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        start_new_session=True,
    )
    os.close(slave_fd)
    output = bytearray()
    deadline = time.monotonic() + 15
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([master_fd], [], [], 0.2)
            if readable:
                try:
                    output.extend(os.read(master_fd, 65536))
                except OSError:
                    break
            if (
                "正在切换到 Textual TUI".encode() in output
                and b"NaumiAgent" in output
            ):
                break
            if proc.poll() is not None:
                break
        assert "正在切换到 Textual TUI".encode() in output, output.decode(errors="replace")
        assert b"NaumiAgent" in output, output.decode(errors="replace")
    finally:
        if proc.poll() is None:
            os.write(master_fd, b"\x03")
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=2)
        os.close(master_fd)

print("terminal-ui-textual-fallback-smoke: ok")
PY
```

The smoke must use the real `_launch_terminal_ui` subprocess path and the real `_launch_tui` implementation. It may use a temporary local config with no secret and must not write to the project `.naumi` directory.

- [ ] **Step 6: Perform the required self-review**

Check:

- `rg -n -- "--classic|Prompt Toolkit 兼容 CLI" README.md scripts/install.sh scripts/windows/setup.ps1 src/naumi_agent/main.py` returns no public registration or recommendation;
- normal/signal exits do not fallback;
- fallback is single-shot and errors are redacted;
- explicit Textual does not probe Node;
- Windows BOM remains `EF BB BF`;
- unrelated Ollama and `.superpowers/` files remain untracked and unstaged.

Record the intentional compatibility boundary honestly: Prompt Toolkit implementation files, tests, and required dependencies remain by explicit user decision even though the public entry is retired; Windows PowerShell was statically verified only on the current macOS host.

- [ ] **Step 7: Commit documentation and evidence**

```bash
git add README.md docs/terminal-ui-integration.md tests/unit/test_install_script.py
git commit -m "docs: document terminal UI automatic fallback"
```

- [ ] **Step 8: Merge and push**

Fast-forward `codex/terminal-ui-default-fallback` into `main`, rerun the same targeted acceptance set on merged `main`, then:

```bash
git push origin main
git rev-parse HEAD
git rev-parse origin/main
```

Expected: both hashes match and `git status --short` contains only the preserved root `.superpowers/` and Ollama design document.

---

## Plan Self-Review

- Spec coverage: public entry retirement, automatic fallback, signal exits, explicit Textual, installer behavior, safe errors, current docs, and targeted verification each have an owning task.
- Scope: semantic rendering, animation, full docs cleanup, and the full platform matrix remain outside this slice. Prompt Toolkit physical deletion is prohibited by the user's explicit decision.
- Type consistency: all tasks use `_launch_interactive_ui(config_path: str) -> int` and existing `_launch_tui(config_path: str) -> None`.
- Verification: no full Python or Node suite appears; the acceptance commands are limited to three unit modules plus narrow lint/compile/syntax checks and one real PTY smoke.
- No placeholder steps or unspecified error handling remain.
