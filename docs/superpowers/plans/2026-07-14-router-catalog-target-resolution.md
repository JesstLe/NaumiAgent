# Router Catalog Target Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Load an optional provider catalog into the production Router and resolve safe provider aliases plus catalog-backed model metadata without changing request transport.

**Architecture:** A pure `targets.py` module owns resolution grammar and errors. `ModelRouter` composes that resolver with field-wise metadata merging, while `AppConfig` and `AgentEngine` only load and inject the catalog at the composition root.

**Tech Stack:** Python 3.13+, frozen dataclasses, Pydantic Settings, LiteLLM metadata API, pytest, Ruff.

## Global Constraints

- Do not change `resolve_model(tier) -> str`.
- Do not change `call()` or `stream()` transport model, endpoint, headers, adapter, or credentials.
- Never reverse-match `upstream_id` as a local alias.
- Unqualified aliases resolve only inside `ModelConfig.provider`.
- Hidden static models raise a distinct Chinese error.
- Legacy models remain byte-for-byte passthrough after surrounding whitespace is removed.
- Catalog loading must not read secret references, Keychain, environment values, or network resources.
- Every Python test command must set `NAUMI_MODELS__API_KEY=unit-test-placeholder`.
- Run only named modules/tests; never run the full test suite.

---

### Task 1: Pure catalog target resolver

**Files:**
- Create: `src/naumi_agent/model/targets.py`
- Create: `tests/unit/test_model_targets.py`

**Interfaces:**
- Consumes: `ProviderCatalog`, `ProviderSpec`, and `ProviderModelSpec` from `model.catalog`.
- Produces: `ModelResolutionError`, frozen `ResolvedModelTarget`, and `resolve_model_target(model, *, provider, catalog)`.

- [ ] **Step 1: Write failing tests**

Create a two-provider catalog with `parse_provider_catalog_json()` and assert:

```python
target = resolve_model_target(
    "nvidia/local/glm",
    provider=None,
    catalog=catalog,
)
assert target.source == "catalog"
assert target.canonical_model == "nvidia/local/glm"
assert target.upstream_model == "z-ai/glm4.7"

active = resolve_model_target("local/glm", provider="nvidia", catalog=catalog)
assert active.canonical_model == "nvidia/local/glm"

legacy = resolve_model_target("openai/gpt-4o", provider="openai", catalog=catalog)
assert legacy.source == "legacy"
assert legacy.upstream_model == "openai/gpt-4o"
```

Add separate assertions that an unknown alias, filtered alias, and empty input raise `ModelResolutionError`; two aliases sharing one upstream remain distinct; and a provider with `api_format=None` still resolves.

- [ ] **Step 2: Verify RED**

Run: `PYTHONPATH=src NAUMI_MODELS__API_KEY=unit-test-placeholder .venv/bin/python -m pytest tests/unit/test_model_targets.py -q`

Expected: collection fails because `naumi_agent.model.targets` does not exist.

- [ ] **Step 3: Implement resolver**

```python
@dataclass(frozen=True)
class ResolvedModelTarget:
    requested_model: str
    canonical_model: str
    upstream_model: str
    provider: ProviderSpec | None
    model: ProviderModelSpec | None
    source: Literal["catalog", "legacy"]


def resolve_model_target(
    model: str,
    *,
    provider: str | None,
    catalog: ProviderCatalog | None,
) -> ResolvedModelTarget:
    requested = model.strip()
    if not requested:
        raise ModelResolutionError("模型名称不能为空。")
    if catalog is None:
        return _legacy_target(requested)

    prefix, separator, alias = requested.partition("/")
    selected = catalog.providers.get(prefix.lower()) if separator else None
    if selected is not None:
        return _catalog_target(selected, alias, requested)

    active = catalog.providers.get((provider or "").strip().lower())
    if active is None:
        return _legacy_target(requested)
    return _catalog_target(active, requested, requested)
```

`_catalog_target()` must check `provider.models` first to distinguish filtered from unknown, then check IDs from `provider.visible_models()` and return canonical `f"{provider.id}/{alias}"`.

- [ ] **Step 4: Verify GREEN and lint**

Run: `PYTHONPATH=src NAUMI_MODELS__API_KEY=unit-test-placeholder .venv/bin/python -m pytest tests/unit/test_model_targets.py tests/unit/test_provider_catalog.py -q`

Run: `.venv/bin/ruff check src/naumi_agent/model/targets.py tests/unit/test_model_targets.py`

Expected: selected tests pass and Ruff reports no errors.

- [ ] **Step 5: Commit**

```bash
git add src/naumi_agent/model/targets.py tests/unit/test_model_targets.py
git commit -m "feat: resolve provider catalog model targets"
```

### Task 2: Router metadata integration

**Files:**
- Modify: `src/naumi_agent/model/router.py:84-185`
- Modify: `tests/unit/test_model_router.py:1-135`

**Interfaces:**
- Consumes: `resolve_model_target()` and optional `ProviderCatalog`.
- Produces: `ModelRouter(config, *, catalog=None)` and `resolve_target(model)`; `get_model_info()` merges catalog/config/LiteLLM metadata per field.

- [ ] **Step 1: Write failing Router tests**

Add tests that construct `ModelRouter(config, catalog=catalog)` and verify:

```python
target = router.resolve_target("local-glm")
assert target.canonical_model == "nvidia/local-glm"
assert target.upstream_model == "z-ai/glm4.7"
assert router.resolve_model(ModelTier.CAPABLE) == "local-glm"
```

Patch `litellm.get_model_info` to return upstream context/output/prices, then declare partial catalog and `ModelMeta` values. Assert exact requested config fields override canonical config fields, canonical overrides catalog, catalog overrides LiteLLM, and missing price/output fields continue falling through independently. Assert different requested aliases cached separately.

- [ ] **Step 2: Verify RED**

Run: `PYTHONPATH=src NAUMI_MODELS__API_KEY=unit-test-placeholder .venv/bin/python -m pytest tests/unit/test_model_router.py -q`

Expected: new tests fail because `ModelRouter.__init__` has no `catalog` keyword and no `resolve_target()`.

- [ ] **Step 3: Implement target injection and field-wise metadata merging**

```python
def __init__(self, config: ModelConfig, *, catalog: ProviderCatalog | None = None) -> None:
    self._config = config
    self._catalog = catalog
    ...

def resolve_target(self, model: str) -> ResolvedModelTarget:
    return resolve_model_target(model, provider=self._config.provider, catalog=self._catalog)
```

Build metadata from fallback, merge safe LiteLLM fields for `target.upstream_model`, merge catalog limits, then canonical and exact `ModelMeta` fields. Convert per-million config prices to per-token fields so `get_cost_rates()` can combine independently inherited input/output prices. Keep `_info_cache` keyed by requested string.

- [ ] **Step 4: Verify GREEN and lint**

Run: `PYTHONPATH=src NAUMI_MODELS__API_KEY=unit-test-placeholder .venv/bin/python -m pytest tests/unit/test_model_router.py tests/unit/test_model_targets.py -q`

Run: `.venv/bin/ruff check src/naumi_agent/model/router.py src/naumi_agent/model/targets.py tests/unit/test_model_router.py tests/unit/test_model_targets.py`

Expected: selected tests pass and Ruff reports no errors.

- [ ] **Step 5: Commit**

```bash
git add src/naumi_agent/model/router.py tests/unit/test_model_router.py
git commit -m "feat: use catalog targets in model metadata"
```

### Task 3: Config and Engine composition

**Files:**
- Modify: `src/naumi_agent/config/settings.py:27-38,180-215`
- Modify: `src/naumi_agent/orchestrator/engine.py:1-70,405-416`
- Modify: `config.yaml.example:4-16`
- Modify: `tests/unit/test_config.py:10-105`
- Modify: `tests/unit/test_engine.py`

**Interfaces:**
- Consumes: `models.catalog_path`, `load_provider_catalog()`, and `ModelRouter(..., catalog=...)`.
- Produces: relative catalog path anchoring and production Engine injection.

- [ ] **Step 1: Write failing config and Engine tests**

In `test_config.py`, load YAML containing `models.catalog_path: catalogs/providers.json` and assert the value becomes `str(tmp_path / "catalogs" / "providers.json")`.

In `test_engine.py`, write a minimal native catalog, construct `AppConfig` with `models.provider="local"` and the catalog path, then assert:

```python
engine = AgentEngine(config)
target = engine.router.resolve_target("chat")
assert target.canonical_model == "local/chat"
assert target.upstream_model == "upstream-chat"
```

Add a no-path test or assertion that `AgentEngine(AppConfig(...)).router.resolve_target("legacy")` remains legacy. Use temporary memory paths and disable long-term memory to avoid unrelated persistent state.

- [ ] **Step 2: Verify RED**

Run: `PYTHONPATH=src NAUMI_MODELS__API_KEY=unit-test-placeholder .venv/bin/python -m pytest tests/unit/test_config.py::TestAppConfig::test_from_yaml_anchors_model_catalog_path tests/unit/test_engine.py -k "model_catalog" -q`

Expected: config rejects/ignores the new path and Engine Router remains legacy.

- [ ] **Step 3: Implement production composition**

Add `catalog_path: str | None = None` to `ModelConfig`. In `_resolve_runtime_paths()` anchor it when non-empty. In `AgentEngine.__init__()`:

```python
catalog = (
    load_provider_catalog(config.models.catalog_path)
    if config.models.catalog_path
    else None
)
self._router = ModelRouter(config.models, catalog=catalog)
```

Document `catalog_path` in `config.yaml.example` without adding a default file.

- [ ] **Step 4: Verify production path and real local catalog**

Run the two named config/Engine tests, then a short Python script that loads `/Users/lv/Workspace/ai-config-sync/opencode/opencode.json`, injects it into a Router, and asserts:

```python
router.resolve_target("nvidia/z-ai/glm4.7").upstream_model == "z-ai/glm4.7"
router.resolve_target("zhipuai-coding-plan/glm-5.1").upstream_model == "glm-5.1"
```

The script must not read referenced secret files or make network calls.

- [ ] **Step 5: Run focused regression and lint**

Run: `PYTHONPATH=src NAUMI_MODELS__API_KEY=unit-test-placeholder .venv/bin/python -m pytest tests/unit/test_model_targets.py tests/unit/test_provider_catalog.py tests/unit/test_model_router.py tests/unit/test_config.py::TestAppConfig::test_from_yaml_anchors_model_catalog_path tests/unit/test_engine.py -k "model_catalog" -q`

Run: `.venv/bin/ruff check src/naumi_agent/model/targets.py src/naumi_agent/model/router.py src/naumi_agent/config/settings.py src/naumi_agent/orchestrator/engine.py tests/unit/test_model_targets.py tests/unit/test_model_router.py tests/unit/test_config.py tests/unit/test_engine.py`

Expected: selected tests pass and Ruff reports no errors.

- [ ] **Step 6: Commit**

```bash
git add src/naumi_agent/config/settings.py src/naumi_agent/orchestrator/engine.py config.yaml.example tests/unit/test_config.py tests/unit/test_engine.py
git commit -m "feat: load model catalogs into the router"
```

### Task 4: Merge verification

**Files:**
- Verify only.

- [ ] **Step 1: Run the same focused Router/catalog/config/Engine tests and Ruff checks from Task 3**

Expected: all selected checks pass without Keychain prompts.

- [ ] **Step 2: Review `git diff main...HEAD --check` and branch status**

Expected: no whitespace errors and no uncommitted implementation files.

- [ ] **Step 3: Merge to `main`, repeat focused verification, push `origin/main`, and remove the owned worktree/branch**

Expected: `main` and `origin/main` resolve to the same commit.
