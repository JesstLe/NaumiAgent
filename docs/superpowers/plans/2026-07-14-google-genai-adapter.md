# Google GenAI Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans and superpowers:test-driven-development to implement this plan task-by-task. Do not dispatch subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `google_genai` catalog providers execute Gemini native text/tool/stream requests and discover current `generateContent` models through the existing shared Router and UI/TUI model-list surfaces.

**Architecture:** Catalog loading normalizes Google API-key identity, `provider_runtime.py` maps trusted catalog targets to LiteLLM's native `gemini/` transport, and `ModelDiscoveryService` parses Google's bounded `/models` envelope into the existing dynamic overlay. `ModelRouter` remains provider-neutral; real local loopback tests prove request paths, auth, tools, streaming, usage, and discovery-to-execution without Google network or Keychain access.

**Tech Stack:** Python 3.13+, Pydantic catalog contracts, LiteLLM `gemini/`, httpx, stdlib `ThreadingHTTPServer`, pytest/pytest-asyncio, Ruff.

## Global Constraints

- Execute inline only; the user explicitly prohibited further subagents.
- Implement only Google AI Studio `google_genai` over native `generateContent` / `streamGenerateContent` plus Google `/models` discovery.
- Do not implement Vertex AI, Google Interactions, Azure, Ollama inference, OpenAI-compatible Gemini, Files/Live/Batch/Embedding/media, built-in provider defaults, or a new UI picker.
- Never hardcode a "latest models" list. The provider endpoint plus existing TTL cache is the source of current IDs; static catalog entries remain supported.
- `baseURL` is explicit and includes the API version, for example `https://generativelanguage.googleapis.com/v1beta`; do not infer it from provider ID or URL.
- Standard Google auth is `api_key_header` with case-insensitive `X-Goog-Api-Key`; bearer/custom/none must not fall back to ambient `GOOGLE_API_KEY` or `GEMINI_API_KEY`.
- Preserve the existing 2 MiB discovery response limit, 500-model cap, 256-character model-ID cap, TTL, negative cache, stale-if-error, single-flight, and at-most-four-provider concurrency.
- All user-visible errors are Chinese and must not include secret values, response bodies, or query strings.
- Use `NAUMI_MODELS__API_KEY=unit-test-placeholder uv run python -m pytest`; never use bare `pytest`, so tests do not touch macOS Keychain or fall back to a system interpreter.
- Run only the targeted files and node-free commands in this plan. Do not run the full Python or Node suites.
- Preserve the pre-existing untracked `.superpowers/` directory.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/naumi_agent/model/catalog.py` | Normalize OpenCode Google secret references to Google API-key header identity. |
| `src/naumi_agent/model/provider_runtime.py` | Build explicit native Gemini transport without ambient credential fallback. |
| `src/naumi_agent/model/discovery.py` | Parse bounded Google `/models` responses and filter non-`generateContent` resources. |
| `tests/unit/test_provider_catalog.py` | Lock Google auth normalization and secret-safety behavior. |
| `tests/unit/test_provider_runtime.py` | Lock model/auth/header/timeout/dispatcher mappings and pre-network failures. |
| `tests/unit/test_model_discovery.py` | Lock Google model parsing, filtering, cache, auth, and dynamic listing behavior. |
| `tests/unit/test_model_router_transport.py` | Prove Google transport is used by non-stream/stream and discovered targets. |
| `tests/integration/test_google_genai_loopback.py` | Exercise real LiteLLM Gemini HTTP text, tools, streaming, usage, and discovery. |
| `docs/15-model-provider-configuration.md` | Add current user configuration and `/models` behavior. |
| `README.md` | Record Google GenAI among actually implemented native formats. |
| `docs/superpowers/specs/2026-07-14-google-genai-adapter-design.md` | Record implemented status and focused evidence after acceptance. |

---

### Task 1: Google Catalog Authentication Identity

**Files:**
- Modify: `src/naumi_agent/model/catalog.py:28-42,360-400`
- Modify: `tests/unit/test_provider_catalog.py`

**Interfaces:**
- Consumes: `_parse_opencode_auth(options, path, api_format)` and `APIFormat.GOOGLE_GENAI`.
- Produces: OpenCode `@ai-sdk/google` secret references as `ProviderAuthSpec(type=API_KEY_HEADER, header="X-Goog-Api-Key", ...)`.
- Preserves: native catalog auth, Anthropic `X-API-Key`, plaintext-secret rejection, and side-effect-free parsing.

- [x] **Step 1: Write the failing OpenCode Google auth test**

Add beside the Anthropic OpenCode auth test:

```python
def test_opencode_google_provider_uses_x_goog_api_key() -> None:
    catalog = parse_provider_catalog_json(json.dumps({
        "provider": {
            "google": {
                "npm": "@ai-sdk/google",
                "options": {
                    "baseURL": "https://generativelanguage.googleapis.com/v1beta",
                    "apiKey": "{env:GEMINI_SELECTED_KEY}",
                },
                "models": {
                    "flash": {"upstreamId": "gemini-3.5-flash"},
                },
            },
        },
    }))

    provider = catalog.providers["google"]
    assert provider.api_format is APIFormat.GOOGLE_GENAI
    assert provider.auth == ProviderAuthSpec(
        type=AuthType.API_KEY_HEADER,
        secret_source=SecretSource.ENV,
        secret_ref="GEMINI_SELECTED_KEY",
        header="X-Goog-Api-Key",
    )
```

- [x] **Step 2: Verify RED**

Run:

```bash
NAUMI_MODELS__API_KEY=unit-test-placeholder uv run python -m pytest -q \
  tests/unit/test_provider_catalog.py \
  -k 'opencode_google_provider_uses_x_goog_api_key'
```

Expected: FAIL because the current OpenCode parser produces bearer auth.

- [x] **Step 3: Implement the Google auth branch**

Change only `_parse_opencode_auth()`:

```python
if source and api_format is APIFormat.ANTHROPIC_MESSAGES:
    auth_type = AuthType.API_KEY_HEADER
    header = "X-API-Key"
elif source and api_format is APIFormat.GOOGLE_GENAI:
    auth_type = AuthType.API_KEY_HEADER
    header = "X-Goog-Api-Key"
else:
    auth_type = AuthType.BEARER if source else AuthType.NONE
    header = None
```

Do not add provider-name or base-URL heuristics.

- [x] **Step 4: Verify GREEN and catalog regressions**

Run:

```bash
NAUMI_MODELS__API_KEY=unit-test-placeholder uv run python -m pytest -q \
  tests/unit/test_provider_catalog.py \
  -k 'google or anthropic or opencode or plaintext or duplicate'
uv run ruff check src/naumi_agent/model/catalog.py tests/unit/test_provider_catalog.py
git diff --check
```

Expected: selected tests pass, Ruff passes, no whitespace errors.

- [x] **Step 5: Self-review and commit**

Confirm the parser does not resolve `GEMINI_SELECTED_KEY`, echo references in an error, or change native provider auth. Commit:

```bash
git add src/naumi_agent/model/catalog.py tests/unit/test_provider_catalog.py
git commit -m "fix: normalize Google provider authentication"
```

---

### Task 2: Explicit Native Gemini Transport

**Files:**
- Modify: `src/naumi_agent/model/provider_runtime.py:80-220`
- Modify: `tests/unit/test_provider_runtime.py`

**Interfaces:**
- Consumes: `ResolvedModelTarget`, `_resolve_secret()`, `_assert_no_auth_header_conflict()`, `NO_GLOBAL_API_KEY`.
- Produces: `build_google_genai_transport(target, *, catalog_source) -> ProviderTransport`.
- Produces: dispatcher support for `APIFormat.GOOGLE_GENAI`.
- Transport model is `gemini/<normalized_upstream_id>`; kwargs remain immutable and secret-safe.

- [x] **Step 1: Write failing standard-key transport tests**

Import `build_google_genai_transport` and add:

```python
def test_google_genai_maps_standard_key_model_base_headers_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_SELECTED_KEY", "google-selected-secret")
    target = _target(
        api_format=APIFormat.GOOGLE_GENAI,
        base_url="https://generativelanguage.googleapis.com/v1beta/",
        auth=_auth(
            AuthType.API_KEY_HEADER,
            SecretSource.ENV,
            "GEMINI_SELECTED_KEY",
            header="x-goog-api-key",
        ),
        headers={"X-Tenant": "tenant-a"},
    )

    transport = build_google_genai_transport(
        target,
        catalog_source="/tmp/providers.json",
    )

    assert transport.model == "gemini/gemini-model-v2"
    assert transport.kwargs == {
        "api_base": "https://generativelanguage.googleapis.com/v1beta",
        "api_key": "google-selected-secret",
        "extra_headers": {"X-Tenant": "tenant-a"},
        "timeout": 12.345,
    }
    assert "google-selected-secret" not in repr(transport)


def test_google_genai_strips_one_official_models_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_SELECTED_KEY", "secret")
    target = replace(
        _target(
            api_format=APIFormat.GOOGLE_GENAI,
            auth=_auth(
                AuthType.API_KEY_HEADER,
                SecretSource.ENV,
                "GEMINI_SELECTED_KEY",
                header="X-Goog-Api-Key",
            ),
        ),
        upstream_model="models/gemini-3.5-flash",
    )

    transport = build_google_genai_transport(
        target,
        catalog_source="/tmp/providers.json",
    )

    assert transport.model == "gemini/gemini-3.5-flash"
```

- [x] **Step 2: Write failing custom/none/fallback tests**

```python
@pytest.mark.parametrize(
    ("auth", "expected_headers"),
    [
        (
            _auth(AuthType.BEARER, SecretSource.ENV, "GOOGLE_CUSTOM_SECRET"),
            {"Authorization": "Bearer custom-secret"},
        ),
        (
            _auth(
                AuthType.API_KEY_HEADER,
                SecretSource.ENV,
                "GOOGLE_CUSTOM_SECRET",
                header="X-Proxy-Key",
            ),
            {"X-Proxy-Key": "custom-secret"},
        ),
    ],
)
def test_google_custom_auth_uses_header_and_nonsecret_placeholder(
    monkeypatch: pytest.MonkeyPatch,
    auth: ProviderAuthSpec,
    expected_headers: dict[str, str],
) -> None:
    monkeypatch.setenv("GOOGLE_CUSTOM_SECRET", "custom-secret")
    monkeypatch.setenv("GOOGLE_API_KEY", "ambient-must-not-win")
    monkeypatch.setenv("GEMINI_API_KEY", "ambient-must-not-win")

    transport = build_google_genai_transport(
        _target(api_format=APIFormat.GOOGLE_GENAI, auth=auth),
        catalog_source="/tmp/providers.json",
    )

    assert transport.kwargs["api_key"] == NO_GLOBAL_API_KEY
    assert transport.kwargs["extra_headers"] == expected_headers


def test_google_none_auth_never_reads_ambient_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "ambient-must-not-win")
    monkeypatch.setenv("GEMINI_API_KEY", "ambient-must-not-win")

    transport = build_google_genai_transport(
        _target(
            api_format=APIFormat.GOOGLE_GENAI,
            auth=_auth(AuthType.NONE, None, None),
        ),
        catalog_source="/tmp/providers.json",
    )

    assert transport.kwargs["api_key"] == NO_GLOBAL_API_KEY
    assert transport.kwargs["extra_headers"] == {}
```

Add parametrized invalid upstream IDs `""`, `"models/"`, `"models/a/b"`, `"a:generateContent"`, `"a?key=x"`, and a control-character value. Each must raise `ProviderRuntimeError` before secret lookup and must not echo the full unsafe value.

- [x] **Step 3: Verify RED**

Run:

```bash
NAUMI_MODELS__API_KEY=unit-test-placeholder uv run python -m pytest -q \
  tests/unit/test_provider_runtime.py \
  -k 'google_genai or google_custom or google_none or dispatcher'
```

Expected: import/dispatch failures because no Google transport exists.

- [x] **Step 4: Implement Google model normalization and auth**

Add these private helpers:

```python
def _normalize_google_model_id(target: ResolvedModelTarget) -> str:
    value = target.upstream_model.strip()
    if value.startswith("models/"):
        value = value.removeprefix("models/")
    if (
        not value
        or "/" in value
        or any(char in value for char in (":", "?", "#"))
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ProviderRuntimeError("Google GenAI upstream model ID 无效。")
    return value


def _resolve_google_auth(
    provider: ProviderSpec,
    *,
    catalog_source: str,
) -> tuple[str, tuple[str, str] | None]:
    auth = provider.auth
    if auth.type is AuthType.NONE:
        if any(
            value is not None
            for value in (auth.secret_source, auth.secret_ref, auth.header, auth.scheme)
        ):
            raise _invalid_auth(provider)
        return NO_GLOBAL_API_KEY, None
    if auth.secret_source is None or not auth.secret_ref:
        raise _invalid_auth(provider)
    secret = _resolve_secret(provider, auth, catalog_source=catalog_source)
    if auth.type is AuthType.API_KEY_HEADER:
        header = auth.header or "X-Goog-Api-Key"
        if header.casefold() == "x-goog-api-key":
            return secret, None
        return NO_GLOBAL_API_KEY, (header, secret)
    if auth.type is AuthType.BEARER:
        header = auth.header or "Authorization"
        scheme = auth.scheme or "Bearer"
        return NO_GLOBAL_API_KEY, (header, f"{scheme} {secret}")
    raise _invalid_auth(provider)
```

Before `_resolve_google_auth()`, call `_assert_no_auth_header_conflict()` so conflicting static auth headers fail before secret resolution.

- [x] **Step 5: Implement the public builder and dispatcher**

Add:

```python
def build_google_genai_transport(
    target: ResolvedModelTarget,
    *,
    catalog_source: str,
) -> ProviderTransport:
    provider = _require_catalog_provider(target)
    _validate_provider_format(provider, expected_format=APIFormat.GOOGLE_GENAI)
    static_headers = dict(provider.headers)
    _assert_no_auth_header_conflict(provider, static_headers)
    api_key, auth_header = _resolve_google_auth(
        provider,
        catalog_source=catalog_source,
    )
    if auth_header is not None:
        name, value = auth_header
        static_headers[name] = value
    kwargs: dict[str, Any] = {
        "api_base": provider.base_url.rstrip("/"),
        "api_key": api_key,
        "extra_headers": MappingProxyType(static_headers),
    }
    if provider.request_timeout_ms is not None:
        kwargs["timeout"] = provider.request_timeout_ms / 1000
    return ProviderTransport(
        model=f"gemini/{_normalize_google_model_id(target)}",
        kwargs=MappingProxyType(kwargs),
    )
```

Keep the existing positive-timeout validation by extracting/reusing one private helper rather than silently accepting zero. Add the `GOOGLE_GENAI` branch to `build_provider_transport()`.

- [x] **Step 6: Verify GREEN, regression set, and Ruff**

Run:

```bash
NAUMI_MODELS__API_KEY=unit-test-placeholder uv run python -m pytest -q \
  tests/unit/test_provider_runtime.py \
  -k 'google or openai or anthropic or dispatcher or header_conflict or missing_base'
uv run ruff check src/naumi_agent/model/provider_runtime.py \
  tests/unit/test_provider_runtime.py
git diff --check
```

Expected: all selected transport tests pass; existing three formats remain unchanged.

- [x] **Step 7: Self-review and commit**

Review secret repr, ambient env isolation, case-insensitive header conflicts, immutable copied headers, model path injection, zero timeout, and protocol mismatch. Commit:

```bash
git add src/naumi_agent/model/provider_runtime.py tests/unit/test_provider_runtime.py
git commit -m "feat: add Google GenAI provider transport"
```

---

### Task 3: Bounded Google Model Discovery

**Files:**
- Modify: `src/naumi_agent/model/discovery.py:70-95,270-335,380-455`
- Modify: `tests/unit/test_model_discovery.py`

**Interfaces:**
- Consumes: existing `ModelDiscoveryService`, `build_provider_http_config()`, `_parse_rows()` and `_ParsedModels`.
- Produces: `_parse_google_models(provider_id: str, payload: object) -> _ParsedModels`.
- Produces: `GOOGLE_GENAI` in the allowed discovery formats and existing `ProviderModelListing` output.

- [x] **Step 1: Write failing pure parser tests**

Expose no new public parser. Test through `_parse_google_models` within the module or through a real `MockTransport` service call:

```python
def test_google_models_strip_resource_prefix_and_filter_generation_support() -> None:
    parsed = discovery._parse_google_models("google", {
        "models": [
            {
                "name": "models/gemini-3.5-flash",
                "supportedGenerationMethods": ["generateContent", "countTokens"],
            },
            {
                "name": "models/text-embedding-current",
                "supportedGenerationMethods": ["embedContent"],
            },
            {"name": "models/gemini-proxy"},
            {"name": "invalid-without-resource-prefix"},
        ],
    })

    assert parsed.ids == ("gemini-3.5-flash", "gemini-proxy")
    assert parsed.invalid_count == 1
    assert parsed.unsupported_count == 1
    assert "不支持 generateContent" in (parsed.warning or "")
```

Add cases for duplicate normalized IDs, empty models, every row invalid/unsupported, control characters, `models/a/b`, and 501 valid rows. If the envelope contains rows but none can be called with `generateContent`, expect `ModelDiscoveryError` with no raw row content.

- [x] **Step 2: Write the failing HTTP/auth/listing test**

Use `httpx.MockTransport` and an explicit Google provider:

```python
@pytest.mark.asyncio
async def test_google_discovery_uses_models_path_key_header_and_dynamic_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    monkeypatch.setenv("DISCOVERY_GOOGLE_KEY", "selected-google-secret")

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={
            "models": [{
                "name": "models/gemini-3.5-flash",
                "supportedGenerationMethods": ["generateContent"],
            }],
        })

    service = ModelDiscoveryService(
        _catalog(_provider(
            api_format=APIFormat.GOOGLE_GENAI,
            base_url="https://generativelanguage.googleapis.com/v1beta",
            auth=_auth(
                AuthType.API_KEY_HEADER,
                SecretSource.ENV,
                "DISCOVERY_GOOGLE_KEY",
                header="X-Goog-Api-Key",
            ),
            discovery=ModelDiscoverySpec(enabled=True, path="/models"),
        )),
        transport=httpx.MockTransport(handler),
    )

    listing = await service.list_provider("vendor")

    assert [model.id for model in listing.models] == ["gemini-3.5-flash"]
    assert requests[0].url.path == "/v1beta/models"
    assert requests[0].headers["x-goog-api-key"] == "selected-google-secret"
```

Assert no Authorization header and no secret in `repr(listing)` or warning text.

- [x] **Step 3: Verify RED**

Run:

```bash
NAUMI_MODELS__API_KEY=unit-test-placeholder uv run python -m pytest -q \
  tests/unit/test_model_discovery.py \
  -k 'google'
```

Expected: FAIL because Google discovery is rejected and no parser/unsupported count exists.

- [x] **Step 4: Extend the parsed-result contract**

Add `unsupported_count: int = 0` to `_ParsedModels` and include this exact warning fragment when nonzero:

```python
if self.unsupported_count:
    messages.append(
        f"忽略 {self.unsupported_count} 条不支持 generateContent 的模型记录"
    )
```

Do not change OpenAI/Ollama call sites; the default preserves their behavior.

- [x] **Step 5: Implement the Google parser**

Implement one bounded pass:

```python
def _parse_google_models(provider_id: str, payload: object) -> _ParsedModels:
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise ModelDiscoveryError(
            f'provider "{provider_id}" 模型发现返回结构无效。'
        )
    rows = payload["models"]
    normalized: list[object] = []
    invalid_count = 0
    unsupported_count = 0
    for row in rows:
        if not isinstance(row, dict):
            invalid_count += 1
            continue
        raw_name = row.get("name")
        if not isinstance(raw_name, str) or not raw_name.startswith("models/"):
            invalid_count += 1
            continue
        model_id = raw_name.removeprefix("models/")
        if not model_id or "/" in model_id:
            invalid_count += 1
            continue
        methods = row.get("supportedGenerationMethods")
        if isinstance(methods, list) and "generateContent" not in methods:
            unsupported_count += 1
            continue
        normalized.append({"id": model_id})
    parsed = _parse_rows(provider_id, normalized, keys=("id",))
    return replace(
        parsed,
        invalid_count=parsed.invalid_count + invalid_count,
        unsupported_count=unsupported_count,
    )
```

If `rows` is nonempty and `normalized` is empty, raise the same safe no-valid-model error after counting; do not leak model names. Preserve the 500-item cap by delegating final ID validation/deduplication to `_parse_rows()`.

- [x] **Step 6: Route Google discovery and keep cache policy unchanged**

Add `APIFormat.GOOGLE_GENAI` to `_fetch_remote_models()`'s allowed set and dispatch to `_parse_google_models()` after JSON parsing. Do not alter TTL/single-flight/stale/failure logic.

- [x] **Step 7: Verify GREEN and focused cache regressions**

Run:

```bash
NAUMI_MODELS__API_KEY=unit-test-placeholder uv run python -m pytest -q \
  tests/unit/test_model_discovery.py \
  -k 'google or single_flight or stale or ttl or bounded or whitelist or blacklist'
uv run ruff check src/naumi_agent/model/discovery.py \
  tests/unit/test_model_discovery.py
git diff --check
```

Expected: Google and selected shared policy tests pass.

- [x] **Step 8: Self-review and commit**

Review URL path composition, standard Google auth, unsupported method handling, invalid/duplicate counts, bounds, stable ordering, static-upstream deduplication, and no raw response leakage. Commit:

```bash
git add src/naumi_agent/model/discovery.py tests/unit/test_model_discovery.py
git commit -m "feat: discover Google GenAI models"
```

---

### Task 4: Router and Real Gemini Loopback

**Files:**
- Modify: `tests/unit/test_model_router_transport.py`
- Create: `tests/integration/test_google_genai_loopback.py`

**Interfaces:**
- Consumes: `build_provider_transport()`, `ModelRouter.call()`, `ModelRouter.stream()`, `ModelRouter.list_available_models()`.
- Proves: static and dynamically discovered `google_genai` targets use native Gemini transport for text, tools, streaming, usage, and tool-result continuation.
- Production Router changes are allowed only if the RED tests expose a provider-neutral bug; do not add a Google branch to Router call/stream.

- [x] **Step 1: Write failing Router transport assertions**

In `test_model_router_transport.py`, create a Google catalog target and monkeypatch `litellm.acompletion` with one async recorder. Assert both `call()` and `stream()` receive:

```python
assert captured[0]["model"] == "gemini/gemini-3.5-flash"
assert captured[0]["api_base"] == "https://generativelanguage.googleapis.com/v1beta"
assert captured[0]["api_key"] == "selected-google-secret"
assert captured[0]["extra_headers"] == {"X-Tenant": "tenant-a"}
```

Also assert an explicit supported reasoning effort reaches `reasoning_effort`, while an undeclared effort fails before `litellm.acompletion`.

- [x] **Step 2: Verify the Router unit RED state**

Run:

```bash
NAUMI_MODELS__API_KEY=unit-test-placeholder uv run python -m pytest -q \
  tests/unit/test_model_router_transport.py \
  -k 'google_genai'
```

Expected: FAIL before Task 2 is present; after Task 2 it may pass without Router production changes. Record that as proof the provider-neutral boundary works; do not invent a change merely to make the task look larger.

- [x] **Step 3: Create the local Google HTTP handler**

In `test_google_genai_loopback.py`, use `ThreadingHTTPServer` and capture `(method, path, headers, body)`. Implement:

- `GET /v1beta/models`: return a Google models envelope with one `generateContent` model and one embedding-only model;
- `POST /v1beta/models/gemini-loopback:generateContent`: return text, functionCall, or final text depending on request parts;
- `POST /v1beta/models/gemini-loopback:streamGenerateContent?alt=sse`: emit `data: <GenerateContentResponse>\n\n` chunks for text and functionCall;
- every response uses bounded static JSON and no external calls.

Use these Google response shapes:

```python
def _text_response(text: str, *, prompt: int, output: int) -> dict[str, Any]:
    return {
        "candidates": [{
            "content": {"role": "model", "parts": [{"text": text}]},
            "finishReason": "STOP",
            "index": 0,
        }],
        "usageMetadata": {
            "promptTokenCount": prompt,
            "candidatesTokenCount": output,
            "totalTokenCount": prompt + output,
        },
        "modelVersion": "gemini-loopback",
    }


def _tool_response() -> dict[str, Any]:
    return {
        "candidates": [{
            "content": {"role": "model", "parts": [{
                "functionCall": {
                    "name": "file_read",
                    "args": {"path": "README.md"},
                },
            }]},
            "finishReason": "STOP",
            "index": 0,
        }],
        "usageMetadata": {
            "promptTokenCount": 7,
            "candidatesTokenCount": 4,
            "totalTokenCount": 11,
        },
    }
```

The handler must select a final response when any request part contains `functionResponse`; this proves the second tool turn rather than only parsing a synthetic first response.

- [x] **Step 4: Write the real end-to-end test**

Build a native catalog with no static models, `discovery.enabled=true`, explicit loopback `/v1beta` base, and `GEMINI_LOOPBACK_KEY`. Then:

```python
listing = await router.list_available_models("google", refresh=True)
text = await router.call([{"role": "user", "content": "你好"}])
tool = await router.call(
    [{"role": "user", "content": "读取 README"}],
    tools=TOOLS,
)
tool_call = tool.tool_calls[0]
final = await router.call([
    {"role": "user", "content": "读取 README"},
    {"role": "assistant", "content": None, "tool_calls": [tool_call]},
    {
        "role": "tool",
        "tool_call_id": tool_call["id"],
        "name": "file_read",
        "content": "# NaumiAgent",
    },
], tools=TOOLS)
chunks = [chunk async for chunk in router.stream(
    [{"role": "user", "content": "流式回答"}]
)]
```

Assert:

```python
assert [model.id for model in listing[0].models] == ["gemini-loopback"]
assert text.content == "loopback-ok"
assert text.usage.total_tokens == 8
assert tool_call["function"]["name"] == "file_read"
assert json.loads(tool_call["function"]["arguments"]) == {"path": "README.md"}
assert final.content == "tool-result-ok"
assert "".join(chunk.token for chunk in chunks) == "流式成功"
assert any(chunk.usage and chunk.usage.total_tokens > 0 for chunk in chunks)
assert all(
    request.headers.get("x-goog-api-key") == "loopback-google-secret"
    for request in inference_requests
)
assert not any(
    "ambient-must-not-win" in json.dumps(request.body)
    for request in requests
)
```

Inspect captured request bodies to prove system/user parts, function declarations, functionCall and functionResponse were sent in Google native shape. Assert no `stream_options`, secret reference, or discovery metadata appears in inference bodies.

- [x] **Step 5: Verify RED against the real LiteLLM path**

Run:

```bash
NAUMI_MODELS__API_KEY=unit-test-placeholder uv run python -m pytest -q \
  tests/integration/test_google_genai_loopback.py
```

Expected before Tasks 1-3: transport/discovery failure. If the first run reveals an exact LiteLLM path or valid response-field difference, update only the local handler to the installed dependency's documented contract; do not weaken product assertions.

- [x] **Step 6: Make only provider-neutral Router corrections exposed by RED**

The real loopback exposed that LiteLLM treats a dynamically discovered unknown Gemini model as not supporting system messages. `ProviderTransport` now carries immutable model-registration metadata and Router registers any transport-declared capability through one generic, locked, idempotent path. No `provider.id == "google"` branch was added to `call()` or `stream()`; failures are Chinese, redacted, and occur before network I/O.

- [x] **Step 7: Verify GREEN and focused regressions**

Run:

```bash
NAUMI_MODELS__API_KEY=unit-test-placeholder uv run python -m pytest -q \
  tests/unit/test_provider_catalog.py \
  tests/unit/test_provider_runtime.py \
  tests/unit/test_model_discovery.py \
  tests/unit/test_model_router.py \
  tests/unit/test_model_router_transport.py \
  tests/integration/test_google_genai_loopback.py \
  -k 'google or transport or dynamic or reasoning_effort'
uv run ruff check src/naumi_agent/model/catalog.py \
  src/naumi_agent/model/provider_runtime.py \
  src/naumi_agent/model/discovery.py \
  src/naumi_agent/model/router.py \
  tests/unit/test_provider_catalog.py \
  tests/unit/test_provider_runtime.py \
  tests/unit/test_model_discovery.py \
  tests/unit/test_model_router.py \
  tests/unit/test_model_router_transport.py \
  tests/integration/test_google_genai_loopback.py
git diff --check
```

Expected: selected tests and Ruff pass; no full suite runs.

- [x] **Step 8: Self-review and commit**

Review real path/query, x-goog auth, ambient key isolation, tool round-trip, stream completion, usage, dynamic overlay, model filtering, error redaction, server shutdown, and test determinism. Commit:

```bash
git add tests/unit/test_model_router_transport.py \
  tests/integration/test_google_genai_loopback.py \
  src/naumi_agent/model/router.py
git commit -m "fix: preserve Google system instructions"
```

Omit `src/naumi_agent/model/router.py` from `git add` when no production correction was necessary.

---

### Task 5: Active Documentation, Acceptance, Merge, and Push

**Files:**
- Modify: `docs/15-model-provider-configuration.md`
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-07-14-google-genai-adapter-design.md`
- Modify: `docs/superpowers/plans/2026-07-14-google-genai-adapter.md`

**Interfaces:**
- Consumes: implemented `google_genai` transport and discovery behavior.
- Produces: truthful user configuration, current support matrix, exact acceptance evidence, and next adapter order.

- [x] **Step 1: Update active configuration documentation**

Add the native JSON example from the design to `docs/15-model-provider-configuration.md`, including:

- `.naumi/config.yaml` with `models.catalog_path: providers.json` and `provider: google`;
- `.naumi/providers.json` with explicit `/v1beta` baseURL, env or provider-scoped credential reference, static alias, and discovery;
- `/models google --refresh` usage;
- `None` of the examples contain a real API key;
- explain that discovered IDs are current endpoint facts but context/pricing/reasoning metadata require static declarations until models.dev support lands.

Update README's support statement to list only actually executable formats: OpenAI-compatible Chat, OpenAI Responses, Anthropic Messages, and Google GenAI.

- [x] **Step 2: Mark design implemented only after focused acceptance**

Change design status to `已批准并实现` and add an evidence block with exact test counts, loopback scenarios, Ruff result, and the explicit statement that no Google external network, Keychain, or full suite ran.

- [x] **Step 3: Run the final focused Python acceptance**

Run the complete files for this small subsystem, not a broad repository suite:

```bash
NAUMI_MODELS__API_KEY=unit-test-placeholder uv run python -m pytest -q \
  tests/unit/test_provider_catalog.py \
  tests/unit/test_provider_runtime.py \
  tests/unit/test_model_discovery.py \
  tests/unit/test_model_targets.py \
  tests/unit/test_model_router.py \
  tests/unit/test_model_router_transport.py \
  tests/integration/test_google_genai_loopback.py
```

Record the exact pass count and duration in the design and this plan.

- [x] **Step 4: Run final Ruff, compile, and diff checks**

```bash
uv run ruff check src/naumi_agent/model/catalog.py \
  src/naumi_agent/model/provider_runtime.py \
  src/naumi_agent/model/discovery.py \
  src/naumi_agent/model/router.py \
  tests/unit/test_provider_catalog.py \
  tests/unit/test_provider_runtime.py \
  tests/unit/test_model_discovery.py \
  tests/unit/test_model_targets.py \
  tests/unit/test_model_router.py \
  tests/unit/test_model_router_transport.py \
  tests/integration/test_google_genai_loopback.py
uv run python -m py_compile \
  src/naumi_agent/model/catalog.py \
  src/naumi_agent/model/provider_runtime.py \
  src/naumi_agent/model/discovery.py \
  src/naumi_agent/model/router.py
git diff --check
```

- [x] **Step 5: Manual real configuration smoke**

In a temporary directory, create `.naumi/config.yaml` and `providers.json` pointing to the loopback server, set only a temporary env key, then run the shared model-list and one Router call. Confirm:

- `/models google --refresh` shows only `generateContent` models;
- runtime identity is `provider=google`, `api_format=google_genai`;
- inference reaches `:generateContent` and returns a normalized response;
- temporary files/state are removed and live `.naumi`/Keychain are unchanged.

- [x] **Step 6: Multi-round final review**

Review at least:

- Does any path read ambient Google/OpenAI/Anthropic credentials after explicit catalog auth?
- Can model ID, base URL, discovery path, static header, or response body inject a path/query/header?
- Are inference and discovery both bounded, timed out, and redacted?
- Does Google discovery list only models that can serve Agent chat?
- Can a discovered model immediately execute through the same target/transport?
- Do text, tools, tool results, streaming, usage, reasoning and finish reasons survive normalization?
- Do `/models`, REST, new UI and TUI still share one listing without Google-specific frontend logic?
- Did the slice accidentally implement Vertex, Interactions, Azure, Ollama, or hardcoded model versions?
- Are docs current and are historical design documents clearly separated from active user docs?

- [x] **Step 7: Commit documentation**

```bash
git add docs/15-model-provider-configuration.md README.md \
  docs/superpowers/specs/2026-07-14-google-genai-adapter-design.md \
  docs/superpowers/plans/2026-07-14-google-genai-adapter.md
git commit -m "docs: document Google GenAI provider support"
```

- [ ] **Step 8: Fast-forward merge, verify on main, and push**

Fetch `origin/main`; if remote advanced, inspect and integrate it without discarding user changes. Fast-forward merge the feature branch into `main`, rerun only Steps 3-4 on merged `main`, push `origin/main`, and preserve external worktrees plus `.superpowers/`.

## Plan Self-Review

Final implementation evidence before merge:

- targeted acceptance: `225 passed in 3.46s` across the seven provider/Router files;
- Ruff, four-module `py_compile`, and `git diff --check`: passed;
- temporary real `.naumi/config.yaml` + `providers.json` loopback smoke: passed and cleaned;
- review-discovered correction: native catalog now exposes positive `requestTimeoutMs` /
  `request_timeout_ms`, so the transport timeout is user-configurable instead of test-only;
- no Google external request, real Keychain access, live `.naumi` mutation, subagent, or full suite.

- Spec coverage: catalog auth, explicit native transport, standard/custom/none auth, model normalization, Google discovery, dynamic routing, call, stream, tools, tool results, usage, reasoning, UI/TUI list reuse, docs and real loopback all have an owner.
- Scope: Vertex AI, Interactions, OpenAI compatibility, Azure, Ollama inference, media APIs, built-in provider defaults and new picker remain explicit follow-ups.
- Security: no ambient key fallback, plaintext catalog secret, raw response error, path/query model injection, unbounded discovery response, or startup eager fetch is introduced.
- Type consistency: `build_google_genai_transport()` returns immutable `ProviderTransport` plus optional immutable registration metadata; discovery returns existing `_ParsedModels`/`ProviderModelListing`; Router continues consuming `ResolvedModelTarget`.
- Fidelity: the plan requires real local HTTP through installed LiteLLM, including a complete tool continuation, instead of treating a kwargs mapping test as protocol support.
- UX: existing `/models`/REST/new UI/TUI data path is reused, failure remains Chinese/actionable, stale lists remain usable, and no API key is displayed.
- Performance: existing per-provider TTL, negative cache, single-flight and list-all concurrency remain unchanged; no startup enumeration is added.
- User constraint: execution is inline, one reviewable capability at a time, with only targeted small-module verification and no full suite.
