# Provider Model Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Discover, cache, expose, and safely route models returned by OpenAI-compatible `/models` and Ollama `/api/tags` endpoints.

**Architecture:** A focused async discovery service obtains secure provider HTTP configuration from the existing credential runtime, performs bounded protocol-specific parsing, and merges remote IDs with immutable static catalog models. `ModelRouter` keeps a runtime-only dynamic overlay so discovered models are executable; shared slash and REST surfaces consume the same listings.

**Tech Stack:** Python 3.12+, `httpx`, `asyncio`, immutable dataclasses/mapping proxies, pytest/pytest-asyncio, local loopback HTTP servers.

## Global Constraints

- Execute inline only; the user explicitly prohibited subagents.
- Discovery is lazy and must not read credentials or access the network at Engine startup.
- Only `openai_chat`, `openai_responses`, and `ollama` discovery response shapes are implemented.
- Response bytes are capped at 2 MiB and accepted models at 500 per provider.
- Successful TTL is the catalog value; failed first refresh is cached for 30 seconds.
- Concurrent requests for one provider share one shielded task; cancellation of one waiter cannot cancel it.
- Static model metadata wins; remote IDs are sorted, deduplicated against static upstream IDs, then filtered.
- Errors never expose secrets, raw response bodies, authentication headers, or URL query strings.
- Run only provider discovery/runtime/router/slash/API tests, never the full repository suite.

---

### Task 1: Secure provider HTTP request configuration

**Files:**
- Modify: `src/naumi_agent/model/provider_runtime.py`
- Modify: `tests/unit/test_provider_runtime.py`

**Interfaces:**
- Consumes: `ProviderSpec`, existing `_resolve_auth()` / `_resolve_anthropic_auth()`, catalog source.
- Produces: `ProviderHTTPConfig(base_url: str, headers: Mapping[str, str], timeout_seconds: float)` and `build_provider_http_config(provider, *, catalog_source)`.

- [ ] **Step 1: Write failing tests**

Add tests proving standard bearer becomes an Authorization header, custom API-key headers and static headers merge, `auth=none` ignores global keys, repr omits secrets, missing base URL fails safely, and conflicts are rejected before credential lookup.

```python
config = build_provider_http_config(provider, catalog_source="/tmp/providers.json")
assert config.base_url == "https://api.example.test/v1"
assert config.headers == {"Authorization": "Bearer selected-secret"}
assert "selected-secret" not in repr(config)
```

- [ ] **Step 2: Verify RED**

Run: `PYTHONPATH=src .../.venv/bin/python -m pytest tests/unit/test_provider_runtime.py -q -k 'http_config'`

Expected: import failure because `ProviderHTTPConfig` and `build_provider_http_config` do not exist.

- [ ] **Step 3: Implement the immutable HTTP config**

Add a frozen dataclass with `headers=field(repr=False)`. Validate the provider base URL, reuse existing auth resolution, translate standard API keys to `Authorization: Bearer`, copy static headers through `MappingProxyType`, and use `request_timeout_ms / 1000` or a 10-second default.

- [ ] **Step 4: Run focused provider runtime tests**

Run: `PYTHONPATH=src .../.venv/bin/python -m pytest tests/unit/test_provider_runtime.py -q`

Expected: all provider runtime tests pass and no test accesses a real keyring.

- [ ] **Step 5: Commit**

```bash
git add src/naumi_agent/model/provider_runtime.py tests/unit/test_provider_runtime.py
git commit -m "feat: build secure provider http config"
```

### Task 2: Bounded discovery, merge, cache, and single-flight

**Files:**
- Create: `src/naumi_agent/model/discovery.py`
- Create: `tests/unit/test_model_discovery.py`

**Interfaces:**
- Consumes: `ProviderCatalog`, `ProviderSpec`, `ProviderModelSpec`, `build_provider_http_config()`.
- Produces: `AvailableModel`, `ProviderModelListing`, `ModelDiscoveryError`, `ModelDiscoveryService.list_provider()` and `.list_all()`.

- [ ] **Step 1: Write parser and merge RED tests**

Use `httpx.MockTransport` to cover OpenAI `data[].id`, Ollama `models[].model/name`, duplicate/invalid rows, deterministic order, static upstream deduplication, whitelist/blacklist, unsupported format, malformed JSON, 500-item truncation, HTTP 401/404/429, and a response larger than 2 MiB.

```python
listing = await service.list_provider("gateway")
assert [model.canonical_id for model in listing.models] == [
    "gateway/static-alias",
    "gateway/new-model",
]
assert listing.models[0].source == "static"
assert listing.models[1].source == "discovered"
```

- [ ] **Step 2: Verify parser RED**

Run: `PYTHONPATH=src .../.venv/bin/python -m pytest tests/unit/test_model_discovery.py -q -k 'openai or ollama or merge or bounded'`

Expected: module import failure for `naumi_agent.model.discovery`.

- [ ] **Step 3: Implement data contracts and bounded fetch**

Create frozen dataclasses, pure `_parse_openai_models()` / `_parse_ollama_models()` functions, streaming `_read_bounded_json()`, safe HTTP error mapping, and merge/filter helpers. Return static models plus a warning when the first remote fetch fails.

- [ ] **Step 4: Add cache/concurrency RED tests**

Use a fake monotonic clock and a delayed MockTransport. Prove fresh TTL, expiry, explicit refresh, 30-second negative caching, stale-if-error, 50 concurrent calls causing one request, and cancellation of one waiter leaving the shared task alive.

```python
results = await asyncio.gather(*(service.list_provider("gateway") for _ in range(50)))
assert request_count == 1
assert all(result.models == results[0].models for result in results)
```

- [ ] **Step 5: Implement per-provider cache and single-flight**

Store successful and failed entries against monotonic deadlines. Protect in-flight task creation with one `asyncio.Lock`; await with `asyncio.shield`; remove completed tasks in a `finally` section; cap `list_all()` with a semaphore of four.

- [ ] **Step 6: Run complete discovery tests and Ruff**

Run: `PYTHONPATH=src .../.venv/bin/python -m pytest tests/unit/test_model_discovery.py -q`

Run: `PYTHONPATH=src .../.venv/bin/python -m ruff check src/naumi_agent/model/discovery.py tests/unit/test_model_discovery.py`

Expected: all selected tests and Ruff pass.

- [ ] **Step 7: Commit**

```bash
git add src/naumi_agent/model/discovery.py tests/unit/test_model_discovery.py
git commit -m "feat: discover and cache provider models"
```

### Task 3: Dynamic model resolution and automatic first-call discovery

**Files:**
- Modify: `src/naumi_agent/model/targets.py`
- Modify: `src/naumi_agent/model/router.py`
- Modify: `tests/unit/test_model_targets.py`
- Modify: `tests/unit/test_model_router.py`
- Modify: `tests/unit/test_model_router_transport.py`

**Interfaces:**
- Consumes: `ModelDiscoveryService` listings.
- Produces: dynamic overlay support in `resolve_model_target()`, `ModelRouter.list_available_models()`, and async discovery-aware call/stream transport resolution.

- [ ] **Step 1: Write dynamic resolution RED tests**

Prove a dynamic model resolves to `provider/model`, visibility rules still apply, and a dynamic map for one provider never leaks into another.

```python
target = resolve_model_target(
    "remote-model",
    provider="gateway",
    catalog=catalog,
    dynamic_models={"gateway": {"remote-model": remote_spec}},
)
assert target.upstream_model == "remote-model"
```

- [ ] **Step 2: Verify target RED and implement optional overlay**

Run the named target tests; expect an unexpected keyword argument error. Add the optional mapping without changing callers that omit it.

- [ ] **Step 3: Write Router RED tests**

Prove static calls perform zero discovery requests; an unknown discovery-enabled model triggers one fetch and reaches LiteLLM; a missing remote model is rejected; 20 concurrent calls share discovery; runtime identity returns `catalog_pending` without I/O before the first fetch.

- [ ] **Step 4: Implement Router discovery lifecycle**

Construct `ModelDiscoveryService` only when a catalog exists. Store dynamic `ProviderModelSpec` mappings by provider. Add `list_available_models()`, `_discover_for_model()`, and `_resolve_transport_async()`. Change `call()` and `stream()` to await the async resolver; leave static resolution and legacy behavior unchanged.

- [ ] **Step 5: Run Router/target regression tests**

Run: `PYTHONPATH=src .../.venv/bin/python -m pytest tests/unit/test_model_targets.py tests/unit/test_model_router.py tests/unit/test_model_router_transport.py -q`

Expected: dynamic and existing OpenAI/Responses/Anthropic routes all pass.

- [ ] **Step 6: Commit**

```bash
git add src/naumi_agent/model/targets.py src/naumi_agent/model/router.py tests/unit/test_model_targets.py tests/unit/test_model_router.py tests/unit/test_model_router_transport.py
git commit -m "feat: route dynamically discovered models"
```

### Task 4: Shared `/models` command and real REST config

**Files:**
- Modify: `src/naumi_agent/main.py`
- Modify: `src/naumi_agent/cli/completer.py`
- Modify: `src/naumi_agent/ui/bridge.py`
- Modify: `frontend/terminal-ui/src/state.js`
- Modify: `src/naumi_agent/api/routes/tools.py`
- Modify: `src/naumi_agent/api/schemas.py`
- Create: `tests/unit/test_model_surfaces.py`
- Modify: `tests/unit/test_cli_completer.py`
- Modify: `frontend/terminal-ui/test/state.test.js`

**Interfaces:**
- Consumes: `ModelRouter.list_available_models(provider_id, refresh=...)`.
- Produces: `/models [provider] [--refresh]`, normalized slash completion, and dynamic `GET /config` model records/warnings.

- [ ] **Step 1: Write slash/API RED tests**

Use a fake Router returning static/discovered/stale listings. Assert `/models gateway`, `/models --refresh`, unknown provider, bounded 100-row output, Chinese warning text, and `get_config()` returning real model IDs without `kimi-for-coding`.

- [ ] **Step 2: Verify surface RED**

Run: `PYTHONPATH=src .../.venv/bin/python -m pytest tests/unit/test_model_surfaces.py tests/unit/test_cli_completer.py -q -k 'models or config'`

Expected: `/models` is unrecognized and `/config` returns the hardcoded Kimi record.

- [ ] **Step 3: Implement shared surfaces**

Register `/models` in the completer, bridge fallback registry and terminal UI defaults. Parse only an optional provider and `--refresh`; reject extra arguments with a usage line. Render provider headings and bounded rows. Extend `ModelInfo`/`ConfigResponse`, and make `get_config()` await the Router listing while preserving static fallback warnings.

- [ ] **Step 4: Run Python and Node surface tests**

Run: `PYTHONPATH=src .../.venv/bin/python -m pytest tests/unit/test_model_surfaces.py tests/unit/test_cli_completer.py tests/unit/test_ui_bridge.py -q -k 'models or slash_command'`

Run: `node --test --test-name-pattern 'models|slash' frontend/terminal-ui/test/state.test.js frontend/terminal-ui/test/components.test.js`

Expected: selected tests pass and both terminal clients receive `/models` from the shared registry.

- [ ] **Step 5: Commit**

```bash
git add src/naumi_agent/main.py src/naumi_agent/cli/completer.py src/naumi_agent/ui/bridge.py frontend/terminal-ui/src/state.js src/naumi_agent/api/routes/tools.py src/naumi_agent/api/schemas.py tests/unit/test_model_surfaces.py tests/unit/test_cli_completer.py frontend/terminal-ui/test/state.test.js
git commit -m "feat: expose discovered provider models"
```

### Task 5: Real loopback acceptance and integration

**Files:**
- Create: `tests/integration/test_model_discovery_loopback.py`

**Interfaces:**
- Consumes: catalog, discovery service, Router, `/models` renderer and REST config route.
- Produces: real local proof for OpenAI and Ollama endpoints with no external service or Keychain.

- [ ] **Step 1: Add real loopback tests**

Start bounded `ThreadingHTTPServer` instances. One verifies `GET /v1/models` plus bearer/static headers; one verifies `GET /api/tags` without auth. Use actual `httpx.AsyncClient`, 50 concurrent listing calls, dynamic Router resolution, slash output and direct REST route call.

- [ ] **Step 2: Run acceptance tests**

Run: `PYTHONPATH=src NAUMI_MODELS__API_KEY=unit-test-placeholder .../.venv/bin/python -m pytest tests/integration/test_model_discovery_loopback.py -q`

Expected: OpenAI and Ollama scenarios pass, each endpoint sees one request per cache generation, and no external keyring/network is used.

- [ ] **Step 3: Final focused verification**

Run Ruff and `py_compile` on changed Python files; run provider runtime/discovery/target/router/surface/loopback tests; run selected terminal UI slash tests and `npm --prefix frontend/terminal-ui run check`; run `git diff --check`.

- [ ] **Step 4: Merge and push**

Commit acceptance coverage, fetch `origin/main`, merge the feature branch to `main`, repeat the same focused verification on `main`, push, and confirm local/remote commit equality. Preserve `.naumi/terminal-ui-debug.jsonl`, `.superpowers/`, and the unrelated permission-hardening worktree.

