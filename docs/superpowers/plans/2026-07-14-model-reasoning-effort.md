# Model reasoning-effort implementation plan

> Implement this plan one task at a time with test-first changes. Run only the listed targeted
> modules; do not run the repository-wide suite.

**Goal:** Deliver a capability-validated reasoning-intensity control whose effective value reaches
real provider requests and stays synchronized across configuration, REST, CLI, TUI, and the new
terminal UI.

**Architecture:** A small `model.reasoning` domain module defines the public vocabulary and status
shape. Catalog and direct `model_info` metadata provide model-scoped capabilities. `ModelRouter`
resolves runtime/model/global precedence immediately before each request and is the only component
that injects `reasoning_effort`. UI surfaces consume router status rather than duplicating provider
logic.

**Tech stack:** Python 3.13+, Pydantic Settings, LiteLLM, FastAPI, Rich/Textual, JSONL bridge,
Node terminal UI.

---

## Task 1: Add strict reasoning capability types and catalog parsing

**Files:**

- Create: `src/naumi_agent/model/reasoning.py`
- Modify: `src/naumi_agent/model/catalog.py`
- Modify: `src/naumi_agent/model/discovery.py`
- Test: `tests/unit/test_provider_catalog.py`
- Test: `tests/unit/test_model_discovery.py`

### Step 1: Write failing catalog tests

Cover:

- legacy `reasoning: true/false` remains valid;
- object form parses ordered efforts and default;
- object form implies `supports_reasoning=True`;
- missing/empty/non-list `efforts`, duplicates, unknown values, `auto`, and a default outside the
  list each fail with the model capability path in Chinese;
- static discovery propagates the tuple/default;
- dynamically discovered-only models expose no guessed capability.

Run:

```bash
PYTHONPATH=src NAUMI_MODELS__API_KEY=unit-test-placeholder \
  .venv/bin/python -m pytest \
  tests/unit/test_provider_catalog.py tests/unit/test_model_discovery.py \
  -k 'reasoning or static_metadata' -q
```

Expected: fail because the new fields and object parser do not exist.

### Step 2: Implement the domain types and parser

Define:

- `ReasoningEffort`: `none|minimal|low|medium|high|xhigh|max`;
- `ReasoningEffortSetting`: the same values plus `auto`;
- immutable helpers that normalize strict string values without silently coercing arbitrary input.

Add `reasoning_efforts` and `default_reasoning_effort` to `ProviderModelSpec` and `AvailableModel`.
Replace the boolean-only parser with one helper that accepts boolean or the strict object form and
returns all three capability fields.

### Step 3: Re-run the focused tests

Use the same command and require green.

### Step 4: Commit

```bash
git add src/naumi_agent/model/reasoning.py src/naumi_agent/model/catalog.py \
  src/naumi_agent/model/discovery.py tests/unit/test_provider_catalog.py \
  tests/unit/test_model_discovery.py
git commit -m "feat: model reasoning effort capabilities"
```

---

## Task 2: Add configuration selection and capability overrides

**Files:**

- Modify: `src/naumi_agent/config/settings.py`
- Modify: `config.yaml.example`
- Test: `tests/unit/test_config.py`

### Step 1: Write failing configuration tests

Cover:

- global `models.reasoning_effort` defaults to `auto`;
- every supported value loads from YAML/environment;
- arbitrary strings fail;
- `model_info` loads selection, effort list, and default;
- duplicate/empty capability lists and a default outside the list fail at config load;
- the example remains free of credentials and documents `.naumi/config.yaml` metadata.

Run:

```bash
PYTHONPATH=src NAUMI_MODELS__API_KEY=unit-test-placeholder \
  .venv/bin/python -m pytest tests/unit/test_config.py -k reasoning_effort -q
```

### Step 2: Implement Pydantic models and validators

Add global selection to `ModelConfig`; add selection/capability/default fields to `ModelMeta`.
Use a model validator to enforce non-empty unique capabilities and contained default. Preserve
existing path anchoring and credential behavior.

### Step 3: Update the example

Document a commented direct-model example and explain that the list must match actual provider
transport support. Do not add an API key field or a speculative built-in list.

### Step 4: Re-run and commit

```bash
git add src/naumi_agent/config/settings.py config.yaml.example tests/unit/test_config.py
git commit -m "feat: configure model reasoning effort"
```

---

## Task 3: Resolve and transport effort in the router

**Files:**

- Modify: `src/naumi_agent/model/reasoning.py`
- Modify: `src/naumi_agent/model/router.py`
- Test: `tests/unit/test_model_router.py`
- Test: `tests/unit/test_model_router_transport.py`

### Step 1: Write failing resolution tests

Cover:

- `auto` with no metadata;
- catalog capability and direct `model_info` capability;
- direct metadata narrows catalog values;
- precedence `runtime > per-model > global > auto`;
- requested/canonical/alias metadata lookup;
- explicit runtime `auto` shadows configured values;
- reset returns to config;
- invalid runtime value and unsupported configured/runtime values produce Chinese errors with the
  supported list;
- switching the active model recomputes status.

### Step 2: Write failing request tests

Patch `litellm.acompletion` and assert both `call()` and `stream()`:

- omit `reasoning_effort` for `auto`;
- send explicit effort as a top-level keyword;
- omit `temperature` for explicit effort;
- validate before calling LiteLLM;
- reject explicit Kimi `thinking` plus effort;
- preserve existing Kimi automatic thinking when effort is `auto`.

Run:

```bash
PYTHONPATH=src NAUMI_MODELS__API_KEY=unit-test-placeholder \
  .venv/bin/python -m pytest \
  tests/unit/test_model_router.py tests/unit/test_model_router_transport.py \
  -k reasoning_effort -q
```

### Step 3: Implement one shared request decorator

Add router methods to get status, set/reset runtime override, resolve model capability metadata,
and apply the effective effort. Call the same decorator after transport resolution in both
`call()` and `stream()`. Keep runtime state process-local and avoid mutating `ModelConfig`.

### Step 4: Add local loopback transport evidence

Add isolated loopback tests that capture the actual body produced by the installed LiteLLM for:

- one OpenAI-compatible reasoning model/value;
- Claude 4.6 `medium` and `max`, proving `output_config.effort` and adaptive thinking;
- a value rejected by the installed Claude transport, proving it is excluded by capability
  validation before the network.

No real provider key or internet request is used.

### Step 5: Re-run and commit

```bash
git add src/naumi_agent/model/reasoning.py src/naumi_agent/model/router.py \
  tests/unit/test_model_router.py tests/unit/test_model_router_transport.py
git commit -m "feat: route validated reasoning effort"
```

---

## Task 4: Expose effort through shared commands and REST

**Files:**

- Modify: `src/naumi_agent/main.py`
- Modify: `src/naumi_agent/cli/completer.py`
- Modify: `src/naumi_agent/api/schemas.py`
- Modify: `src/naumi_agent/api/routes/tools.py`
- Modify: `tests/unit/test_slash_router.py`
- Modify: `tests/unit/test_cli_completer.py`
- Modify: `tests/unit/test_model_surfaces.py`
- Modify: `tests/unit/test_api.py`

### Step 1: Write failing surface tests

Cover:

- `/effort` status, set, `auto`, `reset`, invalid value, and unsupported model copy;
- `/model` includes current effective/source/supported values;
- `/models` lists exact effort values/default;
- command completion and help registry include `/effort`;
- REST `ModelInfo` carries capability metadata;
- REST `ConfigResponse` carries authoritative current effort status.

Run:

```bash
PYTHONPATH=src NAUMI_MODELS__API_KEY=unit-test-placeholder \
  .venv/bin/python -m pytest \
  tests/unit/test_slash_router.py tests/unit/test_cli_completer.py \
  tests/unit/test_model_surfaces.py tests/unit/test_api.py \
  -k 'effort or model_config or available_models' -q
```

### Step 2: Implement shared renderers and schemas

Keep all user-visible copy Chinese. `/effort` uses router methods and does not edit YAML. Rename
reasoning visibility descriptions to `思考文本显示` where touched. Use Pydantic-native serialization
for enum values.

### Step 3: Re-run and commit

```bash
git add src/naumi_agent/main.py src/naumi_agent/cli/completer.py \
  src/naumi_agent/api/schemas.py src/naumi_agent/api/routes/tools.py \
  tests/unit/test_slash_router.py tests/unit/test_cli_completer.py \
  tests/unit/test_model_surfaces.py tests/unit/test_api.py
git commit -m "feat: expose reasoning effort controls"
```

---

## Task 5: Synchronize JSONL bridge and new terminal UI

**Files:**

- Modify: `src/naumi_agent/ui/bridge.py`
- Modify: `frontend/terminal-ui/src/protocol.js`
- Modify: `frontend/terminal-ui/src/state.js`
- Modify: `frontend/terminal-ui/src/components/footer.js`
- Modify the welcome component located by `rg '已就绪|permission_mode|welcome' frontend/terminal-ui/src`
- Modify: `frontend/terminal-ui/protocol-contract.json`
- Modify: `tests/unit/test_ui_bridge.py`
- Modify: `frontend/terminal-ui/test/protocol.test.js`
- Modify: `frontend/terminal-ui/test/state.test.js`
- Modify the focused footer/welcome tests located by `rg 'StatusFooter|welcome' frontend/terminal-ui/test`

### Step 1: Write failing bridge/protocol tests

Assert the status payload includes a bounded object with effective value, source, supported values,
and default. After the shared `/effort high` command, the bridge emits refreshed authoritative
status. Protocol normalization rejects non-string effort identities and bounds arrays.

### Step 2: Write failing visual-state tests

Assert:

- welcome shows `思考强度`;
- footer shows `强度: <value>`;
- visibility is renamed `思考文本: on/off`;
- `/effort` is sent through the shared submit path, not handled by a duplicate frontend state
  machine;
- narrow footer packing still preserves information across wrapped lines without ellipsis loss.

Run:

```bash
PYTHONPATH=src NAUMI_MODELS__API_KEY=unit-test-placeholder \
  .venv/bin/python -m pytest tests/unit/test_ui_bridge.py -k effort -q
npm --prefix frontend/terminal-ui test -- \
  --test-name-pattern='effort|reasoning label|welcome|footer'
```

### Step 3: Implement status flow and rendering

Compute bridge status fresh from the active capable model. Normalize only the documented bounded
shape. Keep `/reasoning` as the one local visibility command; `/effort` goes to the bridge as a
normal shared slash command.

### Step 4: Re-run and commit

```bash
git add src/naumi_agent/ui/bridge.py frontend/terminal-ui/src \
  frontend/terminal-ui/protocol-contract.json tests/unit/test_ui_bridge.py \
  frontend/terminal-ui/test
git commit -m "feat: show reasoning effort in terminal ui"
```

---

## Task 6: Synchronize Textual TUI and documentation

**Files:**

- Modify: `src/naumi_agent/tui/app.py`
- Modify: `README.md`
- Modify: `docs/02-configuration.md`
- Modify relevant TUI test modules found with `rg 'StatusBar|reasoning:' tests -g '*.py'`

### Step 1: Write failing Textual status tests

Assert the status surface distinguishes `思考文本` from `强度`, and shared `/effort` output updates
the status through authoritative router state.

### Step 2: Implement and document

Use the shared command path; do not add a TUI-only resolver. Document `.naumi/config.yaml`, direct
`model_info` capability metadata, provider catalog object form, `/effort`, and the separation from
`/reasoning`. Keep secrets guidance unchanged.

### Step 3: Run focused tests and commit

```bash
PYTHONPATH=src NAUMI_MODELS__API_KEY=unit-test-placeholder \
  .venv/bin/python -m pytest <focused-tui-test-files> -k 'effort or reasoning_status' -q
git add src/naumi_agent/tui/app.py README.md docs/02-configuration.md <focused-tests>
git commit -m "docs: document reasoning effort workflow"
```

---

## Task 7: Targeted verification, real scenario, and self-review

### Step 1: Run touched Python modules only

```bash
PYTHONPATH=src NAUMI_MODELS__API_KEY=unit-test-placeholder \
  .venv/bin/python -m pytest \
  tests/unit/test_provider_catalog.py tests/unit/test_model_discovery.py \
  tests/unit/test_config.py tests/unit/test_model_router.py \
  tests/unit/test_model_router_transport.py tests/unit/test_model_surfaces.py \
  tests/unit/test_slash_router.py tests/unit/test_cli_completer.py \
  tests/unit/test_api.py tests/unit/test_ui_bridge.py <focused-tui-test-files> -q
```

Do not expand this command to `tests/`.

### Step 2: Run touched Node modules only

Use Node's test-name/file filters determined in Task 5. Do not run unrelated terminal UI suites.

### Step 3: Run static checks only on touched Python paths

```bash
.venv/bin/ruff check \
  src/naumi_agent/model/reasoning.py src/naumi_agent/model/catalog.py \
  src/naumi_agent/model/discovery.py src/naumi_agent/model/router.py \
  src/naumi_agent/config/settings.py src/naumi_agent/main.py \
  src/naumi_agent/cli/completer.py src/naumi_agent/api/schemas.py \
  src/naumi_agent/api/routes/tools.py src/naumi_agent/ui/bridge.py \
  src/naumi_agent/tui/app.py
.venv/bin/python -m py_compile <same-python-files>
git diff --check main...HEAD
```

### Step 4: Run a real local configuration scenario

In a temporary project, create `.naumi/config.yaml` plus `providers.json` with a loopback provider
and one effort-capable model. Start through the real config resolver/router path, set `/effort`, and
capture the actual outgoing JSON request. Confirm status, command output, and request body agree.
Use no real API key and leave no workspace files behind.

### Step 5: Self-review

Inspect every changed file and answer:

- Does any surface claim an effort that the request omitted?
- Can model switching leave a stale or unsupported runtime effort?
- Did any error include a credential or raw provider body?
- Are display visibility and model compute intensity always distinct?
- Did catalog/direct configuration compatibility remain intact?
- Are call and stream behavior identical?

Fix any issue in the relevant commit-sized slice and rerun only its focused tests.

### Step 6: Merge and push

After the targeted verification is green:

```bash
git -C /Users/lv/Workspace/NaumiAgent merge --ff-only codex/model-reasoning-effort
git -C /Users/lv/Workspace/NaumiAgent push origin main
```

Preserve the main worktree's untracked `.superpowers/` and the unrelated
`integrate-terminal-permission-hardening` worktree.
