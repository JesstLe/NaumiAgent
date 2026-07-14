# Naumi Project Configuration Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `.naumi/config.yaml` the shared default for project configuration while preserving non-destructive root `config.yaml` compatibility.

**Architecture:** Add a side-effect-free resolver in `naumi_agent.config.paths`, then make Python CLI, JSONL bridge, deploy CLI, and Node UI defaults consume its contract. `AppConfig.from_yaml()` remains the YAML loader and anchors runtime paths to the selected file's directory.

**Tech Stack:** Python 3.12+, `pathlib`, Typer, argparse, pydantic-settings, pytest, Node.js built-in test runner, PowerShell.

## Global Constraints

- New project configuration is `.naumi/config.yaml`.
- Modern ancestor configuration wins over all legacy ancestor configuration.
- A non-default explicit `--config` path is never replaced by a fallback.
- Existing root `config.yaml` is read in place and never copied, moved, rewritten, or deleted by path resolution.
- Model secrets remain in the OS credential store or environment variables.
- Explicit container `/app/config.yaml` paths remain unchanged.
- Run only targeted configuration, launcher, bridge, deploy, onboarding, and Node protocol tests.
- Do not dispatch subagents; the user explicitly prohibited them.

---

### Task 1: Shared project configuration resolver

**Files:**
- Create: `src/naumi_agent/config/paths.py`
- Create: `tests/unit/test_config_paths.py`
- Modify: `src/naumi_agent/main.py`
- Modify: `src/naumi_agent/ui/bridge.py`
- Test: `tests/unit/test_ui_bridge.py`

**Interfaces:**
- Produces: `DEFAULT_CONFIG_PATH = ".naumi/config.yaml"`.
- Produces: `resolve_config_path(path: str | Path, *, cwd: Path | None = None) -> str`.
- Consumes: filesystem existence and the caller working directory; performs no writes.

- [ ] **Step 1: Write the failing resolver tests**

Add tests for nearest modern match, modern-over-legacy precedence, nearest legacy fallback, missing default creation target, explicit existing and missing paths, absolute paths, directories being rejected, and `~` expansion. The central precedence example is:

```python
def test_modern_config_in_parent_wins_over_nearer_legacy(tmp_path: Path) -> None:
    project = tmp_path / "project"
    child = project / "src" / "pkg"
    child.mkdir(parents=True)
    (project / ".naumi").mkdir()
    modern = project / ".naumi" / "config.yaml"
    modern.write_text("log_level: INFO\n")
    (child / "config.yaml").write_text("log_level: DEBUG\n")

    assert resolve_config_path(DEFAULT_CONFIG_PATH, cwd=child) == str(modern)
```

- [ ] **Step 2: Run the resolver test and confirm RED**

```bash
PYTHONPATH=src NAUMI_MODELS__API_KEY=unit-test-placeholder \
  /Users/lv/Workspace/NaumiAgent/.venv/bin/python -m pytest \
  tests/unit/test_config_paths.py -q
```

Expected: collection fails because `naumi_agent.config.paths` does not exist.

- [ ] **Step 3: Implement the resolver**

```python
DEFAULT_CONFIG_PATH = ".naumi/config.yaml"
LEGACY_CONFIG_PATH = "config.yaml"


def resolve_config_path(path: str | Path, *, cwd: Path | None = None) -> str:
    start = (cwd or Path.cwd()).expanduser().resolve()
    requested = Path(path).expanduser()
    if str(path) != DEFAULT_CONFIG_PATH:
        return str(requested if requested.is_absolute() else start / requested)
    directories = (start, *start.parents)
    for relative in (Path(DEFAULT_CONFIG_PATH), Path(LEGACY_CONFIG_PATH)):
        for directory in directories:
            candidate = directory / relative
            if candidate.is_file():
                return str(candidate)
    return str(start / DEFAULT_CONFIG_PATH)
```

Use `is_file()` so a directory cannot masquerade as YAML. Do not search source-tree examples.

- [ ] **Step 4: Replace duplicate Python resolvers**

Import the shared constant and function in `main.py` and `ui/bridge.py`. Keep `_resolve_config_path` as a compatibility alias in `main.py`, and remove bridge `_find_default_config_path()`.

- [ ] **Step 5: Run selected resolver and bridge tests**

```bash
PYTHONPATH=src NAUMI_MODELS__API_KEY=unit-test-placeholder \
  /Users/lv/Workspace/NaumiAgent/.venv/bin/python -m pytest \
  tests/unit/test_config_paths.py tests/unit/test_ui_bridge.py -q -k 'config_path'
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/naumi_agent/config/paths.py src/naumi_agent/main.py \
  src/naumi_agent/ui/bridge.py tests/unit/test_config_paths.py tests/unit/test_ui_bridge.py
git commit -m "feat: resolve project config from naumi directory"
```

---

### Task 2: Synchronize runtime defaults

**Files:**
- Modify: `src/naumi_agent/main.py`
- Modify: `src/naumi_agent/ui/bridge.py`
- Modify: `src/naumi_agent/deploy.py`
- Modify: `frontend/terminal-ui/src/protocol.js`
- Test: `tests/unit/test_terminal_ui_launcher.py`
- Test: `tests/unit/test_deploy.py`
- Test: `frontend/terminal-ui/test/protocol.test.js`

**Interfaces:**
- Consumes: `DEFAULT_CONFIG_PATH` and `resolve_config_path()` from Task 1.
- Produces: identical `.naumi/config.yaml` defaults across Typer, argparse, deploy, and direct Node invocation.

- [ ] **Step 1: Write failing surface tests**

Assert the no-argument CLI and compatibility command forward `DEFAULT_CONFIG_PATH`, deploy parser defaults to the same value, and Node does this:

```javascript
test("parseArgs defaults to the project Naumi config", () => {
  assert.equal(parseArgs([]).config, ".naumi/config.yaml");
});
```

- [ ] **Step 2: Run the selected tests and confirm RED**

Run terminal launcher, deploy parser, and Node `parseArgs` selections. Expected failures show `config.yaml`.

- [ ] **Step 3: Replace Python defaults**

Use `DEFAULT_CONFIG_PATH` for every Typer config option in `main.py`, bridge argparse, and `naumi-deploy validate`. Resolve the `configure` command path before calling `configure_project()`, and resolve deploy's path before existence checks; explicit `/app/config.yaml` remains exact.

- [ ] **Step 4: Replace Node default**

```javascript
const parsed = {
  config: ".naumi/config.yaml",
  bridgeCommand: "",
  bridgeCommandJson: "",
};
```

- [ ] **Step 5: Run targeted surface tests**

```bash
PYTHONPATH=src NAUMI_MODELS__API_KEY=unit-test-placeholder \
  /Users/lv/Workspace/NaumiAgent/.venv/bin/python -m pytest \
  tests/unit/test_terminal_ui_launcher.py tests/unit/test_deploy.py -q
node --test --test-name-pattern 'parseArgs' frontend/terminal-ui/test/protocol.test.js
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/naumi_agent/main.py src/naumi_agent/ui/bridge.py src/naumi_agent/deploy.py \
  frontend/terminal-ui/src/protocol.js tests/unit/test_terminal_ui_launcher.py \
  tests/unit/test_deploy.py frontend/terminal-ui/test/protocol.test.js
git commit -m "feat: default runtimes to naumi project config"
```

---

### Task 3: Persistence, onboarding, documentation, and real scenario

**Files:**
- Modify: `.gitignore`
- Modify: `config.yaml.example`
- Modify: `README.md`
- Modify: `scripts/windows/setup.ps1`
- Modify: `src/naumi_agent/cli/onboarding.py`
- Test: `tests/unit/test_config.py`
- Test: `tests/unit/test_configure_command.py`
- Test: `tests/unit/test_onboarding.py`
- Create: `tests/integration/test_naumi_config_layout.py`

**Interfaces:**
- Consumes: Task 1 resolution and `AppConfig.from_yaml()` path anchoring.
- Produces: ignored local configuration/state, correct setup copy, and a real temporary-project proof.

- [ ] **Step 1: Write failing persistence tests**

Load `.naumi/config.yaml` with `models.catalog_path: providers.json` and assert:

```python
assert config.models.catalog_path == str(project / ".naumi" / "providers.json")
assert config.memory.session_db_path == str(project / ".naumi" / "data" / "sessions.db")
assert config.memory.vector_db_path == str(project / ".naumi" / "data" / "chroma")
```

Invoke `configure --non-interactive --provider kimi` without `--config` from a temporary project and assert `.naumi/config.yaml` exists while root `config.yaml` does not. Stub credential storage; never read or write a real secret.

- [ ] **Step 2: Run config/onboarding selections and confirm RED**

Expected failures name the old root default or missing modern file.

- [ ] **Step 3: Update persistence rules and example**

Add only these ignore rules, preserving tracked `.naumi/skills/`:

```gitignore
.naumi/config.yaml
.naumi/providers.json
.naumi/data/
```

Change the example header and catalog path:

```yaml
# 复制为 .naumi/config.yaml；密钥请存入系统凭据库或环境变量
models:
  # catalog_path: "providers.json"
```

- [ ] **Step 4: Update setup copy**

README creates `.naumi/` before copying the root template. Windows setup creates `$repoRoot\.naumi\config.yaml` only when neither modern nor legacy config exists; it preserves an existing modern file and keeps an existing legacy root file active without creating a competing copy. Onboarding text names `.naumi/config.yaml` without suggesting secrets belong in it.

- [ ] **Step 5: Add a real filesystem integration test**

Create a nested working directory with parent `.naumi/config.yaml` and `.naumi/providers.json`; call the shared resolver, load through `AppConfig.from_yaml()`, and verify catalog/session/vector paths all resolve beneath the parent `.naumi/`.

- [ ] **Step 6: Run targeted final verification**

```bash
PYTHONPATH=src NAUMI_MODELS__API_KEY=unit-test-placeholder \
  /Users/lv/Workspace/NaumiAgent/.venv/bin/python -m pytest \
  tests/unit/test_config_paths.py tests/unit/test_config.py tests/unit/test_configure_command.py \
  tests/unit/test_onboarding.py tests/unit/test_terminal_ui_launcher.py \
  tests/unit/test_ui_bridge.py tests/unit/test_deploy.py \
  tests/integration/test_naumi_config_layout.py -q \
  -k 'config or onboarding or terminal or deploy or naumi'
node --test --test-name-pattern 'parseArgs' frontend/terminal-ui/test/protocol.test.js
ruff check src/naumi_agent/config src/naumi_agent/main.py src/naumi_agent/ui/bridge.py \
  src/naumi_agent/deploy.py tests/unit/test_config_paths.py \
  tests/integration/test_naumi_config_layout.py
git diff --check
```

Expected: all selected tests and static checks pass; no repository-wide suite runs.

- [ ] **Step 7: Self-review compatibility**

Confirm source-tree example fallback is gone, explicit paths are preserved, legacy root config is not mutated, `.naumi/skills/` remains trackable, no plaintext key was added, and setup copy names the modern path.

- [ ] **Step 8: Commit**

```bash
git add .gitignore config.yaml.example README.md scripts/windows/setup.ps1 \
  src/naumi_agent/cli/onboarding.py tests/unit/test_config.py \
  tests/unit/test_configure_command.py tests/unit/test_onboarding.py \
  tests/integration/test_naumi_config_layout.py
git commit -m "docs: migrate project config to naumi directory"
```

---

### Task 4: Merge and remote equality

**Files:**
- No additional product files.

**Interfaces:**
- Consumes: all feature commits and targeted verification from Tasks 1-3.
- Produces: verified `main` identical to `origin/main`.

- [ ] **Step 1: Fetch and inspect remote `main`**

Run `git fetch origin main`, confirm the feature base, and inspect local untracked files before switching branches.

- [ ] **Step 2: Merge into `main`**

Fast-forward local `main` when possible, merge `codex/naumi-config-layout`, and preserve `.superpowers/` plus the permission-hardening worktree.

- [ ] **Step 3: Re-run Task 3 targeted verification on merged `main`**

Expected: the same selected tests, Node tests, ruff, and diff checks pass.

- [ ] **Step 4: Push and verify equality**

Push `main`, fetch it again, and assert `git rev-parse main` equals `git rev-parse origin/main`.
