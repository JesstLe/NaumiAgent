# Launch Workspace Binding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. The user explicitly prohibited subagents.

**Goal:** Make every interactive Naumi launch use its startup directory as the active workspace without persisting that directory during onboarding.

**Architecture:** Add one `AppConfig.bind_runtime_workspace()` normalization boundary and invoke it before constructing an Engine in the Node bridge and Textual fallback. Onboarding emits relative workspace defaults, while non-interactive API and deployment entrypoints retain their explicit configuration behavior.

**Tech Stack:** Python 3.12+, Pydantic Settings, Typer, pytest, YAML

## Global Constraints

- Do not run the full test suite; run only config, onboarding, bridge, TUI launch, and permission tests named below.
- Do not change the bypass short-circuit: bypass must allow every directory without confirmation.
- Do not rewrite an existing `.naumi/config.yaml` during launch.
- User-visible text is Chinese; code comments and commit messages are English.
- Produce one implementation commit for this feature.

---

### Task 1: Bind interactive runtime workspace to the launch directory

**Files:**
- Modify: `src/naumi_agent/config/settings.py`
- Modify: `src/naumi_agent/cli/onboarding.py`
- Modify: `src/naumi_agent/main.py`
- Modify: `src/naumi_agent/ui/bridge.py`
- Modify: `tests/unit/test_config.py`
- Modify: `tests/unit/test_onboarding.py`
- Modify: `tests/unit/test_ui_bridge.py`
- Modify: `tests/unit/test_permissions.py`
- Modify: `README.md`
- Modify: `config.yaml.example`

**Interfaces:**
- Produces: `AppConfig.bind_runtime_workspace(launch_dir: str | Path | None = None) -> Path`
- Consumes: `Path.cwd()`, existing `AppConfig.workspace_root`, and `SafetyConfig.allowed_dirs`
- Guarantees: the returned path, `config.workspace_root`, the bridge ready payload, and `AgentEngine.workspace_root` describe the same launch directory

- [ ] **Step 1: Write failing config normalization tests**

Add tests equivalent to:

```python
def test_bind_runtime_workspace_replaces_legacy_workspace_and_preserves_extra_dir(
    tmp_path, monkeypatch
) -> None:
    legacy = tmp_path / "legacy"
    launch = tmp_path / "launch"
    shared = tmp_path / "shared"
    for path in (legacy, launch, shared):
        path.mkdir()
    config = AppConfig(
        workspace_root=str(legacy),
        safety={"allowed_dirs": [str(legacy), str(shared), str(shared)]},
    )

    result = config.bind_runtime_workspace(launch)

    assert result == launch.resolve()
    assert config.workspace_root == str(launch.resolve())
    assert config.safety.allowed_dirs == [str(launch.resolve()), str(shared.resolve())]
```

Also cover a missing launch directory raising a Chinese `ValueError` and `None` using `Path.cwd()`.

- [ ] **Step 2: Run config tests and confirm the missing method fails**

Run:

```bash
uv run python -m pytest tests/unit/test_config.py -k 'bind_runtime_workspace' -q
```

Expected: failure because `AppConfig` has no `bind_runtime_workspace` method.

- [ ] **Step 3: Implement the config normalization boundary**

Add a method with this behavior:

```python
def bind_runtime_workspace(self, launch_dir: str | Path | None = None) -> Path:
    requested = Path.cwd() if launch_dir is None else Path(launch_dir).expanduser()
    if not requested.exists() or not requested.is_dir():
        raise ValueError(f"启动工作区不存在或不是目录：{requested}")
    launch = requested.resolve()
    previous = self.resolve_workspace_root()
    normalized: list[str] = []
    for raw in self.safety.allowed_dirs:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        resolved = candidate.resolve()
        replacement = launch if resolved == previous else resolved
        value = str(replacement)
        if value not in normalized:
            normalized.append(value)
    launch_value = str(launch)
    if launch_value not in normalized:
        normalized.insert(0, launch_value)
    self.workspace_root = launch_value
    self.safety.allowed_dirs = normalized
    return launch
```

Resolve the previous workspace before changing `self.workspace_root`.

- [ ] **Step 4: Remove the onboarding workspace question**

Change `_build_config()` to accept only provider, preset, and permission mode. Emit:

```python
"workspace_root": ".",
"safety": {
    "permission_mode": permission_mode,
    "allowed_dirs": ["."],
    "max_turns": DEFAULT_RUNTIME_MAX_TURNS,
},
```

Update onboarding tests so prompt answer iterators contain only the permission mode and assert the serialized YAML has `workspace_root == "."` and `allowed_dirs == ["."]`.

- [ ] **Step 5: Bind both interactive entrypoints before Engine construction**

In `_launch_tui()` and `create_bridge()` call:

```python
config.bind_runtime_workspace(Path.cwd())
```

Do this after YAML loading/path anchoring and before constructing `AgentEngine`. Add a bridge test that writes a config with a different legacy absolute workspace, changes cwd to a launch directory, creates the bridge, and asserts both `bridge.engine.workspace_root` and ready payload use the launch directory.

- [ ] **Step 6: Lock the bypass and scoped-mode regression behavior**

Keep the existing bypass short-circuit unchanged and assert:

```python
checker = PermissionChecker(
    PermissionMode.BYPASS,
    allowed_dirs=[str(workspace)],
    workspace_root=str(workspace),
)
assert checker.check("file_write", {"path": str(outside / "file.txt")}).allowed
```

In the same test module retain the moderate assertion that an outside path is rejected with `PATH_OUTSIDE_SANDBOX`.

- [ ] **Step 7: Update user documentation and example configuration**

Document that interactive `naumi` always uses the launch directory, onboarding does not ask for a workspace, `workspace_root` remains an advanced non-interactive setting, and bypass can operate outside the workspace. Keep `config.yaml.example` on relative `.` values.

- [ ] **Step 8: Run targeted verification**

Run:

```bash
uv run python -m pytest \
  tests/unit/test_config.py -k 'workspace' \
  tests/unit/test_onboarding.py \
  tests/unit/test_ui_bridge.py -k 'workspace or ready' \
  tests/unit/test_permissions.py -k 'bypass_allows_paths_outside_sandbox or path_sandbox' -q
uv run ruff check \
  src/naumi_agent/config/settings.py \
  src/naumi_agent/cli/onboarding.py \
  src/naumi_agent/main.py \
  src/naumi_agent/ui/bridge.py \
  tests/unit/test_config.py \
  tests/unit/test_onboarding.py \
  tests/unit/test_ui_bridge.py \
  tests/unit/test_permissions.py
uv run python scripts/check_docs.py
git diff --check
```

Expected: all selected tests and static checks pass.

- [ ] **Step 9: Perform one real launch-directory bridge smoke test**

Create a temporary config containing a legacy absolute workspace, start the JSONL bridge from a different temporary directory, send `initialize`, and assert the emitted ready payload reports the process launch directory. Do not contact a model provider.

- [ ] **Step 10: Commit the feature**

```bash
git add \
  src/naumi_agent/config/settings.py \
  src/naumi_agent/cli/onboarding.py \
  src/naumi_agent/main.py \
  src/naumi_agent/ui/bridge.py \
  tests/unit/test_config.py \
  tests/unit/test_onboarding.py \
  tests/unit/test_ui_bridge.py \
  tests/unit/test_permissions.py \
  README.md \
  config.yaml.example
git commit -m "fix: bind interactive workspace to launch directory"
```

## Plan Self-Review

- Spec coverage: onboarding, old-config compatibility, both interactive UIs, permission modes, documentation, and real bridge evidence are covered.
- Placeholder scan: every code-changing step contains concrete behavior and an exact command.
- Type consistency: `bind_runtime_workspace()` accepts `str | Path | None`, returns `Path`, and all call sites use `Path.cwd()`.
- Scope: API server, deployment, queueing, heartbeat, search credentials, model contracts, packaging, Harness, and concurrency remain separate feature slices.
